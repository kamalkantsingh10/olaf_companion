# Component Brief: voice-agent-pipeline

**Parent project:** OLAF Companion (Personal Voice Agent)
**Status:** Design phase
**Author:** Kamal
**Last updated:** 2026-05-03
**Audience:** LLM coding partner (Claude Code) implementing the component

---

## Executive Summary

`voice-agent-pipeline` is the Pipecat-based service that owns OLAF Companion's voice loop and embodiment surface. It captures user speech, dispatches turns to a separate orchestrator (a Claude Code session) for reasoning, generates spoken responses with Cartesia Sonic-3, and drives OLAF's physical expression in sync with that voice — all while keeping a strict separation from the components that reason or hold belief state.

The architecture is shaped by one performance constraint and one correctness constraint. **Performance:** a single-LLM voice loop forces the user to wait for the entire reasoning cycle before any audio comes back; that's seconds of dead air on every multi-step turn. The pipeline answers this with a Talker fast-path inside Pipecat — simple turns are spoken directly from belief state while the orchestrator's deeper work runs in the background. **Correctness:** voice and embodiment must stay synced, always. The pipeline answers this with a single fan-out point — the tag splitter — that emits both Cartesia text and OLAF expression events from the same parsed segment, anchored to the same audio frames.

This component is the **only** part of OLAF that touches audio hardware, and the **only** publisher to OLAF's expression channel. It deliberately does not reason, does not call MCP tools, and does not write belief state. That narrow scope is the brief's most important contract — when in doubt, the pipeline does less.

## The Problem

A naive personal voice agent has three failure modes that this design exists to prevent:

1. **Dead air on complex turns.** A single LLM call doing both reasoning and voice generation makes the user wait for the entire reasoning cycle — multi-second silences after every "what's on my calendar?" or "summarize this thread." For a companion robot in your home, that silence is the difference between "alive" and "broken."
2. **Drift between voice and embodiment.** If voice and OLAF expression travel separate channels (one stream to TTS, a parallel stream to ROS 2), they desynchronize. OLAF ends up smiling on a sad sentence, or holding a laugh pose two seconds after the laugh ended. There is no tolerable amount of drift here.
3. **Cloud-dependent transcription.** Routing every utterance through a cloud STT provider adds a round-trip, a privacy footprint, and a cost line — none of which are acceptable for a local personal agent.

## The Solution

Pipecat owns the voice loop; the orchestrator (a separate Claude Code session reachable over HTTP/WebSocket) owns reasoning. The pipeline's responsibilities are narrow and concrete:

- **Audio I/O** — mic capture, speaker playback, local devices or WebRTC transport.
- **Wake-word detection** — always-on, low-power, on-device.
- **On-device STT** — Whisper (or equivalent) with Hailo-8L acceleration where viable. No cloud dependency.
- **Talker fast-path** — in-pipeline LLM call (e.g. `claude-haiku-4-5`) that reads belief state via the daemon's API and answers simple turns immediately.
- **Turn dispatch** — send transcripts to the orchestrator, receive a typed event stream (narration, subagent_progress, response_chunk, turn_end).
- **Tag splitter** — streaming SSML parser; segment on emotion boundaries or sentence terminators; map Cartesia tags to OlafAction events.
- **Cartesia TTS** — stream audio frames back into Pipecat with emotional prosody.
- **Expression publisher** — emit OlafAction events to ROS 2 (`/olaf/expression`), anchored to audio frames so voice and pose align.
- **Lifecycle signaling** — publish OLAF lifecycle state (SLEEPING, LISTENING, THINKING, SPEAKING, IDLE).

## What Makes This Different

The five architectural decisions that an LLM implementing this component must not violate:

1. **Single fan-out point.** The tag splitter is the *only* place text and expression events diverge. They come from the same parsed segment. There is no separate parallel channel for OLAF expression — that prevents drift by construction.
2. **Single-writer belief state.** Only the orchestrator writes belief state. The Talker reads via the daemon API, never directly. No race conditions.
3. **Audio-frame anchoring.** Expression events ride the audio frame they correspond to and are published when that frame is sent, not when the tag is parsed. Target alignment: ~30-80ms anticipatory (slightly ahead of voice feels best on prototypes).
4. **Mapping is data, not code.** `expression_map.yaml` is loaded at startup, reloadable on `SIGHUP`. Adding a new emotion or burst is a config change.
5. **Talker lives inside the pipeline, not the orchestrator.** Decided. Avoids designing the orchestrator around fast-path/slow-path branching; keeps the daemon's API uniform.

## Stakeholders & Consumers

This is a personal project, so "users" in the commercial sense means one person (Kamal). The components that consume this contract are the more important audience:

- **Kamal** — primary user and dev. Success means a companion that feels live, not laggy.
- **Orchestrator daemon (Claude Code session)** — consumes `POST /turn` requests with lifecycle context; produces the typed event stream.
- **OLAF embodiment renderer** — consumes `OlafAction` events on `/olaf/expression` (ROS 2). Decides interpolation and ease curves itself; the pipeline only states target poses.
- **Future motion controller** — will consume lifecycle states for non-voice-driven behaviors (idle sway, listening indicators, sleep transitions).

## Success Criteria

A turn feels alive when these hold:

- **No perceptible dead air on simple turns.** Talker fast-path serves them without daemon round-trip latency. Target: first audio frame within **TBD ms** of end-of-speech.
- **Tolerable latency on complex turns.** First narration audio plays within **TBD ms** of end-of-speech, even when subagents are still working in the background.
- **Voice/embodiment alignment.** OLAF expression visibly matches voice. ~30-80ms anticipatory window — drift outside this is a bug.
- **Robust unmapped-tag handling.** Any Cartesia tag the LLM might emit produces a defined OLAF behavior via the fallback table. Truly unknown tags fall through to neutral with a logged warning. No silent gaps.
- **Wake-word reliability.** False-positive rate **TBD per hour** of normal ambient background; false-negative rate acceptable for natural conversation start.
- **On-device STT latency.** Whisper + Hailo-8L on Pi delivers transcripts within **TBD ms** of end-of-speech. Acceptable to live conversation, not a noticeable bottleneck.

> **TBD numbers** to be filled once Phase 0/1 prototypes give measurable baselines.

## Scope

**In scope (v1, Phases 0-3):**

- Audio I/O, wake-word, on-device STT, Cartesia TTS, ROS 2 expression, lifecycle signaling
- Talker fast-path (in-pipeline LLM)
- HTTP/WebSocket stream contract with orchestrator (Claude Code session) — `narration`, `subagent_started`, `subagent_progress`, `subagent_done`, `response_chunk`, `turn_end`
- Tag splitter — streaming SSML parser, primary + secondary emotion mapping, full fallback table coverage for the rest of Cartesia's vocab
- Configuration: `expression_map.yaml` + `pipeline.toml`

**Out of scope (v1):**

- Tertiary emotion mappings (flirtatious, mysterious, sarcastic) — fall back to nearest primary
- Telephony / SIP transport — local audio first, WebRTC next
- Multiple concurrent user sessions — single-user
- On-device TTS — Cartesia cloud only
- Avatar / screen rendering — OLAF embodiment is the only renderer
- Emotion intensity scaling — Cartesia doesn't expose it for sustained emotions; treat as binary
- User expression reading (OAK-D camera) — separate component, not part of this pipeline
- Direct motion control — expression and lifecycle channels only

## Phasing

| Phase | Goal | Validation |
|-------|------|------------|
| 0 | Bare voice loop: Pipecat + on-device STT + Cartesia TTS, push-to-talk, hardcoded prompt | Audio I/O, STT latency on Pi, transport |
| 1 | Wake-word + daemon dispatch + tag splitter; OLAF still mocked (stdout) | Streaming parser correctness, segment-on-emotion logic |
| 2 | Real OLAF expression via `Ros2OlafInterface` | Audio-anchored timing, ROS 2 message arrival |
| 3 | Lifecycle signaling + full fallback coverage; SLEEPING ↔ LISTENING via wake-word | LLM emits unusual emotions; pipeline doesn't break |

## Decisions & Open Questions

**Decided:**

- Talker lives inside Pipecat (its own LLM call), not in the orchestrator.
- OLAF renderer handles burst pose interpolation. Pipeline says "go to laughter, hold 1500ms, return to base"; renderer chooses the curves.
- When Cartesia silently drops an emotion tag (because the text doesn't match the emotion), OLAF still renders the LLM's emotional intent. The orchestrator's job is to keep tags consistent with text.
- Pipeline never publishes non-voice-driven OLAF events. Idle behaviors and motion are handled by other components.

**Still open (work out empirically):**

- Barge-in handling. When the user interrupts mid-response, what's the right transition? Working assumption: SPEAKING → LISTENING directly, with the splitter flushing in-flight expression events so OLAF doesn't get stuck on a half-finished state. Validate during Phase 2.

## Vision

Phase 3 is the v1 finish line. Beyond v1, the design intentionally leaves room for:

- Tertiary emotion mappings for full Cartesia vocabulary (v2)
- Intensity scaling once Cartesia exposes it
- Telephony / SIP transport for remote conversations
- On-device TTS when a model that meets the quality bar runs on Pi
- OAK-D camera input feeding user-expression signals back to the orchestrator (separate component, but the pipeline's narrow scope must survive the addition)

The component is meant to be **stable, narrow, and replaceable**. The contracts that must survive any future rewrite are the `POST /turn` request/response shape with the orchestrator, the OlafAction event shape on ROS 2, and the `expression_map.yaml` schema. Everything else is implementation.
