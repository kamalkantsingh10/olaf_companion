---
proposalDate: '2026-05-06'
trigger: spec-triple-direction-shift
triggerCommits:
  - 6f3bfe3  # PRD/brief/distillate update
  - ed8b276  # architecture.md surgical refresh
proposalStatus: APPROVED
mode: incremental
scopeClassification: moderate  # backlog reorganization; fundamental specs already realigned
artifactsTouched:
  - build_documents/planning-artifacts/epics.md
  - build_documents/implementation-artifacts/sprint-status.yaml
  - build_documents/planning-artifacts/sprint-change-proposal-2026-05-06.md (this file)
artifactsAlreadyAligned:
  - build_documents/planning-artifacts/prd.md (commit 6f3bfe3)
  - build_documents/planning-artifacts/voice-agent-pipeline-brief.md (commit 6f3bfe3)
  - build_documents/planning-artifacts/voice-agent-pipeline.md (commit 6f3bfe3)
  - build_documents/planning-artifacts/architecture.md (commit ed8b276)
v1StoryCountChange: '22 → 30 (+8 net; 7 net-new + 1 deferred to v1.5)'
v1_5BacklogItemsAdded: 4
---

# Sprint Change Proposal — 2026-05-06 Direction-Shift Course Correction

**Workflow:** `bmad-correct-course`
**Driven by:** John (PM persona)
**Date:** 2026-05-06
**Mode:** Incremental (per-epic edit proposals approved sequentially)

## Section 1: Issue Summary

**Triggering issue:** A deliberate product direction shift, recognized before Epic 3 (the next backlog epic) had any stories started. The original architecture would have shipped a turn-shaped, single-channel-publish, idle-auto-sleep voice agent. User testing of intermediate Talker/Cartesia behavior in Epics 1+2 (2026-05-03 through 2026-05-05) made clear the experience would feel mechanical: re-saying the wake word every turn, scripted greetings ("Hello, I am OLAF"), and mood snapping turn-to-turn.

**Categorization:** *New requirement emerged from stakeholder.* Kamal-as-user and Kamal-as-architect agreed to reframe the v1 surface around four new ideas, then carry them through the canonical specs:

1. **Continuous conversation while AWAKE** — wake-word fires only on `sleeping → waking`; mic stays open for follow-up turns.
2. **Intent-based sleep** — Talker LLM detects "we're done" semantically and fires a `go_to_sleep()` tool call; no idle auto-sleep timer.
3. **Mood-tinted wake greeting** — Talker generates a 2–8 word "cool friend" greeting on every wake, tinted by current mood; static fallback if Talker is unreachable / overlong / >800ms.
4. **Four-topic event publish with common envelope** — single `/olaf/expression` channel split into `mood`, `activity`, `speech_emotion`, `vocalization` with shared `EventEnvelope` and `schema_version=2`.

**Discovery context:** the issue surfaced as Epic 2 was capstoning (Story 2.5, commit `4df609c` on 2026-05-05). The simple-turn loop was alive end-to-end, which made the missing user-experience qualities visible. Rather than push them to v1.5 and ship a coherent-but-mechanical v1, the decision was to reshape v1 itself — keeping Epic 1 + Epic 2 (12 stories at `review`) untouched as solid foundation, but reworking Epic 3 + 4 + 5 (15 backlog stories) before they start.

**Evidence:**
- PRD validation report (`validation-report-2026-05-06.md`): 5/5 standalone quality on the updated PRD; 0 violations across 85 requirements.
- Architecture validation: 7/7 BMAD principles met after the surgical refresh (commit `ed8b276`).
- Spec-triple commit (`6f3bfe3`) documents the direction shift in detail in its commit message.

## Section 2: Impact Analysis

### Epic Impact

| Epic | Status | Change |
|---|---|---|
| **Epic 1: Listen** (7 stories at `review`) | Done | **No change.** Foundation work (bootstrap, config, logging, audio capture, wake-word, VAD, STT) is forward-compatible with new direction. |
| **Epic 2: Speak** (5 stories at `review`) | Done | **No change.** Audio playback, Talker (provider-agnostic factory), Cartesia TTS, TurnRouter (sync logic), pipeline assembly. Talker tool-using upgrade comes in Epic 4.4 — extends rather than rewrites. |
| **Epic 3: Embodiment Channel** (5 → 7 stories) | Backlog | **Substantively expanded** — now four-topic publish + mood module + event-schema rebuild. Goal preserved ("emotion in lockstep with voice") but scope widens. Stories 3.1–3.3 carry over with minor revisions; 3.4 NEW (event schemas), 3.5 REBUILT (was old 3.4 — `Ros2EventPublisher` with four publish methods), 3.6 NEW (mood module), 3.7 EVOLVED (was old 3.5 — audio-frame metadata + Talker SSML). |
| **Epic 4: Activity FSM + Tool-Use + Slow Path** (5 → 7 stories) | Backlog | **Renamed** (was "Complex Questions & Lifecycle"); **theme widens** to encompass the new conversation-shape work. 4.1 + 4.2 carry over. 4.3 NEW (activity FSM, replaces old 4.4 lifecycle), 4.4 NEW (Talker tool-using), 4.5 NEW (wake greeting), 4.6 NEW (mic-mode flip), 4.7 EVOLVED (merges old 4.3 + 4.5 — slow-path wiring). |
| **Epic 5: Production Hardening** (5 → 4 stories) | Backlog | **Internal renumber** with old 5.1 (barge-in) moved to v1.5 backlog. Old 5.2/5.3/5.4/5.5 → new 5.1/5.2/5.3/5.4. Story 5.4 sign-off list expanded for NFR30/31/32 + intent-sleep FP/FN + mood cadence. |
| **v1.5 Backlog** (new section) | n/a | 4 new items: barge-in (was Epic 5.1), cross-restart mood persistence, expanded `working` sub-modes, configurable idle auto-sleep fallback. |

**Net story count change:** 22 → 30 (+8). Of those 8: 7 net-new direction-shift stories, 1 net-removed (idle auto-sleep — old FR28). Plus 4 v1.5 items captured.

### Story-Level Impact (Epic 3 + 4 + 5)

| Old story | New story | Delta |
|---|---|---|
| 3.1 expression-map-loader | 3.1 expression-map-loader | Minor: `schema_version=2`, `expression_data` payload key, doc updates |
| 3.2 mapping-resolver-and-cache | 3.2 mapping-resolver-and-cache | Minor: returns `SpeechEmotionPayload` with `raw_tag` + `resolved_fallback`; cache scoped per-turn |
| 3.3 streaming-ssml-state-machine | 3.3 streaming-ssml-state-machine | Revise: emits two distinct event paths (`speech_emotion` + `vocalization`); strip-vs-keep-in-text driven by `tts_supported` |
| — | 3.4 event-schema-rebuild *(NEW)* | `EventEnvelope` mixin + four typed events; replaces placeholder `expression_event.py` + `lifecycle_event.py` |
| 3.4 ros2-expression-publisher | 3.5 event-publisher-ros2-and-log-adapter *(REBUILT)* | `EventPublisher` Protocol with 4 publish methods; `Ros2EventPublisher` (four publishers, per-topic QoS); `LogEventPublisher` adapter |
| — | 3.6 mood-module-state-and-controller *(NEW)* | `Mood` Literal + `MoodState` + `MoodController` with publisher-boundary cooldown (NFR31) |
| 3.5 audio-frame-metadata-and-ssml-prompt | 3.7 audio-frame-metadata-and-ssml-prompt *(EVOLVED)* | Two distinct metadata slots on `AudioRawFrame`; integration test verifies NFR5 for both topics |
| 4.1 belief-state-client | 4.1 belief-state-client | Minor: integrates with Talker.complete_with_tools (Story 4.4) instead of old all-in-one Talker |
| 4.2 orchestrator-client-sse | 4.2 orchestrator-client-sse | Minor: `cancel()` stub stays; wired in **v1.5-1 (barge-in)** |
| 4.3 turn-router-fast-slow | (merged into 4.7) | Old 4.3 routing logic largely landed in Story 2.4; remaining work merges into 4.7 |
| 4.4 lifecycle-state-machine | 4.3 activity-fsm-core *(NEW; replaces old 4.4)* | 7-state FSM, deferred-sleep, mic-mode signaling; `lifecycle/` → `activity/` rename |
| — | 4.4 talker-tool-using-upgrade *(NEW)* | `complete_with_tools`, `greet`, tool registry, `GoToSleepTool`, `SetMoodTool` |
| — | 4.5 wake-greeting *(NEW)* | `talker.greet()` greeting mode + FSM trigger + 800ms timeout + J1 integration test |
| — | 4.6 mic-mode-flip *(NEW)* | `audio/transport.py` consumes FSM mic-mode signal (FR47) |
| 4.5 pipeline-slow-path-and-complex-turn | 4.7 turn-router-slow-path-and-complex-turn *(EVOLVED)* | Merges old 4.3 + 4.5; updates FSM `working[delegating]` sub-mode; J3 + NFR2 baseline |
| 5.1 barge-in | **v1.5-1 barge-in** | **Moved to v1.5 backlog.** |
| 5.2 sighup-atomic-swap | 5.1 sighup-atomic-swap | Renumber + minor revise (mood enum / activity states / tool registry are NOT SIGHUP-reloadable) |
| 5.3 security-and-config-hardening | 5.2 security-and-config-hardening | Renumber + revise (contract test exercises 4 event types) |
| 5.4 systemd-service-deployment | 5.3 systemd-service-deployment | Renumber + minor revise (graceful shutdown drains `MoodController`) |
| 5.5 soak-tuning-and-v1-signoff | 5.4 soak-tuning-and-v1-signoff | Renumber + expand sign-off list (NFR30/31/32, intent-sleep FP/FN, mood cadence, J1/J4/J5 acceptance) |

### Artifact Conflicts and Updates

| Artifact | Status | Action |
|---|---|---|
| **PRD** (`prd.md`) | ✅ Aligned | Updated in commit `6f3bfe3` (5/5 validation). No further action. |
| **Brief** (`voice-agent-pipeline-brief.md`) | ✅ Aligned | Updated in commit `6f3bfe3`. |
| **Distillate** (`voice-agent-pipeline.md`) | ✅ Aligned | Updated in commit `6f3bfe3`. |
| **Architecture** (`architecture.md`) | ✅ Aligned | Refreshed in commit `ed8b276` (surgical pass with edit-history block; Mermaid data-flow diagram replacing ASCII). |
| **Epics** (`epics.md`) | ❌ → ✅ Updated this proposal | Substantive rewrite of Epics 3–5 + new v1.5 Backlog section. Frontmatter `editsCompleted` + `editHistory` block + `deferredToV1_5` field. FR Coverage Map fully refreshed. |
| **`sprint-status.yaml`** | ❌ → ✅ Updated this proposal | New story IDs reflected; Epic 3/4 expanded; Epic 5 renumbered; v1.5 backlog items added; comment header updated with 2026-05-06 entry. |
| **Story specs in `implementation-artifacts/`** | Untouched | 1.1–2.5 stay at `review`. New stories will be created by `bmad-create-story` workflow story-by-story when each is picked up for implementation. |
| **UI/UX specifications** | N/A | No UI surface for this component. |
| **Code (`src/voice_agent_pipeline/`)** | Untouched | Implementation changes (`lifecycle/` → `activity/` rename, event schema rebuild, etc.) are story-scoped — they happen inside Stories 3.4 / 4.3 etc. when those stories run. |

### Technical Impact

- **No code changes in this commit.** This proposal is documentation-only. Code changes happen story-by-story.
- **Implementation seams stable:** the `EventPublisher` Protocol is new (replacing `ExpressionPublisher`), but no callers exist yet — Story 1.4 only declared the placeholder. Same for `schemas/expression_event.py` + `lifecycle_event.py` — placeholders only, replaced wholesale in Story 3.4.
- **Test infrastructure forward-compat:** existing 167 unit tests in `tests/unit/{audio,config,logging,publisher,schemas,splitter,stt,tts,turn}` remain green through the rewrite. Stories 3.4, 4.3, 4.4 will add new tests under `tests/unit/{schemas,activity,mood,turn}/` and remove the placeholder schema tests.
- **Per-story commit policy** (CLAUDE.md project rule + project memory) is preserved: each story's Task 7 instructs a single commit + push.

## Section 3: Recommended Approach

**Selected: Hybrid — primarily Option 1 (Direct Adjustment) with embedded Option 3 (MVP Review for barge-in).**

### Rationale

- **Option 1 — Direct Adjustment** is overwhelmingly the right path. The canonical four-document spec set is already coherent (commits `6f3bfe3` + `ed8b276`). Restructuring 17 stories across Epic 3 + 4 + 5 to match is mechanical work with clear acceptance boundaries. Epic 1 + 2 are forward-compatible — no rollback needed.
- **Option 2 — Rollback** is not viable. There is no in-flight Epic 3 work to roll back; the direction shift is forward-looking. Epic 1 + 2 stories use provider-agnostic Talker, generic audio path, and decoupled splitter — all forward-compat with the new direction.
- **Option 3 — MVP Review** applies narrowly: barge-in (FR5/FR29/FR30) is genuinely scoped out of v1. The PRD already reflects this (commit `6f3bfe3`). The barge-in deferral preserves quality budget on the new conversation-shape behaviours that are the actual value-add of v1.

### Trade-offs Considered

- **Renumbering Epic 5 stories vs leaving holes.** Chose internal renumber (5.2 → 5.1, etc.) for cleanliness. The cost is renaming story files in `implementation-artifacts/` if they're created later — acceptable since they're at `backlog`.
- **5-epic structure vs splitting into 6.** Considered creating "Epic 4.5 — Wake/Sleep + Mood + Tools" between Epic 4 and Epic 5. Rejected: the new content fits coherently inside the renamed Epic 4 ("Activity FSM + Tool-Use + Slow Path"), and a 6-epic structure would force renumbering Epic 5 → Epic 6 with downstream churn. Epic 4 with 7 stories is on the larger side but holds together thematically.
- **v1.5 backlog placement.** Chose to append `## v1.5 Backlog (Post-v1)` to `epics.md` (rather than a separate `v1.5-backlog.md` file). Keeps the deferred items close to v1 stories so a sprint-status check sees both at once; enables future sprint planning to graduate items from this section into a v1.5-numbered epic.

### Effort and Risk

- **Effort:** Medium. The 7 net-new stories (3.4, 3.5 rebuild, 3.6, 4.3, 4.4, 4.5, 4.6) are bounded and their dependencies are clear. The 4 evolved stories (3.3, 3.7, 4.7, 5.4) have specific scope deltas. Story 4.3 is the largest single new story (Activity FSM is the central spine); it's the natural "first hard story" of Epic 4.
- **Risk:** Low overall. Highest specific risks: (a) Story 4.4's text-before-tools concurrency ordering (FR45 parallel + FR46 deferred-sleep depend on it) — needs careful integration testing; (b) Story 4.3's mic-mode signaling crosses module boundaries (`activity/` → `audio/`) — could surface coordination bugs in soak; (c) Story 4.5's 800 ms greeting timeout pressures Talker provider TTFB (Groq Llama 8B Instant on this hardware is ~150–270 ms per turn — comfortable headroom but worth monitoring during Story 5.4 soak).

## Section 4: Detailed Change Proposals

The detailed story-by-story change proposals are landed directly in `epics.md` with the new content. See:

- **Epic 3** detailed section (`epics.md` lines beginning at `## Epic 3: Embodiment Channel — four typed event topics + mood`)
- **Epic 4** detailed section (`## Epic 4: Activity FSM + Tool-Use + Slow Path`)
- **Epic 5** detailed section (`## Epic 5: Production Hardening`)
- **v1.5 Backlog** detailed section (`## v1.5 Backlog (Post-v1)`)
- **FR Coverage Map** (refreshed in `## Requirements Inventory`)

The Epic List section (`## Epic List`) carries the brief 2–4 paragraph summary per epic; the per-epic detailed sections carry full Acceptance Criteria for every story.

`sprint-status.yaml` reflects the new story IDs and explicitly tags v1.5 items with status `v1.5-backlog` (not part of the v1 sprint set).

## Section 5: Implementation Handoff

### Scope Classification: **Moderate**

This is **backlog reorganization** — story IDs change, story content rewritten, scope of Epic 4 widens. It is **not** a fundamental replan: the canonical specs (PRD/brief/distillate/architecture) are already aligned with the new direction.

### Handoff Recipients

| Role | Agent / Persona | Responsibility |
|---|---|---|
| **PM (this proposal)** | John (`bmad-agent-pm`) | Drove the course correction. Output: this proposal + `epics.md` rewrite + `sprint-status.yaml` update. **Done.** |
| **Story creator** | Bob (`bmad-create-story`) — *or any agent running this workflow* | Creates per-story spec files in `implementation-artifacts/` from the new `epics.md` Acceptance Criteria, one at a time, when each story is picked up. Each story file follows the existing format used in 1.1–2.5. |
| **Developer** | Amelia (`bmad-agent-dev` / `bmad-dev-story`) | Implements each story. Per-story commit policy preserved (CLAUDE.md rule: commit per story as the story spec dictates). |
| **Code reviewer** | Fresh context (`bmad-code-review` after Dev marks `review`) | Reviews each completed story before marking `done`. |

### Sequencing for Epic 3+

Epic 3 stories run roughly in numeric order:
- 3.1 → 3.2 → 3.3 (mapping infrastructure; Stories 3.1 + 3.2 can run in parallel after 3.3's state machine work begins)
- 3.4 (event schemas) — must land before 3.5 + 3.6 + 3.7
- 3.5 (publisher) — depends on 3.4
- 3.6 (mood module) — depends on 3.5
- 3.7 (audio-frame metadata + Talker SSML prompt + integration test) — depends on 3.5 + 3.6 (mood read for Talker prompt context)

Epic 4 stories:
- 4.1 → 4.2 (client-side stories first; independent of FSM cluster)
- 4.3 (FSM core) — gateway story; 4.4–4.6 depend on its FSM signal API
- 4.4 (Talker tool-using) — depends on 4.3 + 4.1 (belief grounding)
- 4.5 (wake greeting) — depends on 4.3 + 4.4 (Talker.greet from 4.4)
- 4.6 (mic-mode flip) — depends on 4.3 (FSM mic-mode signal source)
- 4.7 (slow-path wiring) — depends on 4.2 + 4.3 (FSM working sub-modes)

Epic 5 stories: independent of each other except 5.4 depends on 5.3 (systemd) being live for the soak.

### Success Criteria for the Handoff

- [x] `epics.md` rewrite committed with `editHistory` block.
- [x] `sprint-status.yaml` reflects new story IDs and status (Epic 3 + 4 + 5 stories all `backlog`; Epic 1 + 2 stories untouched at `review`; v1.5 items marked `v1.5-backlog`).
- [x] This Sprint Change Proposal committed at `build_documents/planning-artifacts/sprint-change-proposal-2026-05-06.md` for audit traceability.
- [x] All four canonical spec docs (PRD/brief/distillate/architecture) consistent with epics.md (already true post-`ed8b276`).
- [ ] Next agent (`bmad-create-story` driven by Amelia or whoever picks up Epic 3 first) generates the Story 3.1 spec in `implementation-artifacts/3-1-expression-map-loader.md` *(not done in this proposal — happens when Epic 3 begins)*.

### Approval

User (Kamal) explicitly approved the proposal incrementally:
- Mode preference: **Incremental** (per-epic edit proposal review)
- Epic 3 outline: **Approved** (single "a" response)
- Epic 4 outline: **Approved** (single "a" response)
- Epic 5 + v1.5 outline: **Approved** (single "a" response)
- Decision points (5-epic structure, Epic 3/4 sizing, v1.5 placement): **Deferred to PM judgement** ("your call")

This proposal is approved for implementation. **No further user approval needed for the documentation changes captured here.** Per-story implementation will require Kamal's continued engagement story-by-story per existing developer-loop conventions.
