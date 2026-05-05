# Story 2.5: Pipeline assembly + simple-turn integration test (NFR1 baseline)

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want `pipeline.py` wiring `wakeword → vad → stt → router → talker → cartesia → speaker` end-to-end with an integration test for journey 1 and a measured NFR1 baseline,
so that I can run `just run`, say "Hey OLAF, what time is it?", and hear OLAF respond — proving the simple-turn loop hits ≤1500ms p95.

## Acceptance Criteria

1. **Cartesia synthesis stage replaces `_TalkerResponseLogger`.** Story 2.4's `_TalkerResponseLogger` (TEMPORARY) is **deleted**. A new `CartesiaSynthesisProcessor` consumes `TalkerResponseFrame`, calls `cartesia_client.synthesize(frame.text)`, wraps each yielded chunk into a Pipecat `AudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1)`, and pushes it downstream so `transport.output()` plays it.

2. **Final pipeline stage list:**
   ```
   transport.input()
     -> WakewordProcessor
     -> VadProcessor
     -> SttProcessor
     -> _SttResultLogger
     -> _WakewordEventLogger
     -> TurnDispatchProcessor
     -> CartesiaSynthesisProcessor
     -> _FrameCounter
     -> transport.output()
   ```
   Assembly happens **once at startup**, never per-turn. The pipeline runs forever; turns are ephemeral state inside processors.

3. **`__main__.py` runs the full Epic 2 startup-validation chain.** Sequence:
   1. `load_setup_config()` — Story 1.2.
   2. `configure_logging(config)` — Story 1.3.
   3. `_validate_wakeword_credentials(config)` — Story 1.6 (Picovoice).
   4. `talker.validate_credentials(config)` — Story 2.2 (Anthropic).
   5. `cartesia.validate_credentials(config)` — Story 2.3 (Cartesia).
   6. `resolve_audio_devices(...)` — Story 2.1 (input + output regex match).
   7. `run_pipeline(config)` — start the loop.
   Each probe wraps native errors as `StartupValidationError`. Sequence is mostly already in place from Stories 2.2/2.3 — verify and add audio-device validation as a pre-pipeline probe (it currently happens inside `run_pipeline` via `resolve_audio_devices`, which is acceptable; the AC is that ANY failure of these is fatal pre-pipeline).

4. **Startup-failure logging — CRITICAL `startup.failed` + non-zero exit.** On any startup-validation failure, `__main__.py` emits a `log.critical("startup.failed", error=..., error_class=...)` and returns exit code 1. **No partial run, no degraded mode.** The existing handler in `__main__.py:main` already does this for `VoiceAgentError`; verify it still triggers for the new probes.

5. **`just run` works end-to-end.** With valid `setup.toml` + `.env` (all three keys: `PICOVOICE_ACCESS_KEY`, `ANTHROPIC_API_KEY`, `CARTESIA_API_KEY`) on the dev host, `just run` accepts speech and replies through the speaker. **Wall-clock requirement:** end-of-speech → first audible response audio in ≤1.5 s (loose; NFR1 is the formal measurement).

6. **Integration test `tests/integration/test_simple_turn.py` (PRD Journey 1).** Runs the full pipeline with **all five external boundaries mocked at their Protocol seams**:
   - Porcupine (`pvporcupine`) — emits a wake-word frame on cue.
   - Silero VAD — emits an `UtteranceCapturedFrame` on cue (or use synthetic audio that genuinely triggers the real VAD; mock is simpler).
   - faster-whisper — `STTBackend.transcribe` returns a canned `TranscriptionResult(text="what time is it?", confidence=0.85)`.
   - Anthropic — `TalkerClient.complete` returns a canned reply (e.g., `"It's just past three o'clock."`).
   - Cartesia — `TTSClient.synthesize` yields a canned series of fake PCM byte chunks.
   For 30 simulated turns, measure end-of-speech (`UtteranceCapturedFrame.end_ns`) → first downstream `AudioRawFrame` (out of `CartesiaSynthesisProcessor`). Compute p50/p95/max. **Record p95 as the NFR1 baseline** in the commit message and Dev Agent Record.

7. **Live integration test (manual, gated).** A second test in the same file (or a sibling `test_simple_turn_live.py`) gated behind `RUN_LIVE_TTS=true`:
   - Uses real Anthropic + real Cartesia (still mocks the audio inputs — Whisper / Porcupine / VAD — to keep the test deterministic and fast, OR uses recorded audio fixtures).
   - Measures live p95 over ≥10 turns.
   - **Expected:** live p95 will exceed mocked baseline by 500-1000 ms (real network). Both numbers logged for comparison in commit message.

8. **No transcripts at INFO; no API key in any log; no raw audio in any log.** Runtime privacy invariants from Stories 1.3 + 1.7. The integration test asserts on log contents:
   - No `stt.transcript` INFO line contains the field `text` or `transcript`.
   - No log line of any level contains the substring of `ANTHROPIC_API_KEY` or `CARTESIA_API_KEY` values (test reads `.env` to know what to look for).
   - No log line of any level contains `audio_bytes`, `audio_data`, `pcm` field names.

9. **Graceful SIGTERM shutdown.** When `__main__.py`'s SIGTERM handler fires mid-turn, the pipeline:
   - Stops accepting new wake-word events.
   - Drains any in-flight Cartesia chunks (`transport.output()` plays them out).
   - Closes the `cartesia.AsyncCartesia` and `anthropic.AsyncAnthropic` clients (`async with` cleanup is preferred; explicit `aclose()` works too).
   - Exits 0.
   v1 fail-fast for genuine errors stays — graceful shutdown is for the SIGTERM happy path. Barge-in (interrupt mid-utterance) is Story 5.1, not this story.

10. **Cartesia mid-turn failure crashes the process** (FR16 deferred — v1 fail-fast). If `client.synthesize(...)` raises mid-stream (Story 2.3's `CartesiaError`), it propagates through `CartesiaSynthesisProcessor` → pipeline task → `__main__.py`'s top-level handler. Process exits non-zero; systemd restarts (Epic 5).

11. **README updated.** Quick-start section reflects the full Epic 2 setup:
    - All three secrets in `.env`.
    - `just play-test-tone` for speaker validation.
    - `just run` + the spoken example.
    - Expected log events: `wakeword.detected` → `stt.transcript` → `talker.responded` → `tts.first_frame`.
    - NFR1 mocked-baseline number included so future regressions are visible.
    - "What's deferred" section: emotion/SSML (Epic 3), complex questions (Epic 4), barge-in + systemd (Epic 5).

12. **`just check` stays green.** Unit tests still all pass. The integration test runs as part of `just test` (full suite), not `just check` (fast subset).

## Tasks / Subtasks

- [ ] **Task 1: Implement `CartesiaSynthesisProcessor`** (AC: #1)
  - [ ] In `pipeline.py`, add the processor below `TurnDispatchProcessor` and remove the temporary `_TalkerResponseLogger`:
    ```python
    class CartesiaSynthesisProcessor(FrameProcessor):
        """Streams TTS audio from Cartesia and pushes AudioRawFrames downstream.

        Consumes :class:`TalkerResponseFrame` (Story 2.4), opens a streaming
        synthesis to Cartesia (Story 2.3), and emits each PCM chunk as a
        Pipecat ``AudioRawFrame`` so ``transport.output()`` (Story 2.1)
        plays it through the speaker.
        """

        def __init__(self, client: TTSClient) -> None:
            super().__init__()  # pyright: ignore[reportUnknownMemberType]
            self._client = client

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, TalkerResponseFrame):
                async for chunk in self._client.synthesize(frame.text):
                    await self.push_frame(
                        AudioRawFrame(
                            audio=chunk,
                            sample_rate=16000,
                            num_channels=1,
                        ),
                        direction,
                    )
            await self.push_frame(frame, direction)
    ```
  - [ ] **Verify Pipecat's `AudioRawFrame` constructor signature against pipecat-ai 1.1.0.** Field name may be `audio` or `data`; sample-rate field may be `sample_rate` or `sampleRate`. Use what the rest of the codebase already uses (`audio/transport.py` config style).
  - [ ] **Verify `transport.output()` actually plays `AudioRawFrame`s pushed from upstream processors.** Pipecat's frame routing should "just work" but verify with the test tone path from Story 2.1.

- [ ] **Task 2: Wire CartesiaClient into `run_pipeline`** (AC: #1, #2)
  - [ ] In `pipeline.py:run_pipeline`, after the Talker/router construction:
    ```python
    cartesia_client = CartesiaClient(config.tts, config.cartesia_api_key)
    ```
  - [ ] Build the pipeline list per AC #2's order. Drop `_TalkerResponseLogger`.
  - [ ] Note: `CartesiaClient` doesn't need a pre-load step (no model download — it's a remote API). Construction is enough.

- [ ] **Task 3: Verify startup-validation sequence in `__main__.py`** (AC: #3, #4)
  - [ ] Confirm Stories 2.2 and 2.3 added their probes. Sequence should read:
    ```python
    await _validate_wakeword_credentials(config)
    log.info("startup.validated.wakeword")
    await talker_module.validate_credentials(config)
    log.info("startup.validated.talker")
    await cartesia_module.validate_credentials(config)
    log.info("startup.validated.cartesia")
    ```
  - [ ] If any probe is missing or out of order, fix it. Don't add new probes — the three external services are exhaustive for Epic 2.
  - [ ] Audio-device validation already happens inside `resolve_audio_devices` (called from `run_pipeline`). The pipeline catches `StartupValidationError` from there and propagates it to `__main__`'s top-level handler. **Acceptable as-is** — explicit pre-pipeline call is not required since the failure is still pre-audio-loop.
  - [ ] Verify `__main__`'s `try / except VoiceAgentError` catches `StartupValidationError` (it does — `StartupValidationError` is a `VoiceAgentError`). The CRITICAL log + return 1 path is already in place from Story 1.6.

- [ ] **Task 4: Implement integration test `tests/integration/test_simple_turn.py`** (AC: #6)
  - [ ] New test file. The test stands up a real `Pipeline` with mocked Protocol-seam clients.
  - [ ] Skeleton:
    ```python
    """Journey 1 (PRD): wake-word → utterance → STT → Talker → Cartesia → speaker.

    Mocks all five external boundaries at their Protocol seams. Measures
    end-of-speech → first AudioRawFrame out of CartesiaSynthesisProcessor;
    reports p50 / p95 / max over 30 simulated turns. p95 is the NFR1 baseline.
    """

    import asyncio
    import time

    import pytest
    from pipecat.frames.frames import AudioRawFrame, Frame
    # ... import the pipeline factory + frame types ...

    @pytest.mark.asyncio
    async def test_simple_turn_p95_baseline(monkeypatch, ...):
        # 1. Patch out the audio transport (no real PyAudio).
        # 2. Patch faster-whisper, anthropic, cartesia at their module
        #    boundaries — same patterns Stories 1.7 / 2.2 / 2.3 use.
        # 3. Construct the pipeline via run_pipeline (or a helper that
        #    factors out the assembly without the runner.run forever loop).
        # 4. For 30 iterations, push an UtteranceCapturedFrame, await first
        #    AudioRawFrame, record latency.
        # 5. Compute p50/p95/max; assert max < some loose ceiling (e.g.,
        #    100 ms with all mocks — real timings happen in the live test).
        ...
    ```
  - [ ] **Refactor opportunity**: `run_pipeline` currently runs forever. Extract the pipeline construction into a helper `build_pipeline(config) -> tuple[Pipeline, ...]` so the test can drive it without the `runner.run` loop. Update `run_pipeline` to call the helper. **Caveat**: only refactor if it cleanly drops out of the existing structure; don't bend the architecture for testability if it makes production code worse.
  - [ ] **Alternative — drive the pipeline via `PipelineTask.queue_frame` and observe the frame flow** through a sink processor that records timestamps. Pipecat's testing utilities (if any in 1.1.0) may already provide this.
  - [ ] **Privacy assertions** (AC #8): use a `caplog`-equivalent sink for structlog (Story 1.3's tests have the pattern); after the test loop, scan all captured records for forbidden field names + secret values. Fail loudly if any are present.

- [ ] **Task 5: Implement live-gated integration test** (AC: #7)
  - [ ] In the same file or a sibling, gated behind `pytest.mark.skipif(os.environ.get("RUN_LIVE_TTS") != "true")`.
  - [ ] Real Anthropic + real Cartesia. Audio inputs (Porcupine / VAD / faster-whisper) still mocked since automating mic input in CI is brittle.
  - [ ] Run ≥10 turns; report p50/p95.
  - [ ] **Don't run this in `just check`** — it's slow + costs money. Document in the test docstring how to run it manually:
    ```python
    """Live integration — real Anthropic + real Cartesia.

    Run with:
        RUN_LIVE_TTS=true uv run pytest tests/integration/test_simple_turn.py -k live -v

    Costs: ~10 Anthropic completions + ~10 Cartesia syntheses ≈ <$0.50 USD.
    """
    ```

- [ ] **Task 6: SIGTERM graceful shutdown** (AC: #9)
  - [ ] Verify `__main__.py`'s SIGTERM handler does what AC #9 requires. Currently:
    ```python
    loop.add_signal_handler(signal.SIGTERM, shutdown.set)
    ...
    pipeline_task = asyncio.create_task(run_pipeline(config))
    shutdown_task = asyncio.create_task(shutdown.wait())
    _, pending = await asyncio.wait(
        [pipeline_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    ```
  - [ ] On SIGTERM, the shutdown_task wins and pipeline_task gets cancelled. The pipeline's CancelledError propagates; Pipecat's runner cleans up. **Add explicit `aclose()` for the Anthropic + Cartesia clients** if Pipecat's cleanup doesn't reach them. Implement with `try / finally` in `run_pipeline`:
    ```python
    cartesia_client = CartesiaClient(config.tts, config.cartesia_api_key)
    talker = AnthropicTalker(config.talker, config.anthropic_api_key)
    try:
        # ... build pipeline + await runner.run ...
    finally:
        await cartesia_client._client.close()  # or whatever the SDK exposes
        await talker._client.close()
    ```
  - [ ] **Or** wrap the SDK clients in `async with` blocks if their lifecycle naturally fits — that's the architecture's preferred pattern (architecture.md §"Async Patterns": "client lifecycle via `async with httpx.AsyncClient() as client:`").
  - [ ] Manual test: `just run` in one terminal; `kill -TERM <pid>` in another mid-utterance. Expect: process drains the in-flight Cartesia stream, exits 0 within ~1 second.

- [ ] **Task 7: README updates** (AC: #11)
  - [ ] Read the existing README; refresh:
    - Quick-start: `cp .env.example .env`, fill all three keys, `uv sync`, `just play-test-tone` to sanity-check speaker, `just run` and speak.
    - Expected log flow on a turn.
    - NFR1 mocked p95 number from Task 4's measurement.
    - Deferred items list (Epic 3 / 4 / 5 highlights).
  - [ ] Don't add new doc files — extend the existing README.

- [ ] **Task 8: Live test — full simple turn end-to-end** (AC: #5, #6)
  - [ ] On the dev host with all three keys + valid voice IDs:
    - `just run`.
    - Say "Hey OLAF, what time is it?".
    - Expected: wake fires, transcribes, Talker replies (~600-900 ms), Cartesia streams audio back, you hear it from the speaker within ~1.5 s of finishing your sentence.
  - [ ] Repeat 5-10 times. Record perceived latency. Document in Dev Agent Record.
  - [ ] Run the mocked baseline test (`just test tests/integration/test_simple_turn.py -k baseline -v`) and capture p50/p95/max for the commit message.

- [ ] **Task 9: Commit + push** — single commit titled `Story 2.5: pipeline assembly + simple-turn integration test (NFR1 baseline)`, then `git push`.

## Dev Notes

### Architectural intent

Story 2.5 is the **Sprint 2 capstone**. It delivers Journey 1 from the PRD: Kamal speaks "Hey OLAF, what time is it?" — within ~1.5 s OLAF responds in voice. No emotion (Epic 3), no complex questions (Epic 4), no barge-in (Epic 5) — but the full simple-turn loop works end-to-end.

After this story:
- Stories 2.1-2.4 are integrated into one running pipeline.
- The startup-validation chain proves all three external services are reachable before audio opens.
- The integration test gives an NFR1 mocked baseline that future regressions show against.
- The live test (manual) gives a real-world p95 against actual Anthropic + Cartesia round-trips.

The story is mostly **wiring** — every stage's logic landed in 2.1-2.4. The novel work here is:
- The Cartesia-output processor (Talker text → AudioRawFrame stream).
- The integration test harness (timing measurement + privacy assertions).
- The graceful shutdown drain (in-flight Cartesia chunks).

### What this story does NOT do

- **No emotion / SSML.** Story 3.5 wires Cartesia's emotion tags through the splitter. v1 plays plain audio.
- **No complex questions.** Story 4.3 wires the orchestrator. v1's `TurnDispatchProcessor` raises `NotImplementedError` for `target="orchestrator"`.
- **No barge-in.** Story 5.1 detects mid-utterance interruption.
- **No systemd unit.** Story 5.4 deploys.
- **No 7-day soak.** Story 5.5 calibrates wake-word thresholds and runs the soak.
- **No Cartesia retry / fallback.** v1 crashes on first failure.

### Integration test design — what's hard

The integration test is the most subtle work in this story. Pipecat's pipelines are **forever loops by design** — they keep awaiting frames until cancelled. Driving 30 turns through that pattern needs care:

**Approach A: keep the runner; inject 30 utterances as queued frames; observe sink frames.**

```python
runner = PipelineRunner()
runner_task = asyncio.create_task(runner.run(task))
for i in range(30):
    started_ns = time.time_ns()
    await task.queue_frame(UtteranceCapturedFrame(audio=b"...", end_ns=started_ns))
    audio_frame = await sink.recv_audio_frame()
    record_latency(time.time_ns() - started_ns)
runner_task.cancel()
```

**Approach B: refactor `run_pipeline` to expose `build_pipeline(config) -> Pipeline` and drive frames through a fresh `PipelineTask` per turn.**

```python
pipeline = build_pipeline(config, mocks=...)
for i in range(30):
    task = PipelineTask(pipeline)
    started_ns = time.time_ns()
    await task.queue_frame(UtteranceCapturedFrame(...))
    # ...
```

**Pick A** — Pipecat pipelines aren't really designed for repeated short tasks; one runner driving 30 frames is closer to the production behavior. If A is awkward (frame routing breaks across "turn boundaries"), B is the fallback. Document the choice.

**Sink processor** is the easiest way to observe output frames:
```python
class _AudioSink(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self._first_audio_frame: asyncio.Future[AudioRawFrame] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame) and self._first_audio_frame and not self._first_audio_frame.done():
            self._first_audio_frame.set_result(frame)
        await self.push_frame(frame, direction)

    def reset(self) -> None:
        self._first_audio_frame = asyncio.Future()
```

Place `_AudioSink` between `CartesiaSynthesisProcessor` and `transport.output()` (or replace `transport.output()` entirely with the sink in tests so no PyAudio touches the test process).

### NFR1 — what the baseline number means

NFR1: end-of-speech → first audio frame, simple-turn p95 ≤ 1500 ms.

The mocked baseline measures **everything except external services** — pipeline routing overhead, processor instantiation, frame propagation, asyncio scheduling. With all five mocks returning instantly, expect p95 in the **single-digit to low-tens of milliseconds**. If the mocked baseline is >50 ms, something is leaking real I/O into the test (or there's a sleep/await hidden in a processor).

The live test then adds:
- ~600-900 ms Anthropic round-trip (Haiku 4.5).
- ~200-400 ms Cartesia first-frame TTFB.
- ~50 ms total for STT + VAD + wake (these are mocked in the live test too — automating real audio-input latency is unwarranted complexity).
- = 850-1350 ms typical, p95 likely 1200-1500 ms.

Story 5.5's soak is where you tune. If the live p95 is consistently >1500 ms in Story 2.5, document the breakdown so 5.5 knows where to optimize.

### Privacy assertions (NFR25, FR39)

The integration test must prove no PII leaks to `voice-agent.log` (INFO+) or `errors.log` (WARN+). Three forbidden surfaces:
1. **Transcripts at INFO+.** `stt.transcript` may include `text`/`transcript` ONLY at DEBUG. Check by scanning all captured INFO+ records for those field names.
2. **API keys.** Read `.env`; for each non-empty value, scan log records (raw text) for substring match. Fail if any record contains a key value.
3. **Raw audio.** Field names `audio_bytes`, `audio_data`, `pcm`, `audio`. Should never appear in any log record at any level — the redaction processor strips them, but verify.

Story 1.3's redaction tests have the structlog-record-capture pattern; mirror it. Don't roll your own.

### Graceful shutdown — what to drain

When SIGTERM fires:
1. The shutdown_task wins; pipeline_task cancels.
2. **In-flight Cartesia stream** — let it finish naturally. The CancelledError propagates through `async for chunk in client.synthesize(...)`. Cartesia's stream cleanup runs. The audio frames already pushed to `transport.output()` continue to play. Any chunks not yet yielded are dropped — fine for v1.
3. **Anthropic in-flight call** — `messages.create` is one-shot; cancellation aborts the HTTPX request mid-flight. Acceptable.
4. **PyAudio cleanup** — Pipecat's `LocalAudioTransport` handles its own teardown.
5. **Client connections** — wrap `cartesia.AsyncCartesia` and `anthropic.AsyncAnthropic` in `async with` if their SDKs support it (verify), or `try/finally` with explicit `.close()`.

Avoid the temptation to "carefully drain everything for 5 seconds before exiting." v1's contract is: SIGTERM → exit 0 promptly, mid-utterance audio gets cut off mid-sentence. Story 5.4's systemd unit gives you `RestartSec=5` headroom.

### Project structure notes

This story creates:
- `tests/integration/__init__.py` (if not yet present)
- `tests/integration/test_simple_turn.py`

It modifies:
- `src/voice_agent_pipeline/pipeline.py` (`CartesiaSynthesisProcessor`, drop `_TalkerResponseLogger`, wire `CartesiaClient`)
- `src/voice_agent_pipeline/__main__.py` (verify probe sequence; no new probes)
- `README.md` (quick-start refresh + NFR1 baseline)

It may modify:
- `src/voice_agent_pipeline/pipeline.py` (extract `build_pipeline` helper if needed for testability — see Approach A/B).

It does NOT create:
- New top-level packages.
- New modules in `src/voice_agent_pipeline/turn/` or `tts/` — Stories 2.2/2.3/2.4 already wrote them.

### Testing standards

- **Mock at the five Protocol seams**: `STTBackend`, `TalkerClient`, `TTSClient`, `WakeWordDetectedFrame` source (the Porcupine wrapper), `UtteranceCapturedFrame` source (the VAD).
- **Use real `RouteDecision` / `TranscriptFrame` / `TalkerResponseFrame`** — these are pydantic models, not Protocol seams; mocking them violates architecture's mock-only-at-Protocol-boundaries rule.
- **The mocked baseline test runs in `just test`**, not `just check`. `just check` should stay <30 s.
- **The live-gated test** has `@pytest.mark.skipif(...)` so default `just test` skips it.

### What "done" looks like

- `just check` exits 0.
- `just test` exits 0 (mocked integration baseline runs and reports p95).
- `just run` end-to-end: speak "Hey OLAF, what time is it?" → hear OLAF respond from the speaker within ~1.5 s.
- Live test (manual, `RUN_LIVE_TTS=true`) exercises real APIs and reports live p95.
- Sprint 2 outcome achieved. Sprint 3 (Epic 3 — embodiment) can begin.
- `kill -TERM <pid>` mid-turn → process exits 0 within ~1 second.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Implementation sequence] — pipeline assembly + lifecycle as the integration step.
- [Source: build_documents/planning-artifacts/architecture.md#Async Patterns] — `async with` for client lifecycle.
- [Source: build_documents/planning-artifacts/prd.md#Journey 1] — simple-turn end-to-end demo.
- [Source: build_documents/planning-artifacts/prd.md#NFR1] — simple-turn ≤1500 ms p95.
- [Source: build_documents/planning-artifacts/prd.md#NFR25, FR39] — no transcripts at INFO+; no credentials in logs.
- [Source: build_documents/planning-artifacts/epics.md#Story 2.5: Pipeline assembly + simple-turn integration test (NFR1 baseline)]
- [Source: build_documents/implementation-artifacts/2-1-audio-playback-speaker-pinning.md] — speaker output stage this story finally has audio for.
- [Source: build_documents/implementation-artifacts/2-2-talker-client-anthropic.md] — Talker construction.
- [Source: build_documents/implementation-artifacts/2-3-cartesia-client-tts.md] — `CartesiaClient.synthesize` contract.
- [Source: build_documents/implementation-artifacts/2-4-turn-router-and-clarification.md] — `TalkerResponseFrame` is the input to this story's Cartesia stage.

## Dev Agent Record

### Agent Model Used

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List
