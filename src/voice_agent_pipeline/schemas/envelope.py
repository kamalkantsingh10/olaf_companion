"""``EventEnvelope`` — common envelope shared by all four typed events.

Story 3.4 — the wire-format substrate for Epic 3's broadcast surface.
Every event published on any of the four ROS 2 topics (``mood``,
``activity``, ``speech_emotion``, ``vocalization``) carries the same
envelope fields, with a topic-specific ``payload`` typed by each
concrete subclass.

The envelope's stable contract (architecture.md §"Stable contracts"):

- ``schema_version: int = 2`` — bumped from 1 in this story to mark the
  Epic 3 topology change (single channel → four topics).
- ``timestamp: datetime`` — UTC, ISO8601 on the wire.
- ``source: Literal["voice_agent_pipeline"]`` — discriminator that lets
  multi-producer subscribers tell our events apart from a future
  ros2-bag replay producer.
- ``correlation_id: UUID`` — per-turn binding so all four topics' events
  from one user turn share an id (Story 3.7's pipeline binds it; the
  default ``uuid4`` factory is for tests + standalone construction).
- ``payload`` — typed Pydantic model per concrete subclass. Each event
  class tightens this field to a topic-specific ``BaseModel``.

The envelope is **frozen** so events are safe to pass between async
tasks without defensive copies, and ``extra="forbid"`` so a typo at
construction time fails loudly.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventEnvelope(BaseModel):
    """Common envelope mixin for all four typed events.

    Concrete subclasses (``MoodEvent``, ``ActivityEvent``, etc.)
    inherit these fields and tighten ``payload`` to a topic-specific
    pydantic model.

    Attributes:
        schema_version: Always ``2`` post-Epic-3. Subscribers reject
            other values via :func:`assert_schema_version` at parse
            boundaries (NFR27).
        timestamp: UTC datetime at construction (default factory).
            Pydantic v2 serializes datetime → ISO8601 with timezone
            offset on ``model_dump_json()``.
        source: Discriminator literal — always ``"voice_agent_pipeline"``.
        correlation_id: Per-turn id (UUID4 by default; Story 3.7
            overrides at the call site to bind across topics for one
            turn).
        payload: Typed payload — each event subclass overrides this
            field's type. Subclasses set ``payload: <PayloadType>``;
            this base class only declares it as a generic ``BaseModel``
            placeholder for the typing contract.
    """

    # frozen=True → safe to share across async tasks; mutation raises.
    # extra="forbid" → typos fail loudly at construction time.
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 2
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: Literal["voice_agent_pipeline"] = "voice_agent_pipeline"
    correlation_id: UUID = Field(default_factory=uuid4)
    # Subclasses override this field's type to be a specific payload
    # model. The base type is permissive so the field exists on the
    # envelope; concrete events tighten via standard subclass override.
    payload: BaseModel
