"""``ActivityState`` + ``WorkingSubmode`` Literal re-exports (Story 4.3).

The Literals already live in :mod:`voice_agent_pipeline.schemas.activity_event`
(Story 3.4 landed them there as part of the wire schema). Story 4.3
re-imports rather than re-declaring them — same pattern as Story 3.6's
``mood/state.py`` re-importing :data:`Mood` from
:mod:`schemas.mood_event`. The single-source-of-truth keeps wire
schema and FSM type consistent automatically: adding a new state
(after a ``schema_version`` bump per CLAUDE.md rule #6) requires one
edit in ``schemas/activity_event.py`` and the FSM picks it up.

Why a separate ``states.py`` rather than re-exporting from
``__init__.py`` directly: keeps the import path stable
(``from voice_agent_pipeline.activity.states import ActivityState``)
even if the package's ``__init__.py`` changes shape later, and gives
a grep-able home for any future state-set helpers (e.g., predicates
like ``is_awake_state(s)`` if they earn their keep).
"""

from voice_agent_pipeline.schemas.activity_event import ActivityState, WorkingSubmode

__all__ = ["ActivityState", "WorkingSubmode"]
