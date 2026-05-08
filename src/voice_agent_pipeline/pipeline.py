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

import time
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

from voice_agent_pipeline.activity.machine import ActivityFSM
from voice_agent_pipeline.audio._silence import suppress_native_stderr
from voice_agent_pipeline.audio.devices import resolve_audio_devices
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
from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
)
from voice_agent_pipeline.splitter.mapping import LastPublishedCache
from voice_agent_pipeline.splitter.segmenter import Segment, Segmenter
from voice_agent_pipeline.stt import STTBackend, build_stt_backend
from voice_agent_pipeline.tts.cartesia import CartesiaClient
from voice_agent_pipeline.tts.client import TTSClient
from voice_agent_pipeline.turn import build_talker
from voice_agent_pipeline.turn.beliefs import HttpBeliefStateClient, async_http_client
from voice_agent_pipeline.turn.orchestrator import HttpOrchestratorClient
from voice_agent_pipeline.turn.router import TurnRouter

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

    v1 dispatch table:

    - ``decision.target == "talker"`` -> ``await router.talker.complete(...)``
      -> emit :class:`TalkerResponseFrame`.
    - ``decision.target == "orchestrator"`` -> ``NotImplementedError``
      (Story 4.3 wires the orchestrator path; the explicit raise is
      the wall, not silent fall-through).

    Errors from ``talker.complete`` propagate as
    :class:`TalkerError` (CLAUDE.md rule #4 forbids catching
    ExternalServiceError downstream — process crashes, systemd
    restarts).
    """

    def __init__(self, router: TurnRouter) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._router = router

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """On TranscriptFrame: route, dispatch, emit TalkerResponseFrame."""
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
                response_text = await self._router.talker.complete(decision.text)
                latency_ms = (time.time_ns() - started_ns) // 1_000_000
                log.info(
                    "talker.responded",
                    latency_ms=latency_ms,
                    clarification=decision.clarification,
                )
                await self.push_frame(
                    TalkerResponseFrame(text=response_text),
                    direction,
                )
            else:
                # Story 4.3 will wire this branch; raising explicitly
                # makes a misconfiguration scream rather than fall through.
                raise NotImplementedError(
                    "orchestrator path is wired in Epic 4 (Story 4.3); "
                    f"got target={decision.target!r}"
                )

        # Pass the original frame through so future stages can observe.
        await self.push_frame(frame, direction)


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
            # Story 4.2: orchestrator client is now wired in for Story 4.7's
            # slow-path dispatch (TurnRouter target="orchestrator"). Story
            # 2.4's NotImplementedError stub in TurnDispatchProcessor still
            # gates the actual call site — Story 4.7 removes it.
            router = TurnRouter(config.stt, talker, orchestrator=orchestrator_client)
        else:
            # Dev mode: no daemon coupling. Belief reads are skipped
            # (Story 4.4: ``beliefs=None`` → plain system prompt); the
            # orchestrator branch in ``TurnDispatchProcessor`` is gated
            # by its own NotImplementedError, which is the right error
            # if a turn somehow routes there with the daemon disabled.
            log.info("daemon.disabled", reason="[daemon] enabled = false")
            talker = build_talker(config, beliefs=None)
            router = TurnRouter(config.stt, talker, orchestrator=None)

        # Story 3.6: mood module. State + controller wired BEFORE
        # publish_initial so the first event lives on the latched topic.
        mood_state = MoodState(initial=config.mood.initial)
        mood_controller = MoodController(
            mood_state,
            event_publisher,
            cooldown_publishes_per_hour=config.mood.cooldown_publishes_per_hour,
        )
        await mood_controller.publish_initial()

        # Story 4.3: activity FSM. Constructed AFTER the publisher
        # connects (FSM transitions publish via event_publisher) and
        # BEFORE the pipeline starts (start() publishes the initial
        # ``starting → sleeping`` transition + first ``wake_word_only``
        # mic-mode signal). Story 4.5 will inject the wake-greeting
        # callback here; Story 4.6 will subscribe to mic_mode_queue.
        activity_fsm = ActivityFSM(publisher=event_publisher)
        await activity_fsm.start()

        # Story 3.2 + 3.3 + 3.7: segmenter + cache, then the processors
        # that drive them in the Pipecat pipeline.
        segmenter = Segmenter(mapping)
        cache = LastPublishedCache()
        segmenter_processor = SegmenterProcessor(segmenter, cache)

        pipeline = Pipeline(
            [
                transport.input(),
                wakeword,
                vad,
                SttProcessor(stt_backend),
                _SttResultLogger(config.stt.low_confidence_threshold),
                _WakewordEventLogger(),
                # Story 4.3: drive FSM transitions from wake + VAD frames.
                _FsmEventBridge(activity_fsm),
                TurnDispatchProcessor(router),
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
