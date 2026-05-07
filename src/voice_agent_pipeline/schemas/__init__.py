"""pydantic event schemas — typed wire contracts for Epic 3's four topics.

Story 3.4 — replaced Story 1.4's placeholder ``ExpressionEvent`` +
``LifecycleEvent`` types with the post-direction-shift four-topic
surface.

Re-exports the public types so consumers can write::

    from voice_agent_pipeline.schemas import MoodEvent, ActivityEvent, ...

instead of reaching into the per-event submodules. ``OrchestratorStreamEvent``
(Story 1.4's SSE union) is unaffected and stays here too.
"""

from voice_agent_pipeline.schemas.activity_event import (
    ActivityEvent,
    ActivityPayload,
    ActivityState,
    WorkingSubmode,
)
from voice_agent_pipeline.schemas.envelope import EventEnvelope
from voice_agent_pipeline.schemas.mood_event import Mood, MoodEvent, MoodPayload
from voice_agent_pipeline.schemas.speech_emotion_event import (
    SpeechEmotionEvent,
    SpeechEmotionPayload,
)
from voice_agent_pipeline.schemas.stream import OrchestratorStreamEvent
from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
    VocalizationPayload,
)

__all__ = [
    "ActivityEvent",
    "ActivityPayload",
    "ActivityState",
    "EventEnvelope",
    "Mood",
    "MoodEvent",
    "MoodPayload",
    "OrchestratorStreamEvent",
    "SpeechEmotionEvent",
    "SpeechEmotionPayload",
    "VocalizationEvent",
    "VocalizationPayload",
    "WorkingSubmode",
]
