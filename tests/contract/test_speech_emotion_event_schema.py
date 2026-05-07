"""Contract — :class:`SpeechEmotionEvent` JSON round-trip + schema_version."""

import pytest

from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import SchemaVersionError
from voice_agent_pipeline.schemas.speech_emotion_event import (
    SpeechEmotionEvent,
    SpeechEmotionPayload,
)


def test_speech_emotion_event_round_trips_through_json() -> None:
    original = SpeechEmotionEvent(
        payload=SpeechEmotionPayload(
            emotion="excited",
            source_tag="enthusiastic",
            raw_tag="enthusiastic",
            resolved_fallback="high_energy_positive",
            expression_data={"led_color": "#ffa040", "led_intensity": 0.9},
        )
    )
    wire = original.model_dump_json()
    rebuilt = SpeechEmotionEvent.model_validate_json(wire)

    assert rebuilt.payload.emotion == "excited"
    assert rebuilt.payload.raw_tag == "enthusiastic"
    assert rebuilt.payload.resolved_fallback == "high_energy_positive"
    assert rebuilt.payload.expression_data == {
        "led_color": "#ffa040",
        "led_intensity": 0.9,
    }


def test_speech_emotion_event_old_schema_version_rejected() -> None:
    event = SpeechEmotionEvent(
        payload=SpeechEmotionPayload(
            emotion="neutral",
            source_tag="neutral",
            raw_tag="neutral",
            resolved_fallback=None,
            expression_data={"k": "v"},
        ),
        schema_version=1,
    )
    with pytest.raises(SchemaVersionError):
        assert_schema_version(event.schema_version, source="SpeechEmotionEvent")
