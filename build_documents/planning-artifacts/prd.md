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
  - step-e-01-discovery
  - step-e-02-review
  - step-e-03-edit
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
lastEdited: '2026-05-06'
editHistory:
  - date: '2026-05-06'
    summary: |
      Direction shift — continuous-conversation while AWAKE (no per-turn wake-word),
      sleep via Talker LLM tool-call (intent-based), mood-tinted 2–8 word wake greeting,
      4-topic ROS 2 event model (mood / activity / speech_emotion / vocalization)
      replacing single /olaf/expression channel, common event envelope
      (timestamp, schema_version, source, correlation_id), Talker becomes tool-using
      (go_to_sleep, set_mood). Removed idle auto-sleep (FR28). Deferred barge-in
      (FR5, FR29, FR30, Journey 3) to v1.5. Stale "Anthropic" references replaced
      with "active Talker provider" per Story 2.2 provider-agnostic factory.
      Event schema_version bumped to 2 (breaking change to publish topology).
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

`voice-agent-pipeline` is the Pipecat-based service that owns OLAF Companion's voice loop and embodiment surface — the layer that makes OLAF feel like a companion rather than a remote API call. It captures user speech, dispatches turns to a separate orchestrator (a Claude Code session) for reasoning, generates spoken responses with Cartesia Sonic-3, and drives OLAF's expressive surface in sync with that voice. It is the **only** component touching audio hardware and the **only** publisher to OLAF's four event topics (`mood`, `activity`, `speech_emotion`, `vocalization`); it deliberately does not reason, call MCP tools, or write belief state.

The component is conversation-shaped, not turn-shaped: the wake word transitions OLAF from `SLEEPING` to `AWAKE`, after which user speech flows continuously without re-arming. Sleep is intent-driven — the Talker LLM decides when the user has signalled "we're done" and fires a `go_to_sleep()` tool-call to return the pipeline to `SLEEPING`. There is no idle auto-sleep.

The PRD covers v1 of this component — Phases 0 through 3 — targeting a single user (Kamal) on a Raspberry Pi with Hailo-8L NPU, integrated with OLAF's physical embodiment over ROS 2.

### What Makes This Special

Six architectural decisions shape the v1 commitment and must survive any future rewrite:

1. **Talker fast-path inside Pipecat.** Simple turns are answered from belief state in-pipeline without waiting for the orchestrator's deeper reasoning. This eliminates the dead air that single-LLM voice agents force on every multi-step turn.
2. **Single fan-out for audio-anchored events.** Voice (to Cartesia TTS), `speech_emotion`, and `vocalization` events are emitted from the same parsed segment, anchored to the same audio frames. Drift between voice and these expression events is prevented by construction — no parallel channels. Target alignment: ~30–80ms anticipatory. (`mood` and `activity` events are FSM-driven and publish on transition; they are not audio-anchored.)
3. **On-device STT (Whisper + Hailo-8L).** Speech transcription runs locally on the Pi. Both privacy and latency benefit from the same decision; cloud STT is explicitly excluded for v1.
4. **Mapping is data, not code.** The Cartesia tag → OLAF expression mapping lives in `expression_map.yaml`, reloadable on `SIGHUP`. Adding emotions, bursts, or fallback families is a config change, not a deploy. The `speech_emotion` event payload carries both the raw tag and the resolved fallback so consumers can use either.
5. **Continuous conversation; intent-based sleep.** Wake-word fires only on the `SLEEPING → AWAKE` transition. While `AWAKE`, the mic stays open and turns flow without re-prompting. The Talker LLM detects sleep intent in natural language and fires a `go_to_sleep()` tool — no exact-phrase match, no idle timer.
6. **Multi-topic event publish with a common envelope.** Pipeline publishes on four typed ROS 2 topics — `mood` (latched, slow-changing disposition), `activity` (FSM transitions including `working` sub-modes), `speech_emotion` (per-segment Cartesia tags, open-set), `vocalization` (punctual non-verbals: `[laugh]`, `[sigh]`, …). Every event carries a common envelope: `timestamp`, `schema_version`, `source`, `correlation_id`. The publisher is Protocol-based; ROS 2 is the v1 implementation.

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

A conversation feels alive when:

- He says the wake word once and OLAF wakes with a 2–8 word greeting that sounds like a friend ("hey, what's up?") — not a scripted "Hello, I am OLAF"
- After waking, he can ask follow-up questions without re-saying the wake word — the conversation just flows until he signals he's done
- He can ask "what's on my calendar today?" and hear OLAF start responding without an awkward pause
- OLAF's pose, eyes, and LEDs visibly match the emotional tone of the response — no smiling on a sad sentence
- OLAF's mood is coherent across the conversation — it doesn't flicker between happy and gloomy turn-by-turn
- The wake word triggers reliably from across the room and doesn't false-trigger during phone calls or background TV
- When he says something like "okay, that's all for now," OLAF understands it as a goodbye and goes back to sleep without a literal command
- He doesn't think "I'm talking to software" — he just talks to OLAF

### Project Success (build outcomes)

- All six hard architectural decisions (Talker fast-path, single fan-out for audio-anchored events, on-device STT, mapping-as-data, continuous-conversation/intent-sleep, multi-topic publish with common envelope) are upheld in the implementation; violations are bugs, not tradeoffs
- The component is **replaceable**: the `POST /turn` contract with the orchestrator and the four typed event schemas on ROS 2 (with versioned envelope) survive any future rewrite of internals
- Configuration is data-driven: adding a new emotion mapping requires editing `expression_map.yaml`, not code
- The `EventPublisher` is Protocol-based: ROS 2 is the v1 channel, but a fake/log adapter exists for tests and future channel adapters (Zenoh, NATS) require no consumer changes

### Technical Success (measurable performance)

Starting targets — validated against measured baseline during Phase 0/1, then locked. Each row below is formalized as a testable NFR (NFR1–NFR7, NFR12, NFR13, NFR30–NFR32) with p95 measurement context.

| Metric | Target | Maps to | Rationale |
|---|---|---|---|
| **Simple turn** (Talker fast-path): end-of-speech → first audio frame | **≤ 1500ms** | NFR1 | Voice agents over ~2s feel dead; under 1.5s feels live |
| **Complex turn** (orchestrator narration): end-of-speech → first audio frame | **≤ 1000ms** | NFR2 | Narration ("let me check…") must arrive before the user wonders if anything is happening |
| **Wake-greeting latency**: wake-word detected → first greeting audio frame | **≤ 1500ms** | NFR30 | First impression on wake; longer than this and the greeting feels delayed and robotic |
| **On-device STT latency**: end-of-speech → transcript ready | **≤ 500ms** | NFR3 | Whisper-small + Hailo-8L on Pi should hit this |
| **Voice/`speech_emotion` alignment** | **30–80ms anticipatory** | NFR5 | Audio-anchored expression slightly ahead of voice; outside this window is a perceivable bug. (`mood` and `activity` are FSM-driven, no audio-alignment requirement.) |
| **`mood` event cadence** | **≤ 4 publishes per hour** sustained | NFR31 | Mood is a slow-changing disposition; flickering is a defect |
| **Wake-word false positives** | **≤ 1 per hour** of normal ambient background | NFR12 | Higher rates make the system feel paranoid |
| **Wake-word false negatives** | **≤ 5%** in normal speaking conditions | NFR13 | Higher rates frustrate the user into shouting |
| **Cartesia TTS latency**: text-with-tags → first audio frame | **≤ 400ms** | NFR4 | Mostly Cartesia's responsibility; splitter must not add buffering |
| **Talker tool-call decision overhead** | **≤ 100ms** added to simple-turn budget | NFR32 | `go_to_sleep` / `set_mood` detection cannot push simple turn past NFR1 budget |
| **Unmapped Cartesia tag handling** | **100% fallback coverage** | FR21, FR38 | Truly unknown tags log warning and render neutral. No silent gaps. |

### Measurable Outcomes (how we know v1 is done)

A 30-minute live conversation session completes without any of:

- Drift between voice and `speech_emotion` / `vocalization` events noticeable to the user
- A `mood` flicker (mood publish more than once every ~10 minutes for no narrative reason)
- A missed `go_to_sleep` intent (Kamal says a clear goodbye, OLAF stays awake) or a false-fire (Talker decides to sleep mid-conversation when user did not signal)
- An unhandled Cartesia tag causing OLAF to freeze or default visibly
- A missed wake-word or a false-fire
- A failed wake-greeting (silence on wake, scripted "Hello", or wrong-mood greeting)
- Audio cutout, stutter, or buffering pause longer than 100ms
- A latency target above being missed by more than 20%

Plus: all Phase 0–3 validation goals pass (per phase table in `voice-agent-pipeline.md`), and `expression_map.yaml` reloads via `SIGHUP` without restart.

## Product Scope

### MVP — Minimum Viable Product (Phase 3 complete)

The component is "useful" when it can serve a multi-turn live conversation with OLAF: wake-word triggers a mood-tinted greeting, the conversation flows continuously without re-prompting, voice and audio-anchored expression are in sync, and the Talker LLM detects the user's goodbye and returns OLAF to sleep. Specifically:

- All six primary emotions (neutral, content, excited, sad, angry, scared) render correctly on OLAF
- Secondary emotions (happy, curious, sympathetic, surprised, frustrated, melancholic) map to their primary equivalents and render
- Fallback table covers Cartesia's full 60+ emotion vocabulary; `speech_emotion` event payload carries both `raw_tag` and `resolved_fallback`
- **Continuous conversation while AWAKE.** Wake-word fires on `SLEEPING → AWAKE`; subsequent turns flow without re-arming the wake word.
- **Intent-based sleep.** Talker LLM detects "we're done" semantically and fires a `go_to_sleep()` tool-call; no exact-phrase match, no idle timer.
- **Mood-tinted wake greeting.** On every wake, Talker generates a 2–8 word greeting in a "cool friend" register, tinted by current mood (e.g. "what's up?", "hey, you again", "hmm, hi").
- **Mood model.** Six to eight discrete mood states (`happy, playful, calm, curious, gloomy, grumpy, sleepy, excited`); slow-changing (~15–20 min cadence). Talker fires `set_mood(mood)` tool when conversation context warrants a shift.
- **Activity FSM publishes on transition** to the `activity` topic: `starting, sleeping, waking, listening, working, speaking, going_to_sleep`. The `working` state has v1 sub-modes `thinking` (Talker generating in-pipeline) and `delegating` (orchestrator dispatched, awaiting response).
- **Four-topic event publish** on ROS 2: `mood`, `activity`, `speech_emotion`, `vocalization`. Every event carries common envelope `{timestamp, schema_version, source, correlation_id, payload}`. Vocalizations (`[laugh]`, `[sigh]`, …) come from LLM-emitted inline tags parsed pre-TTS.
- **Talker is tool-using.** v1 tools: `go_to_sleep()`, `set_mood(mood)`. Tool registry hardcoded in v1.
- Wake-word + on-device STT + Cartesia TTS + 4-topic ROS 2 publish all integrated end-to-end
- Talker fast-path serves simple turns; orchestrator dispatch serves complex turns
- All technical-success latency targets hit (within 20% margin), including wake-greeting latency (NFR30) and Talker tool-call overhead (NFR32)

### Growth Features (Post-MVP, v1.1)

- **Barge-in handling.** Mid-utterance interruption support (FR5/FR29/FR30 cluster previously in v1, deferred — adds VAD-during-TTS, splitter flush on interrupt, lifecycle SPEAKING → LISTENING short-circuit)
- **Expanded `working` sub-modes.** Add `searching` (RAG / web tool in flight), `tooling` (other function/tool calls), `composing` (long-form streaming) to enable richer Olaf animations
- Tertiary emotion mappings (flirtatious, mysterious, sarcastic) — full custom OLAF behaviors instead of fallback
- Polished secondary-emotion poses — lift from "maps to primary" to first-class distinct expressions
- Bursts beyond `[laughter]` once Cartesia ships them (`[gasp]`, `[clears_throat]`) wired through the `vocalization` topic
- Latency budget tightening based on measured Phase 0–3 baselines
- Configurable idle auto-sleep (currently disabled) for contexts where strict-intent sleep is undesirable

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

**Deferred from v1 (this edit, 2026-05-06):** the barge-in cluster (FR5, FR29, FR30, Journey 6) is moved to v1.5. **Removed (this edit):** idle auto-sleep (formerly FR28) — sleep is now intent-driven only via Talker tool-call (FR45/FR46). All other Phase 0-3 items in the brief/distillate remain MVP for this component. The brief and distillate must be updated in the same change-set (NFR26) to reflect this direction shift.

### Risk Mitigation Strategy

**Technical risks**

| Risk | Mitigation |
|---|---|
| **Hailo-8L driver/runtime maturity** for Whisper inference. New SDKs lag; on-device inference may not hit 500ms target. | Phase 0 validates STT latency on CPU first (Whisper-base baseline). Phase 1 adds Hailo-8L acceleration. If Hailo-8L fails to deliver, fall back to a smaller Whisper variant (tiny/base) on CPU and re-evaluate latency target. |
| **Cartesia cloud dependency** (network, rate limits, pricing changes). | Pipeline must degrade gracefully when Cartesia is unreachable: text-only mode with a `<emotion value="sad"/>` LED indicator on OLAF. No silent failure. Long-term mitigation lives in Vision (on-device TTS). |
| **Audio-frame anchoring complexity** in Pipecat. Threading expression-event metadata through the frame pipeline correctly is the technically hardest piece. | Phase 2 validates this with measurable end-to-end timing tests. If Pipecat's processor model can't cleanly carry the metadata, fall back to time-based correlation (publish OLAF event N ms after audio frame play time). Document the deviation if used. |
| **ROS 2 multicast on home network** can be fragile. | Mitigation: colocate the pipeline and OLAF nodes on the same machine, or wired LAN with explicit DDS domain ID. WiFi multicast is not supported for v1. |
| **Wake-word false positives** during phone calls, music, TV. | Tunable threshold in `pipeline.toml`. Phase 3 includes soak testing in real ambient conditions to set a sane default. If a single wake-word model can't hit the false-positive target, consider dual-stage: low-power detector + small confirmation model. |
| **Cartesia emotion vocabulary drift.** Cartesia ships ~60 tags now; future tags will land outside our mapping. | The fallback family table covers the principle; `speech_emotion` events publish both `raw_tag` and `resolved_fallback` so consumers (Olaf, dashboard) can use either; `unmapped emotion` warnings make drift visible. Out-of-band: quarterly check for new Cartesia emotions and mapping table updates. |
| **Talker tool-call reliability.** The `go_to_sleep` / `set_mood` tools depend on the LLM detecting intent correctly. False-positive `go_to_sleep` ends a real conversation; false-negative leaves OLAF stuck awake when user has signalled they're done. | (1) Tool inputs validated against typed Pydantic schemas before execution; invalid calls log WARN and are dropped. (2) Wake-word remains the recovery path if false-positive sleep fires — user re-wakes immediately. (3) No idle auto-sleep means false-negatives are recoverable: user just says it again. (4) Soak-test prompt tuning during Phase 3; track FP/FN rate as part of the 30-min session pass criteria. |
| **Four-topic schema drift across consumers.** Olaf, dashboards, future channels each consume `mood`/`activity`/`speech_emotion`/`vocalization` independently; uncoordinated schema changes break consumers silently. | (1) Common envelope carries `schema_version` (FR52) — breaking changes bump it, additive changes don't. (2) `EventPublisher` Protocol decouples wire format from publishing logic; ROS 2 `.msg` definitions live alongside the Pydantic models in this repo. (3) Schema contracts pinned in `voice-agent-pipeline.md` distillate; updates require simultaneous PRD/distillate edits per NFR26. |
| **Mood publish-rate enforcement vs LLM cooperation.** Talker prompt asks for restraint, but LLMs sometimes fire `set_mood` aggressively. | Cooldown is enforced at the publisher (FR49 / NFR31), not trusted of the LLM. Over-rate calls are dropped with WARN; in-memory mood state is not updated until a publish succeeds. |

**External dependency risks** (the equivalent of "market risk" for a personal project)

| Risk | Mitigation |
|---|---|
| **Cartesia API pricing or availability changes.** | Designed to be replaceable: TTS is a single processor in the Pipecat pipeline. Swapping providers is a config change + processor rewrite, not an architecture change. |
| **Cloud LLM for Talker** *(Story 2.2 final design: provider-agnostic factory across OpenAI / Groq / Gemini.)* | Same — Talker is one in-pipeline LLM call. v1 ships with all three providers wired; operator picks via `[talker] provider` in setup.toml. Default is Groq Llama 8B Instant (cheapest, ~150–270 ms TTFB on the dev host — fits inside NFR1's budget with headroom). Switching providers is a one-line config change; switching to a self-hosted vLLM / Together / Fireworks is a one-line entry in `_PROVIDER_BASE_URLS` plus a sub-block on TalkerConfig. |
| **Hailo-8L hardware availability / firmware EOL.** | Phase 0 validates that CPU Whisper is acceptable as a fallback. The pipeline must not hard-require Hailo-8L; it should degrade to CPU with a logged warning. |

**Resource / execution risks**

| Risk | Mitigation |
|---|---|
| **Solo dev with LLM coding partner** — context drift between this PRD/distillate and reality is the biggest risk. | The PRD + distillate + brief are the canonical contracts. Any deviation discovered during implementation must update those documents (treat them as living, not write-once). Don't let the code drift past the spec. |
| **Phase 2 (real OLAF integration) is the riskiest single jump.** | Phase 2 is gated on Phase 1 working with stdout mock. If Phase 1 isn't clean, do not move to Phase 2 — debug the splitter first. |
| **Soak testing requires real ambient conditions** that can't be fully simulated. | Plan: Phase 3 lives on the Pi for at least a week of normal household use before declaring v1 done. Bug bash is the conversation-quality test, not a unit test count. |

## User Journeys

> **Audience note:** voice-agent-pipeline is a single-user component. "Journeys" here are concrete interaction scenarios the implementation must support, written from Kamal's perspective. Each one names the capabilities it exercises — that feeds the functional requirements section directly.

### Journey 1: Wake from sleep (mood-tinted greeting)

**Scenario.** OLAF is sleeping. Kamal walks into the kitchen.

**Sequence:**

1. `activity = sleeping`. Only the wake-word detector is active on the audio stream; no STT, no Talker invocation.
2. Kamal: "Hey OLAF."
3. Wake-word detector fires; `activity → waking` published on the activity topic.
4. Pipeline invokes Talker with a wake-greeting prompt (system prompt + current `mood` state, no user transcript). Talker returns 2–8 words in the "cool friend" register, mood-tinted:
   - mood `calm` → `<emotion value="content"/> hey, what's up?`
   - mood `playful` → `<emotion value="excited"/> oh, hi!`
   - mood `sleepy` → `<emotion value="calm"/> mm, hey...`
5. Splitter parses; `speech_emotion` publishes for first segment; Cartesia streams audio.
6. `activity → speaking` on first audio frame; `speech_emotion(raw_tag, resolved_fallback)` anchored to it.
7. Audio plays; last frame triggers `speaking → listening` (continuous mic capture).
8. Total wall time: wake-word fired → first greeting audio frame **≤ 1500ms** (NFR30).

**Failure modes & recovery:**

- *Talker LLM unreachable / timeout* → fallback to a static greeting list (`["hey", "yeah?", "hi"]`), no mood tint. Logged at WARN.
- *Mood not initialized* (first wake of process lifetime) → default mood = `calm`.
- *Wake-word false positive* → greeting still plays (the design assumes responsiveness over silence); pipeline returns to `listening`. FP rate is bounded by NFR12.

**Capabilities exercised:** wake-word detection, `mood` state persistence, Talker greeting-mode invocation, `activity` topic transitions (`sleeping → waking → speaking → listening`), `speech_emotion` publish, audio-frame anchoring, fallback greeting.

### Journey 2: Continuous-conversation turn (Talker fast-path)

**Scenario.** Conversation is already ongoing. Kamal is making coffee and asks the time. The wake word is **not** required — OLAF is already `AWAKE`.

**Sequence:**

1. `activity = listening`, mic open. (Pipeline returned here from a prior turn's `speaking`.)
2. Kamal: "What time is it?"
3. VAD detects end-of-speech; on-device STT transcribes.
4. Splitter routes the transcript: short, factual question — Talker fast-path takes it. `activity → working` with sub-mode `thinking`.
5. Talker reads belief state via daemon API (`GET /beliefs?keys=time`) and generates a reply with no tool-call: `<emotion value="content"/> it's 8:47 in the morning.`
6. `activity → speaking` on first audio frame.
7. Cartesia streams audio; splitter fans out: text+SSML to TTS, `speech_emotion(raw_tag="content", resolved_fallback="content")` to ROS 2, anchored to first audio frame.
8. OLAF (consumer) receives `speech_emotion` and renders pose; LED ring goes warm amber.
9. Audio plays; last frame triggers `speaking → listening`. Mic stays open for the next turn — no wake-word required.
10. Total wall time: end-of-speech to first audio frame **≤ 1500ms** (NFR1).

**Failure modes & recovery:**

- *STT confidence too low* → Talker generates a clarification ("Sorry, I didn't catch that?") via the same fast-path; `activity` cycles `working.thinking → speaking → listening`.
- *Daemon belief-state read fails* → Talker falls back to dispatching to the orchestrator (Journey 3).

**Capabilities exercised:** continuous mic capture (no wake-word per turn), VAD-bounded utterance, on-device STT, Talker LLM call (without tool), belief-state read API, Cartesia TTS streaming, splitter fan-out, `activity` topic transitions, `speech_emotion` publish, audio-frame anchoring.

### Journey 3: Complex turn (orchestrator dispatch)

**Scenario.** Kamal asks about his day. The orchestrator must check the calendar (subagent) and reply with structured narration. Continuous-conversation context.

**Sequence:**

1. `activity = listening`. Kamal: "What's on my calendar today?"
2. VAD, STT, transcript ready.
3. Splitter routes: question requires data fetch — dispatch to orchestrator via `POST /turn`. `activity → working` with sub-mode `delegating`.
4. Stream from orchestrator begins:
   - `{"type": "narration", "text": "let me check..."}` → splitter segments, sends to Cartesia, audio plays within ≤1000ms of end-of-speech (NFR2).
   - `{"type": "subagent_started", "name": "comms"}` → no `activity` change in v1 (sub-mode stays `delegating`); orchestrator events become richer sub-modes in v1.5.
   - `{"type": "subagent_progress", "name": "comms", "msg": "Reading calendar"}` → continued.
   - `{"type": "subagent_done", "name": "comms"}`.
   - `{"type": "response_chunk", "text": "<emotion value=\"content\"/> you've got "}` → splitter buffers, segments at sentence / emotion / vocalization boundary; `speech_emotion` publishes per segment.
   - `{"type": "response_chunk", "text": "two meetings today — one at 10..."}` → continues.
   - `{"type": "turn_end"}` → flush splitter; `activity → speaking` (already, on first audio) → `listening` after last audio frame.
5. OLAF expression matches narration; consumer-side animation transitions smoothly on each `speech_emotion` event.

**Failure modes & recovery:**

- *Orchestrator stream stalls* (no event for >5s) → pipeline plays a filler ("still working on it…") via Talker fast-path; `activity` stays at `working.delegating`.
- *Stream ends without `turn_end`* → splitter flushes pending text; `activity → listening` after last audio frame.
- *Cartesia rejects an emotion tag* → Cartesia silently drops; the `speech_emotion` event still publishes the raw tag and resolved fallback for OLAF's consumption.

**Capabilities exercised:** orchestrator dispatch (HTTP/WebSocket stream), streaming SSML parser, segment-on-emotion-or-sentence-or-vocalization logic, narration handling, `working.delegating` sub-mode, `turn_end` cleanup, `speech_emotion` per-segment publish.

### Journey 4: Sleep on intent (Talker tool-call)

**Scenario.** Mid-conversation, Kamal signals he's done. He doesn't have to use a literal phrase — the Talker LLM detects the intent.

**Sequence:**

1. `activity = listening`. Kamal: "okay, that's all for now, thanks."
2. STT transcribes. Splitter routes to Talker fast-path. `activity → working.thinking`.
3. Talker reads the transcript and decides intent is "sleep." It emits a tool-call: `go_to_sleep()` plus a brief acknowledgement: `<emotion value="content"/> alright, see you in a bit.`
4. Pipeline executes the tool — schedules the sleep transition to fire **after** the acknowledgement audio finishes (so the goodbye plays, then OLAF goes quiet).
5. `activity → speaking` on first audio frame; `speech_emotion(content)` published.
6. Last audio frame plays → `activity → going_to_sleep` (transient, optional brief animation hook for OLAF) → `activity → sleeping`.
7. Mic returns to wake-word-only mode. STT, Talker, splitter all idle.

**Failure modes & recovery:**

- *Talker false-fires `go_to_sleep`* mid-real-conversation → user can simply say wake word again immediately; OLAF wakes and resumes. Logged as candidate FP for prompt tuning.
- *Talker misses real sleep intent* → user can keep talking; eventually says it more explicitly. No idle auto-sleep means there's no "hidden" exit.
- *Tool-call decision overhead pushes simple-turn past NFR1* → guard rail: NFR32 (≤100ms tool-call decision overhead).

**Capabilities exercised:** Talker tool-using mode, `go_to_sleep()` tool execution, post-audio deferred transitions, `activity` transitions through `going_to_sleep → sleeping`, mic mode change to wake-word-only.

### Journey 5: Mood shift mid-conversation (Talker tool-call)

**Scenario.** Kamal and OLAF have just shared a laugh. The conversation has become more playful. Talker decides the mood should shift.

**Sequence:**

1. `mood = calm` (latched, last published 12 minutes ago).
2. Kamal makes a joke; OLAF responds with `<emotion value="happy"/> [laugh] oh, that's good.` Splitter publishes `speech_emotion(happy)` and `vocalization([laugh])` events anchored to audio.
3. On the next turn, before generating the response, Talker evaluates the conversation context (last N turns + current `mood`) and decides the mood has drifted. It emits a tool-call: `set_mood("playful")`.
4. Pipeline executes the tool: `mood` topic publishes `playful` (latched). Cooldown timer starts to enforce NFR31 (≤4/hour).
5. Talker continues with its normal response for the current turn, now generated with `playful` mood in its prompt context.
6. OLAF (consumer) receives latched `mood = playful` and shifts ambient base (e.g. eye color shift, posture change). The shift is gradual on Olaf's side, not abrupt.

**Failure modes & recovery:**

- *Talker fires `set_mood` too frequently* → cooldown rejects the publish; logged WARN. NFR31 is enforced at the publisher, not just trusted of the LLM.
- *Talker passes an unknown mood string* → publisher rejects with WARN; previous latched `mood` retained.
- *Wake greeting on next session* → mood is persisted across the SLEEPING period (in-process state for v1; persistence across restarts is v1.5).

**Capabilities exercised:** Talker tool-using mode, `set_mood(mood)` tool execution, `mood` topic publish (latched), mood enum validation, publish-rate cooldown enforcement, mood-aware Talker prompt construction.

### Journey 6: Barge-in mid-response (DEFERRED to v1.5)

> **Status: deferred.** Originally planned for v1, this journey moves to v1.1 / v1.5. Continuous-conversation feel is achievable without barge-in (the user simply waits for OLAF to finish a typically short response, then speaks). Barge-in adds VAD-during-TTS, splitter flush, and lifecycle short-circuit logic — non-trivial work that does not block v1's "alive feeling." Re-prioritized when the soak in Phase 3 reveals whether response lengths feel constrained without it. FRs FR5, FR29, FR30 also marked deferred.

The contract below remains the v1.5 design intent and is preserved here so it doesn't have to be re-derived.

**Scenario.** OLAF starts a long answer; Kamal interrupts.

**Sequence (v1.5 design):**

1. Kamal asks a question; OLAF is mid-response (`activity = speaking`).
2. Kamal speaks again ("wait, actually—") before OLAF finishes.
3. VAD detects sustained voice during `speaking` → barge-in event fires.
4. Pipeline transitions `speaking → listening` immediately.
5. Cartesia audio playback halts; remaining audio frames discarded.
6. Splitter flushes in-flight `speech_emotion` and `vocalization` events: any unpublished events are dropped, *not* published — OLAF doesn't get stuck mid-pose.
7. New utterance is captured and dispatched normally.

**Failure modes & recovery (v1.5):**

- *VAD false-positive during `speaking`* (background noise, OLAF's own audio bleed) → must not trigger barge-in; barge-in requires sustained voice over a threshold.
- *Splitter has just published an event but corresponding audio not yet played* → published event is the truth; OLAF holds that pose until next event.

**Capabilities exercised (v1.5):** mid-stream barge-in detection, splitter state flush, `speaking → listening` short-circuit, audio playback abort.

### Journey 7: Unmapped emotion fallback

**Scenario.** The orchestrator's LLM emits a Cartesia tag the pipeline hasn't explicitly mapped (`<emotion value="enthusiastic"/>`). Pragmatic v1 model: open-set tags are accepted; the pipeline still resolves a fallback for downstream consumers that need the closed enum.

**Sequence:**

1. Response chunk arrives: `<emotion value="enthusiastic"/> that's amazing!`
2. Splitter parses tag → looks up `enthusiastic` in `expression_map.yaml`.
3. Not found in primary or secondary tier → falls through to family table: `enthusiastic` ∈ `high_energy_positive` → `excited`.
4. `speech_emotion` event publishes with payload `{raw_tag: "enthusiastic", resolved_fallback: "excited", family: "high_energy_positive"}`. Consumers (OLAF, dashboard) decide which to honour.
5. Cartesia receives the original tag; if Sonic-3 supports `enthusiastic`, voice prosody reflects it; if not, Cartesia ignores the unknown tag and voice is unchanged.
6. A log entry at DEBUG level: `unmapped emotion 'enthusiastic' → fallback 'excited' via high_energy_positive` (first occurrence per process lifetime).

**Failure modes & recovery:**

- *Tag not in family table at all* (truly unknown) → fall through to `unknown: neutral`; payload still published with `resolved_fallback="neutral"`; logged at WARN.
- *`expression_map.yaml` is malformed at startup* → pipeline refuses to start, error to stderr; doesn't silently run with broken mapping.

**Capabilities exercised:** fallback family resolution, `speech_emotion` open-set schema with raw-and-resolved payload, structured logging for observability, config validation at startup.

### Journey 8: Operator — live mapping tune

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

The eight journeys (seven v1, one deferred) reveal these capability clusters that the functional requirements section specifies:

| Capability area | Journeys exercising it |
|---|---|
| Wake-word detection (always-on, low-power) | 1, 8 |
| Continuous mic capture while AWAKE (no per-turn wake-word) | 2, 3, 4, 5 |
| On-device STT (Whisper + Hailo-8L) | 2, 3, 4, 5 |
| Talker fast-path (in-pipeline LLM, belief-state read) | 2, 4, 5 |
| Talker greeting mode (mood-tinted, 2–8 word) | 1 |
| Talker tool-using (`go_to_sleep`, `set_mood`) | 4, 5 |
| Orchestrator dispatch (HTTP/WebSocket stream) | 3 |
| Streaming SSML parser + tag splitter (emotion, vocalization, sentence boundaries) | 1, 2, 3, 5, 7 |
| Cartesia TTS streaming | 1, 2, 3, 4, 5 |
| Splitter fan-out + audio-frame anchoring | 1, 2, 3, 5 |
| `mood` topic publish (latched, slow-cadence) | 1, 5 |
| `activity` topic publish (FSM transitions + working sub-modes) | all v1 |
| `speech_emotion` topic publish (open-set, raw + fallback) | 1, 2, 3, 4, 5, 7 |
| `vocalization` topic publish (`[laugh]`, `[sigh]`, …) | 5 |
| Common event envelope (`timestamp`, `schema_version`, `source`, `correlation_id`) | all v1 |
| Post-audio deferred state transitions (sleep-after-goodbye) | 4 |
| Mood publish-rate cooldown (NFR31 enforcement) | 5 |
| Wake-greeting fallback (static list when Talker unreachable) | 1 |
| Barge-in detection + splitter flush (DEFERRED to v1.5) | 6 |
| Fallback family resolution | 7 |
| Config: schema validation, SIGHUP reload, atomic swap | 8 |
| Observability: structured logging, warning levels | 4, 5, 7, 8, all |

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
| **Talker LLM** | HTTPS | Out | Active provider per `setup.toml` — Groq (default, llama-3.1-8b-instant), OpenAI (gpt-5.4-nano), or Gemini (gemini-2.5-flash) |
| **Belief-state read** | HTTP | Out | `http://localhost:8001/beliefs` (orchestrator daemon) |
| **Orchestrator dispatch** | HTTP/WebSocket (SSE) | Out | `http://localhost:8001/turn` (orchestrator daemon) |
| **TTS** | HTTPS / WebSocket | Out | Cartesia Sonic-3 API |
| **`mood` topic** | ROS 2 (DDS) | Out | `/olaf/mood` topic — latched (transient_local), reliable, depth=1; slow-changing disposition |
| **`activity` topic** | ROS 2 (DDS) | Out | `/olaf/activity` topic — latched (transient_local), reliable, depth=1; FSM transitions including `working` sub-modes |
| **`speech_emotion` topic** | ROS 2 (DDS) | Out | `/olaf/speech_emotion` topic — volatile, reliable, depth=10; per-segment, audio-anchored Cartesia tags |
| **`vocalization` topic** | ROS 2 (DDS) | Out | `/olaf/vocalization` topic — volatile, reliable, depth=10; punctual non-verbals (`[laugh]`, `[sigh]`, …) |

> All four topics use `ros_domain_id=7` and share the common event envelope `{timestamp, schema_version, source, correlation_id, payload}`. The publisher is implemented behind an `EventPublisher` Protocol; ROS 2 is the v1 channel adapter, with a fake/log adapter available for tests. Adding Zenoh / NATS / WebSocket adapters in the future requires no consumer-side changes.

**Network reachability requirements:**

- **Outbound to internet:** Cartesia (TTS), active Talker provider (Groq / OpenAI / Gemini)
- **Outbound on local network:** Orchestrator daemon (default: localhost; configurable to LAN-reachable, but must require shared secret/mTLS if so — bare HTTP exposure rejected at startup)
- **Local DDS multicast:** ROS 2 traffic; pipeline must be on the same DDS domain as OLAF nodes
- **Inbound:** None. The pipeline is purely an outbound client; nothing else dials in.

### Power Profile

The Pi is **mains-powered** (plugged in continuously as a fixed-location device). Power efficiency still matters for two reasons:

- **Always-on wake-word detector** must run continuously without significant CPU draw. Use a dedicated low-power wake-word model (e.g., openWakeWord, Picovoice Porcupine), not full Whisper. Target: < 5% CPU sustained.
- **Thermal throttling** on the Pi can cause audio dropouts under load. STT + Cartesia decoding + ROS 2 publishing concurrently must stay below the throttle threshold. Active cooling (fan + heatsink) recommended.

**OLAF embodiment power:** TBD — if OLAF is battery-powered, the pipeline must avoid spamming high-frequency events. Already designed for this: the splitter's "last published" cache prevents republishing unchanged `speech_emotion` values within a turn (FR24), `mood` is rate-limited (NFR31), and `activity` only publishes on actual transitions.

### Security Model

> **Single-user personal device on a private home network.** No multi-tenant, no compliance regime. Threat model is realistic, not adversarial: protect against accidental mistakes, not nation-state attackers.

**Credentials:**

- **Cartesia API key** stored in a separate secrets file (path referenced from `pipeline.toml`, not inline). File permissions `0600`. Never logged. Rotation: manual.
- **Active Talker provider's API key** (one of `OPENAI_API_KEY` / `GROQ_API_KEY` / `GEMINI_API_KEY` — whichever matches `[talker] provider` in setup.toml) — same handling as Cartesia.
- **No other outbound credentials.**

**Network exposure:**

- **Default: localhost-only** for orchestrator daemon connection. The pipeline does not bind any listening port itself.
- If `daemon.url` is configured to a LAN address, the pipeline **must** require a shared secret (Bearer token in `Authorization` header) or mTLS. Validation happens at startup; if a network URL is set without a shared secret configured, pipeline refuses to start with a clear error.

**Privacy:**

- **Wake-word-gated mic capture (when SLEEPING).** While `activity = sleeping`, only the wake-word detector consumes the mic stream. Pre-wake audio is buffered in-memory only and discarded; not written to disk, not transmitted.
- **Continuous mic capture while AWAKE.** When `activity ∈ {listening, working, speaking}`, the mic is continuously captured for VAD + STT (Journey 2). This is an explicit privacy trade-off for the continuous-conversation experience. Audio is still in-memory only and discarded after STT; transcripts are not persisted (FR42).
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

- **FR1**: The pipeline can detect a configurable wake-word from continuous mic input while in `activity = sleeping` without dispatching any downstream processing prior to detection. Wake-word detection is the **only** event that transitions `sleeping → waking`.
- **FR2**: The pipeline can capture user speech from the local mic device while `activity ∈ {listening, working, speaking}` (continuous capture while AWAKE), terminating each utterance on voice-activity end-of-speech. **No wake-word is required between turns.**
- **FR3**: The pipeline can play synthesized audio through the local speaker device with no perceivable buffering pause between frames.
- **FR4**: The pipeline can pin audio devices by stable name in configuration, surviving reboots and USB hot-plug events of unrelated devices.
- **FR5** *(DEFERRED to v1.5)*: The pipeline can detect mid-utterance barge-in (user speaking during `activity = speaking`) and abort current playback. **Status: deferred from v1**. v1 ships without barge-in; user waits for OLAF to finish before speaking.

### Speech Recognition

- **FR6**: The pipeline can transcribe user speech to text via a configurable STT backend selected in `setup.toml`. The v1 default is **Groq Whisper-Large-V3-Turbo** (cloud, OpenAI-compatible API). An on-device Whisper backend (`faster-whisper`) is retained as an opt-in offline alternative behind the same Protocol seam (Story 1.4). *Reversal of the original "on-device only" FR6: see `sprint-change-proposal-2026-05-12.md` for the latency / accuracy / hardware-feasibility rationale.*
- **FR7**: The pipeline can use the Hailo-8L NPU when present to accelerate Whisper inference; if not present, it can fall back to CPU inference with a logged warning. *Note: FR7's premise — that Hailo acceleration is necessary for STT viability — was revised on 2026-05-12 (see `sprint-change-proposal-2026-05-12.md`). With cloud STT as v1 default, Hailo capacity is freed for other v2 workloads.*
- **FR8**: The pipeline can attach a confidence score to each transcript and route low-confidence transcripts to a clarification path.

### Conversational Intelligence

- **FR9**: The pipeline can route a transcribed user turn to the Talker fast-path or to the orchestrator daemon based on a configurable routing decision.
- **FR10**: The pipeline can read belief state from the orchestrator daemon via HTTP API to inform Talker responses.
- **FR11**: The pipeline can dispatch a user turn to the orchestrator daemon via `POST /turn` and consume the typed event stream (narration, subagent events, response chunks, turn_end).
- **FR12**: The pipeline can synthesize a fast-path response from belief state using an in-pipeline LLM (Talker), emitting Cartesia-tagged text and (optionally) tool-calls. The Talker operates in two modes: **conversational** (generates a spoken reply, may emit tool-calls in parallel) and **greeting** (generates a 2–8 word mood-tinted wake greeting, see FR44).
- **FR13**: The pipeline can recover gracefully from orchestrator stream stalls, emitting a filler response after a configurable timeout while keeping `activity` at `working.delegating`.
- **FR14**: The pipeline can recover gracefully from a missing `turn_end` event by flushing the splitter and transitioning `activity → listening` after the last audio frame plays.

### Voice Synthesis

- **FR15**: The pipeline can stream Cartesia-tagged text to Cartesia Sonic-3 and receive audio frames in response.
- **FR16**: The pipeline can degrade gracefully when Cartesia is unreachable, entering a text-only mode signaled by a sad-emotion OLAF expression and a logged error.
- **FR17**: The pipeline can use a configurable Cartesia voice ID and default emotion.

### Embodiment Expression

- **FR18**: The pipeline can parse incoming text streams as Cartesia SSML, identifying `<emotion value="X"/>` tags, vocalization tags (`[laugh]`, `[sigh]`, `[gasp]`, …), and any other inline events incrementally (token-by-token; tags may split across token boundaries).
- **FR19**: The pipeline can segment text on whichever boundary comes first: sentence terminator, emotion tag, or vocalization tag.
- **FR20**: The pipeline can produce a `speech_emotion` event for every `<emotion/>` tag encountered in the stream, carrying the **resolved canonical emotion name** plus an audit trail (`raw_tag` as emitted by the LLM, `resolved_fallback` indicating how the resolver landed on that name). The schema accepts open-set Cartesia tag strings, including ones unknown to the v1 mapping; consumers handle unknowns gracefully via the audit fields. The wire payload is identity-only — embodiment vocabulary (pose / LED / eye state) is the consumer's responsibility, keyed on the canonical name. (Pre-schema-3 the payload also carried an `expression_data: dict[str, Any]` block populated from `expression_map.yaml`; that field was removed in sprint-change-proposal-2026-05-10 to repair the consumer-agnostic publisher boundary.)
- **FR21**: The pipeline can resolve unmapped emotion tags through a fallback family table, populating the `resolved_fallback` field of the `speech_emotion` event with a defined family member and logging at DEBUG (first occurrence) or WARN (truly unknown, fell to `neutral`).
- **FR22**: The pipeline can attach `speech_emotion` and `vocalization` event metadata to the matching Cartesia audio frame, ensuring audio-anchored events publish in lockstep with audio.
- **FR23**: The pipeline can publish `speech_emotion` events to ROS 2 on `/olaf/speech_emotion`, anchored to audio frame send time, achieving 30–80ms anticipatory alignment with voice (NFR5).
- **FR24**: The pipeline can suppress republishing of unchanged `speech_emotion` values via a "last published" cache (turn-scoped, reset at `activity → listening`), while always publishing `vocalization` events.
- **FR25**: The pipeline can publish `vocalization` events (e.g. `[laugh]`, `[sigh]`) to ROS 2 on `/olaf/vocalization`, deciding per-tag whether to also pass the tag to Cartesia (when Cartesia supports it) or strip it from the TTS text. Vocalization source: LLM-emitted inline tags parsed pre-TTS (Cartesia-emitted bursts are not v1).

### Lifecycle State Management

- **FR26**: The pipeline can publish `activity` state changes to ROS 2 on `/olaf/activity` at every transition. The v1 state set is `{starting, sleeping, waking, listening, working, speaking, going_to_sleep}`. The `working` state has v1 sub-modes `{thinking, delegating}`, encoded in the event payload.
- **FR27**: The pipeline can transition between `activity` states based on observable events: wake-word detection (`sleeping → waking`), Talker greeting first audio frame (`waking → speaking`), TTS end (`speaking → listening`), VAD end-of-speech (`listening → working`), Talker fast-path or orchestrator dispatch (`working` sub-mode resolution), Talker `go_to_sleep()` tool-call (post-audio: `speaking → going_to_sleep → sleeping`).
- **FR28** *(REMOVED — v1 has no idle auto-sleep; sleep is intent-driven only via FR45.)*
- **FR29** *(DEFERRED to v1.5)*: The pipeline can transition from `speaking` to `listening` directly on barge-in detection. Status: deferred from v1.
- **FR30** *(DEFERRED to v1.5)*: The pipeline can flush in-flight `speech_emotion` and `vocalization` events on barge-in to prevent OLAF being stuck on a half-finished pose. Status: deferred from v1.

### Wake/Sleep & Talker Tool-Use

- **FR44**: The pipeline can generate a 2–8 word mood-tinted wake greeting via Talker on every `sleeping → waking` transition. Talker is invoked in **greeting mode** with the current `mood` value and a system prompt enforcing the "cool friend" register. If Talker returns >8 words, is unreachable, or returns nothing within a configurable timeout (default 800 ms), the pipeline plays a static fallback from a configured list (`["hey", "yeah?", "hi"]`) and logs a WARN.
- **FR45**: The pipeline can prompt Talker with a registered tool-set (`go_to_sleep`, `set_mood`) and accept tool-calls in Talker's response, following the active provider's tool-use protocol (OpenAI / Groq / Gemini all expose openai-compatible tool-use). Tool inputs are validated against typed Pydantic schemas before execution; invalid tool-calls log WARN and are dropped without side-effect.
- **FR46**: The pipeline can execute the `go_to_sleep()` tool-call by **scheduling** the `speaking → going_to_sleep → sleeping` transition to fire **after** the current turn's audio finishes playing. This ensures Talker's goodbye is heard in full before the mic returns to wake-word-only mode.
- **FR47**: The pipeline keeps the mic continuously open while `activity ∈ {listening, working, speaking}`, capturing user utterances without re-arming the wake-word detector. The wake-word detector is the **only** mic consumer when `activity = sleeping`.

### Mood Control

- **FR48**: The pipeline can execute the `set_mood(mood)` tool-call by validating the mood string against the v1 mood enum (`happy, playful, calm, curious, gloomy, grumpy, sleepy, excited`), publishing a `mood` event on `/olaf/mood` (latched / transient_local QoS) when the cooldown allows, and updating the in-process mood state used by future Talker prompts and the wake greeting.
- **FR49**: The pipeline enforces a configurable mood publish-rate cooldown (default: ≥15 minutes between consecutive `mood` publishes, mapping to NFR31). `set_mood` tool-calls that fire faster than the cooldown are dropped with a WARN log; the in-memory mood state is NOT updated until a publish succeeds.
- **FR50**: The pipeline retains the current `mood` value across `sleeping` periods within a single process lifetime. Mood resets to default `calm` on process restart. Cross-restart persistence is v1.5.

### Event Publishing & Channels

- **FR51**: The pipeline publishes events on four topics — `mood`, `activity`, `speech_emotion`, `vocalization` — via an `EventPublisher` Protocol. The v1 implementation publishes to ROS 2 (DDS) on `/olaf/{topic}`; a fake/log adapter exists for tests. Adding alternative channel adapters (Zenoh, NATS, WebSocket) requires no consumer-side changes.
- **FR52**: Every event on every topic carries a common envelope: `timestamp` (UTC ISO8601), `schema_version` (integer; bumped only on breaking changes per CLAUDE.md rule 6), `source` (component name string), `correlation_id` (UUID — turn-scoped for audio-anchored events, session-scoped for `mood`/`activity`), and `payload` (topic-specific Pydantic model).
- **FR53**: The event envelope's `schema_version` for this PRD direction is **3**. Bump history: `1 → 2` (Story 3.4 — single `/olaf/expression` channel replaced by four-topic publish); `2 → 3` (sprint-change-proposal-2026-05-10 — `SpeechEmotionPayload.expression_data` removed to repair the consumer-agnostic publisher boundary; embodiment vocabulary is now consumer-side, keyed on the canonical emotion name). Consumers of any prior `schema_version` must be migrated; the pipeline will not run in dual-emit mode.

### Configuration & Operations

- **FR31**: The pipeline can load `expression_map.yaml` and `pipeline.toml` at startup, validating schema and refusing to start on validation failure.
- **FR32**: The pipeline can hot-reload `expression_map.yaml` on `SIGHUP`, swapping the in-memory mapping atomically; if validation fails, it retains the prior mapping and logs the error.
- **FR33**: The pipeline can defer `SIGHUP` reloads received mid-utterance, applying them after the current turn completes.
- **FR34**: The pipeline can load credentials (Cartesia + active Talker provider's API key) from a secrets file referenced by path in `pipeline.toml`, never inlined and never logged. *(Story 2.2 revisions 2026-05-05: was "Anthropic" originally → "OpenAI" intermediate → finalised on a **provider-agnostic Talker factory** wired to OpenAI / Groq / Gemini, all reachable via the same `openai` SDK because each exposes an openai-compatible endpoint. Operator picks one provider in setup.toml; only the matching `.env` key is required at startup. v1 default is **Groq** for NFR1 latency headroom — measured ~150–270 ms per turn vs OpenAI's ~1–1.7 s on the dev host.)*
- **FR35**: The pipeline can refuse to start when configured with a non-localhost orchestrator URL without a corresponding shared-secret or mTLS configuration.
- **FR36**: The pipeline can run as a systemd service with restart-on-failure and structured logging to journald.

### Observability & Diagnostics

- **FR37**: The pipeline can emit structured (JSON) logs at INFO/WARN/ERROR levels for `activity` transitions, `mood` transitions, Talker tool-call invocations (with input + outcome), emotion fallback resolutions, config reloads, and external service failures.
- **FR38**: The pipeline can log unmapped Cartesia emotion tags with the mapped fallback (DEBUG-level on first occurrence, WARN if completely unknown).
- **FR39**: The pipeline can omit raw audio from all logs at all levels; transcripts only appear at DEBUG level, which is off by default.
- **FR40**: The pipeline can rotate logs locally with a configurable retention window (default: 7 days).
- **FR41**: The pipeline can verify Hailo-8L driver presence at startup and log a clear error before falling back to CPU inference.
- **FR42**: The pipeline does not persist user audio or transcripts to disk in the default operational path.
- **FR43**: The pipeline does not initiate any outbound network connection beyond the configured Cartesia API, the active Talker provider's API (one of OpenAI / Groq / Gemini per `[talker] provider` in setup.toml), and orchestrator daemon endpoints (no telemetry, no analytics).

## Non-Functional Requirements

> **Selective by design.** Only categories that apply to a single-user embedded voice pipeline. Scalability and accessibility (in the WCAG/Section-508 sense) are not relevant at v1 scope.

### Performance

These are the latency budgets from the Success Criteria, restated as testable NFRs (NFR1–NFR7 trace back to the Technical Success table).

- **NFR1**: Simple-turn end-to-end latency (end-of-speech → first audio frame from Cartesia) must be ≤ 1500ms at p95 over a 30-min soak.
- **NFR2**: Complex-turn end-to-end latency (end-of-speech → first narration audio frame) must be ≤ 1000ms at p95.
- **NFR3**: On-device STT latency (end-of-speech → transcript ready) must be ≤ 500ms at p95 with Hailo-8L. CPU fallback path defines its own p95.
- **NFR4**: Cartesia TTS latency (text-with-tags → first audio frame) must be ≤ 400ms at p95.
- **NFR5**: Voice / `speech_emotion` alignment must be 30–80ms anticipatory at p95; outside this window is a defect. (`mood` and `activity` events are FSM-driven and have no audio-alignment requirement; `vocalization` events are also audio-anchored under the same NFR5 window.)
- **NFR6**: Audio playback must not introduce buffering pauses > 100ms during a single utterance.
- **NFR7**: `SIGHUP`-triggered config reload must complete within 1 second from signal receipt.

### Reliability

- **NFR8**: Pipeline must run continuously for ≥ 7 days under normal household ambient conditions without an unplanned restart, panic, or unrecoverable error state.
- **NFR9**: Recovery from external service failure (Cartesia or the active Talker provider unreachable) must complete within 5 seconds of reachability return, without manual intervention.
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
- **NFR21**: ROS 2 publishing must use per-topic QoS appropriate to the event semantics: `mood` and `activity` use **transient_local** (latched), reliable, depth=1 — late subscribers receive last-known state; `speech_emotion` and `vocalization` use **volatile**, reliable, depth=10 — transient events. Lost messages on any topic are not acceptable for embodiment correctness within reliability bounds.
- **NFR22**: Active Talker provider API integration must implement graceful degradation: if the Talker API is unreachable, dispatch the turn to the orchestrator instead (slow path). Wake greetings (FR44) fall back to a static list when Talker is unreachable.

### Security

> Functional security requirements are in FR34, FR35, FR42, FR43. NFRs below are quality attributes complementing them.

- **NFR23**: All API credentials must be stored at file permission `0600` and loaded from disk only at process startup; the process must not re-read or expose them at runtime.
- **NFR24**: Outbound HTTPS connections (Cartesia and the active Talker provider) must validate TLS certificates; the pipeline must refuse to start if certificate validation is disabled.
- **NFR25**: All log output must be inspectable by Kamal locally; no log line may contain raw credential material, raw audio bytes, or (at INFO level or above) user transcripts.

### Maintainability

- **NFR26**: This PRD, the brief (`voice-agent-pipeline-brief.md`), and the distillate (`voice-agent-pipeline.md`) are the canonical specs. Any implementation decision deviating from them must update the relevant document in the same change.
- **NFR27**: Configuration schemas (`expression_map.yaml` and `pipeline.toml`) must be versioned with a `schema_version` field; the pipeline must reject configs with an incompatible schema version at startup.
- **NFR28**: Components within the pipeline (wake-word, STT, Talker, splitter, TTS, `EventPublisher` adapters) must be independently testable — each can be exercised in isolation with mock or synthetic inputs. The `EventPublisher` Protocol enables substituting a fake/log adapter for the ROS 2 adapter in tests with no consumer changes.
- **NFR29**: Logs must be machine-readable JSON to enable post-hoc analysis without manual parsing.

### User-Experience Latency (continuous-conversation direction)

- **NFR30**: Wake-greeting end-to-end latency (wake-word fired → first greeting audio frame from Cartesia) must be ≤ 1500ms at p95. The greeting Talker call, Cartesia TTS first-frame, and audio playback path together budget to NFR1 + NFR4 with no extra slack.
- **NFR31**: `mood` topic publish cadence must not exceed 4 publishes per hour sustained, enforced at the `EventPublisher` boundary (FR49). Tool-calls that would exceed the rate are dropped, not queued.
- **NFR32**: Talker tool-call decision overhead must add ≤ 100ms to the simple-turn budget at p95. Total simple-turn latency (NFR1, ≤ 1500ms) must not be exceeded as a result of tool-aware Talker invocation.
