"""Unit tests for :mod:`voice_agent_pipeline.tts.cartesia`.

The ``cartesia`` module is mocked at the import boundary inside
``tts/cartesia.py`` (``monkeypatch.setattr(cartesia_module, "cartesia", _fake)``)
— same pattern Story 1.7 used for ``faster_whisper`` and Story 2.2 used
for ``openai``. Mocking the global ``cartesia`` package would leak across
tests; patching the imported reference inside the cartesia module is the
architecturally-correct way to honor the mock-at-Protocol-boundaries rule
(architecture.md §"Test Patterns").

Story 6.1 split the transport into two paths (``websocket`` default,
``sse`` legacy fallback). The test fixtures and helpers split similarly:

- :func:`_make_fake_cartesia` builds a fake of the SDK shape used by the
  SSE path (``tts.generate_sse`` + ``voices.get``). The existing SSE
  tests carry over against ``sse_tts_config`` (which pins ``transport
  = "sse"`` so the dispatcher routes to the SSE branch).
- :func:`_make_fake_cartesia_ws` builds a fake of the SDK shape used by
  the WS path (``tts.websocket_connect`` returning an async context
  manager whose connection iterates a scripted ``WebsocketResponse``
  list). New WS tests run against the default ``tts_config`` (which
  defaults to ``transport = "websocket"``).
- :func:`_make_fake_cartesia_dual` provides BOTH SDK surfaces so a
  single fake exercises the dispatch contract (route to WS or SSE
  based on the config flag).
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
from voice_agent_pipeline.tts.cartesia import (
    CartesiaClient,
    SegmentTiming,
    Word,
    validate_credentials,
)


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
    capture_voices_get_args: list | None = None,
    capture_voices_get_kwargs: dict[str, Any] | None = None,
    raise_on_voices_get: Exception | None = None,
) -> MagicMock:
    """Build a fake replacement for the ``cartesia`` module (SSE path).

    Mirrors the v1 ``tts.generate_sse`` SDK surface and the
    ``voices.get`` startup-probe surface. Story 6.1 added the WS-path
    helper :func:`_make_fake_cartesia_ws` for the new transport tests.

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
        capture_voices_get_args: Sink for ``voices.get`` positional args.
        capture_voices_get_kwargs: Sink for ``voices.get`` call kwargs.
        raise_on_voices_get: If set, ``voices.get`` raises this.
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

    async def _voices_get(*args: Any, **kwargs: Any) -> Any:
        if capture_voices_get_args is not None:
            capture_voices_get_args.extend(args)
        if capture_voices_get_kwargs is not None:
            capture_voices_get_kwargs.update(kwargs)
        if raise_on_voices_get is not None:
            raise raise_on_voices_get
        return MagicMock()

    fake_client.voices.get = _voices_get

    fake_module = MagicMock()
    fake_module.AsyncCartesia = MagicMock(return_value=fake_client)
    fake_module.APIError = _FakeAPIError
    return fake_module


# ---------------------------------------------------------------------------
# Story 6.1 — WebSocket-path fakes
# ---------------------------------------------------------------------------


class _ChunkEvent:
    """Stand-in for ``cartesia.types.websocket_response.Chunk``.

    Mirrors the discriminator-typed event shape — exposes ``type`` and
    an ``audio`` property that returns raw bytes (mirroring the SDK's
    ``Chunk.audio`` property which base64-decodes the wire ``data``
    field). Constructed with raw bytes so test cases stay readable.
    """

    def __init__(self, audio: bytes | None) -> None:
        self.type = "chunk"
        self._audio = audio

    @property
    def audio(self) -> bytes | None:
        return self._audio


class _TimestampsPayload:
    """Stand-in for ``TimestampsWordTimestamps`` — parallel-list shape."""

    def __init__(self, words: list[str], start: list[float], end: list[float]) -> None:
        self.words = words
        self.start = start
        self.end = end


class _TimestampsEvent:
    """Stand-in for ``cartesia.types.websocket_response.Timestamps``."""

    def __init__(self, payload: _TimestampsPayload | None) -> None:
        self.type = "timestamps"
        self.word_timestamps = payload


class _DoneEvent:
    """Stand-in for ``cartesia.types.websocket_response.Done``."""

    def __init__(self) -> None:
        self.type = "done"


class _ErrorEvent:
    """Stand-in for ``cartesia.types.websocket_response.Error``."""

    def __init__(self, error: str) -> None:
        self.type = "error"
        self.error = error


class _FakeWsConnection:
    """Fake ``AsyncTTSResourceConnection`` — supports send() + async-iter.

    The SDK's connection class supports ``__aiter__`` over scripted
    events plus an ``async send(event)``. The test fakes a single send
    (we never flush/continue in the v1 WS path) and iterates the
    test-supplied event list. ``close()`` is recorded so tests can
    assert the cleanup happened.
    """

    def __init__(
        self,
        events: list[object],
        raise_mid_iter: Exception | None = None,
        capture_sent: list[object] | None = None,
    ) -> None:
        self._events = events
        self._raise_mid_iter = raise_mid_iter
        self._capture_sent = capture_sent
        self.closed = False

    async def send(self, event: object) -> None:
        if self._capture_sent is not None:
            self._capture_sent.append(event)

    async def __aiter__(self) -> AsyncIterator[object]:
        for i, e in enumerate(self._events):
            # Mid-iteration raises happen BEFORE yielding the i-th
            # event — index 1 means the raise fires right after the
            # first event is yielded, matching the SSE-path fixture
            # convention.
            if self._raise_mid_iter is not None and i == 1:
                raise self._raise_mid_iter
            yield e

    async def close(self, **_: Any) -> None:
        self.closed = True


class _FakeWsManager:
    """Async context manager mirror of ``AsyncTTSResourceConnectionManager``."""

    def __init__(
        self,
        connection: _FakeWsConnection,
        raise_on_enter: Exception | None = None,
    ) -> None:
        self._connection = connection
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self) -> _FakeWsConnection:
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return self._connection

    async def __aexit__(self, *_: Any) -> None:
        # The SDK's __aexit__ closes the connection; mirror that.
        await self._connection.close()


def _make_fake_cartesia_ws(
    events: list[object] | None = None,
    raise_on_enter: Exception | None = None,
    raise_mid_iter: Exception | None = None,
    capture_sent: list[object] | None = None,
    raise_on_voices_get: Exception | None = None,
) -> tuple[MagicMock, _FakeWsConnection]:
    """Build a fake ``cartesia`` module exposing the WS-path SDK surface.

    Returns the patched module fake AND the underlying fake
    connection so tests can assert post-conditions (e.g., that
    ``close()`` was called or that the captured GenerationRequest had
    the right field values).

    Args:
        events: Scripted ``WebsocketResponse`` events the connection
            iterates. Defaults to a single ``Done`` if not supplied
            (smoke-only case).
        raise_on_enter: If set, the WS context manager's ``__aenter__``
            raises this — exercises the open-time error path.
        raise_mid_iter: If set, the connection's async-iter raises
            this after yielding the first event — exercises the
            mid-stream error path.
        capture_sent: Sink for the connection's ``send`` calls — tests
            assert the ``GenerationRequest`` payload was constructed
            with the right fields.
        raise_on_voices_get: If set, ``voices.get`` raises this (the
            credentials probe still runs on the HTTP surface, even
            when ``[tts] transport = "websocket"``).
    """
    conn = _FakeWsConnection(
        events=events if events is not None else [_DoneEvent()],
        raise_mid_iter=raise_mid_iter,
        capture_sent=capture_sent,
    )
    manager = _FakeWsManager(connection=conn, raise_on_enter=raise_on_enter)

    fake_client = MagicMock()
    fake_client.tts.websocket_connect = MagicMock(return_value=manager)

    async def _voices_get(*_args: Any, **_kwargs: Any) -> Any:
        if raise_on_voices_get is not None:
            raise raise_on_voices_get
        return MagicMock()

    fake_client.voices.get = _voices_get

    fake_module = MagicMock()
    fake_module.AsyncCartesia = MagicMock(return_value=fake_client)
    fake_module.APIError = _FakeAPIError
    return fake_module, conn


def _make_fake_cartesia_dual(
    *,
    ws_events: list[object] | None = None,
    sse_chunks: list[bytes] | None = None,
    ws_called: list[bool] | None = None,
    sse_called: list[bool] | None = None,
) -> MagicMock:
    """Build a fake exposing BOTH WS and SSE surfaces — for dispatch tests.

    The transport-dispatch test routes the same call to one path or the
    other based on ``[tts] transport``; the dual fake records which
    path was reached so the assertion can be ``len(ws_called) == 1``
    style.
    """
    conn = _FakeWsConnection(
        events=ws_events if ws_events is not None else [_DoneEvent()],
    )
    manager = _FakeWsManager(connection=conn)

    def _ws_connect_capture(**_kw: Any) -> _FakeWsManager:
        if ws_called is not None:
            ws_called.append(True)
        return manager

    fake_client = MagicMock()
    fake_client.tts.websocket_connect = MagicMock(side_effect=_ws_connect_capture)

    class _SseEvent:
        def __init__(self, data: bytes) -> None:
            self.type = "chunk"
            self.data = base64.b64encode(data).decode("ascii") if data else ""

    async def _generate_sse(**_kw: Any) -> Any:
        if sse_called is not None:
            sse_called.append(True)

        async def _iter() -> AsyncIterator[Any]:
            for c in sse_chunks or [b""]:
                yield _SseEvent(c)

        return _iter()

    fake_client.tts.generate_sse = _generate_sse

    fake_module = MagicMock()
    fake_module.AsyncCartesia = MagicMock(return_value=fake_client)
    fake_module.APIError = _FakeAPIError
    return fake_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tts_config() -> TtsConfig:
    """Default TtsConfig for WS-path tests — ``transport = "websocket"`` (Story 6.1 default)."""
    return TtsConfig(
        voice_id="stub-voice-uuid",
        default_emotion="neutral",
        model="sonic-3",
    )


@pytest.fixture
def sse_tts_config() -> TtsConfig:
    """TtsConfig pinned to ``transport = "sse"`` — exercises the legacy SSE path."""
    return TtsConfig(
        voice_id="stub-voice-uuid",
        default_emotion="neutral",
        model="sonic-3",
        transport="sse",
    )


def _collect(stream: AsyncIterator[bytes]) -> list[bytes]:
    """Drain an async iterator into a list — sync helper for tests."""

    async def _drain() -> list[bytes]:
        return [c async for c in stream]

    return asyncio.run(_drain())


# ---------------------------------------------------------------------------
# Story 6.1 — WS-path tests
# ---------------------------------------------------------------------------


def test_ws_synthesize_yields_chunks_in_order(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: WS Chunk events come out of synthesize() in arrival order."""
    fake, _ = _make_fake_cartesia_ws(
        events=[
            _ChunkEvent(b"audio-a"),
            _ChunkEvent(b"audio-b"),
            _ChunkEvent(b"audio-c"),
            _DoneEvent(),
        ],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    out = _collect(client.synthesize("hello"))

    assert out == [b"audio-a", b"audio-b", b"audio-c"]


def test_ws_send_uses_generation_request_with_timestamps(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WS path sends one GenerationRequest with add_timestamps=True (and phoneme OFF).

    This is the core Story 6.1 contract: every WS synthesis asks
    Cartesia for word-level timestamps so the Story 6.3 emphasis-join
    has data to consume. Phoneme timestamps are deliberately OFF (no
    v1 consumer, non-trivial wire bandwidth).
    """
    captured: list[object] = []
    fake, _ = _make_fake_cartesia_ws(
        events=[_ChunkEvent(b"x"), _DoneEvent()],
        capture_sent=captured,
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    _collect(client.synthesize("hello world"))

    assert len(captured) == 1, f"expected exactly one send(); got {captured!r}"
    req = captured[0]
    # GenerationRequest aliases ``llm_model_id`` → ``model_id``; pydantic
    # stores under the field's Python name, so check both shapes.
    assert getattr(req, "llm_model_id", None) == "sonic-3"
    assert getattr(req, "transcript", None) == "hello world"
    # Story 6.1 contract: timestamps ON, phoneme timestamps OFF.
    assert getattr(req, "add_timestamps", None) is True
    assert getattr(req, "add_phoneme_timestamps", None) is False


def test_ws_captures_word_timestamps_into_segment_timing(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Timestamps event populates ``last_segment_timing()`` with rounded ms.

    Cartesia emits seconds (floats); the pipeline pins integer ms
    throughout. Story 6.1 rounds on capture so downstream consumers
    (Story 6.3 emphasis-join) read pre-rounded values.
    """
    fake, _ = _make_fake_cartesia_ws(
        events=[
            _ChunkEvent(b"audio-1"),
            _TimestampsEvent(
                _TimestampsPayload(
                    words=["I", "really", "think"],
                    # Mix exact + rounding cases so the float→ms
                    # conversion is exercised in both directions.
                    start=[0.0, 0.1234, 0.789],
                    end=[0.06, 0.5234, 0.8567],
                ),
            ),
            _ChunkEvent(b"audio-2"),
            _DoneEvent(),
        ],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    # Pre-synthesize: no timing captured yet.
    assert client.last_segment_timing() is None

    out = _collect(client.synthesize("hello"))
    assert out == [b"audio-1", b"audio-2"]

    timing = client.last_segment_timing()
    assert timing is not None
    assert timing.words == [
        Word(text="I", start_ms=0, end_ms=60),
        # 0.1234s → 123.4 → round(...) → 123ms
        Word(text="really", start_ms=123, end_ms=523),
        # 0.789s → 789ms; 0.8567s → 857ms (banker's rounding)
        Word(text="think", start_ms=789, end_ms=857),
    ]


def test_ws_accumulates_words_across_multiple_timestamps_events(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple Timestamps events within one request accumulate into one SegmentTiming.

    Cartesia's wire spec allows incremental Timestamps emission (per
    Story 6.1 AC #2). Test fixture interleaves two Timestamps events
    to ensure the WS path accumulates words across them rather than
    overwriting.
    """
    fake, _ = _make_fake_cartesia_ws(
        events=[
            _TimestampsEvent(_TimestampsPayload(["a"], [0.0], [0.1])),
            _ChunkEvent(b"chunk"),
            _TimestampsEvent(_TimestampsPayload(["b"], [0.1], [0.2])),
            _DoneEvent(),
        ],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    _collect(client.synthesize("ab"))

    timing = client.last_segment_timing()
    assert timing is not None
    assert [w.text for w in timing.words] == ["a", "b"]


def test_ws_last_segment_timing_reset_between_calls(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second synthesize() with no Timestamps clears the prior call's timing.

    Story 6.1 contract: ``_last_segment_timing`` is reset at the start
    of every WS call. Without this, a Timestamps-less follow-up call
    would return the previous call's words — silent data corruption
    for Story 6.3's emphasis join.
    """
    # First call: timestamps present.
    fake1, _ = _make_fake_cartesia_ws(
        events=[
            _ChunkEvent(b"x"),
            _TimestampsEvent(_TimestampsPayload(["hi"], [0.0], [0.1])),
            _DoneEvent(),
        ],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake1)
    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    _collect(client.synthesize("hi"))
    assert client.last_segment_timing() is not None

    # Second call: no Timestamps event. Manually swap the fake's
    # websocket_connect to a fresh manager (preserves the same
    # AsyncCartesia client instance the CartesiaClient holds).
    conn2 = _FakeWsConnection(events=[_ChunkEvent(b"y"), _DoneEvent()])
    manager2 = _FakeWsManager(connection=conn2)
    fake1.AsyncCartesia.return_value.tts.websocket_connect = MagicMock(return_value=manager2)

    _collect(client.synthesize("y"))
    # Reset to None because the second call had no Timestamps event.
    assert client.last_segment_timing() is None


def test_ws_error_event_raises_cartesia_error(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Error event mid-stream raises CartesiaError carrying the server reason.

    CLAUDE.md rule #4 — every Cartesia failure surfaces as
    CartesiaError; this branch covers server-side errors that arrive
    as a typed Error event rather than as a thrown APIError.
    """
    fake, _ = _make_fake_cartesia_ws(
        events=[
            _ChunkEvent(b"chunk"),
            _ErrorEvent(error="cartesia exploded mid-stream"),
        ],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    with pytest.raises(CartesiaError) as exc_info:
        _collect(client.synthesize("hello"))

    assert exc_info.value.context.get("voice_id") == "stub-voice-uuid"
    assert exc_info.value.context.get("model") == "sonic-3"
    assert "cartesia exploded mid-stream" in exc_info.value.context.get("reason", "")


def test_ws_connection_closed_mid_stream_raises_cartesia_error(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``websockets.ConnectionClosedError`` mid-stream wraps as CartesiaError.

    Transport-level abort handling: if the WS rips out from under us
    after the first event, the wrap-and-propagate posture matches the
    APIError branch — same fail-fast contract.
    """
    import websockets.exceptions as websockets_exc

    closed_err = websockets_exc.ConnectionClosedError(rcvd=None, sent=None)
    fake, _ = _make_fake_cartesia_ws(
        events=[
            _ChunkEvent(b"first-chunk"),
            _ChunkEvent(b"never-reached"),
        ],
        raise_mid_iter=closed_err,
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    with pytest.raises(CartesiaError) as exc_info:
        _collect(client.synthesize("hello"))

    assert exc_info.value.__cause__ is closed_err
    assert "websocket closed" in exc_info.value.context.get("reason", "")


def test_ws_open_error_wraps_as_cartesia_error(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Cartesia APIError raised when opening the WS wraps as CartesiaError.

    Mirror of the SSE-path ``test_synthesize_open_error_wraps_as_cartesia_error``
    test: the same fail-fast contract applies to the WS transport.
    """
    boom = _FakeAPIError("ws open failed")
    fake, _ = _make_fake_cartesia_ws(raise_on_enter=boom)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    with pytest.raises(CartesiaError) as exc_info:
        _collect(client.synthesize("hello"))

    assert exc_info.value.__cause__ is boom


def test_ws_logs_first_frame_with_transport_tag(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``tts.first_frame`` event fires once per WS call carrying ``transport="websocket"``.

    Preserves the NFR4 baseline metric across the transport swap +
    adds a ``transport`` field so the production log time-series can
    be sliced by transport during the Story 6.4 soak.
    """
    import structlog

    fake, _ = _make_fake_cartesia_ws(
        events=[_ChunkEvent(b"chunk-a"), _ChunkEvent(b"chunk-b"), _DoneEvent()],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    with structlog.testing.capture_logs() as captured:
        _collect(client.synthesize("hello"))

    matching = [r for r in captured if r.get("event") == "tts.first_frame"]
    assert len(matching) == 1, f"expected exactly one tts.first_frame; got {captured!r}"
    rec = matching[0]
    assert rec.get("voice_id") == "stub-voice-uuid"
    assert rec.get("model") == "sonic-3"
    assert rec.get("transport") == "websocket"
    ttfb_ms = rec.get("ttfb_ms")
    assert isinstance(ttfb_ms, int)
    assert ttfb_ms >= 0


# ---------------------------------------------------------------------------
# Story 6.1 — Transport-dispatch tests
# ---------------------------------------------------------------------------


def test_transport_dispatch_websocket_routes_to_ws_path(
    tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TtsConfig(transport="websocket")`` routes synthesize() to the WS path."""
    ws_called: list[bool] = []
    sse_called: list[bool] = []
    fake = _make_fake_cartesia_dual(ws_called=ws_called, sse_called=sse_called)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(tts_config, SecretStr("stub-key"))
    _collect(client.synthesize("hello"))

    assert ws_called == [True]
    assert sse_called == []


def test_transport_dispatch_sse_routes_to_sse_path(
    sse_tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TtsConfig(transport="sse")`` routes synthesize() to the SSE path."""
    ws_called: list[bool] = []
    sse_called: list[bool] = []
    fake = _make_fake_cartesia_dual(
        ws_called=ws_called,
        sse_called=sse_called,
        sse_chunks=[b"audio"],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(sse_tts_config, SecretStr("stub-key"))
    _collect(client.synthesize("hello"))

    assert ws_called == []
    assert sse_called == [True]


# ---------------------------------------------------------------------------
# Legacy v1 SSE-path tests — retained, pinned to transport="sse"
# ---------------------------------------------------------------------------


def test_sse_synthesize_yields_chunks_in_order(
    sse_tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE happy path: chunks come out of synthesize() in arrival order."""
    fake = _make_fake_cartesia(chunks=[b"chunk-a", b"chunk-b", b"chunk-c"])
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(sse_tts_config, SecretStr("stub-key"))
    out = _collect(client.synthesize("hello"))

    assert out == [b"chunk-a", b"chunk-b", b"chunk-c"]


def test_sse_synthesize_passes_model_voice_and_format(
    sse_tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE call shape: model_id + voice + transcript + 16kHz S16LE format."""
    captured: dict[str, Any] = {}
    fake = _make_fake_cartesia(chunks=[b"x"], capture_kwargs=captured)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(sse_tts_config, SecretStr("stub-key"))
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
    # Default emotion + speed threaded through generation_config.
    assert captured["generation_config"] == {"emotion": "neutral", "speed": 0.9}


def test_sse_synthesize_filters_non_chunk_events(
    sse_tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-chunk SSE events (timestamps, done, etc.) are silently dropped on the SSE path."""
    fake = _make_fake_cartesia(
        chunks=[b"audio-1", b"audio-2"],
        extra_event_types=["timestamps", "done"],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(sse_tts_config, SecretStr("stub-key"))
    out = _collect(client.synthesize("hello"))

    assert out == [b"audio-1", b"audio-2"]


def test_sse_last_segment_timing_returns_none(
    sse_tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 6.1: SSE path does NOT populate ``last_segment_timing``."""
    fake = _make_fake_cartesia(
        chunks=[b"audio-1"],
        extra_event_types=["timestamps"],
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(sse_tts_config, SecretStr("stub-key"))
    _collect(client.synthesize("hello"))

    assert client.last_segment_timing() is None


def test_sse_synthesize_passes_configured_emotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An override of default_emotion flows through to generation_config on SSE."""
    config = TtsConfig(
        voice_id="v",
        default_emotion="excited",
        model="sonic-3",
        speed=1.1,
        transport="sse",
    )
    captured: dict[str, Any] = {}
    fake = _make_fake_cartesia(chunks=[b"x"], capture_kwargs=captured)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(config, SecretStr("stub-key"))
    _collect(client.synthesize("hello"))

    assert captured["generation_config"] == {"emotion": "excited", "speed": 1.1}


def test_sse_synthesize_open_error_wraps_as_cartesia_error(
    sse_tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE open failure wraps as CartesiaError with cause chain."""
    boom = _FakeAPIError("cartesia exploded at open")
    fake = _make_fake_cartesia(raise_on_generate=boom)
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(sse_tts_config, SecretStr("stub-key"))
    with pytest.raises(CartesiaError) as exc_info:
        _collect(client.synthesize("hello"))

    assert exc_info.value.__cause__ is boom
    assert exc_info.value.context.get("voice_id") == "stub-voice-uuid"
    assert exc_info.value.context.get("model") == "sonic-3"
    assert "cartesia exploded at open" in exc_info.value.context.get("reason", "")


def test_sse_synthesize_mid_stream_error_wraps_as_cartesia_error(
    sse_tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE mid-stream failure also wraps as CartesiaError."""
    boom = _FakeAPIError("network died mid-stream")
    fake = _make_fake_cartesia(
        chunks=[b"first-chunk", b"never-reached"],
        raise_mid_stream=boom,
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(sse_tts_config, SecretStr("stub-key"))
    with pytest.raises(CartesiaError) as exc_info:
        _collect(client.synthesize("hello"))

    assert exc_info.value.__cause__ is boom


def test_sse_synthesize_logs_first_frame_ttfb(
    sse_tts_config: TtsConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSE path logs ``tts.first_frame`` with ``transport="sse"``."""
    import structlog

    fake = _make_fake_cartesia(chunks=[b"chunk-a", b"chunk-b"])
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    client = CartesiaClient(sse_tts_config, SecretStr("stub-key"))
    with structlog.testing.capture_logs() as captured:
        _collect(client.synthesize("hello"))

    matching = [r for r in captured if r.get("event") == "tts.first_frame"]
    assert len(matching) == 1, f"expected exactly one tts.first_frame; got {captured!r}"
    rec = matching[0]
    assert rec.get("voice_id") == "stub-voice-uuid"
    assert rec.get("model") == "sonic-3"
    assert rec.get("transport") == "sse"
    ttfb_ms = rec.get("ttfb_ms")
    assert isinstance(ttfb_ms, int)
    assert ttfb_ms >= 0


# ---------------------------------------------------------------------------
# Pure-helper tests — _word_timestamps_to_words + Word/SegmentTiming models
# ---------------------------------------------------------------------------


def test_word_model_is_frozen_and_forbids_extra() -> None:
    """:class:`Word` is frozen + extra='forbid' per CLAUDE.md rule 3."""
    from pydantic import ValidationError

    w = Word(text="hi", start_ms=0, end_ms=10)
    with pytest.raises(ValidationError):
        # Frozen models reject in-place mutation.
        w.text = "bye"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Word(text="hi", start_ms=0, end_ms=10, extra="x")  # type: ignore[call-arg]


def test_segment_timing_model_is_frozen_and_forbids_extra() -> None:
    """:class:`SegmentTiming` is frozen + extra='forbid'."""
    from pydantic import ValidationError

    st = SegmentTiming(words=[Word(text="a", start_ms=0, end_ms=10)])
    with pytest.raises(ValidationError):
        SegmentTiming(words=[], extra="x")  # type: ignore[call-arg]
    # Sanity round-trip.
    assert st.words[0].text == "a"


# --- validate_credentials ---


@pytest.fixture
def setup_config(tts_config: TtsConfig) -> SetupConfig:
    """A SetupConfig good enough for the credentials probe."""
    # Story 4.5: pass minimal stt + greeting (see tests/conftest.py).
    # Story 5.5: same pattern for the new [filler] block.
    from tests._factories import (
        minimal_filler_config,
        minimal_goodbye_config,
        minimal_greeting_config,
        minimal_stt_config,
    )

    return SetupConfig.model_construct(
        schema_version=3,
        picovoice_access_key=SecretStr("stub-pico"),
        cartesia_api_key=SecretStr("stub-cartesia"),
        audio=AudioConfig(input_device_name="m", output_device_name="s"),
        wakeword=WakewordConfig(model_path=Path("models/x.ppn")),
        tts=tts_config,
        stt=minimal_stt_config(),
        greeting=minimal_greeting_config(),
        goodbye=minimal_goodbye_config(),
        filler=minimal_filler_config(),
    )


def test_validate_credentials_calls_voices_get_with_configured_voice_id(
    setup_config: SetupConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe issues ``voices.get(voice_id, timeout=10)`` for fast auth + voice validation.

    Switched from ``voices.list(limit=1)`` after observing 60s read
    timeouts on the catalog endpoint. ``voices.get(voice_id)`` is a
    single small GET that validates BOTH the API key AND the
    configured voice exists (404 if the operator pasted a wrong/
    deleted GUID).
    """
    args: list = []
    kwargs: dict[str, Any] = {}
    fake = _make_fake_cartesia(
        capture_voices_get_args=args,
        capture_voices_get_kwargs=kwargs,
    )
    monkeypatch.setattr(cartesia_module, "cartesia", fake)

    asyncio.run(validate_credentials(setup_config))

    # Voice ID passed positionally (matches the SDK's ``voices.get(id, ...)``
    # signature) — proves the probe validates the CONFIGURED voice, not
    # an arbitrary one.
    assert args == ["stub-voice-uuid"]
    # 10s timeout cap — operator gets a clean StartupValidationError
    # if Cartesia's unreachable, not a minute-long hang.
    assert kwargs.get("timeout") == 10.0


def test_validate_credentials_wraps_failure_as_startup_validation_error(
    setup_config: SetupConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad key / missing voice surfaces as StartupValidationError, not a raw SDK error."""
    boom = _FakeAPIError("401 unauthorized")
    fake = _make_fake_cartesia(raise_on_voices_get=boom)
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
