"""``MoodEvent`` — typed event on the ``mood`` topic.

Story 3.4 owns this module. The ``Mood`` Literal lives here (rather
than in ``mood/state.py`` which Story 3.6 builds) because the schema
is the wire contract — owning the type alias with the wire schema
matches the architecture's "wire-contract types live with the wire
schema" rule. Story 3.6's ``mood/state.py`` re-imports from this
module.

The mood enum lifecycle is **code-level, not YAML** (architecture.md
§"Mood enum lifecycle"). Adding a value is a code change because
(1) the Talker system prompt is fine-tuned to the enum values and
(2) the consumer side may pose-map per mood.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from voice_agent_pipeline.schemas.envelope import EventEnvelope

#: Discrete moods OLAF can hold. Code-level Literal — additions require
#: a code change + the Talker system prompt update. Final v1 set per
#: the architecture's §"Activity FSM + Mood Control + Tool Registry".
Mood = Literal[
    "calm",
    "happy",
    "playful",
    "curious",
    "thoughtful",
    "sleepy",
    "grumpy",
    "excited",
]


class MoodPayload(BaseModel):
    """Inner payload of :class:`MoodEvent`.

    Attributes:
        mood: The mood OLAF transitioned **into**. The on-the-wire
            ``mood`` topic is the source of truth (architecture.md
            §"Decision Impact Analysis" — publish before in-process
            state mutation).
        reason: Optional human-readable reason for the transition,
            e.g. ``"set_mood tool"``, ``"startup"``, ``"calibration"``.
            Useful for debugging the mood-cooldown decisions but not
            required for embodiment rendering.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mood: Mood
    reason: str | None = None


class MoodEvent(EventEnvelope):
    """Event published on the ``mood`` topic (latched, transient_local).

    The ``mood`` topic uses ``transient_local`` durability + ``depth=1``
    QoS so a late-joining subscriber (re-connecting embodiment) learns
    the current mood at connect (architecture.md §"Per-topic QoS").
    Cooldown enforced at :class:`MoodController.set` (Story 3.6,
    NFR31 — ≤4 publishes/hour).
    """

    payload: MoodPayload  # type: ignore[assignment]  # narrows envelope's BaseModel
