# Story 1.4: Event schemas, error hierarchy, Protocol seams

Status: review

## Story

As Kamal,
I want `ExpressionEvent` + `LifecycleEvent` pydantic models, the full custom exception hierarchy, and all 6 Protocol seams declared as types before any feature code consumes them,
so that subsequent stories implement against stable, typed interfaces with no retrofitting — and contract tests prove the schema versioning + JSON round-trip hold from day one.

## Acceptance Criteria

1. `src/voice_agent_pipeline/schemas/expression_event.py` declares `ExpressionEvent` as a frozen pydantic v2 model with fields: `schema_version: int`, `event_type: Literal["expression"]`, `emotion: str`, `source_tag: str`, `audio_frame_id: str | None`, `timestamp_ns: int`, `payload: dict[str, Any]`. Config: `model_config = ConfigDict(frozen=True, extra="forbid")`.

2. `src/voice_agent_pipeline/schemas/lifecycle_event.py` declares `LifecycleEvent` as a frozen pydantic v2 model with fields: `schema_version: int`, `event_type: Literal["lifecycle"]`, `state: Literal["SLEEPING","LISTENING","THINKING","SPEAKING","IDLE"]`, `timestamp_ns: int`, `payload: dict[str, Any]` (default `{}`). Config: `frozen=True, extra="forbid"`.

3. `src/voice_agent_pipeline/schemas/stream.py` declares `OrchestratorStreamEvent` as a discriminated union (`Annotated[..., Field(discriminator="type")]`) over placeholder typed events: `NarrationEvent`, `SubagentStartedEvent`, `SubagentProgressEvent`, `SubagentDoneEvent`, `ResponseChunkEvent`, `TurnEndEvent`. Each is a frozen pydantic model with `type: Literal["..."]` and the minimum fields the architecture's Story 4.2 description names. **Story 4.2 will refine; this story creates the type surface so Story 1.4 contract tests can assert structure.**

4. `src/voice_agent_pipeline/errors.py` is **expanded** from Story 1.2's subset to the full hierarchy: `VoiceAgentError` (root) → `ConfigError`, `SchemaVersionError(ConfigError)`, `StartupValidationError`, `ExternalServiceError` → `CartesiaError`, `OrchestratorError`, `TalkerError`. Plus siblings of `VoiceAgentError`: `PublisherError`, `SplitterError`. All carry kwargs context per Story 1.2's pattern.

5. The 6 Protocol seam files are created with full method signatures (no implementations):
   - `src/voice_agent_pipeline/stt/backend.py` — `STTBackend` Protocol with `async transcribe(audio: bytes) -> TranscriptionResult` and a `TranscriptionResult` `@dataclass(frozen=True)` carrying `text: str` and `confidence: float`.
   - `src/voice_agent_pipeline/turn/talker.py` — `TalkerClient` Protocol with `async complete(transcript: str, context: dict[str, Any] | None = None) -> str`.
   - `src/voice_agent_pipeline/turn/orchestrator.py` — `OrchestratorClient` Protocol with `async dispatch(transcript: str, session_id: str) -> AsyncIterator[OrchestratorStreamEvent]` and `async cancel(session_id: str) -> None`.
   - `src/voice_agent_pipeline/turn/beliefs.py` — `BeliefStateClient` Protocol with `async read(keys: list[str]) -> dict[str, Any]`.
   - `src/voice_agent_pipeline/tts/client.py` — `TTSClient` Protocol with `async synthesize(text: str) -> AsyncIterator[bytes]` (returns audio frame bytes).
   - `src/voice_agent_pipeline/publisher/interface.py` — `ExpressionPublisher` Protocol with `async connect() -> None`, `async disconnect() -> None`, `async is_healthy() -> bool`, `async publish_expression(event: ExpressionEvent) -> None`, `async publish_lifecycle(event: LifecycleEvent) -> None`.

6. `tests/contract/test_expression_event_schema.py` covers: happy-path serialization → `model_dump_json()` → parse → equality; `extra="forbid"` rejects an unknown key; bad `event_type` literal rejected; missing required field rejected.

7. `tests/contract/test_lifecycle_event_schema.py` covers: happy-path round-trip; bad `state` literal rejected; default `payload={}` works.

8. `tests/contract/test_schema_version_check.py` covers: parsing an event with an unsupported `schema_version` raises `SchemaVersionError` (using `assert_schema_version` from Story 1.2). The schema_version field on the events themselves is just an `int`; *enforcement* happens at parse boundaries — the test demonstrates the pattern Stories 3.4/4.2 will use.

9. `tests/unit/errors/test_hierarchy.py` covers: every exception in the hierarchy can be constructed with kwargs, the kwargs survive on `e.context`, and the inheritance chain is correct (e.g., `isinstance(SchemaVersionError(...), ConfigError)` is True).

10. `pyright --strict` reports zero errors on `src/`. No `Any` outside the documented `payload: dict[str, Any]` extensibility seam in `ExpressionEvent`/`LifecycleEvent` and the placeholder `OrchestratorStreamEvent` payload fields. `just check` stays green.

## Tasks / Subtasks

- [x] **Task 1: Expand `errors.py` to full hierarchy** (AC: #4, #9)
  - [x] Add `StartupValidationError(VoiceAgentError)`, `ExternalServiceError(VoiceAgentError)`, `CartesiaError(ExternalServiceError)`, `OrchestratorError(ExternalServiceError)`, `TalkerError(ExternalServiceError)`, `PublisherError(VoiceAgentError)`, `SplitterError(VoiceAgentError)`.
  - [x] Update `__all__`.
  - [x] Update module docstring (remove the "Story 1.4 will extend" note since this IS that story).
  - [x] See snippet in Dev Notes.

- [x] **Task 2: Author event schemas** (AC: #1, #2)
  - [x] `src/voice_agent_pipeline/schemas/__init__.py` re-exports `ExpressionEvent`, `LifecycleEvent`, `OrchestratorStreamEvent`.
  - [x] `expression_event.py` per AC #1; snippet in Dev Notes.
  - [x] `lifecycle_event.py` per AC #2; snippet in Dev Notes.

- [x] **Task 3: Author orchestrator stream event union** (AC: #3)
  - [x] `stream.py` with the 6 placeholder event types + the `Annotated` discriminated union; snippet in Dev Notes.
  - [x] Add a module-level comment that Story 4.2 may refine field names/types as the orchestrator contract solidifies.

- [x] **Task 4: Author the 6 Protocol seam files** (AC: #5)
  - [x] `stt/backend.py` — `STTBackend` + `TranscriptionResult`.
  - [x] `turn/talker.py` — `TalkerClient`.
  - [x] `turn/orchestrator.py` — `OrchestratorClient` (uses `OrchestratorStreamEvent` from `schemas/stream.py`).
  - [x] `turn/beliefs.py` — `BeliefStateClient`.
  - [x] `tts/client.py` — `TTSClient`.
  - [x] `publisher/interface.py` — `ExpressionPublisher` (uses `ExpressionEvent`, `LifecycleEvent` from `schemas/`).
  - [x] Each file's `__init__.py` re-exports the Protocol(s).
  - [x] Each Protocol carries a one-line class docstring explaining the contract.
  - [x] See snippets in Dev Notes.

- [x] **Task 5: Contract tests** (AC: #6, #7, #8)
  - [x] `tests/contract/__init__.py` (empty if not already from Story 1.1).
  - [x] `tests/contract/test_expression_event_schema.py`:
    - `test_round_trip` — construct `ExpressionEvent`, call `model_dump_json()`, parse with `ExpressionEvent.model_validate_json(...)`, assert equality.
    - `test_extra_field_rejected`
    - `test_bad_event_type_literal_rejected`
    - `test_missing_required_field_rejected`
    - `test_payload_can_be_arbitrary_dict` — `payload={"led_intensity": 0.7, "custom_field": [1, 2, 3]}` survives round-trip.
  - [x] `tests/contract/test_lifecycle_event_schema.py`:
    - `test_round_trip`
    - `test_bad_state_literal_rejected`
    - `test_default_payload_empty_dict`
    - `test_all_5_states_accepted` — happy-path each of `SLEEPING`, `LISTENING`, `THINKING`, `SPEAKING`, `IDLE`.
  - [x] `tests/contract/test_schema_version_check.py`:
    - `test_assert_schema_version_passes_on_match` — already covered Story 1.2 but re-prove inside contract layer.
    - `test_parsing_unsupported_schema_version_can_be_rejected_via_helper` — load JSON with `schema_version=99`, parse to `ExpressionEvent`, then call `assert_schema_version(event.schema_version, source="ExpressionEvent")`, assert `SchemaVersionError` raised.

- [x] **Task 6: Unit tests for errors hierarchy** (AC: #9)
  - [x] `tests/unit/errors/__init__.py` (empty).
  - [x] `tests/unit/errors/test_hierarchy.py`:
    - `test_each_exception_constructs_with_kwargs` — parametrize over the 9 exception classes.
    - `test_kwargs_stored_on_context` — assert `e.context == {...}`.
    - `test_inheritance_chain` — `isinstance(SchemaVersionError(), ConfigError)`, `isinstance(CartesiaError(), ExternalServiceError)`, etc.

- [x] **Task 7: Verify pyright strict + just check** (AC: #10)
  - [x] Run `just check`; resolve any pyright complaints.
  - [x] Confirm `Any` only appears in the `payload` fields and the orchestrator stream's loosely-typed slots.
  - [x] No `# type: ignore` without an inline reason comment.

- [x] **Task 8: Commit** — single commit titled `Story 1.4: event schemas, error hierarchy, Protocol seams`.

## Dev Notes

### Architectural intent

This story lands the **type surface** every Epic 2–5 story consumes. By the end of Story 1.4:
- `ExpressionEvent` + `LifecycleEvent` are **stable contracts** that survive any future internal rewrite (architecture §"Stable contracts").
- 6 Protocol seams define the v2 swap points (Hailo STT, resilience-wrapped clients, alternate publisher transports).
- Custom exception hierarchy is complete — no later story has to add base classes, only raise existing ones.

Authoring the type surface here (instead of deferring per-Protocol implementation stories) prevents two failure modes the architecture explicitly warns against: (1) refactoring types when the second consumer arrives, and (2) accidentally diverging Protocol method signatures across stories.

### What this story does NOT do

- **No Protocol implementations.** Concrete classes (`AnthropicTalker`, `CartesiaClient`, `WhisperBackend`, `Ros2ExpressionPublisher`, etc.) land in their respective stories (2.2, 2.3, 1.7, 3.4).
- **No actual event publishing.** No code calls `publish_expression(...)` in this story. The publisher impl lands Story 3.4 and is first invoked Story 3.5.
- **No SSE event handling.** `OrchestratorStreamEvent` types are placeholders — Story 4.2 wires them to the live SSE stream.

### Why placeholder `OrchestratorStreamEvent` now

Story 4.2 needs typed events to dispatch by `type` field. Defining the type surface here lets Story 4.2 focus on SSE plumbing without simultaneously inventing the type contract. The placeholders are intentionally minimal — Story 4.2 may refine field names (e.g., add `name` to subagent events, refine `response_chunk` to allow markdown/SSML). That refinement is forward-compat as long as new fields are added (not renamed) — no `schema_version` bump needed.

### Why `SchemaVersionError` enforcement is a *test pattern*, not a parser hook

The pydantic model just declares `schema_version: int`. We do NOT make pydantic validate the version because pydantic's role is shape validation, not policy. Policy ("we support version 1") is enforced by the **caller** at the parse boundary — exactly where Story 1.2 already enforced it for configs. This story's contract test demonstrates the pattern: parse the event, then call `assert_schema_version(event.schema_version, source=...)`. Story 3.4 (publisher) and Story 4.2 (orchestrator) will follow this same pattern at their own parse boundaries.

### `errors.py` final form

```python
"""Custom exception hierarchy for the voice-agent-pipeline."""

from typing import Any


class VoiceAgentError(Exception):
    """Root exception for all voice-agent-pipeline errors."""

    def __init__(self, **context: Any) -> None:
        super().__init__(self._format(context))
        self.context = context

    def _format(self, context: dict[str, Any]) -> str:
        if not context:
            return self.__class__.__name__
        parts = ", ".join(f"{k}={v!r}" for k, v in context.items())
        return f"{self.__class__.__name__}({parts})"


class ConfigError(VoiceAgentError):
    """Configuration file invalid or missing."""


class SchemaVersionError(ConfigError):
    """Configuration or event schema_version is unsupported."""


class StartupValidationError(VoiceAgentError):
    """A required external dependency failed validation at startup."""


class ExternalServiceError(VoiceAgentError):
    """Base class for failures of external services. Never caught in v1 — crash + systemd restart."""


class CartesiaError(ExternalServiceError):
    """Cartesia TTS API failure."""


class OrchestratorError(ExternalServiceError):
    """Orchestrator daemon failure (HTTP/SSE)."""


class TalkerError(ExternalServiceError):
    """Anthropic Talker API failure."""


class PublisherError(VoiceAgentError):
    """Broadcast publisher (ROS 2 / DDS) failure."""


class SplitterError(VoiceAgentError):
    """Streaming SSML splitter / state machine failure."""


__all__ = [
    "VoiceAgentError",
    "ConfigError",
    "SchemaVersionError",
    "StartupValidationError",
    "ExternalServiceError",
    "CartesiaError",
    "OrchestratorError",
    "TalkerError",
    "PublisherError",
    "SplitterError",
]
```

### `schemas/expression_event.py` snippet

```python
"""ExpressionEvent — broadcast on the configured expression channel.

The wire schema is permissive: the open `payload` dict carries embodiment-specific
fields from `expression_map.yaml`. Adding new payload keys (e.g., `haptic_intensity`)
is forward-compat — downstream consumers ignore unknown fields.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ExpressionEvent(BaseModel):
    """Typed expression event published on the broadcast bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    event_type: Literal["expression"]
    emotion: str
    source_tag: str
    audio_frame_id: str | None
    timestamp_ns: int
    payload: dict[str, Any]
```

### `schemas/lifecycle_event.py` snippet

```python
"""LifecycleEvent — broadcast on the configured lifecycle channel."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LifecycleEvent(BaseModel):
    """Typed lifecycle state-change event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    event_type: Literal["lifecycle"]
    state: Literal["SLEEPING", "LISTENING", "THINKING", "SPEAKING", "IDLE"]
    timestamp_ns: int
    payload: dict[str, Any] = Field(default_factory=dict)
```

### `schemas/stream.py` snippet (placeholders refined by Story 4.2)

```python
"""OrchestratorStreamEvent — typed union over SSE event types from the orchestrator daemon.

Placeholder shapes; Story 4.2 may refine fields as the live contract solidifies.
Adding fields is forward-compat — don't rename or remove.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _StreamEventBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NarrationEvent(_StreamEventBase):
    type: Literal["narration"]
    text: str


class SubagentStartedEvent(_StreamEventBase):
    type: Literal["subagent_started"]
    name: str


class SubagentProgressEvent(_StreamEventBase):
    type: Literal["subagent_progress"]
    name: str
    msg: str


class SubagentDoneEvent(_StreamEventBase):
    type: Literal["subagent_done"]
    name: str


class ResponseChunkEvent(_StreamEventBase):
    type: Literal["response_chunk"]
    text: str


class TurnEndEvent(_StreamEventBase):
    type: Literal["turn_end"]


OrchestratorStreamEvent = Annotated[
    NarrationEvent
    | SubagentStartedEvent
    | SubagentProgressEvent
    | SubagentDoneEvent
    | ResponseChunkEvent
    | TurnEndEvent,
    Field(discriminator="type"),
]
```

### `stt/backend.py` snippet

```python
"""STTBackend Protocol — v1 impl is WhisperBackend (Story 1.7); v2 is HailoWhisperBackend."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    confidence: float


class STTBackend(Protocol):
    """Async STT inference behind a stable interface for v1/v2 backend swap."""

    async def transcribe(self, audio: bytes) -> TranscriptionResult: ...
```

### `turn/talker.py`, `turn/orchestrator.py`, `turn/beliefs.py`, `tts/client.py` snippets

```python
# turn/talker.py
from typing import Any, Protocol


class TalkerClient(Protocol):
    """In-pipeline LLM. v1 impl is AnthropicTalker (Story 2.2)."""

    async def complete(self, transcript: str, context: dict[str, Any] | None = None) -> str: ...
```

```python
# turn/orchestrator.py
from collections.abc import AsyncIterator
from typing import Protocol

from voice_agent_pipeline.schemas.stream import OrchestratorStreamEvent


class OrchestratorClient(Protocol):
    """Streaming dispatch to the orchestrator daemon. v1 impl is HttpOrchestratorClient (Story 4.2)."""

    async def dispatch(self, transcript: str, session_id: str) -> AsyncIterator[OrchestratorStreamEvent]: ...

    async def cancel(self, session_id: str) -> None: ...
```

```python
# turn/beliefs.py
from typing import Any, Protocol


class BeliefStateClient(Protocol):
    """Per-turn fresh belief-state read. v1 impl is HttpBeliefStateClient (Story 4.1)."""

    async def read(self, keys: list[str]) -> dict[str, Any]: ...
```

```python
# tts/client.py
from collections.abc import AsyncIterator
from typing import Protocol


class TTSClient(Protocol):
    """Streaming TTS. v1 impl is CartesiaClient (Story 2.3)."""

    async def synthesize(self, text: str) -> AsyncIterator[bytes]: ...
```

### `publisher/interface.py` snippet

```python
"""ExpressionPublisher Protocol — v1 impl is Ros2ExpressionPublisher (Story 3.4)."""

from typing import Protocol

from voice_agent_pipeline.schemas.expression_event import ExpressionEvent
from voice_agent_pipeline.schemas.lifecycle_event import LifecycleEvent


class ExpressionPublisher(Protocol):
    """Broadcast publisher behind a stable interface; v1 transport is ROS 2 / DDS."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_healthy(self) -> bool: ...

    async def publish_expression(self, event: ExpressionEvent) -> None: ...

    async def publish_lifecycle(self, event: LifecycleEvent) -> None: ...
```

### Why Protocols and not `abc.ABC`

Architecture rule (CLAUDE.md #3): `typing.Protocol` for interfaces, never `abc.ABC`. Protocols are structural — implementations don't need to inherit, just match the shape. This matters for testing (mock objects don't have to inherit) and for v2 (e.g., a Hailo backend doesn't need to import `STTBackend`, just provide the same shape).

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/schemas/expression_event.py`, `lifecycle_event.py`, `stream.py`
- `src/voice_agent_pipeline/stt/backend.py`
- `src/voice_agent_pipeline/turn/talker.py`, `orchestrator.py`, `beliefs.py`
- `src/voice_agent_pipeline/tts/client.py`
- `src/voice_agent_pipeline/publisher/interface.py`
- `tests/contract/test_expression_event_schema.py`, `test_lifecycle_event_schema.py`, `test_schema_version_check.py`
- `tests/unit/errors/__init__.py`, `tests/unit/errors/test_hierarchy.py`

It modifies:
- `src/voice_agent_pipeline/errors.py` (expand to full hierarchy)
- `src/voice_agent_pipeline/schemas/__init__.py`, `stt/__init__.py`, `turn/__init__.py`, `tts/__init__.py`, `publisher/__init__.py` (re-exports)

It does NOT touch `__main__.py`, `setup.toml`, or `pipeline.py` — no behavior change.

### Testing standards

- Contract tests live in `tests/contract/` — these prove **stable interfaces** survive across the project lifetime. Adding new tests here is rare; modifying existing assertions on a stable contract is a red flag (consider whether you're breaking the wire format).
- Round-trip tests use `model_dump_json()` + `model_validate_json()` — not `model_dump()` + `model_validate(dict)` — because the wire format is JSON, not Python dicts.
- Errors-hierarchy test uses `pytest.parametrize` over the 9 exception classes to keep the test compact and force every new exception to be added to the parametrize list (lint signal for the next maintainer).

### What "done" looks like

- `just check` exits 0 with zero pyright errors.
- `import voice_agent_pipeline.schemas.expression_event` works from the REPL.
- Contract tests pass.
- Story 1.5 (audio capture) can begin and use `STTBackend` Protocol when wiring downstream stages.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Publisher Contract + Event Schemas]
- [Source: build_documents/planning-artifacts/architecture.md#Type System Conventions]
- [Source: build_documents/planning-artifacts/architecture.md#Internal seams]
- [Source: build_documents/planning-artifacts/architecture.md#Error Handling]
- [Source: build_documents/planning-artifacts/architecture.md#Schema Conventions]
- [Source: build_documents/planning-artifacts/architecture.md#Stable contracts]
- [Source: build_documents/planning-artifacts/architecture.md#V1 wire format simplification] — std_msgs/String + JSON, frozen models JSON-encode cleanly
- [Source: build_documents/planning-artifacts/prd.md#NFR27, NFR28] — schema versioning + Protocol-mockable testability
- [Source: build_documents/planning-artifacts/epics.md#Story 1.4: Event schemas, error hierarchy, Protocol seams]

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- All snippets in Dev Notes implemented as written, with the comment density expansion per `feedback_code_comments.md`.
- ruff flagged 5 issues on first run: 2× S108 ("/tmp/x" insecure path) → swapped to "/some/path"; 2× N802 (UPPER in test name `_NOT_`) → renamed to `_not_`; 1× E501 (orchestrator Protocol docstring > 100 cols) → tightened wording.
- pyright flagged `tuple[Literal[...], ...]` annotation in test_lifecycle_event_schema.py as invalid type-form. `Literal[...]` isn't a type at runtime, only inside `Annotated[..., ...]`. Replaced with `tuple[str, ...]` (matches `get_args` runtime shape).
- 85 tests pass via `just test` (69 unit + 16 contract).

### Completion Notes List

- All 10 ACs satisfied. `just check` green.
- Contract tests now exercise the wire format end-to-end via `model_dump_json()` + `model_validate_json()` round-trips.
- Errors hierarchy is now complete and tested: 10 classes, parametrized construct/context/repr tests, plus explicit `isinstance` chain assertions for the architecturally-load-bearing relationships (SchemaVersionError under ConfigError; Cartesia/Orchestrator/Talker under ExternalServiceError; PublisherError + SplitterError NOT under ExternalServiceError per CLAUDE.md rule #4 reasoning).
- Protocol seams are signature-only — no implementations, exactly per spec. Each Protocol re-exported from its package's `__init__.py`.
- No deviations from architecture or spec. Some `Any` types appear in payload slots and the BeliefStateClient return — all flagged in source comments as deliberate extensibility seams.
- **Comments:** All authored modules carry module + class + function docstrings + key inline comments per the `feedback_code_comments.md` policy.

### File List

**New files:**
- `src/voice_agent_pipeline/schemas/expression_event.py`
- `src/voice_agent_pipeline/schemas/lifecycle_event.py`
- `src/voice_agent_pipeline/schemas/stream.py`
- `src/voice_agent_pipeline/stt/backend.py`
- `src/voice_agent_pipeline/turn/talker.py`
- `src/voice_agent_pipeline/turn/orchestrator.py`
- `src/voice_agent_pipeline/turn/beliefs.py`
- `src/voice_agent_pipeline/tts/client.py`
- `src/voice_agent_pipeline/publisher/interface.py`
- `tests/contract/test_expression_event_schema.py`
- `tests/contract/test_lifecycle_event_schema.py`
- `tests/contract/test_schema_version_check.py`
- `tests/unit/errors/__init__.py`
- `tests/unit/errors/test_hierarchy.py`

**Modified files:**
- `src/voice_agent_pipeline/errors.py` (Story 1.2 subset → full hierarchy of 10 classes)
- `src/voice_agent_pipeline/schemas/__init__.py` (re-exports the 3 event surfaces)
- `src/voice_agent_pipeline/stt/__init__.py` (re-exports STTBackend, TranscriptionResult)
- `src/voice_agent_pipeline/turn/__init__.py` (re-exports the 3 Protocols)
- `src/voice_agent_pipeline/tts/__init__.py` (re-exports TTSClient)
- `src/voice_agent_pipeline/publisher/__init__.py` (re-exports ExpressionPublisher)
- `build_documents/implementation-artifacts/sprint-status.yaml` (1-4 status `ready-for-dev` → `review`)
- `build_documents/implementation-artifacts/1-4-event-schemas-error-hierarchy-protocol-seams.md` (this file)

## Change Log

| Date | Change |
|---|---|
| 2026-05-05 | Story 1.4 implemented. Full error hierarchy (10 classes), 3 frozen pydantic event schemas, 6 Protocol seams. 16 new tests across contract + unit. `just check` + `just test` both green; 85 tests pass total. Status moved to `review`. |
