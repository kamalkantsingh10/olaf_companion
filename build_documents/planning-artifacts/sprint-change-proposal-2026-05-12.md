---
proposalDate: '2026-05-12'
trigger: cloud-stt-accuracy-deviation
proposalStatus: APPROVED
mode: direct-adjustment
scopeClassification: moderate  # reverses brief Core Decision #3 + brief Problem #3; net code change is small
artifactsTouched:
  - .env.example
  - setup.toml
  - src/voice_agent_pipeline/stt/groq.py (new)
  - src/voice_agent_pipeline/stt/__init__.py
  - src/voice_agent_pipeline/config/setup.py
  - src/voice_agent_pipeline/__main__.py
  - src/voice_agent_pipeline/errors.py
  - tests/unit/stt/test_groq.py (new)
  - tests/unit/stt/test_init.py
  - tests/contract/test_setup_config.py
  - build_documents/planning-artifacts/prd.md
  - build_documents/planning-artifacts/architecture.md
  - build_documents/planning-artifacts/voice-agent-pipeline.md
  - build_documents/planning-artifacts/voice-agent-pipeline-brief.md
  - build_documents/planning-artifacts/epics.md
  - build_documents/planning-artifacts/validation-report-2026-05-06.md
  - build_documents/planning-artifacts/sprint-change-proposal-2026-05-12.md (this file)
schemaVersionBump: 'none (wire schema unchanged)'
storyCountChange: 'no story renumber; Story 1.7 AC amended in-place; Epic 1 FR list amended'
---

# Sprint Change Proposal — 2026-05-12 Cloud STT Adoption (Groq Whisper-Large-V3-Turbo)

**Workflow:** ad-hoc deviation (no `bmad-correct-course` ceremony — single-fix scope)
**Driven by:** Claude (assistant) on Kamal's direction
**Date:** 2026-05-12
**Mode:** Direct adjustment (single coherent change)

## Section 1: Issue Summary

**Triggering issue:** Kamal reported degraded STT accuracy in field use — specifically Indian-accented English misrecognitions. Investigation (this conversation, 2026-05-12) explored three remediation paths:

1. Bump the local Whisper variant (e.g., `Systran/faster-distil-whisper-small.en` → `faster-distil-whisper-large-v3`). Free win on the existing on-device path; bounded gain on Indian-accent specifically.
2. Swap to an accent-specific fine-tune (e.g., `Tejveer12/Indian-Accent-English-Whisper-Finetuned`, large-v3-turbo-derived). Targeted fix; CT2 conversion needed; ~750 MB local weights; locks compute to PC-class GPU.
3. Swap to a cloud STT API. Eliminates local compute as a bottleneck; multiple providers (Groq, OpenAI, Deepgram, Google) viable; Groq's Whisper-Large-V3-Turbo offers ~216× real-time at $0.04/hr.

**Decision:** Kamal selected path #3 with Groq as the v1 default. Rationale (from the conversation):

- **Local Pi-class compute is the real constraint going forward.** The brief originally assumed on-device STT on Pi 5 + Hailo-8L. Investigation 2026-05-12 confirmed Hailo's GenAI Model Zoo caps at Whisper-Small (244M, no distil variant, no Indian-accent fine-tune at that size class). Best-case Hailo deployment would *regress* accuracy vs the current `distil-small.en` on PC. Cloud STT decouples accuracy from the embedded-compute ceiling.
- **The latency objection no longer holds.** Brief Problem #3 cited "a round-trip" as a reason to reject cloud STT. Groq's LPU + co-located audio endpoint clears the NFR3 budget (≤500 ms end-of-speech → transcript ready) at p95 in expected operating conditions; the round-trip is no longer the dominant factor.
- **The privacy footprint is acknowledged, not eliminated.** Cartesia (TTS) and the Talker provider (OpenAI/Groq/Gemini) already see *text* of every utterance. Cloud STT adds *audio* of every utterance to the external surface. The trade is deliberate, documented here, and tunable: `setup.toml` operators retain `whisper-cpu` as an opt-in offline alternative.
- **Cost is bounded.** Whisper-Large-V3-Turbo at $0.04/hr means ~$0.60/month for 30 min/day of conversation — well inside hobby-scale.

**Categorization:** *Reversal of a brief-level architectural decision driven by a hardware-feasibility finding and a latency-assumption revision.* This is NOT a drift discovered in shipped code (cf. 2026-05-10's `expression_data` boundary repair). It is a deliberate inversion of brief Problem #3 ("Cloud-dependent transcription… not acceptable for a local personal agent") and brief Core Decision #3 ("On-device STT… cloud STT is explicitly excluded for v1"). Honoring NFR26 (spec-as-contract): when the project's stated values shift, the four canonical documents (prd, brief, voice-agent-pipeline.md, architecture.md) must shift with the code in the same commit.

**Concurrent change (bundled? No.):** A separate, smaller change — startup-time mic + speaker openability probe — was raised in the same conversation. That change lands in a **separate commit**: it's additive (new startup invariant), uncontroversial (no spec value reversed), and merging it into this proposal would muddy the audit record of the brief-level reversal. The mic/speaker probe gets its own focused commit and its own (smaller) update to epics.md's startup-validation list.

**Discovery context:** Kamal raised the accuracy issue, asked about Qwen ASR, then Hailo, then Indian-accent fine-tunes — surfacing along the way that Hailo deployment caps at Whisper-Small without an Indian-accent variant. Cloud STT is the cleanest path to accuracy *and* unblocks the embedded port: a Pi 5 + Hailo build can target a 16 GB Pi running cloud STT, freeing the NPU budget for other workloads (vision, future on-device LLM).

**Evidence:**
- `build_documents/planning-artifacts/voice-agent-pipeline-brief.md:27` — Problem #3 (cloud-dependent transcription rejected).
- `build_documents/planning-artifacts/voice-agent-pipeline-brief.md:51` — Core Decision #3 (on-device STT, cloud STT "explicitly excluded for v1").
- `build_documents/planning-artifacts/prd.md:589` — FR6 (on-device transcription requirement).
- `build_documents/planning-artifacts/architecture.md:83,937,1043` — three rows that materialize FR6 in the architecture.
- `build_documents/planning-artifacts/epics.md:96,310,378,741` — FR6 in functional requirements, FR → epic mapping, Epic 1 scope, Story 1.7 acceptance criteria.

## Section 2: Impact Analysis

### Spec-Value Impact (the heart of this proposal)

| Brief artifact | Before | After |
|---|---|---|
| Problem #3 (line 27) | "Cloud-dependent transcription… not acceptable for a local personal agent." | Reframed: cloud STT is acceptable as a deliberate trade; the original rejection was a latency-on-Pi-class-hardware concern that Groq's LPU resolves. Privacy footprint is acknowledged explicitly. |
| Core Decision #3 (line 51) | "On-device STT (Whisper + Hailo-8L)… cloud STT is explicitly excluded for v1." | "Configurable STT backend: cloud (Groq Whisper-Large-V3-Turbo) is the v1 default; on-device Whisper remains available as an offline alternative behind the same Protocol seam." Privacy/latency rationale rewritten honestly. |

**No other brief decisions change.** Decisions #1 (Talker fast-path), #2 (single fan-out), #4 (mapping as data), #5 (continuous conversation + intent-sleep), #6 (multi-topic publish) are untouched.

### Wire-Schema Impact

**None.** STT is upstream of the wire — transcripts feed the Talker; `speech_emotion` / `mood` / `activity` / `vocalization` payloads are unchanged. `schema_version` stays at `3`.

### Code Impact

| File | Change |
|---|---|
| `src/voice_agent_pipeline/stt/groq.py` (new) | `GroqAsrBackend` implementing `STTBackend`. Uses the existing `openai` SDK (`base_url="https://api.groq.com/openai/v1"`) — same SDK Talker uses for its Groq path, so no new dependency. POSTs to `audio/transcriptions`. Derives confidence from response `logprobs` (Groq exposes `verbose_json` response_format with per-segment `avg_logprob`); falls back to fixed `1.0` + one-time WARN if absent. Wraps the synchronous SDK call in `asyncio.to_thread`. Raises `GroqAsrError` (new `ExternalServiceError` subclass) on API failures — never caught in v1 code paths per CLAUDE.md rule #4. |
| `src/voice_agent_pipeline/stt/__init__.py` | Factory adds `"groq"` branch. New module-level `async def validate_credentials(config: SetupConfig) -> None`: no-op for `whisper-cpu`; for `groq`, issues a minimal probe (silent 1-second 16 kHz S16LE buffer) against `audio/transcriptions` and raises `StartupValidationError` on failure (bad key / wrong base_url / model not on Groq's catalog / network unreachable). Mirrors the pattern from `turn/__init__.py:131`. |
| `src/voice_agent_pipeline/config/setup.py` | `SttConfig.backend` becomes `Literal["groq", "whisper-cpu"]` (default `"groq"`). New optional `groq_model: str = "whisper-large-v3-turbo"`. New optional `api_key_env: str = "GROQ_API_KEY"` (parallel to Talker's per-provider key lookup; same env var if Talker also uses Groq). The existing `model` / `compute_type` / `device` fields remain for the whisper-cpu path. The `low_confidence_threshold` semantics are reused — Groq's `avg_logprob → exp` is the same shape as faster-whisper's. |
| `src/voice_agent_pipeline/__main__.py` | Stage 3 adds a new `reporter.stage("stt", f"stt validated   ({config.stt.backend})")` block between `talker` and `cartesia`, calling `await stt.validate_credentials(config)`. Same failure-wrapping pattern as the existing wakeword / talker / cartesia probes. |
| `src/voice_agent_pipeline/errors.py` | Add `GroqAsrError(ExternalServiceError)` to the hierarchy, parallel to `CartesiaError` / `TalkerError`. |
| `setup.toml` | `[stt] backend = "groq"` becomes the default. Existing `model = "Systran/faster-distil-whisper-small.en"`, `compute_type`, `device` are kept (whisper-cpu path still wired). Add `groq_model = "whisper-large-v3-turbo"`. Comment block rewritten to describe the choice and the offline-fallback path. |
| `.env.example` | Uncomment `GROQ_API_KEY=<your-groq-api-key>` (was commented; now mandatory when `backend = "groq"` — which is the default). |

**Untouched:** `stt/whisper_cpu.py` (the offline backend stays exactly as-is — opt-in via `backend = "whisper-cpu"`). The `STTBackend` Protocol (`stt/backend.py`) is unchanged — the seam designed in Story 1.4 absorbs the new backend cleanly, as intended.

### Test Impact

| File | Change |
|---|---|
| `tests/unit/stt/test_groq.py` (new) | Unit tests mocking the openai SDK client only (Protocol-boundary mocking per CLAUDE.md rule #7). Coverage: transcribe success path; transcribe with `verbose_json` logprobs → confidence value; transcribe without logprobs → confidence=1.0 + one-time WARN log; `validate_credentials` success; `validate_credentials` bad-key → `StartupValidationError`; backend `load()` is a no-op. |
| `tests/unit/stt/test_init.py` (extended) | Factory dispatch test gains a `backend = "groq"` case; `_SUPPORTED_BACKENDS` constant assertion updated; `validate_credentials` dispatch test added. |
| `tests/contract/test_setup_config.py` (extended) | TOML round-trip: `backend = "groq"` + `groq_model = "whisper-large-v3-turbo"` parses. Default value: omitting `[stt]` block yields `backend = "groq"` (the new v1 default). |

**Untouched:** existing `test_whisper_backend.py` (the whisper-cpu path is not modified). Existing integration tests that build a real `WhisperBackend` in unit-runtime are unaffected (they pin `backend = "whisper-cpu"` in their fixtures).

### Planning-Doc Impact (NFR26 spec-as-contract)

| Artifact | Section / lines | Change |
|---|---|---|
| `prd.md` | line 589 (FR6) | Rewritten: "The pipeline can transcribe user speech to text via a configurable STT backend selected in `setup.toml`. The v1 default is **Groq Whisper-Large-V3-Turbo** (cloud, OpenAI-compatible API). An on-device Whisper backend (`faster-whisper`) is retained as an opt-in offline alternative behind the same Protocol seam (Story 1.4)." |
| `prd.md` | line 590 (FR7) | Already deferred to v2 (Hailo). No textual change, but cross-reference to this proposal added in a footnote-style trailing sentence: "*Note: FR7's premise — that Hailo acceleration is necessary for STT viability — was revised on 2026-05-12 (see sprint-change-proposal-2026-05-12.md). With cloud STT as v1 default, Hailo capacity is freed for other v2 workloads.*" |
| `architecture.md` | line 83 (Speech Recognition row in the FR-cluster table) | "Configurable STT backend (`STTBackend` Protocol, Story 1.4). v1 default: Groq Whisper-Large-V3-Turbo. Offline: on-device `faster-whisper`. Confidence-based clarification routing unchanged." |
| `architecture.md` | line 937 (FR → File Mapping) | "STT \| FR6, FR8 \| `stt/groq.py`, `stt/whisper_cpu.py`, `stt/backend.py`" — `groq.py` added as first entry (v1 default). |
| `architecture.md` | new sub-section under §"Decision Impact Analysis" | New entry "STT backend default reversal (2026-05-12)" — short pointer to this proposal explaining the why. |
| `voice-agent-pipeline.md` | line 42 (component bullet) | "**STT (configurable)** — Groq Whisper-Large-V3-Turbo (cloud) as v1 default; on-device `faster-whisper` retained as offline alternative behind the same Protocol seam. Cloud round-trip sits inside NFR3 (≤500 ms p95)." |
| `voice-agent-pipeline.md` | line 535 area (Phase 0 in deployment phases) | Update "On-device TTS — Cartesia (cloud) only for v1" framing to acknowledge cloud STT is also in scope; both Talker (cloud) and STT (cloud) are listed honestly. |
| `voice-agent-pipeline-brief.md` | line 27 (Problem #3) | Rewritten honestly: "**Cloud-STT latency on Pi-class hardware (resolved).** The original concern — that cloud STT added an unacceptable round-trip to every utterance on a local personal agent — assumed CPU/GPU STT was free of network cost. With purpose-built LPU providers (Groq) the round-trip is now ~150 ms p95, well inside NFR3. The privacy footprint of cloud STT remains real and is acknowledged: audio of every utterance leaves the device. v1 ships with Groq as the default for accuracy; on-device Whisper stays available for operators who weight privacy over accuracy." |
| `voice-agent-pipeline-brief.md` | line 51 (Core Decision #3) | Rewritten: "**Configurable STT backend with cloud as v1 default.** The pipeline ships with Groq Whisper-Large-V3-Turbo as the default STT (cloud, OpenAI-compatible API). On-device `faster-whisper` is the opt-in offline alternative behind the same Protocol seam (Story 1.4). The original 'cloud STT explicitly excluded' wording is reversed deliberately — see sprint-change-proposal-2026-05-12.md for the latency / accuracy / hardware-feasibility rationale." |
| `voice-agent-pipeline-brief.md` | line 79 (Success Criteria — NFR3) | Update "On-device STT latency" wording to "STT latency" (the budget applies regardless of backend; the brief's old "Whisper + Hailo-8L on Pi" specifics are decoupled from the latency target). |
| `epics.md` | line 96 (FR6) | Match prd.md rewording. |
| `epics.md` | line 310 (FR → Epic mapping) | "FR6 (STT backend) \| Epic 1 \| `GroqAsrBackend` (v1 default) + `WhisperBackend` (offline fallback), both behind `STTBackend` Protocol" |
| `epics.md` | line 376 (Epic 1 — STT bullet) | "STT: `GroqAsrBackend` using openai-compatible SDK against Groq's `audio/transcriptions` endpoint; `WhisperBackend` using faster-whisper retained as offline alternative. Both async-wrapped; both return transcript + confidence." |
| `epics.md` | line 741 (Story 1.7 AC — Whisper-specific Given/When/Then) | AC amended in-place: the Given clause shifts from "`stt/whisper_cpu.py` implements `STTBackend`" to "the configured STT backend implements `STTBackend`" — the AC text becomes backend-agnostic so it covers both `groq` (v1 default) and `whisper-cpu` (offline). Story 1.7 status remains `done`; the AC text is corrected to match what the seam was designed for from Story 1.4 onward. |
| `validation-report-2026-05-06.md` | line 98 (problem-coverage table) | "Cloud STT → FR6 *(coverage framing inverted on 2026-05-12: FR6 now *permits* cloud STT with on-device as opt-in, per sprint-change-proposal-2026-05-12.md. Coverage is preserved — the requirement still maps to a concrete backend choice, just with a different default.)*" |

### Out-of-Scope Artifacts (deliberately untouched)

- `build_documents/implementation-artifacts/` — frozen story specs from already-executed work. This proposal is the canonical record of the deviation; story specs reflect what was built at the time.
- `src/voice_agent_pipeline/stt/whisper_cpu.py` — the offline backend stays exactly as-is. Operators flip one TOML line to use it.
- `src/voice_agent_pipeline/stt/backend.py` — the Protocol seam was designed for this exact case; it is unchanged. Story 1.4's design pays off here.
- Wire schemas (`schemas/*.py`) — STT is upstream of the publisher; no payload changes.
- All other `src/` modules — the change is confined to `stt/` + config + startup + the errors hierarchy.

### Technical Risk Assessment

- **Brief-level reversal of a stated decision:** The reversal is documented, traceable, and reversible (operators can flip `backend = "whisper-cpu"` to revert per-machine). Future readers of the brief will see the rewritten text and an explicit pointer to this proposal. **Risk: low** to the project's coherence; **bounded** to the privacy framing, which is now explicit instead of implicit.
- **Privacy footprint (audio of every utterance leaves the device):** Acknowledged as a deliberate trade. Already the case for text via Talker + Cartesia. No regulated-data context (personal home use). **Risk: low** for the stated user; **callout in brief #3** makes this visible to future readers and consumers.
- **Cloud-availability dependency:** Per V1's fail-fast posture (no graceful degradation), Groq outage = STT down = pipeline crashes = systemd restarts. Same handling as a Cartesia outage today. Existing `ExternalServiceError` plumbing covers this. **Risk: low.**
- **Cost runaway:** $0.04/hr × hobby usage = sub-$1/month. A bug that loops audio submissions (e.g., a stuck VAD that keeps re-firing) would still cap at a few dollars/day. **Risk: very low.**
- **Test churn:** Bounded — one new test file + two extended files; no test in the existing suite asserts "audio never leaves the device", because that property was carried by the FR text, not by test assertions. **Risk: low.**
- **API-shape drift:** Groq's `audio/transcriptions` endpoint is OpenAI-compatible (response shape, parameters). If Groq ever diverges from OpenAI's shape, the `GroqAsrBackend` is the one place to fix it (boundary concentration). **Risk: low** — provider has held compatibility for the Talker SDK use for ~1 year.
- **Confidence semantics:** Groq returns `avg_logprob` per segment in `verbose_json`; the existing `exp(mean(avg_logprob))` confidence formula maps directly. `low_confidence_threshold = 0.5` may need recalibration in Story 5.5 soak — captured as a calibration note, not a blocker.

## Section 3: Recommended Approach

**Selected: Direct adjustment, single commit (the FR6 deviation + Groq backend land together).**

### Rationale

- **NFR26 (spec-as-contract) requires same-commit spec + code.** The brief reversal and the code that materializes it must land together so future readers cannot encounter a state where the brief still says "cloud STT excluded" while the code ships a Groq backend.
- **No story replan.** Story 1.7 (Epic 1) had the right *seam* — the Protocol design from Story 1.4 absorbs the new backend without restructuring. AC text is corrected to match the seam's actual coverage.
- **Single coherent commit.** Code + planning-doc edits + tests + setup.toml + .env.example land together. The proposal is part of that commit as the audit record.
- **Mic + speaker openability probe is a SEPARATE commit.** Bundling it would muddy the audit record of a brief-level reversal. The probe is additive, uncontroversial, and gets its own focused commit.

### Trade-offs Considered

- **Replace Whisper entirely vs keep as opt-in fallback.** Chose opt-in fallback. Cost: a few hundred bytes of dead config + the `whisper_cpu.py` module on disk. Benefit: operators retain a no-cloud path with one TOML edit; debug-mode and offline-dev keep working without changes. The Protocol seam makes this nearly free to retain. **Rejected** "delete whisper-cpu entirely" — would force a re-implementation if Kamal ever wants offline mode back, and the maintenance cost of keeping it is trivial.
- **Cloud provider: Groq vs Deepgram vs OpenAI vs Google.** Chose Groq. Reasons: cheapest by ~3-9× ($0.04/hr vs $0.26/hr Deepgram vs $0.36/hr OpenAI); the Talker side already speaks `openai` SDK to Groq, so no new dependency or new SDK to learn; whole-utterance call shape matches the existing VAD-segmented pipeline (no streaming complexity). **Rejected** Deepgram (streaming overkill for our VAD-segmented input); **rejected** OpenAI Whisper-1 (9× more expensive for similar quality); **rejected** Google STT (no openai-compatible surface, would add a new SDK and credential type).
- **Model: `whisper-large-v3-turbo` vs `whisper-large-v3`.** Chose turbo. Reasons: ~2.5× faster inference, accuracy within ~1% of full v3, smaller decoder so lower variance on short utterances. Cost the same at Groq's pricing tier. **Configurable** via `groq_model` so swapping is one TOML edit.
- **Bundle the mic/speaker probe with the Groq change.** Rejected. The probe is uncontroversial; bundling would mix audit records (one for a brief-level reversal, one for an additive startup invariant). Two commits, two clean records.
- **API key reuse: GROQ_API_KEY shared with Talker vs separate STT key.** Chose shared. The Groq account key is the same; forcing two env vars would be operator friction. The config field `api_key_env: str = "GROQ_API_KEY"` is overridable in `setup.toml` for operators who want separation.

### Effort and Risk

- **Effort:** Small-to-moderate. ~1 new code file (~120 LOC), ~3 modified code files (~30 LOC delta total), ~1 new test file (~150 LOC), ~2 modified test files (~50 LOC delta), ~6 planning docs (~30 line edits across them, plus this proposal).
- **Risk:** Low for delivery (mechanical change behind a well-designed Protocol seam). The risk being taken — a brief-level reversal of a stated value — is product-level, not engineering-level, and Kamal has made that call explicitly.

## Section 4: Detailed Change Proposals

The detailed code shapes, validation logic, and spec-doc diffs are in **Section 2: Impact Analysis** above. The full per-file diffs land in the same commit as this proposal — see commit message for the file list.

`epics.md` Story 1.7 AC text correction is unusual (story already `done`) but the alternative is for AC text to lie about what the seam was designed for. The Story 1.4 Protocol design *already* anticipated backend swap; making the AC text reflect that is honest, not retroactive.

`voice-agent-pipeline-brief.md` Problem #3 and Core Decision #3 rewrites are the *substantive* change in this proposal. Both are rewritten to be honest about what shifted (latency assumption + hardware feasibility) and what didn't (privacy footprint is now explicit, not eliminated). Future readers of the brief see the trade clearly.

## Section 5: Implementation Handoff

### Scope Classification: **Moderate**

One brief-level architectural decision reversed (Core Decision #3), one brief-level problem reframed (Problem #3), one FR rewritten (FR6), one new backend added, default changed. No story renumber, no epic restructure, no wire-schema bump, no external-consumer impact (no consumers yet for the wire; FR6 is upstream of the wire).

### Sequencing

1. Sprint change proposal (this file) — commit-ready first artifact.
2. Tests written to target shape (TDD — fixtures first).
3. Code: `errors.py` (`GroqAsrError`) → `stt/groq.py` → `config/setup.py` (`SttConfig` updates) → `stt/__init__.py` (factory + `validate_credentials`) → `__main__.py` (Stage 3 probe).
4. `setup.toml` + `.env.example` flipped to Groq default.
5. Planning docs updated (`architecture.md` first, then prd, then brief, then voice-agent-pipeline.md, then epics.md, then validation-report).
6. `just check` — must be green.
7. Single commit + push (project rule: push immediately after commit).
8. **Separate commit:** the mic + speaker openability probe (additive startup invariant, not part of this proposal's audit).

### Approval

Approved by Kamal in-conversation on 2026-05-12:
- STT direction: cloud STT replaces on-device as v1 default; on-device retained as opt-in offline.
- Provider: Groq.
- Model: `whisper-large-v3-turbo` (selected by recommendation; configurable).
- Sequence: brief reversal + Groq backend land in a single coherent commit; mic/speaker probe lands in a separate commit afterward.
- Privacy framing: audio leaving the device for STT is acknowledged in the brief as a deliberate trade, not glossed over.

This proposal is approved for implementation as a single commit.
