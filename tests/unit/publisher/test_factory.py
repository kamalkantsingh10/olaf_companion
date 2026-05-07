"""Tests for :func:`build_publisher` — config-driven dispatch."""

from voice_agent_pipeline.config.setup import PublisherConfig, TopicNames
from voice_agent_pipeline.publisher import build_publisher
from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher


def test_build_publisher_log_adapter() -> None:
    """``adapter="log"`` returns a :class:`LogEventPublisher`."""
    config = PublisherConfig(adapter="log", topics=TopicNames())
    pub = build_publisher(config)
    assert isinstance(pub, LogEventPublisher)


def test_build_publisher_log_adapter_does_not_import_rclpy() -> None:
    """Building a log adapter must not trigger the rclpy import.

    The architectural promise: ``rclpy`` is concentrated in
    ``publisher/ros2.py``. A ``log`` adapter run on a CI host without
    ROS 2 installed must succeed.
    """
    import sys

    # Pre-condition: rclpy may or may not be loaded; we just verify
    # that building a log adapter doesn't load it on hosts where it
    # wasn't already.
    pre_loaded = "rclpy" in sys.modules

    config = PublisherConfig(adapter="log", topics=TopicNames())
    build_publisher(config)

    post_loaded = "rclpy" in sys.modules
    if not pre_loaded:
        assert not post_loaded, "build_publisher(log) leaked an rclpy import"
