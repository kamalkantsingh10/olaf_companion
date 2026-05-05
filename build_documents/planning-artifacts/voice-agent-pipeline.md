# voice-agent-pipeline — Canonical Component Spec

**Parent project:** OLAF Companion (Personal Voice Agent)
**Status:** Design phase
**Author:** Kamal
**Last updated:** 2026-05-03
**Audience:** LLM coding partner (Claude Code) implementing the component

> One-file canonical reference. Brief framing on top, full contracts and configuration below. Point Claude Code at this file when implementing.

---

## 1. Summary

`voice-agent-pipeline` is the Pipecat-based service that owns OLAF Companion's voice loop and embodiment surface: speech in, spoken response out, OLAF physical expression in sync. It is the **only** component touching audio hardware and the **only** publisher to OLAF's expression channel. It does not reason, does not call MCP tools, does not write belief state — that's the orchestrator (a separate Claude Code session). This component is the voice and embodiment surface.

The architecture is shaped by two constraints:

- **Performance.** A single-LLM voice loop forces the user to wait for the full reasoning cycle before any audio comes back — seconds of dead air on multi-step turns. The Talker fast-path inside Pipecat answers simple turns directly from belief state while the orchestrator's deeper work runs in the background.
- **Correctness.** Voice and embodiment must stay synced. The tag splitter is a **single fan-out point** that emits both Cartesia text and OLAF expression events from the same parsed segment, anchored to the same audio frames. There is no parallel channel for OLAF expression — that prevents drift by construction.

## 2. Hard Architectural Constraints

These are the decisions an LLM implementing this component **must not violate**. Everything else is implementation choice.

1. **Single fan-out point.** The tag splitter is the only place text and expression diverge. Both come from the same parsed segment.
2. **Single-writer belief state.** Only the orchestrator writes belief state. The Talker reads via the daemon API, never directly.
3. **Audio-frame anchoring.** Expression events ride the audio frame they correspond to and are published when that frame is sent — not when the tag is parsed. Target alignment: ~30-80ms anticipatory.
4. **Mapping is data, not code.** `expression_map.yaml` is loaded at startup, reloadable on `SIGHUP`. Adding emotions or bursts is a config change.
5. **Talker lives inside Pipecat**, not the orchestrator. Decided.
6. **Pipeline never publishes non-voice-driven OLAF events.** Idle behaviors and motion are owned by other components.

## 3. Responsibilities

- **Audio I/O** — mic capture, speaker playback, local audio devices or WebRTC transport.
- **Wake-word detection** — always-on, low-power, on-device. Wakes the full pipeline only on detection.
- **STT (on-device)** — local Whisper or equivalent on the Pi, accelerated via Hailo-8L where viable. No cloud dependency.
- **Turn dispatch** — send user transcripts to the orchestrator daemon over HTTP/WebSocket; receive structured response stream.
- **Talker (fast-path replies)** — for simple turns, generate spoken reply directly from belief state without waiting for the daemon's deeper reasoning.
- **Tag splitter** — parse the response stream for Cartesia emotion tags and bursts; segment text on emotion boundaries; build per-segment events.
- **TTS (Cartesia, cloud)** — feed text-with-tags to Cartesia Sonic-3, stream audio frames back into Pipecat.
- **Expression publisher** — publish OLAF expression events to ROS 2 (`/olaf/expression`), anchored to the corresponding audio frames.
- **Lifecycle signaling** — publish OLAF lifecycle state changes (SLEEPING, LISTENING, THINKING, SPEAKING, IDLE) so the bot stays in sync with the conversation.

## 4. Non-Responsibilities

- Reasoning, planning, multi-step work — orchestrator's job.
- Tool / MCP invocation — orchestrator's specialists.
- Belief state writes — orchestrator only; pipeline reads via daemon API.
- Direct OLAF motion control — only expression and lifecycle channels; motion is owned by the orchestrator's home subagent or a dedicated motion controller.

## 5. Stakeholders & Consumers

- **Kamal** — primary user and dev.
- **Orchestrator daemon (Claude Code session)** — consumes `POST /turn` requests, produces typed event stream.
- **OLAF embodiment renderer** — consumes `OlafAction` events on `/olaf/expression` (ROS 2). Decides interpolation and ease curves itself.
- **Future motion controller** — consumes lifecycle states for non-voice-driven behaviors (idle sway, listening indicators, sleep transitions).

## 6. Architecture

```
                         wake trigger
                              ↓
  Mic (always on) → Wake-word detector → VAD → On-device STT (Whisper + Hailo-8L)
                                                          ↓
                                                  [user transcript]
                                                          ↓
                                ┌─────────────────────────┴──────────────────────────┐
                                ↓                                                    ↓
                         Talker (fast)                                  Orchestrator daemon
                         reads belief                                   HTTP /turn (Claude Code)
                         state via API                                  → typed event stream
                                ↓                                                    ↓
                                └────────────────┬───────────────────────────────────┘
                                                 ↓
                                        Tag splitter (streaming SSML parser)
                                        — segment on emotion change OR sentence
                                        — map Cartesia tag → OlafAction
                                        — anchor expression to audio frame
                                                 ↓
                                ┌────────────────┴────────────────┐
                                ↓                                 ↓
                          Cartesia TTS                   ROS 2 publisher
                          (cloud, Sonic-3)               via OlafInterface
                                ↓                                 ↓
                          Audio frames + anchored OlafAction      OLAF nodes
                                ↓
                            Speaker
```

**Why this shape:**

- The splitter is the single fan-out. Text+SSML to Cartesia, structured `OlafAction` to OLAF — both from the same parsed segment, inherently in sync.
- Talker reads belief state via the daemon's API, never directly. Belief state stays single-writer; pipeline is read-only consumer.
- Expression events are anchored to audio frames, not fired on tag parse. The audio frame carries the matching expression event through Pipecat's frame pipeline; the transport processor publishes both at once. Result: ~30-80ms anticipatory alignment.

## 7. Stream Contract with Orchestrator Daemon

Pipecat sends user transcripts to `POST /turn` and receives a stream of typed events. Contract is intentionally narrow.

**Request:**

```json
{
  "session_id": "abc123",
  "user_text": "what's on my calendar today?",
  "context": { "lifecycle_state": "LISTENING" }
}
```

**Response (Server-Sent Events / WebSocket):**

```json
{ "type": "narration",         "text": "Let me check..." }
{ "type": "subagent_started",  "name": "comms" }
{ "type": "subagent_progress", "name": "comms", "msg": "Reading calendar" }
{ "type": "subagent_done",     "name": "comms" }
{ "type": "response_chunk",    "text": "<emotion value=\"content\"/> You've got " }
{ "type": "response_chunk",    "text": "two meetings today — one at 10..." }
{ "type": "turn_end" }
```

**Pipeline behavior:**

- `narration` and `response_chunk` → splitter (segment, fan out to TTS + OLAF).
- `subagent_*` → lifecycle state hints (subtle "working" indicator on OLAF).
- `turn_end` → close audio stream, transition OLAF to IDLE.

## 8. Tag Splitter — Implementation Requirements

The most algorithmically interesting piece. Requirements:

- **Streaming, not batched.** Token-by-token parsing. Buffer until either a complete tag is recognized or a clear non-tag character is seen.
- **Tags can split across token boundaries.** Standard incremental SSML parser pattern — small state machine, ~50 lines.
- **Segment on whichever comes first:** sentence terminator (`.?!`), emotion tag boundary, or burst tag.
- **Strip Cartesia-unsupported bursts from the TTS stream**, keep them on the OLAF stream. Lookup table for what Cartesia v1 supports.
- **Anchor expression events to audio frames, not text frames.** The Cartesia TTS processor produces audio frames; the splitter attaches the matching `OlafAction` event metadata; the transport processor publishes the OLAF event at the moment the audio frame is sent to the user.
- **Maintain a "last published" cache for OLAF base emotion.** Don't republish the same base if it hasn't changed (saves bandwidth, lets OLAF's renderer hold pose). Bursts always publish.

## 9. Cartesia Tag → OLAF Action Mapping

The mapping is **the core contract** for embodiment. Every Cartesia tag the LLM might emit has a defined OLAF behavior. The mapping table is the canonical source of truth — if a tag isn't here, OLAF doesn't know how to express it.

### 9.1 Mapping Principles

- Every Cartesia tag has a default behavior. **No silent gaps.** Tags not explicitly mapped fall back to a defined "nearest neighbor" emotion (see §9.4).
- OLAF expresses the **intent** of the tag, not the literal voice prosody. Cartesia changes pitch and pace; OLAF changes pose, eye state, and LED color. Two renderers, same emotional intent.
- Mapping is data, not code. Lives in `expression_map.yaml`, loaded at startup. Editable without code changes.
- **Bursts layer over base.** A burst (`[laughter]`) sets a transient overlay; the base emotion underneath persists. Renderer logic: `burst || base`.
- **Transitions are renderer-side.** Pipeline publishes target states; the OLAF renderer interpolates between them with appropriate ease curves. Pipeline doesn't manage transition timing — it just says "go to this state."

### 9.2 Sustained Emotional States — `<emotion value="X"/>`

Cartesia ships 60+ emotion values. The primary set with the most training data is `neutral, angry, excited, content, sad, scared`. v1 implements all six primaries with full OLAF expression; the rest fall back to the nearest primary (or are mapped lightly via §9.2.2).

#### 9.2.1 Primary tier (v1)

| Cartesia tag | OLAF base pose | Eye display | LED ring | Notes |
|---|---|---|---|---|
| neutral | Centered, slight forward lean | Open, relaxed | Soft white | Default state |
| content | Slight nod, slow sway | Soft, partial blink | Warm amber | Calm, settled |
| excited | Forward lean, head up | Wide, bright | Saturated yellow | Energetic |
| sad | Head tilt down, slumped | Half-lidded, downturn | Cool blue, dim | Subdued |
| angry | Forward lean, head low | Narrowed, intense | Red, sharp pulse | Use sparingly |
| scared | Recoil back, head up | Wide, alert | Cold white, fast pulse | Startle pose |

#### 9.2.2 Secondary tier (v1.1)

| Cartesia tag | OLAF base pose | Eye display | LED ring | Notes |
|---|---|---|---|---|
| happy | Like content but more upright | Wide, soft blink | Warm yellow | Maps to content in v1 |
| curious | Head tilt to side, lean forward | Wide, focused | Cyan | Maps to content+lean in v1 |
| sympathetic | Slight head tilt, forward | Soft | Warm pink | Maps to content in v1 |
| surprised | Quick recoil, head up | Wide | White flash | Maps to scared (gentler) in v1 |
| frustrated | Subtle head shake | Narrowed | Orange | Maps to angry (gentler) in v1 |
| melancholic | Like sad but slower | Half-lidded | Deep blue | Maps to sad in v1 |

#### 9.2.3 Tertiary tier (v2 — out of scope for v1)

`flirtatious`, `mysterious`, `sarcastic` — fallback to nearest primary in v1 via §9.4. Full mapping in v2.

#### 9.2.4 Remaining 40+ Cartesia tags

Fallback via the family table in §9.4.

### 9.3 Burst Events — Cartesia `[X]`

Currently only `[laughter]` is supported by Cartesia; future bursts (`[sigh]`, `[gasp]`, etc.) plan to ship. Pipeline's splitter accepts the full set already; ones Cartesia doesn't yet support are stripped from the TTS stream but still drive OLAF.

| Burst tag | Cartesia v1 | OLAF burst pose | Duration |
|---|---|---|---|
| `[laughter]` | ✅ supported | Head bob/shake, eyes squint, LED warm pulse | 1500ms |
| `[sigh]` | ❌ not yet | Slow head drop + recover, eyes close briefly | 1200ms |
| `[gasp]` | ❌ not yet | Quick head-up, eyes wide, brief LED flash | 400ms |
| `[clears_throat]` | ❌ not yet | Slight head tilt + pause | 600ms |

### 9.4 Fallback for Unmapped Cartesia Tags

When the LLM emits a tag we haven't explicitly mapped, the splitter falls back via this table:

| Family | Members | Falls back to |
|---|---|---|
| High-energy positive | enthusiastic, elated, euphoric, triumphant, amazed | `excited` |
| Calm positive | peaceful, serene, calm, grateful, affectionate, trust | `content` |
| Engaged neutral | curious, mysterious, anticipation | `content` (v1) |
| Negative active | outraged, mad, agitated, threatened | `angry` |
| Negative passive | dejected, melancholic, disappointed, hurt, guilty, bored, tired | `sad` |
| Apprehensive | anxious, alarmed, panicked, insecure | `scared` |
| Edgy | sarcastic, ironic, contempt, disgusted, envious | `angry` (mild) |
| Truly unknown | anything not in Cartesia's vocab | `neutral` + log warning |

The fallback table is also data, also in `expression_map.yaml`. Updating it doesn't require code changes.

## 10. Lifecycle State Mapping

Pipeline publishes OLAF's lifecycle state at conversation milestones. Separate from emotion — about **what OLAF is doing** rather than how it feels.

| Pipeline event | OLAF lifecycle state | Triggered when |
|---|---|---|
| User starts speaking (VAD detects voice) | LISTENING | First voice frame after silence |
| User finishes utterance | THINKING | End-of-speech detected, before daemon response |
| First audio frame from Cartesia plays | SPEAKING | TTS audio starts |
| Last audio frame plays | IDLE | Speech ends, conversation paused |
| Idle for >5 minutes | SLEEPING | Configurable timeout |

OLAF's renderer can use these states for things outside emotion: a small "listening" indicator while the user talks, a subtle thinking pulse while the agent reasons, returning to neutral pose after speaking. Lifecycle states give it the structure to do that without the pipeline needing to micro-manage poses.

## 11. Configuration

### 11.1 `expression_map.yaml`

Loaded at Pipecat startup. Reloadable on `SIGHUP` for live tuning.

```yaml
emotions:
  neutral:
    tier: primary
    olaf:
      base_pose: { yaw: 0, pitch: 0, roll: 0, lean: 5 }
      eye_state: open_relaxed
      led_color: "#FFFFFF"
      led_intensity: 0.3
  content:
    tier: primary
    olaf:
      base_pose: { yaw: 0, pitch: -5, roll: 0, lean: 3 }
      eye_state: soft_blink
      led_color: "#FFA060"
      led_intensity: 0.5
  # ...
  curious:
    tier: secondary
    fallback: content
    olaf:
      base_pose: { yaw: 0, pitch: 0, roll: 12, lean: 8 }
      eye_state: wide_focused
      led_color: "#40D0FF"

bursts:
  laughter:
    cartesia_supported: true
    duration_ms: 1500
    olaf:
      burst_pose: head_bob
      eye_state: squint
      led_pulse: warm

fallback_families:
  high_energy_positive: excited
  calm_positive: content
  # ...
  unknown: neutral
```

### 11.2 `pipeline.toml`

Service-level config: STT/TTS providers, daemon URL, audio device, ROS domain.

```toml
[transport]
type = "local"  # or "webrtc", "telephony"

[stt]
provider = "deepgram"
model = "nova-3"

[tts]
provider = "cartesia"
model = "sonic-3"
voice_id = "..."
default_emotion = "neutral"

[talker]
model = "claude-haiku-4-5"
read_belief_state = true

[daemon]
url = "http://localhost:8001"
turn_endpoint = "/turn"
beliefs_endpoint = "/beliefs"

[olaf]
transport = "ros2"
ros_domain_id = 7
node_name = "pipecat_voice"
```

## 12. Success Criteria

A turn feels alive when these hold:

- **No perceptible dead air on simple turns.** Talker fast-path serves them without daemon round-trip latency. Target: first audio frame within **TBD ms** of end-of-speech.
- **Tolerable latency on complex turns.** First narration audio plays within **TBD ms** of end-of-speech, even when subagents are still working in the background.
- **Voice/embodiment alignment.** OLAF expression visibly matches voice. ~30-80ms anticipatory window — drift outside this is a bug.
- **Robust unmapped-tag handling.** Any Cartesia tag the LLM might emit produces a defined OLAF behavior via the fallback table. Truly unknown tags fall through to neutral with a logged warning. No silent gaps.
- **Wake-word reliability.** False-positive rate **TBD per hour** of normal ambient background; false-negative rate acceptable for natural conversation start.
- **On-device STT latency.** Whisper + Hailo-8L on Pi delivers transcripts within **TBD ms** of end-of-speech.

> **TBD** numbers to be filled once Phase 0/1 prototypes give measurable baselines.

## 13. Scope

### 13.1 In scope (v1, Phases 0-3)

- Audio I/O, wake-word, on-device STT, Cartesia TTS, ROS 2 expression, lifecycle signaling
- Talker fast-path (in-pipeline LLM call)
- HTTP/WebSocket stream contract with orchestrator (Claude Code session)
- Tag splitter — streaming SSML parser, primary + secondary emotion mapping, full fallback table coverage
- Configuration: `expression_map.yaml` + `pipeline.toml`

### 13.2 Out of scope (v1)

- Tertiary emotion mappings (flirtatious, mysterious, sarcastic) — fallback to nearest primary
- Telephony / SIP transport — local audio first, WebRTC next
- Multiple concurrent user sessions — single-user
- On-device TTS — Cartesia (cloud) only for v1
- Avatar / screen rendering of expression — OLAF embodiment is the only renderer
- Emotion intensity scaling — Cartesia doesn't expose it for sustained emotions; treat as binary
- User expression reading (OAK-D camera) — separate component, not part of this pipeline
- Direct OLAF motion control — expression and lifecycle channels only

## 14. Phasing

| Phase | Goal | Validation |
|---|---|---|
| **0** | Bare voice loop: Pipecat with on-device STT and Cartesia TTS, push-to-talk trigger, single hardcoded prompt. Run on laptop or Pi with mic + speakers. | Audio I/O, STT latency on Pi, transport |
| **1** | Wake-word + daemon dispatch + tag splitter. Replace push-to-talk with always-on wake-word. Wire in orchestrator daemon. Implement streaming SSML parser and segment-on-emotion-or-sentence logic. OLAF still mocked (log expression events to stdout). | Streaming parser correctness, segment logic, daemon integration |
| **2** | Real OLAF expression. Replace stdout mock with `Ros2OlafInterface` from `olaf_interface` library. Validate first that ROS 2 messages arrive at OLAF, then validate audio-anchored timing. | ROS 2 message arrival, audio-anchored alignment ≤ 80ms |
| **3** | Lifecycle + full mapping coverage. Implement lifecycle state publishing including SLEEPING ↔ LISTENING transitions driven by wake-word. Fill out secondary emotion mappings and fallback table to cover full Cartesia vocab. | LLM emits unusual emotions; pipeline doesn't break |

## 15. Decisions & Open Questions

### 15.1 Decided

- **Talker placement.** Lives inside Pipecat (its own LLM call), not in the orchestrator. Avoids designing the orchestrator around fast-path/slow-path branching; keeps the daemon's API uniform.
- **Burst pose timing.** OLAF renderer interpolates burst pose ease-in/ease-out itself. Pipeline says "go to laughter, hold 1500ms, return to base"; renderer chooses the curves.
- **LLM emotions Cartesia rejects.** Cartesia silently drops emotion tags whose value doesn't match the transcript (e.g. `<emotion value="sad"/> I'm so excited!`). When that happens, OLAF still renders the LLM's emotional intent. Orchestrator's job is to keep tags consistent with text.
- **Non-voice OLAF events.** Pipeline does not publish them. Idle behaviors and motion are owned by the orchestrator's home subagent or a dedicated motion controller. Channel stays simple.

### 15.2 Open (work out empirically)

- **Barge-in lifecycle handoff.** When the user interrupts mid-response, what's the right transition? Working assumption: SPEAKING → LISTENING directly, with the splitter flushing in-flight expression events so OLAF doesn't get stuck on a half-finished state. Validate during Phase 2.

## 16. Vision

Phase 3 is the v1 finish line. The component is meant to be **stable, narrow, and replaceable** — voice surface only, no reasoning. Beyond v1, the design intentionally leaves room for:

- Tertiary emotion mappings for full Cartesia vocabulary (v2)
- Intensity scaling once Cartesia exposes it
- Telephony / SIP transport for remote conversations
- On-device TTS when a model that meets the quality bar runs on Pi
- OAK-D camera input feeding user-expression signals back to the orchestrator (separate component, but the pipeline's narrow scope must survive the addition)

The contracts that must survive any future rewrite of this component:

- `POST /turn` request/response shape with the orchestrator
- `OlafAction` event shape on `/olaf/expression` (ROS 2)
- `expression_map.yaml` schema

Everything else is implementation.
