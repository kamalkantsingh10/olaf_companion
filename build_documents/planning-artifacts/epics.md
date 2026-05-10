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
editsCompleted:
  - edit-2026-05-06-direction-shift-correct-course
  - edit-2026-05-10-speech-emotion-boundary-repair
inputDocuments:
  - build_documents/planning-artifacts/prd.md
  - build_documents/planning-artifacts/voice-agent-pipeline-brief.md
  - build_documents/planning-artifacts/voice-agent-pipeline.md
  - build_documents/planning-artifacts/architecture.md
scope: v1-active-set-only
deferredToV1_5:
  - FR5, FR29, FR30 (barge-in cluster — VAD-during-SPEAKING + flush + cancel; v1.5 headline feature)
  - Cross-restart mood persistence (additive under FR48)
  - Expanded `working` sub-modes — `searching`, `tooling`, `composing` (additive Literal extension)
  - Configurable idle auto-sleep fallback (additive opt-in safety net)
deferredToV2:
  - FR7, FR41 (Hailo-8L acceleration + driver verification — Pi port)
  - FR13, FR16 (orchestrator stall filler + Cartesia text-only degraded mode — resilience layer)
  - NFR9, NFR19, NFR20, NFR22 (recovery, retry/backoff, stall heartbeat, Talker→orchestrator failover — resilience layer)
  - NFR14, NFR15, NFR16, NFR17, NFR18 (Pi 5 resource calibration — Pi port)
approachGuidance: |
  Per Kamal: go super simple 1st and progressively add complexity in each sprint.
  Start with the leanest possible epic that proves the core flow end-to-end,
  then layer complexity sprint by sprint.
lastEdited: '2026-05-10'
editHistory:
  - date: '2026-05-06'
    summary: |
      `bmad-correct-course` ran against the spec-triple direction shift
      committed in 6f3bfe3 (PRD/brief/distillate) + ed8b276 (architecture).
      Restructured Epic 3 (5 → 7 stories) for four-topic event publish +
      mood module + event-schema rebuild; Epic 4 retitled "Activity FSM +
      Tool-Use + Slow Path" (5 → 7 stories) for 7-state activity FSM with
      deferred-sleep, Talker tool-using (`go_to_sleep`, `set_mood`),
      mood-tinted wake greeting, mic-mode flip; Epic 5 (5 → 4 stories,
      barge-in moved to v1.5 backlog, sign-off list expanded for NFR30/31/32
      + intent-sleep FP/FN + mood cadence). Net v1 story count: 22 → 30
      across 5 epics. New `## v1.5 Backlog (Post-v1)` section captures
      4 deferred items. Epic 1 + Epic 2 (both at `review` status) untouched.
  - date: '2026-05-10'
    summary: |
      Boundary-repair edit (sprint-change-proposal-2026-05-10).
      `SpeechEmotionPayload.expression_data: dict[str, Any]` removed
      from the wire — OLAF-renderer vocabulary that violated the
      consumer-agnostic publisher boundary. ACs in Stories 3.1, 3.2,
      and 3.4 amended in-place to drop `expression_data` references;
      the YAML's `emotions:` block reshapes from a mapping to a list of
      canonical names. `schema_version` bumped 2 → 3 in lockstep with
      `setup.toml`, `expression_map.yaml`, and `EventEnvelope`. Bundled:
      two new vocalization tags `nod` and `shake` (with
      `tts_supported: false`) — gesture cues for affirmatives /
      negatives, with the matching Talker prompt update. No story
      renumber, no epic restructure; impl-artifacts story specs
      (frozen, post-execution) untouched — the sprint change proposal
      is the audit record. FR20 / FR53 / NFR27 narratives carried
      through.
---

# olaf_companion — voice-agent-pipeline — Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for the **voice-agent-pipeline** component of `olaf_companion`, decomposing the requirements from the PRD, brief, distillate, and Architecture documents into implementable stories.

**Scope:** 5 epics, 30 v1 stories. Epic 1 + 2 (12 stories) are complete; Epic 3 + 4 + 5 (18 stories) remain. v1.5-deferred items (barge-in, cross-restart mood persistence, expanded `working` sub-modes, idle auto-sleep fallback) are captured in `## v1.5 Backlog (Post-v1)` for traceability but do not produce v1 stories. v2-deferred FRs/NFRs (resilience layer, Pi/Hailo port) are tracked in frontmatter.

**Approach:** lean-first, then progressive complexity. Each sprint adds one new capability layer on top of a runnable artifact from the prior sprint.

**Current state (as of 2026-05-06):** Epic 1 (7 stories, 1.1–1.7) + Epic 2 (5 stories, 2.1–2.5) at `review` status — simple-turn loop alive end-to-end through Story 2.5. Remaining work: Epic 3 (Embodiment Channel — four-topic events + mood), Epic 4 (Activity FSM + Tool-Use + Slow Path), Epic 5 (Production Hardening). The 2026-05-06 direction shift (commits `6f3bfe3` + `ed8b276`) reshaped Epics 3–5 substantially before any of their stories had started.

## Requirements Inventory

### Functional Requirements

> v1 active set: 49 FRs across 11 clusters. v1.5-deferred FRs (FR5, FR29, FR30) are listed below with their target epic but produce v1.5 backlog stories. v2-deferred FRs (FR7, FR13, FR16, FR41) and removed FR (FR28 — old IDLE state) are intentionally omitted.

**Audio I/O & Capture**

- **FR1**: The pipeline can detect a configurable wake-word from continuous mic input without dispatching downstream processing prior to detection.
- **FR2**: The pipeline can capture user speech from the local mic device after wake-word detection, terminating capture on voice-activity end-of-speech.
- **FR3**: The pipeline can play synthesized audio through the local speaker device with no perceivable buffering pause between frames.
- **FR4**: The pipeline can pin audio devices by stable name in configuration, surviving reboots and USB hot-plug events of unrelated devices.
- **FR5**: *(deferred to v1.5)* The pipeline can detect mid-utterance barge-in (user speaking during SPEAKING activity state) and abort current playback.

**Speech Recognition**

- **FR6**: The pipeline can transcribe user speech to text on-device, without transmitting audio to a cloud service.
- **FR8**: The pipeline can attach a confidence score to each transcript and route low-confidence transcripts to a clarification path.

**Conversational Intelligence**

- **FR9**: The pipeline can route a transcribed user turn to the Talker fast-path or to the orchestrator daemon based on a configurable routing decision.
- **FR10**: The pipeline can read belief state from the orchestrator daemon via HTTP API to inform Talker responses.
- **FR11**: The pipeline can dispatch a user turn to the orchestrator daemon via `POST /turn` and consume the typed event stream (narration, subagent events, response chunks, turn_end).
- **FR12**: The pipeline can synthesize a fast-path response from belief state using an in-pipeline LLM (Talker), emitting Cartesia-tagged text. Talker operates in two modes: **conversational** (spoken reply, may emit tool-calls in parallel) and **greeting** (2–8 word mood-tinted wake greeting).
- **FR13** *(deferred to v2)*: Stall heartbeat / filler response on slow orchestrator stream — resilience layer.
- **FR14**: The pipeline can recover gracefully from a missing `turn_end` event by flushing the splitter and transitioning activity FSM after the last audio frame plays.

**Voice Synthesis**

- **FR15**: The pipeline can stream Cartesia-tagged text to Cartesia Sonic-3 and receive audio frames in response.
- **FR17**: The pipeline can use a configurable Cartesia voice ID and default emotion.

**Embodiment Expression**

- **FR18**: The pipeline can parse incoming text streams as Cartesia SSML, identifying `<emotion value="X"/>` tags and `[vocalization]` events incrementally (token-by-token, tags may split across token boundaries).
- **FR19**: The pipeline can segment text on whichever boundary comes first: sentence terminator, emotion tag, or vocalization tag.
- **FR20**: The pipeline can map every Cartesia emotion tag to a defined `SpeechEmotionEvent` via the `expression_map.yaml` mapping table, with no silent gaps. The payload carries both `emotion` (resolved name) and `raw_tag` + `resolved_fallback` (audit trail).
- **FR21**: The pipeline can resolve unmapped emotion tags through a fallback family table, producing a defined `SpeechEmotionEvent` with a logged warning.
- **FR22**: The pipeline can attach `SpeechEmotionEvent` and `VocalizationEvent` metadata to the matching Cartesia audio frame, ensuring audio-anchored events publish in lockstep with audio.
- **FR23**: The pipeline can publish `SpeechEmotionEvent` and `VocalizationEvent` to the configured `/olaf/speech_emotion` and `/olaf/vocalization` topics, anchored to audio frame send time, achieving 30-80ms anticipatory alignment with voice.
- **FR24**: The pipeline can suppress republishing of unchanged base emotions via a "last published" cache scoped per-turn, while always publishing vocalization events.
- **FR25**: The pipeline can strip Cartesia-unsupported vocalization tags from the TTS stream while still publishing them as `VocalizationEvent` to the broadcast channel.

**Activity FSM (lifecycle)**

- **FR26**: The pipeline can publish `ActivityEvent` (`starting, sleeping, waking, listening, working, speaking, going_to_sleep`) to the configured `/olaf/activity` topic on every state transition. The `working` state has v1 sub-modes `thinking` (Talker reasoning in-pipeline) and `delegating` (orchestrator dispatched, awaiting response).
- **FR27**: The pipeline can transition between activity states based on observable events: wake-word detection (`sleeping → waking`), end-of-speech (`listening → working`), first audio frame (`working → speaking`), last audio frame (`speaking → listening` or `speaking → going_to_sleep` on deferred sleep), Talker `go_to_sleep()` tool-call.
- **FR28** *(REMOVED)*: ~~The pipeline can transition from IDLE to SLEEPING after a configurable idle timeout.~~ Idle auto-sleep removed in 2026-05-06 direction shift; sleep is intent-only via Talker `go_to_sleep()` tool. Optional configurable idle auto-sleep is v1.5 backlog.
- **FR29** *(deferred to v1.5)*: The pipeline can transition from `speaking → listening` directly on barge-in detection, bypassing the deferred-sleep path.
- **FR30** *(deferred to v1.5)*: The pipeline can flush in-flight `speech_emotion` + `vocalization` events on barge-in to prevent the consumer being stuck on a half-finished pose.

**Wake/Sleep & Tool-Use** *(new in 2026-05-06 direction shift)*

- **FR44**: The pipeline can generate a 2–8 word mood-tinted wake greeting via Talker greeting-mode on every `sleeping → waking` transition. Greeting must complete within 800 ms; on timeout/error/overlong-response, fall back to a static list (`["hey", "yeah?", "hi"]` default).
- **FR45**: The pipeline can expose a Talker tool registry (`go_to_sleep`, `set_mood`) with typed Pydantic input schemas validated before execution. Invalid tool calls log WARN and are dropped without side effects. Tool calls execute concurrently with text emission (text-first ordering preserved).
- **FR46**: The pipeline can defer the `speaking → going_to_sleep → sleeping` transition until after the acknowledgement audio finishes when Talker fires `go_to_sleep()`, so the goodbye is heard before the mic mode flips back to wake-word-only.
- **FR47**: The pipeline can keep the mic stream continuously open while AWAKE — wake-word fires only on `sleeping → waking` transition. The audio transport flips between `wake_word_only` mode (Porcupine engaged, VAD/STT suspended) and `vad_stt` mode (VAD + STT engaged, Porcupine suspended) on FSM mic-mode signals. Single audio source; no parallel-listener architecture.

**Mood Control** *(new in 2026-05-06 direction shift)*

- **FR48**: The pipeline can maintain a discrete mood state (~6–8 enum values) updated by Talker via the `set_mood(mood)` tool. Initial mood at startup is `"calm"`. v1 lifetime is single-process (cross-restart persistence is v1.5 backlog).
- **FR49**: The pipeline can enforce a mood publish cooldown of ≤ 4 publishes per hour (NFR31) at the `MoodController.set()` boundary. Over-rate `set_mood` calls drop with WARN; in-process mood state is updated only on successful publish.
- **FR50**: The pipeline can publish `MoodEvent` to the configured `/olaf/mood` topic with latched / transient_local QoS, so subscribers learn the current mood immediately on connect.

**Event Publishing & Channels** *(new in 2026-05-06 direction shift)*

- **FR51**: The pipeline can publish on four typed ROS 2 topics (`/olaf/mood`, `/olaf/activity`, `/olaf/speech_emotion`, `/olaf/vocalization`), each with the appropriate QoS profile (latched/transient_local for `mood` + `activity`; volatile depth=8 for `speech_emotion` + `vocalization`).
- **FR52**: The pipeline can serialize every event with a common `EventEnvelope` carrying `timestamp` (UTC ISO8601), `schema_version` (currently 3), `source` (`"voice_agent_pipeline"`), `correlation_id` (UUID), and `payload` (topic-specific Pydantic model). Per CLAUDE.md rule 6: bump `schema_version` only on breaking changes; additive field changes are forward-compat.
- **FR53**: The pipeline can produce events with `schema_version=3`. Bump history: `1 → 2` (Story 3.4 — single `/olaf/expression` channel split into four topics); `2 → 3` (sprint-change-proposal-2026-05-10 — `SpeechEmotionPayload.expression_data` removed; consumer-agnostic boundary repair).

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

> v1 active set: 23 NFRs. v2-deferred NFRs (NFR9, NFR14–NFR20, NFR22) are intentionally omitted.

**Performance**

- **NFR1**: Simple-turn end-to-end latency (end-of-speech → first audio frame from Cartesia) must be ≤ 1500ms at p95 over a 30-min soak.
- **NFR2**: Complex-turn end-to-end latency (end-of-speech → first narration audio frame) must be ≤ 1000ms at p95.
- **NFR3**: On-device STT latency (end-of-speech → transcript ready) must be ≤ 500ms at p95. v1 measures on host CPU/GPU (faster-whisper); Hailo-accelerated p95 is a v2 target.
- **NFR4**: Cartesia TTS latency (text-with-tags → first audio frame) must be ≤ 400ms at p95.
- **NFR5**: Voice / `speech_emotion` alignment must be 30–80ms anticipatory at p95; outside this window is a defect. (`mood` and `activity` events are FSM-driven, not audio-anchored — no alignment requirement.)
- **NFR6**: Audio playback must not introduce buffering pauses > 100ms during a single utterance.
- **NFR7**: `SIGHUP`-triggered config reload must complete within 1 second from signal receipt.
- **NFR30**: Wake-greeting end-to-end latency (`sleeping → waking` transition → first audio frame of greeting) must be ≤ 800 ms at p95, including fallback to the static list when Talker greeting mode times out / errors / over-runs the 8-word cap.
- **NFR32**: Talker tool-call dispatch overhead (Talker emit `tool_calls` → side-effect-complete in `ActivityFSM` / `MoodController`) must not block text emission; dispatch p95 < 50 ms measured against in-process side effects.

**Reliability**

- **NFR8**: Pipeline must run continuously for ≥ 7 days under normal household ambient conditions without an unplanned restart, panic, or unrecoverable error state.
- **NFR10**: A malformed config file at startup must produce a clear error and prevent startup; a malformed config on `SIGHUP` must produce a clear error and retain the prior config (no silent-broken state).
- **NFR11**: Pipeline must survive USB hot-plug events on unrelated devices without restart or audio interruption.
- **NFR12**: Wake-word false-positive rate must be ≤ 1 per hour of typical household ambient (TV, conversation, kitchen sounds) at the production threshold.
- **NFR13**: Wake-word false-negative rate must be ≤ 5% in normal speaking conditions at the production threshold.

**Mood Cadence**

- **NFR31**: `MoodEvent` publishes must occur at a rate ≤ 4 per hour (sliding 60-minute window) per process. Cooldown is enforced at the `MoodController.set()` boundary; over-rate `set_mood` calls drop with WARN. In-process mood state is not updated until the publish succeeds.

**Integration Reliability**

- **NFR21**: Broadcast publishing on all four configured topics (`mood`, `activity`, `speech_emotion`, `vocalization`) must use reliable delivery (RELIABLE QoS for ROS 2 / DDS in v1) with per-topic QoS profiles: latched / transient_local for `mood` + `activity`; volatile depth=8 for `speech_emotion` + `vocalization`.

**Security**

- **NFR23**: All API credentials must be stored at file permission `0600` and loaded from disk only at process startup; the process must not re-read or expose them at runtime.
- **NFR24**: Outbound HTTPS connections (Cartesia, Anthropic) must validate TLS certificates; the pipeline must refuse to start if certificate validation is disabled.
- **NFR25**: All log output must be inspectable by Kamal locally; no log line may contain raw credential material, raw audio bytes, or (at INFO level or above) user transcripts.

**Maintainability**

- **NFR26**: PRD, brief, distillate, and architecture are the canonical specs (the four-document set governed by CLAUDE.md rule 9). Any implementation decision deviating from them must update the relevant document(s) in the same change.
- **NFR27**: Configuration schemas (`expression_map.yaml`, `setup.toml`) and event schemas (`MoodEvent`, `ActivityEvent`, `SpeechEmotionEvent`, `VocalizationEvent` — all sharing the common `EventEnvelope`) must be versioned with an integer `schema_version` field; the pipeline must reject incompatible versions at startup. Current event schema version is **3**. Bump history: `1 → 2` (Story 3.4 topology change); `2 → 3` (sprint-change-proposal-2026-05-10 boundary repair, removing `SpeechEmotionPayload.expression_data`).
- **NFR28**: Components within the pipeline (wake-word, STT, Talker, splitter, TTS, publisher) must be independently testable — each can be exercised in isolation with mock or synthetic inputs at its Protocol seam.
- **NFR29**: Logs must be machine-readable JSON to enable post-hoc analysis without manual parsing.

### Additional Requirements

> Sourced from `architecture.md`. These are technical/infrastructure requirements implied by the architectural decisions that don't appear as numbered FRs/NFRs in the PRD but must produce stories or be embedded into Story 1.1.

**Project bootstrap (Architecture §"Selected Starter" + §"First Implementation Priority"):**

- Initialize project via `uv init voice-agent-pipeline --python 3.12` with the documented dependency set: `pipecat-ai[local]`, `openai` (provider-agnostic Talker via openai-compatible endpoints — Story 2.2), `cartesia`, `httpx`, `httpx-sse`, `pvporcupine`, `faster-whisper`, `pydantic`, `pydantic-settings`, `structlog`, dev: `ruff`, `pyright`, `pytest`, `pytest-asyncio`.
- `rclpy` is installed via system ROS 2 distro (e.g., `ros-jazzy-rclpy`), exposed to the venv via PYTHONPATH; documented in README.
- Project layout follows the documented module-by-domain tree (`src/voice_agent_pipeline/{audio,stt,turn,tts,splitter,publisher,activity,mood,config,logging,schemas}` + `tests/{unit,integration,contract}`). The `lifecycle/` package is renamed to `activity/` in Story 4.3; the `mood/` package and `turn/tools.py` module are added in Stories 3.6 and 4.4 respectively.
- Committed root files: `pyproject.toml`, `uv.lock`, `justfile`, `setup.toml`, `expression_map.yaml`, `.env.example`, `.gitignore`, `README.md`, `CLAUDE.md`, `.python-version`.
- Gitignored: `.env`, `./logs/`, `.venv/`.
- `justfile` recipes: `run`, `check`, `test`, `reload`, `lint`, `format`.

**Type/style discipline (Architecture §"Implementation Patterns"):**

- `snake_case` everywhere keys are written (Python, TOML, YAML, JSON payload, DDS field names, log keys).
- `typing.Protocol` for interfaces (no `abc.ABC`); pydantic v2 `BaseModel` for events/config/data; `typing.Literal` for fixed string sets (no `enum.Enum`); `@dataclass(frozen=True)` only for internal trivial structs.
- `pyright` strict for `src/`, basic for `tests/`. No `Any` outside the documented `payload: dict[str, Any]` extensibility seam.
- Custom exception hierarchy in `errors.py` (`VoiceAgentError` root → `ConfigError`, `SchemaVersionError`, `StartupValidationError`, `ExternalServiceError` + subclasses (`CartesiaError`, `TalkerError`, `OrchestratorError`), `PublisherError`, `SplitterError`).
- `just check` runs ruff + pyright + `pytest tests/unit`; AI partner runs this pre-commit; failures block.

**Wake-word asset (Architecture §"Audio + STT Pipeline"):**

- Custom Picovoice Porcupine wake-word phrase trained via Picovoice console; `.ppn` file committed to `models/wakeword/hey_olaf.ppn`.
- `PICOVOICE_ACCESS_KEY` added to `.env.example` and validated at startup alongside `CARTESIA_API_KEY` and the active Talker provider key (one of `OPENAI_API_KEY` / `GROQ_API_KEY` / `GEMINI_API_KEY` per `[talker] provider`).

**v1 fail-fast posture (Architecture §"V1 Posture"):**

- Startup validates: Cartesia API key + reachable, active Talker provider's API key + reachable (one of OpenAI / Groq / Gemini per `setup.toml`), Picovoice access key valid, orchestrator daemon reachable + `GET /health` 200, `EventPublisher` initialized with all four topic publishers (`mood`, `activity`, `speech_emotion`, `vocalization`), audio devices resolvable by name, Talker tool registry loadable + validates against typed Pydantic input schemas.
- Any failure → process refuses to start with a clear error.
- At runtime, external-service failures crash the process; systemd restarts. **No retry, no in-process recovery, no partial-mode fallbacks in v1.**

**Architectural seams (Architecture §"Internal seams"):**

- Seven Protocol seams must be defined: `STTBackend`, `TalkerClient`, `ToolRegistry`, `OrchestratorClient`, `BeliefStateClient`, `TTSClient`, `EventPublisher`.
- Each external library is imported in **exactly one file** (boundary concentration rule).
- v1 ships **two** `EventPublisher` implementations: `Ros2EventPublisher` (production; four publishers + per-topic QoS + `std_msgs/String` + JSON-encoded `EventEnvelope`) and `LogEventPublisher` (in-memory adapter for tests + pre-Epic-3 dev). No custom ROS 2 `.msg` package in v1.

**Event schemas (Architecture §"Publisher Contract + Event Schemas"):**

- `EventEnvelope` mixin (frozen pydantic v2): `schema_version: int = 3`, `timestamp: datetime` (UTC ISO8601 on the wire), `source: Literal["voice_agent_pipeline"]`, `correlation_id: UUID`, `payload: <topic-specific BaseModel>`.
- `MoodEvent` — `payload: MoodPayload(mood: Mood, reason: str | None)`. `Mood` is a `Literal[...]` of 6–8 values defined in `mood/state.py`.
- `ActivityEvent` — `payload: ActivityPayload(state: ActivityState, working_submode: WorkingSubmode | None, transition_reason: str | None, from_state: ActivityState | None)`. `ActivityState` is the 7-value Literal; `WorkingSubmode = Literal["thinking", "delegating"]`.
- `SpeechEmotionEvent` — `payload: SpeechEmotionPayload(emotion: str, source_tag: str, audio_frame_id: str | None, raw_tag: str, resolved_fallback: str | None)`. Resolved canonical name + audit trail; embodiment vocabulary is consumer-side, keyed on `payload.emotion` (schema-3 boundary repair removed the prior `expression_data: dict[str, Any]` field).
- `VocalizationEvent` — `payload: VocalizationPayload(tag: str, audio_frame_id: str | None, tts_supported: bool)`.
- DDS wire format: `std_msgs/String` per topic, body is the full `EventEnvelope` JSON-encoded as a single string.

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
- `tests/integration/` runs the full Pipecat pipeline with Protocol mocks for external services; covers v1's 7 active PRD journeys (J1 wake-greeting, J2 simple turn, J3 complex turn, J4 intent-sleep, J5 coherent mood, J7 unmapped emotion, J8 SIGHUP). J6 (barge-in) is v1.5 backlog.
- `tests/contract/` verifies pydantic ↔ JSON ↔ DDS round-trip stability and pre-current `schema_version` rejection across all four event types.
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
| FR3 (audio playback, no buffering pause) | Epic 2 | Speaker output wired Epic 2 |
| FR4 (audio devices by stable name) | Epic 1 (mic) → Epic 2 (speaker) | `resolve_audio_devices()` name-regex pattern established Epic 1, extended Epic 2 |
| FR5 (barge-in detection) | **v1.5 backlog** | VAD-during-SPEAKING + sustained-voice threshold; deferred from Epic 5 in 2026-05-06 direction shift |
| FR6 (on-device STT) | Epic 1 | `WhisperBackend` (faster-whisper, CPU/GPU as available) |
| FR8 (transcript confidence + clarification routing) | Epic 1 | Low-confidence escape hatch |
| FR9 (Talker vs orchestrator routing) | Epic 2 (Talker-only) → Epic 4 (slow-path wired) | Routing logic landed in Story 2.4; Epic 4.7 wires the orchestrator branch and removes the `NotImplementedError` stub |
| FR10 (belief-state read) | Epic 4 | `BeliefStateClient` per-turn `GET /beliefs?keys=...`, no cache (Story 4.1) |
| FR11 (orchestrator dispatch + SSE consume) | Epic 4 | `OrchestratorClient` httpx + httpx-sse (Story 4.2) |
| FR12 (Talker emits Cartesia-tagged text + greeting mode) | Epic 2 (basic Talker, no SSML, no tools) → Epic 3 (SSML prompt) → Epic 4 (tool-using + greeting mode + belief grounding) | Progressive enrichment across Stories 2.2 / 3.7 / 4.4 / 4.5 |
| FR14 (missing `turn_end` recovery) | Epic 4 | Splitter flush + activity FSM transition after last frame (Story 4.7) |
| FR15 (Cartesia streaming) | Epic 2 | `CartesiaClient` (Story 2.3) |
| FR17 (configurable voice ID + default emotion) | Epic 2 | `setup.toml` `[tts]` |
| FR18 (streaming SSML parser, token-by-token) | Epic 3 | Hand-rolled state machine ~50–100 LOC (Story 3.3) |
| FR19 (segment on sentence/emotion/vocalization boundary) | Epic 3 | Segmenter emits two distinct event paths (`speech_emotion` + `vocalization`) |
| FR20 (every Cartesia tag mapped, no silent gaps) | Epic 3 | `expression_map.yaml` full primary + secondary + family fallback → `SpeechEmotionPayload` (Stories 3.1 + 3.2) |
| FR21 (unmapped → fallback family) | Epic 3 | Resolver + WARN log (Story 3.2) |
| FR22 (attach speech_emotion + vocalization metadata to audio frame) | Epic 3 | Extend `AudioRawFrame` with two distinct metadata slots (Story 3.7) |
| FR23 (publish on configured speech_emotion + vocalization topics) | Epic 3 | `Ros2EventPublisher` per-topic publishers (Story 3.5 + audio-anchored publish in 3.7) |
| FR24 (last-published cache, dedup base emotions) | Epic 3 | Per-turn scope; vocalization events always publish (Story 3.2) |
| FR25 (strip Cartesia-unsupported vocalization tags from TTS) | Epic 3 | Splitter responsibility (Story 3.3) |
| FR26 (publish `ActivityEvent` on activity topic) | Epic 4 | First time activity FSM is broadcast (Story 4.3) |
| FR27 (activity transitions on observable events) | Epic 4 | 7-state FSM with `working` sub-modes (Story 4.3); deferred-sleep scheduler same story |
| FR28 (~~IDLE → SLEEPING after timeout~~) | **REMOVED** | Idle auto-sleep removed in 2026-05-06 direction shift; sleep is intent-only via Talker `go_to_sleep()` tool |
| FR29 (speaking → listening on barge-in) | **v1.5 backlog** | New transition path; deferred from Epic 5 |
| FR30 (flush in-flight events on barge-in) | **v1.5 backlog** | Splitter flush + DELETE `/turn/{id}`; deferred from Epic 5 |
| FR31 (config schema validation, refuse-to-start on bad) | Epic 1 (`setup.toml` + `.env`) → Epic 3 (`expression_map.yaml` validation) | Pattern established Epic 1 (Story 1.2); extended Epic 3 (Story 3.1) |
| FR32 (SIGHUP atomic swap of `expression_map.yaml`) | Epic 5 | Atomic in-memory swap, rollback on validation fail (Story 5.1) |
| FR33 (defer mid-utterance reload) | Epic 5 | Pair with FR32 (Story 5.1) |
| FR34 (load creds from `.env`, never inlined or logged) | Epic 1 (Picovoice) → Epic 2 (Talker provider key + Cartesia) | Each epic adds the keys it needs |
| FR35 (refuse non-localhost orchestrator without secret) | Epic 5 | Startup validation rule (Story 5.2) |
| FR36 (systemd service, restart-on-failure) | Epic 5 | `deploy/systemd/voice-agent-pipeline.service` (Story 5.3) |
| FR37 (structured JSON logs at INFO/WARN/ERROR) | Epic 1 | Pattern established Story 1.3; events grow per epic |
| FR38 (log unmapped tags w/ fallback) | Epic 3 | DEBUG on first occurrence, WARN if completely unknown (Story 3.2) |
| FR39 (no raw audio in logs, transcripts DEBUG-only) | Epic 1 | Redaction processor + level discipline from Story 1.3 |
| FR40 (log rotation, configurable retention) | Epic 5 | RotatingFileHandler config (Story 5.2) |
| FR42 (no audio/transcript persistence) | Epic 1 | Architectural property, true from day 1 |
| FR43 (no telemetry beyond configured deps) | Epic 1 | Architectural property, true from day 1 |
| FR44 (mood-tinted wake greeting, 800ms timeout, fallback list) | Epic 4 | `talker.greet()` + `activity/greeting.trigger_greeting()` (Story 4.5) |
| FR45 (Talker tool registry — `go_to_sleep`, `set_mood` — typed Pydantic input validation) | Epic 4 | `turn/tools.py` + Talker tool-using upgrade (Story 4.4) |
| FR46 (deferred-sleep transition after last audio frame) | Epic 4 | FSM `sleep_pending` flag + last-frame trigger (Story 4.3 logic + Story 4.4 tool dispatch) |
| FR47 (continuous mic capture while AWAKE; mic-mode flip on FSM signal) | Epic 4 | `audio/transport.py` mic-mode router (Story 4.6); FSM signal source (Story 4.3) |
| FR48 (mood enum + Talker `set_mood` tool integration) | Epic 3 (mood module + cooldown) → Epic 4 (Talker tool wiring) | `mood/state.py` Story 3.6; tool registry Story 4.4 |
| FR49 (mood publish cooldown ≤4/hr at controller boundary) | Epic 3 | `MoodController.set()` boundary check (Story 3.6) |
| FR50 (publish `MoodEvent` on /olaf/mood with latched QoS) | Epic 3 | Latched / transient_local QoS (Story 3.5 publisher impl + 3.6 controller) |
| FR51 (four typed ROS 2 topics with per-topic QoS) | Epic 3 | `Ros2EventPublisher` four publishers + per-topic QoS (Story 3.5) |
| FR52 (common `EventEnvelope` across topics) | Epic 3 | `schemas/envelope.py` + four event types (Story 3.4) |
| FR53 (`schema_version=3` bump) | Epic 3 + sprint-change-2026-05-10 | 1→2 in event schema rebuild (Story 3.4); 2→3 in boundary repair (sprint-change-proposal-2026-05-10) |

**Coverage check:** all 49 v1-active FRs mapped. FR5 + FR29 + FR30 deferred to v1.5 (tracked in `## v1.5 Backlog (Post-v1)`). FR28 removed. FR7, FR13, FR16, FR41 intentionally absent (v2 deferred).

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

### Epic 3: Embodiment Channel — four typed event topics + mood

**Goal:** Talker emits Cartesia SSML tags. The streaming splitter parses them and segments along sentence / emotion / vocalization boundaries; segments anchor to audio frames; `Ros2EventPublisher` broadcasts on four typed topics — `mood` (latched, slow-cadence), `activity` (FSM-driven, latched — wired Epic 4), `speech_emotion` (audio-anchored, volatile), `vocalization` (audio-anchored, volatile) — sharing a common envelope with `schema_version=3`. Full Cartesia tag → `speech_emotion` mapping with no silent gaps. Mood module ships with publisher-boundary cooldown enforcement (≤4/hr, NFR31).

**User outcome:** OLAF's voice now carries emotion. Subscribers on the bus see four distinct event topics in lockstep with audio (speech_emotion + vocalization) or on transition (mood + activity, the latter wired in Epic 4). Adding a new `speech_emotion` is a YAML edit (no code touchpoints — the architectural extensibility test).

**What's built:**

- Streaming SSML state machine (`splitter/state_machine.py`, ~50–100 LOC, hand-rolled, zero-dep).
- Segmenter: boundary-based emission with two distinct event paths — `speech_emotion` (on emotion tag boundary) and `vocalization` (on vocalization tag boundary).
- Mapping resolver: full primary (6) + secondary (6) + fallback family table covering all 60+ Cartesia tags + `unknown → neutral`. Returns `SpeechEmotionPayload`.
- Last-published cache for `speech_emotion` dedup, scoped per-turn; vocalizations always publish.
- Vocalization-tag stripping: Cartesia-supported tags pass through to TTS; unsupported tags stripped from text but still published as `VocalizationEvent`.
- Audio-frame metadata threading: extend Pipecat's `AudioRawFrame` with optional `speech_emotion_event` AND `vocalization_event` metadata (two distinct slots); transport reads on send and calls the corresponding `EventPublisher.publish_*` method.
- **NEW: Event schema rebuild** — `schemas/envelope.py` (`EventEnvelope` mixin) + four `<topic>_event.py` files (`mood_event`, `activity_event`, `speech_emotion_event`, `vocalization_event`). `schema_version=3` (initial 2 from Story 3.4; bumped to 3 in sprint-change-proposal-2026-05-10 boundary repair). Replaces placeholder `expression_event.py` + `lifecycle_event.py`.
- **NEW: `EventPublisher` Protocol + Implementations** — Protocol with four publish methods + connect/disconnect/health. `Ros2EventPublisher` (four publishers, per-topic QoS, `std_msgs/String` + JSON envelope). `LogEventPublisher` (in-memory adapter for tests + pre-Epic-3 dev).
- **NEW: Mood module** — `mood/state.py` (Mood Literal + MoodState; default `"calm"`) + `mood/controller.py` (`MoodController.set()` with ≤4/hr cooldown enforced at publisher boundary; over-rate drops with WARN; in-process state updated only on successful publish).
- `expression_map.yaml` ships with the canonical taxonomy (12 first-class names + 7 fallback families + 6 vocalizations); loaded at startup, schema-validated (extends FR31). `schema_version=3` enforced.
- Talker prompt updated to emit `<emotion value="..."/>` SSML tags.
- Startup validation extended: `EventPublisher` initialized with all four topic publishers; initial `MoodEvent("calm")` published on connect.

**FRs:** FR12 (extended — Talker now emits SSML), FR18, FR19, FR20, FR21, FR22, FR23, FR24, FR25, FR31 (extended), FR38, FR48, FR49, FR50, FR51, FR52, FR53
**NFRs primarily proven:** NFR5 (30–80ms anticipatory alignment for `speech_emotion` + `vocalization`), NFR21 (per-topic QoS), NFR31 (mood cadence cooldown)

---

### Epic 4: Activity FSM + Tool-Use + Slow Path

**Goal:** Build the conversation-shaped surface: 7-state activity FSM with deferred-sleep transition + mic-mode signaling; Talker becomes tool-using (`go_to_sleep`, `set_mood`); mood-tinted wake greeting on every wake; continuous mic capture while AWAKE; orchestrator slow-path with belief-state grounding.

**User outcome:** OLAF wakes with a "hey," follows up without re-saying the wake word, says goodbye on natural language and returns to sleep. Kamal asks "what's on my calendar today?" — OLAF says "let me check…" within ~1s, runs the comms subagent via the orchestrator, then narrates the result. The four-topic `/olaf/activity` channel emits state transitions in real time. Journeys J1, J3, J4, J5 all demonstrable.

**What's built:**

- `BeliefStateClient` (httpx): per-turn fresh `GET /beliefs?keys=...`, no cache (Story 4.1).
- `OrchestratorClient` (httpx + httpx-sse): `POST /turn` returns SSE; persistent `httpx.AsyncClient`. Dispatch by `type` field; unknown types → log WARN + ignore. `cancel(session_id)` stub raises `NotImplementedError` (wired in v1.5 barge-in) (Story 4.2).
- **NEW: Activity FSM core** — `activity/states.py` (7-state Literal + `WorkingSubmode` Literal). `activity/machine.py` (sync FSM, transition methods, deferred-sleep scheduler, mic-mode signal emitter, `ActivityEvent` publish on transition). Single-writer rule. Renames `lifecycle/` → `activity/`. (Story 4.3)
- **NEW: Talker tool-using upgrade** — `turn/tools.py` (`ToolSpec`, `ToolRegistry`, `GoToSleepTool`, `SetMoodTool`). Validation via Pydantic; invalid input → WARN + drop. Extend `TalkerClient` Protocol with `complete_with_tools()` + `greet()`. Wire into `TurnDispatchProcessor` — text emitted **before** tool dispatch returns (FR45 parallel + FR46 deferred-sleep depend on this ordering). (Story 4.4)
- **NEW: Wake greeting** — `activity/greeting.py:trigger_greeting(mood)` invoked by FSM on `sleeping → waking`. Talker greeting mode with 800 ms timeout + static fallback list. Output via `TalkerResponseFrame` rejoins normal output path. (Story 4.5)
- **NEW: Mic-mode flip** — `audio/transport.py` consumes FSM mic-mode signals, switches between `wake_word_only` (Porcupine engaged) and `vad_stt` (VAD + STT engaged). Single audio source; no parallel listening. (Story 4.6)
- **EVOLVED**: TurnRouter slow-path wiring + missing-`turn_end` recovery + complex-turn integration test. Old TurnRouter logic landed in Story 2.4; this story removes the `NotImplementedError` stub, wires `OrchestratorClient.dispatch()`, handles missing `turn_end`, sets `WorkingSubmode="delegating"` during orchestrator dispatch. (Story 4.7)
- Talker fast-path uses belief state for grounding (extends FR12).
- Startup validation extended: orchestrator daemon reachable + `GET /health` 200 (spec-drift item).
- Startup validation extended: tool registry loadable + validates against typed Pydantic input schemas.

**FRs:** FR9 (slow-path wiring), FR10, FR11, FR12 (extended with belief state + tool-using + greeting mode), FR14, FR26, FR27, FR44, FR45, FR46, FR47
**NFRs primarily proven:** NFR2 (complex-turn ≤1000ms p95), NFR30 (wake-greeting ≤800ms p95), NFR32 (tool-call dispatch overhead bounded)
**Coordination point:** orchestrator project must expose `GET /health`. Story 4.2 surfaces this on the spec-drift list.

---

### Epic 5: Production Hardening

**Goal:** Make OLAF hot-tunable and durable. SIGHUP swaps `expression_map.yaml` atomically with mid-utterance defer; systemd manages the service; logs rotate; LAN orchestrator without a shared secret refuses to start; 7-day soak validates wake-word thresholds, intent-sleep FP/FN, mood cadence, and wake-greeting timing against real household ambient. **Interruptibility (barge-in) is the headline v1.5 feature — deferred to keep v1's quality budget on conversation-shape reliability.**

**User outcome:** OLAF is now a service Kamal can leave running. Tweaking `excited` pose values is a YAML edit + `kill -HUP` away. The pipeline survives a week of daily use without manual restart. Wake-word thresholds, intent-sleep prompt, and mood cadence are all tuned against real ambient. v1 sign-off complete; release tag cut.

**What's built:**

- SIGHUP handler in `__main__.py` → `expression_map` atomic swap; rollback on validation failure with line-number error. (Mood enum, activity state set, tool registry are code-level — not SIGHUP-reloadable.)
- Mid-utterance reload defer: SIGHUP during turn queues until current turn ends.
- LAN orchestrator + shared-secret/mTLS validation rule at startup (refuse-to-start with clear error).
- systemd unit at `deploy/systemd/voice-agent-pipeline.service`: `Type=simple`, `Restart=on-failure`, `RestartSec=5`, `StartLimitInterval=60`, `StartLimitBurst=5`, `WorkingDirectory` pinned, app reads `.env` directly via pydantic-settings.
- Log rotation: size-based (default 50MB/file), retention 7 days default, configurable in `setup.toml`.
- Schema versioning enforcement: refuse to load configs/parse events with unsupported `schema_version`. Contract test exercises all four event types.
- 7-day soak under real household ambient; tune wake-word threshold to NFR12 (≤1 FP/hour) and NFR13 (≤5% FN); tune Talker sleep-intent prompt for FP/FN; verify mood cadence (NFR31) and wake-greeting timing (NFR30); verify J1/J4/J5 journeys end-to-end.

**FRs:** FR32, FR33, FR35, FR36, FR40
**NFRs primarily proven:** NFR7 (SIGHUP <1s), NFR8 (7-day soak), NFR10 (malformed config rollback), NFR11 (USB hot-plug survival), NFR12 (final FP threshold), NFR13 (final FN threshold), NFR27 (schema versioning enforcement, all four event types), NFR30 (wake-greeting timing soak validation), NFR31 (mood cadence soak validation)
**Deferred to v1.5:** FR5, FR29, FR30 (barge-in cluster) — see `## v1.5 Backlog (Post-v1)`.

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

## Epic 3: Embodiment Channel — four typed event topics + mood

**Goal:** Talker emits Cartesia SSML tags; the streaming splitter parses them and segments along sentence / emotion / vocalization boundaries; segments anchor to audio frames; `Ros2EventPublisher` broadcasts on four typed topics — `mood` (latched, slow-cadence), `activity` (FSM-driven, latched — wired Epic 4), `speech_emotion` (audio-anchored, volatile), `vocalization` (audio-anchored, volatile) — sharing a common envelope with `schema_version=3`. Full Cartesia tag → `speech_emotion` mapping with no silent gaps. Mood module ships with publisher-boundary cooldown enforcement (≤4/hr, NFR31). The "OLAF feels alive" sprint.

### Story 3.1: `expression_map.yaml` authoring + loader + schema validation

As Kamal,
I want a complete `expression_map.yaml` covering all Cartesia emotion tags + vocalizations plus a pydantic-validated loader that refuses bad maps at startup,
So that subsequent stories have a typed, complete mapping table to consume — and adding new tags is forever a YAML edit.

**Acceptance Criteria:**

**Given** `expression_map.yaml` at the project root,
**When** I inspect it,
**Then** it contains an integer `schema_version: 3`, an `emotions:` list of all 6 primary (`neutral, content, excited, sad, angry, scared`) and 6 secondary (`happy, curious, sympathetic, surprised, frustrated, melancholic`) canonical emotion names (a vocabulary, not renderer hints — embodiment vocabulary lives consumer-side), a `vocalizations:` block of 6 entries (4 audio bursts: `laughter, sigh, gasp, clears_throat`; 2 gesture cues: `nod, shake` with `tts_supported: false`), a `fallback_families:` block grouping the remaining 50+ Cartesia tags into 7 families (e.g., `high_energy_positive → excited`, `low_energy_negative → sad`), and an `unknown:` entry mapping to `neutral`.

**Given** `src/voice_agent_pipeline/config/expression_map.py`,
**When** I inspect it,
**Then** `ExpressionMapConfig` is a pydantic v2 model with the full schema (emotions, vocalizations, fallback_families, unknown, schema_version), `extra="forbid"` on every nested model, and a `load_from_path(path) -> ExpressionMapConfig` function that validates at startup.

**Given** a malformed `expression_map.yaml` (missing key, wrong type, unknown extra key),
**When** the pipeline starts,
**Then** loading raises `ConfigError` with the offending key/path and exits non-zero (FR31 extension).

**Given** an `expression_map.yaml` with `schema_version` ≠ 3,
**When** the pipeline starts,
**Then** it raises `SchemaVersionError` naming the file, the file's version, and the supported version 2 (NFR27).

**Given** a coverage check at startup,
**When** the loader validates the map,
**Then** every primary + secondary emotion name is present in the `emotions:` list; a missing canonical name raises `ConfigError(missing_emotions=[...])` (FR20 — no silent gaps). The schema-3 boundary repair removed the per-emotion `expression_data:` block — the loader's completeness check is now a set-difference against `PRIMARY_EMOTIONS + SECONDARY_EMOTIONS`, no per-entry payload check.

**Given** the architectural extensibility test,
**When** I append a new canonical name (e.g., `serene`) to the `emotions:` list, restart the pipeline, and the LLM emits `<emotion value="serene"/>`,
**Then** the resolver (Story 3.2) finds it as first-class — proven by the unit test in 3.2 covering the new entry. (SIGHUP hot-reload of this same change is Epic 5.)

**Given** unit tests in `tests/unit/config/test_expression_map.py`,
**When** valid + several invalid maps are loaded,
**Then** valid loads succeed and each invalid load raises the right exception subclass with the expected message.

---

### Story 3.2: Mapping resolver + last-published cache

As Kamal,
I want a pure-function resolver that turns any Cartesia tag into a `SpeechEmotionPayload` via the loaded mapping with fallback-family resolution,
So that the splitter (Story 3.3) can call one function regardless of whether the tag is primary, secondary, family-fallback, or completely unknown.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/splitter/mapping.py`,
**When** I inspect it,
**Then** it exposes `resolve(tag: str, mapping: ExpressionMapConfig) -> SpeechEmotionPayload` returning a fully-populated payload with `emotion` (resolved canonical name), `source_tag` (original Cartesia tag), `raw_tag`, and `resolved_fallback` (FR20, FR21). Embodiment vocabulary is consumer-side, keyed on `payload.emotion`.

**Given** a tag that exists in the `emotions:` block,
**When** `resolve("excited", mapping)` is called,
**Then** it returns a payload with `emotion="excited"`, `source_tag="excited"`, `raw_tag="excited"`, `resolved_fallback=None` — with no log noise.

**Given** a tag in `fallback_families` mapped to a primary,
**When** `resolve("enthusiastic", mapping)` is called,
**Then** it returns a payload with `emotion="excited"`, `source_tag="enthusiastic"`, `raw_tag="enthusiastic"`, `resolved_fallback="high_energy_positive"` — and logs `event="speech_emotion.fallback"` at DEBUG level on first occurrence per process (de-duped via in-memory set), per FR38.

**Given** a tag truly absent from any family,
**When** `resolve("neverbeforeseentag", mapping)` is called,
**Then** it returns a payload with `emotion="neutral"`, `source_tag="neverbeforeseentag"`, `resolved_fallback="unknown"` — and logs `event="speech_emotion.unmapped"` at WARN level (FR38).

**Given** `LastPublishedCache` in `splitter/mapping.py`,
**When** the same base emotion (`excited`) resolves twice consecutively within the same turn without a different emotion intervening,
**Then** `cache.should_publish(resolved)` returns `True` for the first call and `False` for the second (FR24 dedup, scoped per-turn — cache resets at turn boundary).

**Given** vocalization events,
**When** a vocalization (`[laughter]`, `[sigh]`, etc.) is offered to the cache,
**Then** `cache.should_publish(vocalization)` always returns `True` (vocalizations are never deduped, per FR24).

**Given** a unit test in `tests/unit/splitter/test_mapping.py`,
**When** the resolver is exercised against primary, secondary, family-fallback, unknown, and vocalization inputs,
**Then** all expected outputs hold and log assertions match (DEBUG vs WARN per case).

**Given** a unit test for the cache,
**When** sequences like `[content, content, sad, content, [laughter], [laughter]]` are offered within one turn,
**Then** `should_publish` returns `[T, F, T, T, T, T]` — vocalizations always publish, base emotions dedup until they change. After turn boundary, the cache resets so a fresh `content` re-publishes.

---

### Story 3.3: Streaming SSML state machine + boundary-based segmenter

As Kamal,
I want a hand-rolled streaming parser that consumes Cartesia-tagged text token-by-token, splits across token boundaries safely, and emits segments on whichever boundary comes first (sentence / emotion-tag / vocalization-tag),
So that segments can be handed to TTS and the resolver in lockstep without buffering the full response, with two distinct event paths (`speech_emotion` + `vocalization`).

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/splitter/state_machine.py`,
**When** I inspect it,
**Then** it implements a hand-rolled state machine in ~50–100 LOC, zero external dependencies, parsing `<emotion value="X"/>` tags and `[vocalization_name]` events from a token stream incrementally (FR18).

**Given** a tag split across two tokens (e.g., `<emoti` then `on value="excited"/>`),
**When** the state machine consumes the tokens,
**Then** the tag is correctly assembled and emitted as a single tag event (FR18: token-by-token, tags may split across boundaries).

**Given** `src/voice_agent_pipeline/splitter/segmenter.py`,
**When** the segmenter consumes the state machine's events,
**Then** it emits a `Segment(text, speech_emotion_payload, vocalization_payloads)` on whichever boundary comes first: sentence terminator (`.?!`), emotion tag, or vocalization tag (FR19). `speech_emotion_payload` is set when the segment crosses an emotion boundary; `vocalization_payloads` is a list of any vocalizations encountered during the segment.

**Given** a stream containing `<emotion value="content"/> Hello there. <emotion value="excited"/> Great news!`,
**When** segmented,
**Then** segments emit in order: `Segment(text="Hello there.", speech_emotion_payload=<content>, vocalization_payloads=[])` then `Segment(text="Great news!", speech_emotion_payload=<excited>, vocalization_payloads=[])`.

**Given** a stream with `[laughter]` mid-sentence (Cartesia-supported tag),
**When** segmented,
**Then** the vocalization is added to `vocalization_payloads` for that segment; the tag is **kept in the text** going to TTS so Cartesia renders the laugh audio (FR25).

**Given** a stream with `[sigh]` (Cartesia-unsupported tag),
**When** segmented,
**Then** the vocalization is added to `vocalization_payloads` for that segment; the tag is **stripped from the text** going to TTS (FR25). The `VocalizationPayload.tts_supported` field reflects `True` for `[laughter]` and `False` for `[sigh]`.

**Given** state across calls,
**When** the segmenter retains `current_emotion` and `last_published_emotion` (the latter consumed by Story 3.2's cache),
**Then** the dedup contract (FR24) is satisfied: a segment with no emotion change does not republish its `SpeechEmotionEvent`.

**Given** a malformed tag (e.g., `<emotion value=`),
**When** the parser encounters end-of-stream without closure,
**Then** it raises `SplitterError` (catchable only at the process boundary in v1; crash → systemd restart in Epic 5).

**Given** unit tests in `tests/unit/splitter/test_state_machine.py` and `test_segmenter.py`,
**When** the suite runs,
**Then** the following cases pass: token-boundary tag assembly, sentence-terminator emission, mixed sentence+emotion+vocalization, vocalization-keep-in-text vs strip-from-text by `tts_supported`, malformed tag error, multiple emotion changes in one stream, no-emotion plain-text fallthrough, vocalization-only segment with no emotion change.

---

### Story 3.4: Event schema rebuild — common envelope + four typed events

As Kamal,
I want an `EventEnvelope` mixin and four typed event classes (`MoodEvent`, `ActivityEvent`, `SpeechEmotionEvent`, `VocalizationEvent`) replacing the placeholder `expression_event.py` + `lifecycle_event.py` from Story 1.4,
So that subsequent stories (3.5 publisher, 3.6 mood module, 4.3 activity FSM) consume a coherent typed-event surface with `schema_version=3`.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/schemas/envelope.py`,
**When** I inspect it,
**Then** it defines `EventEnvelope` as a frozen pydantic v2 BaseModel with `model_config = ConfigDict(frozen=True, extra="forbid")` and fields: `schema_version: int = 3`, `timestamp: datetime` (UTC), `source: Literal["voice_agent_pipeline"]`, `correlation_id: UUID`, `payload` (typed by each subclass).

**Given** `src/voice_agent_pipeline/schemas/mood_event.py`,
**When** I inspect it,
**Then** it defines `MoodPayload(mood: Mood, reason: str | None)` (where `Mood` is a `Literal[...]` of 6–8 values imported from `mood/state.py` — Story 3.6) and `MoodEvent(EventEnvelope)` with `payload: MoodPayload`.

**Given** `src/voice_agent_pipeline/schemas/activity_event.py`,
**When** I inspect it,
**Then** it defines `ActivityState = Literal["starting", "sleeping", "waking", "listening", "working", "speaking", "going_to_sleep"]`, `WorkingSubmode = Literal["thinking", "delegating"]`, `ActivityPayload(state: ActivityState, working_submode: WorkingSubmode | None, transition_reason: str | None, from_state: ActivityState | None)`, and `ActivityEvent(EventEnvelope)` with `payload: ActivityPayload`. (Note: `working_submode` is non-null only when `state="working"`; `from_state` is null only on the initial `starting` publish. Validators enforce both invariants.)

**Given** `src/voice_agent_pipeline/schemas/speech_emotion_event.py`,
**When** I inspect it,
**Then** it defines `SpeechEmotionPayload(emotion: str, source_tag: str, audio_frame_id: str | None, raw_tag: str, resolved_fallback: str | None)` and `SpeechEmotionEvent(EventEnvelope)` with `payload: SpeechEmotionPayload`. The wire payload is identity-only — embodiment vocabulary is consumer-side, keyed on `payload.emotion` (schema-3 boundary repair removed the prior `expression_data: dict[str, Any]` extensibility-seam field).

**Given** `src/voice_agent_pipeline/schemas/vocalization_event.py`,
**When** I inspect it,
**Then** it defines `VocalizationPayload(tag: str, audio_frame_id: str | None, tts_supported: bool)` and `VocalizationEvent(EventEnvelope)` with `payload: VocalizationPayload`.

**Given** the placeholder schemas from Story 1.4 (`expression_event.py`, `lifecycle_event.py`),
**When** Story 3.4 lands,
**Then** both files are removed; `tests/unit/schemas/test_expression_event.py` and `test_lifecycle_event.py` are removed; `tests/contract/test_expression_event_schema.py` and `test_lifecycle_event_schema.py` are removed (replaced by per-event-type contract tests below).

**Given** contract tests in `tests/contract/test_event_envelope.py`, `test_mood_event_schema.py`, `test_activity_event_schema.py`, `test_speech_emotion_event_schema.py`, `test_vocalization_event_schema.py`,
**When** the suite runs,
**Then** for each event type: a representative instance round-trips through `model_dump_json` → JSON parse → `model_validate` with field equality intact; an instance with `schema_version=1` raises `SchemaVersionError`; an instance violating an invariant (e.g., `working_submode` set when state is not `working`) raises `ValidationError` at construction.

**Given** unit tests in `tests/unit/schemas/`,
**When** the suite runs,
**Then** each event type has tests for: minimal valid construction, `extra="forbid"` enforcement on extra fields, correlation_id UUID generation, timestamp serialization to ISO8601 UTC, `Literal` enforcement on `state` / `working_submode` / `mood`.

---

### Story 3.5: `EventPublisher` Protocol + `Ros2EventPublisher` + `LogEventPublisher` (per-topic QoS)

As Kamal,
I want the `EventPublisher` Protocol with four publish methods and two implementations — `Ros2EventPublisher` (production, four `rclpy` publishers + per-topic QoS + JSON envelope) and `LogEventPublisher` (in-memory adapter for tests + pre-Epic-3 dev),
So that v1 ships the four-topic broadcast surface with zero ament/colcon overhead, downstream consumers see correct per-topic QoS, and tests can drive the publisher without standing up a ROS 2 environment.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/publisher/interface.py`,
**When** I inspect it,
**Then** it defines `EventPublisher` as a `typing.Protocol` with async methods: `connect() -> None`, `disconnect() -> None`, `is_healthy() -> bool`, `publish_mood(MoodEvent) -> None`, `publish_activity(ActivityEvent) -> None`, `publish_speech_emotion(SpeechEmotionEvent) -> None`, `publish_vocalization(VocalizationEvent) -> None`. (Story 1.4's placeholder `ExpressionPublisher` Protocol is removed.)

**Given** `src/voice_agent_pipeline/publisher/ros2.py`,
**When** I inspect it,
**Then** it implements `EventPublisher` as `Ros2EventPublisher`; `rclpy` is imported only in this file (boundary-concentration rule). The class holds four `rclpy.Publisher` instances, one per topic.

**Given** ROS 2 QoS configuration,
**When** the publisher is constructed,
**Then** per-topic QoS profiles are set: `mood` → RELIABLE + `transient_local` durability + depth=1 (latched); `activity` → RELIABLE + `transient_local` + depth=1 (latched); `speech_emotion` → RELIABLE + `volatile` + depth=8; `vocalization` → RELIABLE + `volatile` + depth=8 (NFR21, FR51).

**Given** `setup.toml` `[publisher]` block with `adapter = "ros2"`, `dds_domain_id = ...`, and `topics = { mood = "/olaf/mood", activity = "/olaf/activity", speech_emotion = "/olaf/speech_emotion", vocalization = "/olaf/vocalization" }`,
**When** the pipeline starts,
**Then** the publisher reads all four topic names + DDS domain from config (no hard-coded topic names — agnostic publisher per the project's pipeline-scope boundary memory).

**Given** `Ros2EventPublisher.connect()`,
**When** called at startup,
**Then** it initializes `rclpy`, creates a node, creates all four publishers with their per-topic QoS, and returns; failure raises `PublisherError` and `StartupValidationError` cascades (v1 fail-fast — broadcast bus is a hard dep).

**Given** any of the four `publish_*(event)` methods,
**When** called,
**Then** it serializes `event.model_dump_json()` (full envelope including payload) into a `String` message and publishes on the corresponding topic; runtime failure raises `PublisherError` and crashes the process.

**Given** `src/voice_agent_pipeline/publisher/log_adapter.py`,
**When** I inspect it,
**Then** `LogEventPublisher` implements the same Protocol with all four methods recording events to an in-memory list (`self.published: list[tuple[str, EventEnvelope]]` keyed by topic name); `connect`/`disconnect`/`is_healthy` are no-ops returning healthy. Used by integration tests and pre-Epic-3 dev runs.

**Given** unit tests in `tests/unit/publisher/test_ros2.py` mocking `rclpy`,
**When** events are published on each of the four topics,
**Then** the JSON content matches `event.model_dump_json()`, the topic name matches config, and per-topic QoS settings match the spec.

**Given** unit tests in `tests/unit/publisher/test_log_adapter.py`,
**When** the adapter is exercised with all four event types,
**Then** `published` records each event under the correct topic key in publish order.

**Given** the README,
**When** I open the deployment notes,
**Then** they explain the system-installed `rclpy` requirement (e.g., `apt install ros-jazzy-rclpy`) and how to expose it to the venv via `PYTHONPATH`.

---

### Story 3.6: Mood module — `MoodState` + `MoodController` + cooldown enforcement

As Kamal,
I want a `mood/` package owning the discrete mood enum, the in-process current-mood cell, and a controller that enforces the ≤4/hr publish cooldown at the publisher boundary,
So that Talker's `set_mood(mood)` tool (Story 4.4) and `activity/greeting.py` (Story 4.5) have a single coherent surface for reading and updating mood — and NFR31 is enforced in one place.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/mood/state.py`,
**When** I inspect it,
**Then** it defines `Mood = Literal[<6–8 values>]` (final list a code-level decision — recommended: `"calm", "happy", "playful", "curious", "thoughtful", "sleepy", "grumpy", "excited"`), and `MoodState` as a single mutable cell with `current: Mood = "calm"`. Read path is sync; write path goes through `MoodController.set()`.

**Given** `src/voice_agent_pipeline/mood/controller.py`,
**When** I inspect it,
**Then** `MoodController` holds references to `MoodState` and `EventPublisher`. Its async `set(mood: Mood, reason: str) -> bool` method: (a) checks the cooldown window (sliding 60-min, ≤4 publishes), (b) if allowed, builds a `MoodEvent` with the new mood and `reason`, calls `publisher.publish_mood(event)`, and **only on successful publish** updates `MoodState.current = mood`; returns `True`. (c) If over-rate, logs `event="mood.publish_dropped"` at WARN with `attempted_mood`, `current_mood`, `reason="cooldown"`, returns `False`, leaves state unchanged.

**Given** the cooldown's "publish history,"
**When** `set()` is called,
**Then** the controller maintains a deque of timestamps of successful publishes, popping any older than 60 minutes; if the deque length is ≥ 4 the call is rate-limited.

**Given** startup,
**When** `EventPublisher.connect()` succeeds,
**Then** `MoodController.publish_initial()` publishes `MoodEvent(mood="calm", reason="startup")` exactly once, even though this counts toward the cooldown budget. Subsequent test/dev workflow may see this initial publish in the latched topic.

**Given** unit tests in `tests/unit/mood/test_state.py` and `tests/unit/mood/test_controller.py`,
**When** the suite runs,
**Then** the following cases pass: state defaults to `"calm"`; controller `set()` happy path updates state and publishes; over-rate `set()` drops with WARN, leaves state unchanged, returns `False`; sliding-window window math (4 calls within 60 min vs straddling the window) verified with `freezegun` or equivalent; invalid `Mood` raises `ValidationError` at the type boundary; `publish_initial()` fires once at startup.

**Given** `setup.toml` `[mood]` block,
**When** the pipeline starts,
**Then** it reads optional `[mood] cooldown_publishes_per_hour = 4` (default 4) and `[mood] initial = "calm"` (default `"calm"`); the controller respects both. The mood **enum** is code-level and not config-overridable (per architecture's mood enum lifecycle decision).

---

### Story 3.7: Audio-frame metadata threading + Talker SSML prompt + embodiment alignment integration test

As Kamal,
I want segments' `SpeechEmotionEvent` AND `VocalizationEvent` metadata threaded through Pipecat's audio frames so the publisher fires when each frame is sent — and Talker updated to emit Cartesia SSML tags — and an integration test that proves voice / `speech_emotion` alignment hits the 30–80ms anticipatory window (NFR5),
So that Sprint 3 delivers visible (on-bus) embodiment in lockstep with audio across both audio-anchored topics.

**Acceptance Criteria:**

**Given** Pipecat's `AudioRawFrame`,
**When** the splitter (Story 3.3) emits a `Segment` with a `speech_emotion_payload` and/or `vocalization_payloads`,
**Then** the segment's first audio frame from Cartesia carries the events in **two** optional metadata slots — `speech_emotion_event` and `vocalization_events: list[VocalizationEvent]` — both wrapped with full envelope (timestamp, correlation_id, etc.) (architecture's Batch 2 decision; if Pipecat's processor model can't carry it cleanly, fall back to time-based correlation per the documented PRD risk fallback — and document the deviation in PRD/architecture per NFR26).

**Given** `LocalAudioTransport` output,
**When** it sends an audio frame to the speaker,
**Then** if `frame.speech_emotion_event` is set, it calls `EventPublisher.publish_speech_emotion(event)` immediately before frame send; if `frame.vocalization_events` is non-empty, it calls `EventPublisher.publish_vocalization(event)` for each, in order, immediately before frame send (FR22, FR23).

**Given** the last-published cache from Story 3.2,
**When** the segmenter resolves a segment's emotion and the cache says `should_publish=False`,
**Then** no `speech_emotion_event` is attached to that segment's audio frame (FR24 dedup). Vocalizations are unaffected — they always attach.

**Given** `prompts/talker_system.md`,
**When** I inspect it,
**Then** it now instructs Talker to emit responses with Cartesia `<emotion value="..."/>` SSML tags inline (e.g., `<emotion value="content"/> It's 8:47.`), and to use `[laughter]` / `[sigh]` / `[gasp]` vocalizations naturally. The prompt enumerates the 6 primary + 6 secondary emotions for the LLM (FR12 extension). Greeting-mode prompt is added in Story 4.5; here we only update conversational mode.

**Given** an integration test in `tests/integration/test_embodiment_alignment.py`,
**When** the full pipeline runs with mocked Cartesia (yielding deterministic audio frames at known timestamps) and `LogEventPublisher` (capturing publish times per topic),
**Then** for 30 simulated turns, the p95 of (publish_time − frame_send_time) for **both** `speech_emotion` and `vocalization` events falls within the 30–80ms anticipatory window (NFR5).

**Given** an integration test mocking the publisher and asserting on event payloads,
**When** Talker emits a response with one primary, one secondary, one fallback-family tag, and one `[laughter]`,
**Then** three `SpeechEmotionEvent`s and one `VocalizationEvent` publish with correct field values, in correct order, base emotions deduped per FR24, vocalization always present.

**Given** the redaction discipline,
**When** Epic 3's integration tests run,
**Then** no log line contains `audio_bytes`, raw transcripts at INFO level, or credentials (NFR25).

**Given** the v1 deferred fallback path (PRD risk),
**When** audio-frame metadata cannot be threaded cleanly through Pipecat,
**Then** the story's implementation switches to time-based correlation (publish at `frame.send_time + offset`) and the deviation is recorded in `architecture.md` (NFR26 spec-as-contract update in the same change).

---

## Epic 4: Activity FSM + Tool-Use + Slow Path

**Goal:** Build the conversation-shaped surface: 7-state activity FSM with deferred-sleep transition + mic-mode signaling; Talker becomes tool-using (`go_to_sleep`, `set_mood`); mood-tinted wake greeting on every wake; continuous mic capture while AWAKE; orchestrator slow-path with belief-state grounding. After this, OLAF wakes with a "hey," follows up without re-saying the wake word, says goodbye on natural language, and answers complex questions via the orchestrator. Journeys J1, J3, J4, J5 demonstrable.

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
**When** Story 4.4's `TalkerClient.complete_with_tools(transcript)` is called,
**Then** it optionally fetches belief state via `BeliefStateClient.read(...)` based on a configurable list of keys (e.g., `[talker] grounded_keys = ["time", "calendar_today"]`) and includes them in the system prompt context (FR12 extension — belief grounding). Note: The integration of `BeliefStateClient` into Talker is finalized in Story 4.4 (which delivers `complete_with_tools`); this story defines the client and stubs the consumer hook.

**Given** a unit test in `tests/unit/turn/test_beliefs.py` mocking `httpx.AsyncClient`,
**When** `read(["time"])` is called,
**Then** the request URL matches `?keys=time`, the parsed response shape matches expectations, and a 500 response raises `OrchestratorError`.

---

### Story 4.2: `OrchestratorClient` — SSE stream consumer over `httpx-sse`

As Kamal,
I want an `OrchestratorClient` that opens `POST /turn` as an SSE stream and yields typed events as they arrive (narration, subagent_started, subagent_progress, subagent_done, response_chunk, turn_end),
So that Story 4.7's pipeline can dispatch complex turns without buffering the full response.

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
**Then** `HttpOrchestratorClient.cancel(session_id)` is **stubbed** in Epic 4 (raises `NotImplementedError`); the `HTTP DELETE /turn/{session_id}` wiring lands in **v1.5 Story v1.5-1 (barge-in)** — see `## v1.5 Backlog (Post-v1)`.

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

### Story 4.3: Activity FSM core — 7-state + deferred-sleep + mic-mode signaling

As Kamal,
I want a 7-state activity FSM in a new `activity/` package (renamed from `lifecycle/`) that transitions on observable events, schedules deferred-sleep on `go_to_sleep` tool calls, signals mic-mode flips to the audio transport, and publishes `ActivityEvent` on every transition,
So that the conversation-shaped pipeline has a single coherent state spine — and Stories 4.4 (Talker tool-using), 4.5 (wake greeting), and 4.6 (mic-mode flip) have a stable surface to integrate with.

**Acceptance Criteria:**

**Given** the existing placeholder package `src/voice_agent_pipeline/lifecycle/`,
**When** Story 4.3 lands,
**Then** the package is renamed to `src/voice_agent_pipeline/activity/`. Any imports across the codebase referencing `lifecycle/` are updated. The rename is a single discrete change with no other behaviour mixed in.

**Given** `src/voice_agent_pipeline/activity/states.py`,
**When** I inspect it,
**Then** it defines `ActivityState = Literal["starting", "sleeping", "waking", "listening", "working", "speaking", "going_to_sleep"]` and `WorkingSubmode = Literal["thinking", "delegating"]`, both re-exported from `activity/__init__.py` for ergonomic imports.

**Given** `src/voice_agent_pipeline/activity/machine.py`,
**When** I inspect it,
**Then** `ActivityFSM` is a sync class holding `current_state: ActivityState`, `working_submode: WorkingSubmode | None`, a `sleep_pending: bool` flag, references to `EventPublisher` and an event-emitter for mic-mode signals (e.g., `asyncio.Queue` consumed by audio/transport in Story 4.6). Single-writer rule: only the FSM mutates `current_state`. Other components emit transition events into FSM methods, never mutate state directly.

**Given** the FSM's transition methods,
**When** I inspect them,
**Then** they cover: `on_wake_detected()` (`sleeping → waking`), `on_speech_started()` (`waking → listening` if waking, otherwise `listening` stays), `on_speech_ended()` (`listening → working[thinking]`), `on_dispatch_to_orchestrator()` (`working[thinking] → working[delegating]`), `on_first_audio_frame()` (`working → speaking`), `on_last_audio_frame()` (`speaking → listening` OR `speaking → going_to_sleep` if `sleep_pending=True`), `on_going_to_sleep_complete()` (`going_to_sleep → sleeping`), `on_tool_call_go_to_sleep()` (sets `sleep_pending=True`, no transition).

**Given** the deferred-sleep scheduler,
**When** `on_tool_call_go_to_sleep()` is called mid-`speaking` state,
**Then** `sleep_pending` is set; on next `on_last_audio_frame()`, the FSM transitions `speaking → going_to_sleep`, schedules an immediate follow-up transition `going_to_sleep → sleeping`, and publishes both transitions. If a wake-word fires before `on_last_audio_frame()` (edge case), `sleep_pending` is cleared and the deferred-sleep is cancelled (FR46).

**Given** the mic-mode signaling,
**When** the FSM enters `sleeping`,
**Then** it emits `mic_mode = "wake_word_only"` on the signal queue. When it enters `waking`, `listening`, `working`, or `speaking`, it emits `mic_mode = "vad_stt"`. When it enters `going_to_sleep`, mic mode stays at `vad_stt` (so a follow-up wake-word from the user could in theory cancel — though edge case). On entering `sleeping`, mic mode flips to `wake_word_only`. (Story 4.6 wires the consumer side.)

**Given** every transition,
**When** the FSM transitions,
**Then** it publishes an `ActivityEvent` via `EventPublisher.publish_activity()` with `from_state`, `to_state`, `working_submode` (when applicable), `transition_reason` (e.g., `"wake_detected"`, `"end_of_speech"`, `"go_to_sleep_tool_call"`, `"last_audio_frame"`, `"deferred_sleep_complete"`).

**Given** an illegal transition request,
**When** invoked (e.g., `on_first_audio_frame()` from `sleeping`),
**Then** `ActivityFSM` raises `VoiceAgentError` with a message naming the current state and the requested transition; v1 fail-fast crashes the process.

**Given** redaction discipline,
**When** activity logs fire,
**Then** they emit `event="activity.transition"` at INFO with `from_state`, `to_state`, `working_submode`, `transition_reason`, `correlation_id` — no transcript content, no audio bytes (NFR25).

**Given** unit tests in `tests/unit/activity/test_machine.py`,
**When** legal and illegal transitions are exercised + deferred-sleep + mic-mode signal sequencing (using `pytest-asyncio`),
**Then** all expected transitions succeed, illegal ones raise, deferred-sleep fires correctly (including the wake-word-cancel edge case), mic-mode signals are emitted in the correct sequence, and `publish_activity` is called for each transition.

**Given** an integration test in `tests/integration/test_activity_lifecycle.py`,
**When** the full pipeline runs through a simple turn,
**Then** the published activity sequence is `[starting, sleeping, waking, listening, working[thinking], speaking, listening]` with `correlation_id` shared across the turn.

---

### Story 4.4: Talker tool-using upgrade — `complete_with_tools` + tool registry + `GoToSleepTool` + `SetMoodTool`

As Kamal,
I want Talker to become tool-using — exposing `go_to_sleep` and `set_mood` to the LLM via the openai SDK's `tools=` parameter, validating tool inputs against typed Pydantic schemas, and dispatching to `ActivityFSM` and `MoodController` concurrently with text emission,
So that Talker's natural-language goodbye triggers the deferred-sleep path (FR46) and natural-language mood shifts publish on `/olaf/mood` — without blocking text-to-TTS on tool side effects.

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/turn/tools.py`,
**When** I inspect it,
**Then** it defines `ToolSpec` (frozen pydantic v2: `name: str`, `description: str`, `input_schema: type[BaseModel]`, `dispatch: Callable[[BaseModel], Awaitable[None]]`), `ToolRegistry` (holds list of `ToolSpec`, exposes `dispatch(tool_call: ToolCall)` async method + `as_openai_tools_param()` returning the openai SDK's tool definitions list), `GoToSleepTool` (empty input model `GoToSleepInput()`; dispatch calls `activity_fsm.on_tool_call_go_to_sleep()`), `SetMoodTool` (input model `SetMoodInput(mood: Mood)`; dispatch calls `mood_controller.set(mood, reason="talker_set_mood")`).

**Given** the tool registry's `dispatch(tool_call)` method,
**When** invoked with a `tool_call.name` matching a registered tool,
**Then** it looks up the spec, calls `spec.input_schema.model_validate(tool_call.arguments)` — Pydantic raises `ValidationError` on bad input; the registry catches it, logs `event="tool.dispatch_invalid_input"` at WARN with `tool=<name>`, `error=<message>`, drops the call with no side effect. Successful validation calls `spec.dispatch(validated_input)` and awaits.

**Given** an unknown tool name,
**When** `dispatch(tool_call)` is called,
**Then** the registry logs `event="tool.dispatch_unknown_name"` at WARN and drops the call.

**Given** the `TalkerClient` Protocol in `turn/talker.py`,
**When** I inspect it,
**Then** it gains two new methods: `complete_with_tools(prompt: str, tool_registry: ToolRegistry, **kwargs) -> TalkerResponse(text: str, tool_calls: list[ToolCall])` and `greet(mood: Mood) -> str`. The existing `complete()` method stays for backward compatibility but is no longer called in production — `TurnDispatchProcessor` switches to `complete_with_tools()`.

**Given** the openai-SDK-backed Talker implementation (Story 2.2),
**When** `complete_with_tools()` is called,
**Then** it passes `tools=tool_registry.as_openai_tools_param()` to `client.chat.completions.create()`, parses the response for tool_calls (alongside text content), and returns `TalkerResponse(text=..., tool_calls=...)`. Belief-state grounding from Story 4.1 is integrated here: if `[talker] grounded_keys` is configured, `BeliefStateClient.read(grounded_keys)` is called and results injected into the system prompt context.

**Given** `TurnDispatchProcessor` (Story 2.4),
**When** updated,
**Then** it calls `talker.complete_with_tools(prompt, tool_registry)`, **emits `TalkerResponseFrame(text)` immediately** to the splitter, then concurrently kicks off `asyncio.gather(*[tool_registry.dispatch(tc) for tc in tool_calls])` — text-to-TTS is never blocked on tool-side-effect completion (FR45 parallel + FR46 deferred-sleep). The dispatcher does NOT await tool dispatches; they run as background tasks (created with `asyncio.create_task`, with logging of any exceptions via task done-callbacks).

**Given** `setup.toml` `[tools]` block,
**When** I inspect it,
**Then** it contains optional flags `enable_go_to_sleep = true`, `enable_set_mood = true` (default both true). When false, the corresponding tool is not registered with the openai SDK at invocation time.

**Given** unit tests in `tests/unit/turn/test_tools.py`,
**When** the suite runs,
**Then** the following cases pass: registry construction with tool list; `as_openai_tools_param()` emits correct openai-format tool definitions; `dispatch()` happy-path validates input + calls dispatch coroutine; `dispatch()` invalid-input logs WARN + drops; `dispatch()` unknown tool logs WARN + drops; `GoToSleepTool` dispatch invokes `activity_fsm.on_tool_call_go_to_sleep()`; `SetMoodTool` dispatch invokes `mood_controller.set(mood, reason="talker_set_mood")`.

**Given** an updated unit test for the Talker implementation,
**When** `complete_with_tools(...)` is called,
**Then** the openai SDK is called with `tools=` param; if the mocked response includes `tool_calls`, they are parsed correctly; belief-state grounding (when configured) is fetched and injected into the system prompt.

**Given** an integration test `tests/integration/test_intent_sleep.py` (PRD Journey 4) at the dispatch level (does not require the full FSM to be wired into the pipeline yet — that's Story 4.7's complex-turn test),
**When** Talker's mocked response includes `tool_calls=[{"name": "go_to_sleep", "arguments": {}}]`,
**Then** (a) `TalkerResponseFrame(text)` is emitted to the splitter before `tool_registry.dispatch` completes, (b) `activity_fsm.sleep_pending` is set to `True` after dispatch completes.

---

### Story 4.5: Wake greeting — `talker.greet()` greeting mode + `activity/greeting.trigger_greeting()` + 800ms timeout + J1 integration test

As Kamal,
I want a 2–8 word mood-tinted wake greeting fired automatically on every `sleeping → waking` FSM transition, generated by Talker in greeting mode with an 800ms timeout and a static fallback list,
So that wake-up feels like a friend acknowledging me ("hey", "yeah?", "what's up?") instead of a scripted "Hello, I am OLAF" — and Journey 1 (wake-with-greeting) becomes demonstrable end-to-end.

**Acceptance Criteria:**

**Given** the existing `TalkerClient` Protocol (extended in Story 4.4 with `greet(mood: Mood) -> str`),
**When** the Talker implementation's `greet(mood)` is called,
**Then** it invokes the openai SDK with a **dedicated greeting-mode system prompt** in `prompts/talker_greeting.md` (or as a constant in `turn/talker.py`) that directs: 2–8 words, "cool friend" register (NOT formal, NOT scripted), tinted by the supplied `mood` (e.g., `playful` → "what's up?"; `sleepy` → "mm yeah?"; `calm` → "yeah?"). Returns the LLM's text reply directly (no SSML tags in greeting mode).

**Given** `src/voice_agent_pipeline/activity/greeting.py`,
**When** I inspect it,
**Then** it exposes `async def trigger_greeting(mood: Mood, talker: TalkerClient, fallback_list: list[str]) -> str`. The implementation: (a) calls `await asyncio.wait_for(talker.greet(mood), timeout=0.8)`; (b) on success, validates the response is 2–8 words (split on whitespace, strip punctuation), returns the text if valid; (c) on `asyncio.TimeoutError`, on Talker exception, or on word-count failure, logs `event="greeting.fallback"` at INFO with `mood`, `reason=<timeout|error|too_long>`, and returns `random.choice(fallback_list)`.

**Given** `setup.toml` `[greeting]` block,
**When** I inspect it,
**Then** it contains `timeout_seconds = 0.8` (default), `min_words = 2` (default), `max_words = 8` (default), `fallback_list = ["hey", "yeah?", "hi"]` (default). All overridable.

**Given** `ActivityFSM` (Story 4.3),
**When** the FSM transitions `sleeping → waking`,
**Then** the `_publish_transition()` callback (or equivalent hook) creates a background task `asyncio.create_task(_handle_wake_greeting())` which: reads `mood_controller.state.current` (sync), calls `await trigger_greeting(mood, talker, fallback_list)`, wraps the returned text in a `TalkerResponseFrame`, and pushes it to the splitter — same downstream path as conversational replies. The `ActivityEvent(state="waking")` publish happens immediately on the FSM transition, NOT awaiting the greeting (decouples FSM publishing from greeting latency).

**Given** the splitter receives a greeting `TalkerResponseFrame`,
**When** it segments the text,
**Then** the greeting flows through the same splitter / TTS / audio-anchored event publish path. Greetings typically have no SSML tags, so no `speech_emotion_event` is published; that's fine — `speech_emotion` is for in-conversation emotion, not the greeting.

**Given** unit tests in `tests/unit/activity/test_greeting.py`,
**When** the suite runs,
**Then** the following cases pass: happy-path `trigger_greeting` returns Talker's response within 800ms; timeout falls back to static list; Talker exception falls back; overlong response (>8 words) falls back; word-count math is correct (whitespace split, punctuation-stripped); each `mood` value is exercisable.

**Given** an integration test `tests/integration/test_wake_greeting.py` (PRD Journey 1),
**When** the test simulates: pipeline up + `MoodController` initial mood `"calm"` + wake-word fires →,
**Then** within ~1 second: (a) `ActivityEvent(state="waking")` publishes, (b) a `TalkerResponseFrame` containing the greeting text is observed at the splitter, (c) audio frames flow to the speaker, (d) `ActivityEvent(state="listening")` publishes after the last greeting audio frame. Both Talker-success and Talker-timeout (fallback list) variants are exercised.

**Given** NFR30,
**When** the integration test measures `(sleeping_to_waking transition timestamp → first audio frame of greeting)` for 30 simulated wakes,
**Then** the p95 is ≤ 800 ms (NFR30 baseline; final soak validation in Story 5.4).

---

### Story 4.6: Mic-mode flip — `audio/transport` consumes FSM mic-mode signal (FR47)

As Kamal,
I want `audio/transport.py` to subscribe to the `ActivityFSM` mic-mode signal queue and route the single mic stream between `wake_word_only` (Porcupine engaged, VAD/STT suspended) and `vad_stt` (VAD + STT engaged, Porcupine suspended) modes,
So that wake-word fires only on `sleeping → waking` (not on every turn) and follow-up turns flow without re-prompting (FR47, continuous conversation while AWAKE).

**Acceptance Criteria:**

**Given** `src/voice_agent_pipeline/audio/transport.py`,
**When** I inspect it,
**Then** the `LocalAudioTransport` wrapper holds an internal `mic_mode: Literal["wake_word_only", "vad_stt"]` (default `"wake_word_only"` at startup, before FSM transitions to `sleeping` via `starting → sleeping`). It subscribes to the FSM's mic-mode signal queue and updates `mic_mode` on each signal.

**Given** `mic_mode == "wake_word_only"`,
**When** mic frames arrive,
**Then** they are routed to `audio/wakeword.py` (Porcupine processor); VAD + STT processors are skipped (no audio frames flow into them).

**Given** `mic_mode == "vad_stt"`,
**When** mic frames arrive,
**Then** they are routed to VAD + STT processors; Porcupine is **not** called (no wake-word detection runs while AWAKE — FR47 single-stream invariant).

**Given** the mode transition from `wake_word_only → vad_stt`,
**When** triggered (FSM enters `waking`),
**Then** Porcupine's internal buffer is cleared (no stale buffered audio leaking into VAD); VAD + STT are reset to a clean starting state.

**Given** the mode transition from `vad_stt → wake_word_only`,
**When** triggered (FSM enters `sleeping`),
**Then** any in-flight VAD detection state is dropped; STT's transcription buffer is cleared; Porcupine is re-engaged on subsequent frames.

**Given** unit tests in `tests/unit/audio/test_transport.py`,
**When** the suite runs,
**Then** the following cases pass: starting `mic_mode` is `"wake_word_only"`; signal-driven transitions update the mode; in `wake_word_only` Porcupine receives frames + VAD doesn't; in `vad_stt` VAD receives frames + Porcupine doesn't; mode transitions reset internal buffers correctly; rapid sequential signals (e.g., `wake_word_only → vad_stt → wake_word_only`) are handled correctly without dropped frames.

**Given** an integration test `tests/integration/test_continuous_conversation.py`,
**When** the test simulates: wake-word → simple turn 1 → simple turn 2 (no second wake-word) → intent-sleep,
**Then** turn 2's transcript is captured WITHOUT a second wake-word fire; the activity FSM stays in `listening` between turns (no return to `sleeping`); only at the very end (Talker `go_to_sleep`) does the FSM return to `sleeping` and `mic_mode` flip back to `wake_word_only`.

---

### Story 4.7: TurnRouter slow-path wiring + missing-`turn_end` recovery + complex-turn integration test (J3, NFR2 baseline)

As Kamal,
I want `pipeline.py` to wire the slow path (TurnRouter `target="orchestrator"` → orchestrator SSE → splitter → TTS+publisher) including missing-`turn_end` cleanup and FSM `working[delegating]` sub-mode coordination, plus an integration test for journey 3 (complex turn) recording the NFR2 baseline,
So that I can ask "what's on my calendar?" and OLAF actually answers — narration first, then real result via subagent — with the activity FSM correctly tracking `delegating` during orchestrator dispatch.

**Acceptance Criteria:**

**Given** `pipeline.py` updates,
**When** `TurnRouter` (Story 2.4) returns `target="orchestrator"`,
**Then** `TurnDispatchProcessor` (updated from Story 4.4 to call `complete_with_tools` for fast-path) routes to a new `OrchestratorDispatchProcessor`, which: (a) calls `activity_fsm.on_dispatch_to_orchestrator()` (FSM transitions `working[thinking] → working[delegating]`), (b) calls `OrchestratorClient.dispatch(...)`, (c) consumes the SSE stream, (d) pipes `narration` + `response_chunk` text to the splitter as `TalkerResponseFrame` instances. `subagent_*` events update an internal "thinking" indicator (logged but no audio impact in v1).

**Given** the splitter sees a slow-path stream,
**When** segments emit,
**Then** they flow to TTS + publisher exactly like fast-path segments (single-fan-out architectural constraint preserved — splitter doesn't know or care about source).

**Given** an orchestrator stream that ends without a `turn_end` event,
**When** the SSE connection closes,
**Then** the pipeline flushes the splitter (any pending text is segmented + sent to TTS), waits for the last audio frame, the FSM transitions normally on `on_last_audio_frame()` — to `listening` (or to `going_to_sleep` if a `go_to_sleep` tool call fired during the slow turn) — and a WARN log `orchestrator.missing_turn_end` is emitted (FR14).

**Given** an orchestrator stream that includes `turn_end`,
**When** received,
**Then** the splitter drains immediately and the FSM transitions normally on last-frame.

**Given** the `TurnRouter`'s old `NotImplementedError` stub from Story 2.4 in the orchestrator branch,
**When** Story 4.7 lands,
**Then** the stub is removed; the slow path now actually invokes `OrchestratorClient.dispatch(...)`.

**Given** an integration test in `tests/integration/test_complex_turn.py` (PRD Journey 3),
**When** the pipeline runs with mocked orchestrator emitting the full event sequence (`narration → subagent_started → subagent_progress → subagent_done → response_chunk × N → turn_end`),
**Then** end-of-speech → first narration audio frame is measured for 30 simulated turns and p95 is recorded as the **NFR2 baseline** (≤1000ms target). The FSM publishes `[working[thinking], working[delegating], speaking, listening]` correctly across each turn.

**Given** an integration test for missing-`turn_end`,
**When** the mock orchestrator drops the `turn_end` event after the last `response_chunk`,
**Then** the pipeline still completes the turn (splitter flushes, FSM transitions on last frame), and `orchestrator.missing_turn_end` WARN is emitted.

**Given** the redaction discipline,
**When** Epic 4's integration tests run,
**Then** orchestrator stream content is not logged at INFO level (raw response chunks contain LLM text — treated like transcripts; gated to DEBUG only).

**Given** v1 fail-fast,
**When** the orchestrator returns 5xx or the stream stalls indefinitely (no events for >60s connection-level timeout),
**Then** the pipeline raises `OrchestratorError` and crashes (filler-response/heartbeat resilience is FR13/NFR20 — deferred to v2).

---

## Epic 5: Production Hardening

**Goal:** Make OLAF hot-tunable and durable. SIGHUP swaps `expression_map.yaml` atomically with mid-utterance defer; systemd manages the service; logs rotate; LAN orchestrator without a shared secret refuses to start; 7-day soak validates wake-word thresholds, intent-sleep FP/FN, mood cadence (NFR31), and wake-greeting timing (NFR30) against real household ambient. **Interruptibility (barge-in) is the headline v1.5 feature — deferred to keep v1's quality budget on conversation-shape reliability.**

### Story 5.1: SIGHUP atomic swap of `expression_map.yaml` + mid-utterance defer

As Kamal,
I want to edit `expression_map.yaml` and apply changes without restarting the pipeline mid-session, with mid-utterance reloads deferred until the current turn completes and validation failures retaining the prior mapping,
So that I can tune `speech_emotion` poses live during real conversation without breaking the running session.

**Acceptance Criteria:**

**Given** the pipeline is running and `__main__.py` has installed a SIGHUP handler,
**When** I send `kill -HUP <pid>` (or run `just reload`, which does `kill -HUP $(pgrep -f voice_agent_pipeline)`),
**Then** the handler dispatches to `config/expression_map.py` for an atomic swap (FR32).

**Given** the new `expression_map.yaml` is valid,
**When** the reload runs,
**Then** the in-memory `ExpressionMapConfig` is replaced atomically (the resolver in `splitter/mapping.py` reads via a single reference that's swapped under a lock); the next turn that emits an emotion uses the new mapping.

**Given** the new `expression_map.yaml` is malformed (bad schema, unknown extra key, missing canonical emotion, `schema_version` ≠ 3),
**When** the reload runs,
**Then** validation rejects, the **prior mapping is retained** (no silent-broken state), and a clear error is logged at WARN with the line number and key path.

**Given** a SIGHUP arrives mid-utterance (FSM in `speaking` with audio frames still flowing),
**When** the handler runs,
**Then** the reload is **deferred** — queued — and applied after the current turn completes (FSM returns to `listening`) (FR33). A `config.reload.deferred` INFO log fires.

**Given** NFR7 (≤1s SIGHUP reload),
**When** measured on the dev host (not mid-utterance),
**Then** signal-receipt → reload-complete is < 1 second at p95 over 30 reloads.

**Given** the architecture's open question on router-rule hot-reload,
**When** Story 5.1 lands,
**Then** `[router]` patterns are **also** included in the SIGHUP swap (per architecture's "lean yes, config-only extensibility theme" — extends FR32 to cover router patterns); `setup.toml` non-router/non-mapping fields still require restart. The mood enum, activity state set, tool registry, and greeting fallback list are **code-level** and not SIGHUP-reloadable (per architecture's mood enum lifecycle decision).

**Given** an integration test in `tests/integration/test_sighup_reload.py` (PRD Journey 8),
**When** the test runs the pipeline, edits `expression_map.yaml` with a valid change, sends SIGHUP, then triggers a turn,
**Then** the new mapping is in effect for that turn.
**And** when the same test sends SIGHUP with a malformed file, **then** the old mapping persists and an error is logged.
**And** when the same test sends SIGHUP mid-utterance, **then** the reload defers until the turn ends and only then takes effect.

---

### Story 5.2: Security & config hardening — LAN orchestrator rule + log-rotation config + schema-version contract test

As Kamal,
I want the pipeline to refuse to start with an insecure orchestrator config, log retention to be configurable not just defaulted, and a contract test proving every config file and the four event schemas reject incompatible `schema_version` consistently,
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
**When** the test loads `setup.toml`, `expression_map.yaml`, `MoodEvent`, `ActivityEvent`, `SpeechEmotionEvent`, `VocalizationEvent`, and `OrchestratorStreamEvent` each with an unsupported `schema_version`,
**Then** every load/parse raises `SchemaVersionError` with a message naming the file/type, the file's version, and the supported version 2 (NFR27 final enforcement — proves the contract holds across **all** schema-versioned surfaces and **all four event types**, not just the ones already tested epic-by-epic).

**Given** an unit test for the LAN-orchestrator rule,
**When** invalid configurations are loaded (LAN URL with no bearer/mTLS, malformed bearer env reference, mTLS path not readable),
**Then** each raises `StartupValidationError` with the right message.

---

### Story 5.3: systemd service deployment

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
**Then** the pipeline drains in-flight Cartesia frames (Story 2.5 contract), closes httpx clients, calls `EventPublisher.disconnect()` (which closes all four publishers), calls `MoodController` final cleanup if applicable, and exits 0.

**Given** an integration test in `tests/integration/test_systemd_lifecycle.py` (manual on the dev host, not CI),
**When** the test exercises start → trigger turn → SIGTERM → restart → trigger turn,
**Then** both turns complete cleanly and the service journal shows expected lifecycle messages.

---

### Story 5.4: 7-day soak + intent-sleep tuning + mood cadence verification + v1 sign-off

As Kamal,
I want a continuous 7-day run on the dev host under real household ambient with wake-word thresholds tuned against actual conditions, intent-sleep FP/FN tuned via Talker prompt iteration, mood cadence verified against NFR31, USB hot-plug survival confirmed, and a final v1 sign-off checklist,
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

**Given** intent-sleep events during the soak,
**When** Talker `go_to_sleep()` tool calls are tracked across real conversations,
**Then** false-positive rate (Talker fires `go_to_sleep` mid-real-conversation, ending it prematurely) and false-negative rate (Talker misses an actual goodbye, leaves OLAF awake until manual sleep) are measured and tuned to within target via iterative Talker system prompt revision. Final prompt committed in this story.

**Given** mood cadence during the soak,
**When** `MoodEvent` publishes vs `mood.publish_dropped` (cooldown) WARN logs are counted,
**Then** publish rate is ≤ 4 per hour over any sliding 60-min window (NFR31), and the cooldown drops are not so frequent that the user-facing mood is "stuck" — if the soak shows the cooldown rate-limiting too aggressively, the mood enum or Talker prompt is tuned.

**Given** wake-greeting timing during the soak,
**When** `(sleeping → waking transition timestamp → first audio frame of greeting)` is measured for ≥30 wakes,
**Then** the p95 is ≤ 800 ms (NFR30) — including any falls-back-to-static-list invocations.

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
1. **All v1 PRD measurable outcomes** hold over a 30-min representative slice of the soak: no voice / `speech_emotion` drift, no unhandled tag freeze, wake-word reliable at threshold, no audio cutout >100ms (NFR6), no latency target missed by >20%, mood doesn't flicker turn-to-turn, intent-sleep doesn't false-trigger.
2. **NFR1** (simple-turn ≤1500ms p95) verified against soak traffic.
3. **NFR2** (complex-turn ≤1000ms p95) verified against soak traffic.
4. **NFR4** (Cartesia TTS ≤400ms p95) verified.
5. **NFR5** (voice / `speech_emotion` 30–80ms anticipatory p95) verified.
6. **NFR8** (7-day continuous uptime) verified — this story's primary contract.
7. **NFR12, NFR13** (wake-word FP/FN) verified at final threshold.
8. **NFR30** (wake-greeting ≤800ms p95) verified.
9. **NFR31** (`mood` cadence ≤4 publishes/hour) verified.
10. **NFR32** (tool-call dispatch overhead p95 < 50 ms) verified.
11. **Intent-sleep FP/FN** within tuned target; final Talker system prompt committed.
12. **J1, J4, J5** acceptance verified end-to-end in real conditions (wake-with-greeting, intent-sleep, coherent mood across the conversation). J2 (simple turn) and J3 (complex turn) inherit from Epic 2 + 4 integration tests; J7 (unmapped emotion) and J8 (SIGHUP) from Epic 3 + 5 integration tests.
13. **NFR26** (spec-as-contract) — PRD, brief, distillate, and architecture have all been updated wherever implementation deviated; the spec-drift list is empty or has documented rationale for each open item.

**Given** the sign-off completes,
**When** I cut the v1 release tag,
**Then** the release notes summarize: epics shipped (5 epics, 30 stories), NFR results table (target vs measured), known **v1.5-deferred items** (barge-in, cross-restart mood persistence, expanded `working` sub-modes, configurable idle auto-sleep), known **v2-deferred items** (Hailo port, resilience layer, Pi resource calibration), and any architecture deviations. The v1 canonical spec quartet (PRD, brief, distillate, architecture) is tagged at the same commit.

---

## v1.5 Backlog (Post-v1)

These items were deferred from v1 in the 2026-05-06 direction shift to keep v1's quality budget focused on conversation-shape reliability. They are tracked here for v1.5 sprint planning. Story specs are intentionally lightweight (1–2 paragraphs) — full acceptance criteria are written when v1.5 planning begins.

### Story v1.5-1: Barge-in — VAD-during-SPEAKING + flush + cancellation

The barge-in cluster (FR5, FR29, FR30) was the headline v1 capability deferred in the 2026-05-06 direction shift. Activity FSM (Story 4.3) has the `speaking → listening` arrow already in place; v1.5 wires the VAD-during-SPEAKING detection (with sustained-voice threshold to avoid false positives from OLAF's own audio bleed), splitter flush of in-flight `speech_emotion` + `vocalization` events, last-published cache reset, and `OrchestratorClient.cancel(session_id)` (the v1 stub from Story 4.2 becomes real). Activates `tests/integration/test_barge_in.py` (PRD Journey J6).

**v1 stub artifacts to wire:** `OrchestratorClient.cancel()` raises `NotImplementedError` in v1 (Story 4.2). This story replaces the body with the real `HTTP DELETE /turn/{session_id}` call.

**Configurables to add in `setup.toml`:** `[barge_in] sustained_ms = 250`, `[barge_in] energy_threshold = ...`, conservative defaults favoring false-negatives over false-positives initially; final tuning lives in v1.5 soak.

---

### Story v1.5-2: Cross-restart mood persistence

Persist `current_mood` to a small state file (`./state/mood.json` or similar) on shutdown; restore on startup. `MoodState.__init__` reads if present; `MoodController.set` writes through. Default-on with `[mood] persist = true`; off if disabled. Single-file, ~30 LOC. Mood survives systemd restarts (relevant for the 7-day soak; without persistence, every restart resets to `"calm"`).

---

### Story v1.5-3: Expanded `working` sub-modes

Extend `WorkingSubmode = Literal["thinking", "delegating", "searching", "tooling", "composing"]` (additive Literal extension; per CLAUDE.md rule 6 this is forward-compat, no `schema_version` bump needed). Talker prompt updated to indicate which sub-mode applies when. Activity FSM payload schema is forward-compat. New sub-modes published in `ActivityEvent` payloads. Useful when v1.5 adds RAG / web-search tooling beyond the v1 `go_to_sleep` + `set_mood` registry.

---

### Story v1.5-4: Configurable idle auto-sleep fallback

Optional fallback if Talker fails to detect goodbye. `setup.toml` `[activity] idle_auto_sleep_seconds = 0` (off by default; positive value enables). When enabled and no user speech for N seconds while AWAKE, FSM schedules `going_to_sleep` deferred-transition (same path as `go_to_sleep` tool call). For users who want a safety net beyond intent-only sleep — should remain off-by-default since v1 ships intent-only as the canonical behaviour.

---

## Cross-Cutting NFRs

These NFRs aren't owned by a single epic — they're enforced as architectural properties from Epic 1 onward and re-validated each sprint:

- **NFR23–NFR25** (security & redaction): logging/redaction land Epic 1; new credentials added in Epic 2 inherit the `.env` pattern.
- **NFR26** (spec-as-contract): every epic must update PRD, brief, distillate, and architecture if it deviates (the four-document set governed by CLAUDE.md rule 9).
- **NFR28** (testability): every Epic adds tests at the Protocol-mock seam in `tests/unit/`, plus integration tests covering the journey unlocked.
- **NFR29** (JSON logs): established Epic 1, immutable thereafter.
- **NFR31** (mood cadence): established Epic 3 (Story 3.6 — `MoodController.set()` boundary check); soak-validated Epic 5 (Story 5.4).
- **NFR32** (tool-call dispatch overhead bounded): established Epic 4 (Story 4.4 — async-gather text-first, tools-concurrent ordering); soak-validated Epic 5 (Story 5.4).

