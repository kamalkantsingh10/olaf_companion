"""Silero VAD wrapped as a Pipecat FrameProcessor; activated by WakeWordDetectedFrame.

Pipeline shape (Story 1.7):
    transport.input() -> WakewordProcessor -> VadProcessor -> SttProcessor -> ...

Lifecycle (gated by wake-word per FR1):
    1. ``__init__`` — store config; SileroVADAnalyzer instantiated lazily in setup().
    2. Idle (active=False) — audio frames pass through, no buffering, no VAD.
    3. WakeWordDetectedFrame received — flip active=True, reset buffer + timers.
    4. Active — buffer audio, feed every 512-sample chunk to Silero, track silence
       run. When sustained silence exceeds ``silence_duration_ms`` and the
       captured speech length is above ``min_speech_duration_ms``, emit an
       :class:`UtteranceCapturedFrame` and flip active=False.
    5. Back to (2) until next wake-word.

The 16 kHz mono S16LE format pinned by ``audio/transport.py`` matches Silero's
expected input exactly — no resampling needed inside this processor.

Why a custom processor instead of pipecat's ``VADProcessor``: pipecat's wrapper
emits speaking-start/stop frames but does NOT include the captured audio
buffer. We need the audio bytes to feed faster-whisper, so we run Silero
ourselves and bundle the bytes into a custom :class:`UtteranceCapturedFrame`.
"""

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import structlog
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)

from voice_agent_pipeline.audio.mic_mode import _ModeStampedAudioFrame
from voice_agent_pipeline.audio.wakeword import WakeWordDetectedFrame
from voice_agent_pipeline.config.setup import VadConfig

log = structlog.get_logger(__name__)

# Silero's required chunk size at 16 kHz. We feed exactly this many samples
# (2 bytes each = 1024 bytes per call) to ``voice_confidence``.
_SILERO_FRAME_SAMPLES = 512
_SILERO_FRAME_BYTES = _SILERO_FRAME_SAMPLES * 2
# 16 kHz mono → 32 ms per Silero frame. Used to translate silence-frame
# count into milliseconds.
_SILERO_FRAME_MS = 32  # int(_SILERO_FRAME_SAMPLES * 1000 / 16000)
_SAMPLE_RATE = 16000


@dataclass
class UtteranceCapturedFrame(Frame):
    """Pipecat frame emitted when VAD finishes capturing an utterance.

    Attributes:
        audio: Raw 16 kHz mono S16LE PCM bytes covering the whole utterance.
            STT consumes this directly via :meth:`STTBackend.transcribe`.
        start_ns: Monotonic ns timestamp at the wake-word arrival (or first
            speech frame, whichever the processor uses for "utterance
            started"). Used by ``end_to_transcript_ms`` calculations.
        end_ns: Monotonic ns timestamp at the moment we emit the frame
            (i.e. end of speech). Story 1.7's NFR3 measurement uses this.
        sample_rate: 16000 in v1; carrying the field keeps future variants
            (8 kHz, 24 kHz) explicit instead of magic-numbered.
    """

    # Defaults provided so dataclass machinery is happy when subclassing
    # pipecat's non-frozen Frame; values are always populated by VadProcessor.
    audio: bytes = b""
    start_ns: int = 0
    end_ns: int = field(default_factory=time.time_ns)
    sample_rate: int = _SAMPLE_RATE


class VadProcessor(FrameProcessor):
    """Silero VAD as a Pipecat FrameProcessor; emits utterances on speech-end.

    Wakes only after a :class:`WakeWordDetectedFrame` arrives — honors FR1
    (no downstream dispatch before wake). Deactivates after each utterance
    is emitted; the next wake-word reactivates.
    """

    def __init__(self, vad_config: VadConfig) -> None:
        """Construct a VAD processor (deferred Silero load).

        Args:
            vad_config: Validated :class:`VadConfig`. All timing knobs come
                from here; this processor doesn't read env vars.
        """
        # pipecat FrameProcessor.__init__ has **kwargs typed Unknown.
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._cfg = vad_config

        # Silero analyzer instance, built in setup() when pipecat hands us
        # the runtime infrastructure. Optional[...] until then.
        self._silero: SileroVADAnalyzer | None = None

        # State machine fields:
        #   _active             — True when collecting an utterance.
        #   _utterance_buffer   — accumulated S16LE bytes covering the utterance.
        #   _vad_frame_buffer   — bytes pending a 512-sample Silero chunk.
        #   _utterance_start_ns — ns when wake-word landed.
        #   _silence_run_ms     — milliseconds of consecutive silence so far.
        #   _speech_seen        — at least one Silero chunk was over the start
        #                         threshold (filters wake-word echo before
        #                         the user actually starts speaking).
        self._active: bool = False
        self._utterance_buffer: bytearray = bytearray()
        self._vad_frame_buffer: bytearray = bytearray()
        self._utterance_start_ns: int = 0
        self._silence_run_ms: int = 0
        self._speech_seen: bool = False

    async def setup(self, setup: FrameProcessorSetup) -> None:
        """Pipecat lifecycle hook — load the Silero VAD model.

        Silero loads its bundled ONNX model file from the pipecat package
        data dir. Loading takes a few hundred ms but is one-shot at startup
        — well under our latency budget for the first turn.
        """
        await super().setup(setup)  # pyright: ignore[reportUnknownMemberType]

        # VADParams maps our threshold knobs into pipecat's VAD analyzer
        # config. We don't use pipecat's start_secs/stop_secs because we
        # implement our own state machine here (matched against our
        # silence_duration_ms / min_speech_duration_ms in VadConfig).
        params = VADParams(
            confidence=self._cfg.start_threshold,
        )
        self._silero = SileroVADAnalyzer(sample_rate=_SAMPLE_RATE, params=params)
        self._silero.set_sample_rate(_SAMPLE_RATE)
        log.info(
            "vad.processor.started",
            silence_duration_ms=self._cfg.silence_duration_ms,
            min_speech_duration_ms=self._cfg.min_speech_duration_ms,
            start_threshold=self._cfg.start_threshold,
            end_threshold=self._cfg.end_threshold,
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Pipecat hook — drive the VAD state machine; emit on end-of-speech."""
        await super().process_frame(frame, direction)

        if isinstance(frame, WakeWordDetectedFrame):
            self._activate(frame.timestamp_ns)
        elif (
            isinstance(frame, _ModeStampedAudioFrame)
            and frame.mic_mode == "vad_stt"
            and self._active
            and self._silero is not None
        ):
            # Story 4.6: gate on mic_mode stamp. VAD only processes
            # audio when the active mode is ``"vad_stt"`` — in
            # ``"wake_word_only"`` the frames flow through but VAD is
            # skipped, enforcing FR47's single-stream invariant.
            self._consume_audio(frame.audio, direction)
            # Check for end-of-utterance after each frame's worth of chunks.
            await self._maybe_emit_utterance(direction)

        # Always pass the original frame downstream — wake-word and audio
        # frames are still useful to other processors (e.g. Story 5.1's
        # barge-in detector).
        await self.push_frame(frame, direction)

    def reset_state(self) -> None:
        """Drop in-flight VAD buffers + flags (Story 4.6).

        Called by the mic-mode-change orchestrator on transitions in
        either direction. Clears any partially-buffered utterance,
        VAD chunk buffer, silence counter, and speech-seen flag.

        **Does NOT** touch ``_active`` — that's owned by the
        wake-word path (the next :class:`WakeWordDetectedFrame` sets
        it; the deferred-sleep path doesn't need it cleared because
        the FSM's mic-mode flip is the gate, not ``_active``).
        """
        self._utterance_buffer.clear()
        self._vad_frame_buffer.clear()
        self._silence_run_ms = 0
        self._speech_seen = False
        log.info("vad.state_reset")

    def _activate(self, wake_timestamp_ns: int) -> None:
        """Reset state and start collecting an utterance after a wake-word."""
        self._active = True
        self._utterance_buffer.clear()
        self._vad_frame_buffer.clear()
        self._utterance_start_ns = wake_timestamp_ns
        self._silence_run_ms = 0
        self._speech_seen = False
        log.debug("vad.utterance.started", wake_timestamp_ns=wake_timestamp_ns)

    def _consume_audio(self, audio_bytes: bytes, direction: FrameDirection) -> None:
        """Feed audio bytes to both the utterance buffer and the Silero chunker."""
        # Always append to the utterance buffer — even silence inside an
        # utterance is part of the captured audio (faster-whisper benefits
        # from a bit of leading/trailing silence for context).
        self._utterance_buffer.extend(audio_bytes)
        # Buffer for Silero's fixed-size chunks. We may consume multiple
        # 512-sample chunks per audio frame depending on its size.
        self._vad_frame_buffer.extend(audio_bytes)

        for confidence in self._drain_vad_chunks():
            if confidence >= self._cfg.start_threshold:
                # Clearly speech — reset silence counter and remember we've
                # seen real speech (gates the min_speech_duration filter).
                self._speech_seen = True
                self._silence_run_ms = 0
            else:
                # Anything below start_threshold counts as silence.
                # ``end_threshold`` was originally meant to be a hysteresis
                # band against flapping; in practice Silero returns values
                # in the 0.35-0.5 dead-zone often enough that hysteresis
                # made silence_run never accumulate. Treating "not speech"
                # as silence is more reliable, at the (theoretical) cost
                # of cutting an utterance one chunk early on a borderline
                # speech tail.
                self._silence_run_ms += _SILERO_FRAME_MS

    def _drain_vad_chunks(self) -> Iterable[float]:
        """Yield voice-confidence values for every complete 512-sample chunk."""
        # Use a guard rather than `while True` so a misconfigured analyzer
        # can't hang the loop. self._silero is checked before this is called.
        while len(self._vad_frame_buffer) >= _SILERO_FRAME_BYTES and self._silero is not None:
            chunk = bytes(self._vad_frame_buffer[:_SILERO_FRAME_BYTES])
            del self._vad_frame_buffer[:_SILERO_FRAME_BYTES]
            yield self._silero.voice_confidence(chunk)

    async def _maybe_emit_utterance(self, direction: FrameDirection) -> None:
        """Emit :class:`UtteranceCapturedFrame` if end-of-speech criteria are met."""
        if not self._speech_seen:
            # No speech detected yet — keep buffering. This handles the
            # "wake word fired but user hasn't started speaking" window.
            return
        if self._silence_run_ms < self._cfg.silence_duration_ms:
            return
        speech_ms = (
            len(self._utterance_buffer) // 2 * 1000 // _SAMPLE_RATE  # bytes -> samples -> ms
        )
        if speech_ms < self._cfg.min_speech_duration_ms:
            # Too short — drop silently. Probably a cough or false wake.
            log.debug(
                "vad.utterance.dropped_short",
                duration_ms=speech_ms,
                min_speech_duration_ms=self._cfg.min_speech_duration_ms,
            )
            self._active = False
            return

        end_ns = time.time_ns()
        utterance = UtteranceCapturedFrame(
            audio=bytes(self._utterance_buffer),
            start_ns=self._utterance_start_ns,
            end_ns=end_ns,
            sample_rate=_SAMPLE_RATE,
        )
        log.info(
            "vad.utterance.captured",
            duration_ms=speech_ms,
            silence_run_ms=self._silence_run_ms,
        )
        # Deactivate BEFORE pushing so a re-entrant push_frame can't
        # accidentally consume more audio while we're emitting.
        self._active = False
        self._utterance_buffer.clear()
        self._vad_frame_buffer.clear()
        await self.push_frame(utterance, direction)
