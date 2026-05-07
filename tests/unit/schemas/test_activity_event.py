"""Tests for :mod:`voice_agent_pipeline.schemas.activity_event`.

The two ``model_validator``-enforced invariants are the meat of this
test file — every state combination has to be tested both ways.
"""

import pytest
from pydantic import ValidationError

from voice_agent_pipeline.schemas.activity_event import (
    ActivityEvent,
    ActivityPayload,
    ActivityState,
    WorkingSubmode,
)


def test_starting_state_minimal() -> None:
    """``starting`` is the only state where ``from_state`` may be None."""
    event = ActivityEvent(payload=ActivityPayload(state="starting"))
    assert event.payload.state == "starting"
    assert event.payload.from_state is None


def test_non_starting_state_requires_from_state() -> None:
    """Every state except ``starting`` MUST carry ``from_state``."""
    with pytest.raises(ValidationError, match="from_state required"):
        ActivityPayload(state="listening")


def test_starting_state_must_not_have_from_state() -> None:
    """``starting`` with ``from_state`` set raises (initial-publish invariant)."""
    with pytest.raises(ValidationError, match="from_state must be None"):
        ActivityPayload(state="starting", from_state="sleeping")


def test_working_state_requires_submode() -> None:
    """``state="working"`` without ``working_submode`` raises."""
    with pytest.raises(ValidationError, match="working_submode required"):
        ActivityPayload(state="working", from_state="listening")


def test_non_working_state_with_submode_rejected() -> None:
    """``working_submode`` on any non-working state raises."""
    with pytest.raises(ValidationError, match="working_submode allowed only"):
        ActivityPayload(
            state="listening",
            from_state="waking",
            working_submode="thinking",
        )


def test_working_with_thinking_submode_valid() -> None:
    """Happy path: working + thinking + from_state."""
    payload = ActivityPayload(
        state="working",
        from_state="listening",
        working_submode="thinking",
        transition_reason="talker_dispatch",
    )
    assert payload.state == "working"
    assert payload.working_submode == "thinking"


def test_working_with_delegating_submode_valid() -> None:
    """Happy path: working + delegating + from_state."""
    payload = ActivityPayload(
        state="working",
        from_state="listening",
        working_submode="delegating",
    )
    assert payload.working_submode == "delegating"


def test_all_seven_activity_states_accepted() -> None:
    """All 7 declared ActivityState values pass — pin the Literal."""
    # Most need from_state; we use a sentinel value where allowed.
    states_with_from: list[ActivityState] = [
        "sleeping",
        "waking",
        "listening",
        "speaking",
        "going_to_sleep",
    ]
    for state in states_with_from:
        ActivityPayload(state=state, from_state="listening")
    # `working` requires a submode.
    ActivityPayload(state="working", from_state="listening", working_submode="thinking")
    # `starting` has no from_state.
    ActivityPayload(state="starting")


def test_invalid_state_rejected() -> None:
    """An 8th value (not in the Literal) raises."""
    with pytest.raises(ValidationError):
        ActivityPayload(state="unknown")  # type: ignore[arg-type]


def test_invalid_working_submode_rejected() -> None:
    """``working_submode`` Literal enforcement."""
    with pytest.raises(ValidationError):
        ActivityPayload(
            state="working",
            from_state="listening",
            working_submode="bogus",  # type: ignore[arg-type]
        )


def test_payload_extra_forbid() -> None:
    """Unknown payload field raises."""
    with pytest.raises(ValidationError):
        ActivityPayload(state="starting", bogus=1)  # type: ignore[call-arg]


def test_working_submode_alias_is_exported() -> None:
    """Story 4.x will need WorkingSubmode at the pipeline boundary."""
    from voice_agent_pipeline.schemas.activity_event import (
        WorkingSubmode as ImportedSubmode,
    )

    assert ImportedSubmode is WorkingSubmode
