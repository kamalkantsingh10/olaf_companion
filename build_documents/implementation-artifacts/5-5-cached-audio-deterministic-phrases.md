# Story 5.5: Pre-rendered cached audio for deterministic phrases (greetings, goodbyes, clarifications, thinking fillers)

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

**Execution-order note:** Numerically this is the last Epic 5 story but it MUST land **before** Story 5.4 (soak + v1 sign-off). Reason: the soak measures NFR1 latency and Cartesia cost characteristics; if cached-audio playback isn't in place, soak measures the wrong baseline. Recommended order in Epic 5: 5.1 → 5.2 → 5.3 → **5.5** → 5.4.

## Story

As Kamal,
I want every deterministic-text audio surface (wake greetings, goodbyes, low-confidence clarifications, and a new "thinking" filler that fires while STT+Talker+TTS run) to play from **pre-rendered cached WAV files** rather than hitting Cartesia at runtime,
so that (a) Cartesia spend drops to near-zero for these four surfaces — they're called every wake/sleep/clarification/turn and produce 80-90% of Cartesia's per-day character count even though their text is fully deterministic; (b) end-of-speech → first audio frame perceived latency improves because the cached filler fires within ~50 ms of VAD end-of-speech while the real Talker+Cartesia chain (~1.5-2 s) runs in parallel; (c) the wake-greeting / goodbye / clarification paths gain ~700-1500 ms by not waiting for Cartesia TTFB on text that never changes.

## Acceptance Criteria

1. **Four cached audio surfaces.** All four read their text lists from existing `setup.toml` sections and play pre-rendered WAV files instead of calling Cartesia at runtime:

   | Surface | Existing config block | Existing text count | Behavior change |
   |---|---|---|---|
   | Wake greetings | `[greeting.greetings_by_mood]` (8 mood buckets) | ~80 phrases | `sequential_loop._wait_for_wake` → `activity.greeting.trigger_greeting` returns text **and** audio-file path; loop plays cached file, no Cartesia call |
   | Goodbyes | `[goodbye] phrases` | ~12 phrases | `sequential_loop`'s goodbye-pre-sleep block plays cached file, no Cartesia call |
   | Clarifications | `[stt] clarification_prompts` | ~40 phrases | Story 3.7's clarification short-circuit (`cdf3618`) plays cached file, no Cartesia call. The `clarification.picked` log + downstream FSM transitions stay |
   | **Thinking fillers** (NEW) | `[filler.phrases_by_mood]` (new TOML section) | ~50 phrases starter | Fires on VAD end-of-speech if Cartesia first-frame hasn't arrived within `[filler] min_pause_ms` (default 400). Plays cached file matching current mood |

2. **Audio asset layout under `assets/audio/`** (committed to repo per Kamal's 2026-05-12 design call):

   ```
   assets/audio/
   ├── manifest.json                    # phrase-hash → file-path map; built by the recipe
   ├── greetings/
   │   ├── calm/   {01..NN}.wav
   │   ├── happy/  {01..NN}.wav
   │   └── … (8 mood buckets)
   ├── goodbyes/
   │   └── {01..NN}.wav
   ├── clarifications/
   │   └── {01..NN}.wav
   └── fillers/
       ├── calm/   {01..NN}.wav
       └── … (8 mood buckets)
   ```

   - **File naming:** zero-padded sequence within each bucket (`01.wav`, `02.wav`, ...). Stable across regenerations as long as the ordered TOML list is stable.
   - **Audio format:** 16 kHz mono S16LE WAV — matches pipeline-wide format (`audio/transport.py` `_SAMPLE_RATE = 16000`). No resampling at playback. Cartesia's SSE endpoint already returns raw S16LE PCM at 16 kHz; the recipe just wraps in a WAV header (~44 bytes) and writes to disk.
   - **Repo size impact:** ~180 files × ~1-3 s × 32 KB/s ≈ ~5-15 MB total. Acceptable per the project's repo-size budget; assets/ would be the only large directory and is easy to .gitignore later if it grows.
   - **`.gitattributes`:** add `assets/audio/**/*.wav binary` to suppress diff churn on regeneration.

3. **`manifest.json` schema (committed):**

   ```json
   {
     "schema_version": 1,
     "generated_at": "2026-05-12T20:00:00Z",
     "voice_id": "6ccbfb76-...",
     "tts_model": "sonic-3",
     "entries": [
       {
         "surface": "greeting",
         "mood": "calm",
         "phrase_hash": "sha256:abc123...",
         "phrase": "hey",
         "path": "assets/audio/greetings/calm/01.wav",
         "duration_ms": 412
       },
       …
     ]
   }
   ```

   - `phrase_hash = sha256(phrase + voice_id + tts_model)` — change any of those and the hash mismatches, forcing regeneration.
   - `duration_ms` lets the filler logic decide whether a filler will fit the expected gap.
   - Loaded once at startup into a `CachedAudioManifest` pydantic model under `src/voice_agent_pipeline/audio/cached.py`.

4. **`just regenerate-audio` recipe** (new in `justfile`):

   ```python
   # src/voice_agent_pipeline/audio/regenerate.py — runnable as `python -m voice_agent_pipeline.audio.regenerate`
   ```
   - Reads `setup.toml` blocks: `[greeting.greetings_by_mood]`, `[goodbye] phrases`, `[stt] clarification_prompts`, `[filler.phrases_by_mood]`.
   - For each phrase: synthesize via `CartesiaClient.generate(...)` at the configured voice/model, write WAV to the canonical path, update `manifest.json`.
   - **Idempotent:** re-runs skip phrases whose `phrase_hash` is already in `manifest.json` with a matching file on disk.
   - **Pruning:** removes manifest entries (and files) for phrases no longer in `setup.toml`. Logs every action.
   - **CLI ergonomics:** `--dry-run` prints what would be regenerated; `--force` regenerates everything regardless of cache.
   - Uses the existing `CartesiaClient` — no new dep, no new SDK surface.

5. **`[filler]` section in `setup.toml`** (new):

   ```toml
   # Story 5.5: thinking-filler audio cached + played on VAD end-of-speech if
   # the real Talker+Cartesia chain hasn't produced its first audio frame within
   # `min_pause_ms`. Mood-bucketed like greetings so Ooppi's filler matches her
   # current mood. Operator should expand to 30-40 entries per mood over time.
   [filler]
   min_pause_ms = 400  # threshold: fire filler only when real audio is late
   max_consecutive_repeat = 0  # don't pick the same filler twice in a row

   [filler.phrases_by_mood]
   calm = ["hmm", "uh", "umm", "let me think", "one moment", "okay", "right"]
   happy = ["oh!", "ooh", "hmm yeah", "okay!", "let me see"]
   playful = ["oo!", "hmm hmm", "huh", "lemme see", "wait wait"]
   curious = ["hmm interesting", "ooh", "lemme think", "uhh", "okay so"]
   thoughtful = ["hmm", "let me think", "right", "okay", "mm-hmm"]
   sleepy = ["mmh", "uh", "lemme see", "hmm", "mm okay"]
   grumpy = ["hmm", "uh", "right", "okay", "let me see"]
   excited = ["oh!", "ooh!", "okay okay", "wait wait", "uhh"]
   ```

   - In `src/voice_agent_pipeline/config/setup.py`:
     - Add `class FillerConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")`:
       - `min_pause_ms: int = Field(default=400, gt=0)`
       - `max_consecutive_repeat: int = Field(default=0, ge=0)`
       - `phrases_by_mood: dict[Mood, list[str]] = Field(default_factory=lambda: _DEFAULT_FILLERS)`
     - `model_validator(mode="after")`: every `Mood` Literal value has ≥1 filler entry (parallel to `GreetingConfig._validate_*` from Story 4.5).
     - `filler: FillerConfig = Field(default_factory=FillerConfig)` on `SetupConfig`.

6. **`src/voice_agent_pipeline/audio/cached.py` (new module).** Single import boundary for the audio-asset side of the pipeline (parallel to `audio/devices.py` boundary-concentration). Exports:

   ```python
   class CachedAudioManifest(BaseModel):
       """Loaded once at startup; immutable thereafter."""
       schema_version: int
       generated_at: datetime
       voice_id: str
       tts_model: str
       entries: list[CachedAudioEntry]

       def lookup(self, surface: Literal["greeting","goodbye","clarification","filler"],
                  phrase: str, mood: Mood | None = None) -> CachedAudioEntry: ...

   def load_manifest(path: Path) -> CachedAudioManifest: ...

   async def play_cached(pa: pyaudio.PyAudio, output_index: int, path: Path) -> None:
       """Play a cached WAV to the configured output device.

       Mirrors the streaming-write shape of sequential_loop._speak but reads
       from disk via `wave.open` instead of from Cartesia SSE. Stops + closes
       the stream after the file ends. ~1-5 ms startup cost vs Cartesia's
       700-1500 ms TTFB.
       """
   ```

   - Uses stdlib `wave` for parsing (no new dep).
   - Direct PyAudio output stream — same format already used by `_speak`.

7. **Startup validation invariant (new in `__main__.py` Stage 3):**

   After `audio devices openable`, add:

   ```python
   async with reporter.stage("audio_assets", "audio assets present"):
       manifest = await asyncio.to_thread(
           load_and_validate_manifest,
           config=config,
           manifest_path=Path("assets/audio/manifest.json"),
       )
   ```

   `load_and_validate_manifest` checks:
   - manifest.json exists and parses
   - `voice_id` and `tts_model` match `config.tts.voice_id` and `config.tts.model` (else: stale assets, regenerate)
   - Every phrase in `setup.toml`'s static lists (greetings, goodbyes, clarifications, fillers) has a matching `phrase_hash` in the manifest
   - Every manifest entry's `path` exists on disk

   Failure → `StartupValidationError(stage="audio_assets", reason="...", action="run `just regenerate-audio`")`. Operator sees a clean instruction to regenerate.

8. **Runtime change — greeting / goodbye / clarification paths in `sequential_loop`:**

   - Today: `await _speak(pa, indices, tts, text)` — calls Cartesia, streams frames.
   - After: `await play_cached(pa, indices.output_index, manifest.lookup(<surface>, text, mood).path)` — plays from disk.
   - The `_speak` function is NOT removed — it's still used for real conversational replies. Only the deterministic-text call sites switch.
   - Logs: rename `tts.speak.start` → `cached_audio.play.start` on the new path; keep `tts.speak.start` on real-reply path. Operator can tell them apart in voice-agent.log.

9. **Runtime change — new thinking-filler path.** After VAD captures the utterance and the FSM transitions to `working`, start a parallel task:

   ```python
   filler_task = asyncio.create_task(
       _maybe_play_filler(
           pa=pa,
           indices=indices,
           mood=mood_controller.current(),
           manifest=manifest,
           min_pause_ms=config.filler.min_pause_ms,
           audio_started=audio_started_event,  # asyncio.Event set by _speak on first frame
       ),
   )
   ```

   The function:
   - `await asyncio.sleep(min_pause_ms / 1000.0)`
   - if `audio_started.is_set()`: return without playing — fast turn, no filler needed.
   - else: pick a filler matching current mood (last-N suppression), play it.
   - If `audio_started` fires while filler is playing: let filler finish, then `_speak` queues naturally after.

   **Mood lookup falls back** to `calm` bucket if current mood's bucket is empty (parallel to greeting fallback).

   **Last-N suppression:** keep a small ring buffer (size = `max_consecutive_repeat + 1`) of recently-played filler hashes; pick from `bucket - recent` until that's empty, then reset.

10. **Tests:**

    Unit:
    - `tests/unit/audio/test_cached.py` — `CachedAudioManifest.lookup` happy path + missing-phrase raise; `load_and_validate_manifest` happy + stale-voice + missing-file paths.
    - `tests/unit/audio/test_filler.py` — `_maybe_play_filler` with mocked `asyncio.Event`: fast-response case (no filler), slow-response case (filler plays), last-N suppression (same filler not picked twice).
    - `tests/unit/config/test_setup.py` — extend with `[filler]` block parse + mood-bucket completeness validator.

    Contract:
    - `tests/contract/test_audio_manifest.py` — manifest.json schema_version contract + round-trip.

    Integration:
    - `tests/integration/test_cached_greeting.py` — wake → greeting plays from disk, no Cartesia network call (mock at the openai/Cartesia SDK boundary; assert it was NEVER called for the greeting).
    - `tests/integration/test_filler_timing.py` — synthesize an end-of-speech event, delay the mock TTS first-frame by 800 ms, assert filler audio played starting at ~400 ms.

11. **Documentation updates** (same commit):

    - `README.md`: add an "Audio assets" section covering `just regenerate-audio`, the manifest, and when to re-run (after editing `setup.toml` phrase lists or changing `voice_id`).
    - `setup.toml` comment blocks: `[greeting]`, `[goodbye]`, `[stt] clarification_prompts`, `[filler]` each gain a note "Edits here require `just regenerate-audio` before next startup — the asset-manifest check will refuse to start otherwise."
    - `architecture.md` line ~83 (FR cluster table): "Audio playback" row note that deterministic-text surfaces use cached WAVs; only conversational replies hit Cartesia at runtime.

12. **Commit policy (Task 7, this story):** Single commit per `feedback_commit_policy.md`. Files: new module `audio/cached.py`, regenerate recipe `audio/regenerate.py`, config schema `config/setup.py` extension, `__main__.py` startup probe, `sequential_loop.py` call-site swaps, `assets/audio/manifest.json` + the WAV files (initial render), `setup.toml`, `.gitattributes`, README + planning-doc touch, tests. Push immediately after commit per `feedback_push_after_commit.md`.

## Tasks / Subtasks

- [ ] **Task 1: Config schema + `[filler]` TOML block** (AC: #5)
  - [ ] Add `FillerConfig` to `config/setup.py` with the validator
  - [ ] Add `_DEFAULT_FILLERS` module-level constant
  - [ ] Mount `filler: FillerConfig` on `SetupConfig`
  - [ ] Add `[filler]` + `[filler.phrases_by_mood]` to `setup.toml`
  - [ ] Unit test: `tests/unit/config/test_setup.py` extension

- [ ] **Task 2: `audio/cached.py` module — manifest + playback** (AC: #6)
  - [ ] `CachedAudioEntry` + `CachedAudioManifest` pydantic models
  - [ ] `load_manifest(path)` function
  - [ ] `play_cached(pa, output_index, path)` async function (parses WAV header via stdlib `wave`, streams to PyAudio)
  - [ ] Unit tests in `tests/unit/audio/test_cached.py`

- [ ] **Task 3: `audio/regenerate.py` recipe** (AC: #4)
  - [ ] Reads `setup.toml`, iterates all four surfaces
  - [ ] Calls existing `CartesiaClient.generate(...)` for each phrase
  - [ ] Writes WAV to canonical path; updates manifest
  - [ ] `--dry-run`, `--force`, prune-stale flags
  - [ ] `justfile` recipe: `regenerate-audio: uv run python -m voice_agent_pipeline.audio.regenerate`
  - [ ] First-run generation: produce `manifest.json` + all WAVs; commit

- [ ] **Task 4: Startup validation probe** (AC: #7)
  - [ ] `load_and_validate_manifest(config, manifest_path)` function in `audio/cached.py`
  - [ ] Wire into `__main__.py` Stage 3 after `audio devices openable`
  - [ ] Wrap missing-asset failures as `StartupValidationError(stage="audio_assets")`
  - [ ] Error message names the operator action: "run `just regenerate-audio`"

- [ ] **Task 5: Call-site swaps — greeting / goodbye / clarification** (AC: #8)
  - [ ] `sequential_loop` greeting path: lookup + `play_cached`
  - [ ] `sequential_loop` goodbye path: same
  - [ ] `pipeline.py` / `sequential_loop` clarification short-circuit: same
  - [ ] Log rename: `cached_audio.play.start` on cached paths

- [ ] **Task 6: Filler timer + interleaving** (AC: #9)
  - [ ] `audio_started: asyncio.Event` plumbed from `_speak`'s first-frame point
  - [ ] `_maybe_play_filler` async function in `sequential_loop`
  - [ ] Last-N suppression ring buffer
  - [ ] Mood-fallback to `calm` if current mood's filler bucket is empty
  - [ ] Unit + integration tests (`test_filler.py`, `test_filler_timing.py`)

- [ ] **Task 7: Docs + commit** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] README.md "Audio assets" section
  - [ ] setup.toml comment-block notes
  - [ ] architecture.md row update
  - [ ] `just check` — must be green
  - [ ] Single commit, push to origin

## Dev Notes

### Relevant architecture patterns and constraints

- **Boundary concentration (`CLAUDE.md` + `architecture.md`):** `pyaudio` is imported in exactly two files today — `audio/devices.py` and `audio/transport.py`. `audio/cached.py` becomes the third allowed import site (it streams to a PyAudio output stream directly, mirroring `sequential_loop._speak`'s pattern). Update the architecture's "Architectural Boundaries" section to reflect the three legitimate import sites.

- **Pydantic at boundaries (`CLAUDE.md` rule 3):** `CachedAudioEntry`, `CachedAudioManifest`, `FillerConfig` all `BaseModel` with `extra="forbid"`. `surface` field is `Literal["greeting","goodbye","clarification","filler"]`.

- **No `enum.Enum` (`CLAUDE.md` rule 3):** Use `Literal[...]` for `surface`. Mirrors existing `Mood`, `ActivityState`, `WorkingSubmode` style.

- **External-service-error hygiene (`CLAUDE.md` rule 4):** `regenerate.py` calls Cartesia and can raise `CartesiaError` (existing `ExternalServiceError` subclass). The recipe is a CLI tool, not part of the pipeline runtime — catching `CartesiaError` there for a clean operator error message is allowed (CLAUDE.md rule 4 applies to "v1 code paths" = the pipeline runtime, not offline tools).

- **Fail-fast posture (project memory `project_v1_scope_fail_fast.md`):** Missing audio asset at startup → crash, don't try to fall back to runtime Cartesia. The operator-action message ("run `just regenerate-audio`") makes the failure recoverable. No silent degradation to "filler-less" or "Cartesia-fallback" modes.

- **Per-story commit (project memory `feedback_commit_policy.md`):** This story's Task 7 is the single commit. Do NOT split into per-task commits — the implementation-artifact pattern is one commit per story.

- **Audio format pinning:** 16 kHz mono S16LE is the pipeline-wide invariant (`audio/transport.py:_SAMPLE_RATE`, `audio/devices.py probe_devices_openable`, `stt/groq.py _WAV_SAMPLE_RATE_HZ`). The regenerate recipe MUST request this format from Cartesia (their SSE endpoint defaults to it for `sonic-3`; verify in the recipe).

### Source tree components to touch

| File | Action | Notes |
|---|---|---|
| `src/voice_agent_pipeline/config/setup.py` | Modify | Add `FillerConfig`, mount on `SetupConfig` |
| `src/voice_agent_pipeline/audio/cached.py` | New | Manifest + playback + validation |
| `src/voice_agent_pipeline/audio/regenerate.py` | New | CLI recipe |
| `src/voice_agent_pipeline/__main__.py` | Modify | Stage 3 audio-assets probe |
| `src/voice_agent_pipeline/sequential_loop.py` | Modify | Greeting / goodbye / clarification call-site swaps; filler timer |
| `setup.toml` | Modify | Add `[filler]` block; add comment-notes in 4 surface blocks |
| `.gitattributes` | New or modify | `assets/audio/**/*.wav binary` |
| `justfile` | Modify | Add `regenerate-audio` recipe |
| `assets/audio/manifest.json` | New | Generated artifact, committed |
| `assets/audio/**/*.wav` | New | Generated artifacts, committed (~5-15 MB) |
| `README.md` | Modify | Audio-assets section |
| `build_documents/planning-artifacts/architecture.md` | Modify | One row in FR-cluster table |
| `tests/unit/audio/test_cached.py` | New | |
| `tests/unit/audio/test_filler.py` | New | |
| `tests/unit/config/test_setup.py` | Modify | `[filler]` parse + validator |
| `tests/contract/test_audio_manifest.py` | New | |
| `tests/integration/test_cached_greeting.py` | New | |
| `tests/integration/test_filler_timing.py` | New | |

### Testing standards summary

- `just check` (ruff + pyright + `pytest tests/unit -q`) must be green pre-commit.
- Protocol-boundary mocking only (CLAUDE.md rule 7) — mock the openai SDK and PyAudio at their module boundaries; never mock internal functions or pydantic models.
- Audio-asset tests use small fixture WAVs (a few hundred ms of silence) under `tests/_fixtures/audio/`, NOT the production assets/audio/ tree.

### Project Structure Notes

- **`assets/` is a new top-level directory.** Per `CLAUDE.md` rule 2 ("Honor the module-by-domain layout. Don't introduce new top-level directories without updating `architecture.md`"), update `architecture.md`'s repo-layout diagram to include `assets/audio/`. Rationale: binary audio assets aren't source; they're build artifacts that happen to be committed for zero-friction setup. Living under `src/voice_agent_pipeline/audio/cached_assets/` would falsely imply they're Python.

- **`audio/cached.py` parallels `audio/devices.py`.** Both are utility modules with a small public API; both import `pyaudio` directly; both are exercised by startup-time probes. Same module-docstring style.

### References

- `build_documents/planning-artifacts/voice-agent-pipeline-brief.md` §"The Problem" #1 (dead air on complex turns) — the filler portion of this story directly targets this stated failure mode without adding any LLM call.
- `build_documents/planning-artifacts/prd.md` FR8 (low-confidence clarification routing), FR44 (wake greeting), and the goodbye behavior under FR46 (deferred-sleep) — all three deterministic-text surfaces this story converts.
- `build_documents/planning-artifacts/epics.md` Story 4.5 — `activity/greeting.py:trigger_greeting` is the call site this story extends; the static-random pattern this story applies to fillers is a direct copy.
- `build_documents/planning-artifacts/epics.md` Story 2.4 — clarification path defined here; Story 3.7's `cdf3618` short-circuit (also referenced) is the runtime call site to convert.
- `build_documents/planning-artifacts/sprint-change-proposal-2026-05-12.md` — STT Groq swap; Cartesia is the next-largest external service in the budget after Talker, and deterministic-text surfaces are ~80-90% of Cartesia's per-day chars in steady-state household use.
- `src/voice_agent_pipeline/audio/transport.py:_SAMPLE_RATE` — pinned 16 kHz mono S16LE, the format the cached WAVs must match.
- `src/voice_agent_pipeline/sequential_loop.py` — `_speak`, `_wait_for_wake`, goodbye-pre-sleep block, clarification short-circuit are the four call sites.
- `src/voice_agent_pipeline/activity/greeting.py:trigger_greeting` — the function whose pattern (static random pick + mood fallback + last-resort `"hey"`) the filler picker copies.
- `src/voice_agent_pipeline/tts/cartesia.py:CartesiaClient` — the SDK used by `regenerate.py` (no new dep).
- `setup.toml` `[greeting.greetings_by_mood]`, `[goodbye]`, `[stt] clarification_prompts` — the existing phrase lists this story consumes.

### Cost analysis (informs Task 3's recipe ergonomics)

- **One-time regeneration:** ~180 phrases × avg 25 chars = ~4500 chars ≈ 4500 Cartesia credits ≈ ~$0.11 (against Pro tier; less on the $49 plan's bundled budget). Negligible.
- **Steady-state savings (vs current runtime Cartesia for these surfaces):**
  - Wake greetings: ~5 wakes/day × ~5 chars = trivial alone, but…
  - Goodbyes: ~5 goodbyes/day × ~10 chars = trivial.
  - Clarifications: depends on STT accuracy — ~5-30 chars × 0-20 turns/day.
  - **Fillers (NEW):** *every turn that's slower than 400 ms*. With current Talker (~500 ms median) + Cartesia (~700-1500 ms) TTFB chain, **fillers would fire on ~95% of turns** — this is where the user-perceptible "alive" win is. If we ran fillers via Cartesia at runtime instead of caching, they'd be 150 turns/day × ~5 chars = ~750 chars/day = ~22k chars/month. ~$0.55/month.
  - **Combined savings on Cartesia bill:** modest in absolute terms (~$1-3/month) but ~80% of the deterministic-text bill, plus the latency win on fillers is the bigger story.

### Risks & mitigations

- **Regeneration drift:** operator edits a phrase in setup.toml and forgets to regenerate → startup fails. Mitigation: clear `StartupValidationError` message names the action.
- **Voice change:** operator changes `voice_id` → manifest's `voice_id` field mismatches → startup fails with "stale assets, regenerate". Same mitigation.
- **Filler-too-long:** filler audio is, say, 800 ms but real audio is ready at 500 ms → real audio waits for filler to finish. Mitigation: keep fillers short (the starter lists are 1-3 syllables). Document in setup.toml comment.
- **Filler ticky-feeling:** fires on every turn. Mitigation: 400 ms threshold + last-N suppression + 5-7 entries per mood gives a varied feel. Document the tunability.
- **Repo size growth:** ~15 MB upper bound. Mitigation: `.gitattributes` binary flag suppresses diff churn; can move to git LFS later if it ever grows.

## Dev Agent Record

### Agent Model Used

(populated by dev agent)

### Debug Log References

### Completion Notes List

### File List
