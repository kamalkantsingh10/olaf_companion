# voice-agent-pipeline — Canonical Component Spec

**Parent project:** OLAF Companion (Personal Voice Agent)
**Status:** Design phase
**Author:** Kamal
**Last updated:** 2026-05-06
**Audience:** LLM coding partner (Claude Code) implementing the component

> One-file canonical reference. Brief framing on top, full contracts and configuration below. Point Claude Code at this file when implementing.

---

## 1. Summary

`voice-agent-pipeline` is the Pipecat-based service that owns OLAF Companion's voice loop and embodiment surface: speech in, spoken response out, OLAF expressive surface in sync. It is the **only** component touching audio hardware and the **only** publisher to OLAF's four ROS 2 event topics — `/olaf/mood`, `/olaf/activity`, `/olaf/speech_emotion`, `/olaf/vocalization`. It does not reason, does not call MCP tools, does not write belief state — that's the orchestrator (a separate Claude Code session). This component is the voice and embodiment surface.

The component is **conversation-shaped**, not turn-shaped: the wake word transitions OLAF from `sleeping` to `awake`, after which user speech flows continuously without re-arming. Sleep is intent-driven — the Talker LLM detects "we're done" semantically and fires a `go_to_sleep()` tool-call. There is no idle auto-sleep. On every wake, Talker generates a 2–8 word mood-tinted greeting in a "cool friend" register; during conversation, Talker can update OLAF's mood via `set_mood(mood)`.

The architecture is shaped by two constraints:

- **Performance.** A single-LLM voice loop forces the user to wait for the full reasoning cycle before any audio comes back — seconds of dead air on multi-step turns. The Talker fast-path inside Pipecat answers simple turns directly from belief state while the orchestrator's deeper work runs in the background.
- **Correctness.** Voice and the audio-anchored embodiment events (`speech_emotion`, `vocalization`) must stay synced. The tag splitter is a **single fan-out point** that emits Cartesia text plus those two event types from the same parsed segment, anchored to the same audio frames. There is no parallel channel for OLAF expression — drift is prevented by construction. The other two topics (`mood`, `activity`) are FSM-driven and publish on transition; they are not audio-anchored.

## 2. Hard Architectural Constraints

These are the decisions an LLM implementing this component **must not violate**. Everything else is implementation choice.

1. **Single fan-out for audio-anchored events.** The tag splitter is the only place text, `speech_emotion`, and `vocalization` events diverge. All three come from the same parsed segment, anchored to the same audio frames. (`mood` and `activity` are FSM-driven and not audio-anchored.)
2. **Single-writer belief state.** Only the orchestrator writes belief state. The Talker reads via the daemon API, never directly.
3. **Audio-frame anchoring.** `speech_emotion` and `vocalization` events ride the audio frame they correspond to and are published when that frame is sent — not when the tag is parsed. Target alignment: ~30–80ms anticipatory.
4. **Mapping is data, not code.** `expression_map.yaml` is loaded at startup, reloadable on `SIGHUP`. Adding emotions, vocalization tags, or fallback families is a config change. The `speech_emotion` event payload carries both the raw tag and the resolved fallback so consumers can use either.
5. **Talker lives inside Pipecat**, not the orchestrator. Talker is **tool-using** in v1 — registered tool-set is `{go_to_sleep(), set_mood(mood)}`, with typed Pydantic input schemas validated before execution.
6. **Pipeline publishes only on the four event topics.** No other ROS 2 publishes from this component; no non-voice-driven events. Idle behaviors and motion are owned by other components.
7. **Continuous conversation; intent-based sleep.** Wake-word fires only on the `sleeping → waking` transition. While AWAKE, the mic stays open and turns flow without re-prompting. Sleep is fired by Talker `go_to_sleep()` tool-call, scheduled to take effect after the acknowledgement audio finishes (deferred sleep). No idle auto-sleep.
8. **Multi-topic event publish with a common envelope.** Four typed ROS 2 topics (`mood`, `activity`, `speech_emotion`, `vocalization`); every event carries `{timestamp, schema_version, source, correlation_id, payload}`. Schema version is currently **3**. Bump history: 1 → 2 (Story 3.4 — single `/olaf/expression` channel split into four topics); 2 → 3 (sprint-change-proposal-2026-05-10 — `SpeechEmotionPayload.expression_data` removed; consumer-agnostic boundary repair). The publisher is Protocol-based; ROS 2 is the v1 channel adapter, with a fake/log adapter for tests.

## 3. Responsibilities

- **Audio I/O** — mic capture, speaker playback, local audio devices or WebRTC transport.
- **Wake-word detection** — always-on, low-power, on-device. Active **only** while `activity = sleeping`; gates the `sleeping → waking` transition. Mic is the single audio stream consumer in either mode (wake-word listener while sleeping; VAD + STT while AWAKE).
- **Continuous mic capture while AWAKE** — `activity ∈ {listening, working, speaking}` keeps the mic open without re-arming the wake word; turns flow back-to-back.
- **STT (on-device)** — local Whisper or equivalent on the Pi, accelerated via Hailo-8L where viable. No cloud dependency.
- **Turn dispatch** — send user transcripts to the orchestrator daemon over HTTP/WebSocket; receive structured response stream.
- **Talker (tool-using fast-path)** — provider-agnostic in-pipeline LLM (OpenAI / Groq / Gemini via openai-compatible API surface). Two invocation modes:
  - **conversational** — generate spoken reply, may emit tool-calls (`go_to_sleep`, `set_mood`) in parallel.
  - **greeting** — generate 2–8 word mood-tinted wake greeting on every `sleeping → waking` transition. If unreachable / overlong / too slow (>800ms timeout), fall back to a static list (`["hey", "yeah?", "hi"]`).
- **Talker tool execution** — validate tool inputs against typed Pydantic schemas; invalid calls log WARN and are dropped without side-effect. v1 tools: `go_to_sleep` (schedules deferred sleep transition; takes effect after current audio finishes); `set_mood(mood)` (validates against mood enum, publishes to `mood` topic if cooldown allows, updates in-process state).
- **Mood control** — discrete enum (~6–8 states); slow-cadence publishing (≤4/hour, enforced at the publisher boundary, not trusted of the LLM); in-process persistence across `sleeping` periods within a single process lifetime.
- **Tag splitter** — parse the response stream for emotion tags (`<emotion value="X"/>`), vocalization tags (`[laugh]`, `[sigh]`, `[gasp]`, …), and sentence terminators; segment on whichever boundary comes first; build per-segment `speech_emotion` and `vocalization` events.
- **TTS (Cartesia, cloud)** — feed text-with-tags to Cartesia Sonic-3, stream audio frames back into Pipecat. Cartesia receives whichever tags it supports and silently ignores the rest; the pipeline publishes them all.
- **Event publisher (`EventPublisher` Protocol)** — publishes to four typed ROS 2 topics with the common envelope. ROS 2 is the v1 channel adapter; a fake/log adapter exists for tests; future Zenoh / NATS / WebSocket adapters require no consumer-side changes.
- **Activity FSM publish** — publish state transitions on `/olaf/activity` (latched / transient_local QoS). State set: `{starting, sleeping, waking, listening, working, speaking, going_to_sleep}`. The `working` state has v1 sub-modes `{thinking, delegating}`, encoded in the event payload.

## 4. Non-Responsibilities

- Reasoning, planning, multi-step work — orchestrator's job.
- MCP / external-tool invocation — orchestrator's specialists. (The Talker tool registry in this pipeline is **strictly local** — `go_to_sleep` and `set_mood` are state-mutating actions on the pipeline itself, not external tool calls.)
- Belief state writes — orchestrator only; pipeline reads via daemon API.
- Direct OLAF motion control — pipeline publishes only on the four event topics. Motion is owned by the orchestrator's home subagent or a dedicated motion controller.

## 5. Stakeholders & Consumers

- **Kamal** — primary user and dev.
- **Orchestrator daemon (Claude Code session)** — consumes `POST /turn` requests, produces typed event stream.
- **OLAF embodiment renderer** — subscribes to the four ROS 2 topics (`/olaf/mood`, `/olaf/activity`, `/olaf/speech_emotion`, `/olaf/vocalization`). Decides interpolation, ease curves, and ambient/burst layering itself; the pipeline only states target poses, mood, activity, and tag events.
- **Future motion controller** — consumes `activity` for non-voice-driven behaviors (idle sway, listening indicators, sleep transitions) and `mood` for ambient base shifts.
- **Future dashboard / telemetry consumers** — same four topics; the Protocol-based publisher means adding consumers (or alternative channel adapters like a WebSocket bridge) doesn't require pipeline changes.

## 6. Architecture

```
                                Activity FSM (single source of truth for activity transitions)
                                ↓                                              ↑
                                publishes to /olaf/activity                    transitions on observable events
                                                                               (wake-word, EOS, first/last audio,
                                                                                Talker go_to_sleep tool-call)

  ┌────────── activity = sleeping ────────────┐    ┌────── activity ∈ {listening, working, speaking} (AWAKE) ─────┐
  │                                           │    │                                                              │
  │  Mic → Wake-word detector ────fires──────▶ activity → waking ────▶ Talker (greeting mode)                     │
  │  (only consumer of mic                     │    │                  + current mood → 2–8 word reply             │
  │  while sleeping)                           │    │                                                              │
  │                                           │    │  Mic → VAD → On-device STT (Whisper + Hailo-8L)              │
  │                                           │    │                          ↓                                    │
  │                                           │    │                  [user transcript]                           │
  │                                           │    │                          ↓                                    │
  │                                           │    │              ┌───────────┴───────────┐                       │
  │                                           │    │              ↓                       ↓                       │
  │                                           │    │       Talker (conversational)   Orchestrator daemon          │
  │                                           │    │       reads belief state         HTTP /turn (Claude Code)    │
  │                                           │    │       via daemon API             → typed event stream        │
  │                                           │    │       MAY emit tool-calls:       (narration, response_chunk, │
  │                                           │    │         go_to_sleep()             subagent_*, turn_end)      │
  │                                           │    │         set_mood(mood)                                        │
  │                                           │    │              ↓                       ↓                       │
  └───────────────────────────────────────────┘    │              └──────────┬────────────┘                       │
                                                   │                         ↓                                    │
                                                   │             Tag splitter (streaming SSML parser)             │
                                                   │             — segment on emotion / vocalization / sentence    │
                                                   │             — emit speech_emotion (raw_tag + resolved_fallback)│
                                                   │             — emit vocalization ([laugh]/[sigh]/...)          │
                                                   │             — anchor both to audio frame                      │
                                                   │                         ↓                                    │
                                                   │             ┌───────────┴───────────┐                        │
                                                   │             ↓                       ↓                        │
                                                   │       Cartesia TTS         EventPublisher (Protocol)         │
                                                   │       (cloud, Sonic-3)     ├─▶ /olaf/speech_emotion          │
                                                   │             ↓              ├─▶ /olaf/vocalization            │
                                                   │       Audio frames         │   (both audio-anchored)         │
                                                   │       (interleaved with    │                                 │
                                                   │        anchored events)    │   Also published from this      │
                                                   │             ↓              │   surface (FSM-driven, not      │
                                                   │         Speaker            │   audio-anchored):              │
                                                   │                            ├─▶ /olaf/mood (set_mood tool-call)│
                                                   │                            └─▶ /olaf/activity (FSM transitions)│
                                                   │                                       ↓                       │
                                                   │                                  OLAF nodes                  │
                                                   └──────────────────────────────────────────────────────────────┘
```

**Why this shape:**

- The splitter is the single fan-out for **audio-anchored events**. Text+SSML to Cartesia, `speech_emotion` (with `raw_tag + resolved_fallback`) and `vocalization` to ROS 2 — all from the same parsed segment, inherently in sync.
- The activity FSM is the single source of truth for activity transitions; it publishes to `/olaf/activity` directly (not via the splitter) because transitions fire on observable events that are not text-parsed (wake-word, end-of-speech, first audio frame, last audio frame, Talker tool-call result). Latched / transient_local QoS so late subscribers learn the current state immediately.
- The mood topic is published from the `set_mood` tool-execution path. Cooldown enforced at the publisher boundary (≤4/hour, NFR31) — over-rate calls drop with WARN; in-process mood state is not updated until the publish succeeds.
- Wake-word detector and VAD share the mic stream but are mutually exclusive consumers gated by `activity` state — wake-word listens only while `sleeping`; VAD listens only while AWAKE. Mic capture itself is continuous; the consumer changes.
- Talker reads belief state via the daemon's API, never directly. Belief state stays single-writer; pipeline is read-only consumer.
- Audio-anchored events ride the audio frame they correspond to and are published when that frame is sent. The audio frame carries the matching event metadata through Pipecat's frame pipeline; the transport processor publishes both at once. Result: ~30–80ms anticipatory alignment.
- Sleep is deferred: when Talker fires `go_to_sleep()`, the activity FSM schedules the `speaking → going_to_sleep → sleeping` transition to fire after the current acknowledgement audio finishes, so the goodbye is heard before the mic mode flips.

## 7. Stream Contract with Orchestrator Daemon

Pipecat sends user transcripts to `POST /turn` and receives a stream of typed events. Contract is intentionally narrow.

**Request:**

```json
{
  "session_id": "abc123",
  "turn_id": "turn-uuid-7",
  "user_text": "what's on my calendar today?",
  "context": {
    "activity": "listening",
    "mood": "calm"
  }
}
```

`turn_id` is the same UUID the pipeline puts in the `correlation_id` field of the resulting audio-anchored events (see §17 Common Event Envelope), letting downstream consumers tie an `activity` transition or `speech_emotion` event back to the orchestrator turn that produced it.

**Response (Server-Sent Events / WebSocket):**

```json
{ "type": "narration",         "text": "Let me check..." }
{ "type": "subagent_started",  "name": "comms" }
{ "type": "subagent_progress", "name": "comms", "msg": "Reading calendar" }
{ "type": "subagent_done",     "name": "comms" }
{ "type": "response_chunk",    "text": "<emotion value=\"content\"/> you've got " }
{ "type": "response_chunk",    "text": "two meetings today — one at 10..." }
{ "type": "turn_end" }
```

**Pipeline behavior:**

- `narration` and `response_chunk` → splitter (segment, fan out to Cartesia TTS + `speech_emotion` + `vocalization` events).
- `subagent_*` → no `activity` change in v1 (sub-mode stays `working.delegating`); orchestrator events become richer activity sub-modes in v1.5 (`searching`, `tooling`, `composing`).
- `turn_end` → flush splitter; transition `activity → listening` after the last audio frame plays. (No transition to `idle` — that state is removed; the pipeline returns directly to listening for continuous conversation.)

## 8. Tag Splitter — Implementation Requirements

The most algorithmically interesting piece. Requirements:

- **Streaming, not batched.** Token-by-token parsing. Buffer until either a complete tag is recognized or a clear non-tag character is seen.
- **Tags can split across token boundaries.** Standard incremental SSML parser pattern — small state machine, ~50 lines.
- **Segment on whichever comes first:** sentence terminator (`.?!`), emotion tag boundary, or vocalization tag boundary.
- **Recognise both emotion tags (`<emotion value="X"/>`) and vocalization tags (`[laugh]`, `[sigh]`, `[gasp]`, `[clears_throat]`, …).** Vocalization tags are LLM-emitted inline; the splitter is the single parser for both.
- **For each emotion tag, emit a `speech_emotion` event** with payload `{raw_tag, resolved_fallback, family}` where `resolved_fallback` is looked up via `expression_map.yaml` (primary tier → secondary tier → family fallback → `neutral`). Schema accepts open-set tags — any string from the LLM is valid; consumers handle unknowns via the fallback field.
- **For each vocalization tag, emit a `vocalization` event** with payload `{tag, duration_ms, cartesia_supported}`. `duration_ms` comes from `expression_map.yaml`; `cartesia_supported` controls whether the tag is passed to Cartesia in the TTS stream or stripped before send.
- **Strip Cartesia-unsupported tags from the TTS stream, but still publish the corresponding event.** `[sigh]`, `[gasp]`, `[clears_throat]` are not supported by Cartesia v1; pass `[laughter]` through, strip the rest. Lookup table is data in `expression_map.yaml`.
- **Anchor `speech_emotion` and `vocalization` events to audio frames, not text frames.** The Cartesia TTS processor produces audio frames; the splitter attaches the matching event metadata; the transport processor publishes both at the moment the audio frame is sent. Result: ~30–80ms anticipatory alignment (NFR5).
- **Maintain a "last published" cache for `speech_emotion`.** Don't republish the same `raw_tag + resolved_fallback` if it hasn't changed within a turn (saves bandwidth, lets OLAF's renderer hold pose). Cache scope is **turn-scoped** and resets at `activity → listening`. **`vocalization` events always publish** (they are punctual one-shots).

## 9. Cartesia Tag → speech_emotion / vocalization Mapping

The mapping is **the core contract** for embodiment. Every Cartesia tag the LLM might emit produces a `speech_emotion` or `vocalization` event with a defined payload. The mapping table is the resolver source of truth — but the schema is **open-set**: tags not in the table are still published with `raw_tag` populated and `resolved_fallback` falling through the family table.

### 9.1 Mapping Principles

- **Open-set schema, but no silent gaps.** Any Cartesia tag string from the LLM is valid input; the `speech_emotion` event always carries both `raw_tag` (verbatim from the LLM) and `resolved_fallback` (looked up via `expression_map.yaml`, falling through tiers and families to `neutral` if truly unknown). Consumers (OLAF renderer, dashboards) decide whether to honour the raw tag or the fallback. Cartesia silently drops unrecognised tags on the voice side.
- OLAF expresses the **intent** of the tag, not the literal voice prosody. Cartesia changes pitch and pace; OLAF changes pose, eye state, and LED color. Two renderers, same emotional intent.
- Mapping is data, not code. Lives in `expression_map.yaml`, loaded at startup. Editable without code changes.
- **Vocalizations layer over base mood and current speech_emotion.** A vocalization (`[laugh]`, `[sigh]`) sets a transient overlay; the base `mood` and current `speech_emotion` underneath persist. Renderer logic: `vocalization_overlay || speech_emotion || mood_base`.
- **Vocalization source is LLM-emitted inline tags**, parsed pre-TTS by the splitter (§8). Cartesia-emitted bursts are not v1.
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

### 9.3 Vocalization Events — `[laugh]`, `[sigh]`, …

Vocalization tags are **LLM-emitted inline**, parsed pre-TTS by the splitter, and published on `/olaf/vocalization` (audio-anchored, volatile QoS). Cartesia receives the tags it supports and silently ignores the rest; the pipeline publishes them all so OLAF can express them regardless of voice support.

| Vocalization tag | Cartesia v1 voice | OLAF vocalization pose | Duration |
|---|---|---|---|
| `[laugh]` (or `[laughter]`) | ✅ supported | Head bob/shake, eyes squint, LED warm pulse | 1500ms |
| `[sigh]` | ❌ not yet | Slow head drop + recover, eyes close briefly | 1200ms |
| `[gasp]` | ❌ not yet | Quick head-up, eyes wide, brief LED flash | 400ms |
| `[clears_throat]` | ❌ not yet | Slight head tilt + pause | 600ms |

The `cartesia_supported` field in `expression_map.yaml` controls whether the splitter passes the tag to Cartesia (true) or strips it (false). Either way, the `vocalization` event is published.

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

## 10. Activity FSM

Pipeline publishes activity state changes on `/olaf/activity` (latched / transient_local QoS, depth=1) at every transition. Separate from emotion and mood — about **what the pipeline is doing** rather than how OLAF feels.

### 10.1 State Set

| State | Description | Mic mode |
|---|---|---|
| `starting` | Process is initialising — loading config, models, opening audio devices | none |
| `sleeping` | Default idle state. No STT, no Talker, no splitter. Awaiting wake word. | wake-word listener only |
| `waking` | Transient. Wake-word fired; pipeline is invoking Talker in greeting mode and awaiting first audio frame. | VAD active (continuous capture begins) |
| `listening` | Mic open, awaiting user speech. Steady-state between turns during a continuous conversation. | VAD + STT |
| `working` | STT complete, processing turn. Has v1 sub-modes (see §10.2). | VAD + STT (still capturing in case of v1.5 barge-in) |
| `speaking` | Cartesia TTS audio playing. | VAD + STT (v1.5 barge-in path) |
| `going_to_sleep` | Transient. Talker fired `go_to_sleep()`; acknowledgement audio is finishing playback. Mic mode is about to flip back to wake-word-only. | VAD + STT (still) |

> **Removed in this edit:** the old `IDLE` state and the 5-minute idle-to-sleep auto-transition. Sleep is now intent-only via Talker `go_to_sleep()` tool-call. After a turn's `speaking` ends, the pipeline returns directly to `listening` for continuous conversation.

### 10.2 `working` Sub-Modes

The `working` state has v1 sub-modes encoded in the event payload (`{state: "working", sub_mode: "thinking" | "delegating"}`):

| Sub-mode | When it fires | Notes |
|---|---|---|
| `thinking` | Talker fast-path is generating the reply in-pipeline (TurnRouter routed simple turn to Talker, or complex turn pre-dispatch) | Default for the first phase of any turn |
| `delegating` | TurnRouter dispatched to the orchestrator daemon; pipeline is awaiting / consuming the typed event stream | Long turns spend most of their time here |

> **Deferred to v1.5:** `searching` (RAG / web tool in flight), `tooling` (function/tool calls beyond go_to_sleep / set_mood), `composing` (long-form streaming response). v1 ships with `{thinking, delegating}` only.

### 10.3 Transitions

ASCII state diagram (single-turn flow shown):

```
                  ┌────────────────┐  process boot
                  │   starting     │ ←─────────────
                  └────────┬───────┘
                           │ init complete
                           ↓
                  ┌────────────────┐
        ┌────────▶│   sleeping     │
        │         └────────┬───────┘
        │                  │ wake-word fires
        │                  ↓
        │         ┌────────────────┐
        │         │    waking      │  Talker greeting mode invoked
        │         └────────┬───────┘  (with current mood)
        │                  │ greeting first audio frame
        │                  ↓
        │         ┌────────────────┐ ←─────── last audio frame
        │         │   listening    │          (continuous conversation)
        │         └────────┬───────┘
        │                  │ VAD end-of-speech, STT complete
        │                  ↓
        │         ┌────────────────┐
        │         │    working     │  sub_mode = thinking
        │         │  (thinking →   │  (or delegating after dispatch)
        │         │   delegating)  │
        │         └────────┬───────┘
        │                  │ first audio frame from Cartesia
        │                  ↓
        │         ┌────────────────┐
        │         │    speaking    │
        │         └────────┬───────┘
        │                  │ last audio frame
        │                  ├──────────────▶ (continuous: back to `listening`)
        │                  │
        │                  │ if Talker fired go_to_sleep() this turn:
        │                  ↓
        │         ┌────────────────┐
        │         │ going_to_sleep │  transient (~50–200ms); animation hook
        │         └────────┬───────┘
        │                  │
        └──────────────────┘
```

Transition triggers (canonical list):

| From | Event | To | Notes |
|---|---|---|---|
| (none) | process boot | `starting` | |
| `starting` | init complete (config + models + audio + ROS 2 ready) | `sleeping` | Default boot state |
| `sleeping` | wake-word detector fires | `waking` | Mic mode flips to VAD; Talker invoked in greeting mode (FR44) |
| `waking` | first audio frame from Cartesia (greeting) | `speaking` | |
| `speaking` | last audio frame plays | `listening` | If `go_to_sleep` was fired this turn, route to `going_to_sleep` instead |
| `speaking` | last audio frame, AND Talker fired `go_to_sleep` this turn | `going_to_sleep` | Deferred sleep — goodbye is heard before mic mode flips |
| `going_to_sleep` | transient animation window expires | `sleeping` | Mic mode flips to wake-word-only |
| `listening` | VAD end-of-speech, STT transcript ready | `working` (sub_mode=`thinking`) | Always enters `thinking` first |
| `working.thinking` | TurnRouter dispatches to orchestrator | `working.delegating` | Sub-mode change, no top-level transition |
| `working.thinking` or `working.delegating` | first audio frame from Cartesia (response) | `speaking` | |

### 10.4 What Activity Powers on the Renderer Side

OLAF's renderer can use activity state for things outside emotion: a small "listening" indicator while the user talks, a subtle thinking pulse during `working`, eyes opening on `waking`, returning to ambient base on `listening`, eyes closing on `going_to_sleep`. Activity state gives it the structure to do that without the pipeline needing to micro-manage poses.

`activity` uses **transient_local** (latched) QoS so a late-subscribing OLAF node receives the current state immediately on connect — no need to wait for the next transition to know what to render.

## 11. Configuration

### 11.1 `expression_map.yaml`

Loaded at Pipecat startup. Reloadable on `SIGHUP` for live tuning. Schema version is checked at load; incompatible versions are rejected at startup (FR31 / NFR27).

Post-schema-3 (sprint-change-proposal-2026-05-10) the file is a
**vocabulary**, not renderer hints — `emotions:` is a list of canonical
names; the per-emotion `expression_data:` blocks shipping pose / LED /
eye-state are gone. The embodiment project owns that mapping
consumer-side. Two new gesture vocalizations `nod` and `shake` ride on
the same channel as the audio bursts but with `tts_supported: false`
(visual cues only, never audio).

```yaml
schema_version: 3

emotions:
  - neutral
  - content
  - excited
  - sad
  - angry
  - scared
  - happy
  - curious
  - sympathetic
  - surprised
  - frustrated
  - melancholic

vocalizations:
  laughter: { tts_supported: true }
  sigh:     { tts_supported: false }
  gasp:     { tts_supported: false }
  clears_throat: { tts_supported: false }
  nod:      { tts_supported: false }   # gesture cue (head-nod), never audio
  shake:    { tts_supported: false }   # gesture cue (head-shake), never audio

fallback_families:
  high_energy_positive:
    members: [enthusiastic, gleeful, joyful, elated, eager, ecstatic, exuberant, thrilled]
    maps_to: excited
  low_energy_positive:
    members: [relaxed, serene, peaceful, satisfied, calm, hopeful, grateful, fond]
    maps_to: content
  high_energy_negative:
    members: [furious, irritated, annoyed, aggressive, indignant, enraged, hostile, exasperated]
    maps_to: angry
  low_energy_negative:
    members: [melancholy, disappointed, gloomy, regretful, tearful, despondent, weary, defeated, resigned]
    maps_to: sad
  curious_inquisitive:
    members: [inquisitive, interested, intrigued, pondering, contemplative, thoughtful, attentive]
    maps_to: curious
  sympathetic_caring:
    members: [concerned, apologetic, caring, gentle, tender, compassionate, soothing, reassuring]
    maps_to: sympathetic
  surprise_alarm:
    members: [shocked, astonished, startled, alarmed, worried, fearful, anxious, dismayed, taken_aback]
    maps_to: surprised

unknown:
  maps_to: neutral
```

The Mood enum lives in code (`schemas/mood_event.py`), not YAML — it is
fine-tuned with the Talker system prompt and consumer-side rendering,
both of which are code-coupled. v1 values: `calm, happy, playful,
curious, thoughtful, sleepy, grumpy, excited`.

### 11.2 `pipeline.toml`

Service-level config: STT / TTS / Talker providers, daemon URL, audio device, ROS domain, mood + greeting knobs.

```toml
[transport]
type = "local"  # or "webrtc", "telephony"

[stt]
provider = "faster-whisper"
model = "small"
device = "hailo"   # "hailo" | "cpu"

[tts]
provider = "cartesia"
model = "sonic-3"
voice_id = "..."
default_emotion = "neutral"

[talker]
# Provider-agnostic factory — pick one. Only the matching .env API key is
# required at startup. (Story 2.2 final design, 2026-05-05.)
provider = "groq"   # "groq" | "openai" | "gemini"
model    = "llama-3.1-8b-instant"   # whichever model matches the chosen provider
read_belief_state = true
# v1 default is groq for latency headroom on NFR1 (~150–270 ms TTFB on dev host
# vs ~1–1.7 s for openai/gpt-5.4-nano). Switching providers is a one-line edit;
# adding a self-hosted vLLM / Together / Fireworks endpoint is a one-line entry
# in `_PROVIDER_BASE_URLS` plus a sub-block here.
tools = ["go_to_sleep", "set_mood"]   # tool registry exposed to Talker

[wake_greeting]
# 2–8 word greeting register. Talker is invoked in greeting mode on every
# `sleeping → waking` transition with the current mood as context.
timeout_ms = 800        # if Talker doesn't return in this window, use fallback
fallback   = ["hey", "yeah?", "hi"]
register   = "cool_friend"

[mood]
default = "calm"        # used on first wake of a process lifetime
cooldown_minutes = 15   # publisher-enforced (NFR31). Tool-calls under this rate are dropped with WARN.
# enum lives in expression_map.yaml under `moods:`

[daemon]
url = "http://localhost:8001"
turn_endpoint = "/turn"
beliefs_endpoint = "/beliefs"

[olaf]
transport = "ros2"
ros_domain_id = 7
node_name = "pipecat_voice"
# Topic names are conventionally /olaf/{mood,activity,speech_emotion,vocalization}
# but each can be overridden here if needed.

[wake_word]
provider = "porcupine"   # Picovoice; trained on a custom phrase
threshold = 0.55         # tuned during Phase 3 soak (NFR12 / NFR13)
```

### 11.3 Talker Tool Registry (code-defined, not config)

Tool definitions live in code (Pydantic schemas) — not in `pipeline.toml` — because they must be type-validated at the boundary per CLAUDE.md rule 3. The `[talker] tools` array in `pipeline.toml` is a **whitelist of tool names** the Talker is allowed to call; adding a new tool name requires a code change to register the corresponding handler. See §17 (Talker Tools Registry) for the v1 schemas.

## 12. Success Criteria

A conversation feels alive when these hold. Numbers below are the v1 commitments locked in the 2026-05-06 PRD edit; full p95 / observation-window context lives in `prd.md` (NFRs 1–32).

- **Mood-tinted greeting on every wake.** 2–8 words, "cool friend" register; first greeting audio frame within **≤ 1500ms** of wake-word fire (NFR30).
- **No perceptible dead air on simple turns.** Talker fast-path: end-of-speech → first audio frame **≤ 1500ms** at p95 over a 30-min soak (NFR1).
- **Tolerable latency on complex turns.** First narration audio plays within **≤ 1000ms** of end-of-speech (NFR2), even when subagents are still working in the background.
- **Continuous-conversation feel.** After waking, follow-up turns flow without re-saying the wake word. Mic stays open while AWAKE.
- **Intent-based sleep.** Talker detects natural-language goodbye and fires `go_to_sleep()`; OLAF returns to sleep after the acknowledgement audio finishes. No idle auto-sleep.
- **Coherent mood across the conversation.** `mood` publish cadence ≤ **4 / hour** sustained (NFR31), enforced at the publisher boundary.
- **Voice / `speech_emotion` alignment.** Audio-anchored expression matches voice within a **30–80ms anticipatory** window at p95 (NFR5) — outside this is a defect.
- **Robust unmapped-tag handling.** `speech_emotion` payload always carries `raw_tag + resolved_fallback`; truly unknown tags fall to `neutral` with a logged warning. No silent gaps.
- **Wake-word reliability.** False-positive rate **≤ 1 per hour** of typical household ambient (NFR12); false-negative rate **≤ 5%** in normal speaking conditions (NFR13).
- **On-device STT latency.** Whisper + Hailo-8L on Pi: **≤ 500ms** at p95 (NFR3).
- **Talker tool-call decision overhead** must add **≤ 100ms** to the simple-turn budget at p95 (NFR32).

## 13. Scope

### 13.1 In scope (v1, Phases 0-3)

- Audio I/O, wake-word, on-device STT, Cartesia TTS, four-topic ROS 2 publish (`mood`, `activity`, `speech_emotion`, `vocalization`), activity FSM
- Continuous mic capture while AWAKE (no per-turn wake word)
- Talker fast-path (in-pipeline LLM, provider-agnostic factory across OpenAI / Groq / Gemini)
- Talker tool-using — `go_to_sleep()` and `set_mood(mood)` with typed Pydantic input schemas
- Mood-tinted wake greeting (Talker greeting mode + static fallback list)
- Mood model (~6–8 discrete states; slow-cadence publish; in-process persistence within a process lifetime)
- Common event envelope (`timestamp`, `schema_version`, `source`, `correlation_id`, `payload`); `schema_version=3`
- HTTP/WebSocket stream contract with orchestrator (Claude Code session)
- Tag splitter — streaming SSML parser, emotion + vocalization tag parsing, primary + secondary mapping, full fallback table coverage; `speech_emotion` payload carries both raw and resolved tags
- Configuration: `expression_map.yaml` + `pipeline.toml`

### 13.2 Deferred to v1.5

- **Barge-in handling** — mid-utterance interruption. Was on the v1 list as "Open (work out empirically)"; the 2026-05-06 PRD edit moved it formally to v1.5. v1 ships without barge-in; user waits for OLAF to finish before speaking.
- **Expanded `working` sub-modes** — `searching`, `tooling`, `composing` for richer Olaf animations.
- **Cross-restart mood persistence** — v1 retains mood within a single process lifetime only.
- **Configurable idle auto-sleep** — disabled by default; available as v1.5 opt-in.

### 13.3 Out of scope (v1)

- Tertiary emotion mappings (flirtatious, mysterious, sarcastic) — fallback to nearest primary
- Telephony / SIP transport — local audio first, WebRTC next
- Multiple concurrent user sessions — single-user
- On-device TTS — Cartesia (cloud) only for v1
- Avatar / screen rendering of expression — OLAF embodiment is the only renderer
- Emotion intensity scaling — Cartesia doesn't expose it for sustained emotions; treat as binary
- User expression reading (OAK-D camera) — separate component, not part of this pipeline
- Direct OLAF motion control — pipeline publishes only on the four event topics

## 14. Phasing

| Phase | Goal | Validation |
|---|---|---|
| **0** | Bare voice loop: Pipecat with on-device STT and Cartesia TTS, push-to-talk trigger, single hardcoded prompt. Run on laptop or Pi with mic + speakers. | Audio I/O, STT latency on Pi, transport |
| **1** | Wake-word + continuous capture + daemon dispatch + tag splitter + Talker tool-emit + greeting mode. Replace push-to-talk with always-on wake-word; STT is continuously available while AWAKE. Wire in orchestrator daemon. Implement streaming SSML parser, segment-on-emotion-or-vocalization-or-sentence logic, and the Talker tool-call path (`go_to_sleep`, `set_mood`). OLAF still mocked (log adapter behind `EventPublisher` Protocol). | Streaming parser correctness, segment logic, daemon integration, Talker tool-call FP/FN baseline, greeting timing |
| **2** | Real OLAF expression via four-topic publisher. Replace log adapter with `Ros2EventPublisher` (publishes `mood`, `activity`, `speech_emotion`, `vocalization`). Validate first that ROS 2 messages arrive on each of the four topics with the right QoS, then validate audio-anchored timing for `speech_emotion` and `vocalization`. | ROS 2 message arrival on all four topics, latched semantics for `mood`/`activity`, audio-anchored alignment ≤ 80ms |
| **3** | Activity FSM full coverage + mapping completeness + soak. Implement the full activity state set with `working` sub-modes; intent-sleep deferred-transition path; mood lifecycle with cooldown; full secondary-emotion + family-fallback coverage of the Cartesia vocab. Soak in real ambient conditions for ≥ 7 days. | LLM emits unusual emotions; sleep-intent FP/FN within target; mood cadence within NFR31; no flicker; 30-min session pass criteria from PRD §Measurable Outcomes |

## 15. Decisions & Open Questions

### 15.1 Decided

- **Talker placement.** Lives inside Pipecat (its own LLM call), not in the orchestrator. Avoids designing the orchestrator around fast-path/slow-path branching; keeps the daemon's API uniform.
- **Talker is tool-using in v1.** Tool registry: `go_to_sleep()` and `set_mood(mood)`. Tool inputs validated against typed Pydantic schemas before execution; invalid calls log WARN and are dropped without side effects. (See §17.)
- **Continuous conversation while AWAKE.** Wake-word gates `sleeping → waking` only. Subsequent turns flow without re-arming. The mic is the single audio stream consumer in either mode (wake-word listener while sleeping; VAD + STT while AWAKE) — no parallel-listener architecture.
- **Sleep is intent-driven, deferred-execution.** Talker decides; the pipeline executes the transition after the acknowledgement audio finishes so the goodbye is heard before the mic mode flips. No idle auto-sleep.
- **Wake greeting** is generated by Talker in greeting mode on every wake-word fire, tinted by current mood. Static fallback list (`["hey", "yeah?", "hi"]`) for unreachable Talker / overlong responses / 800ms timeout.
- **Four-topic event publish, common envelope, schema version 3.** `mood`, `activity`, `speech_emotion`, `vocalization` over ROS 2. Common envelope: `timestamp`, `schema_version`, `source`, `correlation_id`, `payload`. The publisher is Protocol-based; ROS 2 is the v1 channel adapter.
- **`speech_emotion` schema is open-set.** Any Cartesia tag string is accepted; the payload carries `raw_tag + resolved_fallback`. Cartesia silently drops tags it doesn't recognise on the voice side.
- **Vocalization source: LLM-emitted inline tags**, parsed pre-TTS. Cartesia receives the tags it supports; the pipeline publishes all of them as `vocalization` events anchored to audio frames.
- **Vocalization pose timing.** OLAF renderer interpolates ease-in/ease-out itself. Pipeline says "go to laughter, hold 1500ms, return to base"; renderer chooses the curves.
- **LLM emotions Cartesia rejects.** Cartesia silently drops emotion tags whose value doesn't match the transcript (e.g. `<emotion value="sad"/> I'm so excited!`). When that happens, OLAF still renders the LLM's emotional intent via `speech_emotion`. Orchestrator's job is to keep tags consistent with text.
- **Non-voice OLAF events.** Pipeline does not publish them. Idle behaviors and motion are owned by the orchestrator's home subagent or a dedicated motion controller. Channel stays simple — pipeline publishes only on the four event topics.
- **Barge-in deferred to v1.5.** Was on the previous "still open" list. The 2026-05-06 PRD edit moved it formally to v1.5 backlog (the design contract is preserved in PRD Journey 6).

### 15.2 Open (work out empirically)

- **Sleep-intent prompt tuning.** Talker false-positive `go_to_sleep` (ends real conversation) and false-negative (misses goodbye) rates are emergent from prompt design. Track FP/FN as part of the Phase 3 30-min soak pass criteria; tune the system prompt iteratively.
- **Mood enum stability.** The v1 enum (`happy, playful, calm, curious, gloomy, grumpy, sleepy, excited`) is a starting point; soak may reveal states that should be added or pruned. Cooldown enforcement is at the publisher boundary, so adding states is data-only.
- **Wake-greeting fallback list.** Currently `["hey", "yeah?", "hi"]` configured in `pipeline.toml`. May want richer fallbacks per mood; v1 ships a single uniform list.

## 16. Vision

Phase 3 is the v1 finish line. The component is meant to be **stable, narrow, and replaceable** — voice surface only, no reasoning. Beyond v1, the design intentionally leaves room for:

- **v1.5:** Barge-in (deferred from v1), expanded `working` sub-modes, cross-restart mood persistence, configurable idle auto-sleep
- Tertiary emotion mappings for full Cartesia vocabulary (v2)
- Intensity scaling once Cartesia exposes it
- Telephony / SIP transport for remote conversations
- On-device TTS when a model that meets the quality bar runs on Pi
- Alternative channel adapters (Zenoh, NATS, WebSocket bridge) — Protocol-based publisher means consumers don't change
- OAK-D camera input feeding user-expression signals back to the orchestrator (separate component, but the pipeline's narrow scope must survive the addition)

The contracts that must survive any future rewrite of this component:

- `POST /turn` request/response shape with the orchestrator
- The four typed event schemas on ROS 2 — `mood`, `activity`, `speech_emotion`, `vocalization` — with their common envelope and current `schema_version=3`
- The `expression_map.yaml` schema (mapping table + vocalization table + fallback families + mood enum)
- The Talker tool registry contract — name + Pydantic input schema for each tool, not implementation

Everything else is implementation.

## 17. Event Envelope, Four-Topic Schema, and Talker Tools Registry

This section is the implementation-grade contract for the four-topic publish surface and the Talker tool registry. Everything here is what an LLM coding partner needs to generate the publisher, the FSM hooks, and the tool-execution path correctly. Schemas are shown in Pydantic-style pseudo-code (CLAUDE.md rule 3 — `pydantic.BaseModel` at all event/config/data boundaries) and JSON-on-wire form.

### 17.1 Common Event Envelope

Every event on every topic carries the same envelope. Topic-specific payload lives in `payload`.

```python
# python pseudo-code (illustrative)
from datetime import datetime
from uuid import UUID
from typing import Literal
from pydantic import BaseModel

class EventEnvelope(BaseModel):
    timestamp: datetime          # UTC, ISO8601, microsecond precision
    schema_version: int          # currently 3; bumps only on breaking changes (CLAUDE.md rule 6)
    source: str                  # component name string, e.g. "voice-agent-pipeline"
    correlation_id: UUID         # turn-scoped for audio-anchored events; session-scoped for mood/activity
    payload: dict                # topic-specific Pydantic model (see §17.2)
```

JSON-on-wire example:

```json
{
  "timestamp": "2026-05-06T08:47:12.314152Z",
  "schema_version": 3,
  "source": "voice-agent-pipeline",
  "correlation_id": "f3a51c5e-3b8d-4d29-8b1a-6e0a7c1e3a9c",
  "payload": { "...": "topic-specific" }
}
```

**`correlation_id` rules:**

- For `speech_emotion` and `vocalization` events: use the **turn UUID** (the same `turn_id` sent to the orchestrator in `POST /turn`, or a freshly minted UUID for fast-path turns). This lets consumers tie audio-anchored events back to the turn that produced them.
- For `activity` transitions: use the **turn UUID** when the transition is turn-scoped (e.g. `listening → working` on STT complete, `speaking → listening` on TTS end), or the **session UUID** for session-scoped transitions (e.g. `starting → sleeping`, `sleeping → waking`).
- For `mood` events: use the **session UUID**. Mood is session-scoped, not turn-scoped.

### 17.2 Four-Topic Schema

#### `/olaf/mood`

QoS: **transient_local** (latched), reliable, depth=1. Late subscribers receive last-known mood immediately.

```python
class MoodPayload(BaseModel):
    mood: Literal["happy", "playful", "calm", "curious", "gloomy", "grumpy", "sleepy", "excited"]
    reason: str | None = None    # optional free-text explanation from Talker, for logging/dashboards
```

```json
{ "payload": { "mood": "playful", "reason": "user just made a joke" } }
```

Cadence: ≤ 4 publishes/hour sustained (NFR31), enforced at the publisher boundary.

#### `/olaf/activity`

QoS: **transient_local** (latched), reliable, depth=1.

```python
class ActivityPayload(BaseModel):
    state: Literal[
        "starting", "sleeping", "waking",
        "listening", "working", "speaking",
        "going_to_sleep",
    ]
    sub_mode: Literal["thinking", "delegating"] | None = None  # only when state == "working"
    previous_state: str | None = None  # the state we transitioned from, for animation hooks
```

```json
{ "payload": { "state": "working", "sub_mode": "delegating", "previous_state": "listening" } }
```

`sub_mode` is null for all states except `working`. Future v1.5 sub-modes (`searching`, `tooling`, `composing`) extend the Literal additively (no `schema_version` bump per CLAUDE.md rule 6).

#### `/olaf/speech_emotion`

QoS: **volatile**, reliable, depth=10. Per-segment, audio-anchored.

```python
class SpeechEmotionPayload(BaseModel):
    raw_tag: str                          # verbatim from the LLM; open-set
    resolved_fallback: str                # primary-tier name (neutral/content/excited/sad/angry/scared)
    family: str | None = None             # family-fallback name when resolved via §9.4 family table
    text_segment: str | None = None       # the segment of speech this emotion applies to (DEBUG-level only;
                                          # excluded at INFO level per FR39)
```

```json
{ "payload": {
    "raw_tag": "enthusiastic",
    "resolved_fallback": "excited",
    "family": "high_energy_positive"
} }
```

Cache: turn-scoped last-published cache; suppress republishing the same `raw_tag + resolved_fallback` within a turn. Cache resets at `activity → listening`.

#### `/olaf/vocalization`

QoS: **volatile**, reliable, depth=10. Punctual, audio-anchored. Always publishes (no last-published suppression).

```python
class VocalizationPayload(BaseModel):
    tag: Literal["laugh", "sigh", "gasp", "clears_throat"]   # extends as new vocalizations are added
    duration_ms: int                       # from expression_map.yaml
    cartesia_supported: bool               # whether Cartesia received this in the TTS stream
```

```json
{ "payload": { "tag": "laugh", "duration_ms": 1500, "cartesia_supported": true } }
```

LLM-emitted aliases (e.g. `[laughter]` → `laugh`) are normalised by the splitter via `aliases:` in `expression_map.yaml` before publishing.

### 17.3 EventPublisher Protocol

```python
from typing import Protocol

class EventPublisher(Protocol):
    """Publishes events on the four typed topics. ROS 2 is the v1 implementation;
    a fake/log adapter exists for tests. Adapter substitution requires no consumer
    or pipeline changes (CLAUDE.md rule 7 — mock at Protocol boundaries only)."""

    async def publish_mood(self, payload: MoodPayload, *, correlation_id: UUID) -> None: ...
    async def publish_activity(self, payload: ActivityPayload, *, correlation_id: UUID) -> None: ...
    async def publish_speech_emotion(self, payload: SpeechEmotionPayload, *, correlation_id: UUID) -> None: ...
    async def publish_vocalization(self, payload: VocalizationPayload, *, correlation_id: UUID) -> None: ...
```

The publisher applies the common envelope (`timestamp`, `schema_version=3`, `source`, the supplied `correlation_id`, the supplied `payload`) and writes to the appropriate ROS 2 topic with the QoS profile defined above. Mood-cooldown enforcement (NFR31) and `speech_emotion` last-published-cache (FR24) live inside the publisher implementation, not in the pipeline call sites.

### 17.4 Talker Tools Registry

v1 tool-set: `go_to_sleep` and `set_mood`. Tool definitions live in code (Pydantic schemas), not config — the `[talker] tools` whitelist in `pipeline.toml` only controls which registered tools Talker is allowed to call.

```python
from typing import Literal
from pydantic import BaseModel, Field

# ---- go_to_sleep ----------------------------------------------------------

class GoToSleepInput(BaseModel):
    """Talker calls this when it has detected the user signalling 'we're done'.
    The pipeline schedules the speaking → going_to_sleep → sleeping transition
    to fire after the current turn's audio finishes (deferred sleep)."""
    reason: str | None = Field(
        None,
        description="Optional free-text explanation, used in the structured WARN/INFO log only.",
    )

class GoToSleepResult(BaseModel):
    scheduled: bool                 # True if the deferred sleep was scheduled successfully
    error: str | None = None        # populated if scheduling failed (rare)

# ---- set_mood -------------------------------------------------------------

MoodValue = Literal[
    "happy", "playful", "calm", "curious",
    "gloomy", "grumpy", "sleepy", "excited",
]

class SetMoodInput(BaseModel):
    """Talker calls this when conversation context has drifted enough to justify a
    mood shift. The publisher enforces NFR31 cooldown (≤ 4 publishes / hour);
    over-rate calls are dropped with a WARN log and the in-process mood state is
    NOT updated until a publish succeeds."""
    mood: MoodValue
    reason: str | None = Field(
        None,
        description="Free-text justification, included in the published MoodPayload.reason field.",
    )

class SetMoodResult(BaseModel):
    published: bool                 # True if the mood event was published; False if cooldown dropped it
    current_mood: MoodValue         # the mood after this call (unchanged if dropped)
    cooldown_remaining_seconds: int # seconds until the next publish would be allowed
```

**Tool execution rules:**

- **Validate before executing.** Tool input is parsed against the Pydantic schema. ValidationError → log WARN with `{tool: "go_to_sleep", input: <raw>, error: <pydantic-error>}` and drop the call. No side effect.
- **Execute synchronously within the turn.** `go_to_sleep` and `set_mood` are fast (sub-ms once validated). Long-running tool calls are a v1.5 concern.
- **Return tool results to Talker.** v1 returns the typed `*Result` model. Talker prompt instructions tell it how to incorporate results (e.g. "if `set_mood` returns `published=False`, do not announce a mood shift to the user").
- **Tool-call decision overhead** must add ≤ 100ms to the simple-turn budget at p95 (NFR32). Practical implication: keep the system prompt's tool descriptions tight; use the smallest model the provider exposes (Groq Llama 8B Instant is the v1 default for headroom).

### 17.5 Schema Version Migration Notes

`schema_version=3` is the current wire version. Bump history:

- **`1 → 2`** (Story 3.4, 2026-05-07): single `/olaf/expression` topic carrying `OlafAction` events with lifecycle states `{SLEEPING, LISTENING, THINKING, SPEAKING, IDLE}` was replaced by four typed topics (`mood`, `activity`, `speech_emotion`, `vocalization`) sharing a common `EventEnvelope`.
- **`2 → 3`** (sprint-change-proposal-2026-05-10): `SpeechEmotionPayload.expression_data: dict[str, Any]` removed (consumer-agnostic publisher boundary repair). Pre-3 the field shipped OLAF-specific renderer vocabulary (pose / LED / eye state) verbatim from `expression_map.yaml`; that vocabulary now lives consumer-side, keyed on `payload.emotion`.

**Breaking changes in v2:**

- Single `/olaf/expression` topic split into four topics (`mood`, `activity`, `speech_emotion`, `vocalization`).
- `OlafAction` event type retired; replaced by per-topic typed payloads.
- Lifecycle state set replaced by the activity FSM (state set + sub-modes).
- Idle auto-sleep removed; sleep is intent-driven only.

**No dual-emit mode.** v2 publishers do not also publish v1 events. Consumers of v1 must be migrated. Since the pipeline has no v1 consumers in production yet (Epic 3 not started), this is a clean cut.

**Future additive changes** (e.g. new `working` sub-modes in v1.5, new mood enum entries, new vocalization tags) extend payloads additively and do **not** bump `schema_version` per CLAUDE.md rule 6 — only breaking changes (field removal, type change, topic restructuring) require a bump.
