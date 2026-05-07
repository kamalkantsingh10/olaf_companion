# Story 3.7: Audio-frame metadata threading + Talker SSML prompt + embodiment alignment integration test

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want segments' `SpeechEmotionEvent` AND `VocalizationEvent` metadata threaded through Pipecat's audio frames so the publisher fires when each frame is sent — Talker updated to emit Cartesia SSML tags inline — `MoodController.publish_initial()` and `EventPublisher.connect()` wired into the pipeline lifecycle — and an integration test that proves voice / `speech_emotion` alignment hits the 30–80ms anticipatory window (NFR5),
so that Sprint 3 delivers visible (on-bus) embodiment in lockstep with audio across both audio-anchored topics — Epic 3 capstone.

## Acceptance Criteria

1. **`Segmenter` is wrapped in a Pipecat processor.** Define `SegmenterProcessor(FrameProcessor)` in `src/voice_agent_pipeline/pipeline.py` (next to `CartesiaSynthesisProcessor` from Story 2.5). Behavior:
   - Consumes `TalkerResponseFrame` from upstream (Story 2.4's `TurnDispatchProcessor`).
   - For each token chunk in the frame's text (whether the frame is a single complete response or — future — streaming chunks), drives `Segmenter.consume(chunk)` from Story 3.3 and `Segmenter.flush()` after the last chunk.
   - **Replaces the current direct `TalkerResponseFrame → CartesiaSynthesisProcessor` flow**: the segmenter sits between, emitting one `SegmentFrame(segment: Segment)` per `Segment`, where `SegmentFrame` is a new pipecat-compatible frame class also defined in `pipeline.py`.
   - On `_FrameCounter` boundaries (last frame of a turn) or whenever the turn naturally ends, calls `segmenter.flush()` and `segmenter.reset()`. **Coordinates with `LastPublishedCache.reset()` from Story 3.2** — both reset on the same boundary signal.

2. **`CartesiaSynthesisProcessor` is updated to consume `SegmentFrame`.** Replaces Story 2.5's "consume `TalkerResponseFrame`" behavior:
   - For each `SegmentFrame(segment)`: calls `cartesia_client.synthesize(segment.text)` (the segment's text already has vocalizations kept-or-stripped per `tts_supported`); for each `chunk` yielded, wraps it in an **enhanced** `OutputAudioRawFrame` carrying the segment's metadata.

3. **`OutputAudioRawFrame` carries metadata in two slots.** Either subclass Pipecat's `OutputAudioRawFrame` to add the slots, or attach via `frame.metadata` if Pipecat 1.1.0 supports per-frame arbitrary metadata. **Decision tree (decide first thing in implementation, document in dev record)**:
   - **A — subclass**: define `EmbodimentAudioFrame(OutputAudioRawFrame)` with `speech_emotion_event: SpeechEmotionEvent | None = None` and `vocalization_events: list[VocalizationEvent] = field(default_factory=list)`. Probably the cleaner option if Pipecat's frame model allows subclassing without disrupting frame routing.
   - **B — metadata dict**: piggyback on `OutputAudioRawFrame.metadata` if Pipecat exposes it. Less type-safe but no subclass risk.
   - **C — fallback to time-based correlation**: if neither A nor B works due to Pipecat's frame-routing constraints, the documented PRD risk (architecture.md §"Notable risk vectors") applies: switch to time-based correlation — emit events at `frame.send_time + offset`, where offset is the configured anticipatory window (e.g., 50ms midpoint). **Choosing C requires a same-commit `architecture.md` deviation note** (NFR26 — spec-as-contract).
   - **Recommended**: try A first; if Pipecat's `Frame` is a frozen dataclass that resists subclassing, fall back to B; only fall back to C if both fail.

4. **The publisher fires before each audio frame is sent (FR22, FR23).** In `LocalAudioTransport.output()` or its equivalent in the pipeline (find via Story 2.1's transport code):
   - Before pushing each `EmbodimentAudioFrame` to the speaker hardware, check the metadata slots:
     - If `frame.speech_emotion_event is not None`: `await event_publisher.publish_speech_emotion(frame.speech_emotion_event)`.
     - For each `event` in `frame.vocalization_events`: `await event_publisher.publish_vocalization(event)`.
   - **Order**: publish `speech_emotion` first if present, then all vocalizations in order, then push the audio frame to hardware.
   - **The 30–80ms anticipatory window (NFR5)** comes "for free" if the publish happens before the audio frame buffers into the speaker's playback queue — the buffer's drain time is the natural anticipatory offset. Verify this with the integration test (AC #11).

5. **`LastPublishedCache.should_publish()` gates `speech_emotion` attachment** (FR24). In `SegmenterProcessor`, when building the `EmbodimentAudioFrame` for a segment's first audio frame:
   - If `segment.speech_emotion_payload is not None`:
     - Build `event = SpeechEmotionEvent(payload=segment.speech_emotion_payload, correlation_id=current_turn_id)`.
     - Call `cache.should_publish(segment.speech_emotion_payload)`. If `True`: attach to `frame.speech_emotion_event`. If `False`: skip attachment (FR24 dedup).
   - Vocalizations always attach: for each `payload` in `segment.vocalization_payloads`, build `VocalizationEvent(payload=payload, correlation_id=current_turn_id)` and append to `frame.vocalization_events`. **No cache call** — vocalizations are never deduped (FR24).

6. **`audio_frame_id` populated on payloads.** Pipecat's frame model has some kind of frame identifier (frame_id, sequence_number, or timestamp). Use whatever the existing transport assigns to `OutputAudioRawFrame`. Set `payload.audio_frame_id = str(frame.id)` (or whatever the field is named) on the **first** frame of a segment, before publishing. Subsequent frames in the same segment do **not** re-publish (FR24 + the dedup cache).

7. **`prompts/talker_system.md` updated for SSML emission.** Append a section instructing Talker to emit `<emotion value="..."/>` and `[laughter]` / `[sigh]` / `[gasp]` / `[clears_throat]` inline (FR12 extension):
   - List the **12 emotion values** (6 primary + 6 secondary): `neutral, content, excited, sad, angry, scared, happy, curious, sympathetic, surprised, frustrated, melancholic`. Tell the LLM to pick the value that best fits the emotional tone of the response, and to emit it **before** the relevant text segment.
   - List the **4 vocalization tags**: `[laughter]`, `[sigh]`, `[gasp]`, `[clears_throat]`. Tell the LLM these are inline, optional, and should reflect natural speech.
   - **Example response shape** in the prompt:
     ```
     <emotion value="content"/> Sure, I can help with that. <emotion value="curious"/> What kind of project are you working on?
     ```
     ```
     <emotion value="happy"/> [laughter] That's a great one! <emotion value="content"/> So your next move is...
     ```
   - **Greeting-mode prompt is NOT updated here** — that's Story 4.5 (wake greeting). Story 3.7 only updates the conversational mode.

8. **`MoodController.publish_initial()` wired into pipeline startup.** In `pipeline.py:run_pipeline` (or `__main__.py`'s startup sequence — pick the cleaner home):
   - **After** `await event_publisher.connect()` succeeds.
   - **Before** the pipeline runner's main loop starts (so the first event on the latched `mood` topic is the initial `calm` mood).
   - Call: `await mood_controller.publish_initial()`.
   - Document the ordering in `pipeline.py`'s module docstring + the dev record.

9. **`EventPublisher` injection through the pipeline.** `run_pipeline(config)` constructs:
   - `event_publisher = build_publisher(config.publisher)` (Story 3.5).
   - `await event_publisher.connect()`.
   - `mood_state = MoodState(initial=config.mood.initial)`.
   - `mood_controller = MoodController(mood_state, event_publisher, cooldown_publishes_per_hour=config.mood.cooldown_publishes_per_hour)`.
   - Builds the `Segmenter(mapping)` (where `mapping = load_from_path(Path("expression_map.yaml"))`).
   - Builds the `LastPublishedCache()`.
   - Builds the `SegmenterProcessor(segmenter, cache, event_publisher)`.
   - Wires the pipeline list:
     ```
     transport.input()
       → WakewordProcessor
       → VadProcessor
       → SttProcessor
       → _SttResultLogger
       → _WakewordEventLogger
       → TurnDispatchProcessor
       → SegmenterProcessor                           # NEW (this story)
       → CartesiaSynthesisProcessor                   # UPDATED (consumes SegmentFrame)
       → _FrameCounter
       → transport.output()                           # UPDATED (publishes events before each frame)
     ```
   - `await mood_controller.publish_initial()`.
   - Then start the runner.

10. **Turn-boundary reset.** When the activity FSM emits `working → listening` (which Story 4.3 wires; for now, Story 3.7 uses a proxy: end-of-`SegmentFrame` stream from a turn = end-of-segment-flush + the next `UtteranceCapturedFrame`):
    - `segmenter.reset()`.
    - `cache.reset()`.
    Implementation note: In v1, before the activity FSM lands, the proxy boundary is "first byte of next `UtteranceCapturedFrame`." Document this as a Story 3.7 stopgap; Story 4.3 will replace it with the FSM signal.

11. **Integration test `tests/integration/test_embodiment_alignment.py`.** Mirrors Story 2.5's `test_simple_turn.py` structure. Test:
    - **Mocks Cartesia** to yield deterministic synthetic audio chunks at known intervals (e.g., 50ms each over 1.5s = 30 chunks).
    - **Uses `LogEventPublisher`** (Story 3.5) so publishes are captured in `published`.
    - **Drives 30 simulated turns**, each with a Talker response containing one primary emotion, one secondary emotion, one fallback-family tag (a `enthusiastic`-equivalent), and one `[laughter]`. Talker is mocked to return the canned response.
    - **For each turn, measures**:
      - `speech_emotion` publish time (from `published[i].timestamp`-equivalent — record `time.monotonic_ns()` at publish for test purposes; do **not** use `event.timestamp` which is the construction time, not the publish time).
      - First audio frame send time (from the sink processor's intercept).
      - Compute `(audio_send_time - publish_time)` per event.
    - **Asserts**: `(audio_send_time - publish_time)` falls within `[30ms, 80ms]` for the **p95** of the 30 turns × ~3 events per turn ≈ 90 measurements. Same assertion for `vocalization` publishes.
    - **Records p50/p95/max** in the test output + the commit message.
    - **Privacy assertions** (NFR25, FR39): no `audio_bytes` field in any log, no transcripts at INFO+, no API key value in any log line. Mirror Story 2.5's `test_simple_turn.py` patterns.

12. **Integration test for Talker → publish flow correctness.** A second test in the same file (or a sibling `test_embodiment_correctness.py` if cleaner):
    - Talker mock returns: `<emotion value="content"/> Hi there. <emotion value="excited"/> [laughter] Great to see you! <emotion value="enthusiastic"/> Welcome.`
    - Drives one turn through the pipeline.
    - Asserts on `LogEventPublisher.published` ordering:
      1. `("speech_emotion", SpeechEmotionEvent(payload=<content>, ...))`
      2. `("speech_emotion", SpeechEmotionEvent(payload=<excited>, ...))`
      3. `("vocalization", VocalizationEvent(payload=<laughter>, ...))`
      4. `("speech_emotion", SpeechEmotionEvent(payload=<excited via family fallback>, raw_tag="enthusiastic", resolved_fallback="high_energy_positive"))`
        - **OR** — if "enthusiastic → excited" is a no-change emotion, the cache dedups it and there's no #4. Pick the test phrasing that matches your fallback-family authoring (Story 3.1's high_energy_positive). **Recommend a third tag that resolves to a different emotion** (e.g., `melancholy → sad`) so the test demonstrably covers the fallback-emit path without dedup.
    - Asserts the segment's `text` going to TTS has `[laughter]` retained and (if any vocalizations are tts_supported=False) those stripped.

13. **`v1 deferred fallback path` documented if invoked.** If AC #3 falls back to time-based correlation:
    - Add a section to `architecture.md` under §"Notable risk vectors" documenting the fallback was actually invoked, the offset chosen, and the trade-off (NFR5 still hits but the alignment is statistical not exact).
    - Update PRD's risk section likewise (NFR26 — spec-as-contract).
    - **Commit the doc change in the same commit as the code change.** Per CLAUDE.md rule #9.

14. **No transcripts at INFO+; no API key in any log; no raw audio in any log** (NFR25, FR39 — standing). Stories 1.3/1.7/2.5's privacy invariants continue. The integration test asserts on log contents (mirror Story 2.5's existing assertions).

15. **`just check` stays green.** All Story 1/2 + 3.1-3.6 unit tests still pass. The new integration test runs as part of `just test` (full suite), not `just check` (fast subset).

16. **Live end-to-end test (manual).** With a real DDS subscriber on the dev host (`ros2 topic echo /olaf/speech_emotion`), `just run` + speak "Hey OLAF, tell me a joke" — observe `speech_emotion` events arriving on the bus aligned with each phrase + a `vocalization` event on `[laughter]`. Document the manual smoke result in the commit message + dev record. **This is the Epic 3 visible-on-bus capstone.**

## Tasks / Subtasks

- [ ] **Task 1: Decide and implement audio-frame metadata strategy** (AC: #3, #13)
  - [ ] Read Pipecat 1.1.0's `OutputAudioRawFrame` source — is subclassing supported? Does it have a `metadata` field?
  - [ ] Pick option A (subclass) / B (metadata dict) / C (time-based fallback). Document the choice + rationale in `pipeline.py` module docstring + dev record.
  - [ ] If C: write the `architecture.md` deviation note in the same commit (NFR26).

- [ ] **Task 2: `SegmenterProcessor` and `SegmentFrame` in `pipeline.py`** (AC: #1, #5, #6, #10)
  - [ ] Define `SegmentFrame(Frame)` (or whatever Pipecat's frame base class is). Field: `segment: Segment` (Story 3.3's class).
  - [ ] Implement `SegmenterProcessor(FrameProcessor)` per AC #1.
  - [ ] Inject `Segmenter`, `LastPublishedCache`, `EventPublisher`, `correlation_id_supplier` (a callable returning the per-turn id — for v1, a simple `lambda: uuid4()` per turn boundary suffices; Story 4.x will replace with the activity FSM's turn id).
  - [ ] **Reset coordination** (AC #10): hook into the next-`UtteranceCapturedFrame` boundary as the v1 proxy for "turn end."

- [ ] **Task 3: Update `CartesiaSynthesisProcessor`** (AC: #2)
  - [ ] Change input frame type from `TalkerResponseFrame` to `SegmentFrame`.
  - [ ] Set the metadata slots on each emitted audio frame:
    - First audio frame of a segment carries the segment's `speech_emotion_event` (if dedup allows) + all `vocalization_events`.
    - Subsequent frames of the same segment carry no metadata (the events fired on the first frame).
  - [ ] Verify the mid-segment behavior: a segment producing 5 audio chunks emits one `EmbodimentAudioFrame` with metadata + 4 plain `OutputAudioRawFrame`s.

- [ ] **Task 4: Update `transport.output()` to publish before send** (AC: #4)
  - [ ] Find Story 2.1's transport wiring. Identify where each `OutputAudioRawFrame` is pushed to PyAudio.
  - [ ] **Option**: subclass / wrap the transport to intercept, OR add a pre-output `_PrePublishProcessor(FrameProcessor)` between `_FrameCounter` and `transport.output()` that does the publishes when it sees `EmbodimentAudioFrame`.
  - [ ] **Recommend** the pre-publish processor — keeps the transport untouched and the publish logic tested in isolation. Place it just before `transport.output()`.

- [ ] **Task 5: Wire pipeline + lifecycle** (AC: #8, #9)
  - [ ] Update `run_pipeline(config)` per AC #9.
  - [ ] Add the post-`connect()` `await mood_controller.publish_initial()` call. Test it with `LogEventPublisher` capturing the initial event.

- [ ] **Task 6: Update Talker system prompt** (AC: #7)
  - [ ] Append the SSML-emission section to `prompts/talker_system.md`.
  - [ ] Live-test on the dev host (with a real Talker provider — Groq per `setup.toml`): does the LLM actually emit `<emotion value="..."/>` tags? Prompt iteration may be required. Document the final prompt + provider behavior in the dev record.

- [ ] **Task 7: Embodiment-alignment integration test** (AC: #11)
  - [ ] `tests/integration/test_embodiment_alignment.py`.
  - [ ] Mirror Story 2.5's harness for Cartesia mocking. Yield 30 chunks per turn at 50ms intervals (`asyncio.sleep(0.05)` between yields).
  - [ ] Sink processor records `time.monotonic_ns()` per frame send.
  - [ ] `LogEventPublisher` captures publishes; tests record `time.monotonic_ns()` at each publish via a small wrapper around the publisher methods.
  - [ ] Assert p95 `(send_time - publish_time)` ∈ [30ms, 80ms] for both `speech_emotion` and `vocalization`.

- [ ] **Task 8: Embodiment-correctness integration test** (AC: #12)
  - [ ] In the same file or a sibling. Asserts on event ordering + content for a hand-crafted Talker response.
  - [ ] Use Story 3.1's `expression_map.yaml` (real, not mocked) so fallback resolution exercises the production map.

- [ ] **Task 9: Live smoke (manual)** (AC: #16)
  - [ ] Source ROS 2 (`source /opt/ros/jazzy/setup.bash` or equivalent).
  - [ ] In one terminal: `ros2 topic echo /olaf/speech_emotion`.
  - [ ] In another: `just run`. Speak a "tell me a joke" prompt.
  - [ ] Observe `speech_emotion` events flowing in lockstep with phrases. Record the experience (timing, count, any anomalies) in the dev record + commit message.

- [ ] **Task 10: Pass `just check`; verify all earlier stories' tests still green** (AC: #15)
  - [ ] `uv run pytest tests/unit -v` — full unit suite passes.
  - [ ] `uv run pytest tests/integration -v` — integration suite (including Story 2.5's `test_simple_turn.py`) still passes; new alignment tests pass.

- [ ] **Task 11: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] Single commit titled `Story 3.7: audio-frame metadata + Talker SSML + embodiment alignment integration (Epic 3 capstone)`.
  - [ ] Body: list the metadata strategy chosen (A/B/C), the integration-test p95 numbers, the live smoke result, and any deviations.
  - [ ] `git push` immediately.

## Dev Notes

### Architectural intent

Story 3.7 is the **Epic 3 capstone** — Sprint 3's "OLAF feels alive" deliverable. It wires every prior Epic 3 story into the running pipeline:
- Story 3.1's `expression_map.yaml` loaded at startup.
- Story 3.2's resolver + cache called inside `SegmenterProcessor`.
- Story 3.3's `Segmenter` driven token-by-token by `SegmenterProcessor`.
- Story 3.4's `EventEnvelope` + four event types serialized + published.
- Story 3.5's `EventPublisher` (`Ros2EventPublisher` for prod, `LogEventPublisher` for tests) invoked from `_PrePublishProcessor`.
- Story 3.6's `MoodController.publish_initial()` fires on connect.

The hard architectural risk is **AC #3** — Pipecat's frame model has to carry the embodiment metadata cleanly. The PRD risk register flags this; architecture.md notes the time-based-correlation fallback. The dev MUST decide A/B/C up front and document the choice, because every other AC depends on the metadata-carrying contract.

### Why publish-before-send (and not "publish after send confirmation")

NFR5: **30–80ms anticipatory window** — events arrive at embodiment subscribers **before** audio reaches the listener, so the embodiment (LED, pose, etc.) can pre-position to match the emotion the audio's about to express.

If we published after the audio is heard, the visible/embodied response would lag the audio by the same delay — feels reactive, not alive. The architecture's anticipatory contract is what makes the system **feel like it has an internal state that's being expressed**, not an animatronic chasing audio.

The 30ms minimum is "long enough for the embodiment renderer to finish its move"; the 80ms max is "short enough that the user doesn't perceive the lag." The window comes naturally from the audio's buffer-to-speaker latency — Pipecat + PyAudio + the OS audio buffer total ~50–100ms. **Publishing right before `transport.output()` pushes the frame to PyAudio gives us the window for free**, IF Pipecat's frame ordering preserves it.

Test the actual window (AC #11) — if measured p95 is outside [30, 80], something is wrong with the pipeline or the assumed buffer depth, not the architecture.

### `_PrePublishProcessor` vs subclassing the transport

Two valid placements for the publish-before-send logic:
- **Inside `transport.output()`** — subclass `LocalAudioTransport` to intercept; clean but couples the transport to the publisher.
- **As a separate processor** between `_FrameCounter` and `transport.output()` — `_PrePublishProcessor` reads `EmbodimentAudioFrame.speech_emotion_event` + `vocalization_events`, calls publish, then forwards the frame.

**Recommend the separate processor.** Three reasons:
1. **Testability**: a separate processor can be unit-tested in isolation (drive `EmbodimentAudioFrame` in, capture publishes via `LogEventPublisher`).
2. **Boundary-concentration**: the transport stays narrowly focused on speaker I/O; the publisher stays in `publisher/`.
3. **Pipecat ergonomics**: subclassing `LocalAudioTransport` means owning a fork; a separate processor is just composition.

### Segmenter token-by-token vs whole-frame

Story 2.5's pipeline currently passes the whole `TalkerResponseFrame.text` to Cartesia at once. Story 3.7's segmenter wants to consume **chunks** to support real streaming.

For v1, Story 2.5 uses Talker's complete response (the openai/groq SDK returns one full message; Talker doesn't stream). So:
- The "token stream" entering the segmenter is the **whole completed message text**, fed in one chunk.
- The segmenter still emits `Segment`s on internal tag/sentence boundaries — so the segmentation works, just on a non-streaming input.
- When a future story (post-v1) makes Talker stream, the segmenter is already shaped to consume chunks correctly.

Document this in `SegmenterProcessor`'s docstring. Don't over-engineer — Story 2.5's flow gives one chunk; that's fine.

### Talker SSML prompt — the LLM cooperation question

The architecture's tension: the Talker prompt ASKS the LLM to emit emotion tags + vocalizations naturally. The LLM will sometimes:
1. **Skip them entirely** — replies are plain text. Result: no `speech_emotion` events fire; the embodiment runs on the prior latched mood/emotion, which is correct behavior.
2. **Emit them too eagerly** — every sentence has `<emotion value="X"/>`. Result: lots of `speech_emotion` events, mostly deduped by the cache (FR24), so wire-noise is bounded. Acceptable.
3. **Emit malformed tags** — `<emotion value="excited">` (missing self-close) or `<emotion val="..."/>`. Result: the state machine treats them as plain text, which Cartesia receives as garbage and may render literally. Story 5.5 calibration territory; v1 ships with whatever the prompt + Groq produces.

**The prompt is the cheapest lever.** Iterate on the prompt during Task 6's live test. If Groq's Llama 3.1 8B is unreliable on the SSML form, escalate to a 70B variant (architecture.md §"Talker provider"); document the swap. Prompt is in `prompts/talker_system.md` — committed file, evolves through git.

### `correlation_id` per-turn binding

For v1 (before activity FSM lands in Story 4.3), the per-turn correlation_id is generated at the start of each turn — the simplest source is "uuid4 per `UtteranceCapturedFrame`." Bind it once and pass through to:
- Each `SpeechEmotionEvent.correlation_id` and `VocalizationEvent.correlation_id` for that turn's segments.
- Any `MoodEvent` fired during the turn (Story 4.4 territory mostly, but `set_mood` from a tool dispatch within the turn shares the id).
- The `ActivityEvent`s (Story 4.3 wires them; not in this story).

**Storing and threading the id**: a contextvar (`structlog`'s `bind_contextvars`) is the cleanest approach — the pipeline binds it once per `UtteranceCapturedFrame`, every downstream processor reads it via `get_contextvars()`. Story 1.3's logging setup uses contextvars; reuse the pattern.

### What this story does NOT do

- **No activity FSM.** Story 4.3 builds it; Story 3.7 uses a v1 proxy for the turn boundary signal (next-utterance edge).
- **No tool registry.** Story 4.4 builds `SetMoodTool` + `GoToSleepTool`. Story 3.7's pipeline initializes `MoodController` but the tool dispatch isn't wired here — Talker still doesn't call `set_mood(...)` until Story 4.4.
- **No wake greeting.** Story 4.5.
- **No mic-mode flip.** Story 4.6.
- **No barge-in.** v1.5 backlog (`v1.5-1-barge-in`).
- **No SIGHUP reload of `expression_map.yaml`.** Epic 5 (Story 5.2 hardening territory).
- **No live integration test for real DDS publish-receive.** The mocked alignment test (AC #11) is sufficient for `just test`; the live smoke (AC #16) is manual + dev-host-only.

### Project structure notes

This story creates:
- `tests/integration/test_embodiment_alignment.py`
- (optional) `tests/integration/test_embodiment_correctness.py` — sibling for the AC #12 ordering test if it grows long.

It modifies (heavily):
- `src/voice_agent_pipeline/pipeline.py` — `SegmenterProcessor`, `SegmentFrame`, `EmbodimentAudioFrame` (or metadata-dict equivalent), `_PrePublishProcessor`, updated `run_pipeline` per AC #9.
- `prompts/talker_system.md` — SSML emission section.
- `tests/integration/test_simple_turn.py` (Story 2.5) — likely needs adjustment to cope with the new pipeline shape; ideally just one test fix (e.g., the canned Talker response now goes through `SegmenterProcessor`, which still routes plain-text to Cartesia correctly).

It MAY modify (depending on AC #3 choice):
- `architecture.md` — only if option C (time-based fallback) is invoked.
- The PRD's risk section — same condition.

It does NOT modify:
- `src/voice_agent_pipeline/splitter/*.py` (Stories 3.1-3.3 are upstream producers).
- `src/voice_agent_pipeline/schemas/*.py` (Story 3.4).
- `src/voice_agent_pipeline/publisher/*.py` (Story 3.5).
- `src/voice_agent_pipeline/mood/*.py` (Story 3.6).

### Testing standards

- **Mocks at Protocol seams only.** `LogEventPublisher` is a real implementation, used as the test fake. `Cartesia` is mocked at the `TTSClient` Protocol seam. Real `ExpressionMapConfig`, real `Segmenter`, real `Resolver`, real `MoodController`.
- **Integration tests** measure timing — use `time.monotonic_ns()` (not `time.time()`) for clock-stable measurement.
- **Privacy assertions** mirror Story 2.5's pattern. The redaction processor (Story 1.3) handles most of it; the test verifies no regressions.
- **Async** throughout — `pytest.mark.asyncio` on every integration test.

### Performance budget

NFR5 30–80ms anticipatory window dominates this story's quality bar. If the integration test consistently shows p95 > 80ms:
1. **Check the publish-call latency** — `LogEventPublisher.publish_*` should be sub-millisecond (it's just a list append). If it isn't, something else is hot.
2. **Check the audio-frame buffer depth** — if Pipecat is buffering 200ms of audio before `transport.output()` writes, the anticipatory window is 200ms, not 80ms. Tune the audio buffer in `setup.toml`'s `[audio]` block (Story 1.5/2.1's territory) — smaller buffer, tighter window.
3. **Document the trade-off** — if real-DDS publish latency itself is 100ms (network/QoS overhead), the architecture's window assumption is broken. Open a Story 5.5 calibration item.

### What "done" looks like

- `just check` exits 0.
- `just test` exits 0 (the new alignment + correctness integration tests pass).
- `just run` end-to-end on the dev host produces:
  - Audio output through the speaker (Stories 2.1-2.5 still alive).
  - `speech_emotion` events on `/olaf/speech_emotion` aligned with each phrase, anticipatory by 30–80ms.
  - `vocalization` events on `/olaf/vocalization` for any LLM-emitted `[laughter]` etc.
  - Initial `mood` event on `/olaf/mood` (latched, `mood="calm"`).
- `ros2 topic echo /olaf/speech_emotion` shows the JSON envelope live.
- Sprint 3 outcome achieved. Sprint 4 (Epic 4 — Activity FSM + tools) can begin.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Streaming + Concurrency (Batch 2)] — boundary-based segmentation; audio-frame metadata threading.
- [Source: build_documents/planning-artifacts/architecture.md#Notable risk vectors] — Pipecat metadata-threading risk; time-based correlation fallback.
- [Source: build_documents/planning-artifacts/architecture.md#Decision Impact Analysis] — anticipatory-window justification.
- [Source: build_documents/planning-artifacts/prd.md#NFR5] — 30–80ms anticipatory window.
- [Source: build_documents/planning-artifacts/prd.md#FR22, FR23, FR24, FR25] — publish-on-frame-send + dedup + vocalization keep-strip.
- [Source: build_documents/planning-artifacts/prd.md#FR12] — Talker SSML emission (this story extends Story 2.2's plain-text prompt).
- [Source: build_documents/planning-artifacts/epics.md#Story 3.7: Audio-frame metadata threading + Talker SSML prompt + embodiment alignment integration test]
- [Source: build_documents/implementation-artifacts/3-1-expression-map-loader.md] — `load_from_path("expression_map.yaml")`.
- [Source: build_documents/implementation-artifacts/3-2-mapping-resolver-and-cache.md] — `resolve`, `resolve_vocalization`, `LastPublishedCache`.
- [Source: build_documents/implementation-artifacts/3-3-streaming-ssml-state-machine.md] — `Segmenter`, `Segment`.
- [Source: build_documents/implementation-artifacts/3-4-event-schema-rebuild.md] — `EventEnvelope`, `SpeechEmotionEvent`, `VocalizationEvent`, `MoodEvent`.
- [Source: build_documents/implementation-artifacts/3-5-event-publisher-ros2-and-log-adapter.md] — `EventPublisher`, `build_publisher`, `LogEventPublisher`.
- [Source: build_documents/implementation-artifacts/3-6-mood-module-state-and-controller.md] — `MoodController.publish_initial`.
- [Source: build_documents/implementation-artifacts/2-5-pipeline-assembly-simple-turn.md] — `pipeline.py:run_pipeline` baseline + `tests/integration/test_simple_turn.py` test harness pattern.
- [Source: src/voice_agent_pipeline/pipeline.py] — current pipeline assembly; `CartesiaSynthesisProcessor`, `_FrameCounter` baseline.
- [Source: prompts/talker_system.md] — current plain-text prompt (Story 2.2). This story appends the SSML section.
- [External: https://docs.pipecat.ai/reference/frames] — Pipecat 1.1.0 `Frame` / `OutputAudioRawFrame` reference for AC #3 decision.
- [External: https://docs.cartesia.ai/build-with-cartesia/capabilities/voice-control] — Cartesia inline emotion/vocalization tag reference for the Talker prompt examples.

## Dev Agent Record

### Agent Model Used

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List
