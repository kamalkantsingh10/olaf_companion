# Story 6.1: Cartesia SSE→WebSocket migration + word `timestamps` capture + TTFB spike

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

**Execution-order note:** First story in Epic 6 ("v2 Expression Upgrades"). It is the **shared enabler** for Stories 6.2 (which needs the new overlap-friendly Cartesia client + the spike's TTFB number to finalise the opener subsystem design) and 6.3 (which needs `timestamps` capture for the emphasis-join). The current v1 finish-line execution order (epics.md): `Epic 6 (6.1 → 6.2 ∥ 6.3 → 6.4) → 5-1 → 5-2 → 5-3 → 5-4`.

## Story

As Kamal,
I want `tts/cartesia.py` to stream over the **Cartesia WebSocket API** instead of SSE, capture per-segment word `timestamps` events (currently silently dropped at the SSE branch `tts/cartesia.py:129`), and run a one-shot **TTFB measurement spike** of 100+ requests across the websocket transport,
so that (a) Story 6.3's emphasis-vocalization join has the timing data it needs (the LLM's marked-word indices × Cartesia's word `start_ms` per segment); (b) Story 6.2's opener subsystem is shaped against a **measured** TTFB number rather than a guessed one, resolving DR-001's keystone open question (whether the cached-opener subsystem could be reshaped toward pure-live per DR-001 Option D); (c) the v1 SSE shape stays available as a config-knob fallback for the implementation window so the team can flip back if WS proves flaky in early soak.

## Acceptance Criteria

1. **Cartesia transport swaps from SSE → WebSocket in `tts/cartesia.py`.** The implementation uses Cartesia SDK 3.0.2's **`async_client.tts.websocket_connect(...)` → `AsyncTTSResourceConnection`** (the `AsyncTTSResourceConnectionManager` async-context-manager flow). The connection is opened once per `synthesize()` call (open → send `GenerationRequest` → consume responses → close on `Done`). The base-class flow is the same shape `generate_sse` had — yield `bytes` as fast as `Chunk` responses arrive — but with timestamps captured (see AC #2) instead of silently dropped. **The `TTSClient` Protocol signature does NOT change** — callers continue to call `synthesize(text) -> AsyncIterator[bytes]` and get raw S16LE PCM bytes at 16 kHz mono. The CartesiaError hierarchy is unchanged: any `cartesia.APIError` or `websockets.exceptions.ConnectionClosedError` (and subclasses) wraps as `CartesiaError` and propagates per CLAUDE.md rule #4 (no downstream catches).

   - **SDK reference call shape** (verified against `cartesia==3.0.2`):

     ```python
     # tts/cartesia.py — new WS path (sketch)
     async with self._client.tts.websocket_connect() as conn:
         await conn.send(GenerationRequest(
             model_id=self._config.model,                  # "sonic-3"
             output_format=_OUTPUT_FORMAT,                 # raw S16LE 16 kHz
             transcript=text,
             voice={"id": self._config.voice_id, "mode": "id"},  # VoiceSpecifierParam
             generation_config={"emotion": self._config.default_emotion,
                                "speed":   self._config.speed},
             add_timestamps=True,                          # NEW for Story 6.1
             add_phoneme_timestamps=False,                 # see AC #3 — zero-cost only
         ))
         async for event in conn:
             if event.type == "chunk":
                 yield event.audio                         # bytes property auto-decodes base64
             elif event.type == "timestamps":
                 self._capture_timing(event.word_timestamps)
             elif event.type == "done":
                 break
             elif event.type == "error":
                 raise CartesiaError(..., reason=event.error) ...
     ```

   - `websocket_connect()` returns an `AsyncTTSResourceConnectionManager` (an async context manager). `__aenter__` opens the websocket and returns an `AsyncTTSResourceConnection`. The connection's `__aiter__` yields `WebsocketResponse` union types (`Chunk`, `Timestamps`, `PhonemeTimestamps`, `Done`, `FlushDone`, `Error`). Closing on `Done` is critical — without an explicit close, the connection lingers and the event loop hangs at pipeline shutdown.

2. **Per-segment `timestamps` capture into `SegmentTiming(words: list[Word])`.** A new dataclass / pydantic model `SegmentTiming` is exposed by `tts/cartesia.py` (or a sibling `tts/timing.py` if it grows). The Cartesia WS `Timestamps` event payload is shaped as:

   ```python
   # cartesia.types.websocket_response.Timestamps
   class Timestamps(BaseModel):
       type: Literal["timestamps"]
       done: bool
       status_code: int
       word_timestamps: TimestampsWordTimestamps | None
       context_id: str | None
       flush_id: int | None

   class TimestampsWordTimestamps(BaseModel):
       words: list[str]                # ["I", "really", "think"]
       start: list[float]              # [0.0, 0.24, 0.60]   seconds
       end:   list[float]              # [0.06, 0.52, 0.82]
   ```

   `SegmentTiming` projects this onto a per-word list:

   ```python
   class Word(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       text: str
       start_ms: int
       end_ms: int

   class SegmentTiming(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       words: list[Word]
   ```

   - **Unit conversion:** Cartesia emits seconds as floats; the pipeline uses milliseconds as ints everywhere (see `tts.first_frame.ttfb_ms`, `audio_frame_id` semantics in `splitter/segmenter.py` future work). Round to nearest int millisecond on capture.
   - **One `SegmentTiming` per generation request.** A single Cartesia `synthesize(text)` call corresponds to one synthesized segment; one or more `Timestamps` events may arrive (typically one for the whole segment, but the spec allows incremental). Accumulate words across events within the same request.
   - **Exposure to callers:** the `CartesiaClient` instance exposes a method `last_segment_timing() -> SegmentTiming | None` that returns the most recently accumulated timing after the previous `synthesize()` generator exhausts. **No change to the `TTSClient` Protocol signature** — Story 6.3 will consume this via the concrete class, not via the Protocol (downstream contract evolves there, not here). If `add_timestamps=False` was effectively used (e.g., the SSE fallback path), `last_segment_timing()` returns `None`.

3. **`phoneme_timestamps` capture is opt-in and OFF by default.** The `GenerationRequest.add_phoneme_timestamps` parameter is hard-coded to `False` in v1 WS path. Capturing phonemes adds non-trivial bandwidth and we have no consumer for them in Epic 6 (lip-sync is a separate future project). Story 6.1 does NOT add a config knob for phoneme capture — that's a deliberate choice to keep scope tight. Future lip-sync work warrants its own decision record.

4. **TTFB measurement spike — 100+ sample requests, report committed.**
   - New CLI tool: `src/voice_agent_pipeline/tts/ttfb_spike.py`, runnable as `python -m voice_agent_pipeline.tts.ttfb_spike` (and via a `justfile` recipe — see AC #6).
   - Reads `setup.toml` + `.env` via the existing `load_setup_config` path; uses `CartesiaClient` (no new SDK surface).
   - Sends **at least 100 requests** through the websocket transport. Each request uses one of a small pool of varied test transcripts (5–10 sentences, mixed lengths 3–30 words — covering realistic Talker-reply spans). Random cycling through the pool; do NOT use the same transcript every time (some Cartesia endpoints cache same-text responses, which would skew the measurement).
   - Per request, measure **TTFB** = time from `await conn.send(GenerationRequest)` completion to the **first `Chunk` event received**. Same single-clock methodology as the existing `tts.first_frame.ttfb_ms` log at `tts/cartesia.py:145` (preserves comparability with the production log baseline).
   - Tool writes a report to `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md` containing:
     - Run metadata: timestamp, voice_id, model, sample count, dev-host fingerprint (machine, kernel, network type), Cartesia SDK version.
     - Statistics: p25, p50 (median), p75, p90 of TTFB across the sample. Same percentile shape as DR-001 §"Empirical evidence" so the numbers slot directly into the design-record's table.
     - Raw timings: the full 100+ sample list as a fenced code block (so the spike can be re-checked or re-aggregated).
     - **Keystone-question call-out:** if median TTFB ≤ 0.4 s, an explicit "DR-001 OPTION D VIABILITY" section calls out that Story 6.2 should re-evaluate whether the cached-opener subsystem is still justified versus pure-live (see DR-001 §"Open question (keystone)"). If median TTFB > 0.4 s, an explicit "CACHED OPENER DESIGN STANDS" section preserves the v1-shape opener plan.
     - Comparison band: the report references DR-001's existing measurement of median TTFB on SSE (~1.07 s p50, ~1.64 s p75 — DR-001 §"Empirical evidence") so the SSE→WS delta is visible.

5. **`[tts] transport` config knob in `setup.toml`.** A new field `transport: Literal["websocket", "sse"] = "websocket"` on `TtsConfig` (in `config/setup.py`). The implementation contains both paths:
   - WS path (new, default): `_synthesize_websocket(text)`.
   - SSE path (legacy, retained): `_synthesize_sse(text)` — the current `tts.generate_sse(...)` flow, lifted into a private method. `last_segment_timing()` returns `None` from this path (SSE drops timestamps; Story 6.1 explicitly does NOT add timestamps to the SSE path because the SSE event stream omits them upstream — operators wanting timestamps must use WS).
   - The public `synthesize(text)` method dispatches on `self._config.transport`. Behaviour identical from the caller's perspective for the bytes-yield contract; only the side-effect (timing capture) differs.
   - **Removal plan:** the SSE fallback exists for the implementation window only. Once WS is settled in soak (verified during Story 6.4's v2 soak), a future small story removes the SSE branch + the `transport` knob, narrowing the surface back to one path. Do NOT remove SSE in Story 6.1.

6. **`just ttfb-spike` recipe.** New `justfile` recipe:
   ```
   ttfb-spike:
       uv run python -m voice_agent_pipeline.tts.ttfb_spike
   ```
   Runs the spike against the configured `[tts]` voice/model, writes the report at the canonical path. Idempotent — re-running overwrites the previous report with the same filename (older reports go in git history if needed).

7. **All existing tests continue to pass.** The SSE→WS swap is internal; the `TTSClient` Protocol contract (`synthesize(text) -> AsyncIterator[bytes]`) is unchanged. Specifically:
   - `tests/unit/tts/test_cartesia.py` — keep passing. May need a fixture/mock update if the test mocks `generate_sse` directly (which is now only used in SSE-fallback mode). The unit test should ideally cover BOTH transports — see AC #8.
   - `tests/integration/test_simple_turn.py` — keep passing. This test exercises the simple turn loop end-to-end with mocked external services; the Cartesia mock should still satisfy `synthesize(text) -> AsyncIterator[bytes]`.

8. **New unit tests** in `tests/unit/tts/test_cartesia.py`:
   - **WS-path mock**: mock `AsyncCartesia.tts.websocket_connect()` to return a fake `AsyncTTSResourceConnection` that yields a scripted sequence of `Chunk` / `Timestamps` / `Done` events. Assert:
     - `synthesize(text)` yields bytes in arrival order (matching scripted chunks).
     - After exhaustion, `client.last_segment_timing()` returns a `SegmentTiming` whose `words` list matches the scripted `Timestamps.word_timestamps` (with seconds → ms rounding).
     - On `Error` event, `synthesize()` raises `CartesiaError` (no swallowing).
     - On `WebSocket.ConnectionClosedError` mid-stream, `synthesize()` raises `CartesiaError` (same).
   - **SSE-path mock** (existing): still passes through the SSE branch when `[tts] transport = "sse"`. `last_segment_timing()` returns `None`.
   - **Transport-dispatch test**: `TtsConfig(transport="websocket")` routes to WS path; `TtsConfig(transport="sse")` routes to SSE path. The dispatch is exercised at the `synthesize()` entry.

9. **Documentation updates** (same commit):
   - `setup.toml` `[tts]` block gains a comment-line documenting the new `transport` field, default value, and that the SSE fallback is intended for the v2 implementation window only.
   - `README.md` gains a one-line entry under "Common commands" or similar: `just ttfb-spike — run the Cartesia TTFB measurement spike (writes report under build_documents/implementation-artifacts/)`.
   - `build_documents/planning-artifacts/architecture.md` — the "Cartesia API" boundary row + the v2 implementation-sequence item #20 footnote already point at Story 6.1 (commits ccfc0a0 / 4797b9a); verify they're still accurate after this story lands and amend in this same commit if drift surfaces (NFR26 spec-as-contract).
   - `build_documents/planning-artifacts/decision-records.md` — once the spike report is written, the spike-report path is referenced from DR-001 §"Open question (keystone)" as the resolution artefact. The DR file says records are immutable once frozen, so the reference is appended (a small "Closed by: 6-1-ttfb-spike-report.md (2026-MM-DD)" line under the keystone section, or as a new minor DR record — the implementer picks the lightest-touch form consistent with the file's doctrine).

10. **Commit policy (Task 8 below).** Single commit per `feedback_commit_policy.md`. Push immediately after commit per `feedback_push_after_commit.md`. The Story 6.1 spike-report file is **included in the commit** (operators clone the repo and have the result at a stable path — same posture as Story 5.5's committed manifest + WAVs).

## Tasks / Subtasks

- [x] **Task 1: Config schema — `[tts] transport` field** (AC: #5)
  - [x] Add `transport: Literal["websocket", "sse"] = "websocket"` to `TtsConfig` in `config/setup.py`
  - [x] Update the `TtsConfig` docstring's "Attributes" section to cover the new field, the default rationale, and the SSE-fallback removal plan
  - [x] Add `transport = "websocket"` (commented) to `setup.toml`'s `[tts]` block with a documentation comment
  - [x] Extend `tests/unit/config/test_setup.py` to cover (a) default value (b) accepts "sse" (c) rejects any other string

- [x] **Task 2: WS-path implementation in `tts/cartesia.py`** (AC: #1, #2, #3)
  - [x] Define `Word` + `SegmentTiming` pydantic models. Place in `tts/cartesia.py` (small) or a new `tts/timing.py` (if it grows past one screen). Both frozen, both `extra="forbid"`.
  - [x] Add `self._last_segment_timing: SegmentTiming | None = None` instance state to `CartesiaClient`
  - [x] Implement `async def _synthesize_websocket(self, text: str) -> AsyncIterator[bytes]:`
    - [x] `async with self._client.tts.websocket_connect() as conn:`
    - [x] `await conn.send(GenerationRequest(..., add_timestamps=True, add_phoneme_timestamps=False))`
    - [x] `async for event in conn:` — dispatch on `event.type`
    - [x] `chunk` → `yield event.audio` (the `.audio` property auto-decodes base64)
    - [x] `timestamps` → accumulate via `_word_timestamps_to_words(event.word_timestamps)`; convert seconds → integer ms (round-to-nearest)
    - [x] `done` → break (close on context-manager exit)
    - [x] `error` → raise `CartesiaError` with `reason=event.error` (with fallback to `event.message` / `event.title` per the real SDK's looser-than-typed Error event shape — observed during the spike's first run)
    - [x] log `tts.first_frame` on first `chunk` event (preserves the existing ttfb_ms metric; adds `transport="websocket"` field)
  - [x] Reset `self._last_segment_timing = None` at the **start** of `_synthesize_websocket()` so callers never see stale timing from a prior request
  - [x] Wrap `cartesia.APIError` AND `websockets.exceptions.ConnectionClosedError` (+ subclasses) as `CartesiaError`; use `raise ... from e`
  - [x] **Implementation note (discovered during smoke-test):** Cartesia's WS API rejects `GenerationRequest` without a `context_id` (HTTP 400, `"context_id is invalid"` because the SDK auto-fills a composite id with an empty trailing segment). Story 6.1 generates a fresh `uuid.uuid4().hex` per call and threads it into `GenerationRequest(context_id=...)`. Alphanumeric + underscore + hyphen are the only allowed characters per Cartesia's validator; `uuid4().hex` (no dashes) satisfies it. Documented inline so future contributors don't reintroduce the bug.

- [x] **Task 3: SSE-path retained as fallback + dispatch in `synthesize()`** (AC: #1, #5)
  - [x] Lift the existing `generate_sse` flow into `_synthesize_sse(self, text: str) -> AsyncIterator[bytes]:` — identical behaviour to today including the `tts.first_frame` log (now tagged with `transport="sse"`)
  - [x] `_synthesize_sse()` does NOT set `self._last_segment_timing` (SSE drops timestamps; the WS path is the only one that captures)
  - [x] Implement `synthesize()` as a thin dispatcher that returns `self._synthesize_websocket(text)` or `self._synthesize_sse(text)` based on `self._config.transport`
  - [x] Confirm with a unit test that the dispatch picks the right path (`test_transport_dispatch_websocket_routes_to_ws_path` + `test_transport_dispatch_sse_routes_to_sse_path`)

- [x] **Task 4: `last_segment_timing()` accessor** (AC: #2)
  - [x] Public method on `CartesiaClient`: `def last_segment_timing(self) -> SegmentTiming | None`
  - [x] Returns the most recently captured `SegmentTiming` after a `synthesize()` generator exhausts (WS path) or `None` (SSE path / not yet called)
  - [x] Module docstring at the top of `tts/cartesia.py` documents the per-call lifecycle: reset on synthesize-start, populated as timestamps events arrive, readable after the generator exhausts

- [x] **Task 5: TTFB spike CLI** (AC: #4, #6)
  - [x] New module `src/voice_agent_pipeline/tts/ttfb_spike.py`
  - [x] Loads `setup.toml` + `.env` via `load_setup_config` (existing helper)
  - [x] Builds a `CartesiaClient` with `transport="websocket"` regardless of config (the spike measures WS specifically)
  - [x] Cycles through ~20 test transcripts (defined inline in the module — short / medium / long / question / statement mix). Sample length spans 3–~30 words to cover realistic Talker reply spans
  - [x] **Upgraded from 100 to 500 samples (250 cold + 250 warm) per Kamal's 2026-05-28 request — output goes into a research paper**, so the spike now stratifies by transcript-length bucket (short / medium / long) and adds a cold-vs-warm connection comparison (one-WS-per-call vs. one reused WS for many calls). Per request: open a connection, send, record `t_send`, capture first chunk arrival `t_first_chunk`, compute `ttfb_ms = (t_first_chunk - t_send) // 1_000_000`. Sleep ~75 ms between requests so we don't saturate Cartesia and skew TTFB upward
  - [x] Aggregates p25 / p50 / p75 / p90 / p95 / p99 + mean + stdev + min + max via stdlib `statistics`. Per-mode (overall / cold / warm) and per-bucket (short / medium / long) statistic blocks.
  - [x] Writes the report to `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md` with the upgraded template (run metadata, percentile tables, warm-vs-cold delta, keystone-question call-out, raw samples in a fenced code block, SSE-comparison reference to DR-001's existing numbers)
  - [x] `justfile`: add the `ttfb-spike` recipe

- [x] **Task 6: Unit tests** (AC: #7, #8)
  - [x] In `tests/unit/tts/test_cartesia.py`:
    - [x] **WS path — happy:** mock `websocket_connect` to return an async context manager yielding `[Chunk, Chunk, Timestamps, Chunk, Done]`. Assert bytes order matches; `last_segment_timing()` returns the correct `SegmentTiming` post-exhaustion
    - [x] **WS path — error event:** mock yields `[Chunk, Error]`. Assert `CartesiaError` raises mid-stream
    - [x] **WS path — connection closed:** mock raises `ConnectionClosedError`. Assert `CartesiaError` raises
    - [x] **WS path — seconds→ms rounding:** mock `Timestamps.word_timestamps` with `start=[0.0, 0.1234, 0.789]`. Assert captured `Word.start_ms` values match the round-to-nearest convention
    - [x] **WS path — open error wraps:** mock manager `__aenter__` raises `APIError`, asserts CartesiaError surfaced with cause chain
    - [x] **WS path — last_segment_timing reset between calls:** two-call test confirms a Timestamps-less follow-up call clears the prior call's timing
    - [x] **WS path — multi-Timestamps accumulation:** two `Timestamps` events in one request accumulate into one `SegmentTiming`
    - [x] **WS path — first_frame log carries transport tag**
    - [x] **WS path — GenerationRequest add_timestamps=True, add_phoneme_timestamps=False contract**
    - [x] **SSE path:** existing test pattern still passes when `TtsConfig(transport="sse")`. `last_segment_timing()` returns `None`
    - [x] **Transport dispatch:** `TtsConfig(transport="websocket")` routes to WS; `TtsConfig(transport="sse")` routes to SSE — verified via mock-method-called assertions
    - [x] **Pure-helper tests:** `Word` + `SegmentTiming` frozen + `extra="forbid"`
  - [x] In `tests/unit/config/test_setup.py`: `transport` field accepts both literals + defaults to `"websocket"` + rejects other strings
  - [x] No contract test extension needed — `transport` is config-side, not wire-side

- [x] **Task 7: Run the spike + commit the report** (AC: #4, #10)
  - [x] Run `just ttfb-spike` against the dev host (500 samples — 250 cold + 250 warm)
  - [x] Verify the report writes correctly and the keystone-question call-out renders. Result: **overall p50 = 383 ms** (under the 400 ms threshold via mixed cold+warm) → "DR-001 OPTION D VIABILITY" branch fired. **However, runtime cold p50 = 467 ms** (above 400 ms); the runtime path is one-WS-per-call (cold), so production today still sits above the keystone threshold. Warm pooling would unlock Option D (230 ms p50). The report's hedged "may close" wording captures this nuance correctly.
  - [x] Commit the report file (`6-1-ttfb-spike-report.md`) alongside the code changes — same single commit per `feedback_commit_policy.md`
  - [x] Sanity-check the median against DR-001's SSE baseline (~1.07 s): **SSE→WS cut cold p50 by ~57%** (1070 ms → 467 ms). Substantial improvement; consistent with switching from request-response SSE to a streaming WS that starts decoding immediately. No measurement bug suspected (0/500 errors; warm path further halves TTFB which is exactly what amortising TLS+WS handshake predicts).

- [x] **Task 8: Docs + commit** (AC: #9, #10)
  - [x] `setup.toml` `[tts]` comment block
  - [x] `README.md` "Common commands" entry for `just ttfb-spike`
  - [x] `build_documents/planning-artifacts/architecture.md` — verified; v2 implementation-sequence item #20 footnote + Cartesia boundary row at line 183 already accurately describe Story 6.1. No amendment needed (NFR26 spec-as-contract preserved).
  - [x] `build_documents/planning-artifacts/decision-records.md` — appended "Closed by: 6-1-ttfb-spike-report.md (2026-05-28)" closure paragraph under DR-001's "Open question (keystone)" section, including a note about the warm-vs-cold delta which the closure section also calls out
  - [x] `just check` green: ruff (lint + format), pyright (0 errors), `pytest tests/unit -q` (546 passed)
  - [x] Single commit per `feedback_commit_policy.md`; push to origin per `feedback_push_after_commit.md`

## Dev Notes

### Relevant architecture patterns and constraints

- **Boundary concentration (`CLAUDE.md` + `architecture.md` §"Architectural Boundaries"):** the `cartesia` SDK is imported in exactly two files today — `tts/cartesia.py` (runtime) and `audio/regenerate.py` (offline asset render, reuses `CartesiaClient`). Story 6.1 does NOT add a third import site; the WS swap lives inside `tts/cartesia.py`. The TTFB spike (`tts/ttfb_spike.py`) reuses `CartesiaClient` — same boundary concentration as `audio/regenerate.py`.

- **No knob to disable TLS validation (NFR24, `CLAUDE.md`):** `AsyncCartesia(api_key=...)` is used as-is. The WS connection inherits the same TLS posture (the Cartesia SDK uses `websockets` library under the hood, which validates by default). Do NOT add `verify=False` / `ssl_context` overrides anywhere.

- **Fail-fast posture (CLAUDE.md rule #4, `project_v1_scope_fail_fast.md`):** `cartesia.APIError` and `websockets.exceptions.ConnectionClosedError` (and subclasses thereof) wrap as `CartesiaError` (existing `ExternalServiceError` subclass) and are never caught downstream. Process crashes; systemd restarts (when Story 5-3 lands). Story 6.1 preserves this.

- **Pydantic at boundaries (CLAUDE.md rule 3):** `Word` and `SegmentTiming` are pydantic v2 `BaseModel` with `model_config = ConfigDict(frozen=True, extra="forbid")`. `TtsConfig` already follows this pattern; the new `transport` field is `Literal["websocket", "sse"]` (no Enum).

- **No `Any` in `src/` (CLAUDE.md):** the SDK's `WebsocketResponse` union is typed; dispatch on `event.type` and access fields via the concrete subclass shapes documented in `cartesia.types.websocket_response`. Avoid `getattr(event, "type", None)` style; the existing code uses it for the SSE path because the SSE event union has subclasses without `.data`, but the WS `WebsocketResponse` union is cleaner.

- **Audio format pinning:** 16 kHz mono S16LE is the pipeline-wide invariant (`audio/transport.py:_SAMPLE_RATE`, `_OUTPUT_FORMAT` in `tts/cartesia.py:46`). The WS path uses the same `_OUTPUT_FORMAT` dict — no resampling, no format change.

- **TTFB metric continuity:** the existing `tts.first_frame.ttfb_ms` log at `tts/cartesia.py:145` is what DR-001 §"Empirical evidence" mined (production logs n=308). Preserve this log on the WS path so the log-time-series stays comparable across the transport swap. The spike's measurement methodology matches this log's methodology (single clock, intra-request delta).

- **`schema_version` unchanged at 3:** Story 6.1 is a transport / capture change; nothing on the wire (DDS topics) changes. No `EventEnvelope.schema_version` bump.

### Source tree components to touch

| File | Action | Notes |
|---|---|---|
| `src/voice_agent_pipeline/tts/cartesia.py` | Modify | WS path + SSE fallback + `SegmentTiming` capture + `last_segment_timing()` accessor |
| `src/voice_agent_pipeline/tts/timing.py` | New (optional) | If `Word` + `SegmentTiming` outgrow inline placement in `cartesia.py`, split them here. Otherwise leave them in `cartesia.py` |
| `src/voice_agent_pipeline/tts/ttfb_spike.py` | New | CLI tool — runnable as `python -m voice_agent_pipeline.tts.ttfb_spike` |
| `src/voice_agent_pipeline/config/setup.py` | Modify | Add `transport` field on `TtsConfig` |
| `setup.toml` | Modify | Add `[tts] transport` line + comment |
| `justfile` | Modify | Add `ttfb-spike` recipe |
| `README.md` | Modify | One-line entry under "Common commands" |
| `build_documents/planning-artifacts/architecture.md` | Maybe modify | Only if drift surfaces; the existing footnote is already correct |
| `build_documents/planning-artifacts/decision-records.md` | Modify (lightly) | Append "Closed by:" line under DR-001's keystone-question section |
| `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md` | New (generated) | Committed alongside code, same as Story 5.5's `manifest.json` |
| `tests/unit/tts/test_cartesia.py` | Modify | New WS-path tests + transport-dispatch test + existing SSE tests stay |
| `tests/unit/config/test_setup.py` | Modify | New `transport` field coverage |

### Testing standards summary

- `just check` (ruff + ruff format + pyright + `pytest tests/unit -q`) must be green pre-commit.
- **Protocol-boundary mocking only (CLAUDE.md rule 7):** mock the `cartesia` SDK at its module boundary (`AsyncCartesia.tts.websocket_connect`, `AsyncCartesia.tts.generate_sse`). Do NOT mock internal `CartesiaClient` methods. The WS connection is an async context manager — the mock should return an `AsyncMock` configured with `__aenter__` returning a fake connection that supports `__aiter__()` over a scripted event list, plus `send()` as a no-op `AsyncMock`.
- WS-event fixtures live inline in the test file (not in a separate fixture file) — small scripted lists of `Chunk` / `Timestamps` / `Done` / `Error` instances. Use the actual SDK types (`cartesia.types.websocket_response.Chunk`, etc.) so type-checking catches drift if the SDK evolves.

### Project Structure Notes

- **No new top-level directory.** All files land in existing `src/voice_agent_pipeline/tts/` or `build_documents/implementation-artifacts/`. CLAUDE.md rule 2 (don't add new top-level dirs without updating architecture.md) is not triggered.

- **`tts/ttfb_spike.py` parallels `audio/regenerate.py`.** Both are CLI tools, both reuse the runtime client class (`CartesiaClient`), both write artefacts under `build_documents/`. Same module-docstring style as `audio/regenerate.py`.

- **The `last_segment_timing()` accessor is intentionally NOT on the `TTSClient` Protocol.** Story 6.3 will need it, but the cleanest evolution is to either (a) extend the Protocol at the Story 6.3 boundary, (b) access the concrete `CartesiaClient` via dependency injection. Story 6.1 stays scoped to the implementation — Protocol changes are Story 6.3's territory.

### Cartesia SDK 3.0.2 — verified shapes

(Captured from the installed SDK; do not re-derive at implementation time.)

- **WS entry:** `cartesia.AsyncCartesia.tts.websocket_connect(extra_query={}, extra_headers={}, websocket_connection_options={}) -> AsyncTTSResourceConnectionManager` — an async context manager.
- **`__aenter__`:** opens the websocket, returns an `AsyncTTSResourceConnection`.
- **`AsyncTTSResourceConnection`** methods:
  - `async def send(event: WebsocketClientEvent | WebsocketClientEventParam) -> None`
  - `async def recv() -> WebsocketResponse`
  - `__aiter__() -> AsyncIterator[WebsocketResponse]` — infinite iterator until `ConnectionClosedOK`
  - `async def close(code: int = 1000, reason: str = "") -> None`
- **`GenerationRequest`** fields (from `cartesia.types.generation_request`):
  - Required: `model_id` (note: aliased from `llm_model_id`; pass as `model_id=`), `output_format` (`OutputFormat`), `transcript` (str), `voice` (`VoiceSpecifier`).
  - Story 6.1 fields: `add_timestamps=True` (word-level), `add_phoneme_timestamps=False` (off).
  - Reuse `_OUTPUT_FORMAT` dict at `tts/cartesia.py:46`.
- **`WebsocketResponse`** union (from `cartesia.types.websocket_response`):
  - `Chunk` — `type="chunk"`, `data: str` (base64), `audio: bytes | None` property auto-decodes, `done: bool`, `step_time: float`, optional `context_id`, `flush_id`.
  - `Timestamps` — `type="timestamps"`, `word_timestamps: TimestampsWordTimestamps | None`. `TimestampsWordTimestamps` carries parallel lists `words: list[str]`, `start: list[float]` (seconds), `end: list[float]` (seconds). Round to ms; same-index entries belong together.
  - `PhonemeTimestamps` — `type="phoneme_timestamps"`. Not consumed in v1 WS path (off).
  - `Done` — `type="done"`. Use as break signal.
  - `FlushDone` — `type="flush_done"`. Not used in v1 (no flush command).
  - `Error` — `type="error"`, `error: str`. Raise `CartesiaError`.
- **`websocket_connect()` is NOT deprecated**; the older `websocket()` (line 695 in the SDK) is deprecated in favour of `websocket_connect()` — use the latter.

### Risks & mitigations

- **WS connection lingers across requests if `Done` is missed.** Mitigation: the async-context-manager pattern (`async with ... as conn:`) ensures `__aexit__` closes on every code path, including exceptions. Test: `[Chunk, Error]` mock should still close the connection (verify via `conn.close.assert_awaited()` style assertion if the mock tracks it).
- **Cartesia WS might rate-limit a 100-sample burst.** Mitigation: 50–100 ms sleep between requests in the spike. If rate-limiting hits, the spike report's `Error` count surfaces it and the operator can re-run with longer sleeps.
- **WS may be slower than SSE for short transcripts (one-shot overhead).** Mitigation: the spike measures actual TTFB across mixed transcript lengths so we see the real distribution. If the median is materially worse than SSE's baseline (DR-001 ~1.07 s p50), Story 6.2 stays on the cached opener path and we don't reshape toward DR-001 Option D.
- **`websockets.exceptions.ConnectionClosedError` subclass handling.** Mitigation: import the base class from `websockets.exceptions` and catch it; the SDK already exposes the connection's lifecycle, but a mid-stream Cartesia disconnect surfaces here. Wrap as `CartesiaError` consistently.
- **Test mocking complexity.** The async context-manager + async-iterator mock is fiddly. Use a small helper in the test file (`_make_ws_mock(events: list[WebsocketResponse])`) so each test is a one-liner. Don't fight the mock library — if it's painful, write a tiny `class _FakeConnection:` directly.
- **TTFB spike report format drift.** Mitigation: the report template is documented inline in `tts/ttfb_spike.py`; keep it simple Markdown with fixed-percentile fields so future runs are diff-able.

### References

- `build_documents/planning-artifacts/decision-records.md` §DR-001 — the keystone open question this spike resolves; §DR-002 — the head-motion design that consumes the captured timestamps via Story 6.3; §DR-004 — the wire-shape resolution (emphasis as the 7th vocalization tag) that Story 6.3 implements.
- `build_documents/planning-artifacts/epics.md` §Epic 6 — overall epic context; §Story 6.1 — the AC summary; §Story 6.2 + §Story 6.3 — the downstream consumers that depend on this story landing first.
- `build_documents/planning-artifacts/prd.md` FR15 (v2 WebSocket footnote), FR59 (Cartesia WS transport + timestamps capture), NFR4 (Cartesia TTFB ≤ 400 ms p95 — informs whether the WS measurement clears the same bar).
- `build_documents/planning-artifacts/architecture.md` v2 implementation-sequence item #20 (Cartesia SSE→WS + timestamps capture + TTFB spike) + the Cartesia API row in §"Architectural Boundaries".
- `src/voice_agent_pipeline/tts/cartesia.py` — the file to modify. The `:129` line (where `timestamps` events are currently dropped) is the focal point.
- `src/voice_agent_pipeline/tts/client.py` — the `TTSClient` Protocol (signature stays unchanged in Story 6.1).
- `src/voice_agent_pipeline/audio/regenerate.py` — the parallel CLI-tool pattern Story 6.1's `tts/ttfb_spike.py` should mirror.
- `src/voice_agent_pipeline/sequential_loop.py` — the runtime caller of `CartesiaClient.synthesize()`. Verify the call site still works post-swap (the integration test covers this, but a manual smoke is cheap).
- `src/voice_agent_pipeline/config/setup.py:340-377` — the `TtsConfig` class being extended.
- `setup.toml` — the operator-facing config; the `[tts]` block to extend.
- `.venv/lib/python3.12/site-packages/cartesia/resources/tts.py:835` — the `websocket_connect` method definition (verified SDK 3.0.2).
- `.venv/lib/python3.12/site-packages/cartesia/types/websocket_response.py` — the `WebsocketResponse` union types (`Chunk`, `Timestamps`, `Done`, `Error`, etc.).
- `.venv/lib/python3.12/site-packages/cartesia/types/generation_request.py` — the `GenerationRequest` event shape (the WS-side equivalent of `generate_sse`'s kwargs).
- Previous-story precedent: `build_documents/implementation-artifacts/5-5-cached-audio-deterministic-phrases.md` — the depth, ACs-and-tasks split, and Dev-Notes style this story file follows.

## Dev Agent Record

### Agent Model Used

Claude Opus 4.7 (1M context) — `claude-opus-4-7[1m]` (Anthropic).

### Debug Log References

- `tts.first_frame` log now carries `transport="websocket"` (or `"sse"`) so the production time-series can be sliced by transport during Story 6.4's soak.
- The spike emits `ttfb_spike.cold_sample` / `ttfb_spike.warm_sample` per request and `ttfb_spike.cold_phase_complete` / `ttfb_spike.warm_phase_complete` at phase boundaries. Run-time progress is monitorable via these events when `LOG_CONSOLE=true`.

### Completion Notes List

1. **Cartesia WS context_id bug discovered on first spike run.** The SDK accepts `GenerationRequest(...)` without `context_id` at the Python level (the field defaults to `None`), but the Cartesia server rejects it with HTTP 400 (`"The context_id field should only contain alphanumeric characters, underscores, and hyphens."` — the request_id surfaced a composite-id form like `586bc956-...:` with an empty trailing segment). Fix: pass `uuid.uuid4().hex` per call (no dashes, satisfies the validator). Documented inline in `tts/cartesia.py`.
2. **Cartesia WS `Error` event has a looser shape than the typed SDK model.** The SDK declares `error: str` (required) on `cartesia.types.websocket_response.Error`, but in practice a 400 validation error populates `error=None` and the actual reason lives in `message` and `title`. Story 6.1's WS error branch falls back through `event.error → event.message → event.title → status_code` so the operator sees the real reason, not "None".
3. **Test fixture refactor.** The existing v1 cartesia tests built fixtures targeting `tts.generate_sse`, which is now only used when `transport="sse"`. Story 6.1 added a parallel `sse_tts_config` fixture and pinned the legacy tests to `transport="sse"` so they continue to exercise the SSE branch. New WS-path tests run against the default `tts_config` (`transport="websocket"`).
4. **Spike scope upgraded mid-implementation per Kamal's request.** Initial spec called for ≥100 samples with p25/p50/p75/p90. After confirming the run would feed a research paper, the spike was expanded to 500 samples (250 cold + 250 warm), stratified by transcript-length bucket, with p95/p99 + mean+stdev added. The report template is correspondingly richer; the keystone call-out + SSE comparison band remain unchanged.
5. **Three pre-existing integration test failures in `tests/integration/test_simple_turn.py` are unchanged by Story 6.1.** Verified by running against `main` pre-change (`git stash` + `pytest` round-trip). All 546 unit tests pass; the 3 integration failures are out-of-scope debt from prior stories and predate this work.
6. **No top-level directory added** (CLAUDE.md rule 2). The Cartesia boundary stays concentrated: `tts/cartesia.py` (runtime) + `audio/regenerate.py` (offline) + the new `tts/ttfb_spike.py` (spike) are the only three Cartesia import sites.
7. **Schema_version unchanged at 3** (CLAUDE.md rule 6). Story 6.1 is internal transport + capture machinery; nothing on the wire (DDS topics, JSON envelope shape, event payloads) changes.

### File List

**Modified:**

- `README.md` — added `just ttfb-spike` to the "Common commands" block.
- `build_documents/implementation-artifacts/6-1-cartesia-websocket-and-ttfb-spike.md` — story file: Status, Tasks/Subtasks checkboxes, Dev Agent Record, File List.
- `build_documents/implementation-artifacts/sprint-status.yaml` — `6-1-cartesia-websocket-and-ttfb-spike` flipped `ready-for-dev → in-progress` (will move to `review` on completion).
- `build_documents/planning-artifacts/decision-records.md` — appended DR-001 "Open question (keystone)" closure paragraph referencing the spike report.
- `justfile` — added the `ttfb-spike` recipe.
- `setup.toml` — added the `[tts] transport` line + documentation comment.
- `src/voice_agent_pipeline/config/setup.py` — added `transport: Literal["websocket", "sse"] = "websocket"` to `TtsConfig`; expanded docstring.
- `src/voice_agent_pipeline/tts/cartesia.py` — Story-6.1 rewrite: `Word` + `SegmentTiming` pydantic models, `_synthesize_websocket` (with `context_id` fix, robust `Error` event handling), `_synthesize_sse` (lifted v1 SSE flow), `synthesize` dispatcher, `last_segment_timing()` accessor, `tts.first_frame` log now carries `transport`. `validate_credentials` unchanged.
- `tests/unit/config/test_setup.py` — added three `transport` field tests (default, accepts "sse", rejects unknown).
- `tests/unit/tts/test_cartesia.py` — rewrote with split WS / SSE fixture infrastructure; 24 tests covering WS happy / error event / connection closed / seconds→ms rounding / multi-Timestamps accumulation / reset between calls / first_frame log shape / GenerationRequest contract / transport dispatch + retained SSE tests pinned to `transport="sse"` + `Word` + `SegmentTiming` model invariants.

**New:**

- `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md` — generated by `just ttfb-spike` (500 samples, 0 errors, 2026-05-28). Headline percentiles + per-mode + per-length stratified tables + DR-001 keystone call-out + SSE comparison + raw samples.
- `src/voice_agent_pipeline/tts/ttfb_spike.py` — operator CLI for the TTFB spike (cold + warm phases, per-length stratification, Markdown report).

## Change Log

| Date | Author | Change |
|---|---|---|
| 2026-05-28 | Amelia (dev-story / Claude Opus 4.7) | Story 6.1 implemented: Cartesia WebSocket transport with per-word timestamps capture, SSE retained as opt-in fallback, 500-sample TTFB spike resolves DR-001 keystone. Headline: cold p50 = 467 ms, warm p50 = 230 ms, overall p50 = 383 ms (vs. SSE baseline ~1070 ms). 0 errors over 500 samples. |
