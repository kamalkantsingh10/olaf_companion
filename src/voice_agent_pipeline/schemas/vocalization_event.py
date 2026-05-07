"""``VocalizationEvent`` — typed event on the ``vocalization`` topic.

Story 3.4 — promotes :class:`VocalizationPayload` from its interim
home in ``splitter/mapping.py`` (Story 3.2) to its canonical location
here, and wraps it in the :class:`EventEnvelope` mixin.

Field set is identical to Story 3.2's interim version. Story 3.2's
``resolve_vocalization`` return type and Story 3.3's segmenter
``Segment.vocalization_payloads`` field both update their imports
(no field-level changes).
"""

from pydantic import BaseModel, ConfigDict

from voice_agent_pipeline.schemas.envelope import EventEnvelope


class VocalizationPayload(BaseModel):
    """Inner payload of :class:`VocalizationEvent`.

    Vocalizations are flatter than emotions — no fallback families in
    v1. The ``tts_supported`` flag drives Story 3.3's segmenter's
    keep-vs-strip behavior in TTS text (FR25).

    Attributes:
        tag: The vocalization name (e.g. ``"laughter"``, ``"sigh"``).
        audio_frame_id: Story 3.7 populates; resolver leaves None.
        tts_supported: Whether Cartesia renders audio for this tag,
            sourced from ``mapping.vocalizations[<tag>].tts_supported``.
            Unknown tags get ``False`` (safe default — strip from TTS).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    audio_frame_id: str | None = None
    tts_supported: bool


class VocalizationEvent(EventEnvelope):
    """Event published on the ``vocalization`` topic (volatile, depth=8).

    Same QoS profile as ``speech_emotion`` (volatile, depth=8) — both
    are punctual audio-anchored events.
    """

    payload: VocalizationPayload  # type: ignore[assignment]
