# Story 6.5: Cartesia → Gemini TTS provider migration (Live API streaming + batch re-render)

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want the TTS layer to migrate from Cartesia Sonic-3 to Google **Gemini TTS** (Live API for the streaming hot path, batch endpoint for cached-asset render) behind the existing `TTSClient` Protocol seam, selectable by a `[tts] provider` knob,
so that voice cost drops (~35% cheaper per minute) and I gain Gemini's 30-voice / prompt-steerable voice range — without regressing simple-turn latency (NFR4 ≤ 400 ms) or breaking the Story 6.2 cached-opener and Story 6.3 emphasis paths.

## Context & Decisions (frozen 2026-06-04)

These were decided with the user before authoring. Do **not** relitigate them mid-implementation.

- **Provider:** Gemini TTS via the `google-genai` SDK (dep `google-genai>=2.7.0` is already added to `pyproject.toml:23`).
- **Drivers:** cost + voice quality/options. (Not latency — latency must merely *not regress*.)
- **Placement:** Story 6.5, appended under Epic 6 (v2 Expression Upgrades).
- **Streaming surprise (load-bearing):** the dedicated Gemini TTS models (`gemini-2.5-flash-preview-tts`) **do not stream** — `generate_content` returns the whole clip in one blob. The **only** low-TTFB streaming path is the **Live API** (`client.aio.live.connect`, model `gemini-2.5-flash-live-preview`). Therefore:
  - **Hot path** (`GeminiClient.synthesize()`, NFR4-critical) → **Live API streaming**.
  - **Offline render** (`regenerate.py`, latency irrelevant) → MAY use the one-shot **batch** `generate_content` endpoint (cheaper/simpler); reusing `synthesize()` is acceptable if it keeps the code single-path. Implementer's call, documented in Completion Notes.
- **TTFB de-risk:** **spike-gated**. Task 2 measures Gemini Live-API TTFB with the existing Story 6.1 harness and **gates** the rest of the story. If the gate fails, STOP at a documented spike report — do not ship a half-migrated pipeline.
- **Sample rate:** Gemini emits **24 kHz** mono s16le PCM; the pipeline is pinned to **16 kHz**. **Resample 24k→16k inside `GeminiClient`** so the seam stays a one-file change and the rest of the pipeline + cached WAVs stay 16 kHz.
- **Word timestamps:** Gemini returns **none**, and precise per-word timing is **NOT required** (user decision, memory `project_tts_word_timestamps_not_required`). Emphasis still fires a ROS message per marked word at **approximate** timing — see AC8.
- **Cartesia is not deleted.** It stays selectable (`provider = "cartesia"`) — this is a provider *addition + default flip*, mirroring the Talker's multi-provider pattern (Story 2.2).

> ⚠️ **Pre-flight (process, not an AC):** the working tree currently holds an **unrelated** uncommitted latency/reliability hardening pass (`reasoning_effort`, error filler, a Cartesia voice swap + re-rendered `assets/audio/*.wav` + `manifest.json`). **Commit or stash that work before starting this story** — do not entangle it with the migration commit. Note the Cartesia voice swap re-rendered the cache for a Cartesia voice; this story re-renders it again for the Gemini voice (AC9), so coordinate the ordering.

## Acceptance Criteria

1. **Provider selection.** `TtsConfig` gains `provider: Literal["cartesia", "gemini"]` (default chosen per AC11) plus a `[tts.gemini]` sub-block (pydantic `BaseModel`, `extra="forbid"`). Setting `provider = "gemini"` routes all synthesis through the new `GeminiClient`; `provider = "cartesia"` preserves today's behavior byte-for-byte. No `enum.Enum`, no plain dicts at the boundary (CLAUDE.md rule 3).
2. **TTS factory.** A `build_tts_client(config) -> TTSClient` factory (in `tts/__init__.py`, mirroring `turn/__init__.py`'s talker factory) returns `CartesiaClient` or `GeminiClient` by `config.tts.provider`. All three current construction sites use it: `sequential_loop.py:177`, `pipeline.py:1185`, `audio/regenerate.py:354`. No `CartesiaClient(...)` literal remains outside the factory + `ttfb_spike.py`.
3. **Gemini TTFB spike gate (BLOCKING — Task 2).** Reusing the Story 6.1 harness (`tts/ttfb_spike.py`), a Gemini Live-API TTFB measurement runs the same 500-sample (cold/warm, length-stratified) protocol and writes a report under `build_documents/implementation-artifacts/`. **Gate:** Gemini Live p95 TTFB ≤ **400 ms** (NFR4). The report records median/p25/p75/p90/p95 split cold vs warm and an explicit PASS/FAIL verdict vs the Cartesia baseline (warm p50 230 ms, cold p50 467 ms from Story 6.1). If FAIL, AC4–AC10 are not attempted and the story is returned for a provider re-decision.
4. **Live-API streaming client.** `tts/gemini.py` defines `GeminiClient` implementing the `TTSClient` Protocol (`tts/client.py:26`): `synthesize(text: str) -> AsyncIterator[bytes]` (a normal `def` returning an async generator, matching the Protocol's documented calling convention). It opens a Live session (`client.aio.live.connect(model=<live_model>, config=...)`), sends `text`, and yields PCM chunks **as they arrive** (first chunk must not wait for the full utterance).
5. **24k→16k resample.** `GeminiClient` downsamples Gemini's 24 kHz mono s16le output to **16 kHz mono s16le** before yielding, so consumers (`sequential_loop.py:1125` playback at `paInt16`/1ch/16000, `regenerate.py` WAV writer at 16000) are unchanged. Resampling adds < 5 ms to TTFB and introduces no audible artifacts. Partial-frame carry across chunks must be handled (don't drop/duplicate samples at chunk boundaries).
6. **Error posture (CLAUDE.md rule 4).** A new `GeminiTtsError(ExternalServiceError)` (in `errors.py`, alongside `CartesiaError`) wraps Live-API connection / session / SDK errors and propagates — never caught in v1 paths. The process crashes and lets systemd restart; no graceful degradation.
7. **Style / emotion mapping.** Gemini has no SSML. `config.tts.default_emotion` (and any per-segment emotion already stripped by the splitter) maps to a Gemini mechanism: a natural-language style prefix and/or bracket tags (e.g. `[warmly]`). Document the mapping. The cleaned text the splitter already produces (emotion/vocalization tags stripped, published as ROS events) reaches Gemini as plain text exactly as it reached Cartesia — verify the splitter contract is unaffected.
8. **Emphasis at approximate timing (Story 6.3 preserved).** Because Gemini gives no word timestamps, `GeminiClient.last_segment_timing()` returns an **approximate** `SegmentTiming`: split the spoken text into words and distribute `start_ms`/`end_ms` evenly across the measured segment duration (derived from total PCM byte count ÷ 16 kHz). `_publish_emphasis_events` (`sequential_loop.py:812-888`) then emits one `vocalization(tag="emphasis", audio_frame_id=...)` per marked word **unchanged** — "around the word" is good enough. (Do not return `None`; that would silently skip emphasis. Verify the join still fires for `provider = "gemini"`.)
9. **Cached-asset re-render + startup probe.** With `provider = "gemini"`, `just regenerate-audio` re-renders every cached greeting / goodbye / clarification / opener via Gemini. The manifest's voice/model identity (`cached.py` `load_and_validate_manifest`, validated at `__main__.py:201-209`) reflects the **active provider's** identifiers, so the Stage-3 `audio_assets` probe passes on a freshly-rendered cache and still *fails loudly* on a voice/model mismatch. Provide `effective_voice_id()` / `effective_model()` on `TtsConfig` (returning the Cartesia or Gemini identifiers per `provider`) and route `regenerate.py` + the probe's hashing through them.
10. **No latency regression, no schema bump.** Simple-turn integration test stays green; `end_to_first_real_audio_ms` / `ttfb_ms` instrumentation (Story 6.4) keeps working for the Gemini path. `schema_version` stays **3** (no wire-shape change — the `emphasis` vocalization tag already exists; this story changes only the audio source). Audio output stays 16 kHz mono s16le end-to-end.
11. **Config back-compat + default.** Default `provider` value and the shipped `setup.toml` are set so the project's intended live config selects Gemini, while a `provider = "cartesia"` config still loads and runs (regression-safe). Existing Cartesia keys (`voice_id`, `model`, `speed`, `default_emotion`, `transport`) remain valid under the Cartesia branch.
12. **Spec-as-contract (NFR26, CLAUDE.md rule 9).** In the **same commit**, update the planning docs that name Cartesia as the v1/v2 TTS: `architecture.md` (TTS decision row + TTSClient seam), `decision-records.md` (note Gemini provider + the new DR or amendment for the streaming/timestamp trade-off), `epics.md` (add Story 6.5 under Epic 6), `prd.md` (NFR4 wording "Cartesia TTS latency" → provider-neutral; cost note), `voice-agent-pipeline-brief.md` + the distillate. Add `6-5-gemini-tts-migration` to `sprint-status.yaml`.

## Tasks / Subtasks

> Test-first throughout (red → green → refactor). Mock **only** at Protocol/SDK boundaries (CLAUDE.md rule 7) — mock the `genai` Live session, never internal functions or pydantic models. Run `just check` before the commit (rule 1).

- [ ] **Task 1 — Config: provider knob + Gemini sub-block (AC1, AC9, AC11)**
  - [ ] Add `provider: Literal["cartesia", "gemini"]` to `TtsConfig` (`config/setup.py:~359-420`).
  - [ ] Add `_GeminiTtsSection(BaseModel, extra="forbid")`: `voice_name: str` (e.g. `"Kore"`), `live_model: str = "gemini-2.5-flash-live-preview"`, `batch_model: str = "gemini-2.5-flash-preview-tts"`, optional `style_prompt: str = ""`. Mount as `gemini: _GeminiTtsSection = Field(default_factory=...)`.
  - [ ] Add `effective_voice_id()` / `effective_model()` methods returning the active provider's identifiers (Cartesia `voice_id`/`model` vs Gemini `voice_name`/`batch_model`).
  - [ ] Confirm `gemini_api_key` already exists on the top-level config (`config/setup.py:886`) — reuse it; the Live API uses the same Google AI Studio `GEMINI_API_KEY`.
  - [ ] Tests: parse a `provider="gemini"` TOML; `extra="forbid"` rejects typos; `effective_*` returns the right pair per provider.

- [ ] **Task 2 — Gemini Live TTFB spike + GATE (AC3)** ⛔ *blocks Tasks 3-9*
  - [ ] Extend `tts/ttfb_spike.py` (or add a sibling) to drive the Gemini Live API over the same 500-sample stratified protocol; capture first-PCM-chunk arrival as TTFB.
  - [ ] Write `6-5-gemini-ttfb-spike-report.md`: median/p25/p75/p90/**p95**, cold vs warm, PASS/FAIL vs the 400 ms NFR4 gate and the Cartesia baseline.
  - [ ] **STOP if FAIL.** Surface the report to the user for a provider re-decision; do not start Task 3.

- [ ] **Task 3 — `GeminiClient` (Live API + resample + approx timing) (AC4, AC5, AC6, AC7, AC8)**
  - [ ] `tts/gemini.py`: `GeminiClient.__init__(config: TtsConfig, api_key: SecretStr)` (same shape as `CartesiaClient.__init__`, `cartesia.py:204`); build `genai.Client(api_key=...)`.
  - [ ] `synthesize(text)`: open Live session, send text (+ style prefix from AC7), `async for response in session.receive()` → yield resampled 16 kHz PCM chunks; accumulate total bytes + word list for the approximate `SegmentTiming`.
  - [ ] 24k→16k resampler with cross-chunk partial-frame carry (AC5).
  - [ ] `last_segment_timing() -> SegmentTiming | None`: return the even-distribution approximation (AC8); reset to `None` at each `synthesize()` entry (mirror `cartesia.py:347`).
  - [ ] `GeminiTtsError(ExternalServiceError)` in `errors.py`; wrap Live-API/SDK exceptions; never catch downstream.
  - [ ] Tests: mock the `genai` Live session to yield two 24 kHz chunks → assert resampled 16 kHz bytes, incremental yield (first chunk before stream end), approximate `SegmentTiming` word count == word count, and that a session error surfaces as `GeminiTtsError`.

- [ ] **Task 4 — TTS factory + wire the 3 sites (AC2)**
  - [ ] `build_tts_client(config)` in `tts/__init__.py`; raise the standard missing-key startup error if `provider="gemini"` and `gemini_api_key is None` (mirror `turn/__init__.py:73-83`).
  - [ ] Replace literals at `sequential_loop.py:177`, `pipeline.py:1185`, `regenerate.py:354`. (`pipeline.py` is dormant per project memory — update for consistency, don't deepen it.)
  - [ ] Tests: factory returns the right class per provider; missing-key path raises the startup error.

- [ ] **Task 5 — Emphasis approximate-timing verification (AC8)**
  - [ ] Confirm `_publish_emphasis_events` (`sequential_loop.py:812`) fires for `provider="gemini"` using the approximate `SegmentTiming` (no code change expected there; the approximation lives in `GeminiClient`).
  - [ ] Test: a 2-marked-word segment under Gemini publishes 2 `vocalization(tag="emphasis")` events with plausible `audio_frame_id`s within the segment duration.

- [ ] **Task 6 — Offline render + cached re-render + probe (AC9)**
  - [ ] Route `regenerate.py` through the factory; have it + `cached.py` hashing use `effective_voice_id()`/`effective_model()`.
  - [ ] Run `just regenerate-audio` with `provider="gemini"`; commit the re-rendered `assets/audio/*.wav` + updated `manifest.json` (Gemini voice/model identity).
  - [ ] Verify `__main__.py:201` Stage-3 probe passes on the fresh cache and still fails on a deliberate voice mismatch.

- [ ] **Task 7 — Integration + latency check (AC10)**
  - [ ] Update `tests/integration/test_opener_overlap_timing.py` / `test_emphasis_event.py` if they construct Cartesia directly.
  - [ ] Simple-turn integration green; confirm `ttfb_ms`/`end_to_first_real_audio_ms` logged for Gemini; assert no NFR4 regression vs the Task 2 spike.

- [ ] **Task 8 — Spec-as-contract updates (AC12)**
  - [ ] Update `architecture.md`, `decision-records.md`, `epics.md` (add Story 6.5), `prd.md` (provider-neutral NFR4 + cost note), `voice-agent-pipeline-brief.md`, distillate.
  - [ ] Add `6-5-gemini-tts-migration: ready-for-dev` (then → review on completion) to `sprint-status.yaml`.

- [ ] **Task 9 — Quality gate + commit (CLAUDE.md rules 1, plus per-story commit policy)**
  - [ ] `just check` green (ruff lint+format, pyright, fast pytest).
  - [ ] Single per-story commit (do not batch); push to remote immediately after (project memory `feedback_commit_policy`, `feedback_push_after_commit`).

## Dev Notes

### Source tree — exact touchpoints (verified file:line)

| File | Why it matters |
|---|---|
| `src/voice_agent_pipeline/tts/client.py:12-37` | `TTSClient` Protocol — `synthesize(text)->AsyncIterator[bytes]`, declared `def` (not `async def`) on purpose; match it. |
| `src/voice_agent_pipeline/tts/cartesia.py:204-287` | `CartesiaClient` shape to mirror: `__init__(config, api_key)`, `synthesize` dispatch, `last_segment_timing` property, `Word`/`SegmentTiming` models (`:89-142`), `_word_timestamps_to_words` (`:145`). |
| `src/voice_agent_pipeline/config/setup.py:359-420` | `TtsConfig` (voice_id req'd, default_emotion="neutral", model="sonic-3", speed=0.9, transport Literal; `extra="forbid"`). |
| `src/voice_agent_pipeline/config/setup.py:886` | `gemini_api_key: SecretStr \| None` already present — reuse. |
| `src/voice_agent_pipeline/config/setup.py:166` | `_GeminiTalkerSection` — copy this pattern for `_GeminiTtsSection`. |
| `src/voice_agent_pipeline/turn/__init__.py:73-99` | Talker provider factory + missing-key startup error — copy this pattern for `build_tts_client`. |
| `src/voice_agent_pipeline/sequential_loop.py:177` | Live construction site (this is prod — half-duplex loop, NOT pipeline.py; project memory `project_live_turn_loop`). |
| `src/voice_agent_pipeline/sequential_loop.py:1112-1132` | Playback drain: `paInt16` / 1ch / `rate=_SAMPLE_RATE(16000)`; first-chunk TTFB stamp. Resample target. |
| `src/voice_agent_pipeline/sequential_loop.py:812-888` | `_publish_emphasis_events` — reads `tts.last_segment_timing()`, skips on `None`. AC8 feeds it approximate timing so it fires. `frame_id=f"seg-{i}-w-{word.start_ms}"`. |
| `src/voice_agent_pipeline/audio/regenerate.py:141-169,354` | Offline render: drains `synthesize()`, writes 16 kHz/1ch/16-bit WAV; hashes phrase×voice_id×model. |
| `src/voice_agent_pipeline/audio/cached.py:388-508` | `load_and_validate_manifest` — Invariant 2 (`manifest.voice_id == config.tts.voice_id`, `manifest.tts_model == config.tts.model`) → use `effective_*`. |
| `src/voice_agent_pipeline/__main__.py:201-209` | Stage-3 `audio_assets` probe call site. |
| `src/voice_agent_pipeline/tts/ttfb_spike.py:366,686` | TTFB harness (Story 6.1) — reuse for the Task 2 gate; note it reads `default_emotion`/forces `transport="websocket"` (Cartesia-specific — branch or fork for Gemini). |
| `src/voice_agent_pipeline/errors.py` | `CartesiaError(ExternalServiceError)` lives here — add `GeminiTtsError` beside it. |
| `pyproject.toml:23` | `google-genai>=2.7.0` already added. |

The pipeline pins 16 kHz in **three** module-scope constants: `audio/transport.py:27`, `sequential_loop.py:90`, `audio/regenerate.py:66`. AC5 keeps all three valid by resampling at the seam — **do not** change them.

### Gemini SDK specifics (researched 2026-06-04 — flag any drift on SDK bump)

- **Auth:** `from google import genai` → `genai.Client(api_key=...)` reads the same AI Studio `GEMINI_API_KEY` (Developer API, *not* the OpenAI-compat endpoint the Talker uses; same credential). Vertex/ADC not needed.
- **Live streaming (hot path):** `async with client.aio.live.connect(model="gemini-2.5-flash-live-preview", config=cfg) as session: await session.send_realtime_input(text=...)` then `async for r in session.receive(): r.server_content.model_turn.parts[].inline_data.data` → bytes, **24 kHz s16le mono**, incremental. `turn_complete` ends the turn. Prefer typed `types.LiveConnectConfig` / `types.SpeechConfig` / `types.VoiceConfig` / `types.PrebuiltVoiceConfig(voice_name=...)` over dict form (CLAUDE.md rule 3).
- **Batch (optional, offline only):** `client.models.generate_content(model="gemini-2.5-flash-preview-tts", contents=..., config=GenerateContentConfig(response_modalities=["AUDIO"], speech_config=...))` → full clip, same 24 kHz s16le. Does **not** stream.
- **Voices:** 30 prebuilt (Kore, Puck, Charon, Zephyr, Fenrir, Leda, …). Pick a default in `setup.toml`; the user picks the final voice during AC9 re-render (a "voice quality" driver — surface a couple of candidates).
- **Style/emphasis:** natural-language prefix ("Say warmly:") and/or bracket tags (`[whispers]`, `[excited]`). No SSML, no `<emphasis>`. Emphasis on a specific word is fuzzy (prompt instruction / caps) — but AC8's timing is independent of acoustic emphasis (we anchor the ROS event by position, the body renders the cue).
- **Output:** raw PCM, no WAV header (you already wrap in `regenerate.py`).
- **All TTS/Live models are PREVIEW.** Risk: a preview deprecation is a hard outage under the v1 fail-fast stance (memory `project_v1_scope_fail_fast`). This is v2 work, but call it out in `decision-records.md` (AC12).
- **Pricing (the cost driver):** Gemini 2.5 Flash TTS ≈ $0.019/min, Live ≈ $0.018/min vs Cartesia Sonic ≈ $0.03/min → ~35-40% cheaper; token billing also charges the style-prompt text, so keep prefixes short.

### Regressions to protect (the system must still work end-to-end, not just pass ACs)

- **Story 6.2 opener overlap:** the real-answer synthesis overlaps opener playback. Gemini Live opens a WebSocket session per `synthesize()` — ensure session setup latency doesn't reintroduce the deleted serialization tax. The Task 2 spike measures cold (new session) vs warm; if cold session-open dominates, consider a warm/pooled session (note it, don't over-engineer in v1).
- **Story 6.3 emphasis:** must keep emitting (AC8). The current code path *skips* on `None` timing — that's the trap; the approximation is what keeps it alive.
- **Stage-3 probe:** a Cartesia-rendered manifest + `provider="gemini"` config must fail loudly with the "run `just regenerate-audio`" action (AC9) — that's correct behavior, not a bug.
- **`ttfb_spike.py`** reads Cartesia-only fields — fork/branch it for Gemini rather than breaking the Cartesia spike.

### Testing standards

- Framework: pytest; `just check` runs ruff (lint+format) + pyright + fast pytest.
- Mock the `genai` Live session object (the SDK boundary) to yield canned 24 kHz chunks; assert resampling, incremental yield, approximate timing, and `GeminiTtsError` wrapping. Never mock `GeminiClient` internals or pydantic models (rule 7).
- Keep the live-network spike (Task 2) out of the fast suite — it's an operator-run harness, like the Story 6.1 spike.

### Project Structure Notes

- New module `tts/gemini.py` sits beside `tts/cartesia.py` — no new top-level dir, so `architecture.md`'s module-by-domain layout is unaffected (CLAUDE.md rule 2), but the TTS decision row + seam description still need the provider-addition note (AC12).
- `snake_case` everywhere (rule 5): TOML keys (`voice_name`, `live_model`, `batch_model`, `style_prompt`), Python, log fields.
- Factory pattern is the established precedent (`turn/__init__.py`) — follow it; don't invent a registry.

### References

- [Source: src/voice_agent_pipeline/tts/client.py#TTSClient] — Protocol contract + the `def`-not-`async def` rationale.
- [Source: src/voice_agent_pipeline/tts/cartesia.py#CartesiaClient] — shape to mirror; `Word`/`SegmentTiming`/`_word_timestamps_to_words`.
- [Source: src/voice_agent_pipeline/turn/__init__.py#L73-99] — multi-provider factory + missing-key startup error precedent (Story 2.2).
- [Source: src/voice_agent_pipeline/config/setup.py#TtsConfig, #_GeminiTalkerSection, #L886] — config seam + existing `gemini_api_key`.
- [Source: src/voice_agent_pipeline/sequential_loop.py#L812-888, #L1112-1132] — emphasis join + playback drain.
- [Source: src/voice_agent_pipeline/audio/cached.py#load_and_validate_manifest, src/voice_agent_pipeline/__main__.py#L201] — Stage-3 voice/model probe.
- [Source: build_documents/planning-artifacts/decision-records.md#DR-001] — Cartesia TTFB keystone; baseline warm p50 230 ms / cold p50 467 ms; NFR4 gate framing.
- [Source: build_documents/planning-artifacts/decision-records.md#DR-002, #DR-004] — emphasis-as-7th-vocalization-tag; head-motion timing (now satisfied by approximate timing, memory `project_tts_word_timestamps_not_required`).
- [Source: build_documents/planning-artifacts/prd.md#NFR4] — "≤ 400ms at p95" TTS first-frame; provider-neutral after AC12.
- [Gemini speech-generation] https://ai.google.dev/gemini-api/docs/speech-generation — TTS doesn't stream; format; voices; style tags.
- [Gemini Live API] https://ai.google.dev/gemini-api/docs/live-api/capabilities — streaming session, 24 kHz s16le PCM, no word timestamps.
- [Gemini pricing] https://ai.google.dev/gemini-api/docs/pricing — the cost driver.

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
