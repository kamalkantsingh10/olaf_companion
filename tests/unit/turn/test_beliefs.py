"""Tests for :class:`HttpBeliefStateClient` (Story 4.1).

The :class:`httpx.AsyncClient` is mocked at the Protocol seam (CLAUDE.md
rule #7). Each test exercises one behavior of the contract — happy
path, non-200, transport error, JSON decode failure, non-dict response,
the persistent-client invariant, and the privacy invariant (response
body never logged at any level).

We use :class:`unittest.mock.AsyncMock` with ``spec=httpx.AsyncClient``
so the mock's surface matches the real client's, not the architecture's
"narrow dep tree" rule (no respx / pytest-httpx).
"""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import structlog

from voice_agent_pipeline.errors import OrchestratorError
from voice_agent_pipeline.turn.beliefs import HttpBeliefStateClient


def _make_response(
    status_code: int,
    json_data: Any = None,
    text: str = "",
    *,
    raise_on_json: Exception | None = None,
) -> MagicMock:
    """Build an ``httpx.Response`` mock for a single test case.

    Args:
        status_code: HTTP status to surface.
        json_data: Object the ``.json()`` call returns. Ignored when
            ``raise_on_json`` is set.
        text: Response body text (for the truncated error context).
        raise_on_json: Set to a :class:`json.JSONDecodeError` to
            simulate malformed-JSON responses.
    """
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text
    if raise_on_json is not None:
        resp.json = MagicMock(side_effect=raise_on_json)
    else:
        resp.json = MagicMock(return_value=json_data)
    request = MagicMock()
    # ``str(resp.request.url)`` lands in OrchestratorError.context["url"].
    request.url = "http://localhost:8001/beliefs?keys=time"
    resp.request = request
    return resp


def _make_client(
    get_return_value: MagicMock | None = None,
    get_side_effect: BaseException | None = None,
) -> tuple[HttpBeliefStateClient, AsyncMock]:
    """Build (HttpBeliefStateClient, http_client_mock) per test scenario.

    Each test configures ``http_client.get`` independently. The
    persistent-client invariant test reuses one client across multiple
    ``read()`` calls.
    """
    http_client = AsyncMock(spec=httpx.AsyncClient)
    if get_side_effect is not None:
        http_client.get.side_effect = get_side_effect
    else:
        http_client.get.return_value = get_return_value
    client = HttpBeliefStateClient(http_client, base_url="http://localhost:8001")
    return client, http_client


def test_read_issues_get_with_comma_joined_keys() -> None:
    """``read(["time", "calendar_today"])`` issues GET with the comma-joined keys param.

    Architecture's URL convention is the single ``?keys=time,calendar_today``
    form, NOT the repeated ``?keys=time&keys=calendar_today`` form. Verify
    via the call args on the mock.
    """
    resp = _make_response(200, json_data={"time": "08:47", "calendar_today": []})
    client, http_client = _make_client(get_return_value=resp)

    result = asyncio.run(client.read(["time", "calendar_today"]))

    assert result == {"time": "08:47", "calendar_today": []}
    http_client.get.assert_called_once_with(
        "http://localhost:8001/beliefs",
        params={"keys": "time,calendar_today"},
    )


def test_read_returns_parsed_json_dict() -> None:
    """Happy-path read returns the parsed dict shape verbatim."""
    resp = _make_response(200, json_data={"a": 1, "b": [2, 3], "c": {"nested": True}})
    client, _ = _make_client(get_return_value=resp)

    result = asyncio.run(client.read(["a", "b", "c"]))

    assert result == {"a": 1, "b": [2, 3], "c": {"nested": True}}


def test_read_empty_keys_still_calls_endpoint() -> None:
    """``read([])`` issues the request with ``params={"keys": ""}`` — daemon decides.

    The client doesn't second-guess "what should empty keys mean"; that's
    the daemon's contract.
    """
    resp = _make_response(200, json_data={})
    client, http_client = _make_client(get_return_value=resp)

    result = asyncio.run(client.read([]))

    assert result == {}
    http_client.get.assert_called_once_with(
        "http://localhost:8001/beliefs",
        params={"keys": ""},
    )


def test_read_500_raises_orchestrator_error() -> None:
    """Non-200 5xx raises :class:`OrchestratorError` with status_code + body context.

    Body is truncated to 200 chars defensively so accidental ``str(err)``
    logging doesn't exfiltrate large response bodies.
    """
    resp = _make_response(500, text="Internal Server Error")
    client, _ = _make_client(get_return_value=resp)

    with pytest.raises(OrchestratorError) as excinfo:
        asyncio.run(client.read(["time"]))
    assert excinfo.value.context["status_code"] == 500
    assert excinfo.value.context["body"] == "Internal Server Error"


def test_read_404_raises_orchestrator_error() -> None:
    """4xx responses surface the same way as 5xx — no per-class branching at the client."""
    resp = _make_response(404, text="not found")
    client, _ = _make_client(get_return_value=resp)

    with pytest.raises(OrchestratorError) as excinfo:
        asyncio.run(client.read(["time"]))
    assert excinfo.value.context["status_code"] == 404


def test_read_connection_error_raises_orchestrator_error() -> None:
    """``httpx.ConnectError`` (transport failure) wraps as OrchestratorError + cause chain.

    The ``raise ... from exc`` shape preserves the original traceback so
    a postmortem can see what httpx actually raised.
    """
    client, _ = _make_client(get_side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(OrchestratorError) as excinfo:
        asyncio.run(client.read(["time"]))
    assert excinfo.value.context["reason"] == "ConnectError"
    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)


def test_read_invalid_json_raises_orchestrator_error() -> None:
    """Malformed JSON in the response body raises OrchestratorError(reason='invalid_json')."""
    resp = _make_response(
        200,
        text="not-json",
        raise_on_json=json.JSONDecodeError("bad", "not-json", 0),
    )
    client, _ = _make_client(get_return_value=resp)

    with pytest.raises(OrchestratorError) as excinfo:
        asyncio.run(client.read(["time"]))
    assert excinfo.value.context["reason"] == "invalid_json"
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


def test_read_non_dict_response_raises_orchestrator_error() -> None:
    """A JSON list (not object) is contract drift; raises invalid_response_shape.

    The architectural contract is "JSON object"; a list / scalar / null
    means the daemon broke its end of the contract — fail-fast, don't
    silently coerce.
    """
    resp = _make_response(200, json_data=["a", "b", "c"])
    client, _ = _make_client(get_return_value=resp)

    with pytest.raises(OrchestratorError) as excinfo:
        asyncio.run(client.read(["time"]))
    assert excinfo.value.context["reason"] == "invalid_response_shape"
    assert excinfo.value.context["got_type"] == "list"


def test_read_does_not_construct_client_per_call() -> None:
    """Persistent-client invariant — multiple reads share one ``AsyncClient`` instance.

    The whole point of injecting the client at construction is to share
    the keep-alive pool across calls. If a future refactor accidentally
    constructs a client per call, the latency budget collapses.
    """
    resp = _make_response(200, json_data={"time": "08:47"})
    client, http_client = _make_client(get_return_value=resp)

    asyncio.run(client.read(["time"]))
    asyncio.run(client.read(["time"]))
    asyncio.run(client.read(["time"]))

    # Three calls to the same client.get — no new client construction
    # in between (which would have shown up as separate AsyncClient
    # instances on the mock if the impl had bug here).
    assert http_client.get.call_count == 3


def test_read_logs_event_belief_read_on_success() -> None:
    """A successful read emits one INFO ``belief.read`` log with safe fields."""
    resp = _make_response(200, json_data={"time": "08:47"})
    client, _ = _make_client(get_return_value=resp)

    with structlog.testing.capture_logs() as captured:
        asyncio.run(client.read(["time"]))

    matching = [r for r in captured if r.get("event") == "belief.read"]
    assert matching, f"expected belief.read log; got: {captured!r}"
    rec = matching[0]
    assert rec.get("keys") == ["time"]
    assert rec.get("key_count") == 1
    assert "duration_ms" in rec


def test_read_logs_event_belief_read_failed_on_non_200() -> None:
    """A 5xx response emits a WARN ``belief.read_failed`` log; no INFO success log."""
    resp = _make_response(500, text="boom")
    client, _ = _make_client(get_return_value=resp)

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(OrchestratorError):
            asyncio.run(client.read(["time"]))

    failed = [r for r in captured if r.get("event") == "belief.read_failed"]
    success = [r for r in captured if r.get("event") == "belief.read"]
    assert failed, "expected belief.read_failed log on 5xx"
    assert not success, "must not emit success log when the call failed"
    assert failed[0].get("status_code") == 500


def test_read_does_not_log_response_body() -> None:
    """Privacy invariant (NFR25, FR39): response values never appear in log fields.

    Drives a happy-path read whose value contains a sentinel string;
    asserts no captured log record carries the sentinel. The redaction
    processor is the safety net but the code path itself must not pass
    values into log fields.
    """
    sentinel = "SECRET_BELIEF_VALUE_xyz123"
    resp = _make_response(200, json_data={"time": sentinel})
    client, _ = _make_client(get_return_value=resp)

    with structlog.testing.capture_logs() as captured:
        asyncio.run(client.read(["time"]))

    # Check every log record's full repr — values land nowhere.
    for record in captured:
        assert sentinel not in repr(record), f"belief value leaked into log: {record!r}"
