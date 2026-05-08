"""Talker tool registry, v1 tool specs, dispatch surface (Story 4.4).

The Talker (in :mod:`turn.talker`) becomes tool-using in Story 4.4 —
it surfaces a small fixed set of tools to the LLM via the openai SDK's
``tools=`` parameter, parses the LLM's tool-call response into typed
:class:`ToolCall` instances, and hands them to a :class:`ToolRegistry`
for validate-then-dispatch. This module owns:

- :class:`ToolCall` — the typed shape the Talker parses from the
  openai SDK's response. Mirrors openai's ``ChatCompletionMessageToolCall``
  but uses a parsed-JSON ``arguments`` dict (the SDK ships a JSON
  string).
- :class:`ToolSpec` — frozen pydantic model holding a tool's name,
  description (LLM-facing), input schema (pydantic v2 model class),
  and async dispatch closure.
- :class:`ToolRegistry` — holder of ``list[ToolSpec]`` indexed by name.
  Two surfaces: :meth:`dispatch` (validate-then-call), and
  :meth:`as_openai_tools_param` (formats specs as openai's tools=
  parameter shape).
- :func:`make_go_to_sleep_tool`, :func:`make_set_mood_tool` — factory
  functions returning configured :class:`ToolSpec` instances. Closures
  capture the FSM / mood-controller references so the registry stays
  decoupled from the call sites.
- :func:`build_tool_registry` — the single construction site for the
  v1 registry. Mirrors :func:`build_talker` in ``turn/__init__.py``.

Dispatch contract (architecture.md §"Tool-call validation"):

    Inside ``ToolRegistry.dispatch``:
    1. Look up the spec by name. Unknown name → log WARN + return.
    2. ``input_schema.model_validate(tool_call.arguments)`` →
       ``ValidationError`` → log WARN + return.
    3. ``await spec.dispatch(validated)`` — propagate any exception.
       Internal sinks (:class:`ActivityFSM`, :class:`MoodController`)
       are first-party code; their bugs should crash, not be silently
       caught (CLAUDE.md rule #4 + architecture's no-catch contract on
       ``ExternalServiceError`` / first-party programming errors).

Text-first parallel-tools dispatch (FR45 / FR46):

The :class:`TurnDispatchProcessor` in ``pipeline.py`` emits the
``TalkerResponseFrame(text)`` to the splitter BEFORE awaiting any tool
dispatch. Tool calls run via ``asyncio.create_task`` — fire-and-forget
with a done-callback that logs any exception. **Text always plays
first**; the user hears the goodbye before mic mode flips. See
``pipeline.py:TurnDispatchProcessor.process_frame`` for the call site.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from voice_agent_pipeline.activity.machine import ActivityFSM
from voice_agent_pipeline.config.setup import ToolsConfig
from voice_agent_pipeline.mood.controller import MoodController
from voice_agent_pipeline.schemas.mood_event import Mood

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Typed message shapes — ToolCall (input from LLM) and ToolSpec (registry entry)
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """Typed shape parsed from openai SDK's ``ChatCompletionMessageToolCall``.

    The SDK ships ``arguments`` as a JSON string; the Talker parses it
    to a ``dict[str, Any]`` BEFORE constructing this model so the
    registry's input-schema validation works against parsed JSON,
    not raw text. JSON parse errors live in the Talker — they drop
    the specific tool call but don't crash the turn.

    Attributes:
        id: openai's per-call identifier. v1 doesn't use it (the
            tool-results loop is v2 territory), but we keep the
            field for forward-compat — when a future story adds the
            results loop, the call site has the id ready.
        name: Tool name as the LLM emitted it (e.g. ``"go_to_sleep"``).
            Looked up against :class:`ToolRegistry`'s name index.
        arguments: Parsed JSON arguments. Validated against
            :attr:`ToolSpec.input_schema` before dispatch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    arguments: dict[str, Any]


class ToolSpec(BaseModel):
    """One tool's full definition — LLM surface + validation + dispatch.

    Frozen so registries can't be mutated after construction (the v1
    set is fixed at startup; future hot-reload work would clone, not
    mutate). ``arbitrary_types_allowed=True`` is required because
    pydantic doesn't have built-in serializers for ``Callable`` or
    ``type[BaseModel]``. **Internal/in-process state, never serialized**
    — the model is registry plumbing, not a wire payload — so the
    arbitrary-types relaxation is safe here. Architecture.md's
    "no plain dicts at boundaries" / "use pydantic.BaseModel" rules
    don't constrain in-process plumbing types.

    Attributes:
        name: Exact openai tool name (the LLM emits this in
            ``ChatCompletionMessageToolCall.function.name``).
        description: LLM-facing help text. Surfaced via
            :meth:`ToolRegistry.as_openai_tools_param`. Helps the
            LLM decide when to call the tool — write it as if
            describing the tool to a smart-but-naive operator.
        input_schema: pydantic v2 model class used to validate the
            tool's ``arguments``. Empty models (no fields) are valid
            for tools that take no arguments (e.g. ``go_to_sleep``).
        dispatch: Async callable invoked with the validated input
            instance. Returns ``None``; side-effect-only (the v1
            tools mutate FSM state or publish events — the LLM's
            text response is the user-facing reply). Errors propagate
            (no v1 catch on first-party sinks).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str
    description: str
    input_schema: type[BaseModel]
    dispatch: Callable[[BaseModel], Awaitable[None]]


# ---------------------------------------------------------------------------
# Per-tool input schemas — validated before dispatch
# ---------------------------------------------------------------------------


class GoToSleepInput(BaseModel):
    """Input model for the ``go_to_sleep`` tool — empty by design.

    The LLM doesn't pass arguments to "go to sleep"; the FSM's
    deferred-sleep flag-flip needs no parameters. Empty pydantic
    models with ``extra="forbid"`` reject any keys the LLM might
    hallucinate, so an LLM that emits ``{"reason": "user said
    goodnight"}`` triggers a clean :class:`ValidationError` (caught
    by :meth:`ToolRegistry.dispatch`, dropped with WARN).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class SetMoodInput(BaseModel):
    """Input model for the ``set_mood`` tool — single ``mood`` field.

    Bounded by the :data:`Mood` Literal in
    :mod:`schemas.mood_event`. An LLM emitting ``{"mood": "ecstatic"}``
    (not in the enum) triggers :class:`ValidationError` and the call
    is silently dropped. The architecture's mood enum lifecycle
    (architecture.md §"Mood enum lifecycle") makes this Literal the
    single source of truth — the prompt and the registry both see the
    same set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mood: Mood


# ---------------------------------------------------------------------------
# Tool factories — closures capture the call-site references
# ---------------------------------------------------------------------------


def make_go_to_sleep_tool(activity_fsm: ActivityFSM) -> ToolSpec:
    """Construct the ``go_to_sleep`` :class:`ToolSpec`.

    The closure captures ``activity_fsm`` so the registry doesn't
    need to know about FSM internals — it just calls ``await spec.
    dispatch(validated)``. The dispatch flips ``ActivityFSM
    ._sleep_pending`` via the public ``on_tool_call_go_to_sleep``
    method (sync — the FSM doesn't publish on this call; the
    deferred transition publishes later when ``on_last_audio_frame``
    fires).
    """

    async def _dispatch(_input: BaseModel) -> None:
        # Sync FSM call — no ``await``. ``on_tool_call_go_to_sleep``
        # only flips a flag; no publish, no transition. The deferred
        # transition fires later in ``ActivityFSM.on_last_audio_frame``.
        del _input  # GoToSleepInput is empty; nothing to read.
        activity_fsm.on_tool_call_go_to_sleep()

    return ToolSpec(
        name="go_to_sleep",
        description=(
            "Schedule OLAF to go to sleep after the current response finishes "
            "playing. Use when the user says goodbye or asks OLAF to sleep. The "
            "audio response is delivered first; the system flips to wake-word-only "
            "mode after the last word."
        ),
        input_schema=GoToSleepInput,
        dispatch=_dispatch,
    )


def make_set_mood_tool(mood_controller: MoodController) -> ToolSpec:
    """Construct the ``set_mood`` :class:`ToolSpec`.

    Closure captures ``mood_controller`` for the publish-then-mutate
    path. The cooldown enforcement (NFR31, ≤4 publishes/hour) lives
    inside :meth:`MoodController.set` — over-rate calls drop with a
    ``mood.publish_dropped`` WARN, but the LLM's text response still
    flows because the dispatch already succeeded from the registry's
    POV (the WARN is at the controller layer).
    """

    async def _dispatch(input_: BaseModel) -> None:
        # The dispatch closure is typed as accepting any ``BaseModel``
        # to satisfy :attr:`ToolSpec.dispatch`'s
        # ``Callable[[BaseModel], Awaitable[None]]`` signature. The
        # registry only ever calls this with a ``SetMoodInput``
        # because validation happens upstream against
        # :attr:`ToolSpec.input_schema` — but pyright can't prove
        # that without a runtime guard. The ``isinstance`` check
        # acts as the recognized pyright type-narrowing pattern
        # *and* gives runtime safety. ``assert isinstance`` would
        # be cleaner but ruff S101 forbids ``assert`` in
        # production code (asserts strip under ``python -O``); the
        # ``if/raise`` form is the explicit equivalent.
        if not isinstance(input_, SetMoodInput):
            raise TypeError(
                f"set_mood dispatch received {type(input_).__name__}, expected SetMoodInput",
            )
        await mood_controller.set(input_.mood, reason="talker_set_mood")

    return ToolSpec(
        name="set_mood",
        description=(
            "Update OLAF's current mood. Pick from the allowed values; the mood "
            "tints subsequent voice responses (e.g. 'playful' makes the next "
            "replies playful). Don't call this gratuitously — only when the "
            "user's intent clearly shifts mood."
        ),
        input_schema=SetMoodInput,
        dispatch=_dispatch,
    )


# ---------------------------------------------------------------------------
# Registry — name-indexed dispatcher + openai tools-param formatter
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Name-indexed holder of :class:`ToolSpec` instances.

    Two responsibilities:

    1. :meth:`dispatch` — validate-then-call for incoming
       :class:`ToolCall` instances from the Talker.
    2. :meth:`as_openai_tools_param` — format the registered specs
       as the openai SDK's ``tools=`` parameter shape so the Talker
       can pass them straight through.

    Construction-time-only: the spec list is captured into a name →
    spec dict at ``__init__``. v1 doesn't support hot-reload of the
    registry; rebuild + restart is the v1 path. The name index is a
    plain dict (not a frozenset / immutable mapping) because pyright
    doesn't gain anything from the extra ceremony at this scale —
    five entries max, none of which mutate.
    """

    def __init__(self, tools: list[ToolSpec]) -> None:
        """Index ``tools`` by name; preserve registration order.

        Args:
            tools: List of :class:`ToolSpec` instances. Duplicate names
                are NOT detected — the last wins. v1 callers go through
                :func:`build_tool_registry` which doesn't produce
                duplicates, so the dedup-on-conflict policy is
                irrelevant in practice. (A future story that surfaces
                dynamic tools could add a duplicate-name guard here.)
        """
        # Insertion-order dict — both the registration order (for tests'
        # ``as_openai_tools_param`` output ordering assertions) and the
        # dispatch lookup speed are O(1).
        self._tools: dict[str, ToolSpec] = {t.name: t for t in tools}

    def __len__(self) -> int:
        """Number of tools registered. Used in tests + sanity checks."""
        return len(self._tools)

    async def dispatch(self, tool_call: ToolCall) -> None:
        """Validate ``tool_call.arguments`` against the matching spec, then dispatch.

        Three failure modes, two distinct dispositions:

        1. **Unknown tool name.** Log WARN ``tool.dispatch_unknown_name``
           and return. The LLM may have hallucinated a tool name; we
           drop without raising so the rest of the turn (text emission)
           is unaffected. CLAUDE.md rule #4 doesn't apply because this
           isn't an external-service error.
        2. **Validation error on arguments.** Log WARN
           ``tool.dispatch_invalid_input`` (with truncated pydantic
           error excerpt — never the raw arguments, which could carry
           untrusted LLM output) and return. Same disposition as
           unknown name.
        3. **Internal sink failure** — e.g., ``MoodController.set``
           raises :class:`PublisherError`, or :class:`ActivityFSM`'s
           transition guard raises :class:`VoiceAgentError`. **Not
           caught here.** Propagates to the caller, which (in
           production) is :class:`TurnDispatchProcessor`'s
           ``asyncio.create_task`` background task. The done-callback
           logs the exception via ``log.exception``; the pipeline
           does NOT crash mid-utterance. v1 trade-off documented in
           the story spec's "Async-task done-callback" section.
        """
        spec = self._tools.get(tool_call.name)
        if spec is None:
            # Privacy: log only the (LLM-emitted) name. Never log
            # arguments — they could carry untrusted text.
            log.warning("tool.dispatch_unknown_name", name=tool_call.name)
            return

        try:
            validated = spec.input_schema.model_validate(tool_call.arguments)
        except ValidationError as exc:
            # Truncate the pydantic error excerpt to the first three
            # entries so a verbose validation report doesn't fill the
            # log. Never log the raw arguments dict — its values may
            # be LLM hallucinations carrying user-state echoes.
            log.warning(
                "tool.dispatch_invalid_input",
                tool=tool_call.name,
                error=str(exc.errors()[:3]),
            )
            return

        # Successful validation — log INFO, then dispatch. The
        # dispatch's exceptions propagate by design (see method
        # docstring). Logging happens BEFORE the await so the log
        # event lands even if dispatch raises.
        log.info("tool.dispatch", tool=tool_call.name)
        await spec.dispatch(validated)

    def as_openai_tools_param(self) -> list[dict[str, Any]]:
        """Format the registered specs as the openai SDK's ``tools=`` parameter.

        The shape openai accepts (``ChatCompletionToolParam``):

        ::

            [{
                "type": "function",
                "function": {
                    "name": "go_to_sleep",
                    "description": "...",
                    "parameters": {
                        "type": "object",
                        "properties": {...},
                        ...JSON Schema...
                    }
                }
            }, ...]

        ``model_json_schema()`` on a pydantic v2 model returns the
        JSON Schema. openai accepts that shape directly — no
        translation layer needed. Order = registration order = dict
        insertion order.

        Returns:
            A list ready to pass as ``tools=`` to
            ``client.chat.completions.create()``. Empty list when
            the registry is empty (valid; openai treats empty tools
            as "no tools available").
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema.model_json_schema(),
                },
            }
            for spec in self._tools.values()
        ]


# ---------------------------------------------------------------------------
# Top-level construction site — mirrors build_talker in turn/__init__.py
# ---------------------------------------------------------------------------


def build_tool_registry(
    config: ToolsConfig,
    activity_fsm: ActivityFSM,
    mood_controller: MoodController,
) -> ToolRegistry:
    """Construct the v1 :class:`ToolRegistry` from config + injected sinks.

    Mirrors :func:`build_talker`'s pattern in
    :mod:`voice_agent_pipeline.turn`: a single construction site so
    the dependency graph is documented in one place.

    Args:
        config: :class:`ToolsConfig` from
            :class:`SetupConfig.tools`. Each ``enable_*`` flag gates
            inclusion of one tool.
        activity_fsm: The :class:`ActivityFSM` instance ``go_to_sleep``
            dispatches to. Must already be constructed; the registry
            captures the reference into a closure.
        mood_controller: The :class:`MoodController` ``set_mood``
            dispatches to. Same lifetime requirement.

    Returns:
        A :class:`ToolRegistry` with the enabled tools registered in
        the order the v1 architecture specifies (``go_to_sleep`` first,
        then ``set_mood`` — matters for
        :meth:`ToolRegistry.as_openai_tools_param` ordering tests).
    """
    tools: list[ToolSpec] = []
    if config.enable_go_to_sleep:
        tools.append(make_go_to_sleep_tool(activity_fsm))
    if config.enable_set_mood:
        tools.append(make_set_mood_tool(mood_controller))
    return ToolRegistry(tools)


# :mod:`voice_agent_pipeline.turn` re-exports these from its own
# ``__all__``. The Talker (``turn/talker.py``) consumes
# :class:`ToolCall` + :class:`ToolRegistry` directly.
__all__ = [
    "GoToSleepInput",
    "SetMoodInput",
    "ToolCall",
    "ToolRegistry",
    "ToolSpec",
    "build_tool_registry",
    "make_go_to_sleep_tool",
    "make_set_mood_tool",
]
