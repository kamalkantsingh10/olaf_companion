"""Activity FSM — 7-state state machine + deferred-sleep + mic-mode signaling.

Story 4.3 renamed this package from ``lifecycle/`` (the old placeholder)
and built out the full FSM: 7 states, sub-modes for ``working``,
deferred-sleep on ``go_to_sleep`` tool calls, mic-mode signal queue
consumed by Story 4.6's ``audio/transport`` mic-mode router, and an
``ActivityEvent`` publish on every transition.

Re-exports:

- :class:`ActivityFSM` (sync state mutation + async publish discipline).
- :class:`ActivityState` / :class:`WorkingSubmode` Literals
  (re-imported from ``schemas/activity_event.py`` to avoid drift).
- :data:`MicMode` Literal — the type of items in
  :attr:`ActivityFSM.mic_mode_queue`.
"""

from voice_agent_pipeline.activity.machine import ActivityFSM, MicMode
from voice_agent_pipeline.activity.states import ActivityState, WorkingSubmode

__all__ = ["ActivityFSM", "ActivityState", "MicMode", "WorkingSubmode"]
