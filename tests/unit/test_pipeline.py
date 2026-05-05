"""Unit tests for :mod:`voice_agent_pipeline.pipeline` processors.

The dispatcher tests live in ``tests/unit/turn/test_dispatch.py``
because they're conceptually a turn-routing concern. This file
covers Story 2.5's :class:`CartesiaSynthesisProcessor` — same
file because it's the only pipeline-resident processor with
non-trivial behaviour beyond pass-through wrappers.

TTSClient is mocked at the Protocol seam — same architectural
convention as Story 2.4's dispatcher tests.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from pipecat.frames.frames import Frame, OutputAudioRawFrame

from voice_agent_pipeline.errors import CartesiaError
from voice_agent_pipeline.pipeline import (
    CartesiaSynthesisProcessor,
    TalkerResponseFrame,
)


def _drain_pushed(processor: CartesiaSynthesisProcessor) -> list[Any]:
    """Capture frames pushed downstream during ``process_frame`` calls."""
    pushed: list[Any] = []

    async def _capture(frame: Any, direction: Any) -> None:
        pushed.append(frame)

    processor.push_frame = _capture  # type: ignore[method-assign]
    return pushed


def _make_tts_client(chunks: list[bytes]) -> MagicMock:
    """Build a TTSClient stub yielding the given chunks from synthesize()."""
    client = MagicMock()

    def _synthesize(text: str) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            for c in chunks:
                yield c

        return _gen()

    client.synthesize = _synthesize
    return client


def test_synthesizer_emits_output_audio_frames_in_order() -> None:
    """Each synthesize() chunk lands as an OutputAudioRawFrame downstream.

    Pinning OutputAudioRawFrame (the DataFrame subclass) — not the
    bare AudioRawFrame mixin — is the contract. Discovered in Story
    2.1's play_test_tone work; the bare mixin lacks framework-managed
    attrs and crashes the runner.
    """
    client = _make_tts_client([b"audio-a", b"audio-b", b"audio-c"])
    processor = CartesiaSynthesisProcessor(client)
    pushed = _drain_pushed(processor)

    frame = TalkerResponseFrame(text="hello there")
    asyncio.run(processor.process_frame(frame, direction=None))  # type: ignore[arg-type]

    audio_frames = [f for f in pushed if isinstance(f, OutputAudioRawFrame)]
    # Three audio chunks in order.
    assert len(audio_frames) == 3
    assert audio_frames[0].audio == b"audio-a"
    assert audio_frames[1].audio == b"audio-b"
    assert audio_frames[2].audio == b"audio-c"
    # Format pinned to 16 kHz mono — same as the rest of the pipeline.
    for f in audio_frames:
        assert f.sample_rate == 16000
        assert f.num_channels == 1


def test_synthesizer_passes_through_talker_response_frame() -> None:
    """The original TalkerResponseFrame is pushed downstream after the audio chunks.

    Future stages (Epic 3 expression-event publisher, Story 5.1
    barge-in) need the turn-boundary marker. The audio chunks are
    pushed as OutputAudioRawFrame; the original TalkerResponseFrame
    flows through unchanged so observers can distinguish "audio
    segment N" from "the whole turn ended".
    """
    client = _make_tts_client([b"x"])
    processor = CartesiaSynthesisProcessor(client)
    pushed = _drain_pushed(processor)

    frame = TalkerResponseFrame(text="ok")
    asyncio.run(processor.process_frame(frame, direction=None))  # type: ignore[arg-type]

    response_frames = [f for f in pushed if isinstance(f, TalkerResponseFrame)]
    assert len(response_frames) == 1
    assert response_frames[0] is frame


def test_synthesizer_skips_empty_text_frames() -> None:
    """An empty TalkerResponseFrame (text="") doesn't burn a synthesis call.

    Defensive guard. v1 Talker always returns *something*; a stray
    empty frame would otherwise hit the TTS API for nothing.
    """
    client = _make_tts_client([b"would-not-be-pushed"])
    # Replace synthesize with a recorder so we can assert it WAS NOT called.
    called_with: list[str] = []

    def _record_call(text: str) -> AsyncIterator[bytes]:
        called_with.append(text)

        async def _empty() -> AsyncIterator[bytes]:
            return
            yield  # unreachable; satisfies the async generator type

        return _empty()

    client.synthesize = _record_call

    processor = CartesiaSynthesisProcessor(client)
    pushed = _drain_pushed(processor)

    frame = TalkerResponseFrame(text="")
    asyncio.run(processor.process_frame(frame, direction=None))  # type: ignore[arg-type]

    # No synthesis call, no audio frames — only the original
    # TalkerResponseFrame passed through.
    assert called_with == []
    assert pushed == [frame]


def test_synthesizer_passes_through_non_talker_frames() -> None:
    """Non-TalkerResponseFrames pass through unchanged; synthesize() not invoked."""
    client = _make_tts_client([b"would-not-be-pushed"])
    called_with: list[str] = []

    def _record_call(text: str) -> AsyncIterator[bytes]:
        called_with.append(text)

        async def _empty() -> AsyncIterator[bytes]:
            return
            yield

        return _empty()

    client.synthesize = _record_call

    processor = CartesiaSynthesisProcessor(client)
    pushed = _drain_pushed(processor)

    other = Frame()
    asyncio.run(processor.process_frame(other, direction=None))  # type: ignore[arg-type]

    assert called_with == []
    assert pushed == [other]


def test_synthesizer_propagates_cartesia_error() -> None:
    """CartesiaError mid-stream propagates — CLAUDE.md rule #4 forbids catching.

    The synthesize() generator may raise either at open-time or
    after yielding chunks (Story 2.3's mid-stream error wrapping).
    Either way the dispatcher's process_frame path lets it bubble
    out so systemd restarts the process.
    """
    boom = CartesiaError(voice_id="v", model="sonic-3", reason="network died")
    client = MagicMock()

    def _synthesize_fail(text: str) -> AsyncIterator[bytes]:
        async def _fail() -> AsyncIterator[bytes]:
            raise boom
            yield  # unreachable

        return _fail()

    client.synthesize = _synthesize_fail

    processor = CartesiaSynthesisProcessor(client)
    _drain_pushed(processor)

    frame = TalkerResponseFrame(text="hello")
    with pytest.raises(CartesiaError) as exc_info:
        asyncio.run(processor.process_frame(frame, direction=None))  # type: ignore[arg-type]
    assert exc_info.value is boom


def test_synthesizer_logs_synthesis_complete_with_metadata() -> None:
    """``tts.synthesis_complete`` INFO event fires after a successful turn.

    Operator-side observability — chunk counts + byte totals per
    turn so ops can watch synthesis output drift over time. Privacy:
    NO transcript / response text in this log line; only audio
    metadata.
    """
    import structlog

    client = _make_tts_client([b"abc", b"defg"])
    processor = CartesiaSynthesisProcessor(client)
    _drain_pushed(processor)

    frame = TalkerResponseFrame(text="hello")
    with structlog.testing.capture_logs() as captured:
        asyncio.run(processor.process_frame(frame, direction=None))  # type: ignore[arg-type]

    matching = [r for r in captured if r.get("event") == "tts.synthesis_complete"]
    assert len(matching) == 1
    rec = matching[0]
    assert rec.get("chunk_count") == 2
    assert rec.get("byte_total") == 7  # b"abc" + b"defg"
    # Privacy invariant — no text fields.
    assert "text" not in rec
    assert "transcript" not in rec
