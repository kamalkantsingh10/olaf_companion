---
proposalDate: '2026-05-10'
trigger: speech-emotion-boundary-violation
proposalStatus: APPROVED
mode: direct-adjustment
scopeClassification: minor  # one wire payload field removed + small vocab addition
artifactsTouched:
  - expression_map.yaml
  - prompts/talker_system.md
  - src/voice_agent_pipeline/config/expression_map.py
  - src/voice_agent_pipeline/schemas/speech_emotion_event.py
  - src/voice_agent_pipeline/schemas/envelope.py
  - src/voice_agent_pipeline/splitter/mapping.py
  - tests/unit/config/test_expression_map.py
  - tests/unit/splitter/test_segmenter.py
  - tests/unit/publisher/test_ros2.py
  - tests/contract/test_speech_emotion_event_schema.py
  - build_documents/planning-artifacts/architecture.md
  - build_documents/planning-artifacts/prd.md
  - build_documents/planning-artifacts/voice-agent-pipeline.md
  - build_documents/planning-artifacts/voice-agent-pipeline-brief.md
  - build_documents/planning-artifacts/epics.md
  - build_documents/planning-artifacts/sprint-change-proposal-2026-05-10.md (this file)
schemaVersionBump: '2 → 3 (breaking — speech_emotion payload field removed)'
storyCountChange: 'no story renumber; ACs in 3.1/3.2/3.4 amended in-place'
---

# Sprint Change Proposal — 2026-05-10 SpeechEmotion Boundary Repair + Gesture Cues

**Workflow:** ad-hoc deviation (no `bmad-correct-course` ceremony — single-fix scope)
**Driven by:** Amelia (`bmad-agent-dev`) on Kamal's direction
**Date:** 2026-05-10
**Mode:** Direct adjustment (single coherent change)

## Section 1: Issue Summary

**Triggering issue:** Discovered while drafting the OLAF embodiment consumer brief (a sibling-project specification document Kamal asked for on 2026-05-10). The `SpeechEmotionPayload.expression_data: dict[str, Any]` field — populated verbatim from `expression_map.yaml`'s per-emotion `expression_data:` blocks — ships OLAF-specific renderer vocabulary on the wire: `base_pose.yaw`, `base_pose.pitch`, `eye_state`, `led_color`, `led_intensity`. This directly contradicts the project's stated scope boundary:

> *Pipeline ends at typed event publish on configurable channels; OLAF rendering and host hardware are out of scope.*
> — `project_pipeline_scope_boundary` memory; `architecture.md` §"Decision Impact Analysis"

The `MoodEvent` schema (the parallel slow-cadence channel) honors this boundary correctly — it ships `mood: Literal[...]` + `reason: str | None`, with the embodiment owning the pose/LED mapping. `SpeechEmotion` should match.

**Categorization:** *Boundary violation in shipped code, surfaced by writing the consumer-side spec.* Not a direction shift — the architecture's stated intent has always been agnostic publish. Story 3.1's implementation drifted in the opposite direction (the YAML carries placeholder values "negotiated with the embodiment project") and Stories 3.2 + 3.4 propagated the dict[str, Any] field downstream onto the wire.

**Concurrent change (bundled):** Add two new vocalization tags `nod` and `shake` (with `tts_supported: false`) — gesture cues for affirmation/negation. Surfaced in the same conversation; same artifacts touched (yaml + Talker prompt + planning docs). Bundling avoids a second commit churning the same files.

**Discovery context:** the issue was not visible until the consumer-side brief forced an honest description of what is on the wire. This is exactly the discovery posture NFR26 (spec-as-contract) is meant to surface — writing down what the wire promises makes embodiment-specific leakage impossible to miss.

**Evidence:**
- `expression_map.yaml` lines 41–129: 12 emotion entries each carry `base_pose`, `eye_state`, `led_color`, `led_intensity` — pure OLAF hardware vocabulary.
- `src/voice_agent_pipeline/schemas/speech_emotion_event.py:59`: `expression_data: dict[str, Any]` ships verbatim onto the wire.
- `src/voice_agent_pipeline/splitter/mapping.py:112,139,159`: three resolver branches assemble the dict.
- `prompts/talker_system.md:73-76`: vocalization list enumerates four entries; line 80–81 forbids inventing others — so adding `nod`/`shake` requires a coordinated prompt edit, not just a YAML change.

## Section 2: Impact Analysis

### Wire-Schema Impact

| Field | Before (schema_version 2) | After (schema_version 3) |
|---|---|---|
| `SpeechEmotionPayload.emotion` | `str` (canonical name from resolver) | `str` — unchanged |
| `SpeechEmotionPayload.source_tag` | `str` | unchanged |
| `SpeechEmotionPayload.audio_frame_id` | `str \| None` | unchanged |
| `SpeechEmotionPayload.raw_tag` | `str` | unchanged |
| `SpeechEmotionPayload.resolved_fallback` | `str \| None` | unchanged |
| `SpeechEmotionPayload.expression_data` | `dict[str, Any]` (renderer hints) | **REMOVED** |
| `EventEnvelope.schema_version` | `int = 2` | `int = 3` |
| `VocalizationPayload.tag` values | `laughter, sigh, gasp, clears_throat` | adds `nod, shake` (`tts_supported: false`) |

**Other three topics (`mood`, `activity`, `vocalization`) are otherwise unchanged.** Schema version is a pipeline-wide envelope field; the bump applies to all four topics for consistency, even though only `speech_emotion` removed a field.

### YAML-Schema Impact (`expression_map.yaml`)

| Block | Before | After |
|---|---|---|
| `schema_version` | `2` | `3` |
| `emotions:` | mapping; each entry has `expression_data:` with `base_pose / eye_state / led_color / led_intensity` | **list of 12 canonical names** (`[neutral, content, excited, sad, angry, scared, happy, curious, sympathetic, surprised, frustrated, melancholic]`) |
| `vocalizations:` | 4 entries | 6 entries (`nod`, `shake` added with `tts_supported: false`) |
| `fallback_families:` | 7 families × ~57 Cartesia tags | unchanged |
| `unknown:` | `maps_to: neutral` | unchanged |

**Loader API impact:** `EmotionEntry` model is **deleted** (it had a single `expression_data` field with no other purpose); `ExpressionMapConfig.emotions: dict[str, EmotionEntry]` becomes `emotions: list[str]`. The `_assert_completeness` check loses the per-entry `expression_data` non-empty branch; missing-emotion detection still happens (set-difference against the canonical 12).

### Code Impact

| File | Change |
|---|---|
| `src/voice_agent_pipeline/config/expression_map.py` | Delete `EmotionEntry`; `emotions: list[str]`; drop `expression_data` non-empty validation; refresh module docstring (the "open-ended on inner expression_data dict" rationale no longer applies — the data is taxonomy, not renderer hints). |
| `src/voice_agent_pipeline/schemas/speech_emotion_event.py` | Drop `expression_data: dict[str, Any]` field; remove the `Any` import; drop the docstring paragraph claiming `expression_data` as the extensibility seam. |
| `src/voice_agent_pipeline/schemas/envelope.py` | Bump `schema_version: int = 2` to `3`. |
| `src/voice_agent_pipeline/splitter/mapping.py` | Drop `expression_data=` kwarg from all three `SpeechEmotionPayload(...)` constructions (first-class hit, fallback-family hit, unknown). The reference-not-copy comment at line 100 is removed. |
| `expression_map.yaml` | Convert `emotions:` to list form; drop all `expression_data:` blocks; add `nod` and `shake` under `vocalizations:`; refresh file-level comment block. |
| `prompts/talker_system.md` | Extend the vocalization bullet list (lines 73–76) to include `[nod]` and `[shake]` with emission policy; preserve the "do not invent other tag values" rule (line 80–81), now covering 6 tags. |

### Test Impact

| File | Change |
|---|---|
| `tests/unit/config/test_expression_map.py` | YAML fixtures lose `expression_data` blocks and switch `emotions:` to list form. The `test_empty_expression_data_raises_config_error` test is replaced with `test_empty_emotions_list_raises_config_error` (the equivalent invariant in the new shape). New cases cover `nod` and `shake` parsing. |
| `tests/unit/splitter/test_segmenter.py` | Drop `EmotionEntry(expression_data=...)` constructions (the import goes too); fixtures use the simpler `ExpressionMapConfig.emotions: list[str]` shape. |
| `tests/unit/splitter/test_mapping.py` (if present) | Same pattern — drop `expression_data` from all asserted payloads. |
| `tests/unit/publisher/test_ros2.py` | Line 224: drop `expression_data={"k": "v"}` from the `SpeechEmotionPayload(...)` fixture. |
| `tests/contract/test_speech_emotion_event_schema.py` | Drop `expression_data` from fixture and from the round-trip equality assertion; assert `EventEnvelope.schema_version == 3`. |

### Planning-Doc Impact (NFR26 spec-as-contract)

| Artifact | Lines / Sections | Change |
|---|---|---|
| `architecture.md` | line 383 (`SpeechEmotionEvent` schema row) | Drop `expression_data: dict[str, Any]` from the field list; rephrase the FR20 rationale (it was about the audit trail — that part stays — but the sentence framing `expression_data` as the open extensibility seam is removed). |
| `architecture.md` | line 524 (Type System Conventions row "Events / config / data models") | Remove the "one allowed open `dict[str, Any]`" exception sentence. The rule becomes: **no plain dicts at boundaries, period.** |
| `architecture.md` | line 527 (Type hints row) | Remove the `Any` exception clause for `expression_data`. The rule becomes: **no `Any` in `src/`, period.** |
| `architecture.md` | line 648 (Anti-patterns) | Remove the `expression_data` exception line; the anti-pattern becomes flat "adding `Any` outside `src/` test fixtures". |
| `architecture.md` | new sub-section under §"Decision Impact Analysis" | Add a short *"What left the wire on schema_version=3 and why"* block — points to this proposal. |
| `prd.md` | FR20 wording | Reframe: pipeline publishes the canonical resolved emotion name + audit metadata; consumer owns rendering. Drop any reference to `expression_data` as a payload field. |
| `voice-agent-pipeline.md` | event-schema section | Match `architecture.md` — remove `expression_data` from the schema description. |
| `voice-agent-pipeline-brief.md` | §"What Makes This Different" item 4 ("Mapping is data, not code") | Tighten: the data is a **taxonomy** (canonical names + Cartesia-tag fallback families), not renderer hints. The renderer side is the consumer's `embodiment_map.yaml`. |
| `epics.md` | Stories 3.1, 3.2, 3.4 ACs | Strike `expression_data` references throughout (~12 mentions across these three stories). The "extensibility — adding a new emotion is one YAML edit" promise stays but the YAML edit is now a list-append + a Cartesia-tag family update, not a new `expression_data` block. |

### Out-of-Scope Artifacts (deliberately untouched)

- `build_documents/implementation-artifacts/3-1-expression-map-loader.md`, `3-2-mapping-resolver-and-cache.md`, `3-4-event-schema-rebuild.md` — frozen story specs from already-executed work. This proposal is the canonical record of the deviation; story specs reflect what was built at the time.
- `sprint-status.yaml` — no story status changes; no renumbering; no new stories.
- All other `src/` modules — the change is confined to the splitter resolver + the schema/loader + the envelope.

### Technical Risk Assessment

- **Breaking schema bump (2 → 3):** No external consumers exist yet. The OLAF embodiment project is at the spec stage (this proposal is what unblocks its brief). `LogEventPublisher` adapter consumes the same in-process payload — unaffected by wire-version semantics. **Risk: low.**
- **Talker prompt change:** Adding `[nod]` and `[shake]` to the enumerated set is additive; the LLM keeps emitting the same four it knows about until the prompt change exposes the new two. The "do not invent" discipline is preserved (the list grows from 4 to 6, the rule stays exactly as worded). **Risk: low.**
- **Test churn:** Four test files lose a field from fixtures. The change is mechanical. `just check` (ruff + pyright + fast pytest) is the gate. **Risk: low.**
- **Hidden coupling:** Other modules grepping `expression_data`? Verified clean — the field only appears in the four planning docs (under `expression_data` references), the impl-artifacts story specs (frozen), the four code files, and the four test files listed above. No use sites outside the splitter resolver.

## Section 3: Recommended Approach

**Selected: Direct adjustment, single commit.**

### Rationale

- **No story replan.** The existing Stories 3.1 / 3.2 / 3.4 had the right *intent* — the implementation drifted. Re-shipping under the original story numbers with amended ACs preserves traceability without renumbering.
- **Single coherent commit.** Code + yaml + prompt + planning docs land together per CLAUDE.md rule 9 (NFR26 spec-as-contract: spec deviations update the canonical four documents in the same commit). The sprint change proposal is part of that commit as the audit record.
- **Bundled vocab addition.** `nod` / `shake` touch the same files (yaml + Talker prompt + planning docs) and are gated by the same prompt's "do not invent" rule. Splitting into two commits would churn identical artifacts twice.

### Trade-offs Considered

- **List vs mapping for `emotions:` in the YAML.** Chose list form (`emotions: [neutral, content, ...]`) over mapping-with-empty-values (`emotions: { neutral: {}, ... }`). The data is now a vocabulary; the list is the most honest shape. Cost: loader API change (`emotions: list[str]` instead of `dict[str, EmotionEntry]`) — internal-only, no callers outside `splitter/mapping.py`.
- **Naming `nod` / `shake` vs `yes` / `no`.** Chose gesture-named tags. Rationale: `yes`/`no` are *words*; `nod`/`shake` describe the *gesture*. With `tts_supported: false` the tag never reaches Cartesia, so naming it after the audio it doesn't make would mislead a future reader. Also: a nod doesn't carry locale assumptions ("yes" varies by language; nodding is universal-ish).
- **Tightening `emotion: str` to `Literal[...]` of the 12 names.** Rejected. The architecture's "adding a new emotion is one YAML edit" promise (`architecture.md` §"Extensibility") would force a code change for every new entry. Runtime validity is enforced by the loader's set-difference completeness check.

### Effort and Risk

- **Effort:** Small. ~5 code files, ~4 test files, ~5 planning docs, 1 yaml, 1 prompt. Bounded change with mechanical rewrites (drop a field, switch a shape).
- **Risk:** Low. No external consumers, no in-flight stories disturbed, `just check` is the gate.

## Section 4: Detailed Change Proposals

The detailed wire/yaml/code shapes are in **Section 2: Impact Analysis** above. The full per-file diffs land in the same commit as this proposal — see commit message for the file list.

`epics.md` updates are surgical strikes against AC text in Stories 3.1, 3.2, 3.4 (all already at `done` status). Editing the AC of a `done` story is unusual, but here the AC text would otherwise lie about what the wire promises — choose truth over chronology, document the choice (this proposal).

## Section 5: Implementation Handoff

### Scope Classification: **Minor**

One field removed from a wire payload, one config schema reshape, two new gesture tags. No story renumber, no epic restructure, no external-consumer impact (no consumers yet).

### Sequencing

1. Sprint change proposal (this file) — commit-ready first artifact.
2. Tests updated to target shape (red phase per Amelia's TDD principle).
3. Code updated to make tests green.
4. `expression_map.yaml` + Talker prompt updated.
5. Planning docs updated (`architecture.md` first, then dependents).
6. `just check` — must be green.
7. Single commit + push (project rule: push immediately after commit).
8. **Then** the OLAF embodiment consumer brief is written to schema_version=3 reality.

### Approval

Approved by Kamal in-conversation on 2026-05-10:
- Yaml shape: list form ("Recommended").
- Cleanup scope: hard removal ("Recommended").
- Sequence: fix pipeline first, then write brief ("Recommended").
- Naming: nod/shake ("Recommended").
- Bundling: same commit as cleanup, "do not invent" discipline preserved.
- Emission policy: sparingly but not stingy.

This proposal is approved for implementation as a single commit.
