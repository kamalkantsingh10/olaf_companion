"""Tests for :class:`Ros2EventPublisher` — mocked rclpy throughout.

Mocking strategy: ``sys.modules`` patching for ``rclpy``,
``rclpy.qos``, ``rclpy.node``, ``std_msgs``, ``std_msgs.msg``. This
keeps the test runnable on hosts without ROS 2 installed (CI + most
dev hosts). The boundary-concentration rule (only ``publisher/ros2.py``
imports rclpy) means the patch is scoped narrowly.
"""

import sys
from unittest.mock import MagicMock

import pytest

from voice_agent_pipeline.errors import PublisherError, StartupValidationError
from voice_agent_pipeline.schemas.mood_event import MoodEvent, MoodPayload


@pytest.fixture
def mock_rclpy(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Patch ``sys.modules`` for the rclpy import surface.

    Returns the mock objects so tests can introspect ``init`` /
    ``create_publisher`` / ``publish`` calls.
    """
    rclpy = MagicMock()
    qos = MagicMock()
    # Real-shape DurabilityPolicy and ReliabilityPolicy enums — tests
    # need to introspect the values that get passed to QoSProfile.
    qos.ReliabilityPolicy = MagicMock(RELIABLE="RELIABLE")
    qos.DurabilityPolicy = MagicMock(TRANSIENT_LOCAL="TRANSIENT_LOCAL", VOLATILE="VOLATILE")

    # QoSProfile records its kwargs so tests can inspect what was
    # passed for each topic.
    captured_qos: list[dict[str, object]] = []

    def _qos_factory(**kwargs: object) -> MagicMock:
        captured_qos.append(kwargs)
        m = MagicMock()
        m.kwargs = kwargs
        return m

    qos.QoSProfile = _qos_factory

    node_module = MagicMock()

    # Each create_publisher call must return a distinct MagicMock so
    # the four per-topic publishers are independently introspectable
    # (default MagicMock returns the same instance every call, which
    # collapses all four publish counts into one).
    def _create_publisher_factory(*_a: object, **_kw: object) -> MagicMock:
        return MagicMock()

    node_module.Node.return_value.create_publisher.side_effect = _create_publisher_factory
    std_msgs = MagicMock()
    std_msgs_msg = MagicMock()
    # String() must return an instance with a writable .data attribute.
    std_msgs_msg.String = MagicMock(side_effect=lambda: MagicMock(data=""))

    # Patch sys.modules BEFORE the publisher.ros2 module is imported.
    monkeypatch.setitem(sys.modules, "rclpy", rclpy)
    monkeypatch.setitem(sys.modules, "rclpy.qos", qos)
    monkeypatch.setitem(sys.modules, "rclpy.node", node_module)
    monkeypatch.setitem(sys.modules, "std_msgs", std_msgs)
    monkeypatch.setitem(sys.modules, "std_msgs.msg", std_msgs_msg)

    # Force re-import of voice_agent_pipeline.publisher.ros2 so it
    # picks up the mocked rclpy. (If a prior test imported the real
    # one, the cached module would shadow our mock.)
    monkeypatch.delitem(sys.modules, "voice_agent_pipeline.publisher.ros2", raising=False)

    return {
        "rclpy": rclpy,
        "qos": qos,
        "node_module": node_module,
        "std_msgs_msg": std_msgs_msg,
        "captured_qos": captured_qos,  # type: ignore[dict-item]
    }


def _make_config(
    *,
    adapter: str = "ros2",
    mood_topic: str = "/olaf/mood",
    activity_topic: str = "/olaf/activity",
    speech_emotion_topic: str = "/olaf/speech_emotion",
    vocalization_topic: str = "/olaf/vocalization",
) -> object:
    """Build a minimal PublisherConfig-shaped object for tests."""
    from voice_agent_pipeline.config.setup import PublisherConfig, TopicNames

    return PublisherConfig(
        adapter=adapter,  # type: ignore[arg-type]
        dds_domain_id=0,
        topics=TopicNames(
            mood=mood_topic,
            activity=activity_topic,
            speech_emotion=speech_emotion_topic,
            vocalization=vocalization_topic,
        ),
    )


@pytest.mark.asyncio
async def test_connect_initializes_rclpy_node_and_four_publishers(
    mock_rclpy: dict[str, MagicMock],
) -> None:
    """``connect`` calls rclpy.init + creates a node + 4 publishers."""
    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

    pub = Ros2EventPublisher(_make_config())
    await pub.connect()

    # rclpy.init called exactly once.
    assert mock_rclpy["rclpy"].init.call_count == 1
    # Node constructed with the pipeline name.
    node_class = mock_rclpy["node_module"].Node
    assert node_class.call_count == 1
    assert node_class.call_args[0][0] == "voice_agent_pipeline"
    # Four publishers created.
    node_instance = node_class.return_value
    assert node_instance.create_publisher.call_count == 4


@pytest.mark.asyncio
async def test_qos_profiles_match_architecture_spec(
    mock_rclpy: dict[str, MagicMock],
) -> None:
    """Per-topic QoS values match architecture.md §"Per-topic QoS"."""
    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

    pub = Ros2EventPublisher(_make_config())
    await pub.connect()

    captured = mock_rclpy["captured_qos"]
    assert len(captured) == 4  # type: ignore[arg-type]
    # Two latched (depth=1, transient_local) + two volatile (depth=8).
    latched = [c for c in captured if c.get("depth") == 1]  # type: ignore[union-attr]
    volatile = [c for c in captured if c.get("depth") == 8]  # type: ignore[union-attr]
    assert len(latched) == 2, f"expected 2 latched profiles, got {len(latched)}"
    assert len(volatile) == 2, f"expected 2 volatile profiles, got {len(volatile)}"
    for c in latched:
        assert c["reliability"] == "RELIABLE"  # type: ignore[index]
        assert c["durability"] == "TRANSIENT_LOCAL"  # type: ignore[index]
    for c in volatile:
        assert c["reliability"] == "RELIABLE"  # type: ignore[index]
        assert c["durability"] == "VOLATILE"  # type: ignore[index]


@pytest.mark.asyncio
async def test_topic_names_read_from_config_not_hardcoded(
    mock_rclpy: dict[str, MagicMock],
) -> None:
    """Custom topic names propagate to ``create_publisher``."""
    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

    pub = Ros2EventPublisher(
        _make_config(
            mood_topic="/custom/m",
            activity_topic="/custom/a",
            speech_emotion_topic="/custom/se",
            vocalization_topic="/custom/v",
        )
    )
    await pub.connect()

    create_publisher = mock_rclpy["node_module"].Node.return_value.create_publisher
    # Inspect the topic-name positional arg (index 1) of every call.
    topic_names_used = {call.args[1] for call in create_publisher.call_args_list}
    assert topic_names_used == {"/custom/m", "/custom/a", "/custom/se", "/custom/v"}


@pytest.mark.asyncio
async def test_publish_mood_serializes_event_to_json_string(
    mock_rclpy: dict[str, MagicMock],
) -> None:
    """``publish_mood`` calls the underlying publisher with the JSON envelope."""
    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

    pub = Ros2EventPublisher(_make_config())
    await pub.connect()

    event = MoodEvent(payload=MoodPayload(mood="calm", reason="test"))
    await pub.publish_mood(event)

    # Find the mock publisher created for the mood topic. The Ros2-
    # adapter stores them in self._publishers; introspect via its
    # internal map.
    mood_pub = pub._publishers["mood"]  # type: ignore[reportPrivateUsage]
    assert mood_pub.publish.call_count == 1
    msg = mood_pub.publish.call_args.args[0]
    assert msg.data == event.model_dump_json()


@pytest.mark.asyncio
async def test_publish_activity_speech_emotion_vocalization_dispatch_to_correct_topic(
    mock_rclpy: dict[str, MagicMock],
) -> None:
    """Each publish_<topic> method targets the matching publisher."""
    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher
    from voice_agent_pipeline.schemas.activity_event import (
        ActivityEvent,
        ActivityPayload,
    )
    from voice_agent_pipeline.schemas.speech_emotion_event import (
        SpeechEmotionEvent,
        SpeechEmotionPayload,
    )
    from voice_agent_pipeline.schemas.vocalization_event import (
        VocalizationEvent,
        VocalizationPayload,
    )

    pub = Ros2EventPublisher(_make_config())
    await pub.connect()

    activity = ActivityEvent(payload=ActivityPayload(state="starting"))
    speech = SpeechEmotionEvent(
        payload=SpeechEmotionPayload(
            emotion="content",
            source_tag="content",
            raw_tag="content",
            resolved_fallback=None,
            expression_data={"k": "v"},
        )
    )
    voc = VocalizationEvent(payload=VocalizationPayload(tag="laughter", tts_supported=True))

    await pub.publish_activity(activity)
    await pub.publish_speech_emotion(speech)
    await pub.publish_vocalization(voc)

    assert pub._publishers["activity"].publish.call_count == 1  # type: ignore[reportPrivateUsage]
    assert pub._publishers["speech_emotion"].publish.call_count == 1  # type: ignore[reportPrivateUsage]
    assert pub._publishers["vocalization"].publish.call_count == 1  # type: ignore[reportPrivateUsage]
    # And mood was NOT touched.
    assert pub._publishers["mood"].publish.call_count == 0  # type: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_connect_failure_wraps_in_startup_validation_error(
    mock_rclpy: dict[str, MagicMock],
) -> None:
    """rclpy.init failure → StartupValidationError (fail-fast)."""
    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

    mock_rclpy["rclpy"].init.side_effect = RuntimeError("DDS not available")
    pub = Ros2EventPublisher(_make_config())
    with pytest.raises(StartupValidationError):
        await pub.connect()


@pytest.mark.asyncio
async def test_publish_failure_wraps_in_publisher_error(
    mock_rclpy: dict[str, MagicMock],
) -> None:
    """A mid-stream publish failure raises PublisherError uncaught."""
    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

    pub = Ros2EventPublisher(_make_config())
    await pub.connect()

    pub._publishers["mood"].publish.side_effect = RuntimeError("DDS down")  # type: ignore[reportPrivateUsage]
    event = MoodEvent(payload=MoodPayload(mood="calm"))
    with pytest.raises(PublisherError):
        await pub.publish_mood(event)


@pytest.mark.asyncio
async def test_disconnect_idempotent(mock_rclpy: dict[str, MagicMock]) -> None:
    """Calling ``disconnect`` twice doesn't raise."""
    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

    pub = Ros2EventPublisher(_make_config())
    await pub.connect()
    await pub.disconnect()
    await pub.disconnect()  # second call no-ops via the latch
    assert mock_rclpy["rclpy"].shutdown.call_count == 1


@pytest.mark.asyncio
async def test_is_healthy_returns_false_before_connect(
    mock_rclpy: dict[str, MagicMock],
) -> None:
    """Pre-connect, the publisher reports unhealthy."""
    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

    pub = Ros2EventPublisher(_make_config())
    assert await pub.is_healthy() is False
