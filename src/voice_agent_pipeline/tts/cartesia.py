"""CartesiaClient — Sonic-3 streaming TTS implementation of TTSClient (Story 2.3 / 6.1).

This module is the **single import boundary** for the ``cartesia`` SDK
(architecture.md §"Architectural Boundaries"). Other modules speak
through the :class:`TTSClient` Protocol from ``tts/client.py``.

TLS posture (NFR24): the Cartesia SDK uses ``httpx`` internally with
cert validation on by default, and the WebSocket transport rides on
``websockets`` (also TLS-validating by default). v1 deliberately
exposes no knob to disable validation — a future contributor adding
such a knob would be introducing a security regression. The
:class:`CartesiaClient` constructor passes only the api_key to
``cartesia.AsyncCartesia``; no ``verify=False`` / ``base_url`` override
/ cert path overrides land here.

Streaming contract (FR15 / FR59): :meth:`CartesiaClient.synthesize` is
an async generator that yields raw S16LE PCM bytes at 16 kHz mono —
same format the ``LocalAudioTransport`` output stage (Story 2.1)
consumes, so no resampler runs in the hot path. Each chunk is yielded
as soon as the SDK delivers it; the generator does NOT buffer the full
stream before yielding (real-time NFR4 contract).

v1 fail-fast: any ``cartesia.APIError`` raised mid-stream (SSE path)
or ``websockets.exceptions.ConnectionClosedError`` mid-stream (WS path)
propagates as :class:`CartesiaError` (a subclass of
:class:`ExternalServiceError`). CLAUDE.md rule #4 forbids catching
:class:`CartesiaError` anywhere downstream — process crashes, systemd
restarts (Epic 5).

Story 6.1 — WebSocket transport + word-timestamps capture
---------------------------------------------------------

The transport defaults to ``"websocket"``; the legacy SSE path is
retained as an opt-in fallback (``[tts] transport = "sse"``) for the
implementation window. The WS path is the only path that captures
per-segment word timing — Cartesia's SSE wire format in v1's
configuration does not emit ``timestamps`` events, so SSE callers see
:meth:`last_segment_timing` return ``None``.

Per-call lifecycle for :attr:`_last_segment_timing` (WS path only):

1. :meth:`_synthesize_websocket` resets ``self._last_segment_timing =
   None`` at the **start** of the call (before opening the WS) so a
   caller never observes stale timing from a prior request.
2. Each incoming ``Timestamps`` event accumulates its parallel
   ``words / start / end`` lists into the live ``SegmentTiming``
   buffer (seconds → integer milliseconds, rounded).
3. On ``Done`` the generator exits cleanly; the connection's
   async-context-manager ``__aexit__`` closes the websocket.
4. After the caller drains the generator, :meth:`last_segment_timing`
   returns the populated ``SegmentTiming | None`` for Story 6.3's
   emphasis-vocalization join to consume via the concrete
   :class:`CartesiaClient` reference.
"""

import base64
import time
import uuid
from collections.abc import AsyncIterator

import cartesia
import structlog
import websockets.exceptions as websockets_exc
from cartesia.types.generation_request import GenerationRequest
from cartesia.types.websocket_response import TimestampsWordTimestamps
from pydantic import BaseModel, ConfigDict, SecretStr

from voice_agent_pipeline.config.setup import SetupConfig, TtsConfig
from voice_agent_pipeline.errors import CartesiaError, StartupValidationError

log = structlog.get_logger(__name__)

# 16 kHz mono S16LE — same format the rest of the pipeline pins.
# RawEncoding values supported by Cartesia 3.0.2:
# 'pcm_f32le' | 'pcm_s16le' | 'pcm_mulaw' | 'pcm_alaw'.
# Sample rate values: 8000 | 16000 | 22050 | 24000 | 44100 | 48000.
_OUTPUT_FORMAT: dict[str, object] = {
    "container": "raw",
    "encoding": "pcm_s16le",
    "sample_rate": 16000,
}


# ---------------------------------------------------------------------------
# Story 6.1 — per-segment word timing (consumed by Story 6.3's emphasis join)
# ---------------------------------------------------------------------------


class Word(BaseModel):
    """One Cartesia-reported word inside a synthesized segment (Story 6.1).

    Holds the word's text plus its [start_ms, end_ms] interval relative
    to the start of the segment. The pipeline pins integer milliseconds
    everywhere it talks about audio timing (matches ``tts.first_frame.
    ttfb_ms`` shape + the ``audio_frame_id`` semantics future stories
    use for splitter alignment), so the Cartesia-reported float seconds
    are converted on capture.

    Frozen + ``extra="forbid"`` per CLAUDE.md rule 3 (pydantic at
    boundaries; no mutation after construction; typos in mock fixtures
    fail loudly).

    Attributes:
        text: The word as Cartesia reports it. Whitespace + punctuation
            handling matches the SDK's tokenisation; the consumer (Story
            6.3) joins per-word entries against the LLM's emphasis index
            using simple textual alignment.
        start_ms: Start offset within the segment, integer milliseconds,
            rounded from Cartesia's float-seconds value.
        end_ms: End offset within the segment, integer milliseconds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    start_ms: int
    end_ms: int


class SegmentTiming(BaseModel):
    """Per-call accumulated word timing for one Cartesia synthesize() request.

    A single :meth:`CartesiaClient.synthesize` call corresponds to one
    synthesized segment; Cartesia typically emits a single
    ``Timestamps`` event covering the whole segment, but the wire spec
    allows incremental emission. The WS path accumulates every word
    across every ``Timestamps`` event within the same request into a
    single :class:`SegmentTiming`.

    Frozen + ``extra="forbid"`` — once exposed via
    :meth:`CartesiaClient.last_segment_timing` the model is read-only;
    callers consume the parallel ``Word`` list and don't mutate it.

    Attributes:
        words: Ordered list of :class:`Word` entries — order matches
            Cartesia's emission order, which matches the spoken order
            of the transcript.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    words: list[Word]


def _word_timestamps_to_words(payload: TimestampsWordTimestamps) -> list[Word]:
    """Project Cartesia's parallel-list shape onto a :class:`Word` list.

    Cartesia emits a ``TimestampsWordTimestamps`` with three parallel
    lists (``words``, ``start``, ``end``); same-index entries belong
    together. The pipeline prefers a per-word struct, and pins integer
    ms throughout (architecture.md §"Audio timing"), so this helper
    handles both the projection and the seconds→ms rounding in one
    pass.

    Rounding policy: ``round(...)`` (banker's rounding) on the
    float-seconds value times 1000. The ~1ms resolution is well within
    Cartesia's reported precision; the choice of rounding over
    truncation prevents systematic skew when many word-end times land
    near 0.5 ms.

    Args:
        payload: The ``Timestamps.word_timestamps`` sub-model from one
            Cartesia WS ``Timestamps`` event.

    Returns:
        Ordered list of :class:`Word` entries — empty if any of the
        parallel lists is empty.
    """
    # Defensive: the parallel-list invariant is upheld by Cartesia, but
    # zipping length-mismatched lists silently truncates which would
    # produce subtle bugs. min(len(...)) makes the truncation explicit.
    n = min(len(payload.words), len(payload.start), len(payload.end))
    return [
        Word(
            text=payload.words[i],
            start_ms=round(payload.start[i] * 1000),
            end_ms=round(payload.end[i] * 1000),
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# CartesiaClient — Protocol implementation (TTSClient)
# ---------------------------------------------------------------------------


class CartesiaClient:
    """Streaming TTS via Cartesia Sonic-3 — implements :class:`TTSClient`.

    Yields raw S16LE PCM bytes at 16 kHz mono. The :meth:`synthesize`
    entry dispatches on :attr:`TtsConfig.transport` to either the
    Story-6.1 WebSocket path (default — captures word timing) or the
    legacy v1 SSE path (fallback for the implementation window;
    timestamps dropped).

    Story 6.1 added a per-instance :attr:`_last_segment_timing` buffer
    populated by the WS path on each call. Story 6.3 consumes it via
    :meth:`last_segment_timing` after the synthesize generator
    exhausts; the SSE path leaves the buffer as ``None`` (since the
    SSE event stream doesn't carry timestamps in v1's configuration).
    """

    def __init__(self, config: TtsConfig, api_key: SecretStr) -> None:
        """Build the Cartesia client; voice + model + transport live on the config.

        Args:
            config: Validated :class:`TtsConfig` carrying voice_id,
                default_emotion, model identifier, and (Story 6.1) the
                ``transport`` knob.
            api_key: Cartesia API key, wrapped in :class:`SecretStr` so
                ``repr(self)`` doesn't leak it.
        """
        self._config = config
        # AsyncCartesia maintains its own httpx connection pool + a
        # websockets pool for the WS transport; we construct one per
        # CartesiaClient (lifetime-bound to the pipeline) so connection
        # reuse cuts TLS handshake cost from every turn's TTFB. No
        # verify=False or cert override — TLS validation is locked on
        # per NFR24.
        self._client = cartesia.AsyncCartesia(api_key=api_key.get_secret_value())
        # Story 6.1: per-call WS path populates this on every Timestamps
        # event; SSE path leaves it None. Reset to None at the start of
        # each WS call so a downstream consumer never observes stale
        # timing from a prior request.
        self._last_segment_timing: SegmentTiming | None = None

    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream synthesized audio chunks for ``text``.

        Dispatches on :attr:`TtsConfig.transport`:

        - ``"websocket"`` (default, Story 6.1) → :meth:`_synthesize_websocket`.
            Opens a Cartesia WS, captures word-level ``timestamps``
            events into :attr:`_last_segment_timing`, yields chunk
            bytes as they arrive.
        - ``"sse"`` (legacy fallback) → :meth:`_synthesize_sse`. Same
            behaviour as the v1 SSE path; word timestamps NOT captured.

        Args:
            text: The text to synthesize. v1 is plain text only; Story
                3.x will pass Cartesia inline emotion tags (parsed +
                stripped earlier in the pipeline by the streaming SSML
                splitter, but the text param here may still carry tag
                remnants in some edge cases — Cartesia treats unknown
                tags as text).

        Returns:
            Async iterator yielding raw S16LE PCM bytes at 16 kHz mono.
            Frame size is the SDK's choice (typically a few hundred
            bytes per chunk). The :class:`TTSClient` Protocol contract
            (`synthesize(text) -> AsyncIterator[bytes]`) is unchanged
            from v1.

        Raises:
            CartesiaError: On any ``cartesia.APIError`` subclass during
                stream open or mid-stream (either transport), and on
                any ``websockets.exceptions.ConnectionClosedError`` /
                ``Error`` event on the WS transport. Cause chain
                preserved via ``raise ... from e``. v1 fail-fast —
                never caught downstream (CLAUDE.md rule #4).
        """
        if self._config.transport == "websocket":
            return self._synthesize_websocket(text)
        return self._synthesize_sse(text)

    def last_segment_timing(self) -> SegmentTiming | None:
        """Return the most recently captured per-segment word timing (Story 6.1).

        Populated by the WS path on every call; the SSE path leaves it
        ``None``. Callers read this AFTER draining the iterator from
        :meth:`synthesize` (the WS path accumulates word timestamps as
        events arrive; the buffer is finalized when the generator
        exhausts on the ``Done`` event).

        The SSE path does NOT populate this because Cartesia's SSE
        wire format in v1's configuration does not emit ``timestamps``
        events; operators wanting word timing must use the default
        WebSocket transport.

        Returns:
            A :class:`SegmentTiming` if the most recent call was on the
            WS path AND at least one ``Timestamps`` event arrived,
            otherwise ``None``. A fresh :class:`CartesiaClient` (before
            any :meth:`synthesize` call) also returns ``None``.
        """
        return self._last_segment_timing

    # -----------------------------------------------------------------
    # Story 6.1 — WebSocket transport (default)
    # -----------------------------------------------------------------

    async def _synthesize_websocket(self, text: str) -> AsyncIterator[bytes]:
        """WebSocket transport: open WS, yield chunks, capture timestamps.

        Wire shape (verified against ``cartesia==3.0.2``):

        1. ``async with self._client.tts.websocket_connect() as conn:``
           — the SDK returns an ``AsyncTTSResourceConnectionManager``
           whose ``__aenter__`` opens a websocket and yields an
           ``AsyncTTSResourceConnection``. The async-context-manager
           pattern guarantees ``__aexit__`` closes the connection on
           every exit path (clean ``Done``, ``Error`` event, mid-stream
           exception). Without explicit close the connection lingers
           and the event loop hangs at pipeline shutdown.
        2. ``await conn.send(GenerationRequest(...))`` — single send
           per call (no flush / no continue). ``add_timestamps=True``
           is the Story 6.1 add; ``add_phoneme_timestamps=False`` is
           hard-coded off (no v1 consumer for phonemes, and they add
           non-trivial wire bandwidth).
        3. ``async for event in conn:`` — the connection's
           ``__aiter__`` yields ``WebsocketResponse`` union members
           until either ``Done`` (clean close) or the connection
           closes from the server side. Dispatch on ``event.type``.

        Per-call lifecycle for :attr:`_last_segment_timing`:

        - Reset to ``None`` at function entry — even if the WS open
          fails, the caller sees no stale timing from a prior request.
        - Populated on every ``Timestamps`` event; the parallel-list
          ``word_timestamps`` payload is projected onto a
          :class:`Word` list and accumulated.

        Error handling:

        - ``cartesia.APIError`` (server-side error during open or any
          response phase) → :class:`CartesiaError`.
        - ``websockets.exceptions.ConnectionClosedError`` (transport
          ripped from under us mid-stream) → :class:`CartesiaError`.
        - WS ``Error`` event (server reports an error mid-stream
          without closing) → :class:`CartesiaError`.

        Args:
            text: The text to synthesize. See :meth:`synthesize`.

        Yields:
            Raw S16LE PCM bytes from each ``Chunk`` event (the SDK's
            ``Chunk.audio`` property auto-decodes the base64 ``data``
            field, returning ``None`` if the chunk is empty — empties
            are skipped).

        Raises:
            CartesiaError: See above.
        """
        # Story 6.1: reset before opening the WS so a failed open still
        # leaves the buffer in a defined state (``None``).
        self._last_segment_timing = None
        # Local accumulator for words across (potentially many)
        # Timestamps events within this single request. Materialised
        # into the immutable SegmentTiming when at least one event
        # arrives.
        accumulated_words: list[Word] = []

        request_start_ns = time.time_ns()
        first_frame_logged = False
        try:
            async with self._client.tts.websocket_connect() as conn:
                # GenerationRequest carries the same model/voice/format
                # fields the SSE path passes positionally to
                # generate_sse(...). model_id is the field's serialised
                # alias (the SDK declares it as
                # ``llm_model_id = FieldInfo(alias="model_id")``);
                # constructing via the alias keeps the wire shape
                # identical across transports.
                #
                # context_id is required on the WS path — Cartesia's
                # WS protocol uses it to multiplex multiple in-flight
                # generations on a single connection. We open one WS
                # per call (no multiplexing in v1), so a fresh UUID
                # hex per call is enough. Cartesia's validator only
                # allows alphanumeric + underscore + hyphen; a
                # bare uuid4().hex satisfies this without dashes.
                context_id = uuid.uuid4().hex
                request = GenerationRequest(
                    model_id=self._config.model,
                    output_format=_OUTPUT_FORMAT,  # type: ignore[arg-type]
                    transcript=text,
                    voice={"id": self._config.voice_id, "mode": "id"},  # type: ignore[arg-type]
                    generation_config={  # type: ignore[arg-type]
                        "emotion": self._config.default_emotion,
                        "speed": self._config.speed,
                    },
                    context_id=context_id,
                    # Story 6.1 — capture per-word timestamps for the
                    # downstream emphasis join (Story 6.3).
                    add_timestamps=True,
                    # Story 6.1 — explicitly OFF; no v1 consumer for
                    # phonemes (lip-sync is a separate future project).
                    add_phoneme_timestamps=False,
                )
                await conn.send(request)

                async for event in conn:
                    # Dispatch on the discriminator-typed event union.
                    # The SDK's WebsocketResponse is a tagged union
                    # over Literal[type] members — pyright narrows the
                    # event type per branch via the ``event.type``
                    # check.
                    if event.type == "chunk":
                        # Chunk.audio is a property that auto-decodes
                        # the base64 ``data`` field. Returns None on
                        # an empty data string; skip those rather than
                        # yielding ``b""`` so downstream consumers
                        # don't see spurious empty frames.
                        audio = event.audio
                        if audio is None:
                            continue
                        if not first_frame_logged:
                            # NFR4 baseline metric: request-sent →
                            # first audio byte. Same single-clock
                            # methodology + log shape the SSE path
                            # used so the time-series stays comparable
                            # across the transport swap (DR-001
                            # §"Empirical evidence" mines this log).
                            ttfb_ms = (time.time_ns() - request_start_ns) // 1_000_000
                            log.info(
                                "tts.first_frame",
                                ttfb_ms=ttfb_ms,
                                voice_id=self._config.voice_id,
                                model=self._config.model,
                                transport="websocket",
                            )
                            first_frame_logged = True
                        yield audio
                    elif event.type == "timestamps":
                        # word_timestamps is Optional[...]; skip events
                        # whose payload is absent (defensive — should
                        # not happen with add_timestamps=True, but the
                        # SDK's type allows it).
                        if event.word_timestamps is None:
                            continue
                        accumulated_words.extend(
                            _word_timestamps_to_words(event.word_timestamps),
                        )
                    elif event.type == "done":
                        # Clean stream end. ``async with`` cleans up
                        # the connection on exit.
                        break
                    elif event.type == "error":
                        # Server-side error event mid-stream. Wrap as
                        # CartesiaError so CLAUDE.md rule #4's
                        # never-caught contract applies uniformly.
                        # The SDK's typed Error model declares
                        # ``error: str`` but in practice some error
                        # responses populate only ``message`` / ``title``
                        # (e.g., 400 validation errors). Fall back
                        # through the alternative fields so the
                        # operator sees the actual reason, not "None".
                        reason = (
                            event.error
                            or getattr(event, "message", None)
                            or getattr(event, "title", None)
                            or f"status_code={getattr(event, 'status_code', '?')}"
                        )
                        raise CartesiaError(
                            voice_id=self._config.voice_id,
                            model=self._config.model,
                            reason=reason,
                        )
                    # phoneme_timestamps / flush_done are unused in v1's
                    # WS path; silently drop (we never asked for either).
        except cartesia.APIError as e:
            # v1 fail-fast: wrap and propagate. CLAUDE.md rule #4 —
            # never caught downstream. Process crashes; systemd restarts.
            raise CartesiaError(
                voice_id=self._config.voice_id,
                model=self._config.model,
                reason=str(e),
            ) from e
        except websockets_exc.ConnectionClosedError as e:
            # Transport-level abort (e.g., Cartesia closed the
            # connection mid-stream without sending ``Done``). Treat as
            # a Cartesia failure — the wrap-and-propagate posture
            # matches the APIError branch above.
            raise CartesiaError(
                voice_id=self._config.voice_id,
                model=self._config.model,
                reason=f"websocket closed: {e}",
            ) from e

        # Finalise the accumulated word list into the immutable
        # SegmentTiming buffer, but only if we actually captured words.
        # Otherwise leave the buffer as None (set at function entry).
        if accumulated_words:
            self._last_segment_timing = SegmentTiming(words=accumulated_words)

    # -----------------------------------------------------------------
    # SSE transport (legacy v1 fallback — retained for implementation window)
    # -----------------------------------------------------------------

    async def _synthesize_sse(self, text: str) -> AsyncIterator[bytes]:
        """SSE transport: the original v1 path, lifted into its own method.

        Identical behaviour to the pre-Story-6.1 ``synthesize()`` —
        opens an SSE stream via ``tts.generate_sse(...)``, yields raw
        S16LE PCM from each chunk event, drops non-chunk events
        silently. Does NOT capture word timestamps (SSE wire format
        in v1's configuration doesn't emit them), so callers reading
        :meth:`last_segment_timing` after an SSE call see ``None``.

        This branch is retained only for the v2 implementation window
        so a quick flip back to SSE is possible if the WS path
        misbehaves during Story 6.4's soak. A follow-up story removes
        the SSE branch + the ``transport`` knob once WS is settled.

        Args:
            text: See :meth:`synthesize`.

        Yields:
            Raw S16LE PCM bytes at 16 kHz mono.

        Raises:
            CartesiaError: On any ``cartesia.APIError`` subclass during
                stream open or mid-stream. v1 fail-fast.
        """
        request_start_ns = time.time_ns()
        first_frame_logged = False
        try:
            # ``tts.generate_sse()`` is Cartesia's pre-WS streaming
            # entry point — returns an ``AsyncSSEEventStream`` of
            # events with ``.type`` ("chunk", "timestamps", etc.) and
            # ``.data`` (base64-encoded bytes for chunk events).
            stream = await self._client.tts.generate_sse(
                model_id=self._config.model,
                transcript=text,
                voice={"id": self._config.voice_id, "mode": "id"},
                output_format=_OUTPUT_FORMAT,  # type: ignore[arg-type]
                generation_config={  # type: ignore[arg-type]
                    "emotion": self._config.default_emotion,
                    "speed": self._config.speed,
                },
            )
            async for event in stream:
                # SSE events come in multiple types; we only care about
                # raw audio chunks on the SSE path (timestamps are not
                # emitted here in v1's configuration; the WS path is
                # the canonical timestamp-bearing transport).
                if getattr(event, "type", None) != "chunk":
                    continue
                # ``event.data`` is a base64-encoded string on chunk
                # events. Decode to raw S16LE PCM bytes here so the
                # ``synthesize()`` contract stays clean. getattr keeps
                # pyright happy because the SDK's SSE event union has
                # subclasses without ``.data``.
                encoded = getattr(event, "data", "")
                if not encoded:
                    continue
                chunk: bytes = base64.b64decode(encoded)
                if not first_frame_logged:
                    # NFR4 baseline metric — same single-clock shape as
                    # the WS path, so the production log time-series
                    # is comparable across the transport flag.
                    ttfb_ms = (time.time_ns() - request_start_ns) // 1_000_000
                    log.info(
                        "tts.first_frame",
                        ttfb_ms=ttfb_ms,
                        voice_id=self._config.voice_id,
                        model=self._config.model,
                        transport="sse",
                    )
                    first_frame_logged = True
                yield chunk
        except cartesia.APIError as e:
            # v1 fail-fast: wrap and propagate. CLAUDE.md rule #4 —
            # never caught downstream. Process crashes; systemd restarts.
            raise CartesiaError(
                voice_id=self._config.voice_id,
                model=self._config.model,
                reason=str(e),
            ) from e


async def validate_credentials(config: SetupConfig) -> None:
    """Startup probe — confirm the Cartesia key + the configured voice exists.

    Called by ``__main__.py`` before pipeline assembly. Uses
    :meth:`AsyncCartesia.voices.get` (single GET for the configured
    voice ID) because:

    1. It validates the API key (401/403 on bad key) without burning
       any synthesis tokens.
    2. It validates that the configured ``voice_id`` is actually
       reachable (404 if the operator pasted a wrong/deleted GUID),
       which is more useful than just "any voice catalog read works".
    3. The response is tiny (one Voice record) — no pagination, no
       large catalog dump. The earlier ``voices.list(limit=1)`` probe
       observed a 60 s read timeout on the catalog endpoint;
       :meth:`voices.get` returns in ~hundreds of ms.

    A 10 s timeout caps the wait — operator gets a clean
    StartupValidationError if Cartesia is unreachable, rather than a
    minute-long hang at startup.

    Story 6.1 note: the probe stays on the HTTP ``voices.get`` path
    regardless of ``[tts] transport``. The WS transport gets validated
    implicitly on the first real synthesize call; pre-validating it at
    startup would burn an extra synthesis token per process start with
    no real reliability benefit (a WS that's broken at startup is
    near-certainly broken on the first turn too, where the existing
    fail-fast posture catches it).

    Raises:
        StartupValidationError: On any ``cartesia.APIError`` — the
            operator sees a clean ``startup.failed`` log + non-zero
            exit, not a stack trace from inside the SDK.
    """
    client = cartesia.AsyncCartesia(api_key=config.cartesia_api_key.get_secret_value())
    try:
        await client.voices.get(config.tts.voice_id, timeout=10.0)
    except cartesia.APIError as e:
        raise StartupValidationError(stage="cartesia", reason=str(e)) from e
