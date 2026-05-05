"""OrchestratorStreamEvent — typed union over SSE event types from the orchestrator daemon.

The orchestrator daemon (external dependency owned by another service)
streams Server-Sent Events back over HTTP during a slow-path turn. Each
event carries a ``type`` discriminator and one of six payload shapes.

Story 1.4 (this story) lands the **type surface** so that Story 4.2 (which
wires the live SSE handler) has a stable contract to dispatch against.
The shapes here are deliberately minimal — Story 4.2 may **add** fields as
the live contract solidifies, but **renaming or removing** would force a
``schema_version`` bump (CLAUDE.md rule #6).

Why a discriminated union and not duck-typing on a dict: pydantic's
``discriminator="type"`` makes ``OrchestratorStreamEvent.model_validate(...)``
dispatch to the correct subclass automatically and reject unknown ``type``
values. Story 4.2's SSE handler can write::

    event: OrchestratorStreamEvent = TypeAdapter(OrchestratorStreamEvent).validate_json(line)
    match event:
        case NarrationEvent(text=t): ...
        case ResponseChunkEvent(text=t): ...
        ...

— full pyright strict typing throughout, no manual ``isinstance`` ladders.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StreamEventBase(BaseModel):
    """Shared config for every concrete stream-event subclass.

    Frozen + extra="forbid" mirrors the broadcast-event models. The
    leading underscore marks it private — consumers should pattern-match on
    the concrete subclasses (``NarrationEvent``, etc.), never this base.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class NarrationEvent(_StreamEventBase):
    """Orchestrator narration text — typically a "I'm thinking about..." filler."""

    type: Literal["narration"]
    text: str


class SubagentStartedEvent(_StreamEventBase):
    """Notification that a named subagent has begun work on this turn."""

    type: Literal["subagent_started"]
    name: str


class SubagentProgressEvent(_StreamEventBase):
    """Progress update from a running subagent. ``msg`` is human-readable."""

    type: Literal["subagent_progress"]
    name: str
    msg: str


class SubagentDoneEvent(_StreamEventBase):
    """Subagent has finished. Result text (if any) lands in a subsequent ``response_chunk``."""

    type: Literal["subagent_done"]
    name: str


class ResponseChunkEvent(_StreamEventBase):
    """Streaming chunk of the orchestrator's final response.

    Story 4.2 may extend this to carry markdown/SSML structure if the live
    contract introduces formatting. ``text`` stays as-is for backward compat.
    """

    type: Literal["response_chunk"]
    text: str


class TurnEndEvent(_StreamEventBase):
    """Sentinel marking the end of a turn — the SSE stream will close after this."""

    type: Literal["turn_end"]


# Discriminated union. ``Annotated[..., Field(discriminator="type")]`` tells
# pydantic to look at the ``type`` field to choose the right concrete class
# during ``model_validate``. Adding a new event type means: define the new
# subclass above, then add it to this union.
OrchestratorStreamEvent = Annotated[
    NarrationEvent
    | SubagentStartedEvent
    | SubagentProgressEvent
    | SubagentDoneEvent
    | ResponseChunkEvent
    | TurnEndEvent,
    Field(discriminator="type"),
]
