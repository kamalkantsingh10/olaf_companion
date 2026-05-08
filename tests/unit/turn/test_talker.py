"""Unit tests for :mod:`voice_agent_pipeline.turn.talker`.

The ``openai`` module is mocked at the import boundary inside ``turn/talker.py``
(``monkeypatch.setattr(talker_module, "openai", _fake_openai)``) — same pattern
Story 1.7 used for ``faster_whisper``. Mocking the global ``openai`` package
would leak across tests; patching the imported reference inside the talker
module is the architecturally-correct way to honor the mock-at-Protocol-
boundaries rule (architecture.md §"Test Patterns").

Story 2.2 has a **single** concrete :class:`Talker` class serving all three
providers (OpenAI, Groq, Gemini) — they all reach the same ``openai`` SDK
via openai-compatible endpoints. The factory routing tests live in
``test_factory.py`` (provider→key→base_url dispatch); these tests pin the
Talker class's behaviour with provider-agnostic fixtures.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog
from pydantic import SecretStr

from voice_agent_pipeline.config.setup import TalkerConfig
from voice_agent_pipeline.errors import TalkerError
from voice_agent_pipeline.turn import talker as talker_module
from voice_agent_pipeline.turn.talker import Talker


@dataclass
class _StubMessage:
    """Mirror ``response.choices[0].message`` from openai's Chat Completions."""

    content: str | None = ""


@dataclass
class _StubChoice:
    """Mirror ``response.choices[0]`` from openai's Chat Completions."""

    message: _StubMessage = None  # type: ignore[assignment]


@dataclass
class _StubUsage:
    """Mirror ``response.usage`` — token counts for the talker.completion log."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class _StubResponse:
    """Mirror the slice of ``ChatCompletion`` we actually use.

    Real ChatCompletion has many fields; we only consume choices[0]
    .message.content and usage. Keep the fake minimal so tests don't
    accidentally depend on unrelated SDK fields.
    """

    choices: list[_StubChoice]
    usage: _StubUsage | None = None


# Stand-in for ``openai.APIError`` inside the patched module.
class _FakeAPIError(Exception):
    """Stand-in for ``openai.APIError`` — must be the same class instance the
    Talker's except-clause matches against."""


def _make_fake_openai(
    response_text: str = "",
    raise_error: Exception | None = None,
    capture_kwargs: dict[str, Any] | None = None,
    capture_init_kwargs: dict[str, Any] | None = None,
    usage: _StubUsage | None = None,
) -> MagicMock:
    """Build a fake replacement for the ``openai`` module.

    Args:
        response_text: What ``chat.completions.create`` returns as the
            assistant message ``content`` on success.
        raise_error: If non-None, ``chat.completions.create`` raises
            this exception instead of returning a stub. Use with
            :class:`_FakeAPIError`.
        capture_kwargs: If provided, the ``chat.completions.create``
            call's kwargs are recorded into this dict for later
            assertion.
        capture_init_kwargs: If provided, the ``AsyncOpenAI(...)`` ctor
            kwargs are recorded into this dict (used to assert that
            ``base_url`` was correctly threaded through).
        usage: Stub :class:`_StubUsage` for the response. ``None``
            simulates a provider that omits usage info — the Talker
            should NOT log ``talker.completion`` in that case.

    Returns:
        A :class:`MagicMock` shaped like the ``openai`` module —
        exposes ``AsyncOpenAI``, ``APIError``, plus internal hooks the
        patched :class:`Talker` uses.
    """
    fake_client = MagicMock()

    async def _create(**kwargs: Any) -> _StubResponse:
        if capture_kwargs is not None:
            capture_kwargs.update(kwargs)
        if raise_error is not None:
            raise raise_error
        return _StubResponse(
            choices=[_StubChoice(message=_StubMessage(content=response_text))],
            usage=usage,
        )

    fake_client.chat.completions.create = _create

    def _construct_client(**init_kwargs: Any) -> Any:
        if capture_init_kwargs is not None:
            capture_init_kwargs.update(init_kwargs)
        return fake_client

    fake_module = MagicMock()
    fake_module.AsyncOpenAI = _construct_client
    # Talker catches openai.APIError specifically; the patched module's
    # APIError must be the SAME exception class the test raises, otherwise
    # the except-clause won't match and the test crashes with a stray
    # exception instead of a TalkerError.
    fake_module.APIError = _FakeAPIError
    return fake_module


@pytest.fixture
def system_prompt_file(tmp_path: Path) -> Path:
    """Write a stub system prompt under tmp_path; return its path."""
    p = tmp_path / "talker_system.md"
    p.write_text("You are OLAF.", encoding="utf-8")
    return p


@pytest.fixture
def talker_config(system_prompt_file: Path) -> TalkerConfig:
    """A TalkerConfig pointing at the temp prompt — the test default."""
    return TalkerConfig(
        provider="openai",
        max_tokens=128,
        system_prompt_path=system_prompt_file,
    )


def test_complete_returns_response_text(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: complete() returns the assistant's plain-text reply."""
    fake_openai = _make_fake_openai(response_text="It's just past three o'clock.")
    monkeypatch.setattr(talker_module, "openai", fake_openai)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    result = asyncio.run(talker.complete("what time is it?"))

    assert result == "It's just past three o'clock."


def test_complete_passes_model_system_and_user_messages(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Chat Completions call shape matches: model + system+user messages + max_tokens."""
    captured: dict[str, Any] = {}
    fake_openai = _make_fake_openai(response_text="hi", capture_kwargs=captured)
    monkeypatch.setattr(talker_module, "openai", fake_openai)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    asyncio.run(talker.complete("hello"))

    assert captured["model"] == "gpt-5.4-nano"
    # Messages array: system prompt first, user transcript second.
    assert captured["messages"] == [
        {"role": "system", "content": "You are OLAF."},
        {"role": "user", "content": "hello"},
    ]
    # talker_config.max_tokens is 128 in the fixture.
    assert captured["max_tokens"] == 128


def test_complete_threads_base_url_into_client_construction(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``base_url`` (per-provider override) is passed to AsyncOpenAI's ctor.

    Pinning this contract guarantees the openai-compatible endpoint
    pattern — Talker pointed at a custom base_url goes there, not at
    OpenAI's default. The factory uses this to switch to Groq / Gemini.
    """
    init_kwargs: dict[str, Any] = {}
    fake_openai = _make_fake_openai(
        response_text="ok",
        capture_init_kwargs=init_kwargs,
    )
    monkeypatch.setattr(talker_module, "openai", fake_openai)

    Talker(
        talker_config,
        SecretStr("stub-key"),
        model="llama-3.1-8b-instant",
        base_url="https://api.groq.com/openai/v1",
    )

    assert init_kwargs["base_url"] == "https://api.groq.com/openai/v1"
    assert init_kwargs["api_key"] == "stub-key"


def test_complete_default_base_url_is_none(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``base_url`` is omitted, the SDK gets ``None`` (i.e., OpenAI default)."""
    init_kwargs: dict[str, Any] = {}
    fake_openai = _make_fake_openai(
        response_text="ok",
        capture_init_kwargs=init_kwargs,
    )
    monkeypatch.setattr(talker_module, "openai", fake_openai)

    Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")

    assert init_kwargs.get("base_url") is None


def test_complete_raises_talker_error_on_api_failure(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai.APIError gets wrapped as TalkerError with the cause chain preserved.

    Documents CLAUDE.md rule #4: TalkerError (an ExternalServiceError) must
    propagate, not be swallowed. The ``raise ... from e`` chain lets the
    operator see the original SDK error in post-mortem stack traces.
    """
    boom = _FakeAPIError("openai exploded")
    fake_openai = _make_fake_openai(raise_error=boom)
    monkeypatch.setattr(talker_module, "openai", fake_openai)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    with pytest.raises(TalkerError) as exc_info:
        asyncio.run(talker.complete("hello"))

    # Cause chain preserved + project-typed wrapping carries the model
    # + provider for log triage.
    assert exc_info.value.__cause__ is boom
    assert exc_info.value.context.get("model") == "gpt-5.4-nano"
    assert exc_info.value.context.get("provider") == "openai"
    assert "openai exploded" in exc_info.value.context.get("reason", "")


def test_init_reads_system_prompt_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The system prompt is read in __init__ — never per-turn.

    Spelling this out as a test pins the contract: prompt evolution
    flows through restart, not through file-watcher magic. Story 5.2's
    SIGHUP reload is for ``expression_map.yaml``, NOT for the Talker
    prompt.
    """
    prompt_path = tmp_path / "talker_system.md"
    prompt_path.write_text("ORIGINAL", encoding="utf-8")
    config = TalkerConfig(
        provider="openai",
        max_tokens=128,
        system_prompt_path=prompt_path,
    )

    captured: dict[str, Any] = {}
    fake_openai = _make_fake_openai(response_text="ok", capture_kwargs=captured)
    monkeypatch.setattr(talker_module, "openai", fake_openai)

    talker = Talker(config, SecretStr("stub-key"), model="gpt-5.4-nano")

    # Mutate the file AFTER construction — the prompt the Talker sends
    # should still be the ORIGINAL content, proving the read happened
    # at __init__ time.
    prompt_path.write_text("MODIFIED", encoding="utf-8")
    asyncio.run(talker.complete("hello"))

    # System message in the messages array should be the original.
    system_msg = next(m for m in captured["messages"] if m["role"] == "system")
    assert system_msg["content"] == "ORIGINAL"


def test_complete_accepts_context_kwarg_and_ignores_it_in_v1(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 4.1 will populate ``context``; v1 accepts but doesn't leak it.

    Pinning this contract in v1 means Story 4.1 can wire the
    belief-state read into ``context=`` without a Protocol signature
    change. v1's job is just "don't throw on the kwarg AND don't
    accidentally pass it through to the API call."
    """
    captured: dict[str, Any] = {}
    fake_openai = _make_fake_openai(response_text="ok", capture_kwargs=captured)
    monkeypatch.setattr(talker_module, "openai", fake_openai)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    asyncio.run(talker.complete("hi", context={"date": "2026-05-05"}))

    # The context dict should NOT have leaked into the API call kwargs —
    # Story 4.1 will define the canonical merge strategy when it lands.
    assert "context" not in captured
    assert "date" not in captured


def test_complete_logs_token_usage(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful complete() emits a ``talker.completion`` INFO log with token counts.

    Operator-side observability: at ~100 turns/day in production, you
    want per-turn token counts in voice-agent.log so cost / verbosity
    drift surfaces in the standard log feed without DEBUG. The log
    happens AFTER a successful call (failed calls raise, so no log).

    Uses ``structlog.testing.capture_logs`` because the Talker module
    routes through structlog's own logger; pytest's ``caplog`` only
    captures stdlib records.
    """
    import structlog

    fake_openai = _make_fake_openai(
        response_text="hi",
        usage=_StubUsage(prompt_tokens=42, completion_tokens=7, total_tokens=49),
    )
    monkeypatch.setattr(talker_module, "openai", fake_openai)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    with structlog.testing.capture_logs() as captured:
        asyncio.run(talker.complete("hello"))

    matching = [r for r in captured if r.get("event") == "talker.completion"]
    assert matching, f"expected talker.completion log; got: {captured!r}"
    rec = matching[0]
    assert rec.get("provider") == "openai"
    assert rec.get("model") == "gpt-5.4-nano"
    assert rec.get("prompt_tokens") == 42
    assert rec.get("completion_tokens") == 7
    assert rec.get("total_tokens") == 49


def test_complete_skips_token_log_when_usage_missing(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a provider omits ``response.usage``, the log line is skipped (not crashed).

    Documents the defensive guard around the usage log: not every
    openai-compatible endpoint populates usage identically. The Talker
    must keep returning the response text even if the observability
    hook can't fire.
    """
    import structlog

    fake_openai = _make_fake_openai(response_text="hi", usage=None)
    monkeypatch.setattr(talker_module, "openai", fake_openai)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    with structlog.testing.capture_logs() as captured:
        result = asyncio.run(talker.complete("hello"))

    assert result == "hi"
    matching = [r for r in captured if r.get("event") == "talker.completion"]
    assert not matching


# ---------------------------------------------------------------------------
# Story 4.4: complete_with_tools tests
# ---------------------------------------------------------------------------
#
# These exercise the tool-using surface added in Story 4.4. The fake
# openai module gains support for tool_calls in the response; the
# tests assert on belief grounding, tool-call parsing, JSON-error
# drop, content=None coercion, and the ``tool_call_count`` log field.


@dataclass
class _StubFunction:
    """Mirror ``ChatCompletionMessageFunctionToolCall.function`` from openai."""

    name: str
    arguments: str


@dataclass
class _StubToolCall:
    """Mirror ``ChatCompletionMessageFunctionToolCall``.

    The Talker's ``isinstance`` check uses
    ``ChatCompletionMessageFunctionToolCall`` from the openai SDK,
    which we can't easily fake — so the patched-in stub here uses
    monkeypatch to also redirect that isinstance class at the same
    module level. See ``_make_fake_openai_with_tools`` below.
    """

    id: str
    type: str
    function: _StubFunction


def _make_fake_openai_with_tools(
    response_text: str | None = "",
    tool_calls: list[_StubToolCall] | None = None,
    raise_error: Exception | None = None,
    capture_kwargs: dict[str, Any] | None = None,
    usage: _StubUsage | None = None,
) -> MagicMock:
    """Build a fake openai module that supports tool_calls in the response.

    Extends :func:`_make_fake_openai` for the tool-using surface. The
    Talker filters tool_calls by ``isinstance(tc,
    ChatCompletionMessageFunctionToolCall)``; we monkey-patch that
    class to :class:`_StubToolCall` in the test so the isinstance
    check accepts our stubs.
    """
    fake_client = MagicMock()

    async def _create(**kwargs: Any) -> _StubResponse:
        if capture_kwargs is not None:
            capture_kwargs.update(kwargs)
        if raise_error is not None:
            raise raise_error
        message = _StubMessage(content=response_text)
        # ``tool_calls`` is the openai SDK's name on
        # ``ChatCompletionMessage``; assigning the list directly is
        # what the Talker reads.
        message.tool_calls = tool_calls or []  # type: ignore[attr-defined]
        return _StubResponse(choices=[_StubChoice(message=message)], usage=usage)

    fake_client.chat.completions.create = _create

    def _construct_client(**init_kwargs: Any) -> Any:
        del init_kwargs
        return fake_client

    fake_module = MagicMock()
    fake_module.AsyncOpenAI = _construct_client
    fake_module.APIError = _FakeAPIError
    return fake_module


def _patch_tool_call_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkey-patch ``ChatCompletionMessageFunctionToolCall`` to ``_StubToolCall``.

    The Talker filters incoming tool_calls via ``isinstance(tc,
    ChatCompletionMessageFunctionToolCall)``. Tests construct
    :class:`_StubToolCall`; redirecting the isinstance reference
    inside the talker module lets the stubs flow through.
    """
    monkeypatch.setattr(
        talker_module,
        "ChatCompletionMessageFunctionToolCall",
        _StubToolCall,
    )


class _StubToolRegistry:
    """Stand-in for :class:`ToolRegistry` — only ``as_openai_tools_param`` is used.

    The Talker calls ``as_openai_tools_param()`` to build the openai
    SDK's ``tools=`` payload, then never touches the registry again
    (dispatch happens downstream in :class:`TurnDispatchProcessor`).
    These tests don't need a real registry — a stub returning a
    fixed list is enough.
    """

    def __init__(self, tools_param: list[dict[str, Any]] | None = None) -> None:
        self._tools_param: list[dict[str, Any]] = tools_param or []

    def as_openai_tools_param(self) -> list[dict[str, Any]]:
        return self._tools_param


def test_complete_with_tools_passes_tools_to_openai_sdk(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tools=`` kwarg matches ``registry.as_openai_tools_param()``; ``tool_choice="auto"``."""
    captured: dict[str, Any] = {}
    fake_openai = _make_fake_openai_with_tools(
        response_text="hi",
        capture_kwargs=captured,
    )
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    sentinel_tools = [{"type": "function", "function": {"name": "stub"}}]
    registry = _StubToolRegistry(sentinel_tools)
    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    asyncio.run(talker.complete_with_tools("hello", registry))  # type: ignore[arg-type]

    assert captured["tools"] == sentinel_tools
    assert captured["tool_choice"] == "auto"


def test_complete_with_tools_returns_text_only_when_no_tool_calls(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text-only response: ``TalkerResponse(text=..., tool_calls=[])``."""
    fake_openai = _make_fake_openai_with_tools(response_text="hi there")
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    response = asyncio.run(
        talker.complete_with_tools("hi", _StubToolRegistry()),  # type: ignore[arg-type]
    )

    assert response.text == "hi there"
    assert response.tool_calls == []


def test_complete_with_tools_returns_text_and_tool_calls(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text + tool_calls: parse openai's stub into a ``ToolCall`` list."""
    tool_calls = [
        _StubToolCall(
            id="t1",
            type="function",
            function=_StubFunction(name="go_to_sleep", arguments="{}"),
        ),
    ]
    fake_openai = _make_fake_openai_with_tools(
        response_text="goodnight",
        tool_calls=tool_calls,
    )
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    response = asyncio.run(
        talker.complete_with_tools("goodnight olaf", _StubToolRegistry()),  # type: ignore[arg-type]
    )

    assert response.text == "goodnight"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "t1"
    assert response.tool_calls[0].name == "go_to_sleep"
    # Empty arguments string was coerced to {} inside the Talker.
    assert response.tool_calls[0].arguments == {}


def test_complete_with_tools_handles_none_content(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``message.content = None`` (tools-only) → ``TalkerResponse.text == ""`` (not None)."""
    tool_calls = [
        _StubToolCall(
            id="t1",
            type="function",
            function=_StubFunction(name="go_to_sleep", arguments="{}"),
        ),
    ]
    fake_openai = _make_fake_openai_with_tools(
        response_text=None,  # openai returns None when only tools are emitted
        tool_calls=tool_calls,
    )
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    response = asyncio.run(
        talker.complete_with_tools("hi", _StubToolRegistry()),  # type: ignore[arg-type]
    )

    assert response.text == ""  # coerced from None
    assert len(response.tool_calls) == 1


def test_complete_with_tools_invalid_arguments_json_drops_call_warns(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad-JSON tool arguments → drop that call, log WARN, others flow through."""
    tool_calls = [
        # Malformed JSON — gets dropped with WARN.
        _StubToolCall(
            id="t1",
            type="function",
            function=_StubFunction(name="set_mood", arguments="not-json"),
        ),
        # Valid JSON — flows through.
        _StubToolCall(
            id="t2",
            type="function",
            function=_StubFunction(name="go_to_sleep", arguments="{}"),
        ),
    ]
    fake_openai = _make_fake_openai_with_tools(
        response_text="ok",
        tool_calls=tool_calls,
    )
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    with structlog.testing.capture_logs() as captured:
        response = asyncio.run(
            talker.complete_with_tools("hi", _StubToolRegistry()),  # type: ignore[arg-type]
        )

    # Only the valid tool call survived.
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "t2"
    # WARN log surfaces (with raw_length, never the malformed string).
    matching = [r for r in captured if r.get("event") == "talker.tool_call_invalid_json"]
    assert len(matching) == 1
    assert matching[0].get("tool") == "set_mood"
    assert matching[0].get("raw_length") == len("not-json")


def test_complete_with_tools_belief_grounding_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beliefs + grounded_keys configured → system prompt gets ``## Belief state`` section."""
    captured: dict[str, Any] = {}
    fake_openai = _make_fake_openai_with_tools(
        response_text="ok",
        capture_kwargs=captured,
    )
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    # System prompt file
    sp = tmp_path / "talker_system.md"
    sp.write_text("You are OLAF.", encoding="utf-8")
    config = TalkerConfig(
        provider="openai",
        max_tokens=128,
        system_prompt_path=sp,
        grounded_keys=["time", "calendar_today"],
    )

    # Mock BeliefStateClient with an AsyncMock-style read.
    class _StubBeliefs:
        async def read(self, keys: list[str]) -> dict[str, Any]:
            assert keys == ["time", "calendar_today"]
            return {"time": "08:47", "calendar_today": []}

    talker = Talker(
        config,
        SecretStr("stub-key"),
        model="gpt-5.4-nano",
        beliefs=_StubBeliefs(),  # type: ignore[arg-type]
    )
    asyncio.run(
        talker.complete_with_tools("hi", _StubToolRegistry()),  # type: ignore[arg-type]
    )

    # The system message should now include the belief context block.
    system_msg = captured["messages"][0]
    assert system_msg["role"] == "system"
    assert "## Belief state" in system_msg["content"]
    assert "08:47" in system_msg["content"]


def test_complete_with_tools_skips_belief_grounding_when_no_keys(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``grounded_keys=[]`` → BeliefStateClient.read NOT called; plain system prompt."""
    fake_openai = _make_fake_openai_with_tools(response_text="ok")
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    read_called = []

    class _StubBeliefs:
        async def read(self, keys: list[str]) -> dict[str, Any]:
            read_called.append(keys)
            return {}

    talker = Talker(
        talker_config,
        SecretStr("stub-key"),
        model="gpt-5.4-nano",
        beliefs=_StubBeliefs(),  # type: ignore[arg-type]
    )
    asyncio.run(
        talker.complete_with_tools("hi", _StubToolRegistry()),  # type: ignore[arg-type]
    )

    assert read_called == []  # no read; grounded_keys is empty


def test_complete_with_tools_skips_belief_grounding_when_beliefs_none(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``beliefs=None`` → no NPE, no read; plain system prompt."""
    fake_openai = _make_fake_openai_with_tools(response_text="ok")
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    talker = Talker(
        talker_config,
        SecretStr("stub-key"),
        model="gpt-5.4-nano",
        beliefs=None,  # explicit none — daemon disabled
    )
    # Just exercising the call — no exception means the None-check works.
    asyncio.run(
        talker.complete_with_tools("hi", _StubToolRegistry()),  # type: ignore[arg-type]
    )


def test_complete_with_tools_logs_completion_with_tool_call_count(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``talker.completion`` log gains ``tool_call_count`` field."""
    tool_calls = [
        _StubToolCall(
            id="t1",
            type="function",
            function=_StubFunction(name="go_to_sleep", arguments="{}"),
        ),
    ]
    usage = _StubUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    fake_openai = _make_fake_openai_with_tools(
        response_text="goodnight",
        tool_calls=tool_calls,
        usage=usage,
    )
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    with structlog.testing.capture_logs() as captured:
        asyncio.run(
            talker.complete_with_tools("hi", _StubToolRegistry()),  # type: ignore[arg-type]
        )

    matching = [r for r in captured if r.get("event") == "talker.completion"]
    assert len(matching) == 1
    assert matching[0].get("tool_call_count") == 1


def test_complete_with_tools_openai_error_raises_talker_error(
    talker_config: TalkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``openai.APIError`` → wrapped as :class:`TalkerError`."""
    boom = _FakeAPIError("rate limited")
    fake_openai = _make_fake_openai_with_tools(raise_error=boom)
    monkeypatch.setattr(talker_module, "openai", fake_openai)
    _patch_tool_call_class(monkeypatch)

    talker = Talker(talker_config, SecretStr("stub-key"), model="gpt-5.4-nano")
    with pytest.raises(TalkerError) as exc_info:
        asyncio.run(
            talker.complete_with_tools("hi", _StubToolRegistry()),  # type: ignore[arg-type]
        )
    assert exc_info.value.__cause__ is boom
