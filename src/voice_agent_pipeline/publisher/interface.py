"""ExpressionPublisher Protocol — the broadcast publisher seam.

The pipeline ends at typed event publish on configurable channels
(memory: project_pipeline_scope_boundary). The Protocol below is the
**stable** API every concrete publisher must implement; v1 transport is
ROS 2 / DDS via :class:`Ros2ExpressionPublisher` (Story 3.4). Future
transports (e.g. NATS, MQTT, in-process queue for tests) plug in here
without changing call sites.
"""

from typing import Protocol

from voice_agent_pipeline.schemas.expression_event import ExpressionEvent
from voice_agent_pipeline.schemas.lifecycle_event import LifecycleEvent


class ExpressionPublisher(Protocol):
    """Broadcast publisher behind a stable interface; v1 transport is ROS 2 / DDS."""

    async def connect(self) -> None:
        """Open the underlying transport — must be called before any publish.

        Stories 3.4 and 5.4 call this from the lifecycle startup phase. A
        failure here raises :class:`PublisherError` and aborts startup.
        """
        ...

    async def disconnect(self) -> None:
        """Close the transport cleanly. Idempotent — safe to call multiple times."""
        ...

    async def is_healthy(self) -> bool:
        """Return True if the transport is connected and ready to publish.

        Used by the startup probe (Story 3.4) and by the lifecycle FSM
        (Story 4.4) to decide whether to gate transitions on publisher
        readiness.
        """
        ...

    async def publish_expression(self, event: ExpressionEvent) -> None:
        """Publish a typed expression event on the configured expression channel.

        Args:
            event: A frozen :class:`ExpressionEvent`. Implementations should
                JSON-encode it via ``event.model_dump_json()`` and put the
                resulting string on the wire (architecture's v1 wire format
                simplification).
        """
        ...

    async def publish_lifecycle(self, event: LifecycleEvent) -> None:
        """Publish a typed lifecycle event on the configured lifecycle channel.

        Args:
            event: A frozen :class:`LifecycleEvent`. Same JSON-encoding
                contract as :meth:`publish_expression`.
        """
        ...
