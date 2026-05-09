"""Unit tests for :mod:`voice_agent_pipeline.audio.wakeword`.

``pvporcupine`` is mocked at the import boundary — we replace the live
SDK with a deterministic fake whose ``create()`` returns an instance with
a configurable ``process()`` return value. This keeps the tests hermetic
(no Picovoice access key required, no audio hardware) while exercising
the same code path production uses.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.processors.frame_processor import FrameDirection
from pydantic import SecretStr

from voice_agent_pipeline.audio import wakeword as wakeword_mod
from voice_agent_pipeline.audio.mic_mode import _ModeStampedAudioFrame
from voice_agent_pipeline.audio.wakeword import WakeWordDetectedFrame, WakewordProcessor


class _StubSetup:
    """Minimal stand-in for :class:`pipecat.processors.frame_processor.FrameProcessorSetup`.

    pipecat's real setup() calls into the clock / task manager / observer
    on the payload. For unit tests we hand it MagicMock instances so the
    super().setup() call path doesn't crash; we only care about whether our
    Porcupine init runs.
    """

    def __init__(self) -> None:
        self.clock = MagicMock()
        # task_manager.cancel_task is awaited inside pipecat's cleanup, so
        # the manager's methods must be AsyncMocks. (A plain MagicMock here
        # returns a MagicMock from cancel_task, which isn't awaitable.)
        self.task_manager = AsyncMock()
        # observer must be None (not a MagicMock) — pipecat's process_frame
        # awaits observer.on_process_frame(...) when observer is truthy,
        # and a MagicMock isn't awaitable.
        self.observer = None


class _FakePorcupine:
    """Drop-in replacement for ``pvporcupine.Porcupine``.

    The processor calls ``sample_rate`` (attr), ``frame_length`` (attr),
    ``process(samples)`` (method), and ``delete()`` (method). We mock all
    four and let tests configure ``process_return_value``.
    """

    def __init__(self, process_return_value: int = -1) -> None:
        self.sample_rate = 16000
        # Match Porcupine's standard frame size on Linux x86_64 to keep
        # buffer math realistic. Tests that want fewer samples per frame
        # can adjust by setting frame_length directly after construction.
        self.frame_length = 512
        self._process_return_value = process_return_value
        self.delete_called = False
        self.process_calls: list[Any] = []

    def process(self, samples: Any) -> int:
        self.process_calls.append(samples)
        return self._process_return_value

    def delete(self) -> None:
        self.delete_called = True


class _FakePvporcupine:
    """Stand-in for the ``pvporcupine`` module exposing only what we use."""

    def __init__(self, instance: _FakePorcupine) -> None:
        self._instance = instance
        self.create_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakePorcupine:
        self.create_calls.append(kwargs)
        return self._instance


@pytest.fixture
def fake_porcupine() -> _FakePorcupine:
    """A fresh Porcupine fake per test (default: negative detection)."""
    return _FakePorcupine(process_return_value=-1)


@pytest.fixture
def patched_pvporcupine(
    monkeypatch: pytest.MonkeyPatch,
    fake_porcupine: _FakePorcupine,
) -> _FakePvporcupine:
    """Replace ``wakeword_mod.pvporcupine`` with a deterministic fake."""
    fake_module = _FakePvporcupine(fake_porcupine)
    monkeypatch.setattr(wakeword_mod, "pvporcupine", fake_module)
    return fake_module


def _build_processor(
    sensitivity: float = 0.5,
) -> WakewordProcessor:
    """Construct a WakewordProcessor with stub paths + key for tests."""
    return WakewordProcessor(
        keyword_paths=[Path("models/wakeword/hey_olaf.ppn")],
        access_key=SecretStr("stub-access-key"),
        sensitivity=sensitivity,
    )


async def _send_audio_chunk(
    processor: WakewordProcessor,
    byte_count: int,
) -> list[Frame]:
    """Drive ``processor.process_frame`` with one AudioRawFrame and capture pushes.

    Pipecat's :class:`FrameProcessor` calls ``self.push_frame`` on its
    next-stage link; we replace ``push_frame`` with a list-collector so
    the test can introspect what would have been pushed downstream.
    """
    pushed: list[Frame] = []

    async def _collect(frame: Frame, direction: FrameDirection) -> None:
        pushed.append(frame)

    # type: ignore[method-assign] — monkey-patching for test introspection.
    processor.push_frame = _collect  # type: ignore[assignment]

    audio_bytes = bytes(byte_count)  # all-zero PCM is fine for buffer math.
    # Story 4.6: WakewordProcessor now gates on _ModeStampedAudioFrame
    # with mic_mode == "wake_word_only". Production stamping is done
    # by MicModeRouter; tests stamp directly here to skip that layer.
    audio_frame = _ModeStampedAudioFrame(
        audio=audio_bytes,
        sample_rate=16000,
        num_channels=1,
        mic_mode="wake_word_only",
    )
    await processor.process_frame(audio_frame, FrameDirection.DOWNSTREAM)
    return pushed


@pytest.mark.asyncio
async def test_positive_detection_emits_frame(
    patched_pvporcupine: _FakePvporcupine,
    fake_porcupine: _FakePorcupine,
) -> None:
    """A positive detection (process returns >= 0) emits WakeWordDetectedFrame."""
    fake_porcupine._process_return_value = 0  # positive detection on keyword 0
    processor = _build_processor()
    await processor.setup(_StubSetup())
    # 512 samples at int16 = 1024 bytes — exactly one Porcupine frame.
    pushed = await _send_audio_chunk(processor, byte_count=1024)
    detected = [f for f in pushed if isinstance(f, WakeWordDetectedFrame)]
    assert len(detected) == 1, f"expected 1 wake event, got {len(detected)}"
    assert detected[0].keyword == "hey_olaf"
    assert detected[0].keyword_index == 0


@pytest.mark.asyncio
async def test_negative_detection_emits_no_frame(
    patched_pvporcupine: _FakePvporcupine,
) -> None:
    """A negative detection (process returns -1) emits no WakeWordDetectedFrame."""
    processor = _build_processor()  # default fixture is negative
    await processor.setup(_StubSetup())
    pushed = await _send_audio_chunk(processor, byte_count=1024)
    detected = [f for f in pushed if isinstance(f, WakeWordDetectedFrame)]
    assert detected == []


@pytest.mark.asyncio
async def test_audio_frame_passes_through_on_negative(
    patched_pvporcupine: _FakePvporcupine,
) -> None:
    """The original AudioRawFrame must pass downstream regardless of detection."""
    processor = _build_processor()
    await processor.setup(_StubSetup())
    pushed = await _send_audio_chunk(processor, byte_count=1024)
    assert any(isinstance(f, AudioRawFrame) for f in pushed)


@pytest.mark.asyncio
async def test_audio_frame_passes_through_on_positive(
    patched_pvporcupine: _FakePvporcupine,
    fake_porcupine: _FakePorcupine,
) -> None:
    """Even on a wake, the audio frame still flows so downstream VAD can see it."""
    fake_porcupine._process_return_value = 0
    processor = _build_processor()
    await processor.setup(_StubSetup())
    pushed = await _send_audio_chunk(processor, byte_count=1024)
    audio = [f for f in pushed if isinstance(f, AudioRawFrame)]
    detected = [f for f in pushed if isinstance(f, WakeWordDetectedFrame)]
    assert audio, "audio frame must pass through"
    assert detected, "wake event must also fire"


@pytest.mark.asyncio
async def test_buffer_accumulates_until_frame_size(
    patched_pvporcupine: _FakePvporcupine,
    fake_porcupine: _FakePorcupine,
) -> None:
    """Audio shorter than one Porcupine frame is buffered, not processed yet."""
    fake_porcupine._process_return_value = 0
    processor = _build_processor()
    await processor.setup(_StubSetup())

    # Send 512 bytes (256 samples) — half a Porcupine frame. Should buffer
    # without triggering process().
    pushed_first = await _send_audio_chunk(processor, byte_count=512)
    assert fake_porcupine.process_calls == [], "should not have processed yet"
    assert not any(isinstance(f, WakeWordDetectedFrame) for f in pushed_first)

    # Second chunk completes the frame; process() should run once.
    pushed_second = await _send_audio_chunk(processor, byte_count=512)
    assert len(fake_porcupine.process_calls) == 1
    assert any(isinstance(f, WakeWordDetectedFrame) for f in pushed_second)


@pytest.mark.asyncio
async def test_processor_uses_to_thread(
    monkeypatch: pytest.MonkeyPatch,
    patched_pvporcupine: _FakePvporcupine,
    fake_porcupine: _FakePorcupine,
) -> None:
    """``pvporcupine.process`` must run via ``asyncio.to_thread`` (no event-loop block).

    Patches ``asyncio.to_thread`` with a wrapper that records calls but
    proxies through to the real implementation, so the test asserts on
    intent (the call happened) without breaking the call chain.
    """
    fake_porcupine._process_return_value = 0
    real_to_thread = asyncio.to_thread
    calls: list[Callable[..., Any]] = []

    async def _recording_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _recording_to_thread)

    processor = _build_processor()
    await processor.setup(_StubSetup())
    await _send_audio_chunk(processor, byte_count=1024)

    # process() should have been routed through to_thread at least once.
    # Bound-method identity (`is`) doesn't work — each attribute access on
    # an instance creates a new bound method object. Use name comparison
    # which is robust to that.
    assert any(getattr(c, "__name__", None) == "process" for c in calls), (
        "pvporcupine.process must run inside asyncio.to_thread, not on the event loop"
    )


@pytest.mark.asyncio
async def test_stop_processor_releases_porcupine(
    patched_pvporcupine: _FakePvporcupine,
    fake_porcupine: _FakePorcupine,
) -> None:
    """``stop_processor`` calls ``Porcupine.delete()`` and clears the reference."""
    processor = _build_processor()
    await processor.setup(_StubSetup())
    await processor.cleanup()
    assert fake_porcupine.delete_called


@pytest.mark.asyncio
async def test_unexpected_sample_rate_raises(
    patched_pvporcupine: _FakePvporcupine,
    fake_porcupine: _FakePorcupine,
) -> None:
    """A non-16kHz Porcupine instance triggers StartupValidationError."""
    fake_porcupine.sample_rate = 22050  # wrong rate
    processor = _build_processor()
    from voice_agent_pipeline.errors import StartupValidationError

    with pytest.raises(StartupValidationError) as exc_info:
        await processor.setup(_StubSetup())
    assert "16000" in str(exc_info.value)


@pytest.mark.asyncio
async def test_no_audio_bytes_in_logs(
    patched_pvporcupine: _FakePvporcupine,
    fake_porcupine: _FakePorcupine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanity: nothing this module logs ever contains an `audio_bytes` field."""
    import logging

    fake_porcupine._process_return_value = 0
    processor = _build_processor()
    with caplog.at_level(logging.DEBUG, logger="voice_agent_pipeline.audio.wakeword"):
        await processor.setup(_StubSetup())
        await _send_audio_chunk(processor, byte_count=1024)
    for record in caplog.records:
        # message field
        assert "audio_bytes" not in record.getMessage()
        # extras (structlog routes structured kwargs through extra fields)
        assert "audio_bytes" not in str(getattr(record, "args", ""))
