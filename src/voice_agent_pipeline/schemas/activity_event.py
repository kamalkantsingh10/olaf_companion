"""``ActivityEvent`` — typed event on the ``activity`` topic.

Story 3.4 owns the ``ActivityState`` + ``WorkingSubmode`` Literals;
Story 4.3 (activity FSM core) builds ``activity/machine.py`` consuming
them. The schema is the wire contract — invariants on the payload's
field combinations enforced via pydantic ``model_validator``.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from voice_agent_pipeline.schemas.envelope import EventEnvelope

#: 7-state activity FSM (architecture.md §"Activity FSM + Mood Control
#: + Tool Registry" — added 2026-05-06 in the direction-shift correct-
#: course).
ActivityState = Literal[
    "starting",
    "sleeping",
    "waking",
    "listening",
    "working",
    "speaking",
    "going_to_sleep",
]

#: Sub-mode while ``state="working"``. Indicates whether OLAF is
#: thinking locally (Talker fast-path) or delegating to the
#: orchestrator (slow path).
WorkingSubmode = Literal["thinking", "delegating"]


class ActivityPayload(BaseModel):
    """Inner payload of :class:`ActivityEvent`.

    Invariants (enforced by ``model_validator``):

    - ``working_submode`` is non-``None`` if and only if
      ``state == "working"``. Other states never carry a sub-mode;
      working state never lacks one.
    - ``from_state`` is ``None`` if and only if this is the initial
      ``starting`` publish. Every transition AFTER startup carries
      the prior state; the very first publish has no prior state.

    Attributes:
        state: The activity state OLAF transitioned **into**.
        working_submode: When ``state="working"``, whether OLAF is
            ``"thinking"`` (Talker) or ``"delegating"`` (orchestrator).
        transition_reason: Optional human-readable reason — e.g.
            ``"wake_word"``, ``"go_to_sleep tool"``, ``"vad_silence"``.
        from_state: The prior state (or ``None`` on initial startup).
            Subscribers can build a transition history by chaining
            events on ``from_state → state``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ActivityState
    working_submode: WorkingSubmode | None = None
    transition_reason: str | None = None
    from_state: ActivityState | None = None

    @model_validator(mode="after")
    def _check_working_submode(self) -> "ActivityPayload":
        """``working_submode`` allowed iff ``state == 'working'``."""
        if self.state == "working" and self.working_submode is None:
            raise ValueError("working_submode required when state='working'")
        if self.state != "working" and self.working_submode is not None:
            raise ValueError("working_submode allowed only when state='working'")
        return self

    @model_validator(mode="after")
    def _check_from_state(self) -> "ActivityPayload":
        """``from_state`` is ``None`` iff this is the initial ``starting`` publish."""
        if self.state == "starting" and self.from_state is not None:
            raise ValueError("from_state must be None when state='starting'")
        if self.state != "starting" and self.from_state is None:
            raise ValueError("from_state required when state != 'starting'")
        return self


class ActivityEvent(EventEnvelope):
    """Event published on the ``activity`` topic (latched, transient_local).

    Same QoS as ``mood`` (latched/transient_local depth=1) — late
    subscribers learn the current activity state at connect
    (architecture.md §"Per-topic QoS"). Story 4.3's FSM publishes on
    every transition.
    """

    payload: ActivityPayload  # type: ignore[assignment]
