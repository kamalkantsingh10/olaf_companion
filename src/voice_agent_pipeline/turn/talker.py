"""TalkerClient Protocol — the in-pipeline LLM seam.

The Talker handles **fast-path** turns (short, conversational, non-grounded
questions). Slow-path turns go through :class:`OrchestratorClient` instead
(see ``orchestrator.py``). v1 impl is :class:`AnthropicTalker` (Story 2.2).
"""

from typing import Any, Protocol


class TalkerClient(Protocol):
    """In-pipeline LLM. v1 impl is AnthropicTalker (Story 2.2)."""

    async def complete(self, transcript: str, context: dict[str, Any] | None = None) -> str:
        """Produce a single-shot response to a transcript.

        Args:
            transcript: The user's spoken utterance, post-STT.
            context: Optional belief-state grab from
                :class:`BeliefStateClient` (Story 4.1). When ``None``, the
                Talker runs context-free (the architecture's default for
                pure conversational turns).

        Returns:
            The generated text response. The pipeline's splitter (Story 3.3)
            consumes this output verbatim.
        """
        ...
