"""Tests for :mod:`voice_agent_pipeline.schemas.speech_emotion_event`.

The schema-3 boundary repair (sprint-change-proposal-2026-05-10)
removed the ``expression_data: dict[str, Any]`` field that was the
documented "open-extensibility seam" — embodiment vocabulary
(pose / LED) belongs on the consumer side, not on this wire payload.
The remaining fields are the audit-trail surface only:
``emotion`` / ``source_tag`` / ``raw_tag`` / ``resolved_fallback``
plus the optional ``audio_frame_id`` slot Story 3.7 fills.
"""

import pytest
from pydantic import ValidationError

from voice_agent_pipeline.schemas.speech_emotion_event import (
    SpeechEmotionEvent,
    SpeechEmotionPayload,
)


def _payload(**overrides: object) -> SpeechEmotionPayload:
    """Minimal-valid SpeechEmotionPayload with optional overrides."""
    base: dict[str, object] = {
        "emotion": "excited",
        "source_tag": "excited",
        "raw_tag": "excited",
        "resolved_fallback": None,
    }
    base.update(overrides)
    return SpeechEmotionPayload(**base)  # type: ignore[arg-type]


def test_minimal_valid_speech_emotion_event() -> None:
    event = SpeechEmotionEvent(payload=_payload())
    assert event.payload.emotion == "excited"
    assert event.payload.audio_frame_id is None  # default
    assert event.schema_version == 3


def test_payload_with_fallback_metadata() -> None:
    payload = _payload(
        emotion="excited",
        source_tag="enthusiastic",
        raw_tag="enthusiastic",
        resolved_fallback="high_energy_positive",
    )
    assert payload.resolved_fallback == "high_energy_positive"


def test_payload_extra_forbid() -> None:
    """``extra="forbid"`` rejects unknown fields at construction.

    Post-boundary-repair, attempting to pass ``expression_data`` (the
    removed field) is itself an extra-key violation — the test doubles
    as a regression alarm if the field accidentally returns.
    """
    with pytest.raises(ValidationError):
        SpeechEmotionPayload(
            emotion="excited",
            source_tag="excited",
            raw_tag="excited",
            resolved_fallback=None,
            bogus=1,  # type: ignore[call-arg]
        )


def test_removed_expression_data_field_is_rejected() -> None:
    """Passing the removed ``expression_data`` field raises ``ValidationError``.

    Wire-contract regression alarm: the field was deleted in the
    schema-3 boundary repair. Re-introducing it (intentionally or
    accidentally) breaks ``extra="forbid"`` and surfaces here.
    """
    with pytest.raises(ValidationError):
        SpeechEmotionPayload(
            emotion="excited",
            source_tag="excited",
            raw_tag="excited",
            resolved_fallback=None,
            expression_data={"led_color": "#ffa040"},  # type: ignore[call-arg]
        )


def test_payload_is_frozen() -> None:
    payload = _payload()
    with pytest.raises(ValidationError):
        payload.emotion = "happy"  # type: ignore[misc]


def test_audio_frame_id_optional_and_str() -> None:
    payload = _payload(audio_frame_id="frame-42")
    assert payload.audio_frame_id == "frame-42"
