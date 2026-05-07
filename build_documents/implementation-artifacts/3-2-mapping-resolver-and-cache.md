# Story 3.2: Mapping resolver + last-published cache

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want a pure-function resolver that turns any Cartesia tag into a `SpeechEmotionPayload` via the loaded mapping with fallback-family resolution, plus a `LastPublishedCache` that dedups consecutive same-emotion publishes within a turn,
so that the splitter (Story 3.3) can call one function regardless of whether the tag is primary, secondary, family-fallback, or completely unknown — and the publisher (Story 3.5) only sees real emotion changes.

## Acceptance Criteria

1. **`SpeechEmotionPayload` lands as a pydantic model.** Story 3.2 introduces the **payload** half of the eventual `SpeechEmotionEvent` (the `EventEnvelope` mixin + event wrapper land in Story 3.4). Decision tree: place it in `src/voice_agent_pipeline/splitter/mapping.py` as an interim home, with a module-level comment marking the intended migration to `src/voice_agent_pipeline/schemas/speech_emotion_event.py` in Story 3.4. Frozen pydantic v2 BaseModel, `extra="forbid"`. Fields: `emotion: str`, `source_tag: str`, `audio_frame_id: str | None = None`, `raw_tag: str`, `resolved_fallback: str | None`, `expression_data: dict[str, Any]`. The `dict[str, Any]` is the documented extensibility seam (CLAUDE.md rule #3 / architecture.md §"Type System Conventions").

2. **`VocalizationPayload` also lands here as an interim home.** Same module (`splitter/mapping.py`) — Story 3.3's segmenter is the first caller, Story 3.4 promotes it to `schemas/vocalization_event.py`. Frozen pydantic v2 BaseModel, `extra="forbid"`. Fields: `tag: str`, `audio_frame_id: str | None = None`, `tts_supported: bool`. The Story 3.2 + 3.3 work depends on this shape; Story 3.4 will move the file but **not change the field set**.

3. **`resolve(tag, mapping) -> SpeechEmotionPayload` resolves any tag.** Signature: `def resolve(tag: str, mapping: ExpressionMapConfig) -> SpeechEmotionPayload:` — pure function (no side effects beyond the documented log emission). Behavior by case:

   - **Primary or secondary first-class hit** (`tag in mapping.emotions`): returns `SpeechEmotionPayload(emotion=tag, source_tag=tag, raw_tag=tag, resolved_fallback=None, expression_data=mapping.emotions[tag].expression_data)`. **No log emission** (this is the happy path; logs at this rate would be noise).
   - **Fallback-family hit** (`tag in <some family>.members`): returns `SpeechEmotionPayload(emotion=<family.maps_to>, source_tag=tag, raw_tag=tag, resolved_fallback=<family name>, expression_data=mapping.emotions[<family.maps_to>].expression_data)`. Logs `event="speech_emotion.fallback"` at **DEBUG** the first time per process per (tag, family) pair (de-duped via an in-memory set; FR38). Subsequent occurrences silent.
   - **Unmapped** (tag in zero families and not first-class): returns `SpeechEmotionPayload(emotion=mapping.unknown.maps_to, source_tag=tag, raw_tag=tag, resolved_fallback="unknown", expression_data=mapping.emotions[mapping.unknown.maps_to].expression_data)`. Logs `event="speech_emotion.unmapped"` at **WARN** (FR38). Every occurrence logs (truly unknown tags are alarm-worthy until they're added to a family in `expression_map.yaml`).

4. **Resolution order is exactly: primary/secondary → fallback families → unknown.** A tag listed BOTH as a first-class emotion AND in a family's members is treated as first-class (the loader does **not** explicitly forbid this overlap; the resolver's order makes the right thing happen). A tag that appears in two families is a YAML authoring bug — the resolver picks **whichever family iterates first** (Python 3.7+ dict insertion order); document this as deterministic-but-undefined-by-spec, and add a TODO in dev notes for a future loader-side uniqueness check.

5. **`LastPublishedCache` enforces FR24 dedup, scoped per-turn.** Class in `src/voice_agent_pipeline/splitter/mapping.py`:
   - `__init__(self) -> None:` — initializes `self._last: str | None = None` (the most recently approved emotion name, or `None` at start-of-turn).
   - `should_publish(self, payload: SpeechEmotionPayload) -> bool:` — returns `True` and updates `self._last = payload.emotion` if the **resolved emotion name** differs from `self._last`; returns `False` otherwise (no state mutation on `False`).
   - `should_publish` for a `VocalizationPayload`-shaped input (typed via overload or a separate method `should_publish_vocalization(payload: VocalizationPayload) -> bool`): always returns `True`. Vocalizations are punctual, never deduped (FR24).
   - `reset(self) -> None:` — sets `self._last = None`. Story 3.7's pipeline integration calls this on `activity → listening` transition (turn boundary).
   - **Decision (recommended)**: implement two separate methods (`should_publish` for emotion, `should_publish_vocalization` for vocalization) rather than an overload — clearer call sites, simpler types, no `isinstance` ladder.

6. **Cache scope is per-`LastPublishedCache`-instance, not global.** Story 3.7's pipeline holds one instance for the whole pipeline lifecycle; `reset()` is called at turn boundaries. Story 3.2 itself does not wire the lifecycle — that's 3.7. Story 3.2's tests instantiate the cache directly and verify behavior given a controlled call sequence.

7. **Vocalizations are typed end-to-end.** When the splitter (Story 3.3) encounters `[laughter]` etc., it constructs a `VocalizationPayload(tag="laughter", tts_supported=mapping.vocalizations["laughter"].tts_supported)`. Story 3.2 provides a helper `def resolve_vocalization(tag: str, mapping: ExpressionMapConfig) -> VocalizationPayload:` that:
   - If `tag in mapping.vocalizations`: returns `VocalizationPayload(tag=tag, tts_supported=mapping.vocalizations[tag].tts_supported)`. No log.
   - If `tag` is **not** in `mapping.vocalizations`: returns `VocalizationPayload(tag=tag, tts_supported=False)` — unknown vocalizations are forwarded with `tts_supported=False` (safe default — strip from TTS text). Logs `event="vocalization.unmapped"` at WARN. (No fallback families for vocalizations in v1; this is the correct shape.)

8. **Unit tests in `tests/unit/splitter/test_mapping.py`.** Mirror Story 1.2's `_VALID_TOML` pattern with a `_make_mapping()` test helper that builds a small `ExpressionMapConfig` programmatically (no YAML round-trip — tests exercise the resolver, not the loader). Cases:
   - `test_resolve_primary_emotion` — `resolve("excited", mapping)` returns the right payload, `resolved_fallback is None`, no log emitted.
   - `test_resolve_secondary_emotion` — `resolve("happy", mapping)` returns secondary payload, `resolved_fallback is None`.
   - `test_resolve_fallback_family_logs_debug_first_time` — `resolve("enthusiastic", mapping)` (where `enthusiastic ∈ high_energy_positive.members`) returns the family's `maps_to` payload with `resolved_fallback="high_energy_positive"`; emits one DEBUG `speech_emotion.fallback`. Calling again with `enthusiastic` does **not** emit (de-duped via the in-memory set).
   - `test_resolve_unmapped_tag_logs_warn_every_time` — `resolve("nevereverseen", mapping)` returns `emotion=neutral`, `resolved_fallback="unknown"`; emits WARN every call.
   - `test_resolve_first_class_takes_priority_over_family` — a tag appearing both in `mapping.emotions` and in a family's members is resolved as first-class.
   - `test_cache_dedups_consecutive_same_emotion` — `should_publish(content)` → `True`; `should_publish(content)` → `False`; `should_publish(sad)` → `True`; `should_publish(sad)` → `False`.
   - `test_cache_after_reset_republishes` — call `reset()` between two same-emotion calls → both return `True`.
   - `test_cache_vocalization_always_publishes` — `should_publish_vocalization(laughter)` returns `True` every call; `should_publish_vocalization` does not affect the emotion-cache state (interleaving an emotion + vocalization doesn't make the next emotion republish).
   - `test_resolve_vocalization_known_tag` — `resolve_vocalization("laughter", mapping)` returns `tts_supported=True` (per the production map), no log.
   - `test_resolve_vocalization_unknown_tag_warns` — `resolve_vocalization("burp", mapping)` returns `tts_supported=False`, emits `vocalization.unmapped` WARN.
   - `test_log_assertions_use_caplog` — use Story 1.7's structlog test capture pattern (the `caplog` fixture wired to structlog's `LoggerFactory`); assert on event name + level + key fields. Don't grep raw text.
   - `test_payload_extra_forbid_enforced` — constructing `SpeechEmotionPayload(..., bogus="x")` raises `ValidationError`.

9. **Logging discipline (architecture.md §"Logging Conventions"):**
   - DEBUG: `speech_emotion.fallback` — first occurrence per (raw_tag, family) per process. Fields: `raw_tag`, `resolved_fallback` (family name), `emotion` (resolved). De-duped via `set[tuple[str, str]]` at module level (or instance-level if you prefer encapsulation; module-level is fine since it's per-process state and the de-dup intent is exactly per-process).
   - WARN: `speech_emotion.unmapped` — every occurrence. Fields: `raw_tag`, `resolved_fallback="unknown"`, `emotion=neutral`.
   - WARN: `vocalization.unmapped` — every occurrence. Fields: `tag`.
   - **Never** log `expression_data` contents at any level (per Story 3.1's discipline — it may carry operator-private device addresses).
   - **Never** log raw transcripts. The resolver only sees tag strings, not user transcripts; this is structurally safe.

10. **No mocking of `ExpressionMapConfig`.** Tests construct real `ExpressionMapConfig` instances via `_make_mapping()` (or by loading a tiny tmp-path YAML). Mocking pydantic models violates CLAUDE.md rule #7.

11. **`just check` stays green; no regression in earlier stories.** ruff + ruff format + pyright + `pytest tests/unit -q`. All Story 3.1 + Epic 1/2 tests continue to pass.

12. **Cite the future migration in code comments.** A 3-line comment at the top of `splitter/mapping.py` flagging that `SpeechEmotionPayload` and `VocalizationPayload` are **temporarily** here until Story 3.4's event-schema rebuild moves them to `schemas/`. This prevents future drift like "why is the payload defined in `splitter/`?" and reduces ambiguity during 3.4's migration.

## Tasks / Subtasks

- [ ] **Task 1: Land `SpeechEmotionPayload` and `VocalizationPayload` in `splitter/mapping.py`** (AC: #1, #2, #12)
  - [ ] Create `src/voice_agent_pipeline/splitter/mapping.py` (module + `__init__.py` if not present in `splitter/`).
  - [ ] Module docstring per `feedback_code_comments.md` — explain: this module owns the tag → payload resolver + per-turn dedup cache for the embodiment channel; payload classes are temp residents until Story 3.4.
  - [ ] Define `SpeechEmotionPayload`, `VocalizationPayload` per AC #1 / #2. Frozen, `extra="forbid"`, `dict[str, Any]` only on `expression_data` (cite the architecture exception inline).
  - [ ] Add the 3-line "moves to schemas/ in Story 3.4" pointer at the top.

- [ ] **Task 2: Implement `resolve` for emotions** (AC: #3, #4, #9)
  - [ ] `def resolve(tag: str, mapping: ExpressionMapConfig) -> SpeechEmotionPayload:`. Three-case branch: first-class → family hit → unknown. Iterate `mapping.fallback_families.items()` for family lookup (insertion order is stable per Python 3.7+).
  - [ ] Module-private de-dup set: `_FALLBACK_LOG_SEEN: set[tuple[str, str]] = set()`. `(raw_tag, family_name)` as the key. **Why module-level**: the FR38 contract is "DEBUG first occurrence per process," not per call site; module-level state survives the right scope (process lifetime) without the resolver having to thread a `seen_set` arg through every caller.
  - [ ] Emit logs per AC #9 — DEBUG / WARN level discipline; no `expression_data` in any log; use structlog's `bind_contextvars` if a turn-correlation_id is in scope (it's not, in the resolver — only in segmenter; OK to skip).
  - [ ] Function-level docstring explaining the three-case resolution + the FR38 log contract.

- [ ] **Task 3: Implement `resolve_vocalization`** (AC: #7)
  - [ ] `def resolve_vocalization(tag: str, mapping: ExpressionMapConfig) -> VocalizationPayload:`. Two-case branch: known → unknown.
  - [ ] WARN log on unknown.
  - [ ] No de-dup on the WARN — vocalization unknowns are rare enough that suppressing them risks hiding regressions.

- [ ] **Task 4: Implement `LastPublishedCache`** (AC: #5, #6)
  - [ ] Class with `_last: str | None`. Instance-level state (one cache per pipeline; Story 3.7 owns lifecycle).
  - [ ] `should_publish(payload)`, `should_publish_vocalization(payload)`, `reset()`.
  - [ ] **Do NOT** add a `should_publish_polymorphic(payload: SpeechEmotionPayload | VocalizationPayload)` overload — the call-site clarity from two named methods beats the slight duplication.
  - [ ] One-line class docstring: "Per-turn dedup of `SpeechEmotionEvent`s; vocalizations always publish (FR24)." No function docstrings (architecture.md §"Documentation").

- [ ] **Task 5: Write `tests/unit/splitter/test_mapping.py`** (AC: #8, #10)
  - [ ] `tests/unit/splitter/__init__.py` if not present.
  - [ ] `_make_mapping()` helper that returns a small valid `ExpressionMapConfig` (3-4 emotions, 1-2 families, 2 vocalizations) — call `ExpressionMapConfig.model_validate({...})` directly with a Python dict, no YAML round-trip.
  - [ ] Use Story 1.7's structlog `caplog` capture pattern. If unsure, search `tests/unit/stt/test_whisper_cpu.py` for the existing fixture.
  - [ ] One behavior per test, named `test_<behavior>`. Run incrementally with `uv run pytest tests/unit/splitter/test_mapping.py -v`.

- [ ] **Task 6: Pass `just check`; fix anything red** (AC: #11)
  - [ ] Watch for: pyright on `dict[str, Any]` (cite the architecture exception); ruff on import sorting (`from voice_agent_pipeline.config.expression_map import ExpressionMapConfig` should land in the local-first-party group); `tests/unit/config/test_expression_map.py` (Story 3.1) still passing.

- [ ] **Task 7: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] Single commit titled `Story 3.2: mapping resolver + last-published cache`.
  - [ ] `git push` immediately after.

## Dev Notes

### Architectural intent

Story 3.2 builds the **resolver layer** between the `expression_map.yaml` substrate (Story 3.1) and the streaming SSML splitter (Story 3.3). Two pure functions + one stateful cache. No I/O, no network, no async. The resolver's contract is: "give me any tag, I'll give you a typed payload" — that's the entire architectural promise of the embodiment channel's quality bar (architecture.md §"`speech_emotion` Mapping Completeness — The V1 Quality Bar").

The cache exists because of FR24 — a single Cartesia turn often emits multiple sentences with the **same** emotion tag, and we don't want to publish four `SpeechEmotionEvent`s with `emotion=content` for one paragraph. Per-turn scope is intentional: when the user starts a new turn, last-published-emotion resets so the first segment of the new reply always publishes (the embodiment system needs to know "we're back").

### Why payload classes interim-live in `splitter/mapping.py`

Story 3.4 (event schema rebuild) will own `schemas/speech_emotion_event.py` and `schemas/vocalization_event.py`. Until that lands, Story 3.2 needs a typed return shape for `resolve()` — and the payload class is the right shape. Putting it temporarily in `splitter/mapping.py` keeps the schemas/ folder owned by 3.4's coordinated migration.

When 3.4 lands:
1. Move `SpeechEmotionPayload` from `splitter/mapping.py` to `schemas/speech_emotion_event.py`. Add the `SpeechEmotionEvent(EventEnvelope)` wrapper in the same file.
2. Move `VocalizationPayload` to `schemas/vocalization_event.py`. Add `VocalizationEvent(EventEnvelope)`.
3. Update Story 3.2's import: `from voice_agent_pipeline.schemas.speech_emotion_event import SpeechEmotionPayload`.
4. Story 3.2's tests follow the import. Test bodies do not change.

The 3-line "moves to schemas/" comment at the top of `splitter/mapping.py` (AC #12) is the migration breadcrumb. Don't drop it.

### Resolution-order rationale

Primary/secondary first → families → unknown. The story's AC #4 makes this explicit: a tag listed both as first-class and in a family is resolved as first-class. **Why**: a YAML author who deliberately promotes a tag from family to first-class (the architecture's primary extensibility story) shouldn't have to remember to also remove it from the family — the resolver does the right thing automatically.

The reverse priority (families before first-class) would mean: "removing a tag from a family is the operative way to promote it" — fragile and surprising.

The "tag in two families" case is a YAML authoring bug. The current resolver's behavior is "whichever family Python iterates first" (Python 3.7+ dict insertion-order semantics). Document this in the dev record + add a TODO for the loader (Story 3.1's loader could in theory check uniqueness across `members:` lists; deferred to keep 3.1 narrow).

### `_FALLBACK_LOG_SEEN` — why module-level

The de-dup contract is **per-process**, not per-call-site. Module-level state is the right scope. Per-call-site (e.g., a `seen` arg on `resolve`) would force every caller to thread the same set, which is needless plumbing. Per-instance state would mean creating a `Resolver` class — overkill for what is otherwise two pure functions.

The trade-off: module-level state is harder to reset between tests. Workaround: have the test fixture clear `_FALLBACK_LOG_SEEN` in `setup`/`teardown` (or use pytest's `monkeypatch.setattr` to swap in a fresh empty set per test). Story 1.4's tests have the same pattern for module-level singleton state — copy that.

### `should_publish` API shape — two methods, not polymorphic

The story spec (and earlier drafts in the epic) reference `should_publish(...)` taking either type. Resist the urge to make the method polymorphic via `Union` types or `isinstance`. Reasons:
1. The two payload types have different semantics (emotion: dedup; vocalization: always). Encoding both behaviors behind one method name obscures the contract.
2. Pyright on `Union` returns mostly-decent inference but loses precision on the `update self._last` branch — the type-narrowing isn't free.
3. Two methods (`should_publish`, `should_publish_vocalization`) read more clearly at the call site (Story 3.7's pipeline reads as "if cache.should_publish(emotion_payload) → publish; for v in vocs: cache.should_publish_vocalization(v) → publish").

### Known unknowns: vocalization fallback families

The architecture treats `vocalization` as a flatter surface than `speech_emotion` — no fallback families in v1. Vocalizations Cartesia doesn't render still publish (with `tts_supported=False`); truly unmapped vocalizations also publish as-is with `tts_supported=False`. If the v1 quality bar later demands vocalization fallback (e.g., `[chuckle]` falling to `[laughter]`), it's a YAML edit + a small change to `resolve_vocalization` — out of scope here.

### What this story does NOT do

- **No segmentation.** Story 3.3 owns the streaming state machine + segmenter.
- **No publisher.** Story 3.5 owns `EventPublisher` + adapters.
- **No `EventEnvelope`.** Story 3.4. The payload classes here are just the **inner** payload — Story 3.4 wraps them in event classes with `schema_version`, `timestamp`, `correlation_id`.
- **No turn-boundary lifecycle.** Story 3.7 wires `LastPublishedCache.reset()` to the activity FSM's `working → listening` transition. This story exposes `reset()` and tests it — does not call it from any pipeline.
- **No SIGHUP atomic swap.** Epic 5.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/splitter/mapping.py` (the splitter package's `__init__.py` already exists).
- `tests/unit/splitter/__init__.py` (if not present) + `tests/unit/splitter/test_mapping.py`.

It does NOT modify:
- `src/voice_agent_pipeline/config/expression_map.py` (Story 3.1's loader is the producer).
- `src/voice_agent_pipeline/schemas/*.py` (Story 3.4's territory).
- Any pipeline assembly file (Story 3.7).

### Testing standards

- **One behavior per test.** Don't parametrize `(input, expected)` over the resolver's three cases — three named tests are clearer.
- **No mocks.** The resolver is pure; the cache is in-memory. Mocking ExpressionMapConfig violates CLAUDE.md rule #7.
- **Real `ExpressionMapConfig` via `_make_mapping()`.** Build the small mapping programmatically (`.model_validate({...})`); no YAML round-trip.
- **`caplog` for log assertions** — Story 1.7's pattern (find it in `tests/unit/stt/test_whisper_cpu.py` if you need a concrete example). Assert on `event` name and key fields, not raw rendered text.
- **Reset module-level `_FALLBACK_LOG_SEEN` between tests** (autouse fixture) — otherwise `test_resolve_fallback_family_logs_debug_first_time` becomes order-dependent.

### What "done" looks like

- `just check` exits 0.
- `from voice_agent_pipeline.splitter.mapping import resolve, resolve_vocalization, LastPublishedCache, SpeechEmotionPayload, VocalizationPayload` works in a Python REPL.
- A small smoke session confirms: `resolve("excited", mapping)` returns the excited payload; `resolve("nevereverseen", mapping)` returns neutral with WARN; `LastPublishedCache().should_publish(...)` dedups consecutive same-emotion calls.
- Story 3.3's segmenter can drive `resolve` and `resolve_vocalization` without further plumbing.

### References

- [Source: build_documents/planning-artifacts/architecture.md#`speech_emotion` Mapping Completeness — The V1 Quality Bar] — full primary + secondary mapping is the launch quality bar.
- [Source: build_documents/planning-artifacts/architecture.md#Type System Conventions] — `dict[str, Any]` only on `expression_data`.
- [Source: build_documents/planning-artifacts/architecture.md#Anti-Patterns (Don't)] — no internal-function mocks; no Enum.
- [Source: build_documents/planning-artifacts/architecture.md#Logging Conventions] — DEBUG/WARN/ERROR level discipline.
- [Source: build_documents/planning-artifacts/prd.md#FR20] — `speech_emotion` carries `raw_tag` + `resolved_fallback`.
- [Source: build_documents/planning-artifacts/prd.md#FR21] — fallback family table.
- [Source: build_documents/planning-artifacts/prd.md#FR24] — last-published cache, turn-scoped.
- [Source: build_documents/planning-artifacts/prd.md#FR38] — DEBUG (first) / WARN (truly unknown) log levels.
- [Source: build_documents/planning-artifacts/epics.md#Story 3.2: Mapping resolver + last-published cache]
- [Source: build_documents/implementation-artifacts/3-1-expression-map-loader.md] — `ExpressionMapConfig` shape; uses `EmotionEntry.expression_data`, `FallbackFamily.members + maps_to`, `UnknownEntry.maps_to`, `VocalizationEntry.tts_supported`.
- [Source: src/voice_agent_pipeline/config/expression_map.py] — the produced `ExpressionMapConfig` class this resolver consumes.
- [Source: src/voice_agent_pipeline/errors.py] — no new exception subclass needed (resolver doesn't raise; the loader caught everything Story 3.1 cared about).
- [Source: tests/unit/stt/test_whisper_cpu.py] — structlog `caplog` capture pattern (Story 1.7).

## Dev Agent Record

### Agent Model Used

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List
