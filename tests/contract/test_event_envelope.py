"""Contract test — :class:`EventEnvelope` JSON wire-shape stability.

The wire form must be deterministic across producer / consumer
versions. Pin the envelope's serialized shape — a future pydantic
update or accidental field rename should be caught loudly here.

We exercise the wire shape via a concrete subclass (``MoodEvent``)
because pydantic needs the typed ``payload`` field to deserialize
correctly. The envelope's base-class ``payload: BaseModel`` is just
a placeholder for the typing contract; only subclasses are
round-trip-validatable end-to-end.
"""

from datetime import UTC, datetime
from uuid import UUID

from voice_agent_pipeline.schemas.mood_event import MoodEvent, MoodPayload


def test_envelope_round_trips_through_json_via_concrete_subclass() -> None:
    """A populated MoodEvent round-trips field-equal — proves the envelope shape."""
    original = MoodEvent(
        payload=MoodPayload(mood="calm", reason="contract-test"),
        timestamp=datetime(2026, 5, 7, 13, 42, 18, tzinfo=UTC),
        correlation_id=UUID("12345678-1234-5678-1234-567812345678"),
    )
    wire = original.model_dump_json()
    rebuilt = MoodEvent.model_validate_json(wire)

    # Envelope fields round-trip equal.
    assert rebuilt.schema_version == original.schema_version == 2
    assert rebuilt.source == original.source == "voice_agent_pipeline"
    assert rebuilt.correlation_id == original.correlation_id
    assert rebuilt.timestamp == original.timestamp
    # Payload preserved.
    assert rebuilt.payload.mood == "calm"
    assert rebuilt.payload.reason == "contract-test"


def test_envelope_wire_form_contains_expected_substrings() -> None:
    """Visible string content on the wire — pins the shape consumers parse."""
    event = MoodEvent(
        payload=MoodPayload(mood="calm"),
        timestamp=datetime(2026, 5, 7, 13, 42, 18, tzinfo=UTC),
        correlation_id=UUID("12345678-1234-5678-1234-567812345678"),
    )
    wire = event.model_dump_json()
    # ISO8601 timestamp visible.
    assert "2026-05-07" in wire
    # Correlation id round-trips as the same UUID string.
    assert "12345678-1234-5678-1234-567812345678" in wire
    # Source is the locked literal.
    assert "voice_agent_pipeline" in wire
    # schema_version present on wire.
    assert '"schema_version":2' in wire
