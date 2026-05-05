"""ExpressionEvent — typed broadcast event for embodiment expression updates.

Published on the configured expression channel (Story 3.4 wires the ROS 2
publisher; Story 3.5 first calls it from the splitter state machine).

Wire-format intent: this model JSON-encodes cleanly into a single
``std_msgs/String`` payload — that's the v1 wire format simplification
(architecture.md §"V1 wire format simplification"). Subscribers parse the
JSON and may safely **ignore** unknown ``payload`` keys, making payload
extension forward-compat.

The model is ``frozen=True`` so once an event is constructed it cannot be
mutated — which means it's safe to cache, compare, and pass between async
tasks without defensive copies.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ExpressionEvent(BaseModel):
    """Typed expression event published on the broadcast bus.

    Attributes:
        schema_version: Integer marker, validated by callers via
            :func:`assert_schema_version` at parse boundaries.
        event_type: Discriminator literal — always ``"expression"``.
            Subscribers that demultiplex multiple event types on a shared
            channel use this to dispatch.
        emotion: Resolved emotion name (e.g. ``"excited"``, ``"sad"``).
            Comes from the splitter's mapping resolver (Story 3.2).
        source_tag: Original Cartesia tag this expression resolved from
            (e.g. ``"<laughs>"``, ``"<sigh>"``). Carrying it preserves
            traceability when an expression came from a fallback family
            rather than a direct map (architecture.md §"Splitter").
        audio_frame_id: Frame ID of the audio chunk this expression aligns
            to, or ``None`` for non-audio-aligned events (e.g. lifecycle
            transitions). Lets embodiment renderers sync expressions to
            speech timing.
        timestamp_ns: Monotonic nanosecond timestamp at event creation.
            Subscribers use this to detect lag and order events.
        payload: Open extensibility slot. Embodiment-specific fields
            (LED intensity, haptic strength, etc.) live here so we don't
            bump ``schema_version`` for every new device.
    """

    # frozen=True → immutable after construction; safe across async tasks.
    # extra="forbid" → typos in producer code fail loudly at construction time.
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    event_type: Literal["expression"]
    emotion: str
    source_tag: str
    audio_frame_id: str | None
    timestamp_ns: int
    # Intentionally typed as ``dict[str, Any]`` (not stricter): this is the
    # documented extensibility seam. Architecture.md §"Stable contracts"
    # explicitly endorses an open payload here.
    payload: dict[str, Any]
