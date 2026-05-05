"""Pipecat pipeline assembly + lifecycle orchestration.

Story 1.5 landed mic capture + a frame counter. Story 1.6 inserted
:class:`WakewordProcessor`. Story 1.7 closes the listening half-loop:
VAD bounds the captured utterance, STT transcribes it, a result logger
surfaces ``stt.transcript`` events.

Stage list as of Story 1.7::

    transport.input()
        -> WakewordProcessor          # gates the rest of the chain
        -> VadProcessor               # bounds the utterance
        -> SttProcessor               # transcribes the utterance
        -> _SttResultLogger           # surfaces transcript + confidence
        -> _WakewordEventLogger       # logs wake events for ops
        -> _FrameCounter              # debug-only ticker

Story 2.5 will replace ``_SttResultLogger`` with a turn router that
dispatches to the Talker / orchestrator.
"""

import time
from dataclasses import dataclass

import structlog
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_agent_pipeline.audio.devices import resolve_audio_devices
from voice_agent_pipeline.audio.transport import build_input_transport
from voice_agent_pipeline.audio.vad import UtteranceCapturedFrame, VadProcessor
from voice_agent_pipeline.audio.wakeword import WakeWordDetectedFrame, WakewordProcessor
from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.stt import STTBackend, build_stt_backend

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
            # Two log calls so the transcript text actually surfaces when
            # operators run with ``LOG_LEVEL=DEBUG``:
            #
            # 1) INFO — confidence + latency only. Redaction drops the
            #    ``transcript`` field at INFO (Story 1.3) anyway, but more
            #    importantly debug.log is filtered to DEBUG records ONLY,
            #    so an INFO call would never show transcripts there even
            #    if redaction passed it through.
            # 2) DEBUG — same event, with the transcript text. Lands in
            #    debug.log only (handler filter), and only when the
            #    operator opted in via LOG_LEVEL=DEBUG.
            log.info(
                "stt.transcript",
                confidence=frame.confidence,
                end_to_transcript_ms=frame.end_to_transcript_ms,
            )
            log.debug(
                "stt.transcript",
                confidence=frame.confidence,
                end_to_transcript_ms=frame.end_to_transcript_ms,
                transcript=frame.text,
            )
            if frame.confidence < self._threshold:
                # Story 2.4 will wire the actual clarification dialog.
                # For now, the WARN is the placeholder; downstream code
                # subscribes by listening for this event name.
                log.warning(
                    "stt.low_confidence",
                    confidence=frame.confidence,
                    end_to_transcript_ms=frame.end_to_transcript_ms,
                    clarification_pending=True,
                )

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
    transport = build_input_transport(config, indices)

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

    pipeline = Pipeline(
        [
            transport.input(),
            wakeword,
            vad,
            SttProcessor(stt_backend),
            _SttResultLogger(config.stt.low_confidence_threshold),
            _WakewordEventLogger(),
            _FrameCounter(),
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
