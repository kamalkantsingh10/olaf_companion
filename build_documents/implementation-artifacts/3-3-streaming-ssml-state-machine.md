# Story 3.3: Streaming SSML state machine + boundary-based segmenter

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want a hand-rolled streaming parser that consumes Cartesia-tagged text token-by-token, splits across token boundaries safely, and emits segments on whichever boundary comes first (sentence terminator / emotion tag / vocalization tag),
so that segments can be handed to TTS and the resolver in lockstep without buffering the full response, with two distinct event paths (`speech_emotion` + `vocalization`) — and Story 3.7 can attach segment metadata to Pipecat audio frames.

## Acceptance Criteria

1. **`src/voice_agent_pipeline/splitter/state_machine.py` — hand-rolled streaming parser.** Parses two surface forms inline within an otherwise plain-text token stream:
   - **Emotion tag**: `<emotion value="X"/>` — self-closing XML-ish, value is a Cartesia emotion tag string. Matches the Cartesia SSML emotion-tag form documented at https://docs.cartesia.ai/.
   - **Vocalization tag**: `[name]` — bracket-wrapped lowercase identifier. Names are tokens like `laughter`, `sigh`, `gasp`, `clears_throat` from `expression_map.yaml`'s `vocalizations:` block.
   - Implementation discipline: ~50–100 LOC, **zero external dependencies** (no regex, no XML parser — both buffer the full stream, killing streaming). Hand-rolled state-machine reading char-by-char from token chunks. (FR18.)

2. **The state machine handles tags split across token boundaries.** Given input tokens `["Hello <emoti", "on value=\"excited\"/> Great!"]`, the state machine produces events as if the input were `"Hello <emotion value=\"excited\"/> Great!"` — partial-tag state is preserved in the machine across `consume(token)` calls, and the assembled `<emotion value="excited"/>` emits exactly once. Same for bracket vocalization tags split across tokens. (FR18: "tags may split across boundaries.")

3. **Emit interface — events flow OUT of the state machine.** The state machine exposes `consume(token: str) -> Iterator[ParseEvent]` (or async-iterator) where `ParseEvent` is one of:
   - `TextEvent(text: str)` — plain text chunk between tags. Multiple `TextEvent`s may emit per `consume` call (e.g., text before a tag + text after).
   - `EmotionTagEvent(value: str)` — a fully-assembled `<emotion value="X"/>` tag's value.
   - `VocalizationTagEvent(name: str)` — a fully-assembled `[name]` vocalization.
   - `EndOfStreamEvent()` — explicit terminator emitted by `flush() -> Iterator[ParseEvent]` when the upstream stream is exhausted. `flush()` also flushes any buffered plain text as a final `TextEvent` if non-empty.
   - These three event types can be a tagged-union of pydantic v2 BaseModels (frozen, `extra="forbid"`) defined in the same file. Or `@dataclass(frozen=True)` if pydantic feels heavy here — both are acceptable per architecture.md §"Type System Conventions"; **pick dataclass** for the parse-event types since they don't cross a wire boundary.

4. **Malformed tag at end-of-stream → `SplitterError`.** If `flush()` is called while the parser is mid-tag (e.g., last consumed token was `"<emoti"` with no closing token), `flush()` raises `SplitterError(state=<machine_state>, partial=<buffered>)`. v1 fail-fast — caller does not catch; process exits, systemd restarts (Epic 5). FR18 + architecture.md §"Error Handling".

5. **`src/voice_agent_pipeline/splitter/segmenter.py` — boundary-based emission.** Consumes the state machine's `ParseEvent` stream and emits `Segment` instances on whichever boundary comes first:
   - **Sentence terminator** (`.`, `?`, `!` at end of accumulated text) — primary cadence boundary.
   - **Emotion-tag change** — a new emotion value differs from the segment's emotion. Emits the prior segment with its emotion + accumulated text, then starts a new segment carrying the new emotion.
   - **Vocalization tag** — vocalization within the current segment doesn't immediately emit; the vocalization's payload is **added to the segment's `vocalization_payloads`** (FR19). The segment still emits on the next sentence-terminator or emotion-tag boundary, carrying any vocalizations that were captured.

6. **`Segment` shape.** `Segment` is a frozen pydantic v2 BaseModel (`extra="forbid"`) in `splitter/segmenter.py`:
   - `text: str` — the segment's plain text, **with vocalization tags retained or stripped per `tts_supported`** (see AC #8). This is what gets handed to Cartesia.
   - `speech_emotion_payload: SpeechEmotionPayload | None` — set when the segment carries an emotion change (resolved via Story 3.2's `resolve()`). `None` when the segment continues the prior segment's emotion (the cache will dedup; the segment may still have vocalizations).
   - `vocalization_payloads: list[VocalizationPayload]` — every vocalization seen during this segment, in order. May be empty.

7. **End-to-end stream flow example (AC reference for tests):** Input `<emotion value="content"/> Hello there. <emotion value="excited"/> Great news!` produces, in order:
   - `Segment(text="Hello there.", speech_emotion_payload=<content payload>, vocalization_payloads=[])`
   - `Segment(text="Great news!", speech_emotion_payload=<excited payload>, vocalization_payloads=[])`

8. **Vocalization text handling — `tts_supported` drives keep-vs-strip.** When the segmenter encounters a `VocalizationTagEvent("laughter")`:
   - It calls Story 3.2's `resolve_vocalization("laughter", mapping)` to get a `VocalizationPayload`.
   - It appends the payload to the segment's `vocalization_payloads`.
   - It then asks: is `payload.tts_supported`?
     - **`True`** (e.g., `[laughter]` per the production map): the literal `[laughter]` characters are **kept in the segment's `text`** so Cartesia renders the audio. (FR25.)
     - **`False`** (e.g., `[sigh]`): the literal `[sigh]` characters are **stripped from `text`** before going to TTS. The segment still publishes the `VocalizationEvent` for embodiment. (FR25.)

9. **Emotion dedup deferred to the cache, not the segmenter.** The segmenter reports the emotion on every emotion-changed segment via `speech_emotion_payload`; Story 3.2's `LastPublishedCache.should_publish(payload)` (called by the pipeline in Story 3.7) decides whether to actually publish. The segmenter does **not** maintain its own dedup state. (Architecture: single source of truth — the cache.)

10. **State the segmenter retains across calls (FR24 plumbing):** `current_emotion: SpeechEmotionPayload | None` (the last seen, regardless of cache decision) and `_buffer: str` (text accumulated since last segment emission). Both reset on `reset() -> None` at turn boundary. The segmenter does **not** retain `last_published_emotion` — that's the cache's job (`LastPublishedCache._last`). Architecture's "single source of truth for what was published" lives in the cache, not the segmenter.

11. **Unit tests in `tests/unit/splitter/test_state_machine.py`** — state-machine-level tests (parse stream → ParseEvent stream):
    - `test_plain_text_emits_text_event`
    - `test_emotion_tag_emits_emotion_event` — `<emotion value="excited"/>` → exactly one `EmotionTagEvent("excited")`.
    - `test_vocalization_tag_emits_vocalization_event` — `[laughter]` → `VocalizationTagEvent("laughter")`.
    - `test_tag_split_across_token_boundary` — `["Hello <emoti", "on value=\"excited\"/> Great"]` → `TextEvent("Hello ")`, `EmotionTagEvent("excited")`, `TextEvent(" Great")`. Order matters.
    - `test_vocalization_split_across_token_boundary` — `["Ha[laug", "hter] there"]` → `TextEvent("Ha")`, `VocalizationTagEvent("laughter")`, `TextEvent(" there")`.
    - `test_multiple_emotion_tags_in_stream` — three emotion changes → three events in order.
    - `test_no_tags_passes_through` — `"Hello world."` → exactly one `TextEvent("Hello world.")`.
    - `test_malformed_tag_raises_at_flush` — input `"Hello <emoti"` then `flush()` → `SplitterError` with the partial buffer in context.
    - `test_self_closing_tag_with_extra_whitespace` — `<emotion value="excited" />` (space before `/>`) parses successfully. The Cartesia emitter is consistent but defensive parsing is cheap.
    - `test_attribute_value_with_apostrophe_or_double_quote` — Cartesia is reliable on double-quotes; the parser supports `value="X"`. **Single-quote support is NOT required** in v1 (document the choice).
    - `test_open_bracket_without_close_in_text` — text containing literal `[` not followed by an identifier-then-`]` is treated as plain text. (Avoid false-positive vocalization triggers on `"[redacted]"` style strings — though Cartesia's prompts shouldn't emit those.)
    - `test_flush_emits_buffered_text` — buffer holds `"hello"` → `flush()` yields `TextEvent("hello")` then `EndOfStreamEvent()`.

12. **Unit tests in `tests/unit/splitter/test_segmenter.py`** — segmenter-level tests (`Segment` emission timing + content):
    - `test_sentence_terminator_emits_segment`
    - `test_emotion_change_emits_segment` — emotion change closes the prior segment.
    - `test_vocalization_attaches_to_segment` — `[laughter]` mid-segment adds payload to `vocalization_payloads`; segment emits at next sentence terminator.
    - `test_supported_vocalization_kept_in_text` — `[laughter]` stays in `Segment.text` (`tts_supported=True`).
    - `test_unsupported_vocalization_stripped_from_text` — `[sigh]` removed from `Segment.text` (`tts_supported=False`).
    - `test_multiple_vocalizations_in_segment` — `Hello [laughter]. [sigh] World.` → first segment `text="Hello [laughter]."` with one vocalization; second segment `text=" World."` (note space) with one vocalization.
    - `test_no_emotion_segment_carries_none_payload` — input with no emotion tag → `Segment.speech_emotion_payload is None`.
    - `test_consecutive_same_emotion_does_not_set_payload_twice` — clarify with the AC #9 contract: every emotion-tagged segment carries the resolver-produced payload; the **cache** filters subsequent same-emotion segments.
    - `test_reset_clears_buffer_and_emotion` — `reset()` between two streams → second stream's first segment emotion is fresh.
    - `test_segmenter_drives_real_resolver` — instead of mocking `resolve`, build a real small `ExpressionMapConfig` and let the segmenter call `resolve` and `resolve_vocalization` for real (Story 3.2's pure functions). Asserts on the resulting `Segment.speech_emotion_payload` field shape.

13. **`SpeechEmotionPayload` and `VocalizationPayload` import from Story 3.2.** Until Story 3.4 moves them to `schemas/`, `from voice_agent_pipeline.splitter.mapping import SpeechEmotionPayload, VocalizationPayload, resolve, resolve_vocalization`. The `Segment` class lives in `splitter/segmenter.py`.

14. **Logging:** sparse. `splitter.malformed_tag` at ERROR before raising `SplitterError` (this is the only path where logging adds information beyond the exception's str). No per-token DEBUG logs — that's volume, not signal. **Never log token contents at INFO+** — they may carry transcript or response text (NFR25).

15. **No mocks of pydantic models or internal functions.** Tests construct real `ExpressionMapConfig` via Story 3.2's `_make_mapping()` helper or a tmp-path tiny YAML. Architecture.md §"Test Patterns" + CLAUDE.md rule #7.

16. **`just check` stays green.** All Story 1/2 + 3.1 + 3.2 tests still pass. ruff + ruff format + pyright + pytest.

## Tasks / Subtasks

- [ ] **Task 1: Implement `splitter/state_machine.py`** (AC: #1, #2, #3, #4)
  - [ ] Module docstring per `feedback_code_comments.md` — explain: hand-rolled state machine, why no regex/XML parser, two surface forms (`<emotion value="X"/>` and `[name]`), token-boundary handling.
  - [ ] Define `@dataclass(frozen=True)` event types: `TextEvent`, `EmotionTagEvent`, `VocalizationTagEvent`, `EndOfStreamEvent`. Group via a `ParseEvent = TextEvent | EmotionTagEvent | VocalizationTagEvent | EndOfStreamEvent` type alias.
  - [ ] State machine: small enum-like states like `TEXT`, `IN_EMOTION_TAG`, `IN_VOCALIZATION_TAG`, with internal char-by-char buffers. Use `Literal[...]` (CLAUDE.md rule #3 — no `enum.Enum`).
  - [ ] `consume(token: str)` is a generator (`def consume(...) -> Iterator[ParseEvent]:` with `yield`). `flush()` is the same shape, drains any buffered plain text, then emits `EndOfStreamEvent()`. If mid-tag, raises `SplitterError(state=..., partial=...)` BEFORE yielding.
  - [ ] Aim for ≤100 LOC including blank lines; if it grows past 150, your design is fighting the problem.

- [ ] **Task 2: Write `tests/unit/splitter/test_state_machine.py`** (AC: #11)
  - [ ] Test the state machine in isolation (no segmenter, no resolver). The test surface is just `consume` + `flush`.
  - [ ] One behavior per test. Tag-split tests are the most valuable — that's where regressions will be subtle.
  - [ ] Use `list(machine.consume(token))` to materialize the generator into a list per test step.

- [ ] **Task 3: Implement `splitter/segmenter.py`** (AC: #5, #6, #7, #8, #9, #10)
  - [ ] Module docstring per `feedback_code_comments.md` — explain: boundary-based emission (sentence/emotion/vocalization), keep-vs-strip vocalization based on `tts_supported`, single source of truth for "what was published" lives in the cache (not here).
  - [ ] `Segment` pydantic v2 BaseModel (`frozen=True, extra="forbid"`) per AC #6.
  - [ ] `Segmenter` class. `__init__(self, mapping: ExpressionMapConfig)`. State: `_buffer: str = ""`, `current_emotion: SpeechEmotionPayload | None = None`, `_pending_vocalizations: list[VocalizationPayload] = []`.
  - [ ] `consume(token: str) -> Iterator[Segment]`: drive the state machine, accumulate text/emotion/vocalization, emit `Segment` on boundaries.
  - [ ] `flush() -> Iterator[Segment]`: emit any final partial segment; reset state via `reset()`.
  - [ ] `reset() -> None`: clears `_buffer`, `current_emotion`, `_pending_vocalizations`. Story 3.7 calls this on turn boundaries.
  - [ ] **Sentence terminator detection**: scan accumulated text after each text chunk for `.`, `?`, `!`. Decision: emit immediately on terminator (don't wait for whitespace) so the segment closes cleanly even if the next token continues into a new sentence. The text *includes* the terminator.

- [ ] **Task 4: Write `tests/unit/splitter/test_segmenter.py`** (AC: #12, #15)
  - [ ] Build small real `ExpressionMapConfig` via `_make_mapping()` (re-use Story 3.2's helper if accessible — extract to `tests/unit/splitter/conftest.py` if it makes the call site cleaner).
  - [ ] Drive the segmenter via `list(seg.consume(token))` per token; collect emitted segments across calls.
  - [ ] **Critical case**: vocalization-keep-vs-strip in `Segment.text`. This is the one Story 3.7 will rely on for correct TTS.

- [ ] **Task 5: Pass `just check`; fix anything red** (AC: #16)
  - [ ] ruff (especially `S` security rules — none should fire here), pyright on the `Iterator[ParseEvent]` typing (ensure `from __future__ import annotations` if needed, or use the `Iterator` from `collections.abc`), pytest unit run.

- [ ] **Task 6: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] Single commit titled `Story 3.3: streaming SSML state machine + boundary-based segmenter`.
  - [ ] `git push` immediately.

## Dev Notes

### Architectural intent

Story 3.3 builds the **streaming intelligence** — the only piece in Epic 3 that isn't trivial wiring. The state machine is the architecturally hard part: it must be incremental (no buffering of the full LLM response), zero-dependency (no regex, no XML parser — both will buffer), and robust to token boundaries.

The segmenter is a thin orchestrator over the state machine + Story 3.2's resolver. Its only "logic" is boundary detection (when to close a segment) and vocalization keep-vs-strip in TTS text.

This story does NOT need to know about Pipecat, audio frames, or `EventEnvelope`. Story 3.7 is the integration story that wraps `Segmenter` in a Pipecat processor and threads metadata onto audio frames. Keep this story's surface clean and pure.

### Why hand-rolled, not regex / XML

A regex like `r'<emotion value="([^"]+)"/>'` against the streaming token buffer **will work** but has two failure modes:
1. **Buffer growth**: regex needs the full string from "no match yet" to "match found" — i.e., it accumulates the entire stream until a match, defeating streaming.
2. **Token-boundary safety**: regex doesn't know which prefix is "definitely no match" vs "could still match" — you'd need bespoke prefix-matching logic on top.

An XML parser (e.g., `xml.etree`) is even worse — it requires the full document.

A hand-rolled state machine reads char-by-char, can emit `TextEvent`s as soon as text is "definitely not part of a tag" (i.e., after seeing any char that breaks a tag-prefix), and uses a tiny per-state buffer (~32 bytes worst case for `<emotion value="..."/>`).

Reference architecture: every "streaming token parser" in production Pipecat-style systems uses this exact pattern. ~50–100 LOC is the right ballpark.

### `dataclass` vs `pydantic` for `ParseEvent`

The architecture allows both. Pick **dataclass** here:
- These types don't cross a wire boundary (they're internal to the splitter).
- They're throwaway — emitted, consumed, discarded.
- pydantic's validation overhead is wasted on internal types.
- `@dataclass(frozen=True)` is half a line; pydantic's `BaseModel` + `ConfigDict` is more typing.

`Segment` is different — it crosses to Story 3.7 (the pipeline integration), and it's worth pydantic-validating. Keep `Segment` as a `pydantic.BaseModel`.

### Sentence-terminator detection edge cases

Three subtleties:
1. **Decimals** (`3.14`) — `.` between digits is NOT a sentence terminator. v1 punt: treat any `.` as a terminator. Cartesia's prompts produce conversational speech, so decimals are rare. If false-positives occur in production, Story 5.5 calibration owns the fix.
2. **Abbreviations** (`Mr. Smith`) — same punt. Spelled-out names mostly avoid this; if not, 5.5 owns.
3. **Multi-char terminators** (`?!`, `...`) — segment on the first terminator; subsequent terminators continue plain text. Acceptable for v1.

Document the punt in the dev record. The architecture's NFR1 latency is dominated by external services, not segment-boundary timing — over-engineering this is wasted effort.

### Whitespace handling around tags

Cartesia's prompts produce text like `<emotion value="content"/> Hello there.` — leading space after the tag. In test cases AC #7, the segment text is `"Hello there."` (no leading space). The segmenter should consume one optional whitespace character after a tag end, before starting the next segment's text buffer. Document this in a code comment so a reader doesn't think "the space is missing — bug?"

### Why `Segment.text` keeps the sentence terminator

The terminator goes to Cartesia. Cartesia's prosody depends on punctuation (rising pitch on `?`, falling on `.`). Stripping the terminator would degrade speech quality. AC #5/#6 phrasing is implicit on this — make it explicit in the test (`Segment.text == "Hello there."` not `"Hello there"`).

### What this story does NOT do

- **No publishing.** Story 3.5 owns the `EventPublisher`. The segmenter emits `Segment` instances with attached payloads; the pipeline (Story 3.7) wraps them in events and publishes.
- **No turn-boundary lifecycle.** Story 3.7 calls `Segmenter.reset()` on `working → listening`.
- **No audio-frame metadata threading.** Story 3.7's territory.
- **No Talker SSML prompt update.** Story 3.7.
- **No `EventEnvelope`.** Story 3.4.
- **No fancy XML / SSML feature support.** v1 supports exactly two tag forms: `<emotion value="X"/>` and `[name]`. Anything else is plain text. If the LLM emits `<break time="500ms"/>` in some future revision, it'll fall through as plain text, which Cartesia will treat as garbage and likely render literally — Story 5.5 calibration territory.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/splitter/state_machine.py`
- `src/voice_agent_pipeline/splitter/segmenter.py`
- `tests/unit/splitter/test_state_machine.py`
- `tests/unit/splitter/test_segmenter.py`

It modifies:
- (none expected; if `splitter/__init__.py` needs to re-export new symbols for callers, add them — but Story 3.7 will import from the submodules directly).

It does NOT modify:
- `splitter/mapping.py` (Story 3.2's territory; only imports from it).
- `pipeline.py` (Story 3.7).
- `schemas/` (Story 3.4).

### Testing standards

- **Hand-rolled state machine deserves heavy test coverage.** The token-boundary cases are where regressions hide. Test a tag split at every possible byte position (test via parametrize over a range of split points if you want belt-and-suspenders).
- **No mocks** — the state machine is pure; the segmenter calls Story 3.2's pure functions. Real `ExpressionMapConfig` via `_make_mapping()`.
- **caplog for the malformed-tag ERROR** — assert on `event="splitter.malformed_tag"` and the partial content key.
- **One behavior per test.** State-machine tests are simple — resist the urge to bundle.

### What "done" looks like

- `just check` exits 0.
- A REPL session can drive the pipeline:
  ```python
  from voice_agent_pipeline.splitter.state_machine import StateMachine
  from voice_agent_pipeline.splitter.segmenter import Segmenter
  # ...build mapping...
  seg = Segmenter(mapping)
  for chunk in tokens:
      for segment in seg.consume(chunk):
          print(segment)
  for segment in seg.flush():
      print(segment)
  ```
  produces the expected `Segment`s.
- Story 3.7 can `from voice_agent_pipeline.splitter.segmenter import Segmenter, Segment` and integrate with no further refactoring.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Streaming + Concurrency (Batch 2)] — boundary-based segmentation strategy.
- [Source: build_documents/planning-artifacts/architecture.md#Type System Conventions] — Literal for state enums; pydantic for cross-boundary types; dataclass for internal-only.
- [Source: build_documents/planning-artifacts/architecture.md#Error Handling] — `SplitterError` for parser-side failures; v1 fail-fast.
- [Source: build_documents/planning-artifacts/prd.md#FR18] — token-by-token streaming, tags may cross boundaries.
- [Source: build_documents/planning-artifacts/prd.md#FR19] — boundary-based segmentation (sentence / emotion / vocalization).
- [Source: build_documents/planning-artifacts/prd.md#FR24] — last-published cache (lives in 3.2; segmenter just reports).
- [Source: build_documents/planning-artifacts/prd.md#FR25] — vocalization keep-vs-strip from TTS text per `tts_supported`.
- [Source: build_documents/planning-artifacts/epics.md#Story 3.3: Streaming SSML state machine + boundary-based segmenter]
- [Source: build_documents/implementation-artifacts/3-1-expression-map-loader.md] — `ExpressionMapConfig` shape, `vocalizations.<name>.tts_supported`.
- [Source: build_documents/implementation-artifacts/3-2-mapping-resolver-and-cache.md] — `resolve`, `resolve_vocalization`, `SpeechEmotionPayload`, `VocalizationPayload`.
- [Source: src/voice_agent_pipeline/errors.py] — `SplitterError` already exists (Story 1.4).
- [External: https://docs.cartesia.ai/build-with-cartesia/capabilities/voice-control] — Cartesia inline tag syntax reference.

## Dev Agent Record

### Agent Model Used

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List
