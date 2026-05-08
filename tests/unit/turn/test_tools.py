"""Unit tests for :mod:`voice_agent_pipeline.turn.tools` (Story 4.4).

Covers:

- :class:`ToolSpec` construction.
- :class:`SetMoodInput` validation against the :data:`Mood` Literal.
- :class:`ToolRegistry` indexing by name + ``__len__``.
- :meth:`ToolRegistry.as_openai_tools_param` shape.
- :meth:`ToolRegistry.dispatch` happy path.
- :meth:`ToolRegistry.dispatch` validation failure (drop with WARN).
- :meth:`ToolRegistry.dispatch` unknown name (drop with WARN).
- :meth:`ToolRegistry.dispatch` propagates internal sink errors.
- ``go_to_sleep`` factory: dispatch flips ``ActivityFSM.sleep_pending``.
- ``set_mood`` factory: dispatch invokes ``MoodController.set``.
- :func:`build_tool_registry` factory respects enable flags.

Mock surface (CLAUDE.md rule #7 — only at Protocol boundaries):

- :class:`ActivityFSM` — DON'T mock. Real instance with
  :class:`LogEventPublisher`. The FSM is small + fast + has a public
  surface that's exactly the test surface.
- :class:`MoodController` — DON'T mock. Real instance + real
  :class:`MoodState` + :class:`LogEventPublisher`.
- :class:`EventPublisher` — :class:`LogEventPublisher` (in-memory
  adapter, ships from production code per architecture's
  "two publishers" pattern). Mocking the publisher Protocol would
  trigger the same anti-pattern.

Privacy / log assertions follow Story 3.6 / 4.3's caplog pattern.
"""

import asyncio

import pytest
import structlog
from pydantic import ValidationError

from voice_agent_pipeline.activity.machine import ActivityFSM
from voice_agent_pipeline.config.setup import ToolsConfig
from voice_agent_pipeline.errors import PublisherError
from voice_agent_pipeline.mood.controller import MoodController
from voice_agent_pipeline.mood.state import MoodState
from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher
from voice_agent_pipeline.turn.tools import (
    GoToSleepInput,
    SetMoodInput,
    ToolCall,
    ToolRegistry,
    ToolSpec,
    build_tool_registry,
    make_go_to_sleep_tool,
    make_set_mood_tool,
)

# ---------------------------------------------------------------------------
# Fixtures — real first-party objects per CLAUDE.md rule #7
# ---------------------------------------------------------------------------


@pytest.fixture
def publisher() -> LogEventPublisher:
    """In-memory publisher; the same one production uses for adapter='log'."""
    return LogEventPublisher()


@pytest.fixture
def mood_controller(publisher: LogEventPublisher) -> MoodController:
    """Real MoodController with a default-state cell and the log publisher."""
    state = MoodState(initial="calm")
    return MoodController(state, publisher, cooldown_publishes_per_hour=10)


@pytest.fixture
def activity_fsm(publisher: LogEventPublisher) -> ActivityFSM:
    """Real ActivityFSM started into ``sleeping`` state."""
    fsm = ActivityFSM(publisher=publisher)
    asyncio.run(fsm.start())
    return fsm


# ---------------------------------------------------------------------------
# ToolSpec / ToolCall / input schema tests
# ---------------------------------------------------------------------------


def test_tool_spec_construction() -> None:
    """A :class:`ToolSpec` carries name, description, schema, dispatch."""

    async def _noop(_input):  # type: ignore[no-untyped-def]
        return None

    spec = ToolSpec(
        name="dummy",
        description="does nothing",
        input_schema=GoToSleepInput,
        dispatch=_noop,
    )
    assert spec.name == "dummy"
    assert spec.description == "does nothing"
    assert spec.input_schema is GoToSleepInput


def test_tool_call_validates_against_input_schema() -> None:
    """SetMoodInput validation: in-Literal mood passes; out-of-Literal raises."""
    # Happy path — mood is in the Literal.
    valid = SetMoodInput.model_validate({"mood": "playful"})
    assert valid.mood == "playful"

    # Sad path — "ecstatic" isn't in the Mood Literal.
    with pytest.raises(ValidationError):
        SetMoodInput.model_validate({"mood": "ecstatic"})

    # Sad path — extra field is rejected (extra="forbid").
    with pytest.raises(ValidationError):
        SetMoodInput.model_validate({"mood": "happy", "extra": "nope"})


# ---------------------------------------------------------------------------
# ToolRegistry surface tests
# ---------------------------------------------------------------------------


def test_registry_construction_indexes_by_name(
    activity_fsm: ActivityFSM,
    mood_controller: MoodController,
) -> None:
    """A registry with two specs has ``len(registry) == 2``."""
    go_to_sleep = make_go_to_sleep_tool(activity_fsm)
    set_mood = make_set_mood_tool(mood_controller)
    registry = ToolRegistry([go_to_sleep, set_mood])
    assert len(registry) == 2


def test_as_openai_tools_param_emits_correct_format(
    activity_fsm: ActivityFSM,
    mood_controller: MoodController,
) -> None:
    """``as_openai_tools_param`` returns the openai SDK's expected ``tools=`` shape."""
    registry = ToolRegistry(
        [
            make_go_to_sleep_tool(activity_fsm),
            make_set_mood_tool(mood_controller),
        ]
    )
    params = registry.as_openai_tools_param()

    # Two tools, in registration order.
    assert len(params) == 2
    assert params[0]["type"] == "function"
    assert params[0]["function"]["name"] == "go_to_sleep"
    # ``description`` flows through unchanged from the spec.
    assert "sleep" in params[0]["function"]["description"].lower()
    # ``parameters`` is the JSON Schema from ``GoToSleepInput.model_json_schema()``.
    # Empty model: properties is empty dict (or missing), required is empty.
    go_to_sleep_schema = params[0]["function"]["parameters"]
    assert go_to_sleep_schema["type"] == "object"

    # set_mood spec: parameters has properties.mood.enum == Mood Literal values.
    set_mood_schema = params[1]["function"]["parameters"]
    assert params[1]["function"]["name"] == "set_mood"
    mood_enum = set_mood_schema["properties"]["mood"]["enum"]
    # The Mood Literal's values, surfaced via pydantic's JSON Schema emission.
    assert "playful" in mood_enum
    assert "calm" in mood_enum
    # Required field — the LLM has to provide ``mood``.
    assert "mood" in set_mood_schema["required"]


# ---------------------------------------------------------------------------
# Dispatch behavior — happy path + failure modes
# ---------------------------------------------------------------------------


def test_dispatch_happy_path_calls_dispatch_coroutine(
    activity_fsm: ActivityFSM,
    mood_controller: MoodController,
) -> None:
    """Validated input → dispatch invoked → side effect observed (mood updated)."""
    registry = ToolRegistry(
        [
            make_set_mood_tool(mood_controller),
        ]
    )

    asyncio.run(
        registry.dispatch(
            ToolCall(id="t1", name="set_mood", arguments={"mood": "playful"}),
        )
    )

    # Side effect: mood state updated, mood event published.
    assert mood_controller._state.current == "playful"


def test_dispatch_invalid_input_logs_warn_and_drops(
    mood_controller: MoodController,
) -> None:
    """Bad arguments → log WARN ``tool.dispatch_invalid_input``; no dispatch call."""
    registry = ToolRegistry([make_set_mood_tool(mood_controller)])

    starting_mood = mood_controller._state.current

    with structlog.testing.capture_logs() as captured:
        asyncio.run(
            registry.dispatch(
                ToolCall(id="t1", name="set_mood", arguments={"mood": "ecstatic"}),
            )
        )

    # No dispatch — mood is unchanged.
    assert mood_controller._state.current == starting_mood

    # WARN log surfaces with tool name + truncated error.
    matching = [r for r in captured if r.get("event") == "tool.dispatch_invalid_input"]
    assert len(matching) == 1
    rec = matching[0]
    assert rec.get("tool") == "set_mood"
    # ``error`` is the truncated pydantic error excerpt — we assert it
    # exists; the exact message format is pydantic's contract.
    assert rec.get("error") is not None


def test_dispatch_unknown_name_logs_warn_and_drops(
    activity_fsm: ActivityFSM,
) -> None:
    """Unknown tool name → log WARN ``tool.dispatch_unknown_name``; no raise."""
    registry = ToolRegistry([make_go_to_sleep_tool(activity_fsm)])

    with structlog.testing.capture_logs() as captured:
        asyncio.run(
            registry.dispatch(
                ToolCall(id="t1", name="nonexistent_tool", arguments={}),
            )
        )

    matching = [r for r in captured if r.get("event") == "tool.dispatch_unknown_name"]
    assert len(matching) == 1
    assert matching[0].get("name") == "nonexistent_tool"


def test_dispatch_internal_exception_propagates(
    activity_fsm: ActivityFSM,
    mood_controller: MoodController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal sink failures (e.g., PublisherError) propagate — registry doesn't catch.

    Architectural invariant (architecture.md §"Tool-call validation"):
    the registry catches :class:`ValidationError` (LLM bad output) but
    NOT exceptions from ``spec.dispatch``. First-party bugs should
    crash, not be silently swallowed.
    """

    # Make the underlying publisher raise PublisherError on publish.
    async def _explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise PublisherError(reason="simulated_failure")

    monkeypatch.setattr(mood_controller._publisher, "publish_mood", _explode)

    registry = ToolRegistry([make_set_mood_tool(mood_controller)])

    with pytest.raises(PublisherError):
        asyncio.run(
            registry.dispatch(
                ToolCall(id="t1", name="set_mood", arguments={"mood": "playful"}),
            )
        )


# ---------------------------------------------------------------------------
# Per-tool factory behavior — go_to_sleep + set_mood side effects
# ---------------------------------------------------------------------------


def test_go_to_sleep_dispatch_invokes_fsm_method(
    activity_fsm: ActivityFSM,
) -> None:
    """``go_to_sleep`` dispatch flips ``ActivityFSM.sleep_pending`` to True.

    The FSM's ``on_tool_call_go_to_sleep`` is sync (it only sets a
    flag; no publish, no transition until ``on_last_audio_frame``
    fires). The async dispatch closure wraps the sync call so it
    fits the registry's ``Callable[[BaseModel], Awaitable[None]]``
    signature.
    """
    registry = ToolRegistry([make_go_to_sleep_tool(activity_fsm)])

    # Pre: not pending.
    assert activity_fsm.sleep_pending is False

    asyncio.run(
        registry.dispatch(
            ToolCall(id="t1", name="go_to_sleep", arguments={}),
        )
    )

    # Post: pending. The transition itself doesn't happen here —
    # that's the deferred-sleep contract (Story 4.3 / FR46).
    assert activity_fsm.sleep_pending is True


def test_set_mood_dispatch_invokes_mood_controller_set(
    mood_controller: MoodController,
) -> None:
    """``set_mood`` dispatch updates ``MoodState.current`` via the controller."""
    registry = ToolRegistry([make_set_mood_tool(mood_controller)])

    starting_mood = mood_controller._state.current
    target_mood = "happy" if starting_mood != "happy" else "playful"

    asyncio.run(
        registry.dispatch(
            ToolCall(id="t1", name="set_mood", arguments={"mood": target_mood}),
        )
    )

    assert mood_controller._state.current == target_mood


# ---------------------------------------------------------------------------
# build_tool_registry factory — config flag wiring
# ---------------------------------------------------------------------------


def test_build_tool_registry_factory_respects_enable_flags(
    activity_fsm: ActivityFSM,
    mood_controller: MoodController,
) -> None:
    """``build_tool_registry`` honors ``[tools] enable_*`` flags."""
    # Only set_mood — go_to_sleep is gated off.
    config = ToolsConfig(enable_go_to_sleep=False, enable_set_mood=True)
    registry = build_tool_registry(config, activity_fsm, mood_controller)
    assert len(registry) == 1
    params = registry.as_openai_tools_param()
    assert params[0]["function"]["name"] == "set_mood"

    # Both disabled → empty registry.
    config_none = ToolsConfig(enable_go_to_sleep=False, enable_set_mood=False)
    registry_none = build_tool_registry(config_none, activity_fsm, mood_controller)
    assert len(registry_none) == 0
    assert registry_none.as_openai_tools_param() == []

    # Both enabled (default) → both tools.
    config_both = ToolsConfig()
    registry_both = build_tool_registry(config_both, activity_fsm, mood_controller)
    assert len(registry_both) == 2
