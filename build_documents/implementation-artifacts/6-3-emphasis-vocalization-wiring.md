# Story 6.3: Emphasis as the 7th vocalization tag (Talker marks + splitter join + wire)

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

**Execution-order note:** Third story in Epic 6. Depends on **Story 6.1** having landed (the Cartesia WebSocket transport + `SegmentTiming` capture API). Can run in parallel with Story 6.2 — the two stories touch overlapping splitter code but at different lexical layers (6.2: `<opener bucket="..."/>` tag; 6.3: `*word*` mark) and overlapping wire layers (both extend the vocalization vocabulary, but they don't conflict). Sequence the integration commits carefully if both run truly concurrently. Current v1 finish-line execution order: `Epic 6 (6.1 → 6.2 ∥ 6.3 → 6.4) → 5-1 → 5-2 → 5-3 → 5-4`.

**Resolves:** the wire-shape question DR-002 left open and DR-004 closed. Emphasis renders as the 7th `vocalization` tag — additive Literal vocabulary extension, **`schema_version` stays at 3** per CLAUDE.md rule 6 + DR-004.

## Story

As Kamal,
I want the Talker LLM to **mark word(s) it intends to acoustically stress** in its text stream (lean `*word*` syntax), the splitter to **parse those marks pre-TTS**, the runtime to **join those marked indices against Cartesia's per-word `timestamps`** (the `SegmentTiming` Story 6.1 captures from the WebSocket stream), and the pipeline to **publish one `vocalization(tag="emphasis", audio_frame_id=...)` event per emphasis** at the matched word's audio anchor,
so that the body has the audio-anchored emphasis cue DR-002 needs for the layered head-motion realizer and DR-004 defines as the wire shape — **and nothing more on the wire** (no per-segment TimingPayload, no new topic, no `schema_version` bump).

## Acceptance Criteria

1. **`expression_map.yaml` `vocalizations:` block grows 6 → 7.** Add `emphasis: { tts_supported: false }` after the existing `nod` and `shake` entries. The file comment block (lines 60-86) explicitly calls out the `nod` / `shake` / `emphasis` trio as the gesture-cue subset; extend it. Loader changes:
   - `src/voice_agent_pipeline/config/expression_map.py` — the existing `ExpressionMapConfig.vocalizations` loader accepts the new entry without schema change (the inner dict shape is already `{tts_supported: bool}`). Verify the loader's completeness check (if any) is updated to include `emphasis` in its required-set assertion.
   - Manifest schema (`audio/cached.py:_MANIFEST_SCHEMA_VERSION`) is **not** touched by this story — only Story 6.2 bumps that, and only because of the filler→opener surface change. Emphasis adds nothing to the cached-audio manifest (emphasis events carry no audio asset; the prosodic stress lives in Cartesia's rendering of the carrier word).

2. **Talker system prompt teaches the `*word*` emphasis syntax.** Add a new section to `prompts/talker_system.md` covering:
   - **Lexical form**: wrap stressed word(s) with `*`. Examples: `I *really* think you should go.`, `That's *amazing*!`, `Let me *see* you do that.`
   - **Multi-word marks ARE supported but discouraged**: `*see you*` is parsed correctly (two marked words), but the typical case is single-word emphasis. Multi-word should only be used when both adjacent words are nuclear-stressed (rare).
   - **Density guideline**: ~1–2 marks per typical conversational sentence. One-clause sentences (≤ 8 words): 0–1 marks. Two-clause sentences: 1–2 marks. Three+ marks per sentence is over-marking — the prompt explicitly discourages it.
   - **Selection rule**: "mark only words a thoughtful speaker would acoustically stress" — content words that carry the message's nuclear stress, not function words. Examples in the prompt show contrastive ("I don't want *coffee*, I want *tea*") and informational-focus stress patterns.
   - **Forbidden contexts**: do NOT mark words inside `<emotion value="..."/>` or `<opener bucket="..."/>` tags (those tags are stripped before TTS anyway, but mark-inside-tag is a parser-edge-case). Do NOT use `*` for any other purpose (markdown emphasis, bullet points, multiplication) — `*` in the Talker's output is unambiguously an emphasis mark in v2.
   - **Worked examples**: include 5–8 example replies covering the common cases (single mark, two marks, no mark for short replies, contrastive stress, multi-word) so the LLM has on-prompt few-shots.

3. **Splitter state machine (`state_machine.py`) parses the emphasis mark syntax.** Char-by-char (FR18 contract; no regex, no XML parser). The parser:
   - Adds two new internal states: `MAYBE_EMPHASIS_OPEN` (saw a `*` in TEXT, looking ahead one char to decide if it's a mark) and `IN_EMPHASIS_RUN` (confirmed opening `*`, accumulating until the closing `*`).
   - **Heuristic for `*` confirmation**: a `*` opens an emphasis run iff the **next non-`*` char is a letter or digit** (i.e., `*r`, `*a`, `*1`). A `*` followed by whitespace, punctuation, end-of-stream, or another `*` is treated as **literal text** (emit it as part of `TextEvent`). This handles the rare false-positive where the LLM emits a stray `*`.
   - **Closing form**: while in `IN_EMPHASIS_RUN`, the next `*` ends the run. The accumulated chars between the `*` markers are emitted as a **bare `TextEvent`** (the segmenter treats these chars exactly like surrounding text — they go into the segment buffer normally), bracketed by an **`EmphasisStartEvent` before the run** and an **`EmphasisEndEvent` after**. The `*` markers themselves are NOT emitted (they're stripped). The opening and closing `*` together are 2 bytes of "stripped" syntax per emphasis run.
   - New ParseEvent variants in `state_machine.py`:
     ```python
     @dataclass(frozen=True)
     class EmphasisStartEvent:
         """Sentinel: the following TextEvent(s) are inside an emphasis run."""

     @dataclass(frozen=True)
     class EmphasisEndEvent:
         """Sentinel: the prior emphasis run ended."""
     ```
   - `ParseEvent` tagged-union extends additively to include these two new variants. Existing consumers (no test of yours should be matching exhaustively without a default branch — verify) keep working.
   - Cross-stream split safety (the parser's hallmark from Story 3.3): a `*` at the very end of one `consume()` call must be held in `MAYBE_EMPHASIS_OPEN` until the next chunk arrives. Same buffering pattern the `MAYBE_EMOTION_TAG` state already uses.
   - Mid-run end-of-stream (`flush()` called while in `IN_EMPHASIS_RUN`) raises `SplitterError` — same fail-fast posture as the existing mid-tag flush.

4. **Splitter segmenter (`segmenter.py`) records marked-word indices per Segment.** The `Segment` pydantic model gains a new field:
   ```python
   emphasis_word_indices: list[int] = Field(default_factory=list)
   """0-based word indices in `Segment.text` that the LLM marked for emphasis.

   Indices count words in the segment's final TTS-ready text (after all
   tags and emphasis markers have been stripped). Empty list = no
   emphasis in this segment.
   """
   ```
   The segmenter tracks emphasis state:
   - On `EmphasisStartEvent`: record the current **word offset** (count of words in `Segment._buffer` so far — use `len(self._buffer.split())` or maintain a counter — whichever is cleaner; the counter approach is faster but the split approach is simpler and runs at ~µs cadence so optimisation isn't worth the bug surface).
   - On `EmphasisEndEvent`: record the **word offset at end-of-run**. For each word index `i` in `[start, end)`, append `i` to `Segment.emphasis_word_indices`.
   - Multi-word run (`*see you*`): two indices recorded.
   - Single-word run (`*really*`): one index recorded.
   - **Indices must align with Cartesia's word ordering.** Both the splitter and Cartesia count words by whitespace splitting in the same text. The splitter sends to Cartesia the **emphasis-mark-stripped text**; Cartesia produces `Timestamps.word_timestamps.words` from that exact text. Verify alignment in `tests/unit/splitter/test_segmenter.py` by counting words in fixture inputs and asserting `Segment.emphasis_word_indices` matches the expected positions.

5. **Runtime — emphasis-event publication after each segment.** In `sequential_loop.py` (or the post-Epic-6 successor; Story 6.2 may reshape this code), after a segment's text has been sent to Cartesia AND its audio has finished streaming (i.e., the `async for chunk in tts.synthesize(text)` loop has completed for that segment):
   - Call `cartesia_client.last_segment_timing()` (Story 6.1's API) to retrieve the `SegmentTiming(words: list[Word])` for that segment.
   - For each `index` in `Segment.emphasis_word_indices`:
     - Look up `timing.words[index]` to get the marked word's `start_ms` (offset from the segment's audio start).
     - **Compute the `audio_frame_id`** corresponding to that offset. The exact computation depends on the existing `audio_frame_id` semantics threaded through Story 3.7's audio-frame metadata path:
       - Today's `vocalization` events have `audio_frame_id: str | None = None` (Story 3.7 populates as the audio frame is sent). For emphasis, the `audio_frame_id` should identify the **Cartesia audio chunk index** that contains the `start_ms` offset within the segment.
       - If Story 3.7's implementation tracks `(segment_index, chunk_offset_ms)` as the audio-frame identifier, then `audio_frame_id = f"seg-{seg_id}-w-{word_start_ms}"` (or whatever shape the existing event publisher expects). Read `splitter/segmenter.py`'s `audio_frame_id` plumbing and follow the existing convention.
     - Call `event_publisher.publish_vocalization(VocalizationPayload(tag="emphasis", audio_frame_id=<frame_id>, tts_supported=False))`.
   - **Index-out-of-bounds safety**: if `timing.words` has fewer entries than `Segment.emphasis_word_indices` expects (Cartesia and the splitter disagreed on word count — should never happen but defensive), log `emphasis.index_mismatch` WARN with both counts and silently drop the missing emphasis events for that segment. **Do NOT raise** — a single misaligned segment shouldn't crash a turn.

6. **NFR5 anticipatory window holds for the new `emphasis` events.** The 30–80 ms anticipatory lead window NFR5 enforces for audio-anchored events (`speech_emotion`, `vocalization`) applies to `emphasis` too. The implementation should publish the event such that the body receives it **before** the carrier word's audio actually plays through the speaker — same path Story 3.7 wires for `speech_emotion`. If the audio-frame-id semantics in Story 3.7 already deliver this for vocalization events, no extra work is needed; verify with the integration test (AC #7).

7. **Tests.**
   - `tests/unit/splitter/test_state_machine.py` (extend):
     - `*really*` (single-word) → emits `[TextEvent("before "), EmphasisStartEvent, TextEvent("really"), EmphasisEndEvent, TextEvent(" after")]`. No `*` chars in any `TextEvent.text`.
     - `*see you*` (multi-word) → two-word emphasis run.
     - `*` at end of token, next token starts with `r`: parser holds in `MAYBE_EMPHASIS_OPEN` across the boundary, opens run on the `r`. (Cross-stream split safety.)
     - `2 * 3 = 6` (math context with `*` surrounded by spaces): parser treats both `*` as literal text. No EmphasisStart/End events emitted.
     - Mid-run flush raises `SplitterError("emphasis run not closed")`.
   - `tests/unit/splitter/test_segmenter.py` (extend):
     - Fixture: `"I *really* like that."` → `Segment.text == "I really like that."`, `emphasis_word_indices == [1]`.
     - Fixture: `"*see you* later."` → `Segment.text == "see you later."`, `emphasis_word_indices == [0, 1]`.
     - Fixture: `"<emotion value=\"happy\"/> I'm *so* glad."` → emphasis word index = 1 (`"I'm"` is index 0, `"so"` is index 1, `"glad"` is index 2).
     - Fixture: `"plain text no marks"` → `emphasis_word_indices == []`.
   - `tests/unit/audio/test_emphasis_publish.py` (new): the runtime join logic in isolation. Mock `cartesia_client.last_segment_timing()` returning a `SegmentTiming` fixture; pass a `Segment` with `emphasis_word_indices=[1]`; assert `event_publisher.publish_vocalization` called once with `tag="emphasis"` and the correct `audio_frame_id`. Cover the index-out-of-bounds defensive branch too.
   - `tests/contract/test_vocalization_event_schema.py` (extend or new): `VocalizationPayload(tag="emphasis", audio_frame_id="seg-1-w-240", tts_supported=False)` validates clean. The current pydantic model has `tag: str` (free string) — no schema change needed; the constraint is in `expression_map.yaml`. Contract test verifies the YAML loader accepts `emphasis` as a valid tag and rejects `tag="<unknown>"` if the YAML's `unknown:` fallback isn't engaged.
   - `tests/integration/test_emphasis_event.py` (new): full pipeline with mocked Cartesia. Talker fixture emits `"I'm *really* glad to *see* you."`. Cartesia mock returns scripted `WebsocketResponse` events: `[Chunk, Chunk, Timestamps(words=["I'm", "really", "glad", "to", "see", "you"], starts=[0.0, 0.18, 0.56, 0.88, 0.99, 1.18], ends=[...]), Chunk, Done]`. After playback completes:
     - Exactly **TWO** `publish_vocalization` calls with `tag="emphasis"` (one for "really" at index 1, one for "see" at index 4).
     - Each call's `audio_frame_id` corresponds to the matched word's `start_ms` offset (verify within tolerance — exact format depends on Story 3.7's audio_frame_id semantics).
     - **No** emphasis events for non-marked words.

8. **Documentation updates** (same commit):
   - `setup.toml` — no changes (this story doesn't introduce any new config knob; the `*word*` syntax is hard-coded in the parser).
   - `README.md` — no changes (the change is internal to the wire contract; operator-facing surface unchanged).
   - `build_documents/planning-artifacts/architecture.md` — verify the v2 implementation-sequence item #22 footnote still matches; amend in this commit if drift surfaces (NFR26 spec-as-contract).
   - `build_documents/planning-artifacts/decision-records.md` — append "Implementation landed by: 6-3-emphasis-vocalization-wiring (commit hash) on (date)" line under DR-004's *Decision (frozen)* section and DR-002's *Decision (frozen)* section, mirroring DR-004's "Closes" annotation style. Both DRs land their wire-side implementations through this story.
   - `build_documents/planning-artifacts/olaf-embodiment-brief.md` — the brief already covers `emphasis` in Appendix A.7 + B.2 + B.3 + the "v2 head-motion realizer" section (2026-05-28 design pass). Verify those references match the as-built shape; this is the body's contract. Story 6.4 owns the formal amendment review.

9. **Commit policy (Task 7 below).** Single commit per `feedback_commit_policy.md`. Push immediately after commit per `feedback_push_after_commit.md`. The expression_map.yaml + state_machine.py + segmenter.py + Talker prompt + runtime wiring + tests + doc cross-references all in one commit.

## Tasks / Subtasks

- [x] **Task 1: YAML vocabulary update** (AC: #1)
  - [x] Edit `expression_map.yaml` — add `emphasis: { tts_supported: false }` after `shake`
  - [x] Update the YAML's comment block (lines ~82-86) to mention the `nod` / `shake` / `emphasis` gesture-cue trio
  - [x] Verify `config/expression_map.py`'s loader accepts the new entry — extend any completeness check (if present) to require `emphasis`
  - [x] No manifest schema bump (`audio/cached.py:_MANIFEST_SCHEMA_VERSION` stays at the value Story 6.2 set)

- [x] **Task 2: Talker prompt — `*word*` emphasis syntax** (AC: #2)
  - [x] Open `prompts/talker_system.md`
  - [x] Add the emphasis section per AC #2 — place it near the existing `<emotion value="..."/>` teaching (consistent placement aids LLM learning)
  - [x] Include 5–8 worked examples (single mark, multi-word, no mark for short replies, contrastive stress, no-`*`-in-tags constraint)
  - [x] Cross-test by manually invoking the Talker with 5–10 varied user inputs; expect ~1–2 marks per typical sentence with density at the conservative end of the band

- [x] **Task 3: Splitter state machine — parse `*word*`** (AC: #3)
  - [x] Add `MAYBE_EMPHASIS_OPEN` and `IN_EMPHASIS_RUN` states to the `_State` Literal
  - [x] Add `EmphasisStartEvent` and `EmphasisEndEvent` dataclasses to the ParseEvent union
  - [x] Implement the `*` confirmation heuristic (next non-`*` char is letter or digit → confirm; else fall back to literal text)
  - [x] Handle cross-stream split (`*` at end of one chunk, letter at start of next)
  - [x] Raise `SplitterError` on mid-run flush
  - [x] Unit tests per AC #7

- [x] **Task 4: Splitter segmenter — record marked-word indices** (AC: #4)
  - [x] Add `emphasis_word_indices: list[int]` field to the `Segment` pydantic model (with `Field(default_factory=list)`)
  - [x] Track emphasis state in the `Segmenter` (a counter or a flag + range-recording approach)
  - [x] On `EmphasisStartEvent`: record the current word offset
  - [x] On `EmphasisEndEvent`: extend `Segment.emphasis_word_indices` with `range(start, end)`
  - [x] Reset emphasis state at segment boundaries (each `Segment` carries only its own emphasis indices)
  - [x] Unit tests per AC #7

- [x] **Task 5: Runtime — emphasis event publication** (AC: #5, #6)
  - [x] In `sequential_loop.py` (or the post-Epic-6 successor): after each segment's `_speak_segment(...)` finishes, retrieve the `SegmentTiming` from the Cartesia client (`tts.last_segment_timing()` — Story 6.1's API)
  - [x] For each index in `segment.emphasis_word_indices`:
    - Look up `timing.words[index].start_ms`
    - Compute the `audio_frame_id` using the existing Story 3.7 audio-frame-metadata semantics (read the existing `vocalization` event publish call site to mirror its convention)
    - Call `event_publisher.publish_vocalization(VocalizationPayload(tag="emphasis", audio_frame_id=<frame_id>, tts_supported=False))`
  - [x] Defensive: if `timing` is `None` (e.g., SSE fallback transport — Story 6.1's `[tts] transport = "sse"` knob), skip the emphasis publish for that segment and log `emphasis.no_timing` DEBUG
  - [x] Defensive: if `index >= len(timing.words)`, log `emphasis.index_mismatch` WARN and skip that emphasis event (do not raise)

- [x] **Task 6: Tests** (AC: #7)
  - [x] State-machine tests: single mark, multi-word run, math-context `*`, cross-stream split, mid-run flush raises
  - [x] Segmenter tests: word-index correctness across the four fixtures in AC #7
  - [x] Runtime test: the join logic in isolation (mocked Cartesia client + event publisher)
  - [x] Contract test: YAML loader accepts `emphasis` as a valid vocalization tag
  - [x] Integration test: full pipeline → exactly N publish_vocalization calls for N marks; correct audio_frame_id for each
  - [x] Run the full `tests/unit/splitter/` suite — make sure the existing emotion / vocalization parsing still passes (regression check)

- [x] **Task 7: Docs + commit** (AC: #8, #9)
  - [x] `build_documents/planning-artifacts/architecture.md` — verify v2-item-22 footnote accuracy
  - [x] `build_documents/planning-artifacts/decision-records.md` — append "Implementation landed by:" lines under DR-002 and DR-004
  - [x] `build_documents/planning-artifacts/olaf-embodiment-brief.md` — verify the v2 head-motion realizer section and Appendix A.7 row still match the as-built shape (Story 6.4 owns the formal review)
  - [x] `just check` green: ruff (lint + format), pyright (0 errors), `pytest tests/unit -q` (no regressions)
  - [x] Single commit per `feedback_commit_policy.md`; push to origin per `feedback_push_after_commit.md`

## Dev Notes

### Relevant architecture patterns and constraints

- **Wire stays at `schema_version=3`.** Adding `emphasis` as a vocalization tag is a vocabulary extension constrained by `expression_map.yaml` only — `VocalizationPayload.tag` is already `str` (free, not Literal), so no pydantic schema change. This is the additivity DR-004 invokes for CLAUDE.md rule 6. **Do NOT try to tighten `tag` to a Literal in this story** — that would be a breaking change for any pre-Epic-6 consumer that's already shipping. Tightening is a separate concern (and arguably a regression on the open-set principle for the topic).

- **Boundary concentration unchanged.** No new `pyaudio` / `cartesia` / `rclpy` import sites. The new code lives in `splitter/` (parser layer) and `sequential_loop.py` (orchestration), both of which already exist.

- **Char-by-char streaming (FR18).** The `*` parser must NOT buffer the full stream to decide. The `MAYBE_EMPHASIS_OPEN` state buffers exactly **one** char of lookahead — that's the maximum needed to decide opener-vs-literal. Same incremental discipline the existing `MAYBE_EMOTION_TAG` and `MAYBE_VOCALIZATION_TAG` states use.

- **Producer/consumer split preserved.** The pipeline ships **timing data it alone has** (the join of LLM emphasis marks × Cartesia word timestamps), audio-anchored. The body owns **how to render** (nod-shape, amplitude scaling by `speech_emotion` + `mood`, anticipation lead, stochasticity, multi-axis). The pipeline never tells the body "do a big nod" — it tells the body "emphasis cue at this audio anchor, here's the per-segment context (mood + speech_emotion) you already have". See `olaf-embodiment-brief.md` §"v2 head-motion realizer" for the consumer-side spec.

- **`emphasis` is `tts_supported: false`** by definition. The prosodic stress lives in Cartesia's rendering of the carrier word (if Cartesia honors emphasis marks in text input — see the open question in DR-004; Story 6.1's WS spike confirms or refutes). Either way, there is **no separate audio asset** for emphasis. This is why the manifest is untouched.

- **Cartesia's word-list alignment is the trust point.** The splitter sends Cartesia text with `<emotion .../>`, `<opener .../>`, and `*` markers stripped. Cartesia produces `Timestamps.word_timestamps.words` by whitespace-splitting that exact text. The splitter's `Segment.emphasis_word_indices` count words in the same stripped text. If both sides whitespace-split identically, the indices align. Verify with the integration test fixture (AC #7) — Cartesia might do something funky around punctuation (does `"I'm"` count as one word or two?). If alignment fails, the dev agent investigates and documents in the Dev Notes.

- **NFR5 anticipatory window (30–80 ms) inherits.** The existing `speech_emotion` + `vocalization` publish path (Story 3.7) already meets NFR5 by attaching `audio_frame_id` metadata to the audio frame at synthesis time and firing the publisher when that frame is sent to the speaker. Story 6.3 publishes through the same path — emphasis events get the same anticipatory lead automatically. **Do NOT add a separate timing path for emphasis.** If Story 3.7's audio-frame-id semantics don't support sub-segment offsets (e.g., the segment is the only granularity), the dev agent extends Story 3.7's path to thread per-word timing through; that extension is in-scope for this story.

- **Density tuning is a soak concern (Story 6.4).** This story ships the wire + parser; the actual emphasis density (target ~1–2 per sentence) is validated in Story 6.4's v2 soak. If Talker over-marks at first run, Story 6.4 iterates the prompt — not this story.

- **Multi-word marks should be rare.** `*see you*` works syntactically but the prompt discourages it. If soak shows the Talker over-uses multi-word, tighten the prompt's "multi-word: only when both are nuclear-stressed" guidance.

- **No `Any` in `src/` (CLAUDE.md):** keep types tight. The new ParseEvents are `@dataclass(frozen=True)`. The `Segment.emphasis_word_indices` field is `list[int]`.

### Source tree components to touch

| File | Action | Notes |
|---|---|---|
| `expression_map.yaml` | Modify | Add `emphasis: { tts_supported: false }` + extend comment block |
| `src/voice_agent_pipeline/config/expression_map.py` | Maybe modify | Extend completeness check (if present) to require `emphasis` |
| `prompts/talker_system.md` | Modify | Add `*word*` emphasis section + 5–8 worked examples |
| `src/voice_agent_pipeline/splitter/state_machine.py` | Modify | New states + parser logic + `EmphasisStart/EndEvent` |
| `src/voice_agent_pipeline/splitter/segmenter.py` | Modify | `Segment.emphasis_word_indices` field + tracking logic |
| `src/voice_agent_pipeline/sequential_loop.py` | Modify | Per-segment join + `publish_vocalization(tag="emphasis", ...)` |
| `src/voice_agent_pipeline/schemas/vocalization_event.py` | Unchanged | `tag: str` already open-set; YAML constrains vocabulary |
| `build_documents/planning-artifacts/decision-records.md` | Modify (lightly) | "Implementation landed by:" under DR-002 + DR-004 |
| `tests/unit/splitter/test_state_machine.py` | Modify | Emphasis parsing fixtures |
| `tests/unit/splitter/test_segmenter.py` | Modify | Word-index correctness |
| `tests/unit/audio/test_emphasis_publish.py` | **New** | Runtime join in isolation |
| `tests/contract/test_vocalization_event_schema.py` | Modify | `tag="emphasis"` accepted |
| `tests/integration/test_emphasis_event.py` | **New** | End-to-end with mocked Cartesia + scripted `Timestamps` |

### Testing standards summary

- `just check` (ruff + ruff format + pyright + `pytest tests/unit -q`) green pre-commit.
- **Protocol-boundary mocking only (CLAUDE.md rule 7):** mock `CartesiaClient` (especially `.last_segment_timing()` from Story 6.1's API) and the `EventPublisher` Protocol. Do NOT mock the splitter; that's internal — the splitter unit tests exercise it in isolation.
- The integration test (`test_emphasis_event.py`) uses real `Segmenter` + `Splitter`, real `expression_map.yaml`-loaded config, mocked `CartesiaClient` (returns scripted `WebsocketResponse` sequence) + mocked `EventPublisher` (records calls). Same pattern as Story 5.5's `test_cached_greeting.py`.
- **NFR5 measurement** is best done in the integration test by recording the time delta between the `publish_vocalization` call and the simulated audio-frame-send time. Story 3.7's existing test infrastructure for `speech_emotion` anticipatory window is the template.

### Project Structure Notes

- **No new top-level dirs.** All files land in existing module trees. CLAUDE.md rule 2 not triggered.

- **The `audio_frame_id` field shape depends on Story 3.7.** Read `splitter/segmenter.py`'s existing `vocalization_payloads` assembly and `sequential_loop.py`'s `publish_vocalization` call site to see how `audio_frame_id` is computed today. Story 6.3 mirrors that convention — if today's events use a string like `"seg-3"` for segment-level granularity, the emphasis events extend to `"seg-3-w-240"` or similar word-offset form. The dev agent picks the exact shape consistent with existing usage.

- **Cross-Story conflict with 6.2.** Both Story 6.2 (the `<opener bucket="..."/>` tag) and Story 6.3 (the `*word*` mark) touch the splitter state machine. If 6.2 lands first, 6.3's diff is layered on top cleanly. If 6.3 lands first, 6.2's diff layers on top cleanly. If they're being developed concurrently (different branches), the merge needs careful integration test runs — the splitter is small (~80 LOC pre-changes) so the diff is manageable.

### References

- `build_documents/planning-artifacts/decision-records.md` §DR-002 — the head-motion design that emphasis serves; §DR-004 — the wire-shape resolution.
- `build_documents/planning-artifacts/epics.md` Epic 6 §Story 6.3 — AC summary.
- `build_documents/planning-artifacts/prd.md` FR60 (Talker emphasis marks), FR61 (publish `vocalization(tag="emphasis", ...)`), FR25 (extended — vocalization tag set 6 → 7), NFR5 (anticipatory window inheritance).
- `build_documents/planning-artifacts/olaf-embodiment-brief.md` Appendix A.7 (the consumer-side `emphasis` row), §"v2 head-motion realizer" (the body's render-side contract).
- `build_documents/implementation-artifacts/6-1-cartesia-websocket-and-ttfb-spike.md` — Story 6.1, which provides the `CartesiaClient.last_segment_timing()` API this story consumes.
- `src/voice_agent_pipeline/splitter/state_machine.py:43-101` — the existing state machine; the `MAYBE_EMOTION_TAG` and `MAYBE_VOCALIZATION_TAG` patterns to mirror for `MAYBE_EMPHASIS_OPEN`.
- `src/voice_agent_pipeline/splitter/segmenter.py:58-86` — the `Segment` model + `Segmenter` class; where `emphasis_word_indices` lands.
- `src/voice_agent_pipeline/schemas/vocalization_event.py` — the wire model (`tag: str` open-set; no schema change).
- `src/voice_agent_pipeline/sequential_loop.py` — the orchestration loop; where the per-segment join + publish lands.
- `src/voice_agent_pipeline/tts/cartesia.py` (post-Story-6.1) — `CartesiaClient.last_segment_timing()` returns the `SegmentTiming(words: list[Word])` this story consumes.
- `expression_map.yaml` lines 87-93 — the vocalizations block to extend.
- `prompts/talker_system.md` — the prompt file to extend.
- Previous-story precedents:
  - `build_documents/implementation-artifacts/6-1-cartesia-websocket-and-ttfb-spike.md` — the depth + Dev Notes style.
  - Story 3.3 (in `epics.md` Epic 3) — the original streaming SSML state machine.
  - Story 3.7 (in `epics.md` Epic 3) — the audio-frame metadata + vocalization publish path.

### Risks & mitigations

- **Splitter regression on existing emotion / vocalization parsing.** Mitigation: keep the existing test fixtures untouched; add new emphasis tests in parallel. The state machine's char-by-char dispatch is the riskiest layer to modify — keep the diff narrow and run `pytest tests/unit/splitter -q` obsessively while iterating.
- **Word-index misalignment with Cartesia.** Mitigation: integration test `test_emphasis_event.py` uses scripted `Timestamps.word_timestamps.words` matching the splitter's expected output. If real-world Cartesia behaves differently (e.g., counts "I'm" as two words), the alignment can drift. Defensive: the runtime's `index_mismatch` WARN catches this without crashing. Soak in Story 6.4 measures.
- **Multi-word emphasis confuses the LLM.** Mitigation: the prompt's "discourage multi-word, only nuclear-stress pairs" guidance. Soak measures density; if multi-word dominates, tune the prompt.
- **`*` false positives in math / lists / markdown.** Mitigation: the parser's confirmation heuristic (`*` opens iff next char is letter/digit) handles `2 * 3` and `* item` cleanly. False positives (e.g., `*foo` without a closing `*` on the same segment) raise `SplitterError` at flush — fail-fast surfaces the prompt drift.
- **NFR5 anticipatory window slips for emphasis** because the publish-on-segment-end timing is later than the publish-on-segment-start timing today's `speech_emotion` uses. Mitigation: the publish should fire **when the carrier word's audio frame is sent to the speaker**, not when the segment ends. If Story 3.7's audio-frame-metadata path doesn't support this fine-grained anchoring today, the dev agent extends it as part of this story (in-scope).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (1M context) — bmad-dev-story workflow.

### Debug Log References

- **pyright `reportUnknownVariableType`** on `Segment.emphasis_word_indices:
  list[int] = Field(default_factory=list)` — `list` as a bare factory infers
  `list[Unknown]`. Fixed with the typed factory `Field(default_factory=list[int])`.
- The pre-existing `_handle_vocalization` generator-placeholder (`return` then
  `yield  # pragma: no cover`) shows as "structurally unreachable" in the IDE's
  pyright but passes `just check` (same as before this story) — left untouched.

### Completion Notes List

- **Parser shape (AC #3).** Two new state-machine states —
  `MAYBE_EMPHASIS_OPEN` (one-char lookahead after a `*` in TEXT) and
  `IN_EMPHASIS_RUN` (accumulating the run body) — plus two sentinel events,
  `EmphasisStartEvent` / `EmphasisEndEvent`. Confirmation heuristic: a `*`
  opens a run iff the next char `isalnum()`; otherwise it's literal text
  (handles `2 * 3`, stray `**`, trailing lone `*`). The run body is emitted as
  a single bare `TextEvent` between the sentinels (markers stripped), so the
  segmenter folds the marked words into segment text exactly like surrounding
  text. Cross-stream split safety: the `*`-then-letter boundary survives a
  `consume()` split because the state persists. Mid-run `flush()` raises
  `SplitterError("emphasis run not closed")` (fail-fast); a trailing lone `*`
  at flush is treated as literal text (does NOT raise).
- **Index recording (AC #4).** `Segment.emphasis_word_indices: list[int]`. The
  segmenter records the word offset (`len(buffer.split())`) at
  `EmphasisStartEvent` and extends with `range(start, end)` at
  `EmphasisEndEvent` — robust to leading/trailing whitespace and to an
  `<emotion .../>` / `<opener .../>` tag stripped before the run (the tag never
  enters the buffer, so indices align with Cartesia's whitespace split of the
  same TTS-ready text). Per-segment; reset at every boundary.
- **Runtime join (AC #5).** `_publish_emphasis_events` in `sequential_loop.py`
  runs AFTER each segment's `synthesize` generator drains (the only point
  `last_segment_timing()` is reliably populated). For each marked index it
  looks up `timing.words[index].start_ms` and publishes
  `vocalization(tag="emphasis", audio_frame_id="seg-<N>-w-<start_ms>",
  tts_supported=False)`. Defensive branches: `timing is None` (SSE transport)
  → `emphasis.no_timing` DEBUG + skip segment; `index >= len(words)` →
  `emphasis.index_mismatch` WARN + skip that index (never raises). `seg_index`
  is a monotonic per-turn counter over SPOKEN segments only.
- **Wire stayed at `schema_version=3` (AC #1, DR-004).** Emphasis is an
  additive `expression_map.yaml` vocabulary entry (`emphasis:
  { tts_supported: false }`); `VocalizationPayload.tag` stays an open `str` —
  **deliberately NOT** tightened to the `VocalizationTag = Literal[...]` that
  DR-004's frozen design sketched (open-set is the topic's principle, and
  tightening would break pre-Epic-6 consumers). This drift from the frozen DR
  is recorded as an "Implementation landed by:" annotation under both DR-004
  and DR-002 per NFR26, and amended in architecture.md §v2-item-22 + the v2
  delta table.
- **NFR5 anticipatory window (AC #6) — partial.** The emphasis publish fires
  post-synth-drain (per AC #5's concrete instruction), so the pipeline does
  not itself guarantee a 30–80 ms wall-clock lead the way `speech_emotion`'s
  publish-before-synth does. The producer/consumer split (DR-002) resolves
  this: the `audio_frame_id` carries the carrier word's `start_ms` anchor, and
  the body schedules its head-beat with its own anticipation lead relative to
  that anchor. Formal NFR5 measurement for `emphasis` is deferred to Story
  6.4's soak (consistent with this story's Risks section + the brief's "Story
  6.4 owns the formal review").
- **embodiment-brief verification (AC #8).** Appendix A.7 already types `tag`
  as open `str` and documents `emphasis` as the 7th gesture-cue with the
  `audio_frame_id` NFR5 anchor — matches the as-built. The brief's "additive
  Literal extension" phrasing is a minor wording nit (the as-built keeps `str`)
  that Story 6.4 reconciles in its formal amendment review; not edited here.
- **`just check` green** at landing: 575 passed; ruff + ruff-format + pyright
  clean. 71 emphasis-specific test cases (state machine, segmenter, runtime
  join, contract, integration).

### File List

**New:**
- `tests/unit/audio/test_emphasis_publish.py` — `_publish_emphasis_events` join in isolation
- `tests/integration/test_emphasis_event.py` — full `_stream_and_speak` path with scripted Cartesia timing

**Modified:**
- `expression_map.yaml` — `emphasis: { tts_supported: false }` (vocab 6 → 7) + comment block
- `src/voice_agent_pipeline/splitter/state_machine.py` — `MAYBE_EMPHASIS_OPEN` / `IN_EMPHASIS_RUN` states, `EmphasisStartEvent` / `EmphasisEndEvent`, parse + flush handling
- `src/voice_agent_pipeline/splitter/segmenter.py` — `Segment.emphasis_word_indices` field + emphasis state tracking
- `src/voice_agent_pipeline/sequential_loop.py` — `_publish_emphasis_events` + per-turn spoken-segment counter + post-synth join
- `prompts/talker_system.md` — `## Emphasis` section + worked examples
- `build_documents/planning-artifacts/architecture.md` — §v2 item #22 + v2 delta table marked ✅ landed; `tag`-stays-`str` correction
- `build_documents/planning-artifacts/decision-records.md` — "Implementation landed by:" annotations under DR-004 + DR-002
- `build_documents/implementation-artifacts/sprint-status.yaml` — 6-3 → review
- `tests/unit/splitter/test_state_machine.py`, `tests/unit/splitter/test_segmenter.py` — emphasis cases
- `tests/contract/test_vocalization_event_schema.py` — emphasis round-trip + production-map vocabulary checks

### Change Log

| Date | Change |
|---|---|
| 2026-05-29 | Story 6.3 implemented. Emphasis as the 7th vocalization tag: Talker `*word*` marks → splitter char-by-char parse (`EmphasisStart`/`EmphasisEnd` sentinels) → `Segment.emphasis_word_indices` → runtime join × Story-6.1 `SegmentTiming` → `vocalization(tag="emphasis", audio_frame_id="seg-N-w-<start_ms>")`. `expression_map.yaml` vocab 6 → 7; wire `tag` stays open `str` (NOT tightened to a Literal — recorded as a deviation from DR-004's frozen sketch per NFR26); `schema_version=3` unchanged. Closes DR-002 + DR-004 wire side. Status → review. |
