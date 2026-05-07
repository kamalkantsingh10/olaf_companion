"""Tests for :class:`voice_agent_pipeline.mood.state.MoodState`.

Narrow surface — the cell holds one value and exposes a read-only
property. Behavior tests focus on default, override, and the absence
of public mutation paths.
"""

from voice_agent_pipeline.mood.state import MoodState


def test_default_initial_is_calm() -> None:
    """``MoodState()`` defaults to ``"calm"`` per architecture."""
    state = MoodState()
    assert state.current == "calm"


def test_initial_argument_overrides() -> None:
    """An explicit ``initial`` value lands as the starting current."""
    state = MoodState(initial="curious")
    assert state.current == "curious"


def test_current_is_read_only_property() -> None:
    """``current`` is a property (no setter) — assignment must fail.

    The architectural promise: mutation goes through MoodController.
    A direct ``state.current = "..."`` would race the wire and is
    treated as a bug. Test that the public surface enforces this.
    """
    import pytest

    state = MoodState()
    with pytest.raises(AttributeError):
        state.current = "happy"  # type: ignore[misc]


def test_controller_can_mutate_via_underscore_field() -> None:
    """The privileged path (``_current``) is accessible for
    :class:`MoodController` post-publish writes — verified here by
    direct assignment to confirm the structural contract.

    Tests for the actual controller semantics live in test_controller.py.
    """
    state = MoodState()
    # The underscore-prefixed field is convention "do not touch from
    # outside" — but it IS the privileged write path the controller
    # uses post-publish. This test documents the contract.
    state._current = "playful"  # type: ignore[reportPrivateUsage]
    assert state.current == "playful"
