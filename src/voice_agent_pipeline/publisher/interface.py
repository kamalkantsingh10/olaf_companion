"""``EventPublisher`` Protocol — the four-topic broadcast surface.

Story 3.5 — recreates the publisher seam Story 3.4 deleted as part of
the schema rebuild. The Protocol is the architecture's stable
contract: alternative transports (Zenoh, NATS, WebSocket bridge)
implement the same surface without changing call sites.

v1 ships two implementations:

- :class:`Ros2EventPublisher` (production) — four ``rclpy.Publisher``
  instances with per-topic QoS, JSON-encoded envelope on each topic.
- :class:`LogEventPublisher` (test/dev) — in-memory adapter that
  records every publish call as a ``(topic, event)`` tuple for later
  test assertion.

Story 1.4's placeholder ``ExpressionPublisher`` (single-channel) was
removed in Story 3.4 along with the placeholder event types it
referenced.
"""

from typing import Protocol

from voice_agent_pipeline.schemas.activity_event import ActivityEvent
from voice_agent_pipeline.schemas.mood_event import MoodEvent
from voice_agent_pipeline.schemas.speech_emotion_event import SpeechEmotionEvent
from voice_agent_pipeline.schemas.vocalization_event import VocalizationEvent


class EventPublisher(Protocol):
    """Four-topic event publisher.

    Architecture.md §"Stable contracts" — concrete transports plug in
    behind this Protocol without changing the splitter, mood
    controller, or activity FSM. v1 transport is ROS 2 / DDS via
    :class:`Ros2EventPublisher`; alternative adapters can swap in by
    config (``[publisher] adapter = "..."``).

    NOT decorated with ``@runtime_checkable`` — structural typing is
    the architectural intent; runtime ``isinstance`` checks against
    this Protocol violate it.
    """

    async def connect(self) -> None:
        """Open the underlying transport. Call once at startup.

        Raises :class:`StartupValidationError` (or its
        :class:`PublisherError` cause) on connection failure — v1
        fail-fast: the broadcast bus is a hard dependency.
        """
        ...

    async def disconnect(self) -> None:
        """Close the transport cleanly. Idempotent — safe to call twice."""
        ...

    async def is_healthy(self) -> bool:
        """Return ``True`` if the transport is connected and ready to publish."""
        ...

    async def publish_mood(self, event: MoodEvent) -> None:
        """Publish on the ``mood`` topic (latched, transient_local depth=1).

        Subscribers learn the current mood at connect via the latched
        durability profile (architecture.md §"Per-topic QoS").
        Cooldown is enforced upstream by Story 3.6's
        :class:`MoodController.set` (NFR31).
        """
        ...

    async def publish_activity(self, event: ActivityEvent) -> None:
        """Publish on the ``activity`` topic (latched, transient_local depth=1).

        Story 4.3's activity FSM publishes on every transition.
        """
        ...

    async def publish_speech_emotion(self, event: SpeechEmotionEvent) -> None:
        """Publish on the ``speech_emotion`` topic (volatile depth=8)."""
        ...

    async def publish_vocalization(self, event: VocalizationEvent) -> None:
        """Publish on the ``vocalization`` topic (volatile depth=8)."""
        ...
