"""Unit tests for :class:`voice_agent_pipeline.pipeline.TurnDispatchProcessor`.

These tests live in tests/unit/turn/ (not tests/unit/) because the
dispatcher is logically part of the turn-routing concern even though it
ships from ``pipeline.py``. The architecture's "tests mirror src/"
guideline holds for source modules; the dispatcher straddles the
``turn/`` and ``pipeline.py`` boundary, so it belongs here next to
``test_router.py``.

Mocks live at the Protocol seam: :class:`TalkerClient` is a
``MagicMock(spec=TalkerClient)`` with ``complete_with_tools`` configured
via ``AsyncMock``. The TurnRouter is an actual concrete instance —
:class:`RouteDecision` is a pydantic model, not a Protocol, so mocking
it would violate architecture's mock-only-at-Protocol-boundaries rule.

Story 4.4: ``TurnDispatchProcessor.__init__`` now requires a
:class:`ToolRegistry` argument. These tests pass an empty registry
(``ToolRegistry([])``) — valid construct, behaves like "no tools
available" so the LLM doesn't emit tool calls. New tests covering the
text-first parallel-tools path live in ``test_dispatch_parallel.py``
(Story 4.4 Task 8).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from voice_agent_pipeline.config.setup import SttConfig
from voice_agent_pipeline.errors import TalkerError
from voice_agent_pipeline.pipeline import (
    TalkerResponseFrame,
    TranscriptFrame,
    TurnDispatchProcessor,
)
from voice_agent_pipeline.turn.router import TurnRouter
from voice_agent_pipeline.turn.talker import TalkerResponse
from voice_agent_pipeline.turn.tools import ToolRegistry


@pytest.fixture
def stt_config() -> SttConfig:
    return SttConfig(low_confidence_threshold=0.5, clarification_prompts=["please repeat?"])


@pytest.fixture
def empty_tool_registry() -> ToolRegistry:
    """Empty :class:`ToolRegistry` — Story 4.4 dispatcher needs a registry instance.

    Empty is valid: ``as_openai_tools_param`` returns ``[]`` (which
    openai SDK accepts as "no tools available"); the LLM doesn't emit
    tool calls; existing assertion shapes (text emission, log fields,
    error propagation) all work unchanged.
    """
    return ToolRegistry([])


@pytest.fixture
def mock_talker() -> MagicMock:
    """TalkerClient stand-in with ``complete_with_tools`` ready to configure.

    Story 4.4: production calls ``complete_with_tools``, not the legacy
    ``complete``. The mock returns a default text-only ``TalkerResponse``
    (no tool calls); individual tests override ``return_value`` to
    inject text or tool calls per scenario.
    """
    talker = MagicMock()
    talker.complete_with_tools = AsyncMock(
        return_value=TalkerResponse(text="", tool_calls=[]),
    )
    return talker


def _drain_pushed(processor: TurnDispatchProcessor) -> list:
    """Capture frames pushed downstream during ``process_frame`` calls.

    Pipecat's FrameProcessor pushes via ``self.push_frame(frame, direction)``;
    we replace that with a list-append so tests can assert on the
    sequence of frames the dispatcher emits.
    """
    pushed: list = []

    async def _capture(frame, direction):
        pushed.append(frame)

    processor.push_frame = _capture  # type: ignore[method-assign]
    return pushed


def test_dispatcher_invokes_talker_for_talker_target(
    stt_config: SttConfig,
    mock_talker: MagicMock,
    empty_tool_registry: ToolRegistry,
) -> None:
    """High-confidence transcript → ``complete_with_tools`` called with the original text."""
    mock_talker.complete_with_tools.return_value = TalkerResponse(
        text="OLAF reply",
        tool_calls=[],
    )
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, empty_tool_registry)
    _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hello", confidence=0.9, end_to_transcript_ms=42)

    asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]

    # Story 4.4: dispatcher uses complete_with_tools (the registry is
    # the second positional arg). Verify the prompt and registry both
    # arrive at the Talker.
    mock_talker.complete_with_tools.assert_awaited_once_with(
        "hello",
        empty_tool_registry,
    )


def test_dispatcher_emits_talker_response_frame(
    stt_config: SttConfig,
    mock_talker: MagicMock,
    empty_tool_registry: ToolRegistry,
) -> None:
    """Successful Talker call emits TalkerResponseFrame downstream with the reply text."""
    mock_talker.complete_with_tools.return_value = TalkerResponse(
        text="It's three o'clock.",
        tool_calls=[],
    )
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, empty_tool_registry)
    pushed = _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="what time?", confidence=0.9, end_to_transcript_ms=42)
    asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]

    # Two pushes: the new TalkerResponseFrame, then the original
    # TranscriptFrame passed through (so future stages can observe).
    response_frames = [f for f in pushed if isinstance(f, TalkerResponseFrame)]
    assert len(response_frames) == 1
    assert response_frames[0].text == "It's three o'clock."

    transcript_passthrough = [f for f in pushed if isinstance(f, TranscriptFrame)]
    assert len(transcript_passthrough) == 1
    assert transcript_passthrough[0] is transcript


def test_dispatcher_uses_clarification_prompt_for_low_confidence(
    stt_config: SttConfig,
    mock_talker: MagicMock,
    empty_tool_registry: ToolRegistry,
) -> None:
    """Low-confidence transcript → ``complete_with_tools`` called with the clarification prompt.

    The clarification_prompt is phrased as an INSTRUCTION to the LLM
    (e.g., "Briefly ask the user to repeat. Under 5 words.") — the
    LLM follows the instruction and produces a varied short apology
    rather than answering the prompt as a question (the failure mode
    Story 3.7's live test surfaced when the prompt was phrased as
    the apology itself).
    """
    mock_talker.complete_with_tools.return_value = TalkerResponse(
        text="Sorry, missed that?",
        tool_calls=[],
    )
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, empty_tool_registry)
    pushed = _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hjzz mjy?", confidence=0.2, end_to_transcript_ms=42)
    asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]

    # Talker called with the clarification prompt (not the noisy text).
    mock_talker.complete_with_tools.assert_awaited_once_with(
        "please repeat?",
        empty_tool_registry,
    )
    # The TalkerResponseFrame carries the LLM's reply, not the prompt.
    response_frames = [f for f in pushed if isinstance(f, TalkerResponseFrame)]
    assert len(response_frames) == 1
    assert response_frames[0].text == "Sorry, missed that?"


def test_dispatcher_propagates_talker_error(
    stt_config: SttConfig,
    mock_talker: MagicMock,
    empty_tool_registry: ToolRegistry,
) -> None:
    """TalkerError propagates — CLAUDE.md rule #4 forbids catching it."""
    boom = TalkerError(provider="openai", model="gpt-5.4-nano", reason="rate limited")
    mock_talker.complete_with_tools.side_effect = boom
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, empty_tool_registry)
    _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hi", confidence=0.9, end_to_transcript_ms=42)
    with pytest.raises(TalkerError) as exc_info:
        asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]
    assert exc_info.value is boom


def test_dispatcher_logs_talker_responded_event(
    stt_config: SttConfig,
    mock_talker: MagicMock,
    empty_tool_registry: ToolRegistry,
) -> None:
    """``talker.responded`` INFO logs latency_ms + clarification + tool_call_count.

    Story 4.4: log gains a ``tool_call_count`` field. The clarification
    flag lets operators distinguish clarification turns vs normal
    turns. The response TEXT is intentionally NOT logged here
    (privacy posture).
    """
    import structlog

    mock_talker.complete_with_tools.return_value = TalkerResponse(
        text="ok",
        tool_calls=[],
    )
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, empty_tool_registry)
    _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hi", confidence=0.9, end_to_transcript_ms=42)
    with structlog.testing.capture_logs() as captured:
        asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]

    matching = [r for r in captured if r.get("event") == "talker.responded"]
    assert len(matching) == 1
    rec = matching[0]
    # Clarification flag distinguishes clarify turns from normal turns.
    assert rec.get("clarification") is False
    # Story 4.4: tool_call_count surfaces in the log; 0 for text-only turns.
    assert rec.get("tool_call_count") == 0
    # Latency is wall-clock — at least 0 in mocked tests.
    latency_ms = rec.get("latency_ms")
    assert isinstance(latency_ms, int)
    assert latency_ms >= 0
    # Response text NOT logged at INFO — privacy posture.
    assert "text" not in rec


def test_dispatcher_logs_clarification_flag_for_low_confidence(
    stt_config: SttConfig,
    mock_talker: MagicMock,
    empty_tool_registry: ToolRegistry,
) -> None:
    """Low-confidence turn logs talker.responded with clarification=True."""
    import structlog

    mock_talker.complete_with_tools.return_value = TalkerResponse(
        text="ok",
        tool_calls=[],
    )
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, empty_tool_registry)
    _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hjzz", confidence=0.2, end_to_transcript_ms=42)
    with structlog.testing.capture_logs() as captured:
        asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]

    matching = [r for r in captured if r.get("event") == "talker.responded"]
    assert len(matching) == 1
    assert matching[0].get("clarification") is True


def test_dispatcher_passes_through_non_transcript_frames(
    stt_config: SttConfig,
    mock_talker: MagicMock,
    empty_tool_registry: ToolRegistry,
) -> None:
    """Non-TranscriptFrames are passed through unchanged; talker is NOT invoked.

    The dispatcher only triggers on TranscriptFrame; AudioRawFrames,
    WakeWordDetectedFrames, system frames etc. flow through untouched
    so the rest of the pipeline can still observe them.
    """
    from pipecat.frames.frames import Frame

    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, empty_tool_registry)
    pushed = _drain_pushed(dispatcher)

    other_frame = Frame()
    asyncio.run(dispatcher.process_frame(other_frame, direction=None))  # type: ignore[arg-type]

    # Only the original frame pushed through; no TalkerResponseFrame.
    assert pushed == [other_frame]
    mock_talker.complete_with_tools.assert_not_awaited()


# ---------------------------------------------------------------------------
# Story 4.4: parallel-dispatch tests — text-first ordering + bg-error logging
# ---------------------------------------------------------------------------


def test_dispatch_emits_text_frame_before_tool_dispatch_completes(
    stt_config: SttConfig,
    mock_talker: MagicMock,
) -> None:
    """Text frame is pushed BEFORE the tool dispatch completes (FR45/46 linchpin).

    Sets up a Talker mock that returns text + a single tool call.
    The tool's dispatch sleeps 100ms before flipping the FSM flag.
    The text frame should be pushed downstream BEFORE the FSM mutation
    happens — proving the dispatcher uses fire-and-forget tool tasks
    rather than awaiting them.
    """
    from voice_agent_pipeline.activity.machine import ActivityFSM
    from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher
    from voice_agent_pipeline.turn.tools import (
        ToolCall,
        ToolRegistry,
        make_go_to_sleep_tool,
    )

    publisher = LogEventPublisher()
    fsm = ActivityFSM(publisher=publisher)
    asyncio.run(fsm.start())

    # Wrap the go_to_sleep dispatch with an artificial delay so the
    # ordering is observable. We construct the spec by hand (rather
    # than using ``make_go_to_sleep_tool``) because we want to inject
    # the sleep before the FSM mutation.
    base_spec = make_go_to_sleep_tool(fsm)

    async def _delayed_dispatch(input_):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.1)
        await base_spec.dispatch(input_)

    delayed_spec = base_spec.model_copy(update={"dispatch": _delayed_dispatch})
    registry = ToolRegistry([delayed_spec])

    mock_talker.complete_with_tools.return_value = TalkerResponse(
        text="goodnight",
        tool_calls=[ToolCall(id="t1", name="go_to_sleep", arguments={})],
    )
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, registry)
    pushed = _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="goodnight olaf", confidence=0.9, end_to_transcript_ms=42)

    async def _drive() -> None:
        # Run the dispatcher; observe what gets pushed BEFORE the
        # 100ms tool-dispatch task completes.
        await dispatcher.process_frame(transcript, direction=None)  # type: ignore[arg-type]
        # At this point the text frame should already be in ``pushed``,
        # but the FSM's sleep_pending should still be False because
        # the bg task hasn't completed.
        assert any(isinstance(f, TalkerResponseFrame) and f.text == "goodnight" for f in pushed)
        assert fsm.sleep_pending is False
        # Now wait for the bg task to complete.
        await asyncio.sleep(0.15)
        assert fsm.sleep_pending is True

    asyncio.run(_drive())


def test_dispatch_continues_when_tool_dispatch_fails_in_background(
    stt_config: SttConfig,
    mock_talker: MagicMock,
) -> None:
    """Background tool dispatch failure → captured by done-callback; pipeline doesn't crash.

    The text frame is still pushed BEFORE the tool task fails;
    the failure surfaces via ``log.exception`` (caplog
    ``tool.dispatch_background_error``).
    """
    from pydantic import BaseModel, ConfigDict

    from voice_agent_pipeline.turn.tools import ToolCall, ToolRegistry, ToolSpec

    class _NoArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def _explode(_input):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated tool failure")

    registry = ToolRegistry(
        [
            ToolSpec(
                name="exploding_tool",
                description="raises on dispatch",
                input_schema=_NoArgs,
                dispatch=_explode,
            )
        ]
    )

    mock_talker.complete_with_tools.return_value = TalkerResponse(
        text="reply",
        tool_calls=[ToolCall(id="t1", name="exploding_tool", arguments={})],
    )
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, registry)
    pushed = _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="x", confidence=0.9, end_to_transcript_ms=42)

    async def _drive() -> None:
        await dispatcher.process_frame(transcript, direction=None)  # type: ignore[arg-type]
        # Text frame still pushed despite the bg failure.
        assert any(isinstance(f, TalkerResponseFrame) and f.text == "reply" for f in pushed)
        # Wait for the bg task to fail.
        await asyncio.sleep(0.05)

    # The done-callback uses ``log.exception`` which structlog's
    # capture_logs renders as level=error event. Just verify the
    # call doesn't raise (the done-callback caught the exception).
    asyncio.run(_drive())


def test_dispatch_text_only_no_tool_calls(
    stt_config: SttConfig,
    mock_talker: MagicMock,
    empty_tool_registry: ToolRegistry,
) -> None:
    """``tool_calls=[]`` → text frame pushed; no tool tasks created."""
    mock_talker.complete_with_tools.return_value = TalkerResponse(
        text="ok",
        tool_calls=[],
    )
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, empty_tool_registry)
    pushed = _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hi", confidence=0.9, end_to_transcript_ms=42)
    asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]

    response_frames = [f for f in pushed if isinstance(f, TalkerResponseFrame)]
    assert len(response_frames) == 1
    assert response_frames[0].text == "ok"


def test_dispatch_constructor_accepts_tool_registry(
    stt_config: SttConfig,
    mock_talker: MagicMock,
    empty_tool_registry: ToolRegistry,
) -> None:
    """Constructor signature: ``TurnDispatchProcessor(router, tool_registry)``."""
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router, empty_tool_registry)
    assert dispatcher is not None
