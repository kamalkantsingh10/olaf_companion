"""``BeliefStateClient`` Protocol + ``HttpBeliefStateClient`` impl (Story 4.1).

The pipeline asks the orchestrator daemon's belief-state service for a
focused subset of context **at the start of each turn**, rather than
holding cached state. This keeps the pipeline stateless across turns
and lets the belief service evolve independently. v1 impl is
:class:`HttpBeliefStateClient` (Story 4.1).

Architectural invariants (architecture.md §"External Clients (Batch 4)"):

- **No cache.** Every :meth:`HttpBeliefStateClient.read` call issues a
  fresh HTTP GET. Caching would force every Story 5.x state mutation
  (mood transitions, FSM transitions, tool dispatches) to invalidate
  it, which is more complex than re-fetching ~10ms of localhost HTTP
  per turn. If a future soak shows the per-turn read is a hot path,
  revisit; until then, fresh on every turn.
- **Persistent ``httpx.AsyncClient``.** The client is constructed once
  in ``pipeline.py:run_pipeline`` and shared with Story 4.2's
  ``HttpOrchestratorClient`` (same origin → same connection pool).
  Lifecycle is bound to the pipeline via ``async with``. We never
  construct a client per call.
- **v1 fail-fast.** Non-200 responses, transport errors, and JSON
  decode failures all raise :class:`OrchestratorError`. CLAUDE.md
  rule #4 forbids catching :class:`ExternalServiceError` (parent of
  :class:`OrchestratorError`) downstream — process crashes, systemd
  restarts. The resilience layer is a v2 deferral.
- **Privacy (NFR25, FR39).** Logs carry only the requested keys, key
  count, status code, and duration_ms. Response body content is
  **never** logged at any level — belief values may include user-
  state (calendar entries, locations, etc.). The error context
  truncates response body to 200 chars for postmortem-debugging
  without exfiltrating values.

This file is one of two ``src/`` files allowed to import ``httpx``
(architecture.md §"External adapter boundaries"). The other is
``turn/orchestrator.py`` (Story 4.2).
"""

import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast

import httpx
import structlog

from voice_agent_pipeline.errors import OrchestratorError

log = structlog.get_logger(__name__)


@asynccontextmanager
async def async_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Open the pipeline's persistent ``httpx.AsyncClient`` (Story 4.1).

    Lives in ``turn/beliefs.py`` (with ``turn/orchestrator.py``) to honor
    the architecture invariant: only these two files may import
    ``httpx`` (architecture.md §"External adapter boundaries"). The
    pipeline-assembly site (``pipeline.py:run_pipeline``) consumes this
    factory via ``async with async_http_client() as client:`` — no
    ``import httpx`` outside this module.

    Timeouts:

    - ``connect=5.0`` — bounded so a stuck daemon triggers a clean
      systemd restart instead of hanging the event loop.
    - ``read=10.0`` — enough for a healthy localhost ``GET /beliefs``;
      Story 4.2's SSE consumer overrides per-call to ``read=60.0`` for
      streaming endpoints.
    - ``write=5.0``, ``pool=5.0`` — defensive bounds on body upload
      and pool acquisition.
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
    ) as client:
        yield client


class BeliefStateClient(Protocol):
    """Per-turn fresh belief-state read. v1 impl is HttpBeliefStateClient (Story 4.1)."""

    async def read(self, keys: list[str]) -> dict[str, Any]:
        """Fetch a focused subset of belief-state values by key.

        Args:
            keys: Keys to retrieve. The set of allowed keys is configured
                in ``setup.toml`` (``[talker].grounded_keys``) — Story 4.1
                wires the validation. Unknown keys may either return
                ``None`` or raise, depending on the impl.

        Returns:
            Mapping of key → value. Values are JSON-deserializable (the
            belief service returns JSON), so :data:`Any` is the honest
            type — pinning shapes per key would need a schema overhaul.
        """
        ...


class HttpBeliefStateClient:
    """HTTP impl of :class:`BeliefStateClient` against the orchestrator daemon.

    Constructor takes a pre-built :class:`httpx.AsyncClient` (lifecycle
    owned by ``pipeline.py:run_pipeline``) plus the daemon's base URL.
    No per-call client construction — the pool's keep-alive is the
    whole point of injection (architecture.md §"Connection management").

    No state across calls: every :meth:`read` is independent. Multiple
    concurrent reads against the same instance are safe (httpx's async
    client is reentrant).
    """

    def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
        """Build the client.

        Args:
            http_client: A persistent :class:`httpx.AsyncClient` whose
                lifecycle outlives this instance — typically the one
                constructed in ``pipeline.py:run_pipeline`` and shared
                with :class:`HttpOrchestratorClient` (Story 4.2).
            base_url: Daemon base URL (e.g. ``"http://localhost:8001"``).
                Trailing slash is stripped defensively even though the
                config validator already does it.
        """
        self._client = http_client
        self._base_url = base_url.rstrip("/")

    async def read(self, keys: list[str]) -> dict[str, Any]:
        """Fetch belief-state values by key (FR10).

        v1 fail-fast contract:

        - Non-200 response → :class:`OrchestratorError` with status_code,
          url, and (truncated) body context. **Note**: ``body`` is
          truncated to 200 chars to bound exfiltration risk on
          accidental logging of the exception's ``str()``.
        - Transport error (connection refused, timeout, DNS failure)
          → :class:`OrchestratorError` from the original exception.
        - JSON decode failure → :class:`OrchestratorError` with
          ``reason="invalid_json"``.
        - Non-dict JSON response (e.g. JSON list) →
          :class:`OrchestratorError` with ``reason="invalid_response_shape"``.
          The daemon contract is "JSON object"; a list or scalar is
          contract drift.

        Empty ``keys`` list still issues the request — the daemon owns
        the "what to return for no keys" contract; we don't second-
        guess at the client.
        """
        url = f"{self._base_url}/beliefs"
        # Comma-joined form per architecture's URL convention (one
        # ``?keys=time,calendar_today``, not the repeated-param
        # ``?keys=time&keys=calendar_today`` form).
        params = {"keys": ",".join(keys)}
        start = time.monotonic()

        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            duration_ms = (time.monotonic() - start) * 1000
            reason = type(exc).__name__
            log.warning(
                "belief.read_failed",
                keys=keys,
                key_count=len(keys),
                reason=reason,
                duration_ms=duration_ms,
            )
            raise OrchestratorError(reason=reason, url=url) from exc

        if resp.status_code != 200:
            duration_ms = (time.monotonic() - start) * 1000
            log.warning(
                "belief.read_failed",
                keys=keys,
                key_count=len(keys),
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )
            raise OrchestratorError(
                status_code=resp.status_code,
                url=str(resp.request.url),
                # Truncated to bound exfil risk; full body never logged.
                body=resp.text[:200],
            )

        try:
            parsed: Any = resp.json()
        except json.JSONDecodeError as exc:
            duration_ms = (time.monotonic() - start) * 1000
            log.warning(
                "belief.read_failed",
                keys=keys,
                key_count=len(keys),
                reason="invalid_json",
                duration_ms=duration_ms,
            )
            raise OrchestratorError(
                reason="invalid_json",
                url=str(resp.request.url),
                body=resp.text[:200],
            ) from exc

        if not isinstance(parsed, dict):
            duration_ms = (time.monotonic() - start) * 1000
            got_type = type(parsed).__name__
            log.warning(
                "belief.read_failed",
                keys=keys,
                key_count=len(keys),
                reason="invalid_response_shape",
                got_type=got_type,
                duration_ms=duration_ms,
            )
            raise OrchestratorError(
                reason="invalid_response_shape",
                got_type=got_type,
                url=str(resp.request.url),
            )

        # Pyright narrows ``parsed`` to ``dict[Unknown, Unknown]`` after the
        # ``isinstance(parsed, dict)`` check — the original was typed Any so
        # the key/value types are erased. The Protocol's contract is
        # ``dict[str, Any]``; cast tells pyright the runtime check above is
        # sufficient. JSON object keys are always strings per the spec, so
        # the cast is sound.
        result = cast(dict[str, Any], parsed)

        duration_ms = (time.monotonic() - start) * 1000
        # Privacy invariant (NFR25, FR39): we log keys + counts +
        # duration. Values from the parsed dict are NEVER logged —
        # they may carry user-state (calendar entries, locations, etc.).
        log.info(
            "belief.read",
            keys=keys,
            key_count=len(keys),
            duration_ms=duration_ms,
        )
        return result
