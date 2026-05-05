"""Unit tests for :class:`WhisperBackend`.

faster-whisper's :class:`WhisperModel` is mocked at the import boundary;
we never actually load Whisper weights or run inference. The fake's
``transcribe`` returns scripted segments so we can assert on the text
concat + confidence formula.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from math import exp
from typing import Any
from unittest.mock import MagicMock

import pytest

from voice_agent_pipeline.stt import whisper_cpu as whisper_mod
from voice_agent_pipeline.stt.backend import TranscriptionResult
from voice_agent_pipeline.stt.whisper_cpu import WhisperBackend


@dataclass
class _FakeSegment:
    """Stand-in for faster-whisper's Segment (we only use .text + .avg_logprob)."""

    text: str
    avg_logprob: float


class _FakeWhisperModel:
    """Replaces ``WhisperModel`` — captures init args and scripts transcribe()."""

    def __init__(self, model_size: str, *, device: str, compute_type: str) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self._segments: list[_FakeSegment] = []
        self.transcribe_calls: list[dict[str, Any]] = []

    def feed_segments(self, segments: list[_FakeSegment]) -> None:
        self._segments = list(segments)

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[list[_FakeSegment], object]:
        self.transcribe_calls.append({"audio": audio, **kwargs})
        return list(self._segments), MagicMock()


@pytest.fixture
def fake_model() -> _FakeWhisperModel:
    """Pre-build a single fake instance per test; reused across constructions.

    Tests call ``WhisperBackend.load()`` which goes through the patched
    ``WhisperModel`` class; the patched class returns this fixture's
    instance. Tests then script behavior via ``fake_model.feed_segments(...)``
    directly — no need to reach into ``backend._model``.
    """
    return _FakeWhisperModel(model_size="placeholder", device="cpu", compute_type="int8")


@pytest.fixture
def patch_whisper_model(
    monkeypatch: pytest.MonkeyPatch, fake_model: _FakeWhisperModel
) -> _FakeWhisperModel:
    """Replace ``whisper_cpu.WhisperModel`` with a class that returns ``fake_model``.

    The constructor on the patched class re-assigns the fake's recorded
    init args so each test sees the size/device/compute_type it asked for.
    """

    class _PatchedWhisperModel:
        def __new__(cls, model_size: str, *, device: str, compute_type: str) -> _FakeWhisperModel:  # type: ignore[misc]
            fake_model.model_size = model_size
            fake_model.device = device
            fake_model.compute_type = compute_type
            return fake_model

    monkeypatch.setattr(whisper_mod, "WhisperModel", _PatchedWhisperModel)
    return fake_model


async def _make_backend(model_size: str = "small") -> WhisperBackend:
    """Build a backend and call load() — used by every transcribe test."""
    backend = WhisperBackend(model_size=model_size, compute_type="int8", device="cpu")
    await backend.load()
    return backend


@pytest.mark.asyncio
async def test_load_constructs_model_with_args(patch_whisper_model: _FakeWhisperModel) -> None:
    """``load()`` instantiates WhisperModel with the configured size/device/compute_type."""
    backend = WhisperBackend(model_size="base", compute_type="float16", device="cuda")
    await backend.load()
    assert patch_whisper_model.model_size == "base"
    assert patch_whisper_model.device == "cuda"
    assert patch_whisper_model.compute_type == "float16"


@pytest.mark.asyncio
async def test_transcribe_returns_text_and_confidence(
    patch_whisper_model: _FakeWhisperModel,
) -> None:
    """A single segment → text == segment.text, confidence == exp(avg_logprob)."""
    backend = await _make_backend()
    patch_whisper_model.feed_segments([_FakeSegment(text="hello", avg_logprob=-0.2)])
    result = await backend.transcribe(audio=b"\x00\x00" * 1000)
    assert isinstance(result, TranscriptionResult)
    assert result.text == "hello"
    assert result.confidence == pytest.approx(exp(-0.2))


@pytest.mark.asyncio
async def test_multi_segment_concatenation_and_avg_confidence(
    patch_whisper_model: _FakeWhisperModel,
) -> None:
    """Two segments → text is concat (stripped); confidence is exp(mean(avg_logprob))."""
    backend = await _make_backend()
    patch_whisper_model.feed_segments(
        [
            _FakeSegment(text="hello ", avg_logprob=-0.2),
            _FakeSegment(text="world", avg_logprob=-0.4),
        ]
    )
    result = await backend.transcribe(audio=b"\x00\x00" * 1000)
    assert result.text == "hello world"
    expected_conf = exp((-0.2 + -0.4) / 2)
    assert result.confidence == pytest.approx(expected_conf)


@pytest.mark.asyncio
async def test_empty_segments_yields_zero_confidence(
    patch_whisper_model: _FakeWhisperModel,
) -> None:
    """No segments returned → empty text + confidence 0.0 (triggers low-conf log)."""
    backend = await _make_backend()
    patch_whisper_model.feed_segments([])
    result = await backend.transcribe(audio=b"\x00\x00" * 1000)
    assert result.text == ""
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_transcribe_runs_in_thread(
    monkeypatch: pytest.MonkeyPatch,
    patch_whisper_model: _FakeWhisperModel,
) -> None:
    """``WhisperModel.transcribe`` must be called via ``asyncio.to_thread``."""
    real_to_thread = asyncio.to_thread
    calls: list[Callable[..., Any]] = []

    async def _recording_to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _recording_to_thread)
    backend = await _make_backend()
    patch_whisper_model.feed_segments([_FakeSegment(text="hi", avg_logprob=-0.1)])
    await backend.transcribe(audio=b"\x00\x00" * 1000)

    # Bound-method identity isn't reliable; compare by name.
    names = [getattr(c, "__name__", None) for c in calls]
    assert "transcribe" in names, f"transcribe must be off-thread; got {names}"


@pytest.mark.asyncio
async def test_transcribe_raises_if_load_not_called() -> None:
    """Calling transcribe without load() first raises a clear RuntimeError."""
    backend = WhisperBackend(model_size="small", compute_type="int8", device="cpu")
    with pytest.raises(RuntimeError) as exc_info:
        await backend.transcribe(audio=b"\x00\x00")
    assert "load()" in str(exc_info.value)
