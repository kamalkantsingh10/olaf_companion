"""Publisher package — four-topic event broadcast surface (Story 3.5).

Public surface:

- :class:`EventPublisher` — Protocol every adapter implements.
- :class:`LogEventPublisher` — in-memory adapter for tests + dev.
- :func:`build_publisher` — config-driven factory.

The production adapter :class:`Ros2EventPublisher` is **not**
re-exported from this module. It carries the only ``rclpy`` import
in the codebase (architecture.md §"Architectural Boundaries" —
boundary concentration). Re-exporting from here would force ``rclpy``
to be importable everywhere; callers who need the production adapter
import directly:

.. code-block:: python

    from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

The :func:`build_publisher` factory does the same — local imports
inside the matching ``if`` branch defer the ``rclpy`` dependency
until the ``"ros2"`` adapter is actually requested.
"""

from voice_agent_pipeline.config.setup import PublisherConfig
from voice_agent_pipeline.errors import ConfigError
from voice_agent_pipeline.publisher.interface import EventPublisher
from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher

__all__ = ["EventPublisher", "LogEventPublisher", "build_publisher"]


def build_publisher(config: PublisherConfig) -> EventPublisher:
    """Construct an :class:`EventPublisher` from config.

    Dispatches on ``config.adapter``. The :class:`Ros2EventPublisher`
    branch imports its module locally so a ``log`` adapter run on a
    host without ``rclpy`` installed never triggers the import.
    """
    if config.adapter == "ros2":
        # Local import: defers the rclpy dependency until actually needed.
        from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher

        return Ros2EventPublisher(config)
    if config.adapter == "log":
        return LogEventPublisher()
    # Unreachable in practice — pydantic's Literal["ros2", "log"]
    # rejects other values at config-load time. Defense-in-depth.
    raise ConfigError(reason=f"unknown publisher.adapter: {config.adapter}")
