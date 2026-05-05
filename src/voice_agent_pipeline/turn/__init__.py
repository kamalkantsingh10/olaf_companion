"""Turn router - fast/slow-path dispatch for user utterances (Stories 2.4, 4.3).

Re-exports the three Protocol seams that the router will consume:
:class:`TalkerClient` (fast path), :class:`OrchestratorClient` (slow path),
:class:`BeliefStateClient` (per-turn belief read).
"""

from voice_agent_pipeline.turn.beliefs import BeliefStateClient
from voice_agent_pipeline.turn.orchestrator import OrchestratorClient
from voice_agent_pipeline.turn.talker import TalkerClient

__all__ = ["BeliefStateClient", "OrchestratorClient", "TalkerClient"]
