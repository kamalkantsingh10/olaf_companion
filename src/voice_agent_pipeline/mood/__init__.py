"""Mood module — single discrete-mood cell + cooldown-enforcing controller.

Story 3.6 — owns OLAF's current-mood read surface and the publish path
for mood transitions. The on-the-wire ``mood`` topic is the source of
truth (architecture.md §"Decision Impact Analysis"); in-process state
mutates only after a successful publish.

Public surface:

- :class:`Mood` — Literal Re-exported from
  :mod:`voice_agent_pipeline.schemas.mood_event` for ergonomic
  imports.
- :class:`MoodState` — read-only ``current`` property; mutation gated
  through :meth:`MoodController.set`.
- :class:`MoodController` — async ``set`` (cooldown-gated) +
  ``publish_initial`` (startup latched value).

What this module does NOT do (downstream stories own these):

- Tool dispatch (:class:`SetMoodTool`) — Story 4.4.
- Greeting tinting that reads :attr:`MoodState.current` — Story 4.5.
- Pipeline lifecycle wiring — Story 3.7's ``run_pipeline``.
- Cross-restart persistence — v1.5 backlog
  (``v1.5-2-cross-restart-mood-persistence``).
"""

from voice_agent_pipeline.mood.controller import MoodController
from voice_agent_pipeline.mood.state import Mood, MoodState

__all__ = ["Mood", "MoodController", "MoodState"]
