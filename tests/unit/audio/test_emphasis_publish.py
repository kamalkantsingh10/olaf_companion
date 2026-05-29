"""Unit tests for the Story 6.3 emphasis-event join (`_publish_emphasis_events`).

The join logic lives in :mod:`voice_agent_pipeline.sequential_loop` (it's
runtime orchestration), but the test surface mirrors the story's named file
``tests/unit/audio/test_emphasis_publish.py``: the function is exercised in
isolation against the in-memory :class:`LogEventPublisher` (the
``EventPublisher`` Protocol — CLAUDE.md rule 7) with a tiny TTS stub that
returns a scripted :class:`SegmentTiming`. No PyAudio, no real Cartesia
socket, no internal-function mocks.
"""

from uuid import uuid4

import pytest

from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher
from voice_agent_pipeline.schemas.vocalization_event import VocalizationEvent
from voice_agent_pipeline.sequential_loop import _publish_emphasis_events
from voice_agent_pipeline.splitter.segmenter import Segment
from voice_agent_pipeline.tts.cartesia import SegmentTiming, Word


class _TimingStub:
    """Minimal stand-in for :class:`CartesiaClient` exposing only the join API.

    ``_publish_emphasis_events`` calls exactly one method on the TTS client —
    :meth:`last_segment_timing` — so the stub implements only that. Returning
    ``None`` models the SSE-transport path (no timestamps captured).
    """

    def __init__(self, timing: SegmentTiming | None) -> None:
        self._timing = timing

    def last_segment_timing(self) -> SegmentTiming | None:
        return self._timing


def _timing(*words: tuple[str, int, int]) -> SegmentTiming:
    """Build a SegmentTiming from (text, start_ms, end_ms) triples."""
    return SegmentTiming(words=[Word(text=t, start_ms=s, end_ms=e) for t, s, e in words])


def _segment(*, emphasis: list[int]) -> Segment:
    """A speakable Segment carrying the given emphasis word indices."""
    return Segment(
        text="I really like that.",
        speech_emotion_payload=None,
        vocalization_payloads=[],
        emphasis_word_indices=emphasis,
    )


@pytest.mark.asyncio
async def test_publishes_one_emphasis_event_per_marked_word() -> None:
    """A single marked word → exactly one `emphasis` vocalization at its anchor."""
    pub = LogEventPublisher()
    turn_id = uuid4()
    timing = _timing(("I", 0, 120), ("really", 120, 480), ("like", 480, 700))
    tts = _TimingStub(timing)

    await _publish_emphasis_events(pub, tts, _segment(emphasis=[1]), turn_id, seg_index=0)

    topics = [t for t, _ in pub.published]
    assert topics == ["vocalization"]
    event = pub.published[0][1]
    assert isinstance(event, VocalizationEvent)
    assert event.payload.tag == "emphasis"
    assert event.payload.tts_supported is False
    # audio_frame_id anchors to word[1].start_ms == 120 within segment 0.
    assert event.payload.audio_frame_id == "seg-0-w-120"
    assert event.correlation_id == turn_id


@pytest.mark.asyncio
async def test_seg_index_threads_into_frame_id() -> None:
    """The seg_index argument is the stable half of the audio_frame_id."""
    pub = LogEventPublisher()
    timing = _timing(("hold", 0, 200), ("on", 200, 400))
    tts = _TimingStub(timing)

    await _publish_emphasis_events(pub, tts, _segment(emphasis=[0]), uuid4(), seg_index=3)

    event = pub.published[0][1]
    assert isinstance(event, VocalizationEvent)
    assert event.payload.audio_frame_id == "seg-3-w-0"


@pytest.mark.asyncio
async def test_no_publish_when_no_marks() -> None:
    """A segment with no emphasis indices publishes nothing (early return)."""
    pub = LogEventPublisher()
    tts = _TimingStub(_timing(("hi", 0, 100)))

    await _publish_emphasis_events(pub, tts, _segment(emphasis=[]), uuid4(), seg_index=0)

    assert pub.published == []


@pytest.mark.asyncio
async def test_no_publish_when_timing_is_none() -> None:
    """SSE transport (timing None) → skip the segment's emphasis publish, no crash."""
    pub = LogEventPublisher()
    tts = _TimingStub(None)

    await _publish_emphasis_events(pub, tts, _segment(emphasis=[1]), uuid4(), seg_index=0)

    assert pub.published == []


@pytest.mark.asyncio
async def test_index_out_of_bounds_is_skipped_not_raised() -> None:
    """An index past the word list is skipped defensively (logged WARN, no raise)."""
    pub = LogEventPublisher()
    # Two marks: index 1 is valid, index 5 is past the 2-word timing.
    timing = _timing(("yes", 0, 150), ("really", 150, 500))
    tts = _TimingStub(timing)
    segment = Segment(
        text="yes really",
        speech_emotion_payload=None,
        vocalization_payloads=[],
        emphasis_word_indices=[1, 5],
    )

    await _publish_emphasis_events(pub, tts, segment, uuid4(), seg_index=0)

    # Only the in-bounds index published; the out-of-bounds one was dropped.
    assert [t for t, _ in pub.published] == ["vocalization"]
    event = pub.published[0][1]
    assert isinstance(event, VocalizationEvent)
    assert event.payload.audio_frame_id == "seg-0-w-150"
