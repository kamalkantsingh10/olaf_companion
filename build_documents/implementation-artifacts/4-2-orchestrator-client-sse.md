# Story 4.2: `OrchestratorClient` — SSE stream consumer over `httpx-sse`

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want an `OrchestratorClient` that opens `POST /turn` as an SSE stream and yields typed events as they arrive (narration, subagent_started, subagent_progress, subagent_done, response_chunk, turn_end),
so that Story 4.7's pipeline can dispatch complex turns without buffering the full response.

## Acceptance Criteria

1. **`OrchestratorClient` Protocol stays narrow.** `src/voice_agent_pipeline/turn/orchestrator.py` already declares the Protocol — two methods: `async def dispatch(self, transcript: str, session_id: str) -> AsyncIterator[OrchestratorStreamEvent]` and `async def cancel(self, session_id: str) -> None`. Story 4.2 does **not** widen the Protocol — no `connect()` / `close()` / health methods on the seam itself. Lifecycle is owned by the concrete class + the pipeline-assembly site (AC #5), mirroring Story 4.1's `BeliefStateClient` pattern.

2. **`HttpOrchestratorClient` concrete implementation in the same module.** `class HttpOrchestratorClient` lives in `turn/orchestrator.py` (single-file module — Protocol + sole v1 impl together; mirrors Story 4.1's `turn/beliefs.py` shape). Constructor signature:
   ```python
   class HttpOrchestratorClient:
       def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
           self._client = http_client
           self._base_url = base_url.rstrip("/")
   ```
   **Reuse Story 4.1's persistent `httpx.AsyncClient`.** The same instance constructed in `pipeline.py:run_pipeline` (Story 4.1 wiring) is injected here. Both `HttpBeliefStateClient` and `HttpOrchestratorClient` share the connection-pool — keep-alive at the origin level, not the per-client level (architecture.md §"Connection management": "Persistent `httpx.AsyncClient` per service" — *one* shared client is the v1 architecture intent because both endpoints live on the same origin).

3. **`dispatch(transcript, session_id)` opens `POST /turn` as an SSE stream and yields typed events.**
   - URL: `f"{self._base_url}/turn"`.
   - Method: HTTP POST. Body: JSON `{"transcript": <str>, "session_id": <str>}` (use `json=` kwarg on httpx, *not* `data=`).
   - Headers: `{"Accept": "text/event-stream"}`. The shared `httpx.AsyncClient`'s default headers stay; this header is set per-call.
   - Streaming: open via `httpx_sse.aconnect_sse(self._client, "POST", url, json=..., headers=...)` — the canonical `httpx-sse` async pattern.
   - Iteration: `async for sse in event_source.aiter_sse():` — each `sse` is an `httpx_sse.ServerSentEvent` with `.event`, `.data`, `.id`, `.retry`. Story 4.2's contract dispatches **on the JSON `type` field of `sse.data`**, not on `sse.event` (the orchestrator currently emits a single SSE event name and discriminates inside the JSON payload — confirmed by `schemas/stream.py`'s `type: Literal[...]` discriminator design).
   - Parse: `event = TypeAdapter(OrchestratorStreamEvent).validate_json(sse.data)` (the `TypeAdapter` invocation pattern is documented in `schemas/stream.py`'s module docstring).
   - Yield each parsed event to the caller. **No buffering for full-stream completion** (architecture.md §"Streaming consumption" — real-time latency is the contract; Story 4.7's splitter consumes events as they arrive).
   - Stream terminates naturally when the SSE source closes after `TurnEndEvent`. The `async for` loop exits; the `aconnect_sse` context manager closes the underlying response cleanly.

   **Implementation skeleton** (developer should follow this shape — not literal copy-paste, but the control flow is load-bearing):
   ```python
   async def dispatch(self, transcript: str, session_id: str) -> AsyncIterator[OrchestratorStreamEvent]:
       url = f"{self._base_url}/turn"
       body = {"transcript": transcript, "session_id": session_id}
       try:
           async with aconnect_sse(self._client, "POST", url, json=body,
                                   headers={"Accept": "text/event-stream"}) as event_source:
               async for sse in event_source.aiter_sse():
                   yield self._parse_or_log(sse)  # see AC #4
       except httpx.HTTPError as exc:
           raise OrchestratorError(reason=type(exc).__name__, url=url) from exc
   ```
   **NOTE on async-generator semantics**: Python forbids `try/except/yield` mixing in some patterns; the cleanest shape is to filter the unknown-type WARN-and-skip case inside `_parse_or_log` and return a sentinel that the iterator skips. See AC #4 for the chosen pattern. Final shape is the dev's call as long as AC #4's "WARN + continue" and AC #6's "raise on framing/JSON error" semantics both hold.

4. **Unknown SSE event `type` → log WARN + continue (forward-compat).** Architecture.md §"SSE event dispatch": "Unknown types → log WARN + ignore (forward-compat for orchestrator evolution)." Concretely, when `TypeAdapter.validate_json` raises `pydantic.ValidationError` AND the cause is a `type` field outside the union (the orchestrator added a new event type the pipeline doesn't know yet):
   - Log WARN `event="orchestrator.unknown_event_type"` with fields: `type` (the unknown discriminator value, parsed from raw JSON), `session_id`, `correlation_id` (if available; Story 3.7 binds via contextvars). **Do not** log `sse.data` raw — it may contain user-state or response text (NFR25 / FR39).
   - **Continue** consuming the stream. The orchestrator can ship new event types without breaking the pipeline (Batch 4 forward-compat decision).
   - **Detection**: peek at the JSON `type` field via `json.loads(sse.data).get("type")` *before* full TypeAdapter validation. If the `type` is not in the known set (`{"narration", "subagent_started", "subagent_progress", "subagent_done", "response_chunk", "turn_end"}`), it's the unknown-type forward-compat case — log + skip, do NOT raise. **Recommend** keeping the known-type set in module-level constant `_KNOWN_EVENT_TYPES: frozenset[str]` derived from the discriminator literals; that way adding a new event type to `schemas/stream.py` automatically extends the set if you derive it programmatically (e.g., from `OrchestratorStreamEvent`'s `__metadata__` discriminator), or update both files together if you list it manually. Prefer programmatic derivation if pyright accepts it.

5. **Framing error or malformed JSON inside an event → `OrchestratorError`, crash (v1 fail-fast).**
   - If `sse.data` is not valid JSON (`json.loads` raises `json.JSONDecodeError`): raise `OrchestratorError(reason="invalid_json", session_id=session_id, raw_length=len(sse.data))` — **do NOT include `sse.data` text** in the context (privacy + log-noise; the `raw_length` is enough for postmortem).
   - If the JSON `type` field is in `_KNOWN_EVENT_TYPES` but `TypeAdapter.validate_json` still raises `ValidationError` (e.g., a known event type with missing/wrong fields): raise `OrchestratorError(reason="invalid_event_shape", type=<the type>, session_id=session_id, errors=<short repr of pydantic errors>) from exc`. This is a *contract* violation, not forward-compat — the orchestrator broke the agreed shape for a known type. Crash + systemd restart per CLAUDE.md rule #4.
   - If the SSE framing itself is broken (httpx-sse raises): `httpx_sse.SSEError` propagates as part of the `httpx.HTTPError` family — wrapped in AC #3's outer `try/except` as `OrchestratorError(reason="<exc class>", url=url) from exc`. **Distinction**: forward-compat is "new event type, don't crash"; broken contract is "stream itself unreliable, crash."

6. **Startup health probe — `GET /health` against `<daemon.url>/health`.**
   - Add `async def probe_health(self) -> None` method on `HttpOrchestratorClient` (instance method, not on the Protocol — it's an impl-only concern). Behavior: `resp = await self._client.get(f"{self._base_url}/health")`. If `resp.status_code != 200`, raise `StartupValidationError(reason="orchestrator_health_non_200", status_code=resp.status_code, url=str(resp.request.url))`. On `httpx.HTTPError`: `raise StartupValidationError(reason=type(exc).__name__, url=...) from exc`.
   - Use `StartupValidationError` (already in `errors.py`) — **not** `OrchestratorError`. Architecture.md §"Error Handling": startup probes are distinct from in-flight failures — different error class, same fail-fast disposition. The pipeline never finished initializing, so `__main__.py`'s top-level handler logs the `StartupValidationError` and exits non-zero; systemd restarts.
   - Wire `probe_health()` into `pipeline.py:run_pipeline` **at startup, before the pipeline runner's main loop** — alongside Story 4.1's `httpx.AsyncClient` lifecycle. Order: construct `http_client` → construct `belief_client` (Story 4.1) + `orchestrator_client` (Story 4.2) → `await orchestrator_client.probe_health()` → continue building the pipeline. The probe is single-shot; if it succeeds, the pipeline enters its main loop. If it fails, the `async with httpx.AsyncClient(...)` block exits cleanly via the exception path.
   - This is the **Story 4.2 deliverable** for the architecture's spec-drift item (architecture.md §"Cross-project integration": "Orchestrator daemon must expose `GET /health` returning 200"). Story 4.1's pipeline-assembly wiring landed the client lifecycle; Story 4.2 adds the probe.

7. **`cancel(session_id)` stubbed in Epic 4.** Per the AC explicitly: `HttpOrchestratorClient.cancel(session_id)` raises `NotImplementedError("Cancel is wired in v1.5 Story v1.5-1 — barge-in")`. The **HTTP DELETE `/turn/{session_id}` wiring lands in v1.5 Story v1.5-1 (barge-in)** — see `epics.md` §"v1.5 Backlog (Post-v1)". Story 4.2 ships the stub explicitly so the Protocol is satisfied at type-check time and any caller invoking `cancel` in v1 fails loudly (instead of silently no-oping, which would mask routing bugs).
   - The Protocol's `cancel` signature stays unchanged; `HttpOrchestratorClient` raises on the call. Document this in the method's docstring with a forward reference to Story v1.5-1.
   - Unit test asserts `pytest.raises(NotImplementedError)` is the contract. **Do not** test "cancel works" — there is no v1 implementation.

8. **v1 retry semantics — none.** Any HTTP error during `dispatch` (connection refused, read timeout, 5xx response, RemoteProtocolError, transport error, malformed framing) → wrap as `OrchestratorError` and let it propagate. Architecture.md §"V1 Posture: Hard Dependencies, Fail-Fast" + Batch 4 decision: zero resilience at this seam in v1. Resilience layer is v2.
   - **Stall detection**: a stream that emits no events for >60s is treated as a stall — set `httpx.AsyncClient`'s `read` timeout to a value that triggers this (Story 4.1's wiring uses `read=10.0`; Story 4.2 may need to override per-call to `read=60.0` because narration → subagent_done can legitimately span tens of seconds while a tool is running). **Recommend**: pass `timeout=httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0)` on the `aconnect_sse` call (httpx-sse forwards `timeout` to the underlying request). If a 60s stall fires, httpx raises `ReadTimeout`, which falls into the AC #3 outer `try/except` and produces `OrchestratorError`.
   - **Do NOT** retry on connection refused. Crash + systemd restart is the v1 strategy.

9. **Pyright-strict typing (no `Any` exfiltration).** The yielded events are typed as `OrchestratorStreamEvent` (the discriminated union from `schemas/stream.py`). Concretely:
   - `dispatch`'s return type is `AsyncIterator[OrchestratorStreamEvent]` per the Protocol. The implementation must yield *only* values that are members of the union — `TypeAdapter.validate_json` enforces this at runtime; pyright's strict mode enforces at static-check time.
   - The `_parse_or_log` helper (or whatever shape the dev chooses) returns `OrchestratorStreamEvent | None` where `None` means "skip this SSE message" (the unknown-type forward-compat case). The caller filters `None` before `yield`.
   - **Only file allowed to import `httpx_sse`.** `rg -F 'import httpx_sse' src/` after implementation must show only `turn/orchestrator.py`. (`turn/beliefs.py` imports `httpx` but not `httpx_sse` — Story 4.1's GET path doesn't need SSE).
   - **Only files allowed to import `httpx` in `src/`**: `turn/beliefs.py` (Story 4.1) + `turn/orchestrator.py` (Story 4.2). No other source file.

10. **Logging discipline (NFR25, FR39).** Per-event log volume matters here: a slow turn may emit 20+ events. Log levels:
    - `event="orchestrator.dispatch_started"` at INFO (once per dispatch call), fields: `session_id`, `transcript_length` (NOT the transcript text — privacy), `url`.
    - `event="orchestrator.event_received"` at **DEBUG** (not INFO — too chatty for INFO), fields: `event_type`, `session_id`. Per-event INFO would blow log volume on long turns.
    - `event="orchestrator.unknown_event_type"` at WARN, fields: `type`, `session_id` (no `data`).
    - `event="orchestrator.dispatch_failed"` at WARN, fields: `session_id`, `reason`, `url` — fired in the outer `except` before raising.
    - `event="orchestrator.dispatch_completed"` at INFO (after a clean stream end), fields: `session_id`, `event_count`, `duration_ms`.
    - **Never log**: `transcript` text, `sse.data` raw bytes, `response_chunk.text` content (it's the model's response — treat like a transcript, gated to DEBUG only). Architecture.md §"Logging discipline".
    - The redaction processor (Story 1.3) is the safety net; Story 4.2's code shouldn't pass these into log fields in the first place.

11. **Architecture spec-drift README/comment update (NFR26 — spec-as-contract).** Architecture.md §"Cross-project integration" lists the orchestrator's `GET /health` requirement on the spec-drift list; the AC says "the requirement that the orchestrator daemon expose `GET /health` is added to the project's spec-drift tracking" once Story 4.2 lands.
    - **Two-line update** to `architecture.md` under §"Cross-project integration" (already exists in the architecture's "Open / pending coordination items" list, line 248): **upgrade** the entry from "document in cross-project README when first wired" to "wired in Story 4.2 — `HttpOrchestratorClient.probe_health()` requires `GET /health` returning 200; pipeline refuses to start otherwise (`StartupValidationError`)." This closes the spec-drift item.
    - Optionally add a one-line note in `README.md` (project root) under a "Dependencies on external services" section — if such a section exists, extend it; if not, create it under "## Architecture notes" or similar. **Recommend**: just update architecture.md (NFR26 — spec-as-contract); the README is not a primary spec surface.
    - **Commit the doc change in the same commit as the code change.** Per CLAUDE.md rule #9.

12. **Unit tests in `tests/unit/turn/test_orchestrator.py`** mock the SSE source and cover:
    - `test_dispatch_issues_post_with_correct_body` — patch `httpx_sse.aconnect_sse`, invoke `dispatch("hello", "session-1")`, assert: URL is `<base_url>/turn`, method is POST, body JSON is `{"transcript": "hello", "session_id": "session-1"}`, headers include `Accept: text/event-stream`. Use `unittest.mock.patch` on `voice_agent_pipeline.turn.orchestrator.aconnect_sse` (patch where the symbol is imported, not where defined).
    - `test_dispatch_yields_parsed_events` — mock `event_source.aiter_sse` to yield 4 fake `ServerSentEvent` objects (one each: `narration`, `subagent_started`, `response_chunk`, `turn_end`). Iterate the async generator; assert the yielded events are `NarrationEvent(text=...)`, `SubagentStartedEvent(name=...)`, `ResponseChunkEvent(text=...)`, `TurnEndEvent()` instances respectively. Each event's fields match the mocked JSON.
    - `test_dispatch_unknown_event_type_logs_warn_and_continues` — mock event sequence: `narration` → unknown `{"type": "future_extension", ...}` → `turn_end`. Assert: 2 events yielded (narration + turn_end), the unknown is skipped. Capture WARN log `event="orchestrator.unknown_event_type"` with `type="future_extension"`. Assert no exception was raised. **Critical for forward-compat invariant.**
    - `test_dispatch_invalid_json_raises_orchestrator_error` — mock `sse.data = "not-valid-json{"` for one of the events; `pytest.raises(OrchestratorError)`; `excinfo.value.context["reason"] == "invalid_json"`. Assert `excinfo.value.context` does NOT contain a key like `data` / `body` / `raw` (privacy invariant — only `raw_length`).
    - `test_dispatch_invalid_event_shape_raises_orchestrator_error` — mock `sse.data = '{"type": "narration"}'` (missing `text` field — known type but bad shape). `pytest.raises(OrchestratorError)`; `excinfo.value.context["reason"] == "invalid_event_shape"`, `["type"] == "narration"`. Distinct from forward-compat unknown-type case.
    - `test_dispatch_connection_error_raises_orchestrator_error` — mock `aconnect_sse` to raise `httpx.ConnectError("connection refused")`; `pytest.raises(OrchestratorError)`; assert `excinfo.value.__cause__` is the original `httpx.ConnectError` (the `from exc` chain).
    - `test_dispatch_read_timeout_raises_orchestrator_error` — same shape with `httpx.ReadTimeout`; same cause-chain assertion. Validates the 60s stall-detection contract (AC #8).
    - `test_dispatch_logs_event_orchestrator_dispatch_started` — happy path; assert one INFO log with `event="orchestrator.dispatch_started"`, fields `session_id="session-1"`, `transcript_length=5` (for `"hello"`), `url=<base_url>/turn`. Critical: assert the log line does NOT contain the transcript text `"hello"` itself (privacy).
    - `test_dispatch_logs_event_orchestrator_dispatch_completed_on_clean_end` — happy path with N events; assert INFO log `event="orchestrator.dispatch_completed"` with `event_count=N` and a `duration_ms` field present.
    - `test_dispatch_logs_event_orchestrator_event_received_at_debug_level` — happy path; assert `event="orchestrator.event_received"` is logged at DEBUG level (not INFO). Caplog fixture filters by level.
    - `test_dispatch_does_not_log_response_chunk_text` — mock a `response_chunk` event with `text="user_secret_123"`; drive dispatch; assert no captured log line at any level above DEBUG contains `"user_secret_123"`. (NFR25 privacy invariant.)
    - `test_probe_health_200_succeeds` — mock `client.get` for `/health` returning 200; `await client.probe_health()` returns `None` (no exception).
    - `test_probe_health_non_200_raises_startup_validation_error` — mock `status_code=503`; `pytest.raises(StartupValidationError)`; `excinfo.value.context["status_code"] == 503`, `["reason"] == "orchestrator_health_non_200"`.
    - `test_probe_health_connection_error_raises_startup_validation_error` — mock `client.get` raises `httpx.ConnectError`; `pytest.raises(StartupValidationError)`; `excinfo.value.context["reason"] == "ConnectError"`.
    - `test_cancel_raises_not_implemented_error` — `pytest.raises(NotImplementedError)` with a message mentioning v1.5 / barge-in (so a future contributor knows where the impl lands).
    - `test_dispatch_does_not_construct_client_per_call` — drive 2 sequential `dispatch(...)` calls fully through; assert the injected `http_client` mock's `__init__` was not invoked between calls (persistent-client invariant — same as Story 4.1 AC #11).

13. **Pipeline-assembly wiring (extends Story 4.1's hooks).** In `src/voice_agent_pipeline/pipeline.py:run_pipeline`:
    - Inside the `async with httpx.AsyncClient(...)` block (Story 4.1's wiring):
      - Construct `orchestrator_client = HttpOrchestratorClient(http_client, base_url=config.daemon.url)` — same pattern as Story 4.1's `belief_client` construction.
      - Call `await orchestrator_client.probe_health()` — single-shot startup probe. If it raises `StartupValidationError`, the outer `__main__.py` handler catches and exits non-zero. Place this **after** `event_publisher.connect()` (which is itself a startup probe per Story 3.5/3.7) and **before** the pipeline-stage construction loop.
    - Pass `orchestrator_client` to the future TurnRouter / TurnDispatchProcessor wiring **as a stub-only injection** in this story — **Story 4.7** wires the slow-path call (`OrchestratorDispatchProcessor.dispatch(...)`). Concretely: extend the `TurnRouter` / `TurnDispatchProcessor` constructor (Story 2.4 baseline) to accept `orchestrator_client: OrchestratorClient` as a parameter, store as instance attribute, but **do not invoke `dispatch()` yet** — the existing `NotImplementedError` stub in the `target="orchestrator"` branch (Story 2.4) stays in place. Story 4.7 removes that stub and wires the call. **If extending the constructor is too invasive at this point**, the alternative is to defer the constructor injection to Story 4.7 too — document the deferral in the dev record. **Recommend** landing the injection now (mirrors Story 4.1's "wire the reference, defer the call site to 4.4" pattern).
    - Update `pipeline.py`'s module docstring: append a Story 4.2 entry to the "Story progression" list — `httpx.AsyncClient` lifecycle now holds two clients (belief + orchestrator); startup probes now include `orchestrator_client.probe_health()`.

14. **`just check` stays green.** All earlier stories' tests pass. New tests in `tests/unit/turn/test_orchestrator.py`. Run `uv run pytest tests/unit/turn/test_orchestrator.py -v` first, then full `just check`. Pyright strict on `src/` must accept the new code.

15. **No raw audio / credentials / transcripts logged.** Standing privacy invariant (NFR25, FR39). Story 4.2's `dispatch` handles the transcript at the call boundary; logs only `transcript_length`, never the transcript text. Logs only `event_type`, never the `response_chunk.text` content. The redaction processor catches mistakes; Story 4.2's code shouldn't make them.

16. **Forward-compat invariant — explicit comment in code.** In `turn/orchestrator.py`, add a class-level comment block above `_KNOWN_EVENT_TYPES` explaining: "Forward-compat: when the orchestrator project ships a new event type, this set updates *after* the new type is added to `schemas/stream.py`'s union. Until then, the new type is logged-and-skipped (architecture.md Batch 4 decision). Renaming or removing an event type forces a `schema_version` bump (CLAUDE.md rule #6)." This is the WHY-comment that survives — future contributors must know why unknown types don't crash.

## Tasks / Subtasks

- [x] **Task 1: `HttpOrchestratorClient` impl in `turn/orchestrator.py`** (AC: #1, #2, #3, #4, #5, #6, #7, #8, #9, #10, #16)
  - [ ] Open `src/voice_agent_pipeline/turn/orchestrator.py`. The Protocol is already there. Add the `HttpOrchestratorClient` class beneath the Protocol.
  - [ ] Imports (top of file, three groups per Story 1.4 isort convention): stdlib (`import json`, `import time`, `from collections.abc import AsyncIterator`, `from typing import Any`), third-party (`import httpx`, `from httpx_sse import aconnect_sse`, `import structlog`, `from pydantic import TypeAdapter, ValidationError`), local (`from voice_agent_pipeline.errors import OrchestratorError, StartupValidationError`, `from voice_agent_pipeline.schemas.stream import OrchestratorStreamEvent`).
  - [ ] **Module-level**: `log = structlog.get_logger(__name__)`. **Module-level constant** `_KNOWN_EVENT_TYPES: frozenset[str] = frozenset({"narration", "subagent_started", "subagent_progress", "subagent_done", "response_chunk", "turn_end"})` with the AC #16 comment block. **Optional improvement**: derive programmatically — `_KNOWN_EVENT_TYPES = frozenset(get_args(get_args(OrchestratorStreamEvent)[0])[0].model_fields["type"].annotation.__args__)` — pyright-strict-friendliness varies. **Recommend** the explicit literal set with the comment; the programmatic form is finicky and the explicit set is grep-able.
  - [ ] **Module-level** TypeAdapter instance: `_event_adapter: TypeAdapter[OrchestratorStreamEvent] = TypeAdapter(OrchestratorStreamEvent)`. Construct once (per pydantic best practice — `TypeAdapter` construction is non-trivial).
  - [ ] Class skeleton:
    ```python
    class HttpOrchestratorClient:
        """SSE consumer for the orchestrator daemon's `POST /turn` slow-path.

        Reuses the persistent ``httpx.AsyncClient`` constructed in
        ``pipeline.py:run_pipeline`` (Story 4.1 wiring) — the orchestrator
        daemon and the belief-state endpoint live behind one origin so a
        single keep-alive pool is the architecturally correct shape.

        Forward-compat: unknown event ``type`` values log WARN + continue
        (Batch 4 decision); framing / JSON / known-type-bad-shape raise
        ``OrchestratorError`` and crash (v1 fail-fast — CLAUDE.md rule #4).
        """

        def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
            self._client = http_client
            self._base_url = base_url.rstrip("/")
    ```
  - [ ] Implement `dispatch` per AC #3-#5. The async-generator shape:
    ```python
    async def dispatch(self, transcript: str, session_id: str) -> AsyncIterator[OrchestratorStreamEvent]:
        url = f"{self._base_url}/turn"
        body = {"transcript": transcript, "session_id": session_id}
        start = time.monotonic()
        event_count = 0
        log.info("orchestrator.dispatch_started",
                 session_id=session_id, transcript_length=len(transcript), url=url)
        try:
            async with aconnect_sse(self._client, "POST", url, json=body,
                                    headers={"Accept": "text/event-stream"},
                                    timeout=httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0)) as event_source:
                async for sse in event_source.aiter_sse():
                    parsed = self._parse_or_warn(sse, session_id)
                    if parsed is None:
                        continue  # forward-compat: unknown type, already WARN-logged
                    event_count += 1
                    log.debug("orchestrator.event_received",
                              event_type=parsed.type, session_id=session_id)
                    yield parsed
        except httpx.HTTPError as exc:
            duration_ms = (time.monotonic() - start) * 1000
            log.warning("orchestrator.dispatch_failed",
                        session_id=session_id, reason=type(exc).__name__,
                        url=url, duration_ms=duration_ms)
            raise OrchestratorError(reason=type(exc).__name__, url=url) from exc
        duration_ms = (time.monotonic() - start) * 1000
        log.info("orchestrator.dispatch_completed",
                 session_id=session_id, event_count=event_count, duration_ms=duration_ms)
    ```
    Where `_parse_or_warn(sse, session_id) -> OrchestratorStreamEvent | None` is the helper that handles AC #4 + #5 logic: peek at `type` → unknown returns None + WARN log; known + valid returns parsed event; framing/JSON/bad-shape raises `OrchestratorError`. The helper is sync (parsing is sync) — keep it as a private method.
  - [ ] Implement `_parse_or_warn` per AC #4 + #5. Pseudocode:
    ```python
    def _parse_or_warn(self, sse: ServerSentEvent, session_id: str) -> OrchestratorStreamEvent | None:
        try:
            raw = json.loads(sse.data)
        except json.JSONDecodeError as exc:
            raise OrchestratorError(reason="invalid_json", session_id=session_id,
                                    raw_length=len(sse.data)) from exc
        type_value = raw.get("type")  # may be None or non-string; both fall to unknown
        if type_value not in _KNOWN_EVENT_TYPES:
            log.warning("orchestrator.unknown_event_type",
                        type=type_value, session_id=session_id)
            return None
        try:
            return _event_adapter.validate_python(raw)
        except ValidationError as exc:
            raise OrchestratorError(reason="invalid_event_shape", type=type_value,
                                    session_id=session_id,
                                    errors=str(exc.errors()[:3])) from exc  # cap repr
    ```
    Note: `validate_python(raw)` (already-parsed dict) avoids re-parsing the JSON. Errors-list capped to first 3 to avoid log/exception bloat.
  - [ ] Implement `probe_health` per AC #6. Pseudocode:
    ```python
    async def probe_health(self) -> None:
        url = f"{self._base_url}/health"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise StartupValidationError(reason=type(exc).__name__, url=url) from exc
        if resp.status_code != 200:
            raise StartupValidationError(reason="orchestrator_health_non_200",
                                         status_code=resp.status_code,
                                         url=str(resp.request.url))
    ```
    Function docstring: "Wired into pipeline startup (Story 4.2's `pipeline.py` extension); refusal to start when daemon is down is the v1 fail-fast contract — see architecture.md §'Cross-project integration'."
  - [ ] Implement `cancel` per AC #7:
    ```python
    async def cancel(self, session_id: str) -> None:
        raise NotImplementedError(
            "Cancel is wired in v1.5 Story v1.5-1 (barge-in). Until then, "
            "in-flight orchestrator turns complete naturally on TurnEndEvent."
        )
    ```
  - [ ] **Pyright-strict** check: run `uv run pyright src/voice_agent_pipeline/turn/orchestrator.py` after writing. Expect zero errors. Likely pyright concerns:
    - `json.loads` returns `Any` — assign to a typed local `raw: dict[str, Any] = json.loads(sse.data)` if pyright complains.
    - `aconnect_sse`'s type stubs may be `Any`-ish — wrap in a local-typed adapter or accept the `Any` exfil at the boundary (it's a third-party lib seam).
  - [ ] **Imports invariant**: after writing, `rg -F 'import httpx_sse' src/` shows only `turn/orchestrator.py`; `rg -F 'from httpx_sse' src/` likewise.

- [x] **Task 2: Pipeline-assembly extension — orchestrator client + `probe_health()`** (AC: #6, #13)
  - [ ] Open `src/voice_agent_pipeline/pipeline.py`. Inside the `async with httpx.AsyncClient(...)` block from Story 4.1:
    - After `belief_client = HttpBeliefStateClient(http_client, base_url=config.daemon.url)`, add:
      `orchestrator_client = HttpOrchestratorClient(http_client, base_url=config.daemon.url)`.
    - Add the startup probe call: `await orchestrator_client.probe_health()` — place it after `event_publisher.connect()` and after `belief_client` construction (the order doesn't matter for `probe_health` itself, but grouping the daemon-related setup keeps the code readable).
    - Inject `orchestrator_client` into the TurnRouter / TurnDispatchProcessor constructor (Story 2.4 baseline) — extend the constructor signature, store as instance attribute. **Do NOT invoke `dispatch()` yet** — Story 4.7 wires that. The existing `NotImplementedError` stub in the orchestrator branch of `TurnRouter` (Story 2.4) stays in place.
  - [ ] Update `pipeline.py`'s module docstring: append a Story 4.2 entry to the "Story progression" list. **Cite specifically** that the `httpx.AsyncClient` is shared between `HttpBeliefStateClient` (Story 4.1) and `HttpOrchestratorClient` (Story 4.2), and that startup probes now include `orchestrator_client.probe_health()`.
  - [ ] **Optional**: a small unit test in `tests/unit/test_pipeline.py` asserting `run_pipeline` constructs the client + probes health. **If wiring this test is high-friction** (lots of pipeline mocks), document the deferral in the dev record per Story 4.1's pattern; the AC #12 unit tests for `probe_health` validate the probe in isolation, and Story 4.7's integration test will validate the full wiring.

- [x] **Task 3: Architecture spec-drift closure** (AC: #11)
  - [ ] Open `build_documents/planning-artifacts/architecture.md`. Find §"Cross-project integration" — the `/health` line in the "Open / pending coordination items" list (around line 248).
  - [ ] **Update the bullet** from "document in cross-project README when first wired" to: "**Wired in Story 4.2** — `HttpOrchestratorClient.probe_health()` requires `GET /health` returning 200 at pipeline startup; `StartupValidationError` raised + non-zero exit otherwise. Closes spec-drift item."
  - [ ] **No PRD or brief change** is needed for this AC — the orchestrator's `/health` endpoint is an architecture-level coordination decision, not a product-level requirement.
  - [ ] Verify with `git diff` that the change is two-line scoped — don't accidentally edit other architecture sections.
  - [ ] **Commit the architecture.md change in the same commit as the code change** (CLAUDE.md rule #9 — spec-as-contract / NFR26).

- [x] **Task 4: Unit tests for `HttpOrchestratorClient`** (AC: #12, #15)
  - [ ] Create `tests/unit/turn/test_orchestrator.py`. Module docstring per `feedback_code_comments.md` — explain: mocks at `httpx.AsyncClient` + `httpx_sse.aconnect_sse` Protocol seams (CLAUDE.md rule #7); each test exercises one behavior of the SSE-streaming + parsing + error-handling contract.
  - [ ] **Imports**: `pytest`, `httpx`, `httpx_sse` (for `ServerSentEvent` test fixtures), `from unittest.mock import AsyncMock, MagicMock, patch`, `from voice_agent_pipeline.errors import OrchestratorError, StartupValidationError`, `from voice_agent_pipeline.turn.orchestrator import HttpOrchestratorClient`, `from voice_agent_pipeline.schemas.stream import NarrationEvent, SubagentStartedEvent, ResponseChunkEvent, TurnEndEvent`.
  - [ ] **Helper fixture**: factory that returns `(client, http_client_mock)` where `http_client_mock = AsyncMock(spec=httpx.AsyncClient)` and `client = HttpOrchestratorClient(http_client_mock, "http://localhost:8001")`.
  - [ ] **Helper for SSE event mocks**: `def make_sse(data: str, event_name: str = "message") -> MagicMock` returning a mock with `.data` and `.event` attrs (mirror `httpx_sse.ServerSentEvent`'s shape).
  - [ ] **Helper for SSE source mocks**: `make_sse_source(events: list[MagicMock])` returning an async-iterator-shaped mock supporting `async with` context. Pattern:
    ```python
    class _MockSSESource:
        def __init__(self, events): self._events = events
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def aiter_sse(self):
            for e in self._events: yield e
    ```
    Patch `voice_agent_pipeline.turn.orchestrator.aconnect_sse` (the imported symbol) to return `_MockSSESource(events)` — use `patch` from `unittest.mock` with the patch target at the *importing* module (Python mocking rule: patch where the symbol is looked up).
  - [ ] Implement the 16 test cases listed in AC #12 using `pytest.mark.asyncio` (auto mode is on per `pyproject.toml`).
  - [ ] **Log-capture pattern** for the privacy / log-level tests: reuse Story 1.7 / 3.6 / 4.1 patterns. For DEBUG-level assertions use `caplog.set_level(logging.DEBUG)` then filter records by level.
  - [ ] **`from exc` cause-chain assertions**: `with pytest.raises(OrchestratorError) as excinfo: ...; assert isinstance(excinfo.value.__cause__, httpx.ConnectError)` — for the connection-error and read-timeout tests.
  - [ ] **Privacy assertion** (`test_dispatch_does_not_log_response_chunk_text`): drive a `response_chunk` with sentinel text `"user_secret_123"`; assert no log record's full text contains the sentinel. Mirror Story 3.7's `test_no_audio_field_names_in_logs` shape.

- [x] **Task 5: Run `just check` + verify regressions** (AC: #14)
  - [ ] `rg -F 'import httpx' src/` — assert only `turn/beliefs.py` (Story 4.1) + `turn/orchestrator.py` (Story 4.2) appear. `rg -F 'import httpx_sse' src/` — only `turn/orchestrator.py`.
  - [ ] `uv run pytest tests/unit/turn/test_orchestrator.py -v` — all new tests pass.
  - [ ] `just check` — ruff lint+format + pyright strict + fast unit tests all green. **No new pyright suppressions** in `src/` without an inline justification comment (architecture.md §"Anti-Patterns").
  - [ ] **No regressions in earlier stories' tests** — Stories 1.x, 2.x, 3.x, 4.1's tests still pass.

- [ ] **Task 6: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] Single commit covering: `turn/orchestrator.py` (impl), `pipeline.py` (orchestrator-client + `probe_health` wiring + TurnRouter constructor injection), `architecture.md` (§"Cross-project integration" spec-drift closure — Task 3), `tests/unit/turn/test_orchestrator.py`, possibly `tests/unit/turn/test_router.py` or `tests/unit/turn/test_dispatch.py` (if the constructor extension breaks existing tests — fix-and-commit), the implementation-artifacts story file (this file — task ticks + dev record).
  - [ ] Suggested commit message: `Story 4.2: OrchestratorClient (httpx-sse stream consumer + /health probe)`.
  - [ ] `git push` immediately after the commit (per `feedback_push_after_commit.md`).
  - [ ] **Sprint-status update**: flip `4-2-orchestrator-client-sse: ready-for-dev` → `review` after live verification (or → `in-progress` first if any Task remains pending).

## Dev Notes

### Architectural intent — Story 4.2's role in Epic 4

Epic 4 builds the conversation-shaped surface. Story 4.1 landed the "ground" half of slow-path (read belief state). **Story 4.2 lands the streaming-dispatch half.** Story 4.7 wires the slow path into the pipeline (`TurnRouter` → `OrchestratorClient.dispatch` → splitter); Story 4.2 only delivers the SSE consumer + the startup `/health` probe.

**Why land 4.2 second** (per `epics.md` Epic 4 sequencing): 4.2 reuses 4.1's persistent `httpx.AsyncClient` lifecycle. The pipeline already constructs the client at startup (Story 4.1 wiring); 4.2 adds a second consumer behind the same client + a startup probe. The TurnRouter side of the wiring (Story 4.7) waits until the activity FSM is in place (Story 4.3) so the slow-path can correctly transition `working[thinking] → working[delegating]` during dispatch.

### Forward-compat: unknown event types vs broken contract

This is the most subtle architectural call in the story. Two failure modes look superficially similar but differ in disposition:

- **Unknown event type** — orchestrator added a new event the pipeline doesn't understand (e.g., it ships a v2 with `subagent_streaming_response`). **Forward-compat**: log WARN + skip + keep consuming the stream. The orchestrator project can ship new event types without breaking the pipeline. Architecture.md §"SSE event dispatch" (Batch 4 decision) makes this explicit.
- **Broken contract** — orchestrator emits a known event type with malformed/missing fields, OR the SSE framing is broken, OR the JSON inside is corrupt. **v1 fail-fast**: raise `OrchestratorError`, crash, systemd restart. The orchestrator violated the agreed shape; resilience layer (v2) decides whether to gracefully recover.

The detection key is the JSON `type` field. Peek at it *before* `TypeAdapter.validate_json`:
- `type` not in `_KNOWN_EVENT_TYPES` → forward-compat case → WARN + skip.
- `type` in `_KNOWN_EVENT_TYPES` but validation fails → broken contract → raise.

Story 4.2 must distinguish. Adding a single `try/except ValidationError` around the whole parse without the type-peek would conflate the two cases — **don't**. The unit tests AC #12 explicitly cover both.

### Why share the `httpx.AsyncClient` with Story 4.1

Two consumers (`HttpBeliefStateClient` + `HttpOrchestratorClient`), one origin (`http://localhost:8001`). httpx's connection pool is keyed by origin — sharing the client means both consumers share the pool, which means keep-alive across belief-state reads + orchestrator dispatches. Per architecture.md §"Connection management": "Persistent `httpx.AsyncClient` per service" — *one* shared client because both endpoints live behind one daemon "service" boundary.

Concretely: a complex turn fires `belief_client.read([...])` (one HTTP GET on the keep-alive pool) and then `orchestrator_client.dispatch(...)` (one HTTP POST + SSE stream on the same pool, reusing the same TCP connection if the timing aligns). Constructing two separate `AsyncClient` instances would mean two independent pools and lose this benefit.

If the v2 orchestrator splits across two services (one for beliefs, one for turn-dispatch), the two-client pattern lands then. Until then, one client.

### Startup probe — why a method on `HttpOrchestratorClient`, not a function

Two design choices:
1. **Free function** in `pipeline.py` — `await probe_orchestrator_health(http_client, base_url)`.
2. **Instance method** on `HttpOrchestratorClient` — `await orchestrator_client.probe_health()`.

Picked (2) because:
- The method has access to `self._base_url` already (no duplication of the rstrip-trailing-slash logic).
- Keeps the pipeline-assembly site clean: one method on the constructed client, not a parallel free-function call.
- If a future v2 swaps `OrchestratorClient` for a non-HTTP impl (e.g., gRPC), the probe semantics live on the impl — a non-HTTP client would have a different probe shape.

The probe is **not on the Protocol** because it's a startup-only impl concern — the Protocol's contract is "dispatch + cancel"; "do you have a startup-side health-check method" varies by impl.

### `ServerSentEvent.event` vs `type` field in JSON

`httpx-sse`'s `ServerSentEvent` has both `.event` (the SSE-protocol-level event name, defaulting to `"message"`) and `.data` (the payload string). The orchestrator's contract (per `schemas/stream.py`'s discriminator design) is to emit a single SSE event name (probably `"message"` or just unset) and put the **discriminator inside the JSON `data` payload** as the `type` field.

**Don't switch on `sse.event` for dispatch**. Switch on the JSON `type` field after parsing `sse.data`. This is what `schemas/stream.py`'s `OrchestratorStreamEvent` discriminated union expects.

If a future orchestrator adopts SSE-event-name-based dispatch (a different protocol shape), it's a `schema_version` bump — Story 4.2's parsing logic doesn't accommodate it forward-compat-wise. That's fine; the SSE-event-name vs `data.type` choice is a contract decision, not a forward-compat concern.

### Test-mocking pattern (CLAUDE.md rule #7)

**Mock only at Protocol boundaries**:
- `httpx.AsyncClient` (Protocol seam to the orchestrator daemon's HTTP transport) — `AsyncMock(spec=httpx.AsyncClient)`.
- `httpx_sse.aconnect_sse` (Protocol seam to the SSE-streaming layer) — `patch` at the importing module.
- `OrchestratorStreamEvent` subclasses (the typed events) — **never mocked**; tests construct real `NarrationEvent(type="narration", text="hi")` instances against the production pydantic models.

**Do NOT install `respx` or `pytest-httpx`**. Architecture's "narrow dep tree" stance — manual mocking with `unittest.mock` is sufficient. Story 4.1 set this precedent for httpx mocking; mirror.

**No real HTTP, no real SSE.** All tests pass with no daemon running, even no network. Story 4.7's integration test will use a fake-daemon fixture (in-process FastAPI / aiohttp-test-server) for E2E validation; that's not Story 4.2's scope.

### Pipeline-assembly wiring (extends Story 4.1)

Story 4.1 wired the persistent `httpx.AsyncClient` at the top of `run_pipeline`. Story 4.2 adds:
1. `orchestrator_client = HttpOrchestratorClient(http_client, base_url=config.daemon.url)` — same construction pattern as `belief_client`.
2. `await orchestrator_client.probe_health()` — startup probe, alongside `event_publisher.connect()`.
3. `orchestrator_client` injection into `TurnRouter` / `TurnDispatchProcessor`'s constructor — **stub-only injection**; Story 4.7 wires the call site.

The TurnRouter constructor extension is the only "may break things" change. Story 2.4's existing tests in `tests/unit/turn/test_router.py` and `test_dispatch.py` may need updating if the constructor signature changes. **Recommend** making the new parameter optional with a default of `None` — TurnRouter only invokes `orchestrator_client.dispatch()` in the `target="orchestrator"` branch, which is currently the `NotImplementedError` stub anyway. So at the type level: `orchestrator_client: OrchestratorClient | None = None`. Story 4.7 will tighten this back to required when it removes the stub. This minimizes the test churn for Story 4.2.

### Stall detection — why 60s read timeout

A slow turn legitimately spans tens of seconds: `narration` (instant) → `subagent_started` (instant) → tool runs for 30s → `subagent_done` → `response_chunk` × N → `turn_end`. The `httpx` `read` timeout fires when no bytes arrive *within the timeout window* — not when the total stream takes longer. So a 60s `read` timeout means "no event has arrived for 60 seconds" — that's a stall, not a long-running turn.

If real-world soak (Story 5.4) shows legitimate >60s gaps inside a slow turn (e.g., a subagent running for 90s with no progress events), tune up. **Recommend**: leave at 60s for v1; Story 5.4 calibrates if needed.

### What this story does NOT do

- **No actual slow-path dispatch wiring.** The TurnRouter stub stays as `NotImplementedError`; Story 4.7 removes it.
- **No `cancel(session_id)` impl.** Stubbed with `NotImplementedError`; v1.5 Story v1.5-1 (barge-in) lands the impl.
- **No retries, no resilience.** v1 fail-fast posture (architecture.md §"V1 Posture"); resilience layer is v2.
- **No real-fake-daemon integration test.** Story 4.7 will set that up for the full slow-path flow.
- **No SSE-server-side concerns.** The orchestrator project owns `/health` + `/turn` endpoints; Story 4.2 only consumes.
- **No belief-state coupling.** Story 4.4 wires belief grounding into Talker; Story 4.2 is the orchestrator side independently.

### Project structure notes

This story creates:
- `tests/unit/turn/test_orchestrator.py` — new test module.

It modifies:
- `src/voice_agent_pipeline/turn/orchestrator.py` — adds `HttpOrchestratorClient` + `_KNOWN_EVENT_TYPES` + `_event_adapter` beneath the existing Protocol.
- `src/voice_agent_pipeline/pipeline.py` — extends Story 4.1's `httpx.AsyncClient` block to also construct `HttpOrchestratorClient`, call `await orchestrator_client.probe_health()`, inject into TurnRouter / TurnDispatchProcessor.
- `src/voice_agent_pipeline/turn/router.py` and/or `src/voice_agent_pipeline/turn/talker.py` — extend constructor signatures to accept `orchestrator_client: OrchestratorClient | None`. **Do NOT invoke `dispatch()`** — the existing `NotImplementedError` stub stays.
- `tests/unit/turn/test_router.py`, `tests/unit/turn/test_dispatch.py` — possibly minor updates for the new constructor parameter (Optional default keeps changes minimal).
- `build_documents/planning-artifacts/architecture.md` — §"Cross-project integration" spec-drift closure (two-line update per AC #11).
- `build_documents/implementation-artifacts/sprint-status.yaml` — `4-2-orchestrator-client-sse: ready-for-dev → in-progress → review`.

It does NOT modify:
- `src/voice_agent_pipeline/schemas/stream.py` — the discriminated union is already complete from Story 1.4. Story 4.2 only consumes.
- `src/voice_agent_pipeline/errors.py` — `OrchestratorError` and `StartupValidationError` already exist from Story 1.4.
- `src/voice_agent_pipeline/turn/beliefs.py` — Story 4.1's territory; Story 4.2's only relationship is sharing the `httpx.AsyncClient`.
- `setup.toml` — no new config fields needed (daemon.url already lands in Story 4.1).
- `src/voice_agent_pipeline/lifecycle/` — stays as the placeholder until Story 4.3 renames to `activity/`.

### Testing standards

- **`pytest-asyncio`** in auto mode (`asyncio_mode = "auto"` in `pyproject.toml`).
- **`AsyncMock(spec=httpx.AsyncClient)`** + **`patch` on `aconnect_sse`** are the only mock surfaces. No `respx`, no `pytest-httpx`, no real HTTP, no real SSE.
- **One behavior per test** — 16 tests in `test_orchestrator.py` per AC #12.
- **Privacy assertions** mirror Story 4.1's `test_read_does_not_log_response_body` pattern. The redaction processor (Story 1.3) is the safety net; Story 4.2's tests verify code path doesn't pass response text into log fields.
- **Pyright strict on `src/`** — `dict[str, Any]` from `json.loads` is the only `Any` exfil and is documented; `aconnect_sse`'s third-party stubs may need an inline `# type: ignore` with reason if pyright's strict mode complains.

### Performance budget

NFR2 slow-path turn budget — `end-of-speech → first narration audio frame ≤1000ms p95` (Story 4.7 measures the baseline). Story 4.2's contribution to that budget:
- `aconnect_sse` connection establishment: ~1-5ms on localhost (keep-alive amortizes after first call).
- Per-event parse: `TypeAdapter.validate_json` is sub-millisecond for the small event shapes here.
- `_KNOWN_EVENT_TYPES` set lookup: O(1).

The hot-path concern is **not parsing latency** but **streaming consumption shape** — Story 4.2 must yield events as they arrive, not buffer. The `async for sse in event_source.aiter_sse(): yield parsed` pattern guarantees this; **don't accumulate into a list and yield at the end**. Architecture.md §"Streaming consumption" — "Real-time latency is the contract."

### What "done" looks like

- `just check` exits 0 (ruff + pyright strict + fast unit tests pass).
- 16 unit tests in `tests/unit/turn/test_orchestrator.py` pass.
- `pipeline.py:run_pipeline` constructs the persistent `httpx.AsyncClient`, builds both `HttpBeliefStateClient` (Story 4.1) and `HttpOrchestratorClient` (Story 4.2), calls `await orchestrator_client.probe_health()`, injects `orchestrator_client` into TurnRouter / TurnDispatchProcessor.
- `architecture.md` §"Cross-project integration" `/health` spec-drift item shows "Wired in Story 4.2" with the closure note.
- `rg -F 'import httpx' src/` shows only `turn/beliefs.py` + `turn/orchestrator.py`. `rg -F 'import httpx_sse' src/` shows only `turn/orchestrator.py`.
- Story 2.4's `NotImplementedError` stub in TurnRouter's orchestrator branch **stays in place** — Story 4.7 removes it.
- Sprint-status flips to `review` after green.

### References

- [Source: build_documents/planning-artifacts/architecture.md#External Clients (Batch 4)] — `httpx (async)` + `httpx-sse`; SSE event dispatch by `type` field; unknown types → log + ignore (forward-compat); framing/JSON errors → raise → crash.
- [Source: build_documents/planning-artifacts/architecture.md#Connection management] — persistent `httpx.AsyncClient` per service, lifecycle-bound; startup validation: connect + `GET /health` against orchestrator daemon.
- [Source: build_documents/planning-artifacts/architecture.md#Async Patterns] — `async with httpx.AsyncClient() as client:` lifecycle pattern.
- [Source: build_documents/planning-artifacts/architecture.md#Project Structure & Boundaries] — `turn/orchestrator.py` is the only file (with `turn/beliefs.py`) allowed to import `httpx`; only `turn/orchestrator.py` may import `httpx_sse`.
- [Source: build_documents/planning-artifacts/architecture.md#Internal seams] — `OrchestratorClient` Protocol consumed by `turn/router.py`.
- [Source: build_documents/planning-artifacts/architecture.md#Cross-project integration] — orchestrator daemon `GET /health` spec-drift item — closed by this story.
- [Source: build_documents/planning-artifacts/architecture.md#Error Handling] — `OrchestratorError(ExternalServiceError)` (in-flight) vs `StartupValidationError` (startup probe); both fail-fast (CLAUDE.md rule #4).
- [Source: build_documents/planning-artifacts/architecture.md#Schema versioning] — bump only on breaking changes; forward-compat additions don't bump (CLAUDE.md rule #6).
- [Source: build_documents/planning-artifacts/prd.md#FR11] — "The pipeline can dispatch a user turn to the orchestrator daemon via `POST /turn` and consume the typed event stream."
- [Source: build_documents/planning-artifacts/prd.md#NFR25, FR39] — privacy invariant: never log transcripts or response text at INFO+.
- [Source: build_documents/planning-artifacts/prd.md#NFR2] — slow-path turn budget (≤1000ms p95 end-of-speech → first narration audio frame); Story 4.7 measures.
- [Source: build_documents/planning-artifacts/epics.md#Story 4.2: OrchestratorClient — SSE stream consumer over httpx-sse] — full AC list.
- [Source: build_documents/planning-artifacts/epics.md#Epic 4 Goal] — "orchestrator slow-path with belief-state grounding."
- [Source: build_documents/planning-artifacts/epics.md#v1.5 Backlog (Post-v1)#Story v1.5-1: Barge-in] — `cancel(session_id)` is wired here, not in Story 4.2.
- [Source: build_documents/implementation-artifacts/4-1-belief-state-client.md] — `httpx.AsyncClient` lifecycle pattern + `OrchestratorError` raise pattern + privacy invariant testing pattern. Story 4.2 mirrors all three.
- [Source: build_documents/implementation-artifacts/3-7-audio-frame-metadata-and-ssml-prompt.md] — pipeline-assembly extension pattern; Story 4.2 extends 3.7's `run_pipeline`.
- [Source: src/voice_agent_pipeline/turn/orchestrator.py] — Protocol scaffold (already landed; Story 4.2 adds the impl).
- [Source: src/voice_agent_pipeline/schemas/stream.py] — `OrchestratorStreamEvent` discriminated union; the `TypeAdapter.validate_json` pattern documented in the module docstring.
- [Source: src/voice_agent_pipeline/errors.py] — `OrchestratorError(ExternalServiceError)` + `StartupValidationError(VoiceAgentError)` already defined; Story 4.2 raises both.
- [External: https://www.python-httpx.org/async/] — `httpx.AsyncClient` reference; lifecycle and timeout configuration.
- [External: https://www.encode.io/httpx-sse/] — `httpx-sse` documentation; `aconnect_sse` async context manager pattern; `ServerSentEvent` shape.
- [External: https://docs.pydantic.dev/latest/concepts/type_adapter/] — `TypeAdapter.validate_json` / `validate_python` patterns; performance considerations (construct once, reuse).

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- **Discriminator-peek vs single-validate**: First impl was a single
  ``try: TypeAdapter.validate_python(raw); except ValidationError``
  — but that conflates forward-compat (unknown ``type``) with broken
  contract (known ``type`` + bad fields). Refactored to peek
  ``raw["type"]`` first, check membership in ``_KNOWN_EVENT_TYPES``,
  then validate. Unknown → WARN+skip; known+invalid → raise.
  Test ``test_dispatch_unknown_event_type_logs_warn_and_continues``
  pins the forward-compat behavior.
- **Pyright narrowing on ``json.loads`` → dict**: Same pattern as
  Story 4.1 — ``cast(dict[str, Any], raw)`` after ``isinstance(raw,
  dict)`` so the ``raw_dict.get("type")`` call has a typed return.
- **httpx_sse mocking pattern**: ``aconnect_sse`` is itself an
  ``@asynccontextmanager``-decorated function. The test fake mirrors
  this with a small ``@asynccontextmanager`` of its own that yields
  a ``_FakeEventSource`` whose ``aiter_sse()`` is an async generator.
  Patched at the importing module
  (``voice_agent_pipeline.turn.orchestrator.aconnect_sse``).
  Documented in the test file's helper comment block.
- **Per-call timeout override**: AC #8 requires read=60s for SSE
  streams (a slow turn legitimately spans tens of seconds). Story
  4.1's shared client uses read=10s for one-shot GETs; the dispatch
  call passes a per-request ``timeout=httpx.Timeout(read=60.0, ...)``
  to ``aconnect_sse``. Test
  ``test_dispatch_read_timeout_raises_orchestrator_error_with_cause``
  validates the ReadTimeout → OrchestratorError mapping.

### Completion Notes List

- **Tasks 1-6 satisfied as written.** No deviations from the story
  spec.
- **AC coverage:**
  - AC #1: Protocol stays narrow (``dispatch`` + ``cancel``).
  - AC #2: Constructor takes injected ``httpx.AsyncClient`` from
    Story 4.1's ``async_http_client()``; rstrips trailing slash.
  - AC #3: ``aconnect_sse`` opens POST /turn with the right body +
    Accept header; events parsed via cached module-level
    ``TypeAdapter[OrchestratorStreamEvent]``.
  - AC #4: Unknown event ``type`` → WARN + skip (forward-compat).
  - AC #5: Framing / JSON / known-shape errors → ``OrchestratorError``.
  - AC #6: ``probe_health()`` raises ``StartupValidationError`` on
    non-200 / transport error.
  - AC #7: ``cancel(session_id)`` raises ``NotImplementedError`` with
    a v1.5-pointer message.
  - AC #8: Per-call read=60s; test pins the stall-detection contract.
  - AC #9: ``rg -F 'import httpx_sse' src/`` shows only
    ``turn/orchestrator.py``; ``rg -F 'import httpx' src/`` shows only
    ``turn/beliefs.py`` + ``turn/orchestrator.py``. Pyright-strict on
    full ``src/`` is clean.
  - AC #10: INFO ``orchestrator.dispatch_started`` / ``_completed``;
    WARN ``orchestrator.unknown_event_type`` / ``dispatch_failed``.
    Privacy invariant validated by
    ``test_dispatch_does_not_log_response_chunk_text`` (sentinel
    string check).
  - AC #11: architecture.md §"Open / pending coordination items"
    ``/health`` line updated in this commit (NFR26 — spec-as-contract).
  - AC #12: 17 unit tests in ``tests/unit/turn/test_orchestrator.py``
    (one extra over the 16 in the story spec — added
    ``test_dispatch_yields_subagent_done_event_correctly`` for full
    union-member coverage).
  - AC #13: Pipeline-assembly extension — ``orchestrator_client``
    constructed inside ``async_http_client`` block; ``probe_health()``
    awaited at startup; passed to ``TurnRouter`` (Story 2.4 already
    accepted ``orchestrator: OrchestratorClient | None``).
  - AC #14: ``just check`` green — 350 unit tests, 0 ruff/pyright issues.
  - AC #15: Privacy invariant validated.
  - AC #16: ``_KNOWN_EVENT_TYPES`` constant has the inline forward-compat
    comment block.

### File List

**New files:**
- ``tests/unit/turn/test_orchestrator.py`` — 17 unit tests covering
  dispatch happy-path, forward-compat skip, broken-contract raises
  (invalid_json, invalid_event_shape, non-dict payload), connection /
  read-timeout error chain, dispatch_started privacy log assertion,
  dispatch_completed log, response_chunk privacy invariant,
  probe_health success/fail/transport, cancel stub, persistent-client
  invariant, full union-member coverage.

**Modified files:**
- ``src/voice_agent_pipeline/turn/orchestrator.py`` — added
  ``HttpOrchestratorClient`` (dispatch + probe_health + cancel stub),
  ``_KNOWN_EVENT_TYPES`` constant, module-level ``_event_adapter``
  TypeAdapter, ``_parse_or_warn`` helper. Re-exports the schema event
  types for caller ergonomics.
- ``src/voice_agent_pipeline/pipeline.py`` — extended the Story 4.1
  ``async with async_http_client():`` block to also construct
  ``HttpOrchestratorClient``, ``await probe_health()`` at startup,
  inject into ``TurnRouter(... orchestrator=orchestrator_client)``.
- ``build_documents/planning-artifacts/architecture.md`` —
  §"Open / pending coordination items" ``/health`` bullet updated to
  "wired in Story 4.2" with the closure note. Closes the spec-drift item.
- ``build_documents/implementation-artifacts/sprint-status.yaml`` —
  ``4-2-orchestrator-client-sse: ready-for-dev → in-progress → review``.
- ``build_documents/implementation-artifacts/4-2-orchestrator-client-sse.md`` —
  this file: tasks ticked, dev record + file list populated.

## Change Log

| Date | Change |
|---|---|
| 2026-05-07 | Story 4.2 prepared — OrchestratorClient (httpx-sse stream consumer + /health probe). |
| 2026-05-07 | Story 4.2 implemented — HttpOrchestratorClient (dispatch + probe_health + cancel stub). 17 unit tests covering happy path, forward-compat unknown-type, broken-contract raises, transport errors, privacy invariant. ``just check`` green (350 unit tests). Architecture.md /health spec-drift closed. ``import httpx`` / ``httpx_sse`` invariant honored. |
