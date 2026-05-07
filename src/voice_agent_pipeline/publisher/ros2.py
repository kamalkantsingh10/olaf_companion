"""``Ros2EventPublisher`` — production ``EventPublisher`` over ROS 2 / DDS.

This is the **only file in the codebase** that imports ``rclpy``
(architecture.md §"Architectural Boundaries" — boundary concentration).
The rest of the system references the :class:`EventPublisher` Protocol
(``publisher/interface.py``); the adapter swap to a different transport
(Zenoh, NATS, custom) is a single-file change here.

v1 wire format
--------------

Per architecture.md §"V1 wire format simplification" — every topic
uses ``std_msgs/String`` carrying the full :class:`EventEnvelope`
(envelope fields + topic-specific payload) JSON-encoded via
``event.model_dump_json()``. No custom ``.msg`` IDL, no
``ament_python``/``colcon`` build complexity. When a typed consumer
(embodiment project) materializes, a custom ``.msg`` package can drop
in alongside without changing what producers send.

Per-topic QoS (NFR21, FR51)
---------------------------

- ``mood``: RELIABLE + ``transient_local`` durability + depth=1 (latched).
- ``activity``: RELIABLE + ``transient_local`` + depth=1 (latched).
- ``speech_emotion``: RELIABLE + ``volatile`` + depth=8.
- ``vocalization``: RELIABLE + ``volatile`` + depth=8.
"""

import asyncio
import logging
from typing import Any

# rclpy ships no .pyi stubs as of 2025; bare `# type: ignore` is
# banned by architecture.md §"Anti-Patterns" — pair with the specific
# rule code + reason per import.
import rclpy  # type: ignore[import-not-found,import-untyped]
from rclpy.node import Node  # type: ignore[import-not-found,import-untyped]
from rclpy.qos import (  # type: ignore[import-not-found,import-untyped]
    DurabilityPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String  # type: ignore[import-not-found,import-untyped]

from voice_agent_pipeline.errors import PublisherError, StartupValidationError
from voice_agent_pipeline.schemas.activity_event import ActivityEvent
from voice_agent_pipeline.schemas.envelope import EventEnvelope
from voice_agent_pipeline.schemas.mood_event import MoodEvent
from voice_agent_pipeline.schemas.speech_emotion_event import SpeechEmotionEvent
from voice_agent_pipeline.schemas.vocalization_event import VocalizationEvent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-topic QoS profiles
# ---------------------------------------------------------------------------
#
# Stored as a module-level dict so ``test_qos_profiles_match_architecture_spec``
# can introspect them without touching the publisher's construction
# path. Architecture.md §"Per-topic QoS" is the spec; if these drift,
# the test fails loudly.


def _build_qos_profiles() -> dict[str, "QoSProfile"]:
    """Construct the four per-topic QoS profiles per the architecture spec.

    Wrapped in a function rather than a top-level constant because
    ``QoSProfile`` is rclpy-loaded — at import time on a host without
    rclpy installed (e.g., a CI mock test), constructing the profiles
    would fail. Lazy construction avoids that.
    """
    latched = {
        "reliability": ReliabilityPolicy.RELIABLE,
        "durability": DurabilityPolicy.TRANSIENT_LOCAL,
        "depth": 1,
    }
    volatile = {
        "reliability": ReliabilityPolicy.RELIABLE,
        "durability": DurabilityPolicy.VOLATILE,
        "depth": 8,
    }
    return {
        "mood": QoSProfile(**latched),
        "activity": QoSProfile(**latched),
        "speech_emotion": QoSProfile(**volatile),
        "vocalization": QoSProfile(**volatile),
    }


# ---------------------------------------------------------------------------
# Ros2EventPublisher
# ---------------------------------------------------------------------------


class Ros2EventPublisher:
    """Production ``EventPublisher`` — four ``rclpy.Publisher`` instances.

    Constructor stores config; the actual rclpy initialization happens
    in :meth:`connect` (architecture.md §"Async Patterns" — no I/O in
    constructors). On any rclpy failure, raises
    :class:`StartupValidationError` (wrapping :class:`PublisherError`)
    so Story 2.5's ``__main__.py`` startup-validation handler fires.

    Attributes are populated by ``connect`` and consumed by the
    ``publish_*`` methods.
    """

    def __init__(self, config: Any) -> None:
        """Store ``PublisherConfig``. Defer all rclpy work to ``connect``.

        ``config`` is typed ``Any`` to break what would otherwise be a
        circular import (config.setup imports schemas which the
        publisher also touches). Architecture.md doesn't mandate
        circular-free imports for this seam; the actual config shape
        is enforced upstream by pydantic.
        """
        self._config = config
        self._node: Node | None = None
        self._publishers: dict[str, Any] = {}
        # Idempotency latch for disconnect.
        self._closed: bool = False

    async def connect(self) -> None:
        """Initialize rclpy, create node + four publishers per AC #3 / #5.

        Wraps the sync rclpy calls in :func:`asyncio.to_thread` so the
        event loop doesn't block (architecture.md §"Async Patterns" —
        sync library at the boundary).
        """
        try:
            await asyncio.to_thread(self._connect_sync)
        except Exception as e:
            # Wrap any rclpy failure in PublisherError, then surface
            # as StartupValidationError so __main__'s startup probe
            # treats it as fail-fast.
            log.error("publisher.connect_failed", extra={"error": str(e)})
            raise StartupValidationError(
                component="ros2_publisher",
                error=str(e),
            ) from PublisherError(reason="connect_failed", error=str(e))

        log.info(
            "publisher.connected",
            extra={
                "adapter": "ros2",
                "dds_domain_id": self._config.dds_domain_id,
                "topic_count": 4,
            },
        )

    def _connect_sync(self) -> None:
        """Sync init — runs inside ``asyncio.to_thread``."""
        rclpy.init()
        self._node = Node("voice_agent_pipeline")
        qos_profiles = _build_qos_profiles()
        topics = self._config.topics
        # Map of topic-name → (config attribute, qos key).
        for topic_key, topic_path in (
            ("mood", topics.mood),
            ("activity", topics.activity),
            ("speech_emotion", topics.speech_emotion),
            ("vocalization", topics.vocalization),
        ):
            # rclpy ships no stubs; the create_publisher return type is
            # partially-unknown to pyright. The runtime contract is
            # rclpy.Publisher; we store it via the publishers Any-typed
            # dict so call-site usage in publish_* doesn't require
            # further suppressions.
            self._publishers[topic_key] = self._node.create_publisher(  # pyright: ignore[reportUnknownMemberType]
                String,
                topic_path,
                qos_profile=qos_profiles[topic_key],
            )

    async def disconnect(self) -> None:
        """Tear down node + rclpy. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.to_thread(self._disconnect_sync)
        except Exception as e:
            # Disconnect failures are logged but don't propagate —
            # we're shutting down anyway and the v1 process exit is
            # immediate.
            log.warning("publisher.disconnect_warning", extra={"error": str(e)})
        else:
            log.info("publisher.disconnected")

    def _disconnect_sync(self) -> None:
        if self._node is not None:
            for pub in self._publishers.values():
                try:
                    self._node.destroy_publisher(pub)
                except Exception:  # noqa: S110
                    # Per-publisher destruction failures are swallowed
                    # — see ``disconnect`` docstring; v1 shutdown path
                    # tolerates partial-cleanup.
                    pass
            self._node.destroy_node()
            self._node = None
        rclpy.shutdown()
        self._publishers = {}

    async def is_healthy(self) -> bool:
        """Healthy iff the rclpy context is live."""
        if self._node is None:
            return False
        return bool(self._node.context.ok())

    async def publish_mood(self, event: MoodEvent) -> None:
        await self._publish("mood", event)

    async def publish_activity(self, event: ActivityEvent) -> None:
        await self._publish("activity", event)

    async def publish_speech_emotion(self, event: SpeechEmotionEvent) -> None:
        await self._publish("speech_emotion", event)

    async def publish_vocalization(self, event: VocalizationEvent) -> None:
        await self._publish("vocalization", event)

    async def _publish(self, topic_key: str, event: EventEnvelope) -> None:
        """Serialize the event to JSON, push on the topic.

        v1 keeps the publish call synchronous inside the async method
        (no ``asyncio.to_thread``) — rclpy's local-DDS publish is
        sub-millisecond on the dev host. Story 3.7's NFR5 alignment
        test will catch any jitter from event-loop blocking.
        """
        publisher = self._publishers.get(topic_key)
        if publisher is None:
            raise PublisherError(
                topic=topic_key,
                reason="not_connected",
            )

        msg = String()
        msg.data = event.model_dump_json()

        try:
            publisher.publish(msg)
        except Exception as e:
            # Wrap and propagate — CLAUDE.md rule #4: don't catch in v1.
            log.error(
                "publisher.publish_failed",
                extra={"topic": topic_key, "error": str(e)},
            )
            raise PublisherError(topic=topic_key, error=str(e)) from e

        log.debug(
            "publisher.published",
            extra={
                "topic": topic_key,
                "correlation_id": str(event.correlation_id),
            },
        )
