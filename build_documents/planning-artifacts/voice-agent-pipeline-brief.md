# Component Brief: voice-agent-pipeline

**Parent project:** OLAF Companion (Personal Voice Agent)
**Status:** Design phase
**Author:** Kamal
**Last updated:** 2026-05-06
**Audience:** LLM coding partner (Claude Code) implementing the component

---

## Executive Summary

`voice-agent-pipeline` is the Pipecat-based service that owns OLAF Companion's voice loop and embodiment surface. It captures user speech, dispatches turns to a separate orchestrator (a Claude Code session) for reasoning, generates spoken responses with Cartesia Sonic-3, and drives OLAF's expressive surface in sync with that voice via four typed ROS 2 event topics — all while keeping strict separation from the components that reason or hold belief state.

The architecture is shaped by one performance constraint and one correctness constraint. **Performance:** a single-LLM voice loop forces the user to wait for the entire reasoning cycle before any audio comes back; that's seconds of dead air on every multi-step turn. The pipeline answers this with a Talker fast-path inside Pipecat — simple turns are spoken directly from belief state while the orchestrator's deeper work runs in the background. **Correctness:** voice and the audio-anchored embodiment events must stay synced, always. The pipeline answers this with a single fan-out point — the tag splitter — that emits Cartesia text plus `speech_emotion` and `vocalization` events from the same parsed segment, anchored to the same audio frames.

The component is **conversation-shaped**, not turn-shaped. The wake word transitions OLAF from `SLEEPING` to `AWAKE`; once awake, user speech flows continuously without re-arming. Sleep is intent-driven — the Talker LLM detects when the user has signalled "we're done" and fires a `go_to_sleep()` tool-call. There is no idle auto-sleep. On every wake, Talker generates a 2–8 word mood-tinted greeting (a "cool friend" register, not a scripted "Hello"); during conversation, Talker can update OLAF's mood via a `set_mood()` tool. These behaviours surface on the four event topics — `mood` (slow-changing disposition), `activity` (FSM transitions), `speech_emotion` (per-segment Cartesia tags), `vocalization` (punctual non-verbals like `[laugh]` and `[sigh]`).

This component is the **only** part of OLAF that touches audio hardware, and the **only** publisher to OLAF's four event topics. It deliberately does not reason, does not call MCP tools, and does not write belief state. That narrow scope is the brief's most important contract — when in doubt, the pipeline does less.

## The Problem

A naive personal voice agent has four failure modes that this design exists to prevent:

1. **Dead air on complex turns.** A single LLM call doing both reasoning and voice generation makes the user wait for the entire reasoning cycle — multi-second silences after every "what's on my calendar?" or "summarize this thread." For a companion robot in your home, that silence is the difference between "alive" and "broken."
2. **Drift between voice and embodiment.** If voice and OLAF expression travel separate channels, they desynchronize. OLAF ends up smiling on a sad sentence, or holding a laugh pose two seconds after the laugh ended. There is no tolerable amount of drift here.
3. **Cloud-dependent transcription.** Routing every utterance through a cloud STT provider adds a round-trip, a privacy footprint, and a cost line — none of which are acceptable for a local personal agent.
4. **Mechanical conversation feel.** Re-saying the wake word for every follow-up turn, hearing a scripted "Hello, I am OLAF" on each wake, watching mood snap turn-to-turn instead of drifting like a real person — these tells make the user aware they're talking to software. The pipeline answers this with continuous conversation while AWAKE, a 2–8 word mood-tinted greeting on each wake, intent-based sleep, and a slow-changing mood model.

## The Solution

Pipecat owns the voice loop; the orchestrator (a separate Claude Code session reachable over HTTP/WebSocket) owns reasoning. The pipeline's responsibilities are narrow and concrete:

- **Audio I/O** — mic capture, speaker playback, local devices or WebRTC transport.
- **Wake-word detection** — always-on, low-power, on-device. Active **only** while the activity FSM is in `sleeping`; gates the `sleeping → waking` transition. Mic continues to capture continuously while AWAKE without re-arming the wake word.
- **On-device STT** — Whisper (or equivalent) with Hailo-8L acceleration where viable. No cloud dependency.
- **Talker (tool-using fast-path)** — in-pipeline LLM call routed through a provider-agnostic factory (OpenAI / Groq / Gemini, all via the openai-compatible API surface). Reads belief state via the daemon's API and answers simple turns immediately. Operates in two modes: **conversational** (spoken reply, may emit tool-calls in parallel) and **greeting** (2–8 word mood-tinted wake greeting). v1 tool-set: `go_to_sleep()` and `set_mood(mood)`.
- **Turn dispatch** — send transcripts to the orchestrator, receive a typed event stream (narration, subagent_progress, response_chunk, turn_end).
- **Tag splitter** — streaming SSML parser; segment on emotion / vocalization / sentence boundaries; emit `speech_emotion` and `vocalization` events anchored to audio frames.
- **Cartesia TTS** — stream audio frames back into Pipecat with emotional prosody. Vocalization tags (`[laugh]`, `[sigh]`, …) are LLM-emitted inline; Cartesia receives those it supports and silently ignores the rest, while the pipeline still publishes them.
- **Event publisher** — publish on four typed ROS 2 topics: `mood` (latched, slow-cadence), `activity` (FSM transitions including `working` sub-modes), `speech_emotion` (per-segment, audio-anchored), `vocalization` (punctual, audio-anchored). Behind a Protocol-based publisher, so a fake/log adapter is available for tests.
- **Activity FSM** — transitions on observable events: wake-word, end-of-speech, first/last audio frame, Talker `go_to_sleep()` tool-call. State set: `starting, sleeping, waking, listening, working, speaking, going_to_sleep`. The `working` state has v1 sub-modes `thinking` (Talker reasoning in-pipeline) and `delegating` (orchestrator dispatched, awaiting response).
- **Mood control** — discrete enum (~6–8 states); slow-cadence (≤4 publishes/hour, enforced at the publisher boundary); Talker fires `set_mood(mood)` when conversation context warrants a shift.

## What Makes This Different

The six architectural decisions that an LLM implementing this component must not violate:

1. **Talker fast-path inside Pipecat, not in the orchestrator.** Simple turns are answered from belief state in-pipeline without waiting for the orchestrator's deeper reasoning. This eliminates the dead air that single-LLM voice agents force on every multi-step turn. Avoids designing the orchestrator around fast-path/slow-path branching; keeps the daemon's API uniform.
2. **Single fan-out for audio-anchored events.** The tag splitter is the *only* place text, `speech_emotion`, and `vocalization` events diverge. All three come from the same parsed segment, anchored to the same audio frames — no parallel channel, drift prevented by construction. Target alignment: ~30–80ms anticipatory. (`mood` and `activity` events are FSM-driven and publish on transition; they are not audio-anchored. Single-writer belief state — only the orchestrator writes; Talker reads via the daemon API — is a related correctness invariant that lives under this decision.)
3. **On-device STT (Whisper + Hailo-8L).** Speech transcription runs locally on the Pi. Privacy and latency benefit from the same decision; cloud STT is explicitly excluded for v1.
4. **Mapping is data, not code — and the data is a *taxonomy*, not renderer hints.** `expression_map.yaml` is loaded at startup, reloadable on `SIGHUP`. Adding a new emotion or burst is a config change (a one-token list append for first-class emotions; a new family member for fallback Cartesia tags). The file carries a *vocabulary* — canonical first-class names plus the Cartesia-tag fallback families — and nothing about pose, LED, eye state, or any other embodiment vocabulary; that side lives in the consumer's own config keyed on the canonical emotion name. The `speech_emotion` event payload carries both the raw tag and the resolved fallback so consumers know what was asked AND what was rendered. (Pre-schema-3 the file also carried per-emotion `expression_data:` blocks that shipped on the wire; the schema-3 boundary repair removed them — see architecture.md §"What left the wire on schema_version=3".)
5. **Continuous conversation; intent-based sleep.** Wake-word fires only on the `sleeping → waking` transition. While AWAKE, the mic stays open and turns flow without re-prompting. The Talker LLM detects sleep intent in natural language and fires a `go_to_sleep()` tool — no exact-phrase match, no idle timer. On every wake, Talker generates a 2–8 word mood-tinted greeting in greeting-mode invocation.
6. **Multi-topic event publish with a common envelope.** Pipeline publishes on four typed ROS 2 topics — `mood`, `activity`, `speech_emotion`, `vocalization` — every event carrying a common envelope `{timestamp, schema_version, source, correlation_id, payload}`. The publisher is Protocol-based; ROS 2 is the v1 implementation, with a fake/log adapter for tests. Adding alternative channel adapters (Zenoh, NATS, WebSocket) requires no consumer-side changes. Schema version is currently **3**. Bump history: 1 → 2 (topology change, single `/olaf/expression` channel split into four topics, Story 3.4); 2 → 3 (boundary repair, `SpeechEmotionPayload.expression_data` removed, sprint-change-proposal-2026-05-10).

## Stakeholders & Consumers

This is a personal project, so "users" in the commercial sense means one person (Kamal). The components that consume this contract are the more important audience:

- **Kamal** — primary user and dev. Success means a companion that feels live, not laggy.
- **Orchestrator daemon (Claude Code session)** — consumes `POST /turn` requests with activity context; produces the typed event stream.
- **OLAF embodiment renderer** — subscribes to the four ROS 2 topics (`/olaf/mood`, `/olaf/activity`, `/olaf/speech_emotion`, `/olaf/vocalization`). Decides interpolation, ease curves, and ambient/burst layering itself; the pipeline only states target poses, mood, activity, and tag events.
- **Future motion controller** — will consume `activity` for non-voice-driven behaviors (idle sway, listening indicators, sleep transitions) and `mood` for ambient base shifts.
- **Future dashboard / telemetry consumers** — same four topics; the Protocol-based publisher means adding consumers (or alternative channel adapters like a WebSocket bridge) doesn't require pipeline changes.

## Success Criteria

A conversation feels alive when these hold:

- **Mood-tinted greeting on every wake.** 2–8 words, "cool friend" register; first greeting audio frame within **≤ 1500ms** of wake-word fire (NFR30).
- **Continuous-conversation feel.** After waking, follow-up turns flow without re-saying the wake word. Mic stays open while AWAKE.
- **No perceptible dead air on simple turns.** Talker fast-path serves them without daemon round-trip latency. Simple-turn end-of-speech → first audio frame: **≤ 1500ms** at p95 (NFR1).
- **Tolerable latency on complex turns.** First narration audio plays within **≤ 1000ms** of end-of-speech (NFR2), even when subagents are still working in the background.
- **Intent-based sleep.** Talker detects natural-language goodbye and fires `go_to_sleep()`; OLAF returns to sleep after the acknowledgement audio finishes. No idle auto-sleep.
- **Coherent mood across the conversation.** Mood publishes are slow (≤ 4 / hour, NFR31); mood doesn't flicker turn-to-turn.
- **Voice / `speech_emotion` alignment.** Audio-anchored expression visibly matches voice within a 30–80ms anticipatory window (NFR5) — drift outside this is a bug. (`mood` and `activity` are FSM-driven, no audio-alignment requirement.)
- **Robust unmapped-tag handling.** The `speech_emotion` event payload carries both the raw Cartesia tag and a resolved fallback; consumers can use either. No silent gaps.
- **Wake-word reliability.** False-positive rate ≤ **1 per hour** of normal ambient background (NFR12); false-negative rate ≤ **5%** in normal speaking conditions (NFR13).
- **On-device STT latency.** Whisper + Hailo-8L on Pi delivers transcripts within **≤ 500ms** of end-of-speech (NFR3).

> Numbers above are the v1 commitments; full p95 / observation-window context lives in `prd.md` (NFRs 1–32). Brief numbers were locked in the 2026-05-06 PRD edit, replacing the prior "TBD per phase 0/1 prototypes" placeholders.

## Scope

**In scope (v1, Phases 0-3):**

- Audio I/O, wake-word, on-device STT, Cartesia TTS, four-topic ROS 2 publish (mood / activity / speech_emotion / vocalization), activity FSM signaling
- Continuous mic capture while AWAKE (no per-turn wake word)
- Talker fast-path (in-pipeline LLM, provider-agnostic factory across OpenAI / Groq / Gemini)
- Talker tool-using — `go_to_sleep()` and `set_mood(mood)` tool registry, validated against typed Pydantic input schemas
- Mood-tinted wake greeting (2–8 word "cool friend" register, mood-driven, with static fallback)
- Mood model (~6–8 discrete states; slow-cadence publish; in-process persistence across SLEEPING within a single process lifetime)
- Common event envelope — `timestamp`, `schema_version` (currently 3), `source`, `correlation_id`, `payload`
- HTTP/WebSocket stream contract with orchestrator (Claude Code session) — `narration`, `subagent_started`, `subagent_progress`, `subagent_done`, `response_chunk`, `turn_end`
- Tag splitter — streaming SSML parser, primary + secondary emotion mapping, vocalization tag parsing (`[laugh]`, `[sigh]`, …), full fallback table coverage for the rest of Cartesia's vocab; `speech_emotion` payload carries both raw and resolved tags
- Configuration: `expression_map.yaml` + `pipeline.toml`

**Deferred to v1.5:**

- **Barge-in handling** — mid-utterance interruption. Was on the v1 list as "Open (work out empirically)"; this PRD edit (2026-05-06) formally deferred it to v1.5. v1 ships without barge-in; user waits for OLAF to finish before speaking.
- **Expanded `working` sub-modes** — `searching`, `tooling`, `composing` for richer Olaf animations.
- **Cross-restart mood persistence** — v1 retains mood within a single process lifetime only; cross-restart persistence is v1.5.
- **Configurable idle auto-sleep** — disabled by default in v1; available as v1.5 opt-in.

**Out of scope (v1):**

- Tertiary emotion mappings (flirtatious, mysterious, sarcastic) — fall back to nearest primary
- Telephony / SIP transport — local audio first, WebRTC next
- Multiple concurrent user sessions — single-user
- On-device TTS — Cartesia cloud only
- Avatar / screen rendering — OLAF embodiment is the only renderer
- Emotion intensity scaling — Cartesia doesn't expose it for sustained emotions; treat as binary
- User expression reading (OAK-D camera) — separate component, not part of this pipeline
- Direct motion control — pipeline publishes only on the four event topics

## Phasing

| Phase | Goal | Validation |
|-------|------|------------|
| 0 | Bare voice loop: Pipecat + on-device STT + Cartesia TTS, push-to-talk, hardcoded prompt | Audio I/O, STT latency on Pi, transport |
| 1 | Wake-word + daemon dispatch + tag splitter + Talker tool-emit + greeting mode; OLAF still mocked (log adapter) | Streaming parser correctness, segment-on-emotion-or-vocalization logic, Talker tool-call FP/FN baseline |
| 2 | Four-topic ROS 2 publish via `Ros2EventPublisher`; replace log adapter | Audio-anchored timing for `speech_emotion` + `vocalization`, `mood` + `activity` latched semantics, ROS 2 message arrival across all four topics |
| 3 | Activity FSM full coverage (continuous-conversation + intent-sleep + wake greeting + mood lifecycle); soak | LLM emits unusual emotions, sleep-intent FP/FN within target, mood cadence within NFR31, no flicker |

## Decisions & Open Questions

**Decided:**

- Talker lives inside Pipecat (its own LLM call), not in the orchestrator.
- Talker is **tool-using** in v1. Tool registry: `go_to_sleep()` and `set_mood(mood)`. Tool inputs validated against typed Pydantic schemas before execution; invalid calls log WARN and are dropped without side effects.
- **Continuous conversation while AWAKE.** Wake-word gates `sleeping → waking` only; subsequent turns flow without re-arming. The mic is the single audio stream consumer in either mode (wake-word listener while sleeping; VAD + STT while AWAKE) — there is no parallel-listener architecture.
- **Sleep is intent-driven**, not regex-driven and not idle-timer-driven. Talker decides; the pipeline executes the transition after the acknowledgement audio finishes (deferred sleep) so the goodbye is heard in full before the mic returns to wake-word-only mode.
- **Wake greeting** is generated by Talker in greeting mode on every wake-word fire, tinted by current mood. Static fallback list for unreachable Talker / overlong responses.
- **Four-topic event publish.** `mood`, `activity`, `speech_emotion`, `vocalization`. Common envelope. ROS 2 is the v1 channel adapter; the publisher is Protocol-based for future Zenoh / NATS / WebSocket adapters with no consumer-side changes.
- **`speech_emotion` schema is open-set.** Any Cartesia tag string is accepted; the payload carries `raw_tag + resolved_fallback`. Cartesia silently drops tags it doesn't recognise on the voice side; the pipeline publishes them anyway so OLAF can use either.
- **Vocalization source: LLM-emitted inline tags** (`[laugh]`, `[sigh]`, `[gasp]`, …), parsed pre-TTS. Cartesia receives the tags it supports and ignores the rest; the pipeline publishes all of them as `vocalization` events anchored to audio frames.
- OLAF renderer handles burst pose interpolation. Pipeline says "go to laughter, hold 1500ms, return to base"; renderer chooses the curves.
- When Cartesia silently drops an emotion tag (because the text doesn't match the emotion), OLAF still renders the LLM's emotional intent via `speech_emotion`. The orchestrator's job is to keep tags consistent with text.
- Pipeline never publishes non-voice-driven OLAF events. Idle behaviors and motion are handled by other components.
- **Barge-in deferred to v1.5.** Was on the previous "still open" list. The 2026-05-06 PRD edit moved it formally to v1.5 backlog. v1 ships without barge-in; user waits for OLAF to finish before speaking. If Phase 3 soak reveals response lengths feel constrained, re-prioritize.

**Still open (work out empirically):**

- **Sleep-intent prompt tuning.** Talker false-positive `go_to_sleep` (ends real conversation) and false-negative (misses goodbye) rates are emergent from prompt design. Track FP/FN as part of the Phase 3 30-min soak pass criteria; tune the system prompt iteratively.
- **Mood enum stability.** The v1 enum (`happy, playful, calm, curious, gloomy, grumpy, sleepy, excited`) is a starting point; soak may reveal states that should be added or pruned. Cooldown enforcement is at the publisher boundary (NFR31), so adding states is data-only.
- **Wake-greeting fallback list.** Currently named in FR44 as `["hey", "yeah?", "hi"]`. May want to make this a `pipeline.toml` knob if longer/different fallbacks are wanted; v1 ships hardcoded.

## Vision

Phase 3 is the v1 finish line. Beyond v1, the design intentionally leaves room for:

- **v1.5:** Barge-in (deferred from v1), expanded `working` sub-modes (`searching`, `tooling`, `composing`), cross-restart mood persistence, configurable idle auto-sleep
- Tertiary emotion mappings for full Cartesia vocabulary (v2)
- Intensity scaling once Cartesia exposes it
- Telephony / SIP transport for remote conversations
- On-device TTS when a model that meets the quality bar runs on Pi
- Alternative channel adapters (Zenoh, NATS, WebSocket bridge) — Protocol-based publisher means consumers don't change
- OAK-D camera input feeding user-expression signals back to the orchestrator (separate component, but the pipeline's narrow scope must survive the addition)

The component is meant to be **stable, narrow, and replaceable**. The contracts that must survive any future rewrite are:

- The `POST /turn` request/response shape with the orchestrator
- The four typed event schemas on ROS 2 — `mood`, `activity`, `speech_emotion`, `vocalization` — with their common envelope and current `schema_version=3`
- The `expression_map.yaml` schema (mapping table + fallback families)
- The Talker tool registry contract (`go_to_sleep`, `set_mood`) — name + input schema, not implementation

Everything else is implementation.
