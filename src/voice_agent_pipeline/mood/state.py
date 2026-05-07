"""``MoodState`` — single mutable cell for OLAF's current mood.

Story 3.6 — read surface for the rest of the pipeline. Story 4.5's
greeting tinting reads :attr:`MoodState.current`; Story 3.7 / 4.x's
Talker prompt assembly reads it at turn start.

Architectural promise (architecture.md §"Decision Impact Analysis"
§12): the on-the-wire ``mood`` topic is the source of truth.
:class:`MoodController.set` is the only legitimate write path —
it publishes first, then mutates ``self._current`` only on
successful publish. Direct mutation through ``state._current`` would
race the wire and is treated as a bug.

v1 lifetime is single-process. Cross-restart persistence (the saved
"OLAF was grumpy when we last talked" memory) is a v1.5 backlog item
(``v1.5-2-cross-restart-mood-persistence``).
"""

from voice_agent_pipeline.schemas.mood_event import Mood

# Re-export ``Mood`` from this module so callers can use a single import:
#   from voice_agent_pipeline.mood.state import Mood, MoodState
__all__ = ["Mood", "MoodState"]


class MoodState:
    """Single mutable cell holding OLAF's current mood.

    Public surface is the ``current`` property (read-only). Mutation
    happens through :class:`MoodController.set` — it sets
    ``_current`` directly after a successful publish.

    Why not pydantic
    ----------------

    Three reasons (architecture.md §"Type System Conventions"):

    1. The cell is mutable; pydantic's ``frozen=True`` would block all
       writes including the controller's legitimate path.
    2. The "private setter, public getter" intent is structural, not
       enforceable in pydantic.
    3. ``MoodState`` never serializes — only :class:`MoodEvent` does.
    """

    def __init__(self, initial: Mood = "calm") -> None:
        """Initialize with the configured starting mood.

        ``initial`` is typed ``Mood`` (the Literal); pyright catches
        invalid values at static analysis. Runtime enforcement happens
        upstream at the pydantic config boundary —
        ``MoodConfig.initial: Mood`` rejects unknown values from
        ``setup.toml`` before this constructor sees them.
        """
        # Underscore-prefixed: convention "do not touch from outside";
        # MoodController writes through this directly post-publish.
        self._current: Mood = initial

    @property
    def current(self) -> Mood:
        """Return the in-process current mood.

        Read-only by design. The on-the-wire mood topic is the source
        of truth; readers consult this cell for synchronous in-process
        decisions (greeting tinting, Talker prompt assembly).
        """
        return self._current
