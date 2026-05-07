"""Contract — :class:`MoodEvent` JSON round-trip + schema_version policy."""

import pytest

from voice_agent_pipeline.config.version import (
    SUPPORTED_SCHEMA_VERSION,
    assert_schema_version,
)
from voice_agent_pipeline.errors import SchemaVersionError
from voice_agent_pipeline.schemas.mood_event import MoodEvent, MoodPayload


def test_mood_event_round_trips_through_json() -> None:
    original = MoodEvent(payload=MoodPayload(mood="calm", reason="startup"))
    wire = original.model_dump_json()
    rebuilt = MoodEvent.model_validate_json(wire)

    assert rebuilt.payload.mood == original.payload.mood
    assert rebuilt.payload.reason == original.payload.reason
    assert rebuilt.schema_version == original.schema_version


def test_mood_event_at_supported_schema_version_passes_policy() -> None:
    """Sanity: an event at SUPPORTED_SCHEMA_VERSION passes the helper."""
    event = MoodEvent(payload=MoodPayload(mood="calm"))
    assert_schema_version(event.schema_version, source="MoodEvent")


def test_mood_event_with_old_schema_version_rejected() -> None:
    """An event constructed with schema_version=1 fails the policy check."""
    event = MoodEvent(payload=MoodPayload(mood="calm"), schema_version=1)
    with pytest.raises(SchemaVersionError) as exc_info:
        assert_schema_version(event.schema_version, source="MoodEvent")
    msg = str(exc_info.value)
    assert "1" in msg
    assert str(SUPPORTED_SCHEMA_VERSION) in msg
    assert "MoodEvent" in msg
