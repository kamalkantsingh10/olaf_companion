# Story 4.7: TurnRouter slow-path wiring + missing-`turn_end` recovery + complex-turn integration test (J3, NFR2 baseline)

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want `pipeline.py` to wire the slow path (TurnRouter `target="orchestrator"` → orchestrator SSE → splitter → TTS+publisher) including missing-`turn_end` cleanup and FSM `working[delegating]` sub-mode coordination, plus an integration test for journey 3 (complex turn) recording the NFR2 baseline,
so that I can ask "what's on my calendar?" and OLAF actually answers — narration first, then real result via subagent — with the activity FSM correctly tracking `delegating` during orchestrator dispatch.

## Acceptance Criteria

1. **`TurnRouter` gains config-driven slow-path escalation** (the `target="orchestrator"` branch becomes live):
   - In `src/voice_agent_pipeline/turn/router.py`:
     - Add `slow_path_patterns: list[str]` and `default_target: Literal["talker", "orchestrator"]` parameters to `TurnRouter.__init__`. **Recommend** wiring via a new `RouterConfig` (AC #2) rather than scattering parameters.
     - In `route(transcript, confidence)`: after the low-confidence clarification check (which still routes to `talker` per Story 2.4's contract), iterate `slow_path_patterns`. For each regex, `re.search(pattern, transcript, re.IGNORECASE)` — if any match, return `RouteDecision(target="orchestrator", text=transcript, clarification=False)`. Otherwise, fall back to `default_target` (which v1 keeps as `"talker"`).
     - **Compile patterns once at construction** for performance: `self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in slow_path_patterns]`. The hot path becomes `any(p.search(transcript) for p in self._compiled_patterns)`.
     - **Pattern compilation errors** (invalid regex) raise `re.error` at `TurnRouter.__init__`. Wrap in `try/except` and re-raise as `ConfigError(stage="router", reason=str(exc), pattern=...)` for a clean startup-time message.
   - **No hot-reload of routing rules** in v1 (architecture's open question explicitly defers this; reload requires SIGHUP plumbing per Story 5.2 territory). Document in `route()`'s docstring.

2. **`setup.toml` `[router]` block + `RouterConfig` model**:
   ```toml
   # Story 4.7: TurnRouter slow-path escalation. Patterns are case-
   # insensitive regex strings; if any matches the transcript, the turn
   # routes to the orchestrator daemon instead of Talker. Tune per the
   # operator's belief-state surface and orchestrator capabilities.
   # `default` is the fallback when no pattern matches; v1 ships "talker".
   [router]
   slow_path_patterns = [
       "calendar",                    # "what's on my calendar"
       "weather",                     # "what's the weather"
       "tomorrow|next week|today",    # time-grounded queries
       "schedule",                    # "schedule a meeting"
   ]
   default = "talker"
   ```
   In `src/voice_agent_pipeline/config/setup.py`:
   - Add `class RouterConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")`:
     - `slow_path_patterns: list[str] = Field(default_factory=list)` (empty default — operator opts in).
     - `default: Literal["talker", "orchestrator"] = "talker"`.
   - Add `router: RouterConfig = Field(default_factory=RouterConfig)` to `SetupConfig`.
   - Update setup.toml's "Sections populated by subsequent stories" comment list — remove `[router]` entry.

3. **`OrchestratorDispatchProcessor` — new Pipecat processor** in `src/voice_agent_pipeline/pipeline.py` next to `TurnDispatchProcessor`. Consumes the orchestrator branch of `RouteDecision`:
   ```python
   class OrchestratorDispatchProcessor(FrameProcessor):
       """Slow-path dispatcher — Story 4.7.

       Consumes a transcript via TurnRouter's orchestrator branch:
       1) drives the FSM into ``working[delegating]``,
       2) opens an SSE stream via :class:`OrchestratorClient`,
       3) pipes ``narration`` + ``response_chunk`` text downstream as
          ``TalkerResponseFrame`` instances (same path as Talker fast-path
          replies — splitter / TTS / audio-anchored events all "just work").
       4) handles missing-``turn_end`` cleanup: flushes the splitter,
          emits a WARN log, and lets the FSM transition naturally on
          last audio frame.
       """

       def __init__(
           self,
           orchestrator: OrchestratorClient,
           activity_fsm: ActivityFSM,
           session_id_supplier: Callable[[], str],
       ) -> None:
           super().__init__()  # pyright: ignore[reportUnknownMemberType]
           self._orchestrator = orchestrator
           self._activity_fsm = activity_fsm
           self._session_id_supplier = session_id_supplier
   ```
   - **Why a separate processor (not extending `TurnDispatchProcessor`)**: `TurnDispatchProcessor` (Story 4.4) handles the fast-path tool-using shape — text frame + parallel tool dispatch. The slow-path is structurally different (consumes SSE, multiple `TalkerResponseFrame`s per turn, FSM sub-mode change). Architecture.md §"Talker placement in Pipecat" treats the dispatcher as the routing decision; **extending the existing dispatcher** is acceptable IF the impl stays clean. Two patterns considered:
     - (A) Single processor with branched logic (extend `TurnDispatchProcessor`): one `process_frame` method handles both routing targets.
     - (B) Separate `OrchestratorDispatchProcessor` placed downstream from `TurnDispatchProcessor`; the router's decision is read by both, each fires on its own target.
   - **Recommend (A)** — single dispatcher with a branch in `process_frame`. The duplication (TranscriptFrame consumer + RouteDecision dispatch) is too high in (B). The new logic in `TurnDispatchProcessor` handles `decision.target == "orchestrator"` by calling into a private `_dispatch_orchestrator(transcript)` method (or a sibling helper class — dev's call). **Story 4.7 may go with the helper class shape** if the dispatcher grows large enough; document the choice.
   - **For this story spec, the helper class shape (`OrchestratorDispatchProcessor` — listed above) is the recommended encoding for testability** — but the dev may collapse to a single dispatcher if simpler. Both satisfy the AC.

4. **Slow-path orchestration logic** (the linchpin behavior):
   - When `decision.target == "orchestrator"`:
     1. **Call `await activity_fsm.on_dispatch_to_orchestrator()`** — FSM transitions `working[thinking] → working[delegating]`. Publishes `ActivityEvent(state="working", working_submode="delegating", ...)`. **Note**: the FSM must already be in `working[thinking]` (entered via `on_speech_ended`); if not, `on_dispatch_to_orchestrator` raises `VoiceAgentError(reason="illegal_transition")` per Story 4.3's contract — which is correct fail-fast.
     2. **Generate session_id** via the injected supplier (probably the per-turn `correlation_id` from Story 3.7's contextvar; supplier function returns `str(get_contextvars().get("correlation_id"))` or `uuid4().hex` if not bound). The session_id must be stable for the duration of the turn so the orchestrator can stitch context.
     3. **Open SSE stream**: `async for event in self._orchestrator.dispatch(transcript, session_id):`. Story 4.2's `HttpOrchestratorClient.dispatch` yields typed `OrchestratorStreamEvent` instances.
     4. **Dispatch by event type** (use `match` statement for type-narrowing; pyright handles the discriminated union per `schemas/stream.py`):
        ```python
        match event:
            case NarrationEvent(text=t) | ResponseChunkEvent(text=t):
                await self.push_frame(TalkerResponseFrame(text=t), direction)
            case SubagentStartedEvent(name=n):
                log.info("orchestrator.subagent_started", subagent_name=n, session_id=session_id)
                # No audio impact in v1.
            case SubagentProgressEvent(name=n, msg=m):
                log.info("orchestrator.subagent_progress", subagent_name=n, msg=m, session_id=session_id)
                # Future: could emit a low-priority "still thinking" filler — out of scope.
            case SubagentDoneEvent(name=n):
                log.info("orchestrator.subagent_done", subagent_name=n, session_id=session_id)
            case TurnEndEvent():
                log.info("orchestrator.turn_end", session_id=session_id)
                # Splitter drains naturally on last audio frame; no special action here.
        ```
     5. **The async for loop exits naturally** on stream close (after `TurnEndEvent` or stream-end-without-turn-end — see AC #5).
   - **No `asyncio.gather` over events** — the `async for` consumes events serially. Each `TalkerResponseFrame` flows through the splitter as it arrives. **Story 4.2's contract** is "no buffering for full-stream completion" (architecture.md §"Streaming consumption"); Story 4.7's dispatcher honors this by yielding to the splitter as events arrive.

5. **Missing-`turn_end` recovery** (FR14):
   - **The contract** (per epics.md AC): "Given an orchestrator stream that ends without a `turn_end` event, when the SSE connection closes, then the pipeline flushes the splitter (any pending text is segmented + sent to TTS), waits for the last audio frame, the FSM transitions normally on `on_last_audio_frame()` — to `listening` (or to `going_to_sleep` if a `go_to_sleep` tool call fired during the slow turn) — and a WARN log `orchestrator.missing_turn_end` is emitted."
   - **Implementation**: track whether `TurnEndEvent` was seen during the `async for` loop:
     ```python
     turn_end_seen = False
     async for event in self._orchestrator.dispatch(transcript, session_id):
         if isinstance(event, TurnEndEvent):
             turn_end_seen = True
         # ... existing dispatch logic ...
     if not turn_end_seen:
         log.warning("orchestrator.missing_turn_end", session_id=session_id)
     # Either way: do NOT explicitly call splitter.flush() here.
     # The splitter naturally drains when the upstream stops sending frames
     # (Story 3.7's segmenter is event-driven; no idle-flush logic needed).
     # The FSM's on_last_audio_frame fires from _PrePublishProcessor when
     # the last audio frame leaves the transport — same path as fast-path.
     ```
   - **Why no explicit flush**: Story 3.7's `Segmenter.consume(text)` operates on each text chunk; segments emit at boundaries naturally. When the stream stops, any in-progress segment that's a complete sentence flushes; partial segments may be lost (acceptable v1 behavior — no explicit drain semantics required).
   - **Alternative if drain becomes important**: introduce a `_TurnBoundaryFrame` push at end-of-stream (Story 4.3 already defines this frame for FSM-driven turn boundary). The processor pushes `_TurnBoundaryFrame` after the loop exits → `SegmenterProcessor.process_frame` triggers `segmenter.reset()`. **Recommend** doing this — it's the same plumbing Story 4.3 introduced; Story 4.7 reuses for orchestrator turn-end. Document the choice.

6. **Remove `NotImplementedError` stub from `TurnDispatchProcessor`** (Story 2.4 baseline / Story 4.4 update):
   - In `src/voice_agent_pipeline/pipeline.py:TurnDispatchProcessor.process_frame`, the existing branch:
     ```python
     else:
         raise NotImplementedError(
             "orchestrator path is wired in Epic 4 (Story 4.3); "
             f"got target={decision.target!r}"
         )
     ```
     — replace with the orchestrator dispatch logic per AC #4. **Either**:
     - Inline the orchestrator dispatch directly in `TurnDispatchProcessor.process_frame` (single dispatcher pattern A), OR
     - Delegate to `await self._orchestrator_dispatcher.dispatch(decision.text, direction)` where `_orchestrator_dispatcher` is the helper class (pattern B).
   - **Constructor extension**: `TurnDispatchProcessor.__init__(router, tool_registry, orchestrator_dispatcher)` (or pass `orchestrator: OrchestratorClient + activity_fsm + session_id_supplier` directly if using pattern A).
   - **Update test fixtures** in `tests/unit/turn/test_dispatch.py` — additional constructor arg.
   - **Comment fix-up**: the existing stub comment says "Story 4.3 will wire this branch" — the actual story is 4.7. Update or just delete the comment (the now-live code makes it obsolete).

7. **Pipeline-assembly wiring** (`pipeline.py:run_pipeline`):
   - **Already exists from Story 4.2**: `orchestrator_client = HttpOrchestratorClient(http_client, base_url=config.daemon.url)` — Story 4.7 starts using it.
   - **Already exists from Story 4.3**: `activity_fsm` — Story 4.7 starts calling `on_dispatch_to_orchestrator`.
   - **New construction**: 
     ```python
     turn_router = TurnRouter(
         stt_config=config.stt,
         talker=talker,
         orchestrator=orchestrator_client,
         slow_path_patterns=config.router.slow_path_patterns,
         default_target=config.router.default,
     )
     # Session-id supplier wraps the per-turn correlation_id contextvar.
     def _session_id_supplier() -> str:
         ctx = structlog.contextvars.get_contextvars()
         return str(ctx.get("correlation_id", uuid4().hex))
     # Dispatcher (pattern A: single processor; OR pattern B: separate orchestrator dispatcher)
     turn_dispatch_processor = TurnDispatchProcessor(
         router=turn_router,
         tool_registry=tool_registry,
         orchestrator=orchestrator_client,
         activity_fsm=activity_fsm,
         session_id_supplier=_session_id_supplier,
     )
     ```
   - **No new pipeline list entry** (the dispatcher is already in the list; this story extends its responsibilities).
   - Update `pipeline.py`'s module docstring with a Story 4.7 entry — slow path live; TurnRouter pattern-driven escalation.

8. **Logging discipline** (NFR25, FR39, the v1 redaction-discipline standing rule):
   - INFO `orchestrator.subagent_started|subagent_progress|subagent_done|turn_end` — fields: `subagent_name`, `session_id`, possibly `msg` (subagent-progress carries human-readable status). Story 4.2's `dispatch_started` / `dispatch_completed` logs already cover the per-call envelope; Story 4.7's logs cover per-event observability.
   - WARN `orchestrator.missing_turn_end` — fields: `session_id`. Single log per affected turn.
   - **`narration.text` and `response_chunk.text` content stays at DEBUG** (per epics.md AC: "raw response chunks contain LLM text — treated like transcripts; gated to DEBUG only"). Concretely: when the dispatcher pushes `TalkerResponseFrame(text=t)`, log at DEBUG `orchestrator.text_emitted` with `text=t, length=len(t), session_id=session_id`. **Do NOT log this at INFO**. The redaction processor (Story 1.3) is the safety net; Story 4.7's code shouldn't pass response text to INFO+ logs.
   - INFO `router.escalated_to_orchestrator` — fields: `pattern_matched` (the regex source string that matched), `transcript_length`. Logged inside `TurnRouter.route` when a pattern hits. **Privacy**: log the pattern, not the transcript text.
   - The `router.escalated_to_orchestrator` log is at INFO so operators can audit which patterns are firing in production; the actual transcript stays gated.

9. **TurnRouter unit-test extensions** in `tests/unit/turn/test_router.py` (Story 2.4 baseline):
   - `test_router_escalates_on_keyword_match` — `slow_path_patterns=["calendar"]`; `route("what's on my calendar today", confidence=0.9)` returns `target="orchestrator"`.
   - `test_router_escalates_case_insensitive` — `route("CALENDAR check", confidence=0.9)` matches `["calendar"]` regex.
   - `test_router_does_not_escalate_when_no_match` — `slow_path_patterns=["calendar"]`; `route("tell me a joke", confidence=0.9)` returns `target="talker"` (default).
   - `test_router_default_target_orchestrator` — `default_target="orchestrator"` + empty `slow_path_patterns`; any high-confidence transcript routes to orchestrator. (Edge case: `default` flips the world.)
   - `test_router_low_confidence_routes_to_talker_even_with_slow_path_match` — confidence below threshold + transcript matches a pattern; assert `target="talker"`, `clarification=True`. Clarification beats slow-path escalation (the user's input is unreliable; ground via clarification first).
   - `test_router_invalid_regex_raises_config_error_at_init` — `slow_path_patterns=["[invalid"]` → `pytest.raises(ConfigError)` on `TurnRouter(...)` construction; assert `excinfo.value.context["stage"] == "router"`.
   - `test_router_emits_log_on_escalation` — assert INFO `router.escalated_to_orchestrator` log with `pattern_matched`.

10. **`OrchestratorDispatchProcessor` (or extended `TurnDispatchProcessor`) unit tests** in `tests/unit/turn/test_dispatch.py` or `tests/unit/test_pipeline.py`:
    - **Mock**: `OrchestratorClient` (`AsyncMock(spec=OrchestratorClient)` with `dispatch` returning an async iterator of typed events).
    - **Real**: `ActivityFSM(publisher=LogEventPublisher())`, `LogEventPublisher`.
    - `test_orchestrator_dispatch_transitions_fsm_to_delegating` — drive a transcript through the dispatcher with `target="orchestrator"`; mock orchestrator yields `[NarrationEvent(text="thinking..."), TurnEndEvent()]`; assert `fsm.current_state == "working"` AND `fsm.working_submode == "delegating"` after `on_dispatch_to_orchestrator` fires. (Use `LogEventPublisher.published` to verify the activity event sequence.)
    - `test_orchestrator_dispatch_emits_text_frames_for_narration_and_response_chunk` — mock yields `[NarrationEvent(text="thinking"), ResponseChunkEvent(text="here's"), ResponseChunkEvent(text=" the answer"), TurnEndEvent()]`; assert THREE `TalkerResponseFrame` instances pushed downstream with the corresponding texts.
    - `test_orchestrator_dispatch_logs_subagent_events` — mock yields `[SubagentStartedEvent(name="calendar_lookup"), SubagentDoneEvent(name="calendar_lookup"), ResponseChunkEvent(text="..."), TurnEndEvent()]`; assert INFO logs for `subagent_started` + `subagent_done`; assert NO `TalkerResponseFrame` for the subagent events (they have no `text`).
    - `test_orchestrator_dispatch_missing_turn_end_logs_warn` — mock yields `[NarrationEvent(text="hi"), ResponseChunkEvent(text="bye")]` (no `TurnEndEvent`); after the `async for` loop completes (stream closed), assert WARN `orchestrator.missing_turn_end` is logged. Assert `TalkerResponseFrame`s were still pushed.
    - `test_orchestrator_dispatch_orchestrator_error_propagates` — mock `dispatch` raises `OrchestratorError(reason="ConnectError")`; assert `pytest.raises(OrchestratorError)` propagates. v1 fail-fast (CLAUDE.md rule #4); the FSM is already in `working[delegating]` — let the process crash (the broken state is fine; restart resets).
    - `test_orchestrator_dispatch_session_id_passed_through` — assert the `session_id` argument to `orchestrator.dispatch(...)` matches what the supplier returns.
    - `test_orchestrator_dispatch_text_logged_at_debug_not_info` — drive a `ResponseChunkEvent(text="user_secret")`; assert `caplog` at INFO does NOT contain `"user_secret"`; at DEBUG the `orchestrator.text_emitted` log fires. Privacy invariant.
    - `test_dispatch_target_talker_unchanged_path_works` — drive a `target="talker"` decision; assert the existing fast-path (Story 4.4) still works. **Critical** — the constructor extension shouldn't break the fast-path.

11. **`RouterConfig` unit tests** in `tests/unit/config/test_setup.py`:
    - `test_router_defaults` — `RouterConfig()` → `slow_path_patterns == []`, `default == "talker"`.
    - `test_router_slow_path_patterns_explicit` — TOML `[router] slow_path_patterns = ["foo", "bar"]` parses correctly.
    - `test_router_default_orchestrator` — `[router] default = "orchestrator"` parses.
    - `test_router_default_invalid_raises` — `[router] default = "subagent"` → `ConfigError` from Literal enforcement.

12. **Integration test `tests/integration/test_complex_turn.py`** (NEW; PRD Journey 3 — the Epic 4 capstone):
    - **Mock**: Cartesia (synthetic audio chunks at 50ms intervals); STT (returns canned transcript "what's on my calendar today"); Talker (mocked but not consumed — slow path bypasses it); Wake-word (single trigger). `OrchestratorClient` mocked at the Protocol seam — yields the **full event sequence**: `narration → subagent_started → subagent_progress → subagent_done → response_chunk × N → turn_end`.
    - **Real**: `ActivityFSM`, `LogEventPublisher`, `MoodController`, `MicModeRouter`, `_GreetingInjectorProcessor`, `SegmenterProcessor`, `_PrePublishProcessor`, full splitter chain.
    - **Configuration**: `RouterConfig(slow_path_patterns=["calendar"], default="talker")` so the canned transcript escalates.
    - **Drive 30 simulated complex turns** (per epics.md AC):
      1. Wake-word fires; FSM `sleeping → waking → listening`.
      2. STT emits canned transcript "what's on my calendar today".
      3. TurnRouter matches `"calendar"` pattern → `target="orchestrator"`.
      4. `TurnDispatchProcessor` delegates to orchestrator dispatch.
      5. FSM `listening → working[thinking] → working[delegating]`.
      6. Orchestrator emits the canned event sequence.
      7. `TalkerResponseFrame`s flow through the splitter; Cartesia synthesizes; first audio frame leaves the transport.
      8. **MEASURE**: `(end-of-speech timestamp → first narration audio frame timestamp)`. Use `time.monotonic_ns()` (architecture.md §"Test Patterns"). Record per-turn.
      9. FSM `working[delegating] → speaking → listening`.
      10. End of turn; loop.
    - **Assertions** (per epics.md AC):
      - **NFR2 baseline**: compute p50/p95/max across 30 turns. Assert p95 ≤ 1000ms. Print values to stdout AND record in commit message + dev record.
      - FSM publishes `[working[thinking], working[delegating], speaking, listening]` in correct order across each turn (assert via `LogEventPublisher.published` filtered by `ActivityEvent`).
    - **Privacy assertion** mirroring earlier integration tests — no transcript content in INFO+ logs; no orchestrator `response_chunk` text in INFO+ logs.

13. **Integration test for missing-`turn_end`** (`tests/integration/test_complex_turn.py` second test):
    - Use the same harness shape; configure orchestrator mock to drop `TurnEndEvent` after the last `response_chunk`.
    - **Drive 1 turn** (no need for 30; this is a behavior test, not a perf measurement).
    - **Assert**:
      - Pipeline still completes the turn (audio frames flow; FSM transitions `speaking → listening`).
      - WARN `orchestrator.missing_turn_end` is logged.
      - The next turn (after waking again, if continuous-conversation flow holds) still works — pipeline isn't stuck.

14. **`just check` stays green.** Updates required:
    - `tests/unit/turn/test_router.py` (Story 2.4) — 7 new tests per AC #9; update existing tests if `TurnRouter` constructor signature changes.
    - `tests/unit/turn/test_dispatch.py` (Story 2.4 / 4.4) — 8 new tests per AC #10; update existing fixtures for the new constructor args.
    - `tests/unit/test_pipeline.py` — `TurnDispatchProcessor` fixture updates.
    - `tests/unit/config/test_setup.py` — 4 `RouterConfig` tests.
    - `tests/integration/test_simple_turn.py` (Story 2.5) / `test_embodiment_alignment.py` (3.7) / `test_activity_lifecycle.py` (4.3) / `test_intent_sleep.py` (4.4) / `test_wake_greeting.py` (4.5) / `test_continuous_conversation.py` (4.6) — pass `RouterConfig()` with empty patterns; pass orchestrator-related deps (mock or `None` if using Optional).

15. **No transcripts / API keys / raw audio in any log** (NFR25, FR39 — standing). The orchestrator's `response_chunk.text` and `narration.text` are gated to DEBUG only. The `subagent_progress.msg` is system-message-class (not user content) — INFO is fine. Document the field-by-field treatment in the dispatcher's docstring.

16. **v1 fail-fast on orchestrator failure** (per epics.md AC):
    - Story 4.2 already raises `OrchestratorError` on 5xx, transport errors, framing errors, JSON errors. Story 4.7's dispatcher does NOT catch — let propagate.
    - **Stall detection**: Story 4.2's 60s read timeout fires `httpx.ReadTimeout` → wrapped as `OrchestratorError`. Process crashes; systemd restarts. Story 4.7's dispatcher inherits this behavior — no additional stall-detection logic needed.
    - **The `working[delegating]` FSM state is left orphaned on crash** — that's fine. Process restart re-initializes the FSM to `starting`, then `start()` puts it back to `sleeping`. Document this in dev notes.

17. **Architecture compliance** — final Epic 4 invariants:
    - The pipeline now supports both fast-path (Talker tools-using, Story 4.4) and slow-path (orchestrator SSE, this story).
    - The splitter (Story 3.3 / 3.7) is **agnostic to the source** — it consumes `TalkerResponseFrame` regardless of whether the Talker or the orchestrator emitted it. Architecture.md §"Single fan-out point at the splitter" stays preserved.
    - All four event topics (mood, activity, speech_emotion, vocalization) fire correctly across both paths. Slow-path turns produce more activity events (the `working[delegating]` transition is unique to slow-path).
    - **Epic 4 capstone — Journeys J1, J3, J4, J5 all demonstrable** end-to-end. J3 is this story's primary delivery.

## Tasks / Subtasks

- [ ] **Task 1: `RouterConfig` + `[router]` setup.toml block** (AC: #1, #2)
  - [ ] In `src/voice_agent_pipeline/config/setup.py`:
    - Add `class RouterConfig(BaseModel)` with `extra="forbid"`, two fields per AC #2. Class docstring per `feedback_code_comments.md`.
    - Add `router: RouterConfig = Field(default_factory=RouterConfig)` to `SetupConfig`.
  - [ ] In `setup.toml`:
    - Add the `[router]` block per AC #2. Include the example patterns commented out OR active (operator-tunable).
    - Update the trailing "subsequent stories" comment list — remove `[router]`.

- [ ] **Task 2: `TurnRouter` pattern compilation + escalation** (AC: #1, #9)
  - [ ] Open `src/voice_agent_pipeline/turn/router.py`.
  - [ ] Extend `__init__` with `slow_path_patterns: list[str]` + `default_target: Literal["talker", "orchestrator"] = "talker"`.
  - [ ] Compile patterns once: `try: self._compiled_patterns = [re.compile(p, re.IGNORECASE) for p in slow_path_patterns]; except re.error as exc: raise ConfigError(stage="router", reason=str(exc), pattern=...) from exc`. **Track which pattern failed** by iterating manually rather than list comprehension if you need the bad-pattern context.
  - [ ] Update `route()`:
    1. Existing low-confidence clarification check → return as-is.
    2. NEW: scan compiled patterns; if any matches, log INFO `router.escalated_to_orchestrator` with the matched pattern's source string + transcript_length; return `RouteDecision(target="orchestrator", text=transcript, clarification=False)`.
    3. Fall back to `default_target`.
  - [ ] Update `route()` docstring with the new logic.
  - [ ] **Pyright-strict** check.

- [ ] **Task 3: Slow-path dispatcher (extend `TurnDispatchProcessor` OR add `OrchestratorDispatchProcessor`)** (AC: #3, #4, #5, #6, #8)
  - [ ] **Pick design pattern**: A (single dispatcher) or B (helper class). Document the choice in the dev record.
  - [ ] Add the orchestrator-dispatch logic per AC #4 (FSM transition, SSE consumption, event-type dispatch, `TalkerResponseFrame` push).
  - [ ] Add missing-`turn_end` recovery per AC #5: track `turn_end_seen` flag; log WARN at end of stream if not set; **emit `_TurnBoundaryFrame`** downstream after the loop exits to drive segmenter reset (Story 4.3 plumbing).
  - [ ] Remove the existing `NotImplementedError` stub from `TurnDispatchProcessor.process_frame`.
  - [ ] Update constructor signature to accept the new dependencies (`orchestrator: OrchestratorClient`, `activity_fsm: ActivityFSM`, `session_id_supplier: Callable[[], str]`).
  - [ ] Logging per AC #8 — orchestrator subagent events at INFO, response_chunk text at DEBUG only, missing-turn_end WARN.
  - [ ] **Pyright-strict** check: the `match event:` discriminated-union dispatch should narrow correctly given `schemas/stream.py`'s `OrchestratorStreamEvent` union.

- [ ] **Task 4: Pipeline-assembly wiring** (AC: #7)
  - [ ] In `src/voice_agent_pipeline/pipeline.py:run_pipeline`:
    - Construct `turn_router` with `slow_path_patterns=config.router.slow_path_patterns`, `default_target=config.router.default`.
    - Define `_session_id_supplier` per AC #7's pseudocode.
    - Construct `turn_dispatch_processor` with the orchestrator deps.
    - Pipeline list: `TurnDispatchProcessor` already in place (Story 4.4 wiring); no list change needed.
  - [ ] Update `pipeline.py`'s module docstring with a Story 4.7 entry — slow path live; TurnRouter pattern-driven.

- [ ] **Task 5: Unit tests for `TurnRouter` extension** (AC: #9)
  - [ ] Open `tests/unit/turn/test_router.py`. Add 7 new tests per AC #9. Update existing tests if constructor signature changes (most likely they pass `slow_path_patterns=[]` and `default_target="talker"` as defaults — non-breaking).

- [ ] **Task 6: Unit tests for orchestrator dispatch** (AC: #10)
  - [ ] Open `tests/unit/turn/test_dispatch.py` (or `tests/unit/test_pipeline.py` if dispatch tests live there).
  - [ ] **Helper**: factory for an `AsyncMock(spec=OrchestratorClient)` whose `dispatch` returns an async iterator. Pattern:
    ```python
    async def _async_iter(events):
        for e in events: yield e
    orch_mock = AsyncMock(spec=OrchestratorClient)
    orch_mock.dispatch.return_value = _async_iter([NarrationEvent(...), TurnEndEvent()])
    ```
    Note: pyright may complain about `AsyncMock`'s `dispatch.return_value`; use `dispatch = AsyncMock(side_effect=...)` if cleaner.
  - [ ] Implement the 8 test cases per AC #10.
  - [ ] **Use `LogEventPublisher` + real `ActivityFSM`** for the FSM-dependent assertions.

- [ ] **Task 7: `RouterConfig` config tests** (AC: #11)
  - [ ] Open `tests/unit/config/test_setup.py`. Add 4 tests per AC #11.

- [ ] **Task 8: Integration test for J3 complex turn + NFR2 baseline** (AC: #12)
  - [ ] Create `tests/integration/test_complex_turn.py`. Mirror Stories 4.5 / 4.6's harness shape.
  - [ ] **30 simulated turns** with NFR2 measurement per AC #12. Print p50/p95/max to stdout.
  - [ ] **Assert p95 ≤ 1000ms**.
  - [ ] FSM transition assertions per AC #12.
  - [ ] Privacy assertion.

- [ ] **Task 9: Integration test for missing-`turn_end`** (AC: #13)
  - [ ] In the same `test_complex_turn.py` (or sibling `test_complex_turn_missing_end.py` if cleaner). Single-turn shape; assert WARN log + pipeline completion.

- [ ] **Task 10: Update earlier integration test harnesses** (AC: #14)
  - [ ] Pass `RouterConfig()` with empty `slow_path_patterns` to all earlier integration tests so behavior stays unchanged.
  - [ ] Pass mock orchestrator clients (or accept `None` if Optional) to the dispatcher in earlier tests' fixtures.

- [ ] **Task 11: Pass `just check` + `just test` + live smoke** (AC: #14)
  - [ ] `uv run pytest tests/unit/turn/ -v` — Story 2.4 / 4.4 / 4.7's tests all pass.
  - [ ] `uv run pytest tests/integration/test_complex_turn.py -v` — passes; NFR2 baseline recorded.
  - [ ] Full `just check` — green.
  - [ ] Full `just test` — all integration tests green (Stories 2.5, 3.7, 4.3, 4.4, 4.5, 4.6, 4.7).
  - [ ] **Live smoke (manual)** — `just run` on the dev host with a real (or mocked) orchestrator daemon at `http://localhost:8001`:
    - Configure `[router] slow_path_patterns = ["calendar", "weather", "schedule"]` in setup.toml.
    - Speak: "Hey OLAF, what's on my calendar today?" → expect TurnRouter to escalate; orchestrator stream consumed; narration + response audio plays.
    - Watch `ros2 topic echo /olaf/activity` for the `working[delegating]` transition.
    - Speak: "Hey OLAF, tell me a joke" → fast-path (Story 4.4); orchestrator NOT contacted.
    - Document live observations in commit message + dev record.
  - [ ] **NFR2 baseline measurement** captured in the commit message.

- [ ] **Task 12: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] Single commit covering all the new + modified files. Suggested commit message: `Story 4.7: TurnRouter slow-path + complex-turn integration (J3, NFR2 baseline)`.
  - [ ] `git push` immediately after.
  - [ ] **Sprint-status flips to `review`**: `4-7-turn-router-slow-path-and-complex-turn`. **AND** `epic-4: in-progress → review` (Epic 4 capstone closed).

## Dev Notes

### Architectural intent — Story 4.7 is the Epic 4 capstone

Story 4.7 closes Epic 4. Before 4.7: Talker tools-using (4.4), Activity FSM (4.3), wake greeting (4.5), continuous-conversation mic flip (4.6) all exist independently. **Slow-path was scaffolded but never live** — the `NotImplementedError` stub in TurnRouter (Story 2.4) made the orchestrator branch unreachable.

After 4.7: the full conversation surface works. User asks "what's on my calendar today?" → TurnRouter escalates → orchestrator dispatches via SSE → narration audio plays while subagent runs → response chunks stream as the answer arrives → FSM tracks `working[delegating]` so external observers (embodiment subscribers) see "OLAF is thinking, hand off to a subsystem" distinctly from "OLAF is thinking locally."

**Land 4.7 last in Epic 4** because it weaves together everything:
- 4.1's `BeliefStateClient` (fast-path grounding) — orthogonal but available.
- 4.2's `OrchestratorClient.dispatch` — consumed here.
- 4.3's FSM `on_dispatch_to_orchestrator` — called here.
- 4.4's `TurnDispatchProcessor` — extended here with the orchestrator branch.
- 4.5's wake greeting — orthogonal but verified to coexist.
- 4.6's mic-mode flip — verified to handle "user starts new turn while slow-path is mid-stream" (i.e., a wake-word during `working[delegating]` is illegal per FSM contract; if it ever fires, fail-fast).

The integration test (AC #12) exercises the full orchestrator path end-to-end. The NFR2 baseline (≤1000ms p95) is the architectural contract — Story 5.4 calibrates against real ambient + real orchestrator.

### Slow-path pattern matching — keep it dumb

The `slow_path_patterns` are **simple regex strings**, no LLM-based intent classification. Pros:
- **Predictable**: an operator can grep the log for "router.escalated_to_orchestrator" and know exactly which patterns fire.
- **Fast**: pre-compiled regex; sub-millisecond per turn.
- **Tunable**: operator owns the patterns in `setup.toml`; no model retraining.

Cons:
- **Brittle**: "what's coming up tomorrow" doesn't match `calendar` unless the operator adds `tomorrow` to patterns.
- **Conflicts**: "tell me about a calendar app" routes to orchestrator even though the user wants a Talker reply.

V1 stance: brittle is acceptable; the user can rephrase. Story 5.4 calibration may extend to a small intent classifier (LLM-as-a-router) — that's a v2-shaped change, not v1.

**Empty `slow_path_patterns` (default)**: every turn goes to Talker; the orchestrator path is dead code. This is the v1 default — operators opt in.

### Dispatcher pattern A vs pattern B — recommend single dispatcher

The architecture (epics.md AC) frames `OrchestratorDispatchProcessor` as a separate concept ("`TurnDispatchProcessor`... routes to a new `OrchestratorDispatchProcessor`"). Two encodings:
- **Pattern A**: single `TurnDispatchProcessor` with branched `process_frame` body. The "OrchestratorDispatchProcessor" is conceptual but lives as a private helper or method.
- **Pattern B**: literally a separate `FrameProcessor` class downstream of `TurnDispatchProcessor`.

**Recommend A** — Pattern B duplicates the `TranscriptFrame` consumer logic and the route-decision computation. The dispatcher's responsibility is "consume a transcript, route, dispatch to the right backend" — that's one cohesive job.

If the dev finds the single processor body grows >100 lines, refactor to a helper class (no inheritance — composition: `TurnDispatchProcessor` holds an `_orchestrator_dispatcher: _OrchestratorDispatcher` instance). Document the choice. Either satisfies the AC.

### Missing-`turn_end` — why not raise?

Architecture is "fail-fast on broken contracts" (CLAUDE.md rule #4). But missing-`turn_end` is **fixable degradation** — the stream closed without the sentinel; the actual response was delivered (response_chunks landed). Crashing here loses a usable turn for a recoverable issue.

Per epics.md AC: WARN log + flush splitter + let FSM transition naturally. The *contract* violation is the orchestrator's bug; the pipeline's response is "log loudly so the operator sees it; deliver what we have; continue."

This is the narrow exception to "missing data crashes" — the data IS there (response_chunks), only the trailing sentinel is missing. Compare to Story 4.2's "broken JSON in event" which DOES crash — that's data corruption, not just a missing post-hoc marker.

### `session_id` semantics

The orchestrator uses `session_id` to stitch multi-turn context (e.g., follow-up "what about tomorrow?" after "what's on my calendar today?"). Story 4.7's `session_id_supplier` returns the **per-turn `correlation_id`** — same id across all events of the same turn, different across turns.

**Should `session_id` be PER-CONVERSATION (across all turns)?** Architecture.md §"Stories 4.x scope" doesn't pin this. **v1 stance**: per-turn (matches `correlation_id`). The orchestrator daemon can stitch context across turns via its own session-aware logic (architecture.md §"Belief-state read") — the pipeline doesn't need to track conversation-wide IDs.

If a Story 5.x soak shows the orchestrator needs conversation-wide IDs, switch the supplier to a single UUID per conversation (lifecycle: bind on `sleeping → waking`; clear on `going_to_sleep → sleeping`). v1 stays simple.

### NFR2 baseline measurement

The integration test measures `(end-of-speech timestamp → first narration audio frame timestamp)`. Where the budget goes:
- STT inference (faster-whisper): ~100-300ms (depends on utterance length + model size).
- TurnRouter pattern match: <1ms.
- `activity_fsm.on_dispatch_to_orchestrator`: <10ms (state mutation + publish).
- `OrchestratorClient.dispatch` open + first event: 50-300ms (localhost HTTP round-trip + orchestrator's TTFB to emit first `narration`).
- Splitter consume + Cartesia first byte: 200-400ms (Cartesia Sonic-3 TTFB).
- Audio buffer to speaker first frame: 50-100ms.

Total: 410-1110ms. **The 1000ms budget is tight on real hardware**; the synthetic test uses mocked Cartesia (fast) + mocked orchestrator (controlled timing) so the synthetic baseline should comfortably hit p95 ≤ 1000ms. Real-world p95 lands in Story 5.4's soak.

### Live smoke requires an orchestrator daemon

Story 4.7's live smoke needs a live orchestrator daemon at `http://localhost:8001`. Two options:
1. Run the actual orchestrator project (sibling repo). **Recommended** — exercises the real cross-project integration.
2. Run a mock daemon — small FastAPI script that exposes `/health`, `/turn`, `/beliefs` with canned responses. **Acceptable if the orchestrator project isn't running**; document in dev notes.

**For Story 4.7's commit**: the integration test (Task 8) uses the mock at the Protocol level — `OrchestratorClient` itself is mocked, no real HTTP. The live smoke is the operator's verification step; if the orchestrator daemon isn't available, document the deferral and rely on the integration test's NFR2 measurement.

### What this story does NOT do

- **No barge-in** — v1.5 backlog (`v1.5-1-barge-in`).
- **No `cancel(session_id)` impl** — Story 4.2 stubbed it; v1.5 lands the wiring.
- **No subagent-driven embodiment events** — `subagent_*` events log only; future could emit "still thinking" filler audio. Out of scope for v1.
- **No retry on orchestrator failure** — v1 fail-fast (architecture.md §"V1 Posture"). Story 4.2 raises; Story 4.7 propagates.
- **No conversation-wide session_id** — per-turn, matches `correlation_id`. v1.x may revisit.
- **No LLM-based intent routing** — simple regex patterns; v2 territory.

### Project structure notes

This story creates:
- `tests/integration/test_complex_turn.py` — J3 + NFR2 baseline + missing-turn_end behavior tests.

It modifies:
- `src/voice_agent_pipeline/turn/router.py` — `TurnRouter` pattern compilation + escalation logic; constructor signature.
- `src/voice_agent_pipeline/pipeline.py` — `TurnDispatchProcessor` orchestrator branch (replace `NotImplementedError` with live dispatch); session_id supplier; `_TurnBoundaryFrame` emission on stream end.
- `src/voice_agent_pipeline/config/setup.py` — `RouterConfig` model.
- `setup.toml` — `[router]` block.
- `tests/unit/turn/test_router.py` — 7 new tests + existing-test updates.
- `tests/unit/turn/test_dispatch.py` — 8 new tests + fixture updates.
- `tests/unit/test_pipeline.py` — possible fixture updates.
- `tests/unit/config/test_setup.py` — 4 `RouterConfig` tests.
- `tests/integration/test_simple_turn.py` (Story 2.5) / `test_embodiment_alignment.py` (3.7) / `test_activity_lifecycle.py` (4.3) / `test_intent_sleep.py` (4.4) / `test_wake_greeting.py` (4.5) / `test_continuous_conversation.py` (4.6) — pass `RouterConfig()` with empty patterns; pass mock orchestrators.
- `build_documents/implementation-artifacts/sprint-status.yaml` — Story status flip + `epic-4: in-progress → review`.

It does NOT modify:
- `src/voice_agent_pipeline/turn/orchestrator.py` — Story 4.2's territory; Story 4.7 only consumes.
- `src/voice_agent_pipeline/turn/beliefs.py` — Story 4.1's territory; orthogonal to slow-path.
- `src/voice_agent_pipeline/activity/machine.py` — Story 4.3's territory; Story 4.7 only invokes `on_dispatch_to_orchestrator`.
- `src/voice_agent_pipeline/turn/talker.py` / `tools.py` — Story 4.4's territory; orthogonal to slow-path.
- `src/voice_agent_pipeline/audio/*` — Stories 1.5/1.6/1.7/2.1/4.6 territory.

### Testing standards

- **`pytest-asyncio`** in auto mode.
- **Mock at Protocol seams only**: `OrchestratorClient` (`AsyncMock(spec=OrchestratorClient)`); real `ActivityFSM` + `LogEventPublisher` for FSM-dependent assertions.
- **Async iterator mock pattern**: build an async generator function that yields the test events; assign to `dispatch.return_value` (or `dispatch.side_effect`).
- **One behavior per test** — 7 + 8 + 4 unit tests + 30-turn NFR2 + 1 missing-turn_end integration tests.
- **Privacy assertions** mirror earlier stories (no transcript / response text in INFO+ logs).
- **Pyright strict on `src/`** — `match event:` discriminated-union dispatch should narrow correctly; `Callable[[], str]` for `session_id_supplier`.

### Performance budget

NFR2: ≤1000ms p95 from end-of-speech to first narration audio frame. Story 4.7's synthetic test measures this; Story 5.4 validates against live providers + real orchestrator + real network.

If synthetic p95 > 1000ms: bug in plumbing. Likely candidates:
- TurnRouter pattern compilation in the hot path (verify it's at construction time, not per-turn).
- Splitter accumulating events before flushing (verify segmenter emits per-chunk).
- Cartesia mock not yielding fast enough (verify the mock's `asyncio.sleep` intervals).

NFR32: tool-call dispatch overhead bounded — Story 4.4 establishes the baseline; Story 4.7 verifies the orchestrator path doesn't accidentally serialize tool dispatch (e.g., if a slow-path turn somehow emits tool calls — it doesn't, but the architecture must not preclude it).

### What "done" looks like

- `just check` exits 0.
- `just test` exits 0; `test_complex_turn.py` reports p95 ≤ 1000ms across 30 turns.
- `just run` end-to-end on the dev host:
  - "what's on my calendar today" routes to orchestrator; live smoke produces audio response.
  - "tell me a joke" routes to Talker; fast-path works.
  - `ros2 topic echo /olaf/activity` shows `working[delegating]` transition during slow-path turns.
- Commit message records the synthetic NFR2 baseline (p50/p95/max).
- Sprint-status flips to `review` for Story 4.7 AND `epic-4` (Epic 4 capstone closed).
- **All 7 Epic 4 stories now in `review`**; Epic 4 retrospective is the natural follow-up (`bmad-retrospective` skill).

### Epic 4 retrospective hook

After Story 4.7's `review` flip, the natural next step is `bmad-retrospective` for Epic 4. Sample retrospective topics:
- Did the FSM-as-spine design hold up across 7 stories?
- Was the activity-FSM-driven turn boundary (replacing Story 3.7's stopgap) cleaner in practice?
- Did the per-story commit policy work for the Epic 4 sequencing, or did batching make sense in places?
- NFR2 / NFR30 / NFR32 baselines — how close to the real production numbers (Story 5.4 will validate)?

Sprint-status's `epic-4-retrospective: optional` entry stays optional; flag it as next-step in the commit message if the retrospective is desired.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Talker placement in Pipecat] — Single TurnRouter + dispatcher pattern; orchestrator client as dispatcher dependency.
- [Source: build_documents/planning-artifacts/architecture.md#Streaming consumption] — TurnRouter consumes SSE async iterator; yields events as they arrive; no buffering.
- [Source: build_documents/planning-artifacts/architecture.md#Activity FSM + Mood Control + Tool Registry (Batch 6)] — `working[delegating]` sub-mode for slow-path tracking.
- [Source: build_documents/planning-artifacts/architecture.md#NFR2] — slow-path turn budget ≤1000ms p95 (this story records the baseline).
- [Source: build_documents/planning-artifacts/prd.md#FR9] — TurnRouter target selection.
- [Source: build_documents/planning-artifacts/prd.md#FR11] — orchestrator dispatch over SSE.
- [Source: build_documents/planning-artifacts/prd.md#FR14] — missing-`turn_end` recovery.
- [Source: build_documents/planning-artifacts/prd.md#NFR2] — slow-path turn budget.
- [Source: build_documents/planning-artifacts/prd.md#NFR25, FR39] — privacy invariants.
- [Source: build_documents/planning-artifacts/epics.md#Story 4.7] — full AC list (Story 4.7's source of truth).
- [Source: build_documents/planning-artifacts/epics.md#Epic 4 Goal] — "OLAF actually answers complex questions via orchestrator."
- [Source: build_documents/implementation-artifacts/4-2-orchestrator-client-sse.md] — `HttpOrchestratorClient.dispatch` consumer pattern; forward-compat unknown-event handling; broken-contract raise.
- [Source: build_documents/implementation-artifacts/4-3-activity-fsm-core.md] — `on_dispatch_to_orchestrator` transition method; `_TurnBoundaryFrame` emission pattern.
- [Source: build_documents/implementation-artifacts/4-4-talker-tool-using-upgrade.md] — `TurnDispatchProcessor` constructor pattern; text-first dispatch invariant.
- [Source: src/voice_agent_pipeline/turn/router.py] — `TurnRouter` baseline (Story 2.4); modification target.
- [Source: src/voice_agent_pipeline/turn/orchestrator.py] — `OrchestratorClient` Protocol + `HttpOrchestratorClient` impl (Story 4.2).
- [Source: src/voice_agent_pipeline/schemas/stream.py] — `OrchestratorStreamEvent` discriminated union; pattern-matching dispatch target.
- [Source: src/voice_agent_pipeline/pipeline.py:208-271] — `TurnDispatchProcessor` baseline; orchestrator-branch `NotImplementedError` to remove.
- [External: https://docs.python.org/3.12/library/re.html#re.compile] — regex compilation reference.
- [External: https://docs.python.org/3.12/reference/compound_stmts.html#match] — `match` statement type-narrowing reference.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

### Completion Notes List

### File List

## Change Log

| Date | Change |
|---|---|
| 2026-05-07 | Story 4.7 prepared — TurnRouter slow-path + complex-turn integration (J3, NFR2 baseline). Epic 4 capstone. |
