"""Pipecat pipeline assembly + lifecycle orchestration.

Epic 2 capstone (Story 2.5): the simple-turn loop is now end-to-end.
Mic input → wake-word → VAD → STT → router → Talker → Cartesia →
speaker. Speaking "Hey OLAF, what time is it?" produces an audible
Ooppi reply through the configured speaker.

Stage list as of Story 2.5::

    transport.input()
        -> WakewordProcessor          # gates the rest of the chain
        -> VadProcessor               # bounds the utterance
        -> SttProcessor               # transcribes the utterance
        -> _SttResultLogger           # surfaces transcript + confidence
        -> _WakewordEventLogger       # logs wake events for ops
        -> TurnDispatchProcessor      # routes -> Talker
        -> CartesiaSynthesisProcessor # Talker reply -> audio chunks
        -> _FrameCounter              # debug-only ticker
        -> transport.output()         # speaker sink

Future epics layer onto this without changing the assembly order:

- Epic 3 inserts a streaming SSML splitter between the dispatcher
  and the Cartesia stage (so Talker can emit inline emotion tags).
- Epic 4 wires the orchestrator path inside ``TurnDispatchProcessor``
  for slow-path turns.
- Story 5.1 adds barge-in (VAD-during-SPEAKING).
"""

import time
from dataclasses import dataclass

import structlog
from pipecat.frames.frames import AudioRawFrame, Frame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_agent_pipeline.audio.devices import resolve_audio_devices
from voice_agent_pipeline.audio.transport import build_audio_transport
from voice_agent_pipeline.audio.vad import UtteranceCapturedFrame, VadProcessor
from voice_agent_pipeline.audio.wakeword import WakeWordDetectedFrame, WakewordProcessor
from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.stt import STTBackend, build_stt_backend
from voice_agent_pipeline.tts.cartesia import CartesiaClient
from voice_agent_pipeline.tts.client import TTSClient
from voice_agent_pipeline.turn import build_talker
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
                started_ns = time.time_ns()
                response_text = await self._router.talker.complete(decision.text)
                # ``talker.responded`` carries latency + the
                # clarification flag so operators can see clarification
                # turns vs normal turns in the same INFO feed. The
                # response TEXT is intentionally NOT logged here —
                # privacy posture (Story 1.3 redaction); the temporary
                # ``_TalkerResponseLogger`` below logs the text at DEBUG
                # only, which Story 2.5 will replace with Cartesia
                # synthesis (and the text never lands in INFO+ logs).
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


class CartesiaSynthesisProcessor(FrameProcessor):
    """Streams TTS audio from the configured TTSClient downstream (Story 2.5).

    Consumes :class:`TalkerResponseFrame` (from
    :class:`TurnDispatchProcessor`), opens a streaming synthesis call
    via :meth:`TTSClient.synthesize`, and emits each PCM chunk as a
    Pipecat :class:`OutputAudioRawFrame` so ``transport.output()``
    plays it through the speaker.

    The OutputAudioRawFrame distinction (vs the bare AudioRawFrame
    mixin) was discovered in Story 2.1 — Pipecat's runner / observers
    / output transport all expect the DataFrame subclass with
    framework-managed attrs (id, transport_destination, etc.).
    Inline comment in :mod:`audio.play_test_tone` carries the gory
    details.

    Privacy: the response text never lands in INFO+ logs. The
    Talker's reply is observable via the upstream
    ``talker.response_text`` DEBUG event; this processor only logs
    audio metadata (chunk counts, byte totals — nothing that reveals
    the text).
    """

    def __init__(self, client: TTSClient) -> None:
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._client = client

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """On TalkerResponseFrame: synthesize, push OutputAudioRawFrames."""
        await super().process_frame(frame, direction)

        if isinstance(frame, TalkerResponseFrame) and frame.text:
            # Empty text shouldn't happen in practice (the Talker
            # always returns *something*) but guard explicitly so a
            # stray empty frame doesn't burn a synthesis API call.
            chunk_count = 0
            byte_total = 0
            async for chunk in self._client.synthesize(frame.text):
                chunk_count += 1
                byte_total += len(chunk)
                await self.push_frame(
                    OutputAudioRawFrame(
                        audio=chunk,
                        sample_rate=16000,
                        num_channels=1,
                    ),
                    direction,
                )
            # Operator-side observability — chunk count + bytes per
            # turn. Lets ops watch synthesis output volume drift over
            # time without DEBUG. Privacy-safe: no transcript text.
            log.info(
                "tts.synthesis_complete",
                chunk_count=chunk_count,
                byte_total=byte_total,
            )

        # Pass the original TalkerResponseFrame through so future
        # stages (e.g., Epic 3's expression event publisher) can
        # observe turn boundaries. The audio chunks are pushed
        # separately above; downstream stages distinguish via
        # isinstance().
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
    Story 4.4's lifecycle FSM will later subscribe to the same frame and
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


async def run_pipeline(config: SetupConfig) -> None:
    """Build and run the full listen pipeline (mic -> wake -> VAD -> STT) until cancelled.

    Steps:

    1. Resolve audio devices via the regex patterns in ``config.audio``.
    2. Build the input transport (PyAudio-backed mic capture).
    3. Build :class:`WakewordProcessor` from ``config.wakeword`` + the
       Picovoice access key.
    4. Build :class:`VadProcessor` from ``config.vad``.
    5. Build the STT backend via :func:`build_stt_backend`; ``await
       load()`` here so the model download / load lands at startup, not
       on the first turn.
    6. Assemble: ``input -> wakeword -> vad -> stt -> stt_logger ->
       wakeword_logger -> frame_counter``.
    7. Run forever until cancelled.
    """
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

    # Build + pre-load the STT backend. Loading takes seconds; doing it
    # here means the first turn doesn't pay for cold-start.
    stt_backend = build_stt_backend(config.stt)
    await stt_backend.load()

    # Story 2.4: build the Talker (provider-agnostic factory picks
    # OpenAI / Groq / Gemini based on [talker] provider) and the
    # TurnRouter that owns it. v1 always passes None for the
    # orchestrator — Story 4.3 wires that path.
    talker = build_talker(config)
    router = TurnRouter(config.stt, talker, orchestrator=None)

    # Story 2.5: build the Cartesia TTS client. No pre-load (it's a
    # remote API; construction just opens the SDK's connection pool).
    cartesia_client = CartesiaClient(config.tts, config.cartesia_api_key)

    pipeline = Pipeline(
        [
            transport.input(),
            wakeword,
            vad,
            SttProcessor(stt_backend),
            _SttResultLogger(config.stt.low_confidence_threshold),
            _WakewordEventLogger(),
            # Story 2.4: TurnDispatchProcessor consumes TranscriptFrame,
            # calls Talker, emits TalkerResponseFrame.
            TurnDispatchProcessor(router),
            # Story 2.5: CartesiaSynthesisProcessor consumes
            # TalkerResponseFrame, streams audio chunks downstream.
            CartesiaSynthesisProcessor(cartesia_client),
            _FrameCounter(),
            # Story 2.1 wired the speaker stage; Story 2.5 now feeds
            # it OutputAudioRawFrames from CartesiaSynthesisProcessor.
            # The simple-turn loop is end-to-end.
            transport.output(),
        ]
    )
    task = PipelineTask(pipeline)
    runner = PipelineRunner()

    log.info("pipeline.started")
    try:
        await runner.run(task)
    finally:
        # Logged in finally so we get a stop event even on cancellation /
        # exception paths — useful for post-mortem.
        log.info("pipeline.stopped")
