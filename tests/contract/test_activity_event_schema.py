"""Contract — :class:`ActivityEvent` JSON round-trip + invariants."""

import pytest
from pydantic import ValidationError

from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import SchemaVersionError
from voice_agent_pipeline.schemas.activity_event import ActivityEvent, ActivityPayload


def test_activity_event_round_trips_through_json() -> None:
    original = ActivityEvent(
        payload=ActivityPayload(
            state="working",
            from_state="listening",
            working_submode="thinking",
            transition_reason="talker_dispatch",
        )
    )
    wire = original.model_dump_json()
    rebuilt = ActivityEvent.model_validate_json(wire)

    assert rebuilt.payload.state == "working"
    assert rebuilt.payload.working_submode == "thinking"
    assert rebuilt.payload.from_state == "listening"
    assert rebuilt.payload.transition_reason == "talker_dispatch"


def test_activity_event_invariants_enforced_on_round_trip() -> None:
    """A wire payload violating the invariants is rejected on validate."""
    # Hand-craft an invalid wire form (no from_state on a non-starting state).
    invalid_json = (
        '{"payload": {"state": "listening", "working_submode": null, '
        '"transition_reason": null, "from_state": null}}'
    )
    with pytest.raises(ValidationError):
        ActivityEvent.model_validate_json(invalid_json)


def test_activity_event_old_schema_version_rejected() -> None:
    event = ActivityEvent(
        payload=ActivityPayload(state="starting"),
        schema_version=1,
    )
    with pytest.raises(SchemaVersionError):
        assert_schema_version(event.schema_version, source="ActivityEvent")
