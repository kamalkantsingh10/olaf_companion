# Story 4.6: Mic-mode flip — `audio/transport` consumes FSM mic-mode signal (FR47)

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want `audio/transport.py` to subscribe to the `ActivityFSM` mic-mode signal queue and route the single mic stream between `wake_word_only` (Porcupine engaged, VAD/STT suspended) and `vad_stt` (VAD + STT engaged, Porcupine suspended) modes,
so that wake-word fires only on `sleeping → waking` (not on every turn) and follow-up turns flow without re-prompting (FR47, continuous conversation while AWAKE).

## Acceptance Criteria

1. **`MicMode` Literal lives at `voice_agent_pipeline.activity.machine`** (already declared in Story 4.3 as `MicMode = Literal["wake_word_only", "vad_stt"]`). Story 4.6 imports and consumes it. Re-export from `audio/__init__.py` for caller ergonomics if the dev finds it useful.

2. **`MicModeRouter(FrameProcessor)` — new Pipecat processor** placed BEFORE `WakewordProcessor` and `VadProcessor` in the pipeline list. Owns the mic-mode state and **stamps every `AudioRawFrame`** with the current mode so downstream processors gate on the stamp.
   - Class shape (in a new file `src/voice_agent_pipeline/audio/mic_mode.py` — keeps the audio package's domain-by-component layout coherent):
     ```python
     @dataclass
     class _ModeStampedAudioFrame(AudioRawFrame):
         """AudioRawFrame stamped with the active mic mode (Story 4.6).

         Wakeword and VAD processors check this stamp before processing.
         The single-stream invariant (FR47) is enforced by the stamp:
         exactly one downstream processor consumes each frame.
         """
         mic_mode: MicMode = "wake_word_only"

     class MicModeRouter(FrameProcessor):
         def __init__(self, mic_mode_queue: asyncio.Queue[MicMode]) -> None:
             super().__init__()  # pyright: ignore[reportUnknownMemberType]
             self._queue = mic_mode_queue
             self._mic_mode: MicMode = "wake_word_only"  # default before FSM start()
             self._signal_task: asyncio.Task[None] | None = None
             self._on_mode_change: Callable[[MicMode, MicMode], Awaitable[None]] | None = None
     ```
   - **`setup()`** Pipecat lifecycle hook: kick off the background signal-consumer task `self._signal_task = asyncio.create_task(self._consume_signals())`. Mirror Story 1.6 / 1.7's `setup()` patterns.
   - **`cleanup()`**: cancel the signal task; await its cancellation.
   - **`_consume_signals()`** background loop:
     ```python
     async def _consume_signals(self) -> None:
         while True:
             try:
                 new_mode = await self._queue.get()
             except asyncio.CancelledError:
                 break
             old_mode = self._mic_mode
             if old_mode == new_mode:
                 continue  # idempotent — Story 4.3's de-dup invariant should prevent this, defensive
             self._mic_mode = new_mode
             log.info("mic_mode.transition", from_mode=old_mode, to_mode=new_mode)
             if self._on_mode_change is not None:
                 await self._on_mode_change(old_mode, new_mode)
     ```
   - **`process_frame(frame, direction)`**: on `AudioRawFrame` (and only the base type, not its subclasses — see "buffer-clear hook" below): wrap in `_ModeStampedAudioFrame` carrying current `_mic_mode`; push downstream. **Pass-through for all non-audio frames** (Pipecat's contract).
   - **`set_on_mode_change(callback)`** method that the pipeline-assembly site calls after construction to inject the buffer-clear callback (AC #5). Keeps `MicModeRouter` decoupled from the wakeword/VAD processors directly.

3. **Default starting mode = `"wake_word_only"`.** The FSM's `start()` (Story 4.3) emits `"wake_word_only"` immediately on `starting → sleeping`; the router's default just matches what would land. **No race** in practice because:
   - Pipeline assembly: `activity_fsm = ActivityFSM(...)` constructs first; `await fsm.start()` queues the first mic-mode signal; `MicModeRouter` consumes it after `setup()` runs.
   - The pipeline starts processing audio frames AFTER both `setup()` calls complete.
   - **If the queue has the signal already enqueued before the router starts consuming** (race window during startup), the consumer task drains it on first iteration. No frames are lost (the router defaults to `"wake_word_only"` in the meantime, which is the correct startup posture).

4. **`WakewordProcessor.process_frame` updates** to gate on the mode stamp (AC: from epics):
   - Open `src/voice_agent_pipeline/audio/wakeword.py` (Story 1.6 baseline).
   - Replace the `if isinstance(frame, AudioRawFrame) and self._porcupine is not None:` check with:
     ```python
     if (isinstance(frame, _ModeStampedAudioFrame)
             and frame.mic_mode == "wake_word_only"
             and self._porcupine is not None):
         # ... existing buffer + Porcupine.process logic stays unchanged ...
     ```
   - **Crucially**: do NOT just check `isinstance(frame, AudioRawFrame)` — that would still match the stamped subclass. Use the `mic_mode` field as the discriminator. **Pyright concern**: `isinstance(frame, _ModeStampedAudioFrame)` narrows; `frame.mic_mode` access is then type-safe.
   - **Why two stages instead of one router-driven gate**: the alternative — having `MicModeRouter` DROP frames not destined for the current mode's consumer — fails because Pipecat's pipeline is linear; both `WakewordProcessor` and `VadProcessor` are *downstream* of the router, in series. A drop kills frames for both. The stamp-then-self-gate pattern lets each consumer make its own decision.
   - **Frame still flows downstream** (`await self.push_frame(frame, direction)` at the end of `process_frame`) — so VAD also sees the stamped frame and applies its own gate (AC #5).

5. **`VadProcessor.process_frame` updates**:
   - Open `src/voice_agent_pipeline/audio/vad.py` (Story 1.7 baseline).
   - Replace the `elif isinstance(frame, AudioRawFrame) and self._active and self._silero is not None:` check with:
     ```python
     elif (isinstance(frame, _ModeStampedAudioFrame)
             and frame.mic_mode == "vad_stt"
             and self._active
             and self._silero is not None):
         self._consume_audio(frame.audio, direction)
         await self._maybe_emit_utterance(direction)
     ```
   - **`WakeWordDetectedFrame` activation** (`self._active = True` on `_activate()`) stays as-is — no mode-gate change there. The wake-detection frame fires once on `sleeping → waking` (per FR47); the FSM emits the mode signal `"vad_stt"` simultaneously; from then on, audio frames have `mic_mode="vad_stt"` and VAD consumes them.
   - **`STTProcessor`** (Story 1.7): does NOT see `AudioRawFrame`s directly — it consumes `UtteranceCapturedFrame`s emitted by `VadProcessor`. **No update needed.** When VAD doesn't run (wake_word_only mode), no `UtteranceCapturedFrame`s are emitted, STT is implicitly idle.

6. **Buffer-clear / state-reset on mode transitions** (AC: from epics):
   - **`wake_word_only → vad_stt`** (FSM enters `waking` after wake-word fires):
     - **Porcupine's internal buffer cleared**: `WakewordProcessor` exposes a method `clear_buffer()` → `self._buffer.clear()`. Called by the mode-change callback. Prevents stale buffered audio from after-wake leaking into the next wake-word check (irrelevant in single-shot wake mode but defensive against bugs).
     - **VAD/STT reset to clean starting state**: `VadProcessor` exposes `reset_state()` → calls `self._activate(time.time_ns())` essentially (drop existing `_active` state, clear buffers, set `_active=True` because the user is about to speak after wake). Actually more nuanced: VAD's `_active` is set on `WakeWordDetectedFrame`, which fires concurrently with the mode flip. **Simplest**: `reset_state()` clears `_utterance_buffer`, `_vad_frame_buffer`, `_silence_run_ms`, sets `_speech_seen=False`. Don't touch `_active` — let `WakeWordDetectedFrame` set it. Document in the method's docstring.
   - **`vad_stt → wake_word_only`** (FSM enters `sleeping` after deferred-sleep complete):
     - **In-flight VAD detection state dropped**: `VadProcessor.reset_state()` (same method) clears all buffers + `_active=False`.
     - **STT's transcription buffer cleared**: `SttProcessor` (Story 1.7) — if it has any per-utterance state, clear via `STTProcessor.reset_state()`. **Most likely**: `SttProcessor` is stateless between utterances (it processes one `UtteranceCapturedFrame` at a time). **Verify** by reading `pipeline.py:103-..._SttResultLogger` and `STTProcessor` (which lives in `pipeline.py` per Story 1.7); if no state, no `reset_state()` needed. **Document the finding**.
     - **Porcupine re-engaged on subsequent frames**: no method call needed — Porcupine processes frames whenever `mic_mode == "wake_word_only"`, which is now true. The next `_ModeStampedAudioFrame` lands, Porcupine processes.
   - **Mode-change callback orchestrator** (in `pipeline.py:run_pipeline`):
     ```python
     async def _on_mic_mode_change(old: MicMode, new: MicMode) -> None:
         if new == "vad_stt":
             # wake_word_only → vad_stt
             await asyncio.to_thread(wakeword_processor.clear_buffer)
             vad_processor.reset_state()  # sync; VAD state is in-process
         elif new == "wake_word_only":
             # vad_stt → wake_word_only
             vad_processor.reset_state()
             # STT processor reset if it has state — verify & call
     mic_mode_router.set_on_mode_change(_on_mic_mode_change)
     ```
     **Reasoning**: the orchestration lives at the pipeline-assembly site because it requires references to multiple processors. Keeping it out of `MicModeRouter` keeps the router single-purpose.

7. **`going_to_sleep` mic-mode behavior** (per epics.md AC):
   - "When [the FSM] enters `going_to_sleep`, mic mode stays at `vad_stt` (so a follow-up wake-word from the user could in theory cancel — though edge case)."
   - **Story 4.6 implementation**: this is an FSM-side concern (Story 4.3 already covers it). Story 4.3's `_emit_mic_mode` only fires on actual mode changes; transitioning into `going_to_sleep` does NOT emit a new signal because the previous mode (`vad_stt`) is the same. **Verify** Story 4.3's `_emit_mic_mode` de-dup invariant covers this — re-read Story 4.3's AC #7. If Story 4.3 doesn't cover it correctly, Story 4.6 is the rallying point that flags the gap. **Recommend** adding a unit test in Story 4.6's test file that drives the FSM through `speaking → going_to_sleep → sleeping` and asserts the mic-mode queue receives only `["wake_word_only"]` (one entry, fired on the `sleeping` transition). If Story 4.3's de-dup is broken, fix it in this story (small Story-4.3 addendum noted in the dev record).

8. **`MicModeRouter` + buffer-clear callback wiring in `pipeline.py:run_pipeline`** (AC: from epics + Story 4.6 plumbing):
   - Construct `mic_mode_router = MicModeRouter(activity_fsm.mic_mode_queue)`.
   - Define the orchestrator callback per AC #6's pseudocode and pass via `mic_mode_router.set_on_mode_change(_on_mic_mode_change)`.
   - **Insert in pipeline list** BEFORE `WakewordProcessor`:
     ```
     transport.input()
       → MicModeRouter                               # NEW (Story 4.6) — stamps audio
       → WakewordProcessor                           # gates on stamp
       → VadProcessor                                # gates on stamp
       → SttProcessor
       → _SttResultLogger
       → _WakewordEventLogger
       → _FsmEventBridge
       → TurnDispatchProcessor
       → _GreetingInjectorProcessor
       → SegmenterProcessor
       → CartesiaSynthesisProcessor
       → _FrameCounter
       → _PrePublishProcessor
       → transport.output()
     ```
   - Update `pipeline.py`'s module docstring with a Story 4.6 entry — mic-mode flip wired; Wakeword/VAD now consume `_ModeStampedAudioFrame`.

9. **Logging discipline** (NFR25, FR39):
   - INFO `mic_mode.transition` (in `MicModeRouter._consume_signals`) — fields: `from_mode`, `to_mode`. **Critical for live diagnostics** — operator can grep this in `voice-agent.log` to confirm the mic flips at the right moments.
   - DEBUG `mic_mode.frame_stamped` (per-frame, optional) — fields: `mic_mode`. **Skip this in v1** unless debugging — it's high-volume (every audio frame). The architecture doesn't ask for it; mention as future-debug toggle in the docstring.
   - INFO `mic_mode.buffer_cleared` (in the orchestrator callback) — fields: `from_mode`, `to_mode`, `processors_reset` (list of processor names that had `reset_state()` called). Helps debug "did the buffer clear actually fire?"
   - INFO `wakeword.buffer_cleared` (in `WakewordProcessor.clear_buffer`) — fields: `prior_buffer_bytes`. Lets the operator see how much audio was dropped on transition (should be small, <1KB typically).
   - **Never log** audio bytes or buffer contents. Standing privacy invariant; Story 4.6's logs only carry sizes / counts / mode literals.

10. **Unit tests in `tests/unit/audio/test_mic_mode.py`** (NEW). Cover:
    - **Helper fixture**: `make_router()` returns `(router, mic_mode_queue)` where `mic_mode_queue = asyncio.Queue()` and `router = MicModeRouter(mic_mode_queue)`. Drive `router.setup(...)` manually for the consumer task.
    - `test_default_mic_mode_is_wake_word_only` — `router._mic_mode == "wake_word_only"` immediately after construction.
    - `test_consume_signals_updates_mic_mode` — `await mic_mode_queue.put("vad_stt")`; let the consumer task tick; assert `router._mic_mode == "vad_stt"`. **How to wait for the consumer task to tick**: `await asyncio.sleep(0)` is insufficient (returns control but doesn't guarantee the task ran); use `await asyncio.wait_for(asyncio.shield(asyncio.sleep(0.01)), timeout=0.1)` or an explicit `Event` in the consumer that the test waits on. **Recommend** adding a private `_signal_processed: asyncio.Event` to `MicModeRouter` that's set after each signal — tests await it for synchronization.
    - `test_consume_signals_logs_transition` — assert INFO `mic_mode.transition` with `from_mode`, `to_mode` fields.
    - `test_consume_signals_skips_idempotent_signals` — push the same mode twice; assert one transition log + one `_on_mode_change` callback invocation. (Defensive — Story 4.3's de-dup should prevent it from arriving twice in production.)
    - `test_process_frame_stamps_audio_with_current_mode` — drive the router with a plain `AudioRawFrame`; assert downstream receives a `_ModeStampedAudioFrame` with `mic_mode == "wake_word_only"`. Switch to `vad_stt`; drive again; assert the next stamp matches.
    - `test_process_frame_passes_non_audio_frames_through_unchanged` — drive the router with a `WakeWordDetectedFrame` or other non-audio Frame; assert downstream receives the same instance, no transformation.
    - `test_on_mode_change_callback_invoked_on_transition` — set a callback via `set_on_mode_change`; push a mode signal; assert the callback was awaited with `(old_mode, new_mode)` tuple.
    - `test_setup_and_cleanup_lifecycle` — call `setup()`; verify `_signal_task` is created. Call `cleanup()`; verify the task is cancelled cleanly + the consumer loop exits.
    - **Privacy assertion**: drive the router with audio frames carrying sentinel bytes; assert no log line contains the bytes.

11. **Updated unit tests for `WakewordProcessor`** in `tests/unit/audio/test_wakeword.py` (Story 1.6 baseline):
    - **Update existing tests** to drive `_ModeStampedAudioFrame(audio=..., mic_mode="wake_word_only")` instead of plain `AudioRawFrame`. The expected behavior (Porcupine processing the chunks) stays.
    - `test_wakeword_skips_when_mic_mode_is_vad_stt` — drive `_ModeStampedAudioFrame(audio=..., mic_mode="vad_stt")`; assert Porcupine's `process` was NOT called (mock the Porcupine instance's `process` method); assert no `WakeWordDetectedFrame` emitted.
    - `test_clear_buffer_drops_pending_audio` — drive a few partial frames (less than `_frame_byte_size`); call `processor.clear_buffer()`; assert `processor._buffer == bytearray()`. Assert INFO `wakeword.buffer_cleared` with `prior_buffer_bytes` value.
    - `test_clear_buffer_when_empty_is_noop` — `clear_buffer()` on empty buffer; no error, log fires with `prior_buffer_bytes=0`.

12. **Updated unit tests for `VadProcessor`** in `tests/unit/audio/test_vad.py` (Story 1.7 baseline):
    - **Update existing tests** to drive `_ModeStampedAudioFrame(audio=..., mic_mode="vad_stt")` after `_active=True`. Existing VAD tests assume plain `AudioRawFrame`s flow; the gate update means tests must use stamped frames OR plain `AudioRawFrame`s with the gate temporarily relaxed for backwards-compat (NOT recommended — clean break).
    - `test_vad_skips_when_mic_mode_is_wake_word_only` — `_active=True`; drive `_ModeStampedAudioFrame(audio=..., mic_mode="wake_word_only")`; assert no Silero processing, no `_consume_audio` call.
    - `test_reset_state_clears_buffers_and_speech_flag` — populate `_utterance_buffer`, `_vad_frame_buffer`, `_silence_run_ms`, `_speech_seen` with non-default values; call `processor.reset_state()`; assert all four fields back to defaults; `_active` unchanged (preserved per AC #6's note).
    - `test_reset_state_logs_event` — assert one INFO log when reset is called (some `vad.state_reset` event).

13. **Integration test `tests/integration/test_continuous_conversation.py`** (NEW; FR47 demonstration):
    - **Mock**: Cartesia (synthetic audio chunks); Talker's `complete_with_tools` (returns canned text + no tool calls for turn 1, returns text + `go_to_sleep` for turn 3); STT (returns canned transcripts for each turn).
    - **Real**: `ActivityFSM`, `MicModeRouter`, `WakewordProcessor`, `VadProcessor`, `LogEventPublisher`, `MoodController`, `_GreetingInjectorProcessor`, full splitter chain.
    - **Drive a multi-turn flow**:
      1. Pipeline starts; FSM is in `sleeping`; `mic_mode = "wake_word_only"`.
      2. Wake-word fires (simulate by driving a fake Porcupine detection through the test harness); FSM transitions `sleeping → waking → listening`; `mic_mode` flips to `"vad_stt"`.
      3. **Turn 1**: simulate audio frames carrying user speech; VAD/STT capture; `UtteranceCapturedFrame` flows; Talker responds; audio synthesizes; FSM `speaking → listening`. `mic_mode` STAYS `"vad_stt"`.
      4. **Turn 2**: simulate ANOTHER user utterance — **without** another wake-word firing. Assert: STT transcribes the second utterance; turn flow completes normally; `mic_mode` is still `"vad_stt"` throughout.
      5. **Intent-sleep**: Turn 3's Talker reply triggers `go_to_sleep`; FSM does deferred-sleep on last audio frame; `mic_mode` flips to `"wake_word_only"`.
    - **Assertions** (per epics.md AC):
      - Turn 2's transcript is captured WITHOUT a second wake-word fire (assert `WakewordProcessor.process` was NOT called between Turn 1 and Turn 2 — track via Porcupine mock).
      - The activity FSM stays in `listening` between turns (no return to `sleeping` until intent-sleep). Assert by inspecting `fsm.current_state` mid-flow.
      - Only at the very end (Talker `go_to_sleep`) does the FSM return to `sleeping` and `mic_mode` flip back to `wake_word_only`.
    - **Privacy assertion** mirroring earlier integration tests.

14. **`just check` stays green.** Updates required:
    - `tests/unit/audio/test_wakeword.py` — frame type change + new tests.
    - `tests/unit/audio/test_vad.py` — frame type change + new tests.
    - `tests/integration/test_simple_turn.py` (Story 2.5) — must now insert `MicModeRouter` into the harness, OR (recommended) bypass the router by driving `_ModeStampedAudioFrame(audio=..., mic_mode="vad_stt")` directly into the wakeword/VAD chain. **Recommend the latter** for existing tests — they're focused on Story 2.5's simple-turn shape, not Story 4.6's mode-flip; minimize churn.
    - `tests/integration/test_embodiment_alignment.py` (Story 3.7) — same.
    - `tests/integration/test_activity_lifecycle.py` (Story 4.3) — same. The FSM's mic-mode queue is exercised; the consumer test is now Story 4.6's; Story 4.3's tests can drain the queue directly.
    - `tests/integration/test_intent_sleep.py` (Story 4.4) — same.
    - `tests/integration/test_wake_greeting.py` (Story 4.5) — same.

15. **No transcripts / API keys / raw audio in any log** (NFR25, FR39). Story 4.6's logs only carry mode literals + buffer sizes + processor names. The redaction processor catches mistakes; Story 4.6's code shouldn't pass audio into log fields.

16. **Architecture compliance** — additional notes:
    - `MicModeRouter` lives in `audio/` package (per architecture.md §"Mic-mode signaling"). Does NOT live in `activity/` — that package owns FSM logic, not Pipecat plumbing. Mirrors Story 4.5's `_GreetingInjectorProcessor` placement (in `pipeline.py` because it's pipeline plumbing).
    - **Decision: where to put `MicModeRouter`?** Architecture docs put `transport.py` as the consumer ("audio transport routes the mic stream"). But `audio/transport.py` is a *builder* function, not a class. Two options:
      - (a) Put `MicModeRouter` in `audio/transport.py` as a sibling class (extends the file's scope).
      - (b) Put `MicModeRouter` in `audio/mic_mode.py` (new file; keeps `transport.py` minimal).
    - **Recommend (b)** — keeps the transport builder narrow; `mic_mode.py` is a domain-by-component fit (architecture.md §"Module-by-domain layout"). Document the choice in the dev record.
    - The `_ModeStampedAudioFrame` subclass placement: same file as `MicModeRouter` (new `audio/mic_mode.py`). Re-export from `audio/__init__.py` if needed by Wakeword/VAD via direct import.

## Tasks / Subtasks

- [ ] **Task 1: `MicModeRouter` + `_ModeStampedAudioFrame` in `audio/mic_mode.py`** (AC: #1, #2, #3, #9)
  - [ ] Create `src/voice_agent_pipeline/audio/mic_mode.py`. Module docstring per `feedback_code_comments.md` — explain: single-stream invariant (FR47), frame stamping vs filtering trade-off, lifecycle bound to Pipecat's `setup`/`cleanup`.
  - [ ] Imports: `import asyncio`, `import structlog`, `from collections.abc import Awaitable, Callable`, `from dataclasses import dataclass`, `from pipecat.frames.frames import AudioRawFrame, Frame`, `from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup`, `from voice_agent_pipeline.activity.machine import MicMode`.
  - [ ] Define `_ModeStampedAudioFrame(AudioRawFrame)` dataclass with `mic_mode: MicMode = "wake_word_only"`. **NOT frozen** (matches Pipecat's `Frame` non-frozen base — see `audio/wakeword.py:51-54` for the precedent comment).
  - [ ] Define `MicModeRouter` per AC #2 with all hooks: `__init__`, `setup`, `cleanup`, `process_frame`, `set_on_mode_change`, `_consume_signals`, optional `_signal_processed: asyncio.Event` for test sync.
  - [ ] Re-export `MicModeRouter`, `_ModeStampedAudioFrame` from `audio/__init__.py`.
  - [ ] **Pyright-strict** check: `Callable[[MicMode, MicMode], Awaitable[None]] | None`; `asyncio.Queue[MicMode]` parametrization.

- [ ] **Task 2: `WakewordProcessor` mic-mode gate + `clear_buffer` method** (AC: #4, #6)
  - [ ] Open `src/voice_agent_pipeline/audio/wakeword.py` (Story 1.6 baseline).
  - [ ] Update import: `from voice_agent_pipeline.audio.mic_mode import _ModeStampedAudioFrame`. **Watch for import cycle** — if `mic_mode.py` imports from `wakeword.py`, the cycle is broken; if not, this is one-directional. Verify.
  - [ ] Update `process_frame`'s audio-handling branch per AC #4. The buffer + Porcupine logic stays unchanged.
  - [ ] Add `def clear_buffer(self) -> None:` method. Body: log INFO `wakeword.buffer_cleared` with `prior_buffer_bytes=len(self._buffer)`; `self._buffer.clear()`. Synchronous (no asyncio).

- [ ] **Task 3: `VadProcessor` mic-mode gate + `reset_state` method** (AC: #5, #6)
  - [ ] Open `src/voice_agent_pipeline/audio/vad.py` (Story 1.7 baseline).
  - [ ] Update import: `from voice_agent_pipeline.audio.mic_mode import _ModeStampedAudioFrame`.
  - [ ] Update `process_frame`'s audio-handling `elif` branch per AC #5.
  - [ ] Add `def reset_state(self) -> None:` method. Body: clear `_utterance_buffer`, `_vad_frame_buffer`, `_silence_run_ms = 0`, `_speech_seen = False`. **Do not** touch `_active` (preserved per AC #6 note). Log INFO `vad.state_reset` with the cleared field counts.

- [ ] **Task 4: `STTProcessor` reset (if needed)** (AC: #6)
  - [ ] Read Story 1.7's `STTProcessor` (lives in `pipeline.py:103-...`). Determine if it has per-utterance state.
  - [ ] **If stateless**: document in dev notes that `STTProcessor` is stateless between utterances; no reset needed. Skip the orchestrator callback's STT reset call.
  - [ ] **If stateful**: add a `reset_state()` method clearing the relevant fields; call from the orchestrator callback in AC #6.

- [ ] **Task 5: Pipeline-assembly wiring + buffer-clear orchestrator** (AC: #6, #8)
  - [ ] In `src/voice_agent_pipeline/pipeline.py:run_pipeline`:
    - After `activity_fsm = ActivityFSM(...)` (Story 4.3) and before constructing `WakewordProcessor` / `VadProcessor`:
      - `mic_mode_router = MicModeRouter(activity_fsm.mic_mode_queue)`.
    - Construct the orchestrator callback per AC #6's pseudocode. Capture `wakeword_processor`, `vad_processor`, `stt_processor` (if stateful).
    - Call `mic_mode_router.set_on_mode_change(_on_mic_mode_change)`.
    - Insert `mic_mode_router` in the pipeline list per AC #8's diagram.
  - [ ] Update `pipeline.py`'s module docstring with a Story 4.6 entry.

- [ ] **Task 6: Unit tests for `MicModeRouter`** (AC: #10)
  - [ ] Create `tests/unit/audio/test_mic_mode.py`. Module docstring per `feedback_code_comments.md`.
  - [ ] Implement the 9 test cases listed in AC #10.
  - [ ] **Test sync challenge**: use the `_signal_processed: asyncio.Event` field in `MicModeRouter` for synchronization. Tests `await router._signal_processed.wait()` after pushing a signal.

- [ ] **Task 7: Updated unit tests for `WakewordProcessor`** (AC: #11)
  - [ ] Open `tests/unit/audio/test_wakeword.py`. Update existing tests to drive `_ModeStampedAudioFrame` instead of `AudioRawFrame`. Add 3 new tests per AC #11.

- [ ] **Task 8: Updated unit tests for `VadProcessor`** (AC: #12)
  - [ ] Open `tests/unit/audio/test_vad.py`. Same pattern. Add 3 new tests per AC #12.

- [ ] **Task 9: Integration test for continuous conversation** (AC: #13)
  - [ ] Create `tests/integration/test_continuous_conversation.py`. Mirror Story 4.3 / 4.5's harness shape.
  - [ ] Multi-turn drive per AC #13: wake → turn 1 → turn 2 (no re-wake) → intent-sleep.
  - [ ] Assertions per AC #13's bullet list.
  - [ ] Privacy assertion mirroring earlier tests.

- [ ] **Task 10: Update earlier integration test harnesses** (AC: #14)
  - [ ] `tests/integration/test_simple_turn.py` (Story 2.5) — drive `_ModeStampedAudioFrame(audio=..., mic_mode="vad_stt")` directly; bypass the router for harness simplicity.
  - [ ] `tests/integration/test_embodiment_alignment.py` (Story 3.7) — same.
  - [ ] `tests/integration/test_activity_lifecycle.py` (Story 4.3) — drain `fsm.mic_mode_queue` directly in assertions; the FSM's mic-mode invariants stay covered by Story 4.3's tests.
  - [ ] `tests/integration/test_intent_sleep.py` (Story 4.4) — same.
  - [ ] `tests/integration/test_wake_greeting.py` (Story 4.5) — same.

- [ ] **Task 11: Pass `just check` + live smoke** (AC: #14)
  - [ ] `uv run pytest tests/unit/audio/ -v` — Story 1.6 + 1.7 + 4.6's unit tests all pass.
  - [ ] `uv run pytest tests/integration/test_continuous_conversation.py -v` — passes.
  - [ ] Full `just check` — green.
  - [ ] Full `just test` — all integration tests pass.
  - [ ] **Live smoke (manual)** — `just run` on the dev host. Speak:
    - "Hey OLAF" → expect wake greeting (Story 4.5).
    - Without another wake-word: "What time is it?" → expect Talker reply, NO second wake-word activation.
    - Without another wake-word: "Tell me a joke." → expect Talker reply, still no re-wake.
    - "Goodnight" → expect intent-sleep flow; mic-mode flips back; speaking "Hey OLAF" again should re-wake.
  - [ ] Watch `voice-agent.log` for `mic_mode.transition` events. Document the observed sequence in the commit message + dev record.

- [ ] **Task 12: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] Single commit covering: `audio/mic_mode.py` (new), `audio/__init__.py` (re-exports), `audio/wakeword.py` (gate + `clear_buffer`), `audio/vad.py` (gate + `reset_state`), possibly `pipeline.py:STTProcessor` (`reset_state` if stateful), `pipeline.py:run_pipeline` (router + orchestrator + pipeline list), unit + integration tests, harness updates for earlier stories, sprint-status flip.
  - [ ] Suggested commit message: `Story 4.6: mic-mode flip (audio/transport consumes FSM mic-mode signal — FR47)`.
  - [ ] `git push` immediately after.
  - [ ] Sprint-status: `4-6-mic-mode-flip: ready-for-dev → in-progress → review`.

## Dev Notes

### Architectural intent — Story 4.6's role in Epic 4

Story 4.6 closes the FR47 single-stream / continuous-conversation invariant. Before 4.6, `WakewordProcessor` and `VadProcessor` BOTH see every audio frame; they each have internal flags (`_porcupine` exists, `_active=True`) that control whether they actually process. **The risk**: a frame arriving in the wrong "phase" (e.g., user keeps talking after wake-word, but the FSM is in `working`) might be processed by both, or by neither, depending on flag-toggle race conditions.

After 4.6: every audio frame carries an explicit `mic_mode` stamp. Each processor checks its own gate. **Single-stream invariant is enforced at the type level** — no "both processors might process this frame" ambiguity.

**Why land 4.6 sixth in Epic 4** (per epics.md sequencing): 4.6 depends on Story 4.3's `mic_mode_queue` (the signal source). Story 4.5's wake greeting works without 4.6 (Talker → splitter → audio is independent of mic capture mode). The continuous-conversation flow (turn 2 without re-wake) is what 4.6 actually unlocks; user-facing "feels coherent" comes from this.

### Frame stamping vs frame filtering — the design choice

Architectural alternatives considered:
1. **Two routers** — one before `WakewordProcessor` (drops frames in `vad_stt` mode), one before `VadProcessor` (drops in `wake_word_only`). Both share a mode reference. **Con**: two processors instead of one; harder to reason about.
2. **Single router that DROPS frames** — drops audio frames not destined for the current mode's consumer. **Con**: Pipecat's pipeline is linear; dropping kills frames for both downstream processors. Doesn't work.
3. **Single router that STAMPS frames** (chosen) — wraps `AudioRawFrame` in `_ModeStampedAudioFrame`. Each downstream processor checks the stamp. **Pro**: single integration point; type-safe gate; Pipecat-idiomatic.
4. **Shared mutable mode reference + processors poll** — pure callback approach. **Con**: tight coupling; harder to test in isolation; mode-change atomicity is implicit.

Chose (3) because it's the only option where every audio frame is **type-tagged** with its destination intent. A future contributor reading `WakewordProcessor.process_frame` sees `if frame.mic_mode == "wake_word_only"` and immediately knows the gate; no need to chase down a shared state.

### Why `_ModeStampedAudioFrame` is internal (`_` prefix)

The frame type is an internal pipeline implementation detail — nothing outside the audio + activity packages should construct or pattern-match on it. The `_` prefix marks it as "do not import from outside this package boundary." If an external story (Story 5.1 barge-in) needs to interact, the dependency is documented and the prefix can be reconsidered.

### Buffer-clear orchestration — why at the pipeline-assembly site

The `MicModeRouter` doesn't import `WakewordProcessor` or `VadProcessor`. It exposes `set_on_mode_change(callback)` which the pipeline-assembly site fills in. **Why**: keeps `MicModeRouter` decoupled from specific consumer types — it's a generic "mode broadcaster," not coupled to the wakeword/VAD pair. A future Story 5.x adding (say) a barge-in detector could subscribe to the same callback without modifying `MicModeRouter`.

The trade-off: the pipeline-assembly site has a small orchestrator function. ~10 lines. Cost is minimal; clarity benefits substantial.

### Test-mocking pattern (CLAUDE.md rule #7)

Mock surfaces:
- `pvporcupine.create` / Porcupine instance — already mocked in `test_wakeword.py` (Story 1.6); reuse the pattern.
- Silero VAD — already mocked in `test_vad.py` (Story 1.7); reuse.
- `asyncio.Queue` — **don't mock**; use real `asyncio.Queue[MicMode]` instances.
- `ActivityFSM` — for the integration test, real FSM with `LogEventPublisher`. For unit tests of `MicModeRouter`, just inject a real `asyncio.Queue` directly (no FSM needed).

### Test synchronization — the consumer-task tick problem

`MicModeRouter._consume_signals` runs in an `asyncio.create_task`. When a test pushes to the queue, the consumer task may not run before the test's next assertion (asyncio scheduling is cooperative).

**Solution**: add a private `_signal_processed: asyncio.Event` field. The consumer sets it after each signal. Tests `await router._signal_processed.wait()` then `router._signal_processed.clear()` for the next iteration. This is deterministic and avoids `asyncio.sleep(arbitrary_value)` flakiness.

**Document this pattern** in the test file's module docstring — future test authors will copy it.

### Pipecat `AudioRawFrame` subclassing — verified pattern

Story 1.6's `WakeWordDetectedFrame(Frame)` subclasses Pipecat's `Frame` cleanly. Story 3.7's `EmbodimentAudioFrame(OutputAudioRawFrame)` subclasses an audio frame type. Story 4.6's `_ModeStampedAudioFrame(AudioRawFrame)` mirrors. **No subclassing surprise expected** — but verify by running a small probe in the dev shell:
```python
from pipecat.frames.frames import AudioRawFrame
from dataclasses import dataclass
@dataclass
class _Test(AudioRawFrame):
    mic_mode: str = "test"
f = _Test(audio=b"\x00", sample_rate=16000, num_channels=1, mic_mode="vad_stt")
print(f.mic_mode, f.audio[:1])
```
Story 3.7's dev record confirms `OutputAudioRawFrame` subclassing works; `AudioRawFrame` is in the same dataclass family.

### What this story does NOT do

- **No barge-in mechanics** — VAD-during-`speaking` flush is v1.5 backlog (`v1.5-1-barge-in`). Story 4.6's mic-mode flip is the *prerequisite* (the routing is in place); barge-in adds the flush logic on top.
- **No new wake-word phrase** — Story 5.5 territory.
- **No mic device-pinning changes** — Story 1.5's `audio/transport.py` device handling stays.
- **No VAD parameter retuning** — Story 5.4 / 5.5 calibration territory.
- **No echo cancellation / AEC** — out of scope; the speaker output and mic input pipeline don't have AEC in v1 (NFR-related deferral).

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/audio/mic_mode.py` — `MicModeRouter` + `_ModeStampedAudioFrame`.
- `tests/unit/audio/test_mic_mode.py` — 9 unit tests.
- `tests/integration/test_continuous_conversation.py` — multi-turn FR47 demonstration.

It modifies:
- `src/voice_agent_pipeline/audio/wakeword.py` — `process_frame` mode gate + `clear_buffer` method.
- `src/voice_agent_pipeline/audio/vad.py` — `process_frame` mode gate + `reset_state` method.
- `src/voice_agent_pipeline/audio/__init__.py` — re-export `MicModeRouter` if useful.
- `src/voice_agent_pipeline/pipeline.py` — Construct `MicModeRouter`; orchestrator callback; pipeline list extended; possibly `STTProcessor.reset_state()`.
- `tests/unit/audio/test_wakeword.py` — frame type updates + 3 new tests.
- `tests/unit/audio/test_vad.py` — same pattern + 3 new tests.
- `tests/integration/test_simple_turn.py` (2.5) / `test_embodiment_alignment.py` (3.7) / `test_activity_lifecycle.py` (4.3) / `test_intent_sleep.py` (4.4) / `test_wake_greeting.py` (4.5) — harness updates per AC #14.
- `build_documents/implementation-artifacts/sprint-status.yaml` — story status flip.

It does NOT modify:
- `src/voice_agent_pipeline/audio/transport.py` — Story 1.5/2.1's `build_audio_transport` builder. The router is a separate class.
- `src/voice_agent_pipeline/activity/machine.py` — Story 4.3's FSM. The mic-mode signal source is unchanged.
- `setup.toml` — no new config (single-stream is architectural; not operator-tunable).

### Testing standards

- **`pytest-asyncio`** in auto mode.
- **Real `asyncio.Queue` instances** for the router; mocks only at Pipecat-frame-class boundaries (mock `Porcupine.process` to assert NOT called; mock Silero similarly).
- **One behavior per test** — 9 + 3 + 3 unit tests + 1 integration test.
- **Privacy assertions** mirror earlier stories.
- **Pyright strict on `src/`** — `Callable[[MicMode, MicMode], Awaitable[None]] | None`; `_ModeStampedAudioFrame` field default; `asyncio.Event` typing.

### Performance budget

NFR1 fast-path turn budget is unchanged; Story 4.6 doesn't add hot-path latency. The frame-stamping is a single attribute write per frame (~ns); the gate check is a comparison (~ns). The buffer-clear callback fires only on mode transitions (a few times per session); negligible.

The single-stream invariant **saves CPU** in v1 — Porcupine processes 32ms chunks at ~5-10ms compute each. Skipping Porcupine in `vad_stt` mode means ~50% less CPU on `pvporcupine.process` calls during AWAKE periods. Not architecturally critical, but a nice side effect.

### What "done" looks like

- `just check` exits 0.
- `just test` exits 0 (including `test_continuous_conversation.py` + all earlier stories' tests).
- `just run` end-to-end on the dev host:
  - "Hey OLAF" → wake greeting fires.
  - "What time is it?" (no re-wake) → answered.
  - "Tell me a joke." (no re-wake) → answered.
  - "Goodnight" → intent-sleep; "Hey OLAF" must re-wake from `wake_word_only`.
- `voice-agent.log` shows the `mic_mode.transition` sequence: `wake_word_only → vad_stt` (on wake) → no transitions during turns 2 and 3 → `vad_stt → wake_word_only` (on intent-sleep).
- Sprint-status flips to `review`.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Mic-mode signaling] — `wake_word_only` ↔ `vad_stt` switch on FSM state; single mic consumer.
- [Source: build_documents/planning-artifacts/architecture.md#Wake-word library] — Porcupine engaged only while `sleeping`; gating enforced at the audio transport.
- [Source: build_documents/planning-artifacts/architecture.md#Audio I/O backend] — single audio source; not parallel-listener architecture.
- [Source: build_documents/planning-artifacts/prd.md#FR47] — continuous mic capture while AWAKE; mic-mode flip on FSM signal.
- [Source: build_documents/planning-artifacts/prd.md#NFR25, FR39] — privacy invariants.
- [Source: build_documents/planning-artifacts/epics.md#Story 4.6] — full AC list.
- [Source: build_documents/planning-artifacts/epics.md#Epic 4 Goal] — wake-word fires only on `sleeping → waking`.
- [Source: build_documents/implementation-artifacts/4-3-activity-fsm-core.md] — `mic_mode_queue` + signal de-dup invariant.
- [Source: build_documents/implementation-artifacts/4-5-wake-greeting.md] — `_GreetingInjectorProcessor` placement precedent (Pipecat plumbing in `pipeline.py`).
- [Source: build_documents/implementation-artifacts/1-6-wake-word-detection-porcupine.md] — `WakewordProcessor` baseline.
- [Source: build_documents/implementation-artifacts/1-7-vad-bounded-capture-and-stt.md] — `VadProcessor` + `STTProcessor` baselines.
- [Source: src/voice_agent_pipeline/audio/wakeword.py] — current `WakewordProcessor` impl; modification target.
- [Source: src/voice_agent_pipeline/audio/vad.py] — current `VadProcessor` impl; modification target.
- [Source: src/voice_agent_pipeline/audio/transport.py] — Story 1.5/2.1's `build_audio_transport` (NOT modified).
- [External: https://docs.pipecat.ai/reference/frames] — `Frame` and `AudioRawFrame` reference.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

### Completion Notes List

### File List

## Change Log

| Date | Change |
|---|---|
| 2026-05-07 | Story 4.6 prepared — mic-mode flip (audio/transport consumes FSM mic-mode signal, FR47). |
