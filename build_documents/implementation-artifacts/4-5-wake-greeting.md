# Story 4.5: Wake greeting — static random per-mood pick + clarification list simplification + J1 integration test

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want a static mood-tinted wake greeting fired automatically on every `sleeping → waking` FSM transition — picked at random from per-mood bucket lists in `setup.toml`, no LLM call — and the same static-random treatment applied to the STT clarification prompt,
so that wake-up feels like a friend acknowledging me ("hey", "yeah?", "what's up?") instead of a scripted "Hello, I am OLAF" — Journey 1 (wake-with-greeting) becomes demonstrable end-to-end — and both flows shed the LLM-as-question-answerer failure mode that Story 3.7's `cdf3618` had to work around for clarification.

## Acceptance Criteria

1. **Static-random design replaces the LLM-driven greeting.** Earlier draft used `Talker.greet(mood)` with `asyncio.wait_for(timeout=0.8)` + word-count gate + fallback list. **2026-05-07 revision**: greeting is a flat per-mood lookup + `random.choice`. Trade-off: lose LLM novelty per call; gain zero latency, zero cost, zero hallucination, zero "LLM treated my prompt as a question" risk.

2. **`src/voice_agent_pipeline/activity/greeting.py` — pure synchronous function** (no LLM, no async, no timeout):
   ```python
   def trigger_greeting(mood: Mood, greetings_by_mood: dict[Mood, list[str]]) -> str:
       """Pick a random greeting for the given mood. Sub-microsecond.

       Falls back to the ``calm`` bucket if the mood's bucket is empty
       (operator misconfiguration). Falls back to the literal ``"hey"``
       if even the ``calm`` bucket is empty (last-resort safety).
       """
       bucket = greetings_by_mood.get(mood) or greetings_by_mood.get("calm") or ["hey"]
       text = random.choice(bucket)
       log.info("greeting.picked", mood=mood, text=text)
       return text
   ```
   - Module docstring per `feedback_code_comments.md`: explain the static-random rationale (this story's revision note), the fallback chain (mood-bucket → calm-bucket → literal "hey"), and why this is a free function (no class state — pure mapping + random pick).
   - Imports: `import random`, `import structlog`, `from voice_agent_pipeline.schemas.mood_event import Mood`.
   - Module-level: `log = structlog.get_logger(__name__)`.
   - **Sync, not async.** `random.choice` is microseconds; no `await` needed. Calling code (the FSM hook) is `async def` and just calls `text = trigger_greeting(mood, greetings)` directly — no `await`.

3. **`setup.toml` `[greeting]` block — per-mood bucket lists.** Replaces the timeout/min_words/max_words/fallback_list block from the earlier draft:
   ```toml
   # Story 4.5: wake greeting (static random pick by mood). The wake greeting
   # fires automatically on every sleeping→waking FSM transition; we pick
   # one entry uniformly at random from the bucket matching the current
   # mood. Lists below are starter sets — operators are encouraged to expand
   # to 30-40 entries per mood for low repetition over long sessions. Each
   # entry should be 2-8 words, no SSML, no question-mark-ending instructions
   # (the entry IS the spoken text, not an instruction to an LLM).
   [greeting.greetings_by_mood]
   calm = [
       "hey", "yeah?", "yes?", "what's up?", "I'm here", "hi",
       "yeah, hi", "what's up", "hello", "hi there",
   ]
   happy = [
       "hey there!", "hi!", "what's up!", "good to see you",
       "hey hey", "yes!", "hiya", "hi there!",
   ]
   playful = [
       "yo", "what's up", "heya", "sup", "hey hey", "yo what's up",
       "yes? yes?", "hm hm hm hi", "hello hello",
   ]
   curious = [
       "yeah?", "tell me", "what's up?", "what's on your mind",
       "hi, what is it", "yes?", "yeah, hi", "go on",
   ]
   thoughtful = [
       "hmm yes?", "yeah", "mm", "yes", "go on", "I'm listening",
       "yeah, hi", "tell me",
   ]
   sleepy = [
       "mmh", "yeah?", "hi", "hmm what?", "mm yes?", "yeah hi",
       "I'm here", "mmh yeah",
   ]
   grumpy = [
       "yeah", "what?", "uh", "yeah hi", "what is it",
       "yes", "hm", "go on",
   ]
   excited = [
       "hey!", "yes!", "what's up!!", "heyy", "hi hi!", "yo!",
       "yeah hi!", "hello!", "yes yes!",
   ]
   ```
   - Each starter list is 8-10 entries; **operator should expand to 30-40 over time** (a comment in the TOML calls this out).
   - In `src/voice_agent_pipeline/config/setup.py`:
     - Add `class GreetingConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")`:
       - `greetings_by_mood: dict[Mood, list[str]] = Field(default_factory=lambda: {<inline defaults so an empty TOML block still works>})`. **Recommend** putting the default dict in a module-level constant `_DEFAULT_GREETINGS_BY_MOOD` for readability; the `default_factory=lambda` references it.
     - **`model_validator(mode="after")`**: assert every `Mood` Literal value has at least one entry in the dict (via `list(get_args(Mood))`). If a mood is missing, raise `ValueError(f"greetings_by_mood missing entries for mood: {missing}")`. Ensures every mood has at least one greeting at startup; no surprise empty-bucket fallback in production.
     - `greeting: GreetingConfig = Field(default_factory=GreetingConfig)` on `SetupConfig`.
   - Update setup.toml's "Sections populated by subsequent stories" comment list — remove `[greeting]` if listed.

4. **`[stt] clarification_prompts: list[str]`** (replaces the singular `clarification_prompt`):
   ```toml
   # Story 2.4 / 4.5: when STT confidence drops below the threshold, the
   # TurnRouter substitutes a clarification phrase for the user's (noisy)
   # text and pipeline.py:TurnDispatchProcessor's clarification short-
   # circuit emits it directly as a TalkerResponseFrame (Story 3.7's
   # cdf3618 fix bypasses the Talker — clarifying text never goes through
   # the LLM). Story 4.5 (2026-05-07) extends to a list — picked at
   # random per turn for variety. Operators expand to 30-40 entries.
   clarification_prompts = [
       "Sorry, what?",
       "Could you repeat that?",
       "I didn't catch that.",
       "Say again?",
       "What was that?",
       "Hmm, one more time?",
       "Pardon?",
       "Could you say that again?",
       "I missed that.",
       "What did you say?",
       "Sorry, didn't hear you.",
       "Could you repeat?",
       "What's that?",
       "Once more?",
       "Sorry, missed that.",
       "Come again?",
       "Try that again?",
       "Sorry, what was that?",
       "Hm, didn't get that.",
       "One more time?",
   ]
   ```
   - In `src/voice_agent_pipeline/config/setup.py:SttConfig`:
     - **Replace** `clarification_prompt: str = "Briefly ask the user to repeat..."` with `clarification_prompts: list[str] = Field(default_factory=lambda: [<inline 20-entry list>])`.
     - `model_validator(mode="after")` asserts `len(clarification_prompts) >= 1` (at least one entry; otherwise random.choice fails).
   - In `src/voice_agent_pipeline/turn/router.py:TurnRouter.__init__`:
     - **Replace** the single `self._clarification_prompt = stt_config.clarification_prompt` with `self._clarification_prompts = stt_config.clarification_prompts`.
   - In `TurnRouter.route`:
     - **Replace** `text=self._clarification_prompt` with `text=random.choice(self._clarification_prompts)` in the low-confidence branch. The `RouteDecision` carries the picked phrase.
     - Add `import random` at the top of `router.py`.
   - **The dispatcher's clarification short-circuit (Story 3.7 `cdf3618`) stays exactly the same** — it already emits `decision.text` verbatim as a `TalkerResponseFrame`, no LLM round-trip. The only change is that `decision.text` is now a randomly-picked phrase per turn instead of the same string every time.

5. **`_GreetingInjectorProcessor(FrameProcessor)`** — new Pipecat processor in `pipeline.py` that injects the greeting `TalkerResponseFrame` into the splitter pipeline. (Same as the earlier draft — the injector is needed regardless of LLM vs static-random because the FSM hook needs a way to push a frame from outside the normal turn-dispatch path.)
   - Class shape:
     ```python
     class _GreetingInjectorProcessor(FrameProcessor):
         """Injects wake-greeting TalkerResponseFrames into the pipeline (Story 4.5)."""
         def __init__(self) -> None:
             super().__init__()  # pyright: ignore[reportUnknownMemberType]
             self._direction: FrameDirection = FrameDirection.DOWNSTREAM

         async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
             await super().process_frame(frame, direction)
             self._direction = direction
             await self.push_frame(frame, direction)

         async def inject_greeting(self, text: str) -> None:
             """Push a TalkerResponseFrame(text) downstream."""
             await self.push_frame(TalkerResponseFrame(text=text), self._direction)
     ```
   - **Placement in the pipeline list**: after `TurnDispatchProcessor`, before `SegmenterProcessor` (same as the earlier draft).

6. **`ActivityFSM` constructor extension** — same as the earlier draft (this part doesn't change with the LLM removal):
   - Story 4.3's `ActivityFSM.__init__` gains:
     ```python
     on_sleeping_to_waking: Callable[[], Awaitable[None]] | None = None
     ```
   - Inside `on_wake_detected()`: after state mutation, fire the callback as a background task via `asyncio.create_task(...)`. Done-callback `_log_greeting_done(task)` captures any exception via `log.exception("greeting.background_error")`.
   - **Architecture intent unchanged**: `ActivityEvent(state="waking")` publishes immediately on transition, NOT awaiting the greeting (decouples FSM publishing from greeting latency). With static-random, the "latency" is sub-millisecond — but the architectural decoupling pattern stays for cleanliness.
   - **Update Story 4.3's unit tests** in `tests/unit/activity/test_machine.py` — add 3 new tests per the earlier draft's AC #10 (the constructor extension is non-breaking).

7. **Pipeline-assembly wiring** (`pipeline.py:run_pipeline`):
   - Construct `greeting_injector = _GreetingInjectorProcessor()`.
   - Build the greeting closure (no Talker dependency, no timeout, no async LLM call):
     ```python
     async def _on_sleeping_to_waking() -> None:
         """Wake greeting orchestration — runs as background task on FSM transition."""
         mood = mood_controller.state.current  # sync read; current cell
         text = trigger_greeting(mood, config.greeting.greetings_by_mood)
         await greeting_injector.inject_greeting(text)
     ```
   - Pass into the FSM constructor: `activity_fsm = ActivityFSM(publisher=event_publisher, on_sleeping_to_waking=_on_sleeping_to_waking)`.
   - Insert `greeting_injector` in the pipeline list per the earlier draft's diagram (after `TurnDispatchProcessor`, before `SegmenterProcessor`).
   - Update `pipeline.py`'s module docstring with a Story 4.5 entry.

8. **Logging discipline** (NFR25, FR39):
   - INFO `greeting.picked` (in `trigger_greeting`) — fields: `mood`, `text`. Logging `text` at INFO follows the precedent set by Story 2.5's `prompt`/`response` logging (architecture deviation from FR42 for v1 personal use). Greeting text is short (2-8 words) and operator-authored — no privacy concern.
   - INFO `greeting.background_error` (in the FSM's done-callback) — captured if the orchestrator closure (the injector's `push_frame` somehow fails). Should be rare; log + continue.
   - INFO `clarification.picked` (NEW, in `TurnRouter.route` low-confidence branch) — fields: `text`. Mirrors `greeting.picked`.
   - **Never log** the audio bytes / transcript / LLM responses. Standing privacy invariant.

9. **Unit tests in `tests/unit/activity/test_greeting.py`** (NEW). Cover the static-random function:
   - **Test directory**: `tests/unit/activity/__init__.py` already exists (Story 4.3); add `test_greeting.py`.
   - `test_returns_random_choice_from_mood_bucket` — `greetings = {"calm": ["a", "b", "c"]}`; call `trigger_greeting("calm", greetings)` 100 times; assert all returned values are in `{"a", "b", "c"}` AND that all 3 entries appear at least once (validates randomness — use `random.seed(0)` for determinism if pyright complains about the loose assertion).
   - `test_falls_back_to_calm_bucket_when_mood_missing` — `greetings = {"calm": ["calm-greeting"]}`; call `trigger_greeting("playful", greetings)`; returns `"calm-greeting"`.
   - `test_falls_back_to_hey_when_calm_bucket_also_missing` — `greetings = {}`; call `trigger_greeting("calm", greetings)`; returns `"hey"`.
   - `test_falls_back_to_hey_when_mood_bucket_is_empty` — `greetings = {"calm": []}`; returns `"hey"` (empty list is treated as missing — the `or` short-circuits).
   - `test_each_mood_value_handled` — for each `Mood` Literal value (calm, happy, playful, curious, thoughtful, sleepy, grumpy, excited), call with a populated bucket; assert the returned value is in the bucket.
   - `test_logs_event_greeting_picked` — assert one INFO log `event="greeting.picked"` with `mood`, `text` fields per call.

10. **Unit tests for `GreetingConfig`** in `tests/unit/config/test_setup.py`:
    - `test_greeting_defaults_have_all_moods` — `GreetingConfig()` parses; every `Mood` Literal value is a key in `greetings_by_mood`; every bucket has ≥1 entry.
    - `test_greeting_explicit_override` — TOML with explicit `[greeting.greetings_by_mood]` block parses correctly.
    - `test_greeting_missing_mood_raises` — TOML with `greetings_by_mood = {"calm": ["hi"]}` (missing other moods) → `ConfigError` from the `model_validator` (must include all `Mood` values).
    - `test_greeting_extra_mood_key_raises` — TOML with `greetings_by_mood = {"calm": ["hi"], "ecstatic": ["yay"]}` (`ecstatic` not in `Mood` Literal) → `ConfigError` (pydantic rejects unknown keys for `dict[Mood, ...]` typed field).

11. **Updated unit tests for `SttConfig.clarification_prompts`** in `tests/unit/config/test_setup.py`:
    - `test_stt_clarification_prompts_default` — defaults to a non-empty list; assert `len >= 1`.
    - `test_stt_clarification_prompts_explicit_override` — TOML override works.
    - `test_stt_clarification_prompts_empty_raises` — `clarification_prompts = []` → `ConfigError` from the `model_validator`.
    - `test_stt_clarification_prompt_singular_raises` — TOML still using `clarification_prompt = "..."` (singular) → `ConfigError` (extra=forbid catches it).

12. **Updated unit tests for `TurnRouter`** in `tests/unit/turn/test_router.py`:
    - **Update existing low-confidence tests**: `decision.text` is now any value from the configured `clarification_prompts` list; assert `decision.text in self._clarification_prompts` rather than asserting equality to a specific string. Use `random.seed(0)` at test setup for determinism if needed.
    - `test_low_confidence_text_is_from_clarification_list` — confidence below threshold; assert `decision.text` is one of the configured `clarification_prompts`.
    - `test_low_confidence_logs_clarification_picked` — assert INFO `clarification.picked` log with `text` field.
    - **All Story 2.4 existing tests stay green** with minor adjustments (the singular `clarification_prompt` field becomes a list).

13. **Updated unit tests for `ActivityFSM` greeting hook** (mirrors the earlier draft's AC #10) in `tests/unit/activity/test_machine.py`:
    - `test_on_wake_detected_fires_greeting_callback_as_background_task` — pass `AsyncMock` as `on_sleeping_to_waking`; assert called once.
    - `test_on_wake_detected_publish_does_not_await_greeting_callback` — slow callback; assert `publish_activity` returns within 50ms.
    - `test_no_greeting_callback_when_unset` — default `None`; runs cleanly.

14. **Integration test `tests/integration/test_wake_greeting.py`** (J1 demonstration; simpler than the LLM-driven draft):
    - **Mock**: Cartesia (synthetic audio chunks at 50ms intervals); STT (not exercised in wake-only flow); Wake-word (single trigger).
    - **Real**: `ActivityFSM`, `LogEventPublisher`, `MoodController`, `_GreetingInjectorProcessor`, `SegmenterProcessor`, full splitter chain. **No `Talker` mock needed** — greeting bypasses the LLM entirely.
    - **Drive**: `start()` then simulate `on_wake_detected()`.
    - **Assert** (per epics.md AC #11):
      - `ActivityEvent(state="waking", from_state="sleeping")` publishes within 50ms.
      - A `TalkerResponseFrame` containing the greeting text is observed at the splitter input within 100ms (the static-random budget is sub-millisecond; the `inject_greeting` push frame is sub-ms; this is now generous).
      - The greeting text is one of the entries in `config.greeting.greetings_by_mood["calm"]` (or whatever the test's initial mood is).
      - Audio frames flow to the speaker (or test sink) — the same path as conversational replies.
      - `ActivityEvent(state="listening", from_state="speaking")` publishes after the last greeting audio frame.
    - **Both moods exercised**: drive once with mood `"calm"`, once with mood `"playful"`. Assert each picked text is in the corresponding bucket.
    - **Privacy assertion** mirroring earlier integration tests.

15. **NFR30 baseline measurement** (per epics.md AC #12) — still measured; now trivially satisfied:
    - Drive 30 simulated wakes; measure `wake_to_first_audio_ms`. The static-random greeting eliminates the 800ms LLM budget — the timing is dominated by Cartesia mock TTFB (~50ms) + speaker buffer (~50ms). **Assert p95 ≤ 800ms** is now generous; expect p95 well under 200ms in the synthetic test.
    - **Record p50 / p95 / max** in test output AND commit message + dev record. NFR30 baseline is established; Story 5.4 validates the production budget against real Cartesia + real audio output.
    - **Both moods × 15 wakes each** (so 30 total) — validates random-pick variety across the simulated session.

16. **`just check` stays green.** Updates required:
    - `tests/unit/activity/test_machine.py` (Story 4.3) — 3 new tests per AC #13 (constructor extension non-breaking).
    - `tests/unit/config/test_setup.py` — 4 `GreetingConfig` tests + 4 `clarification_prompts` tests.
    - `tests/unit/turn/test_router.py` (Story 2.4) — update existing low-confidence tests + 2 new tests per AC #12.
    - `tests/integration/test_simple_turn.py` (Story 2.5) / `test_embodiment_alignment.py` (3.7) / `test_activity_lifecycle.py` (4.3) / `test_intent_sleep.py` (4.4) — pass `ActivityFSM(...)` constructed without the greeting callback (default `None`) so existing assertions still hold.
    - `tests/unit/test_pipeline.py` — Story 3.7's clarification short-circuit (cdf3618) test asserting `decision.text` was emitted as `TalkerResponseFrame` still passes; the picked text is now random but the assertion `frame.text in self._clarification_prompts` (with the list available in the test fixture) holds.

17. **No transcripts / API keys / raw audio in any log** (NFR25, FR39 — standing). Greeting text and clarification text ARE logged at INFO — operator-authored static strings, no privacy concern. Standing invariant for transcripts / response_chunks / audio bytes still applies.

## Tasks / Subtasks

- [ ] **Task 1: `GreetingConfig` + `[greeting]` setup.toml block (per-mood buckets)** (AC: #3, #10)
  - [ ] In `src/voice_agent_pipeline/config/setup.py`:
    - Define `_DEFAULT_GREETINGS_BY_MOOD: dict[Mood, list[str]]` at module level (the 8-mood × ~10-entry starter set from AC #3). Keep `from typing import get_args` for the model_validator.
    - Add `class GreetingConfig(BaseModel)` with `extra="forbid"`, `greetings_by_mood: dict[Mood, list[str]] = Field(default_factory=lambda: dict(_DEFAULT_GREETINGS_BY_MOOD))` (use `dict(...)` to avoid sharing the mutable default).
    - `model_validator(mode="after")`: collect missing moods; raise `ValueError` if any. Class docstring per `feedback_code_comments.md`.
    - Add `greeting: GreetingConfig = Field(default_factory=GreetingConfig)` to `SetupConfig`.
  - [ ] In `setup.toml`:
    - Add the `[greeting.greetings_by_mood]` block per AC #3.
    - Update the trailing "subsequent stories" comment list — remove `[greeting]` if listed.
  - [ ] **Pyright-strict check**: `dict[Mood, list[str]]` parametrization; `_DEFAULT_GREETINGS_BY_MOOD` constant typing.

- [ ] **Task 2: `SttConfig.clarification_prompts` (replaces singular)** (AC: #4, #11)
  - [ ] In `config/setup.py:SttConfig`:
    - **Replace** the `clarification_prompt: str = ...` field with `clarification_prompts: list[str] = Field(default_factory=lambda: list(_DEFAULT_CLARIFICATION_PROMPTS))`.
    - Define `_DEFAULT_CLARIFICATION_PROMPTS: list[str]` at module level (the 20-entry list from AC #4).
    - `model_validator(mode="after")`: assert `len(clarification_prompts) >= 1`; raise `ValueError` if empty.
  - [ ] In `setup.toml`:
    - **Replace** the singular `clarification_prompt = "..."` line under `[stt]` with the `clarification_prompts = [...]` block per AC #4.
    - **Operator-readable comment** explaining the change: "Story 4.5 (2026-05-07): list-based static random pick replaces the LLM-driven single-string instruction. Operators expand to 30-40 entries for low repetition over long sessions."
  - [ ] In `src/voice_agent_pipeline/turn/router.py`:
    - Replace `self._clarification_prompt = stt_config.clarification_prompt` with `self._clarification_prompts = stt_config.clarification_prompts`.
    - In `route`: replace `text=self._clarification_prompt` with `text=random.choice(self._clarification_prompts)`. Add `import random` at top. Log INFO `clarification.picked` with `text` field.

- [ ] **Task 3: `activity/greeting.py` — pure synchronous `trigger_greeting`** (AC: #2, #8)
  - [ ] Create `src/voice_agent_pipeline/activity/greeting.py`. Module docstring per AC #2 (static-random rationale + fallback chain).
  - [ ] Imports per AC #2.
  - [ ] Implement `trigger_greeting(mood, greetings_by_mood) -> str` per AC #2. **Sync, not async.**
  - [ ] Re-export from `activity/__init__.py`: extend `__all__` with `"trigger_greeting"`.
  - [ ] **Pyright-strict** check: `dict[Mood, list[str]]` parametrization; `random.choice`'s return type narrows correctly because the input is `list[str]`.

- [ ] **Task 4: `_GreetingInjectorProcessor` in `pipeline.py`** (AC: #5)
  - [ ] In `src/voice_agent_pipeline/pipeline.py`:
    - Define `_GreetingInjectorProcessor(FrameProcessor)` per AC #5. Place near other processors (next to `_PrePublishProcessor` is reasonable).
    - Class docstring per `feedback_code_comments.md`.

- [ ] **Task 5: `ActivityFSM` constructor extension** (AC: #6)
  - [ ] In `src/voice_agent_pipeline/activity/machine.py` (Story 4.3 baseline):
    - Add `on_sleeping_to_waking: Callable[[], Awaitable[None]] | None = None` to `__init__`. Store as `self._on_sleeping_to_waking`.
    - In `on_wake_detected()`: after the state mutation but before the `_publish` await, fire the callback as a background task (`asyncio.create_task`). Add `task.add_done_callback(self._log_greeting_done)`.
    - Add `_log_greeting_done(self, task)` instance method — logs any exception via `log.exception("greeting.background_error")`.

- [ ] **Task 6: Pipeline-assembly wiring** (AC: #7)
  - [ ] In `pipeline.py:run_pipeline`:
    - Construct `greeting_injector = _GreetingInjectorProcessor()`.
    - Define the `_on_sleeping_to_waking` async closure per AC #7's pseudocode. Capture `mood_controller`, `config.greeting.greetings_by_mood`, `greeting_injector`. **No `talker` capture** — static-random doesn't use it.
    - Pass into `ActivityFSM` constructor.
    - Insert `greeting_injector` in the pipeline list (after `TurnDispatchProcessor`, before `SegmenterProcessor`).
  - [ ] Update `pipeline.py`'s module docstring with a Story 4.5 entry.

- [ ] **Task 7: Unit tests for `trigger_greeting`** (AC: #9)
  - [ ] Create `tests/unit/activity/test_greeting.py`. Module docstring per `feedback_code_comments.md`.
  - [ ] Implement the 6 test cases listed in AC #9.
  - [ ] **No Talker mock needed.** Tests are pure-function tests over a small dict.

- [ ] **Task 8: Updated unit tests for `ActivityFSM` greeting hook** (AC: #13)
  - [ ] Open `tests/unit/activity/test_machine.py` (Story 4.3). Add 3 new tests per AC #13.

- [ ] **Task 9: Config tests — `GreetingConfig` + `clarification_prompts`** (AC: #10, #11)
  - [ ] Open `tests/unit/config/test_setup.py`. Add 4 `GreetingConfig` tests + 4 `clarification_prompts` tests per ACs.

- [ ] **Task 10: TurnRouter tests — clarification list** (AC: #12)
  - [ ] Open `tests/unit/turn/test_router.py` (Story 2.4). Update existing low-confidence tests; add 2 new tests per AC #12.

- [ ] **Task 11: Integration test for wake greeting** (AC: #14, #15)
  - [ ] Create `tests/integration/test_wake_greeting.py`. Mirror Story 4.3's `test_activity_lifecycle.py` harness.
  - [ ] **Test 1**: greeting picked for mood "calm"; assert text is in `greetings_by_mood["calm"]`.
  - [ ] **Test 2**: greeting picked for mood "playful"; assert text is in `greetings_by_mood["playful"]`.
  - [ ] **Test 3**: NFR30 baseline measurement — drive 30 wakes (15 calm + 15 playful); compute p50/p95/max; assert p95 ≤ 800ms (will pass comfortably). Print values to stdout.
  - [ ] Privacy assertion mirroring earlier tests.

- [ ] **Task 12: Update earlier integration test harnesses** (AC: #16)
  - [ ] `tests/integration/test_activity_lifecycle.py` (Story 4.3) — pass default `on_sleeping_to_waking=None`.
  - [ ] `tests/integration/test_simple_turn.py` (Story 2.5) — same.
  - [ ] `tests/integration/test_embodiment_alignment.py` (Story 3.7) — same.
  - [ ] `tests/integration/test_intent_sleep.py` (Story 4.4) — same.
  - [ ] **Story 3.7 / 2.4 clarification tests**: if any test asserts on the exact clarification text equality, update to assert `frame.text in config.stt.clarification_prompts`. Otherwise, no change.

- [ ] **Task 13: Pass `just check` + live smoke** (AC: #16)
  - [ ] `uv run pytest tests/unit/activity/ -v` — Story 4.3 + 4.5's unit tests all pass.
  - [ ] `uv run pytest tests/integration/test_wake_greeting.py -v` — passes; NFR30 baseline recorded.
  - [ ] Full `just check` — green.
  - [ ] Full `just test` — all integration tests green.
  - [ ] **Live smoke (manual)** — `just run` on the dev host. Speak "Hey OLAF" three times in succession; expect three different (mostly) greetings drawn from the calm bucket. Speak something unintelligible to trigger clarification — expect a varied clarification phrase from the list. Document observed variety in commit message + dev record.
  - [ ] **Capture NFR30 measurement** in commit message.

- [ ] **Task 14: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] Single commit covering: `activity/greeting.py`, `activity/machine.py` (constructor extension), `pipeline.py` (`_GreetingInjectorProcessor` + assembly), `config/setup.py` (`GreetingConfig` + `clarification_prompts`), `setup.toml` (`[greeting]` block + clarification list update), `turn/router.py` (clarification random pick), `tests/unit/activity/test_greeting.py`, `tests/unit/activity/test_machine.py` (3 new tests), `tests/unit/config/test_setup.py` (8 new tests), `tests/unit/turn/test_router.py` (updates + 2 new), `tests/integration/test_wake_greeting.py`, sprint-status flip.
  - [ ] Suggested commit message: `Story 4.5: static-random wake greeting + clarification list (J1 integration; NFR30 baseline)`.
  - [ ] `git push` immediately after.
  - [ ] Sprint-status: `4-5-wake-greeting: ready-for-dev → in-progress → review`.

## Dev Notes

### Why static random (the 2026-05-07 redesign)

Original AC was Talker-driven greeting with 800ms timeout + word-count gate + fallback list. **Three forces pushed toward static-random**:

1. **Latency budget**: real-world Talker TTFB is 50-400ms; the 800ms NFR30 budget swallows this on Groq but barely fits OpenAI/Gemini on a slow network. Static-random is sub-millisecond and trivially fits any budget.

2. **Failure-mode familiarity**: Story 3.7's `cdf3618` commit fixed exactly this class of bug for the clarification flow — Groq Llama 3.1 8B treated `clarification_prompt = "Briefly ask the user to repeat themselves..."` as a question and answered it literally ("No worries, I'm right here on Kamal's desk"). The fix was to short-circuit and emit the prompt verbatim. Greeting's LLM round-trip would re-introduce the same risk for a feature that doesn't need LLM creativity.

3. **Operator authoring is one-time work**: 30-40 entries × 8 moods is a one-time list-curation job. Per-mood buckets preserve the architectural mood-tinting (FR44) without the LLM call.

The trade-off: lose LLM novelty per call (every wake might say "yeah?" twice in a session). With 30-40 per bucket, repetition over a typical session (~20 wakes) is rare enough to not feel scripted.

### Why pure-sync `trigger_greeting`

Earlier draft was `async def trigger_greeting(...)` because it called the Talker. Static-random is `random.choice(...)` — sub-microsecond, no I/O. **Sync function** removes the `await` ceremony at every call site.

The FSM hook closure stays `async def` (it's a coroutine consumed by `asyncio.create_task`), but inside it just calls `text = trigger_greeting(mood, greetings)` directly — no `await`. Then `await greeting_injector.inject_greeting(text)` is async because `push_frame` is async.

### Bucket-curation guidance for the operator

The starter lists in AC #3 are 8-10 entries per mood — enough for unit tests + initial smoke, not enough for production variety. **Operator should expand to 30-40 over time.** Suggested approach:
- Start with the architectural intent: 2-8 words per entry, casual / cool friend register, no SSML, no questions-as-instructions.
- For each mood, brainstorm: "what would a friend who's currently in [mood] say to acknowledge a wake?" Calm: "yeah?", "I'm here". Playful: "yo!", "what's up?". Sleepy: "mmh", "yeah hi?". Etc.
- Don't use the Mood literal value as a synonym (i.e., don't make every "happy" greeting include the word "happy"). The mood TINTS the register, doesn't dictate the content.
- Test live (Task 13) — speak "Hey OLAF" repeatedly; if the same phrase fires twice in three wakes, expand the bucket.

### The clarification list — same architectural pattern

`[stt] clarification_prompts` follows the same shape as greeting buckets — a list of operator-authored static strings, one picked at random per low-confidence turn.

**Why this is a strict improvement over the singular `clarification_prompt`**:
- Story 3.7's `cdf3618` already short-circuits the dispatcher to NOT send to Talker — the prompt is emitted verbatim. So the existing v1 behavior is "say this exact string." Making it a list is a tiny code change with a UX win (variety).
- No new failure modes — pydantic ensures the list is non-empty; `random.choice` always returns a string; the dispatcher's existing emit path handles the result identically.

### Test-mocking pattern

Mock surfaces:
- **No Talker mock needed** for greeting tests. The greeting flow doesn't touch Talker.
- `MoodController` — real instance with `LogEventPublisher`. Mirror Story 3.6 / 4.3 patterns.
- `random.choice` — **don't mock**. Tests use small buckets so all entries are reachable; `random.seed(0)` for determinism if a specific test needs a specific pick.
- `ActivityFSM` — for AC #13's tests, real FSM with `LogEventPublisher`; `AsyncMock` only for the `on_sleeping_to_waking` callback parameter.

### What this story does NOT do

- **No Talker integration for greeting** — Story 4.4 confirmed this in its revised AC #8.
- **No timeout / fallback / word-count gate** — all dropped with the LLM removal.
- **No `prompts/talker_greeting.md` file** — not created.
- **No mic-mode flip** — Story 4.6's territory.
- **No live-tuning of an LLM prompt** — the lists are TOML edits, not prompt iterations.
- **No conversation-wide greeting rotation** (e.g., "don't repeat the previous greeting") — each invocation is independent. v2 may add a "no-repeat-immediate" rule if the operator finds it bothersome; v1 stays simple.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/activity/greeting.py` — `trigger_greeting` sync function.
- `tests/unit/activity/test_greeting.py` — 6 unit tests.
- `tests/integration/test_wake_greeting.py` — J1 integration test.

It modifies:
- `src/voice_agent_pipeline/activity/machine.py` — `ActivityFSM` constructor extension + done-callback method.
- `src/voice_agent_pipeline/activity/__init__.py` — re-export `trigger_greeting`.
- `src/voice_agent_pipeline/pipeline.py` — `_GreetingInjectorProcessor`; `run_pipeline` constructs injector + greeting closure + passes callback to FSM.
- `src/voice_agent_pipeline/config/setup.py` — `GreetingConfig` model; `greeting` field on `SetupConfig`; `SttConfig.clarification_prompts` (replaces singular); module-level `_DEFAULT_GREETINGS_BY_MOOD` + `_DEFAULT_CLARIFICATION_PROMPTS` constants.
- `src/voice_agent_pipeline/turn/router.py` — `clarification_prompts` list consumption; `random.choice` per turn.
- `setup.toml` — `[greeting.greetings_by_mood]` block (NEW); `[stt] clarification_prompts` (replaces singular).
- `tests/unit/activity/test_machine.py` — 3 new tests.
- `tests/unit/config/test_setup.py` — 8 new tests (4 GreetingConfig + 4 clarification_prompts).
- `tests/unit/turn/test_router.py` — existing low-confidence test updates + 2 new tests.
- Earlier integration tests — minor harness updates for the FSM constructor extension.
- `build_documents/implementation-artifacts/sprint-status.yaml` — story status flip.

It does NOT modify:
- `src/voice_agent_pipeline/turn/talker.py` — Story 4.4's territory; **no `greet()` method added** (Story 4.4 revised to drop it).
- `src/voice_agent_pipeline/mood/*` — Story 3.6's territory (read-only consumer).
- `prompts/talker_greeting.md` — file is NOT created (Story 4.4 revised to drop it).

### Testing standards

- **`pytest-asyncio`** in auto mode.
- **No external mocks needed for greeting tests** — pure-function unit tests.
- **One behavior per test** — 6 + 3 + 4 + 4 + 2 unit tests + 3 integration scenarios.
- **Privacy assertions** mirror earlier stories.
- **Pyright strict on `src/`** — `dict[Mood, list[str]]` parametrization; `random.choice` return-type inference; `Callable[[], Awaitable[None]] | None`.

### Performance budget

NFR30: ≤ 800ms p95 from `sleeping → waking` to first audio frame of greeting. Static-random shrinks the local contribution to sub-ms; the budget is dominated by:
- Cartesia TTFB: 200-400ms.
- Audio buffer first-frame: 50-100ms.

Total: ~250-500ms. Comfortable margin under 800ms. Story 5.4 validates against real ambient.

### What "done" looks like

- `just check` exits 0.
- `just test` exits 0; `test_wake_greeting.py` reports a p95 well under 800ms.
- `just run` end-to-end on the dev host:
  - Speak "Hey OLAF" three times → three (mostly) different greetings.
  - Mumble at the mic → varied clarification phrases.
  - The flow feels less "scripted" than the prior single-string clarification.
- Commit message records the synthetic NFR30 baseline.
- Sprint-status flips to `review`.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Wake-greeting mechanism] — `activity/greeting.py:trigger_greeting(mood)` invoked by FSM on `sleeping → waking`. **Static-random is a v1 deviation** — the architecture doc says "Talker greeting-mode invocation"; Story 4.5's redesign substitutes a static-random pick. Update architecture.md as part of this commit per CLAUDE.md rule #9 (NFR26 spec-as-contract): replace "`trigger_greeting` calls Talker.greet with 800ms timeout + fallback list" with "`trigger_greeting` picks `random.choice(greetings_by_mood[mood])` from operator-authored bucket lists". Document the rationale (this dev-notes section).
- [Source: build_documents/planning-artifacts/architecture.md#Wake-greeting trigger] — FSM hook on `sleeping → waking`; concurrent with publish.
- [Source: build_documents/planning-artifacts/prd.md#FR44] — wake greeting on every sleeping→waking; mood-tinted. **Architecture deviation note**: PRD says "by Talker in greeting mode"; static-random preserves the user-facing intent (mood-tinted greeting) but skips the LLM. Update PRD §"FR44" or §"Risks" with the deviation note.
- [Source: build_documents/planning-artifacts/prd.md#NFR30] — `(sleeping_to_waking → first audio frame) ≤ 800ms p95`. Static-random comfortably hits this.
- [Source: build_documents/planning-artifacts/epics.md#Story 4.5] — full AC list (the original Talker-driven version). This story file (the implementation artifact) supersedes the epic AC where they differ.
- [Source: build_documents/implementation-artifacts/4-3-activity-fsm-core.md] — `ActivityFSM.on_wake_detected` is the trigger surface; constructor extension narrowly scoped.
- [Source: build_documents/implementation-artifacts/4-4-talker-tool-using-upgrade.md] — Revised AC #8: no `Talker.greet`, no `prompts/talker_greeting.md`.
- [Source: build_documents/implementation-artifacts/3-7-audio-frame-metadata-and-ssml-prompt.md] — `cdf3618` commit (clarification short-circuit) — the precedent for "static text directly emitted as TalkerResponseFrame, no LLM round-trip."
- [Source: src/voice_agent_pipeline/activity/machine.py] — Story 4.3's `ActivityFSM`; `on_wake_detected` is the modification site.
- [Source: src/voice_agent_pipeline/mood/state.py] — `MoodState.current` read.
- [Source: src/voice_agent_pipeline/turn/router.py] — Story 2.4's `TurnRouter`; clarification short-circuit modification site.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

### Completion Notes List

### File List

## Change Log

| Date | Change |
|---|---|
| 2026-05-07 | Story 4.5 prepared — wake greeting (Talker-driven trigger_greeting + 800ms timeout + fallback + J1 integration). |
| 2026-05-07 | **Revised — static-random per-mood bucket pick replaces Talker.greet**. Drops LLM call, timeout, word-count gate, fallback chain. Extends to STT clarification (`clarification_prompts: list[str]` with random.choice per turn). Same NFR30 budget; trivially satisfied. Updates Story 4.4 (no `Talker.greet`, no `prompts/talker_greeting.md`) + Story 2.4's `clarification_prompt` field. **Architecture.md + PRD §FR44 deviation note** required at commit time per CLAUDE.md rule #9 (NFR26). |
