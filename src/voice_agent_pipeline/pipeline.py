"""Pipecat pipeline assembly + lifecycle orchestration.

Story 1.5 landed mic capture + a frame counter terminal. Story 1.6 inserts
:class:`WakewordProcessor` between input and counter, plus a tiny logger
processor that surfaces wake-word events in the JSON log stream.

Stage list as of Story 1.6::

    transport.input() -> WakewordProcessor -> _WakewordEventLogger -> _FrameCounter

Story 1.7 will insert a VAD processor and an STT processor between
``_WakewordEventLogger`` and ``_FrameCounter``, gated on the wake-word
event so they only run after a valid utterance start.

The lifecycle plumbing here is intentionally minimal — :func:`run_pipeline`
runs until cancelled. ``__main__.py`` owns the cancellation logic via
asyncio signal handlers.
"""

import structlog
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_agent_pipeline.audio.devices import resolve_audio_devices
from voice_agent_pipeline.audio.transport import build_input_transport
from voice_agent_pipeline.audio.wakeword import WakeWordDetectedFrame, WakewordProcessor
from voice_agent_pipeline.config.setup import SetupConfig

log = structlog.get_logger(__name__)


class _FrameCounter(FrameProcessor):
    """No-op terminal stage that counts incoming :class:`AudioRawFrame` objects.

    Logs a DEBUG event every ``log_every`` frames so an operator running
    ``LOG_LEVEL=DEBUG`` can confirm audio is flowing. Non-audio frames
    (system events from Pipecat, :class:`WakeWordDetectedFrame`, etc.)
    pass through unchanged.

    Lives as a private inner class for now; if Story 1.6 / 1.7 needs to
    share the counting semantics, promote to ``audio/frame_counter.py``.
    """

    def __init__(self, log_every: int = 1000) -> None:
        # pipecat's FrameProcessor.__init__ accepts **kwargs typed as Unknown,
        # which trips pyright's strict reportUnknownMemberType check. The call
        # itself is safe — we pass no extra kwargs.
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._count = 0
        self._log_every = log_every

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Pipecat hook — count audio frames and pass everything through."""
        # Always call super first per Pipecat's processor contract.
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame):
            self._count += 1
            # Modulo log_every keeps this O(1) and predictable; at 16 kHz
            # mono with ~20ms frames, a 1000-frame report fires every ~20s.
            if self._count % self._log_every == 0:
                log.debug("audio.frame_counter", count=self._count)
        await self.push_frame(frame, direction)


class _WakewordEventLogger(FrameProcessor):
    """Surface :class:`WakeWordDetectedFrame` arrivals as JSON log events.

    INFO-level so the operator's default ``voice-agent.log`` shows wakes.
    Story 4.4's lifecycle FSM will later subscribe to the same frame and
    drive state transitions; this logger is a separate concern (it doesn't
    mutate state, just observes).
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
    """Build and run the audio + wake-word pipeline until cancelled.

    Steps:

    1. Resolve audio devices via the regex patterns in ``config.audio``.
    2. Build the input transport (PyAudio-backed mic capture).
    3. Build :class:`WakewordProcessor` from ``config.wakeword`` + the
       Picovoice access key.
    4. Assemble: ``input -> wakeword -> wakeword_event_logger ->
       frame_counter``.
    5. Run forever until cancelled.

    Args:
        config: Validated :class:`SetupConfig`. ``config.audio`` and
            ``config.wakeword`` provide all knobs this layer needs.

    Raises:
        StartupValidationError: If audio device resolution or Porcupine
            initialization fails.
        Any exception from Pipecat propagates.
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

    # Four-stage pipeline. Order matters: wake-word must see audio first
    # (so it can fire ASAP), then the event logger surfaces the wake event,
    # then the counter ticks regardless. Subsequent stories will insert
    # VAD / STT between event logger and counter.
    pipeline = Pipeline(
        [
            transport.input(),
            wakeword,
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
