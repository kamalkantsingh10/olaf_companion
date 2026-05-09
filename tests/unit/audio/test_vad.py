"""Unit tests for :mod:`voice_agent_pipeline.audio.vad`.

Pipecat's :class:`SileroVADAnalyzer` is mocked at the import boundary so
tests don't pull the ONNX model file or real inference. The fake's
``voice_confidence`` returns scripted values to drive the state machine
through speech / silence transitions.
"""

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.processors.frame_processor import FrameDirection

from voice_agent_pipeline.audio import vad as vad_mod
from voice_agent_pipeline.audio.mic_mode import _ModeStampedAudioFrame
from voice_agent_pipeline.audio.vad import UtteranceCapturedFrame, VadProcessor
from voice_agent_pipeline.audio.wakeword import WakeWordDetectedFrame
from voice_agent_pipeline.config.setup import VadConfig


class _StubSetup:
    """Minimal stand-in for FrameProcessorSetup (matches Story 1.6's stub)."""

    def __init__(self) -> None:
        self.clock = MagicMock()
        self.task_manager = AsyncMock()
        self.observer = None


class _FakeSileroAnalyzer:
    """Replaces SileroVADAnalyzer with a deterministic confidence sequence.

    Each ``voice_confidence`` call pops the next value off the configured
    queue. Tests drive the state machine by enqueueing values that
    represent "speech" (>= start_threshold) or "silence" (< end_threshold).
    """

    def __init__(self, *, sample_rate: int | None = None, params: object | None = None) -> None:
        self.set_sample_rate_calls: list[int] = []
        self._values: list[float] = []

    def set_sample_rate(self, sample_rate: int) -> None:
        self.set_sample_rate_calls.append(sample_rate)

    def feed_values(self, values: list[float]) -> None:
        self._values = list(values)

    def voice_confidence(self, buffer: bytes) -> float:
        if not self._values:
            # Default to silence if the test ran out of scripted values
            # — keeps the state machine deterministic.
            return 0.0
        return self._values.pop(0)


@pytest.fixture
def fake_silero(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeSileroAnalyzer]:
    """Replace SileroVADAnalyzer in the vad module with our fake; yield the fake."""
    fake = _FakeSileroAnalyzer()

    def _factory(**_kwargs: object) -> _FakeSileroAnalyzer:
        return fake

    monkeypatch.setattr(vad_mod, "SileroVADAnalyzer", _factory)
    yield fake


def _vad_config(
    silence_duration_ms: int = 100,
    min_speech_duration_ms: int = 50,
) -> VadConfig:
    """Build a tight VadConfig so tests don't have to push 700ms of frames."""
    return VadConfig(
        silence_duration_ms=silence_duration_ms,
        min_speech_duration_ms=min_speech_duration_ms,
        start_threshold=0.5,
        end_threshold=0.35,
    )


async def _setup_processor(fake_silero: _FakeSileroAnalyzer) -> VadProcessor:
    """Standard "construct + setup" sequence used by every test."""
    processor = VadProcessor(_vad_config())
    await processor.setup(_StubSetup())
    return processor


def _audio_frame(byte_count: int) -> _ModeStampedAudioFrame:
    """Build a stamped audio frame containing ``byte_count`` zero bytes.

    Story 4.6: VAD gates on ``_ModeStampedAudioFrame.mic_mode ==
    "vad_stt"``. Tests stamp directly to skip the MicModeRouter.
    """
    return _ModeStampedAudioFrame(
        audio=bytes(byte_count),
        sample_rate=16000,
        num_channels=1,
        mic_mode="vad_stt",
    )


def _wake_frame() -> WakeWordDetectedFrame:
    """Build a default WakeWordDetectedFrame for activating the VAD."""
    return WakeWordDetectedFrame()


async def _drive(processor: VadProcessor, frame: Frame) -> list[Frame]:
    """Push one frame through and capture every push_frame call.

    Pipecat's :meth:`push_frame` hands the frame to the next-stage link;
    we replace it with a list-collector so tests can introspect.
    """
    pushed: list[Frame] = []

    async def _collect(f: Frame, direction: FrameDirection) -> None:
        pushed.append(f)

    processor.push_frame = _collect  # type: ignore[assignment]
    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)
    return pushed


@pytest.mark.asyncio
async def test_vad_inactive_until_wake_word(fake_silero: _FakeSileroAnalyzer) -> None:
    """No utterance is emitted from audio that arrives before the first wake-word."""
    processor = await _setup_processor(fake_silero)
    fake_silero.feed_values([0.9] * 100)
    pushed = await _drive(processor, _audio_frame(byte_count=4096))
    assert all(not isinstance(f, UtteranceCapturedFrame) for f in pushed)


@pytest.mark.asyncio
async def test_vad_emits_on_silence_after_speech(fake_silero: _FakeSileroAnalyzer) -> None:
    """A wake-word + speech + sustained silence sequence emits an UtteranceCapturedFrame."""
    processor = await _setup_processor(fake_silero)

    # Activate the VAD.
    await _drive(processor, _wake_frame())

    # Enough speech to clear min_speech_duration_ms (50ms = 800 samples =
    # 1600 bytes) plus enough silence chunks (100ms / 32ms-per-chunk ~= 4
    # silent chunks). One audio frame at ~16k samples (32k bytes) easily
    # carries dozens of 512-sample Silero chunks.
    fake_silero.feed_values([0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    pushed = await _drive(processor, _audio_frame(byte_count=10240))  # 5120 samples

    captured = [f for f in pushed if isinstance(f, UtteranceCapturedFrame)]
    assert len(captured) == 1
    utterance = captured[0]
    assert isinstance(utterance.audio, bytes)
    assert len(utterance.audio) > 0
    assert utterance.sample_rate == 16000
    # end_ns must be after start_ns.
    assert utterance.end_ns >= utterance.start_ns


@pytest.mark.asyncio
async def test_vad_deactivates_after_emit(fake_silero: _FakeSileroAnalyzer) -> None:
    """After an utterance is emitted, additional audio frames don't trigger another."""
    processor = await _setup_processor(fake_silero)
    await _drive(processor, _wake_frame())

    fake_silero.feed_values([0.9, 0.9, 0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    first = await _drive(processor, _audio_frame(byte_count=10240))
    assert any(isinstance(f, UtteranceCapturedFrame) for f in first)

    # Subsequent audio (no new wake-word) must NOT emit a second utterance.
    fake_silero.feed_values([0.9] * 50 + [0.1] * 50)
    second = await _drive(processor, _audio_frame(byte_count=10240))
    assert all(not isinstance(f, UtteranceCapturedFrame) for f in second)


@pytest.mark.asyncio
async def test_min_speech_duration_filter(fake_silero: _FakeSileroAnalyzer) -> None:
    """Speech shorter than min_speech_duration_ms is dropped silently.

    We use a config with min_speech_duration_ms = 1000ms so even a short
    one-chunk burst doesn't qualify.
    """
    processor = VadProcessor(
        VadConfig(
            silence_duration_ms=100,
            min_speech_duration_ms=1000,  # very high — easy to undershoot
            start_threshold=0.5,
            end_threshold=0.35,
        )
    )
    await processor.setup(_StubSetup())

    await _drive(processor, _wake_frame())
    # One short speech chunk then silence.
    fake_silero.feed_values([0.9, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1])
    # Small audio frame — only ~1024 samples (64ms) of buffered speech.
    pushed = await _drive(processor, _audio_frame(byte_count=2048))
    assert all(not isinstance(f, UtteranceCapturedFrame) for f in pushed)


@pytest.mark.asyncio
async def test_audio_frames_pass_through(fake_silero: _FakeSileroAnalyzer) -> None:
    """The original AudioRawFrame must reach the next stage regardless of VAD state."""
    processor = await _setup_processor(fake_silero)
    pushed = await _drive(processor, _audio_frame(byte_count=4096))
    assert any(isinstance(f, AudioRawFrame) for f in pushed)


@pytest.mark.asyncio
async def test_wake_word_passes_through(fake_silero: _FakeSileroAnalyzer) -> None:
    """The original WakeWordDetectedFrame must also reach the next stage."""
    processor = await _setup_processor(fake_silero)
    pushed = await _drive(processor, _wake_frame())
    assert any(isinstance(f, WakeWordDetectedFrame) for f in pushed)
