"""``SpeechEmotionEvent`` — typed event on the ``speech_emotion`` topic.

Story 3.4 — promotes :class:`SpeechEmotionPayload` from its interim
home in ``splitter/mapping.py`` (Story 3.2) to its canonical location
here, and wraps it in the :class:`EventEnvelope` mixin.

Schema-3 boundary repair (sprint-change-proposal-2026-05-10): the
``expression_data: dict[str, Any]`` field — previously the documented
"open-extensibility seam" — has been removed. It carried OLAF-specific
renderer vocabulary (pose / LED / eye state) onto the wire, violating
the project's consumer-agnostic-publisher boundary. The renderer-side
mapping now lives in the embodiment project's own config, keyed on the
canonical ``emotion`` name. Subscribers learn the resolved canonical
name plus the audit metadata (``source_tag`` / ``raw_tag`` /
``resolved_fallback``) and own the rendering decision themselves.
"""

from pydantic import BaseModel, ConfigDict

from voice_agent_pipeline.schemas.envelope import EventEnvelope


class SpeechEmotionPayload(BaseModel):
    """Inner payload of :class:`SpeechEmotionEvent`.

    Architecture.md §"Stable contracts" — the field set is the wire
    contract. Adding a field is forward-compat (subscribers ignore
    unknowns); renaming or removing requires a ``schema_version`` bump
    (the schema-3 repair removed the ``expression_data`` field; that
    bump is captured in :class:`EventEnvelope`).

    Attributes:
        emotion: Resolved first-class emotion name (always one of the
            entries in ``mapping.emotions``). For a fallback-family hit,
            this is the family's ``maps_to``; for unmapped, this is
            ``mapping.unknown.maps_to``. Consumers key their own
            renderer mapping (pose / LED / etc.) on this name.
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
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    emotion: str
    source_tag: str
    audio_frame_id: str | None = None
    raw_tag: str
    resolved_fallback: str | None


class SpeechEmotionEvent(EventEnvelope):
    """Event published on the ``speech_emotion`` topic (volatile, depth=8).

    Volatile QoS depth=8 (architecture.md §"Per-topic QoS") — recent
    emotions only; new subscribers don't replay history. Audio-anchored
    events are punctual and high-cadence; volatile is the right
    durability profile.
    """

    payload: SpeechEmotionPayload  # type: ignore[assignment]
