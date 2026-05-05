"""Unit tests for :class:`voice_agent_pipeline.pipeline.TurnDispatchProcessor`.

These tests live in tests/unit/turn/ (not tests/unit/) because the
dispatcher is logically part of the turn-routing concern even though it
ships from ``pipeline.py``. The architecture's "tests mirror src/"
guideline holds for source modules; the dispatcher straddles the
``turn/`` and ``pipeline.py`` boundary, so it belongs here next to
``test_router.py``.

Mocks live at the Protocol seam: :class:`TalkerClient` is a
``MagicMock(spec=TalkerClient)`` with ``complete`` configured via
``AsyncMock``. The TurnRouter is an actual concrete instance —
:class:`RouteDecision` is a pydantic model, not a Protocol, so mocking
it would violate architecture's mock-only-at-Protocol-boundaries rule.
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


@pytest.fixture
def stt_config() -> SttConfig:
    return SttConfig(low_confidence_threshold=0.5, clarification_prompt="please repeat?")


@pytest.fixture
def mock_talker() -> MagicMock:
    """TalkerClient stand-in with an AsyncMock complete() ready to configure."""
    talker = MagicMock()
    talker.complete = AsyncMock()
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
) -> None:
    """High-confidence transcript → talker.complete called with the original text."""
    mock_talker.complete.return_value = "OLAF reply"
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router)
    _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hello", confidence=0.9, end_to_transcript_ms=42)

    asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]

    mock_talker.complete.assert_awaited_once_with("hello")


def test_dispatcher_emits_talker_response_frame(
    stt_config: SttConfig,
    mock_talker: MagicMock,
) -> None:
    """Successful Talker call emits TalkerResponseFrame downstream with the reply text."""
    mock_talker.complete.return_value = "It's three o'clock."
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router)
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
) -> None:
    """Low-confidence transcript → talker.complete called with the clarification prompt.

    Pins the FR8 closure: the clarification prompt is what reaches
    Talker on a low-confidence turn, NOT the user's noisy text.
    """
    mock_talker.complete.return_value = "Sorry, I missed that — what was it?"
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router)
    _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hjzz mjy?", confidence=0.2, end_to_transcript_ms=42)
    asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]

    # The Talker sees the clarification prompt, not the noisy "hjzz mjy?".
    mock_talker.complete.assert_awaited_once_with("please repeat?")


def test_dispatcher_propagates_talker_error(
    stt_config: SttConfig,
    mock_talker: MagicMock,
) -> None:
    """TalkerError propagates — CLAUDE.md rule #4 forbids catching it."""
    boom = TalkerError(provider="openai", model="gpt-5.4-nano", reason="rate limited")
    mock_talker.complete.side_effect = boom
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router)
    _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hi", confidence=0.9, end_to_transcript_ms=42)
    with pytest.raises(TalkerError) as exc_info:
        asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]
    assert exc_info.value is boom


def test_dispatcher_logs_talker_responded_event(
    stt_config: SttConfig,
    mock_talker: MagicMock,
) -> None:
    """``talker.responded`` INFO event fires per turn with latency_ms + clarification flag.

    The clarification flag lets operators distinguish clarification
    turns vs normal turns without having to re-check STT confidence
    in the log feed. The response TEXT is intentionally NOT logged
    here (that's the temp _TalkerResponseLogger's DEBUG-level job;
    Story 2.5 will replace that with Cartesia synthesis).
    """
    import structlog

    mock_talker.complete.return_value = "ok"
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router)
    _drain_pushed(dispatcher)

    transcript = TranscriptFrame(text="hi", confidence=0.9, end_to_transcript_ms=42)
    with structlog.testing.capture_logs() as captured:
        asyncio.run(dispatcher.process_frame(transcript, direction=None))  # type: ignore[arg-type]

    matching = [r for r in captured if r.get("event") == "talker.responded"]
    assert len(matching) == 1
    rec = matching[0]
    # Clarification flag distinguishes clarify turns from normal turns.
    assert rec.get("clarification") is False
    # Latency is wall-clock — at least 0 in mocked tests.
    latency_ms = rec.get("latency_ms")
    assert isinstance(latency_ms, int)
    assert latency_ms >= 0
    # Response text NOT logged at INFO — privacy posture.
    assert "text" not in rec


def test_dispatcher_logs_clarification_flag_for_low_confidence(
    stt_config: SttConfig,
    mock_talker: MagicMock,
) -> None:
    """Low-confidence turn logs talker.responded with clarification=True."""
    import structlog

    mock_talker.complete.return_value = "ok"
    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router)
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
) -> None:
    """Non-TranscriptFrames are passed through unchanged; talker is NOT invoked.

    The dispatcher only triggers on TranscriptFrame; AudioRawFrames,
    WakeWordDetectedFrames, system frames etc. flow through untouched
    so the rest of the pipeline can still observe them.
    """
    from pipecat.frames.frames import Frame

    router = TurnRouter(stt_config, mock_talker)
    dispatcher = TurnDispatchProcessor(router)
    pushed = _drain_pushed(dispatcher)

    other_frame = Frame()
    asyncio.run(dispatcher.process_frame(other_frame, direction=None))  # type: ignore[arg-type]

    # Only the original frame pushed through; no TalkerResponseFrame.
    assert pushed == [other_frame]
    mock_talker.complete.assert_not_awaited()
