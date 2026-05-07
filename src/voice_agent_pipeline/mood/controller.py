"""``MoodController`` — cooldown-enforcing publish path for mood transitions.

Story 3.6 — the write surface for the mood module. Talker's
``set_mood`` tool (Story 4.4) and ``activity/greeting.py`` (Story 4.5)
route through this controller, NOT through ``MoodState`` directly.

Architectural invariants (architecture.md §"Decision Impact Analysis"
§12, NFR31):

- **Publish-before-mutate**: ``set()`` calls ``publisher.publish_mood``
  FIRST, then mutates ``MoodState._current`` only on successful
  publish. Failure modes:
    * Publish raises → state stays unchanged; exception propagates
      (CLAUDE.md rule #4: don't catch ExternalServiceError or
      PublisherError in v1 code paths).
    * Cooldown rate-limit → no publish, state unchanged, returns
      False with WARN log.
- **Sliding 60-minute cooldown** (NFR31, ≤4 publishes/hour). At any
  instant, at most ``cooldown_publishes_per_hour`` publishes have
  occurred in the prior 60 minutes. Bucket-style "per hour" semantics
  would allow burst-at-the-boundary pathology (4 at 12:59 + 4 at
  13:00 = 8/min); sliding enforces the spirit.
- **Initial mood publishes ONCE at startup** via ``publish_initial``;
  this counts toward the cooldown budget.

Why ``import time`` rather than ``from time import monotonic``
--------------------------------------------------------------

Tests monkey-patch ``time.monotonic`` to control elapsed-time math.
Patching reaches into ``mood.controller.time.monotonic`` because the
controller calls ``time.monotonic()`` through the module reference.
A ``from time import monotonic`` would bind ``monotonic`` to a local
name in this module that the patch wouldn't intercept.

This is a classic pytest footgun. Documented inline so a future
"clean up imports" pass doesn't break the tests.
"""

import logging
import time
from collections import deque

from voice_agent_pipeline.mood.state import MoodState
from voice_agent_pipeline.publisher.interface import EventPublisher
from voice_agent_pipeline.schemas.mood_event import Mood, MoodEvent, MoodPayload

log = logging.getLogger(__name__)

# Sliding-window length for the cooldown check, in seconds. NFR31's
# unit is "per hour" — 3600 seconds.
_COOLDOWN_WINDOW_SECONDS: float = 3600.0


class MoodController:
    """Cooldown-enforcing controller for :class:`MoodState`.

    Holds references to the state cell and the :class:`EventPublisher`.
    All mutation happens through :meth:`set`; :meth:`publish_initial`
    fires the startup latched mood event.

    Constructor args:
        state: The :class:`MoodState` cell this controller writes to.
        publisher: The :class:`EventPublisher` to publish ``MoodEvent``s.
            Stories 3.5+ wire this; Story 3.7's pipeline assembly
            constructs the controller after ``publisher.connect()``.
        cooldown_publishes_per_hour: Maximum sliding-window publishes
            allowed (NFR31 default 4). Configured via ``[mood]
            cooldown_publishes_per_hour`` in setup.toml.
    """

    def __init__(
        self,
        state: MoodState,
        publisher: EventPublisher,
        cooldown_publishes_per_hour: int = 4,
    ) -> None:
        self._state = state
        self._publisher = publisher
        self._budget = cooldown_publishes_per_hour
        # Deque of monotonic timestamps of successful publishes. Older-
        # than-60min entries are popped on each ``set`` call.
        self._publish_history: deque[float] = deque()

    async def set(self, mood: Mood, reason: str) -> bool:
        """Attempt to transition to ``mood`` and publish ``MoodEvent``.

        Algorithm (architecture.md §"Decision Impact Analysis" §12):

        1. Sweep ``_publish_history`` of entries older than 60 minutes.
        2. If history length ≥ budget → rate-limited: log WARN with
           ``reason="cooldown"``, leave state unchanged, return False.
        3. Construct ``MoodEvent``, call ``publisher.publish_mood``.
           Failure propagates (CLAUDE.md rule #4 — no v1 catch).
        4. On successful publish: append timestamp, mutate
           ``state._current``, log INFO, return True.

        Args:
            mood: The mood to transition into. Static-typed at the
                Literal boundary; invalid values caught by pyright.
            reason: Human-readable reason for the transition (e.g.
                ``"set_mood tool"``, ``"calibration"``). Surfaces in
                the published payload AND the WARN log when dropped.

        Returns:
            True iff the mood publish succeeded; False iff the call
            was rate-limited.
        """
        now = time.monotonic()
        self._sweep_old(now)

        if len(self._publish_history) >= self._budget:
            log.warning(
                "mood.publish_dropped",
                extra={
                    "attempted_mood": mood,
                    "current_mood": self._state.current,
                    "reason": "cooldown",
                    "provided_reason": reason,
                    "history_size": len(self._publish_history),
                },
            )
            return False

        event = MoodEvent(payload=MoodPayload(mood=mood, reason=reason))
        # Publish FIRST. Any failure (e.g. PublisherError) propagates;
        # state stays unchanged. CLAUDE.md rule #4: no v1 catch on
        # publisher errors.
        await self._publisher.publish_mood(event)

        # Only on successful publish: record + mutate state.
        self._publish_history.append(now)
        # Underscore-write is the controller's privileged path (per
        # MoodState's documented "private setter" contract). The
        # pyright suppression carries this rationale inline per
        # architecture.md §"Anti-Patterns" — bare ignore comments are
        # banned, paired-with-reason are the documented carve-out.
        self._state._current = mood  # pyright: ignore[reportPrivateUsage]
        log.info("mood.publish", extra={"mood": mood, "reason": reason})
        return True

    async def publish_initial(self) -> None:
        """Publish the startup mood as a latched event.

        Story 3.7's ``run_pipeline`` calls this AFTER
        ``publisher.connect()`` and BEFORE the runner main loop. The
        published event lives on the latched ``mood`` topic so a
        late-joining embodiment subscriber learns the current mood at
        connect.

        This call counts toward the cooldown budget (so a rapid burst
        of ``set`` calls right after startup respects NFR31).

        Idempotent guard NOT applied: a second call within 60 minutes
        is treated like a normal ``set`` would be — if budget allows,
        publishes; if not, drops with WARN. v1 callers shouldn't
        invoke twice; the sliding-window math is the safeguard.
        """
        now = time.monotonic()
        self._sweep_old(now)

        # The initial publish counts toward the cooldown budget — but
        # we still want to log differently from a regular ``set`` so
        # operators can spot "did the startup publish actually fire?"
        # quickly.
        if len(self._publish_history) >= self._budget:
            log.warning(
                "mood.publish_dropped",
                extra={
                    "attempted_mood": self._state.current,
                    "current_mood": self._state.current,
                    "reason": "cooldown",
                    "provided_reason": "startup",
                    "history_size": len(self._publish_history),
                },
            )
            return

        event = MoodEvent(payload=MoodPayload(mood=self._state.current, reason="startup"))
        await self._publisher.publish_mood(event)
        self._publish_history.append(now)
        log.info("mood.publish_initial", extra={"mood": self._state.current})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sweep_old(self, now: float) -> None:
        """Pop history entries older than the 60-minute window.

        ``deque`` is ordered FIFO; old entries live at the front.
        Stop sweeping at the first entry inside the window.
        """
        cutoff = now - _COOLDOWN_WINDOW_SECONDS
        while self._publish_history and self._publish_history[0] < cutoff:
            self._publish_history.popleft()
