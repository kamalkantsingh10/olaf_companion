"""``OrchestratorClient`` Protocol + ``HttpOrchestratorClient`` impl (Story 4.2).

The orchestrator daemon (an external service, owned by a sibling project)
handles complex grounded turns over Server-Sent Events. v1 impl is
:class:`HttpOrchestratorClient`. The Protocol stays stable across that
implementation and any future replacement (e.g. a local-only mock for
offline dev).

Architectural invariants (architecture.md §"External Clients (Batch 4)"):

- **Persistent ``httpx.AsyncClient``.** Constructed by Story 4.1's
  ``async_http_client()`` factory in ``turn/beliefs.py`` and shared
  with :class:`HttpBeliefStateClient`. Same daemon origin → one
  keep-alive pool serves both consumers.
- **Forward-compat for new event types.** When the orchestrator
  ships a new SSE event ``type`` the pipeline doesn't know yet,
  log WARN + skip + keep consuming. Renaming or removing a type
  forces a ``schema_version`` bump (CLAUDE.md rule #6).
- **v1 fail-fast on broken contracts.** Framing errors, malformed
  JSON, or a known event type with bad fields → raise
  :class:`OrchestratorError`. Process crashes; systemd restarts.
- **Per-call timeout override.** SSE streams legitimately span
  tens of seconds (subagent runtime); we override the shared
  client's ``read=10`` to ``read=60`` per ``aconnect_sse`` call so
  a stall (no events for >60s) raises ``ReadTimeout`` and surfaces
  as ``OrchestratorError``.
- **Privacy (NFR25, FR39).** Per-event ``response_chunk.text`` is
  treated like a transcript — gated to DEBUG only. INFO logs carry
  event types + session_id + counts; never raw response content.

This file is one of two ``src/`` files allowed to import ``httpx`` /
``httpx_sse`` (architecture.md §"External adapter boundaries"). The
other is ``turn/beliefs.py`` (Story 4.1).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol, cast

import httpx
import structlog
from httpx_sse import ServerSentEvent, aconnect_sse
from pydantic import TypeAdapter, ValidationError

from voice_agent_pipeline.errors import OrchestratorError, StartupValidationError
from voice_agent_pipeline.schemas.stream import (
    NarrationEvent,
    OrchestratorStreamEvent,
    ResponseChunkEvent,
    SubagentDoneEvent,
    SubagentProgressEvent,
    SubagentStartedEvent,
    TurnEndEvent,
)

log = structlog.get_logger(__name__)

# Forward-compat invariant (architecture.md Batch 4):
# When the orchestrator project ships a new event type, this set updates
# *after* the new type is added to ``schemas/stream.py``'s union. Until
# then, the new type is logged-and-skipped. Renaming or removing an
# event type forces a ``schema_version`` bump (CLAUDE.md rule #6).
# Listing explicitly (rather than deriving from the union via
# ``get_args``) gives a grep-able cross-reference and keeps the keep-set
# pyright-friendly under strict mode.
_KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "narration",
        "subagent_started",
        "subagent_progress",
        "subagent_done",
        "response_chunk",
        "turn_end",
    }
)

# TypeAdapter is non-trivial to construct (pydantic builds the validator
# graph eagerly), so we cache one per module — Story 4.7's dispatcher
# imports + reuses on every event for every turn.
_event_adapter: TypeAdapter[OrchestratorStreamEvent] = TypeAdapter(OrchestratorStreamEvent)


class OrchestratorClient(Protocol):
    """Streaming SSE dispatch to the orchestrator daemon (v1: HttpOrchestratorClient, Story 4.2)."""

    def dispatch(self, transcript: str, session_id: str) -> AsyncIterator[OrchestratorStreamEvent]:
        """Open an SSE stream for a slow-path turn.

        Args:
            transcript: The user's spoken utterance, post-STT.
            session_id: Stable per-conversation ID so the orchestrator can
                stitch multi-turn context. The pipeline allocates this once
                per session (Story 4.7 wires the lifecycle plumbing).

        Yields:
            :data:`OrchestratorStreamEvent` instances — typed via the
            discriminated union in ``schemas/stream.py``. Stream terminates
            with a :class:`TurnEndEvent` (or, on missing-turn_end, the
            ``async for`` simply exits when the underlying SSE stream
            closes — Story 4.7's dispatcher detects this).
        """
        ...

    async def cancel(self, session_id: str) -> None:
        """Abort an in-flight turn.

        Stubbed in Epic 4 (raises :class:`NotImplementedError`); the
        ``HTTP DELETE /turn/{session_id}`` wiring lands in v1.5
        Story v1.5-1 (barge-in).

        Args:
            session_id: The session whose stream should be cancelled.
        """
        ...


class HttpOrchestratorClient:
    """SSE consumer for the orchestrator daemon's slow-path ``POST /turn``.

    Reuses the persistent :class:`httpx.AsyncClient` constructed via
    :func:`turn.beliefs.async_http_client` (Story 4.1). The orchestrator
    daemon and the belief-state endpoint live behind one origin, so a
    single keep-alive pool is the architecturally correct shape
    (architecture.md §"Connection management").

    Forward-compat: unknown event ``type`` values log WARN + continue
    (Batch 4 decision); framing / JSON / known-type-bad-shape raise
    :class:`OrchestratorError` and crash (v1 fail-fast — CLAUDE.md
    rule #4).
    """

    def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
        """Build the client.

        Args:
            http_client: The persistent :class:`httpx.AsyncClient` from
                ``async_http_client()`` (Story 4.1). Lifecycle is owned
                by ``pipeline.py:run_pipeline``.
            base_url: Daemon base URL (e.g. ``"http://localhost:8001"``).
                Trailing slash is stripped defensively even though the
                config validator already does it.
        """
        self._client = http_client
        self._base_url = base_url.rstrip("/")

    async def dispatch(
        self, transcript: str, session_id: str
    ) -> AsyncIterator[OrchestratorStreamEvent]:
        """Open ``POST /turn`` as an SSE stream and yield typed events.

        The implementation is an async generator: events are yielded
        as the SSE stream produces them, never buffered. Story 4.7's
        dispatcher consumes this and pipes ``narration`` /
        ``response_chunk`` text downstream as ``TalkerResponseFrame``
        instances per arrival.

        Per-call timeout override: ``read=60.0`` (vs the shared
        client's ``read=10.0`` for one-shot GETs) because a slow turn
        legitimately spans tens of seconds while a subagent runs. A
        60s gap with no events triggers ``httpx.ReadTimeout`` →
        ``OrchestratorError`` (stall detection).

        Forward-compat (architecture.md Batch 4):

        - Unknown event ``type`` → log WARN, skip, keep iterating.
        - Known event ``type`` with malformed payload → raise
          :class:`OrchestratorError(reason="invalid_event_shape")`.
        - Malformed JSON in ``sse.data`` → raise
          :class:`OrchestratorError(reason="invalid_json")`.
        - Any ``httpx.HTTPError`` (connection / timeout / framing) →
          raise :class:`OrchestratorError(reason=<exc class>)`.

        Privacy (NFR25, FR39): per-event ``narration.text`` and
        ``response_chunk.text`` are NOT logged at any level by this
        method. Story 4.7's dispatcher logs only event-type counts at
        INFO; raw text stays at DEBUG via the ``orchestrator.event_received``
        log (caller's concern).
        """
        url = f"{self._base_url}/turn"
        body = {"transcript": transcript, "session_id": session_id}
        # Per-call read timeout extension for streaming — see method
        # docstring rationale.
        timeout = httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0)
        # Privacy: log transcript_length, NEVER the transcript text.
        log.info(
            "orchestrator.dispatch_started",
            session_id=session_id,
            transcript_length=len(transcript),
            url=url,
        )
        start = time.monotonic()
        event_count = 0
        try:
            async with aconnect_sse(
                self._client,
                "POST",
                url,
                json=body,
                headers={"Accept": "text/event-stream"},
                timeout=timeout,
            ) as event_source:
                async for sse in event_source.aiter_sse():
                    parsed = self._parse_or_warn(sse, session_id)
                    if parsed is None:
                        # Forward-compat: unknown event type, already
                        # logged as WARN by ``_parse_or_warn``. Continue
                        # consuming the stream.
                        continue
                    event_count += 1
                    yield parsed
        except httpx.HTTPError as exc:
            duration_ms = (time.monotonic() - start) * 1000
            reason = type(exc).__name__
            log.warning(
                "orchestrator.dispatch_failed",
                session_id=session_id,
                reason=reason,
                url=url,
                duration_ms=duration_ms,
            )
            raise OrchestratorError(reason=reason, url=url) from exc

        duration_ms = (time.monotonic() - start) * 1000
        log.info(
            "orchestrator.dispatch_completed",
            session_id=session_id,
            event_count=event_count,
            duration_ms=duration_ms,
        )

    def _parse_or_warn(
        self, sse: ServerSentEvent, session_id: str
    ) -> OrchestratorStreamEvent | None:
        """Parse one SSE event; ``None`` means "unknown type, skip".

        The discriminator-peek pattern lets us distinguish
        forward-compat (unknown type, log + skip) from broken
        contract (known type, bad fields → raise). A single
        try/except around ``TypeAdapter.validate_python`` would
        conflate the two.
        """
        try:
            raw: Any = json.loads(sse.data)
        except json.JSONDecodeError as exc:
            raise OrchestratorError(
                reason="invalid_json",
                session_id=session_id,
                # raw_length only — never the malformed payload itself
                # (keeps the privacy invariant intact for any text the
                # daemon may have included pre-corruption).
                raw_length=len(sse.data),
            ) from exc

        if not isinstance(raw, dict):
            # The contract is "JSON object per SSE event". A list /
            # scalar / null is contract drift, not forward-compat.
            raise OrchestratorError(
                reason="invalid_event_shape",
                session_id=session_id,
                got_type=type(raw).__name__,
            )
        # JSON object keys are always strings per the spec; cast tells
        # pyright the runtime check above is sufficient.
        raw_dict = cast(dict[str, Any], raw)
        type_value = raw_dict.get("type")
        if not isinstance(type_value, str) or type_value not in _KNOWN_EVENT_TYPES:
            # Forward-compat: orchestrator added a new event type the
            # pipeline doesn't yet know. WARN + skip; do NOT raise.
            log.warning(
                "orchestrator.unknown_event_type",
                type=type_value,
                session_id=session_id,
            )
            return None

        try:
            return _event_adapter.validate_python(raw_dict)
        except ValidationError as exc:
            # Known type, broken fields. Cap pydantic errors repr to 3
            # entries to avoid exception bloat.
            raise OrchestratorError(
                reason="invalid_event_shape",
                type=type_value,
                session_id=session_id,
                errors=str(exc.errors()[:3]),
            ) from exc

    async def probe_health(self) -> None:
        """Startup probe — refuse to start unless ``GET /health`` returns 200.

        Closes architecture.md §"Cross-project integration" spec-drift:
        the orchestrator daemon must expose ``GET /health``. Wired into
        ``pipeline.py:run_pipeline`` after ``event_publisher.connect()``.

        Raises:
            StartupValidationError: On non-200 response or transport
                error. Distinct error class from in-flight failures
                (``OrchestratorError``) — startup probes can't be
                "recovered from" in v1; the pipeline never finished
                initializing, so ``__main__``'s top-level handler logs
                the failure and exits non-zero.
        """
        url = f"{self._base_url}/health"
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise StartupValidationError(
                stage="orchestrator",
                reason=type(exc).__name__,
                url=url,
            ) from exc
        if resp.status_code != 200:
            raise StartupValidationError(
                stage="orchestrator",
                reason="orchestrator_health_non_200",
                status_code=resp.status_code,
                url=str(resp.request.url),
            )

    async def cancel(self, session_id: str) -> None:
        """Stubbed in Epic 4; v1.5 Story v1.5-1 (barge-in) wires the impl.

        The Protocol's ``cancel(session_id)`` method exists so callers
        can declare the seam at type-check time. Invoking it in v1
        fails loudly (rather than silently no-op'ing) because in-flight
        cancellation is a barge-in concern; until barge-in lands,
        in-flight orchestrator turns complete naturally on
        :class:`TurnEndEvent`.
        """
        # session_id intentionally unused — the v1.5 impl will use it
        # to issue ``HTTP DELETE /turn/{session_id}``. Reference for
        # ruff so the unused-arg lint stays quiet.
        del session_id
        raise NotImplementedError(
            "Cancel is wired in v1.5 Story v1.5-1 (barge-in). Until then, "
            "in-flight orchestrator turns complete naturally on TurnEndEvent."
        )


# Re-export the schema event types from this module for caller ergonomics
# — Story 4.7's dispatcher imports these via
# ``from voice_agent_pipeline.turn.orchestrator import NarrationEvent, ...``
# alongside the client class. The actual definitions live in
# ``schemas/stream.py``.
__all__ = [
    "HttpOrchestratorClient",
    "NarrationEvent",
    "OrchestratorClient",
    "OrchestratorStreamEvent",
    "ResponseChunkEvent",
    "SubagentDoneEvent",
    "SubagentProgressEvent",
    "SubagentStartedEvent",
    "TurnEndEvent",
]
