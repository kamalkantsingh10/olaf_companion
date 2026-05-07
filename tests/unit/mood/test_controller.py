"""Tests for :class:`voice_agent_pipeline.mood.controller.MoodController`.

Real :class:`LogEventPublisher` (Story 3.5) used as the publisher
dependency where possible — it's a real Protocol implementation, not
a mock. Mocks are only injected when testing failure paths
(``PublisherError`` propagation).

Time mocking: ``monkeypatch.setattr("voice_agent_pipeline.mood.
controller.time.monotonic", clock_now)``. The controller imports
``time`` (not ``from time import monotonic``) so the patch path
reaches into the module's binding.
"""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import pytest

from voice_agent_pipeline.errors import PublisherError
from voice_agent_pipeline.mood.controller import MoodController
from voice_agent_pipeline.mood.state import MoodState
from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher

# ---------------------------------------------------------------------------
# Time-mocking helper
# ---------------------------------------------------------------------------


class _Clock:
    """Mutable monotonic-clock shim for cooldown tests.

    The controller calls ``time.monotonic()`` through the module
    reference; tests monkeypatch the call site to use this clock so
    no real ``sleep`` is needed.
    """

    def __init__(self) -> None:
        self._now: float = 0.0

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Generator[_Clock, None, None]:
    """Replace ``time.monotonic`` in the controller module with a fake clock."""
    c = _Clock()
    monkeypatch.setattr("voice_agent_pipeline.mood.controller.time.monotonic", c.now)
    yield c


# ---------------------------------------------------------------------------
# Happy path — ``set`` publishes + updates state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_publishes_and_updates_state(clock: _Clock) -> None:
    """Below-budget call publishes, mutates state, returns True."""
    state = MoodState()
    pub = LogEventPublisher()
    controller = MoodController(state, pub)

    result = await controller.set("happy", "user laughed")

    assert result is True
    assert state.current == "happy"
    # Publisher captured the event with the right payload.
    assert len(pub.published) == 1
    topic, event = pub.published[0]
    assert topic == "mood"
    assert event.payload.mood == "happy"  # type: ignore[union-attr]
    assert event.payload.reason == "user laughed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_set_state_mutation_only_after_successful_publish(
    clock: _Clock,
) -> None:
    """The publish-before-mutate invariant holds.

    Use a mock publisher whose ``publish_mood`` records the state's
    current value at the moment of the call — assert it was the prior
    mood (proving state mutation happens AFTER, not before, publish).
    """
    state = MoodState(initial="calm")
    pub = MagicMock()
    captured_mood_at_publish: list[str] = []

    async def _publish(_event: object) -> None:
        captured_mood_at_publish.append(state.current)

    pub.publish_mood = AsyncMock(side_effect=_publish)

    controller = MoodController(state, pub)
    await controller.set("happy", "test")

    # During the publish call, state was still "calm" (pre-mutation).
    assert captured_mood_at_publish == ["calm"]
    # After: state is updated.
    assert state.current == "happy"


# ---------------------------------------------------------------------------
# Cooldown — sliding 60-min window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_drops_when_over_rate(clock: _Clock, caplog: pytest.LogCaptureFixture) -> None:
    """Submitting more than ``cooldown_publishes_per_hour`` calls in
    the window returns False; later calls in-window also return False.

    AC #8: 4 publishes within the 60-min window is the budget. The 5th
    must drop with WARN log; state stays at the 4th value.
    """
    import logging

    state = MoodState()
    pub = LogEventPublisher()
    controller = MoodController(state, pub, cooldown_publishes_per_hour=4)

    with caplog.at_level(logging.DEBUG, logger="voice_agent_pipeline.mood.controller"):
        # All four publishes within the same monotonic instant — well
        # within the 60-min sliding window.
        for mood in ("happy", "playful", "curious", "thoughtful"):
            assert await controller.set(mood, "test") is True  # type: ignore[arg-type]

        # 5th call must drop.
        result = await controller.set("excited", "test")
        assert result is False

    # State remained at the 4th mood (last successful publish).
    assert state.current == "thoughtful"
    # Publisher only saw 4 events.
    assert len(pub.published) == 4
    # WARN log emitted for the dropped call.
    drop_records = [r for r in caplog.records if "mood.publish_dropped" in r.getMessage()]
    assert len(drop_records) == 1
    assert drop_records[0].levelno == logging.WARNING


@pytest.mark.asyncio
async def test_set_window_slides_after_60_minutes(clock: _Clock) -> None:
    """An entry older than 3600s is evicted; a budget slot opens up.

    AC #8 sliding-window math: 4 publishes at t=0, 5th at t=3601 — the
    first publish has aged out of the window so the 5th succeeds.
    """
    state = MoodState()
    pub = LogEventPublisher()
    controller = MoodController(state, pub, cooldown_publishes_per_hour=4)

    # 4 publishes at t=0.
    for mood in ("happy", "playful", "curious", "thoughtful"):
        assert await controller.set(mood, "test") is True  # type: ignore[arg-type]

    # Advance past the 60-min window.
    clock.advance(3601.0)

    # 5th call now succeeds — the t=0 entry has aged out.
    assert await controller.set("excited", "test") is True
    assert state.current == "excited"


@pytest.mark.asyncio
async def test_set_within_window_at_boundary_still_blocks(clock: _Clock) -> None:
    """An entry exactly at the boundary (3599s ago) still counts.

    Sliding-window contract: at any instant, at most 4 publishes have
    occurred in the prior 60 minutes. ``3599s ago`` is inside the
    window; the cooldown holds.
    """
    state = MoodState()
    pub = LogEventPublisher()
    controller = MoodController(state, pub, cooldown_publishes_per_hour=4)

    for mood in ("happy", "playful", "curious", "thoughtful"):
        assert await controller.set(mood, "test") is True  # type: ignore[arg-type]

    clock.advance(3599.0)

    # 5th call at t=3599 — first publish at t=0 is still inside the
    # 60-min window (t=0 is 3599s ago, < 3600s threshold).
    assert await controller.set("excited", "test") is False
    assert state.current == "thoughtful"


@pytest.mark.asyncio
async def test_set_when_dropped_does_not_call_publisher(clock: _Clock) -> None:
    """Rate-limited calls must not invoke the publisher."""
    state = MoodState()
    pub = MagicMock()
    pub.publish_mood = AsyncMock()

    controller = MoodController(state, pub, cooldown_publishes_per_hour=2)

    # First two succeed.
    await controller.set("happy", "test")
    await controller.set("playful", "test")
    assert pub.publish_mood.call_count == 2

    # Third call drops; publisher NOT invoked.
    assert await controller.set("excited", "test") is False
    assert pub.publish_mood.call_count == 2


# ---------------------------------------------------------------------------
# Failure path — publisher raises mid-set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publisher_failure_propagates_no_state_mutation(clock: _Clock) -> None:
    """``publisher.publish_mood`` raising leaves state + history unchanged.

    CLAUDE.md rule #4: don't catch PublisherError in v1 paths. The
    process crashes; systemd restarts. State stays at the prior mood
    (publish-before-mutate invariant).
    """
    state = MoodState(initial="calm")
    pub = MagicMock()
    pub.publish_mood = AsyncMock(side_effect=PublisherError(topic="mood", reason="connect lost"))

    controller = MoodController(state, pub)

    with pytest.raises(PublisherError):
        await controller.set("happy", "test")

    # State unchanged.
    assert state.current == "calm"
    # History unchanged — no successful publish to record.
    assert len(controller._publish_history) == 0  # type: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# publish_initial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_initial_publishes_startup_mood(clock: _Clock) -> None:
    """``publish_initial`` fires once with the current mood + reason="startup"."""
    state = MoodState(initial="thoughtful")
    pub = LogEventPublisher()
    controller = MoodController(state, pub)

    await controller.publish_initial()

    assert len(pub.published) == 1
    topic, event = pub.published[0]
    assert topic == "mood"
    assert event.payload.mood == "thoughtful"  # type: ignore[union-attr]
    assert event.payload.reason == "startup"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_publish_initial_counts_toward_cooldown(clock: _Clock) -> None:
    """The initial publish counts toward the budget.

    AC: a rapid burst of ``set`` calls right after startup sees one
    fewer slot available because the initial used one.
    """
    state = MoodState()
    pub = LogEventPublisher()
    controller = MoodController(state, pub, cooldown_publishes_per_hour=4)

    await controller.publish_initial()

    # 3 more set() calls succeed (1 initial + 3 = 4 total).
    for mood in ("happy", "playful", "curious"):
        assert await controller.set(mood, "test") is True  # type: ignore[arg-type]

    # 4th set() must drop — budget exhausted (4 publishes within window).
    assert await controller.set("thoughtful", "test") is False
    assert len(pub.published) == 4


@pytest.mark.asyncio
async def test_publish_initial_logs_distinct_event(
    clock: _Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """publish_initial logs ``mood.publish_initial`` at INFO."""
    import logging

    state = MoodState()
    pub = LogEventPublisher()
    controller = MoodController(state, pub)

    with caplog.at_level(logging.DEBUG, logger="voice_agent_pipeline.mood.controller"):
        await controller.publish_initial()

    initial_records = [r for r in caplog.records if "mood.publish_initial" in r.getMessage()]
    assert len(initial_records) == 1
    assert initial_records[0].levelno == logging.INFO
