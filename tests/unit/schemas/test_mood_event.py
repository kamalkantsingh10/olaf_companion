"""Tests for :mod:`voice_agent_pipeline.schemas.mood_event`."""

import pytest
from pydantic import ValidationError

from voice_agent_pipeline.schemas.mood_event import Mood, MoodEvent, MoodPayload


def test_minimal_valid_mood_event() -> None:
    """A MoodEvent with just the mood field constructs cleanly."""
    event = MoodEvent(payload=MoodPayload(mood="calm"))
    assert event.payload.mood == "calm"
    assert event.payload.reason is None
    assert event.schema_version == 3


def test_mood_event_with_reason() -> None:
    """The optional ``reason`` field round-trips."""
    event = MoodEvent(payload=MoodPayload(mood="happy", reason="user laughed"))
    assert event.payload.reason == "user laughed"


def test_mood_payload_extra_forbid() -> None:
    """Unknown payload field raises."""
    with pytest.raises(ValidationError):
        MoodPayload(mood="calm", bogus="x")  # type: ignore[call-arg]


def test_mood_payload_invalid_value_rejected() -> None:
    """``mood`` Literal enforcement: an unknown mood value raises."""
    with pytest.raises(ValidationError):
        MoodPayload(mood="confused")  # type: ignore[arg-type]


def test_all_eight_mood_values_accepted() -> None:
    """All 8 declared Mood values pass — pinned so the Literal can't drift silently."""
    for mood in (
        "calm",
        "happy",
        "playful",
        "curious",
        "thoughtful",
        "sleepy",
        "grumpy",
        "excited",
    ):
        # Each must construct without raising.
        MoodPayload(mood=mood)  # type: ignore[arg-type]  # narrowing


def test_mood_type_alias_is_exported() -> None:
    """Story 3.6's mood/state.py imports this alias; pin its presence."""
    # If this import fails, Story 3.6 will break.
    from voice_agent_pipeline.schemas.mood_event import Mood as ImportedMood

    assert ImportedMood is Mood
