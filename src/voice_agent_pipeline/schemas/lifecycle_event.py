"""LifecycleEvent — typed broadcast event for pipeline lifecycle transitions.

Published on the configured lifecycle channel whenever the pipeline's
top-level state machine transitions (Story 4.4 implements the FSM; Story
3.4 wires the publisher; Story 3.5 emits the first event).

The five states form a simple ring: ``IDLE → SLEEPING`` (after
``idle_to_sleeping_seconds`` of inactivity), ``LISTENING`` (wake word fired),
``THINKING`` (turn dispatched to talker / orchestrator), ``SPEAKING``
(TTS streaming back), then back to ``IDLE``.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LifecycleEvent(BaseModel):
    """Typed lifecycle state-change event published on the broadcast bus.

    Attributes:
        schema_version: Integer marker, validated by callers via
            :func:`assert_schema_version` at parse boundaries.
        event_type: Discriminator literal — always ``"lifecycle"``.
        state: The state the pipeline has transitioned **into**. The five
            allowed values match the architecture's lifecycle FSM. New
            states require both a code change and a schema_version bump
            (CLAUDE.md rule #6).
        timestamp_ns: Monotonic nanosecond timestamp at transition.
        payload: Optional extension slot — defaults to an empty dict.
            Most lifecycle transitions don't need extra data; the slot
            exists so adding context (e.g. ``reason`` for a SLEEPING
            transition) doesn't require a schema bump.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    event_type: Literal["lifecycle"]
    # Five-state lifecycle FSM. Order is irrelevant to validation (Literal
    # is a set semantically), but the listed order matches the typical
    # session arc: SLEEPING → LISTENING → THINKING → SPEAKING → IDLE.
    state: Literal["SLEEPING", "LISTENING", "THINKING", "SPEAKING", "IDLE"]
    timestamp_ns: int
    # default_factory rather than ``= {}`` to avoid the classic Python
    # mutable-default-argument trap — every instance gets a fresh dict.
    payload: dict[str, Any] = Field(default_factory=dict)
