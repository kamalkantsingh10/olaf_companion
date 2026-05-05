---
stepsCompleted: [1, 2, 3, 4, 5, 6, 7, 8]
inputDocuments:
  - build_documents/planning-artifacts/prd.md
  - build_documents/planning-artifacts/voice-agent-pipeline-brief.md
  - build_documents/planning-artifacts/voice-agent-pipeline.md
workflowType: 'architecture'
project_name: 'olaf_companion'
component: 'voice-agent-pipeline'
user_name: 'Kamal'
date: '2026-05-05'
lastStep: 8
status: 'complete'
completedAt: '2026-05-05'
---

# Architecture Decision Document — voice-agent-pipeline

_This document builds collaboratively through step-by-step discovery. Sections are appended as we work through each architectural decision together._

## Project Context Analysis

### Component Scope

`voice-agent-pipeline` is a Pipecat-based voice-agent component. Its responsibility:

- **In scope:** local audio I/O, wake-word detection, on-device STT, in-pipeline LLM (Talker), orchestrator dispatch, Cartesia TTS, streaming SSML splitter, **typed expression-event publish to a configurable broadcast channel**, **typed lifecycle-event publish to a configurable broadcast channel**.
- **Out of scope:** physical embodiment (any robot, screen, motor, LED, display), pose interpolation/ease curves, host hardware design.

The component is **consumer-agnostic**. The event schemas and channel names are the only things downstream cares about — and channel names are config values, not architecture.

**v1 deployment platform:** local Linux PC. Pi 5 + Hailo-8L port is v2.

### Requirements Overview

**Functional Requirements (43 total, 8 clusters — v1 active set):**

| Cluster | FRs | Architectural implication (v1) |
|---|---|---|
| Audio I/O & Capture | FR1–FR5 | Always-on wake-word + VAD-bounded capture + barge-in detection in one Pipecat input stage; device pinning by stable name. |
| Speech Recognition | FR6, FR8 | On-device Whisper running on host CPU/GPU as available; confidence-based clarification routing. No Hailo dependency in v1. |
| Conversational Intelligence | FR9–FR12, FR14 | Routing decision (Talker vs orchestrator), in-pipeline LLM client, belief-state HTTP client, streaming consumer; missing `turn_end` cleanup. |
| Voice Synthesis | FR15, FR17 | Cartesia streaming client. |
| Expression Events | FR18–FR25 | Streaming SSML parser, segmentation, mapping table (full primary + secondary + family fallback), audio-frame metadata threading, **publish to configured broadcast channel**, last-published cache. **Top-priority v1 quality bar.** |
| Lifecycle Events | FR26–FR30 | State machine driving the configured lifecycle channel; SPEAKING→LISTENING bypass for barge-in; in-flight expression flush. |
| Configuration & Operations | FR31–FR36 | `setup.toml` + `.env` + `expression_map.yaml` loaders, schema validation, SIGHUP atomic swap (mapping only), systemd unit. |
| Observability & Diagnostics | FR37–FR43 | Structured JSON logs, level discipline, local rotation, no telemetry, no persistence. |

**Non-Functional Requirements (v1 active set):**

| Category | v1-active NFRs | Architectural driver |
|---|---|---|
| Performance | NFR1–NFR7 | Audio frame metadata anchoring; minimal buffering; concurrency keeps critical path off blocking calls. Validated on local PC for v1; Pi calibration in v2. |
| Reliability | NFR8, NFR10, NFR11 | systemd restart-on-failure is the v1 recovery; soak testing on real ambient. |
| Wake-word accuracy | NFR12, NFR13 | Tuning targets at production threshold; soak validates. |
| Resource | (Spirit only in v1) | Don't be a hog on the host. Pi 5-specific thresholds (NFR14–18) are v2 calibration. |
| Integration | NFR21 | Configured broadcast channel uses reliable delivery (RELIABLE QoS on DDS). |
| Security | NFR23–NFR25 | Credentials from `.env` loaded once at startup; TLS strict; logger filter for raw audio/transcripts. |
| Maintainability | NFR26–NFR29 | Components testable in isolation with mock/synthetic inputs; schema versioning; canonical specs. |

### Scale & Complexity

- **Primary domain:** real-time voice-agent software (publishes events to a broadcast bus, agnostic about consumers)
- **Complexity level:** medium — driven by latency budgets and concurrency, not feature count
- **Estimated logical components:** ~12–14 (most as Pipecat processors)
- **Audience:** single-user (Kamal); no multi-tenancy, no auth surface beyond API keys, no compliance regime

### V1 Posture: Hard Dependencies, Fail-Fast

External dependencies are **hard-required** in v1. At startup the pipeline validates:

- Cartesia API key configured + reachable
- Active Talker provider's API key configured + reachable (one of OpenAI / Groq / Gemini per `setup.toml`)
- Orchestrator daemon reachable
- Broadcast bus connection established (publisher initialized for both expression and lifecycle channels)
- Audio devices (mic, speaker) resolvable by their configured names

If any check fails, the pipeline refuses to start with a clear error.

**At runtime, failures crash the process; systemd restarts it.** No retry-with-backoff, no partial-mode fallbacks, no in-process recovery. This trades resilience for simplicity so v1's quality budget concentrates on **expression-mapping completeness**.

### Deferred to v2

These FRs/NFRs remain in the spec but are explicitly out of v1 scope.

**Resilience layer (per-external-dep adapter + degradation policy):**

| Dependency | Deferred FRs/NFRs | v2 behavior |
|---|---|---|
| Talker (OpenAI) | NFR22 | Reroute to orchestrator slow-path on Talker failure |
| Orchestrator stream | FR13, NFR20 | Stall heartbeat + filler response; reconnect on disconnect |
| Cartesia | FR16, NFR19, NFR9 | Retry with exponential backoff; text-only degraded mode signaled by `<emotion value="sad"/>` event; 5s recovery target |

**Pi + Hailo-8L port (deployment / optimization phase):**

| Concern | Deferred FRs/NFRs | v2 behavior |
|---|---|---|
| Hailo-8L acceleration | FR7, FR41 | Use Hailo-8L when present for Whisper inference; CPU fallback path with logged warning if missing |
| Pi-specific resource thresholds | NFR14, NFR15, NFR16, NFR17, NFR18 | Calibrate to Pi 5 envelope; thermal headroom; active cooling |

**v1 design constraint:** components touching external deps sit behind thin adapters so v2 can drop in resilience policy without restructuring. STT inference is encapsulated so the v2 port can swap CPU Whisper for a Hailo-accelerated path without rippling.

### Expression-Mapping Completeness — The V1 Quality Bar

v1 implements the full Cartesia tag → expression-event mapping with no silent gaps:

- All 6 primary emotions (`neutral, content, excited, sad, angry, scared`) with full payload (base pose, eye state, LED color/intensity — values negotiated with the embodiment project; this pipeline just publishes them as data)
- All 6 secondary emotions (`happy, curious, sympathetic, surprised, frustrated, melancholic`) — distillate v1 maps these to primaries; given the "as perfect as possible" intent, evaluate during architecture whether to lift to first-class poses in v1
- Full fallback family table covering all 60+ Cartesia tags via 7 families + `unknown → neutral`
- Burst events (`[laughter]` Cartesia-supported; `[sigh] / [gasp] / [clears_throat]` parsed and emitted on broadcast though stripped from TTS)
- `expression_map.yaml` schema-validated, SIGHUP-reloadable, atomic swap

#### Extensibility — Adding a New Expression Must Stay Simple

Beyond v1 launch, adding new Cartesia tags (or any custom expression value) must be a **config change with no code touchpoints**. The architecture must guarantee:

- **Streaming SSML splitter accepts any tag value.** It does not validate against a known set; unrecognized tags flow through to the mapping resolver.
- **`expression_map.yaml` schema is open-ended.** Adding a new entry under `emotions:` (with payload) makes a tag first-class. Adding it to a family in `fallback_families:` makes it fall back to existing behavior. Either path is a YAML edit.
- **Publisher passes through arbitrary payload fields.** The wire schema for `ExpressionEvent` is permissive — adding a new field in `expression_map.yaml` (e.g., `haptic_intensity`) results in that field appearing in the published event. Downstream consumers ignore unknown fields (standard forward-compat).
- **SIGHUP reload covers the activation path.** No restart required for either fallback or first-class additions.

The "add a new expression" workflow is:

1. Edit `expression_map.yaml`. Either add `emotions.<name>` with payload (first-class) **or** add `<name>` to a family in `fallback_families` (fallback to existing behavior).
2. `kill -HUP <pid>`.
3. Done. Next time the LLM emits `<emotion value="<name>"/>`, the pipeline routes it correctly.

This workflow is the architectural test for any future internal change: **if a refactor breaks this two-step extension story, the refactor is wrong.**

### Project-Scoped Configuration

| File | Role | Lifecycle |
|---|---|---|
| `setup.toml` | Service config: transport, STT/TTS providers, Talker model, daemon URL, audio device names, broadcast publisher implementation + channel names, DDS domain ID. (Renamed from `pipeline.toml` in PRD/distillate.) | Loaded at startup; restart required for changes |
| `.env` | Credentials only: Cartesia + active Talker provider key (one of `OPENAI_API_KEY` / `GROQ_API_KEY` / `GEMINI_API_KEY`) | Loaded once at startup; not re-read at runtime |
| `expression_map.yaml` | Cartesia tag → expression-event mapping + fallback family table | Loaded at startup; SIGHUP-reloadable with atomic swap |

All three files are **project-scoped**. No cross-project shared config; the orchestrator and embodiment project maintain their own.

### Technical Constraints & Dependencies

**Locked architectural constraints (from distillate §2, generalized):**

1. Single fan-out point at the splitter
2. Single-writer belief state (orchestrator only; pipeline is read-only consumer)
3. Audio-frame anchored expression events (30–80ms anticipatory)
4. Mapping is data, not code (`expression_map.yaml`, SIGHUP-reloadable)
5. Talker lives inside Pipecat
6. Pipeline only publishes voice-driven expression events (idle/non-voice behaviors are downstream concerns)

**Pre-decided technology choices** (architecture must conform):

- **Pipecat** — voice loop framework
- **Cartesia Sonic-3** — cloud TTS, streaming
- **Whisper** — on-device STT, running on host CPU/GPU as available in v1. STT inference is encapsulated behind an interface so a Hailo-8L-accelerated implementation can drop in for the v2 Pi port.
- **Groq llama-3.1-8b-instant** — Talker LLM v1 default (Story 2.2 final revision: was claude-haiku-4-5 originally; brief intermediate landing on OpenAI gpt-5.4-nano; final pick is Groq Llama 8B Instant for NFR1 latency headroom — measured ~150–270 ms per turn vs OpenAI's ~1–1.7 s and Anthropic's ~600–900 ms TTFB on this hardware). The Talker is **provider-agnostic via factory**: OpenAI, Groq, and Gemini are all wired out of the box (each speaks the same `openai` SDK via openai-compatible endpoints) and the operator swaps providers by changing one line in setup.toml — `[talker] provider = "<openai|groq|gemini>"`.
- **systemd** — process supervision and v1 recovery model
- **HTTP / SSE / WebSocket** — orchestrator transport
- **Broadcast publisher** — generic `ExpressionPublisher` interface; v1 implementation is **ROS 2 / DDS** (reliable QoS). The interface is the architecture; ROS 2 is the v1 implementation behind it. Channel names, DDS domain, and (eventually) implementation choice are configured in `setup.toml`.

**Stable contracts** (must survive any future rewrite of internals):

- `POST /turn` request/response schema with the orchestrator
- **`ExpressionEvent`** schema *(working name — was `OlafAction` in PRD/distillate; needs settling)*
- **Lifecycle event** schema
- **`ExpressionPublisher` interface** — connect / health-check / publish_expression / publish_lifecycle. Stable across transport changes.
- `setup.toml`, `.env`, and `expression_map.yaml` schemas

**Notable risk vectors:**

- Audio-frame metadata threading through Pipecat → if the processor model can't carry metadata cleanly, fall back to time-based correlation (documented deviation if used)
- Broadcast bus reliability on home network → if ROS 2/DDS, mitigation is explicit DDS domain + colocate or wired LAN

### Cross-Cutting Concerns Identified

1. **Real-time audio path** — every component sits inside Pipecat's frame pipeline; concurrency model and frame-metadata threading are the critical-path decisions.
2. **Configuration architecture** — `setup.toml` (boot-time) + `.env` (boot-time, secrets) + `expression_map.yaml` (hot-reload), schema-validated at every load, atomic swap on reload, SIGHUP handler for the mapping only.
3. **Observability** — structured JSON logging, redaction at INFO+ level, local rotation, no telemetry.
4. **Fail-fast on external dependency failure (v1)** — startup-validate, refuse to start if any missing, crash + systemd restart on runtime failure. Per-dependency adapters keep v2 resilience clean to drop in.
5. **Security & privacy** — credentials hygiene (`.env`, 0600), TLS strict, no audio/transcript persistence.
6. **Testability** — components mockable in isolation per NFR28.
7. **Spec-as-contract** — PRD/brief/distillate updated alongside code (NFR26).
8. **Expression-mapping completeness AND extensibility** — full primary + secondary mapping + complete fallback family table covering all 60+ Cartesia tags is the v1 launch quality bar. The architecture additionally guarantees that adding new expression tags (first-class or fallback) is forever a config-only operation — no code changes required.
9. **Pluggable publisher transport** — broadcast publishing sits behind a generic `ExpressionPublisher` interface (connect, health-check, `publish_expression`, `publish_lifecycle`). v1 ships **one implementation: `Ros2ExpressionPublisher`** (DDS, reliable QoS). Future transports — MQTT, in-process callback, anything else — implement the same interface. Selection is a `setup.toml` value (`transport = "ros2"` for v1). The splitter, mapping resolver, and upstream pipeline never reference ROS 2 directly; they only call the interface. Out of scope for v1: building any non-ROS 2 implementation. In scope for v1: defining the interface and isolating ROS 2 behind it.
10. **Encapsulated STT inference** — Whisper STT sits behind an inference interface so the v1 CPU/GPU path and the v2 Hailo-8L-accelerated path are interchangeable without changes elsewhere in the pipeline.

### Naming & Spec-Drift Notes

PRD/brief/distillate use OLAF-coupled vocabulary and Pi-specific assumptions throughout. To align with the scope decisions in this analysis, those documents need a rename/refactor pass:

- `OlafAction` → `ExpressionEvent` (working name; final name TBD)
- `/olaf/expression`, `/olaf/lifecycle` → configured channel names in `setup.toml`
- `pipeline.toml` → `setup.toml`
- Inline secrets file reference → `.env`
- Drop "OLAF embodiment" framing from stakeholder/scope sections
- Mark deferred resilience FRs/NFRs (FR13, FR16, NFR9, NFR19, NFR20, NFR22) as v2 — resilience layer
- Mark deferred Hailo/Pi FRs/NFRs (FR7, FR41, NFR14–18) as v2 — Pi port + optimization phase
- v1 deployment target is local Linux PC; PRD's IoT/Embedded section reframes for v2 Pi port

This rename/alignment pass is a follow-up to the architecture workflow — clean separate edit.

## Starter Template Evaluation

### Primary Technology Domain

Real-time voice-agent service in Python, built on the **Pipecat** framework. Not a web app, not a CLI — a long-running asyncio service that owns a single audio loop and publishes typed events on broadcast channels.

### Starter Options Considered

There is no canonical `create-foo-app` generator that fits this exact shape. Two paths considered:

1. **Hand-roll a Pipecat skeleton.** Read Pipecat docs, build the project layout from scratch. Maximum control, more work, drift risk vs. upstream best practices.
2. **Use Pipecat's own scaffold** (`uv init` + `uv add pipecat-ai`, or `pipecat init quickstart`). Aligns with Pipecat docs and examples; strip WebRTC/browser bits we don't need; add local audio I/O and the ROS 2 publisher.

**Selected: option 2.** Pipecat is a hard dep; tracking their scaffold reduces drift as the framework evolves.

### Selected Starter: Pipecat Quickstart + Modern Python Service Skeleton

**Rationale for Selection:**

- Pipecat's idioms (processors, frame pipeline) define the spine we live inside; their scaffold uses those idioms natively.
- WebRTC/browser bits in the quickstart are strippable; what remains (pipeline definition, processor wiring, asyncio entry point) is exactly what we need.
- Modern Python tooling (uv, ruff, pyright, pytest, pydantic-settings, structlog) layers cleanly on top — not Pipecat-provided but standard 2026 service practice.

**Initialization Command:**

```bash
uv init voice-agent-pipeline --python 3.12
cd voice-agent-pipeline
uv add pipecat-ai
uv add openai cartesia pydantic pydantic-settings structlog
uv add --dev ruff pyright pytest pytest-asyncio
# rclpy is installed via ROS 2 distro (e.g., apt install ros-jazzy-rclpy)
# and exposed to the venv via PYTHONPATH or system site-packages
```

**Architectural Decisions Provided by Starter:**

**Language & Runtime:**
- **Python 3.12+** (Pipecat requires 3.11 minimum, 3.12+ recommended)
- **asyncio** as the native concurrency model (Pipecat is asyncio-native)
- **`src/` layout** — prevents accidental imports of in-tree modules; modern Python preference

**Dependency Management:**
- **uv** — single tool for project init, deps, lockfile, Python version pin, virtualenv, task running
- `pyproject.toml` as the source of truth; `uv.lock` committed
- No `requirements.txt`, no pyenv, no separate venv tooling

**Lint + Format:**
- **ruff** for both lint and format (replaces black, isort, flake8, pyupgrade)
- Single config block in `pyproject.toml`

**Type Checking:**
- **pyright** in strict mode for project source; relaxed for tests
- Per-directory strictness lets us isolate Pipecat type quirks if they arise

**Testing:**
- **pytest** + **pytest-asyncio** for async-aware tests
- Layout: `tests/unit/` for per-component (mockable per NFR28), `tests/integration/` for end-to-end with mocked external services

**Configuration:**
- **pydantic-settings** for `setup.toml` + `.env` loading and schema validation
- Pydantic models double as the config schema and the schema-version gate (NFR27)

**Logging — mature, project-rooted, file-first strategy:**
- **structlog → stdlib `logging` → `RotatingFileHandler`.** structlog handles JSON shaping and the redaction processor pipeline; stdlib handles file rotation (well-trodden, process-safe).
- **Logs in `./logs/`** at the project root, not journald, not `/var/log`. Three streams:
  - `voice-agent.log` — main app log, INFO+
  - `errors.log` — WARN+ only (faster post-mortem scan)
  - `debug.log` — DEBUG, opt-in via `LOG_LEVEL=DEBUG`, includes transcripts (FR39 — gated, off by default)
- **Rotation:** size-based (e.g., 50MB per file), keep N rotated copies; retention default 7 days (NFR40), all configurable in `setup.toml`.
- **JSON-only output** (NFR29) — every line a parseable object.
- **Redaction processor** strips raw audio bytes and credential material before serialization (NFR25).
- **Console mirror** during dev via `LOG_CONSOLE=true` env var; off in production.
- **systemd integration** — only systemd's own lifecycle messages (start/stop/crash) hit journald; app logs stay in `./logs/`.

**External SDKs:**
- `openai` (official) — Talker LLM (Story 2.2 revision)
- `cartesia` (official) — Sonic-3 TTS streaming
- `rclpy` — ROS 2 Python binding, **system-installed via ROS 2 distro** (not via uv). Documented in README.

**Out of starter scope (decisions deferred to later architecture steps):**
- Wake-word library (openWakeWord vs Picovoice Porcupine vs others)
- VAD library (Pipecat ships Silero VAD; likely fine — confirm)
- Whisper Python binding (faster-whisper, openai-whisper, transformers) — fits behind the STT inference interface
- systemd unit file shape, journald integration
- Pre-commit hooks, Makefile/justfile, Docker dev loop — TBD

**Note:** Project initialization using the commands above should be the first implementation story.

## Core Architectural Decisions

### Decision Priority Analysis

**Critical (block implementation):** Audio backend, wake-word, VAD, STT binding + interface, splitter implementation, audio-frame metadata threading, Talker placement, async model, segmentation, publisher interface, event schemas, schema versioning, type names, DDS wire format, HTTP client, orchestrator stream transport, belief-state read pattern, retry semantics, systemd unit, redaction processor, audio device pinning.

**Important (shape architecture):** Test organization, task runner, project root layout, SSE event dispatch policy, cross-project health-check contract.

**Deferred:** Health/readiness signaling beyond systemd (post-v1), pre-commit framework (skip — AI runs `just check`), CI tooling (deferred to deployment work), schema-version bump policy (only on breaking change).

### Audio + STT Pipeline (Batch 1)

| Decision | Choice | Notes |
|---|---|---|
| Audio I/O backend | **Pipecat `LocalAudioTransport`** (PyAudio) — `pipecat-ai[local]` extras + system `portaudio`. | Configurable `input_device_index`/`output_device_index`. |
| Wake-word library | **Picovoice Porcupine** (`pvporcupine`). Personal-use free tier; custom phrase trained via Picovoice console → `models/wakeword/hey_olaf.ppn`. | Higher accuracy than openWakeWord. New credential `PICOVOICE_ACCESS_KEY` in `.env` and startup validation. |
| VAD | **Silero VAD** (Pipecat-bundled). | No alternative worth evaluating. |
| Whisper Python binding (v1) | **faster-whisper** (CTranslate2, ~4× faster than reference, CPU + GPU + INT8). | Boring choice. Sits behind STT inference interface; v2 Hailo path swaps in cleanly. |
| STT inference interface | `STTBackend` Protocol — `async transcribe(audio) -> TranscriptionResult`. v1 implementation: `WhisperBackend` (faster-whisper). v2: `HailoWhisperBackend`. Selection via `setup.toml` `[stt] backend = "whisper-cpu"`. | Same adapter pattern as `ExpressionPublisher`. |

### Streaming + Concurrency (Batch 2)

| Decision | Choice | Notes |
|---|---|---|
| Streaming SSML parser | **Hand-rolled state machine**, ~50–100 LOC, zero-dep. | Distillate §8 specifies this. Cartesia tag grammar is small enough that library overhead is unjustified. |
| Audio-frame metadata threading | **Extend Pipecat's `AudioRawFrame` with optional `expression_event` metadata**. Splitter attaches metadata; transport processor reads on frame send and calls `ExpressionPublisher.publish_expression(event)`. | Direct fit to constraint #3 (audio-frame anchored). PRD risk fallback: time-based correlation if Pipecat's frame model can't carry metadata cleanly. |
| Talker placement in Pipecat | **Single `TurnRouter` processor** owning both Talker (openai async client) + orchestrator client. Reads transcript frames, decides fast vs. slow path, emits text-with-tags frames downstream to splitter. | Talker and orchestrator clients are TurnRouter dependencies (Protocols), not separate processors. Easier to mock and test. |
| Async/concurrency model | **asyncio everywhere**; sync libraries (`faster-whisper`, `rclpy.publish`, `pvporcupine.process`) wrapped in `asyncio.to_thread(...)`. | Three async-native clients: `openai.AsyncOpenAI`, Cartesia SDK streaming, `httpx.AsyncClient`. Sync wrappers isolated to the boundary. |
| Segmentation strategy | **Boundary-based emission inside the streaming state machine**: emit on whichever comes first — sentence terminator (`.?!`), emotion tag boundary, or burst tag. State: `current_buffer`, `current_emotion`, `last_published_emotion` (FR24 dedup). | Direct implementation of FR19 + distillate §8.4. |
| Routing rule (TurnRouter) | Config-driven keyword/regex list in `setup.toml` for v1. | Open question: hot-reloadable via SIGHUP like `expression_map.yaml`? Lean yes (config-only extensibility theme). |

### Publisher Contract + Event Schemas (Batch 3)

| Decision | Choice | Notes |
|---|---|---|
| `ExpressionPublisher` interface | Async Protocol: `connect()`, `disconnect()`, `is_healthy() -> bool`, `publish_expression(event)`, `publish_lifecycle(event)`. Fire-and-forget; errors raise. | Aligned with v1 fail-fast. v2 swap to queue-based behind same interface. |
| `ExpressionEvent` schema | **Frozen pydantic v2 model**. Structural fields: `schema_version: int`, `event_type: Literal["expression"]`, `emotion: str` (resolved name), `source_tag: str` (original Cartesia tag), `audio_frame_id: str \| None`, `timestamp_ns: int`. **Open `payload: dict[str, Any]`** holds all embodiment-specific fields from `expression_map.yaml`. | Open `payload` is the extensibility seam (concern #8). Pose/eye/LED/burst/anything new lives in `payload` — wire schema stable, payload evolves. |
| `LifecycleEvent` schema | Frozen pydantic v2 model: `schema_version: int`, `event_type: Literal["lifecycle"]`, `state: Literal["SLEEPING","LISTENING","THINKING","SPEAKING","IDLE"]`, `timestamp_ns: int`, `payload: dict[str, Any] = {}`. | Symmetric with ExpressionEvent (both use `payload`). State enum is FR26. |
| Schema versioning | **Integer `schema_version`** on every event AND every config file. Bumped only on breaking changes; forward-compat additions don't bump. | NFR27 satisfied. Pipeline refuses to load configs/parse events with unsupported version. |
| Type naming (final) | **`ExpressionEvent`** + **`LifecycleEvent`**. Rename from PRD/distillate `OlafAction` complete. | Logged on spec-drift list. |
| DDS wire format | ROS 2 `.msg` IDL mirrors pydantic structural fields; `payload` serializes as **JSON-encoded string field** (`string payload_json`). | Trade typed-DDS purity for extensibility (concern #8). JSON encoding cost is ~µs. |

### External Clients (Batch 4)

| Decision | Choice | Notes |
|---|---|---|
| HTTP client library | **`httpx` (async)** + **`httpx-sse`** for orchestrator stream parsing. | Modern, async-first. |
| Orchestrator stream transport | **SSE for v1** (`POST /turn` returns `text/event-stream`). Cancellation via separate **`HTTP DELETE /turn/{session_id}`** for barge-in. | WebSocket only if SSE+DELETE proves too slow for barge-in (validate Phase 2). |
| Belief-state read | **Per-turn fresh `GET /beliefs?keys=...`, no cache**. | Talker invocations are infrequent; staleness is worse than the latency cost. |
| Connection management | Persistent `httpx.AsyncClient` per service, lifecycle bound to pipeline startup/shutdown. Startup validation: connect + `GET /health` against orchestrator daemon. | Keep-alive avoids per-request reconnection. |
| Retry semantics (v1) | **None.** First failure raises → process crashes → systemd restarts. | Aligned with v1 fail-fast. v2 deferred work adds retry+backoff. |
| SSE event dispatch | Dispatch by `type` field. **Unknown types → log WARN + ignore** (forward-compat for orchestrator evolution). Framing/JSON errors → raise → crash. | Same forward-compat principle as `payload` dict. |
| Streaming consumption | TurnRouter consumes SSE async iterator, yields each parsed event downstream to splitter as it arrives. **No buffering for full-stream completion.** | Real-time latency is the contract. |
| Cross-project integration | Orchestrator daemon must expose `GET /health` returning 200. **Logged on spec-drift list.** | Coordination point with orchestrator project. |

### Operations: systemd, Redaction, Tests (Batch 5)

| Decision | Choice | Notes |
|---|---|---|
| systemd unit | `Type=simple`, `Restart=on-failure`, `RestartSec=5`, `WorkingDirectory` pinned, `User=<dev>`, `StartLimitInterval=60` / `StartLimitBurst=5`. **App reads `.env` directly via pydantic-settings**; systemd doesn't touch credentials. Unit committed at `deploy/systemd/voice-agent-pipeline.service`. | systemd's `EnvironmentFile` syntax is fussy. App-owned `.env` is cleaner. |
| Redaction processor | structlog denylist processor before JSON serializer: drops `audio_bytes`, `audio_data`, `pcm`, plus any field name matching `*api_key`, `*token`, `*password`, `*secret`. Transcripts (`transcript`, `user_text`) only at DEBUG level (FR39, NFR25). | Belt + suspenders for NFR25. Auditable list in code. |
| Test organization | `tests/unit/` (mocked deps, fast), `tests/integration/` (full pipeline, mocked external services), `tests/contract/` (pydantic schemas + DDS round-trip). All run in CI via `uv run pytest`. | NFR28 satisfied; mocks at Protocol seams. e2e = Phase 0–3 manual soak, not CI. |
| Health/readiness | **None for v1.** Process-up = ready; systemd restart-on-failure is the only signal. | `Type=notify` is post-v1 if needed. |
| Audio device pinning (FR4) | Startup helper `resolve_audio_devices(config)` resolves device-name regex → PyAudio index. Refuses to start if no match. | Indices shift across reboots/USB hot-plug; name-regex is the standard fix. |
| Pre-commit hooks | **No `pre-commit` framework.** AI partner runs `just check` (ruff + pyright + fast pytest subset) per `CLAUDE.md` rules. | Solo dev + AI partner workflow. |
| Task runner | **`justfile`** at project root. Recipes: `run`, `check`, `test`, `reload`, `lint`, `format`. `uv` handles deps natively. | Modern boring choice in 2026. |
| Project root layout | Committed: `pyproject.toml`, `uv.lock`, `justfile`, `setup.toml`, `expression_map.yaml`, `models/wakeword/hey_olaf.ppn`, `deploy/systemd/voice-agent-pipeline.service`, `.env.example`, `CLAUDE.md`. Gitignored: `.env`, `./logs/`, `.venv/`. | Conventional. `.env.example` is the schema template. |

### Decision Impact Analysis

**Implementation sequence (suggested order; refines into stories in step 5+):**

1. **Bootstrap** — `uv init`, dependencies, `pyproject.toml`, `justfile`, `CLAUDE.md`, `.env.example`, `.gitignore`, project layout.
2. **Config + secrets** — `pydantic-settings` models for `setup.toml` + `.env`, schema validation, `expression_map.yaml` loader with SIGHUP atomic swap.
3. **Logging** — structlog setup with redaction processor + rotating file handlers in `./logs/`.
4. **Event schemas** — `ExpressionEvent`, `LifecycleEvent` pydantic models + ROS 2 `.msg` IDL files.
5. **`ExpressionPublisher` interface + `Ros2ExpressionPublisher` implementation** — startup connect, health check, publish methods.
6. **STT inference interface + WhisperBackend** — async `transcribe`, `asyncio.to_thread` wrapping faster-whisper.
7. **Wake-word + VAD + audio I/O** — Pipecat LocalAudioTransport, `pvporcupine` integration, audio device pinning.
8. **Streaming SSML splitter** — state machine, segmentation, audio-frame metadata attachment, last-published cache.
9. **External clients** — `OrchestratorClient` (httpx + SSE), `BeliefStateClient` (httpx), `TalkerClient` (openai SDK), `TTSClient` (cartesia SDK) — all behind Protocols.
10. **`TurnRouter`** — routing rule from config, fast-path/slow-path dispatch, streaming SSE consumer.
11. **Lifecycle state machine** — transitions on observable events, publishes `LifecycleEvent` via the publisher.
12. **Wiring + entry point** — Pipecat pipeline assembly, async lifecycle, systemd unit, soak preparation.

**Cross-component dependencies:**

- `ExpressionPublisher` interface (item 5) is depended on by splitter (item 8) and lifecycle (item 11) — must land first.
- `STTBackend` interface (item 6) is depended on by audio I/O (item 7) — likewise.
- `TurnRouter` (item 10) depends on splitter (item 8) AND all four external clients (item 9). The integration point.
- Logging (item 3) is depended on by everything — early wins.
- Config (item 2) is the substrate — every other item reads from it.

## Implementation Patterns & Consistency Rules

### Pattern Categories Defined

**Critical Conflict Points Identified:** ~10 areas where AI agents could plausibly drift. Each has a single named convention below.

### Module & File Layout

Source tree under `src/voice_agent_pipeline/` is organized **by domain, not by layer**:

```
src/voice_agent_pipeline/
├── __main__.py              # entry point: argparse, signal handlers, asyncio.run
├── pipeline.py              # Pipecat pipeline assembly + lifecycle orchestration
├── audio/                   # LocalAudioTransport wiring, wake-word, VAD, device pinning
├── stt/                     # STTBackend Protocol + WhisperBackend implementation
├── turn/                    # TurnRouter, Talker, orchestrator + belief-state clients
├── tts/                     # Cartesia client wrapper
├── splitter/                # streaming SSML state machine, segmentation
├── publisher/               # ExpressionPublisher Protocol + Ros2ExpressionPublisher
├── lifecycle/               # state machine, lifecycle event emission
├── config/                  # pydantic-settings models, expression_map loader, SIGHUP
├── logging/                 # structlog setup, redaction processor
├── schemas/                 # ExpressionEvent, LifecycleEvent (shared types)
└── errors.py                # custom exception hierarchy (single file)
tests/
├── unit/                    # mirrors src/ structure: tests/unit/splitter/test_state.py
├── integration/             # full-pipeline tests with mocked external services
└── contract/                # pydantic + DDS round-trip schema stability
```

**Rule:** new functionality goes in the domain package it belongs to. Cross-domain helpers go in a new package, never in a misc `utils/` dumping ground.

### Naming Conventions

**One convention across every format:** `snake_case` everywhere AI agents write keys.

| Surface | Convention | Example |
|---|---|---|
| Python modules/packages | `snake_case` | `voice_agent_pipeline.publisher.ros2` |
| Python classes | `PascalCase` | `Ros2ExpressionPublisher`, `ExpressionEvent` |
| Python functions/variables | `snake_case` | `publish_expression`, `audio_frame_id` |
| Python constants | `UPPER_SNAKE_CASE` | `DEFAULT_LOG_RETENTION_DAYS` |
| Pydantic model field names | `snake_case` | `audio_frame_id`, `schema_version` |
| TOML keys (`setup.toml`) | `snake_case` | `[stt] backend = "whisper-cpu"` |
| YAML keys (`expression_map.yaml`) | `snake_case` | `base_pose: { yaw: 0, pitch: -5 }` |
| DDS `.msg` IDL field names | `snake_case` | `int32 schema_version` |
| `payload` dict keys (forward-compat) | `snake_case` | `payload["led_intensity"]` |
| structlog log field keys | `snake_case` | `event="lifecycle.transition", from_state="LISTENING"` |
| Test files / functions | `test_<thing>.py` / `def test_<behavior>():` | `test_splitter_state.py::test_emits_on_sentence_terminator` |

**Rule:** if an AI agent is tempted to write `camelCase` or `kebab-case` because a library/format "prefers" it, the answer is no — uniformity beats local convention here.

### Type System Conventions

| Use case | Mechanism | Notes |
|---|---|---|
| **Interfaces / seams** | `typing.Protocol` | Examples: `STTBackend`, `ExpressionPublisher`, `OrchestratorClient`, `BeliefStateClient`, `TalkerClient`, `TTSClient`. Never use `abc.ABC` for these. |
| **Events / config / data models** | `pydantic.BaseModel` v2 | `model_config = ConfigDict(frozen=True, extra="forbid")` for events. `extra="allow"` only on the `payload` field's value type, not the whole model. |
| **Enum-like fixed values** | `typing.Literal[...]` | `state: Literal["SLEEPING","LISTENING","THINKING","SPEAKING","IDLE"]`. Don't reach for `enum.Enum`. |
| **Internal trivial structs** | `@dataclass(frozen=True)` | Only when pydantic validation is overkill; never crosses a boundary. |
| **Type hints** | Required everywhere; pyright strict for `src/`, basic for `tests/` | No `Any` except the documented `payload: dict[str, Any]` extensibility seam. No `# type: ignore` without an inline reason comment. |

### Error Handling

Custom exception hierarchy in `errors.py`:

```python
class VoiceAgentError(Exception): ...                        # root
class ConfigError(VoiceAgentError): ...                      # invalid setup.toml/.env/expression_map.yaml
class SchemaVersionError(ConfigError): ...                   # incompatible schema_version
class StartupValidationError(VoiceAgentError): ...           # missing dep at startup (Cartesia, daemon, etc.)
class ExternalServiceError(VoiceAgentError): ...             # base for external failures
class CartesiaError(ExternalServiceError): ...
class OrchestratorError(ExternalServiceError): ...
class TalkerError(ExternalServiceError): ...
class PublisherError(VoiceAgentError): ...                   # publisher-side failure
class SplitterError(VoiceAgentError): ...                    # parser/state-machine failure
```

**Rules:**

- **v1 fail-fast:** don't catch `ExternalServiceError` (or subclasses) anywhere. Let it propagate, crash, systemd restarts. Resilience layer in v2 will introduce structured catches at the adapter boundary.
- Catch only at the process-level handler in `__main__.py` (log + exit non-zero).
- `raise X from y` to preserve the cause chain.
- Never swallow exceptions with bare `except:` or `except Exception:` (lint rule).
- Custom exceptions carry context as init kwargs, not f-string-baked messages.

### Logging Conventions

```python
import structlog
log = structlog.get_logger(__name__)

# always include event name (verb.subject form)
log.info("lifecycle.transition", from_state="LISTENING", to_state="THINKING", session_id=sid)
log.warning("emotion.unmapped", source_tag="enthusiastic", fallback="excited", family="high_energy_positive")
log.error("publisher.publish_failed", channel="expression", error=str(exc))
```

**Rules:**

- **Event field is mandatory**, in `verb.subject` or `subject.verb` form (be consistent within a module).
- Bind per-turn context via `bind_contextvars(session_id=..., audio_frame_id=...)` so every log line in that turn carries it.
- **Level discipline:**
  - `DEBUG` — transcripts (`user_text`, `transcript`), raw event payloads, splitter token-by-token state
  - `INFO` — lifecycle transitions, config reloads, fallback resolutions, turn boundaries, startup completion
  - `WARN` — unmapped emotions falling to `unknown → neutral`, config drift signals, schema version mismatch (recoverable)
  - `ERROR` — external service failures, validation failures, publish failures
  - `CRITICAL` — process-fatal errors immediately before crash
- Never log raw audio bytes, credentials, or (at INFO+) transcripts. The redaction processor enforces this; don't rely on the processor — write code that doesn't pass these in.
- Log keys in `snake_case`; values that are durations are `_ms` or `_ns` suffixed.

### Async Patterns

| Pattern | Convention |
|---|---|
| Synchronous library at the boundary | `await asyncio.to_thread(sync_call, ...)` — never block the event loop |
| Parallel independent awaits | `await asyncio.gather(a, b, c)` |
| Sequential where ordering matters | individual `await` |
| Client lifecycle | `async with httpx.AsyncClient() as client:` — context-managed; don't manage `aclose()` manually |
| Long-running pipeline | one top-level `asyncio.run(main())` in `__main__.py`; everything below is awaitable |
| Cancellation | catch `asyncio.CancelledError`, clean up, re-raise — never swallow |

### Schema Conventions

- Every persisted schema (config files, events on the wire) carries an integer `schema_version` field.
- Pipeline refuses to load/parse on unsupported version; raises `SchemaVersionError`.
- Backward-compat additions (new optional fields, new payload keys) **don't bump** the version — they're forward-compat by design.
- Breaking changes (rename, removal, type change) bump the version.

### Test Patterns

- File layout mirrors `src/` exactly. `tests/unit/splitter/test_state_machine.py` ↔ `src/voice_agent_pipeline/splitter/state_machine.py`.
- One behavior per test. Test name describes the behavior: `test_emits_on_sentence_terminator`, `test_publish_raises_when_disconnected`.
- **Mock only at Protocol boundaries.** `STTBackend`, `ExpressionPublisher`, etc. Never mock internal functions or pydantic models.
- Use `pytest-asyncio`; mark async tests with `@pytest.mark.asyncio`.
- `conftest.py` at directory level for shared fixtures.
- Integration tests stand up real Pipecat pipelines with Protocol mocks for external services. Contract tests verify pydantic ↔ JSON ↔ DDS round-trip.

### Imports

- **Absolute imports** within the package: `from voice_agent_pipeline.publisher.ros2 import Ros2ExpressionPublisher`.
- Three groups, separated by a blank line: stdlib, third-party, local. ruff-isort enforces.
- No wildcard imports. No relative imports beyond a single `.` for sibling modules in the same subpackage.

### Documentation

- Public Protocols and event models carry one-line class docstrings explaining the contract.
- Module docstrings only when the module's purpose isn't obvious from the path.
- **No function-level docstrings by default.** Type hints + the function name carry the weight. Add a docstring only when the WHY is non-obvious (an invariant, a workaround, a constraint).
- No comments unless they explain WHY. Don't narrate WHAT — well-named identifiers do that.

### Enforcement Guidelines

**All AI agents working on this codebase MUST:**

1. **Run `just check` before committing.** It runs ruff (lint+format), pyright (types), and `pytest tests/unit` (fast unit tests). Failures block commits.
2. **Honor the module-by-domain layout.** Don't introduce new top-level directories without updating this section.
3. **Use Protocol for interfaces, BaseModel for data, Literal for enums.** No ABC, no Enum, no plain dicts at boundaries.
4. **Never catch `ExternalServiceError`** in v1 code paths. Crash and let systemd restart.
5. **Use `snake_case` everywhere keys are written.** Across Python, TOML, YAML, JSON payload, DDS, log fields. No exceptions.
6. **Bump `schema_version` only on breaking changes.** Adding optional fields is forward-compat — don't bump.
7. **Mock only at Protocol boundaries** in tests. Never mock internal functions.
8. **Never log raw audio, credentials, or (at INFO+) transcripts.** Even though the redaction processor catches mistakes, the code shouldn't make them.
9. **Update PRD/brief/distillate in the same commit** if a deviation is needed (NFR26 — spec-as-contract).

`CLAUDE.md` at project root captures rules 1–9 in shorter form for the AI partner's per-turn context.

### Anti-Patterns (Don't)

- Mixed naming conventions (e.g., `setup.toml` keys in `kebab-case` while pydantic fields are `snake_case`)
- `abc.ABC` for interfaces (use `Protocol`)
- `enum.Enum` for fixed string values (use `Literal[...]`)
- `try: ... except Exception: ...` to "make it work" — find the specific exception or let it crash
- Mocking pydantic models or internal functions in tests
- `# type: ignore` without an inline reason comment
- Adding a `utils/` package — there's a domain package it belongs to
- Silently catching external service errors in v1 code
- Writing function docstrings that just restate the function name
- Adding `Any` outside the documented `payload` extensibility seam

## Project Structure & Boundaries

### V1 wire format simplification (revision to Batch 3 decision 3.6)

For v1 with no specific subscriber yet, custom ROS 2 `.msg` IDL files require an `ament_python`/`ament_cmake` package, `colcon build`, and workspace sourcing — build complexity for no v1 benefit. **v1 uses `std_msgs/String` with the entire `ExpressionEvent` (and `LifecycleEvent`) serialized as JSON.** No custom `.msg`, no ament package, no colcon. When a typed consumer materializes (embodiment project), a custom `.msg` package is added — same JSON content, just typed structural fields on top.

### Complete Project Directory Structure

```
voice-agent-pipeline/
├── README.md                                  # overview, install, run, deployment
├── CLAUDE.md                                  # AI partner rules (terse form of step 5 enforcement)
├── pyproject.toml                             # uv project + ruff + pyright config
├── uv.lock                                    # committed
├── justfile                                   # task runner: run, check, test, reload, lint, format
├── .python-version                            # uv-managed Python version pin (3.12)
├── setup.toml                                 # service config (transport, models, channels, audio devices, log rotation)
├── expression_map.yaml                        # Cartesia tag → ExpressionEvent payload mapping + fallback families
├── .env.example                               # template: CARTESIA_API_KEY, OPENAI_API_KEY/GROQ_API_KEY/GEMINI_API_KEY (one), PICOVOICE_ACCESS_KEY
├── .env                                       # gitignored — actual credentials, 0600
├── .gitignore                                 # .env, logs/, .venv/, __pycache__/, etc.
├── logs/                                      # gitignored — rotating log files (created at runtime)
│   ├── voice-agent.log                        # INFO+ main app log
│   ├── errors.log                             # WARN+ only
│   └── debug.log                              # DEBUG, opt-in via LOG_LEVEL=DEBUG
├── models/
│   └── wakeword/
│       └── hey_olaf.ppn                       # Picovoice Porcupine custom phrase (committed)
├── deploy/
│   ├── systemd/
│   │   └── voice-agent-pipeline.service       # unit file (committed reference)
│   └── README.md                              # deployment notes
├── src/
│   └── voice_agent_pipeline/
│       ├── __init__.py
│       ├── __main__.py                        # entry point: argparse, signal handlers, asyncio.run
│       ├── pipeline.py                        # Pipecat pipeline assembly + lifecycle orchestration
│       ├── errors.py                          # custom exception hierarchy (single file)
│       ├── audio/
│       │   ├── __init__.py
│       │   ├── transport.py                   # LocalAudioTransport wiring
│       │   ├── wakeword.py                    # Porcupine async wrapper (asyncio.to_thread)
│       │   ├── vad.py                         # Silero VAD wrapper
│       │   └── devices.py                     # resolve_audio_devices() name → index
│       ├── stt/
│       │   ├── __init__.py
│       │   ├── backend.py                     # STTBackend Protocol
│       │   └── whisper_cpu.py                 # WhisperBackend (faster-whisper)
│       ├── turn/
│       │   ├── __init__.py
│       │   ├── router.py                      # TurnRouter (fast vs slow path, config-driven rule)
│       │   ├── talker.py                      # TalkerClient + Talker (openai.AsyncOpenAI; provider-agnostic via base_url)
│       │   ├── orchestrator.py                # OrchestratorClient (httpx + httpx-sse)
│       │   └── beliefs.py                     # BeliefStateClient (httpx GET)
│       ├── tts/
│       │   ├── __init__.py
│       │   ├── client.py                      # TTSClient Protocol
│       │   └── cartesia.py                    # CartesiaClient streaming wrapper
│       ├── splitter/
│       │   ├── __init__.py
│       │   ├── state_machine.py               # streaming SSML parser, ~50-100 LOC
│       │   ├── segmenter.py                   # boundary-based emission + audio-frame metadata attachment
│       │   └── mapping.py                     # tag → payload via expression_map; fallback resolution; last-published cache
│       ├── publisher/
│       │   ├── __init__.py
│       │   ├── interface.py                   # ExpressionPublisher Protocol
│       │   └── ros2.py                        # Ros2ExpressionPublisher (std_msgs/String + JSON)
│       ├── lifecycle/
│       │   ├── __init__.py
│       │   ├── states.py                      # Literal types for lifecycle states
│       │   └── machine.py                     # transition logic + LifecycleEvent emission
│       ├── config/
│       │   ├── __init__.py
│       │   ├── setup.py                       # SetupConfig (pydantic-settings, setup.toml + .env)
│       │   ├── expression_map.py              # ExpressionMapConfig + SIGHUP atomic swap (deferred mid-utterance)
│       │   └── version.py                     # SchemaVersionError check helpers
│       ├── logging/
│       │   ├── __init__.py
│       │   ├── setup.py                       # structlog configuration, RotatingFileHandler wiring
│       │   └── redaction.py                   # denylist redaction processor
│       └── schemas/
│           ├── __init__.py
│           ├── expression_event.py            # ExpressionEvent (frozen pydantic v2)
│           ├── lifecycle_event.py             # LifecycleEvent (frozen pydantic v2)
│           └── stream.py                      # OrchestratorStreamEvent union for SSE event types
└── tests/
    ├── conftest.py                            # shared fixtures (logger setup, anyio backend)
    ├── unit/
    │   ├── audio/
    │   │   ├── test_devices.py                # name regex → index resolution; not-found refuses startup
    │   │   ├── test_wakeword.py               # mock pvporcupine.process
    │   │   └── test_vad.py
    │   ├── stt/
    │   │   └── test_whisper_cpu.py            # mock faster-whisper; confidence routing
    │   ├── turn/
    │   │   ├── test_router.py                 # routing rule from config; low-confidence escape hatch
    │   │   ├── test_talker.py
    │   │   ├── test_orchestrator.py           # SSE stream parsing; unknown event type → WARN
    │   │   └── test_beliefs.py
    │   ├── splitter/
    │   │   ├── test_state_machine.py          # token-by-token; tag boundary; burst stripping
    │   │   ├── test_segmenter.py              # sentence/tag/burst boundary emission
    │   │   └── test_mapping.py                # primary, secondary, fallback family, unknown → neutral
    │   ├── publisher/
    │   │   └── test_ros2.py                   # mock rclpy; JSON encoding round-trip
    │   ├── lifecycle/
    │   │   └── test_machine.py                # transitions, barge-in flush, idle timeout
    │   ├── config/
    │   │   ├── test_setup.py                  # schema validation; missing keys; LAN orchestrator + secret rule
    │   │   ├── test_expression_map.py         # SIGHUP atomic swap; mid-utterance defer; rollback on invalid
    │   │   └── test_version.py
    │   └── logging/
    │       └── test_redaction.py              # audio bytes dropped; transcripts gated; credentials regex
    ├── integration/
    │   ├── test_simple_turn.py                # full pipeline, mocked external services, journey 1
    │   ├── test_complex_turn.py               # orchestrator SSE flow, journey 2
    │   ├── test_barge_in.py                   # SPEAKING → LISTENING + flush, journey 3
    │   ├── test_unmapped_emotion.py           # fallback family resolution, journey 4
    │   └── test_sighup_reload.py              # config hot-reload, journey 5
    └── contract/
        ├── test_expression_event_schema.py    # pydantic ↔ JSON round-trip
        ├── test_lifecycle_event_schema.py
        └── test_setup_schema_version.py       # schema_version mismatch refused
```

### Architectural Boundaries

**External boundaries** (one place each, never duplicated):

| External | Lives in | Notes |
|---|---|---|
| Audio devices (PyAudio) | `audio/transport.py`, `audio/devices.py` | Only files that call PyAudio APIs |
| Wake-word (Porcupine) | `audio/wakeword.py` | Only file that imports `pvporcupine` |
| VAD (Silero) | `audio/vad.py` | Only file with VAD bindings |
| Whisper STT (faster-whisper) | `stt/whisper_cpu.py` | Only file that imports `faster_whisper` |
| OpenAI / Groq / Gemini API (Talker) | `turn/talker.py` | Only file that imports `openai`; all three providers reach this SDK via openai-compatible endpoints |
| Orchestrator HTTP/SSE | `turn/orchestrator.py`, `turn/beliefs.py` | Only files that import `httpx`, `httpx-sse` |
| Cartesia API | `tts/cartesia.py` | Only file that imports `cartesia` |
| ROS 2 / DDS | `publisher/ros2.py` | Only file that imports `rclpy` |
| File system (logs, configs, models) | `logging/setup.py`, `config/*` | All FS access concentrated here |

**Internal seams** (Protocols — the v2 swap points):

| Protocol | Defined in | Consumers |
|---|---|---|
| `STTBackend` | `stt/backend.py` | `audio/transport.py` (passes to STT processor) |
| `TalkerClient` | `turn/talker.py` | `turn/router.py` |
| `OrchestratorClient` | `turn/orchestrator.py` | `turn/router.py` |
| `BeliefStateClient` | `turn/beliefs.py` | `turn/talker.py` |
| `TTSClient` | `tts/client.py` | `splitter/segmenter.py`, `pipeline.py` |
| `ExpressionPublisher` | `publisher/interface.py` | `splitter/segmenter.py`, `lifecycle/machine.py`, `audio/transport.py` |

### Data Flow

```
Mic ──▶ audio/transport ──▶ audio/wakeword ──▶ audio/vad ──▶ stt/whisper_cpu ──▶ transcript
                                                                                      │
                                                                                      ▼
                                                                              turn/router (decision)
                                                                              ┌────────┴────────┐
                                                                              ▼                 ▼
                                                                          turn/talker     turn/orchestrator
                                                                          (fast path)     (SSE stream)
                                                                              │                 │
                                                                              └────────┬────────┘
                                                                                       ▼
                                                                              splitter/state_machine
                                                                                       ▼
                                                                              splitter/segmenter
                                                                              (segment + attach ExpressionEvent metadata)
                                                                              ┌────────┴────────┐
                                                                              ▼                 ▼
                                                                         tts/cartesia    publisher/ros2
                                                                         (audio frames)  (queued by audio_frame_id)
                                                                              │                 │
                                                                              ▼                 │
                                                                       audio/transport ─────────┘
                                                                       (sends frame + publishes anchored event)
                                                                              │
                                                                              ▼
                                                                            Speaker

Lifecycle state changes: lifecycle/machine ──▶ publisher/ros2 (publish_lifecycle, separate channel)
```

### FR → File Mapping

| FR cluster | FRs | Source files |
|---|---|---|
| Audio I/O | FR1, FR2, FR3, FR4, FR5 | `audio/transport.py`, `audio/wakeword.py`, `audio/vad.py`, `audio/devices.py` |
| STT (v1) | FR6, FR8 | `stt/whisper_cpu.py`, `stt/backend.py` |
| Conversational | FR9, FR10, FR11, FR12, FR14 | `turn/router.py`, `turn/talker.py`, `turn/orchestrator.py`, `turn/beliefs.py` |
| Voice synthesis | FR15, FR17 | `tts/cartesia.py`, `tts/client.py` |
| Embodiment | FR18, FR19, FR20, FR21, FR22, FR23, FR24, FR25 | `splitter/state_machine.py`, `splitter/segmenter.py`, `splitter/mapping.py`, `publisher/ros2.py`, `audio/transport.py` |
| Lifecycle | FR26, FR27, FR28, FR29, FR30 | `lifecycle/machine.py`, `lifecycle/states.py`, `publisher/ros2.py` |
| Config & ops | FR31, FR32, FR33, FR34, FR35 | `config/setup.py`, `config/expression_map.py`, `config/version.py` |
| Operations | FR36 | `deploy/systemd/voice-agent-pipeline.service` |
| Observability | FR37, FR38, FR39, FR40, FR42, FR43 | `logging/setup.py`, `logging/redaction.py`, `splitter/mapping.py` (FR38) |

### Cross-Cutting Concerns → Locations

| Concern | Where it lives | Notes |
|---|---|---|
| Real-time audio path | `audio/transport.py` + Pipecat frame pipeline assembled in `pipeline.py` | The critical path. |
| Config & hot-reload | `config/setup.py`, `config/expression_map.py` | SIGHUP handler in `__main__.py` dispatches to `expression_map`. |
| Observability | `logging/` | Imported by every module; redaction enforced at the processor pipeline. |
| Fail-fast posture | `__main__.py` (process-level handler) + startup validation in `pipeline.py` | Each external client's `connect()` runs at startup; failure raises `StartupValidationError` → exit. |
| Security & privacy | `config/setup.py` (secrets via pydantic-settings), `logging/redaction.py` | Two enforcement points; both required. |
| Testability | `tests/unit/` (Protocol mocks) + `tests/integration/` (mocked externals) | Layout mirrors `src/`. |
| Spec-as-contract | `README.md` documents NFR26; `CLAUDE.md` reminds AI partner | Updates flow PRD ↔ code. |
| Mapping completeness + extensibility | `expression_map.yaml` + `config/expression_map.py` + `splitter/mapping.py` | The "add an expression" workflow lives entirely in YAML. |
| Pluggable publisher transport | `publisher/interface.py` (Protocol) + `publisher/ros2.py` (v1 impl) | v2 implementations land alongside `ros2.py`. |
| Encapsulated STT inference | `stt/backend.py` (Protocol) + `stt/whisper_cpu.py` (v1 impl) | v2 `hailo_whisper.py` lands alongside. |

### Integration Points

**Inbound (none — pure outbound client):**

- Pipeline binds no listening port. SIGHUP is the only inbound signal.

**Outbound (all configured in `setup.toml`, credentialed in `.env`):**

- HTTPS to active Talker provider (OpenAI / Groq / Gemini — picked via `[talker] provider` in setup.toml)
- HTTPS to Cartesia API (TTS streaming)
- HTTP/SSE to orchestrator daemon (`POST /turn`, `DELETE /turn/{id}`, `GET /beliefs`, `GET /health`)
- ROS 2 / DDS publish to two configured channels (expression + lifecycle)

**Local devices:**

- PyAudio mic + speaker (resolved by name regex from `setup.toml`)
- Local file system (`./logs/`, `./models/`, `./setup.toml`, `./expression_map.yaml`, `./.env`)

**Process lifecycle:**

- systemd manages start/stop/restart
- SIGHUP reloads `expression_map.yaml` (atomic swap, deferred mid-utterance)
- SIGTERM triggers graceful shutdown (drain in-flight events, close clients, exit 0)

### Development Workflow

| Task | Command | What it does |
|---|---|---|
| Setup | `uv sync` | Install deps from `uv.lock`; create `.venv` |
| Run | `just run` (`uv run python -m voice_agent_pipeline`) | Start pipeline; reads `setup.toml` + `.env` from cwd |
| Type-check + lint + fast tests | `just check` | Pre-commit gate; AI partner runs this |
| Full test suite | `just test` (`uv run pytest`) | Unit + integration + contract tests |
| Hot-reload mapping | `just reload` | `kill -HUP $(pgrep -f voice_agent_pipeline)` |
| Lint only | `just lint` | `uv run ruff check` |
| Format | `just format` | `uv run ruff format` |

**Deployment to host (Linux PC v1 / Pi v2):**

1. Clone repo into a stable path (e.g., `/home/<user>/voice-agent-pipeline/`)
2. `uv sync` to set up `.venv`
3. `cp .env.example .env`, fill in keys, `chmod 0600 .env`
4. Train wake-word phrase via Picovoice console; place `.ppn` in `models/wakeword/`
5. `cp deploy/systemd/voice-agent-pipeline.service /etc/systemd/system/` (paths/user adjusted)
6. `sudo systemctl daemon-reload && sudo systemctl enable --now voice-agent-pipeline`
7. Logs at `./logs/voice-agent.log`; `journalctl -u voice-agent-pipeline` for systemd lifecycle messages

## Architecture Validation Results

### Coherence Validation ✅

**Decision Compatibility:**

- Pipecat (asyncio) ↔ `httpx` async ↔ `openai.AsyncOpenAI` ↔ Cartesia async streaming — single concurrency model end-to-end.
- Sync libraries (`faster-whisper`, `rclpy.publish`, `pvporcupine.process`) cleanly wrapped at the boundary via `asyncio.to_thread`.
- `pydantic` v2 frozen models → JSON → `std_msgs/String` for DDS — one serialization hop, simple wire.
- `pydantic-settings` (TOML + `.env`) + structlog + RotatingFileHandler — boring, well-trodden combo.
- ROS 2 system-installed `rclpy` + `uv`-managed Python venv — coexist via PYTHONPATH at deploy time; Pipecat doesn't conflict.

**Pattern Consistency:**

- `snake_case` is uniform across Python, TOML, YAML, JSON payload, DDS field names, log keys.
- `Protocol` for interfaces, `BaseModel` for data, `Literal` for enums — consistent across all 6 declared seams (`STTBackend`, `ExpressionPublisher`, `TalkerClient`, `OrchestratorClient`, `BeliefStateClient`, `TTSClient`).
- Module-by-domain layout aligns with the external-boundary concentration (each external library has exactly one file that imports it).

**Structure Alignment:**

- `src/voice_agent_pipeline/{audio,stt,turn,tts,splitter,publisher,lifecycle,config,logging,schemas}` covers every architectural decision.
- `tests/{unit,integration,contract}` mirrors `src/` and supports NFR28 testability.
- `deploy/systemd/` + `.env` handling supports v1 fail-fast posture.

### Requirements Coverage Validation ✅

**Functional Requirements (43 total → 39 in v1 active set, 4 deferred to v2):**

| Status | FRs | Coverage |
|---|---|---|
| ✅ v1 active | FR1–FR6, FR8–FR12, FR14, FR15, FR17, FR18–FR25, FR26–FR30, FR31–FR36, FR37–FR40, FR42, FR43 | All mapped to source files in step 6 FR table |
| ⏸ v2 deferred | FR7, FR41 (Hailo path) | Tracked under "Pi + Hailo-8L port" |
| ⏸ v2 deferred | FR13, FR16 (resilience) | Tracked under "Resilience layer" |

**Non-Functional Requirements (29 total → 20 in v1 active set, 9 deferred to v2):**

| Status | NFRs | Coverage |
|---|---|---|
| ✅ v1 active | NFR1–NFR8, NFR10–NFR13, NFR21, NFR23–NFR29 | Performance (frame anchoring + minimal buffering), reliability (systemd restart + soak), wake-word accuracy targets, ROS 2 reliable QoS, security (.env + TLS + redaction), maintainability (Protocol seams + schema versioning + JSON logs) |
| ⏸ v2 deferred | NFR9, NFR19, NFR20, NFR22 | Resilience layer |
| ⏸ v2 deferred | NFR14–NFR18 | Pi 5 resource calibration |

**5 PRD User Journeys — all architecturally supported:**

| Journey | Path | Supported? |
|---|---|---|
| 1. Simple turn (Talker fast-path) | wake-word → STT → TurnRouter (fast) → Talker → splitter → TTS+publisher → audio | ✅ |
| 2. Complex turn (orchestrator) | wake-word → STT → TurnRouter (slow) → orchestrator SSE stream → splitter → TTS+publisher → audio | ✅ |
| 3. Barge-in mid-response | VAD during SPEAKING → lifecycle SPEAKING→LISTENING → splitter flush + DELETE /turn/{id} | ✅ |
| 4. Unmapped emotion | splitter/mapping → fallback family → ExpressionEvent emitted | ✅ |
| 5. Live mapping tune (SIGHUP) | config/expression_map atomic swap, deferred mid-utterance | ✅ |

### Implementation Readiness Validation ✅

**Decision Completeness:** All 30+ critical decisions across 5 batches have a named choice and rationale. No "TBD" in v1 active set.

**Pattern Completeness:** 10 pattern categories defined (layout, naming, types, errors, logging, async, schema, tests, imports, docs) plus 9 enforcement rules and an anti-patterns list.

**Structure Completeness:** Every file in the project tree has a documented purpose. External-library imports concentrated to one file each. Six Protocol seams declared with their consumers.

### Gap Analysis

**Critical Gaps (block implementation):** None.

**Important Gaps (don't block, worth pinning):**

| Gap | Resolution |
|---|---|
| TurnRouter routing rule shape | Config-driven keyword/regex list in `setup.toml`. Open question (lean yes): should it be SIGHUP-reloadable like `expression_map.yaml`? Track as v1 implementation question. |
| Wake-word retraining workflow | Operational doc in `models/wakeword/README.md` — when soak shows accuracy issues, retrain via Picovoice console + replace `.ppn` + restart. Not architecture; documentation task. |
| Soak test infrastructure | Phase 3 needs week-long soak; for v1 it's manual on the dev host. No CI harness needed. |
| Pre-flight diagnostic command | `just diagnose` would run startup-validation without starting the pipeline (verify Cartesia reachable, daemon up, etc.). Nice-to-have; add when first bring-up frustration appears. |

**Nice-to-Have Gaps:**

| Gap | Notes |
|---|---|
| README and CLAUDE.md content outlines | First implementation story; outline can be drafted at bootstrap time. |
| CI tooling (GitHub Actions / GitLab) | Out of v1 scope; tests run via `just test` locally. Add when needed. |
| Metrics/telemetry beyond logs | Out of scope per fail-fast posture and "no telemetry" privacy stance. v2 may add OpenTelemetry. |
| Schema migration strategy | For single-host v1, just-edit-in-place is fine. Schema bump on breaking change requires a migration script — defer until first bump. |

### Architecture Completeness Checklist

**Requirements Analysis**

- [x] Project context thoroughly analyzed
- [x] Scale and complexity assessed
- [x] Technical constraints identified
- [x] Cross-cutting concerns mapped

**Architectural Decisions**

- [x] Critical decisions documented with versions
- [x] Technology stack fully specified
- [x] Integration patterns defined
- [x] Performance considerations addressed

**Implementation Patterns**

- [x] Naming conventions established
- [x] Structure patterns defined
- [x] Communication patterns specified
- [x] Process patterns documented

**Project Structure**

- [x] Complete directory structure defined
- [x] Component boundaries established
- [x] Integration points mapped
- [x] Requirements to structure mapping complete

### Architecture Readiness Assessment

**Overall Status:** READY FOR IMPLEMENTATION

**Confidence Level:** high

**Key Strengths:**

- **Pluggable seams** (`ExpressionPublisher`, `STTBackend`) make v2 evolution (resilience layer, Hailo port, alternate transports) drop-in changes — no restructuring.
- **Open `payload` dict + open SSE event types** are forward-compat by design — adding emotions or orchestrator events doesn't break the pipeline.
- **Single-fan-out architectural constraint** is structurally preserved via audio-frame metadata threading; no parallel channel for expression events exists in the design, so drift is impossible by construction.
- **Boring Python toolchain** (uv, ruff, pyright, pytest, structlog, pydantic v2) — no novelty risk; widely understood by AI partners and human contributors.
- **v1 fail-fast posture** has a small attack surface and small bug surface — easy to ship; v2 resilience is well-bounded for a future iteration.
- **Spec-as-contract** discipline (NFR26) keeps PRD/brief/distillate honest with the code via the alignment pass tracked in spec-drift notes.

**Areas for Future Enhancement (out of v1 scope, captured for v2+):**

- v2 resilience layer (per-external-dep adapter + degradation policy)
- v2 Pi 5 + Hailo-8L port (`HailoWhisperBackend`, Pi-specific resource calibration)
- v2 typed DDS `.msg` files when a typed consumer materializes (current `std_msgs/String + JSON` is a deliberate v1 simplification)
- Tertiary emotion mappings (flirtatious, mysterious, sarcastic) — currently fall back via family table; v1.1 makes them first-class
- WebSocket transport for orchestrator if SSE+DELETE proves too slow for barge-in
- CI tooling and metrics/telemetry beyond logs

### Implementation Handoff

**AI Agent Guidelines:**

- Follow all architectural decisions exactly as documented.
- Use implementation patterns consistently across all components (snake_case everywhere, Protocol/BaseModel/Literal, structlog with mandatory event field, `asyncio.to_thread` for sync libraries).
- Respect project structure and boundaries (each external library imported in exactly one file; never introduce a `utils/` package).
- Run `just check` before every commit.
- Update PRD/brief/distillate in the same change if any deviation is discovered (NFR26).
- Refer to this document for all architectural questions.

**First Implementation Priority:**

```bash
uv init voice-agent-pipeline --python 3.12
cd voice-agent-pipeline
uv add pipecat-ai[local]
uv add anthropic cartesia httpx httpx-sse pvporcupine faster-whisper pydantic pydantic-settings structlog
uv add --dev ruff pyright pytest pytest-asyncio
# rclpy installed via ROS 2 distro (system); exposed to .venv via PYTHONPATH
```

Then scaffold the module-by-domain skeleton, wire `pipeline.py`'s assembly, and land the bootstrap commit. From there, follow the implementation sequence laid out in "Decision Impact Analysis" (config → logging → schemas → publisher → STT → audio → splitter → clients → TurnRouter → lifecycle → wiring).

**PRD/distillate alignment pass** (spec-drift list compiled across this workflow) — cleanly separable follow-up work, recommended before the bootstrap story so the canonical specs match the architecture.
