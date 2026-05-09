"""Unit tests for :mod:`voice_agent_pipeline.activity.greeting` (Story 4.5).

The module is a single sync function (``trigger_greeting``) that
picks a random entry from a per-mood bucket. Tests cover:

- Random pick from the configured mood's bucket.
- Fallback to ``"calm"`` bucket when the mood is missing.
- Fallback to literal ``"hey"`` when the calm bucket is also missing.
- Empty mood bucket triggers fallback (the ``or`` short-circuit).
- Every :data:`Mood` Literal value is handled.
- INFO ``greeting.picked`` log is emitted.
"""

from typing import get_args

import pytest
import structlog

from voice_agent_pipeline.activity.greeting import trigger_greeting
from voice_agent_pipeline.schemas.mood_event import Mood


def test_returns_random_choice_from_mood_bucket() -> None:
    """100 calls return values only from the mood's bucket; all entries reachable."""
    greetings: dict[Mood, list[str]] = {"calm": ["a", "b", "c"]}
    seen: set[str] = set()
    for _ in range(100):
        text = trigger_greeting("calm", greetings)
        assert text in {"a", "b", "c"}
        seen.add(text)
    # All three entries should surface across 100 calls (probability of
    # missing one is ~1.5e-12 — effectively zero).
    assert seen == {"a", "b", "c"}


def test_falls_back_to_calm_bucket_when_mood_missing() -> None:
    """Missing mood key → falls through to the ``calm`` bucket."""
    greetings: dict[Mood, list[str]] = {"calm": ["calm-greeting"]}
    text = trigger_greeting("playful", greetings)
    assert text == "calm-greeting"


def test_falls_back_to_hey_when_calm_bucket_also_missing() -> None:
    """Missing mood AND missing calm → returns the literal ``"hey"``."""
    text = trigger_greeting("calm", {})
    assert text == "hey"


def test_falls_back_when_mood_bucket_is_empty() -> None:
    """Empty mood bucket is treated like missing — short-circuits to next link."""
    greetings: dict[Mood, list[str]] = {"calm": []}
    text = trigger_greeting("calm", greetings)
    # Calm is empty → falls to ``calm`` again (still empty) → falls to "hey".
    assert text == "hey"


@pytest.mark.parametrize("mood", list(get_args(Mood)))
def test_each_mood_value_handled(mood: Mood) -> None:
    """Every Mood Literal value picks from its own bucket."""
    greetings: dict[Mood, list[str]] = {m: [f"greeting-for-{m}"] for m in get_args(Mood)}
    text = trigger_greeting(mood, greetings)
    assert text == f"greeting-for-{mood}"


def test_logs_event_greeting_picked() -> None:
    """INFO ``greeting.picked`` log fires with mood + text fields."""
    greetings: dict[Mood, list[str]] = {"happy": ["yo"]}
    with structlog.testing.capture_logs() as captured:
        trigger_greeting("happy", greetings)
    matching = [r for r in captured if r.get("event") == "greeting.picked"]
    assert len(matching) == 1
    assert matching[0].get("mood") == "happy"
    assert matching[0].get("text") == "yo"
