# Component Brief: olaf-embodiment

**Parent project:** OLAF Companion (Personal Voice Agent)
**Status:** Spec phase (no implementation yet)
**Author:** Kamal
**Last updated:** 2026-05-10
**Audience:** LLM coding partner (Claude Code) implementing the embodiment service, plus humans who will fork or extend it
**Pairs with:** [voice-agent-pipeline-brief.md](voice-agent-pipeline-brief.md) — the publisher side. This brief is written *to* the wire that brief defines.

---

## Executive Summary

`olaf-embodiment` is the sibling project that brings OLAF to life on the desk. It subscribes to the four typed ROS 2 topics the [voice-agent-pipeline](voice-agent-pipeline-brief.md) publishes — `mood`, `activity`, `speech_emotion`, `vocalization` — and translates them into physical and visual expression: head pose, eye-screen state, LED color/intensity, gesture (nod / shake), and any audio assets the pipeline doesn't render itself. It owns every "what does that look like?" decision; the pipeline owns every "what does that mean?" decision. The line between them is the wire (`schema_version=3`, `EventEnvelope` + four typed payloads).

The architectural posture is the inverse of the pipeline's. The pipeline is consumer-agnostic — it ships canonical names and audit metadata, nothing about rendering. Embodiment is **producer-agnostic** in the same way: it consumes typed events from a configurable DDS domain, runs entirely on its own hardware loop, and never reaches back into the pipeline. The two projects share only the wire schemas (this repo's `src/voice_agent_pipeline/schemas/*_event.py`), which embodiment imports as a Pydantic dependency or re-derives from the JSON shape in §Appendix A.

This component is the **only** part of the OLAF companion that touches embodiment hardware (servos, eye display, LEDs, gesture actuators). It deliberately does not transcribe, reason, or talk to Cartesia. That narrow scope is the brief's most important contract — when in doubt, embodiment renders less, faster.

## The Problem

A voice agent that can speak but can't be *seen* is a missed opportunity for a companion robot. The voice loop is alive end-to-end (Epic 1 + 2 of `voice-agent-pipeline`); the user hears Ooppi reply with mood-tinted wake greetings, sentence-paced TTS, and Cartesia emotion modifiers. But there's no body. The user has no visual signal that:

1. **OLAF is awake and listening** — `activity` transitions (`sleeping → waking → listening`) need to land as visible posture changes (head lift, eyes open, idle LED color).
2. **OLAF is *thinking*** — when `activity` is `working[thinking]` or `working[delegating]`, the user needs a "I heard you, give me a moment" cue rather than the silent uncanny pause of a half-finished voice agent.
3. **OLAF *means* what it just said** — `speech_emotion` events fire 30–80ms anticipatory of audio (NFR5). The body should match: a small head-up on `surprised`, eye-squint on `frustrated`, warm LED on `content`. Without the visual, the prosody alone reads as flat.
4. **OLAF agrees / disagrees** — the new `nod` / `shake` gesture cues (vocalization topic, schema-3) carry punctuated affirmation / negation. They're useless without an actuator.

The pipeline has solved the "what" of expression — it ships canonical emotion names, FSM states, and gesture cues. Embodiment exists to solve the "how": pose, LED, eye state, gesture timing, and idle ambient behavior between events.

## The Solution

A long-running ROS 2 subscriber service on the OLAF body's local controller (Pi 5 in v1.5; Pi 5 + Hailo-8L in v2 if any embodiment workload — gaze tracking, on-body face detection, emotion-classifier-on-camera — turns out to need accelerated inference). Responsibilities are narrow and concrete:

- **DDS subscribe** — connect to four ROS 2 topics on a configurable `dds_domain_id` (must match the pipeline's `[publisher].dds_domain_id`, default `0`). Topic names are operator-tunable on both sides; default `/olaf/{mood,activity,speech_emotion,vocalization}`.
- **Event deserialization** — every topic carries `std_msgs/String` whose body is the full `EventEnvelope` JSON (envelope fields + topic-specific payload). Validate against pydantic models; reject `schema_version != 3` at parse time (raise — match the pipeline's fail-fast posture).
- **Renderer mapping** — the embodiment project owns its own `embodiment_map.yaml` keyed on the canonical emotion / mood / activity / vocalization names. This is where pose, eye state, LED color, gesture animations live; the pipeline never sees this file.
- **Animation loop** — interpolate between target poses on a fixed-tick render thread (~50–100 Hz for servos, lower for LEDs / eye display). The pipeline ships *target states*; embodiment owns easing curves, settle times, and the audio-anticipatory ~30–80ms lead window for `speech_emotion`.
- **Idle / ambient behavior** — gentle sway, micro-head-movements, blinking, breathing-tinted LED. Driven by `mood` + `activity`, NOT by the pipeline. (When `activity=sleeping`, eyes-closed posture and slow LED breathe; when `activity=listening` and no `speech_emotion` for >5s, return to mood-tinted neutral.)
- **Hardware boundary** — all hardware drivers (servo bus, GPIO LEDs, eye display) sit behind Protocol-shaped adapters so a future hardware swap (different head, different LED count, OLED → ePaper eyes) is one adapter implementation.

## What Makes This Different

The architectural decisions that an LLM implementing this component must not violate:

1. **Subscribe-only, never publish back.** The pipeline is the source of truth for mood, activity, and emotion. Embodiment never publishes to those topics — never. If the user pets OLAF on the head and a touch sensor wants to express something, that's a *different* topic on a *different* (future) project, not a write into the pipeline's contract. Single-writer per topic is the invariant that prevents drift.
2. **Renderer mapping is data, in the embodiment repo, keyed on canonical names.** `embodiment_map.yaml` (schema name TBD by embodiment author) maps each first-class emotion / mood / activity / vocalization to whatever the body needs. Pre-schema-3 a chunk of this lived as `expression_data:` blocks inside the *pipeline's* `expression_map.yaml`, shipped on the wire — that was the boundary violation the schema-3 repair undid (see [sprint-change-proposal-2026-05-10.md](sprint-change-proposal-2026-05-10.md)). The mapping has come home.
3. **Animation timing is owned consumer-side.** The pipeline anchors `speech_emotion` events to audio frames and ships them ~30–80ms ahead of the matching audio (NFR5). Embodiment treats those as *target states* to interpolate toward; ease curves, overshoot, settle-time tuning, and idle return are all consumer choices. The pipeline does not specify "how to nod"; embodiment does.
4. **Disambiguate by topic, not by name, when names overlap.** `mood` and `speech_emotion` share three names by accident of vocabulary — `happy`, `curious`, `excited` appear in both. They mean different things at different timescales: mood is slow disposition (≤4 publishes/hour, NFR31); speech_emotion is per-segment, audio-anchored, high-cadence. The renderer mapping keys these separately (`mood.happy → ambient_warm_breathe`, `speech_emotion.happy → quick_smile_with_audio`).
5. **Vocalizations split into audio-bursts vs gesture-cues.** `tts_supported: true` (audio bursts: `laughter`, `sigh`, `gasp`, `clears_throat`) means Cartesia rendered or attempted-to-render the audio; embodiment's job is the *visual* accompaniment (open mouth on laugh, shoulder drop on sigh) plus optional fallback audio when `tts_supported: false`. `tts_supported: false` for gesture cues (`nod`, `shake`) is unambiguous: never play audio for these — they are head movements, period. Adding audio to `[nod]` would be a defect.
6. **Fail-fast on missing dependencies (v1).** No DDS connection? Crash. Missing `embodiment_map.yaml`? Crash. Servo bus offline? Crash. systemd restarts. Same posture as the pipeline (CLAUDE.md rule #4). v2 adds a graceful-degradation layer.

## Stakeholders & Consumers

This project produces no events; it consumes them. The stakeholders are the *publisher* (the pipeline) and the *body* (Kamal's hardware).

- **Kamal** — primary user, primary developer, primary integrator. Runs the embodiment process on the OLAF Pi; owns the servo bus / eye display / LED hardware design choices.
- **voice-agent-pipeline** — the publisher this brief is written *to*. Schema-3 wire contract pinned at [architecture.md §Publisher Contract + Event Schemas](architecture.md). When the pipeline bumps `schema_version`, embodiment must move in lockstep — there is no dual-version mode.
- **Future telemetry consumer** — a third subscriber on the same topics (a dashboard, a logger, a remote-tele-presence companion). The agnostic-publisher boundary means embodiment doesn't care if it exists; it consumes the wire and that's it. Same for embodiment v2 running alongside a v1 — DDS multicast handles the fan-out.

## Success Criteria

A body feels *alive* when these hold:

- **Wake transition is visible within 100ms of the `activity` event.** When `activity` flips `sleeping → waking`, the head lifts and eyes open within 100ms of the embodiment process receiving the event. (The pipeline's wake-greeting NFR30 is 1500ms end-to-end; embodiment shouldn't be the bottleneck on the body side.)
- **Audio-anchored emotions land within the 30–80ms anticipatory window.** Embodiment's pose target hits the body within (audio-anchor − 30ms) to (audio-anchor − 80ms) — i.e., the body moves *just before* the matching audio. This is the consumer side of NFR5.
- **Mood reads as a slow drift, not a flicker.** Mood transitions ease in over 2–4 seconds; never snap. The pipeline already enforces ≤4 mood publishes/hour (NFR31), so the raw event rate is low; embodiment's job is to make even those four feel like a slow background shift, not four sharp poses.
- **Idle behavior at `activity=listening` (no speech_emotion for 3s+)** holds the current mood-tinted neutral pose with micro-movement and slow breath-LED. Doesn't drift, doesn't twitch, doesn't go statue-still.
- **`[nod]` / `[shake]` gestures land within ~150ms of receipt.** These are punctuation, not setpoints — they should feel *crisp*, not animated. Quick attack, fast settle.
- **Schema-version mismatch crashes loudly.** A `schema_version` other than 3 in any received event raises, exits non-zero, and surfaces the discrepancy in journald — never silently truncates or ignores. (The pipeline does the same on its side.)
- **Hardware swap is a one-adapter change.** Replacing the servo bus or the eye display means writing one new Protocol implementation, no changes to the renderer mapping, the DDS layer, or the animation loop.

## Scope

**In scope (v1 / v1.5):**
- ROS 2 subscriber for the four topics, schema-3 envelope validation.
- Renderer mapping in YAML, keyed on canonical names, with a startup loader (mirror the pipeline's `expression_map.py` shape).
- Animation loop driving servos / LEDs / eye display; Protocol-shaped hardware adapters.
- Idle ambient behavior from mood + activity.
- Gesture cues (`[nod]`, `[shake]`) bound to head-nod / head-shake actions.
- systemd-managed long-running process; structured JSON logging; fail-fast on missing deps.

**Out of scope (v1):**
- Anything that publishes to the pipeline's topics. (Future touch / camera input → its own topic on its own project.)
- Voice synthesis (the pipeline owns Cartesia; embodiment renders pose, not phonemes).
- Reasoning, belief state, tools, MCP — those live in the orchestrator, not on the body.
- Cloud round-trips for any embodiment decision. The body must work over LAN-only.
- Cross-restart pose persistence (graceful-default-on-startup is enough for v1).

**Deferred to v2:**
- Hailo-8L acceleration for any on-body inference (gaze tracking, face detection, expression classifier). Justified only if a measured workload needs it.
- Resilience layer (reconnect-with-backoff on DDS drop, graceful degradation on hardware faults, mood persistence across restarts).
- Multi-OLAF orchestration (two bodies sharing a personality).

## Deployment Platform

**v1.5 target: Raspberry Pi 5** (Bookworm, 64-bit). Same hardware family as the v1.5 pipeline port (per project memory: pipeline v1 is local PC, v1.5/v2 is Pi 5; embodiment ships against the same v1.5 Pi). Co-locate with the pipeline on the same Pi, on the same DDS domain — DDS handles loopback efficiently, no network hop. The Pi 5 has enough headroom for both processes plus the servo / eye / LED bus.

**v2 target: Pi 5 + Hailo-8L** if and only if a measured embodiment workload (camera-based gaze, face detection, real-time emotion classifier) needs accelerated inference. Otherwise stay CPU-only — the animation loop is not GPU-bound.

**Single-host deployment is the default.** Multi-host (pipeline on PC, body on Pi over LAN) is supported by DDS and worth keeping the door open, but it's not the v1 scenario; cross-host clock sync becomes the new constraint on the NFR5 timing window.

## Risks

- **Schema-version drift between projects.** If embodiment lags the pipeline through a `schema_version` bump, the entire body silently fails (or noisily fails — the fail-fast posture surfaces it loudly). Mitigation: pin schema imports to a tagged release of `voice-agent-pipeline` rather than `main`; bump in lockstep.
- **NFR5 anticipatory window across hosts.** The 30–80ms lead is achievable on a single Pi over loopback DDS. On separate hosts over Wi-Fi the window can blur to ±100ms. Mitigation: ship single-host as v1 default; document the multi-host caveat.
- **Hardware abstraction over-engineering.** It's tempting to build a generic "render any body" abstraction. Don't. Ship one concrete OLAF v1 body with one concrete adapter set, refactor when a second body materializes (architecture's "three similar lines beats premature abstraction").
- **Renderer-mapping vocabulary lag.** When the pipeline adds a new emotion or vocalization, embodiment's `embodiment_map.yaml` falls out of sync until the operator updates it. Mitigation: embodiment loader logs `embodiment.unmapped_<topic>` at WARN every time it sees a name with no mapping (mirrors the pipeline's `speech_emotion.unmapped` / `vocalization.unmapped` discipline). Falls back to a `default_*` pose so the body doesn't freeze.

---

## Appendix A — Interface Contract

The wire is the contract. This appendix captures it as it stands at `voice-agent-pipeline` schema-3 (sprint-change-proposal-2026-05-10).

### A.1 — Transport

| Setting | Value | Source |
|---|---|---|
| Transport | ROS 2 / DDS | architecture.md §Publisher Contract |
| Wire format | `std_msgs/String` carrying full envelope JSON, single serialization hop | architecture.md §"V1 wire format simplification" |
| Default DDS domain | `0` (configurable on both sides via `[publisher].dds_domain_id`) | `setup.toml` |
| Reliability | RELIABLE on all four topics (NFR21) | architecture.md §Per-topic QoS |
| Discovery | DDS multicast — no broker, no static peer list | ROS 2 default |

### A.2 — Topics + QoS

| Topic | Default name | Durability | Depth | Cadence |
|---|---|---|---|---|
| `mood` | `/olaf/mood` | transient_local (latched) | 1 | On `MoodController.set()`; ≤4 publishes/hour (NFR31). Late-joining subscribers learn the current mood at connect. |
| `activity` | `/olaf/activity` | transient_local (latched) | 1 | On every FSM transition. First event is `state="starting"` with `from_state=None`. Late-joining subscribers learn current state at connect. |
| `speech_emotion` | `/olaf/speech_emotion` | volatile | 8 | Per-segment (sentence terminator or emotion-tag boundary), audio-anchored ~30–80ms anticipatory of the matching audio frame. Deduped upstream by `LastPublishedCache` per turn. |
| `vocalization` | `/olaf/vocalization` | volatile | 8 | Per LLM-emitted `[tag]`, audio-anchored. Never deduped (FR24). |

Topic names are operator-configurable via the pipeline's `[publisher.topics]` block in `setup.toml`. Embodiment must read the same names from its own config.

### A.3 — Common Envelope

Every event on every topic carries this envelope. `payload` is tightened to a topic-specific Pydantic model in each event subclass.

```python
class EventEnvelope(BaseModel):
    schema_version: int = 3
    timestamp: datetime                              # UTC, ISO8601 on the wire
    source: Literal["voice_agent_pipeline"]
    correlation_id: UUID                             # turn-scoped binding for audio-anchored events
    payload: <topic-specific BaseModel>
```

Example wire bytes (single-line JSON; pretty-printed here for the brief):

```json
{
  "schema_version": 3,
  "timestamp": "2026-05-10T13:42:18.123456+00:00",
  "source": "voice_agent_pipeline",
  "correlation_id": "12345678-1234-5678-1234-567812345678",
  "payload": { /* topic-specific */ }
}
```

**Subscriber rule:** if `schema_version != 3`, raise `SchemaVersionError`, log to journald, exit non-zero. Same posture as the pipeline's `assert_schema_version` helper.

### A.4 — `MoodPayload` (topic `mood`)

```python
class MoodPayload(BaseModel):
    mood: Literal["calm", "happy", "playful", "curious",
                  "thoughtful", "sleepy", "grumpy", "excited"]
    reason: str | None = None    # e.g. "set_mood tool", "startup", "calibration"
```

| Field | Type | Notes |
|---|---|---|
| `mood` | `Literal[8]` | Adding a value is a code change in the pipeline (Talker prompt is fine-tuned to the enum). Embodiment binds each value to ambient pose / LED breathe / eye-base-state. |
| `reason` | `str \| None` | Useful for embodiment debugging logs; never required for rendering. |

**Lifecycle for embodiment:**
- Latched topic, depth=1 — late-joining subscribers receive the current mood on connect.
- Cadence ≤4/hour. Embodiment eases pose / LED targets in slowly (2–4s).
- Mood is the **base layer**; `speech_emotion` overlays on top per-utterance and returns to mood between segments.

### A.5 — `ActivityPayload` (topic `activity`)

```python
ActivityState = Literal["starting", "sleeping", "waking", "listening",
                        "working", "speaking", "going_to_sleep"]
WorkingSubmode = Literal["thinking", "delegating"]

class ActivityPayload(BaseModel):
    state: ActivityState
    working_submode: WorkingSubmode | None = None    # non-null only when state="working"
    transition_reason: str | None = None             # e.g. "wake_word", "vad_silence"
    from_state: ActivityState | None                 # None only on initial state="starting"
```

| Field | Type | Notes |
|---|---|---|
| `state` | `Literal[7]` | 7-state FSM. Embodiment binds each to a base posture (sleeping = head down + eyes closed; listening = upright + eyes open + ear-LED breathe; speaking = subtle mouth/jaw cue; etc.). |
| `working_submode` | `Literal[2] \| None` | Non-null only when `state="working"`. Renders the difference between "thinking" (in-process Talker reasoning, short) and "delegating" (orchestrator dispatch, can be long — show longer-running cue). |
| `transition_reason` | `str \| None` | Free-text. Embodiment-side debugging, not rendering. |
| `from_state` | `ActivityState \| None` | Where we came from. Useful for choosing transition animations (sleeping→waking is a different motion than working→listening). `None` only on the very first `starting` event. |

**Lifecycle for embodiment:**
- Latched topic, depth=1.
- Every transition lands as an event. Embodiment animates between base postures; transition style can use `from_state` for asymmetric cues (sleep→wake stretches differently than wake→sleep).

### A.6 — `SpeechEmotionPayload` (topic `speech_emotion`)

**Schema-3 shape** (the pre-3 `expression_data: dict[str, Any]` field is removed — see [sprint-change-proposal-2026-05-10.md](sprint-change-proposal-2026-05-10.md)):

```python
class SpeechEmotionPayload(BaseModel):
    emotion: str                              # resolved canonical first-class name
    source_tag: str                           # original tag from LLM stream
    audio_frame_id: str | None = None         # Pipecat audio-frame id (Story 3.7 populates)
    raw_tag: str                              # verbatim LLM tag for audit
    resolved_fallback: str | None             # None / family name / "unknown"
```

| Field | Type | Notes |
|---|---|---|
| `emotion` | `str` (always one of the 12 canonical first-class names) | The key embodiment indexes its renderer mapping by. v1 first-class set: `neutral`, `content`, `excited`, `sad`, `angry`, `scared` (primary), `happy`, `curious`, `sympathetic`, `surprised`, `frustrated`, `melancholic` (secondary). |
| `source_tag` | `str` | The Cartesia tag that came in. May be a family member or unmapped — `emotion` already resolved that. Useful for debugging logs. |
| `audio_frame_id` | `str \| None` | Anchor for the ~30–80ms anticipatory window (NFR5). When present, embodiment uses it to time the pose target relative to the audio buffer Cartesia is feeding. |
| `raw_tag` | `str` | Verbatim LLM emission. Audit trail (FR20). |
| `resolved_fallback` | `str \| None` | `None` for first-class hits; a family name (e.g. `"high_energy_positive"`) for fallback-family hits; the literal string `"unknown"` for the truly unmapped fall-through. Embodiment can use this for finer rendering ("we *think* it's excited but the source was `enthusiastic` — slightly different LED tint?"), but the simple v1 path keys solely on `emotion`. |

**12 canonical names** — embodiment's `embodiment_map.yaml` MUST cover all 12 with target poses / LEDs / eye states. A v1 unmapped hit on this topic is a startup-blocker for embodiment, mirroring the pipeline's loader strictness.

**Lifecycle for embodiment:**
- Volatile, depth=8.
- Per-segment cadence — typically a few per spoken sentence.
- Anticipatory ~30–80ms ahead of audio. Embodiment treats `emotion` as a target pose and interpolates toward it; on the next event, interpolates toward the new target. Idle return to mood-base at `activity=listening` after >3s of silence.
- Deduped upstream — the pipeline's `LastPublishedCache` ensures embodiment doesn't see two consecutive identical-emotion segments. Embodiment does NOT need its own dedup.

### A.7 — `VocalizationPayload` (topic `vocalization`)

```python
class VocalizationPayload(BaseModel):
    tag: str                                  # vocalization name
    audio_frame_id: str | None = None
    tts_supported: bool                       # whether Cartesia rendered audio
```

| Field | Type | Notes |
|---|---|---|
| `tag` | `str` | v1 set: `laughter`, `sigh`, `gasp`, `clears_throat` (audio bursts), `nod`, `shake` (gesture cues). |
| `audio_frame_id` | `str \| None` | Same NFR5 anchor as `speech_emotion`. |
| `tts_supported` | `bool` | `true` → Cartesia rendered audio; embodiment adds a *visual* accompaniment (open mouth on laugh, shoulder drop on sigh). `false` → Cartesia did NOT render audio; embodiment is fully responsible — either play its own audio asset OR (for `nod`/`shake`) render a silent gesture only. |

**The `tts_supported=false` policy split:**

- For **audio bursts** (`sigh`, `gasp`, `clears_throat` in v1): embodiment MAY supply its own audio asset (a recorded sigh sample, etc.) on top of the visual cue. Optional.
- For **gesture cues** (`nod`, `shake`): embodiment MUST NOT play audio. They are visual gestures; emitting a "yes" or "no" sound would conflict with the LLM's text — which already says yes / no in words on the same line.

**Lifecycle for embodiment:**
- Volatile, depth=8.
- Punctual, never deduped (FR24).
- Crisp attack — `[nod]` / `[shake]` should feel like punctuation, not a held pose.

### A.8 — Schema-3 deltas from the pipeline's prior wire

If the embodiment author has read pre-2026-05-10 versions of the pipeline docs and is wondering what changed:

- **`SpeechEmotionPayload.expression_data: dict[str, Any]` is GONE.** Pre-3 it carried OLAF-renderer-vocabulary (`base_pose`, `eye_state`, `led_color`, `led_intensity`) populated from the pipeline's `expression_map.yaml`. That coupling violated the consumer-agnostic publisher boundary; the renderer mapping has come home to the embodiment side, where it always belonged.
- **`expression_map.yaml`'s `emotions:` block is now a list of canonical names**, not a mapping-of-EmotionEntry. Embodiment doesn't need to read this file — it's a pipeline-internal vocabulary. Embodiment maintains its OWN `embodiment_map.yaml` (or whatever shape it chooses) keyed on the same canonical names.
- **Two new vocalization tags** `nod` and `shake` (gesture cues, `tts_supported: false`). The pipeline's Talker prompt teaches the LLM to emit them on clear affirmatives / negatives.
- **`schema_version` bumped 2 → 3.** Lockstep across `setup.toml`, `expression_map.yaml`, `EventEnvelope`. Embodiment must reject events at any other version.

---

## Appendix B — Embodiment-Side Configuration Shape (recommended starting point)

This is a *recommendation*, not a wire contract — embodiment is free to organize its config however the implementer prefers. The recommendation mirrors the pipeline's split (`setup.toml` for service config, `expression_map.yaml` for vocabulary mapping) for consistency across the two-project family.

### B.1 — `embodiment_setup.toml` (service config)

```toml
schema_version = 1                  # embodiment's own version, independent of pipeline's

[dds]
domain_id = 0                       # MUST match pipeline's [publisher].dds_domain_id

[topics]
mood = "/olaf/mood"
activity = "/olaf/activity"
speech_emotion = "/olaf/speech_emotion"
vocalization = "/olaf/vocalization"

[hardware]
servo_bus = "i2c-1"
eye_display = "spi-0.0"
led_strip_count = 12
led_strip_pin = 18

[animation]
servo_tick_hz = 100
led_tick_hz = 30
mood_ease_seconds = 3.0
emotion_anticipatory_ms = 50        # target lead within the 30–80ms NFR5 window
gesture_attack_ms = 80              # nod/shake snappy attack
gesture_settle_ms = 200

[idle]
return_to_neutral_after_seconds = 3.0
breath_period_seconds = 4.0
```

### B.2 — `embodiment_map.yaml` (vocabulary → render mapping)

```yaml
schema_version: 1

# Mood → ambient base layer. Slow ease (mood_ease_seconds). All 8
# canonical mood values MUST be covered — startup loader rejects gaps.
mood:
  calm:
    base_pose: { yaw: 0, pitch: 0, lean: 5 }
    eye_state: open_relaxed
    led_color: "#a0c0ff"
    led_intensity: 0.3
  happy:
    base_pose: { yaw: 0, pitch: 3, lean: 7 }
    eye_state: bright
    led_color: "#ffd060"
    led_intensity: 0.6
  # ... 6 more (playful, curious, thoughtful, sleepy, grumpy, excited)

# Speech emotion → per-segment overlay on top of mood base. Anticipatory
# pose target. All 12 canonical first-class emotion names MUST be covered.
speech_emotion:
  neutral:    { base_pose: { yaw: 0, pitch: 0 }, eye_state: open, led_overlay: none }
  content:    { base_pose: { yaw: 0, pitch: 0 }, eye_state: open, led_overlay: warm_soft }
  excited:    { base_pose: { yaw: 0, pitch: 5 }, eye_state: wide, led_overlay: orange_pulse }
  sad:        { base_pose: { yaw: 0, pitch: -10 }, eye_state: squint, led_overlay: blue_dim }
  # ... 8 more

# Activity state → which base posture / behavior the body holds.
# All 7 canonical activity states MUST be covered (working_submode optional).
activity:
  starting:        { posture: powering_up,  eye_state: closed_to_open, led: boot_sequence }
  sleeping:        { posture: head_down,    eye_state: closed,         led: slow_breath_cool }
  waking:          { posture: head_lifting, eye_state: opening,        led: warm_fade_in }
  listening:       { posture: upright,      eye_state: open,           led: ear_breathe }
  working:
    thinking:      { posture: head_tilt_left,  eye_state: focused,     led: thinking_pulse_blue }
    delegating:    { posture: head_tilt_right, eye_state: distant,     led: working_spinner }
  speaking:        { posture: upright,      eye_state: animated,       led: warm_active }
  going_to_sleep:  { posture: head_dropping, eye_state: closing,       led: warm_fade_out }

# Vocalization → punctual gesture / audio cue.
# All 6 canonical vocalization tags MUST be covered (laughter, sigh, gasp,
# clears_throat, nod, shake).
vocalization:
  laughter:        { gesture: shoulder_bob,    audio_asset: null,         visible_only: false }
  sigh:            { gesture: shoulder_drop,   audio_asset: "sigh.wav",   visible_only: false }
  gasp:            { gesture: head_up_quick,   audio_asset: "gasp.wav",   visible_only: false }
  clears_throat:   { gesture: head_tilt_brief, audio_asset: "ahem.wav",   visible_only: false }
  nod:             { gesture: head_nod,        audio_asset: null,         visible_only: true }
  shake:           { gesture: head_shake,      audio_asset: null,         visible_only: true }
```

`visible_only: true` is the gesture-cue invariant — embodiment MUST NOT play audio for these tags even if `audio_asset` is later configured. Validation at load time.

### B.3 — Startup Validation (mirror the pipeline's discipline)

The embodiment loader, on startup:

1. Loads `embodiment_setup.toml` and `.env` (if any secrets — none in v1).
2. Loads `embodiment_map.yaml`.
3. **Asserts vocabulary completeness** against the pipeline's canonical sets:
   - All 8 `Mood` values covered under `mood:`.
   - All 7 `ActivityState` values covered under `activity:` (with both `working_submode` values nested under `working:`).
   - All 12 first-class `speech_emotion` names covered under `speech_emotion:`.
   - All 6 `vocalization` tags covered under `vocalization:`.
4. **Asserts `visible_only: true` on `nod` and `shake`** — startup blocker if either is missing the flag.
5. Connects DDS, subscribes to all four topics on the configured domain.
6. Runs through the hardware adapters' `connect()` methods in sequence; any failure is fatal.
7. On all green, transitions to `running` and starts the animation loop.

A failure at any step raises with a clear operator-facing message, exits non-zero, journald captures it, systemd restarts the unit. Same posture as the pipeline.

---

*This brief lives in the `voice-agent-pipeline` repo because that's where the wire contract is authored — the brief travels WITH the contract. When the embodiment project is set up as a sibling repo, this file should be copied to its `docs/` and kept in sync via a tagged release of the pipeline.*
