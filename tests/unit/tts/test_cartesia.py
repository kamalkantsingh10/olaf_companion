"""Unit tests for :mod:`voice_agent_pipeline.tts.cartesia`.

The ``cartesia`` module is mocked at the import boundary inside
``tts/cartesia.py`` (``monkeypatch.setattr(cartesia_module, "cartesia", _fake)``)
— same pattern Story 1.7 used for ``faster_whisper`` and Story 2.2 used
for ``openai``. Mocking the global ``cartesia`` package would leak across
tests; patching the imported reference inside the cartesia module is the
architecturally-correct way to honor the mock-at-Protocol-boundaries rule
(architecture.md §"Test Patterns").
"""

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from voice_agent_pipeline.config.setup import (
    AudioConfig,
    SetupConfig,
    TtsConfig,
    WakewordConfig,
)
from voice_agent_pipeline.errors import CartesiaError, StartupValidationError
from voice_agent_pipeline.tts import cartesia as cartesia_module
from voice_agent_pipeline.tts.cartesia import CartesiaClient, validate_credentials


# Stand-in for ``cartesia.APIError`` inside the patched module.
class _FakeAPIError(Exception):
    """Stand-in for ``cartesia.APIError`` — must match what Talker's except clause sees."""


async def _stub_iter_bytes(chunks: list[bytes]) -> AsyncIterator[bytes]:
    """Async generator yielding the given chunks in order — for response.iter_bytes()."""
    for c in chunks:
        yield c


def _make_fake_cartesia(
    chunks: list[bytes] | None = None,
    extra_event_types: list[str] | None = None,
    raise_on_generate: Exception | None = None,
    raise_mid_stream: Exception | None = None,
    capture_kwargs: dict[str, Any] | None = None,
    capture_voices_list_kwargs: dict[str, Any] | None = None,
    raise_on_voices_list: Exception | None = None,
) -> MagicMock:
    """Build a fake replacement for the ``cartesia`` module.

    Args:
        chunks: Bytes for synthesized "chunk" SSE events.
        extra_event_types: Optional non-chunk event types interleaved
            after the first chunk (e.g., "timestamps", "done") — used
            to verify the Talker filters them out.
        raise_on_generate: If set, ``tts.generate_sse`` raises this
            immediately (pre-stream failure).
        raise_mid_stream: If set, the SSE stream raises this AFTER
            yielding the first chunk (mid-stream failure — must still
            wrap as CartesiaError).
        capture_kwargs: Sink for ``tts.generate_sse`` call kwargs.
        capture_voices_list_kwargs: Sink for ``voices.list`` call kwargs.
        raise_on_voices_list: If set, ``voices.list`` raises this.
    """

    class _Event:
        """Stub matching cartesia's SSE event shape: .type + .data.

        Cartesia's real SSE wire format puts base64-encoded audio in
        ``event.data`` (str). This stub mirrors that — bytes are
        base64-encoded for chunk events, raw str for non-chunks.
        """

        def __init__(self, type_: str, data: bytes = b"") -> None:
            self.type = type_
            # Encode chunk-event audio to base64 string per the real
            # SSE format; non-chunk events keep empty/raw strings.
            self.data = base64.b64encode(data).decode("ascii") if data else ""

    fake_client = MagicMock()

    async def _generate_sse(**kwargs: Any) -> Any:
        if capture_kwargs is not None:
            capture_kwargs.update(kwargs)
        if raise_on_generate is not None:
            raise raise_on_generate

        # Build the event list: alternating chunks + any non-chunk
        # filler types the test wants. The Talker MUST yield only
        # chunk-event data, ignoring filler.
        events: list[_Event] = []
        for i, c in enumerate(chunks or []):
            events.append(_Event("chunk", c))
            # Insert each filler event after the first chunk to
            # exercise the type-filter without burying the chunks.
            if i == 0:
                for ft in extra_event_types or []:
                    events.append(_Event(ft, b""))

        async def _iter() -> AsyncIterator[Any]:
            for i, e in enumerate(events):
                if raise_mid_stream is not None and i == 1:
                    raise raise_mid_stream
                yield e

        return _iter()

    fake_client.tts.generate_sse = _generate_sse

    async def _voices_list(**kwargs: Any) -> Any:
        if capture_voices_list_kwargs is not None:
            capture_voices_list_kwargs.update(kwargs)
        if raise_on_voices_list is not None:
            raise raise_on_voices_list
        return MagicMock()

    fake_client.voices.list = _voices_list

    fake_module = MagicMock()
    fake_module.AsyncCartesia = MagicMock(return_value=fake_client)
    fake_module.APIError = _FakeAPIError
    return fake_module


@pytest.fixture
def tts_config() -> TtsConfig:
    """Default TtsConfig for tests — neutral emotion, sonic-3, stub voice."""
    return TtsConfig(
        voice_id="stub-voice-uuid",
        default_emotion="neutral",
        model="sonic-3",
    )


def _collect(stream: AsyncIterator[bytes]) -> list[bytes]:
    """Drain an async iterator into a list — sync helper for tests."""

    async def _drain() -> list[bytes]:
        return [c async for c in stream]

    return asyncio.run(_drain())


def test_synthesize_yields_chunks_in_order(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: chunks come out of synthesize() in the same order the SDK delivered."""
    fake = _make_fake_cartesia(chunks=[b"chunk-a", b"chunk-b", b"chunk-c"])
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    out = _collect(client.synthesize("hello"))

    assert out == [b"chunk-a", b"chunk-b", b"chunk-c"]


def test_synthesize_passes_model_voice_and_format(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK call shape matches: model_id + voice + transcript + 16kHz S16LE format."""
    captured: dict[str, Any] = {}
    fake = _make_fake_cartesia(chunks=[b"x"], capture_kwargs=captured)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    _collect(client.synthesize("hello"))

    assert captured["model_id"] == "sonic-3"
    assert captured["transcript"] == "hello"
    assert captured["voice"] == {"id": "stub-voice-uuid", "mode": "id"}
    # Format pinned to 16 kHz mono S16LE — same as the rest of the pipeline.
    assert captured["output_format"] == {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 16000,
    }
    # Default emotion threaded through generation_config.
    assert captured["generation_config"] == {"emotion": "neutral"}


def test_synthesize_filters_non_chunk_events(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-chunk SSE events (timestamps, done, etc.) are silently dropped.

    Cartesia's SSE stream interleaves chunk events with metadata events
    (e.g. ``timestamps`` if ``add_timestamps=True``). v1 only consumes
    audio bytes; the type filter prevents non-bytes data from leaking
    out of synthesize() as ``yield event.data``.
    """
    fake = _make_fake_cartesia(
        chunks=[b"audio-1", b"audio-2"],
        extra_event_types=["timestamps", "done"],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    out = _collect(client.synthesize("hello"))

    # Only chunk-event payloads make it through; metadata events filtered.
    assert out == [b"audio-1", b"audio-2"]


def test_synthesize_passes_configured_emotion(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override of default_emotion in TtsConfig flows through to generation_config.

    Pinning this contract sets up Story 3.x — when the streaming SSML
    splitter emits per-segment emotion, it'll override default_emotion
    for that call. v1 just proves the wire-through works.
    """
    config = TtsConfig(
        voice_id="v",
        default_emotion="excited",
        model="sonic-3",
    )
    captured: dict[str, Any] = {}
    fake = _make_fake_cartesia(chunks=[b"x"], capture_kwargs=captured)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(config, SecretStr("stub-key"))
    _collect(client.synthesize("hello"))

    assert captured["generation_config"] == {"emotion": "excited"}


def test_synthesize_open_error_wraps_as_cartesia_error(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure at stream-open time raises CartesiaError with cause chain.

    Documents CLAUDE.md rule #4: CartesiaError (an
    ExternalServiceError) must propagate, not be swallowed. The
    ``raise ... from e`` chain lets the operator see the original
    SDK error in post-mortem stack traces.
    """
    boom = _FakeAPIError("cartesia exploded at open")
    fake = _make_fake_cartesia(raise_on_generate=boom)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    with pytest.raises(CartesiaError) as exc_info:
        _collect(client.synthesize("hello"))

    assert exc_info.value.__cause__ is boom
    assert exc_info.value.context.get("voice_id") == "stub-voice-uuid"
    assert exc_info.value.context.get("model") == "sonic-3"
    assert "cartesia exploded at open" in exc_info.value.context.get("reason", "")


def test_synthesize_mid_stream_error_wraps_as_cartesia_error(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure mid-stream (after first chunk) also wraps as CartesiaError.

    Stream-stall watchdog is v2 territory; v1 just propagates the
    underlying httpx / Cartesia error as CartesiaError so systemd can
    restart the process.
    """
    boom = _FakeAPIError("network died mid-stream")
    fake = _make_fake_cartesia(
        chunks=[b"first-chunk", b"never-reached"],
        raise_mid_stream=boom,
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    with pytest.raises(CartesiaError) as exc_info:
        _collect(client.synthesize("hello"))

    assert exc_info.value.__cause__ is boom


def test_synthesize_logs_first_frame_ttfb(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``tts.first_frame`` event fires once per synthesize() with ttfb_ms.

    NFR4 baseline observability hook — operators can read p50/p95
    TTFB from voice-agent.log without DEBUG. The event includes
    voice_id + model so multi-voice deployments can disambiguate.
    """
    import structlog

    fake = _make_fake_cartesia(chunks=[b"chunk-a", b"chunk-b"])
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    with structlog.testing.capture_logs() as captured:
        _collect(client.synthesize("hello"))

    matching = [r for r in captured if r.get("event") == "tts.first_frame"]
    assert len(matching) == 1, f"expected exactly one tts.first_frame; got {captured!r}"
    rec = matching[0]
    assert rec.get("voice_id") == "stub-voice-uuid"
    assert rec.get("model") == "sonic-3"
    # ttfb_ms is wall-clock — at least 0, often a few ms in mocked tests.
    ttfb_ms = rec.get("ttfb_ms")
    assert isinstance(ttfb_ms, int)
    assert ttfb_ms >= 0


# --- validate_credentials ---


@pytest.fixture
def setup_config(tmp_path: Path, tts_config: TtsConfig) -> SetupConfig:
    """A SetupConfig good enough for the credentials probe."""
    return SetupConfig.model_construct(
        schema_version=1,
        picovoice_access_key=SecretStr("stub-pico"),
        cartesia_api_key=SecretStr("stub-cartesia"),
        audio=AudioConfig(input_device_name="m", output_device_name="s"),
        wakeword=WakewordConfig(model_path=Path("models/x.ppn")),
        tts=tts_config,
    )


def test_validate_credentials_calls_voices_list(
    setup_config: SetupConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe issues ``voices.list(limit=1)`` to validate the API key without burning tokens."""
    captured: dict[str, Any] = {}
    fake = _make_fake_cartesia(capture_voices_list_kwargs=captured)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    asyncio.run(validate_credentials(setup_config))

    # limit=1 is the architectural decision — minimal payload, just
    # enough to confirm auth + service health.
    assert captured.get("limit") == 1


def test_validate_credentials_wraps_failure_as_startup_validation_error(
    setup_config: SetupConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad key surfaces as StartupValidationError, not a raw SDK error."""
    boom = _FakeAPIError("401 unauthorized")
    fake = _make_fake_cartesia(raise_on_voices_list=boom)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    with pytest.raises(StartupValidationError) as exc_info:
        asyncio.run(validate_credentials(setup_config))

    assert exc_info.value.__cause__ is boom
    assert exc_info.value.context.get("stage") == "cartesia"
    assert "401" in exc_info.value.context.get("reason", "")


def test_init_uses_async_cartesia(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctor constructs ``cartesia.AsyncCartesia(api_key=...)`` — never the sync flavor.

    The whole pipeline is async; using the sync Cartesia client would
    block the event loop on every TTFB wait. Pinning this contract
    prevents an accidental swap.
    """
    init_kwargs: dict[str, Any] = {}

    async def _empty(**_: Any) -> Any:
        return MagicMock(iter_bytes=lambda: _stub_iter_bytes([]))

    def _construct_client(**kw: Any) -> Any:
        init_kwargs.update(kw)
        client = MagicMock()
        client.tts.generate = _empty
        client.voices.list = _empty
        return client

    fake_module = MagicMock()
    fake_module.AsyncCartesia = MagicMock(side_effect=_construct_client)
    fake_module.APIError = _FakeAPIError
    monkeypatch.setattr(cartesia_module, "cartesia", fake_module)

    CartesiaClient(tts_config, SecretStr("real-key"))

    # The api_key was passed in unwrapped form (SecretStr.get_secret_value()).
    assert init_kwargs["api_key"] == "real-key"


# --- AsyncMock fixture used as a placeholder when iter_bytes shape isn't tested ---
_ = AsyncMock  # silence "unused import" if tests above don't invoke directly
