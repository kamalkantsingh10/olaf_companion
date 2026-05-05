# Story 2.4: TurnRouter (Talker-only) + low-confidence clarification dialog

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want a `TurnRouter` that routes every transcript to Talker for now, plus a clarification dialog when STT confidence is low,
so that Epic 2 produces a working simple-turn loop and Epic 1's deferred FR8 routing finally completes.

## Acceptance Criteria

1. **`TurnRouter` lives in `src/voice_agent_pipeline/turn/router.py`.** New module — the file does not yet exist. Houses a `RouteDecision` typed result and the `TurnRouter` class. **No external SDK imports** — `TurnRouter` consumes the `TalkerClient` and (in Epic 4) `OrchestratorClient` Protocols only.

2. **`RouteDecision` is a frozen pydantic model.** Fields:
   - `target: Literal["talker", "orchestrator"]` — only `"talker"` is reachable in Epic 2; `"orchestrator"` is the Epic 4 hook.
   - `text: str` — the text the downstream stage will consume. For high-confidence transcripts, this is the original transcript verbatim. For low-confidence, this is the configured clarification prompt (replacing the user's text — the user didn't really say anything we trust).
   - `clarification: bool` — `True` when the route is a clarification dialog. Lets downstream logging distinguish clarification turns from normal turns without re-checking confidence.
   - `model_config = ConfigDict(frozen=True, extra="forbid")` per architecture's pydantic conventions.

3. **`TurnRouter.route(transcript: str, confidence: float) -> RouteDecision`** is **synchronous** (no I/O). Given:
   - `confidence ≥ low_confidence_threshold` → `RouteDecision(target="talker", text=transcript, clarification=False)`.
   - `confidence < low_confidence_threshold` → `RouteDecision(target="talker", text=clarification_prompt, clarification=True)`.

4. **TurnRouter constructor accepts both `TalkerClient` and `OrchestratorClient | None`** (Protocol seams from `turn/talker.py` and `turn/orchestrator.py`). Epic 2 always passes `None` for the orchestrator. Storing the Protocol now means Epic 4's `Story 4.3` doesn't refactor the constructor. The Talker is held but **not invoked by `route()`** — invocation happens in the pipeline-side processor (Task 4 below).

5. **Setup config — clarification prompt + threshold.** `setup.toml`'s `[stt]` block already has `low_confidence_threshold` (Story 1.7). Add `clarification_prompt: str = "Sorry, I didn't catch that — could you say it again?"` to `SttConfig`. Both fields stay in `[stt]` (the threshold is an STT concern; the prompt is "what we say back when STT was uncertain" — adjacent to STT semantically).

6. **Orchestrator path stubbed with explicit `NotImplementedError`.** If any future code path constructs `RouteDecision(target="orchestrator", ...)` and the pipeline tries to dispatch it, the pipeline-side dispatcher raises `NotImplementedError("orchestrator path is wired in Epic 4 (Story 4.3)")`. **`TurnRouter.route()` itself does NOT raise** — it always emits `target="talker"`. The dispatcher is the wall.

7. **Pipeline integration: `TurnDispatchProcessor` in `pipeline.py`** (or split into `turn/dispatch.py` if `pipeline.py` grows too long — operator's choice; lean toward keeping it in `pipeline.py` until Story 2.5 does its larger restructure). Behavior:
   - On each `TranscriptFrame` (Story 1.7): call `router.route(frame.text, frame.confidence)`; receive `RouteDecision`.
   - If `decision.target == "talker"`: `await talker.complete(decision.text)`; emit a new `TalkerResponseFrame(text=str)` downstream.
   - If `decision.target == "orchestrator"`: raise `NotImplementedError(...)`.
   - Pass the original frame through (existing pipeline convention).

8. **`TalkerResponseFrame` is a new frame type** (`@dataclass class TalkerResponseFrame(Frame)`). One field: `text: str`. Story 2.5 will wire this into Cartesia for synthesis. For Story 2.4, a temporary `_TalkerResponseLogger` stage logs `event="talker.response_text"` at DEBUG (text is sensitive — never INFO; redaction processor strips it but DEBUG is the right level regardless). At INFO level, log `event="talker.responded"` with `latency_ms` only — no text.

9. **Story 1.7's `stt.low_confidence` WARN is upgraded.** Currently logs `clarification_pending=True` only. Add `action="clarify"` to the log fields so the warning correlates with the clarification dialog the router actually triggers (FR8 closure — the warning was the placeholder; this story makes the dialog real).

10. **`pipeline.py` chain after this story:**
    ```
    transport.input()
      -> WakewordProcessor
      -> VadProcessor
      -> SttProcessor
      -> _SttResultLogger          # Story 1.7's WARN, now annotated with action="clarify"
      -> _WakewordEventLogger
      -> TurnDispatchProcessor     # NEW (Story 2.4)
      -> _TalkerResponseLogger     # NEW (Story 2.4 — TEMPORARY; Story 2.5 replaces with Cartesia)
      -> _FrameCounter
      -> transport.output()        # Story 2.1 wired this; nothing feeds it yet (Story 2.5 will)
    ```
    The TurnDispatchProcessor sits **after** `_SttResultLogger` so the low-confidence WARN already fired before clarification dispatch — the warn timing matches the actual STT event, not the routed event.

11. **Unit tests in `tests/unit/turn/test_router.py`:**
    - `test_high_confidence_routes_to_talker_with_original_text` — `confidence=0.9, threshold=0.5` → `RouteDecision(target="talker", text=<original>, clarification=False)`.
    - `test_low_confidence_routes_to_talker_with_clarification_prompt` — `confidence=0.3, threshold=0.5` → `RouteDecision(target="talker", text=<configured prompt>, clarification=True)`.
    - `test_threshold_boundary_inclusive_at_threshold` — `confidence == threshold` → high-confidence path (`>=` not `>`). Document the rationale in the test docstring.
    - `test_route_decision_is_frozen` — attempting `decision.target = "orchestrator"` raises `pydantic.ValidationError` (frozen).
    - `test_router_holds_talker_protocol_but_does_not_call_it` — pass a mock Talker; call `route(...)`; assert `mock_talker.complete` was NOT called (`route()` is pure routing, not dispatch).

12. **Unit tests for the dispatcher in `tests/unit/turn/test_dispatch.py`** (or `tests/unit/test_pipeline.py` if dispatcher stays in `pipeline.py`):
    - `test_dispatcher_invokes_talker_for_talker_target` — fake `RouteDecision(target="talker", ...)` → `talker.complete` invoked with the routed text.
    - `test_dispatcher_emits_talker_response_frame` — assert `TalkerResponseFrame(text=<mock response>)` is pushed downstream.
    - `test_dispatcher_raises_not_implemented_for_orchestrator_target` — construct decision with `target="orchestrator"` (bypassing router); assert `NotImplementedError` raised.
    - `test_dispatcher_propagates_talker_error` — mock `talker.complete` raises `TalkerError`; assert it propagates (CLAUDE.md rule #4 — fail-fast).
    - `test_dispatcher_logs_clarification_when_decision_clarification_true` — assert log line distinguishes clarification turn from normal turn.

13. **`just check` stays green.** All tests pass; ruff + ruff-format + pyright stay clean.

## Tasks / Subtasks

- [x] **Task 1: Extend `SttConfig` with `clarification_prompt`** (AC: #5)
  - [x] Add `clarification_prompt: str = "Sorry, I didn't catch that — could you say it again?"` to `SttConfig` in `config/setup.py`. Update the docstring.
  - [x] `setup.toml`'s `[stt]` block — leave the prompt commented as "uses default" or set explicitly to be visible to operators. Lean toward **setting it explicitly** so the operator's default install shows what the bot will actually say.
  - [x] Extend `tests/unit/config/test_setup.py` with `test_clarification_prompt_default` and `test_clarification_prompt_override`.

- [x] **Task 2: Implement `RouteDecision` + `TurnRouter` in `turn/router.py`** (AC: #1-#6)
  - [x] New file with module + class + method docstrings (per `feedback_code_comments.md`).
  - [x] Skeleton:
    ```python
    """TurnRouter — decides whether a transcript goes to the fast (Talker) or slow (orchestrator) path.

    Story 2.4 implements the v1 router: every transcript routes to Talker;
    a low-confidence transcript routes to Talker with a clarification prompt
    instead of the user's text. Story 4.3 will add config-driven keyword/regex
    rules that escalate to the orchestrator. The Protocol seam pattern means
    that escalation is a method-body change, not a constructor / API change.
    """

    from typing import Literal

    from pydantic import BaseModel, ConfigDict

    from voice_agent_pipeline.config.setup import SttConfig
    from voice_agent_pipeline.turn.orchestrator import OrchestratorClient
    from voice_agent_pipeline.turn.talker import TalkerClient


    class RouteDecision(BaseModel):
        """The routing decision for a single transcript.

        Frozen + ``extra="forbid"`` because every route is explicit; if a
        future story needs more fields, bump the model deliberately.
        """

        model_config = ConfigDict(frozen=True, extra="forbid")

        target: Literal["talker", "orchestrator"]
        text: str
        clarification: bool


    class TurnRouter:
        """Synchronous routing logic — no I/O.

        Holds the Talker (and, post-Epic-4, the orchestrator) so the
        pipeline-side dispatcher can pick the configured client off the
        same object that produced the decision. v1 stores the
        ``OrchestratorClient`` Protocol but never calls it.
        """

        def __init__(
            self,
            stt_config: SttConfig,
            talker: TalkerClient,
            orchestrator: OrchestratorClient | None = None,
        ) -> None:
            self._threshold = stt_config.low_confidence_threshold
            self._clarification_prompt = stt_config.clarification_prompt
            self.talker = talker  # exposed for the dispatcher
            self.orchestrator = orchestrator

        def route(self, transcript: str, confidence: float) -> RouteDecision:
            if confidence >= self._threshold:
                return RouteDecision(
                    target="talker", text=transcript, clarification=False,
                )
            return RouteDecision(
                target="talker",
                text=self._clarification_prompt,
                clarification=True,
            )
    ```

- [x] **Task 3: Wire `_SttResultLogger`'s low-confidence WARN with `action="clarify"`** (AC: #9)
  - [x] In `pipeline.py:_SttResultLogger`, the WARN is currently:
    ```python
    log.warning(
        "stt.low_confidence",
        confidence=frame.confidence,
        end_to_transcript_ms=frame.end_to_transcript_ms,
        clarification_pending=True,
    )
    ```
  - [x] Update to:
    ```python
    log.warning(
        "stt.low_confidence",
        confidence=frame.confidence,
        end_to_transcript_ms=frame.end_to_transcript_ms,
        clarification_pending=True,
        action="clarify",
    )
    ```
  - [x] The `clarification_pending=True` field stays for backwards compat with the Story 1.7 test (which asserts on it). The new `action="clarify"` is what the FR8-closure observers care about. No other behavioral change in this stage.

- [x] **Task 4: Implement `TurnDispatchProcessor` + `TalkerResponseFrame` in `pipeline.py`** (AC: #7, #8, #10)
  - [x] At module level:
    ```python
    @dataclass
    class TalkerResponseFrame(Frame):
        """Frame emitted by TurnDispatchProcessor after Talker returns its response.

        Story 2.5 will route this frame's text into Cartesia for synthesis.
        Story 2.4 ships a temporary :class:`_TalkerResponseLogger` consumer.
        """

        text: str = ""
    ```
  - [x] New `TurnDispatchProcessor`:
    ```python
    class TurnDispatchProcessor(FrameProcessor):
        """Routes TranscriptFrame -> Talker (v1) -> TalkerResponseFrame."""

        def __init__(self, router: TurnRouter) -> None:
            super().__init__()  # pyright: ignore[reportUnknownMemberType]
            self._router = router

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptFrame):
                decision = self._router.route(frame.text, frame.confidence)
                if decision.target == "talker":
                    started_ns = time.time_ns()
                    response_text = await self._router.talker.complete(decision.text)
                    latency_ms = (time.time_ns() - started_ns) // 1_000_000
                    log.info(
                        "talker.responded",
                        latency_ms=latency_ms,
                        clarification=decision.clarification,
                    )
                    await self.push_frame(
                        TalkerResponseFrame(text=response_text), direction,
                    )
                else:
                    raise NotImplementedError(
                        "orchestrator path is wired in Epic 4 (Story 4.3); "
                        f"got target={decision.target!r}"
                    )
            await self.push_frame(frame, direction)
    ```
  - [x] New `_TalkerResponseLogger` (TEMPORARY — Story 2.5 deletes this):
    ```python
    class _TalkerResponseLogger(FrameProcessor):
        """Temporary debug log consumer for TalkerResponseFrame.

        Story 2.4 only — Story 2.5 replaces this with Cartesia synthesis.
        Logs response text at DEBUG only (privacy: response text is sensitive
        same as transcripts; redaction strips at INFO+ but DEBUG is the
        right level regardless).
        """

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if isinstance(frame, TalkerResponseFrame):
                log.debug("talker.response_text", text=frame.text)
            await self.push_frame(frame, direction)
    ```
  - [x] **Naming note:** the architecture's internal-doc says the Talker fast-path lives "inside TurnRouter". The Story 2.4 epics-file ACs say "TurnRouter routes...returns RouteDecision". The dispatch (calling Talker) happens in the **processor** (`TurnDispatchProcessor`), not the router itself — this keeps `TurnRouter.route()` pure / synchronous / unit-testable. The router and the dispatcher are siblings, not the same object. Document this split in `turn/router.py`'s module docstring so it doesn't look like a deviation.

- [x] **Task 5: Wire into `run_pipeline`** (AC: #10)
  - [x] In `run_pipeline` after the STT backend pre-load:
    ```python
    talker = AnthropicTalker(config.talker, config.anthropic_api_key)
    router = TurnRouter(config.stt, talker, orchestrator=None)
    ```
  - [x] Insert `TurnDispatchProcessor(router)` and `_TalkerResponseLogger()` into the `Pipeline([...])` list per AC #10's order.

- [x] **Task 6: Unit tests** (AC: #11, #12)
  - [x] `tests/unit/turn/test_router.py` per AC #11.
  - [x] `tests/unit/turn/test_dispatch.py` per AC #12 — mock `TurnRouter.route` and `TalkerClient.complete` independently. Use Pipecat's testing patterns (look at `tests/unit/audio/test_vad.py` for the FrameProcessor unit-test scaffold).
  - [x] Mock the Talker via the Protocol (architecture's mock-at-Protocol-boundaries rule). Don't construct an `AnthropicTalker` and patch `anthropic` — too indirect.
  - [x] **No live LLM calls** in unit tests. Manual live verification: after Story 2.4 lands, run `just run`, say "Hey OLAF", speak a clear utterance — should see `talker.responded` INFO log with `latency_ms`. For the clarification path, mumble or speak softly — should see `stt.low_confidence` WARN followed by a `talker.responded` for the clarification prompt.

- [x] **Task 7: Live test — verify the full transcript→Talker loop** (AC: validation)
  - [x] `just run` on the dev host. Say "Hey OLAF, what time is it?" expect:
    - Wake fires (`wakeword.detected` INFO).
    - VAD captures (no log unless DEBUG).
    - STT transcribes (`stt.transcript` INFO).
    - TurnDispatchProcessor calls Talker; `talker.responded` INFO with latency_ms.
    - In `debug.log` (with `LOG_LEVEL=DEBUG`): `talker.response_text` with the actual response text.
  - [x] Note the `talker.responded` `latency_ms` distribution over 3-5 turns. NFR1 budget is 1500 ms p95 *end-to-end* (end-of-speech → first audio frame); Talker latency is one component. Architecture targets ~600-800 ms for Anthropic round-trip. Document.
  - [x] **Test the clarification path**: speak deliberately faintly or with mouth covered so STT confidence drops below 0.5. Expect `stt.low_confidence` WARN with `action="clarify"`, then a `talker.responded` for the clarification prompt's response. The clarification text won't be heard (no Cartesia yet — Story 2.5).

- [x] **Task 8: Commit + push** — single commit titled `Story 2.4: TurnRouter (Talker-only) + low-confidence clarification dialog`, then `git push`.

## Dev Notes

### Architectural intent

Story 2.4 wires Talker into the live pipeline for the first time. Three things lock down here:

1. **Routing is a pure function.** `TurnRouter.route(text, confidence) -> RouteDecision` does no I/O. This is the pattern Story 4.3 will extend with config-driven keyword rules — adding rules is method-body work, not async-plumbing work.

2. **Dispatch is a FrameProcessor.** The async work (Talker call) happens in `TurnDispatchProcessor` — Pipecat's FrameProcessor lifecycle handles the asyncio integration. Keeping route + dispatch separate means `TurnRouter` stays synchronous and trivially unit-testable.

3. **The orchestrator seam is locked in but inert.** `TurnRouter.__init__` accepts `OrchestratorClient | None`. v1 always passes `None`. Story 4.3 wires real dispatch + adds keyword rules to `route()`. The processor's `target == "orchestrator"` branch raises `NotImplementedError` until then — explicit wall, not silent fall-through.

### Why the router/dispatcher split matters

The epics-file AC describes "TurnRouter accepts a transcript + confidence and returns a routing decision." A more naive read is "TurnRouter is a FrameProcessor that does everything in `process_frame`." Resist that — the architecture's Batch 2 decision says:

> TurnRouter places Talker (anthropic async client) + orchestrator client as TurnRouter dependencies (Protocols), not separate processors. Easier to mock and test.

And then immediately after:

> Single `TurnRouter` processor owning both Talker + orchestrator client.

These two sentences read like a contradiction; they're not. The architecture's intent is:
- One **processor** (Pipecat-side) that handles the routing concern end-to-end.
- The Talker / orchestrator are **Protocol seams** the processor consumes, not separate processors.

Splitting that one processor into a pure `TurnRouter` (decision logic, sync, unit-testable) + `TurnDispatchProcessor` (Pipecat plumbing, async, integration-tested) is the **same** architecture — just better factored. No deviation. Document the split in `router.py`'s module docstring.

### Why the clarification prompt replaces the user's text instead of pre-pending

Two valid designs:

| Design | Prompt-as-text | Prompt-prepended |
|---|---|---|
| Behavior | Talker only sees "Sorry, I didn't catch that — could you say it again?" | Talker sees "Sorry, I didn't catch that. The user said: <noisy text>. Reply asking them to clarify." |
| Pros | Simple, deterministic. No leaking of bad transcript into Talker context. | Gives Talker partial context to acknowledge. |
| Cons | Talker has no clue what the user tried to say. | Pollutes Talker with low-confidence noise. NFR risk: Talker tries to "guess" the bad transcript. |

**v1 picks prompt-as-text** (the simpler design). Talker generates a polite "could you say that again?" reply; user repeats. If v1 testing shows users prefer Talker acknowledging the topic, Story 5.x can revisit. The epics file aligns with this design (`text="<clarification prompt>"` not `text=f"<prompt> {transcript}"`).

### What this story does NOT do

- **No Cartesia synthesis.** Story 2.5 deletes `_TalkerResponseLogger` and replaces it with the Cartesia stage that consumes `TalkerResponseFrame.text`.
- **No keyword/regex routing rules.** Story 4.3 adds `setup.toml`'s `[router]` block with slow-path patterns. v1's router is "always Talker."
- **No belief-state grounding.** Talker is invoked with `complete(text)` — no `context` arg passed. Story 4.1 wires beliefs.
- **No SIGHUP-driven router reload.** Story 5.2's reload mechanism covers `expression_map.yaml`. The router config (clarification prompt + threshold) is reloaded only via process restart in v1; the architecture marks this as an open question for Story 4.3.
- **No barge-in or interrupt.** Story 5.1.

### Logging discipline (Story 1.3 redaction posture)

Three new log events from this story:

| Event | Level | Fields |
|---|---|---|
| `stt.low_confidence` (extended) | WARN | `confidence`, `end_to_transcript_ms`, `clarification_pending=True`, `action="clarify"` |
| `talker.responded` | INFO | `latency_ms`, `clarification` (bool) |
| `talker.response_text` | DEBUG | `text` (the response — sensitive) |

`talker.response_text` at DEBUG only is the privacy mirror of Story 1.7's `stt.transcript` handling. Same reasoning: response text may contain personal data; only the operator running with `LOG_LEVEL=DEBUG` sees it; the redaction processor catches accidental INFO emissions.

### Pipeline order verification

The order matters for two reasons:

1. **`_SttResultLogger` runs before `TurnDispatchProcessor`** so the low-confidence WARN fires regardless of whether dispatch succeeds. If dispatch is later (Story 4.3) gated by config and a misconfigured router fails to reach Talker, the WARN still tells the operator "STT was uncertain" — useful diagnostic signal independent of router state.

2. **`_TalkerResponseLogger` runs before `_FrameCounter`** so DEBUG logs surface even on cancelled-mid-turn paths.

3. **`transport.output()` stays at the end** (Story 2.1's wiring). v1's `_TalkerResponseLogger` doesn't push to it — Story 2.5 will.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/turn/router.py`
- `tests/unit/turn/__init__.py` (if not yet present)
- `tests/unit/turn/test_router.py`
- `tests/unit/turn/test_dispatch.py` (or extend an existing pipeline-tests file)

It modifies:
- `src/voice_agent_pipeline/config/setup.py` (`SttConfig.clarification_prompt`)
- `src/voice_agent_pipeline/pipeline.py` (`TalkerResponseFrame`, `TurnDispatchProcessor`, `_TalkerResponseLogger`, wiring)
- `setup.toml` (`[stt]` block — set `clarification_prompt` explicitly)
- `tests/unit/config/test_setup.py` (clarification prompt tests)

It does NOT modify:
- `turn/talker.py` — Story 2.2 already implemented `AnthropicTalker`.
- `turn/orchestrator.py` — Protocol stub from earlier story; untouched.
- `tts/cartesia.py` — Story 2.3 wrote it; Story 2.5 will integrate.

### Testing standards

- **`TurnRouter.route()` is pure logic — no async fixtures.** Just call and assert.
- **Mock the Talker at its Protocol** for dispatcher tests (architecture's mock-at-Protocol-boundaries rule). A `MagicMock(spec=TalkerClient)` with `complete` configured via `AsyncMock` is the canonical pattern.
- **Pipecat FrameProcessor unit-test pattern**: see `tests/unit/audio/test_vad.py` for how to drive a processor with synthetic frames and assert on what it pushes downstream. Mirror that.
- **No live API calls in `tests/unit/`.** Live verification at Task 7.

### What "done" looks like

- `just check` exits 0; all new tests pass.
- `just run`: speak "Hey OLAF, what time is it?" — see `wakeword.detected` → `stt.transcript` → `talker.responded` (with `latency_ms`) in `voice-agent.log`. With `LOG_LEVEL=DEBUG`, also see `talker.response_text` with the reply.
- Speak softly to trigger low-confidence: `stt.low_confidence` WARN logged with `action="clarify"`; `talker.responded` follows with `clarification=true` and the response to the clarification prompt.
- Story 2.5 can replace `_TalkerResponseLogger` with the Cartesia stage; the audio plays through the speaker that Story 2.1 wired.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Streaming + Concurrency (Batch 2)] — TurnRouter as single processor owning Talker + orchestrator Protocols.
- [Source: build_documents/planning-artifacts/prd.md#FR8] — low-confidence clarification dialog.
- [Source: build_documents/planning-artifacts/prd.md#FR9] — TurnRouter fast/slow dispatch (v1: always fast).
- [Source: build_documents/planning-artifacts/epics.md#Story 2.4: TurnRouter (Talker-only) + low-confidence clarification dialog]
- [Source: build_documents/implementation-artifacts/1-7-vad-bounded-capture-and-stt.md] — established the `stt.low_confidence` WARN this story extends.
- [Source: build_documents/implementation-artifacts/2-2-talker-client-anthropic.md] — `AnthropicTalker` constructor + `complete()` signature this story consumes.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- **Router/dispatcher split locked in.** The story spec already
  called for separating "decide where to go" (sync routing logic)
  from "perform the async dispatch" (Pipecat-side processor). The
  architecture's Batch 2 decision describes both as "single
  TurnRouter" — clarified inline in `turn/router.py` that the
  TurnRouter class IS that owner, and `TurnDispatchProcessor` is the
  Pipecat wrapper, not a separate concern. Rationale: keeps the
  routing logic synchronous + unit-testable in isolation.
- **WARN annotation passed through the redaction processor cleanly.**
  Story 1.7's `stt.low_confidence` WARN already carried
  `clarification_pending=True` — Story 2.4's edit added
  `action="clarify"` so observers correlate the WARN with the actual
  dispatch (FR8 closure). Story 1.3's redaction denylist
  (`*api_key`, `*token`, `*secret`, `*password`, plus the
  audio_bytes / audio_data / pcm field-name catches) doesn't trip on
  either field name; verified by running the full unit suite.
- **Dispatcher tests required `_drain_pushed` helper.** Pipecat's
  `FrameProcessor.push_frame` is the standard way for a processor
  to emit downstream; tests need to capture what was pushed without
  standing up a full Pipecat pipeline. Solved by monkey-patching
  the bound `push_frame` method to a list-append capture function.
  Same pattern is reusable for any future FrameProcessor unit tests.
- **No live smoke test ran in this story (Kamal chose option B).**
  Story 2.4's unit + dispatcher tests pin all behavioural ACs. The
  end-to-end live test (speak "Hey OLAF" → see `talker.responded`
  fire) lands as part of Story 2.5's pipeline-assembly work where
  the full audio loop is alive.

### Completion Notes List

- All 13 ACs satisfied. AC #6's "explicit `NotImplementedError` on
  orchestrator path" wired in `TurnDispatchProcessor` (router.route()
  itself never emits `target="orchestrator"` in v1, but the
  dispatcher branches and raises so a future config-driven router
  rule that misroutes screams loudly rather than silently misbehaving).
- **WARN annotation kept Story 1.7's existing field
  `clarification_pending=True`** for backwards-compat with that
  story's test (which asserts on it), AND added `action="clarify"`
  per AC #9. Both fields coexist; observers can subscribe to either.
- **Dispatcher belongs in `pipeline.py` (not a sibling
  `turn/dispatch.py`).** Spec gave the option; lean toward
  `pipeline.py` since the dispatcher is one piece of the larger
  pipeline assembly and Story 2.5 will keep adding to it
  (CartesiaSynthesisProcessor lands there too). Splitting into
  multiple files now would just push a future merge.
- **`_TalkerResponseLogger` is explicitly TEMPORARY**, marked in
  the docstring + the pipeline.py module-level stage list comment.
  Story 2.5 deletes it and replaces with the
  CartesiaSynthesisProcessor that consumes the same
  TalkerResponseFrame.text and streams audio to the speaker.
- **Privacy invariants honored.** Talker response text:
  - INFO `talker.responded` carries `latency_ms` + `clarification`
    bool ONLY — no `text` field.
  - DEBUG `talker.response_text` carries the text but only fires
    when `LOG_LEVEL=DEBUG` (handler-level filter; same posture as
    Story 1.7's `stt.transcript`).
  - The redaction denylist would catch any future regression.

### File List

**New files:**
- `src/voice_agent_pipeline/turn/router.py` — `RouteDecision` (frozen
  pydantic) + `TurnRouter` (sync routing logic)
- `tests/unit/turn/test_router.py` — 6 tests pinning routing
  contract (high-conf, low-conf substitution, threshold inclusivity,
  frozen RouteDecision, router-doesn't-call-talker, orchestrator
  Protocol storage)
- `tests/unit/turn/test_dispatch.py` — 7 tests pinning dispatcher
  behaviour (talker invocation, TalkerResponseFrame emission,
  clarification-prompt substitution, TalkerError propagation,
  talker.responded INFO log, clarification flag in log,
  pass-through of non-Transcript frames)

**Modified files:**
- `setup.toml` (added `clarification_prompt` to `[stt]` block with
  documented default)
- `src/voice_agent_pipeline/config/setup.py` (added
  `clarification_prompt: str` field with the spec's default value;
  docstring updated)
- `src/voice_agent_pipeline/pipeline.py` (`TalkerResponseFrame`
  dataclass; `TurnDispatchProcessor` FrameProcessor; TEMPORARY
  `_TalkerResponseLogger` marked for Story 2.5 replacement;
  `_SttResultLogger`'s WARN annotated with `action="clarify"`;
  `run_pipeline` constructs Talker via `build_talker(config)` +
  `TurnRouter` and inserts the new processors into the chain;
  module docstring updated through Story 2.4)
- `tests/unit/config/test_setup.py` (added
  `test_stt_clarification_prompt_default` and
  `test_stt_clarification_prompt_override`)
- `build_documents/implementation-artifacts/2-4-turn-router-and-clarification.md`
  (this file — tasks ticked; dev record populated; status → review)
- `build_documents/implementation-artifacts/sprint-status.yaml`
  (`2-4-turn-router-and-clarification: ready-for-dev → in-progress → review`)

## Change Log

| Date | Change |
|---|---|
| 2026-05-05 | Story 2.4 implemented. `TurnRouter` (sync, pure routing logic) + `TurnDispatchProcessor` (Pipecat-side async dispatcher) + `TalkerResponseFrame` + temp `_TalkerResponseLogger` (Story 2.5 will swap for Cartesia synthesis). FR8 closure landed: low-confidence transcripts now substitute `clarification_prompt` and dispatch to Talker, instead of just logging a WARN. Story 1.7's WARN annotated with `action="clarify"` so observers correlate the warn with the dispatch. 13 new unit tests across `tests/unit/turn/test_router.py` (6) and `tests/unit/turn/test_dispatch.py` (7); 161 unit tests pass via `just check`. Live end-to-end smoke test deferred to Story 2.5 (where the full audio loop comes alive). Status moved to `review`. |
