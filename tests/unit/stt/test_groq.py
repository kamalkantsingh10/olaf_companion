"""Unit tests for :class:`voice_agent_pipeline.stt.groq.GroqAsrBackend`.

Mocking strategy: Protocol-boundary only (CLAUDE.md rule #7). We mock the
openai SDK's audio-transcriptions surface — the *external* seam the
backend talks to. No internal mocking of :func:`_encode_wav` or the
backend's own methods.

Confidence calculations are tested against known logprob values so a
formula change (geometric vs arithmetic mean, etc.) trips a test rather
than landing silently in production where it would shift the
``low_confidence_threshold`` calibration.
"""

import io
import wave
from math import exp
from typing import Any

import openai
import pytest
from pydantic import SecretStr

from voice_agent_pipeline.errors import GroqAsrError
from voice_agent_pipeline.stt.groq import GroqAsrBackend, _encode_wav

# --- _encode_wav helper ---


def test_encode_wav_produces_valid_wav_container() -> None:
    """The helper wraps raw PCM in a 16 kHz mono S16LE WAV the stdlib can read back."""
    # 1 second of silence at 16 kHz mono int16 = 32_000 bytes
    pcm = b"\x00\x00" * 16_000

    wav_bytes = _encode_wav(pcm)

    # Round-trip through wave.open to confirm header validity.
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16_000
        assert wav.getsampwidth() == 2
        assert wav.readframes(wav.getnframes()) == pcm


def test_encode_wav_empty_pcm_still_produces_header() -> None:
    """Zero-sample PCM still yields a parseable WAV (44-byte header, no frames)."""
    wav_bytes = _encode_wav(b"")
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getnframes() == 0
        assert wav.getframerate() == 16_000


# --- Test doubles for openai SDK ---


class _StubSegment:
    """Stand-in for openai.types.audio.transcription_verbose.TranscriptionSegment."""

    def __init__(self, avg_logprob: float) -> None:
        self.avg_logprob = avg_logprob


class _StubTranscription:
    """Stand-in for openai.types.audio.TranscriptionVerbose."""

    def __init__(self, text: str, segments: list[_StubSegment] | None) -> None:
        self.text = text
        self.segments = segments


class _StubTranscriptions:
    """Captures call args; returns a configurable response."""

    def __init__(self) -> None:
        self.response: _StubTranscription | None = None
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class _StubAudio:
    def __init__(self) -> None:
        self.transcriptions = _StubTranscriptions()


class _StubClient:
    """Stand-in for openai.AsyncOpenAI; carries .audio.transcriptions."""

    def __init__(self) -> None:
        self.audio = _StubAudio()


def _make_backend_with_stub(
    stub: _StubClient,
    model: str = "whisper-large-v3-turbo",
) -> GroqAsrBackend:
    """Build a backend and swap its openai client for the stub."""
    backend = GroqAsrBackend(
        api_key=SecretStr("test-key"),
        model=model,
    )
    # Direct field swap is the cleanest way to inject the stub without
    # patching openai globally — the backend's only openai surface is
    # ``self._client``, set in __init__.
    backend._client = stub  # type: ignore[assignment]
    return backend


# --- load() ---


async def test_load_is_a_noop() -> None:
    """The cloud backend's load() does nothing — credential probe lives elsewhere."""
    backend = GroqAsrBackend(api_key=SecretStr("test"), model="whisper-large-v3-turbo")
    # Should complete without raising and without touching the network.
    await backend.load()


# --- transcribe() success path ---


async def test_transcribe_returns_text_and_confidence_from_segments() -> None:
    """Successful transcribe extracts text + computes confidence from segment logprobs."""
    stub = _StubClient()
    # Two segments with avg_logprob = -0.5 each → geometric mean confidence = exp(-0.5).
    stub.audio.transcriptions.response = _StubTranscription(
        text="hello world",
        segments=[_StubSegment(-0.5), _StubSegment(-0.5)],
    )
    backend = _make_backend_with_stub(stub)

    pcm = b"\x00\x00" * 16_000  # 1 second of silence
    result = await backend.transcribe(pcm)

    assert result.text == "hello world"
    # exp(mean([-0.5, -0.5])) = exp(-0.5)
    assert result.confidence == pytest.approx(exp(-0.5))


async def test_transcribe_uses_geometric_mean_not_arithmetic() -> None:
    """Confidence is exp(mean(logprob)), not mean(exp(logprob)) — guards the formula."""
    stub = _StubClient()
    # Highly asymmetric logprobs: -3.0 and -0.1.
    # Geometric mean: exp((-3.0 + -0.1) / 2) = exp(-1.55) ≈ 0.2122
    # Arithmetic mean: (exp(-3.0) + exp(-0.1)) / 2 ≈ 0.4774 (very different)
    stub.audio.transcriptions.response = _StubTranscription(
        text="x",
        segments=[_StubSegment(-3.0), _StubSegment(-0.1)],
    )
    backend = _make_backend_with_stub(stub)
    result = await backend.transcribe(b"\x00\x00" * 1000)

    assert result.confidence == pytest.approx(exp(-1.55))


async def test_transcribe_strips_leading_whitespace_from_text() -> None:
    """The strip() call removes the tokenizer's leading space sometimes seen on Whisper output."""
    stub = _StubClient()
    stub.audio.transcriptions.response = _StubTranscription(
        text="  hello  ",
        segments=[_StubSegment(-0.1)],
    )
    backend = _make_backend_with_stub(stub)
    result = await backend.transcribe(b"\x00\x00" * 1000)

    assert result.text == "hello"


# --- transcribe() empty / silent audio ---


async def test_transcribe_empty_segments_returns_zero_confidence() -> None:
    """Pure silence (no segments) yields confidence=0.0 — matches WhisperBackend."""
    stub = _StubClient()
    stub.audio.transcriptions.response = _StubTranscription(text="", segments=[])
    backend = _make_backend_with_stub(stub)

    result = await backend.transcribe(b"\x00\x00" * 16_000)

    assert result.text == ""
    assert result.confidence == 0.0


async def test_transcribe_none_segments_returns_zero_confidence() -> None:
    """Missing ``segments`` attribute is treated as no segments (defensive)."""
    stub = _StubClient()
    stub.audio.transcriptions.response = _StubTranscription(text="x", segments=None)
    backend = _make_backend_with_stub(stub)

    result = await backend.transcribe(b"\x00\x00" * 1000)
    assert result.text == "x"
    assert result.confidence == 0.0


# --- transcribe() error wrapping ---


async def test_transcribe_wraps_openai_api_error_as_groq_asr_error() -> None:
    """An openai.APIError surfaces as GroqAsrError (ExternalServiceError subclass)."""
    stub = _StubClient()
    # APIError signature: (message, *, request, body)
    stub.audio.transcriptions.error = openai.APIError("upstream broke", request=None, body=None)  # type: ignore[arg-type]
    backend = _make_backend_with_stub(stub, model="whisper-large-v3-turbo")

    with pytest.raises(GroqAsrError) as exc_info:
        await backend.transcribe(b"\x00\x00" * 1000)

    # Context carries provider + model so log/diagnostic queries can filter.
    assert exc_info.value.context["provider"] == "groq"
    assert exc_info.value.context["model"] == "whisper-large-v3-turbo"
    assert "upstream broke" in exc_info.value.context["reason"]


# --- transcribe() API call shape ---


async def test_transcribe_calls_groq_with_correct_kwargs() -> None:
    """The SDK call uses verbose_json (we need segments) and threads the model id through."""
    stub = _StubClient()
    stub.audio.transcriptions.response = _StubTranscription(text="x", segments=[])
    backend = _make_backend_with_stub(stub, model="whisper-large-v3-turbo")

    await backend.transcribe(b"\x00\x00" * 16_000)

    assert len(stub.audio.transcriptions.calls) == 1
    call = stub.audio.transcriptions.calls[0]
    assert call["model"] == "whisper-large-v3-turbo"
    # response_format=verbose_json is what gives us segment-level avg_logprob.
    assert call["response_format"] == "verbose_json"
    # file tuple: (filename, bytes). The bytes are the WAV-encoded PCM.
    filename, data = call["file"]
    assert filename == "utterance.wav"
    # Validate the file is a parseable WAV (sanity — the SDK call carries
    # what _encode_wav produced).
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getframerate() == 16_000
