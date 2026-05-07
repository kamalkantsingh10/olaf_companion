"""``SpeechEmotionEvent`` — typed event on the ``speech_emotion`` topic.

Story 3.4 — promotes :class:`SpeechEmotionPayload` from its interim
home in ``splitter/mapping.py`` (Story 3.2) to its canonical location
here, and wraps it in the :class:`EventEnvelope` mixin.

The payload field set is **identical** to Story 3.2's interim version
— the migration is a relocation, not a redesign. Story 3.2's resolver
return type and Story 3.3's segmenter ``Segment.speech_emotion_payload``
field both update their imports (no field-level changes).
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from voice_agent_pipeline.schemas.envelope import EventEnvelope


class SpeechEmotionPayload(BaseModel):
    """Inner payload of :class:`SpeechEmotionEvent`.

    Architecture.md §"Stable contracts" — the field set is the wire
    contract. Adding a field is forward-compat (subscribers ignore
    unknowns); renaming or removing requires a ``schema_version`` bump.

    Attributes:
        emotion: Resolved first-class emotion name (always one of the
            keys in ``mapping.emotions``). For a fallback-family hit,
            this is the family's ``maps_to``; for unmapped, this is
            ``mapping.unknown.maps_to``.
        source_tag: The original tag the splitter parsed from the LLM
            stream — same as ``raw_tag`` for first-class hits, the
            family member name for fallback hits, the bogus tag for
            unmapped.
        audio_frame_id: Pipecat audio-frame id this event aligns to.
            Story 3.7 populates it; the resolver leaves it None.
        raw_tag: The verbatim tag the LLM emitted, for audit trail.
            FR20 — consumers see what was asked AND what was rendered.
        resolved_fallback: ``None`` for first-class hits; the family
            name for fallback hits; ``"unknown"`` for the unmapped
            fall-through. FR21.
        expression_data: Opaque dict copied from
            ``ExpressionMapConfig.emotions[<emotion>].expression_data``.
            ``Any``-typed inner values are the documented extensibility
            seam (CLAUDE.md rule #3 carve-out).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    emotion: str
    source_tag: str
    audio_frame_id: str | None = None
    raw_tag: str
    resolved_fallback: str | None
    # The single architecturally-allowed Any seam (architecture.md
    # §"Type System Conventions"). New fields ship via expression_map.yaml
    # edits + this dict — no schema_version bump required.
    expression_data: dict[str, Any]


class SpeechEmotionEvent(EventEnvelope):
    """Event published on the ``speech_emotion`` topic (volatile, depth=8).

    Volatile QoS depth=8 (architecture.md §"Per-topic QoS") — recent
    emotions only; new subscribers don't replay history. Audio-anchored
    events are punctual and high-cadence; volatile is the right
    durability profile.
    """

    payload: SpeechEmotionPayload  # type: ignore[assignment]
