"""In-memory ``EventPublisher`` for tests + pre-Epic-3 dev.

Records every publish call as a ``(topic, event)`` tuple in
``self.published`` for later assertion. ``connect`` / ``disconnect`` /
``is_healthy`` are no-ops — there's nothing to open or close.

Used by:

- Stories 3.6 / 3.7 unit tests as the :class:`EventPublisher` fake.
- Story 3.7's integration tests (mocked Cartesia + LogEventPublisher
  capture every event for ordering + content assertions).
- Local dev runs without rclpy: ``[publisher] adapter = "log"`` in
  ``setup.toml`` to skip the ROS 2 init.
"""

from voice_agent_pipeline.schemas.activity_event import ActivityEvent
from voice_agent_pipeline.schemas.envelope import EventEnvelope
from voice_agent_pipeline.schemas.mood_event import MoodEvent
from voice_agent_pipeline.schemas.speech_emotion_event import SpeechEmotionEvent
from voice_agent_pipeline.schemas.vocalization_event import VocalizationEvent


class LogEventPublisher:
    """In-memory ``EventPublisher`` — records every publish for assertion.

    Implements the structural ``EventPublisher`` Protocol (no explicit
    ``EventPublisher`` base class — duck-typed via Protocol).

    Attributes:
        published: List of ``(topic_name, event)`` tuples in publish
            order. Tests inspect this directly to assert on event
            ordering, payload content, and counts.
    """

    def __init__(self) -> None:
        # Tuple type is (topic_name, event); the event is one of the four
        # concrete subclasses but typed as the envelope base for
        # assignment compatibility.
        self.published: list[tuple[str, EventEnvelope]] = []

    async def connect(self) -> None:
        """No-op. The log adapter has no transport to open."""

    async def disconnect(self) -> None:
        """No-op. The log adapter has no transport to close."""

    async def is_healthy(self) -> bool:
        """Always healthy — there's nothing to be unhealthy about."""
        return True

    async def publish_mood(self, event: MoodEvent) -> None:
        """Record on the ``mood`` topic. No log emission."""
        self.published.append(("mood", event))

    async def publish_activity(self, event: ActivityEvent) -> None:
        """Record on the ``activity`` topic. No log emission."""
        self.published.append(("activity", event))

    async def publish_speech_emotion(self, event: SpeechEmotionEvent) -> None:
        """Record on the ``speech_emotion`` topic. No log emission."""
        self.published.append(("speech_emotion", event))

    async def publish_vocalization(self, event: VocalizationEvent) -> None:
        """Record on the ``vocalization`` topic. No log emission."""
        self.published.append(("vocalization", event))
