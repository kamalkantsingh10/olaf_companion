"""Tests for :class:`LogEventPublisher` — the in-memory ``EventPublisher``.

Real event instances (Story 3.4 schemas), no mocks — the log adapter
is pure data and has no Protocol seams to mock.
"""

import pytest

from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher
from voice_agent_pipeline.schemas.activity_event import ActivityEvent, ActivityPayload
from voice_agent_pipeline.schemas.mood_event import MoodEvent, MoodPayload
from voice_agent_pipeline.schemas.speech_emotion_event import (
    SpeechEmotionEvent,
    SpeechEmotionPayload,
)
from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
    VocalizationPayload,
)


@pytest.mark.asyncio
async def test_publish_mood_records_event() -> None:
    pub = LogEventPublisher()
    event = MoodEvent(payload=MoodPayload(mood="calm"))
    await pub.publish_mood(event)
    assert pub.published == [("mood", event)]


@pytest.mark.asyncio
async def test_publish_activity_records_event() -> None:
    pub = LogEventPublisher()
    event = ActivityEvent(payload=ActivityPayload(state="starting"))
    await pub.publish_activity(event)
    assert pub.published == [("activity", event)]


@pytest.mark.asyncio
async def test_publish_speech_emotion_records_event() -> None:
    pub = LogEventPublisher()
    event = SpeechEmotionEvent(
        payload=SpeechEmotionPayload(
            emotion="excited",
            source_tag="excited",
            raw_tag="excited",
            resolved_fallback=None,
        )
    )
    await pub.publish_speech_emotion(event)
    assert pub.published == [("speech_emotion", event)]


@pytest.mark.asyncio
async def test_publish_vocalization_records_event() -> None:
    pub = LogEventPublisher()
    event = VocalizationEvent(payload=VocalizationPayload(tag="laughter", tts_supported=True))
    await pub.publish_vocalization(event)
    assert pub.published == [("vocalization", event)]


@pytest.mark.asyncio
async def test_publish_order_preserved() -> None:
    """Interleaved publishes record in call order (FIFO)."""
    pub = LogEventPublisher()
    mood = MoodEvent(payload=MoodPayload(mood="calm"))
    activity = ActivityEvent(payload=ActivityPayload(state="starting"))

    await pub.publish_mood(mood)
    await pub.publish_activity(activity)
    await pub.publish_mood(mood)

    assert pub.published == [("mood", mood), ("activity", activity), ("mood", mood)]


@pytest.mark.asyncio
async def test_connect_disconnect_no_ops() -> None:
    """Lifecycle methods don't raise; no side effects."""
    pub = LogEventPublisher()
    await pub.connect()
    await pub.disconnect()
    # Still safe to publish after — there's no actual transport.
    event = MoodEvent(payload=MoodPayload(mood="calm"))
    await pub.publish_mood(event)
    assert len(pub.published) == 1


@pytest.mark.asyncio
async def test_is_healthy_always_true() -> None:
    pub = LogEventPublisher()
    assert await pub.is_healthy() is True


@pytest.mark.asyncio
async def test_implements_event_publisher_protocol() -> None:
    """Structural typing — LogEventPublisher conforms to EventPublisher.

    No isinstance check (Protocol isn't @runtime_checkable). The check
    is "has the right method signatures," verified at the call site by
    exercising every Protocol method on a LogEventPublisher instance.
    """
    pub = LogEventPublisher()
    # Each of these must be a callable method. Direct attribute
    # access — getattr-with-constant is the same shape but lint-flagged.
    assert callable(pub.connect)
    assert callable(pub.disconnect)
    assert callable(pub.is_healthy)
    assert callable(pub.publish_mood)
    assert callable(pub.publish_activity)
    assert callable(pub.publish_speech_emotion)
    assert callable(pub.publish_vocalization)
