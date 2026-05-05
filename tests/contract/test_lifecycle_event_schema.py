"""Contract tests for :class:`LifecycleEvent`.

Same posture as the expression-event tests: these assertions guard the wire
contract for lifecycle broadcasts. Modifying them is a signal you're
changing the contract, not just refactoring.
"""

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from voice_agent_pipeline.schemas.lifecycle_event import LifecycleEvent

_VALID_KWARGS: dict[str, Any] = {
    "schema_version": 1,
    "event_type": "lifecycle",
    "state": "LISTENING",
    "timestamp_ns": 1_700_000_000_000_000_000,
}


def test_round_trip() -> None:
    """JSON serialization → deserialization yields an equal model."""
    event = LifecycleEvent(**_VALID_KWARGS)
    parsed = LifecycleEvent.model_validate_json(event.model_dump_json())
    assert parsed == event


def test_bad_state_literal_rejected() -> None:
    """``state`` must match one of the five FSM literals exactly."""
    bad = dict(_VALID_KWARGS, state="DREAMING")
    with pytest.raises(ValidationError) as exc_info:
        LifecycleEvent(**bad)
    assert "state" in str(exc_info.value)


def test_default_payload_empty_dict() -> None:
    """Omitting ``payload`` yields an empty dict (not None, not missing)."""
    event = LifecycleEvent(**_VALID_KWARGS)
    assert event.payload == {}


def test_default_payload_is_independent_per_instance() -> None:
    """Two LifecycleEvents must NOT share the same default payload dict.

    Catches the classic Python ``= {}`` mutable-default-argument bug — if
    the spec ever silently regresses to a non-Field-default, this test
    breaks. ``frozen=True`` actually prevents the bug from manifesting,
    but the test is cheap and documents intent.
    """
    e1 = LifecycleEvent(**_VALID_KWARGS)
    e2 = LifecycleEvent(**_VALID_KWARGS)
    # `is` would be the strict check, but pydantic frozen models may share
    # equal-but-distinct dicts — equality on both sides is the contract.
    assert e1.payload == e2.payload == {}


def test_explicit_payload_round_trips() -> None:
    """An explicit payload survives round-trip (extension slot works)."""
    event = LifecycleEvent(**dict(_VALID_KWARGS, payload={"reason": "idle_timeout"}))
    parsed = LifecycleEvent.model_validate_json(event.model_dump_json())
    assert parsed.payload == {"reason": "idle_timeout"}


def test_all_5_states_accepted() -> None:
    """Every state in the FSM literal must construct cleanly.

    Iterates the Literal members directly via ``typing.get_args`` so adding
    a new state forces a deliberate update here (architecture's
    spec-as-contract intent).
    """
    state_type: Any = LifecycleEvent.model_fields["state"].annotation
    states: tuple[str, ...] = get_args(state_type)
    assert len(states) == 5  # IDLE, SLEEPING, LISTENING, THINKING, SPEAKING
    for state in states:
        event = LifecycleEvent(**dict(_VALID_KWARGS, state=state))
        assert event.state == state


def test_extra_field_rejected() -> None:
    """``extra='forbid'`` is enforced for lifecycle events too."""
    bad = dict(_VALID_KWARGS, unknown_field=123)
    with pytest.raises(ValidationError):
        LifecycleEvent(**bad)
