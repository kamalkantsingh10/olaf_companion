---
stepsCompleted:
  - step-01-validate-prerequisites
  - step-02-design-epics
  - step-03-create-stories-epic-1
  - step-03-create-stories-epic-2
  - step-03-create-stories-epic-3
  - step-03-create-stories-epic-4
  - step-03-create-stories-epic-5
  - step-04-final-validation
inputDocuments:
  - build_documents/planning-artifacts/prd.md
  - build_documents/planning-artifacts/architecture.md
scope: v1-active-set-only
deferredToV2:
  - FR7, FR41 (Hailo-8L acceleration + driver verification — Pi port)
  - FR13, FR16 (orchestrator stall filler + Cartesia text-only degraded mode — resilience layer)
  - NFR9, NFR19, NFR20, NFR22 (recovery, retry/backoff, stall heartbeat, Talker→orchestrator failover — resilience layer)
  - NFR14, NFR15, NFR16, NFR17, NFR18 (Pi 5 resource calibration — Pi port)
approachGuidance: |
  Per Kamal: go super simple 1st and progressively add complexity in each sprint.
  Start with the leanest possible epic that proves the core flow end-to-end,
  then layer complexity sprint by sprint.
---

# olaf_companion — voice-agent-pipeline — Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for the **voice-agent-pipeline** component of `olaf_companion`, decomposing the requirements from the PRD and Architecture documents into implementable stories.

**Scope:** v1 active set only. v2-deferred FRs/NFRs (resilience layer, Pi/Hailo port) are tracked in frontmatter for traceability but produce no stories in this document.

**Approach:** lean-first, then progressive complexity. Each sprint adds one new capability layer on top of a runnable artifact from the prior sprint.

## Requirements Inventory

### Functional Requirements

> v1 active set: 39 FRs. v2-deferred FRs (FR7, FR13, FR16, FR41) are intentionally omitted.

**Audio I/O & Capture**

- **FR1**: The pipeline can detect a configurable wake-word from continuous mic input without dispatching downstream processing prior to detection.
- **FR2**: The pipeline can capture user speech from the local mic device after wake-word detection, terminating capture on voice-activity end-of-speech.
- **FR3**: The pipeline can play synthesized audio through the local speaker device with no perceivable buffering pause between frames.
- **FR4**: The pipeline can pin audio devices by stable name in configuration, surviving reboots and USB hot-plug events of unrelated devices.
- **FR5**: The pipeline can detect mid-utterance barge-in (user speaking during SPEAKING lifecycle state) and abort current playback.

**Speech Recognition**

- **FR6**: The pipeline can transcribe user speech to text on-device, without transmitting audio to a cloud service.
- **FR8**: The pipeline can attach a confidence score to each transcript and route low-confidence transcripts to a clarification path.

**Conversational Intelligence**

- **FR9**: The pipeline can route a transcribed user turn to the Talker fast-path or to the orchestrator daemon based on a configurable routing decision.
- **FR10**: The pipeline can read belief state from the orchestrator daemon via HTTP API to inform Talker responses.
- **FR11**: The pipeline can dispatch a user turn to the orchestrator daemon via `POST /turn` and consume the typed event stream (narration, subagent events, response chunks, turn_end).
- **FR12**: The pipeline can synthesize a fast-path response from belief state using an in-pipeline LLM (Talker), emitting Cartesia-tagged text.
- **FR14**: The pipeline can recover gracefully from a missing `turn_end` event by flushing the splitter and transitioning lifecycle after the last audio frame plays.

**Voice Synthesis**

- **FR15**: The pipeline can stream Cartesia-tagged text to Cartesia Sonic-3 and receive audio frames in response.
- **FR17**: The pipeline can use a configurable Cartesia voice ID and default emotion.

**Embodiment Expression**

- **FR18**: The pipeline can parse incoming text streams as Cartesia SSML, identifying `<emotion value="X"/>` tags and `[burst]` events incrementally (token-by-token, tags may split across token boundaries).
- **FR19**: The pipeline can segment text on whichever boundary comes first: sentence terminator, emotion tag, or burst tag.
- **FR20**: The pipeline can map every Cartesia emotion tag to a defined `ExpressionEvent` via the `expression_map.yaml` mapping table, with no silent gaps.
- **FR21**: The pipeline can resolve unmapped emotion tags through a fallback family table, producing a defined `ExpressionEvent` with a logged warning.
- **FR22**: The pipeline can attach `ExpressionEvent` metadata to the matching Cartesia audio frame, ensuring expression events publish in lockstep with audio.
- **FR23**: The pipeline can publish `ExpressionEvent` to the configured expression broadcast channel, anchored to audio frame send time, achieving 30-80ms anticipatory alignment with voice.
- **FR24**: The pipeline can suppress republishing of unchanged base emotions via a "last published" cache, while always publishing burst events.
- **FR25**: The pipeline can strip Cartesia-unsupported burst tags from the TTS stream while still publishing them as `ExpressionEvent` to the broadcast channel.

**Lifecycle State Management**

- **FR26**: The pipeline can publish `LifecycleEvent` (SLEEPING, LISTENING, THINKING, SPEAKING, IDLE) to the configured lifecycle broadcast channel at conversation milestones.
- **FR27**: The pipeline can transition between lifecycle states based on observable events: wake-word detection, end-of-speech, first audio frame, last audio frame, idle timeout.
- **FR28**: The pipeline can transition from IDLE to SLEEPING after a configurable idle timeout (default: 5 minutes).
- **FR29**: The pipeline can transition from SPEAKING to LISTENING directly on barge-in detection, bypassing THINKING.
- **FR30**: The pipeline can flush in-flight expression events on barge-in to prevent the consumer being stuck on a half-finished pose.

**Configuration & Operations**

- **FR31**: The pipeline can load `expression_map.yaml` and `setup.toml` at startup, validating schema and refusing to start on validation failure.
- **FR32**: The pipeline can hot-reload `expression_map.yaml` on `SIGHUP`, swapping the in-memory mapping atomically; if validation fails, it retains the prior mapping and logs the error.
- **FR33**: The pipeline can defer `SIGHUP` reloads received mid-utterance, applying them after the current turn completes.
- **FR34**: The pipeline can load credentials (Cartesia, Anthropic, Picovoice API keys) from a `.env` file referenced by path, never inlined and never logged.
- **FR35**: The pipeline can refuse to start when configured with a non-localhost orchestrator URL without a corresponding shared-secret or mTLS configuration.
- **FR36**: The pipeline can run as a systemd service with restart-on-failure and structured logging.

**Observability & Diagnostics**

- **FR37**: The pipeline can emit structured (JSON) logs at INFO/WARN/ERROR levels for lifecycle transitions, emotion fallback resolutions, config reloads, and external service failures.
- **FR38**: The pipeline can log unmapped Cartesia emotion tags with the mapped fallback (DEBUG-level on first occurrence, WARN if completely unknown).
- **FR39**: The pipeline can omit raw audio from all logs at all levels; transcripts only appear at DEBUG level, which is off by default.
- **FR40**: The pipeline can rotate logs locally with a configurable retention window (default: 7 days).
- **FR42**: The pipeline does not persist user audio or transcripts to disk in the default operational path.
- **FR43**: The pipeline does not initiate any outbound network connection beyond the configured Cartesia API, Anthropic API, Picovoice (offline runtime), and orchestrator daemon endpoints (no telemetry, no analytics).

### NonFunctional Requirements

> v1 active set: 20 NFRs. v2-deferred NFRs (NFR9, NFR14–NFR20, NFR22) are intentionally omitted.

**Performance**

- **NFR1**: Simple-turn end-to-end latency (end-of-speech → first audio frame from Cartesia) must be ≤ 1500ms at p95 over a 30-min soak.
- **NFR2**: Complex-turn end-to-end latency (end-of-speech → first narration audio frame) must be ≤ 1000ms at p95.
- **NFR3**: On-device STT latency (end-of-speech → transcript ready) must be ≤ 500ms at p95. v1 measures on host CPU/GPU (faster-whisper); Hailo-accelerated p95 is a v2 target.
- **NFR4**: Cartesia TTS latency (text-with-tags → first audio frame) must be ≤ 400ms at p95.
- **NFR5**: Voice/embodiment alignment must be 30–80ms anticipatory at p95; outside this window is a defect.
- **NFR6**: Audio playback must not introduce buffering pauses > 100ms during a single utterance.
- **NFR7**: `SIGHUP`-triggered config reload must complete within 1 second from signal receipt.

**Reliability**

- **NFR8**: Pipeline must run continuously for ≥ 7 days under normal household ambient conditions without an unplanned restart, panic, or unrecoverable error state.
- **NFR10**: A malformed config file at startup must produce a clear error and prevent startup; a malformed config on `SIGHUP` must produce a clear error and retain the prior config (no silent-broken state).
- **NFR11**: Pipeline must survive USB hot-plug events on unrelated devices without restart or audio interruption.
- **NFR12**: Wake-word false-positive rate must be ≤ 1 per hour of typical household ambient (TV, conversation, kitchen sounds) at the production threshold.
- **NFR13**: Wake-word false-negative rate must be ≤ 5% in normal speaking conditions at the production threshold.

**Integration Reliability**

- **NFR21**: Broadcast publishing on the configured expression and lifecycle channels must use reliable delivery (RELIABLE QoS for ROS 2 / DDS in v1).

**Security**

- **NFR23**: All API credentials must be stored at file permission `0600` and loaded from disk only at process startup; the process must not re-read or expose them at runtime.
- **NFR24**: Outbound HTTPS connections (Cartesia, Anthropic) must validate TLS certificates; the pipeline must refuse to start if certificate validation is disabled.
- **NFR25**: All log output must be inspectable by Kamal locally; no log line may contain raw credential material, raw audio bytes, or (at INFO level or above) user transcripts.

**Maintainability**

- **NFR26**: PRD, brief, distillate, and architecture are the canonical specs. Any implementation decision deviating from them must update the relevant document in the same change.
- **NFR27**: Configuration schemas (`expression_map.yaml`, `setup.toml`) and event schemas (`ExpressionEvent`, `LifecycleEvent`) must be versioned with an integer `schema_version` field; the pipeline must reject incompatible versions at startup.
- **NFR28**: Components within the pipeline (wake-word, STT, Talker, splitter, TTS, publisher) must be independently testable — each can be exercised in isolation with mock or synthetic inputs at its Protocol seam.
- **NFR29**: Logs must be machine-readable JSON to enable post-hoc analysis without manual parsing.

### Additional Requirements

> Sourced from `architecture.md`. These are technical/infrastructure requirements implied by the architectural decisions that don't appear as numbered FRs/NFRs in the PRD but must produce stories or be embedded into Story 1.1.

**Project bootstrap (Architecture §"Selected Starter" + §"First Implementation Priority"):**

- Initialize project via `uv init voice-agent-pipeline --python 3.12` with the documented dependency set: `pipecat-ai[local]`, `anthropic`, `cartesia`, `httpx`, `httpx-sse`, `pvporcupine`, `faster-whisper`, `pydantic`, `pydantic-settings`, `structlog`, dev: `ruff`, `pyright`, `pytest`, `pytest-asyncio`.
- `rclpy` is installed via system ROS 2 distro (e.g., `ros-jazzy-rclpy`), exposed to the venv via PYTHONPATH; documented in README.
- Project layout follows the documented module-by-domain tree (`src/voice_agent_pipeline/{audio,stt,turn,tts,splitter,publisher,lifecycle,config,logging,schemas}` + `tests/{unit,integration,contract}`).
- Committed root files: `pyproject.toml`, `uv.lock`, `justfile`, `setup.toml`, `expression_map.yaml`, `.env.example`, `.gitignore`, `README.md`, `CLAUDE.md`, `.python-version`.
- Gitignored: `.env`, `./logs/`, `.venv/`.
- `justfile` recipes: `run`, `check`, `test`, `reload`, `lint`, `format`.

**Type/style discipline (Architecture §"Implementation Patterns"):**

- `snake_case` everywhere keys are written (Python, TOML, YAML, JSON payload, DDS field names, log keys).
- `typing.Protocol` for interfaces (no `abc.ABC`); pydantic v2 `BaseModel` for events/config/data; `typing.Literal` for fixed string sets (no `enum.Enum`); `@dataclass(frozen=True)` only for internal trivial structs.
- `pyright` strict for `src/`, basic for `tests/`. No `Any` outside the documented `payload: dict[str, Any]` extensibility seam.
- Custom exception hierarchy in `errors.py` (`VoiceAgentError` root → `ConfigError`, `SchemaVersionError`, `StartupValidationError`, `ExternalServiceError` + subclasses, `PublisherError`, `SplitterError`).
- `just check` runs ruff + pyright + `pytest tests/unit`; AI partner runs this pre-commit; failures block.

**Wake-word asset (Architecture §"Audio + STT Pipeline"):**

- Custom Picovoice Porcupine wake-word phrase trained via Picovoice console; `.ppn` file committed to `models/wakeword/hey_olaf.ppn`.
- `PICOVOICE_ACCESS_KEY` added to `.env.example` and validated at startup alongside `CARTESIA_API_KEY` and `ANTHROPIC_API_KEY`.

**v1 fail-fast posture (Architecture §"V1 Posture"):**

- Startup validates: Cartesia API key + reachable, Anthropic API key + reachable, Picovoice access key valid, orchestrator daemon reachable + `GET /health` 200, broadcast bus connection established (expression + lifecycle channels), audio devices resolvable by name.
- Any failure → process refuses to start with a clear error.
- At runtime, external-service failures crash the process; systemd restarts. **No retry, no in-process recovery, no partial-mode fallbacks in v1.**

**Architectural seams (Architecture §"Internal seams"):**

- Six Protocol seams must be defined: `STTBackend`, `TalkerClient`, `OrchestratorClient`, `BeliefStateClient`, `TTSClient`, `ExpressionPublisher`.
- Each external library is imported in **exactly one file** (boundary concentration rule).
- v1 ships **one** `ExpressionPublisher` implementation: `Ros2ExpressionPublisher`, using `std_msgs/String + JSON`-encoded payload (no custom `.msg` package in v1).

**Event schemas (Architecture §"Publisher Contract + Event Schemas"):**

- `ExpressionEvent` (frozen pydantic v2): `schema_version`, `event_type="expression"`, `emotion`, `source_tag`, `audio_frame_id`, `timestamp_ns`, open `payload: dict[str, Any]`.
- `LifecycleEvent` (frozen pydantic v2): `schema_version`, `event_type="lifecycle"`, `state` (Literal of 5 states), `timestamp_ns`, open `payload`.
- DDS wire format: `std_msgs/String` with the entire event JSON-encoded.

**External clients (Architecture §"External Clients"):**

- HTTP: `httpx.AsyncClient` per service, persistent, lifecycle-bound to pipeline startup/shutdown.
- Orchestrator stream transport: SSE via `httpx-sse`. Barge-in cancellation via `HTTP DELETE /turn/{session_id}`.
- Belief-state read: per-turn fresh `GET /beliefs?keys=...`, no cache.
- SSE event dispatch: by `type` field; unknown types → log WARN + ignore (forward-compat); framing/JSON errors → raise → crash.

**Logging (Architecture §"Logging — mature, project-rooted, file-first strategy"):**

- structlog → stdlib `logging` → `RotatingFileHandler`. Three streams: `voice-agent.log` (INFO+), `errors.log` (WARN+), `debug.log` (DEBUG, opt-in via `LOG_LEVEL=DEBUG`).
- Logs in `./logs/` at project root (not journald, not `/var/log`).
- Rotation: size-based (default 50MB/file), retention 7 days, configurable in `setup.toml`.
- Redaction processor: structlog denylist before JSON serializer. Drops `audio_bytes`, `audio_data`, `pcm`; matches `*api_key`, `*token`, `*password`, `*secret`. Transcripts (`transcript`, `user_text`) only at DEBUG level.
- Console mirror via `LOG_CONSOLE=true` env var; off in production.
- Mandatory `event` field in `verb.subject` form on every log line.
- Per-turn context bound via `bind_contextvars(session_id=..., audio_frame_id=...)`.

**Test infrastructure (Architecture §"Test Patterns"):**

- `tests/unit/` mirrors `src/` exactly; one behavior per test; mock only at Protocol boundaries.
- `tests/integration/` runs the full Pipecat pipeline with Protocol mocks for external services; covers all 5 PRD journeys.
- `tests/contract/` verifies pydantic ↔ JSON ↔ DDS round-trip stability and `schema_version` rejection.
- `pytest-asyncio` for async tests; `conftest.py` for shared fixtures.

**Deployment (Architecture §"Operations: systemd"):**

- systemd unit at `deploy/systemd/voice-agent-pipeline.service`: `Type=simple`, `Restart=on-failure`, `RestartSec=5`, `StartLimitInterval=60`, `StartLimitBurst=5`, `WorkingDirectory` pinned, `User=<dev>`.
- App reads `.env` directly via pydantic-settings; systemd does not handle credentials.
- App logs to `./logs/`; only systemd lifecycle messages (start/stop/crash) hit journald.

**Cross-project coordination (Architecture §"Cross-project integration"):**

- Orchestrator daemon must expose `GET /health` returning 200. Tracked on the spec-drift list (separate from this epic plan but Story 1.x must call out that startup validation depends on this contract).

### UX Design Requirements

_None — no UX Design document exists for this component. The voice-agent-pipeline has no GUI surface; "UX" lives in the audio interaction model (latency targets, wake-word reliability, voice/embodiment alignment), which is captured by NFR1–NFR5, NFR12, NFR13 above._

### FR Coverage Map

> Each row maps an FR to the epic(s) that introduce or extend it. Where an FR appears in multiple epics, the entry shows the progressive enrichment.

| FR | Epic(s) | Notes |
|---|---|---|
| FR1 (wake-word) | Epic 1 | Picovoice Porcupine + custom `.ppn` |
| FR2 (capture on VAD end-of-speech) | Epic 1 | Silero VAD + LocalAudioTransport input |
| FR3 (audio playback, no buffering pause) | Epic 2 | First time speaker output is wired |
| FR4 (audio devices by stable name) | Epic 1 (mic) → Epic 2 (speaker) | `resolve_audio_devices()` name-regex pattern established Epic 1, extended Epic 2 |
| FR5 (barge-in detection) | Epic 5 | VAD-during-SPEAKING + sustained-voice threshold |
| FR6 (on-device STT) | Epic 1 | `WhisperBackend` (faster-whisper, CPU/GPU as available) |
| FR8 (transcript confidence + clarification routing) | Epic 1 | Low-confidence escape hatch |
| FR9 (Talker vs orchestrator routing) | Epic 2 (Talker-only) → Epic 4 (full fast/slow) | TurnRouter routes everything to Talker until Epic 4 wires orchestrator |
| FR10 (belief-state read) | Epic 4 | `BeliefStateClient` per-turn `GET /beliefs?keys=...`, no cache |
| FR11 (orchestrator dispatch + SSE consume) | Epic 4 | `OrchestratorClient` httpx + httpx-sse |
| FR12 (Talker fast-path emits Cartesia-tagged text) | Epic 2 (basic Talker, no belief, no SSML) → Epic 3 (Talker prompt updated to emit SSML) → Epic 4 (belief-state grounding) | Progressive enrichment of the same FR |
| FR14 (missing `turn_end` recovery) | Epic 4 | Splitter flush + lifecycle transition after last frame |
| FR15 (Cartesia streaming) | Epic 2 | `CartesiaClient` |
| FR17 (configurable voice ID + default emotion) | Epic 2 | `setup.toml` `[tts]` |
| FR18 (streaming SSML parser, token-by-token) | Epic 3 | Hand-rolled state machine ~50–100 LOC |
| FR19 (segment on sentence/emotion/burst boundary) | Epic 3 | Segmenter |
| FR20 (every Cartesia tag mapped, no silent gaps) | Epic 3 | `expression_map.yaml` full primary + secondary + family fallback |
| FR21 (unmapped → fallback family) | Epic 3 | Resolver + WARN log |
| FR22 (attach `ExpressionEvent` to audio frame) | Epic 3 | Extend `AudioRawFrame` metadata |
| FR23 (publish on configured expression channel) | Epic 3 | `Ros2ExpressionPublisher` (std_msgs/String + JSON) |
| FR24 (last-published cache, dedup base emotions) | Epic 3 | Burst events always publish |
| FR25 (strip burst from TTS, publish to bus) | Epic 3 | Splitter responsibility |
| FR26 (publish `LifecycleEvent` on lifecycle channel) | Epic 4 | First time lifecycle is broadcast |
| FR27 (lifecycle transitions on observable events) | Epic 4 | State machine |
| FR28 (IDLE → SLEEPING after timeout) | Epic 4 | Configurable, default 5min |
| FR29 (SPEAKING → LISTENING on barge-in, bypass THINKING) | Epic 5 | New transition path |
| FR30 (flush in-flight expression events on barge-in) | Epic 5 | Splitter + DELETE `/turn/{id}` |
| FR31 (config schema validation, refuse-to-start on bad) | Epic 1 (`setup.toml` + `.env`) → Epic 3 (`expression_map.yaml` validation) | Pattern established Epic 1 |
| FR32 (SIGHUP atomic swap of `expression_map.yaml`) | Epic 5 | Atomic in-memory swap, rollback on validation fail |
| FR33 (defer mid-utterance reload) | Epic 5 | Pair with FR32 |
| FR34 (load creds from `.env`, never inlined or logged) | Epic 1 (Picovoice) → Epic 2 (Anthropic + Cartesia) | Each epic adds the keys it needs |
| FR35 (refuse non-localhost orchestrator without secret) | Epic 5 | Startup validation rule |
| FR36 (systemd service, restart-on-failure) | Epic 5 | `deploy/systemd/voice-agent-pipeline.service` |
| FR37 (structured JSON logs at INFO/WARN/ERROR) | Epic 1 | Pattern established; events grow per epic |
| FR38 (log unmapped tags w/ fallback) | Epic 3 | DEBUG on first occurrence, WARN if completely unknown |
| FR39 (no raw audio in logs, transcripts DEBUG-only) | Epic 1 | Redaction processor + level discipline from day 1 |
| FR40 (log rotation, configurable retention) | Epic 5 | RotatingFileHandler config |
| FR42 (no audio/transcript persistence) | Epic 1 | Architectural property, true from day 1 |
| FR43 (no telemetry beyond configured deps) | Epic 1 | Architectural property, true from day 1 |

**Coverage check:** all 39 v1-active FRs mapped. v2-deferred FRs (FR7, FR13, FR16, FR41) intentionally absent.

## Epic List

### Epic 1: Listen — wake-word + on-device STT

**Goal:** Kamal speaks the wake phrase and OLAF transcribes the utterance to a structured log entry. Half-loop validation: prove the listening side works in isolation before wiring response generation.

**User outcome:** Run `just run`, say "Hey OLAF, what time is it?" — within ~500ms a transcript appears in `./logs/voice-agent.log`. No response yet, but Kamal can verify wake-word reliability and STT latency on real ambient audio.

**Foundation built here (used by all later epics):**

- Project bootstrap: `uv init voice-agent-pipeline --python 3.12`, full dependency set, `justfile` (`run`, `check`, `test`, `lint`, `format`), module-by-domain layout under `src/voice_agent_pipeline/`, `CLAUDE.md`, `.env.example`, `.gitignore`, `pyproject.toml` with ruff + pyright config.
- Config: `pydantic-settings` models for `setup.toml` + `.env`, schema validation, refuse-to-start on bad config. `PICOVOICE_ACCESS_KEY` validated at startup; Anthropic/Cartesia keys land Epic 2.
- Logging: structlog → stdlib logging → `RotatingFileHandler` in `./logs/`, three streams (`voice-agent.log`, `errors.log`, `debug.log`), redaction processor, mandatory `event` field, JSON-only output, transcripts DEBUG-gated.
- Errors hierarchy in `errors.py` (root + Config/Schema/Startup/External/Publisher/Splitter subclasses).
- Event schemas defined: `ExpressionEvent`, `LifecycleEvent` (frozen pydantic v2 with `schema_version` + open `payload`).
- All 6 Protocol seams declared in their packages (`STTBackend`, `TalkerClient`, `OrchestratorClient`, `BeliefStateClient`, `TTSClient`, `ExpressionPublisher`); only `STTBackend` has a real impl in this epic.
- Audio capture path: `LocalAudioTransport` (input), `resolve_audio_devices(config)` name→index resolver (refuse-to-start if no match).
- Wake-word: `pvporcupine` async-wrapped via `asyncio.to_thread`; custom `.ppn` committed to `models/wakeword/hey_olaf.ppn`.
- VAD: Silero (Pipecat-bundled).
- STT: `WhisperBackend` using faster-whisper, async-wrapped, returning transcript + confidence.

**FRs:** FR1, FR2, FR4 (mic side), FR6, FR8, FR31 (`setup.toml` + `.env`), FR34 (Picovoice key), FR37, FR39, FR42, FR43
**NFRs primarily proven:** NFR3 (STT p95), NFR12, NFR13 (wake-word accuracy baseline), NFR23 (0600 perms), NFR25 (no creds in logs), NFR28 (Protocol seams), NFR29 (JSON logs)

---

### Epic 2: Speak — Talker + Cartesia

**Goal:** Complete the simple voice loop. After wake-word + STT, route the transcript to Anthropic Talker, stream the response through Cartesia TTS, and play it through the speaker.

**User outcome:** Kamal says "Hey OLAF, what time is it?" — within ~1.5s OLAF responds in voice. No emotion in the response yet (no SSML splitter, no embodiment broadcast). No complex questions (no orchestrator). But the full simple-turn loop works end-to-end and hits NFR1.

**What's built:**

- Audio playback path: `LocalAudioTransport` output, speaker device pinning extends FR4.
- `TalkerClient` impl using `anthropic.AsyncAnthropic`. Plain prompt — no SSML tags yet, no belief-state grounding.
- `TTSClient` Protocol + `CartesiaClient` impl using `cartesia` SDK streaming.
- `TurnRouter` minimal: routes every transcript to Talker (orchestrator path stub).
- Startup validation extended: Anthropic + Cartesia API keys validated at startup; refuse-to-start if missing/unreachable.
- Pipeline assembly in `pipeline.py` wires the full simple-turn pipeline.

**FRs:** FR3, FR9 (Talker-only routing), FR12 (basic Talker, no belief, no SSML), FR15, FR17, FR34 (extended with Anthropic + Cartesia keys)
**NFRs primarily proven:** NFR1 (simple-turn ≤1500ms p95), NFR4 (Cartesia TTS p95), NFR6 (no buffering pause), NFR24 (TLS strict)

---

### Epic 3: Embodiment Channel — emotion in lockstep with voice

**Goal:** Talker emits Cartesia SSML tags. The streaming splitter parses them, segments output on boundaries, attaches `ExpressionEvent` metadata to the matching audio frame, and `Ros2ExpressionPublisher` broadcasts the events with 30–80ms anticipatory alignment. Full Cartesia tag → expression mapping with no silent gaps.

**User outcome:** OLAF's voice now carries emotion. With any embodiment subscriber on the bus (or a stdout test consumer), expression events arrive in lockstep with audio. Adding a new emotion is a YAML edit (no code touchpoints — the architectural extensibility test).

**What's built:**

- Streaming SSML state machine (`splitter/state_machine.py`, ~50–100 LOC, hand-rolled, zero-dep).
- Segmenter: boundary-based emission (sentence terminator / emotion tag / burst tag).
- Mapping resolver: full primary (6) + secondary (6) + fallback family table covering all 60+ Cartesia tags + `unknown → neutral`.
- Last-published cache for base-emotion dedup; bursts always publish.
- Burst stripping: removed from TTS stream, still emitted as `ExpressionEvent`.
- Audio-frame metadata threading: extend Pipecat's `AudioRawFrame` with optional `expression_event` metadata; transport reads on send and calls `ExpressionPublisher.publish_expression()`.
- `ExpressionPublisher` Protocol implementation: `Ros2ExpressionPublisher` using `std_msgs/String` + JSON-encoded payload, RELIABLE QoS.
- `expression_map.yaml` ships with full mapping; loaded at startup, schema-validated (extends FR31).
- Talker prompt updated to emit `<emotion value="..."/>` SSML tags.
- Startup validation extended: broadcast bus connection (expression channel) established or refuse-to-start.

**FRs:** FR18, FR19, FR20, FR21, FR22, FR23, FR24, FR25, FR31 (extended with `expression_map.yaml`), FR38, FR12 (extended — Talker now emits SSML)
**NFRs primarily proven:** NFR5 (30–80ms anticipatory alignment), NFR21 (RELIABLE QoS)

---

### Epic 4: Complex Questions & Lifecycle

**Goal:** TurnRouter routes complex intents to the orchestrator via SSE; narration plays first, subagent runs, response chunks stream. Belief-state lookup grounds Talker fast-path. LifecycleEvents (SLEEPING/LISTENING/THINKING/SPEAKING/IDLE) publish so subscribers know what OLAF is doing. Missing-`turn_end` cleanup keeps the splitter sane.

**User outcome:** Kamal asks "what's on my calendar today?" — OLAF says "let me check…" within ~1s, runs the comms subagent via the orchestrator, then narrates the result. Lifecycle channel emits state transitions in real time.

**What's built:**

- `OrchestratorClient` (httpx + httpx-sse): `POST /turn` returns SSE; per-turn `httpx.AsyncClient` lifecycle. Dispatch by `type` field; unknown types → log WARN + ignore (forward-compat).
- `BeliefStateClient` (httpx): per-turn fresh `GET /beliefs?keys=...`, no cache.
- TurnRouter routing rule (config-driven keyword/regex from `setup.toml`).
- Talker fast-path uses belief state for grounding (extends FR12).
- Missing-`turn_end` recovery: flush splitter, transition lifecycle after last audio frame.
- Lifecycle state machine in `lifecycle/machine.py`: `Literal["SLEEPING","LISTENING","THINKING","SPEAKING","IDLE"]` transitions on observable events; `publish_lifecycle()` calls.
- Idle → Sleeping after configurable idle timeout (default 5min).
- Startup validation extended: orchestrator daemon reachable + `GET /health` 200 (spec-drift item — orchestrator project must expose `/health`).
- Startup validation extended: lifecycle broadcast channel established.

**FRs:** FR9 (full fast/slow), FR10, FR11, FR12 (extended with belief state), FR14, FR26, FR27, FR28
**NFRs primarily proven:** NFR2 (complex-turn ≤1000ms p95)
**Coordination point:** orchestrator project must expose `GET /health`. Story will surface this on the spec-drift list.

---

### Epic 5: Production Hardening

**Goal:** Make OLAF interruptible, hot-tunable, and durable. Barge-in halts playback and flushes; SIGHUP swaps `expression_map.yaml` atomically with mid-utterance defer; systemd manages the service; logs rotate; LAN orchestrator without a shared secret refuses to start; 7-day soak validates wake-word thresholds against real household ambient.

**User outcome:** OLAF is now a service Kamal can leave running. Mid-response interruption works ("Wait, actually—"). Tweaking `excited` pose values is a YAML edit + `kill -HUP` away. The pipeline survives a week of daily use without manual restart.

**What's built:**

- Barge-in detection: VAD-during-SPEAKING with sustained-voice threshold to avoid false-fires from OLAF's own audio bleed.
- SPEAKING → LISTENING bypass on barge-in (no THINKING).
- In-flight expression event flush + `HTTP DELETE /turn/{session_id}` for orchestrator cancellation.
- SIGHUP handler in `__main__.py` → `expression_map` atomic swap; rollback on validation failure with line-number error.
- Mid-utterance reload defer: SIGHUP during turn queues until current turn ends.
- LAN orchestrator + shared-secret/mTLS validation rule at startup (refuse-to-start with clear error).
- systemd unit at `deploy/systemd/voice-agent-pipeline.service`: `Type=simple`, `Restart=on-failure`, `RestartSec=5`, `StartLimitInterval=60`, `StartLimitBurst=5`, `WorkingDirectory` pinned, app reads `.env` directly via pydantic-settings.
- Log rotation: size-based (default 50MB/file), retention 7 days default, configurable in `setup.toml`.
- Schema versioning enforcement: refuse to load configs/parse events with unsupported `schema_version`.
- 7-day soak under real household ambient; tune wake-word threshold to NFR12 (≤1 FP/hour) and NFR13 (≤5% FN).

**FRs:** FR5, FR29, FR30, FR32, FR33, FR35, FR36, FR40
**NFRs primarily proven:** NFR7 (SIGHUP <1s), NFR8 (7-day soak), NFR10 (mal config rollback), NFR11 (USB hot-plug survival), NFR12 (final FP threshold), NFR13 (final FN threshold), NFR27 (schema versioning enforcement)

---

## Epic 1: Listen — wake-word + on-device STT

**Goal:** Kamal speaks the wake phrase and OLAF transcribes the utterance to a structured log entry. Half-loop validation: prove the listening side works in isolation before wiring response generation.

### Story 1.1: Project bootstrap & toolchain

As Kamal (the dev),
I want a fresh `voice-agent-pipeline` repo initialized with the agreed toolchain,
So that subsequent stories drop code into a working project skeleton with `just check` passing.

**Acceptance Criteria:**

**Given** the architecture's "Selected Starter" section,
**When** the project is initialized via `uv init voice-agent-pipeline --python 3.12` and dependencies added,
**Then** `pyproject.toml` lists `pipecat-ai[local]`, `anthropic`, `cartesia`, `httpx`, `httpx-sse`, `pvporcupine`, `faster-whisper`, `pydantic`, `pydantic-settings`, `structlog`, plus dev deps `ruff`, `pyright`, `pytest`, `pytest-asyncio`.

**Given** the module-by-domain layout decision,
**When** I inspect `src/`,
**Then** I see `voice_agent_pipeline/{audio,stt,turn,tts,splitter,publisher,lifecycle,config,logging,schemas}/__init__.py` plus `__main__.py`, `pipeline.py`, `errors.py` at the package root.

**Given** the test layout decision,
**When** I inspect `tests/`,
**Then** I see `unit/`, `integration/`, `contract/` subdirectories mirroring `src/` and a top-level `conftest.py`.

**Given** a `justfile` at the project root,
**When** I run `just check`,
**Then** it runs `ruff check`, `ruff format --check`, `pyright`, and `pytest tests/unit -q` in sequence and exits 0 on a clean repo.

**Given** root files committed to git,
**When** I list the repo,
**Then** I see `pyproject.toml`, `uv.lock`, `justfile`, `setup.toml` (placeholder), `expression_map.yaml` (placeholder), `.env.example`, `.gitignore` (with `.env`, `logs/`, `.venv/`, `__pycache__/`), `README.md`, `CLAUDE.md`, `.python-version`.

**Given** `uv run python -m voice_agent_pipeline`,
**When** I execute it without further wiring,
**Then** it exits cleanly with a "not yet implemented" placeholder message and exit code 0.

**Given** pyright strict for `src/` and basic for `tests/` (configured in `pyproject.toml`),
**When** `just check` runs,
**Then** zero pyright errors are reported.

**Given** `CLAUDE.md` content,
**When** I open it,
**Then** the 9 enforcement rules from architecture §"Enforcement Guidelines" are captured in terse form for the AI partner.

---

### Story 1.2: Config loaders (`setup.toml` + `.env`) with schema validation

As Kamal,
I want `setup.toml` and `.env` loaded via pydantic-settings with schema validation that refuses to start on bad config,
So that misconfiguration fails loudly at startup instead of silently at runtime.

**Acceptance Criteria:**

**Given** a valid `setup.toml` and `.env`,
**When** the pipeline starts,
**Then** the `SetupConfig` pydantic-settings model loads cleanly and exposes typed config values to the rest of the app.

**Given** a `setup.toml` with a missing required key,
**When** the pipeline starts,
**Then** it raises `ConfigError` naming the missing key and exits non-zero (FR31).

**Given** a `setup.toml` with an unknown extra key,
**When** the pipeline starts,
**Then** it raises `ConfigError` (pydantic `extra="forbid"`) naming the offending key.

**Given** an `.env` file containing `PICOVOICE_ACCESS_KEY`,
**When** the pipeline loads,
**Then** the credential is loaded only at startup; the process does not re-read it at runtime (NFR23, FR34).

**Given** a `setup.toml` with an integer `schema_version` matching the pipeline's supported version,
**When** the pipeline starts,
**Then** it loads.
**And** when the version is unsupported, **then** it raises `SchemaVersionError` reporting both the file's version and the supported version (NFR27).

**Given** an `.env.example` template,
**When** I inspect it,
**Then** it lists `PICOVOICE_ACCESS_KEY=<your-key-here>` plus commented placeholders for `ANTHROPIC_API_KEY` and `CARTESIA_API_KEY` (wired in Epic 2).

**Given** an `.env` file with permissions looser than `0600`,
**When** the pipeline starts,
**Then** it logs a `config.env.permissions_loose` WARN (advisory; v1 doesn't refuse to start, but the warning surfaces the NFR23 expectation).

**Given** a unit test in `tests/unit/config/test_setup.py`,
**When** valid + several invalid configs are loaded,
**Then** valid loads succeed and each invalid load raises the right exception subclass with the expected message.

---

### Story 1.3: Logging — structlog + redaction + rotating files

As Kamal,
I want JSON-structured logs landing in `./logs/` with three rotation streams and redaction enforced before serialization,
So that I can post-mortem any session without leaking credentials, raw audio, or transcripts.

**Acceptance Criteria:**

**Given** the pipeline starts,
**When** logging initializes,
**Then** `./logs/voice-agent.log` (INFO+), `./logs/errors.log` (WARN+), and `./logs/debug.log` (DEBUG, opt-in via `LOG_LEVEL=DEBUG`) are created with `RotatingFileHandler` (size-based, default 50MB/file, 7-day retention default).

**Given** the structlog configuration,
**When** any module calls `log.info("startup.completed", ...)`,
**Then** the line is JSON-formatted (NFR29) with mandatory `event` field in `verb.subject` form plus `timestamp`, `level`, `logger`.

**Given** a log call passing an `audio_bytes` (or `audio_data`, `pcm`) field,
**When** the redaction processor runs,
**Then** the field is dropped before serialization (NFR25).

**Given** a log call passing fields matching `*api_key`, `*token`, `*password`, `*secret`,
**When** the redaction processor runs,
**Then** those fields are dropped.

**Given** `LOG_LEVEL=INFO` (default),
**When** code logs `transcript=...` or `user_text=...`,
**Then** the field is dropped from the serialized output (FR39, NFR25).

**Given** `LOG_LEVEL=DEBUG`,
**When** code logs `transcript=...`,
**Then** the transcript appears in `debug.log` only.

**Given** `LOG_CONSOLE=true` env var,
**When** the pipeline runs,
**Then** logs also mirror to stdout for dev work.
**And** when `LOG_CONSOLE` is unset/false, **then** stdout stays silent (production posture).

**Given** unit tests in `tests/unit/logging/test_redaction.py`,
**When** the redaction processor is exercised against the documented denylist,
**Then** all dropped-field assertions pass and no false positives for unrelated keys.

---

### Story 1.4: Event schemas, error hierarchy, Protocol seams

As Kamal,
I want `ExpressionEvent` + `LifecycleEvent`, the custom exception hierarchy, and all 6 Protocol seams declared before any feature code consumes them,
So that subsequent stories implement against stable interfaces with no retrofitting.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/schemas/expression_event.py`,
**When** I inspect it,
**Then** `ExpressionEvent` is a frozen pydantic v2 model with fields `schema_version: int`, `event_type: Literal["expression"]`, `emotion: str`, `source_tag: str`, `audio_frame_id: str | None`, `timestamp_ns: int`, `payload: dict[str, Any]`, with `model_config = ConfigDict(frozen=True, extra="forbid")`.

**Given** `src/voice_agent_pipeline/schemas/lifecycle_event.py`,
**When** I inspect it,
**Then** `LifecycleEvent` is a frozen pydantic v2 model with fields `schema_version: int`, `event_type: Literal["lifecycle"]`, `state: Literal["SLEEPING","LISTENING","THINKING","SPEAKING","IDLE"]`, `timestamp_ns: int`, `payload: dict[str, Any] = {}`.

**Given** `src/voice_agent_pipeline/errors.py`,
**When** I inspect it,
**Then** the exception hierarchy is exactly: `VoiceAgentError` (root) → `ConfigError`, `SchemaVersionError(ConfigError)`, `StartupValidationError`, `ExternalServiceError` → `CartesiaError`, `OrchestratorError`, `TalkerError`, plus `PublisherError`, `SplitterError` siblings of `VoiceAgentError`.

**Given** the 6 Protocol seam files (`stt/backend.py`, `turn/talker.py`, `turn/orchestrator.py`, `turn/beliefs.py`, `tts/client.py`, `publisher/interface.py`),
**When** I inspect each,
**Then** it declares its `typing.Protocol` matching the architecture's interface signatures (e.g., `STTBackend.transcribe(audio) -> TranscriptionResult`; `ExpressionPublisher.{connect, disconnect, is_healthy, publish_expression, publish_lifecycle}`; `OrchestratorClient` SSE-streaming method; `TalkerClient.complete(...)`; etc.).

**Given** a contract test in `tests/contract/test_expression_event_schema.py`,
**When** an `ExpressionEvent` is serialized to JSON and parsed back,
**Then** field equality holds across the round-trip.

**Given** a contract test for `schema_version`,
**When** an event with an unsupported `schema_version` is parsed,
**Then** `SchemaVersionError` is raised.

**Given** pyright strict on `src/`,
**When** `just check` runs,
**Then** no `Any` appears outside the documented `payload: dict[str, Any]` extensibility seam, and no untyped function definitions.

---

### Story 1.5: Audio capture path (mic input + device pinning)

As Kamal,
I want the mic resolved by stable name regex and audio frames flowing into the Pipecat input pipeline,
So that downstream stages (wake-word, VAD, STT) can consume real audio from my chosen device.

**Acceptance Criteria:**

**Given** `setup.toml` with `[audio] input_device_name = "USB.*Mic.*"` (regex),
**When** the pipeline starts,
**Then** `resolve_audio_devices(config)` matches the pattern against PyAudio's enumerated devices and returns a numeric index.

**Given** no PyAudio device matches the configured regex,
**When** the pipeline starts,
**Then** it raises `StartupValidationError` listing the regex and the available device names, and exits non-zero (FR4).

**Given** `pipecat-ai[local]` and a configured input device,
**When** `LocalAudioTransport` is initialized with `input_device_index=<resolved>`,
**Then** it begins emitting `AudioRawFrame`s into the pipeline.

**Given** the pipeline is running with no further downstream stages,
**When** mic audio arrives,
**Then** frames flow without raising (verifiable via a debug-only frame counter).

**Given** a USB hot-plug event on an *unrelated* device,
**When** it occurs,
**Then** the pipeline does not restart and audio capture continues (validates NFR11 mechanism; full soak tuning lives in Epic 5).

**Given** a unit test for `resolve_audio_devices`,
**When** the input device enumeration is mocked with several devices,
**Then** the regex match returns the expected index, and a no-match raises `StartupValidationError`.

**Given** audio frames flow through,
**When** INFO-level logs are emitted,
**Then** no `audio_bytes` field appears anywhere in the output (redaction enforcement test).

---

### Story 1.6: Wake-word detection (Picovoice Porcupine + custom phrase)

As Kamal,
I want Porcupine running on the mic stream firing on my custom "Hey OLAF" phrase,
So that the pipeline distinguishes intentional speech from background audio without dispatching downstream until I address it.

**Acceptance Criteria:**

**Given** `models/wakeword/hey_olaf.ppn` is committed and present,
**When** the pipeline starts,
**Then** `pvporcupine.create(...)` loads the keyword file successfully.

**Given** `PICOVOICE_ACCESS_KEY` is missing or invalid,
**When** the pipeline starts,
**Then** it raises `StartupValidationError` and exits non-zero (FR34, startup validation extension).

**Given** the wake-word stage is wired via `audio/wakeword.py`,
**When** mic frames stream in,
**Then** `pvporcupine.process(...)` is invoked inside `asyncio.to_thread(...)` so the event loop is never blocked.

**Given** I speak "Hey OLAF" within mic range,
**When** Porcupine detects the keyword,
**Then** an INFO log `wakeword.detected` fires with timestamp and audio offset.

**Given** background audio without the keyword (TV, conversation),
**When** mic frames stream in for 10 minutes,
**Then** zero `wakeword.detected` logs fire (validates NFR12 mechanism; final FP threshold tuning lives in Epic 5).

**Given** a wake-word fires,
**When** the internal listening state transitions,
**Then** no audio captured prior to the wake-word is buffered to disk or emitted downstream (FR1, FR42).

**Given** a unit test in `tests/unit/audio/test_wakeword.py` mocking `pvporcupine.process`,
**When** the mock returns a positive detection,
**Then** the wakeword stage emits a `WakeWordDetected` frame.
**And** when the mock returns negative, **then** no frame is emitted.

---

### Story 1.7: VAD-bounded capture + STT transcription (faster-whisper)

As Kamal,
I want post-wake-word audio captured until end-of-speech (Silero VAD) and transcribed locally by faster-whisper with confidence,
So that I can verify the listening half-loop with a transcript I can read in `./logs/voice-agent.log`.

**Acceptance Criteria:**

**Given** a wake-word fires,
**When** the VAD stage activates,
**Then** `audio/vad.py` (Silero, Pipecat-bundled) consumes mic frames and signals end-of-speech when sustained silence is detected (FR2).

**Given** end-of-speech is signaled,
**When** the captured utterance is handed to STT,
**Then** audio capture for that turn terminates (no further frames captured this turn).

**Given** `stt/whisper_cpu.py` implements `STTBackend`,
**When** `transcribe(audio)` is called,
**Then** `faster_whisper.WhisperModel.transcribe(...)` runs inside `asyncio.to_thread(...)` and returns a `TranscriptionResult` carrying the transcript text and a confidence score (FR6, FR8).

**Given** a transcript completes,
**When** it is emitted,
**Then** an INFO log `stt.transcript` fires with `confidence=<float>` and the transcript text appears at DEBUG level only (FR39).

**Given** STT measurement on the dev host,
**When** 30 turns of normal speech are processed,
**Then** the p95 of (end-of-speech → transcript-ready) is recorded and logged for NFR3 baseline tracking (final tuning in Epic 5).

**Given** a transcript with confidence below the configured threshold (`setup.toml` `[stt] low_confidence_threshold`),
**When** emitted,
**Then** a `stt.low_confidence` WARN log fires (Epic 1 stops at the WARN; the full clarification dialog routing arrives in Epic 2 with Talker — FR8 progressive enrichment).

**Given** `setup.toml` configures `[stt] backend = "whisper-cpu"` and `[stt] model = "small"`,
**When** the pipeline starts,
**Then** the WhisperBackend is selected via the `STTBackend` Protocol seam (proves the v2 swap point; Hailo backend is v2 — out of scope here).

**Given** an integration test in `tests/integration/test_listen_loop.py`,
**When** it stitches `audio_capture → wakeword → vad → stt` with mocked Porcupine + Silero + faster-whisper,
**Then** a `stt.transcript` event is logged for each simulated turn.

---

## Epic 2: Speak — Talker + Cartesia

**Goal:** Complete the simple voice loop. After wake-word + STT (Epic 1), route the transcript to Anthropic Talker, stream the response through Cartesia TTS, and play it through the speaker. Hits NFR1 (simple-turn ≤1500ms p95).

### Story 2.1: Audio playback path (speaker output + device pinning)

As Kamal,
I want the speaker resolved by stable name regex and `LocalAudioTransport` output emitting audio frames,
So that Cartesia (next story) and any future audio source can play through my chosen speaker.

**Acceptance Criteria:**

**Given** `setup.toml` with `[audio] output_device_name = "USB.*Speaker.*"` (regex),
**When** the pipeline starts,
**Then** `resolve_audio_devices(config)` matches the pattern against PyAudio's enumerated output devices and returns a numeric index (extends Story 1.5's helper).

**Given** no PyAudio output device matches the configured regex,
**When** the pipeline starts,
**Then** it raises `StartupValidationError` listing the regex and the available output device names, exits non-zero (FR4 extension to speaker side).

**Given** `pipecat-ai[local]` and a configured output device,
**When** `LocalAudioTransport` is initialized with `output_device_index=<resolved>`,
**Then** it accepts `AudioRawFrame`s and plays them through the speaker.

**Given** a `just play-test-tone` recipe,
**When** I run it,
**Then** the pipeline plays a short canned tone (1s, 440Hz from a committed `tests/fixtures/test_tone.wav` or generated in-place) through the resolved speaker, then exits 0. Verifies the speaker path independent of TTS.

**Given** audio frames flow to the speaker,
**When** played end-to-end on the dev host,
**Then** no buffering pause >100ms is introduced by the playback path itself (NFR6 mechanism baseline; full Cartesia integration validated in Story 2.3).

**Given** a unit test in `tests/unit/audio/test_devices.py`,
**When** the device enumeration is mocked with several output devices,
**Then** the regex match returns the expected index, and a no-match raises `StartupValidationError`.

---

### Story 2.2: TalkerClient — Anthropic async client behind the Protocol seam

As Kamal,
I want a `TalkerClient` implementation calling Anthropic's API asynchronously, returning a plain text response for a given transcript,
So that Story 2.4 can route transcripts through it without yet wiring belief-state or SSML emission.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/turn/talker.py`,
**When** I inspect it,
**Then** it declares `TalkerClient` Protocol and an `AnthropicTalker` concrete class implementing it; `anthropic` is imported in this file only (boundary-concentration rule).

**Given** `AnthropicTalker.complete(transcript: str) -> str`,
**When** invoked with a transcript,
**Then** it calls `anthropic.AsyncAnthropic` with the configured model (default `claude-haiku-4-5` per architecture) and returns the assistant's plain text response.

**Given** a `setup.toml` `[talker]` block with `model`, `max_tokens`, `system_prompt_path` (path to a markdown file with the system prompt),
**When** the pipeline starts,
**Then** `AnthropicTalker` reads its config and the system prompt file at startup; the prompt is **not** loaded per-turn.

**Given** the system prompt for Epic 2,
**When** I inspect `prompts/talker_system.md` (path configurable),
**Then** it instructs Talker to respond in plain text only — **no SSML/Cartesia emotion tags yet** (Epic 3 will update this prompt).

**Given** `ANTHROPIC_API_KEY` is missing or invalid,
**When** the pipeline starts,
**Then** startup validation issues a one-shot lightweight call (e.g., `messages.count_tokens` or a 1-token completion) and raises `StartupValidationError` on failure (extends FR34 + v1 fail-fast posture).

**Given** v1 fail-fast policy,
**When** Anthropic returns an error or the call fails at runtime,
**Then** the exception propagates as `TalkerError` (subclass of `ExternalServiceError`) and crashes the process (no in-process retry; systemd restart in Epic 5).

**Given** a unit test in `tests/unit/turn/test_talker.py` mocking `anthropic.AsyncAnthropic`,
**When** the mock returns a stub response,
**Then** `AnthropicTalker.complete(...)` returns the response text and the call shape (model, system, user message) matches expectations.

**Given** the simple-turn latency budget,
**When** measured against the dev host with a mocked Anthropic response,
**Then** the wrapper itself adds <50ms overhead (Anthropic round-trip is the dominant term, validated in Story 2.5).

---

### Story 2.3: CartesiaClient — Sonic-3 streaming TTS behind the Protocol seam

As Kamal,
I want a `CartesiaClient` streaming text to Cartesia Sonic-3 and yielding audio frames to the speaker,
So that Story 2.5 can connect Talker output to spoken output end-to-end.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/tts/cartesia.py`,
**When** I inspect it,
**Then** it declares `TTSClient` Protocol (in `tts/client.py`) and a `CartesiaClient` concrete impl using the official `cartesia` SDK; `cartesia` is imported only in this file.

**Given** `CartesiaClient.synthesize(text: str) -> AsyncIterator[AudioRawFrame]`,
**When** invoked with text,
**Then** it opens a streaming session to Cartesia Sonic-3 with the configured voice ID and yields `AudioRawFrame`s incrementally as Cartesia returns them (FR15).

**Given** `setup.toml` `[tts]` block with `voice_id`, `default_emotion`, `model = "sonic-3"`,
**When** Cartesia is invoked,
**Then** the configured voice ID is used (FR17). Default emotion is included in the request payload but tags-in-text are **not** parsed in this story (splitter arrives Epic 3).

**Given** `CARTESIA_API_KEY` is missing or invalid,
**When** the pipeline starts,
**Then** startup validation makes a lightweight Cartesia call (e.g., voices list) and raises `StartupValidationError` on failure (FR34 + fail-fast).

**Given** TLS validation,
**When** the Cartesia client is constructed,
**Then** the underlying HTTP client validates certificates; configuration can **not** disable TLS validation (NFR24 — refuse-to-start if config attempts it).

**Given** a synthesis call,
**When** measured on the dev host,
**Then** time from request send → first audio frame is recorded; baseline logged for NFR4 tracking (≤400ms p95 target).

**Given** v1 fail-fast policy,
**When** Cartesia returns an error or stream stalls,
**Then** the exception propagates as `CartesiaError` and crashes (no retry; resilience layer is v2).

**Given** a unit test in `tests/unit/tts/test_cartesia.py` mocking the `cartesia` SDK,
**When** the mock yields stub audio frames,
**Then** `CartesiaClient.synthesize(...)` yields them through to its caller in order, with no buffering of the full stream (real-time contract).

---

### Story 2.4: TurnRouter (Talker-only) + low-confidence clarification dialog

As Kamal,
I want a `TurnRouter` that routes every transcript to Talker for now, plus a clarification dialog when STT confidence is low,
So that Epic 2 produces a working simple-turn loop and Epic 1's deferred FR8 routing finally completes.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/turn/router.py`,
**When** I inspect it,
**Then** `TurnRouter` accepts a transcript + confidence and returns a routing decision; in Epic 2 the decision is **always Talker** (orchestrator path stubbed for Epic 4 with a clear `NotImplementedError` if reached).

**Given** a transcript with confidence ≥ `[stt] low_confidence_threshold`,
**When** `TurnRouter.route(transcript, confidence)` is called,
**Then** it returns a `RouteDecision(target="talker", text=transcript)`.

**Given** a transcript with confidence < threshold,
**When** `TurnRouter.route(...)` is called,
**Then** it returns `RouteDecision(target="talker", text="<clarification prompt>")` where the clarification prompt is a configurable string in `setup.toml` (`[stt] clarification_prompt`, default `"Sorry, I didn't catch that — could you say it again?"`). FR8 closure: low-confidence now triggers a real spoken clarification, not just a WARN log.

**Given** the WARN log from Story 1.7 (`stt.low_confidence`),
**When** the clarification path triggers,
**Then** the log is upgraded to include `action="clarify"` so observers can correlate the dialog with the STT confidence value.

**Given** v1 scope,
**When** routing decisions are made,
**Then** TurnRouter does NOT yet read keyword/regex routing rules from config (that's Epic 4). Architecture's open question on hot-reload of routing rules also defers to Epic 4.

**Given** a unit test in `tests/unit/turn/test_router.py`,
**When** transcripts above and below the confidence threshold are routed,
**Then** the high-confidence transcript routes to Talker with original text, the low-confidence one routes to Talker with the clarification prompt, and any attempt to route to "orchestrator" raises `NotImplementedError`.

---

### Story 2.5: Pipeline assembly + simple-turn integration test (NFR1 baseline)

As Kamal,
I want `pipeline.py` wiring `wakeword → vad → stt → router → talker → cartesia → speaker` end-to-end with an integration test for journey 1 and a measured NFR1 baseline,
So that I can run `just run`, say "Hey OLAF, what time is it?", and hear OLAF respond — proving the simple-turn loop hits ≤1500ms p95.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/pipeline.py`,
**When** I inspect it,
**Then** it assembles the Pipecat pipeline in this order: `LocalAudioTransport(input)` → `Wakeword` → `VAD` → `STT` → `TurnRouter` → `TalkerClient` → `TTSClient(Cartesia)` → `LocalAudioTransport(output)`; assembly happens once at startup, never per-turn.

**Given** `__main__.py`,
**When** the process starts,
**Then** it parses argv, installs signal handlers (SIGTERM for graceful shutdown — full SIGHUP wiring is Epic 5), runs startup validation for **all Epic 2 deps** (Picovoice + Anthropic + Cartesia + audio devices), then `asyncio.run(pipeline.main())`.

**Given** any startup-validation failure,
**When** the process starts,
**Then** it logs a CRITICAL `startup.failed` with the failure reason and exits non-zero (no partial run, no degraded mode — v1 fail-fast).

**Given** `just run`,
**When** I execute it on the dev host with valid config,
**Then** I can speak "Hey OLAF, what time is it?" into the mic and hear a Cartesia-synthesized response from the speaker within ~1.5s of end-of-speech.

**Given** an integration test in `tests/integration/test_simple_turn.py` (PRD Journey 1),
**When** it runs the full pipeline with mocked Porcupine + Silero + faster-whisper + Anthropic + Cartesia (all at the Protocol seams),
**Then** end-of-speech → first audio frame is measured for 30 simulated turns and the p95 is recorded as the **NFR1 baseline**.

**Given** the integration test runs against the real Anthropic + Cartesia APIs (gated behind a `RUN_LIVE_TTS=true` env var to keep CI hermetic),
**When** invoked manually,
**Then** the live p95 is logged for comparison against the mocked baseline.

**Given** the redaction discipline from Story 1.3,
**When** the simple-turn integration test runs,
**Then** no log line emitted by the pipeline contains a transcript at INFO level, raw audio bytes, or the `ANTHROPIC_API_KEY` / `CARTESIA_API_KEY` value (NFR25, FR39).

**Given** SIGTERM during a turn,
**When** sent,
**Then** the pipeline drains the in-flight Cartesia frames, closes httpx clients via `async with` cleanup, and exits 0 (graceful shutdown contract; barge-in is Epic 5).

**Given** Cartesia is unreachable mid-turn,
**When** an error is raised,
**Then** the process crashes with `CartesiaError` (v1 fail-fast — graceful degraded mode is FR16, deferred to v2 resilience layer).

---

## Epic 3: Embodiment Channel — emotion in lockstep with voice

**Goal:** Talker emits Cartesia SSML tags; the streaming splitter parses them; segments anchor to audio frames; `Ros2ExpressionPublisher` broadcasts `ExpressionEvent`s on the configured channel with 30–80ms anticipatory alignment. Full Cartesia tag → expression mapping with no silent gaps. The "OLAF feels alive" sprint.

### Story 3.1: `expression_map.yaml` authoring + loader + schema validation

As Kamal,
I want a complete `expression_map.yaml` covering all Cartesia emotion tags + bursts plus a pydantic-validated loader that refuses bad maps at startup,
So that subsequent stories have a typed, complete mapping table to consume — and adding new tags is forever a YAML edit.

**Acceptance Criteria:**

**Given** `expression_map.yaml` at the project root,
**When** I inspect it,
**Then** it contains an integer `schema_version`, an `emotions:` block with all 6 primary (`neutral, content, excited, sad, angry, scared`) and 6 secondary (`happy, curious, sympathetic, surprised, frustrated, melancholic`) emotions as first-class entries with full payload (`base_pose`, `eye_state`, `led_color`, `led_intensity` — values negotiated with embodiment but published as opaque `payload`), a `bursts:` block (`laughter, sigh, gasp, clears_throat`), a `fallback_families:` block grouping the remaining 50+ Cartesia tags into 7 families (e.g., `high_energy_positive → excited`, `low_energy_negative → sad`), and an `unknown:` entry mapping to `neutral`.

**Given** `src/voice_agent_pipeline/config/expression_map.py`,
**When** I inspect it,
**Then** `ExpressionMapConfig` is a pydantic v2 model with the full schema (emotions, bursts, fallback_families, unknown, schema_version), `extra="forbid"` on every nested model, and a `load_from_path(path) -> ExpressionMapConfig` function that validates at startup.

**Given** a malformed `expression_map.yaml` (missing key, wrong type, unknown extra key),
**When** the pipeline starts,
**Then** loading raises `ConfigError` with the offending key/path and exits non-zero (FR31 extension).

**Given** an `expression_map.yaml` with an unsupported `schema_version`,
**When** the pipeline starts,
**Then** it raises `SchemaVersionError` (NFR27).

**Given** a coverage check at startup,
**When** the loader validates the map,
**Then** every primary + secondary emotion has a non-empty `payload`; missing payload raises `ConfigError` (FR20 — no silent gaps).

**Given** the architectural extensibility test,
**When** I add a new entry under `emotions:` (e.g., `serene`) with payload, restart the pipeline, and the LLM emits `<emotion value="serene"/>`,
**Then** the resolver (Story 3.2) finds it as first-class — proven by the unit test in 3.2 covering the new entry. (SIGHUP hot-reload of this same change is Epic 5.)

**Given** unit tests in `tests/unit/config/test_expression_map.py`,
**When** valid + several invalid maps are loaded,
**Then** valid loads succeed and each invalid load raises the right exception subclass with the expected message.

---

### Story 3.2: Mapping resolver + last-published cache

As Kamal,
I want a pure-function resolver that turns any Cartesia tag into an `ExpressionEvent` payload via the loaded mapping with fallback-family resolution,
So that the splitter (Story 3.3) can call one function regardless of whether the tag is primary, secondary, family-fallback, or completely unknown.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/splitter/mapping.py`,
**When** I inspect it,
**Then** it exposes `resolve(tag: str, mapping: ExpressionMapConfig) -> ResolvedExpression` returning the resolved emotion name, source tag, and payload dict (FR20, FR21).

**Given** a tag that exists in the `emotions:` block,
**When** `resolve("excited", mapping)` is called,
**Then** it returns `ResolvedExpression(emotion="excited", source_tag="excited", payload=<excited payload>)` with no log noise.

**Given** a tag in `fallback_families` mapped to a primary,
**When** `resolve("enthusiastic", mapping)` is called,
**Then** it returns `ResolvedExpression(emotion="excited", source_tag="enthusiastic", payload=<excited payload>)` and logs `event="emotion.fallback"` at DEBUG level on first occurrence per process (de-duped via in-memory set), per FR38 (DEBUG on first; WARN if completely unknown).

**Given** a tag truly absent from any family,
**When** `resolve("neverbeforeseentag", mapping)` is called,
**Then** it returns `ResolvedExpression(emotion="neutral", source_tag="neverbeforeseentag", payload=<neutral payload>)` and logs `event="emotion.unmapped"` at WARN level (FR38).

**Given** `LastPublishedCache` in `splitter/mapping.py`,
**When** the same base emotion (`excited`) resolves twice consecutively without a different emotion intervening,
**Then** `cache.should_publish(resolved)` returns `True` for the first call and `False` for the second (FR24 dedup).

**Given** burst events,
**When** a burst (`[laughter]`, `[sigh]`, etc.) is offered to the cache,
**Then** `cache.should_publish(burst)` always returns `True` (bursts are never deduped, per FR24).

**Given** a unit test in `tests/unit/splitter/test_mapping.py`,
**When** the resolver is exercised against primary, secondary, family-fallback, unknown, and burst inputs,
**Then** all expected outputs hold and log assertions match (DEBUG vs WARN per case).

**Given** a unit test for the cache,
**When** sequences like `[content, content, sad, content, [laughter], [laughter]]` are offered,
**Then** `should_publish` returns `[T, F, T, T, T, T]` — bursts always publish, base emotions dedup until they change.

---

### Story 3.3: Streaming SSML state machine + boundary-based segmenter

As Kamal,
I want a hand-rolled streaming parser that consumes Cartesia-tagged text token-by-token, splits across token boundaries safely, and emits segments on whichever boundary comes first (sentence / emotion-tag / burst),
So that segments can be handed to TTS and resolver in lockstep without buffering the full response.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/splitter/state_machine.py`,
**When** I inspect it,
**Then** it implements a hand-rolled state machine in ~50–100 LOC, zero external dependencies, parsing `<emotion value="X"/>` tags and `[burst_name]` events from a token stream incrementally (FR18).

**Given** a tag split across two tokens (e.g., `<emoti` then `on value="excited"/>`),
**When** the state machine consumes the tokens,
**Then** the tag is correctly assembled and emitted as a single tag event (FR18: token-by-token, tags may split across boundaries).

**Given** `src/voice_agent_pipeline/splitter/segmenter.py`,
**When** the segmenter consumes the state machine's events,
**Then** it emits a `Segment(text, current_emotion, burst_events)` on whichever boundary comes first: sentence terminator (`.?!`), emotion tag, or burst tag (FR19).

**Given** a stream containing `<emotion value="content"/> Hello there. <emotion value="excited"/> Great news!`,
**When** segmented,
**Then** segments emit in order: `Segment(text="Hello there.", current_emotion="content", bursts=[])` then `Segment(text="Great news!", current_emotion="excited", bursts=[])`.

**Given** a stream with `[laughter]` mid-sentence,
**When** segmented,
**Then** the burst is emitted as a separate event (Story 3.5 attaches it to its audio anchor); the burst tag is **stripped from the text** that goes to TTS (FR25).

**Given** state across calls,
**When** the segmenter retains `current_emotion` and `last_published_emotion` (the latter consumed by Story 3.2's cache),
**Then** the dedup contract (FR24) is satisfied: a segment with no emotion change does not republish its `ExpressionEvent`.

**Given** a malformed tag (e.g., `<emotion value=`),
**When** the parser encounters end-of-stream without closure,
**Then** it raises `SplitterError` (catchable only at the process boundary in v1; crash → systemd restart in Epic 5).

**Given** unit tests in `tests/unit/splitter/test_state_machine.py` and `test_segmenter.py`,
**When** the suite runs,
**Then** the following cases pass: token-boundary tag assembly, sentence-terminator emission, mixed sentence+tag+burst, burst stripping from text, malformed tag error, multiple emotion changes in one stream, no-emotion plain-text fallthrough.

---

### Story 3.4: `Ros2ExpressionPublisher` — `std_msgs/String` + JSON over RELIABLE QoS

As Kamal,
I want the `ExpressionPublisher` Protocol implemented over ROS 2 / DDS using `std_msgs/String` with the full event JSON-serialized (no custom `.msg` package in v1),
So that v1 ships an embodiment broadcast channel with zero ament/colcon overhead and any subscriber on the configured topic receives the events.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/publisher/ros2.py`,
**When** I inspect it,
**Then** it implements `ExpressionPublisher` Protocol; `rclpy` is imported only in this file (boundary-concentration rule).

**Given** the v1 wire-format simplification (architecture revision to Batch 3),
**When** publishing,
**Then** the publisher uses `std_msgs/String` with the entire `ExpressionEvent` (and `LifecycleEvent` — wired in Epic 4) JSON-encoded as the message data; no custom `.msg` IDL, no ament_python package, no colcon.

**Given** ROS 2 QoS configuration,
**When** the publisher is constructed,
**Then** it uses RELIABLE QoS for both expression and lifecycle topics (NFR21 — lost messages are not acceptable for embodiment correctness).

**Given** `setup.toml` with `[publisher] transport = "ros2"`, `dds_domain_id`, `expression_channel = "/olaf/expression"`, `lifecycle_channel = "/olaf/lifecycle"`,
**When** the pipeline starts,
**Then** the publisher reads channel names + DDS domain from config (no hard-coded topic names — agnostic publisher per the project's pipeline-scope boundary memory).

**Given** `Ros2ExpressionPublisher.connect()`,
**When** called at startup,
**Then** it initializes `rclpy`, creates a node, creates publishers on both channels, and returns; failure raises `PublisherError` and `StartupValidationError` cascades (v1 fail-fast — broadcast bus is a hard dep).

**Given** `publish_expression(event)`,
**When** called,
**Then** it serializes `event.model_dump_json()` into a `String` message and publishes on the expression channel; runtime failure raises `PublisherError` and crashes the process.

**Given** `publish_lifecycle(event)` defined for symmetry,
**When** called in Epic 3,
**Then** it works the same way but is **not yet invoked** by any caller (Epic 4 wires the lifecycle machine).

**Given** a unit test in `tests/unit/publisher/test_ros2.py` mocking `rclpy`,
**When** events are published,
**Then** the JSON content matches `event.model_dump_json()`, the topic name matches config, and QoS settings match.

**Given** a contract test in `tests/contract/test_expression_event_schema.py`,
**When** an `ExpressionEvent` is JSON-encoded → `String` message → JSON-decoded back,
**Then** field equality holds across the round-trip (NFR27 schema versioning verified at parse).

**Given** the README,
**When** I open the deployment notes,
**Then** they explain the system-installed `rclpy` requirement (e.g., `apt install ros-jazzy-rclpy`) and how to expose it to the venv via `PYTHONPATH`.

---

### Story 3.5: Audio-frame metadata threading + Talker SSML prompt + integration test

As Kamal,
I want segments' `ExpressionEvent` metadata threaded through Pipecat's audio frames so the publisher fires when each frame is sent — and Talker updated to emit Cartesia SSML tags — and an integration test that proves voice/embodiment alignment hits the 30–80ms anticipatory window,
So that Sprint 3 delivers visible (on-bus) embodiment in lockstep with audio.

**Acceptance Criteria:**

**Given** Pipecat's `AudioRawFrame`,
**When** the splitter (Story 3.3) emits a `Segment` with an `ExpressionEvent`,
**Then** the segment's first audio frame from Cartesia carries the event in an optional `expression_event` metadata field (architecture's Batch 2 decision; if Pipecat's processor model can't carry it cleanly, fall back to time-based correlation per the documented PRD risk fallback — and document the deviation in PRD/architecture per NFR26).

**Given** `LocalAudioTransport` output,
**When** it sends an audio frame to the speaker,
**Then** if `frame.expression_event` is set, it calls `ExpressionPublisher.publish_expression(event)` immediately before `frame.send_to_speaker()` (FR22, FR23).

**Given** the last-published cache from Story 3.2,
**When** the segmenter resolves a segment's emotion via the resolver and the cache says `should_publish=False`,
**Then** no `ExpressionEvent` is attached to that segment's audio frame (FR24 dedup).

**Given** `prompts/talker_system.md`,
**When** I inspect it,
**Then** it now instructs Talker to emit responses with Cartesia `<emotion value="..."/>` SSML tags inline (e.g., `<emotion value="content"/> It's 8:47.`), and to optionally use `[laughter]` / `[sigh]` bursts. The prompt enumerates the 6 primary + 6 secondary emotions for the LLM (FR12 extension).

**Given** an integration test in `tests/integration/test_embodiment_alignment.py`,
**When** the full pipeline runs with mocked Cartesia (yielding deterministic audio frames at known timestamps) and a mocked publisher (capturing publish times),
**Then** for 30 simulated turns, the p95 of (publish_time − frame_send_time) falls within the 30–80ms anticipatory window (NFR5).

**Given** an integration test mocking the publisher and asserting on event payloads,
**When** Talker emits a response with one primary, one secondary, and one fallback-family tag,
**Then** all three `ExpressionEvent`s publish with correct `emotion`, `source_tag`, and `payload` fields, in correct order, with bursts always present and base emotions deduped per FR24.

**Given** the redaction discipline,
**When** Epic 3's integration tests run,
**Then** no log line contains `audio_bytes`, raw transcripts at INFO level, or credentials (NFR25).

**Given** the v1 deferred fallback path (PRD risk),
**When** audio-frame metadata cannot be threaded cleanly through Pipecat,
**Then** the story's implementation switches to time-based correlation (publish at `frame.send_time + offset`) and the deviation is recorded in `architecture.md` (NFR26 spec-as-contract update in the same change).

---

## Epic 4: Complex Questions & Lifecycle

**Goal:** Add the orchestrator dispatch path (SSE), belief-state grounding for Talker, full TurnRouter fast/slow decision, and lifecycle event publishing. After this, OLAF can answer "what's on my calendar?" with narration → subagent → response chunks, and subscribers know what state OLAF is in.

### Story 4.1: `BeliefStateClient` — per-turn fresh `GET /beliefs?keys=...`

As Kamal,
I want a `BeliefStateClient` that fetches belief state from the orchestrator daemon per turn (no cache), used by Talker to ground fast-path responses,
So that Talker can answer "what time is it?" using actual daemon state rather than the LLM's own guess.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/turn/beliefs.py`,
**When** I inspect it,
**Then** `BeliefStateClient` Protocol is declared and `HttpBeliefStateClient` implements it using a persistent `httpx.AsyncClient` keyed to the orchestrator daemon URL.

**Given** `BeliefStateClient.read(keys: list[str]) -> dict[str, Any]`,
**When** invoked with `["time", "calendar_today"]`,
**Then** it issues `GET /beliefs?keys=time,calendar_today` against the configured `daemon.url` and returns the parsed JSON object (FR10).

**Given** v1 architecture decision "no cache",
**When** `read(...)` is called,
**Then** every invocation issues a fresh HTTP request — no in-memory cache, no TTL.

**Given** a non-200 response,
**When** `read(...)` is called,
**Then** it raises `OrchestratorError` (subclass of `ExternalServiceError`); v1 fail-fast crashes the process (resilience layer is v2).

**Given** `setup.toml` `[daemon]` block with `url = "http://localhost:8001"`,
**When** the pipeline starts,
**Then** the client targets the configured URL.

**Given** Talker integration,
**When** `AnthropicTalker.complete(transcript)` is called in Epic 4,
**Then** it now optionally fetches belief state via `BeliefStateClient.read(...)` based on a configurable list of keys (e.g., `[talker] grounded_keys = ["time", "calendar_today"]`) and includes them in the system prompt context (FR12 extension — belief grounding).

**Given** a unit test in `tests/unit/turn/test_beliefs.py` mocking `httpx.AsyncClient`,
**When** `read(["time"])` is called,
**Then** the request URL matches `?keys=time`, the parsed response shape matches expectations, and a 500 response raises `OrchestratorError`.

**Given** an updated unit test for `AnthropicTalker`,
**When** `grounded_keys` is configured and a transcript triggers Talker,
**Then** the test asserts `BeliefStateClient.read(grounded_keys)` was called and the response was injected into the prompt context.

---

### Story 4.2: `OrchestratorClient` — SSE stream consumer over `httpx-sse`

As Kamal,
I want an `OrchestratorClient` that opens `POST /turn` as an SSE stream and yields typed events as they arrive (narration, subagent_started, subagent_progress, subagent_done, response_chunk, turn_end),
So that Story 4.5's pipeline can dispatch complex turns without buffering the full response.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/turn/orchestrator.py`,
**When** I inspect it,
**Then** `OrchestratorClient` Protocol is declared and `HttpOrchestratorClient` implements it using a persistent `httpx.AsyncClient` + `httpx-sse`; `httpx` and `httpx-sse` are imported only in this file plus `turn/beliefs.py`.

**Given** `OrchestratorClient.dispatch(transcript: str, session_id: str) -> AsyncIterator[OrchestratorStreamEvent]`,
**When** invoked,
**Then** it issues `POST /turn` with body `{"transcript": ..., "session_id": ...}` and `Accept: text/event-stream`, then yields each parsed SSE event as a typed pydantic model from `schemas/stream.py` — a discriminated union over `narration`, `subagent_started`, `subagent_progress`, `subagent_done`, `response_chunk`, `turn_end` (FR11).

**Given** an unknown SSE event `type` field,
**When** parsed,
**Then** the client logs `event="orchestrator.unknown_event_type"` at WARN level **and continues** consuming the stream (forward-compat per architecture Batch 4 decision; orchestrator can ship new event types without breaking the pipeline).

**Given** an SSE framing error or malformed JSON inside an event,
**When** parsed,
**Then** the client raises `OrchestratorError` and crashes the process (v1 fail-fast distinguishes "forward-compat extension" from "broken contract").

**Given** startup validation,
**When** the pipeline starts,
**Then** it issues `GET /health` against `<daemon.url>/health` and refuses to start unless 200 is returned (extends startup validation; spec-drift item — orchestrator project must expose `/health`).

**Given** a `cancel(session_id)` method,
**When** declared in the Protocol,
**Then** `HttpOrchestratorClient.cancel(session_id)` is **stubbed** in Epic 4 (raises `NotImplementedError`); the `HTTP DELETE /turn/{session_id}` wiring lands Epic 5 with barge-in.

**Given** v1 retry semantics,
**When** any HTTP error occurs (connection, timeout, non-2xx),
**Then** it raises `OrchestratorError` and crashes (no retry; resilience layer is v2 per architecture Batch 4 decision).

**Given** a unit test in `tests/unit/turn/test_orchestrator.py` with a mocked SSE source emitting a known event sequence,
**When** `dispatch(...)` is iterated,
**Then** the parsed event types and field values match the mock; an unknown `type` field produces a WARN log and stream continues; a malformed JSON event raises `OrchestratorError`.

**Given** the architecture's spec-drift list,
**When** Story 4.2 lands,
**Then** the requirement that the orchestrator daemon expose `GET /health` is added to the project's spec-drift tracking (a clear comment in `architecture.md` or a dedicated section in the README), per NFR26.

---

### Story 4.3: TurnRouter — full fast vs slow routing rule

As Kamal,
I want `TurnRouter` to route transcripts to Talker (fast) or orchestrator (slow) based on a configurable keyword/regex rule, replacing Epic 2's always-Talker stub,
So that simple questions stay fast and complex questions get the orchestrator.

**Acceptance Criteria:**

**Given** `setup.toml` `[router]` block,
**When** I inspect it,
**Then** it contains a `slow_path_patterns` list (regex strings) and a `default = "talker"` setting; transcripts matching any pattern route to `"orchestrator"`, others route to `"talker"` (FR9 full).

**Given** `TurnRouter.route(transcript, confidence)`,
**When** the transcript matches a slow-path pattern,
**Then** it returns `RouteDecision(target="orchestrator", text=transcript)`.

**Given** the transcript does not match,
**When** `route(...)` is called,
**Then** it returns `RouteDecision(target="talker", text=transcript)` (Talker fast-path with belief grounding from Story 4.1).

**Given** the low-confidence clarification path from Story 2.4,
**When** confidence is below threshold,
**Then** routing still produces the clarification prompt to Talker, **bypassing** slow-path matching (clarification always goes through Talker — fast).

**Given** the architecture's open question on hot-reload of the router rule,
**When** Epic 4 lands the rule,
**Then** the rule is **boot-time only** in Epic 4; SIGHUP hot-reload of `[router]` is bundled with the SIGHUP work in Epic 5 (deferred decision: include router patterns in the same atomic-swap mechanism, or only `expression_map.yaml`).

**Given** the orchestrator path's `NotImplementedError` from Story 2.4,
**When** Story 4.3 lands,
**Then** that stub is removed; the slow-path now actually invokes `OrchestratorClient.dispatch(...)` (wired in Story 4.5).

**Given** a unit test in `tests/unit/turn/test_router.py`,
**When** transcripts matching and not matching slow-path patterns are routed,
**Then** the routing decisions match expectations; a malformed regex in config raises `ConfigError` at startup.

---

### Story 4.4: Lifecycle state machine + `publish_lifecycle` wiring + idle→sleeping

As Kamal,
I want a lifecycle state machine that transitions between SLEEPING/LISTENING/THINKING/SPEAKING/IDLE on observable events, publishes each transition on the lifecycle channel, and falls back to SLEEPING after configurable idle,
So that subscribers (embodiment, observability, future analytics) know what OLAF is doing in real time.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/lifecycle/machine.py`,
**When** I inspect it,
**Then** `LifecycleMachine` holds the current state as `Literal["SLEEPING","LISTENING","THINKING","SPEAKING","IDLE"]` and exposes `transition(to_state, reason)` that validates the transition is legal, updates state, and calls `ExpressionPublisher.publish_lifecycle(...)` (FR26, FR27).

**Given** the legal transition table per PRD lifecycle and architecture,
**When** I inspect it,
**Then** legal transitions cover at minimum: `SLEEPING ↔ IDLE`, `IDLE → LISTENING` (on wake-word), `LISTENING → THINKING` (on end-of-speech), `THINKING → SPEAKING` (on first audio frame), `SPEAKING → IDLE` (on last audio frame), `IDLE → SLEEPING` (on idle timeout). Illegal transitions raise `VoiceAgentError`.

**Given** the wakeword + STT + Cartesia events from Epics 1–3,
**When** they fire,
**Then** the lifecycle machine subscribes to the corresponding pipeline events and transitions automatically — no manual `transition(...)` calls scattered across the codebase. Centralized in `lifecycle/machine.py`.

**Given** `setup.toml` `[lifecycle] idle_to_sleeping_seconds = 300`,
**When** the pipeline sits in IDLE for 300s without a wake-word,
**Then** the machine transitions to SLEEPING and publishes the event (FR28).

**Given** `Ros2ExpressionPublisher.publish_lifecycle` from Story 3.4 (defined but unused),
**When** Story 4.4 lands,
**Then** `publish_lifecycle` is now invoked on every legal transition with a `LifecycleEvent` carrying the new state and `timestamp_ns`. The lifecycle channel is established at startup (extends startup validation).

**Given** redaction discipline,
**When** lifecycle logs fire,
**Then** they emit `event="lifecycle.transition"` with `from_state`, `to_state`, `reason`, `session_id` — no transcript content, no audio bytes (NFR25).

**Given** a unit test in `tests/unit/lifecycle/test_machine.py`,
**When** legal and illegal transitions are exercised + the idle timeout fires (using `pytest-asyncio` + a fast-forward time fixture),
**Then** all expected transitions succeed, illegal ones raise, the timer fires, and `publish_lifecycle` is called for each transition.

**Given** an integration test in `tests/integration/test_lifecycle_journey.py`,
**When** the full pipeline runs through journey 1 (simple turn),
**Then** the published lifecycle sequence is `[IDLE, LISTENING, THINKING, SPEAKING, IDLE]` with timestamps matching the observed pipeline events.

---

### Story 4.5: Pipeline wiring (slow path) + missing-`turn_end` recovery + complex-turn integration test (NFR2 baseline)

As Kamal,
I want `pipeline.py` to wire the slow path (router → orchestrator → splitter → TTS+publisher) including missing-`turn_end` cleanup, plus an integration test for journey 2 (complex turn) that records the NFR2 baseline,
So that I can ask "what's on my calendar?" and OLAF actually answers — narration first, then real result via subagent.

**Acceptance Criteria:**

**Given** `pipeline.py` updates,
**When** `TurnRouter` returns `target="orchestrator"`,
**Then** the pipeline dispatches via `OrchestratorClient.dispatch(...)`, consumes the SSE stream, and pipes `narration` + `response_chunk` text to the splitter (Epic 3) just like Talker output. `subagent_*` events update an internal "thinking" indicator (no audio impact in v1).

**Given** the splitter sees a slow-path stream,
**When** segments emit,
**Then** they flow to TTS + publisher exactly like fast-path segments (single-fan-out architectural constraint preserved — splitter doesn't know or care about source).

**Given** an orchestrator stream that ends without a `turn_end` event,
**When** the SSE connection closes,
**Then** the pipeline flushes the splitter (any pending text is segmented + sent to TTS) and waits for the last audio frame before transitioning lifecycle SPEAKING → IDLE (FR14).

**Given** an orchestrator stream that includes `turn_end`,
**When** received,
**Then** the splitter drains immediately and the lifecycle transitions normally on last-frame.

**Given** an integration test in `tests/integration/test_complex_turn.py` (PRD Journey 2),
**When** the pipeline runs with mocked orchestrator emitting the full event sequence (`narration → subagent_started → subagent_progress → subagent_done → response_chunk × N → turn_end`),
**Then** end-of-speech → first narration audio frame is measured for 30 simulated turns and p95 is recorded as the **NFR2 baseline** (≤1000ms target).

**Given** an integration test for missing-`turn_end`,
**When** the mock orchestrator drops the `turn_end` event after the last `response_chunk`,
**Then** the pipeline still completes the turn (splitter flushes, lifecycle transitions to IDLE after last frame), and a WARN log `orchestrator.missing_turn_end` is emitted.

**Given** the redaction discipline,
**When** Epic 4's integration tests run,
**Then** orchestrator stream content is not logged at INFO level (raw response chunks contain LLM text — treated like transcripts; gated to DEBUG only).

**Given** v1 fail-fast,
**When** the orchestrator returns 5xx or the stream stalls indefinitely (no events for >60s connection-level timeout),
**Then** the pipeline raises `OrchestratorError` and crashes (filler-response/heartbeat resilience is FR13/NFR20 — deferred to v2).

---

## Epic 5: Production Hardening

**Goal:** Make OLAF interruptible, hot-tunable, and durable. Barge-in halts playback and flushes; SIGHUP swaps `expression_map.yaml` atomically with mid-utterance defer; systemd manages the service; logs rotate; LAN orchestrator without a shared secret refuses to start; 7-day soak validates wake-word thresholds against real household ambient.

### Story 5.1: Barge-in — VAD-during-SPEAKING + SPEAKING→LISTENING bypass + flush

As Kamal,
I want OLAF to stop speaking when I interrupt mid-response, flush in-flight expression events so embodiment doesn't get stuck on a half-finished pose, and cancel any active orchestrator stream — without false-firing on OLAF's own audio bleed,
So that natural conversation feels like talking to a person, not a voice assistant that won't shut up.

**Acceptance Criteria:**

**Given** the lifecycle is in SPEAKING state,
**When** the VAD signals voice activity above a sustained-voice threshold (configurable in `setup.toml` `[barge_in] sustained_ms = 250` and `[barge_in] energy_threshold = ...`),
**Then** a barge-in event fires (FR5).

**Given** OLAF's own audio is bleeding into the mic,
**When** transient mic energy spikes that don't sustain past `sustained_ms` occur,
**Then** **no** barge-in fires (false-positive guard per PRD Journey 3 failure-mode + NFR12 reasoning).

**Given** a barge-in event fires,
**When** the lifecycle machine receives it,
**Then** it transitions SPEAKING → LISTENING **directly**, bypassing THINKING (FR29). The transition publishes a `LifecycleEvent`.

**Given** Cartesia is mid-stream when barge-in fires,
**When** the barge-in handler runs,
**Then** Cartesia playback halts immediately, remaining audio frames in the buffer are discarded, and the splitter state flushes — any unpublished `OlafAction`/`ExpressionEvent` is **dropped, not published**, so the consumer doesn't get stuck mid-pose (FR30).

**Given** an orchestrator stream is active when barge-in fires,
**When** the handler runs,
**Then** `OrchestratorClient.cancel(session_id)` is invoked (Story 4.2's stub becomes real here), issuing `HTTP DELETE /turn/{session_id}` to release the orchestrator's resources.

**Given** the splitter has just published an `ExpressionEvent` whose corresponding audio frame has not yet played,
**When** barge-in fires,
**Then** the published event is the truth — the consumer holds that pose until next event (PRD Journey 3 contract).

**Given** an integration test in `tests/integration/test_barge_in.py` (PRD Journey 3),
**When** the test simulates: trigger turn → mid-SPEAKING fire VAD voice → assert,
**Then** lifecycle goes SPEAKING → LISTENING (no THINKING), audio frames stop, splitter is empty, `cancel()` was invoked, and a new utterance after the barge-in is dispatched normally.

**Given** PRD's "open in v1" note on barge-in tuning,
**When** Story 5.1 lands,
**Then** the configurable thresholds are exposed in `setup.toml`, defaults are set conservatively (favor false-negatives over false-positives initially), and a comment in the config notes that final tuning lives in Story 5.5's soak.

---

### Story 5.2: SIGHUP atomic swap of `expression_map.yaml` + mid-utterance defer

As Kamal,
I want to edit `expression_map.yaml` and apply changes without restarting the pipeline mid-session, with mid-utterance reloads deferred until the current turn completes and validation failures retaining the prior mapping,
So that I can tune emotion poses live during real conversation without breaking the running session.

**Acceptance Criteria:**

**Given** the pipeline is running and `__main__.py` has installed a SIGHUP handler,
**When** I send `kill -HUP <pid>` (or run `just reload`, which does `kill -HUP $(pgrep -f voice_agent_pipeline)`),
**Then** the handler dispatches to `config/expression_map.py` for an atomic swap (FR32).

**Given** the new `expression_map.yaml` is valid,
**When** the reload runs,
**Then** the in-memory `ExpressionMapConfig` is replaced atomically (the resolver in `splitter/mapping.py` reads via a single reference that's swapped under a lock); the next turn that emits an emotion uses the new mapping.

**Given** the new `expression_map.yaml` is malformed (bad schema, unknown extra key, missing payload, incompatible `schema_version`),
**When** the reload runs,
**Then** validation rejects, the **prior mapping is retained** (no silent-broken state), and a clear error is logged at WARN with the line number and key path.

**Given** a SIGHUP arrives mid-utterance (lifecycle in SPEAKING with audio frames still flowing),
**When** the handler runs,
**Then** the reload is **deferred** — queued — and applied after the current turn completes (lifecycle returns to IDLE) (FR33). A `config.reload.deferred` INFO log fires.

**Given** NFR7 (≤1s SIGHUP reload),
**When** measured on the dev host (not mid-utterance),
**Then** signal-receipt → reload-complete is < 1 second at p95 over 30 reloads.

**Given** the architecture's open question on router-rule hot-reload,
**When** Story 5.2 lands,
**Then** `[router]` patterns are **also** included in the SIGHUP swap (per architecture's "lean yes, config-only extensibility theme" — extends FR32 to cover router patterns); `setup.toml` non-router/non-mapping fields still require restart.

**Given** an integration test in `tests/integration/test_sighup_reload.py` (PRD Journey 5),
**When** the test runs the pipeline, edits `expression_map.yaml` with a valid change, sends SIGHUP, then triggers a turn,
**Then** the new mapping is in effect for that turn.
**And** when the same test sends SIGHUP with a malformed file, **then** the old mapping persists and an error is logged.
**And** when the same test sends SIGHUP mid-utterance, **then** the reload defers until the turn ends and only then takes effect.

---

### Story 5.3: Security & config hardening — LAN orchestrator rule + log-rotation config + schema-version contract test

As Kamal,
I want the pipeline to refuse to start with an insecure orchestrator config, log retention to be configurable not just defaulted, and a contract test proving every config file and event schema rejects incompatible `schema_version` consistently,
So that v1 ships with no surprise security holes or undefended schema drift.

**Acceptance Criteria:**

**Given** `setup.toml` `[daemon] url`,
**When** the URL host is `localhost`, `127.0.0.1`, or `::1`,
**Then** the pipeline starts without requiring a shared secret (default localhost-only posture).

**Given** the URL host is **not** localhost (any LAN address),
**When** the pipeline starts,
**Then** it requires either `[daemon] bearer_token_env = "..."` (referencing a key in `.env`) or `[daemon] mtls = { cert_path = "...", key_path = "..." }`. If neither is set, startup raises `StartupValidationError` with a clear message: `"non-localhost daemon URL requires bearer_token_env or mtls config"` and exits non-zero (FR35).

**Given** `[daemon] bearer_token_env = "DAEMON_BEARER_TOKEN"`,
**When** the orchestrator client is invoked,
**Then** every request includes `Authorization: Bearer <token>` from `.env`; the token is loaded only at startup and never logged (NFR23, NFR25).

**Given** `setup.toml` `[logging]` block,
**When** I inspect it,
**Then** it exposes `max_file_size_mb` (default 50), `retention_days` (default 7), `console_mirror` (default false) — all configurable, satisfying FR40's "configurable retention window."

**Given** the structlog + RotatingFileHandler setup from Story 1.3,
**When** the new `[logging]` config values are applied,
**Then** `RotatingFileHandler` uses them for `maxBytes` and `backupCount` (with retention computed from the largest expected log volume per day).

**Given** a contract test in `tests/contract/test_schema_version.py`,
**When** the test loads `setup.toml`, `expression_map.yaml`, `ExpressionEvent`, `LifecycleEvent`, and `OrchestratorStreamEvent` each with an unsupported `schema_version`,
**Then** every load/parse raises `SchemaVersionError` with a message naming the file/type, the file's version, and the supported version (NFR27 final enforcement — proves the contract holds across **all** schema-versioned surfaces, not just the ones already tested epic-by-epic).

**Given** an unit test for the LAN-orchestrator rule,
**When** invalid configurations are loaded (LAN URL with no bearer/mTLS, malformed bearer env reference, mTLS path not readable),
**Then** each raises `StartupValidationError` with the right message.

---

### Story 5.4: systemd service deployment

As Kamal,
I want a committed systemd unit that manages the pipeline as a service with restart-on-failure, plus deployment instructions,
So that I can install once and have OLAF run on boot, restart on crash, and survive long stretches without manual intervention.

**Acceptance Criteria:**

**Given** `deploy/systemd/voice-agent-pipeline.service`,
**When** I inspect it,
**Then** the unit declares `Type=simple`, `Restart=on-failure`, `RestartSec=5`, `StartLimitInterval=60`, `StartLimitBurst=5`, `WorkingDirectory=<install-path>`, `User=<dev>`, `ExecStart=<venv>/bin/python -m voice_agent_pipeline`, with no `EnvironmentFile` directive (the app loads `.env` itself via pydantic-settings — architecture decision in Batch 5).

**Given** systemd's standard journal,
**When** the service is running,
**Then** only systemd lifecycle messages (start/stop/crash) hit journald — application logs continue to land in `./logs/voice-agent.log` per Story 1.3 architecture.

**Given** the service is running and the pipeline crashes,
**When** the crash occurs,
**Then** systemd restarts within 5 seconds; if 5 crashes happen within 60s, systemd stops trying (StartLimitBurst). A CRITICAL log fires at the crash boundary identifying the failing dependency (FR36).

**Given** `deploy/README.md`,
**When** I open it,
**Then** it contains the step-by-step install procedure from architecture's "Deployment to host" section: clone path, `uv sync`, `cp .env.example .env` + chmod 0600 + fill keys, train wake-word `.ppn`, copy systemd unit, daemon-reload + enable + start, log locations.

**Given** the service is enabled,
**When** the host reboots,
**Then** the pipeline starts automatically (`enable --now` semantics). The first successful boot is logged as `service.boot.completed` with the resolved versions of key dependencies (Pipecat, faster-whisper, Cartesia SDK, etc.) for forensic clarity.

**Given** SIGTERM (sent by `systemctl stop`),
**When** received,
**Then** the pipeline drains in-flight Cartesia frames (Story 2.5 contract), closes httpx clients, calls `Ros2ExpressionPublisher.disconnect()`, and exits 0.

**Given** an integration test in `tests/integration/test_systemd_lifecycle.py` (manual on the dev host, not CI),
**When** the test exercises start → trigger turn → SIGTERM → restart → trigger turn,
**Then** both turns complete cleanly and the service journal shows expected lifecycle messages.

---

### Story 5.5: 7-day soak + wake-word threshold tuning + v1 sign-off

As Kamal,
I want a continuous 7-day run on the dev host under real household ambient with wake-word thresholds tuned against actual conditions, USB hot-plug survival verified, and a final v1 sign-off checklist,
So that I can declare voice-agent-pipeline v1 done with confidence and cut the release.

**Acceptance Criteria:**

**Given** the pipeline running under systemd on the dev host,
**When** it runs continuously for ≥ 7 days under normal household conditions,
**Then** there are no unplanned restarts, panics, or unrecoverable error states across the run (NFR8). systemd restart count == 0 (or each restart is investigated and root-caused).

**Given** the soak period,
**When** wake-word false positives are counted (ambient TV + conversation + kitchen),
**Then** the rate is ≤ 1 per hour at the configured production threshold (NFR12). If above, threshold is raised; if at near-zero with low recall, threshold lowers; either way the final value is committed to `setup.toml`.

**Given** intentional wake-word attempts during the soak,
**When** false-negative rate is measured (≥100 attempts across varied speaking conditions),
**Then** the rate is ≤ 5% at the production threshold (NFR13). Final value committed.

**Given** USB hot-plug events during the soak (intentional or incidental — keyboard, mouse, drive),
**When** they occur on devices unrelated to the pinned mic/speaker,
**Then** the pipeline does not restart and audio capture/playback continue uninterrupted (NFR11).

**Given** the soak's full log corpus,
**When** I inspect it,
**Then** no log line contains raw audio bytes, raw credentials, or transcripts at INFO+ level (NFR25 final audit).
**And** total log volume per day is verified against NFR18-equivalent budget on the dev host (note: NFR18 is Pi-specific and v2-deferred; on the dev host we simply verify no runaway growth).

**Given** the soak completes,
**When** the v1 sign-off checklist is run,
**Then** all of these pass:
1. **All 5 PRD measurable outcomes** for v1 hold over a 30-min representative slice of the soak: no voice/expression drift, no unhandled tag freeze, wake-word reliable, no audio cutout >100ms (NFR6), no latency target missed by >20%.
2. **NFR1** (simple-turn ≤1500ms p95) verified against soak traffic.
3. **NFR2** (complex-turn ≤1000ms p95) verified against soak traffic.
4. **NFR4** (Cartesia TTS ≤400ms p95) verified.
5. **NFR5** (voice/embodiment 30-80ms anticipatory p95) verified.
6. **NFR8** (7-day continuous uptime) verified — this story's primary contract.
7. **NFR12, NFR13** (wake-word FP/FN) verified at final threshold.
8. **NFR26** (spec-as-contract) — PRD/architecture have been updated wherever implementation deviated; the spec-drift list (added in Story 4.2) is empty or has documented rationale for each open item.

**Given** the sign-off completes,
**When** I cut the v1 release tag,
**Then** the release notes summarize: epics shipped, NFR results table (target vs measured), known v2-deferred items (Hailo port, resilience layer, Pi resource calibration), and any architecture deviations. The v1 spec docs (PRD, brief, distillate, architecture) are tagged at the same commit.

---

## Cross-Cutting NFRs

These NFRs aren't owned by a single epic — they're enforced as architectural properties from Epic 1 onward and re-validated each sprint:

- **NFR23–NFR25** (security & redaction): logging/redaction land Epic 1; new credentials added in Epic 2 inherit the `.env` pattern.
- **NFR26** (spec-as-contract): every epic must update PRD/architecture if it deviates.
- **NFR28** (testability): every Epic adds tests at the Protocol-mock seam in `tests/unit/`, plus integration tests covering the journey unlocked.
- **NFR29** (JSON logs): established Epic 1, immutable thereafter.

