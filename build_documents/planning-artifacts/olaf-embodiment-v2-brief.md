# Component Brief: olaf-embodiment — v2 work delta

**Parent project:** OLAF Companion (Personal Voice Agent)
**Status:** v2 sprint kickoff brief — implementation pending pipeline Epic 6
**Author:** Kamal
**Last updated:** 2026-05-28
**Audience:** LLM coding partner (Claude Code) picking up the body project for its v2 sprint, plus the human reviewing what the body needs to schedule.
**Pairs with:** [olaf-embodiment-brief.md](olaf-embodiment-brief.md) — the standing contract for the body project (read this first). This document is a **focused v2 work package** describing only what changes for the body in lockstep with pipeline Epic 6. [voice-agent-pipeline-brief.md](voice-agent-pipeline-brief.md) — the publisher side.
**Promoted from:** [decision-records.md](decision-records.md) §DR-002 (head motion, frozen 2026-05-26) + §DR-004 (emphasis-as-vocalization wire shape, frozen 2026-05-28). [epics.md](epics.md) Epic 6 (4 stories — pipeline-side).

---

## Executive Summary

The pipeline's v2 expression cluster (Epic 6 — `epics.md`) lands four
stories that change what the body sees on the wire and adds one new
expressive surface for the body to render. The wire-side change is
small and additive: **one new `vocalization` tag, `emphasis`** —
DR-004's resolution to DR-002's head-motion design. The *render-side*
work is bigger: implementing DR-002's frozen **layered head-motion
model** (base orientation → stochastic rhythm-substrate → emphasis →
style → explicit gestures → anticipation, stochasticity, multi-axis)
on the body's actual servos / eye display / LEDs.

The architectural posture from the standing brief is unchanged: the
body is the **consumer side** of the agnostic-publisher boundary, it
**owns animation**, the pipeline ships only the timing + tag data it
alone has. v2 doesn't relax any of those rules — it just extends the
vocabulary the body must cover and asks the body to actually build the
realizer DR-002 sketched.

This brief tells the body project's LLM partner: read the standing
brief, then schedule the four work items below. The pipeline-side
Epic 6 stories are listed for coordination context only — the body
does not implement them. `schema_version` stays at **3** (additive
Literal extension is forward-compat per CLAUDE.md rule 6 and DR-004).

## What's new in v2 (the deltas)

Four concrete things change for the body when Epic 6 lands on the
pipeline:

1. **One new `vocalization` tag — `emphasis`.** The pipeline starts
   emitting `vocalization(tag="emphasis", audio_frame_id=...,
   tts_supported=false)` events, one per LLM-marked word, anchored to
   the word's Cartesia timestamp. The body's renderer mapping
   (`embodiment_map.yaml`) must cover this tag. Out of seven canonical
   `vocalization` tags total (the existing six + emphasis).
2. **A new audio-anchored event class to render — the prosodic-stress
   head-beat.** Crisp nod-like motion, scaled by current
   `speech_emotion` (style / amplitude) + current `mood` (base
   amplitude), anticipatory ~30–80 ms ahead of the audio anchor
   (NFR5). Distinct from `[nod]` (which is a bigger, deliberate,
   user-affirming nod). Cadence: ~1–2 per typical conversational
   sentence (constrained by the pipeline's Talker prompt density
   target — see Story 6.3).
3. **A full head-motion realizer, replacing whatever stub the body
   currently has.** DR-002's layered model becomes the body's
   animation loop: base orientation (scripted idle now; camera-gaze
   parked) → stochastic rhythm-substrate body-side (most words:
   nothing) → `emphasis` cue rendering → `speech_emotion` overlay
   shaping → `[nod]`/`[shake]` explicit gestures → anticipation lead
   → multi-axis variation (nod + tilt + turn). This is the bulk of
   v2 body work; the standing brief's §"v2 head-motion realizer"
   section is the spec.
4. **Cross-project soak coordination.** Pipeline Story 6.4 includes
   a 7-day-equivalent soak that measures emphasis density,
   anticipatory-window timing, and operator-rated naturalness of the
   resulting head motion *while the body is running*. The body
   project must be ready to participate — operator runs the body's
   service alongside the pipeline during the soak, captures any
   `embodiment.unmapped_vocalization` WARNs, and iterates the
   renderer mapping if drift surfaces.

## v2 work items for the body project — aligned 1:1 with pipeline Epic 6 stories

Body stories are numbered to **match the pipeline Epic 6 stories
they coordinate with**. Two of them (6.1, 6.2) are regression
checks — the pipeline-side change is internal and should not alter
what the body sees on the wire, but the body must *verify* that.
The other two (6.3, 6.4) are the substantive v2 body work.

**Body execution order:** Body Story 6.1 and 6.2 are quick
regression validations runnable in any order (and parallelisable
with pipeline-side stories). Body Story 6.3 is the bulk of the
work and depends on pipeline Story 6.3 having landed (or staged).
Body Story 6.4 is the wrap, coordinated with pipeline Story 6.4's
soak.

---

### Body Story 6.1: Verify body is invariant to pipeline's SSE → WebSocket transport change

**Pipeline counterpart:** pipeline Story 6.1 — Cartesia SSE→WebSocket
migration + word `timestamps` capture + TTFB spike (pipeline-internal
TTS-client change).

**Body work:** **regression check, near-zero implementation.** The
WS migration is pipeline-internal; the body's subscribed topics
(`mood`, `activity`, `speech_emotion`, `vocalization`) carry the
same envelope and same payload shapes. Only the *timing* of when
events fire relative to wake-word might shift slightly (the WS
transport may have a different TTFB profile, which moves
`speech_emotion` / `vocalization` events forward or back in time).

Acceptance:
- A body integration test (or replay test against a captured pre-
  migration log + a captured post-migration log) shows the same
  set of event types and payload shapes arrive in the same order.
- No `embodiment.unmapped_*` WARN fires from this change. No
  schema-version error. No new event types appear unexpectedly.
- Document any measurable timing shift in NFR5 anticipatory
  window calibration — if the pipeline's TTFB tightens, the body's
  `emphasis_anticipatory_ms` setting (Story 6.3) may need to nudge.

**Coordinates with:** pipeline Story 6.1. The pipeline ships the
WS transport flip; the body confirms transparency by running its
existing integration suite against pre / post pipeline builds.

**Size:** half a day (mostly running existing tests; no new code
unless the regression check needs a new fixture).

---

### Body Story 6.2: Verify body is invariant to pipeline's cached-opener + overlap subsystem

**Pipeline counterpart:** pipeline Story 6.2 — cached opener system
+ Cartesia overlap (supersedes Story 5.5 filler design).

**Body work:** **regression check, near-zero implementation.** The
body **does not see openers** — they play through the pipeline's
cached-audio path before Cartesia synthesis fires. From the body's
perspective:
- No new event types on any topic.
- The temporal gap between `activity = working` and the first
  `speech_emotion` event of the real answer **shrinks** (because the
  pipeline now overlaps Cartesia synthesis with opener playback).
  The body's idle behaviour during `working` should look identical;
  the transition out of `working` just happens sooner.

Acceptance:
- Body integration test confirms no new events appear when an
  opener fires.
- Body's idle-during-`working` behaviour holds visually for the
  shorter `working` window.
- If the body has any "long-`working`-state" special idle (e.g., a
  "thinking longer than 3 s" cue), verify it doesn't fire
  prematurely or fail to fire for genuinely long delegate-turns.

**Coordinates with:** pipeline Story 6.2. Quick integration replay
against pre / post pipeline builds.

**Size:** half a day.

---

### Body Story 6.3: Head-motion realizer (DR-002 layered model) + `emphasis` cue + renderer mapping update

**Pipeline counterpart:** pipeline Story 6.3 — emphasis as the 7th
vocalization tag (Talker marks + splitter join × Cartesia timestamps +
publish).

**Body work:** **the bulk of v2 body work** — the full layered
head-motion realizer DR-002 froze, with the new `emphasis` cue as
the trigger that makes the layered model actually expressive on
the body. Slice internally as needed; below is the natural
breakdown but it's one coordinated piece of work.

**(a) Renderer mapping update.** Add `emphasis` to
`embodiment_map.yaml` (7th `vocalization:` entry — recommended
`gesture: head_beat`, `audio_asset: null`, `visible_only: true`,
`amplitude_source: "speech_emotion+mood"`). Extend the
startup-validation completeness check to require all 7 canonical
`vocalization` tags and to assert `visible_only: true` on `nod`,
`shake`, **and** `emphasis` (the gesture-cue trio).

**(b) Realizer scaffold + animation loop.** Fixed-tick render thread
(~50–100 Hz servos, lower for LEDs / eye display). Drive the **base
orientation** from a scripted idle (slow yaw / pitch drift around
centre, mood-tinted breath rhythm). Layer on the **stochastic
rhythm-substrate** — most "potential beat" ticks produce nothing;
a small minority produce micro-jitter. Substrate density modulates
by perceived speech rate (recent event cadence) and current `mood`.

**(c) `emphasis` cue realizer.** When a
`vocalization(tag="emphasis")` event arrives, schedule a head-beat
at `(audio_anchor − anticipatory_lead_ms)` (anticipatory window in
the NFR5 30–80 ms band; configurable). Amplitude scales by current
`speech_emotion` (e.g. larger on `excited`, smaller on `sad`) and
`mood` (calmer mood reduces global amplitude). Crisp attack + fast
settle — punctuation, not a held pose.

**(d) `speech_emotion` overlay realizer.** Target poses + LED tints
shaping global character *while* speaking with a given emotion.
Returns to mood-base on `activity = listening` after >3 s silence
(matches the standing brief's success criterion).

**(e) `[nod]` / `[shake]` explicit-gesture realizers.** Bigger,
deliberate, longer than `emphasis`. Renderer mapping already
covers them in v1; this verifies they still feel correct against
the new layered model.

**(f) Anticipation, stochasticity, multi-axis polish.** All
audio-anchored events fire ahead of audio (~30–80 ms NFR5);
substrate placement has small random jitter (no metronome); motion
splits across nod + tilt + turn axes (not pinned to nod alone).

**Acceptance:**
- Renderer mapping covers all 7 canonical tags;
  `embodiment.unmapped_vocalization` count = 0 in a 30-min
  conversation.
- Operator-rated naturalness ≥ a hand-tuned threshold (e.g., 4 / 5
  on a quick rubric covering "metronomic", "robotic", "expressive",
  "alive"). Iterate amplitudes / timing knobs until acceptable.
- Cross-test against a captured pipeline log: ~1–2 emphasis events
  per typical sentence land as visible head-beats; no missing
  events; no over-firing on non-marked words.
- NFR5 anticipatory window: emphasis beats land within
  `(audio_anchor − 30 ms)` to `(audio_anchor − 80 ms)` p95 measured
  with an external high-speed camera or audio-synced loopback test.

**Coordinates with:** pipeline Story 6.3 lands the wire-side
change. The body's wire-format validation rejects events at
`schema_version != 3` unchanged; the new tag is additive within
version 3. **Single-host posture (FR62) is required** — cross-host
clock skew exceeds the NFR5 window over Wi-Fi.

**Reading:**
- [decision-records.md](decision-records.md) §DR-002 *Decision (frozen)* — the full layered model.
- [decision-records.md](decision-records.md) §DR-004 — wire-shape resolution and why this collapses to one event per emphasis.
- [olaf-embodiment-brief.md](olaf-embodiment-brief.md) §"v2 head-motion realizer" — section sketches the consumer-side layered model.
- [olaf-embodiment-brief.md](olaf-embodiment-brief.md) Appendix A.6 + A.7 — `SpeechEmotionPayload` + `VocalizationPayload`.
- [voice-agent-pipeline.md](voice-agent-pipeline.md) §17.2 `VocalizationPayload` — the typed schema.

**Size:** 3–5 weeks (the foundational realizer + emphasis cue + style overlay + explicit gestures + polish — multiple animation behaviours to author + soak-tune).

---

### Body Story 6.4: Procedural eyes channel + soak participation + cross-project sign-off

**Pipeline counterpart:** pipeline Story 6.4 — instrumentation + soak +
embodiment-brief amendment review.

**Body work:** **two pieces — eyes channel and soak coordination.**

**(a) Procedural eyes channel.** Distinct from the head channel.
Pupil movement, blink rate, look-away-while-`activity=working`,
glance-on-user-speech, slow-blink-on-`mood=sleepy`, wide-eyes-on-
`speech_emotion=surprised` — all driven body-side off the
`activity` + `speech_emotion` events the body already subscribes
to. No camera input, no new pipeline data. Renderer config lives
in `embodiment_map.yaml`'s eye-state blocks (already present in
the standing brief's Appendix B.2 example).

**(b) Soak participation.** Run the body alongside the pipeline
during pipeline Story 6.4's 7-day soak. The body's responsibility:
- Capture any `embodiment.unmapped_vocalization` WARN logs (should
  be zero after Story 6.3's mapping update — non-zero indicates
  drift between pipeline tag set and body's mapping).
- Capture operator-rated naturalness data points over the soak
  window (the standing brief calls out "operator-rated naturalness
  of the resulting head motion" as the final acceptance test).
- Iterate animation tuning if drift surfaces — amplitudes,
  anticipation lead, substrate density.
- Co-sign the cluster's release tag with the pipeline; verify the
  standing brief still reads correctly against the implemented
  renderer and amend in the same commit if anything drifted (the
  pipeline-side Story 6.4 owns the spec-as-contract review; the
  body confirms accuracy from its side).

**Acceptance:**
- Eye channel covers all `activity` states + all 12 first-class
  `speech_emotion` names with target states.
- Soak completes with `embodiment.unmapped_*` count = 0 and
  operator-rated naturalness within target.
- Joint release tag cut with the pipeline at soak completion.

**Coordinates with:** pipeline Story 6.4 owns the soak script,
the `turn.complete` instrumentation, and the
`olaf-embodiment-brief.md` amendment review. The body is the
consumer side of all three.

**Reading:**
- [decision-records.md](decision-records.md) §DR-002 — "Eyes — separate procedural channel" paragraph.
- [olaf-embodiment-brief.md](olaf-embodiment-brief.md) §"v2 head-motion realizer" — "Eyes" paragraph.
- [olaf-embodiment-brief.md](olaf-embodiment-brief.md) §Appendix B.2 — eye-state config in the example.

**Size:** 1–2 weeks (eyes channel) + 7-day soak window (mostly
running + observation, not implementation).

## Wire deltas (the minimal contract update)

What actually changes on the wire for the body to consume:

| Surface | v1 (today, schema-3) | v2 (Epic 6, schema-3) |
|---|---|---|
| `VocalizationPayload.tag` | `Literal["laughter", "sigh", "gasp", "clears_throat", "nod", "shake"]` — 6 tags | adds `"emphasis"` → 7 tags. Additive Literal extension. |
| `schema_version` | 3 | **3** (unchanged — additive per CLAUDE.md rule 6 + DR-004) |
| Other event payloads | unchanged | unchanged |
| Number of topics | 4 (`mood`, `activity`, `speech_emotion`, `vocalization`) | 4 (unchanged — the per-segment timing payload that DR-002 originally leaned toward is **closed out by DR-004**; the emphasis cue rides the existing `vocalization` topic) |
| QoS | per-topic, unchanged | unchanged |

That is the **entire** wire delta. Anything else the body project
"sees" different in v2 is internal animation work, not a contract
update.

## Renderer mapping update (`embodiment_map.yaml`)

Diff against the v1 7-tag `embodiment_map.yaml`:

```yaml
# vocalization:  (v1 — 6 tags)
+   emphasis:
+     gesture: head_beat           # snappy nod-shaped, smaller than [nod]
+     audio_asset: null            # tts_supported: false on the wire
+     visible_only: true           # gesture cue — never play audio
+     amplitude_source: "speech_emotion+mood"   # consumer-side join at render time
```

Startup-validation rule update (B.3 in the standing brief):
- v2: assert all **7** canonical `vocalization` tags covered.
- v2: assert `visible_only: true` on **all three** gesture cues —
  `nod`, `shake`, `emphasis`.

That is the entire YAML delta. All other entries unchanged.

## Coordination with the pipeline

| Pipeline (Epic 6) | Body (matching number) | Coordination point |
|---|---|---|
| Story 6.1 — Cartesia SSE→WS + word `timestamps` capture + TTFB spike | **Body Story 6.1** — regression check | The WS migration is pipeline-internal. Body verifies that the event stream it consumes is unchanged in shape (only timing shifts allowed). |
| Story 6.2 — Cached opener system + Cartesia overlap | **Body Story 6.2** — regression check | Pipeline-internal. The body doesn't see openers as events — they play through the cached-audio path before Cartesia synthesis fires. Body verifies idle-during-`working` behaviour still reads correctly with the shorter `working` window. |
| Story 6.3 — Emphasis as 7th vocalization tag | **Body Story 6.3** — head-motion realizer + emphasis cue + mapping update | Pipeline ships the wire change first; body silently no-ops on unmapped `emphasis` (logs `embodiment.unmapped_vocalization` WARN + plays `default_vocalization` fallback) until body Story 6.3 lands. Body Story 6.3 is the bulk of v2 body work. |
| Story 6.4 — Instrumentation + soak + embodiment-brief amendment review | **Body Story 6.4** — eyes channel + soak participation + sign-off | Both projects run during the 7-day window. Pipeline operator captures `turn.complete` instrumentation; body captures unmapped-vocalization WARNs + operator naturalness ratings. Joint sign-off + tagged release. |

**Single-host posture (FR62).** v2 head motion requires
single-host deployment — cross-host clock skew exceeds NFR5's
30–80 ms anticipatory window over Wi-Fi. The body and the pipeline
co-locate on the same DDS host. The brief's "Single-host
deployment is the default" stands; multi-host head motion is
deferred until clock-sync work lands.

## Out of scope

Explicitly NOT in this v2 body work (DR-002 §"Parked extension" + §"Out of scope"):

- **Camera gaze-following.** The OAK-D is mounted but the
  gaze-tracker is parked. The realizer's base-orientation slot is
  designed as **pluggable** so the OAK-D can drop in later, but
  Body Story 6.3's base orientation is scripted idle, not camera.
- **Mouth / jaw lip-sync.** Would consume `phoneme_timestamps` from
  Cartesia (which Story 6.1 captures only "if zero additional cost"
  — kept future-proof but not consumed in v2). A future lip-sync
  project warrants its own decision record.
- **Cross-host head motion.** FR62 defers until clock-sync work
  lands.
- **Anything that writes back to the pipeline.** The body never
  publishes on the pipeline's four topics. Touch sensor input,
  camera-based user-expression reading — all separate projects on
  their own future topics.
- **Hailo-8L acceleration.** Only justified if a measured embodiment
  workload (gaze, face detection, on-body emotion classifier)
  actually needs it. The animation loop is not GPU-bound — keep it
  CPU-only.

## Reading list (in suggested order)

1. **[olaf-embodiment-brief.md](olaf-embodiment-brief.md)** — read the whole thing; it's the standing contract.
2. **[decision-records.md](decision-records.md) §DR-002** — the head-motion design rationale and the layered model.
3. **[decision-records.md](decision-records.md) §DR-004** — the wire-shape resolution (why emphasis is a `vocalization`, not a new topic).
4. **[epics.md](epics.md) §Epic 6 + §Story 6.3** — the pipeline-side surface delta you're consuming.
5. **[voice-agent-pipeline.md](voice-agent-pipeline.md) §17.2 `VocalizationPayload`** — the typed schema with the v2 7-tag set.

## Risks (body-side, v2-specific)

- **Naturalness tuning is the hard part.** Authoring the layered
  realizer such that it actually reads as natural rather than
  bobblehead or jittery is iterative. Plan for soak time and
  amplitude/timing knob tuning, not just first-pass implementation.
  Operator subjective rating is the final acceptance test, not unit
  tests.
- **Vocabulary lag during the deployment gap.** Between pipeline
  Story 6.3 landing and body Story 1 landing, the body emits
  `embodiment.unmapped_vocalization` WARN every emphasis event.
  Acceptable for the v2 implementation window if the gap is short;
  consider gating Story 6.3's *enablement* (a config flag) on body
  readiness if the gap stretches.
- **NFR5 anticipatory window across the body's render loop.** A
  servo loop running at 50 Hz has 20 ms tick granularity; the
  ~30–80 ms anticipatory window is comfortably above that, but
  scheduling jitter can erode it. Measure during the soak; tune
  `emphasis_anticipatory_ms` if needed.
- **Mood / speech_emotion join.** Body Story 6.3's amplitude scaling
  depends on the body having current `mood` + `speech_emotion`
  state cached. Both are latched topics for `mood` and per-segment
  cadence for `speech_emotion` — straightforward to track. Worth
  unit-testing the join logic separately from the render loop.

---

*This brief lives in the `voice-agent-pipeline` repo because the wire
contract it references is authored here — the brief travels WITH the
contract. When the body project executes its v2 sprint, this file
should be copied to its `docs/v2-sprint.md` (or similar) and kept in
sync via a tagged release of the pipeline.*
