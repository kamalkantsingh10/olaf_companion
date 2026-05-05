"""pydantic event schemas - typed contracts for emitted events (Story 1.4).

Re-exports the three event surfaces so consumers can write::

    from voice_agent_pipeline.schemas import ExpressionEvent, LifecycleEvent

instead of reaching into the per-event submodules. Story 4.2's SSE handler
imports ``OrchestratorStreamEvent`` from here.
"""

from voice_agent_pipeline.schemas.expression_event import ExpressionEvent
from voice_agent_pipeline.schemas.lifecycle_event import LifecycleEvent
from voice_agent_pipeline.schemas.stream import OrchestratorStreamEvent

__all__ = ["ExpressionEvent", "LifecycleEvent", "OrchestratorStreamEvent"]
