"""TurnRouter — fast/slow-path routing for transcripts (Story 2.4).

The router consumes a transcript + STT confidence and returns a
:class:`RouteDecision` naming the dispatch target (Talker / orchestrator)
and the text that target should consume. Story 2.4 implements the v1
behaviour: every decision routes to Talker. Low-confidence transcripts
route to Talker with a clarification prompt **substituted for** the
user's noisy text, so the model asks for a repeat rather than guessing
at the bad transcript.

Story 4.3 will extend the router with config-driven keyword/regex rules
that escalate complex turns to the orchestrator. The Protocol seam +
factory pattern means that escalation is a method-body change, not an
API change — :meth:`TurnRouter.route` keeps its current signature.

Architectural intent (architecture.md §"Streaming + Concurrency"): the
TurnRouter is **pure routing logic** (synchronous, no I/O,
unit-testable in isolation). The Pipecat-side dispatch — calling
:meth:`TalkerClient.complete` and emitting downstream frames — lives
in :class:`TurnDispatchProcessor` (in ``pipeline.py``). Splitting
"decide where to go" from "do the async I/O" lets each piece stay
single-purpose.
"""

import random
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict

from voice_agent_pipeline.config.setup import SttConfig
from voice_agent_pipeline.turn.orchestrator import OrchestratorClient
from voice_agent_pipeline.turn.talker import TalkerClient

log = structlog.get_logger(__name__)


class RouteDecision(BaseModel):
    """Frozen decision record returned by :meth:`TurnRouter.route`.

    ``extra="forbid"`` so a future story extending RouteDecision (e.g.,
    Story 4.3 adding ``slow_path_intent`` metadata for orchestrator
    routing) bumps the schema deliberately rather than silently growing
    fields.

    Attributes:
        target: Where the dispatcher should send this turn —
            ``"talker"`` (fast path) or ``"orchestrator"`` (slow,
            grounded path; Story 4.3 wires the actual escalation; v1
            never emits this value but the union member stays so the
            dispatcher's exhaustive ``NotImplementedError`` branch
            type-checks cleanly).
        text: What the target should consume. For high-confidence
            transcripts this is the user's verbatim text. For
            low-confidence, this is one entry picked at random from
            :attr:`SttConfig.clarification_prompts` (Story 4.5) —
            the user's noisy text is dropped on the floor and the
            clarification phrase plays directly via Story 3.7's
            short-circuit (no LLM round-trip).
        clarification: ``True`` when this is a clarification dialog —
            lets downstream logging distinguish clarification turns
            without re-checking the confidence value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: Literal["talker", "orchestrator"]
    text: str
    clarification: bool


class TurnRouter:
    """Synchronous routing logic — no I/O, no coroutines.

    Holds the Talker (and, post-Epic-4, the orchestrator) so the
    pipeline-side dispatcher can pick the configured client off the
    same router object that produced the decision. v1 stores the
    :class:`OrchestratorClient` Protocol but never calls it — Story 4.3
    wires real dispatch and adds keyword/regex rules to :meth:`route`.

    Architecture note: the architecture's Batch 2 decision describes
    "Single TurnRouter processor owning both Talker + orchestrator
    client" — this class IS that owner. The processor wrapper
    (:class:`TurnDispatchProcessor` in ``pipeline.py``) consumes
    Pipecat frames and calls into this router; splitting the two
    keeps the routing logic synchronous + unit-testable while letting
    the processor handle Pipecat's async lifecycle.
    """

    def __init__(
        self,
        stt_config: SttConfig,
        talker: TalkerClient,
        orchestrator: OrchestratorClient | None = None,
    ) -> None:
        self._threshold = stt_config.low_confidence_threshold
        # Story 4.5: list of static clarification phrases — picked
        # at random per low-confidence turn. The list is non-empty
        # by SttConfig's model_validator, so random.choice never
        # fails on []. The actual strings live in setup.toml under
        # ``[stt] clarification_prompts``.
        self._clarification_prompts = stt_config.clarification_prompts
        # Public attributes (no underscore) — the dispatcher in
        # pipeline.py reads these directly. Wrapping in private +
        # accessor would be ceremony; the dispatcher is in this
        # codebase and the router/dispatcher pair is one abstraction.
        self.talker = talker
        self.orchestrator = orchestrator

    def route(self, transcript: str, confidence: float) -> RouteDecision:
        """Decide where this transcript goes.

        Inclusive at the threshold (``>=``) — a transcript with
        ``confidence == low_confidence_threshold`` takes the high-
        confidence path. Documented in the test
        ``test_threshold_boundary_inclusive_at_threshold``.

        Args:
            transcript: The user's transcribed utterance from STT.
            confidence: STT-reported confidence in ``[0.0, 1.0]``.

        Returns:
            A :class:`RouteDecision`. v1 always emits ``target="talker"``;
            Story 4.3 will introduce ``target="orchestrator"``.
        """
        if confidence >= self._threshold:
            return RouteDecision(
                target="talker",
                text=transcript,
                clarification=False,
            )
        # Low-confidence path: drop the noisy text, substitute a
        # randomly-picked clarification phrase. Story 3.7's
        # short-circuit in TurnDispatchProcessor emits this verbatim
        # as a TalkerResponseFrame — no LLM round-trip — so the
        # phrase IS what the user hears.
        # ruff S311: random.choice is fine here — clarification variety
        # is UX, not cryptographic. No security boundary.
        picked = random.choice(self._clarification_prompts)  # noqa: S311
        log.info("clarification.picked", text=picked)
        return RouteDecision(
            target="talker",
            text=picked,
            clarification=True,
        )
