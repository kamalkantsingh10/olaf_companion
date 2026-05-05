"""Pipecat pipeline assembly + lifecycle orchestration.

Story 1.5 lands the **first running pipeline**: mic in -> frame counter.
The frame counter is a no-op processor whose only job is to log a DEBUG
event every 1000 audio frames; subsequent stories replace it with real
processors (Story 1.6: wake-word; Story 1.7: VAD + STT; Story 2.5: full
turn assembly).

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
from voice_agent_pipeline.config.setup import SetupConfig

log = structlog.get_logger(__name__)


class _FrameCounter(FrameProcessor):
    """No-op terminal stage that counts incoming :class:`AudioRawFrame` objects.

    Logs a DEBUG event every ``log_every`` frames so an operator running
    ``LOG_LEVEL=DEBUG`` can confirm audio is flowing. Non-audio frames
    (system events from Pipecat) pass through unchanged.

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


async def run_pipeline(config: SetupConfig) -> None:
    """Build and run the audio-capture pipeline until cancelled.

    Steps:

    1. Resolve audio devices via the regex patterns in ``config.audio``.
       Failure here raises :class:`StartupValidationError`.
    2. Build the input transport (PyAudio-backed mic capture).
    3. Assemble a two-stage Pipecat pipeline: ``input -> frame_counter``.
    4. Run forever until cancelled — :func:`asyncio.CancelledError` from
       the SIGTERM/SIGINT handler in ``__main__.py`` ends the loop.

    Args:
        config: Validated :class:`SetupConfig`. ``config.audio`` provides
            the device-name regexes.

    Raises:
        StartupValidationError: If audio device resolution fails.
        Any exception from Pipecat propagates.
    """
    indices = resolve_audio_devices(
        input_pattern=config.audio.input_device_name,
        output_pattern=config.audio.output_device_name,
    )
    transport = build_input_transport(config, indices)

    # Two-stage pipeline: transport.input() (the source) -> _FrameCounter()
    # (the sink). Subsequent stories insert wake-word, VAD, STT, etc.
    # between these two.
    pipeline = Pipeline([transport.input(), _FrameCounter()])
    task = PipelineTask(pipeline)
    runner = PipelineRunner()

    log.info("pipeline.started")
    try:
        await runner.run(task)
    finally:
        # Logged in finally so we get a stop event even on cancellation /
        # exception paths — useful for post-mortem.
        log.info("pipeline.stopped")
