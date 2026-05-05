"""Picovoice Porcupine wake-word detection wrapped as a Pipecat FrameProcessor.

This module flips the pipeline from "always listening" to **wake-word-gated**:
audio frames flow through unchanged so downstream stages (VAD, STT in Story
1.7+) can consume them, but those stages MUST gate on the
:class:`WakeWordDetectedFrame` signal before activating per FR1.

Privacy commitments (FR1, FR42):

- Pre-wake audio stays in-memory only. Porcupine processes a fixed-size
  buffer; we discard each chunk after Porcupine looks at it.
- No audio bytes are ever logged. The structlog redaction processor catches
  accidental leaks; this module simply doesn't pass audio bytes to log calls.

Latency commitments (NFR1, NFR3):

- ``pvporcupine.process`` is sync-CPU work; we wrap it in
  :func:`asyncio.to_thread` so it doesn't block the event loop. The audio
  hot path stays async-friendly.

The :class:`WakeWordDetectedFrame` lives here for now. If Story 1.7 needs to
import the type from a neutral location, promote to ``audio/frames.py``.
"""

import array
import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

import pvporcupine  # pyright: ignore[reportMissingTypeStubs]
import structlog
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)
from pydantic import SecretStr

from voice_agent_pipeline.errors import StartupValidationError

log = structlog.get_logger(__name__)

# Porcupine's API expects 16 kHz. We pin the same rate in audio/transport.py;
# this constant is the cross-check the processor uses at startup.
_EXPECTED_SAMPLE_RATE = 16000


# NOT frozen: pipecat's `Frame` base class is a non-frozen dataclass, and
# Python's dataclass machinery refuses to create a frozen subclass of a
# non-frozen parent. Treat instances as effectively immutable by convention
# (no code mutates them after construction); the price is one missing
# guarantee from the type system.
@dataclass
class WakeWordDetectedFrame(Frame):
    """Pipecat frame emitted on a positive wake-word detection.

    Attributes:
        keyword_index: Porcupine's returned keyword index (``0`` for the
            sole keyword we ship today). Future multi-keyword support
            (e.g. ``"Hey OLAF"`` + ``"OLAF wake up"``) will use higher
            indices without a Frame schema change.
        keyword: Human-readable name; today always ``"hey_olaf"``. Resolved
            via a name table once we ship multiple keywords.
        timestamp_ns: Monotonic nanosecond timestamp at detection. Story
            4.4's lifecycle FSM reads this to decide whether the wake fires
            old (e.g. >5s ago) and should be ignored.
    """

    keyword_index: int = 0
    keyword: str = "hey_olaf"
    timestamp_ns: int = field(default_factory=time.time_ns)


class WakewordProcessor(FrameProcessor):
    """Wake-word gate. Emits :class:`WakeWordDetectedFrame` on positive detection.

    Audio frames pass through unchanged so downstream stages (VAD, STT) can
    consume them — but those stages MUST gate on
    :class:`WakeWordDetectedFrame` before activating, per FR1.

    Lifecycle (pipecat 1.1 hooks):

    1. ``__init__`` — store args. **Does not** open Porcupine yet.
    2. ``setup`` — Pipecat hook called when the pipeline initializes. We
       construct the Porcupine instance here so its lifecycle is bound to
       the pipeline (the startup validation in ``__main__`` constructs a
       throwaway instance just to verify the access key + .ppn file).
    3. ``process_frame`` — buffer audio bytes, slice into frame-sized
       chunks, run Porcupine in a thread, emit a :class:`WakeWordDetectedFrame`
       on positive result.
    4. ``cleanup`` — release Porcupine resources when the pipeline tears down.

    Note: the original Story 1.6 spec referenced ``start_processor``/
    ``stop_processor``; pipecat 1.1.0's actual hooks are ``setup``/``cleanup``.
    The hook names changed between pipecat releases — verify against the
    installed version if pipecat is bumped.
    """

    def __init__(
        self,
        keyword_paths: list[Path],
        access_key: SecretStr,
        sensitivity: float,
    ) -> None:
        """Construct a wake-word processor (deferred Porcupine init).

        Args:
            keyword_paths: List of ``.ppn`` keyword files. Plural because
                Porcupine's API takes a list — forward-compat for shipping
                multiple wake words later without signature change.
            access_key: Picovoice access key (SecretStr so accidental
                ``repr`` doesn't leak).
            sensitivity: Detection threshold in ``[0.0, 1.0]``.
        """
        # pipecat's FrameProcessor.__init__ has **kwargs typed as Unknown,
        # which trips pyright strict. The call itself is safe.
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._keyword_paths = keyword_paths
        self._access_key = access_key
        self._sensitivity = sensitivity
        # Porcupine instance created in start_processor() and torn down in
        # stop_processor(). None outside that window.
        self._porcupine: object | None = None
        # Rolling byte buffer; we feed Porcupine fixed-size int16 frames
        # whose byte length equals frame_length * 2 (int16 = 2 bytes).
        self._buffer = bytearray()
        self._frame_byte_size: int = 0

    async def setup(self, setup: FrameProcessorSetup) -> None:
        """Pipecat lifecycle hook (1.1+) — open Porcupine in a thread.

        Args:
            setup: pipecat's setup payload (clock, task manager, observer).
                Forwarded to the base class which initializes processor
                infrastructure we don't manage directly.

        Raises:
            StartupValidationError: If Porcupine reports a sample rate
                other than 16 kHz (would mean the .ppn was trained for a
                different platform / format).
        """
        # Always call super().setup() first per pipecat's contract — it
        # wires the clock, task manager, and observer. Skipping it leaves
        # the processor in a half-initialized state.
        await super().setup(setup)  # pyright: ignore[reportUnknownMemberType]

        # Off-thread because pvporcupine.create does file I/O + native init.
        self._porcupine = await asyncio.to_thread(
            pvporcupine.create,
            access_key=self._access_key.get_secret_value(),
            keyword_paths=[str(p) for p in self._keyword_paths],
            sensitivities=[self._sensitivity],
        )
        # Porcupine's instance attributes; both should be 16000 / 512 for
        # the standard Linux x86_64 build. Defensive cross-check anyway.
        sample_rate = getattr(self._porcupine, "sample_rate", _EXPECTED_SAMPLE_RATE)
        if sample_rate != _EXPECTED_SAMPLE_RATE:
            raise StartupValidationError(
                stage="wakeword",
                reason=f"Porcupine expects {_EXPECTED_SAMPLE_RATE}Hz, got {sample_rate}",
            )
        frame_length = int(getattr(self._porcupine, "frame_length", 512))
        # int16 = 2 bytes per sample.
        self._frame_byte_size = frame_length * 2
        log.info(
            "wakeword.processor.started",
            sample_rate=sample_rate,
            frame_length=frame_length,
            sensitivity=self._sensitivity,
        )

    async def cleanup(self) -> None:
        """Pipecat lifecycle hook (1.1+) — release Porcupine."""
        # Release Porcupine first so the SDK's native handle goes away
        # before the base class tears down its task manager.
        if self._porcupine is not None:
            delete = getattr(self._porcupine, "delete", None)
            if callable(delete):
                # Off-thread because Porcupine's delete() is sync native code.
                await asyncio.to_thread(delete)
            self._porcupine = None
        log.info("wakeword.processor.stopped")
        await super().cleanup()  # pyright: ignore[reportUnknownMemberType]

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Pipecat hook — buffer audio, run Porcupine in chunks, emit on detect.

        Audio frames pass through unchanged so downstream stages can
        consume them. Wake-word detection results emit as a separate frame
        type (:class:`WakeWordDetectedFrame`) so subscribers don't have to
        peek inside audio frames.
        """
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame) and self._porcupine is not None:
            # Append new audio to the rolling buffer; consume in
            # frame_byte_size chunks. The buffer never grows unbounded —
            # at 16kHz mono with 512-sample Porcupine frames, one chunk =
            # 1024 bytes = ~32ms of audio.
            self._buffer.extend(frame.audio)
            while len(self._buffer) >= self._frame_byte_size:
                # Slice off one frame's worth and remove from the buffer
                # in a single pass. Using a bytes() copy means the
                # subsequent del doesn't disturb the slice.
                chunk = bytes(self._buffer[: self._frame_byte_size])
                del self._buffer[: self._frame_byte_size]
                # array("h", ...) interprets bytes as signed 16-bit ints.
                # Lighter-weight than numpy and avoids the dep entirely.
                samples = array.array("h", chunk)
                # process() is sync CPU work — keep it off the event loop.
                process = getattr(self._porcupine, "process", None)
                if not callable(process):
                    # Should never happen in practice (Porcupine instance
                    # always has .process), but defensive.
                    break
                result = await asyncio.to_thread(process, samples)
                if isinstance(result, int) and result >= 0:
                    await self.push_frame(
                        WakeWordDetectedFrame(keyword_index=result),
                        direction,
                    )

        # Always pass the original frame downstream — wake-word gating is
        # the *consumer's* contract (Story 1.7's VAD), not enforced here.
        await self.push_frame(frame, direction)
