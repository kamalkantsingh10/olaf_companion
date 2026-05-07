"""Tests for :class:`HttpOrchestratorClient` (Story 4.2).

Mocks at the Protocol seams (CLAUDE.md rule #7):

- :class:`httpx.AsyncClient` for the ``probe_health`` ``GET /health`` path.
- The module-imported ``aconnect_sse`` symbol for the streaming
  ``dispatch`` path. Patched at the importing module
  (``voice_agent_pipeline.turn.orchestrator.aconnect_sse``), per the
  Python mocking rule "patch where the symbol is looked up".

Each test exercises one behavior of the SSE-streaming + parsing +
error-handling contract — happy path, forward-compat unknown event
type, broken-contract raise paths, missing fields, transport errors,
``probe_health`` happy + sad, ``cancel`` stub, persistent-client
invariant, and the privacy invariant on response_chunk text.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import structlog

from voice_agent_pipeline.errors import OrchestratorError, StartupValidationError
from voice_agent_pipeline.schemas.stream import (
    NarrationEvent,
    ResponseChunkEvent,
    SubagentDoneEvent,
    SubagentStartedEvent,
    TurnEndEvent,
)
from voice_agent_pipeline.turn.orchestrator import HttpOrchestratorClient

# ---------------------------------------------------------------------------
# Fake SSE infrastructure.
#
# ``aconnect_sse`` is an ``@asynccontextmanager``-decorated function that
# yields an ``EventSource`` whose ``aiter_sse()`` is an async iterator of
# ``ServerSentEvent`` instances. We mimic that shape with three small
# pieces:
#
# - ``_FakeSSE`` — duck-types ``ServerSentEvent`` (only ``.data`` is read).
# - ``_FakeEventSource`` — exposes ``aiter_sse()`` as an async generator.
# - ``_make_fake_aconnect_sse`` — builds a ``@asynccontextmanager`` that
#   replaces ``voice_agent_pipeline.turn.orchestrator.aconnect_sse`` and
#   yields the configured event source.
# ---------------------------------------------------------------------------


class _FakeSSE:
    """Minimal stand-in for ``httpx_sse.ServerSentEvent``."""

    def __init__(self, data: str) -> None:
        self.data = data


class _FakeEventSource:
    """Minimal stand-in for ``httpx_sse.EventSource``."""

    def __init__(self, events: list[_FakeSSE]) -> None:
        self._events = events

    async def aiter_sse(self) -> AsyncIterator[_FakeSSE]:
        for event in self._events:
            yield event


def _make_fake_aconnect_sse(
    events: list[_FakeSSE] | None = None,
    raise_on_enter: BaseException | None = None,
):
    """Return a replacement for ``aconnect_sse`` patched into the module.

    The replacement is itself an ``@asynccontextmanager`` so the SUT's
    ``async with aconnect_sse(...) as event_source:`` line works
    unchanged. Tests pass either ``events`` (happy path) or
    ``raise_on_enter`` (e.g., to simulate ``httpx.ConnectError`` when
    the SSE stream fails to open).

    Returns a tuple ``(fake, calls)`` where ``calls`` is a list that
    records the positional + keyword arguments of every call to
    ``aconnect_sse`` so tests can assert on the URL, body, and
    headers.
    """
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @asynccontextmanager
    async def fake(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        if raise_on_enter is not None:
            raise raise_on_enter
        yield _FakeEventSource(events or [])

    return fake, calls


def _make_client_for_dispatch(
    events: list[_FakeSSE] | None = None,
    raise_on_enter: BaseException | None = None,
) -> tuple[HttpOrchestratorClient, list[tuple[tuple[Any, ...], dict[str, Any]]]]:
    """Build (client, calls-list) for a dispatch test.

    The HTTP client is an ``AsyncMock(spec=httpx.AsyncClient)``; only
    ``probe_health`` calls ``client.get`` directly, so for dispatch
    tests the mock isn't actually exercised. ``aconnect_sse`` is the
    real seam.
    """
    http_client = AsyncMock(spec=httpx.AsyncClient)
    return (
        HttpOrchestratorClient(http_client, base_url="http://localhost:8001"),
        [],  # populated by the patch fixture per test
    )


async def _drain(client: HttpOrchestratorClient, transcript: str, session_id: str):
    """Iterate ``client.dispatch(...)`` and return the list of yielded events."""
    out: list[Any] = []
    async for event in client.dispatch(transcript, session_id):
        out.append(event)
    return out


# ---------------------------------------------------------------------------
# Dispatch — happy path + forward-compat + broken-contract paths.
# ---------------------------------------------------------------------------


def test_dispatch_issues_post_with_correct_body() -> None:
    """``dispatch(transcript, session_id)`` POSTs to ``<base>/turn`` with the right shape."""
    fake, calls = _make_fake_aconnect_sse(events=[_FakeSSE('{"type": "turn_end"}')])
    client, _ = _make_client_for_dispatch()

    with patch(
        "voice_agent_pipeline.turn.orchestrator.aconnect_sse",
        fake,
    ):
        asyncio.run(_drain(client, "hello", "session-1"))

    assert len(calls) == 1
    args, kwargs = calls[0]
    # aconnect_sse(self._client, "POST", url, ...)
    assert args[1] == "POST"
    assert args[2] == "http://localhost:8001/turn"
    assert kwargs["json"] == {"transcript": "hello", "session_id": "session-1"}
    assert kwargs["headers"] == {"Accept": "text/event-stream"}


def test_dispatch_yields_parsed_events_for_known_types() -> None:
    """Parses the SSE stream into typed pydantic events via the discriminated union."""
    events = [
        _FakeSSE('{"type": "narration", "text": "thinking..."}'),
        _FakeSSE('{"type": "subagent_started", "name": "calendar_lookup"}'),
        _FakeSSE('{"type": "response_chunk", "text": "here is the answer"}'),
        _FakeSSE('{"type": "turn_end"}'),
    ]
    fake, _ = _make_fake_aconnect_sse(events=events)
    client, _ = _make_client_for_dispatch()

    with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
        result = asyncio.run(_drain(client, "what's on my calendar?", "s1"))

    assert len(result) == 4
    assert isinstance(result[0], NarrationEvent)
    assert result[0].text == "thinking..."
    assert isinstance(result[1], SubagentStartedEvent)
    assert result[1].name == "calendar_lookup"
    assert isinstance(result[2], ResponseChunkEvent)
    assert result[2].text == "here is the answer"
    assert isinstance(result[3], TurnEndEvent)


def test_dispatch_unknown_event_type_logs_warn_and_continues() -> None:
    """Forward-compat: an unknown event ``type`` is logged + skipped, not raised.

    This is the architectural Batch 4 decision — the orchestrator can
    ship new event types without breaking the pipeline.
    """
    events = [
        _FakeSSE('{"type": "narration", "text": "hi"}'),
        _FakeSSE('{"type": "future_extension_v3", "payload": "anything"}'),
        _FakeSSE('{"type": "turn_end"}'),
    ]
    fake, _ = _make_fake_aconnect_sse(events=events)
    client, _ = _make_client_for_dispatch()

    with structlog.testing.capture_logs() as captured:
        with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
            result = asyncio.run(_drain(client, "x", "s1"))

    # Two events yielded (narration + turn_end); the unknown is skipped.
    assert len(result) == 2
    assert isinstance(result[0], NarrationEvent)
    assert isinstance(result[1], TurnEndEvent)
    # WARN log fires with the unknown type.
    unknown = [r for r in captured if r.get("event") == "orchestrator.unknown_event_type"]
    assert unknown, f"expected unknown_event_type log; got: {captured!r}"
    assert unknown[0].get("type") == "future_extension_v3"


def test_dispatch_invalid_json_raises_orchestrator_error() -> None:
    """Malformed JSON inside an SSE event is contract drift, not forward-compat."""
    events = [_FakeSSE("not-valid-json{")]
    fake, _ = _make_fake_aconnect_sse(events=events)
    client, _ = _make_client_for_dispatch()

    with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
        with pytest.raises(OrchestratorError) as excinfo:
            asyncio.run(_drain(client, "x", "s1"))
    assert excinfo.value.context["reason"] == "invalid_json"
    # Privacy: only raw_length, never the malformed payload itself.
    assert "raw_length" in excinfo.value.context
    assert "data" not in excinfo.value.context
    assert "body" not in excinfo.value.context


def test_dispatch_invalid_event_shape_known_type_raises() -> None:
    """A known event type with bad fields is contract drift, not forward-compat.

    ``narration`` requires ``text``; without it, the discriminator-peek
    detects a known type, ``TypeAdapter.validate_python`` raises
    ``ValidationError``, and we map to ``OrchestratorError(reason='invalid_event_shape')``.
    """
    events = [_FakeSSE('{"type": "narration"}')]  # no text field
    fake, _ = _make_fake_aconnect_sse(events=events)
    client, _ = _make_client_for_dispatch()

    with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
        with pytest.raises(OrchestratorError) as excinfo:
            asyncio.run(_drain(client, "x", "s1"))
    assert excinfo.value.context["reason"] == "invalid_event_shape"
    assert excinfo.value.context["type"] == "narration"


def test_dispatch_non_dict_event_payload_raises() -> None:
    """A JSON list as an SSE event payload is contract drift (object expected)."""
    events = [_FakeSSE("[1, 2, 3]")]
    fake, _ = _make_fake_aconnect_sse(events=events)
    client, _ = _make_client_for_dispatch()

    with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
        with pytest.raises(OrchestratorError) as excinfo:
            asyncio.run(_drain(client, "x", "s1"))
    assert excinfo.value.context["reason"] == "invalid_event_shape"
    assert excinfo.value.context["got_type"] == "list"


def test_dispatch_connection_error_raises_orchestrator_error_with_cause() -> None:
    """``httpx.ConnectError`` on stream-open wraps as OrchestratorError + cause chain."""
    fake, _ = _make_fake_aconnect_sse(raise_on_enter=httpx.ConnectError("connection refused"))
    client, _ = _make_client_for_dispatch()

    with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
        with pytest.raises(OrchestratorError) as excinfo:
            asyncio.run(_drain(client, "x", "s1"))
    assert excinfo.value.context["reason"] == "ConnectError"
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


def test_dispatch_read_timeout_raises_orchestrator_error_with_cause() -> None:
    """A 60s+ stall surfaces as ``httpx.ReadTimeout`` → ``OrchestratorError``."""
    fake, _ = _make_fake_aconnect_sse(raise_on_enter=httpx.ReadTimeout("stall"))
    client, _ = _make_client_for_dispatch()

    with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
        with pytest.raises(OrchestratorError) as excinfo:
            asyncio.run(_drain(client, "x", "s1"))
    assert excinfo.value.context["reason"] == "ReadTimeout"
    assert isinstance(excinfo.value.__cause__, httpx.ReadTimeout)


# ---------------------------------------------------------------------------
# Dispatch — logging assertions.
# ---------------------------------------------------------------------------


def test_dispatch_logs_dispatch_started() -> None:
    """The dispatch_started INFO log carries session_id + transcript_length, NOT transcript."""
    fake, _ = _make_fake_aconnect_sse(events=[_FakeSSE('{"type": "turn_end"}')])
    client, _ = _make_client_for_dispatch()

    with structlog.testing.capture_logs() as captured:
        with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
            asyncio.run(_drain(client, "secret-utterance-xyz", "s1"))

    started = [r for r in captured if r.get("event") == "orchestrator.dispatch_started"]
    assert started
    rec = started[0]
    assert rec.get("session_id") == "s1"
    assert rec.get("transcript_length") == len("secret-utterance-xyz")
    # Privacy: never log the transcript itself.
    for record in captured:
        assert "secret-utterance-xyz" not in repr(record), f"transcript leaked into log: {record!r}"


def test_dispatch_logs_dispatch_completed_on_clean_end() -> None:
    """A clean stream end emits dispatch_completed with event_count + duration_ms."""
    events = [
        _FakeSSE('{"type": "narration", "text": "hi"}'),
        _FakeSSE('{"type": "turn_end"}'),
    ]
    fake, _ = _make_fake_aconnect_sse(events=events)
    client, _ = _make_client_for_dispatch()

    with structlog.testing.capture_logs() as captured:
        with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
            asyncio.run(_drain(client, "x", "s1"))

    completed = [r for r in captured if r.get("event") == "orchestrator.dispatch_completed"]
    assert completed
    rec = completed[0]
    assert rec.get("event_count") == 2
    assert "duration_ms" in rec


def test_dispatch_does_not_log_response_chunk_text() -> None:
    """Privacy invariant: response_chunk.text never appears in any captured log."""
    sentinel = "user_secret_response_xyz"
    events = [
        _FakeSSE(json.dumps({"type": "response_chunk", "text": sentinel})),
        _FakeSSE('{"type": "turn_end"}'),
    ]
    fake, _ = _make_fake_aconnect_sse(events=events)
    client, _ = _make_client_for_dispatch()

    with structlog.testing.capture_logs() as captured:
        with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
            asyncio.run(_drain(client, "x", "s1"))

    for record in captured:
        assert sentinel not in repr(record), f"response_chunk.text leaked into log: {record!r}"


# ---------------------------------------------------------------------------
# probe_health.
# ---------------------------------------------------------------------------


def test_probe_health_200_succeeds() -> None:
    """A 200 from ``GET /health`` returns ``None`` (no exception)."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    request = MagicMock()
    request.url = "http://localhost:8001/health"
    resp.request = request
    http_client.get.return_value = resp

    client = HttpOrchestratorClient(http_client, base_url="http://localhost:8001")
    asyncio.run(client.probe_health())

    http_client.get.assert_called_once_with("http://localhost:8001/health")


def test_probe_health_non_200_raises_startup_validation_error() -> None:
    """A 503 raises StartupValidationError with the status_code in context."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 503
    request = MagicMock()
    request.url = "http://localhost:8001/health"
    resp.request = request
    http_client.get.return_value = resp

    client = HttpOrchestratorClient(http_client, base_url="http://localhost:8001")

    with pytest.raises(StartupValidationError) as excinfo:
        asyncio.run(client.probe_health())
    assert excinfo.value.context["status_code"] == 503
    assert excinfo.value.context["reason"] == "orchestrator_health_non_200"


def test_probe_health_connection_error_raises_startup_validation_error() -> None:
    """A transport error wraps as StartupValidationError + cause chain."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = httpx.ConnectError("daemon down")

    client = HttpOrchestratorClient(http_client, base_url="http://localhost:8001")

    with pytest.raises(StartupValidationError) as excinfo:
        asyncio.run(client.probe_health())
    assert excinfo.value.context["reason"] == "ConnectError"
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


# ---------------------------------------------------------------------------
# cancel — stubbed in Epic 4.
# ---------------------------------------------------------------------------


def test_cancel_raises_not_implemented_error() -> None:
    """``cancel`` raises NotImplementedError; v1.5 Story v1.5-1 wires the impl."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    client = HttpOrchestratorClient(http_client, base_url="http://localhost:8001")

    with pytest.raises(NotImplementedError) as excinfo:
        asyncio.run(client.cancel("session-1"))
    # Forward reference to v1.5 so a future contributor knows where the
    # impl lands.
    assert "v1.5" in str(excinfo.value) or "barge-in" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Persistent-client invariant.
# ---------------------------------------------------------------------------


def test_dispatch_does_not_construct_client_per_call() -> None:
    """Multiple dispatches reuse the injected ``httpx.AsyncClient`` instance.

    The persistent-client invariant is the whole point of injecting the
    client — keep-alive pool reuse. If a future refactor accidentally
    constructs a client per call, this test surfaces it.
    """
    fake, calls = _make_fake_aconnect_sse(events=[_FakeSSE('{"type": "turn_end"}')])
    http_client = AsyncMock(spec=httpx.AsyncClient)
    client = HttpOrchestratorClient(http_client, base_url="http://localhost:8001")

    with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
        asyncio.run(_drain(client, "x", "s1"))
        asyncio.run(_drain(client, "y", "s2"))
        asyncio.run(_drain(client, "z", "s3"))

    # All three calls passed the SAME http_client instance to aconnect_sse
    # (positional arg 0). If the impl constructed a new client per call,
    # we'd see different objects here.
    clients_passed = [args[0] for args, _ in calls]
    assert len(calls) == 3
    assert all(c is http_client for c in clients_passed)


# ---------------------------------------------------------------------------
# Coverage for the SubagentDoneEvent type — exercises the full union.
# ---------------------------------------------------------------------------


def test_dispatch_yields_subagent_done_event_correctly() -> None:
    """The ``subagent_done`` event lands as :class:`SubagentDoneEvent`."""
    events = [
        _FakeSSE('{"type": "subagent_done", "name": "calendar_lookup"}'),
        _FakeSSE('{"type": "turn_end"}'),
    ]
    fake, _ = _make_fake_aconnect_sse(events=events)
    client, _ = _make_client_for_dispatch()

    with patch("voice_agent_pipeline.turn.orchestrator.aconnect_sse", fake):
        result = asyncio.run(_drain(client, "x", "s1"))

    assert isinstance(result[0], SubagentDoneEvent)
    assert result[0].name == "calendar_lookup"
