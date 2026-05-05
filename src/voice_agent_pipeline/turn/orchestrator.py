"""OrchestratorClient Protocol — the slow-path streaming-dispatch seam.

The orchestrator daemon (an external service, owned by a sibling project)
handles complex grounded turns over Server-Sent Events. v1 impl is
:class:`HttpOrchestratorClient` (Story 4.2). The Protocol stays stable
across that implementation and any future replacement (e.g. a local-only
mock for offline dev).
"""

from collections.abc import AsyncIterator
from typing import Protocol

from voice_agent_pipeline.schemas.stream import OrchestratorStreamEvent


class OrchestratorClient(Protocol):
    """Streaming SSE dispatch to the orchestrator daemon (v1: HttpOrchestratorClient, Story 4.2)."""

    async def dispatch(
        self, transcript: str, session_id: str
    ) -> AsyncIterator[OrchestratorStreamEvent]:
        """Open an SSE stream for a slow-path turn.

        Args:
            transcript: The user's spoken utterance, post-STT.
            session_id: Stable per-conversation ID so the orchestrator can
                stitch multi-turn context. The pipeline allocates this once
                per session (Story 4.5 wires the lifecycle plumbing).

        Yields:
            :data:`OrchestratorStreamEvent` instances — typed via the
            discriminated union in ``schemas/stream.py``. Stream terminates
            with a :class:`TurnEndEvent`.
        """
        ...

    async def cancel(self, session_id: str) -> None:
        """Abort an in-flight turn.

        Called when the user barges in (Story 5.1) or when the lifecycle
        transitions away from THINKING/SPEAKING. The implementation may
        no-op if the stream has already ended.

        Args:
            session_id: The session whose stream should be cancelled.
        """
        ...
