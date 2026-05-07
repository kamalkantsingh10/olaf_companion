"""Tests for :class:`voice_agent_pipeline.schemas.envelope.EventEnvelope`.

The envelope is the wire-format substrate for Epic 3's four topics.
Tests pin the field defaults + frozen + extra="forbid" guarantees;
contract tests in tests/contract/ cover JSON round-trip stability.
"""

from datetime import datetime
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from voice_agent_pipeline.schemas.envelope import EventEnvelope


class _DummyPayload(BaseModel):
    """Minimal payload for envelope tests — pydantic model with one field."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str


def test_envelope_constructs_with_minimal_payload() -> None:
    """An envelope with just a payload populates every field with defaults."""
    env = EventEnvelope(payload=_DummyPayload(name="x"))
    assert env.schema_version == 2
    assert env.source == "voice_agent_pipeline"
    assert isinstance(env.timestamp, datetime)
    assert env.timestamp.tzinfo is not None  # UTC-aware
    assert isinstance(env.correlation_id, UUID)
    # Payload is typed BaseModel on the envelope; runtime-narrow before
    # accessing the subclass-specific field.
    assert isinstance(env.payload, _DummyPayload)
    assert env.payload.name == "x"


def test_envelope_correlation_id_defaults_unique_per_instance() -> None:
    """Two envelopes constructed without correlation_id get distinct UUIDs."""
    env1 = EventEnvelope(payload=_DummyPayload(name="a"))
    env2 = EventEnvelope(payload=_DummyPayload(name="b"))
    assert env1.correlation_id != env2.correlation_id


def test_envelope_is_frozen() -> None:
    """Mutation raises ValidationError (architectural safety guarantee)."""
    env = EventEnvelope(payload=_DummyPayload(name="x"))
    with pytest.raises(ValidationError):
        env.schema_version = 3  # type: ignore[misc]


def test_envelope_extra_forbid() -> None:
    """Unknown field at construction raises ValidationError."""
    with pytest.raises(ValidationError):
        EventEnvelope(payload=_DummyPayload(name="x"), bogus="extra")  # type: ignore[call-arg]


def test_envelope_source_locked_to_pipeline_literal() -> None:
    """``source`` must be the literal ``voice_agent_pipeline``; other values rejected."""
    with pytest.raises(ValidationError):
        EventEnvelope(payload=_DummyPayload(name="x"), source="other")  # type: ignore[arg-type]


def test_envelope_caller_can_override_correlation_id() -> None:
    """Story 3.7 binds a per-turn correlation_id at the call site."""
    fixed = UUID("12345678-1234-5678-1234-567812345678")
    env = EventEnvelope(payload=_DummyPayload(name="x"), correlation_id=fixed)
    assert env.correlation_id == fixed


def test_envelope_caller_can_override_timestamp() -> None:
    """Tests + replay tooling can pin the timestamp explicitly."""
    from datetime import UTC

    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    env = EventEnvelope(payload=_DummyPayload(name="x"), timestamp=fixed)
    assert env.timestamp == fixed
