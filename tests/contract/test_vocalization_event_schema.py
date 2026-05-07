"""Contract — :class:`VocalizationEvent` JSON round-trip + schema_version."""

import pytest

from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import SchemaVersionError
from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
    VocalizationPayload,
)


def test_vocalization_event_round_trips_through_json() -> None:
    original = VocalizationEvent(payload=VocalizationPayload(tag="laughter", tts_supported=True))
    wire = original.model_dump_json()
    rebuilt = VocalizationEvent.model_validate_json(wire)

    assert rebuilt.payload.tag == "laughter"
    assert rebuilt.payload.tts_supported is True


def test_vocalization_event_old_schema_version_rejected() -> None:
    event = VocalizationEvent(
        payload=VocalizationPayload(tag="sigh", tts_supported=False),
        schema_version=1,
    )
    with pytest.raises(SchemaVersionError):
        assert_schema_version(event.schema_version, source="VocalizationEvent")
