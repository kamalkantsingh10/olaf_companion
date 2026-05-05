"""Contract tests for :class:`ExpressionEvent`.

These tests guard the **wire contract**. Any breakage here means subscribers
(downstream embodiment renderers) will fail to parse our broadcasts. Be
suspicious of any change that requires modifying these assertions — that's
usually a signal you're breaking the schema, not the test.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from voice_agent_pipeline.schemas.expression_event import ExpressionEvent

# A minimal valid event used by multiple tests below. Constructing once at
# module import keeps the tests focused on what they're actually asserting.
_VALID_KWARGS: dict[str, Any] = {
    "schema_version": 1,
    "event_type": "expression",
    "emotion": "excited",
    "source_tag": "<laughs>",
    "audio_frame_id": "frame-123",
    "timestamp_ns": 1_700_000_000_000_000_000,
    "payload": {"led_intensity": 0.7},
}


def test_round_trip() -> None:
    """JSON serialization → deserialization yields an equal model.

    This is the core wire-format contract — if model_dump_json doesn't
    round-trip, subscribers can't reliably reconstruct what we sent.
    """
    event = ExpressionEvent(**_VALID_KWARGS)
    serialized = event.model_dump_json()
    parsed = ExpressionEvent.model_validate_json(serialized)
    assert parsed == event


def test_extra_field_rejected() -> None:
    """Unknown fields raise ValidationError (extra='forbid' is enforced)."""
    bad = dict(_VALID_KWARGS, unknown_field="oops")
    with pytest.raises(ValidationError) as exc_info:
        ExpressionEvent(**bad)
    assert "unknown_field" in str(exc_info.value)


def test_bad_event_type_literal_rejected() -> None:
    """``event_type`` must be exactly the literal "expression"."""
    bad = dict(_VALID_KWARGS, event_type="lifecycle")
    with pytest.raises(ValidationError):
        ExpressionEvent(**bad)


def test_missing_required_field_rejected() -> None:
    """Missing a required field (e.g. ``emotion``) raises ValidationError."""
    bad = {k: v for k, v in _VALID_KWARGS.items() if k != "emotion"}
    with pytest.raises(ValidationError) as exc_info:
        ExpressionEvent(**bad)
    assert "emotion" in str(exc_info.value)


def test_payload_can_be_arbitrary_dict() -> None:
    """The ``payload`` slot survives round-trip with mixed-type values.

    This is the documented extensibility seam — embodiment-specific fields
    live here without requiring a schema bump.
    """
    payload: dict[str, Any] = {
        "led_intensity": 0.7,
        "custom_field": [1, 2, 3],
        "nested": {"a": "b"},
    }
    event = ExpressionEvent(**dict(_VALID_KWARGS, payload=payload))
    parsed = ExpressionEvent.model_validate_json(event.model_dump_json())
    assert parsed.payload == payload


def test_audio_frame_id_can_be_none() -> None:
    """Lifecycle-style events that aren't audio-aligned use ``audio_frame_id=None``."""
    event = ExpressionEvent(**dict(_VALID_KWARGS, audio_frame_id=None))
    parsed = ExpressionEvent.model_validate_json(event.model_dump_json())
    assert parsed.audio_frame_id is None


def test_frozen_model_cannot_be_mutated() -> None:
    """``frozen=True`` enforces immutability — assigning a field raises."""
    event = ExpressionEvent(**_VALID_KWARGS)
    with pytest.raises(ValidationError):
        event.emotion = "sad"  # type: ignore[misc]
