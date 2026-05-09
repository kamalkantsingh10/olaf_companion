"""Pipecat pipeline assembly + lifecycle orchestration.

Epic 3 capstone (Story 3.7): the simple-turn loop now publishes typed
embodiment events on four ROS 2 topics, anticipating the audio by
30-80 ms (NFR5).

Stage list as of Story 3.7::

    transport.input()
        -> WakewordProcessor          # gates the rest of the chain
        -> VadProcessor               # bounds the utterance
        -> SttProcessor               # transcribes the utterance
        -> _SttResultLogger           # surfaces transcript + confidence
        -> _WakewordEventLogger       # logs wake events for ops
        -> TurnDispatchProcessor      # routes -> Talker
        -> SegmenterProcessor         # Talker text -> SegmentFrame
        -> CartesiaSynthesisProcessor # Segment -> EmbodimentAudioFrame
        -> _PrePublishProcessor       # publish events before each frame
        -> _FrameCounter              # debug-only ticker
        -> transport.output()         # speaker sink

Audio-frame metadata strategy (AC #3, Option A — subclass)
----------------------------------------------------------

:class:`EmbodimentAudioFrame` subclasses :class:`OutputAudioRawFrame`
to add two optional metadata slots: ``speech_emotion_event`` and
``vocalization_events``. Pipecat's frame model is a plain ``@dataclass``
so subclassing is clean (verified against pipecat-ai 1.1.0). The
:class:`_PrePublishProcessor` checks for the metadata on each frame
and publishes via the :class:`EventPublisher` BEFORE forwarding to
``transport.output()``. Audio buffer drain (PyAudio + OS audio +
DDS publish latency) supplies the 30-80ms anticipatory window for free.

Future epics layer onto this without changing the assembly order:

- Epic 4 wires the orchestrator path inside ``TurnDispatchProcessor``
  for slow-path turns; activity FSM transitions publish via the same
  event-publisher seam.
- Story 5.1 adds barge-in (VAD-during-SPEAKING).
"""

import asyncio
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

# Pathlib for the production expression_map.yaml. Imported separately
# so the run_pipeline call site is readable.
from pathlib import Path
from uuid import UUID, uuid4

import structlog
from pipecat.frames.frames import AudioRawFrame, Frame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_agent_pipeline.activity.greeting import trigger_greeting
from voice_agent_pipeline.activity.machine import ActivityFSM, MicMode
from voice_agent_pipeline.audio._silence import suppress_native_stderr
from voice_agent_pipeline.audio.devices import resolve_audio_devices
from voice_agent_pipeline.audio.mic_mode import MicModeRouter
from voice_agent_pipeline.audio.transport import build_audio_transport
from voice_agent_pipeline.audio.vad import UtteranceCapturedFrame, VadProcessor
from voice_agent_pipeline.audio.wakeword import WakeWordDetectedFrame, WakewordProcessor
from voice_agent_pipeline.config.expression_map import load_from_path
from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.logging.startup import StartupReporter
from voice_agent_pipeline.mood.controller import MoodController
from voice_agent_pipeline.mood.state import MoodState
from voice_agent_pipeline.publisher import build_publisher
from voice_agent_pipeline.publisher.interface import EventPublisher
from voice_agent_pipeline.schemas.speech_emotion_event import (
    SpeechEmotionEvent,
)
from voice_agent_pipeline.schemas.stream import (
    NarrationEvent,
    ResponseChunkEvent,
    SubagentDoneEvent,
    SubagentProgressEvent,
    SubagentStartedEvent,
    TurnEndEvent,
)
from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
)
from voice_agent_pipeline.splitter.mapping import LastPublishedCache
from voice_agent_pipeline.splitter.segmenter import Segment, Segmenter
from voice_agent_pipeline.stt import STTBackend, build_stt_backend
from voice_agent_pipeline.tts.cartesia import CartesiaClient
from voice_agent_pipeline.tts.client import TTSClient
from voice_agent_pipeline.turn import build_talker, build_tool_registry
from voice_agent_pipeline.turn.beliefs import HttpBeliefStateClient, async_http_client
from voice_agent_pipeline.turn.orchestrator import HttpOrchestratorClient, OrchestratorClient
from voice_agent_pipeline.turn.router import TurnRouter
from voice_agent_pipeline.turn.tools import ToolRegistry

log = structlog.get_logger(__name__)


@dataclass
class TranscriptFrame(Frame):
    """Pipecat frame emitted by :class:`SttProcessor` after a successful transcription.

    Attributes:
        text: Transcribed text (may be empty if the utterance was silent).
        confidence: Geometric mean of per-segment ``exp(avg_logprob)`` from
            faster-whisper. ``0.0`` to ``1.0``.
        end_to_transcript_ms: Milliseconds from end-of-speech (VAD's
            ``end_ns``) to this frame being emitted. Story 1.7's NFR3
            measurement reads this.
    """

    text: str = ""
    confidence: float = 0.0
    end_to_transcript_ms: int = 0


class SttProcessor(FrameProcessor):
    """Pipecat FrameProcessor — runs STT on each :class:`UtteranceCapturedFrame`.

    The backend is constructed and pre-loaded by :func:`run_pipeline` before
    the pipeline starts, so the per-turn ``transcribe`` call lands fast.
    """

    def __init__(self, backend: STTBackend) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._backend = backend

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """On UtteranceCapturedFrame, transcribe and emit a TranscriptFrame."""
        await super().process_frame(frame, direction)

        if isinstance(frame, UtteranceCapturedFrame):
            result = await self._backend.transcribe(frame.audio)
            # NFR3 metric — end-of-speech to transcript ready.
            elapsed_ms = (time.time_ns() - frame.end_ns) // 1_000_000
            await self.push_frame(
                TranscriptFrame(
                    text=result.text,
                    confidence=result.confidence,
                    end_to_transcript_ms=elapsed_ms,
                ),
                direction,
            )

        # Pass the original frame through so future stages can observe.
        await self.push_frame(frame, direction)


class _SttResultLogger(FrameProcessor):
    """Surfaces transcripts as JSON log events; triggers low-confidence WARN.

    Privacy posture (FR42 + Story 1.3 redaction):
    - INFO log includes ``transcript`` field. The redaction processor in
      :mod:`logging.redaction` strips ``transcript`` at INFO and below;
      it survives only at DEBUG, so transcripts are NOT persisted in the
      default operational path.
    - WARN log on ``confidence < threshold`` carries no transcript text —
      only confidence + clarification flag.
    """

    def __init__(self, low_confidence_threshold: float) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._threshold = low_confidence_threshold

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """On TranscriptFrame: log transcript + maybe a low-confidence WARN."""
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptFrame):
            # Story 2.5 deviation from FR42's strict posture: for v1
            # personal-use the transcribed text surfaces at INFO under
            # the ``heard`` field name. The redaction processor still
            # strips the strict-named ``transcript`` / ``user_text``
            # fields at INFO+ — accidental leaks under those names
            # remain caught. ``heard`` is the deliberate operator-
            # visible alias. For deployed product (Story 5.3) the
            # operator can either rename / remove this field or add
            # ``heard`` to the redaction denylist.
            log.info(
                "stt.transcript",
                confidence=frame.confidence,
                end_to_transcript_ms=frame.end_to_transcript_ms,
                heard=frame.text,
            )
            if frame.confidence < self._threshold:
                # Story 2.4 wired the actual clarification dialog —
                # the TurnRouter substitutes ``clarification_prompt``
                # for the user's noisy text and routes to Talker. The
                # ``action="clarify"`` field is the FR8 closure
                # signal — observers correlate the WARN with the real
                # dialog rather than treating it as a placeholder.
                log.warning(
                    "stt.low_confidence",
                    confidence=frame.confidence,
                    end_to_transcript_ms=frame.end_to_transcript_ms,
                    clarification_pending=True,
                    action="clarify",
                )

        await self.push_frame(frame, direction)


@dataclass
class TalkerResponseFrame(Frame):
    """Pipecat frame carrying the Talker's plain-text reply (Story 2.4).

    Emitted by :class:`TurnDispatchProcessor` after
    :meth:`TalkerClient.complete` returns. Story 2.5's
    ``CartesiaSynthesisProcessor`` consumes this frame's
    :attr:`text` and streams it to the speaker.

    Attributes:
        text: The Talker's response — plain text per the v1 system
            prompt (no SSML; Story 3.5 will rewrite the prompt for
            Cartesia inline emotion tags). May be empty if the
            Talker returned an empty completion (the dispatcher
            still emits the frame so observers see the turn boundary).
    """

    text: str = ""


class TurnDispatchProcessor(FrameProcessor):
    """Pipecat FrameProcessor — TranscriptFrame -> Talker -> TalkerResponseFrame.

    This is the **dispatcher** that pairs with the
    :class:`TurnRouter`'s pure routing logic. The router decides where
    a turn goes; this processor performs the async call and emits the
    response frame downstream. Splitting the two means the router
    stays synchronously unit-testable while the processor handles
    Pipecat's async lifecycle.

    v1 dispatch table (post-Story-4.4):

    - ``decision.target == "talker"`` -> ``await router.talker
      .complete_with_tools(...)`` -> push :class:`TalkerResponseFrame`
      with the text immediately, then kick off
      ``self._tool_registry.dispatch(tc)`` per tool call as
      ``asyncio.create_task`` (fire-and-forget). **Text-first** is
      the architectural rule (FR45 / FR46): the user hears the
      goodbye before the FSM's deferred-sleep flag fires.
    - ``decision.target == "orchestrator"`` -> ``NotImplementedError``
      (Story 4.7 wires the orchestrator path; the explicit raise is
      the wall, not silent fall-through).

    Tool-dispatch error policy (architecture.md §"Tool-call validation"):

    - Validation failures (bad arguments) are caught inside
      :meth:`ToolRegistry.dispatch` and dropped with WARN. The text
      response still flows.
    - Internal sink failures (e.g., :class:`PublisherError` from
      :class:`MoodController.set`) propagate to the background
      ``asyncio.Task``. The done-callback :meth:`_log_tool_done`
      captures the exception via ``log.exception`` so it lands in
      logs rather than disappearing into asyncio's silent-task
      black hole. **Process does NOT crash mid-utterance** — the
      v1 trade-off documented in the story spec; v2 may revisit
      with a smarter retry / surface.

    Errors from ``talker.complete_with_tools`` propagate as
    :class:`TalkerError` (CLAUDE.md rule #4 forbids catching
    ExternalServiceError downstream — process crashes, systemd
    restarts).
    """

    def __init__(
        self,
        router: TurnRouter,
        tool_registry: ToolRegistry,
        orchestrator: OrchestratorClient | None = None,
        activity_fsm: ActivityFSM | None = None,
        session_id_supplier: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._router = router
        self._tool_registry = tool_registry
        # Story 4.7: orchestrator slow-path deps. Optional so tests
        # exercising the fast-path only don't have to wire them. The
        # orchestrator branch raises a clear error if the deps are
        # missing when actually dispatched (see ``_dispatch_orchestrator``).
        self._orchestrator = orchestrator
        self._activity_fsm = activity_fsm
        # Default supplier returns a fresh UUID per call — fine for
        # unit tests that don't care about session correlation.
        # Production wires this from structlog's correlation_id
        # contextvar via run_pipeline.
        self._session_id_supplier: Callable[[], str] = (
            session_id_supplier if session_id_supplier is not None else (lambda: uuid4().hex)
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """On TranscriptFrame: route, dispatch, emit TalkerResponseFrame.

        Text-first ordering (FR45 / FR46):

        1. Talker returns text + tool calls.
        2. Push :class:`TalkerResponseFrame` downstream — the splitter
           starts segmenting and Cartesia starts synthesizing
           immediately.
        3. Kick off each tool dispatch via ``asyncio.create_task``.
           Tasks run alongside TTS; the dispatcher's
           ``process_frame`` returns AFTER pushing the text frame
           but BEFORE tools complete.
        """
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptFrame):
            decision = self._router.route(frame.text, frame.confidence)
            if decision.target == "talker":
                # Both normal and clarification turns route through the
                # Talker (Story 2.4's original design). On clarification,
                # ``decision.text`` is the configured ``clarification_prompt``
                # — phrased as an INSTRUCTION (not as the response itself)
                # so the LLM produces a varied short apology rather than
                # answering it as a question. See setup.toml's [stt]
                # ``clarification_prompt`` for the prompt shape.
                started_ns = time.time_ns()
                response = await self._router.talker.complete_with_tools(
                    decision.text,
                    self._tool_registry,
                )
                latency_ms = (time.time_ns() - started_ns) // 1_000_000
                log.info(
                    "talker.responded",
                    latency_ms=latency_ms,
                    clarification=decision.clarification,
                    tool_call_count=len(response.tool_calls),
                )
                # Step 1: push the text frame IMMEDIATELY. Splitter
                # and Cartesia start working before any tool dispatch
                # has a chance to run. Empty text is fine — the
                # frame still marks the turn boundary for downstream
                # observers.
                await self.push_frame(
                    TalkerResponseFrame(text=response.text),
                    direction,
                )
                # Step 2: fire-and-forget each tool dispatch. We do
                # NOT use ``asyncio.gather`` — gather would await,
                # serializing the dispatcher's process_frame on tool
                # work. ``create_task`` schedules the dispatch on
                # the event loop and returns immediately; the
                # done-callback is the catch boundary for any
                # internal-sink exception.
                for tool_call in response.tool_calls:
                    task = asyncio.create_task(
                        self._tool_registry.dispatch(tool_call),
                    )
                    task.add_done_callback(self._log_tool_done)
            else:
                # Story 4.7: orchestrator slow-path dispatch. The
                # deps are optional at the constructor level; if a
                # turn lands here without them wired, that's a
                # configuration error (the TurnRouter shouldn't
                # have produced ``target="orchestrator"`` if the
                # orchestrator client wasn't supplied).
                if self._orchestrator is None or self._activity_fsm is None:
                    raise RuntimeError(
                        "TurnRouter routed to orchestrator but "
                        "TurnDispatchProcessor was constructed without "
                        "orchestrator + activity_fsm. Check pipeline "
                        "assembly in run_pipeline.",
                    )
                await self._dispatch_orchestrator(decision.text, direction)

        # Pass the original frame through so future stages can observe.
        await self.push_frame(frame, direction)

    async def _dispatch_orchestrator(self, transcript: str, direction: FrameDirection) -> None:
        """Slow-path: dispatch to orchestrator daemon, stream events downstream (Story 4.7).

        Steps (architecture.md §"Talker placement in Pipecat" + epics.md J3 AC):

        1. **FSM transition**: ``working[thinking] → working[delegating]``
           via :meth:`ActivityFSM.on_dispatch_to_orchestrator`. Publishes
           ``ActivityEvent(state="working", working_submode="delegating")``
           so embodiment subscribers can distinguish "OLAF thinking
           locally" from "OLAF delegating to a subagent." The FSM must
           already be in ``working[thinking]``; if not, that method
           raises :class:`VoiceAgentError(reason="illegal_transition")`
           — correct fail-fast (Story 4.3 contract).
        2. **Open SSE stream** via
           :meth:`OrchestratorClient.dispatch`. Each typed event flows
           through a ``match`` block:
           - ``NarrationEvent`` / ``ResponseChunkEvent`` → push
             :class:`TalkerResponseFrame` downstream (same path as
             Talker fast-path replies — splitter / TTS / publisher
             "just work").
           - Subagent events (started/progress/done) → INFO log only
             (no audio impact in v1).
           - ``TurnEndEvent`` → INFO log; flag for missing-end check.
        3. **Missing-`turn_end` recovery** (FR14): track a flag during
           the loop. If the stream closes without ``TurnEndEvent``,
           log WARN ``orchestrator.missing_turn_end``. The splitter
           drains naturally on the audio side; FSM transitions on
           the last audio frame as usual.

        Privacy (NFR25 / FR39): the LLM-emitted text in
        ``narration.text`` and ``response_chunk.text`` is treated like
        a transcript — gated to DEBUG only via the
        ``orchestrator.text_emitted`` event. The redaction processor
        catches any field-name slip; this method just doesn't pass
        text into INFO+ logs in the first place.
        """
        # Tell pyright these are not None (the caller checked).
        assert self._orchestrator is not None  # noqa: S101
        assert self._activity_fsm is not None  # noqa: S101

        # FSM enters working[delegating]. Errors propagate (fail-fast).
        await self._activity_fsm.on_dispatch_to_orchestrator()

        session_id = self._session_id_supplier()
        log.info("orchestrator.dispatch_begin", session_id=session_id)

        turn_end_seen = False
        async for event in self._orchestrator.dispatch(transcript, session_id):
            # Discriminated-union dispatch. ``match`` narrows each
            # case to its concrete event type; pyright infers the
            # ``.text`` / ``.name`` / ``.msg`` fields cleanly.
            match event:
                case NarrationEvent(text=t) | ResponseChunkEvent(text=t):
                    # Privacy: log at DEBUG only (text content is
                    # LLM output, treated like transcript).
                    log.debug(
                        "orchestrator.text_emitted",
                        length=len(t),
                        session_id=session_id,
                    )
                    await self.push_frame(
                        TalkerResponseFrame(text=t),
                        direction,
                    )
                case SubagentStartedEvent(name=n):
                    log.info(
                        "orchestrator.subagent_started",
                        subagent_name=n,
                        session_id=session_id,
                    )
                case SubagentProgressEvent(name=n, msg=m):
                    log.info(
                        "orchestrator.subagent_progress",
                        subagent_name=n,
                        msg=m,
                        session_id=session_id,
                    )
                case SubagentDoneEvent(name=n):
                    log.info(
                        "orchestrator.subagent_done",
                        subagent_name=n,
                        session_id=session_id,
                    )
                case TurnEndEvent():
                    turn_end_seen = True
                    log.info("orchestrator.turn_end", session_id=session_id)

        if not turn_end_seen:
            # FR14: stream ended without explicit turn_end. The splitter
            # drains naturally on the audio side; FSM transitions when
            # the last audio frame leaves the transport (Story 4.3
            # plumbing). Just log so operators can spot the
            # contract drift.
            log.warning("orchestrator.missing_turn_end", session_id=session_id)

    def _log_tool_done(self, task: asyncio.Task[None]) -> None:
        """Done-callback for fire-and-forget ``tool_registry.dispatch`` tasks.

        ``asyncio`` swallows uncaught task exceptions silently — the
        callback re-raises by accessing ``task.result()`` inside a
        try/except, and ``log.exception`` captures the traceback to
        the JSON log. **The pipeline does not crash** even though
        CLAUDE.md rule #4 superficially suggests it should: the
        registry already catches ``ValidationError`` (the only
        "expected" failure mode); anything propagating past it is a
        first-party programming bug. The v1 trade-off — log + continue
        rather than mid-utterance crash — is the better UX (the user
        hears the goodbye partway and the mic flips early on systemd
        restart, vs. the user hears the goodbye fully and an error
        lands in logs). v2 may revisit with a smarter strategy.
        """
        try:
            task.result()
        except Exception:
            # log.exception captures the traceback. The lack of
            # context fields (tool name, etc.) is intentional —
            # the registry's WARN logs already named the tool;
            # this callback's job is to surface the traceback.
            log.exception("tool.dispatch_background_error")


@dataclass
class SegmentFrame(Frame):
    """Pipecat frame carrying one :class:`Segment` from the segmenter (Story 3.7).

    Emitted by :class:`SegmenterProcessor` after the streaming SSML
    state machine + boundary-based segmenter (Story 3.3) close a
    segment. Consumed by :class:`CartesiaSynthesisProcessor`, which
    streams the segment's text to TTS and attaches the segment's
    payloads to the first audio frame's metadata.
    """

    segment: Segment | None = None


@dataclass
class EmbodimentAudioFrame(OutputAudioRawFrame):
    """Audio frame carrying optional embodiment-event metadata (Story 3.7).

    Subclasses :class:`OutputAudioRawFrame` (Pipecat's audio sink type)
    to add two optional slots that :class:`_PrePublishProcessor`
    consumes:

    Attributes:
        speech_emotion_event: Set on the FIRST audio frame of a
            segment whose emotion changed (cache.should_publish allowed).
            ``None`` on subsequent frames of the same segment AND on
            same-emotion-as-prior segments (FR24 dedup).
        vocalization_events: List of ``VocalizationEvent``s captured
            during the segment. Always attached to the first frame of
            a segment that contained vocalizations; never deduped
            (FR24 — vocalizations are always punctual).
    """

    speech_emotion_event: SpeechEmotionEvent | None = None
    # Explicit lambda factory (rather than ``list``) for pyright's
    # type narrowing — ``list`` alone gets typed as ``list[Unknown]``
    # because dataclass.field's overloads can't pin the parameter
    # of an empty ``list()`` call. The explicit annotation on the
    # field handles the rest.
    vocalization_events: list[VocalizationEvent] = field(
        default_factory=lambda: [],
    )


class SegmenterProcessor(FrameProcessor):
    """Drives :class:`Segmenter`; emits :class:`SegmentFrame` on boundaries.

    Consumes :class:`TalkerResponseFrame` from
    :class:`TurnDispatchProcessor`. For each frame:

    1. Drives ``segmenter.consume(text)`` then ``segmenter.flush()``,
       emitting one ``SegmentFrame`` per ``Segment``.
    2. Forwards the original ``TalkerResponseFrame`` so downstream
       stages can observe turn boundaries.

    Resets the segmenter + cache on the next
    :class:`UtteranceCapturedFrame` — the v1 turn-boundary proxy
    (Story 3.7 AC #10). When Story 4.3's activity FSM lands, the
    proxy is replaced with the FSM's ``working → listening``
    transition signal.

    Per-turn ``correlation_id`` is generated on each
    ``UtteranceCapturedFrame`` and bound to every event constructed
    during the same turn — so all four topics' events from one user
    turn share an id (architecture's correlation_id design).
    """

    def __init__(
        self,
        segmenter: Segmenter,
        cache: LastPublishedCache,
    ) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._segmenter = segmenter
        self._cache = cache
        # Per-turn correlation id, bound on UtteranceCapturedFrame.
        # Defaults to a fresh uuid until the first turn — keeps the
        # very first SegmentFrame buildable in tests that bypass the
        # utterance-frame path.
        self._current_turn_id: UUID = uuid4()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, UtteranceCapturedFrame):
            # Turn boundary proxy: reset the segmenter and cache, bind
            # a fresh correlation id for the upcoming turn's events.
            self._segmenter.reset()
            self._cache.reset()
            self._current_turn_id = uuid4()

        if isinstance(frame, TalkerResponseFrame) and frame.text:
            # Drive the streaming SSML state machine over the full
            # response text. v1 Talker returns the complete reply at
            # once (no streaming yet); the segmenter still works
            # correctly, just on a single-chunk input.
            for segment in self._segmenter.consume(frame.text):
                await self.push_frame(SegmentFrame(segment=segment), direction)
            for segment in self._segmenter.flush():
                await self.push_frame(SegmentFrame(segment=segment), direction)

        # Pass through so downstream stages observe upstream events
        # (UtteranceCapturedFrame for any future per-turn observers,
        # TalkerResponseFrame for turn-boundary loggers).
        await self.push_frame(frame, direction)

    @property
    def current_turn_id(self) -> UUID:
        """Read-only access to the current per-turn correlation id.

        Used by :class:`CartesiaSynthesisProcessor` when constructing
        events from segments. The bind happens via shared instance
        access rather than a contextvar to keep the Pipecat pipeline
        deterministic in tests.
        """
        return self._current_turn_id


class CartesiaSynthesisProcessor(FrameProcessor):
    """Streams TTS audio from segments + attaches embodiment metadata (Story 3.7).

    Story 3.7 evolution of the Story 2.5 implementation:

    - **Input frame type changes** from :class:`TalkerResponseFrame` to
      :class:`SegmentFrame`. The segmenter (upstream) handles SSML
      parsing, fallback resolution, and TTS-text construction
      (vocalization keep-vs-strip per ``tts_supported``).
    - **First audio frame of each segment** carries the segment's
      ``speech_emotion_event`` (if cache.should_publish) and any
      ``vocalization_events`` as metadata via :class:`EmbodimentAudioFrame`.
    - **Subsequent chunks** of the same segment are plain
      :class:`OutputAudioRawFrame`s — no metadata, no double-publish.

    The ``segmenter_processor`` reference is the source of the
    per-turn ``correlation_id``; events constructed here pull it
    via ``segmenter_processor.current_turn_id`` so all four topics'
    events from one turn share the same id.
    """

    def __init__(
        self,
        client: TTSClient,
        cache: LastPublishedCache,
        segmenter_processor: SegmenterProcessor,
    ) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._client = client
        self._cache = cache
        self._segmenter_processor = segmenter_processor

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, SegmentFrame) and frame.segment is not None:
            await self._synthesize_segment(frame.segment, direction)

        # Pass-through for upstream observers. SegmentFrame and
        # ancestors go on; downstream distinguishes via isinstance.
        await self.push_frame(frame, direction)

    async def _synthesize_segment(
        self,
        segment: Segment,
        direction: FrameDirection,
    ) -> None:
        """Stream Cartesia audio for ``segment``; attach metadata to first frame."""
        if not segment.text:
            # Empty text — segment carried only vocalizations or was
            # whitespace-stripped. Skip TTS but DO still publish any
            # vocalization events on a synthetic empty frame so
            # embodiment renders the burst.
            return

        turn_id = self._segmenter_processor.current_turn_id

        # Build the speech_emotion event (if dedup allows).
        speech_emotion_event: SpeechEmotionEvent | None = None
        if segment.speech_emotion_payload is not None and self._cache.should_publish(
            segment.speech_emotion_payload
        ):
            speech_emotion_event = SpeechEmotionEvent(
                payload=segment.speech_emotion_payload,
                correlation_id=turn_id,
            )

        # Vocalization events always attach (FR24 — never deduped).
        vocalization_events: list[VocalizationEvent] = [
            VocalizationEvent(payload=p, correlation_id=turn_id)
            for p in segment.vocalization_payloads
        ]

        chunk_count = 0
        byte_total = 0
        async for chunk in self._client.synthesize(segment.text):
            chunk_count += 1
            byte_total += len(chunk)
            if chunk_count == 1:
                # First chunk carries the metadata; embodiment events
                # publish before this frame reaches the speaker.
                await self.push_frame(
                    EmbodimentAudioFrame(
                        audio=chunk,
                        sample_rate=16000,
                        num_channels=1,
                        speech_emotion_event=speech_emotion_event,
                        vocalization_events=vocalization_events,
                    ),
                    direction,
                )
            else:
                # Subsequent chunks: plain OutputAudioRawFrame, no
                # metadata. A single segment publishes its events
                # exactly once, on the leading audio frame.
                await self.push_frame(
                    OutputAudioRawFrame(
                        audio=chunk,
                        sample_rate=16000,
                        num_channels=1,
                    ),
                    direction,
                )

        log.info(
            "tts.synthesis_complete",
            chunk_count=chunk_count,
            byte_total=byte_total,
            had_emotion=speech_emotion_event is not None,
            vocalization_count=len(vocalization_events),
        )


class _GreetingInjectorProcessor(FrameProcessor):
    """Pushes wake-greeting :class:`TalkerResponseFrame` into the splitter chain (Story 4.5).

    The wake greeting flows through the *same* downstream path as a
    Talker reply: splitter → Cartesia → speaker. But the trigger is
    different — it fires from the FSM's ``sleeping → waking``
    transition, not from a transcript. This processor is the
    injection seam.

    Mechanism:

    1. ``process_frame`` records the most recent ``direction`` from
       any frame flowing through (so the injected greeting flows
       in the same direction as the audio stream).
    2. The pipeline assembly site stores a reference to this
       processor and calls :meth:`inject_greeting(text)` from the
       FSM's ``on_sleeping_to_waking`` callback. The injected frame
       is pushed downstream from this processor — the
       :class:`SegmenterProcessor` and downstream Cartesia stage
       see it and synthesize the audio.

    Why a dedicated processor rather than reusing
    :class:`TurnDispatchProcessor`: the dispatcher's contract is
    "consume TranscriptFrame → produce TalkerResponseFrame". The
    greeting has no transcript and no Talker call. Two seams keeps
    each processor single-purpose.
    """

    def __init__(self) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        # Store the most recent direction so injected frames flow the
        # right way. Defaults to DOWNSTREAM (the normal speaker-bound
        # direction) for the case where ``inject_greeting`` is called
        # before any frame has been processed (e.g., very-first wake
        # right after pipeline start).
        self._direction: FrameDirection = FrameDirection.DOWNSTREAM

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Pipecat hook — track direction; pass everything through unchanged."""
        await super().process_frame(frame, direction)
        self._direction = direction
        await self.push_frame(frame, direction)

    async def inject_greeting(self, text: str) -> None:
        """Push a :class:`TalkerResponseFrame` carrying the greeting text.

        Called from the FSM's ``on_sleeping_to_waking`` callback (a
        background task). The pushed frame travels downstream just like
        a Talker reply — segmenter → Cartesia → speaker.
        """
        log.info("greeting.injected", text=text)
        await self.push_frame(TalkerResponseFrame(text=text), self._direction)


class _FsmAudioFlightBridge(FrameProcessor):
    """Drive FSM ``on_first/last_audio_frame`` from the audio flow + pause VAD during speaking.

    Story 4.3 deferred wiring these two FSM transitions to "Story 4.6 /
    4.7 with concrete signal sources." Without them, the FSM gets
    stuck in ``working[thinking]`` forever after the first turn. This
    processor closes that gap.

    State-aware (the 2026-05-09 refit):

    The bridge sees audio frames from THREE distinct sources:
    1. **Greeting audio** — plays during ``waking`` state. NOT a
       turn; FSM stays in ``waking`` until the user starts speaking.
    2. **Bot reply audio** (the linchpin case) — plays during
       ``working`` (then ``speaking``). This is what the FSM
       transitions for: ``working → speaking → listening``.
    3. **Echo / feedback** — bot's own audio bouncing off the
       speaker into the mic. Without an explicit gate, VAD captures
       it, STT transcribes the bot's own words, Talker responds to
       itself, infinite babble loop. The bridge prevents this by
       setting ``vad._active = False`` when entering ``speaking``
       and ``vad._active = True`` when entering ``listening``.

    The bridge fires FSM transitions ONLY when the FSM is in the
    correct preceding state (``on_first_audio_frame`` only when in
    ``working``; ``on_last_audio_frame`` only when in ``speaking``).
    Other audio frames pass through silently. This was the bug in
    the first attempt — greeting audio fired ``on_first_audio_frame``
    from ``waking`` state and triggered illegal-transition errors.

    Last-frame detection:

    Cartesia emits multiple SSE streams per turn (one per segment),
    with gaps of 200-500ms between them. The idle window must be
    longer than that gap or the timer fires prematurely BETWEEN
    segments and the FSM transitions to ``listening`` mid-utterance.
    1500ms is the default; tunable via the constructor.
    """

    def __init__(
        self,
        fsm: ActivityFSM,
        vad: VadProcessor,
        last_frame_idle_ms: int = 1500,
    ) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._fsm = fsm
        # Reference to VAD for the self-feedback gate. The bridge
        # flips ``vad._active`` directly when transitioning between
        # speaking and listening — keeps the audio loop closed
        # without introducing a new pubsub seam.
        self._vad = vad
        self._idle_seconds = last_frame_idle_ms / 1000.0
        # Audio-in-flight flag: True between on_first_audio_frame and
        # on_last_audio_frame. Prevents duplicate ``first`` firings
        # for chunks of the same audio stream.
        self._in_flight = False
        # Pending "last frame" task — re-scheduled on every audio
        # frame; fires when audio truly stops.
        self._last_frame_task: asyncio.Task[None] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Track audio frames flowing downstream; fire FSM transitions on edges."""
        await super().process_frame(frame, direction)

        # Only count downstream-bound output audio frames. Input audio
        # (``InputAudioRawFrame`` / ``_ModeStampedAudioFrame``) flows
        # through this processor too, but those don't represent
        # bot speech and shouldn't drive FSM transitions.
        if isinstance(frame, OutputAudioRawFrame):
            # Cancel any pending "last frame" task — a new audio frame
            # extends the in-flight window.
            if self._last_frame_task is not None and not self._last_frame_task.done():
                self._last_frame_task.cancel()

            if not self._in_flight:
                self._in_flight = True
                # Fire the FSM transition as a background task. The
                # state check happens inside ``_fire_first_audio_frame``
                # so we don't block the audio frame from reaching the
                # speaker even if the FSM is in an unexpected state.
                fire_first = asyncio.create_task(self._fire_first_audio_frame())
                fire_first.add_done_callback(self._log_fsm_done)

            # (Re-)schedule the "last frame" timer.
            self._last_frame_task = asyncio.create_task(self._fire_last_audio_frame_after_idle())
            self._last_frame_task.add_done_callback(self._log_fsm_done)

        # Always pass the frame through unchanged.
        await self.push_frame(frame, direction)

    async def _fire_first_audio_frame(self) -> None:
        """Fire ``fsm.on_first_audio_frame`` only if FSM is in ``working``.

        Greeting audio plays during ``waking`` state — the FSM doesn't
        want a transition there. Echo audio during ``listening`` /
        ``speaking`` similarly doesn't transition. Silently skip in
        those cases.
        """
        if self._fsm.current_state != "working":
            # Greeting (waking) or feedback echo (listening / speaking)
            # — the FSM doesn't transition on this audio. The
            # ``_in_flight`` flag stays True; the timer-driven
            # ``_fire_last_audio_frame_after_idle`` clears it.
            return
        try:
            await self._fsm.on_first_audio_frame()
            # FSM is now in ``speaking``. Pause VAD to break the
            # self-feedback loop — without this, the bot's audio
            # plays out the speaker, hits the mic, VAD captures it,
            # STT transcribes bot's voice, Talker responds to
            # itself, infinite babble. Story 5.1's barge-in feature
            # will reverse this gate selectively.
            self._vad._active = False  # pyright: ignore[reportPrivateUsage]
            log.info("fsm.audio_flight.entered_speaking")
        except Exception:
            log.exception("fsm.first_audio_frame_error")

    async def _fire_last_audio_frame_after_idle(self) -> None:
        """Sleep for the idle window, then fire ``on_last_audio_frame`` if FSM in ``speaking``.

        Cancellation by the next audio frame is the normal path. After
        firing, re-arm VAD so the user's reply gets captured.
        """
        try:
            await asyncio.sleep(self._idle_seconds)
            self._in_flight = False
            if self._fsm.current_state != "speaking":
                # Race: FSM transitioned via another path (e.g.,
                # deferred-sleep mid-stream), or the audio was a
                # greeting that never put us in ``speaking``. Skip.
                return
            await self._fsm.on_last_audio_frame()
            # FSM is now in ``listening``. Re-arm VAD so the next
            # user utterance gets captured. Reset state too — any
            # half-buffered echo audio from speaker bleed into the
            # mic gets dropped.
            self._vad._active = True  # pyright: ignore[reportPrivateUsage]
            self._vad.reset_state()
            log.info("fsm.audio_flight.entered_listening")
        except asyncio.CancelledError:
            # Expected — a new audio frame arrived; the next scheduled
            # task takes over.
            raise
        except Exception:
            log.exception("fsm.last_audio_frame_error")

    @staticmethod
    def _log_fsm_done(task: asyncio.Task[None]) -> None:
        """Done-callback — surface any unexpected exception via log.exception."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            log.exception("fsm.audio_flight_bridge_error", exc_info=exc)


class _PrePublishProcessor(FrameProcessor):
    """Publishes embodiment events before each :class:`EmbodimentAudioFrame` is sent.

    Sits between :class:`CartesiaSynthesisProcessor` and
    ``transport.output()``. For each frame:

    - If it's an :class:`EmbodimentAudioFrame`: publish
      ``speech_emotion_event`` (if set) and every
      ``vocalization_events`` entry, IN ORDER, then forward.
    - Otherwise: forward unchanged.

    The 30-80ms anticipatory window (NFR5) comes from the natural
    delay between ``transport.output()`` queueing the frame and the
    speaker actually playing it (PyAudio buffer + OS audio path +
    DDS publish latency). Story 3.7's integration test pins the
    measured window in the test output.
    """

    def __init__(self, publisher: EventPublisher) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._publisher = publisher

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, EmbodimentAudioFrame):
            # Publish speech_emotion FIRST if present, then every
            # vocalization in order. Architecture: the embodiment
            # subscriber should see the emotion change BEFORE any
            # punctual vocalization (the audio's about to render an
            # `excited [laughter]` phrase — set the pose first, fire
            # the laugh-burst second).
            if frame.speech_emotion_event is not None:
                await self._publisher.publish_speech_emotion(frame.speech_emotion_event)
            for event in frame.vocalization_events:
                await self._publisher.publish_vocalization(event)

        await self.push_frame(frame, direction)


class _FrameCounter(FrameProcessor):
    """No-op terminal stage that counts incoming :class:`AudioRawFrame` objects.

    Logs a DEBUG event every ``log_every`` frames so an operator running
    ``LOG_LEVEL=DEBUG`` can confirm audio is flowing. Non-audio frames
    (system events from Pipecat, :class:`WakeWordDetectedFrame`,
    :class:`UtteranceCapturedFrame`, :class:`TranscriptFrame`, etc.)
    pass through unchanged.
    """

    def __init__(self, log_every: int = 1000) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._count = 0
        self._log_every = log_every

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Pipecat hook — count audio frames and pass everything through."""
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame):
            self._count += 1
            # Modulo log_every keeps this O(1); ~20s between reports at
            # 16 kHz mono with ~20ms frames.
            if self._count % self._log_every == 0:
                log.debug("audio.frame_counter", count=self._count)
        await self.push_frame(frame, direction)


class _WakewordEventLogger(FrameProcessor):
    """Surface :class:`WakeWordDetectedFrame` arrivals as JSON log events.

    INFO-level so the operator's default ``voice-agent.log`` shows wakes.
    Story 4.3's activity FSM uses the same frame as a transition trigger and
    drive state transitions; this logger is a separate concern.
    """

    def __init__(self) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Pipecat hook — log wake events; pass everything through unchanged."""
        await super().process_frame(frame, direction)
        if isinstance(frame, WakeWordDetectedFrame):
            log.info(
                "wakeword.detected",
                keyword=frame.keyword,
                keyword_index=frame.keyword_index,
                timestamp_ns=frame.timestamp_ns,
            )
        await self.push_frame(frame, direction)


class _FsmEventBridge(FrameProcessor):
    """Translate audio-pipeline frames into :class:`ActivityFSM` transitions.

    Story 4.3 — keeps the ``audio/*`` package decoupled from
    ``activity/*``. ``WakewordProcessor`` and ``VadProcessor`` keep
    emitting their existing frames (``WakeWordDetectedFrame``,
    ``UtteranceCapturedFrame``); this bridge consumes both and calls
    the matching FSM method.

    What this story wires:

    - ``WakeWordDetectedFrame`` → ``fsm.on_wake_detected()``
      (``sleeping → waking``).
    - ``UtteranceCapturedFrame`` → ``fsm.on_speech_started()`` then
      ``fsm.on_speech_ended()`` chained together — the v1 VAD only
      emits one frame at end-of-utterance, so the brief ``waking →
      listening`` step happens here in the bridge rather than as a
      separate VAD signal.

    What this story does NOT wire (deferred):

    - ``on_first_audio_frame`` / ``on_last_audio_frame`` — these
      require detecting first/last TTS audio frame leaving the
      transport. Story 3.7's ``_PrePublishProcessor`` doesn't track
      "last frame" yet; Story 4.6 (mic-mode flip) and Story 4.7
      (orchestrator slow-path with ``turn_end`` detection) will land
      this with concrete signal sources.
    - ``_TurnBoundaryFrame`` migration of Story 3.7's segmenter
      reset — also deferred; the existing ``UtteranceCapturedFrame``
      proxy in :class:`SegmenterProcessor` keeps working.

    Why a separate processor (not extending the existing wakeword /
    VAD processors): keeps the audio package decoupled from the
    activity package. ``audio/wakeword.py`` doesn't need to import
    ``activity.machine``; it just emits its detection frame and
    moves on.
    """

    def __init__(self, fsm: ActivityFSM) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._fsm = fsm

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Pipecat hook — drive FSM transitions; forward the frame unchanged."""
        await super().process_frame(frame, direction)

        if isinstance(frame, WakeWordDetectedFrame):
            # If the user manages to fire the wake-word during the
            # deferred-sleep race window (before on_last_audio_frame
            # lands), clear the pending flag first so the next
            # last-frame transitions normally instead of dropping us
            # back to sleeping (FR46 cancellation rule).
            self._fsm.cancel_pending_sleep()
            await self._fsm.on_wake_detected()
        elif isinstance(frame, UtteranceCapturedFrame):
            # End-of-user-speech. v1 VAD doesn't emit a separate
            # speech-started frame; collapse the waking → listening
            # → working[thinking] chain here. Idempotent on
            # on_speech_started if the FSM is already in listening
            # (continuous-conversation flow per Story 4.6).
            if self._fsm.current_state == "waking":
                await self._fsm.on_speech_started()
            await self._fsm.on_speech_ended()

        # Forward the frame downstream so Story 1.7's STT processor
        # and the rest of the chain keep working.
        await self.push_frame(frame, direction)


async def run_pipeline(
    config: SetupConfig,
    *,
    reporter: StartupReporter | None = None,
) -> None:
    """Build and run the full pipeline with embodiment + mood publishing.

    Story 3.7 — Epic 3 capstone. Adds the four-topic event publisher,
    the streaming SSML splitter + segmenter, the mood module, and
    the pre-publish stage that fires events anticipating audio.

    Story 4.1 — wraps the body in ``async with httpx.AsyncClient(...)``
    so a single keep-alive pool serves both the belief-state read
    (Story 4.1) and the orchestrator slow-path SSE consumer
    (Story 4.2). Same daemon origin → same connection pool.

    Args:
        config: Loaded :class:`SetupConfig`.
        reporter: Optional :class:`StartupReporter` from ``__main__``.
            When provided, every startup-phase step is wrapped in a
            ``reporter.stage(...)`` so the operator sees the
            ``[ ✓ ] / [ ✗ ]`` checklist on stderr. Right before the
            pipecat runner starts, ``reporter.mark_startup_complete()``
            is called so the closing rule prints before any runtime
            log noise. ``None`` is accepted for tests / callers that
            don't need the operator UX (the function still works,
            just without checklist output).

    Steps:

    1. Resolve audio devices via the regex patterns in ``config.audio``.
    2. Build the input/output transport (PyAudio-backed).
    3. Build the wake-word processor + VAD processor.
    4. Pre-load the STT backend.
    5. **Build the EventPublisher and connect** (fail-fast on connect
       failure — broadcast bus is a hard dep).
    6. **Open the persistent ``httpx.AsyncClient``** (Story 4.1).
       Lifetime is bound to the rest of ``run_pipeline``; both
       ``HttpBeliefStateClient`` (this story) and Story 4.2's
       ``HttpOrchestratorClient`` share this client.
    7. Build the Talker (with belief-state client) + TurnRouter.
    8. Build the Cartesia TTS client.
    9. Load ``expression_map.yaml`` (Story 3.1).
    10. **Build the mood module**: ``MoodState`` + ``MoodController``.
    11. **Publish initial mood** as the latched startup event.
    12. Build the segmenter + cache + segmenter processor +
        Cartesia synthesis processor + pre-publish processor.
    13. Assemble + run.
    """
    # Use a no-op fallback when no reporter was passed — keeps the
    # call sites below uniform without sprinkling ``if reporter is
    # not None`` everywhere. ``_NullStartupReporter`` mirrors
    # ``StartupReporter``'s ``stage()`` and ``mark_startup_complete()``
    # surfaces but writes nothing.
    rep = reporter if reporter is not None else _NULL_REPORTER

    # Both PyAudio entry points (device enumeration + transport build)
    # spew ALSA/JACK probe diagnostics directly to fd 2 from native
    # code — it bypasses Python logging entirely. Wrap them in
    # ``suppress_native_stderr`` so the StartupReporter's checklist
    # stays clean. The wrapper is restored before the stage's
    # ``__aexit__`` writes the [ ✓ ] / [ ✗ ] line, so the reporter
    # output still reaches stderr normally.
    async with rep.stage("audio_devices", "audio devices"):
        with suppress_native_stderr():
            indices = resolve_audio_devices(
                input_pattern=config.audio.input_device_name,
                output_pattern=config.audio.output_device_name,
            )
            transport = build_audio_transport(config, indices)

    wakeword = WakewordProcessor(
        keyword_paths=[config.wakeword.model_path],
        access_key=config.picovoice_access_key,
        sensitivity=config.wakeword.sensitivity,
    )

    vad = VadProcessor(config.vad)

    # Build + pre-load the STT backend. The model load is the slowest
    # startup step (multi-second cold load on first run, sub-second
    # afterwards), so it gets its own checklist line.
    async with rep.stage("stt_model", "stt model loaded"):
        stt_backend = build_stt_backend(config.stt)
        await stt_backend.load()

    cartesia_client = CartesiaClient(config.tts, config.cartesia_api_key)

    # Story 3.1: load the production expression map. Validates at
    # startup; ConfigError propagates to __main__'s top-level
    # handler if the YAML is malformed (FR31).
    async with rep.stage("expression_map", "expression map loaded"):
        mapping = load_from_path(Path("expression_map.yaml"))

    # Story 3.5: build the four-topic event publisher and connect.
    # connect() raises StartupValidationError on failure; the
    # __main__ handler logs startup.failed CRITICAL and exits.
    async with rep.stage("publisher", "publisher connected"):
        event_publisher = build_publisher(config.publisher)
        await event_publisher.connect()

    # Story 4.1: open the persistent httpx.AsyncClient via the factory
    # in turn/beliefs.py. The factory keeps ``import httpx`` confined to
    # the two adapter files (turn/beliefs.py + turn/orchestrator.py)
    # per the architecture's "External adapter boundaries" invariant.
    # Timeouts are tuned for the daemon's expected response shape; see
    # async_http_client's docstring for the rationale.
    #
    # Dev-mode escape hatch — ``[daemon] enabled = false``: skip the
    # whole HTTP-client + belief + orchestrator block. The Talker
    # receives ``beliefs=None`` (Story 4.4 already handles that path
    # by skipping the belief-grounding system-prompt section); the
    # ``TurnRouter`` is constructed with ``orchestrator=None``;
    # ``TurnDispatchProcessor`` keeps its existing NotImplementedError
    # stub for ``target="orchestrator"`` so a misrouted turn fails
    # loudly. Production deployments leave ``enabled = true`` so the
    # CLAUDE.md rule #4 fail-fast posture stands.
    async with async_http_client() as http_client:
        if config.daemon.enabled:
            # Story 4.1 + 4.2: belief + orchestrator clients share one pool.
            # The orchestrator daemon and the belief-state endpoint live
            # behind one origin; ``httpx.AsyncClient``'s connection pool is
            # keyed by origin so a shared client gives both consumers free
            # keep-alive across belief-reads + orchestrator dispatches.
            belief_client = HttpBeliefStateClient(http_client, base_url=config.daemon.url)
            orchestrator_client = HttpOrchestratorClient(
                http_client,
                base_url=config.daemon.url,
            )

            # Story 4.2: startup probe — refuse to start unless the daemon's
            # ``GET /health`` returns 200. Closes architecture.md's
            # cross-project spec-drift item. Raises StartupValidationError
            # → __main__'s top-level handler logs + exits non-zero. Wrapped
            # in a reporter stage so the failure renders cleanly with
            # ``stage='orchestrator'`` / ``reason=...`` / ``url=...`` from
            # the exception's ``context`` dict.
            async with rep.stage("orchestrator", "orchestrator daemon"):
                await orchestrator_client.probe_health()

            talker = build_talker(config, beliefs=belief_client)
            # Story 4.7: TurnRouter now compiles slow-path patterns +
            # honors the configured default. Patterns from setup.toml
            # under ``[router] slow_path_patterns``; default from
            # ``[router] default``. The dispatcher in
            # ``TurnDispatchProcessor`` consumes the orchestrator client
            # via ``_dispatch_orchestrator`` when a turn escalates.
            router = TurnRouter(
                config.stt,
                talker,
                orchestrator=orchestrator_client,
                slow_path_patterns=config.router.slow_path_patterns,
                default_target=config.router.default,
            )
            orchestrator_for_dispatch: OrchestratorClient | None = orchestrator_client
        else:
            # Dev mode: no daemon coupling. Belief reads are skipped
            # (Story 4.4: ``beliefs=None`` → plain system prompt); the
            # orchestrator client is None, so the dispatcher's
            # orchestrator branch raises a clear error if a turn ever
            # routes there. Operators using ``[daemon] enabled = false``
            # should keep ``[router] slow_path_patterns = []`` so no
            # turn escalates.
            log.info("daemon.disabled", reason="[daemon] enabled = false")
            talker = build_talker(config, beliefs=None)
            router = TurnRouter(
                config.stt,
                talker,
                orchestrator=None,
                slow_path_patterns=config.router.slow_path_patterns,
                default_target=config.router.default,
            )
            orchestrator_for_dispatch = None

        # Story 3.6: mood module. State + controller wired BEFORE
        # publish_initial so the first event lives on the latched topic.
        mood_state = MoodState(initial=config.mood.initial)
        mood_controller = MoodController(
            mood_state,
            event_publisher,
            cooldown_publishes_per_hour=config.mood.cooldown_publishes_per_hour,
        )
        await mood_controller.publish_initial()

        # Story 4.5: wake-greeting injector. Sits in the pipeline AFTER
        # the dispatcher (so it inherits the dispatcher's downstream
        # direction) and BEFORE the segmenter (so injected greetings
        # flow through the same SSML splitter + Cartesia path as Talker
        # replies). The FSM's ``on_sleeping_to_waking`` callback (built
        # below) calls ``greeting_injector.inject_greeting(text)``.
        greeting_injector = _GreetingInjectorProcessor()

        # Story 4.5: greeting closure. Captures ``mood_controller``
        # (current-mood read), ``config.greeting.greetings_by_mood``
        # (the bucket lookup), and ``greeting_injector`` (the push-
        # frame seam). Sub-millisecond — no LLM, no I/O. Logged via
        # ``trigger_greeting``'s own ``greeting.picked`` event.
        async def _on_sleeping_to_waking() -> None:
            # Story 4.6 calibration (2026-05-09): 200ms pause before the
            # greeting fires. Without it, Ooppi replies the instant the
            # wake word is detected — feels jarringly fast, like the bot
            # is interrupting. The brief pause lets the user finish
            # saying "hey OLAF" before the greeting starts.
            await asyncio.sleep(0.2)
            mood = mood_state.current
            text = trigger_greeting(mood, config.greeting.greetings_by_mood)
            await greeting_injector.inject_greeting(text)

        # Story 4.3: activity FSM. Constructed AFTER the publisher
        # connects (FSM transitions publish via event_publisher) and
        # BEFORE the pipeline starts (start() publishes the initial
        # ``starting → sleeping`` transition + first ``wake_word_only``
        # mic-mode signal). Story 4.5 wires the wake-greeting callback
        # here; Story 4.6 wires the MicModeRouter to the mic_mode_queue.
        activity_fsm = ActivityFSM(
            publisher=event_publisher,
            on_sleeping_to_waking=_on_sleeping_to_waking,
        )
        await activity_fsm.start()

        # Story 4.6: mic-mode router. Sits before WakewordProcessor in
        # the pipeline, stamps each AudioRawFrame with the current
        # mode. Wakeword and VAD downstream check the stamp to gate
        # their own processing (FR47 single-stream invariant).
        mic_mode_router = MicModeRouter(activity_fsm.mic_mode_queue)

        # Story 4.6: buffer-clear orchestrator. Fired by the router on
        # every real mode transition. Receives ``(old_mode, new_mode)``
        # so it can be specific:
        #
        # - wake_word_only → vad_stt: Porcupine's rolling buffer is
        #   cleared (defensive — prevents stale post-wake bytes from
        #   being interpreted as a fresh wake check next time we
        #   re-enter wake_word_only). VAD's in-flight state is reset
        #   (the next WakeWordDetectedFrame sets _active anyway).
        # - vad_stt → wake_word_only: VAD's in-flight state dropped
        #   (any partial utterance from before the deferred-sleep
        #   transition is no longer relevant). Porcupine re-engages
        #   automatically because subsequent frames will be stamped
        #   "wake_word_only".
        #
        # STT is stateless between utterances (it processes one
        # ``UtteranceCapturedFrame`` at a time); no reset_state call
        # is needed.
        async def _on_mic_mode_change(old: MicMode, new: MicMode) -> None:
            log.info(
                "mic_mode.buffer_cleared",
                from_mode=old,
                to_mode=new,
                processors_reset=["wakeword", "vad"],
            )
            wakeword.clear_buffer()
            vad.reset_state()

        mic_mode_router.set_on_mode_change(_on_mic_mode_change)

        # Story 4.4: Talker tool registry — single construction site.
        # Captures the FSM and mood_controller references into closures
        # inside the tool dispatch callables, so the registry stays
        # decoupled from those types at the call site. Disabled tools
        # (via [tools] config) are simply omitted from the registry —
        # the LLM never sees them through ``as_openai_tools_param``.
        tool_registry = build_tool_registry(
            config.tools,
            activity_fsm,
            mood_controller,
        )

        # Story 3.2 + 3.3 + 3.7: segmenter + cache, then the processors
        # that drive them in the Pipecat pipeline.
        segmenter = Segmenter(mapping)
        cache = LastPublishedCache()
        segmenter_processor = SegmenterProcessor(segmenter, cache)

        pipeline = Pipeline(
            [
                transport.input(),
                # Story 4.6: stamps every AudioRawFrame with the active
                # mic_mode so wakeword + vad can self-gate. Must run
                # BEFORE wakeword in the chain.
                mic_mode_router,
                wakeword,
                vad,
                SttProcessor(stt_backend),
                _SttResultLogger(config.stt.low_confidence_threshold),
                _WakewordEventLogger(),
                # Story 4.3: drive FSM transitions from wake + VAD frames.
                _FsmEventBridge(activity_fsm),
                # Story 4.4: dispatcher fires ``go_to_sleep`` / ``set_mood``
                # tool calls from Talker (text-first parallel dispatch).
                # Story 4.7: dispatcher now also handles target=
                # "orchestrator" by streaming SSE events from the
                # orchestrator daemon, pushing each narration /
                # response_chunk as a TalkerResponseFrame downstream.
                # Session-id supplier reads the per-turn correlation id
                # set by structlog's contextvars (Story 3.7) so log
                # lines stitch together across the slow-path turn.
                TurnDispatchProcessor(
                    router,
                    tool_registry,
                    orchestrator=orchestrator_for_dispatch,
                    activity_fsm=activity_fsm,
                    session_id_supplier=lambda: str(
                        structlog.contextvars.get_contextvars().get(
                            "correlation_id",
                            uuid4().hex,
                        )
                    ),
                ),
                # Story 4.5: greeting injector. Pushes
                # ``TalkerResponseFrame`` for wake greetings into the
                # same downstream path as Talker replies. The injected
                # frame flows into ``SegmenterProcessor`` next.
                greeting_injector,
                # Story 3.7: SegmenterProcessor consumes TalkerResponseFrame
                # and emits SegmentFrame per boundary.
                segmenter_processor,
                # Story 3.7: CartesiaSynthesisProcessor consumes SegmentFrame
                # and emits EmbodimentAudioFrame (first chunk of each segment)
                # with speech_emotion + vocalization metadata attached.
                CartesiaSynthesisProcessor(
                    cartesia_client,
                    cache,
                    segmenter_processor,
                ),
                # Story 4.6 (calibration 2026-05-09): drive FSM
                # transitions on the audio-flight edges. Without this,
                # the FSM gets stuck in working[thinking] forever
                # after the first turn — Story 4.3 deferred this
                # signal-source wiring and the deferral was never
                # closed in 4.6 / 4.7. Also pauses VAD during
                # ``speaking`` to break the self-feedback loop (bot
                # transcribing its own audio echo). Sits BEFORE
                # _PrePublishProcessor so the FSM transition fires
                # before any embodiment event publish.
                _FsmAudioFlightBridge(activity_fsm, vad),
                # Story 3.7: pre-publish stage — publishes embodiment events
                # before each EmbodimentAudioFrame reaches the speaker.
                _PrePublishProcessor(event_publisher),
                _FrameCounter(),
                transport.output(),
            ]
        )
        task = PipelineTask(pipeline)
        runner = PipelineRunner()

        # All startup probes have passed — close the operator-facing
        # checklist before the pipecat runner starts firing runtime
        # log lines. After this call the structlog console handler is
        # un-quieted, so ``pipeline.started`` and subsequent runtime
        # events render normally.
        rep.mark_startup_complete()

        log.info("pipeline.started")
        try:
            await runner.run(task)
        finally:
            # Story 3.5: disconnect the publisher cleanly on shutdown
            # (idempotent; safe even if connect failed).
            try:
                await event_publisher.disconnect()
            except Exception as e:
                # Disconnect failures are non-fatal — we're shutting down
                # anyway. Log + continue to the rest of the cleanup.
                log.warning("publisher.disconnect_warning", error=str(e))
            log.info("pipeline.stopped")


class _NullStartupReporter:
    """No-op stand-in for :class:`StartupReporter` when none is supplied.

    Used when ``run_pipeline`` is invoked without a reporter (tests,
    embedded-use callers). Mirrors the public surface ``stage()`` +
    ``mark_startup_complete()`` so the call sites stay uniform.
    """

    @asynccontextmanager
    async def stage(self, code: str, description: str):  # type: ignore[no-untyped-def]
        """No-op replacement — yields immediately and discards args."""
        del code, description
        yield

    def mark_startup_complete(self) -> None:
        """No-op — there's no checklist to close."""


# Module-level singleton so ``run_pipeline`` doesn't allocate a fresh
# null-reporter on every invocation. Safe because the null reporter
# holds no state.
_NULL_REPORTER = _NullStartupReporter()
