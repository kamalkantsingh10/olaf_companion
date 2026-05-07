"""Tests for :mod:`voice_agent_pipeline.schemas.speech_emotion_event`.

The migration from ``splitter/mapping.py`` to the canonical home in
``schemas/`` does not change the field set — Story 3.2's existing
tests for the resolver continue to drive this payload type; this file
adds schema-shape coverage.
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
        "expression_data": {"led_color": "#ffa040"},
    }
    base.update(overrides)
    return SpeechEmotionPayload(**base)  # type: ignore[arg-type]


def test_minimal_valid_speech_emotion_event() -> None:
    event = SpeechEmotionEvent(payload=_payload())
    assert event.payload.emotion == "excited"
    assert event.payload.audio_frame_id is None  # default
    assert event.schema_version == 2


def test_payload_with_fallback_metadata() -> None:
    payload = _payload(
        emotion="excited",
        source_tag="enthusiastic",
        raw_tag="enthusiastic",
        resolved_fallback="high_energy_positive",
    )
    assert payload.resolved_fallback == "high_energy_positive"


def test_payload_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        SpeechEmotionPayload(
            emotion="excited",
            source_tag="excited",
            raw_tag="excited",
            resolved_fallback=None,
            expression_data={},
            bogus=1,  # type: ignore[call-arg]
        )


def test_expression_data_accepts_arbitrary_keys() -> None:
    """``expression_data: dict[str, Any]`` is the open-extensibility seam."""
    payload = _payload(expression_data={"any_new_field": [1, 2, 3], "nested": {"k": "v"}})
    assert payload.expression_data["any_new_field"] == [1, 2, 3]
    assert payload.expression_data["nested"] == {"k": "v"}


def test_payload_is_frozen() -> None:
    payload = _payload()
    with pytest.raises(ValidationError):
        payload.emotion = "happy"  # type: ignore[misc]


def test_audio_frame_id_optional_and_str() -> None:
    payload = _payload(audio_frame_id="frame-42")
    assert payload.audio_frame_id == "frame-42"
