# Decision Records — voice-agent-pipeline

An append-only log of significant design decisions. Each record is self-contained:
context → empirical evidence → options → decision → rationale → consequences →
open questions. Newest records go at the top. Records are immutable once frozen;
to change a decision, add a new record that supersedes the old one (and note the
supersession in both).

> These records are written to be citable. Where a record makes an empirical
> claim, the measurement methodology is stated inline so the number can be
> reproduced or quoted in a paper.

**Promotion path.** These records are *rationale*, not a plan. A frozen record
feeds a deliberate **design pass → PRD update → epics/stories** — it is not turned
into epics directly. DR-001/002/003 (the v2 expression work) are captured here and
**scheduled for later**; none are in the current sprint. The 2026-05-28 design pass
promoted them in-place to PRD §"Conversational Openers (v2)" + §"Speech Timing &
Emphasis (v2)" and epics.md Epic 6 (a single cohesive 4-story epic — initial draft
proposed Epic 6 + Epic 7 but consolidated 2026-05-28 on user pushback against
over-decomposition); DR-004 (below) is the small follow-on record that closed
DR-002's open wire-shape question during that pass.

---

## DR-004 — Emphasis renders as a 7th vocalization tag (closes DR-002 wire shape)

- **Date:** 2026-05-28
- **Status:** Frozen 2026-05-28
- **Author:** Kamal (with Claude as design-pass partner)
- **Closes:** DR-002 §"Options & tradeoffs Fork 3" + §"Open questions" — the wire
  shape for the per-segment timing + emphasis payload. DR-002's *layered head-motion
  model* and *prosody-rhythm-from-Cartesia-timestamps + LLM-marked emphasis*
  decisions stand; only the *wire shape that delivers emphasis to the body* is what
  this record settles.
- **Touches:** `expression_map.yaml` (`vocalizations:` list grows 6 → 7),
  `schemas/vocalization_event.py` (Literal extension), `splitter/segmenter.py`
  (emit `vocalization(tag="emphasis")` at the matched word's audio anchor),
  `tts/cartesia.py` (WebSocket `timestamps` capture, still required), Talker
  system prompt (emphasis-mark syntax), `olaf-embodiment-brief.md` Appendix A.7
  (vocabulary completeness).

### Summary

DR-002 left the wire shape as an open question with three candidate forms (per-word
TimingPayload on a new topic / extension of `vocalization` / extension of
`speech_emotion`). The 2026-05-28 design pass collapsed them by recognising that
emphasis is structurally identical to `[nod]`/`[shake]`/`[laugh]` — a punctuated,
audio-anchored, body-renders-it cue. So **emphasis becomes the 7th vocalization
tag** (`tts_supported: false`), the per-segment timing payload disappears from
the wire, and the schema stays at version 3 (additive vocabulary, no bump per
CLAUDE.md rule 6).

### Context — what DR-002 left open

DR-002 froze the **layered head-motion model** (base → rhythm → emphasis → style
→ explicit gestures → anticipation → stochasticity) and the **Cartesia
WebSocket** migration that delivers word `timestamps`. It also leaned toward a
**per-segment timing + emphasis payload** that the body would consume to schedule
beats locally (Fork 3). The exact wire shape was flagged in *Open questions* and
*Consequences* — "likely a new audio-anchored field/topic; check CLAUDE.md rule 6
(optional fields are forward-compat; a new topic is not a `schema_version` bump
by itself)."

### What the design pass surfaced

Two collapses on inspection:

1. **The body must not move on every word — that's the bobblehead failure mode
   DR-002 explicitly rejects.** Rhythm-from-word-timestamps was framed as a
   "beat skeleton," but the body's policy is *most words: nothing*. The
   word-timing substrate is therefore decorative; the body can substitute its
   own stochastic micro-timing and reach the same naturalness (research-supported
   — naturalness comes from stochasticity, not literal word alignment).
2. **Emphasis is structurally identical to existing vocalization cues.** Once
   you drop the rhythm-substrate framing, what crosses the wire for head motion
   is one signal per emphasis mark: "do a punctuated cue here, audio-anchored,
   render at consumer discretion." That is exactly what `vocalization` already
   carries for `[nod]`, `[shake]`, `[laugh]`, `[sigh]`, `[gasp]`,
   `[clears_throat]`.

| Property | Existing `vocalization` | Proposed `emphasis` cue |
|---|---|---|
| Cadence | per occurrence | per occurrence |
| Audio-anchored (NFR5) | yes (`audio_frame_id`) | yes |
| Producer renders text→audio? | yes when `tts_supported`; else no | no (`tts_supported: false`) |
| Body decides motion shape | yes | yes |
| Punctual / crisp | yes | yes |

### Decision (frozen)

Emphasis is a 7th vocalization tag.

```python
VocalizationTag = Literal[
    "laughter", "sigh", "gasp", "clears_throat",   # audio bursts (pre-existing)
    "nod", "shake",                                  # gesture cues (pre-existing, schema-3 boundary repair)
    "emphasis",                                      # NEW — prosodic accent cue
]
```

- `tts_supported: false` — the emphasis lives in the prosody of the carrier word
  rendered by Cartesia (via the LLM's emphasis mark in the text stream), so no
  separate audio asset. This mirrors `nod`/`shake` exactly.
- The pipeline emits one `vocalization(tag="emphasis", audio_frame_id=...)`
  event per emphasis mark, anchored to the word's Cartesia timestamp.
- `expression_map.yaml`'s `vocalizations:` block grows 6 → 7.
- `embodiment_map.yaml` (consumer-side) must cover `emphasis`; same
  startup-blocker discipline as the other six tags.
- **`schema_version` stays at 3** — additive Literal extension is forward-compat
  per CLAUDE.md rule 6.

### Rationale

- **Smallest viable surface.** No new topic, no new payload field, no schema
  bump, no per-word wire cadence. The 5th-topic / per-segment-TimingPayload
  options that Fork 3 leaned toward (and the alternative additive-field options)
  all proposed more wire than the body actually consumes.
- **Semantic fit.** The vocalization topic's job description is "punctuated,
  audio-anchored, body renders." Emphasis fits without smudging the boundary
  (unlike adding timing arrays to `speech_emotion`, which already cadences
  per-segment for *style* not *beat*).
- **Sparse-but-alive motion density matches research.** With rhythm-as-data
  dropped, per-sentence body motion is ~1–3 `speech_emotion` style updates +
  1–2 emphasis nods + occasional `[nod]`/`[shake]` + mood-base ambient. That
  matches co-speech-gesture research (~1 beat gesture per 1–2 prosodic phrases).
  If soak proves too sparse, the lever is "prompt the LLM to mark more
  liberally," not "ship more wire."
- **Producer/consumer split preserved.** The pipeline ships *data it alone has*
  (the join of LLM emphasis marks × Cartesia timestamps, resolved to one event
  per emphasis at the word's audio anchor); the body owns *how to move*
  (nod-shape, amplitude scaling by `speech_emotion` + `mood`, anticipation,
  stochasticity, multi-axis).
- **Cartesia WebSocket migration still pays off.** The SSE→WS swap is still
  required — for the *internal* join (the pipeline needs `timestamps` to anchor
  the emphasis event to the matched word), for DR-001's TTFB investigation, and
  for any future use of `phoneme_timestamps` (mouth/jaw sync, v2+).

### Tradeoffs accepted

| Lost (vs Fork 3's per-segment payload) | Why acceptable |
|---|---|
| Body cannot use per-word rhythm timing data to phase its own micro-motion | The body's policy is "most words: nothing"; stochastic substitution gives equivalent naturalness (research-supported). |
| If future work wants very-subtle per-word jitter modulated by word density (e.g., faster-speech → more eye micro-saccades), the data isn't on the wire | Can be re-added later additively (extra optional `word_count: int` on the vocalization, etc.) without a schema bump. Cross that bridge if soak reveals the need. |
| Pipeline can't ship Cartesia `phoneme_timestamps` for v2 lip-sync via this channel | A future lip-sync project would warrant its own decision record and likely its own topic (high-cadence, distinct semantics). Out of scope here. |

### Consequences / implementation notes

- **Talker prompt** (`prompts/talker_system.md`): teach a single emphasis-mark
  syntax (e.g., `*word*` or `<em>word</em>` — final syntax decided in the
  Epic 6 / Story 6.3 that wires the prompt). Constrain density toward "1–2 per
  sentence; only words a thoughtful speaker would acoustically stress."
- **Cartesia handling.** If Cartesia honors emphasis tags in its text input
  (verify in Epic 6 / Story 6.1's WS spike), pass the marked form through so the audio
  *also* gets prosodic stress. If Cartesia ignores them, strip marks before
  send — the body still nods correctly off the timestamp-joined event; only the
  audio prosody is lost. Either outcome is functional.
- **Splitter** (`splitter/segmenter.py`): the LLM's emphasis-marked text is
  parsed pre-TTS (same path as existing vocalization tags); marked-word indices
  are remembered and joined against Cartesia's `timestamps` from the WS stream;
  the join produces one `vocalization(tag="emphasis", audio_frame_id=...)` per
  emphasis at the word's anchor.
- **Embodiment side** (`embodiment_map.yaml`): bind `emphasis` to a nod-like
  beat; amplitude/style scale by current `speech_emotion` and `mood`. Startup
  loader rejects an `embodiment_map.yaml` that doesn't cover `emphasis` once
  schema rolls out — same discipline as the other six tags.
- **Schema_version stays at 3.** Bump history continues: `1 → 2` (Story 3.4
  topology change), `2 → 3` (sprint-change-proposal-2026-05-10 boundary repair).
  This DR is additive (forward-compat per CLAUDE.md rule 6), no `3 → 4`.

**Implementation landed by:** Story 6.3
(`6-3-emphasis-vocalization-wiring`) on 2026-05-29. As-built deltas from the
frozen design above, recorded per NFR26 (the DR is immutable; this annotation
is the sanctioned channel): (1) the wire model's `VocalizationPayload.tag`
stays an **open `str`**, NOT tightened to the `VocalizationTag = Literal[...]`
shown in §"Decision (frozen)" — `expression_map.yaml` is the vocabulary gate,
and tightening to a Literal would be a breaking change for any pre-Epic-6
consumer (open-set is the topic's principle). (2) Emphasis syntax resolved to
`*word*` (Open question #1). (3) The splitter join lives in the runtime
(`sequential_loop.py:_publish_emphasis_events`), not `segmenter.py` — the
segmenter records `Segment.emphasis_word_indices`; the runtime joins them ×
`SegmentTiming` after each segment's synth drains, since per-word timing is
only reliably available post-drain. `audio_frame_id` shape:
`seg-<N>-w-<start_ms>`. NFR5 anticipatory-window verification for `emphasis`
deferred to Story 6.4 soak (the body anchors its lead to the `audio_frame_id`
offset; the pipeline ships the anchor, not the motion).

### Open questions

- **Emphasis-mark syntax** (final form — `*word*` vs `<em>word</em>` vs custom).
  Resolved by Epic 6 / Story 6.3 (the prompt story); trivially reversible.
- **Cartesia emphasis-input support.** Epic 6 / Story 6.1's WS spike (which is also
  DR-001's TTFB spike) confirms whether the marks survive to audio. Affects only
  the *audio half* of the design; the body half works regardless.
- **Emphasis density per turn.** Linguistic prior ≈ 1–2 per sentence with a
  conservative prompt. Tune empirically during Epic 6 / Story 6.4's soak; lever is the
  prompt, not the wire.

### Supersession

This record **closes** DR-002's wire-shape open question. DR-002 itself is not
superseded — its layered head-motion model, WS-migration decision, and
single-host topology lean all remain frozen. Future records that change the
emphasis wire shape would supersede DR-004; this record does not preclude that.

---

## DR-003 — Live interaction dashboard reads from logs (separate telemetry consumer)

- **Date:** 2026-05-26
- **Status:** Direction frozen; two sub-decisions leaning (transport, sink timing)
- **Author:** Kamal (with Claude as brainstorming partner)
- **Touches:** a **new, separate dashboard project** (NOT the pipeline repo);
  *optionally* a new `events.jsonl` structlog sink in the pipeline (and body) if
  promoted later. **No change to the DDS publisher contract.**

### Summary

**Problem.** A web screen showing live interactions in several sections: what was
said (transcript), the response, and expression/body signals ("woke up", etc.).

**Outcome.** Build it as a **separate consumer that tails the structured JSON
logs** — not code in the pipeline, and not (initially) via new ROS topics.
Everything needed is **already logged**, so this is **zero pipeline change**, the
**best privacy posture**, and it **unifies pipeline + body signals** (both simply
log; the dashboard tails N files). Promotion path to a stable `events.jsonl` sink
exists if log-format coupling bites.

### Context — the problem

A live observability screen: turn-by-turn transcript ↔ response, current
expression state, and **body-emitted signals** like "woke up". Kamal first
suggested new ROS topics, then proposed the dashboard **read from logs** given
their locations.

### What data exists today

| Data | Where it is today | Cost |
|---|---|---|
| mood, **activity** (= "woke up"/listening/working/speaking) | DDS `activity` + logged | **free** |
| speech_emotion, vocalization (`[nod]`/`[shake]`) | DDS + logged | **free** |
| transcript ("what was said"), response | **logs only** (`stt.transcript`→`heard`, `talker.responded`) | free *from logs* |
| latency (STT/TTFB/gap) | logs (`ttfb_ms`, timestamps) | **free** |
| body's *own* confirmation ("wake anim done") | doesn't exist yet → body's future log | free *once body logs* |

The publisher has exactly four publish methods (mood/activity/speech_emotion/
vocalization); transcript & response are **not** on the wire — only in logs.

### Options & tradeoffs

**Fork 1 — data source:**

| Option | Pipeline change | Privacy | Real-time | Contract stability | Verdict |
|---|---|---|---|---|---|
| New ROS topics (`transcript`/`response`) | yes + schema | **raw transcript on wire** (rule 8) → must config-gate | best | versioned (schema-3) | reject for now |
| **Tail existing logs** | **none** | best (no new surface; INFO `heard` already accepted) | ~tens of ms (fine) | **unversioned** (silent-break risk) | **CHOSEN (now)** |
| Dedicated `events.jsonl` sink | small (one handler) | same as logs | ~tens of ms | **versioned, stable** | **promotion path (later)** |

**Fork 2 — browser ↔ data transport** (leaning **custom**):

| Option | Custom code | Deps | Control of JSON shape |
|---|---|---|---|
| rosbridge_suite (roslibjs/WS) | none | full ROS web stack | raw topic JSON |
| **Custom WS/SSE bridge** (FastAPI/Starlette) | small | minimal | **full** (events are JSON already) |

### Decision (tradeoff-driven outcome)

**Decided now:**
- Dashboard is a **separate consumer project**, agnostic-publisher boundary
  preserved (the "future telemetry consumer" the embodiment brief anticipated).
- It **tails the structured JSON logs** — `voice-agent.log` (INFO).
- **Body signals arrive via the body's own log**, not a new ROS topic — this
  dissolves the single-writer/new-topic question for the dashboard entirely.
- Sections: (1) conversation (transcript ↔ response), (2) expression state
  (mood/activity/emotion/vocalization), (3) body status (from body log; future),
  (4) latency/metrics, (5) raw event timeline.

**Leaning (open):**
- Transport → **custom WS/SSE bridge** (Fork 2).
- **Tail human log now → promote to `events.jsonl` later** if format-coupling
  bites (Fork 1).

### Rationale

- Pre-blessed by the architecture; keeps the pipeline consumer-agnostic.
- The logs already carry every required field, so the MVP is zero pipeline change.
- Reading the INFO log (deliberate `heard` alias) keeps the **existing** privacy
  exposure level — strictly better than publishing raw transcripts on the wire.
- "Body must emit signals" reduces to "body writes its own log" — no new topic,
  no single-writer concern, both processes feed the dashboard the same way.

### Consequences / implementation notes (the gotchas)

1. **Rotation-aware tail.** The pipeline uses `RotatingFileHandler`; the tailer
   must follow across roll-over (re-open on rename/truncate), not just `seek(end)`.
2. **Log format is an unversioned contract.** Renaming a log event silently
   breaks the dashboard (unlike schema-3 on the wire). Document the consumed event
   set; promote to `events.jsonl` if this bites.
3. **Read `voice-agent.log` (INFO), not `debug.log`** — debug can carry more under
   `LOG_LEVEL=DEBUG`; reading INFO keeps the accepted exposure level.
4. **Turn reconstruction in the dashboard** — group `vad.utterance.captured →
   stt.transcript → talker.responded` into turns (the same grouping used for the
   DR-001 latency analysis).
5. **Multi-host (future):** a body on a separate Pi has its log on the Pi —
   needs remote access (NFS/ssh/shipper). Single-host = local paths (matches the
   DR-002 single-host lean).
6. **Passive consumer:** the dashboard must never affect the pipeline (separate
   process; the pipeline's fail-fast does not extend to it). Bind to
   localhost/LAN — transcripts are visible; no cloud.

### Open questions

- Transport: custom WS/SSE vs rosbridge (leaning custom).
- `events.jsonl` from day one, or only on promotion?
- Section layout / which metrics to surface live.
- Remote body-log access when multi-host (future).

### For the paper

- The dashboard doubles as the **live demo + metrics panel** — surfacing the
  STT/TTFB/gap numbers (DR-001) and head-motion timing (DR-002) for figures and
  user-study facilitation.
- Session capture/replay from the log stream supports the **naturalness
  evaluation** shared across DR-001/DR-002.

---

## DR-002 — Speech-synchronized head motion (layered prosody + semantics model)

- **Date:** 2026-05-26
- **Status:** Frozen 2026-05-26 (emphasis resolved to LLM-marked; gaze scoped to
  procedural head/eyes with a pluggable base-orientation seam; camera
  gaze-following parked as a future extension). Implementation pending.
- **Author:** Kamal (with Claude as brainstorming partner)
- **Touches:** `tts/cartesia.py` (WebSocket transport + timestamp capture), the
  Talker system prompt (emphasis marks), the wire schema (a per-segment timing +
  emphasis payload — evaluate against CLAUDE.md rule 6), `sequential_loop.py`, and
  the **olaf-embodiment** project (head + eye animation — separate repo, *owns*
  the motion). `architecture.md` / `olaf-embodiment-brief.md` to be updated when
  implemented. (No PCM F0/RMS extraction — emphasis comes from the LLM; see
  *Decision*.)

### Summary — what the research and the API capability decided

**Problem.** During speech, OLAF's head should move in a pattern that depends on
(a) speech emotion, (b) whether it is speaking or not, and (c) word onset and
word-to-word transitions.

**What the research changed.** Human head motion tracks **prosody** (pitch/F0 +
energy + rhythm) **and semantics**, *not words uniformly*. Driving the head off
word boundaries alone produces a metronomic **"bobblehead"** — the opposite of
natural. The state of the art is a **hybrid**: prosody supplies rhythm/emphasis,
semantic context supplies *meaning*. Two findings shaped the design directly:
(i) head motion that **leads** the utterance is rated more natural than
simultaneous motion, and (ii) perceived naturalness correlates with
**likeability and perceived intelligence** (the same axes as DR-001's filler
study).

**What the API capability decided.** Cartesia hands you the *rhythm* slice of
prosody as data (`timestamps` = word, `phoneme_timestamps` = finer) but **not**
pitch or energy. We originally planned to extract F0/RMS from the PCM for
emphasis — but the accepted simplification (see *Decision*) is to have the **LLM
mark emphasis**, which Cartesia renders *and* the body nods on. So prosody splits
by source: **rhythm from Cartesia timestamps, emphasis from LLM marks (timed via
those timestamps), style/meaning from `speech_emotion` + `[nod]`/`[shake]` tags**
— no audio analysis needed.

**Outcome (frozen).** A **layered model** with a **pluggable base-orientation**
(scripted now; camera gaze-following later) + rhythm + LLM-marked emphasis + style
+ semantics + anticipation + stochasticity. The **body owns the animation** (head
*and* procedural eyes); the **pipeline ships the timing + emphasis data** it alone
has, over Cartesia **WebSocket** (serves this *and* DR-001).

### Enabling capability — what Cartesia provides

Cartesia's streaming API emits message types `chunk`, `timestamps`,
`phoneme_timestamps`, `error`, `done`.

| Prosody slice | From Cartesia? | How obtained |
|---|---|---|
| Rhythm / timing | ✅ as data | `timestamps` (word) + `phoneme_timestamps` (finer) |
| Pitch (F0) — melody | ❌ | extract from PCM (pitch tracker, e.g. YIN/pYIN) |
| Energy / stress — emphasis | ❌ | compute from PCM (RMS — trivial) |
| Full expressive delivery | ✅ baked in audio | present in `chunk`, not separable |

Caveats: timestamps are **WebSocket** message types; the current code uses SSE
(`generate_sse`) and **drops** `timestamps` events at `cartesia.py:129`. On the
`sonic` model, timestamps cover `en/de/es/fr` only (all languages on
`sonic-preview`). `phoneme_timestamps` are finer than needed for head motion but
are the right primitive for future **mouth/jaw lip-sync**.

### Research / prior art (web survey, 2026-05-26)

- **Head motion ⟷ prosody (foundational).** Busso et al. (2005) synthesize
  natural head motion from acoustic prosodic features (F0 + energy + derivatives).
  Reported correlations are high at sentence level (~0.83) but **asymmetric and
  loose predictively** (F0→head ~0.25–0.50) — i.e. head motion is only *partly*
  determined by prosody, so a deterministic map looks mechanical; **stochasticity
  is required.**
- **Prosody is rhythmically right but semantically empty — the SOTA fix is
  hybrid.** Sadoughi & Busso, "Speech-Driven Animation with Meaningful
  Behaviors": constrain the prosody-driven model with *contextual meaning*.
  *"A current limitation is that head motions are driven by prosody rather than
  semantic context."* OLAF already produces that semantic context
  (`speech_emotion`, `[nod]`/`[shake]`), so it is unusually well-placed for the
  hybrid.
- **Nods land on stress/accents, tilts at phrase boundaries; nod+tilt beats
  nod-only** for naturalness (HRI dialogue-act models). Confirms: word boundaries
  give rhythm, but F0/energy decide *which* beats move and how.
- **Timing: lead, don't sync.** Robot HRI study — head motion *prior to* the
  utterance rated more natural than simultaneous; naturalness correlated with
  likeability and perceived intelligence. Extends NFR5's ~30–80 ms anticipatory
  window (head may want *more* lead than emotion-pose).
- **Heavy neural talking-heads are the wrong shape here.** Audio2Head, ARTalk,
  Audio2Face-3D, SyncAnimation target high-DOF screen avatars / 3D faces, often
  near-offline. OLAF is a **physical ~2–3-DOF head, hard real-time** — a
  lightweight prosody→few-DOF mapping (rule-based + stochastic, semantically
  conditioned), closer to the Pepper/humanoid **beat-gesture** work, fits.

**Sources:**
[Busso — prosodic head-motion synthesis (2005)](https://www.carlosbusso.com/publications/Busso_2005_2.pdf) ·
[Sadoughi & Busso — meaningful behaviors](https://arxiv.org/pdf/1708.01640) ·
[Prosody-driven head-gesture animation](https://www.researchgate.net/publication/224711168_Prosody-Driven_Head-Gesture_Animation) ·
[Head motion from waveforms (2024)](https://www.sciencedirect.com/science/article/pii/S0167639324000281) ·
[Effect of robot head movement & timing on HRI (2024)](https://link.springer.com/article/10.1007/s12369-024-01196-0) ·
[Nodding/head-tilting for HRI dialogue](https://dl.acm.org/doi/10.1145/2157689.2157797) ·
[Beat gestures for social robots](https://link.springer.com/article/10.1007/s11042-021-11289-x) ·
[Co-speech gesture learning for humanoids (2018)](https://arxiv.org/abs/1810.12541) ·
[Audio2Head](https://www.ijcai.org/proceedings/2021/0152.pdf) ·
[Audio2Face-3D + Audio2Emotion](https://arxiv.org/html/2508.16401v1) ·
[Cartesia TTS WebSocket API](https://docs.cartesia.ai/2024-11-13/api-reference/tts/tts)

### Options & tradeoffs

**Fork 1 — what drives the motion** (decided):

| Approach | Naturalness | Effort | Robustness | Verdict |
|---|---|---|---|---|
| Word-timestamps only (rhythm) | low — **bobblehead** | low | high | reject (research-confirmed failure mode) |
| Audio-envelope only (RMS) | medium (energy, no linguistics) | low | high | useful *layer*, not the whole |
| **Prosody + semantics, layered** | **high** | medium | medium | **CHOSEN** |

**Fork 2 — emphasis source** (RESOLVED → LLM-marked):

| Option | Naturalness | Effort | Verdict |
|---|---|---|---|
| Rhythm + emotion only | acceptable; no real accents | low | insufficient |
| Extract RMS/F0 from PCM | true acoustic accents | medium (DSP + tuning) | rejected (audio analysis) |
| **LLM marks emphasis** | high; semantically correct | low (a tag) | **CHOSEN** |

The LLM already knows what it is emphasizing. It marks the stressed word(s);
Cartesia renders the stress **and** the body nods on the same word (timed by that
word's timestamp). One signal drives both prosody and head motion — **no PCM
F0/RMS extraction needed.**

**Fork 3 — where timing/prosody lives + wire shape** (leaning): pipeline ships a
**per-segment** timing payload (word timestamps + optional energy track),
*one event per segment* (same cadence as `speech_emotion`), and the **body
schedules the per-word beats locally** and owns the animation. Rejected:
per-*word* wire events (cadence + cross-host jitter) and pipeline-computed poses
(violates the producer/consumer boundary).

**Fork 4 — transport** (leaning **WebSocket**): SSE drops timestamps and lacks
`context_id`; WebSocket delivers `timestamps`/`phoneme_timestamps` *and* the
prosody-continuation + TTFB benefit DR-001 wants. One switch serves both records.

**Fork 5 — host topology** (leaning **single co-located host**): word-level
(~100 ms) sync survives loopback DDS but is shredded by cross-host clock skew over
Wi-Fi (NFR5 blur, tighter). Matches the embodiment brief's single-host default.

### Decision (frozen)

**Layered head-motion model** (base → overlays):
- **Base orientation — pluggable input.** Scripted idle / look-around now;
  **camera gaze-following is a parked extension** (OAK-D Pro mounted, not yet
  implemented). The seam is designed in *now* so reviving gaze is a source swap,
  not a rewrite — see *Parked extension*.
- **Rhythm** from Cartesia word timestamps (the beat skeleton).
- **Emphasis** from **LLM-marked** stressed words, timed by the word timestamp
  (the accepted simplification — no PCM F0/RMS).
- **Style + amplitude** from `speech_emotion`.
- **Explicit gestures** from `[nod]`/`[shake]`.
- **Anticipatory** (head leads audio), **stochastic** (no metronome),
  **multiple axes** (nod + tilt + turn).

**Eyes** are a separate **procedural** channel (pupil movement, blink,
look-away-while-`thinking`, glance-on-user-speech), body-side, driven off
`activity` + `speech_emotion` — **no camera, no new pipeline data.**

**Resolved forks:** transport → **Cartesia WebSocket**; emphasis → **LLM-marked**;
wire → **per-segment** timing+emphasis payload (body schedules); topology →
**single co-located host**; boundary → **body owns animation, pipeline ships data.**

**Parked extension — camera gaze-following.** The OAK-D Pro is mounted but not
implemented; revive *later* (graceful-degradation era, as a soft dep, budgeting
tuning time). When revived it becomes the **base-orientation source**, which
reintroduces: gaze↔speech servo arbitration (overlay speech on the tracked base),
the active-vision coupling (a head-mounted camera shakes its own input on big
nods), and gaze-aversion-while-thinking overriding the tracker. The OAK-D's
on-device VPU likely removes the need for the brief's proposed Hailo-8L for this
workload.

### Rationale

- Word-only motion is a research-confirmed failure (bobblehead); prosody +
  semantics is the SOTA and OLAF already holds the semantic signals others must
  infer.
- The producer/consumer split is preserved: the pipeline ships *data it alone
  has* (TTS-derived timing + extracted prosody); the body owns *how to move*.
- Anticipatory + stochastic + multi-axis are each independently
  research-supported naturalness levers.
- WebSocket is chosen once and pays off twice (this + DR-001).

### Consequences / implementation notes

- `tts/cartesia.py`: migrate SSE → WebSocket; **capture** `timestamps` (and
  optionally `phoneme_timestamps`) instead of dropping them at `:129`.
- Talker system prompt: teach an **emphasis mark** on stressed word(s) (plus the
  head-motion length cue for delegating turns). No PCM DSP step — emphasis is a
  tag, not audio analysis.
- Wire schema: add the per-segment **timing + emphasis** payload — likely a new
  audio-anchored field/topic; check CLAUDE.md rule 6 (optional fields are
  forward-compat; a new topic is not a `schema_version` bump by itself).
- olaf-embodiment (separate repo): build the layered **head** realizer
  (base-orientation input + beat scheduling + LLM-emphasis nods + emotion-scaling
  + anticipation + noise) **and** the procedural **eye** behavior; expose the
  base-orientation as a pluggable input so camera gaze can slot in later. Update
  `olaf-embodiment-brief.md`.
- Keep heavy neural talking-head models out of scope (wrong shape for a few-DOF
  physical head).

**Implementation landed by:** the pipeline (producer) half landed across
Story 6.1 (`tts/cartesia.py` WS migration + `SegmentTiming` capture) and
Story 6.3 (`6-3-emphasis-vocalization-wiring`, 2026-05-29 — the Talker
emphasis-mark teaching, the splitter parse + per-segment marked-word index
recording, and the runtime timestamp join emitting
`vocalization(tag="emphasis", audio_frame_id="seg-N-w-<start_ms>")`). The wire
shape is DR-004's resolution (a 7th vocalization tag, no new topic / payload
field / `schema_version` bump). The body-side layered head realizer
(olaf-embodiment) remains the consumer half — out of this repo's scope per the
producer/consumer split.

### Open questions

- Cross-host audio-anchor: the body's beats are scheduled relative to *audio
  start*; if the pipeline plays the audio and the body moves the head, they must
  agree on that instant. Single-host makes this tractable; multi-host needs clock
  sync (defer).
- Exact emphasis-mark syntax in the Talker prompt + how it rides the wire next to
  the word timestamps.
- (Parked) camera gaze-following revival — the arbitration / active-vision items
  under *Parked extension*.

### For the paper

- A **real-time, physical, few-DOF** companion-robot head-motion system driven by
  **TTS word timestamps + LLM-supplied emphasis and emotion** (rather than
  acoustic F0/energy analysis) — directly addressing the SOTA gap that
  prosody-driven motion lacks semantic meaning, on hardware most of the literature
  (high-DOF screen avatars) ignores.
- **Shared evaluation framework with DR-001:** naturalness → likeability +
  perceived intelligence, measured across both contributions (openers + head
  motion).
- A clean **producer/consumer** factoring: pipeline ships TTS-derived prosody
  data; body owns the animation realizer — a reusable architecture for embodied
  voice agents.

---

## DR-001 — Conversational openers replace timer-fired fillers (v2 perceived-latency masking)

- **Date:** 2026-05-26
- **Status:** Frozen (design); implementation pending
- **Author:** Kamal (with Claude as brainstorming partner)
- **Supersedes:** the Story 5.5 filler design (`audio/filler.py`, `min_pause_ms` timer)
  for v2. Story 5.5 remains the shipping behavior until v2 lands.
- **Touches:** `audio/filler.py`, `audio/cached.py`, `audio/regenerate.py`,
  `sequential_loop.py`, the Talker system prompt, and (TBD) the cached-audio
  manifest schema. `architecture.md` / `epics.md` to be updated when implemented
  (CLAUDE.md rule 9).

### Summary — grounded in captured production numbers

This decision was driven end-to-end by **latency captured from production logs**
(`logs/voice-agent.log`, n = 308 clean answer-turns on the current system; full
methodology under *Empirical evidence*). The captured numbers defined the
problem and shaped the fix:

- **The problem the numbers exposed.** The median turn has a **~3.0 s** gap from
  end-of-speech to the spoken answer. The ~0.83 s filler covers only the *front*
  of that gap, leaving **~1.5 s of dead air after the filler ends**. Worse, the
  answer is actually *computed by ~0.9 s* but not *heard until ~3.0 s* — because
  the filler is **serialized in front of the TTS request**, so on **75 % of
  turns the filler actively *adds* latency** rather than only masking it.
- **The fix the numbers motivated.** (a) Remove the serialization so the answer's
  TTS synthesis overlaps the opener — a free ~1 s win (3.0 s → ~1.7-2.0 s).
  (b) Replace the random mood-keyed filler with a **context-selected,
  function-bucketed cached "opener"** chosen by the LLM, played instantly
  (~0.6 s) while the real answer synthesizes underneath, for a seamless handoff
  with zero dead air.
- **The keystone the numbers point to.** Cartesia TTFB (~1.07 s median) is now
  the dominant remaining cost and determines how tight the handoff can be — and
  whether a fully cache-free design ever becomes viable.

Everything below is the evidence and reasoning behind those three statements.

### Context — the problem

The v1 "thinking filler" (Story 5.5) works but reads as *designed*, not natural.
Two complaints from the operator:

1. **Generic / wrong words.** The filler has no relationship to the question —
   it is a random pick from a small mood-keyed bucket.
2. **Fires on the wrong signal.** It fires on a latency *timer*, not on whether
   the answer is actually going to be long.

The goal for v2 is expression that feels natural, starting with the filler.

### How v1 actually works (shared baseline)

- **Trigger is a blind timer, not a content decision.** On VAD end-of-speech,
  `sequential_loop` spawns `maybe_play_filler`, which sleeps `min_pause_ms`
  (600 ms). If real audio is ready before then it is suppressed; otherwise a
  filler plays. (`sequential_loop.py:266`, `audio/filler.py:103`.)
- **It fires *before* STT returns.** The filler task is spawned at
  `sequential_loop.py:266`; `stt.transcribe()` is awaited at `:278`. With cloud
  STT, transcription typically outlasts the 600 ms timer, so the filler plays
  *before the system knows what the user said* — the structural reason it cannot
  be question-aware in v1.
- **Selection is random.** `pick_filler` does `random.choice` over the
  mood-matched bucket (fallback `calm`), minus a last-N ring buffer
  (`audio/filler.py:49`).
- **Fillers are fixed cached WAVs** — the identical waveform plays every time a
  given phrase is chosen.

### Empirical evidence — latency decomposition

**Methodology.** Parsed `logs/voice-agent.log` (structlog JSON, one event per
line). Reconstructed turns by treating each `vad.utterance.captured` as a turn
boundary and pairing the subsequent `stt.transcript`, `filler.picked`,
`talker.completion_streaming`, and `tts.first_frame` events within the turn.
Deltas are computed from each event's `timestamp` (single clock, so intra-turn
diffs are robust). The full log spans **2026-05-05 → 05-24, 50 sessions**, and
straddles the Groq STT swap of 2026-05-12 (before which on-device Whisper made
STT multi-second). To characterize the *current* system, results below are
filtered to **≥ 2026-05-20** and to **"clean answer-turns"** — turns that
produced a real TTS answer with **no tool call** and **no clarification
short-circuit** (n = 308). `ttfb_ms` is read directly from the field emitted at
`tts/cartesia.py:145`. TTFT (LLM time-to-first-*token*) is **not** logged and is
inferred; the LLM figure below is time-to-full-completion, an upper bound on
TTFT.

**Current-system latency (n = 308 clean answer-turns), milliseconds:**

| Segment | p25 | median | p75 | p90 |
|---|---|---|---|---|
| STT (vad → transcript) | 378 | **438** | 507 | 622 |
| LLM (transcript → completion) | 369 | **461** | 740 | 5381 |
| Cartesia TTFB | 796 | **1067** | 1636 | 2185 |
| Filler duration | 557 | 835 | 1021 | 1300 |
| Time-to-first-**sound** | 606 | **606** | 607 | 608 |
| **Full gap** (vad → real audio) | 2520 | **2963** | 3553 | 4074 |

**Two findings drove the decision:**

1. **The answer is ready at ~0.9 s, but the user hears it at ~3.0 s.**
   STT (~0.44 s) + LLM (~0.46 s) ≈ 0.9 s. The remaining ~2 s is Cartesia TTFB
   (~1.07 s) plus a structural waste (below). The median full gap is ~3.0 s —
   far larger than the ~0.83 s filler, so even today there is **~1.5 s of dead
   air *after* the filler finishes and before the answer begins.** The filler
   masks the *front* of the gap, not the back; the trailing silence is what reads
   as awkward.

2. **The filler is serialized in front of Cartesia, so it can *add* latency.**
   `sequential_loop.py:696-714` does `await filler_task` *before*
   `tts.synthesize()`. The Cartesia request — the single fattest chunk — is
   therefore issued only *after* the filler finishes playing. On **227 of 303
   turns (75 %)** the LLM had finished its answer *before the filler stopped
   playing*, yet the pipeline still waited out the filler before calling Cartesia.
   The serialization is only legitimate for the audio *device* (one shared
   PyAudio output stream); it is needlessly applied to the *network* request.

### Options considered

| Option | Onset | Question-aware | Cost | Verdict |
|---|---|---|---|---|
| **A. Timer + random cache** (v1) | 0.6 s, guaranteed | no | free | baseline; the problem |
| **B. Main LLM emits a filler tag** → cached audio | ~0.6–0.7 s (STT+TTFT) | yes (knows question *and* its own answer, incl. tool-calling) | free (reuses the call) | strong; adopted for *selection* |
| **C. Isolated parallel model** (e.g. fine-tuned ultralight LM / classifier) | ~0.6–0.7 s (still STT-gated) | yes (question only, not the answer) | extra infra | not faster than B; only wins if filler logic must stay out of the main prompt or a *local* fine-tuned model is a goal. **Cloud HF rejected** (cold starts, hot-path crash dependency vs CLAUDE.md rule 4) |
| **D. LLM's first sentence *is* the filler, live-synthesized** | ~1.4–1.7 s (pays Cartesia TTFB) | yes | none — deletes the subsystem | elegant but reintroduces the TTFB the cache exists to avoid; viable only if TTFB drops |
| **E. Curated cached-opener library + overlap** | 0.6 s | yes | curated cache + selection | **CHOSEN** |

Key reasoning threads:

- **Any text-based decision is floored by STT (~0.44 s)** — it needs the
  transcript. The only way under that floor is to decide from *audio/prosody*,
  trading question-fit for onset. So B, C, and E all land at ~0.6 s; D is later
  because it pays TTFB.
- **For a fixed cached set, the decision is *classification*, not generation.**
  A generative model that "returns the filler word" either maps back to the
  cached set (classification in disguise) or forces TTS on the filler
  (reintroduces latency). Cached openers ⇒ a selector (LLM tag or classifier),
  not free generation.
- **A finite opener set is not a compromise — it matches human behavior.**
  People reuse a small, repetitive set of openers ("hmm, let me think," "good
  question," "right, so—"). The robotic feeling in v1 is **not** finiteness; it
  is three separable defects: (i) too few phrases, (ii) random/no-fit selection,
  (iii) identical waveform per phrase.

### Decision — the frozen v2 design

Replace the timer-fired random filler with a **context-selected conversational
opener, played from cache and overlapped with the live answer**:

1. **Curated library of ~30 short openers**, replacing the ~3-5 per mood.
2. **Bucketed by conversational *function*** — `thinking`, `acknowledge`,
   `look_up`/`delegate`, `react` — not solely by mood. Select the bucket by
   context; random *within* the bucket for variety.
3. **Multiple recorded takes per phrase (2–3), rotated**, to eliminate the
   identical-waveform tell.
4. **Selection is LLM-emitted (Option B) with a timer fallback (Option A as a
   safety net).** The Talker emits an opener-function tag as (or near) its first
   token; the pipeline maps it to a cached take and plays instantly. If no tag
   has arrived by ~700 ms, fire a generic cached opener on the timer — preserving
   v1's hard onset floor against slow STT/TTFT or a no-tag-but-slow-Cartesia turn.
5. **Length-matched to predicted wait.** The LLM knows when it is calling a
   tool (= long); pick a longer opener ("let me look into that for a sec…") for
   delegating turns and a snappy one for quick answers.
6. **Self-gating.** Short/quick answers may get no opener at all — the answer is
   already fast.
7. **Overlap the real answer's Cartesia synthesis *under* the opener.** Decouple
   the network request from the audio-device serialization: start
   `tts.synthesize()` for the first real segment as soon as it is ready (~0.7 s),
   buffer frames while the opener plays, and only gate *playback* on the opener
   finishing and the stream being free. (Removes the `await filler_task`-before-
   synth ordering at `sequential_loop.py:696-714`.)

**Resulting projected timeline (from measured components; not yet measured
end-to-end):**

```
0.6 s  ▓ cached opener starts (instant, fits the question, varied take)
        ╰─ underneath: real answer synthesizing in Cartesia (fired ~0.7 s)
~1.7 s ███ opener ends → real answer takes over seamlessly
        → zero dead air, instant reassurance, fitting words
```

| | v1 (now) | v2 (frozen) |
|---|---|---|
| time to first sound | ~0.6 s | ~0.6 s |
| dead air after opener | ~1.5 s | **~0 s** |
| total to real answer | ~3.0 s | **~1.7–2.0 s** |
| opener words | random, mood-only | fits the question, function-bucketed |
| fires when | always @600 ms | only when needed (self-gated) |
| waveform variety | one take/phrase | 2–3 takes/phrase |

### Rationale

- **Keep the cache, deliberately.** The pure-live design (Option D) deletes the
  whole subsystem but sacrifices the 0.6 s reassurance onset to TTFB. We keep a
  cached subsystem specifically to preserve that instant "I heard you" signal;
  the trade is judged worth it.
- **The biggest win is free.** Removing the serialization (overlap) alone moves
  the real answer from ~3.0 s to ~1.7–2.0 s with no model and no new asset.
- **Naturalness is three fixes, not one.** Count, fit, and waveform-variety are
  addressed independently; finiteness is not a defect.
- **Selection lives where the knowledge is.** The Talker LLM is the only
  component that knows the question, what it is about to answer, *and* whether it
  is delegating — so it gates and selects best, for free. The timer remains only
  as a robustness floor.

### Tradeoffs & resource cost

Resource profile of the chosen design against the alternatives. *Effort* =
one-time engineering; *runtime* = per-turn hot-path cost; *assets* =
storage/curation.

| Option | Eng. effort | Runtime compute | Assets / storage | Added latency | New external dep | Ongoing upkeep | Key risk |
|---|---|---|---|---|---|---|---|
| A. Timer + random (v1) | — (shipping) | ~0 | 3-5 WAVs/mood | +0 (but masks poorly) | none | trivial | dead-air hole; robotic |
| **B/E. LLM-tag + curated cache + overlap (CHOSEN)** | medium | **~0** (rides the LLM call already made) | ~30 phrases × 2-3 takes × buckets ≈ 60-90 small WAVs (KBs each, negligible) | **−~1 s** (overlap deletes the serialization; selection adds ~0) | **none** | recurring: curate/record + bucket the phrase library | prompt complexity; loss of the hard timing floor (mitigated by timer fallback) |
| C. Isolated *local* fine-tuned model | high | a warm model resident in RAM (+CPU/GPU) | weights + training data | +inference ~0.1-0.3 s | none (if local) | retrain + serve + keep warm | infra/ops cost for no onset gain (STT-gated anyway) |
| C′. Cloud HF call | low-med | none local | none | +network + cold-start (seconds) | **+1 on the hot path** | endpoint mgmt | cold starts; **crashes v1** (CLAUDE.md rule 4) |
| D. Pure live first-sentence (no cache) | **low** (deletes the subsystem) | ~0 | **none** | +Cartesia TTFB before first sound (~1 s onset) | none | none | onset too slow *unless* TTFB drops |

**Net for the chosen design:** the only non-trivial resource is **human curation**
of the phrase library (record ~30 phrases × a few takes, bucket them) — a one-time
+ occasional cost, not a runtime one. Runtime compute is ~zero (selection rides
the LLM call already paid for; cached playback is a file read), storage is
negligible (tens of small WAVs), and crucially it **reduces** hot-path latency
(~−1 s from the overlap) rather than adding any. That asymmetry — large
perceived-quality gain, ~zero runtime cost, net latency *reduction* — is why it
beats the model-based options (C/C′), which spend infra for **no** onset benefit
since every text-based path is floored by STT.

### Consequences / implementation notes

- `audio/filler.py`: `pick_filler` gains a function-bucket input; the manifest
  gains function buckets and multiple takes per phrase.
- `audio/regenerate.py`: render N takes per phrase, organized by function bucket.
- `sequential_loop.py`: decouple Cartesia synthesis from the opener await
  (buffer-during-playback).
- Talker system prompt: teach the opener-function tag emission (and length cue
  for delegating turns). Alternatively/additionally, a local classifier on the
  transcript can drive the bucket.
- Manifest/schema: the opener is a pipeline-internal cached-playback concept; if
  it is also published for the body (a thinking gesture synced to the opener),
  that is a `vocalization`-family addition (schema-3 impact — evaluate against
  CLAUDE.md rule 6 before bumping `schema_version`).
- **Instrument latency for real.** `end_to_transcript_ms` is a hardcoded `0`
  placeholder (`sequential_loop.py:287`); add real STT, TTFT, and end-to-end
  timing so the projected numbers above can be confirmed post-implementation.

**Implementation landed by:** Story 6.2
(`build_documents/implementation-artifacts/6-2-cached-opener-system-and-cartesia-overlap.md`)
on 2026-05-28. Concrete changes:

- `audio/openers.py` (new) — function-bucketed `pick_opener` +
  `trigger_opener_fallback` (timer + race-window-protected fallback).
- `audio/filler.py` (deleted) — mood-bucketed filler subsystem
  retired; Story 5.5's cached-audio infrastructure reused for openers.
- `audio/cached.py` — `CachedAudioSurface` dropped `"filler"`, added
  `"opener"`; `CachedAudioEntry` gained a `bucket` field with a
  surface/mood/bucket consistency validator; manifest schema bumped
  1 → 2.
- `splitter/state_machine.py` + `splitter/segmenter.py` — recognise
  `<opener bucket="..."/>` self-closing tag; strip from Cartesia
  text; sync `opener_callback` invocation.
- `sequential_loop.py` — **deleted the `audio_started.set() + await
  filler_task` block** at the old `:701` site. The Cartesia synth
  call now fires the moment the splitter has the first non-tag text;
  PyAudio's device-level serialisation handles audible ordering on
  the speaker. Splitter callback claims `opener_already_playing`
  before spawning playback; the timer-fallback task respects the
  same event as its race-window check.
- `prompts/talker_system.md` — taught the `<opener bucket="..."/>`
  tag with one worked example per bucket + the self-gating rule.
- `setup.toml` — `[filler]` block removed; `[openers]` +
  `[openers.phrases_by_bucket]` added.

The integration test
(`tests/integration/test_opener_overlap_timing.py`) verifies the
overlap-deletion contract via a controlled timing assertion: a
regression that reintroduced the serialising await would fail the
test loud.

### Open question (keystone) — Cartesia TTFB

Cartesia TTFB (~1.07 s median, ~1.64 s p75) is the dominant remaining cost and
the keystone for two reasons:

1. It sets the v2 floor: the seamless handoff above assumes the answer is ready
   at ~1.7 s. Lower TTFB tightens the handoff; raise it and the opener must be
   longer to bridge.
2. It decides whether the *pure-live* design (Option D) ever becomes viable: at
   TTFB ~0.3 s the answer itself arrives at ~0.9 s, the opener becomes optional,
   and the entire cached subsystem could be deleted.

**To investigate:** websocket vs SSE transport, a faster Cartesia model, their
low-latency/continuation mode, and warm-connection pooling. (TTFB is *not* TLS
handshake — that is already warmed at startup, `tts/cartesia.py:116`.)

**Closed by:** `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md`
(2026-05-28). The Story 6.1 WebSocket migration + 500-sample TTFB spike (250
cold + 250 warm, per-transcript-length stratified) provides the measurement.
The report's headline section answers the Option D viability question and
either confirms the cached-opener-design-stands branch or signals that
Story 6.2 should re-evaluate against pure-live synthesis. Warm-vs-cold delta
in the report quantifies the connection-pool savings the parenthetical above
calls out.

### Related work / prior art (web survey, 2026-05-26)

How others have addressed perceived latency and filler naturalness in voice
agents — both to validate the chosen design and to position the paper.

- **Latency-perception thresholds.** Turn-taking research and industry guidance
  converge: responses < 500 ms feel natural, 500 ms–1.5 s is "slightly slow but
  acceptable," with ~500–1200 ms the optimal window. This frames our ~0.6 s
  onset as natural and the ~1.7–2.0 s answer as acceptable-but-improvable.
- **Cached filler phrases during waits are an established pattern.** LiveKit
  exposes `session.say(text, audio=...)` to play a *pre-synthesized* "let me
  check that for you" at the start of a function/tool call — exactly our
  length-matched delegating opener. Their documented caveat — *cache lookup on a
  full text segment can itself raise TTFB* — is precisely the failure mode our
  overlap-the-synthesis fix sidesteps. **Streaming synthesis from partial text**
  is cited as the single most important TTS latency lever (our sentence-paced
  path).
- **Contextualization matters — and is the crux of our bet.** HCI studies find
  users prefer *any* filler to silence, and that **contextually appropriate
  fillers are ranked above faster-but-generic ones** and improve perceived
  competence — direct support for function-bucketed, fit-the-question selection
  over v1's random pick.
- **Caveat the paper must confront (Jeong et al., CHI 2019).** Conversational
  fillers can make an assistant seem *less intelligent / less likable in
  task-oriented* dialogue (while more entertaining socially). Our wager is that
  *contextualized, length-matched* openers avoid the penalty that *generic*
  fillers incur — a directly testable hypothesis.
- **Closest related system — ConvFill (arXiv 2511.07397, 2025).** "Model
  Collaboration for Responsive Conversational Voice Agents": a small fast model
  emits an immediate conversational filler while a larger model computes the full
  answer. This is precisely our rejected Option C (isolated parallel model). We
  chose LLM-self-emission + cached playback instead because (i) the main LLM
  already knows its own answer and whether it is delegating, and (ii) cached
  audio gives a 0 ms onset a generative filler cannot. **ConvFill is the natural
  baseline to benchmark against.**
- **Speculative methods that break the STT floor.** PredGen (arXiv 2506.15556,
  2025) performs input-time speculative decoding — drafting candidate responses
  *while the user is still speaking* so TTS can begin almost immediately
  (~2–3× perceived-onset reduction). "Speculative TTS" pre-generates likely
  response audio and plays it on a prediction match. These are the only class of
  techniques that beat the **STT-gated ~0.44 s onset floor** we identified, and
  are the obvious v3 direction beyond this record.
- **Disfluency / filled pauses for naturalness.** Speech-synthesis research shows
  inserting filled pauses raises perceived naturalness, that LLM text lacks
  disfluencies (which diminishes naturalness), and that filler *type*
  systematically shapes the surrounding silence duration. Relevant to making both
  the opener and any residual opener→answer seam feel human rather than clipped.

**Sources:**
[Coval — Voice AI latency](https://www.coval.ai/blog/voice-ai-latency) ·
[Hamming — Voice AI latency](https://hamming.ai/resources/voice-ai-latency-whats-fast-whats-slow-how-to-fix-it) ·
[Twilio — core latency](https://www.twilio.com/en-us/blog/developers/best-practices/guide-core-latency-ai-voice-agents) ·
[CallSphere — sub-500ms](https://callsphere.ai/blog/voice-agent-latency-optimization-sub-500ms-response-times) ·
[LiveKit — audio customization (cached say)](https://docs.livekit.io/agents/multimodality/audio/customization/) ·
[LiveKit — reduce latency](https://kb.livekit.io/articles/4490830410-how-can-i-reduce-latency-in-voice-pipeline-agents-using-stt-tts-and-llm) ·
[Pipecat — TTS](https://docs.pipecat.ai/pipecat/learn/text-to-speech) ·
[ConvFill (arXiv 2511.07397)](https://arxiv.org/pdf/2511.07397) ·
[PredGen (arXiv 2506.15556)](https://arxiv.org/pdf/2506.15556) ·
[Behavioral/symbolic fillers for embodied agents in VR (arXiv 2508.11781)](https://arxiv.org/pdf/2508.11781) ·
[Filled pauses in speech synthesis](https://www.researchgate.net/publication/221152430_Filled_Pauses_in_Speech_Synthesis_Towards_Conversational_Speech) ·
[Disfluency insertion in LLM utterances (arXiv 2412.12710)](https://arxiv.org/html/2412.12710) ·
[VA linguistic features & perceptions (Tandfonline 2025)](https://www.tandfonline.com/doi/full/10.1080/10447318.2025.2530058)

### For the paper

Candidate contributions framed by this record:
- An **empirical latency decomposition of a production half-duplex voice agent**
  mined from structured logs (STT / LLM / TTS-TTFB / perceived-gap), including
  the counter-intuitive finding that **a latency-masking filler, when serialized
  ahead of TTS, *adds* latency** on the majority (75 %) of turns.
- The argument that **human opener vocabulary is finite and repetitive**, so a
  curated cached set selected by conversational function matches naturalness
  rather than compromising it — reframing "more variety" as count + fit +
  waveform-variety rather than open-ended generation.
- A design pattern: **context-selected cached conversational openers overlapped
  with streaming TTS**, with the LLM self-gating and length-matching the opener
  to its own predicted latency.
