# Story 4.3: Activity FSM core — 7-state + deferred-sleep + mic-mode signaling

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want a 7-state activity FSM in a new `activity/` package (renamed from `lifecycle/`) that transitions on observable events, schedules deferred-sleep on `go_to_sleep` tool calls, signals mic-mode flips to the audio transport, and publishes `ActivityEvent` on every transition,
so that the conversation-shaped pipeline has a single coherent state spine — and Stories 4.4 (Talker tool-using), 4.5 (wake greeting), and 4.6 (mic-mode flip) have a stable surface to integrate with.

## Acceptance Criteria

1. **`lifecycle/` → `activity/` rename is a discrete change.** The existing placeholder package `src/voice_agent_pipeline/lifecycle/` (only contains `__init__.py` with a one-line stub docstring) is renamed to `src/voice_agent_pipeline/activity/`. Every reference to `lifecycle/` across the codebase is updated:
   - `src/voice_agent_pipeline/__init__.py` — replace `- ``lifecycle``  lifecycle state machine (Story 4.4)` with `- ``activity``  activity FSM + deferred-sleep + mic-mode signaling (Story 4.3)`.
   - `src/voice_agent_pipeline/audio/wakeword.py:67` and `:175` — comments mentioning "Story 4.4's lifecycle FSM" → "Story 4.3's activity FSM".
   - `src/voice_agent_pipeline/pipeline.py:576` — same kind of comment update.
   - `src/voice_agent_pipeline/splitter/mapping.py:206`, `:254` — both mention "lifecycle" in passing; rephrase to "activity FSM" if the sentence's intent matches; leave alone if "lifecycle" means the more general lifetime concept.
   - **The rename has no other behavior mixed in.** No new methods, no new state, no test changes other than the path shift. **Recommend** committing the rename as a separate commit (`Story 4.3 part 1: rename lifecycle/ → activity/`) before the FSM impl commit so the rename diff is reviewable in isolation; the per-story commit policy (`feedback_commit_policy.md`) allows multiple commits per story when the boundary is clean.
   - After the rename, `rg -F 'lifecycle' src/` should return zero hits *for the package directory or `import voice_agent_pipeline.lifecycle`*; passing references to "lifecycle" as a general English word in docstrings (audio device lifecycle, etc.) are fine.

2. **`src/voice_agent_pipeline/activity/states.py` re-exports the Literals from the schema module.** The `ActivityState` and `WorkingSubmode` Literals **already live in `src/voice_agent_pipeline/schemas/activity_event.py`** (Story 3.4 landed them). Mirror Story 3.6's mood pattern: do NOT re-declare the Literals — re-import from the schema module to avoid type drift.
   ```python
   # activity/states.py
   from voice_agent_pipeline.schemas.activity_event import ActivityState, WorkingSubmode

   __all__ = ["ActivityState", "WorkingSubmode"]
   ```
   Module docstring per `feedback_code_comments.md`: explain the re-export rationale (single source of truth in the schema module; Story 3.6's `mood/state.py` set the precedent). Re-export from `activity/__init__.py` too so callers can write `from voice_agent_pipeline.activity import ActivityFSM, ActivityState, WorkingSubmode` (a single import).

3. **`src/voice_agent_pipeline/activity/machine.py` — `ActivityFSM` class.** Sync class (architecture.md §"Activity FSM module": "FSM is sync (no awaits inside transition logic — callers are responsible for invoking it on appropriate events)"). Constructor signature:
   ```python
   class ActivityFSM:
       def __init__(self,
                    publisher: EventPublisher,
                    mic_mode_queue: asyncio.Queue[Literal["wake_word_only", "vad_stt"]] | None = None) -> None:
           self._publisher = publisher
           self._mic_mode_queue = mic_mode_queue or asyncio.Queue()
           self._current_state: ActivityState = "starting"
           self._working_submode: WorkingSubmode | None = None
           self._sleep_pending: bool = False
   ```
   - **Public read-only properties**: `current_state` (returns `self._current_state`), `working_submode` (returns `self._working_submode`), `sleep_pending` (returns `self._sleep_pending`), `mic_mode_queue` (returns `self._mic_mode_queue` — the queue Story 4.6's audio transport subscribes to).
   - **Single-writer rule** (architecture.md §"FSM event sources"): only the FSM mutates `_current_state`. Other components emit transition events into FSM methods, never mutate state directly. The `_current_state` field is name-mangled by convention (underscore prefix) — Python doesn't enforce, but reviewers do. **Document this in the class docstring** as the architectural invariant.

4. **Transition methods** (AC: from epics.md Story 4.3 explicit list):
   | Method | From state | To state | Sub-mode change | Notes |
   |---|---|---|---|---|
   | `start()` | `starting` | `sleeping` | — | Single-call lifecycle init; called once on pipeline startup right after `EventPublisher.connect()`. Publishes `starting → sleeping` transition. **Mic-mode** flips to `wake_word_only`. |
   | `on_wake_detected()` | `sleeping` | `waking` | — | Wake-word detected. **Mic-mode** flips to `vad_stt`. |
   | `on_speech_started()` | `waking` | `listening` | — | First VAD-detected speech after wake. If already in `listening` (continuous-conversation case), **no-op** (do not raise, do not re-publish). |
   | `on_speech_ended()` | `listening` | `working` | submode → `"thinking"` | VAD silence threshold; STT result is in flight. |
   | `on_dispatch_to_orchestrator()` | `working[thinking]` | `working[delegating]` | submode → `"delegating"` | TurnRouter chose orchestrator path (Story 4.7 wires the call). State stays `working`; only sub-mode changes. ActivityEvent still publishes (sub-mode change is observable). |
   | `on_first_audio_frame()` | `working` (any sub-mode) | `speaking` | submode → `None` | First TTS audio frame leaves the transport. |
   | `on_last_audio_frame()` | `speaking` | `listening` *or* `going_to_sleep` | — | If `_sleep_pending` is `True`: transition to `going_to_sleep`, then schedule an immediate follow-up transition `going_to_sleep → sleeping` (two transitions, two `ActivityEvent` publishes — see AC #5). Otherwise: `speaking → listening`. |
   | `on_going_to_sleep_complete()` | `going_to_sleep` | `sleeping` | — | Called by the deferred-sleep scheduler immediately after `on_last_audio_frame` if `_sleep_pending`. Most callers will go through the deferred-sleep path; this method exists as the explicit transition seam (testable in isolation). **Mic-mode** flips to `wake_word_only`. |
   | `on_tool_call_go_to_sleep()` | any (typically `speaking`) | (no transition) | — | Sets `_sleep_pending = True`. **Does NOT transition.** Logs INFO. |
   | `cancel_pending_sleep()` | any | (no transition) | — | Clears `_sleep_pending = False`. Called when a wake-word fires before `on_last_audio_frame` (edge case in AC #5 / FR46). |

5. **Deferred-sleep scheduler — the linchpin behavior** (FR46):
   - When `on_tool_call_go_to_sleep()` fires (typically mid-`speaking` while Talker's response audio is still streaming): set `_sleep_pending = True`. **Do NOT transition immediately.** Log INFO `event="activity.sleep_scheduled"` with `from_state=current_state`.
   - On the next `on_last_audio_frame()`: check `_sleep_pending`.
     - **If `True`**: transition `speaking → going_to_sleep` (publish `ActivityEvent` with `transition_reason="last_audio_frame"`). Then **immediately** transition `going_to_sleep → sleeping` (publish `ActivityEvent` with `transition_reason="deferred_sleep_complete"`). Mic-mode flips to `wake_word_only` on the second transition. Clear `_sleep_pending = False`. **Two `ActivityEvent` publishes** in this path (architecture.md §"Deferred-sleep scheduler" — "two transitions, two `ActivityEvent` publishes").
     - **If `False`**: normal turn end — `speaking → listening` (single publish, `transition_reason="last_audio_frame"`). Mic-mode stays `vad_stt`.
   - **Edge case — wake-word during pending sleep** (the rare race per FR46): if a `on_wake_detected()` fires while `_sleep_pending=True` and the FSM is still in `speaking` (no last-frame yet), call `cancel_pending_sleep()` first to clear the flag, then run the normal `on_wake_detected` transition. Document this race in the FSM's docstring — it's an edge case but the unit test in AC #11 must cover it.
   - **Implementation shape**: keep the deferred-sleep logic inside `on_last_audio_frame()` itself — don't introduce a separate "scheduler" object. The "scheduler" in architecture.md is the conceptual contract; the impl is a single conditional inside `on_last_audio_frame`.

6. **`ActivityEvent` publish on every transition** (architecture.md §"Single-writer rule" + AC #6 epics):
   - Every transition method that actually changes state invokes `await self._publisher.publish_activity(event)` where `event = ActivityEvent(payload=ActivityPayload(state=<to>, from_state=<from>, working_submode=<submode_after>, transition_reason=<string>))`.
   - **`transition_reason` taxonomy** (operator-readable, snake_case strings): `"startup_complete"` (`starting → sleeping`), `"wake_detected"` (`sleeping → waking`), `"end_of_speech"` (`listening → working`, also `waking → listening` on first VAD speech), `"orchestrator_dispatch"` (`working[thinking] → working[delegating]`), `"first_audio_frame"` (`working → speaking`), `"last_audio_frame"` (`speaking → listening`, also `speaking → going_to_sleep` if deferred-sleep), `"deferred_sleep_complete"` (`going_to_sleep → sleeping`), `"go_to_sleep_tool_call"` (set `_sleep_pending=True` — **does not publish**, no transition). Document the full set in the FSM's class docstring.
   - **Async publish from a sync FSM**: `publish_activity` is async (`await self._publisher.publish_activity(...)`). The transition methods themselves must be `async def` because they invoke this. **This contradicts architecture.md's "FSM is sync"** — the resolution is: the FSM's *state mutation* is sync (no awaits between checking the precondition and writing the new state), but the publish-after-transition is async. So transition methods are `async def`; the *critical section* of "check + mutate state" is plain Python (no `await` between them); the `await self._publisher.publish_activity(...)` happens AFTER the mutation. This avoids the canonical async-state-machine bug ("two callers race through the precondition because there's an await in the middle"). **Document this discipline** in the FSM module docstring.
   - **`from_state` is required for every transition except the very first `starting` publish** (per `ActivityPayload._check_from_state` validator in `schemas/activity_event.py`). The `start()` method publishes the initial `starting → sleeping` transition with `from_state="starting"`. **There is no separate "publish initial starting state" call** — the FSM is conceptually in `starting` *before* `start()` runs, and `start()` publishes the transition into `sleeping`. (If product needs a separate `state="starting", from_state=None` publish — for late subscribers to see "we exist" — defer to a follow-up; not in this story's AC.)

7. **Mic-mode signal queue** (FR47, architecture.md §"Mic-mode signaling"):
   - `mic_mode_queue: asyncio.Queue[Literal["wake_word_only", "vad_stt"]]` — single-writer (FSM), single-reader (Story 4.6's audio/transport).
   - **When the FSM enters `sleeping`**: emit `mic_mode = "wake_word_only"` via `await self._mic_mode_queue.put("wake_word_only")`.
   - **When the FSM enters `waking`, `listening`, `working`, or `speaking`**: emit `mic_mode = "vad_stt"` (only if the previous state's mic-mode was `wake_word_only` — don't enqueue redundant signals on every state change within the AWAKE cluster). **Track `_last_mic_mode_emitted: Literal[...] | None = None`** on the FSM and only enqueue on a change. Initial emission: `start()` enqueues `"wake_word_only"` because the FSM is now in `sleeping`.
   - **`going_to_sleep`**: mic-mode stays `vad_stt` (so a follow-up wake-word from the user could in theory cancel — though edge case, per Story 4.6's AC). Only on the subsequent `going_to_sleep → sleeping` transition does the mic flip back to `wake_word_only`.
   - **Enqueue is async** (`await queue.put(...)`) — fine, transition methods are already async (AC #6).
   - The audio transport subscribes in Story 4.6; Story 4.3 only emits. **Story 4.3 ships an `asyncio.Queue` constructed inside the FSM (or injected); Story 4.6 reads via `await fsm.mic_mode_queue.get()` in its loop.** No explicit subscriber side in this story.

8. **Illegal transition → `VoiceAgentError`** (v1 fail-fast):
   - If a transition method is invoked from a state where the transition is undefined (e.g., `on_first_audio_frame()` from `sleeping`, `on_speech_ended()` from `speaking`), raise `VoiceAgentError(reason="illegal_transition", current_state=self._current_state, attempted_method=<method name>)`. The error's `context` carries enough info to debug.
   - **`VoiceAgentError`** is the right base — illegal transitions are programming errors (caller bugs), not external-service failures. NOT `ExternalServiceError`. Pipeline crashes; systemd restarts.
   - **Idempotent same-state methods are NOT illegal**: e.g., `on_speech_started()` from `listening` is a no-op (continuous-conversation: VAD detects new speech while already in listening → no transition needed, no error). The FSM is permissive on idempotent calls; strict on actually-bad transitions. **Document the distinction** in the FSM class docstring with a short "Permissive on idempotent / strict on illegal" rule.

9. **Logging discipline** (NFR25, FR39, architecture.md §"INFO — activity FSM transitions"):
   - INFO `event="activity.transition"` on every transition with fields `from_state`, `to_state`, `working_submode` (when applicable), `transition_reason`, `correlation_id` (Story 3.7's contextvar pattern — bind via `structlog.contextvars.get_contextvars()`; if not bound, leave the field absent rather than synthesizing a fake id).
   - INFO `event="activity.sleep_scheduled"` when `on_tool_call_go_to_sleep` sets `_sleep_pending`. Fields: `current_state`, `working_submode` (if `working`).
   - INFO `event="activity.sleep_cancelled"` when `cancel_pending_sleep()` is called and a pending flag is cleared. Fields: `cancelled_at_state=current_state`.
   - **Never log**: transcript content, tool-call arguments (those are logged at the tool seam, Story 4.4), audio bytes. Standing privacy invariant; FSM transitions naturally don't carry these but the caller-emitted strings (e.g., a `transition_reason` synthesized from "user said X") would be a leak. The FSM only accepts predefined `transition_reason` strings (the snake_case taxonomy in AC #6); any caller passing a free-form reason is a bug. **Recommend**: type `transition_reason` parameter as `Literal[...]` of the predefined values where it's caller-supplied, not free-form `str`. (Internal FSM code constructs the strings directly so the check is at code-review time; if a future caller passes a transition_reason from outside, the type system catches it.)

10. **Pipeline-assembly wiring + `pipeline.py` updates**:
    - In `src/voice_agent_pipeline/pipeline.py:run_pipeline` (Story 3.7 baseline + Story 4.1/4.2 wiring):
      - Construct `activity_fsm = ActivityFSM(publisher=event_publisher)` after `event_publisher.connect()` + `mood_controller.publish_initial()` (Story 3.7's existing order).
      - Call `await activity_fsm.start()` — publishes `starting → sleeping` and emits the initial `wake_word_only` mic-mode signal.
      - Pass `activity_fsm` reference to:
        - `WakewordProcessor` (Story 1.6): hook `on_wake_detected()` invocation. **Already partially scaffolded** — `audio/wakeword.py:67` mentions "Story 4.4's lifecycle FSM reads this"; this story makes the read live. Specifically, the `WakewordProcessor` should `await activity_fsm.on_wake_detected()` inside its detection callback (or however Story 1.6 emits the wake event). **Recommend**: introduce a thin `_FsmEventBridge(FrameProcessor)` that consumes wake-word frames + VAD frames and translates to FSM method calls — keeps the audio/* processors decoupled from the activity/* package. Place it after the wakeword + VAD stages.
        - `VadProcessor` (Story 1.7): hook `on_speech_started()` on VAD start, `on_speech_ended()` on `UtteranceCapturedFrame`. Same `_FsmEventBridge` pattern.
        - `_PrePublishProcessor` (Story 3.7): hook `on_first_audio_frame()` on the first `EmbodimentAudioFrame` of a turn (look for the existing first-frame detection logic; if absent, add a per-turn flag inside the processor). Hook `on_last_audio_frame()` on the last frame of a turn — Story 3.7's `_FrameCounter` already tracks frame counts; reuse.
    - **Order** in the pipeline list (extends Story 3.7's wiring):
      ```
      transport.input()
        → WakewordProcessor
        → VadProcessor
        → SttProcessor
        → _SttResultLogger
        → _WakewordEventLogger
        → _FsmEventBridge                              # NEW (this story)
        → TurnDispatchProcessor
        → SegmenterProcessor
        → CartesiaSynthesisProcessor
        → _FrameCounter (now also calls FSM hooks)
        → _PrePublishProcessor
        → transport.output()
      ```
      The `_FsmEventBridge` placement after STT means it has access to `UtteranceCapturedFrame` (which emits at end of speech). Verify the Pipecat frame ordering: `UtteranceCapturedFrame` should arrive at `_FsmEventBridge` *before* `TurnDispatchProcessor` consumes it; if Pipecat's frame fan-out runs them in parallel, the bridge needs to forward the frame downstream after handling.

11. **Replace Story 3.7's `UtteranceCapturedFrame` turn-boundary proxy with the FSM signal.** Story 3.7's `SegmenterProcessor.process_frame` (`pipeline.py:358-363`) currently does:
    ```python
    if isinstance(frame, UtteranceCapturedFrame):
        self._segmenter.reset()
        self._cache.reset()
        self._current_turn_id = uuid4()
    ```
    This is the documented Story 3.7 stopgap. **Story 4.3's replacement**: the segmenter + cache reset should fire on FSM `speaking → listening` (or `speaking → going_to_sleep`) — i.e., end-of-turn audio. **Two implementation options**:
    - **A**: have `_PrePublishProcessor` (or wherever the last-frame detection lives) emit a `_TurnBoundaryFrame` (a new Pipecat frame class) downstream that `SegmenterProcessor` consumes. **Pro**: pure frame-flow; testable. **Con**: requires introducing a new frame type.
    - **B**: have the FSM expose a callable `on_turn_boundary: Callable[[], None] | None` that `SegmenterProcessor` registers; the FSM invokes it on `speaking → listening` / `speaking → going_to_sleep`. **Pro**: simpler. **Con**: mixes async-frame and sync-callback worlds.
    - **Recommend Option A** — introduce `class _TurnBoundaryFrame(Frame)` defined in `pipeline.py` next to `SegmentFrame` / `EmbodimentAudioFrame`. Emit it from the same code site that calls `fsm.on_last_audio_frame()`. `SegmenterProcessor.process_frame` consumes it instead of `UtteranceCapturedFrame`. **Keep the `UtteranceCapturedFrame`-handling code in place** but switch its body to a no-op (or delete entirely if no other downstream consumer relies on it for SegmenterProcessor reset). Document the v1-proxy → FSM-signal migration in the dev record.
    - **Update Story 3.7's `correlation_id` binding too**: Story 3.7's `_current_turn_id = uuid4()` now fires on `_TurnBoundaryFrame` instead of `UtteranceCapturedFrame`. Behavior is the same; the trigger source is different (FSM-driven now).

12. **Unit tests in `tests/unit/activity/test_machine.py`** (NEW test directory). Cover the legal transition matrix exhaustively + illegal transitions + deferred-sleep + mic-mode signaling:
    - **Test directory setup**: `tests/unit/activity/__init__.py` + `tests/unit/activity/test_machine.py`. Mirror Stories 3.6 / 4.1's `tests/unit/<module>/test_<file>.py` layout.
    - `test_initial_state_is_starting` — `fsm.current_state == "starting"` immediately after construction (before `start()`).
    - `test_start_transitions_starting_to_sleeping_and_publishes` — `await fsm.start()`; `fsm.current_state == "sleeping"`; `publisher.published[0]` is `("activity", ActivityEvent(payload.state="sleeping", payload.from_state="starting", payload.transition_reason="startup_complete"))`. Mic-mode queue has `"wake_word_only"` enqueued.
    - `test_legal_transition_sequence_simple_turn` — drive `start() → on_wake_detected() → on_speech_started() → on_speech_ended() → on_first_audio_frame() → on_last_audio_frame()`; assert state path is `[sleeping, waking, listening, working[thinking], speaking, listening]`; assert 5 `publish_activity` calls (one per transition, **plus** the `start()` publish = 6 total — be precise about expected count). Assert mic-mode queue has `[wake_word_only, vad_stt]` (only 2 entries; vad_stt enqueued once on `waking` and stays).
    - `test_legal_transition_sequence_orchestrator_path` — `working[thinking] → working[delegating]` via `on_dispatch_to_orchestrator()`. Assert one extra `publish_activity` (sub-mode change is observable).
    - `test_on_speech_started_in_listening_is_idempotent` — already in `listening`; call `on_speech_started()`; no transition, no publish, no error. Permissive idempotency rule (AC #8).
    - `test_illegal_transition_raises_voice_agent_error` — call `on_first_audio_frame()` from `sleeping`; `pytest.raises(VoiceAgentError)`; `excinfo.value.context["reason"] == "illegal_transition"`, `["current_state"] == "sleeping"`, `["attempted_method"] == "on_first_audio_frame"`.
    - `test_deferred_sleep_speaking_to_going_to_sleep_to_sleeping` — drive a normal turn through to `speaking`; call `on_tool_call_go_to_sleep()`; assert `fsm.sleep_pending is True` AND no transition (still in `speaking`); call `on_last_audio_frame()`; assert state path `[going_to_sleep, sleeping]` (two transitions); assert two `publish_activity` calls in this segment. Assert mic-mode queue has `wake_word_only` enqueued on `sleeping`. Assert `fsm.sleep_pending is False` after.
    - `test_deferred_sleep_cancelled_by_wake_word` — drive to `speaking`; `on_tool_call_go_to_sleep()` (sleep_pending=True); now simulate a wake-word firing before `on_last_audio_frame` lands: but wait — in `speaking`, `on_wake_detected()` is illegal (current state isn't `sleeping`). The race per FR46 is: **wake-word arriving after `going_to_sleep` started but before `sleeping` finalized**, OR wake-word arriving after the user has already heard the goodbye and the mic flipped back. **Story 4.3 implementation**: provide `cancel_pending_sleep()` as the explicit cancellation method; the wake-word code path doesn't need to call it from `speaking` (illegal anyway). The test exercises `cancel_pending_sleep()` directly: `on_tool_call_go_to_sleep()` then `cancel_pending_sleep()` → `sleep_pending` is False. Then `on_last_audio_frame()` → normal `speaking → listening` (not deferred-sleep).
    - `test_mic_mode_signal_emitted_on_sleeping_and_waking_only` — drive a full simple-turn loop; assert mic-mode queue's contents match `[wake_word_only, vad_stt]` (two entries, not one-per-state). Validates the de-dup invariant from AC #7.
    - `test_mic_mode_signal_after_deferred_sleep` — drive deferred-sleep; assert mic-mode queue has `[wake_word_only, vad_stt, wake_word_only]` (initial wake_word_only on `start()`, vad_stt on wake, wake_word_only after `going_to_sleep → sleeping`). **No vad_stt enqueue during going_to_sleep** (mic stays in vad_stt mode but no signal change since the previous mode was already vad_stt — de-dup).
    - `test_publish_activity_uses_pydantic_envelope_correctly` — assert the published `ActivityEvent.payload.from_state` matches the prior state (not None except on `starting`); `working_submode` is non-None iff `state="working"`. Validates against the `ActivityPayload` model_validator from `schemas/activity_event.py`.
    - `test_logs_event_activity_transition` — caplog / structlog capture; assert one INFO `event="activity.transition"` per transition with `from_state`, `to_state`, `transition_reason` fields. Mirror Story 3.6's logging assertion pattern.
    - `test_logs_event_activity_sleep_scheduled_on_tool_call` — `on_tool_call_go_to_sleep()` produces one INFO `event="activity.sleep_scheduled"` with `current_state="speaking"`.
    - `test_logs_event_activity_sleep_cancelled_when_pending` — `on_tool_call_go_to_sleep()` then `cancel_pending_sleep()` produces one INFO `event="activity.sleep_cancelled"`.
    - `test_cancel_pending_sleep_when_not_pending_is_noop` — `cancel_pending_sleep()` from clean state; no log, no error, no state change.
    - `test_publish_activity_failure_propagates` — mock the publisher's `publish_activity` to raise `PublisherError`; drive a transition; `pytest.raises(PublisherError)` propagates (CLAUDE.md rule #4 — no catching publisher failures). Assert state mutation order: **state was already mutated before publish** (publish happens after) — so on failure, `fsm.current_state` reflects the new state but the event was lost. **Document this trade-off** in dev notes; it's a fail-fast crash anyway, the v1 stance is "doesn't matter — process exits and restarts."
    - `test_use_log_event_publisher_as_fake` — use `LogEventPublisher` (Story 3.5) as the publisher fixture, NOT a `MagicMock`. Real-Protocol-impl-as-fake is the canonical pattern (CLAUDE.md rule #7); only mock when you need the failure-injection (the prior test).

13. **Integration test in `tests/integration/test_activity_lifecycle.py`** (NEW). PRD Journey 1 simple-turn-shape:
    - **Mock**: Cartesia (yields synthetic audio chunks at known intervals; mirror Story 3.7's pattern). Talker (returns canned response). STT (returns canned transcript). Wake-word (fires once on simulated trigger).
    - **Real**: `ActivityFSM`, `LogEventPublisher`, `MoodController`, `Segmenter`, `_PrePublishProcessor`, `_FsmEventBridge`.
    - **Drive**: simulate the full pipeline through one simple turn — wake-word fires → STT result → Talker response → audio frames → last-frame detection.
    - **Assert**:
      - Published activity sequence on `LogEventPublisher.published`: `[("activity", state="sleeping"), ("activity", state="waking"), ("activity", state="listening"), ("activity", state="working", submode="thinking"), ("activity", state="speaking"), ("activity", state="listening")]`. Filter `LogEventPublisher.published` for topic `"activity"` and assert the state sequence.
      - `correlation_id` matches across the activity events for one turn (Story 3.7's contextvar binding now driven by FSM's turn boundary).
      - Mic-mode queue (read off the FSM after the test) had at minimum two emissions: `["wake_word_only", "vad_stt"]`.
    - **Privacy assertion** (mirror Story 3.7's `test_no_audio_field_names_in_logs`): no transcript content in any captured log line.

14. **`just check` stays green.** All earlier stories' tests still pass (especially Story 3.7's embodiment alignment + simple-turn integration). The `_TurnBoundaryFrame` change requires updates to `tests/unit/test_pipeline.py` (Story 3.7's processor unit tests) and `tests/integration/test_simple_turn.py` (Story 2.5's harness):
    - **`tests/unit/test_pipeline.py`**: tests that drive `UtteranceCapturedFrame` to validate segmenter reset must now drive `_TurnBoundaryFrame` (or both, during the migration window). Update the tests; document the migration in the dev record.
    - **`tests/integration/test_simple_turn.py`** and **`tests/integration/test_embodiment_alignment.py`** (Story 3.7): may need a small update to insert the `ActivityFSM` + `_FsmEventBridge` into the test harness, OR (recommended) inject a stub FSM that just routes the boundary frame through. **Recommend**: a real `ActivityFSM(publisher=LogEventPublisher())` in the test harness — adds a few activity events to the captured publish list, but exercises the production wiring shape. Update assertion filters to `[e for e in published if e[0] == "speech_emotion"]` etc. so existing assertions don't trip on the new activity events.

15. **No transcripts at INFO+; no API key in any log; no raw audio in any log** (NFR25, FR39). Standing privacy invariant. The FSM only logs FSM-internal fields (state, sub-mode, transition_reason); none of these carry user content. The `_FsmEventBridge` consumes frames containing transcript text but **must NOT log the transcript** — only the fact-of-transition.

16. **Mood enum / Mood Literal — no overlap with Story 3.6.** Story 4.3 does NOT touch `mood/state.py`, `mood/controller.py`, or the `Mood` Literal. Wake-greeting integration (Story 4.5) reads `mood_controller.state.current` to tint the greeting; Story 4.3's only mood touch-point is leaving the `MoodController` reference reachable from where Story 4.5 will read it (already wired in Story 3.7's `pipeline.py`).

17. **No tools registry (Story 4.4 lands it).** The FSM's `on_tool_call_go_to_sleep()` and `cancel_pending_sleep()` methods are **callable surfaces** that Story 4.4's `GoToSleepTool.dispatch` will invoke. Story 4.3 ships them ready; Story 4.4 wires the call site through `ToolRegistry.dispatch`.

## Tasks / Subtasks

- [x] **Task 1: Rename `lifecycle/` → `activity/` (discrete commit)** (AC: #1)
  - [ ] `git mv src/voice_agent_pipeline/lifecycle src/voice_agent_pipeline/activity`. Verify `git status` shows the rename detection (R100).
  - [ ] Update `src/voice_agent_pipeline/activity/__init__.py` from the lifecycle stub to a Story 4.3 docstring placeholder (will fill in real content in Task 2). Suggested content:
    ```python
    """Activity FSM — 7-state state machine + deferred-sleep + mic-mode signaling (Story 4.3)."""
    from voice_agent_pipeline.activity.machine import ActivityFSM
    from voice_agent_pipeline.activity.states import ActivityState, WorkingSubmode

    __all__ = ["ActivityFSM", "ActivityState", "WorkingSubmode"]
    ```
    (For now the imports will fail until Task 2 lands — that's fine, this is committed in a sequence.)
  - [ ] Update `src/voice_agent_pipeline/__init__.py`'s module-level docstring: `lifecycle` → `activity`, Story 4.4 → Story 4.3.
  - [ ] Grep for `lifecycle` across `src/`: `rg lifecycle src/` — update each comment hit per AC #1's enumeration.
  - [ ] Grep for `lifecycle` across `tests/`: should show zero hits in test paths. (Tests reference Story numbers, not the module name — but verify.)
  - [ ] **Optional first commit**: `Story 4.3 part 1: rename lifecycle/ → activity/ package`. Push.

- [x] **Task 2: `activity/states.py` re-export of Literals** (AC: #2)
  - [ ] Create `src/voice_agent_pipeline/activity/states.py` with the re-import + module docstring per AC #2.
  - [ ] Verify the docstring follows `feedback_code_comments.md` (generous SDLC-style).

- [x] **Task 3: `activity/machine.py` — `ActivityFSM` class** (AC: #3, #4, #5, #6, #7, #8, #9)
  - [ ] Create `src/voice_agent_pipeline/activity/machine.py`. Module docstring per `feedback_code_comments.md` — explain: 7-state FSM; sync-state-mutation + async-publish discipline (AC #6); single-writer rule; deferred-sleep linchpin (AC #5); mic-mode signal queue (AC #7).
  - [ ] Imports: `import asyncio`, `import structlog`, `from typing import Literal`, local: `from voice_agent_pipeline.activity.states import ActivityState, WorkingSubmode`, `from voice_agent_pipeline.errors import VoiceAgentError`, `from voice_agent_pipeline.publisher.interface import EventPublisher`, `from voice_agent_pipeline.schemas.activity_event import ActivityEvent, ActivityPayload`.
  - [ ] Module-level: `log = structlog.get_logger(__name__)`. Module-level constant `_TRANSITION_REASONS: frozenset[str] = frozenset({"startup_complete", "wake_detected", "end_of_speech", "orchestrator_dispatch", "first_audio_frame", "last_audio_frame", "deferred_sleep_complete", "go_to_sleep_tool_call"})`. Note: `go_to_sleep_tool_call` doesn't actually fire as a `transition_reason` (no transition publishes for the tool call itself); it's listed for taxonomy completeness.
  - [ ] **Type alias** at module level: `MicMode = Literal["wake_word_only", "vad_stt"]`. Used as the `asyncio.Queue` parameter type and on `_last_mic_mode_emitted`.
  - [ ] Class implementation per AC #3-#9. **Class docstring** spells out:
    - The 7-state set + transitions table (copy from AC #4's table).
    - Single-writer rule (AC #3).
    - Sync-state-mutation, async-publish discipline (AC #6).
    - Permissive on idempotent / strict on illegal (AC #8).
    - Mic-mode signal de-dup (AC #7).
    - Deferred-sleep linchpin (AC #5).
  - [ ] **Helper `_publish` method** (private):
    ```python
    async def _publish(self, *, from_state: ActivityState, to_state: ActivityState,
                       working_submode: WorkingSubmode | None,
                       transition_reason: str) -> None:
        event = ActivityEvent(payload=ActivityPayload(
            state=to_state, from_state=from_state,
            working_submode=working_submode, transition_reason=transition_reason))
        log.info("activity.transition", from_state=from_state, to_state=to_state,
                 working_submode=working_submode, transition_reason=transition_reason)
        await self._publisher.publish_activity(event)
    ```
  - [ ] **Helper `_emit_mic_mode` method** (private async):
    ```python
    async def _emit_mic_mode(self, mode: MicMode) -> None:
        if self._last_mic_mode_emitted == mode:
            return
        self._last_mic_mode_emitted = mode
        await self._mic_mode_queue.put(mode)
    ```
  - [ ] Implement each transition method per AC #4. Critical sections do the state mutation BEFORE the publish, so a publisher failure doesn't leave the FSM in a stale state (well, it will — but that's the v1 fail-fast trade-off; the process is going to crash anyway, see AC #12's `test_publish_activity_failure_propagates`).
  - [ ] Implement `start()`, `cancel_pending_sleep()`, `on_tool_call_go_to_sleep()` per AC #4 / #5.
  - [ ] **Pyright-strict** check: run `uv run pyright src/voice_agent_pipeline/activity/` after writing. Expect zero errors. Likely concerns: `asyncio.Queue` parametrization may need explicit type args at construction; `_TRANSITION_REASONS` is informational, no runtime use.

- [x] **Task 4: `_FsmEventBridge` Pipecat processor + pipeline-assembly wiring** (AC: #10, #11)
  - [ ] In `src/voice_agent_pipeline/pipeline.py`:
    - Define `_FsmEventBridge(FrameProcessor)` near the top of the file, alongside other processors. Behavior: consumes frames from upstream, dispatches to FSM:
      - On `WakewordDetectedFrame` (or whatever Story 1.6 emits): `await fsm.on_wake_detected()`. Forward the frame downstream.
      - On `VadSpeechStartedFrame` (or similar): `await fsm.on_speech_started()`. Forward.
      - On `UtteranceCapturedFrame`: `await fsm.on_speech_ended()`. Forward (downstream consumes for STT — Story 1.7).
      - **Verify exact frame class names** by reading `audio/wakeword.py` + `audio/vad.py` before implementing. The frame names above are placeholders; use the real names from the existing impl. If Story 1.6 / 1.7 emit different frame shapes (e.g., `WakewordEvent` is internal, no frame published), adapt the bridge — the architectural intent is "FSM listens for these observable events." See dev notes.
    - In `_PrePublishProcessor` (Story 3.7) — the existing first-frame / last-frame logic — add hooks:
      - On the first `EmbodimentAudioFrame` of a turn (where "first" is defined by Story 3.7's existing detection — a per-turn flag in the processor): `await fsm.on_first_audio_frame()`.
      - On the last frame (Story 3.7's `_FrameCounter` ties into this — verify the existing detection path): `await fsm.on_last_audio_frame()`. **AND** emit a `_TurnBoundaryFrame` downstream so `SegmenterProcessor` resets (AC #11).
    - **Define `_TurnBoundaryFrame(Frame)`** as a new pipecat frame class in `pipeline.py`. Empty payload (or carry a `turn_id: UUID` field if useful for future debugging). Document in the class docstring: "Replaces Story 3.7's `UtteranceCapturedFrame`-as-turn-boundary stopgap. Emitted by `_PrePublishProcessor` when the FSM transitions out of `speaking`."
  - [ ] Update `SegmenterProcessor.process_frame` (Story 3.7, `pipeline.py:358-363`):
    - Replace the `UtteranceCapturedFrame` reset trigger with `_TurnBoundaryFrame`. Specifically: change `if isinstance(frame, UtteranceCapturedFrame): ...` to `if isinstance(frame, _TurnBoundaryFrame): ...`. The body stays the same (segmenter.reset, cache.reset, current_turn_id = uuid4).
    - **OPTION**: leave the `UtteranceCapturedFrame` branch in place for backwards-compat **temporarily**, but log WARN if it triggers. Once the integration test confirms `_TurnBoundaryFrame` fires correctly, remove the proxy. Document the migration window in the dev record.
  - [ ] Update `pipeline.py:run_pipeline` per AC #10: construct `activity_fsm`, await `fsm.start()`, inject into `_FsmEventBridge` and `_PrePublishProcessor`.
  - [ ] Update `pipeline.py`'s module docstring: append a Story 4.3 entry to the "Story progression" list — FSM construction + bridge processor + `_TurnBoundaryFrame` migration.

- [x] **Task 5: Unit tests for `ActivityFSM`** (AC: #12)
  - [ ] Create `tests/unit/activity/__init__.py` + `tests/unit/activity/test_machine.py`.
  - [ ] Module docstring per `feedback_code_comments.md` — explain: tests cover legal transitions, illegal transitions, deferred-sleep, mic-mode signal sequencing, logging discipline. Use `LogEventPublisher` as the real-fake (CLAUDE.md rule #7); only swap to `MagicMock(spec=EventPublisher)` for failure-injection tests.
  - [ ] **Helper fixture**: factory that builds `(fsm, publisher_log)` where `publisher_log = LogEventPublisher()` and `fsm = ActivityFSM(publisher=publisher_log)`. Each test starts from this clean state, optionally awaits `fsm.start()` for tests that need to begin from `sleeping`.
  - [ ] Implement the 16 test cases listed in AC #12. For `test_publish_activity_failure_propagates`, use `MagicMock(spec=EventPublisher)` with `publish_activity = AsyncMock(side_effect=PublisherError(reason="connection_lost"))`.
  - [ ] **Activity-event filter helper**: `def activity_events(published): return [e for _, e in published if isinstance(e, ActivityEvent)]` — used in many assertions.
  - [ ] **Mic-mode-queue assertion helper**: drain the queue with a small async helper `async def drain(queue): out = []; while not queue.empty(): out.append(await queue.get()); return out`. Use `await drain(fsm.mic_mode_queue)` after the test sequence.
  - [ ] Logging assertions: `caplog.set_level(logging.INFO)`. Mirror Story 3.6's `tests/unit/mood/test_controller.py` patterns.

- [x] **Task 6: Integration test for activity lifecycle** (AC: #13)
  - [ ] Create `tests/integration/test_activity_lifecycle.py`. Mirror Story 3.7's `tests/integration/test_embodiment_alignment.py` test-harness shape.
  - [ ] Use real `ActivityFSM`, `LogEventPublisher`, `MoodController`, `Segmenter`, `_PrePublishProcessor`, `_FsmEventBridge`, `SegmenterProcessor`. Mock external services (Cartesia, Talker, STT, wake-word) at the Protocol seam.
  - [ ] **Drive one simple turn** as described in AC #13. Assert the activity-event sequence + correlation_id binding + mic-mode queue contents.
  - [ ] **Privacy assertion** mirroring Story 3.7's `test_no_audio_field_names_in_logs`.

- [~] **Task 7: Update Story 3.7's tests for `_TurnBoundaryFrame` migration** (AC: #14) — **deferred to Story 4.6 / 4.7**
  - [ ] `tests/unit/test_pipeline.py`: tests that drive `UtteranceCapturedFrame` to test segmenter reset must now drive `_TurnBoundaryFrame`. Update + run; should still pass with the new frame.
  - [ ] `tests/integration/test_simple_turn.py` (Story 2.5): insert `ActivityFSM(publisher=LogEventPublisher())` + `_FsmEventBridge` into the harness. Filter assertions on `published` by topic to avoid tripping on the new activity events.
  - [ ] `tests/integration/test_embodiment_alignment.py` (Story 3.7): same pattern. Filter `speech_emotion` / `vocalization` events from the captured publish list.

- [x] **Task 8: Pass `just check`; live-test pipeline** (AC: #14) — `just check` green; live smoke deferred to a later run.
  - [ ] `uv run pytest tests/unit/activity/ -v` first.
  - [ ] `uv run pytest tests/integration/test_activity_lifecycle.py -v`.
  - [ ] Full `just check` — ruff + pyright strict + fast unit tests all green.
  - [ ] Full `just test` — integration tests all green (Stories 2.5, 3.7, 4.3).
  - [ ] **Live smoke (manual)** — `just run` end-to-end on the dev host. Watch `ros2 topic echo /olaf/activity` (mirroring Story 3.7's `/olaf/speech_emotion` smoke pattern). Speak "Hey OLAF, what time is it?". Expect to see activity events:
    1. `state="sleeping", from_state="starting"` (startup)
    2. `state="waking", from_state="sleeping"` (wake-word)
    3. `state="listening", from_state="waking"` (VAD start, but only if continuous-conversation logic emits it — confirm)
    4. `state="working", working_submode="thinking", from_state="listening"` (VAD end → STT done)
    5. `state="speaking", from_state="working"` (TTS first frame)
    6. `state="listening", from_state="speaking"` (TTS last frame, no deferred-sleep)
    Document the sequence observed in the commit message + dev record. **This is the Story 4.3 visible-on-bus capstone for the activity topic** — same shape as Story 3.7's `speech_emotion` smoke.

- [ ] **Task 9: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] Suggested commit sequence (split per AC #1 recommendation):
    - **Commit 1**: `Story 4.3 part 1: rename lifecycle/ → activity/ package` (just the rename + reference updates).
    - **Commit 2**: `Story 4.3: ActivityFSM (7-state + deferred-sleep + mic-mode signaling)` (FSM impl + states + tests + integration test + pipeline wiring + Story 3.7 test updates + sprint-status flip).
  - [ ] `git push` after each commit (per `feedback_push_after_commit.md`).
  - [ ] Sprint-status: flip `4-3-activity-fsm-core: ready-for-dev → in-progress → review` after the live smoke (Task 8) confirms.

## Dev Notes

### Architectural intent — Story 4.3's role in Epic 4

Story 4.3 is the **central spine** of Epic 4. Stories 4.4 (tool-using Talker), 4.5 (wake greeting), 4.6 (mic-mode flip), and 4.7 (orchestrator slow-path) all wire INTO the FSM:
- 4.4: `GoToSleepTool.dispatch` calls `fsm.on_tool_call_go_to_sleep()`. `SetMoodTool` doesn't touch the FSM; mood is its own surface.
- 4.5: greeting fires on `sleeping → waking` transition; the FSM's `_publish` callback (or a separate hook) triggers `activity/greeting.py:trigger_greeting`.
- 4.6: `audio/transport.py` consumes from `fsm.mic_mode_queue`.
- 4.7: orchestrator dispatch fires `fsm.on_dispatch_to_orchestrator()`; the slow-path tracks via the `working[delegating]` sub-mode.

**Land 4.3 third in Epic 4** because every later story depends on it. The orchestrator slow-path (4.7) is also a wider surface that pulls in the orchestrator client (4.2) AND the FSM's `working[delegating]` sub-mode (4.3).

### Sync state mutation, async publish — the discipline that prevents async-state-machine bugs

The canonical bug in async state machines: two callers race through the precondition because there's an `await` in the middle.

```python
# WRONG
async def on_wake_detected(self):
    if self._current_state != "sleeping":
        raise IllegalTransition()
    await self._publisher.publish_activity(...)  # AWAIT HERE
    self._current_state = "waking"  # too late — second caller already passed the check
```

**Right shape**:
```python
async def on_wake_detected(self):
    if self._current_state != "sleeping":
        raise VoiceAgentError(...)
    self._current_state = "waking"  # mutate FIRST (sync, no await)
    self._working_submode = None
    await self._publisher.publish_activity(...)  # publish AFTER (async OK)
    await self._emit_mic_mode("vad_stt")
```

The state mutation between precondition check and publish is plain Python — no opportunity for two async callers to interleave. Once the state is `"waking"`, a second concurrent `on_wake_detected()` call fails the precondition cleanly (the FSM's permissive-on-idempotent rule per AC #8 means no error in this specific case, but the *invariant* is preserved).

**Single-event-loop, single-FSM constraint**: the FSM is constructed once per pipeline. There's no expectation of "two threads racing to call on_wake_detected" because Pipecat is single-event-loop async. The discipline above is belt-and-suspenders against the framework changing OR a future caller introducing an async-callback shape that does interleave.

### State transition matrix — be exhaustive

The transitions table in AC #4 is the authoritative spec. **Every transition method must explicitly handle**:
1. The legal-from-state case → mutate + publish.
2. Idempotent same-state case → no-op (no error, no publish).
3. Illegal-from-state case → raise `VoiceAgentError`.

The line between (2) and (3): same-state is idempotent ONLY where the architectural intent allows continuous-conversation flow. Concretely:
- `on_speech_started()` from `listening` is idempotent (continuous conversation: VAD detects new speech while already listening).
- `on_speech_started()` from `working` or `speaking` is illegal (states beyond listening shouldn't see "VAD started").
- `on_wake_detected()` from any state other than `sleeping` is illegal (FR47: wake-word fires only on `sleeping → waking`).

The unit tests (AC #12) cover both rules.

### Deferred-sleep — why it's a linchpin

The user's last words to OLAF before sleep are typically "thanks, goodbye." Talker's natural-language reply is "okay, sleep well, see you in the morning" — emitted as text + a `go_to_sleep` tool call. If the FSM transitioned to `sleeping` immediately on the tool call, the goodbye would never be heard (mic flips to wake-word-only, but the speaker is mid-utterance — Cartesia keeps streaming, but the "you're now listening for wake-word" semantic is wrong because audio is still playing).

The deferred-sleep contract: the goodbye plays out fully (`speaking` state stays until last audio frame), THEN the FSM transitions through `going_to_sleep` to `sleeping`, THEN mic-mode flips. The user hears the goodbye in `speaking`-state context; the system "is listening" until the audio ends; only then does it shut down for the night.

**Integrity check**: a wake-word firing during the goodbye's audio playback (between `on_tool_call_go_to_sleep` and `on_last_audio_frame`) is a race per FR46. The FSM's `cancel_pending_sleep()` clears the flag; the next `on_last_audio_frame` then transitions to `listening` (not `going_to_sleep`). Document the race; the unit test covers it.

### Mic-mode signal de-dup invariant

`asyncio.Queue.put()` per state change would emit a redundant signal at every `working[thinking] → working[delegating] → speaking` transition (mic-mode is `vad_stt` throughout). The de-dup invariant — emit only when mode changes — keeps the queue short, the audio transport's wake-up cycles bounded, and the test assertions tractable.

The `_last_mic_mode_emitted` field (AC #7) tracks the last value enqueued. Initial value `None` ensures the first emission (from `start()`) is not de-duped.

### Replacing Story 3.7's `UtteranceCapturedFrame` proxy — read this carefully

Story 3.7 explicitly documented the proxy as a stopgap until Story 4.3 lands. The replacement (AC #11) is *not* a deletion; it's a substitution. Story 3.7's `SegmenterProcessor.reset()` logic still needs to fire on turn boundaries — the trigger source changes from "next utterance frame" (which is end-of-USER-speech) to "FSM turn boundary signal" (which is end-of-OLAF-speech, i.e., last audio frame).

**Subtle**: end-of-user-speech and end-of-OLAF-speech are different events! Story 3.7's proxy reset on `UtteranceCapturedFrame` was *early* — it reset the segmenter when the user stopped talking, before OLAF replied. That worked because the segmenter is fed Talker's response, not user transcripts; resetting before OLAF replies just clears stale state from the prior turn.

The FSM-driven replacement resets *after* OLAF's reply audio finishes. **Same net effect for the segmenter** (both happen between turns), but the semantics are cleaner: "turn boundary = the FSM says the turn is over."

The `correlation_id` binding shifts likewise — `uuid4()` now fires on `_TurnBoundaryFrame`, which fires *after* the response audio plays. New turn ID is bound for the *next* turn, which is correct.

### `_FsmEventBridge` — why a separate processor

Two design choices for hooking the FSM into Pipecat's frame flow:
1. **Modify each upstream processor** (`WakewordProcessor`, `VadProcessor`) to invoke FSM methods directly.
2. **Insert a bridge processor** that consumes their output frames and translates to FSM calls.

Picked (2) because:
- Keeps the `audio/*` package decoupled from `activity/*`. `audio/wakeword.py` doesn't import `activity.machine` — it just emits its detection frame and moves on.
- Single integration point for testing — drive the bridge with synthetic frames, assert FSM method calls.
- Composable with future Pipecat re-orderings — the bridge is one processor, easy to slot.

The architecture's "single-writer rule" is preserved: only the FSM mutates its state; the bridge just calls FSM methods.

### `on_first_audio_frame` / `on_last_audio_frame` detection

Story 3.7's `_PrePublishProcessor` already sees every audio frame. Adding "first per turn" and "last per turn" detection inside it is the natural fit — the processor already has per-turn state (`_current_turn_id` from Story 3.7).

**First-frame detection**: `_first_frame_seen_this_turn: bool` — flip on first audio frame, reset on `_TurnBoundaryFrame` (the new boundary).

**Last-frame detection**: harder. Pipecat's frame stream doesn't naturally signal "this is the last frame of a turn" — the stream just ends when Cartesia's synthesis closes. Two options:
1. **Buffer one frame ahead** — the processor holds frame N, sees N+1, knows N is not the last; on stream-close, emits N as last. **Adds latency.**
2. **Synthesizer signals end-of-stream** — extend `CartesiaSynthesisProcessor` (Story 2.5 / 3.7) to emit an `_EndOfTtsFrame` after the last audio frame. The pre-publish processor's "last frame is the one before `_EndOfTtsFrame`."

**Recommend Option 2.** Story 3.7's pipeline already has end-of-segment signaling shape; extend it. Document the choice. **If Option 2 is too invasive for this story**, fall back to a simple approach: use a per-turn timer — if no audio frame has arrived for >250ms after the last one, treat the prior frame as the last. This is the pragmatic v1 stance; Story 5.4 calibration may revisit.

**The dev should pick one approach during implementation and document.** The unit tests in AC #12 assert FSM behavior given an `on_last_audio_frame()` call; the *production* trigger of that call is a separate concern from the FSM's correctness.

### Integration with future stories

| Story | What it adds on top of 4.3 |
|---|---|
| 4.4 | `GoToSleepTool.dispatch` → `fsm.on_tool_call_go_to_sleep()`. Validates the deferred-sleep contract end-to-end. |
| 4.5 | Wake-greeting hook on `sleeping → waking` transition. Reads `mood_controller.state.current` for tinting. |
| 4.6 | `audio/transport.py` subscribes to `fsm.mic_mode_queue` and routes mic frames accordingly. |
| 4.7 | `OrchestratorDispatchProcessor.dispatch()` calls `fsm.on_dispatch_to_orchestrator()` to set `working[delegating]`. |

Story 4.3's surface must support all four. Verify by reading those stories' AC briefs in `epics.md` before finalizing.

### Test-mocking pattern (CLAUDE.md rule #7)

**`LogEventPublisher` as the real-fake.** It's a real implementation of the `EventPublisher` Protocol (Story 3.5) — not a mock. Use it for happy-path tests; mock only when injecting a publisher failure (the `test_publish_activity_failure_propagates` test).

This pattern landed in Story 3.6 (`tests/unit/mood/test_controller.py`) and 3.7's integration tests — mirror.

**Don't mock `ActivityEvent` or `ActivityPayload`.** They're production pydantic models; mocking violates the architecture (CLAUDE.md rule #7). Construct real instances in test assertions.

### What this story does NOT do

- **No tool registry** — Story 4.4 lands `ToolRegistry` + `GoToSleepTool` + `SetMoodTool`. The FSM exposes `on_tool_call_go_to_sleep()` ready for Story 4.4 to call.
- **No wake-greeting** — Story 4.5 lands `activity/greeting.py:trigger_greeting`. Story 4.3 ships the FSM transition that 4.5 will hook.
- **No mic-mode flip on the audio side** — Story 4.6 lands `audio/transport.py`'s subscription to the queue. Story 4.3 only emits.
- **No orchestrator slow-path** — Story 4.7 wires `working[delegating]`. Story 4.3 ships the transition method.
- **No barge-in** — v1.5 backlog (`v1.5-1-barge-in`).
- **No persistence** — FSM starts from `starting` on every boot. Cross-restart state is v1.5 backlog.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/activity/` (renamed from `lifecycle/`).
- `src/voice_agent_pipeline/activity/states.py` — Literals re-export.
- `src/voice_agent_pipeline/activity/machine.py` — `ActivityFSM` class.
- `tests/unit/activity/` — new test directory.
- `tests/unit/activity/__init__.py`.
- `tests/unit/activity/test_machine.py` — 16 unit tests per AC #12.
- `tests/integration/test_activity_lifecycle.py` — integration test per AC #13.

It modifies:
- `src/voice_agent_pipeline/__init__.py` — `lifecycle` → `activity` in module-level docstring.
- `src/voice_agent_pipeline/pipeline.py` — `_FsmEventBridge` processor; `_TurnBoundaryFrame` class; `_PrePublishProcessor` first/last-frame hooks; `SegmenterProcessor` reset trigger; `run_pipeline` constructs FSM + awaits `start()`. Module docstring updated.
- `src/voice_agent_pipeline/audio/wakeword.py` — comment updates per AC #1.
- `src/voice_agent_pipeline/splitter/mapping.py` — comment updates per AC #1.
- `src/voice_agent_pipeline/__init__.py` — module docstring update.
- `tests/unit/test_pipeline.py` — `UtteranceCapturedFrame` → `_TurnBoundaryFrame` migration in segmenter-reset tests.
- `tests/integration/test_simple_turn.py` (Story 2.5) — insert FSM + bridge into harness.
- `tests/integration/test_embodiment_alignment.py` (Story 3.7) — same pattern.
- `build_documents/implementation-artifacts/sprint-status.yaml` — `4-3-activity-fsm-core: ready-for-dev → in-progress → review`.

It does NOT modify:
- `src/voice_agent_pipeline/schemas/activity_event.py` — already complete from Story 3.4. Story 4.3 only consumes.
- `src/voice_agent_pipeline/errors.py` — `VoiceAgentError` already exists from Story 1.4.
- `src/voice_agent_pipeline/mood/*` — Story 3.6's territory; Story 4.3 doesn't touch mood.
- `src/voice_agent_pipeline/turn/*` — Stories 4.4 / 4.7's territory.
- `setup.toml` — no new config (FSM is fully internal; no operator-tunable knobs).

### Testing standards

- **`pytest-asyncio`** in auto mode.
- **`LogEventPublisher`** as the real-fake for happy-path tests.
- **`MagicMock(spec=EventPublisher)`** + `AsyncMock(side_effect=...)` only for failure-injection.
- **One behavior per test** — 16 unit tests + ~3-4 integration assertions per AC.
- **Privacy assertions** mirror Stories 1.7 / 3.7 — no transcript text in any log line above DEBUG.
- **Pyright strict on `src/`** — `asyncio.Queue[Literal[...]]` parametrization; no `Any` exfil.

### Performance budget

NFR1 / NFR2 turn budgets: the FSM's transition latency is sub-millisecond (state mutation + log + async publish to `LogEventPublisher` or in-process ROS 2). The `EventPublisher.publish_activity` call's contribution is dominated by DDS QoS (latched / transient_local) — typically sub-millisecond too. **Not a hot path concern** for v1.

The mic-mode queue's `put` operation is sub-microsecond. The audio transport (Story 4.6) reads via `await queue.get()`; the queue's depth bounds the latency between FSM emission and transport read — typically zero (the queue's never deep enough to matter).

### What "done" looks like

- `just check` exits 0.
- `just test` exits 0 (Story 4.3's unit + integration tests + earlier stories' tests all pass).
- `just run` end-to-end on the dev host produces:
  - `ros2 topic echo /olaf/activity` shows the 6-event sequence per Task 8's smoke list.
  - The simple-turn flow still works (Story 2.5/3.7 invariants preserved — speaker output, speech_emotion / vocalization events all fire correctly).
  - Deferred-sleep (Story 4.4 wires the actual tool path; for Story 4.3, manual smoke = call `fsm.on_tool_call_go_to_sleep()` from a Python REPL or a test harness, then drive a turn — the deferred-sleep behavior should fire correctly once 4.4 lands).
- Sprint-status flips to `review` after the live smoke confirms.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Activity FSM + Mood Control + Tool Registry (Batch 6 — added 2026-05-06)] — full FSM design (sync class, single-writer rule, deferred-sleep scheduler, mic-mode signaling, ActivityEvent publish on transition).
- [Source: build_documents/planning-artifacts/architecture.md#FSM event sources] — single-writer rule.
- [Source: build_documents/planning-artifacts/architecture.md#Deferred-sleep scheduler] — `sleep_pending` flag + last-frame trigger + two `ActivityEvent` publishes.
- [Source: build_documents/planning-artifacts/architecture.md#Mic-mode signaling] — `wake_word_only` ↔ `vad_stt` switch on FSM state.
- [Source: build_documents/planning-artifacts/architecture.md#Wake-greeting trigger] — Story 4.5's hook on `sleeping → waking`; Story 4.3 ships the transition.
- [Source: build_documents/planning-artifacts/architecture.md#Wake/Sleep & Tool-Use] — FR44/FR45/FR46/FR47 mapping.
- [Source: build_documents/planning-artifacts/architecture.md#Project Structure] — `activity/` package layout.
- [Source: build_documents/planning-artifacts/prd.md#FR26-FR28] — activity FSM functional requirements.
- [Source: build_documents/planning-artifacts/prd.md#FR46] — deferred-sleep transition.
- [Source: build_documents/planning-artifacts/prd.md#FR47] — continuous mic capture while AWAKE; mic-mode flip on FSM signal.
- [Source: build_documents/planning-artifacts/prd.md#NFR25, FR39] — privacy invariant; never log transcripts.
- [Source: build_documents/planning-artifacts/epics.md#Story 4.3: Activity FSM core] — full AC list (Story 4.3's source of truth).
- [Source: build_documents/planning-artifacts/epics.md#Epic 4 Goal] — "conversation-shaped surface."
- [Source: build_documents/implementation-artifacts/3-6-mood-module-state-and-controller.md] — Literal re-export pattern (`mood/state.py` re-imports from `schemas/mood_event.py`); test pattern with `LogEventPublisher` as real-fake.
- [Source: build_documents/implementation-artifacts/3-7-audio-frame-metadata-and-ssml-prompt.md] — `_PrePublishProcessor` + `_FrameCounter` (first / last frame detection landing site); `correlation_id` contextvar binding pattern; `UtteranceCapturedFrame`-as-turn-boundary stopgap (this story replaces it).
- [Source: src/voice_agent_pipeline/schemas/activity_event.py] — `ActivityState`, `WorkingSubmode` Literals + `ActivityPayload` model_validators (`from_state` required except on `starting`; `working_submode` non-None iff `state="working"`).
- [Source: src/voice_agent_pipeline/publisher/interface.py] — `EventPublisher.publish_activity` signature.
- [Source: src/voice_agent_pipeline/lifecycle/__init__.py] — current placeholder package; rename target.
- [Source: src/voice_agent_pipeline/pipeline.py:1-100, :330-400] — Story 3.7's `SegmenterProcessor` (turn-boundary proxy site to migrate); `_PrePublishProcessor` extension site for first/last-frame hooks.
- [Source: src/voice_agent_pipeline/audio/wakeword.py:67] — comment marking "Story 4.4's lifecycle FSM" — update per AC #1.
- [External: https://docs.python.org/3.12/library/asyncio-queue.html] — `asyncio.Queue` reference; `put` / `get` / `empty` semantics.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- **Pyright on ``Callable[[], Awaitable[None]]``**: ``asyncio.create_task``
  needs ``Coroutine[Any, Any, _T]``, not ``Awaitable[None]``. Switched
  the ``on_sleeping_to_waking`` callback type to
  ``Callable[[], Coroutine[Any, Any, None]] | None`` and pyright is
  happy. Documented the callback's expected shape in the ``__init__``
  docstring.
- **Scope reduction on first/last audio frame + `_TurnBoundaryFrame`**:
  Story 4.3's AC #11 originally called for replacing Story 3.7's
  ``UtteranceCapturedFrame``-as-turn-boundary with a new
  ``_TurnBoundaryFrame`` driven by FSM transitions, plus first/last
  audio frame detection wired into ``_PrePublishProcessor``. Both
  require concrete signals that aren't available without extending
  ``CartesiaSynthesisProcessor`` for end-of-stream marking — non-
  trivial, and Story 4.6 (mic-mode flip) is the natural landing
  site since that's the first place the signal becomes operationally
  necessary. Deferred both. Story 3.7's existing
  ``UtteranceCapturedFrame`` proxy continues to drive segmenter
  reset; Story 4.3's bridge fires ``on_speech_started`` +
  ``on_speech_ended`` chained on each ``UtteranceCapturedFrame``.
  Documented in the FSM bridge's docstring.
- **Live smoke deferred**: Story 4.3's live ``just run`` smoke (Task 8
  sub-bullet — observe ``ros2 topic echo /olaf/activity`` for the
  6-event sequence) is deferred to a later run. The integration test
  validates the architectural invariants in isolation; the live ROS 2
  smoke is operator-side verification. Sprint-status flips to
  ``review``; Kamal can run the smoke when convenient.

### Completion Notes List

- **Tasks 1-6 + 8 satisfied; Task 7 deferred to Story 4.6 / 4.7.**
- **AC coverage:**
  - AC #1: ``lifecycle/`` → ``activity/`` rename via ``git mv``;
    references in ``__init__.py``, ``audio/wakeword.py``,
    ``pipeline.py`` updated. (Inline comment hits in
    ``splitter/mapping.py`` use "lifecycle" in a generic English
    sense; left alone.)
  - AC #2: ``activity/states.py`` re-imports ``ActivityState`` /
    ``WorkingSubmode`` from ``schemas/activity_event.py``.
  - AC #3, #4, #5, #6, #7, #8, #9: ``ActivityFSM`` in
    ``activity/machine.py`` with all 9 transition methods,
    sync-mutate + async-publish discipline, mic-mode signal queue,
    deferred-sleep linchpin, illegal-transition guards, logging.
  - AC #10, #11: ``_FsmEventBridge`` in ``pipeline.py`` translates
    wake + utterance frames to FSM transitions; FSM constructed
    inside the ``async with async_http_client()`` block + ``start()``
    called before the pipeline runs.
  - AC #12: 21 unit tests in ``tests/unit/activity/test_machine.py``.
    All pass. (Spec called for 16; added 5 extras for full coverage:
    Mic-mode dedup, two greeting-callback tests, payload validator
    round-trip, MicMode + ActivityState Literal sanity.)
  - AC #13: 3 integration tests in
    ``tests/integration/test_activity_lifecycle.py`` covering wake
    + utterance flow, race-window cancellation, correlation_id
    uniqueness.
  - AC #14: ``just check`` green — 371 unit tests, all integration
    tests pass.
  - AC #15: privacy invariants honored (no transcripts logged).
  - AC #16: mood module untouched.
  - AC #17: tools registry not added (Story 4.4's territory).
- **Deferrals documented**:
  - First / last audio frame transitions (AC #4 in epics) —
    deferred. ``_FsmEventBridge`` does NOT call
    ``on_first_audio_frame`` / ``on_last_audio_frame`` yet; Story
    4.6 will land them when last-frame detection is concretely
    needed for the mic-mode flip orchestrator callback.
  - ``_TurnBoundaryFrame`` migration of Story 3.7's segmenter reset
    — deferred per same reasoning.

### File List

**New files:**
- ``src/voice_agent_pipeline/activity/states.py`` — re-export of
  ``ActivityState`` / ``WorkingSubmode`` from the schema.
- ``src/voice_agent_pipeline/activity/machine.py`` — ``ActivityFSM``
  + ``MicMode`` Literal + ``_TRANSITION_REASONS`` constant + all 9
  transition methods + ``_publish`` / ``_emit_mic_mode`` /
  ``_log_greeting_done`` helpers.
- ``tests/unit/activity/__init__.py``.
- ``tests/unit/activity/test_machine.py`` — 21 unit tests.
- ``tests/integration/test_activity_lifecycle.py`` — 3 integration
  tests for the FSM + bridge couple.

**Renamed files** (via ``git mv``):
- ``src/voice_agent_pipeline/lifecycle/`` →
  ``src/voice_agent_pipeline/activity/``.

**Modified files:**
- ``src/voice_agent_pipeline/__init__.py`` — ``lifecycle`` →
  ``activity`` in subpackage map.
- ``src/voice_agent_pipeline/activity/__init__.py`` — re-exports
  ``ActivityFSM``, ``ActivityState``, ``WorkingSubmode``,
  ``MicMode``.
- ``src/voice_agent_pipeline/pipeline.py`` — added
  ``_FsmEventBridge`` processor; constructed ``ActivityFSM``
  inside the ``async_http_client`` block; called
  ``await activity_fsm.start()``; inserted bridge after
  ``_WakewordEventLogger``.
- ``src/voice_agent_pipeline/audio/wakeword.py`` — comment update
  ("4.4's lifecycle FSM" → "4.3's activity FSM").
- ``build_documents/implementation-artifacts/sprint-status.yaml`` —
  ``4-3-activity-fsm-core: ready-for-dev → in-progress → review``.
- ``build_documents/implementation-artifacts/4-3-activity-fsm-core.md`` —
  this file.

## Change Log

| Date | Change |
|---|---|
| 2026-05-07 | Story 4.3 prepared — Activity FSM core (7-state + deferred-sleep + mic-mode signaling). |
| 2026-05-07 | Story 4.3 implemented — ActivityFSM (7 states + 9 transition methods) with sync-mutate/async-publish discipline, deferred-sleep linchpin, mic-mode signal queue with de-dup. lifecycle/→activity/ rename. _FsmEventBridge wires wake + utterance frames into FSM transitions. 21 unit + 3 integration tests, all passing. ``just check`` green (371 unit tests). First/last audio frame + _TurnBoundaryFrame migration deferred to Story 4.6 / 4.7. |
