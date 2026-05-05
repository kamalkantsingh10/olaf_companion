---
stepsCompleted:
  - step-01-init
  - step-02-discovery
  - step-02b-vision
  - step-02c-executive-summary
  - step-03-success
  - step-04-journeys
  - step-05-domain (skipped; deferred to NFRs)
  - step-06-innovation (skipped; no genuine innovation signals)
  - step-07-project-type
  - step-08-scoping
  - step-09-functional
  - step-10-nonfunctional
  - step-11-polish
  - step-12-complete
releaseMode: phased
deferredToNFRs:
  - Privacy: wake-word-gated mic capture, on-device STT transcripts not persisted, no telemetry
  - Credentials: Cartesia API key handling (0600 secrets file, never logged), daemon URL must be local-only or require mTLS/shared secret
  - Network egress: Cartesia is the only outbound; graceful degradation if unreachable (text-only mode, sad-LED indicator)
  - Audit: structured logs for lifecycle, fallback resolutions, config reloads, failures — local, rotatable
inputDocuments:
  - build_documents/planning-artifacts/voice-agent-pipeline-brief.md
  - build_documents/planning-artifacts/voice-agent-pipeline.md
documentCounts:
  briefs: 1
  research: 0
  brainstorming: 0
  projectDocs: 0
  distillates: 1
workflowType: prd
component: voice-agent-pipeline
parentProject: olaf_companion
classification:
  projectType: iot_embedded
  domain: general
  complexity: medium
  projectContext: greenfield
  notes: Single-user personal voice agent on Raspberry Pi with Hailo-8L. Real-time audio + ROS 2 + LLM orchestration. No regulatory burden.
---

# Product Requirements Document — voice-agent-pipeline

**Author:** Kamal
**Date:** 2026-05-03
**Parent project:** OLAF Companion (Personal Voice Agent)
**Component:** voice-agent-pipeline

## How to Read This Document

This PRD sits in a three-document set, each with a distinct role:

- **`voice-agent-pipeline-brief.md`** — executive brief. Vision and high-level architectural decisions. ~1.5 pages.
- **`voice-agent-pipeline.md`** — canonical distillate. Full architecture, mapping tables, configuration schemas, stream contracts. The single file to point an LLM coding partner at when implementing.
- **`prd.md`** (this document) — requirements layer. SMART functional requirements, measurable NFRs, user journeys, phasing strategy, risk mitigation. Feeds downstream UX/architecture/epics work.

The PRD does **not** duplicate the contracts in the distillate (stream event types, mapping tables, YAML/TOML schemas). It points at them by reference.

## Executive Summary

`voice-agent-pipeline` is the Pipecat-based service that owns OLAF Companion's voice loop and embodiment surface — the layer that makes OLAF feel like a companion rather than a remote API call. It captures user speech, dispatches turns to a separate orchestrator (a Claude Code session) for reasoning, generates spoken responses with Cartesia Sonic-3, and drives OLAF's physical expression in sync with that voice. It is the **only** component touching audio hardware and the **only** publisher to OLAF's expression channel; it deliberately does not reason, call MCP tools, or write belief state.

The PRD covers v1 of this component — Phases 0 through 3 — targeting a single user (Kamal) on a Raspberry Pi with Hailo-8L NPU, integrated with OLAF's physical embodiment over ROS 2.

### What Makes This Special

Four architectural decisions shape the v1 commitment and must survive any future rewrite:

1. **Talker fast-path inside Pipecat.** Simple turns are answered from belief state in-pipeline without waiting for the orchestrator's deeper reasoning. This eliminates the dead air that single-LLM voice agents force on every multi-step turn.
2. **Single fan-out point at the splitter.** Voice (to Cartesia TTS) and embodiment (to ROS 2 expression) are emitted from the same parsed segment, anchored to the same audio frames. Drift between voice and expression is prevented by construction — no parallel channels. Target alignment: ~30-80ms anticipatory.
3. **On-device STT (Whisper + Hailo-8L).** Speech transcription runs locally on the Pi. Both privacy and latency benefit from the same decision; cloud STT is explicitly excluded for v1.
4. **Mapping is data, not code.** The Cartesia tag → OLAF expression mapping lives in `expression_map.yaml`, reloadable on `SIGHUP`. Adding emotions, bursts, or fallback families is a config change, not a deploy.

The core insight: splitting the voice surface from the reasoning brain is the only way to make a voice agent feel alive while the brain does real, multi-step work. The Talker + single-fan-out splitter together let v1 ship a feeling-alive companion without depending on the orchestrator being fast.

## Project Classification

| Dimension | Value |
|---|---|
| **Project Type** | `iot_embedded` — Pi + Hailo-8L hardware, real-time audio, ROS 2 integration |
| **Domain** | `general` (embedded/AI flavor) — no regulatory burden |
| **Complexity** | `medium` — real-time audio + embedded acceleration + ROS 2 + LLM orchestration is non-trivial; no compliance overhead |
| **Project Context** | `greenfield` — fresh build, no existing system to integrate with |
| **Audience** | Single-user personal voice agent (Kamal); no multi-tenant, no GTM, no commercial scope |

## Success Criteria

### User Success (Kamal's experience)

A turn feels alive when:

- He can ask "what's on my calendar today?" and hear OLAF start responding without an awkward pause
- OLAF's pose, eyes, and LEDs visibly match the emotional tone of the response — no smiling on a sad sentence
- The wake word triggers reliably from across the room and doesn't false-trigger during phone calls or background TV
- He doesn't think "I'm talking to software" — he just talks to OLAF

### Project Success (build outcomes)

- All four hard architectural constraints (single fan-out, single-writer, audio-anchored, mapping-as-data) are upheld in the implementation; violations are bugs, not tradeoffs
- The component is **replaceable**: the `POST /turn` contract with the orchestrator and the `OlafAction` event shape on ROS 2 survive any future rewrite of internals
- Configuration is data-driven: adding a new emotion mapping requires editing `expression_map.yaml`, not code

### Technical Success (measurable performance)

Starting targets — validated against measured baseline during Phase 0/1, then locked. Each row below is formalized as a testable NFR (NFR1–NFR7, NFR12, NFR13) with p95 measurement context.

| Metric | Target | Maps to | Rationale |
|---|---|---|---|
| **Simple turn** (Talker fast-path): end-of-speech → first audio frame | **≤ 1500ms** | NFR1 | Voice agents over ~2s feel dead; under 1.5s feels live |
| **Complex turn** (orchestrator narration): end-of-speech → first audio frame | **≤ 1000ms** | NFR2 | Narration ("let me check…") must arrive before the user wonders if anything is happening |
| **On-device STT latency**: end-of-speech → transcript ready | **≤ 500ms** | NFR3 | Whisper-small + Hailo-8L on Pi should hit this |
| **Voice/expression alignment** | **30–80ms anticipatory** | NFR5 | Embodiment slightly ahead of voice; outside this window is a perceivable bug |
| **Wake-word false positives** | **≤ 1 per hour** of normal ambient background | NFR12 | Higher rates make the system feel paranoid |
| **Wake-word false negatives** | **≤ 5%** in normal speaking conditions | NFR13 | Higher rates frustrate the user into shouting |
| **Cartesia TTS latency**: text-with-tags → first audio frame | **≤ 400ms** | NFR4 | Mostly Cartesia's responsibility; splitter must not add buffering |
| **Unmapped Cartesia tag handling** | **100% fallback coverage** | FR21, FR38 | Truly unknown tags log warning and render neutral. No silent gaps. |

### Measurable Outcomes (how we know v1 is done)

A 30-minute live conversation session completes without any of:

- Drift between voice and OLAF expression noticeable to the user
- An unhandled Cartesia tag causing OLAF to freeze or default visibly
- A missed wake-word or a false-fire
- Audio cutout, stutter, or buffering pause longer than 100ms
- A latency target above being missed by more than 20%

Plus: all Phase 0–3 validation goals pass (per phase table in `voice-agent-pipeline.md`), and `expression_map.yaml` reloads via `SIGHUP` without restart.

## Product Scope

### MVP — Minimum Viable Product (Phase 3 complete)

The component is "useful" when it can serve a 5-minute live conversation with OLAF: wake-word triggers, on-device STT works, the orchestrator responds, voice and embodiment are in sync, and the conversation gracefully ends and returns to idle. Specifically:

- All six primary emotions (neutral, content, excited, sad, angry, scared) render correctly on OLAF
- Secondary emotions (happy, curious, sympathetic, surprised, frustrated, melancholic) map to their primary equivalents and render
- Fallback table covers Cartesia's full 60+ emotion vocabulary
- Lifecycle states publish at correct conversation milestones (LISTENING, THINKING, SPEAKING, IDLE, SLEEPING)
- Wake-word + on-device STT + Cartesia TTS + ROS 2 expression all integrated end-to-end
- Talker fast-path serves simple turns; orchestrator dispatch serves complex turns
- All technical-success latency targets hit (within 20% margin)

### Growth Features (Post-MVP, v1.1)

- Tertiary emotion mappings (flirtatious, mysterious, sarcastic) — full custom OLAF behaviors instead of fallback
- Polished secondary-emotion poses — lift from "maps to primary" to first-class distinct expressions
- Barge-in handling validated and tuned (currently flagged as empirical)
- Bursts beyond `[laughter]` once Cartesia ships them (`[sigh]`, `[gasp]`, `[clears_throat]`)
- Latency budget tightening based on measured Phase 0–3 baselines

### Vision (v2+)

- Telephony / SIP transport for remote conversations (currently local-only)
- On-device TTS once a model meeting the quality bar runs on Pi (removes Cartesia cloud dependency)
- WebRTC transport for browser-based interaction
- Emotion intensity scaling once Cartesia exposes it for sustained emotions
- Integration point for OAK-D camera-driven user-expression signals (separate component, but the pipeline's narrow scope must accommodate)

## Project Scoping & Phased Development

### MVP Strategy & Philosophy

**MVP type: experience MVP.** The v1 bar is "does the conversation feel alive," not feature count. Five things done excellently (wake-word, on-device STT, Talker fast-path, emotion-aligned voice, embodied expression) beats fifteen things done mediocrely. The latency targets in Success Criteria are the integration test.

**Phasing rationale:**

The four phases (defined in `voice-agent-pipeline.md` §14) exist because each one validates one *new* high-risk thing without hiding it behind another:

- **Phase 0** isolates audio I/O + STT + TTS. If Whisper on Hailo-8L doesn't hit ~500ms, we know now, before integration complexity hides it.
- **Phase 1** isolates the streaming SSML parser and orchestrator dispatch. OLAF still mocked. Splitter bugs are visible in stdout, not in OLAF behavior.
- **Phase 2** swaps the OLAF mock for the real ROS 2 publisher. Everything before this can be debugged on a laptop; this is where Pi + OLAF integration becomes real.
- **Phase 3** completes lifecycle handling and full fallback table coverage. Soak testing happens here.

Each phase produces a runnable artifact, not a sub-component — incremental validation, not a 6-week build before turning it on.

**Resource requirements:** Solo dev (Kamal) + Claude Code as LLM coding partner. No team coordination. Implication: this PRD and the distillate must be the canonical contract — if those drift from reality, the LLM partner produces wrong code.

### Scope (in / out)

> **Refer to the `Product Scope` section above for the in-scope MVP feature list, Growth (v1.1) features, and Vision (v2+) items.** Not duplicated here.

**No requirements from the brief/distillate are de-scoped or deferred** in this PRD. All Phase 0-3 items in the source documents are MVP for this component.

### Risk Mitigation Strategy

**Technical risks**

| Risk | Mitigation |
|---|---|
| **Hailo-8L driver/runtime maturity** for Whisper inference. New SDKs lag; on-device inference may not hit 500ms target. | Phase 0 validates STT latency on CPU first (Whisper-base baseline). Phase 1 adds Hailo-8L acceleration. If Hailo-8L fails to deliver, fall back to a smaller Whisper variant (tiny/base) on CPU and re-evaluate latency target. |
| **Cartesia cloud dependency** (network, rate limits, pricing changes). | Pipeline must degrade gracefully when Cartesia is unreachable: text-only mode with a `<emotion value="sad"/>` LED indicator on OLAF. No silent failure. Long-term mitigation lives in Vision (on-device TTS). |
| **Audio-frame anchoring complexity** in Pipecat. Threading expression-event metadata through the frame pipeline correctly is the technically hardest piece. | Phase 2 validates this with measurable end-to-end timing tests. If Pipecat's processor model can't cleanly carry the metadata, fall back to time-based correlation (publish OLAF event N ms after audio frame play time). Document the deviation if used. |
| **ROS 2 multicast on home network** can be fragile. | Mitigation: colocate the pipeline and OLAF nodes on the same machine, or wired LAN with explicit DDS domain ID. WiFi multicast is not supported for v1. |
| **Wake-word false positives** during phone calls, music, TV. | Tunable threshold in `pipeline.toml`. Phase 3 includes soak testing in real ambient conditions to set a sane default. If a single wake-word model can't hit the false-positive target, consider dual-stage: low-power detector + small confirmation model. |
| **Cartesia emotion vocabulary drift.** Cartesia ships ~60 tags now; future tags will land outside our mapping. | The fallback family table covers the principle; `unmapped emotion` warnings make drift visible. Out-of-band: quarterly check for new Cartesia emotions and mapping table updates. |

**External dependency risks** (the equivalent of "market risk" for a personal project)

| Risk | Mitigation |
|---|---|
| **Cartesia API pricing or availability changes.** | Designed to be replaceable: TTS is a single processor in the Pipecat pipeline. Swapping providers is a config change + processor rewrite, not an architecture change. |
| **Anthropic API for Talker.** | Same — Talker is one in-pipeline LLM call; the model is configurable in `pipeline.toml`. Switching to a different small model (e.g., a local Llama-3-8B on the Pi) is config + a different client lib. |
| **Hailo-8L hardware availability / firmware EOL.** | Phase 0 validates that CPU Whisper is acceptable as a fallback. The pipeline must not hard-require Hailo-8L; it should degrade to CPU with a logged warning. |

**Resource / execution risks**

| Risk | Mitigation |
|---|---|
| **Solo dev with LLM coding partner** — context drift between this PRD/distillate and reality is the biggest risk. | The PRD + distillate + brief are the canonical contracts. Any deviation discovered during implementation must update those documents (treat them as living, not write-once). Don't let the code drift past the spec. |
| **Phase 2 (real OLAF integration) is the riskiest single jump.** | Phase 2 is gated on Phase 1 working with stdout mock. If Phase 1 isn't clean, do not move to Phase 2 — debug the splitter first. |
| **Soak testing requires real ambient conditions** that can't be fully simulated. | Plan: Phase 3 lives on the Pi for at least a week of normal household use before declaring v1 done. Bug bash is the conversation-quality test, not a unit test count. |

## User Journeys

> **Audience note:** voice-agent-pipeline is a single-user component. "Journeys" here are concrete interaction scenarios the implementation must support, written from Kamal's perspective. Each one names the capabilities it exercises — that feeds the functional requirements section directly.

### Journey 1: Simple turn (Talker fast-path)

**Scenario.** Kamal is making coffee and asks OLAF the time.

**Sequence:**

1. Kamal: "Hey OLAF, what time is it?"
2. Wake-word detector fires on "hey OLAF" — pipeline transitions IDLE → LISTENING
3. VAD captures the rest of the utterance; on-device STT transcribes
4. Splitter routes the transcript: short, factual question — Talker fast-path takes it
5. Talker reads belief state via daemon API (`GET /beliefs?keys=time`) and generates a natural reply: `<emotion value="content"/> It's 8:47 in the morning.`
6. Lifecycle transitions LISTENING → THINKING (briefly) → SPEAKING
7. Cartesia streams audio frames; splitter fans out: text+SSML to TTS, `OlafAction(emotion=content)` to ROS 2, anchored to first audio frame
8. OLAF base pose shifts to content; LED ring goes warm amber
9. Audio plays through speaker; last frame triggers SPEAKING → IDLE
10. Total wall time: end-of-speech to first audio frame **≤ 1500ms**

**Failure modes & recovery:**

- *Wake-word misfires* on background noise → false-positive logged; pipeline returns to IDLE without dispatching
- *STT confidence too low* → Talker prompts a clarification ("Sorry, I didn't catch that?")
- *Daemon belief-state read fails* → Talker falls back to dispatching to the orchestrator (slow path)

**Capabilities exercised:** wake-word detection, on-device STT, Talker LLM call, belief-state read API, Cartesia TTS streaming, splitter fan-out, ROS 2 expression publish, audio-frame anchoring, lifecycle transitions IDLE↔LISTENING↔THINKING↔SPEAKING.

### Journey 2: Complex turn (orchestrator dispatch)

**Scenario.** Kamal asks about his day. The orchestrator must check the calendar (subagent) and reply with structured narration.

**Sequence:**

1. Kamal: "Hey OLAF, what's on my calendar today?"
2. Wake-word, STT, lifecycle → LISTENING
3. Splitter routes: question requires data fetch — dispatch to orchestrator via `POST /turn`
4. Lifecycle → THINKING; OLAF shows subtle "thinking" indicator
5. Stream from orchestrator begins:
   - `{"type": "narration", "text": "Let me check..."}` → splitter segments, sends to Cartesia, audio plays within ≤1000ms of end-of-speech
   - `{"type": "subagent_started", "name": "comms"}` → lifecycle hint: subagent active
   - `{"type": "subagent_progress", "name": "comms", "msg": "Reading calendar"}` → continued thinking indicator
   - `{"type": "subagent_done", "name": "comms"}`
   - `{"type": "response_chunk", "text": "<emotion value=\"content\"/> You've got "}` → splitter buffers, segments at sentence/emotion boundary
   - `{"type": "response_chunk", "text": "two meetings today — one at 10..."}` → continues
   - `{"type": "turn_end"}` → flush splitter, lifecycle SPEAKING → IDLE after last audio frame
6. OLAF expression matches narration → response transition smoothly; LED goes warm amber on `content`

**Failure modes & recovery:**

- *Orchestrator stream stalls* (no event for >5s) → pipeline plays a filler ("Still working on it...") via Talker, keeps lifecycle at THINKING
- *Stream ends without `turn_end`* → splitter flushes pending text; lifecycle still transitions to IDLE after audio
- *Cartesia rejects emotion* (text doesn't match) → Cartesia silently drops; OLAF still renders the LLM's intent

**Capabilities exercised:** orchestrator dispatch (HTTP/WebSocket stream), streaming SSML parser, segment-on-emotion-or-sentence logic, last-published-cache for OLAF base, narration handling, subagent lifecycle hints, `turn_end` cleanup.

### Journey 3: Barge-in mid-response

**Scenario.** OLAF starts a long answer; Kamal interrupts.

**Sequence:**

1. Kamal asks a question; OLAF is mid-response (lifecycle = SPEAKING)
2. Kamal speaks again ("Wait, actually—") before OLAF finishes
3. VAD detects voice during SPEAKING → barge-in event fires
4. Pipeline transitions SPEAKING → LISTENING immediately
5. Cartesia audio playback halts; remaining audio frames discarded
6. Splitter flushes in-flight expression events: any unpublished `OlafAction` are dropped, *not* published — OLAF doesn't get stuck mid-pose
7. New utterance is captured and dispatched normally

**Failure modes & recovery:**

- *VAD false-positive during SPEAKING* (background noise, OLAF's own audio bleed) → must not trigger barge-in; barge-in requires sustained voice over a threshold
- *Splitter has just published expression event but corresponding audio not yet played* → published event is the truth; OLAF holds that pose until next event

**Capabilities exercised:** mid-stream barge-in detection, splitter state flush, lifecycle SPEAKING→LISTENING transition without going through THINKING, audio playback abort.

> **Open in v1:** exact tuning of barge-in sensitivity is empirical (per distillate §15.2). This journey is the contract; the implementation may need tuning passes during Phase 2.

### Journey 4: Unmapped emotion fallback

**Scenario.** The orchestrator's LLM emits a Cartesia tag the pipeline hasn't explicitly mapped (`<emotion value="enthusiastic"/>`).

**Sequence:**

1. Response chunk arrives: `<emotion value="enthusiastic"/> That's amazing!`
2. Splitter parses tag → looks up `enthusiastic` in `expression_map.yaml`
3. Not found in primary or secondary tier
4. Falls through to family table: `enthusiastic` ∈ `high_energy_positive` → maps to `excited`
5. OLAF renders `excited` base pose (forward lean, head up, wide eyes, saturated yellow LED)
6. Cartesia receives the original tag; if Sonic-3 supports `enthusiastic`, voice prosody reflects it; if not, voice is unchanged but OLAF still expresses excitement
7. A warning is logged (DEBUG level): `unmapped emotion 'enthusiastic' → fallback to 'excited' via high_energy_positive`

**Failure modes & recovery:**

- *Tag not in family table at all* (truly unknown) → fall through to `unknown: neutral`; warning logged at WARN level
- *`expression_map.yaml` is malformed at startup* → pipeline refuses to start, error to stderr; doesn't silently run with broken mapping

**Capabilities exercised:** fallback family resolution, structured logging for observability, config validation at startup, graceful degradation.

### Journey 5: Operator — live mapping tune

**Scenario.** Kamal feels OLAF's `excited` pose isn't energetic enough. He wants to tune the mapping without restarting the pipeline mid-session.

**Sequence:**

1. Pipeline is running; OLAF is idle
2. Kamal edits `expression_map.yaml`:
   - Changes `excited.olaf.base_pose.lean` from `8` to `15`
   - Changes `excited.olaf.led_intensity` from `0.7` to `0.9`
3. Kamal sends `SIGHUP` to the pipeline process: `kill -HUP <pid>`
4. Pipeline reloads `expression_map.yaml`; validates schema; if valid, swaps in-memory mapping atomically
5. If validation fails, pipeline keeps old mapping and logs error; does *not* crash
6. Reload completes within 1s
7. Next turn that emits `excited` uses the new mapping

**Failure modes & recovery:**

- *Edited YAML is malformed* → validation rejects, old mapping retained, error logged with line number
- *SIGHUP arrives mid-utterance* → reload deferred until current turn ends; pipeline doesn't swap mapping on a frame in flight

**Capabilities exercised:** SIGHUP signal handler, atomic config swap, schema validation, graceful reload (no restart, no dropped state), rollback on invalid config.

### Journey Requirements Summary

The five journeys reveal these capability clusters that the functional requirements section specifies:

| Capability area | Journeys exercising it |
|---|---|
| Wake-word detection (always-on, low-power) | 1, 2, 5 |
| On-device STT (Whisper + Hailo-8L) | 1, 2 |
| Talker fast-path (in-pipeline LLM, belief-state read) | 1 |
| Orchestrator dispatch (HTTP/WebSocket stream) | 2 |
| Streaming SSML parser + tag splitter | 2, 3, 4 |
| Cartesia TTS streaming | 1, 2, 3 |
| Splitter fan-out + audio-frame anchoring | 1, 2 |
| ROS 2 expression publish (`OlafAction` events) | 1, 2, 4 |
| Lifecycle state machine | 1, 2, 3, all |
| Barge-in detection + splitter flush | 3 |
| Fallback family resolution | 4 |
| Config: schema validation, SIGHUP reload, atomic swap | 5 |
| Observability: structured logging, warning levels | 4, 5, all |

## IoT / Embedded Specific Requirements

### Hardware Requirements

| Component | Spec | Notes |
|---|---|---|
| **Compute** | Raspberry Pi 5 (TBD: confirm model — Pi 5 / Pi 4 / CM4) | Must support Hailo-8L M.2 HAT or USB connection |
| **NPU accelerator** | Hailo-8L (13 TOPS) | Used for on-device Whisper inference; without it, STT may not hit the 500ms latency target on Pi CPU alone |
| **RAM** | ≥ 8 GB | Whisper-small footprint + Pipecat runtime + Talker LLM context |
| **Storage** | ≥ 64 GB SSD/NVMe (recommended) or fast SD | Whisper model weights, logs, config; SSD strongly preferred for STT load times |
| **Microphone** | TBD (USB or I2S) | Far-field array recommended for wake-word reliability across the room. Examples: ReSpeaker 4-Mic Array, MATRIX Voice |
| **Speaker** | TBD | Audio quality affects perceived "aliveness"; cheap output undermines Cartesia's emotional prosody |
| **Embodiment** | OLAF robot (separate hardware) | Connected via ROS 2; pipeline does not control motors or actuators directly |
| **Network** | WiFi or Ethernet to local network | Required for Cartesia API; orchestrator and ROS 2 are local |

> **Open hardware questions:** Pi model, mic model, speaker model — fill these once selected.

### Connectivity Protocols

| Channel | Protocol | Direction | Endpoint |
|---|---|---|---|
| **Audio capture** | USB / I2S / ALSA | In | Local mic device |
| **Audio playback** | ALSA / PulseAudio | Out | Local speaker device |
| **Wake-word** | On-device, no network | — | — |
| **STT** | On-device (Whisper + Hailo-8L) | — | — |
| **Talker LLM** | HTTPS | Out | Anthropic API (Claude Haiku 4.5) |
| **Belief-state read** | HTTP | Out | `http://localhost:8001/beliefs` (orchestrator daemon) |
| **Orchestrator dispatch** | HTTP/WebSocket (SSE) | Out | `http://localhost:8001/turn` (orchestrator daemon) |
| **TTS** | HTTPS / WebSocket | Out | Cartesia Sonic-3 API |
| **OLAF expression** | ROS 2 (DDS, UDP multicast) | Out | `/olaf/expression` topic, `ros_domain_id=7` |
| **Lifecycle** | ROS 2 | Out | OLAF lifecycle topic |

**Network reachability requirements:**

- **Outbound to internet:** Cartesia (TTS), Anthropic (Talker LLM)
- **Outbound on local network:** Orchestrator daemon (default: localhost; configurable to LAN-reachable, but must require shared secret/mTLS if so — bare HTTP exposure rejected at startup)
- **Local DDS multicast:** ROS 2 traffic; pipeline must be on the same DDS domain as OLAF nodes
- **Inbound:** None. The pipeline is purely an outbound client; nothing else dials in.

### Power Profile

The Pi is **mains-powered** (plugged in continuously as a fixed-location device). Power efficiency still matters for two reasons:

- **Always-on wake-word detector** must run continuously without significant CPU draw. Use a dedicated low-power wake-word model (e.g., openWakeWord, Picovoice Porcupine), not full Whisper. Target: < 5% CPU sustained.
- **Thermal throttling** on the Pi can cause audio dropouts under load. STT + Cartesia decoding + ROS 2 publishing concurrently must stay below the throttle threshold. Active cooling (fan + heatsink) recommended.

**OLAF embodiment power:** TBD — if OLAF is battery-powered, the pipeline must avoid spamming high-frequency expression events. Already designed for this: the splitter's "last published" cache prevents republishing unchanged base emotions.

### Security Model

> **Single-user personal device on a private home network.** No multi-tenant, no compliance regime. Threat model is realistic, not adversarial: protect against accidental mistakes, not nation-state attackers.

**Credentials:**

- **Cartesia API key** stored in a separate secrets file (path referenced from `pipeline.toml`, not inline). File permissions `0600`. Never logged. Rotation: manual.
- **Anthropic API key** (for Talker) — same handling as Cartesia.
- **No other outbound credentials.**

**Network exposure:**

- **Default: localhost-only** for orchestrator daemon connection. The pipeline does not bind any listening port itself.
- If `daemon.url` is configured to a LAN address, the pipeline **must** require a shared secret (Bearer token in `Authorization` header) or mTLS. Validation happens at startup; if a network URL is set without a shared secret configured, pipeline refuses to start with a clear error.

**Privacy:**

- **Wake-word-gated mic capture.** Pre-wake audio is buffered in-memory only and discarded. Not written to disk, not transmitted.
- **STT transcripts are not persisted** by the pipeline. They are passed to Talker / orchestrator for the current turn and dropped.
- **No telemetry.** No phone-home, no analytics, no usage tracking.

**Logging:**

- Structured logs (JSON) at INFO/WARN/ERROR levels. No raw audio. No transcripts at INFO level — transcripts only at DEBUG level, which is off by default.
- Logs rotated locally; default retention 7 days.

### Update Mechanism

Personal-project scale, not a fleet:

- **Code updates:** `git pull` + `systemctl restart pipecat-voice` on the Pi. No A/B partitions, no signed images, no fleet management.
- **Config updates:** `expression_map.yaml` — `SIGHUP` for hot reload (no restart). `pipeline.toml` — restart required (audio device, model selection, etc. can't be hot-swapped safely in v1).
- **Model updates** (Whisper, wake-word): pinned versions in a manifest file; updating means downloading new weights, validating, then restarting.
- **Rollback:** git revert + restart. Config files versioned in git alongside code.

> **Out of scope for v1:** signed update packages, automatic update polling, OTA delivery infrastructure. If this component ever ships beyond Kamal's Pi, the update mechanism would need a real story — but for v1, manual is fine.

### Implementation Considerations

- **systemd service** managing the pipeline process. Restart-on-failure with backoff. Logs to journald + local file.
- **Hailo-8L drivers** must be installed and verified before pipeline starts; pipeline checks for the device at startup and logs a clear error if missing (does not silently fall back to CPU Whisper, since that would miss latency targets).
- **Audio device pinning:** `pipeline.toml` references audio devices by stable name (not numeric index, which can shift across boots). USB hot-plug should not require pipeline restart unless the named device is replaced.
- **ROS 2 colocation:** The pipeline runs on the same machine as (or same DDS domain as) OLAF nodes. Domain ID is configurable.

## Functional Requirements

> **The capability contract.** Every feature in v1 must trace back to one of these. Anything not listed here will not exist in the implementation unless explicitly added.

### Audio I/O & Capture

- **FR1**: The pipeline can detect a configurable wake-word from continuous mic input without dispatching downstream processing prior to detection.
- **FR2**: The pipeline can capture user speech from the local mic device after wake-word detection, terminating capture on voice-activity end-of-speech.
- **FR3**: The pipeline can play synthesized audio through the local speaker device with no perceivable buffering pause between frames.
- **FR4**: The pipeline can pin audio devices by stable name in configuration, surviving reboots and USB hot-plug events of unrelated devices.
- **FR5**: The pipeline can detect mid-utterance barge-in (user speaking during SPEAKING lifecycle state) and abort current playback.

### Speech Recognition

- **FR6**: The pipeline can transcribe user speech to text on-device, without transmitting audio to a cloud service.
- **FR7**: The pipeline can use the Hailo-8L NPU when present to accelerate Whisper inference; if not present, it can fall back to CPU inference with a logged warning.
- **FR8**: The pipeline can attach a confidence score to each transcript and route low-confidence transcripts to a clarification path.

### Conversational Intelligence

- **FR9**: The pipeline can route a transcribed user turn to the Talker fast-path or to the orchestrator daemon based on a configurable routing decision.
- **FR10**: The pipeline can read belief state from the orchestrator daemon via HTTP API to inform Talker responses.
- **FR11**: The pipeline can dispatch a user turn to the orchestrator daemon via `POST /turn` and consume the typed event stream (narration, subagent events, response chunks, turn_end).
- **FR12**: The pipeline can synthesize a fast-path response from belief state using an in-pipeline LLM (Talker), emitting Cartesia-tagged text.
- **FR13**: The pipeline can recover gracefully from orchestrator stream stalls, emitting a filler response after a configurable timeout while keeping the lifecycle in THINKING.
- **FR14**: The pipeline can recover gracefully from a missing `turn_end` event by flushing the splitter and transitioning lifecycle after the last audio frame plays.

### Voice Synthesis

- **FR15**: The pipeline can stream Cartesia-tagged text to Cartesia Sonic-3 and receive audio frames in response.
- **FR16**: The pipeline can degrade gracefully when Cartesia is unreachable, entering a text-only mode signaled by a sad-emotion OLAF expression and a logged error.
- **FR17**: The pipeline can use a configurable Cartesia voice ID and default emotion.

### Embodiment Expression

- **FR18**: The pipeline can parse incoming text streams as Cartesia SSML, identifying `<emotion value="X"/>` tags and `[burst]` events incrementally (token-by-token, tags may split across token boundaries).
- **FR19**: The pipeline can segment text on whichever boundary comes first: sentence terminator, emotion tag, or burst tag.
- **FR20**: The pipeline can map every Cartesia emotion tag to a defined `OlafAction` via the `expression_map.yaml` mapping table, with no silent gaps.
- **FR21**: The pipeline can resolve unmapped emotion tags through a fallback family table, producing a defined `OlafAction` with a logged warning.
- **FR22**: The pipeline can attach `OlafAction` event metadata to the matching Cartesia audio frame, ensuring expression events publish in lockstep with audio.
- **FR23**: The pipeline can publish `OlafAction` events to ROS 2 on `/olaf/expression`, anchored to audio frame send time, achieving 30-80ms anticipatory alignment with voice.
- **FR24**: The pipeline can suppress republishing of unchanged base emotions via a "last published" cache, while always publishing burst events.
- **FR25**: The pipeline can strip Cartesia-unsupported burst tags from the TTS stream while still publishing them as `OlafAction` events to ROS 2.

### Lifecycle State Management

- **FR26**: The pipeline can publish OLAF lifecycle state changes (SLEEPING, LISTENING, THINKING, SPEAKING, IDLE) to ROS 2 at conversation milestones.
- **FR27**: The pipeline can transition between lifecycle states based on observable events: wake-word detection, end-of-speech, first audio frame, last audio frame, idle timeout.
- **FR28**: The pipeline can transition from IDLE to SLEEPING after a configurable idle timeout (default: 5 minutes).
- **FR29**: The pipeline can transition from SPEAKING to LISTENING directly on barge-in detection, bypassing THINKING.
- **FR30**: The pipeline can flush in-flight expression events on barge-in to prevent OLAF being stuck on a half-finished pose.

### Configuration & Operations

- **FR31**: The pipeline can load `expression_map.yaml` and `pipeline.toml` at startup, validating schema and refusing to start on validation failure.
- **FR32**: The pipeline can hot-reload `expression_map.yaml` on `SIGHUP`, swapping the in-memory mapping atomically; if validation fails, it retains the prior mapping and logs the error.
- **FR33**: The pipeline can defer `SIGHUP` reloads received mid-utterance, applying them after the current turn completes.
- **FR34**: The pipeline can load credentials (Cartesia, Anthropic API keys) from a secrets file referenced by path in `pipeline.toml`, never inlined and never logged.
- **FR35**: The pipeline can refuse to start when configured with a non-localhost orchestrator URL without a corresponding shared-secret or mTLS configuration.
- **FR36**: The pipeline can run as a systemd service with restart-on-failure and structured logging to journald.

### Observability & Diagnostics

- **FR37**: The pipeline can emit structured (JSON) logs at INFO/WARN/ERROR levels for lifecycle transitions, emotion fallback resolutions, config reloads, and external service failures.
- **FR38**: The pipeline can log unmapped Cartesia emotion tags with the mapped fallback (DEBUG-level on first occurrence, WARN if completely unknown).
- **FR39**: The pipeline can omit raw audio from all logs at all levels; transcripts only appear at DEBUG level, which is off by default.
- **FR40**: The pipeline can rotate logs locally with a configurable retention window (default: 7 days).
- **FR41**: The pipeline can verify Hailo-8L driver presence at startup and log a clear error before falling back to CPU inference.
- **FR42**: The pipeline does not persist user audio or transcripts to disk in the default operational path.
- **FR43**: The pipeline does not initiate any outbound network connection beyond the configured Cartesia API, Anthropic API, and orchestrator daemon endpoints (no telemetry, no analytics).

## Non-Functional Requirements

> **Selective by design.** Only categories that apply to a single-user embedded voice pipeline. Scalability and accessibility (in the WCAG/Section-508 sense) are not relevant at v1 scope.

### Performance

These are the latency budgets from the Success Criteria, restated as testable NFRs (NFR1–NFR7 trace back to the Technical Success table).

- **NFR1**: Simple-turn end-to-end latency (end-of-speech → first audio frame from Cartesia) must be ≤ 1500ms at p95 over a 30-min soak.
- **NFR2**: Complex-turn end-to-end latency (end-of-speech → first narration audio frame) must be ≤ 1000ms at p95.
- **NFR3**: On-device STT latency (end-of-speech → transcript ready) must be ≤ 500ms at p95 with Hailo-8L. CPU fallback path defines its own p95.
- **NFR4**: Cartesia TTS latency (text-with-tags → first audio frame) must be ≤ 400ms at p95.
- **NFR5**: Voice/embodiment alignment must be 30–80ms anticipatory at p95; outside this window is a defect.
- **NFR6**: Audio playback must not introduce buffering pauses > 100ms during a single utterance.
- **NFR7**: `SIGHUP`-triggered config reload must complete within 1 second from signal receipt.

### Reliability

- **NFR8**: Pipeline must run continuously for ≥ 7 days under normal household ambient conditions without an unplanned restart, panic, or unrecoverable error state.
- **NFR9**: Recovery from external service failure (Cartesia or Anthropic unreachable) must complete within 5 seconds of reachability return, without manual intervention.
- **NFR10**: A malformed config file at startup must produce a clear error and prevent startup; a malformed config on `SIGHUP` must produce a clear error and retain the prior config (no silent-broken state).
- **NFR11**: Pipeline must survive USB hot-plug events on unrelated devices without restart or audio interruption.
- **NFR12**: Wake-word false-positive rate must be ≤ 1 per hour of typical household ambient (TV, conversation, kitchen sounds) at the production threshold.
- **NFR13**: Wake-word false-negative rate must be ≤ 5% in normal speaking conditions at the production threshold.

### Resource Constraints

- **NFR14**: Pipeline at idle (waiting for wake-word) must consume < 5% of Pi 5 CPU sustained.
- **NFR15**: Pipeline during active conversation must keep peak CPU < 80% to leave headroom for OS, ROS 2, and thermal management.
- **NFR16**: Pipeline RAM footprint (excluding Whisper model resident memory) must be < 2 GB.
- **NFR17**: Pipeline must operate within Pi 5 thermal throttle budget under sustained conversation load (no audio dropouts caused by throttling). Active cooling (fan + heatsink) is assumed.
- **NFR18**: Local log volume must not exceed 100 MB per day at default INFO level.

### Integration Reliability

- **NFR19**: Cartesia TTS integration must implement automatic retry with exponential backoff (max 3 retries) on transient network errors before transitioning to text-only degraded mode.
- **NFR20**: Orchestrator stream connection must include a heartbeat or stall-detection timeout (configurable, default 5s) and trigger graceful filler-response on stall.
- **NFR21**: ROS 2 publishing on `/olaf/expression` must use reliable QoS; lost messages are not acceptable for embodiment correctness.
- **NFR22**: Anthropic API integration must implement graceful degradation: if the Talker API is unreachable, dispatch the turn to the orchestrator instead (slow path).

### Security

> Functional security requirements are in FR34, FR35, FR42, FR43. NFRs below are quality attributes complementing them.

- **NFR23**: All API credentials must be stored at file permission `0600` and loaded from disk only at process startup; the process must not re-read or expose them at runtime.
- **NFR24**: Outbound HTTPS connections (Cartesia, Anthropic) must validate TLS certificates; the pipeline must refuse to start if certificate validation is disabled.
- **NFR25**: All log output must be inspectable by Kamal locally; no log line may contain raw credential material, raw audio bytes, or (at INFO level or above) user transcripts.

### Maintainability

- **NFR26**: This PRD, the brief (`voice-agent-pipeline-brief.md`), and the distillate (`voice-agent-pipeline.md`) are the canonical specs. Any implementation decision deviating from them must update the relevant document in the same change.
- **NFR27**: Configuration schemas (`expression_map.yaml` and `pipeline.toml`) must be versioned with a `schema_version` field; the pipeline must reject configs with an incompatible schema version at startup.
- **NFR28**: Components within the pipeline (wake-word, STT, Talker, splitter, TTS, ROS 2 publisher) must be independently testable — each can be exercised in isolation with mock or synthetic inputs.
- **NFR29**: Logs must be machine-readable JSON to enable post-hoc analysis without manual parsing.
