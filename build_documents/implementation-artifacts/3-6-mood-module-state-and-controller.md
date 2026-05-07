# Story 3.6: Mood module — `MoodState` + `MoodController` + cooldown enforcement

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want a `mood/` package owning the in-process current-mood cell and a controller that enforces the ≤4/hr publish cooldown at the publisher boundary,
so that Talker's `set_mood(mood)` tool (Story 4.4) and `activity/greeting.py` (Story 4.5) have a single coherent surface for reading and updating mood — and NFR31 is enforced in one place, not trusted of the LLM.

## Acceptance Criteria

1. **`src/voice_agent_pipeline/mood/state.py` — `MoodState` cell.** Defines:
   - `from voice_agent_pipeline.schemas.mood_event import Mood` — the `Mood` Literal lives in Story 3.4's schema module; `mood/state.py` re-imports rather than re-declaring (avoids type drift).
   - `class MoodState`: a single mutable cell. `__init__(self, initial: Mood = "calm") -> None: self._current: Mood = initial`. **Why not pydantic**: this is an internal mutable cell, not a wire shape; pydantic adds no value. `dataclass` would also work but doesn't naturally express "private setter, public getter."
   - `current` property (getter only) returns `self._current`. **No public setter** — the only way to mutate state is through `MoodController.set()`, which gates on cooldown. This is the architecturally enforced invariant.
   - Re-export `Mood` from `mood/state.py` via `__all__` so callers can write `from voice_agent_pipeline.mood.state import Mood, MoodState` (a single import). `mood/__init__.py` also re-exports both.

2. **`src/voice_agent_pipeline/mood/controller.py` — `MoodController`.** Async controller; constructor takes `state: MoodState`, `publisher: EventPublisher`, `cooldown_publishes_per_hour: int = 4`:
   - Internal state: `self._publish_history: deque[float]` (a deque of `time.monotonic()` timestamps of successful publishes).
   - `async def set(self, mood: Mood, reason: str) -> bool`: full algorithm described in AC #2.
   - `async def publish_initial(self) -> None`: publishes the startup mood event (AC #4).

3. **`set()` cooldown algorithm.** In order:
   - **Step 1**: Pop any timestamps from `_publish_history` older than 60 minutes (`now - ts > 3600.0`).
   - **Step 2**: If `len(_publish_history) >= cooldown_publishes_per_hour`, this call is **rate-limited**: log `event="mood.publish_dropped"` at WARN with fields `attempted_mood`, `current_mood=self._state.current`, `reason="cooldown"`, `provided_reason=reason`, `history_size=len(self._publish_history)`. Return `False`. **Do not** mutate `self._state.current`. **Do not** call `publisher.publish_mood`.
   - **Step 3**: If allowed, build `MoodEvent(payload=MoodPayload(mood=mood, reason=reason))` (the `EventEnvelope` defaults populate `schema_version`, `timestamp`, `correlation_id`, `source` per Story 3.4). Call `await self._publisher.publish_mood(event)`. **Only on successful publish**: append `time.monotonic()` to `_publish_history`, set `self._state._current = mood`, log `event="mood.publish"` at INFO with fields `mood`, `reason`. Return `True`.
   - **Step 4**: If `publisher.publish_mood` raises (`PublisherError` per Story 3.5): **let it propagate** — CLAUDE.md rule #4 forbids catching publisher failures in v1 code paths (it's a `VoiceAgentError`, not an `ExternalServiceError`, but the architecture treats publisher failure as fail-fast too — see architecture.md §"Decision Impact Analysis"). Do not append to history; do not mutate state. The process crashes; systemd restarts.

4. **`publish_initial()`** — fires once at startup, after `EventPublisher.connect()`:
   - Builds `MoodEvent(payload=MoodPayload(mood=self._state.current, reason="startup"))` and publishes.
   - Appends to `_publish_history` (this initial publish DOES count toward the cooldown budget — important for tests asserting "rapid set after startup respects the cooldown").
   - Logs INFO `event="mood.publish_initial"`. Story 3.7's pipeline is the caller.
   - **Idempotent**: a second call within 60 minutes is treated like a normal `set` would be — if budget allows, it publishes; if not, it WARNs and returns. v1 callers shouldn't invoke twice; the safeguard is defensive.

5. **`setup.toml` `[mood]` block.** Optional sub-table; defaults match the architecture spec:
   ```toml
   [mood]
   cooldown_publishes_per_hour = 4
   initial = "calm"
   ```
   Add `class MoodConfig(BaseModel)` to `config/setup.py` with `extra="forbid"`, `cooldown_publishes_per_hour: int = Field(default=4, gt=0, le=20)`, `initial: Mood = "calm"`. Add `mood: MoodConfig = Field(default_factory=MoodConfig)` to `SetupConfig`. **The mood enum itself is NOT config-overridable** — adding a new mood is a code change (architecture.md §"Mood enum lifecycle"). Test that an unknown `initial` value (e.g., `initial = "ecstatic"`) raises `ConfigError` at startup via Literal enforcement.

6. **No mood-module setter outside `MoodController.set()`.** Belt-and-suspenders: even though Python doesn't have true private state, AC #1's `MoodState.current` exposes only a getter property. `_current` (underscore) is the conventional "do not touch" marker. Story 4.5 (greeting) and Talker's prompt assembly (Story 3.7 / 4.x) read `mood_state.current` for tinting; they do not write.

7. **Unit tests in `tests/unit/mood/test_state.py`** — narrow:
   - `test_default_initial_is_calm` — `MoodState()` → `current == "calm"`.
   - `test_initial_argument_overrides` — `MoodState(initial="curious")` → `current == "curious"`.
   - `test_invalid_initial_raises` — `MoodState(initial="ecstatic")` — pydantic Literal isn't enforced inside a plain Python class; this test asserts the constructor accepts only the valid set **only if you implement the typecheck explicitly**. Decision: **don't** add an explicit runtime typecheck — pydantic enforces at the config boundary (AC #5), and `MoodState`'s callers are typed at pyright-strict (`src/`) so a static type error catches mistakes. Drop this test if pure-Python `MoodState` doesn't enforce. **Document the choice** in dev notes.

8. **Unit tests in `tests/unit/mood/test_controller.py`** — the meat of this story:
   - `test_set_publishes_and_updates_state` — happy path; cooldown not reached; `await controller.set("happy", "user laughed")` returns `True`; `state.current == "happy"`; `publisher.published[0] == ("mood", MoodEvent(...))`.
   - `test_set_when_publish_succeeds_updates_state_after_publish` — assert ordering: `publisher.publish_mood` called BEFORE `state._current` mutated. Use a `MagicMock` publisher whose `publish_mood` records the state at call time, then check `state.current` after.
   - `test_set_drops_when_over_rate` — submit 4 successful sets within the same monotonic window; the 5th set returns `False`, leaves `state.current` at the 4th value, logs WARN. Mock `time.monotonic` to return controlled values (no real sleeps).
   - `test_set_window_slides` — submit 4 sets at t=0; advance `time.monotonic` to t=3601; the 5th set succeeds (window slid past the first publish). Assert `_publish_history` length is 4 (4th + 5th from prior + the new one — actually with sliding, length depends on which fall outside the 60-min window).
   - `test_set_returns_false_does_not_call_publisher` — mock publisher; trip the cooldown; on the dropped call, assert `publisher.publish_mood` was NOT called.
   - `test_set_publisher_failure_propagates_no_state_mutation` — make `publisher.publish_mood` raise `PublisherError`; assert `pytest.raises(PublisherError)`; assert `state.current` is unchanged (the pre-set value); assert `_publish_history` length is unchanged.
   - `test_publish_initial_publishes_once_at_startup_value` — `await controller.publish_initial()`; `publisher.published[0]` is a `("mood", MoodEvent)` with `payload.mood == state.current` and `payload.reason == "startup"`.
   - `test_publish_initial_counts_toward_cooldown` — call `publish_initial()`, then 3 successful `set()` calls, then a 4th — should be dropped because the initial counts.
   - `test_log_assertions_use_caplog` — assert `event="mood.publish"` (INFO) on success, `event="mood.publish_dropped"` (WARN) on cooldown drop, `event="mood.publish_initial"` (INFO) on startup. Use Story 1.7's structlog test-capture pattern.

9. **Time mocking — no `freezegun`, no real sleeps.** `monkeypatch.setattr("time.monotonic", lambda: <controlled_value>)`. **Inside the controller, capture `time.monotonic` via `import time` and call `time.monotonic()`** (not `from time import monotonic`) so the monkeypatch hits. Document this choice — the indirection is non-obvious. Tests use a small helper:
   ```python
   def make_clock():
       state = {"now": 0.0}
       def now() -> float: return state["now"]
       def advance(delta: float) -> None: state["now"] += delta
       return now, advance
   ```
   and `monkeypatch.setattr("voice_agent_pipeline.mood.controller.time.monotonic", clock_now)`.

10. **`MoodController` does not own correlation_id binding.** Story 3.7's pipeline binds the per-turn correlation_id at the call site:
    ```python
    await mood_controller.set(mood, reason="user_request", correlation_id=current_turn_id)
    ```
    But — wait — AC #2 doesn't include `correlation_id` in `set()`'s signature. **Decision**: keep `set()`'s signature narrow (`mood, reason`) and let the `MoodEvent` constructor's default `uuid4` win. `mood` events are not user-turn-anchored in the same way `speech_emotion` events are — a user's "set my mood to playful" via the `set_mood` tool fires its own correlation_id by definition. Story 4.4's tool dispatch (which calls `MoodController.set()`) can pass through the turn's correlation_id by extending the signature **then**, when the need is concrete. v1 ships with the default UUID per `MoodEvent`. Document this.

11. **Logging discipline:**
    - INFO `mood.publish` — fields: `mood`, `reason`.
    - INFO `mood.publish_initial` — fields: `mood`.
    - WARN `mood.publish_dropped` — fields: `attempted_mood`, `current_mood`, `reason="cooldown"`, `provided_reason`, `history_size`.
    - **Never** log the full `MoodEvent` payload (it's already structured fields above).

12. **`just check` stays green.** No regression in earlier stories. The `[mood]` config block addition follows Story 2.2's `TalkerConfig` extension pattern in `setup.py`.

## Tasks / Subtasks

- [x] **Task 1: `MoodState` in `mood/state.py`** (AC: #1, #6)
  - [ ] Create `src/voice_agent_pipeline/mood/__init__.py` if not present.
  - [ ] Create `src/voice_agent_pipeline/mood/state.py`. Module docstring per `feedback_code_comments.md` — explain: in-process current-mood cell; v1 lifetime is single-process (cross-restart persistence is v1.5 per `project_v1_scope_fail_fast.md` memory and `architecture.md` §"Deferred to v1.5 / v2"); `current` property is read-only; mutation gated through `MoodController.set()`.
  - [ ] Re-export `Mood` from `mood/state.py` for caller ergonomics.

- [x] **Task 2: `MoodController` in `mood/controller.py`** (AC: #2, #3, #4, #11)
  - [ ] Create `src/voice_agent_pipeline/mood/controller.py`. Module docstring per `feedback_code_comments.md` — explain: cooldown enforcement at publisher boundary (NFR31); state-mutation order (publish-then-mutate); v1 fail-fast on publisher errors.
  - [ ] `from collections import deque`. `from voice_agent_pipeline.publisher.interface import EventPublisher`. `from voice_agent_pipeline.schemas.mood_event import Mood, MoodEvent, MoodPayload`. `from voice_agent_pipeline.mood.state import MoodState`. `import time` (NOT `from time import monotonic` — see AC #9).
  - [ ] Class implementation per AC #2-#4.
  - [ ] **Function docstring on `set()`** — its behavior is non-obvious enough to deserve one (architecture.md §"Documentation": docstrings only when WHY is non-obvious; the publish-then-mutate ordering + the cooldown invariant qualify).

- [x] **Task 3: `MoodConfig` in `config/setup.py`** (AC: #5)
  - [ ] Mirror Story 2.3's `TtsConfig` addition: define `MoodConfig` with `extra="forbid"`, the two fields, and a class docstring per the existing pattern.
  - [ ] `import` `Mood` from `schemas.mood_event` for the `initial: Mood` typing.
  - [ ] Add `mood: MoodConfig = Field(default_factory=MoodConfig)` to `SetupConfig`.
  - [ ] Append the `[mood]` block to `setup.toml` with operator comments.
  - [ ] Update `tests/unit/config/test_setup.py:_VALID_TOML` to include `[mood]` only if the test needs to override defaults; otherwise rely on `default_factory`.

- [x] **Task 4: Unit tests for `MoodState`** (AC: #7)
  - [ ] `tests/unit/mood/__init__.py` if not present.
  - [ ] `tests/unit/mood/test_state.py` — narrow set of state-only tests.

- [x] **Task 5: Unit tests for `MoodController`** (AC: #8, #9)
  - [ ] `tests/unit/mood/test_controller.py` — full cooldown algorithm coverage.
  - [ ] **Use `LogEventPublisher`** (Story 3.5) as the publisher dependency — real adapter, no mock. This validates the controller's call shape against the actual Protocol. For the "publisher fails" test, use `unittest.mock.MagicMock(spec=EventPublisher)` with `publish_mood` set to raise `PublisherError`.
  - [ ] **Time mocking pattern**: `monkeypatch.setattr("voice_agent_pipeline.mood.controller.time.monotonic", clock_now)`. Document in test-file docstring why the patch path is `mood.controller.time.monotonic` (because `controller.py` does `import time; time.monotonic()`).

- [x] **Task 6: Pass `just check`; verify no regressions** (AC: #12)
  - [ ] Run incrementally: `uv run pytest tests/unit/mood/ -v`, then `tests/unit/config/test_setup.py`, then full `just check`.

- [x] **Task 7: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [x] Single commit titled `Story 3.6: mood module — MoodState + MoodController + cooldown`.
  - [x] `git push` immediately.

## Dev Notes

### Architectural intent

Story 3.6 is the **mood layer** — a small module owning a single `Mood` value + the publishing rules that govern when it can change. Two pieces:
1. `MoodState` — the read surface. Story 4.5's greeting tinting reads `state.current`; Story 3.7 / 4.x's Talker prompt assembly reads it.
2. `MoodController` — the write surface. Story 4.4's tool dispatch (Talker calls `set_mood(...)`) routes through this controller. The controller enforces NFR31 (≤4 publishes/hour) and the publish-before-mutate invariant.

The architectural promise: the on-the-wire `mood` topic is the **source of truth** for "what mood is OLAF in?" — in-process state lags wire by one publish call, so a subscriber that reconnects mid-session sees the latched current mood rather than an in-process state that publishes haven't caught up to. This is why `_state._current` mutates **after** the publish (AC #3 step 3).

### Why publish-before-mutate (and not the reverse)

Two failure modes if state mutates first:
1. **Publish fails after state mutation**: in-process now thinks mood is `playful`, but the wire never saw the change. Subscribers (embodiment) stay on the prior mood. The next greeting (which reads in-process state) tints with `playful` while the on-wire `mood` topic still reports `calm`. Inconsistent.
2. **Test flakiness around the `True/False` return**: if `set()` returns `True` only after publish, the contract is "state agrees with wire." If it mutates before publish + returns `True` regardless, callers can't tell what actually published.

Publish-then-mutate keeps the wire as source of truth. Architecture.md §"Decision Impact Analysis" makes this explicit.

### Cooldown — sliding 60-min vs hourly bucket

The architecture says "≤4 publishes per hour, sustained" (NFR31). Two implementations:
- **A**: sliding window — at any instant, at most 4 publishes have occurred in the prior 60 minutes.
- **B**: hourly bucket — reset count at the top of each hour.

A is preferred:
- B has a "burst at the boundary" pathology: 4 publishes at 12:59 + 4 more at 13:00 = 8 publishes in 1 minute, satisfying B but violating the *intent* of NFR31.
- A enforces the intent uniformly.
- Implementation cost is the same (deque vs counter+timestamp) — pick A.

### `time.monotonic` vs `time.time` vs `datetime.now`

`time.monotonic()` — never goes backward, immune to system clock adjustments. Right choice for "elapsed" measurement.

`time.time()` — wall clock; can jump if NTP adjusts. Wrong here.

`datetime.now()` — useful for human-readable timestamps (e.g., the `EventEnvelope.timestamp` field) but overkill for elapsed-time math.

Use `time.monotonic()` in the controller's history; let `EventEnvelope.timestamp` use its `datetime.now(UTC)` default for the wire form.

### Why `import time` not `from time import monotonic`

Pytest monkeypatching swaps the attribute on a module object. `from time import monotonic` binds `monotonic` to a local name in `controller.py` that monkeypatching can't intercept (without explicitly patching `voice_agent_pipeline.mood.controller.monotonic`). `import time; time.monotonic()` keeps the indirection, so `monkeypatch.setattr("voice_agent_pipeline.mood.controller.time.monotonic", ...)` works.

This is a small but real footgun. Tests that use `from time import monotonic` and then can't see the patch are a classic pytest mistake. Document it in the controller's module docstring.

### Why `MoodState` isn't pydantic

Three reasons:
1. **It's a single mutable cell**, not a frozen wire shape. pydantic's `frozen=True` is the wrong default; without it, you lose pydantic's main value (immutability + validation at construction).
2. **The "private setter" intent is structural**, not enforceable in pydantic. A `model_config = ConfigDict(frozen=True)` would block all mutation, including `MoodController.set()`'s legitimate path.
3. **No wire-shape concerns** — `MoodState` never serializes; only `MoodEvent` does (Story 3.4).

A plain class with a property + underscore-prefixed field is the right shape.

### When `set()` returns `False` — what the caller does

Story 4.4's tool dispatch (Talker's `set_mood(mood)` invocation) calls `await mood_controller.set(mood, reason)` and:
- Logs the result (INFO `tool.set_mood.dispatched` with `success=<bool>`).
- Does **not** retry on `False`. Cooldown is the LLM's problem to solve through its prompt (architecture.md §"Mood publish-rate enforcement vs LLM cooperation").
- Returns control to the user-turn flow regardless.

This story doesn't implement the tool dispatch — Story 4.4 does. But the contract is: `set()` returns `bool` so the caller can log + move on, **not** to drive a retry loop.

### What this story does NOT do

- **No tool dispatch.** Story 4.4 builds `SetMoodTool` that calls `MoodController.set()`.
- **No greeting integration.** Story 4.5 reads `MoodState.current`.
- **No pipeline wiring.** Story 3.7 wires `MoodController` into `pipeline.py` and calls `publish_initial()` after publisher.connect().
- **No cross-restart persistence.** v1 is in-process; v1.5 backlog item `v1.5-2-cross-restart-mood-persistence`.
- **No mood inference from speech.** Out of scope; Talker explicitly fires `set_mood(...)` based on conversational cues per the architecture's tool-using design.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/mood/__init__.py`
- `src/voice_agent_pipeline/mood/state.py`
- `src/voice_agent_pipeline/mood/controller.py`
- `tests/unit/mood/__init__.py`, `test_state.py`, `test_controller.py`

It modifies:
- `src/voice_agent_pipeline/config/setup.py` (`MoodConfig`, `SetupConfig.mood`)
- `setup.toml` (`[mood]` block)
- `tests/unit/config/test_setup.py` (test the new block)

It does NOT modify:
- `src/voice_agent_pipeline/pipeline.py` (Story 3.7's territory).
- `src/voice_agent_pipeline/turn/talker.py` or `turn/tools.py` (Story 4.4).

### Testing standards

- **One behavior per test.**
- **Real `LogEventPublisher` for happy-path tests.** Validates the call shape against the actual Protocol. Mock only when injecting failures (`PublisherError`).
- **Time mocking via `monkeypatch.setattr("voice_agent_pipeline.mood.controller.time.monotonic", ...)`** — see AC #9 for the pattern.
- **`caplog` for log assertions** — Story 1.7's structlog pattern.
- **Async tests** use `@pytest.mark.asyncio`.

### What "done" looks like

- `just check` exits 0.
- A REPL session can drive the controller:
  ```python
  from voice_agent_pipeline.mood.state import MoodState
  from voice_agent_pipeline.mood.controller import MoodController
  from voice_agent_pipeline.publisher import LogEventPublisher
  state = MoodState()
  pub = LogEventPublisher()
  await pub.connect()
  ctrl = MoodController(state, pub, cooldown_publishes_per_hour=4)
  await ctrl.publish_initial()
  await ctrl.set("happy", "user told a joke")
  assert state.current == "happy"
  ```
- Story 3.7 + 4.4 + 4.5 can `from voice_agent_pipeline.mood.state import MoodState; from voice_agent_pipeline.mood.controller import MoodController` and integrate.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Activity FSM + Mood Control + Tool Registry (Batch 6 — added 2026-05-06)] — `MoodController` design + cooldown at publisher boundary.
- [Source: build_documents/planning-artifacts/architecture.md#Decision Impact Analysis] §12 — publish-before-mutate invariant.
- [Source: build_documents/planning-artifacts/prd.md#NFR31] — ≤4 mood publishes per hour.
- [Source: build_documents/planning-artifacts/prd.md#FR48-FR50] — mood module functional surface.
- [Source: build_documents/planning-artifacts/epics.md#Story 3.6: Mood module — `MoodState` + `MoodController` + cooldown enforcement]
- [Source: build_documents/implementation-artifacts/3-4-event-schema-rebuild.md] — `Mood` Literal lives in `schemas/mood_event.py`; `MoodEvent` + `MoodPayload`.
- [Source: build_documents/implementation-artifacts/3-5-event-publisher-ros2-and-log-adapter.md] — `EventPublisher` Protocol; `LogEventPublisher` for tests.
- [Source: src/voice_agent_pipeline/config/setup.py] — `TtsConfig` extension pattern (Story 2.3) — mirror for `MoodConfig`.
- [Source: src/voice_agent_pipeline/errors.py] — `PublisherError` (Story 1.4) — propagated by the controller, not caught.
- [Memory: project_v1_scope_fail_fast.md] — v1 in-process; cross-restart persistence is v1.5.
- [Memory: project_bot_persona.md] — bot persona is Ooppi; mood values may evolve over time but the Literal is code-level.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- **All 14 tests passed first try.** The story spec's prescriptive
  ordering (publish-before-mutate, sliding-window math, log levels)
  removed all ambiguity.
- **`pyright` flagged ``self._state._current = mood``** as
  ``reportPrivateUsage``. This is architecturally intentional — the
  controller is the documented privileged write path (see
  ``mood/state.py`` module docstring). Suppression with paired
  ``# pyright: ignore[reportPrivateUsage]`` + inline rationale per
  architecture.md §"Anti-Patterns" carve-out.
- **`Generator[_Clock, None, None]`** for the clock fixture — pytest
  fixtures with ``yield`` need the generator return type for ruff to
  be happy. Standard pattern.
- **Time mocking via ``monkeypatch.setattr("voice_agent_pipeline.
  mood.controller.time.monotonic", clock.now)``**: works because the
  controller calls ``time.monotonic()`` through the imported module
  reference, not via a ``from time import monotonic`` local binding.
  Module docstring documents the deliberate choice.
- **Boundary test (``test_set_within_window_at_boundary_still_blocks``)**
  pinned the inclusive-vs-exclusive contract: at t=3599 (3599s after
  the first publish), the first publish IS still inside the 60-min
  window (it's 3599 < 3600 seconds ago). Sliding-window math is
  ``now - ts > 3600.0`` for eviction.
- **`just check`: 308 unit tests pass** (+14 from this story); ruff
  + pyright clean. No regression in earlier stories.

### Completion Notes List

- All 12 ACs satisfied:
  - AC #1: ``MoodState`` plain class (not pydantic) with default
    ``initial="calm"``, read-only ``current`` property, underscore-
    prefixed ``_current`` field as the privileged write path.
    ``Mood`` Literal re-exported from ``mood/state.py`` for caller
    ergonomics.
  - AC #2: ``MoodController`` async ``set()`` with the three-step
    algorithm: sweep history → check budget → publish-then-mutate.
    Returns ``bool``.
  - AC #3: Cooldown algorithm exact per spec — sliding 60-min window
    via ``time.monotonic()`` deque eviction; rate-limited calls
    return ``False`` and log WARN.
  - AC #4: ``publish_initial`` constructs MoodEvent with
    ``reason="startup"``, counts toward the cooldown budget; logs
    distinct ``mood.publish_initial`` INFO event.
  - AC #5: ``MoodConfig`` with ``cooldown_publishes_per_hour:
    int = Field(default=4, ge=1, le=20)`` and ``initial: Mood
    = "calm"``. Added to ``[mood]`` block in setup.toml. The mood
    enum itself is NOT config-overridable (architecture rule).
  - AC #6: ``current`` property has no setter; the privileged
    ``_current`` write path is the controller's only legitimate
    mutation. Test pins via ``with pytest.raises(AttributeError)``.
  - AC #7: 4 ``MoodState`` tests covering default, override,
    read-only enforcement, and the privileged-path access contract.
  - AC #8: 10 ``MoodController`` tests including all cooldown
    paths (happy, drops, slides, boundary), publisher-failure-no-
    state-mutation, and ``publish_initial`` semantics.
  - AC #9: Time mocking via ``monkeypatch.setattr("voice_agent_pipeline.
    mood.controller.time.monotonic", clock.now)`` — module docstring
    documents the import-style requirement.
  - AC #10: ``set()`` signature stays narrow (``mood, reason``) per
    the architectural decision deferral. Story 4.4's tool dispatch
    can extend the signature when correlation_id binding becomes
    concrete.
  - AC #11: Logging discipline — INFO ``mood.publish`` /
    ``mood.publish_initial`` (mood + reason fields only); WARN
    ``mood.publish_dropped`` (attempted_mood, current_mood, reason,
    provided_reason, history_size). No full-payload logging.
  - AC #12: ``just check`` exits 0; 308 tests pass; no regression
    in Stories 1.x / 2.x / 3.1–3.5.
- **Comments.** Module + class + function docstrings per
  ``feedback_code_comments.md``. The privileged-write pyright
  suppression carries inline rationale.
- **No deviations.** All ACs implemented as written.

### File List

**New files:**
- ``src/voice_agent_pipeline/mood/__init__.py`` — package re-exports
  (Mood, MoodState, MoodController).
- ``src/voice_agent_pipeline/mood/state.py`` — ``MoodState`` cell;
  re-exports ``Mood`` from schemas.
- ``src/voice_agent_pipeline/mood/controller.py`` —
  ``MoodController`` with ``set()`` + ``publish_initial()`` +
  ``_sweep_old()`` helper.
- ``tests/unit/mood/__init__.py``.
- ``tests/unit/mood/test_state.py`` — 4 tests.
- ``tests/unit/mood/test_controller.py`` — 10 tests including the
  ``_Clock`` shim fixture for monotonic-time mocking.

**Modified files:**
- ``src/voice_agent_pipeline/config/setup.py`` — added
  ``MoodConfig``; ``SetupConfig.mood`` field with default factory.
  ``Mood`` imported from schemas for the ``initial: Mood`` typing.
- ``setup.toml`` — added ``[mood]`` block with operator comment.
- ``build_documents/implementation-artifacts/3-6-mood-module-state-
  and-controller.md`` — this file: tasks ticked, dev record populated,
  status → review.
- ``build_documents/implementation-artifacts/sprint-status.yaml`` —
  ``3-6-mood-module-state-and-controller: ready-for-dev →
  in-progress → review``.

## Change Log

| Date | Change |
|---|---|
| 2026-05-07 | Story 3.6 implemented. ``MoodState`` (single mutable cell, read-only ``current`` property) + ``MoodController`` (cooldown-enforcing publish path with sliding 60-min window, ≤4 publishes/hour by default per NFR31). Publish-before-mutate invariant: ``set()`` calls ``publisher.publish_mood`` first; mutates ``MoodState._current`` only on successful publish. Rate-limited calls drop with WARN; publisher errors propagate uncaught (CLAUDE.md rule #4). ``publish_initial()`` fires the startup latched mood event and counts toward the cooldown budget. ``MoodConfig`` exposes ``cooldown_publishes_per_hour`` (1-20) and ``initial`` knobs in setup.toml; the Mood enum itself is code-level. ``time.monotonic`` mocked in tests via module-reference patching (controller imports ``time`` rather than ``from time import monotonic`` to make the patch reach). 14 new tests; ``just check``: 308 unit tests pass; no regression in earlier stories. Status → review. |
