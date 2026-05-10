"""Unit tests for :mod:`voice_agent_pipeline.pipeline` processors.

Story 3.7 evolution: ``CartesiaSynthesisProcessor`` now consumes
:class:`SegmentFrame` (not :class:`TalkerResponseFrame`) and emits
:class:`EmbodimentAudioFrame` for the first chunk of each segment.
The dispatcher tests still live in ``tests/unit/turn/test_dispatch.py``.

TTSClient is mocked at the Protocol seam — same architectural
convention as Stories 2.4 / 2.5.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from pipecat.frames.frames import Frame, OutputAudioRawFrame

from voice_agent_pipeline.config.expression_map import (
    ExpressionMapConfig,
    FallbackFamily,
    UnknownEntry,
    VocalizationEntry,
)
from voice_agent_pipeline.errors import CartesiaError
from voice_agent_pipeline.pipeline import (
    CartesiaSynthesisProcessor,
    EmbodimentAudioFrame,
    SegmenterProcessor,
    SegmentFrame,
)
from voice_agent_pipeline.splitter.mapping import (
    LastPublishedCache,
    SpeechEmotionPayload,
    VocalizationPayload,
)
from voice_agent_pipeline.splitter.segmenter import Segment, Segmenter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _drain_pushed(processor: Any) -> list[Any]:
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


def _make_mapping() -> ExpressionMapConfig:
    """Minimal valid ExpressionMapConfig for processor tests."""
    return ExpressionMapConfig(
        schema_version=3,
        emotions=["neutral", "content"],
        vocalizations={"laughter": VocalizationEntry(tts_supported=True)},
        fallback_families={
            "high_energy_positive": FallbackFamily(members=["enthusiastic"], maps_to="content")
        },
        unknown=UnknownEntry(maps_to="neutral"),
    )


def _make_processor() -> CartesiaSynthesisProcessor:
    """Build a CartesiaSynthesisProcessor with all deps stubbed."""
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    return CartesiaSynthesisProcessor(
        _make_tts_client([b"audio-a", b"audio-b", b"audio-c"]),
        cache,
        segmenter_processor,
    )


def _make_segment(
    text: str,
    *,
    emotion: str | None = None,
    vocalizations: list[str] | None = None,
) -> Segment:
    """Build a Segment with optional emotion + vocalization payloads."""
    speech_emotion: SpeechEmotionPayload | None = None
    if emotion is not None:
        speech_emotion = SpeechEmotionPayload(
            emotion=emotion,
            source_tag=emotion,
            raw_tag=emotion,
            resolved_fallback=None,
        )
    voc_payloads = [
        VocalizationPayload(tag=tag, tts_supported=True) for tag in (vocalizations or [])
    ]
    return Segment(
        text=text,
        speech_emotion_payload=speech_emotion,
        vocalization_payloads=voc_payloads,
    )


# ---------------------------------------------------------------------------
# CartesiaSynthesisProcessor — segment → audio frames
# ---------------------------------------------------------------------------


def test_synthesizer_emits_audio_frames_in_order() -> None:
    """Each synthesize() chunk lands as an audio frame downstream.

    First chunk is an EmbodimentAudioFrame (carries metadata);
    subsequent chunks are plain OutputAudioRawFrames.
    """
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    client = _make_tts_client([b"audio-a", b"audio-b", b"audio-c"])
    processor = CartesiaSynthesisProcessor(client, cache, segmenter_processor)
    pushed = _drain_pushed(processor)

    segment_frame = SegmentFrame(segment=_make_segment("hello there", emotion="content"))
    asyncio.run(processor.process_frame(segment_frame, direction=None))  # type: ignore[arg-type]

    audio_frames = [f for f in pushed if isinstance(f, OutputAudioRawFrame)]
    assert len(audio_frames) == 3
    # First frame is the embodiment-typed subclass with metadata.
    assert isinstance(audio_frames[0], EmbodimentAudioFrame)
    assert audio_frames[0].speech_emotion_event is not None
    # Subsequent frames are plain OutputAudioRawFrame (not Embodiment-typed).
    assert not isinstance(audio_frames[1], EmbodimentAudioFrame)
    assert not isinstance(audio_frames[2], EmbodimentAudioFrame)
    # Audio bytes preserved in order.
    assert audio_frames[0].audio == b"audio-a"
    assert audio_frames[1].audio == b"audio-b"
    assert audio_frames[2].audio == b"audio-c"
    # Format pinned.
    for f in audio_frames:
        assert f.sample_rate == 16000
        assert f.num_channels == 1


def test_synthesizer_passes_through_segment_frame() -> None:
    """The original SegmentFrame passes through after the audio chunks."""
    processor = _make_processor()
    pushed = _drain_pushed(processor)

    segment_frame = SegmentFrame(segment=_make_segment("ok"))
    asyncio.run(processor.process_frame(segment_frame, direction=None))  # type: ignore[arg-type]

    seg_frames = [f for f in pushed if isinstance(f, SegmentFrame)]
    assert len(seg_frames) == 1
    assert seg_frames[0] is segment_frame


def test_synthesizer_skips_empty_segment_text() -> None:
    """Empty Segment.text → no synthesize() call (defensive guard)."""
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    client = MagicMock()
    called: list[str] = []

    def _record(text: str) -> AsyncIterator[bytes]:
        called.append(text)

        async def _empty() -> AsyncIterator[bytes]:
            return
            yield

        return _empty()

    client.synthesize = _record
    processor = CartesiaSynthesisProcessor(client, cache, segmenter_processor)
    pushed = _drain_pushed(processor)

    segment_frame = SegmentFrame(segment=_make_segment(""))
    asyncio.run(processor.process_frame(segment_frame, direction=None))  # type: ignore[arg-type]

    assert called == []
    # Only the SegmentFrame passes through; no audio.
    assert pushed == [segment_frame]


def test_synthesizer_passes_through_non_segment_frames() -> None:
    """Non-SegmentFrames pass through unchanged; synthesize not invoked."""
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    client = MagicMock()
    called: list[str] = []

    def _record(text: str) -> AsyncIterator[bytes]:
        called.append(text)

        async def _empty() -> AsyncIterator[bytes]:
            return
            yield

        return _empty()

    client.synthesize = _record
    processor = CartesiaSynthesisProcessor(client, cache, segmenter_processor)
    pushed = _drain_pushed(processor)

    other = Frame()
    asyncio.run(processor.process_frame(other, direction=None))  # type: ignore[arg-type]

    assert called == []
    assert pushed == [other]


def test_synthesizer_propagates_cartesia_error() -> None:
    """CartesiaError mid-stream propagates uncaught (CLAUDE.md rule #4)."""
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    boom = CartesiaError(voice_id="v", model="sonic-3", reason="network died")
    client = MagicMock()

    def _synthesize_fail(text: str) -> AsyncIterator[bytes]:
        async def _fail() -> AsyncIterator[bytes]:
            raise boom
            yield

        return _fail()

    client.synthesize = _synthesize_fail
    processor = CartesiaSynthesisProcessor(client, cache, segmenter_processor)
    _drain_pushed(processor)

    segment_frame = SegmentFrame(segment=_make_segment("hello"))
    with pytest.raises(CartesiaError) as exc_info:
        asyncio.run(processor.process_frame(segment_frame, direction=None))  # type: ignore[arg-type]
    assert exc_info.value is boom


def test_synthesizer_logs_synthesis_complete_with_metadata() -> None:
    """``tts.synthesis_complete`` INFO event fires per segment with no text."""
    import structlog

    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    client = _make_tts_client([b"abc", b"defg"])
    processor = CartesiaSynthesisProcessor(client, cache, segmenter_processor)
    _drain_pushed(processor)

    segment_frame = SegmentFrame(segment=_make_segment("hello", emotion="content"))
    with structlog.testing.capture_logs() as captured:
        asyncio.run(processor.process_frame(segment_frame, direction=None))  # type: ignore[arg-type]

    matching = [r for r in captured if r.get("event") == "tts.synthesis_complete"]
    assert len(matching) == 1
    rec = matching[0]
    assert rec.get("chunk_count") == 2
    assert rec.get("byte_total") == 7
    assert rec.get("had_emotion") is True
    assert rec.get("vocalization_count") == 0
    # Privacy: no text fields.
    assert "text" not in rec
    assert "transcript" not in rec


# ---------------------------------------------------------------------------
# CartesiaSynthesisProcessor — emotion-event metadata + dedup via cache
# ---------------------------------------------------------------------------


def test_first_segment_with_emotion_attaches_event() -> None:
    """First segment with an emotion change triggers attachment of the event."""
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    client = _make_tts_client([b"chunk1"])
    processor = CartesiaSynthesisProcessor(client, cache, segmenter_processor)
    pushed = _drain_pushed(processor)

    seg_frame = SegmentFrame(segment=_make_segment("hi", emotion="content"))
    asyncio.run(processor.process_frame(seg_frame, direction=None))  # type: ignore[arg-type]

    embodiment_frames = [f for f in pushed if isinstance(f, EmbodimentAudioFrame)]
    assert len(embodiment_frames) == 1
    assert embodiment_frames[0].speech_emotion_event is not None
    assert embodiment_frames[0].speech_emotion_event.payload.emotion == "content"


def test_consecutive_same_emotion_dedups_via_cache() -> None:
    """Second consecutive same-emotion segment carries no speech_emotion_event."""
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    processor = CartesiaSynthesisProcessor(_make_tts_client([b"x"]), cache, segmenter_processor)
    pushed = _drain_pushed(processor)

    seg1 = SegmentFrame(segment=_make_segment("first.", emotion="content"))
    seg2 = SegmentFrame(segment=_make_segment("second.", emotion="content"))

    asyncio.run(processor.process_frame(seg1, direction=None))  # type: ignore[arg-type]
    asyncio.run(processor.process_frame(seg2, direction=None))  # type: ignore[arg-type]

    embodiment_frames = [f for f in pushed if isinstance(f, EmbodimentAudioFrame)]
    # Both segments produced an EmbodimentAudioFrame (it carries
    # vocalization metadata too); but only the first has
    # speech_emotion_event set — second is None due to dedup.
    assert len(embodiment_frames) == 2
    assert embodiment_frames[0].speech_emotion_event is not None
    assert embodiment_frames[1].speech_emotion_event is None


def test_vocalizations_always_attach() -> None:
    """Vocalizations attach to the first frame regardless of dedup state."""
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    processor = CartesiaSynthesisProcessor(_make_tts_client([b"x"]), cache, segmenter_processor)
    pushed = _drain_pushed(processor)

    seg = SegmentFrame(segment=_make_segment("ha", emotion="content", vocalizations=["laughter"]))
    asyncio.run(processor.process_frame(seg, direction=None))  # type: ignore[arg-type]

    embodiment_frames = [f for f in pushed if isinstance(f, EmbodimentAudioFrame)]
    assert len(embodiment_frames) == 1
    assert len(embodiment_frames[0].vocalization_events) == 1
    assert embodiment_frames[0].vocalization_events[0].payload.tag == "laughter"


def test_correlation_id_bound_per_turn() -> None:
    """All events constructed during one turn share the segmenter's
    current_turn_id — Story 3.7's per-turn correlation contract."""
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    processor = CartesiaSynthesisProcessor(_make_tts_client([b"x"]), cache, segmenter_processor)
    pushed = _drain_pushed(processor)

    expected_id = segmenter_processor.current_turn_id

    seg = SegmentFrame(segment=_make_segment("hi", emotion="content", vocalizations=["laughter"]))
    asyncio.run(processor.process_frame(seg, direction=None))  # type: ignore[arg-type]

    embodiment_frames = [f for f in pushed if isinstance(f, EmbodimentAudioFrame)]
    assert embodiment_frames[0].speech_emotion_event.correlation_id == expected_id  # type: ignore[union-attr]
    assert embodiment_frames[0].vocalization_events[0].correlation_id == expected_id
