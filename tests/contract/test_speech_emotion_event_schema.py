"""Contract — :class:`SpeechEmotionEvent` JSON round-trip + schema_version."""

import pytest

from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import SchemaVersionError
from voice_agent_pipeline.schemas.speech_emotion_event import (
    SpeechEmotionEvent,
    SpeechEmotionPayload,
)


def test_speech_emotion_event_round_trips_through_json() -> None:
    """A populated SpeechEmotionEvent serializes and rebuilds unchanged.

    Post-boundary-repair (schema_version=3) the payload is identity-only:
    canonical resolved emotion name plus the raw_tag / resolved_fallback
    audit trail. The renderer-hint dict is gone — embodiment owns its
    own pose / LED mapping keyed on ``emotion``.
    """
    original = SpeechEmotionEvent(
        payload=SpeechEmotionPayload(
            emotion="excited",
            source_tag="enthusiastic",
            raw_tag="enthusiastic",
            resolved_fallback="high_energy_positive",
        )
    )
    wire = original.model_dump_json()
    rebuilt = SpeechEmotionEvent.model_validate_json(wire)

    assert rebuilt.schema_version == 3
    assert rebuilt.payload.emotion == "excited"
    assert rebuilt.payload.source_tag == "enthusiastic"
    assert rebuilt.payload.raw_tag == "enthusiastic"
    assert rebuilt.payload.resolved_fallback == "high_energy_positive"


def test_speech_emotion_event_old_schema_version_rejected() -> None:
    """An event constructed with the previous schema_version is rejected.

    The wire-version validator enforces the current ``schema_version=3``
    at parse boundaries. Constructing one with version 1 (a stand-in
    for any pre-current value) and feeding it through
    :func:`assert_schema_version` raises :class:`SchemaVersionError`.
    """
    event = SpeechEmotionEvent(
        payload=SpeechEmotionPayload(
            emotion="neutral",
            source_tag="neutral",
            raw_tag="neutral",
            resolved_fallback=None,
        ),
        schema_version=1,
    )
    with pytest.raises(SchemaVersionError):
        assert_schema_version(event.schema_version, source="SpeechEmotionEvent")
