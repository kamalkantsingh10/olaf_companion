"""Tests for :mod:`voice_agent_pipeline.sequential_loop`.

Focus: the embodiment-event publishing that the half-duplex loop must do
for each spoken segment — ``speech_emotion`` (deduped) + ``vocalization``
(never deduped). This is the surface the pipecat→half-duplex migration
had dropped; :func:`_publish_segment_events` is the pure, PyAudio-free
publish step, unit-tested here against the ``EventPublisher`` Protocol
via the in-memory :class:`LogEventPublisher` (no mocks of internal pure
functions — CLAUDE.md rule #7).
"""

from uuid import uuid4

import pytest

from voice_agent_pipeline.config.expression_map import (
    ExpressionMapConfig,
    FallbackFamily,
    UnknownEntry,
    VocalizationEntry,
)
from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher
from voice_agent_pipeline.schemas.speech_emotion_event import SpeechEmotionEvent
from voice_agent_pipeline.schemas.vocalization_event import VocalizationEvent
from voice_agent_pipeline.sequential_loop import _is_speakable, _publish_segment_events
from voice_agent_pipeline.splitter.mapping import (
    LastPublishedCache,
    SpeechEmotionPayload,
    VocalizationPayload,
)
from voice_agent_pipeline.splitter.segmenter import Segment, Segmenter


def _make_mapping() -> ExpressionMapConfig:
    """Small valid ExpressionMapConfig — same shape as the segmenter tests."""
    return ExpressionMapConfig(
        schema_version=3,
        emotions=["neutral", "content", "excited", "happy"],
        vocalizations={
            "laughter": VocalizationEntry(tts_supported=True),
            "sigh": VocalizationEntry(tts_supported=False),
        },
        fallback_families={
            "high_energy_positive": FallbackFamily(members=["enthusiastic"], maps_to="excited"),
        },
        unknown=UnknownEntry(maps_to="neutral"),
    )


def _emotion(name: str) -> SpeechEmotionPayload:
    return SpeechEmotionPayload(emotion=name, source_tag=name, raw_tag=name, resolved_fallback=None)


def _voc(tag: str, *, tts_supported: bool) -> VocalizationPayload:
    return VocalizationPayload(tag=tag, tts_supported=tts_supported)


def _drain(seg: Segmenter, *tokens: str) -> list[Segment]:
    out: list[Segment] = []
    for token in tokens:
        out.extend(seg.consume(token))
    out.extend(seg.flush())
    return out


@pytest.mark.asyncio
async def test_publishes_speech_emotion_then_vocalizations_in_order() -> None:
    """A segment with an emotion + vocalizations publishes emotion FIRST."""
    pub = LogEventPublisher()
    cache = LastPublishedCache()
    turn_id = uuid4()
    segment = Segment(
        text="ha ha [laughter]",
        speech_emotion_payload=_emotion("excited"),
        vocalization_payloads=[
            _voc("laughter", tts_supported=True),
            _voc("nod", tts_supported=False),
        ],
    )

    await _publish_segment_events(pub, cache, segment, turn_id)

    topics = [t for t, _ in pub.published]
    assert topics == ["speech_emotion", "vocalization", "vocalization"]
    # All three share the turn's correlation id.
    assert {e.correlation_id for _, e in pub.published} == {turn_id}
    emotion_event = pub.published[0][1]
    assert isinstance(emotion_event, SpeechEmotionEvent)
    assert emotion_event.payload.emotion == "excited"
    voc_tags = [e.payload.tag for _, e in pub.published[1:] if isinstance(e, VocalizationEvent)]
    assert voc_tags == ["laughter", "nod"]


@pytest.mark.asyncio
async def test_repeated_emotion_is_deduped_vocalizations_are_not() -> None:
    """Second identical emotion is suppressed; every vocalization still fires."""
    pub = LogEventPublisher()
    cache = LastPublishedCache()
    turn_id = uuid4()
    seg1 = Segment(
        text="one",
        speech_emotion_payload=_emotion("content"),
        vocalization_payloads=[_voc("sigh", tts_supported=False)],
    )
    seg2 = Segment(
        text="two",
        speech_emotion_payload=_emotion("content"),  # same emotion → deduped
        vocalization_payloads=[_voc("sigh", tts_supported=False)],
    )

    await _publish_segment_events(pub, cache, seg1, turn_id)
    await _publish_segment_events(pub, cache, seg2, turn_id)

    topics = [t for t, _ in pub.published]
    # First segment: emotion + vocalization. Second: vocalization only.
    assert topics == ["speech_emotion", "vocalization", "vocalization"]


@pytest.mark.asyncio
async def test_text_only_segment_publishes_nothing() -> None:
    """A plain-text segment (no emotion change, no vocalization) is silent."""
    pub = LogEventPublisher()
    cache = LastPublishedCache()
    segment = Segment(text="just words.", speech_emotion_payload=None, vocalization_payloads=[])

    await _publish_segment_events(pub, cache, segment, uuid4())

    assert pub.published == []


@pytest.mark.asyncio
async def test_real_segmenter_stream_drives_emotion_and_vocalization_publishes() -> None:
    """End-to-end: a tagged Talker reply parsed by the real Segmenter
    yields segments that publish exactly one excited emotion + one sigh."""
    pub = LogEventPublisher()
    cache = LastPublishedCache()
    turn_id = uuid4()
    segments = _drain(
        Segmenter(_make_mapping()),
        '<emotion value="excited"/> Hi there. [sigh] Bye now.',
    )

    for segment in segments:
        await _publish_segment_events(pub, cache, segment, turn_id)

    emotions = [e.payload.emotion for _, e in pub.published if isinstance(e, SpeechEmotionEvent)]
    vocs = [e.payload.tag for _, e in pub.published if isinstance(e, VocalizationEvent)]
    assert emotions == ["excited"]
    assert vocs == ["sigh"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello there.", True),
        ("3.14 is pi", True),
        ("café", True),  # non-ASCII letters count
        ("", False),
        ("   ", False),
        (".", False),  # the "Wait..." split-out case that 400'd Cartesia
        ("...", False),
        ("?!", False),
        (" . ", False),
    ],
)
def test_is_speakable_skips_empty_and_punctuation_only(text: str, expected: bool) -> None:
    """Guards the Cartesia "empty or punctuation-only transcript" 400."""
    assert _is_speakable(text) is expected
