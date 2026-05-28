# Story 6.4: Instrumentation + soak + embodiment-brief amendment review (Epic 6 wrap)

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

**Execution-order note:** Fourth and final story in Epic 6 — the **wrap**. Lands AFTER Stories 6.1, 6.2, AND 6.3 have all merged. Story 6.4's soak measures the **combined post-Epic-6 system**, which is what v1's sign-off soak (Story 5-4) will then validate. v1 finish-line execution order: `Epic 6 (6.1 → 6.2 ∥ 6.3 → 6.4) → 5-1 → 5-2 → 5-3 → 5-4`.

**Closes:** NFR35 (latency instrumentation backfill), NFR26 (spec-as-contract for the olaf-embodiment-brief.md amendment in lockstep with the wire change), and the soak-validation half of NFR33 / NFR34 / NFR5 for `emphasis` events.

## Story

As Kamal,
I want (a) **per-turn latency instrumentation** so I can see `stt_ms`, `ttft_ms`, `ttfb_ms`, `end_to_first_real_audio_ms`, opener-source / opener-duration / dead-air-after-opener fields, and `emphasis_count_per_turn` in the structured logs (replacing the hardcoded `end_to_transcript_ms=0` placeholder at `sequential_loop.py:287`); (b) a **v2 soak** that measures NFR33 (opener onset ≤ 700 ms p95), NFR34 (dead-air-after-opener ≤ 250 ms p95), NFR5 anticipatory window for `emphasis` events, and emphasis density per sentence (target ~1–2); (c) the **`olaf-embodiment-brief.md` amendment review** to confirm the 2026-05-28 design-pass updates still match the as-built shape post-Stories 6.1–6.3 (NFR26 spec-as-contract — amend in lockstep if drift surfaces),
so that DR-001's projected timeline (median ~3.0 s → ~1.7–2.0 s end-of-speech → real-answer) is **validated against measured numbers**, DR-002's head-motion timing is observable in the body's renderer, and Epic 6 lands as a coordinated cross-project release tag with `olaf-embodiment`.

## Acceptance Criteria

1. **Replace the `end_to_transcript_ms=0` placeholder at `sequential_loop.py:287`.** The current `stt.transcript` log event emits `end_to_transcript_ms=0` (hardcoded — has been "tracked" as a TODO since Story 2.5). Replace with the real measurement:
   - `stt_ms`: nanosecond delta between VAD `utterance.started` event and STT `transcript` event, integer milliseconds.
   - Rename the log field from `end_to_transcript_ms` to `stt_ms` (clearer name; aligns with the new `turn.complete` schema below).
   - The single-clock methodology (existing `tts.first_frame.ttfb_ms` convention — `time.time_ns()` deltas) is the reference; preserves comparability with DR-001's empirical baseline (n=308 turns, mined from existing logs).

2. **Add a new `turn.complete` structured log event, emitted once per turn at the natural turn boundary** (after the last audio frame plays and the FSM transitions back to `listening`). Schema (all integer milliseconds; nullable fields use `None` not `0`):

   ```python
   # Emitted at sequential_loop.py per-turn-end, after the FSM has
   # transitioned back to `listening`. One event per turn.
   log.info(
       "turn.complete",
       # Latency decomposition (NFR35)
       stt_ms=int,                                  # vad_end → transcript (also emitted on stt.transcript per AC #1)
       ttft_ms=int | None,                          # transcript → talker first non-tag token (best-effort; None when provider doesn't expose)
       ttfb_ms=int,                                 # cartesia synth-request-sent → first audio frame received (already emitted on tts.first_frame; carried here for the per-turn rollup)
       end_to_first_real_audio_ms=int,              # vad_end → first frame of the REAL answer (NOT the opener)
       # Opener accounting (NFR33 / NFR34)
       opener_source=Literal["llm_tag", "timer_fallback", None],  # None = self-gated, no opener
       opener_bucket=OpenerBucket | None,           # which bucket fired (None when no opener)
       opener_duration_ms=int | None,               # opener's audio duration (None when no opener)
       opener_onset_ms=int | None,                  # vad_end → first opener audio frame (None when no opener)
       dead_air_after_opener_ms=int | None,         # opener last frame → real-answer first frame (None when no opener)
       # Emphasis accounting (DR-004 / Story 6.3)
       emphasis_count_per_turn=int,                 # number of `vocalization(tag="emphasis", ...)` events published for this turn
       # Turn shape (helps reconstruct mode without joining other events)
       routing=Literal["fast_path", "slow_path", "clarification"],  # talker_only vs orchestrator vs low-confidence-clarification
       had_tool_call=bool,                          # talker emitted any tool calls this turn
   )
   ```

   - The schema is documented inline in `architecture.md` §Logging Conventions, mirroring the existing convention table.
   - A **contract test** in `tests/contract/test_turn_complete_log_schema.py` validates that the emitted event carries the documented field set. Use a structlog test fixture that captures emitted events; assert the schema matches.

3. **Track per-turn latency state in `sequential_loop.py`.** A small dataclass (private to the module, internal-only — `@dataclass(frozen=False)` since values accumulate during the turn) holds the per-turn timing:

   ```python
   @dataclass
   class _TurnTimings:
       vad_end_ns: int                           # set on VAD utterance.captured
       stt_done_ns: int | None = None            # set on STT transcript
       talker_first_token_ns: int | None = None  # best-effort (provider-dependent)
       opener_first_frame_ns: int | None = None  # set on cached opener's first frame
       opener_last_frame_ns: int | None = None   # set when opener's play_cached returns
       real_first_frame_ns: int | None = None    # set on the real-answer's first audio frame
       turn_end_ns: int | None = None            # set on FSM listening transition

       # Annotations from the turn's events
       opener_source: Literal["llm_tag", "timer_fallback", None] = None
       opener_bucket: OpenerBucket | None = None
       opener_duration_ms: int | None = None
       emphasis_count: int = 0
       routing: Literal["fast_path", "slow_path", "clarification"] = "fast_path"
       had_tool_call: bool = False
   ```

   - One instance per turn. Constructed at VAD `utterance.started`. Discarded after `turn.complete` emits.
   - Each "set on event X" is wired at the appropriate point in `sequential_loop.py` — e.g., `_TurnTimings.opener_first_frame_ns = time.time_ns()` at the call into `play_cached(...)`.
   - The `emphasis_count` increments each time Story 6.3's runtime publishes a `vocalization(tag="emphasis", ...)` event. Plumb a callback from the publish-call-site or read the publisher's per-turn counter (whichever is cleaner).
   - `had_tool_call` is set when the talker's stream-end event reports any tool calls (Story 4.4 emits these).
   - `routing` is set at the TurnRouter decision point (Story 2.4 / 4.7).

4. **Emit the `turn.complete` event at turn boundary.** At the natural turn-end point in `sequential_loop.py` (after the last audio frame plays and `fsm.on_last_audio_frame()` returns the FSM to `listening`), compute the derived fields from `_TurnTimings` and emit:

   ```python
   timings.turn_end_ns = time.time_ns()
   log.info(
       "turn.complete",
       stt_ms=(timings.stt_done_ns - timings.vad_end_ns) // 1_000_000,
       ttft_ms=(timings.talker_first_token_ns - timings.stt_done_ns) // 1_000_000 if timings.talker_first_token_ns else None,
       ttfb_ms=...  # carried from tts.first_frame log within this turn (the existing log fires per-segment; carry the first segment's value)
       end_to_first_real_audio_ms=(timings.real_first_frame_ns - timings.vad_end_ns) // 1_000_000 if timings.real_first_frame_ns else None,
       opener_source=timings.opener_source,
       opener_bucket=timings.opener_bucket,
       opener_duration_ms=timings.opener_duration_ms,
       opener_onset_ms=(timings.opener_first_frame_ns - timings.vad_end_ns) // 1_000_000 if timings.opener_first_frame_ns else None,
       dead_air_after_opener_ms=(
           (timings.real_first_frame_ns - timings.opener_last_frame_ns) // 1_000_000
           if timings.opener_last_frame_ns and timings.real_first_frame_ns else None
       ),
       emphasis_count_per_turn=timings.emphasis_count,
       routing=timings.routing,
       had_tool_call=timings.had_tool_call,
   )
   ```

5. **`ttft_ms` (best-effort) via the Talker stream events.** Story 2.2's Talker implementation streams events (text deltas + stream-end). The first non-empty text-delta event corresponds to TTFT. Wire `timings.talker_first_token_ns = time.time_ns()` at the first non-empty `TalkerTextDelta` event. Providers that buffer (some OpenAI configurations) will produce a TTFT that effectively equals time-to-completion; that's accurate even if not useful. Providers that don't expose any token-by-token streaming (none currently used in v1) would leave `ttft_ms=None` — the field's optionality handles this.

6. **`emphasis_count` increment from Story 6.3's publish call.** The cleanest wire: Story 6.3 publishes `vocalization(tag="emphasis", ...)` from `sequential_loop.py` (per Story 6.3 AC #5). At that publish call-site, increment `timings.emphasis_count += 1`. No new abstraction; just a counter on the live timings object.

7. **v2 soak script + run + report.**
   - Add a `scripts/soak_v2.py` (or `src/voice_agent_pipeline/tools/soak_v2.py` if scripts/ doesn't exist) that:
     - Reads the rotating-logfile path from `setup.toml`'s logging config (or hard-code the default `./logs/voice-agent.log`).
     - Tails the file for a configurable window (default: real wall-clock 7 days, but can be cut shorter for the Story 6.4 sign-off via a `--minutes N` flag for dev iteration).
     - Parses `turn.complete` events from the JSON-line log.
     - Aggregates p25 / p50 / p75 / p90 for: `stt_ms`, `ttft_ms`, `ttfb_ms`, `end_to_first_real_audio_ms`, `opener_onset_ms`, `dead_air_after_opener_ms`, `emphasis_count_per_turn`.
     - Counts `opener_source` distribution (`llm_tag` vs `timer_fallback` vs `null`/self-gated).
     - Counts `routing` distribution.
     - Counts `emphasis.index_mismatch` WARN events (should be 0 in a healthy run).
     - Counts `embodiment.unmapped_vocalization` events (visible only if the body's log is also being soaked alongside; if not, skip).
     - Writes a report to `build_documents/implementation-artifacts/6-4-soak-report.md` with the aggregates + a target-comparison table.
   - **Pass criteria** for the soak (the v1 sign-off bar these enable):
     - `end_to_first_real_audio_ms`: p50 ~1.7–2.0 s (DR-001 projection); p95 ≤ NFR1's 1500 ms is **likely violated** because NFR1 was set before Epic 6 — the soak report flags this and prompts a PRD edit if v2 numbers are materially different. The right answer might be "v2 NFR1 is now 2000 ms; update the PRD" — that's a Story 5-4 sign-off conversation, not this story's call.
     - `opener_onset_ms`: p95 ≤ 700 ms (NFR33).
     - `dead_air_after_opener_ms`: p95 ≤ 250 ms (NFR34).
     - `emphasis_count_per_turn`: average ~1–2 per turn (the multi-sentence reply case can carry 2–4; soak measures the distribution shape).
     - `emphasis.index_mismatch` count: 0.
   - **Run the soak**: 7 days is the v1 target; for the Story 6.4 commit, a 30-minute live run with mixed turn shapes is sufficient evidence the instrumentation works. Document the run duration + count in the report. The full 7-day soak runs as part of Story 5-4 once Epic 5 hardening lands.

8. **`olaf-embodiment-brief.md` amendment review.** The 2026-05-28 design pass already wrote amendments to:
   - Appendix A.7 — `tag` table row for `emphasis`.
   - Appendix A.8 — "v2 deltas" section.
   - Appendix B.2 — example `embodiment_map.yaml` block with `emphasis` entry.
   - Appendix B.3 — startup-validation extending the gesture-cue trio check (`nod`, `shake`, `emphasis`).
   - §"What Makes This Different" #5 — `emphasis` mention.
   - New §"v2 head-motion realizer" section with DR-002's layered model sketch.

   Story 6.4 **verifies these amendments still match the as-built shape** post-Stories 6.1–6.3. Walk through each amendment and the relevant pipeline code:
   - If anything drifted (e.g., the `audio_frame_id` shape differs from what the brief describes), **amend the brief in this commit** (NFR26 spec-as-contract).
   - If everything matches, document the verification outcome in this story's Dev Agent Record → Completion Notes: "Brief amendments verified accurate as of commit <hash>, no further amendment needed."

9. **DR back-references.** Append "Implementation landed by:" lines to:
   - DR-001's *Consequences / implementation notes* section — reference Story 6.2's commit (the cached opener + overlap) and Story 6.4's soak report (the projection validation).
   - DR-002's *Decision (frozen)* section — reference Story 6.3's commit (the wire-side implementation).
   - DR-004's *Decision (frozen)* section — reference Story 6.3's commit.

   Per the decision-records.md doctrine ("Records are immutable once frozen; to change a decision, add a new record"), these "Implementation landed by:" notes are **annotations**, not changes to the decision substance — they extend the historical trail. The DR file's existing precedent (DR-004's "Closes: DR-002 ..." annotation) is the template.

10. **Cross-project sign-off.** Once the pipeline-side Epic 6 stories are all committed AND the body project has shipped its Body Story 6.3 (renderer mapping + head-motion realizer per `olaf-embodiment-v2-brief.md`):
    - Tag a coordinated release in both repos (e.g., `pipeline-v0.6.0` + `embodiment-v0.6.0`).
    - Run a smoke test with both projects live: the pipeline emits `emphasis` events, the body renders head-beats, no `embodiment.unmapped_vocalization` WARNs fire.
    - Update the `epics.md` Epic 6 §Status to mark the cluster done.
    - Update `sprint-status.yaml`: all 6-1, 6-2, 6-3, 6-4 → `review` (the dev-story workflow's natural state); epic-6 stays `in-progress` until 5-4 sign-off marks the full v1 done.

11. **Documentation updates** (same commit):
    - `architecture.md` §Logging Conventions — add the `turn.complete` schema documentation.
    - `architecture.md` v2 implementation-sequence item #23 footnote — verify accuracy post-implementation.
    - `decision-records.md` — the "Implementation landed by:" annotations per AC #9.
    - `prd.md` — verify NFR35's instrumentation text matches as-built (no change expected; the AC was already written against the to-be-implemented shape).
    - `olaf-embodiment-brief.md` — amendment review per AC #8; amend if drift.
    - `voice-agent-pipeline.md` (distillate) — verify the v2 implementation-status references match; amend if drift.

12. **Commit policy (Task 7 below).** Single commit per `feedback_commit_policy.md`. Push immediately after commit per `feedback_push_after_commit.md`. The soak report + code changes + doc amendments all in one commit.

## Tasks / Subtasks

- [ ] **Task 1: Replace the `end_to_transcript_ms=0` placeholder + rename to `stt_ms`** (AC: #1)
  - [ ] Edit `sequential_loop.py:287`: replace the hardcoded `end_to_transcript_ms=0` with `stt_ms=int((stt_done_ns - vad_end_ns) // 1_000_000)`
  - [ ] Plumb `vad_end_ns` from VAD `utterance.captured` event (or the closest existing wall-clock anchor) — likely already accessible in the surrounding code
  - [ ] Update any test that asserts on the field name or value (search `tests/` for `end_to_transcript_ms`)

- [ ] **Task 2: Add `_TurnTimings` dataclass + wire per-turn accumulation** (AC: #3, #5, #6)
  - [ ] Add the `_TurnTimings` dataclass (private to `sequential_loop.py`) per AC #3
  - [ ] Construct on VAD `utterance.started`; pass through the turn's async flow
  - [ ] Wire each "set on event X" anchor in `sequential_loop.py` — STT done, Talker first token, opener first frame, opener last frame, real-answer first frame, turn end
  - [ ] Wire `emphasis_count` increment at Story 6.3's `publish_vocalization` call site
  - [ ] Wire `routing` from the TurnRouter decision point
  - [ ] Wire `had_tool_call` from the Talker stream-end event's tool_calls list

- [ ] **Task 3: Emit `turn.complete` at turn boundary** (AC: #2, #4)
  - [ ] At the natural turn-end point (after `fsm.on_last_audio_frame()` returns the FSM to `listening`), compute the derived fields per AC #4 and emit `log.info("turn.complete", ...)`
  - [ ] Handle the no-real-audio case (turn-end without a real-answer frame — e.g., tool-only reply): leave `real_first_frame_ns=None`; derived `end_to_first_real_audio_ms=None`; same for `dead_air_after_opener_ms`
  - [ ] Document the schema inline in `architecture.md` §Logging Conventions

- [ ] **Task 4: Contract test for `turn.complete` schema** (AC: #2)
  - [ ] Create `tests/contract/test_turn_complete_log_schema.py`
  - [ ] Use structlog's testing fixtures to capture emitted events
  - [ ] Assert: every required field present; nullable fields handle `None` correctly; field types are int/str/bool/Literal as documented

- [ ] **Task 5: v2 soak script** (AC: #7)
  - [ ] Create `scripts/soak_v2.py` (or `src/voice_agent_pipeline/tools/soak_v2.py`)
  - [ ] CLI: `--minutes N` (default: read until EOF; intended for log-replay or short live runs) + `--out PATH` (default `build_documents/implementation-artifacts/6-4-soak-report.md`)
  - [ ] Tail the log; parse `turn.complete` events; aggregate p25/50/75/90 + distribution counts
  - [ ] Write the report in Markdown with the metrics + target-comparison table
  - [ ] Run a 30-minute live soak; commit the report in this story's commit

- [ ] **Task 6: Embodiment-brief amendment review** (AC: #8)
  - [ ] Re-read `build_documents/planning-artifacts/olaf-embodiment-brief.md` Appendix A.7, A.8, B.2, B.3, §"What Makes This Different" #5, §"v2 head-motion realizer"
  - [ ] Compare each amendment against the as-built shape (Stories 6.1, 6.2, 6.3 commits)
  - [ ] If drift surfaces (e.g., `audio_frame_id` format differs, or `bucket: OpenerBucket | None` field surfaces differently in `CachedAudioEntry`): amend the brief in this commit
  - [ ] If no drift: document verification in Dev Agent Record → Completion Notes

- [ ] **Task 7: DR back-references + docs + commit** (AC: #9, #10, #11, #12)
  - [ ] Append "Implementation landed by:" annotations to DR-001 / DR-002 / DR-004 sections per AC #9
  - [ ] Update `architecture.md` v2-item-23 footnote if drift; update §Logging Conventions
  - [ ] Verify `voice-agent-pipeline.md` (distillate) v2 references; amend if drift
  - [ ] Verify `prd.md` NFR35 text matches as-built; amend if drift
  - [ ] Update `epics.md` Epic 6 §Status: "Implementation complete (Stories 6.1–6.4 landed); awaiting cross-project sign-off with `olaf-embodiment`"
  - [ ] Update `sprint-status.yaml`: 6-1, 6-2, 6-3, 6-4 → `review`
  - [ ] `just check` green
  - [ ] Single commit per `feedback_commit_policy.md`; push per `feedback_push_after_commit.md`
  - [ ] **Coordination point**: signal to `olaf-embodiment` that the pipeline-side cluster is shipped; cross-project sign-off (AC #10) happens out-of-band

## Dev Notes

### Relevant architecture patterns and constraints

- **`turn.complete` is the FIRST per-turn rollup log event in the pipeline.** Today, log events are per-action (`stt.transcript`, `tts.first_frame`, `activity.transition`, etc.); a downstream consumer (the DR-003 dashboard) reconstructs turn shape by grouping. `turn.complete` collapses that reconstruction work into one event per turn. The per-action events stay — `turn.complete` is **additive**, not a replacement.

- **Single-clock methodology preserved.** All timings come from `time.time_ns()` deltas within one Python process. Cross-process clock comparison (e.g., body's clock vs pipeline's clock) is NOT in scope; the FR62 single-host constraint sidesteps it for v2 head motion. NFR5 anticipatory window holds within the single-host process; any cross-host work is parked.

- **Structured logging conventions (architecture.md §Logging):** the event name is `verb.subject` form; field names are `snake_case`; durations are `_ms` suffixed (or `_ns` if nanosecond precision is intended — `turn.complete` uses `_ms`). The new event follows this.

- **`emphasis_count` is per-turn, not per-segment.** Multi-segment turns accumulate emphasis counts across all segments. A 3-sentence reply with 1 mark per sentence emits `emphasis_count_per_turn=3`.

- **`ttfb_ms` carry-over from `tts.first_frame`.** The first segment's TTFB (`tts.first_frame.ttfb_ms`) is the per-turn-relevant value — subsequent segments' TTFBs are amortised by streaming. Carry the **first** observed value into `turn.complete.ttfb_ms`.

- **`opener_*` fields handle the four cases cleanly**:
  - Self-gated turn (no opener): `opener_source=None`, `opener_bucket=None`, `opener_duration_ms=None`, `opener_onset_ms=None`, `dead_air_after_opener_ms=None`.
  - LLM-tag opener: all four set, `opener_source="llm_tag"`, `opener_bucket=<the bucket the Talker emitted>`.
  - Timer-fallback opener: all four set, `opener_source="timer_fallback"`, `opener_bucket=<config.openers.timer_fallback_bucket>` (default `acknowledge`).
  - Self-gated but real audio is slow → timer fallback fires: same as timer-fallback case.

- **Defensive `None` handling everywhere.** The derivation arithmetic in AC #4 uses `if` guards; if any source `_ns` is `None`, the derived `_ms` is `None`. This is robust against turn-shape variations (tool-only reply, mid-turn cancellation, clarification short-circuit).

- **The soak script is operator-facing, not pipeline-runtime.** Like `audio/regenerate.py` and `tts/ttfb_spike.py`, it sits outside the runtime hot path. CLAUDE.md rule 4 (no `ExternalServiceError` catches) does not apply — the soak script is a CLI tool and can catch its own errors for clean reporting.

- **PRD NFR1 may need a v2 revision after the soak.** NFR1 (simple-turn ≤ 1500 ms p95) was set in v1's PRD before Epic 6 changed the latency profile. If the soak's measured `end_to_first_real_audio_ms` p95 lands at, say, 1800 ms (still better than v1's median of 3.0 s but worse than NFR1's hard cap), the right move is a PRD edit revising NFR1 to v2 values — NOT failing the soak. **That conversation lives in Story 5-4's sign-off.** This story just produces the data.

### Source tree components to touch

| File | Action | Notes |
|---|---|---|
| `src/voice_agent_pipeline/sequential_loop.py` | Modify | `_TurnTimings` dataclass + accumulation wiring + `turn.complete` emit; rename `end_to_transcript_ms` → `stt_ms` |
| `scripts/soak_v2.py` (or `src/voice_agent_pipeline/tools/soak_v2.py`) | **New** | CLI soak-report generator |
| `build_documents/implementation-artifacts/6-4-soak-report.md` | **New** | Soak run output (committed alongside code) |
| `build_documents/planning-artifacts/architecture.md` | Modify | §Logging Conventions extension; v2-item-23 footnote verification |
| `build_documents/planning-artifacts/decision-records.md` | Modify (lightly) | "Implementation landed by:" annotations on DR-001 / 002 / 004 |
| `build_documents/planning-artifacts/olaf-embodiment-brief.md` | Maybe modify | Amendment review; amend if drift surfaces |
| `build_documents/planning-artifacts/prd.md` | Maybe modify | NFR35 text verification; PRD edit if NFR1 needs v2 revision (likely deferred to Story 5-4) |
| `build_documents/planning-artifacts/voice-agent-pipeline.md` | Maybe modify | Distillate v2-status references |
| `build_documents/planning-artifacts/epics.md` | Modify | Epic 6 §Status update |
| `build_documents/implementation-artifacts/sprint-status.yaml` | Modify | All Epic 6 stories → `review` |
| `tests/contract/test_turn_complete_log_schema.py` | **New** | Schema validation |
| `tests/unit/test_sequential_loop.py` (if exists) | Maybe modify | If existing tests assert on `end_to_transcript_ms`, rename to `stt_ms` |

### Testing standards summary

- `just check` (ruff + ruff format + pyright + `pytest tests/unit -q`) green pre-commit.
- **No new integration tests required** — Stories 6.2 and 6.3 own those; this story only adds a contract test for the new log event schema.
- The soak script's `turn.complete` parser should have a small unit test (mock log file with 3–5 known events; assert aggregation correctness). Place at `tests/unit/tools/test_soak_v2.py` if the soak script lives under `src/voice_agent_pipeline/tools/`, or `tests/scripts/test_soak_v2.py` if it's a top-level script.
- **Live soak run is the validation** — the report file is the artefact. The dev agent runs a 30-minute live soak with mixed turn shapes (simple, complex, opener-tag, self-gated, tool-call) and captures the report. The 7-day soak runs as part of Story 5-4.

### Project Structure Notes

- **`scripts/` vs `src/voice_agent_pipeline/tools/`.** The project's existing precedent (`audio/regenerate.py`, `tts/ttfb_spike.py` from Story 6.1) is to put operator tools under `src/voice_agent_pipeline/<domain>/<tool>.py` and run via `python -m voice_agent_pipeline.<domain>.<tool>`. Follow that — place `soak_v2.py` under `src/voice_agent_pipeline/tools/`. Add a `justfile` recipe `soak-v2-report: uv run python -m voice_agent_pipeline.tools.soak_v2 ...` mirroring `regenerate-audio` and `ttfb-spike`.

- **No new top-level dirs.** All files land in existing locations or as siblings to existing artefacts.

- **`build_documents/implementation-artifacts/6-4-soak-report.md`** is a generated artefact, but committed (same pattern as Story 6.1's TTFB report). Operators reading the repo get the most recent numbers at a stable path.

### References

- `build_documents/planning-artifacts/decision-records.md` §DR-001 — the latency projection this soak validates; §DR-002 + §DR-004 — the head-motion + emphasis-wire decisions this soak observes in flight.
- `build_documents/planning-artifacts/epics.md` Epic 6 §Story 6.4 — AC summary.
- `build_documents/planning-artifacts/prd.md` NFR1 (simple-turn ≤ 1500 ms p95 — may need v2 revision based on this soak's findings), NFR5 (anticipatory window — verified for `emphasis` events here), NFR26 (spec-as-contract — the embodiment-brief review), NFR33 (opener onset ≤ 700 ms p95), NFR34 (dead-air-after-opener ≤ 250 ms p95), NFR35 (latency instrumentation — this story implements).
- `build_documents/planning-artifacts/olaf-embodiment-brief.md` — the file under review in AC #8. Pay close attention to Appendix A.7, B.2, B.3, and §"v2 head-motion realizer".
- `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md` — Story 6.1's report; the format template for `6-4-soak-report.md`.
- `build_documents/implementation-artifacts/6-1-cartesia-websocket-and-ttfb-spike.md` — Story 6.1 implementation spec; the precedent for operator-tool placement.
- `build_documents/implementation-artifacts/6-2-cached-opener-system-and-cartesia-overlap.md` — Story 6.2; the opener-fields source.
- `build_documents/implementation-artifacts/6-3-emphasis-vocalization-wiring.md` — Story 6.3; the emphasis-fields source.
- `src/voice_agent_pipeline/sequential_loop.py:287` — the `end_to_transcript_ms=0` placeholder to replace.
- `src/voice_agent_pipeline/sequential_loop.py` overall — the turn-orchestration site for the new `_TurnTimings` dataclass + `turn.complete` emit.
- `src/voice_agent_pipeline/audio/regenerate.py` — precedent for operator-tool placement under `src/voice_agent_pipeline/<domain>/`.
- `src/voice_agent_pipeline/tts/ttfb_spike.py` (post-Story-6.1) — precedent for spike/soak/measurement tools.

### Risks & mitigations

- **`turn.complete` schema drift across the four turn shapes.** Mitigation: the contract test exercises each shape (simple fast-path, slow-path, clarification short-circuit, tool-only-reply) and asserts schema validity.
- **The 30-minute live soak doesn't catch rare events.** Mitigation: this story's soak is sign-off for the **instrumentation**, not for v1 latency targets. The full 7-day soak in Story 5-4 catches rare events. Document the scope-limit in the report's preamble.
- **Embodiment-brief drift between 2026-05-28 design pass and as-built.** Mitigation: AC #8 makes the review explicit + amends in lockstep. If drift is large enough that the amendment is non-trivial, escalate — the brief was supposed to match exactly.
- **PRD NFR1 conflict.** Mitigation: the soak report flags this clearly; the actual NFR revision happens in Story 5-4. Don't try to revise NFR1 in this story — that's Story 5-4's sign-off prerogative.
- **`ttft_ms` is provider-dependent.** Mitigation: the field is nullable; soak report aggregates only non-null entries. Document provider differences in the soak report's preamble.

## Dev Agent Record

### Agent Model Used

(populated by dev agent)

### Debug Log References

### Completion Notes List

### File List
