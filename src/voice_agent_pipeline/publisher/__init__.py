"""Publishers - typed expression + lifecycle event publishers (Story 3.4).

Re-exports :class:`ExpressionPublisher` so callers can write
``from voice_agent_pipeline.publisher import ExpressionPublisher``. The
v1 concrete impl :class:`Ros2ExpressionPublisher` lands in Story 3.4.
"""

from voice_agent_pipeline.publisher.interface import ExpressionPublisher

__all__ = ["ExpressionPublisher"]
