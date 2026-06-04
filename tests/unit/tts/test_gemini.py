"""Unit tests for the Gemini Live-API TTS client (Story 6.5).

Covers the pure Live-response helpers (``first_audio_bytes`` /
``is_turn_complete``) and ``GeminiClient`` end-to-end with the
``google.genai`` Live session mocked at the SDK boundary (CLAUDE.md rule 7
— never mock internal functions or pydantic models). No network or key.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors as genai_errors
from pydantic import SecretStr

from voice_agent_pipeline.config.setup import TtsConfig
from voice_agent_pipeline.errors import GeminiTtsError
from voice_agent_pipeline.tts.gemini import (
    GeminiClient,
    first_audio_bytes,
    is_turn_complete,
)

# ───────────────────────── Live-response shape fakes ─────────────────────────


def _resp(*, parts: list[object] | None = None, turn_complete: bool = False) -> object:
    """A fake ``session.receive()`` item mirroring the Live API response shape."""
    model_turn = SimpleNamespace(parts=parts) if parts is not None else None
    server_content = SimpleNamespace(model_turn=model_turn, turn_complete=turn_complete)
    return SimpleNamespace(server_content=server_content)


def _audio_part(data: bytes) -> object:
    return SimpleNamespace(inline_data=SimpleNamespace(data=data))


def _text_part() -> object:
    return SimpleNamespace(inline_data=None)


# ───────────────────────── helper tests ─────────────────────────


def test_first_audio_bytes_returns_first_payload() -> None:
    """The first non-empty inline_data.data is returned."""
    resp = _resp(parts=[_text_part(), _audio_part(b"\x01\x02"), _audio_part(b"\x03")])
    assert first_audio_bytes(resp) == b"\x01\x02"


def test_first_audio_bytes_none_without_server_content() -> None:
    assert first_audio_bytes(SimpleNamespace(server_content=None)) is None


def test_first_audio_bytes_none_without_model_turn() -> None:
    assert first_audio_bytes(_resp(parts=None)) is None


def test_first_audio_bytes_none_for_text_only_parts() -> None:
    assert first_audio_bytes(_resp(parts=[_text_part(), _text_part()])) is None


def test_first_audio_bytes_skips_empty_data() -> None:
    resp = _resp(parts=[_audio_part(b""), _audio_part(b"\x09")])
    assert first_audio_bytes(resp) == b"\x09"


def test_is_turn_complete_true_when_flagged() -> None:
    assert is_turn_complete(_resp(turn_complete=True)) is True


def test_is_turn_complete_false_mid_turn() -> None:
    assert is_turn_complete(_resp(parts=[_audio_part(b"\x01")], turn_complete=False)) is False


def test_is_turn_complete_false_without_server_content() -> None:
    assert is_turn_complete(SimpleNamespace(server_content=None)) is False


# ───────────────────────── GeminiClient fakes ─────────────────────────


class _FakeSession:
    """Stand-in for a ``client.aio.live.connect()`` session."""

    def __init__(self, responses: list[object], *, raise_on_send: Exception | None = None) -> None:
        self._responses = responses
        self._raise_on_send = raise_on_send
        self.sent: list[tuple[Any, bool]] = []

    async def send_client_content(self, *, turns: Any, turn_complete: bool) -> None:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append((turns, turn_complete))

    async def receive(self) -> Any:
        for r in self._responses:
            yield r


class _FakeConnectCM:
    """Async context manager returned by ``connect(...)``."""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_: object) -> bool:
        return False


def _client_with(session: _FakeSession) -> Any:
    """Build a fake genai client whose live.connect() yields ``session``."""

    def _connect(*, model: str, config: object) -> _FakeConnectCM:
        return _FakeConnectCM(session)

    return SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=_connect)))


def _gemini_client(session: _FakeSession) -> GeminiClient:
    cfg = TtsConfig(provider="gemini", voice_id="placeholder")
    client = GeminiClient(cfg, SecretStr("fake-key"))
    client._client = _client_with(session)  # type: ignore[assignment]  # mock SDK boundary
    return client


async def _drain(client: GeminiClient, text: str) -> list[bytes]:
    return [chunk async for chunk in client.synthesize(text)]


# 24 kHz mono s16le: 480 bytes = 240 samples = 10 ms of audio per chunk.
_CHUNK_24K = b"\x10\x00" * 240


def test_synthesize_resamples_24k_to_16k_and_streams() -> None:
    """Yields resampled 16 kHz chunks incrementally, one per audio response.

    24 kHz → 16 kHz means ~2/3 the bytes out; two audio responses arrive
    before turn_complete, so two chunks are yielded before the stream ends.
    """
    session = _FakeSession(
        [
            _resp(parts=[_audio_part(_CHUNK_24K)]),
            _resp(parts=[_audio_part(_CHUNK_24K)]),
            _resp(turn_complete=True),
        ],
    )
    client = _gemini_client(session)
    chunks = asyncio.run(_drain(client, "hello there friend"))
    assert len(chunks) == 2  # streamed incrementally, one per audio response
    total_out = sum(len(c) for c in chunks)
    total_in = 2 * len(_CHUNK_24K)
    assert 0 < total_out < total_in  # downsampled
    # ~2/3 ratio (allow slack for ratecv filter edge effects).
    assert abs(total_out - total_in * 2 / 3) < 0.15 * total_in


def test_synthesize_sets_approximate_timing() -> None:
    """After draining, last_segment_timing() has one Word per input word, ordered."""
    session = _FakeSession(
        [_resp(parts=[_audio_part(_CHUNK_24K)]), _resp(turn_complete=True)],
    )
    client = _gemini_client(session)
    asyncio.run(_drain(client, "one two three four"))
    timing = client.last_segment_timing()
    assert timing is not None
    assert len(timing.words) == 4  # len("one two three four".split())
    starts = [w.start_ms for w in timing.words]
    assert starts == sorted(starts)  # non-decreasing
    assert timing.words[0].text == "one"


def test_last_segment_timing_none_before_first_call() -> None:
    """No timing exposed before any synthesize() runs."""
    client = _gemini_client(_FakeSession([]))
    assert client.last_segment_timing() is None


def test_synthesize_wraps_api_error_as_gemini_tts_error() -> None:
    """A google.genai APIError during synthesis surfaces as GeminiTtsError."""
    api_err = genai_errors.APIError(503, {"error": {"message": "live down"}})
    session = _FakeSession([], raise_on_send=api_err)
    client = _gemini_client(session)
    with pytest.raises(GeminiTtsError):
        asyncio.run(_drain(client, "hello"))
