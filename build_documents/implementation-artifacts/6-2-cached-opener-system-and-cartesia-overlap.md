# Story 6.2: Cached opener system + Cartesia overlap (supersedes Story 5.5 filler design)

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

**Execution-order note:** Second story in Epic 6. Depends on **Story 6.1** having landed (the Cartesia WebSocket transport + the TTFB measurement report — see AC #1 below for the design-checkpoint against that report). 6.2 can run in parallel with 6.3 once 6.1 is done. Current v1 finish-line execution order: `Epic 6 (6.1 → 6.2 ∥ 6.3 → 6.4) → 5-1 → 5-2 → 5-3 → 5-4`.

**Supersedes:** the Story 5.5 *filler design* (`audio/filler.py` — timer-fired random mood-keyed selection + filler-before-synth ordering). Story 5.5's *cached-audio infrastructure* (`audio/cached.py`, `assets/audio/` layout, manifest discipline, Stage 3 startup probe, `just regenerate-audio`) is **reused, not replaced**. v1 ships Story 5.5 unchanged until this story lands.

## Story

As Kamal,
I want context-fitting **cached openers** — function-bucketed, LLM-tag-selected, with a timer fallback for safety — and the real answer's Cartesia synthesis **overlapped** with opener playback,
so that (a) the opener fits the question (calendar question → `<opener bucket="delegate"/>` "let me look that up for you"; thinking question → `<opener bucket="thinking"/>` "hmm…"; short yes/no → no opener at all) rather than v1's random mood-keyed filler; (b) the ~1 s serialization tax DR-001 surfaced (where the pipeline awaits the filler task before issuing the Cartesia network request — `sequential_loop.py:696-714`, hitting ~75% of turns per DR-001's log analysis) is **deleted**; (c) the dead-air-after-opener gap closes from ~1.5 s median (v1 baseline per DR-001) toward ~0 s (NFR34's ≤ 250 ms p95 target).

## Acceptance Criteria

1. **Story 6.1 TTFB-report design-checkpoint.** Before implementation begins, re-read `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md` (produced by Story 6.1). The implementation choice for this story branches on the report's median TTFB number:
   - **Median TTFB > 0.4 s** → the cached-opener subsystem in this story stands as specified below. This is DR-001 §"Decision (frozen)" Option B/E.
   - **Median TTFB ≤ 0.4 s** → record an inline note in this story's Dev Agent Record explaining whether the cached subsystem is still worth the curation cost vs going to a thinner "live-only" design (DR-001 Option D). The default presumption is "ship cached as specified" unless the report's keystone-question call-out explicitly recommends Option D reshape. If reshape is taken, the AC set below is **revised down**; document the divergence in this story's Change Log + epics.md's Epic 6 §Status.
   - Recording the checkpoint outcome in the Dev Agent Record is a **mandatory first step** of Task 1.

2. **Curated opener library + manifest.** Function buckets, **not mood buckets** (this is the key shape-shift from Story 5.5's filler design):

   | Bucket | When the Talker emits it | Example phrases (operator curates the final list) |
   |---|---|---|
   | `thinking` | Generic "I'm processing" — default for medium-length replies | "hmm…", "let me think…", "right, so…", "okay, so…" |
   | `acknowledge` | Quick affirmation before a short reply | "yeah", "right", "mm-hmm", "okay" |
   | `look_up` | Talker is about to fetch from belief-state / orchestrator (READ-shaped tool calls) | "let me check", "hold on", "one moment", "lemme see" |
   | `delegate` | Talker is about to dispatch a longer orchestrator turn (WRITE / multi-step tool calls) | "let me look that up for you", "alright, working on it", "give me a second…" |
   | `react` | Talker is mirroring user emotion / making a meta-comment | "oh!", "ooh", "ha!", "really?" |

   Each bucket carries ≥ 2 phrases at story-landing time (so the random-within-bucket selector has variety). Layout under `assets/audio/openers/<bucket>/NN.wav` mirrors Story 5.5's `assets/audio/<surface>[/<mood>]/NN.wav` structure exactly.

3. **`[openers]` block in `setup.toml`** (new):

   ```toml
   # Story 6.2: cached function-bucketed opener phrases. Replaces the v1
   # mood-keyed [filler] design. Operator should expand each bucket over
   # time; ≥ 2 phrases per bucket is the floor (FillerConfig-style
   # validator enforces). Edits here require `just regenerate-audio`.
   [openers]
   timer_fallback_ms = 700                   # opener-onset floor; fires this bucket if no LLM tag arrives
   timer_fallback_bucket = "acknowledge"     # generic-safe default
   max_consecutive_repeat = 0                # ring-buffer suppression — same semantics as [filler]

   [openers.phrases_by_bucket]
   thinking    = ["hmm", "let me think", "right so", "okay so", "alright"]
   acknowledge = ["yeah", "right", "mm-hmm", "okay", "sure"]
   look_up     = ["let me check", "hold on", "one moment", "lemme see"]
   delegate    = ["let me look that up for you", "alright working on it", "give me a second", "let me check on that"]
   react       = ["oh", "ooh", "ha", "really", "huh"]
   ```

   `OpenersConfig` in `config/setup.py` mirrors `GreetingConfig` / `FillerConfig`:
   - `model_config = ConfigDict(extra="forbid")`
   - `timer_fallback_ms: int = Field(default=700, gt=0, le=2000)` — clamp prevents misconfig that breaks NFR33.
   - `timer_fallback_bucket: OpenerBucket = "acknowledge"`
   - `max_consecutive_repeat: int = Field(default=0, ge=0)`
   - `phrases_by_bucket: dict[OpenerBucket, list[str]] = Field(default_factory=lambda: _DEFAULT_OPENERS)`
   - `model_validator(mode="after")` — every `OpenerBucket` Literal value has ≥ 1 entry; missing raises `ValueError`.
   - `openers: OpenersConfig = Field(default_factory=OpenersConfig)` on `SetupConfig`.

   `OpenerBucket = Literal["thinking", "acknowledge", "look_up", "delegate", "react"]` lives in `audio/openers.py` (alongside the selector — see AC #5).

4. **`CachedAudioEntry` / `CachedAudioManifest` extended for openers; filler retired; manifest schema bumped 1 → 2.**

   - `CachedAudioSurface = Literal["greeting", "goodbye", "clarification", "opener"]` — `"filler"` is **removed**, `"opener"` is **added**. This is a deliberate breaking change to the manifest format.
   - Add `bucket: OpenerBucket | None` field to `CachedAudioEntry`.
   - `model_validator(mode="after")` on `CachedAudioEntry`:
     - `surface == "greeting"` → `mood is not None and bucket is None`
     - `surface == "opener"` → `bucket is not None and mood is None`
     - `surface in ("goodbye", "clarification")` → `mood is None and bucket is None`
   - Bump `_MANIFEST_SCHEMA_VERSION` from `1` to `2` in `audio/cached.py`. The existing `load_manifest` rejects mismatched-version manifests with `StartupValidationError(action="run \`just regenerate-audio\`")` — operators get a clean upgrade path on first restart after pulling this story.
   - `compute_phrase_hash` extended with a `bucket: OpenerBucket | None` parameter alongside `mood`. The hash input becomes `phrase \x00 voice_id \x00 tts_model \x00 (mood OR bucket OR "__none__")`. Same `__none__` sentinel pattern as today's mood handling.
   - `CachedAudioManifest.lookup` extended: takes `bucket: OpenerBucket | None = None` alongside the existing `mood: Mood | None = None`. Asserts caller passes the right one for the surface.

5. **New `audio/openers.py` module.** Mirrors `audio/filler.py`'s shape (the parallel-module pattern from Story 5.5). Exports:

   ```python
   OpenerBucket = Literal["thinking", "acknowledge", "look_up", "delegate", "react"]

   def pick_opener(
       manifest: CachedAudioManifest,
       bucket: OpenerBucket,
       recent: deque[str],
   ) -> CachedAudioEntry | None:
       """Pick a take from the requested function bucket.

       - NO mood fallback. Openers are function-bucketed, not mood-bucketed.
         If the requested bucket is empty (which the OpenersConfig validator
         should prevent in production), return None and let the caller stay
         silent for this turn.
       - Last-N suppression — exclude entries whose phrase_hash is in
         `recent`. If exclusion empties the candidate list, reset `recent`
         and pick from the full bucket (parallel to pick_filler's behavior).
       - random.choice over the surviving candidates.
       """

   async def trigger_opener_fallback(
       pa: pyaudio.PyAudio,
       indices: AudioDeviceIndices,
       manifest: CachedAudioManifest,
       config: OpenersConfig,
       opener_selected: asyncio.Event,
       opener_already_playing: asyncio.Event,
       recent: deque[str],
   ) -> None:
       """Wait up to `timer_fallback_ms` for an LLM-tag-selected opener.

       Coroutine spawned on VAD end-of-speech. Completes when either:

       - The splitter sets `opener_selected` before `timer_fallback_ms`
         expires → return without playing (the splitter-side path is
         playing its own LLM-tag-selected opener).
       - The threshold expires AND no LLM tag arrived AND `opener_already_playing`
         is still unset → pick from `timer_fallback_bucket` and play it.
       - If `opener_already_playing` is set during the wait, return (race
         window — splitter beat the timer by a microsecond).

       NO mood signal at all — the timer fallback is function-bucketed
       (default `acknowledge`).
       """
   ```

   Why two events, not one: `opener_selected` (set by the splitter when it sees a `<opener bucket="..."/>` tag) is the "cancel the timer" signal. `opener_already_playing` (set by the splitter or the runtime once the cached file is actually playing on the speaker) is the "race-window check" right before the timer fires. Both prevent double-firing without a shared lock.

6. **Splitter (`splitter/state_machine.py` + `splitter/segmenter.py`) recognizes the `<opener bucket="..."/>` tag.**

   - **Lexical extension** in `state_machine.py`: the parser already handles `<emotion value="X"/>` tags (Story 3.3). Add a parallel handler for `<opener bucket="X"/>` where `X` ∈ the `OpenerBucket` Literal. Same self-closing form, same per-state buffer size (≤ 32 bytes), same char-by-char incremental parsing — **no regex, no XML parser**, per FR18's contract.
   - **New `ParseEvent` variant** `OpenerEvent(bucket: OpenerBucket)` in `state_machine.py`. Mirrors `EmotionEvent`'s shape.
   - **Segmenter side-effect** in `segmenter.py`: when the segmenter sees an `OpenerEvent`, it (a) **strips the tag from the text passed to Cartesia** (do NOT pass `<opener .../>` to the TTS — it would be rendered as audio or rejected), and (b) emits a side-channel signal via a callback or queue to the runtime so the cached file can play immediately. Choose the simplest plumbing — likely add an `opener_callback: Callable[[OpenerBucket], None] | None` argument to `Segmenter.__init__`, called synchronously when the `OpenerEvent` is consumed. The runtime registers a callback that triggers playback (see AC #7).
   - **Position discipline**: an `<opener .../>` tag is **only meaningful at or near the start of the response** (per DR-001's "first token" framing). The segmenter accepts it anywhere for robustness, but the Talker prompt (AC #9) teaches the LLM to emit it as the first non-whitespace token. The unit test fixture covers both cases.

7. **Runtime wiring in `sequential_loop.py` — opener playback + Cartesia overlap (the serialization-tax deletion).**

   At VAD end-of-speech, the runtime:
   - Builds `opener_selected: asyncio.Event` and `opener_already_playing: asyncio.Event`.
   - Spawns `trigger_opener_fallback(...)` as a background task — this awaits the timer.
   - Constructs the `Segmenter` with `opener_callback=on_opener_selected_by_splitter`.
   - When the splitter fires `on_opener_selected_by_splitter(bucket)`:
     - Set `opener_selected` (cancels the timer-fallback path).
     - Set `opener_already_playing` immediately (race-window protection for the fallback's late check).
     - Spawn another background task that calls `pick_opener` + `play_cached(...)` for the selected bucket. The task sets `opener_already_playing` (already set above, idempotent), updates the `recent` ring buffer, and completes when playback finishes.
   - **Critical change at the existing `sequential_loop.py:696-714` site**: today the code does `audio_started.set(); await filler_task` **before** opening the output stream and **before** the first `tts.synthesize()` call. The story removes the `await filler_task` line so the real answer's Cartesia request fires the moment the splitter has the first non-tag text segment buffered. **The audio device's PyAudio queue serializes playback automatically** (PyAudio's `stream.write` blocks until the OS buffer has room); the network call is unblocked. Pseudo-code post-refactor:

     ```python
     if not fsm_speaking_fired:
         await fsm.on_first_audio_frame()
         fsm_speaking_fired = True
         with suppress_native_stderr():
             out_stream = pa.open(format=..., output_device_index=indices.output_index)
         # NEW: if an opener is still playing on its OWN output stream
         # (started by the splitter callback above), the PyAudio output
         # stream we just opened blocks naturally until the opener's
         # stream finishes — the OS audio buffer serializes. We do NOT
         # explicitly await the opener task here; that's the point.
     async for chunk in tts.synthesize(text):
         await asyncio.to_thread(out_stream.write, chunk)
     ```

   - Important nuance: today the **opener / filler and the real audio share a single output stream** because `_speak` is the only place that opens one. Post-refactor, the opener's `play_cached(...)` opens its OWN output stream (per `cached.py:play_cached` — line `pa.open(...)`), and the real-answer block opens its own. Both write to the same physical device; PyAudio is responsible for ordering. **Test 8 (integration) verifies there's no audible glitch at the handoff.**

8. **Tests.**

   - `tests/unit/audio/test_openers.py` (new): `pick_opener` happy path; empty-bucket returns None; last-N suppression; exclusion-empties-then-resets.
   - `tests/unit/audio/test_openers_fallback.py` (new): `trigger_opener_fallback` with mocked `asyncio.Event`s — fast splitter cancels timer (no playback); timer fires when no splitter signal; race-window check (event set during the sleep).
   - `tests/unit/splitter/test_state_machine.py` (extend): parser recognizes `<opener bucket="thinking"/>` at start, mid-stream, and split across `consume()` calls. Invalid bucket name raises `SplitterError`.
   - `tests/unit/splitter/test_segmenter.py` (extend): `OpenerEvent` triggers the callback exactly once per tag; the tag string is stripped from `Segment.text`.
   - `tests/unit/config/test_setup.py` (extend): `OpenersConfig` parses the `[openers]` block; validator rejects empty bucket; `timer_fallback_ms` clamping.
   - `tests/contract/test_audio_manifest.py` (extend): manifest `schema_version=2` accepted; `schema_version=1` rejected with the upgrade-path error message.
   - `tests/integration/test_opener_path.py` (new): full pipeline with mocked Cartesia. Talker emits `<opener bucket="thinking"/> here is your answer.` → assert the cached `thinking` WAV plays AND Cartesia is **NEVER called for the opener** (the mock-not-called assertion is the cached-path proof).
   - `tests/integration/test_overlap_timing.py` (new): mock Cartesia's `synthesize` to fire after a controlled 100 ms delay. Talker emits opener tag + long real reply. Assert that `CartesiaClient.synthesize(text)` is called within **≤ 100 ms** of the splitter buffering its first non-tag text segment — proving the synth-network call is **not** blocked on opener playback. A regression-baseline assertion records the pre-refactor timing (~opener_duration ms) so the delta is visible in test output.
   - `tests/integration/test_opener_timing.py` (new): NFR33 measurement — synthesize an end-of-speech event with no LLM tag and Cartesia first-frame delayed 1500 ms. Assert the timer-fallback opener audio starts at ~700 ms (within tolerance). NFR34 measurement — same setup, then real audio fires at 800 ms; assert dead-air gap (opener last frame → real-answer first frame) ≤ 250 ms.

9. **Talker system prompt update** (`prompts/talker_system.md`).

   Add a new section teaching the `<opener bucket="..."/>` tag. The section explains:
   - **Where to put it**: at or very near the first non-whitespace token of the reply. The tag is stripped from the text Cartesia renders; only its semantic side-effect (which cached opener to play) matters.
   - **Bucket selection rules**:
     - `delegate` — when the Talker decides to call a tool that goes to the orchestrator (long-running). Example: `<opener bucket="delegate"/> let me look that up for you. <tool_call name="dispatch_to_orchestrator">...`
     - `look_up` — when the Talker reads belief state / makes a short READ-shaped tool call. Example: `<opener bucket="look_up"/> let me check…`
     - `thinking` — generic; medium-length conversational reply with no tool call. Example: `<opener bucket="thinking"/> right, so the way I see it…`
     - `acknowledge` — quick affirmation before a one-sentence reply. Example: `<opener bucket="acknowledge"/> yeah, that works.`
     - `react` — mirroring user emotion / brief meta-comment. Example: `<opener bucket="react"/> oh wow, that's cool!`
   - **Self-gating** (FR57): for very short / quick replies (e.g., one-word confirmations like "yes", "yep", "no"), **emit no tag** — the opener would be longer than the reply and would feel like padding. The pipeline's timer-fallback will not fire either if the real reply arrives before `timer_fallback_ms`.
   - **No mood signal**: the opener bucket is function, not mood. Don't try to encode mood here — `set_mood` is the separate tool.
   - **Density**: zero or one tag per reply. Multiple `<opener .../>` tags in a single reply is a defect (only the first fires).

10. **`audio/regenerate.py` extended; filler module retired.**
    - Add the `[openers]` surface to the regenerate recipe — iterate over `config.openers.phrases_by_bucket` and render each phrase to `assets/audio/openers/<bucket>/NN.wav`, same per-bucket numbering as the greetings path.
    - **Remove the filler-rendering block** from `regenerate.py`. The recipe's prune pass deletes orphan filler entries from the manifest + removes the WAV files on next run.
    - Delete `src/voice_agent_pipeline/audio/filler.py` entirely. Update any imports of `pick_filler` / `maybe_play_filler` / `FillerConfig` in `sequential_loop.py` to the opener equivalents.
    - Remove `[filler]` block from `setup.toml`. Remove `FillerConfig` from `config/setup.py`. Drop `filler: FillerConfig` from `SetupConfig`.
    - Drop the filler tests (`tests/unit/audio/test_filler.py` if it exists) since the module is gone.

11. **Documentation updates** (same commit):
    - `README.md`: update the "Audio assets" section to reference openers instead of fillers; note that the filler subsystem was retired in Story 6.2 (and the cached-audio infrastructure now serves openers + greetings + goodbyes + clarifications).
    - `setup.toml` comment block updates: `[openers]` carries an instructional comment block matching `[greeting]`'s style.
    - `build_documents/planning-artifacts/architecture.md` — verify the v2 implementation-sequence item #21 footnote still matches the as-built shape; amend in this commit if any drift surfaces (NFR26 spec-as-contract).
    - `build_documents/planning-artifacts/decision-records.md` — DR-001's design-pass record references this story's overlap deletion as the runtime correlate. The DR file says records are immutable once frozen, so the cross-reference is a small "Implementation landed by: 6-2-cached-opener-system-and-cartesia-overlap (commit hash) on (date)" line under DR-001's consequences section, mirroring DR-004's "Closes" annotation style.

12. **Commit policy (Task 9 below).** Single commit per `feedback_commit_policy.md`. Push immediately after commit per `feedback_push_after_commit.md`. The regenerated opener WAVs + updated manifest + the deleted filler tree are all part of the same commit.

## Tasks / Subtasks

- [ ] **Task 1: Story 6.1 TTFB-report design-checkpoint** (AC: #1)
  - [ ] Read `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md`
  - [ ] Record the checkpoint outcome in the Dev Agent Record's "Completion Notes List":
    - Median TTFB number
    - Decision: "ship cached as specified" vs "reshape toward DR-001 Option D"
    - If reshape: cite the AC revisions in epics.md + add a Change Log entry
  - [ ] If the report doesn't yet exist (Story 6.1 hasn't landed), HALT and ask for Story 6.1 first

- [ ] **Task 2: Config schema — `[openers]` block + `OpenersConfig`** (AC: #3)
  - [ ] Add `OpenerBucket` Literal in `audio/openers.py` (the module is created in Task 4 but the type can land here first)
  - [ ] Add `OpenersConfig` to `config/setup.py` with the validator
  - [ ] Add `_DEFAULT_OPENERS` module-level constant — copy the values from AC #3
  - [ ] Mount `openers: OpenersConfig` on `SetupConfig`
  - [ ] Add `[openers]` + `[openers.phrases_by_bucket]` to `setup.toml`
  - [ ] Extend `tests/unit/config/test_setup.py`

- [ ] **Task 3: Manifest schema bump 1 → 2 + retire filler surface** (AC: #4, #10)
  - [ ] In `audio/cached.py`: change `CachedAudioSurface` Literal — drop `"filler"`, add `"opener"`
  - [ ] Add `bucket: OpenerBucket | None` field to `CachedAudioEntry`
  - [ ] Add `model_validator(mode="after")` enforcing the (surface, mood, bucket) one-of-the-other rule
  - [ ] Bump `_MANIFEST_SCHEMA_VERSION` from 1 to 2
  - [ ] Extend `compute_phrase_hash` to accept `bucket` (in addition to `mood`); update hash-input to include whichever is non-None
  - [ ] Extend `CachedAudioManifest.lookup` signature to accept `bucket: OpenerBucket | None = None`
  - [ ] Update `load_and_validate_manifest`: build expected-hashes from `config.openers.phrases_by_bucket` (NEW) and drop the filler-surface enumeration
  - [ ] Extend `tests/contract/test_audio_manifest.py` for schema_version=2

- [ ] **Task 4: New `audio/openers.py` module** (AC: #5)
  - [ ] `OpenerBucket` Literal (or re-export from Task 2 if landed there first)
  - [ ] `pick_opener(manifest, bucket, recent) -> CachedAudioEntry | None` — implementation mirrors `pick_filler` but bucket-keyed not mood-keyed; **no mood fallback**
  - [ ] `trigger_opener_fallback(pa, indices, manifest, config, opener_selected, opener_already_playing, recent)` — timer-based async fallback (mirrors `maybe_play_filler`)
  - [ ] Unit tests in `tests/unit/audio/test_openers.py` + `test_openers_fallback.py`

- [ ] **Task 5: Splitter — opener-tag recognition** (AC: #6)
  - [ ] Extend `state_machine.py` to parse `<opener bucket="X"/>` self-closing tags (parallel to the existing `<emotion value="X"/>` handler)
  - [ ] Add `OpenerEvent(bucket: OpenerBucket)` to the `ParseEvent` union
  - [ ] Validate `bucket` against the `OpenerBucket` Literal at parse time; bad bucket raises `SplitterError`
  - [ ] In `segmenter.py`: add `opener_callback: Callable[[OpenerBucket], None] | None = None` to `Segmenter.__init__`; call it synchronously on each `OpenerEvent`
  - [ ] In `segmenter.py`: strip the `<opener .../>` tag from `Segment.text` (it must NOT reach Cartesia)
  - [ ] Extend `tests/unit/splitter/test_state_machine.py` + `test_segmenter.py`

- [ ] **Task 6: Runtime wiring + Cartesia overlap refactor** (AC: #7)
  - [ ] In `sequential_loop.py`: build `opener_selected` + `opener_already_playing` events on VAD end-of-speech
  - [ ] Spawn `trigger_opener_fallback(...)` background task
  - [ ] Register opener callback on the `Segmenter` that:
    - sets `opener_selected` (cancels the fallback timer)
    - sets `opener_already_playing` (race-window protection)
    - spawns a background task to play the matching cached file via `play_cached(...)`
    - updates the `recent` ring buffer
  - [ ] **Delete the `await filler_task` line at `sequential_loop.py:701`** (and the matching one at `:750` if present) — this is the core overlap-deletion change
  - [ ] Update the `audio_started.set()` semantics if needed — today it signals the filler task to stop; post-refactor it may be removable entirely (verify against the rest of the loop's event flow)
  - [ ] Update imports: remove `from voice_agent_pipeline.audio.filler import ...`; add `from voice_agent_pipeline.audio.openers import ...`
  - [ ] Run `just check` after this task — the integration tests (Task 8) catch regressions but unit + lint must pass first

- [ ] **Task 7: Talker system prompt update** (AC: #9)
  - [ ] Open `prompts/talker_system.md`
  - [ ] Add the new opener-tag section per AC #9. Place it near where the emotion-tag teaching lives (consistent placement helps the LLM learn both)
  - [ ] Include 5 worked examples — one per bucket — covering the self-gating case (no tag, short reply) as the sixth
  - [ ] Cross-test by manually invoking the Talker with 5–10 sample user inputs of varied shapes (the integration test exercises the wire path; this is for prompt-density tuning before soak)

- [ ] **Task 8: Integration + contract tests** (AC: #8)
  - [ ] `tests/integration/test_opener_path.py` — full pipeline with mocked Cartesia; opener tag → cached playback; assert `CartesiaClient.synthesize` was NEVER called for the opener phrase
  - [ ] `tests/integration/test_overlap_timing.py` — the synth-fires-without-waiting assertion (≤ 100 ms after splitter buffers first non-tag text)
  - [ ] `tests/integration/test_opener_timing.py` — NFR33 (≤ 700 ms p95) + NFR34 (≤ 250 ms p95) measurement
  - [ ] Run the full suite under `just check`; investigate any regression — the pre-Epic-6 simple-turn integration test should keep passing because the Talker fixture won't emit `<opener .../>` unless explicitly scripted

- [ ] **Task 9: Retire filler module + regenerate-audio extension + docs + commit** (AC: #10, #11, #12)
  - [ ] Extend `audio/regenerate.py` to render openers from `config.openers.phrases_by_bucket`
  - [ ] Remove the filler-rendering block from `regenerate.py`
  - [ ] `git rm src/voice_agent_pipeline/audio/filler.py`
  - [ ] `git rm tests/unit/audio/test_filler.py` (if it exists)
  - [ ] Remove `FillerConfig` from `config/setup.py`; remove `filler` field from `SetupConfig`
  - [ ] Remove `[filler]` and `[filler.phrases_by_mood]` from `setup.toml`
  - [ ] Run `just regenerate-audio` (locally) — verify it (a) renders all 5 opener buckets, (b) prunes the filler entries, (c) writes a new manifest at schema_version=2
  - [ ] Commit the regenerated WAVs + manifest + code/config changes as **one commit** per `feedback_commit_policy.md`
  - [ ] `README.md` + architecture.md + decision-records.md cross-reference updates per AC #11 in the same commit
  - [ ] `just check` green; push per `feedback_push_after_commit.md`

## Dev Notes

### Relevant architecture patterns and constraints

- **Story 5.5's cached-audio infrastructure is REUSED, not duplicated.** `audio/cached.py` already does manifest loading, validation, and `play_cached` playback. This story extends `CachedAudioEntry` / `CachedAudioSurface` / `compute_phrase_hash` / `CachedAudioManifest.lookup` — but does NOT touch the playback loop, the WAV format pin (16 kHz mono S16LE), or the Stage 3 startup probe's outer shape. **Resist any temptation to fork a new module for openers — extend the existing one.**

- **Boundary concentration (`CLAUDE.md` + `architecture.md` §"Architectural Boundaries"):** `pyaudio` is imported in three files post-Story-5.5: `audio/devices.py`, `audio/transport.py`, `audio/cached.py`. Story 6.2 does NOT add a fourth import site. `audio/openers.py` calls into `audio/cached.py:play_cached` (which owns the PyAudio stream).

- **Function buckets vs mood buckets — deliberate shape-shift.** Story 5.5 picked mood-keyed fillers because there was no signal smarter than current mood to drive selection. DR-001's keystone insight is that the Talker LLM knows the question AND its own answer (incl. tool-call shape), so function-bucketed selection is strictly more accurate. **Do not bring mood back into the opener path even if it feels "consistent" with greetings.** Mood drives greeting because the greeting is generated *before* the LLM sees user input; opener fires *after*, so the LLM knows more.

- **Audio overlap via PyAudio device serialization, not application-level locks.** The Cartesia network call (`synthesize(text)`) is unblocked the moment the splitter has the first non-tag text. The opener's `play_cached` opens its own PyAudio output stream; the real-answer's `_speak` opens its own. Both write to the same physical device; **PyAudio handles the queuing** because `stream.write` blocks until the OS audio buffer has room. **Do NOT add an application-level lock or semaphore here** — that re-introduces the serialization tax this story exists to delete. If the integration test (Test 8 `test_overlap_timing.py`) detects regression, the bug is somewhere else (e.g., a stray `await opener_task` in the real-answer path).

- **No mood fallback in `pick_opener`.** Unlike `pick_filler`, which falls back from the requested mood bucket to `calm` if the requested bucket is empty, `pick_opener` returns `None` on an empty bucket. The `OpenersConfig` validator (AC #3) prevents empty buckets in production; runtime empty means the operator bypassed validation. Log `opener.no_pick` WARN and continue without an opener.

- **The opener tag is "near the first token", not "exactly the first token".** Whitespace, punctuation, and short leading words are fine. The splitter's char-by-char parser accepts the tag anywhere in the stream; the Talker prompt teaches "first non-whitespace token" as a guideline. If the LLM produces it later in the reply, the timer-fallback may have already fired — that's fine; the late-arriving `<opener .../>` tag is silently dropped (log `opener.late_tag` DEBUG) and the speaker hears the fallback opener.

- **Pydantic at boundaries (CLAUDE.md rule 3):** `OpenersConfig` is `BaseModel` with `extra="forbid"`. `OpenerBucket` is `Literal[...]` — no Enum. `CachedAudioEntry`'s new `bucket` field is typed correctly through the union.

- **Fail-fast posture (CLAUDE.md rule #4):** the splitter's `SplitterError` on bad bucket value propagates; no try/except in v1. The opener-callback approach is sync (Python `Callable`, not `Awaitable`) — the callback's side effects (event sets + background task spawns) cannot themselves raise without being noticed.

- **Single-commit policy (per `feedback_commit_policy.md`):** the file list at landing time is substantial (new module, deleted module, new config block, deleted config block, new manifest WAVs, deleted filler WAVs). Stage everything and commit once. Push immediately per `feedback_push_after_commit.md`.

### Source tree components to touch

| File | Action | Notes |
|---|---|---|
| `src/voice_agent_pipeline/audio/openers.py` | **New** | `OpenerBucket` Literal + `pick_opener` + `trigger_opener_fallback` |
| `src/voice_agent_pipeline/audio/filler.py` | **Delete** | Superseded by openers |
| `src/voice_agent_pipeline/audio/cached.py` | Modify | `CachedAudioSurface` Literal change + `bucket` field + validator + schema_version bump + `compute_phrase_hash` extension |
| `src/voice_agent_pipeline/audio/regenerate.py` | Modify | Add opener rendering; remove filler rendering |
| `src/voice_agent_pipeline/config/setup.py` | Modify | Add `OpenersConfig`; remove `FillerConfig`; swap on `SetupConfig` |
| `src/voice_agent_pipeline/splitter/state_machine.py` | Modify | Parse `<opener bucket="X"/>`; add `OpenerEvent` to `ParseEvent` |
| `src/voice_agent_pipeline/splitter/segmenter.py` | Modify | `opener_callback` arg; tag-strip; callback fires on `OpenerEvent` |
| `src/voice_agent_pipeline/sequential_loop.py` | Modify | Spawn opener-fallback task; register splitter callback; **delete `await filler_task` at `:701`** |
| `prompts/talker_system.md` | Modify | Add `<opener bucket="..."/>` teaching + 5 examples |
| `setup.toml` | Modify | Add `[openers]` + `[openers.phrases_by_bucket]`; remove `[filler]` + `[filler.phrases_by_mood]` |
| `assets/audio/openers/<bucket>/NN.wav` | **New** | Generated by `just regenerate-audio` |
| `assets/audio/fillers/...` | **Delete** | Pruned by `just regenerate-audio` |
| `assets/audio/manifest.json` | Modify | `schema_version: 2`; new opener entries; filler entries pruned |
| `README.md` | Modify | Audio assets section update |
| `build_documents/planning-artifacts/architecture.md` | Maybe modify | Verify v2-item-21 footnote accuracy |
| `build_documents/planning-artifacts/decision-records.md` | Modify (lightly) | DR-001 "Implementation landed by:" annotation |
| `tests/unit/audio/test_openers.py` | **New** | `pick_opener` cases |
| `tests/unit/audio/test_openers_fallback.py` | **New** | `trigger_opener_fallback` cases |
| `tests/unit/audio/test_filler.py` | **Delete** | Filler retired |
| `tests/unit/splitter/test_state_machine.py` | Modify | `<opener .../>` parsing |
| `tests/unit/splitter/test_segmenter.py` | Modify | Callback + tag strip |
| `tests/unit/config/test_setup.py` | Modify | `OpenersConfig` cases |
| `tests/contract/test_audio_manifest.py` | Modify | schema_version=2 |
| `tests/integration/test_opener_path.py` | **New** | Cached-path-reached assertion |
| `tests/integration/test_overlap_timing.py` | **New** | Synth-fires-without-waiting; NFR34 measurement |
| `tests/integration/test_opener_timing.py` | **New** | NFR33 measurement |

### Testing standards summary

- `just check` (ruff + ruff format + pyright + `pytest tests/unit -q`) green pre-commit.
- **Protocol-boundary mocking only (CLAUDE.md rule 7):** mock `CartesiaClient.synthesize` (`AsyncIterator[bytes]` Protocol method) and PyAudio at module boundaries. Do NOT mock `pick_opener` / `play_cached` from the runtime tests — those are internal functions; the unit tests already cover them in isolation.
- Integration tests use the existing `Segmenter` + state machine (real), real `OpenersConfig`, real `CachedAudioManifest` (loaded from a fixture manifest pointing at small fixture WAVs under `tests/_fixtures/audio/`), but mocked `CartesiaClient`. This is the pattern established by `test_cached_greeting.py` (Story 5.5).
- **NFR33 / NFR34 measurement tests use `pytest-asyncio`'s `asyncio.get_event_loop().time()` deltas** — single clock, intra-test deltas. Same methodology as Story 6.1's TTFB spike. Use `pytest.mark.timeout(5.0)` to bound flaky-environment hangs.

### Project Structure Notes

- **`audio/openers.py` and `audio/filler.py` are NOT duplicates briefly.** When the dev agent is mid-implementation, both files coexist. Order Task 4 (create openers.py) then Task 6 (wire it in) then Task 9 (delete filler.py). The integration tests in Task 8 should run against the openers path; the existing filler tests (if any) should fail at this point — that's correct, they're deleted in Task 9.

- **Manifest schema 2 makes Story 5.5's existing manifest incompatible.** Operators pulling this story see `StartupValidationError(stage="audio_assets", action="run \`just regenerate-audio\`")` on first restart until they run the recipe. This is the same upgrade path Story 5.5 itself uses; document it in the commit message.

- **Story 5.5's filler WAV directory becomes orphaned.** The regenerate recipe's prune pass should delete `assets/audio/fillers/` entirely when the `[filler]` config is gone. Verify by re-running the recipe after Task 9 — the directory should disappear.

- **Story 4.5's greeting subsystem is UNTOUCHED.** Greetings remain mood-bucketed; only the filler→opener swap is in scope. Cross-tested by ensuring `test_cached_greeting.py` (Story 5.5) still passes after this story.

### References

- `build_documents/planning-artifacts/decision-records.md` §DR-001 — the design rationale for opener selection, the overlap insight, the keystone TTFB question. Read the full §"Empirical evidence" + §"Decision — the frozen v2 design" sections before starting.
- `build_documents/planning-artifacts/epics.md` Epic 6 §Story 6.2 — the epic-level AC summary.
- `build_documents/planning-artifacts/prd.md` FR54 (curated opener library), FR55 (Talker opener-function tag), FR56 (timer fallback), FR57 (self-gating), FR58 (Cartesia overlap), NFR33 (opener onset ≤ 700 ms p95), NFR34 (dead-air-after-opener ≤ 250 ms p95).
- `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md` — **read first** (Task 1 design-checkpoint).
- `build_documents/implementation-artifacts/5-5-cached-audio-deterministic-phrases.md` — the precedent for cached-audio infrastructure, manifest discipline, regenerate recipe. The deliberate parallel.
- `src/voice_agent_pipeline/sequential_loop.py:696-714` — the overlap-refactor target. Lines 700-701 are the specific `audio_started.set() / await filler_task` block to remove.
- `src/voice_agent_pipeline/audio/cached.py` — the module to extend. Note `CachedAudioSurface` Literal at line 90, `CachedAudioEntry` at line 93, `compute_phrase_hash` at line 215, `load_and_validate_manifest` at line 316, `play_cached` at line 431.
- `src/voice_agent_pipeline/audio/filler.py` — the module being deleted. The `pick_filler` shape at line 49 and `maybe_play_filler` at line 103 are the precise templates `pick_opener` and `trigger_opener_fallback` mirror.
- `src/voice_agent_pipeline/activity/greeting.py` — the parallel "static random pick + last-resort fallback" pattern. The opener's selection is closer to filler's than greeting's, but greeting's `trigger_greeting()` is the cleaner example of "spawn a task + plumb the result back to the runtime".
- `src/voice_agent_pipeline/config/setup.py:579-647` — `GreetingConfig` and `FillerConfig` definitions; the templates for `OpenersConfig`. Mirror the docstring depth.
- `src/voice_agent_pipeline/splitter/state_machine.py` — the char-by-char SSML parser. The `<emotion value="X"/>` handler is the template for `<opener bucket="X"/>`.
- `src/voice_agent_pipeline/splitter/segmenter.py` — the boundary-based emitter. Where `EmotionEvent` is consumed, `OpenerEvent` parallels.
- `prompts/talker_system.md` — the Talker system prompt; the file to extend with opener-tag teaching.
- `setup.toml` — the operator config. `[greeting]`, `[filler]`, `[goodbye]` block patterns are the template.

### Risks & mitigations

- **PyAudio device-serialization fails under load.** Mitigation: the integration `test_overlap_timing.py` exercises the handoff with both fast and slow Cartesia mock-times. If the test detects a regression vs the documented expected order, the bug is a stray application-level await — find and remove. Do NOT add a lock back.
- **Late-arriving `<opener .../>` tag** (Talker emits it after the timer fallback has fired) creates an opener-stacking scenario. Mitigation: the splitter callback checks `opener_already_playing` and silently drops if set (log `opener.late_tag` DEBUG). Operator audibly hears one opener per turn.
- **Opener WAV duration mismatch.** If the rendered opener is longer than expected, it occupies the speaker through the start of the real reply, eating into NFR34's gap budget. Mitigation: `OpenersConfig` doesn't constrain phrase length, but the operator-facing comment in `setup.toml` recommends ≤ 1.5 s per phrase. The `[openers]` validator does not enforce this in v1; soak (Story 6.4) measures and tunes.
- **Talker prompt fails to use the tag.** Mitigation: the timer fallback (FR56) preserves opener onset even when no tag fires. Soak (Story 6.4) measures `opener_source` field distribution (`"llm_tag"` vs `"timer_fallback"`); if `"timer_fallback"` dominates, that's a prompt-tuning issue, not a code defect.
- **Splitter regression on the `<emotion .../>` tag** while modifying `state_machine.py`. Mitigation: keep the existing emotion-tag fixtures + tests untouched; add new opener-tag fixtures + tests in parallel. The CHAR-by-char state machine is the riskiest part of this story — keep the diff narrow and run the splitter unit suite obsessively while iterating.

## Dev Agent Record

### Agent Model Used

(populated by dev agent)

### Debug Log References

### Completion Notes List

### File List
