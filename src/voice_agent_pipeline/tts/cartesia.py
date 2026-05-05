"""CartesiaClient — Sonic-3 streaming TTS implementation of TTSClient (Story 2.3).

This module is the **single import boundary** for the ``cartesia`` SDK
(architecture.md §"Architectural Boundaries"). Other modules speak
through the :class:`TTSClient` Protocol from ``tts/client.py``.

TLS posture (NFR24): the Cartesia SDK uses ``httpx`` internally with
cert validation on by default. v1 deliberately exposes no knob to
disable validation — a future contributor adding such a knob would be
introducing a security regression. The :class:`CartesiaClient`
constructor passes only the api_key to ``cartesia.AsyncCartesia``; no
``verify=False`` / ``base_url`` override / cert path overrides land
here.

Streaming contract (FR15): :meth:`CartesiaClient.synthesize` is an
async generator that yields raw S16LE PCM bytes at 16 kHz mono — same
format the ``LocalAudioTransport`` output stage (Story 2.1) consumes,
so no resampler runs in the hot path. Each chunk is yielded as soon
as the SDK delivers it; the generator does NOT buffer the full stream
before yielding (real-time NFR4 contract).

v1 fail-fast: any ``cartesia.APIError`` raised mid-stream propagates
as :class:`CartesiaError` (a subclass of
:class:`ExternalServiceError`). CLAUDE.md rule #4 forbids catching
:class:`CartesiaError` anywhere downstream — process crashes, systemd
restarts (Epic 5).
"""

import base64
import time
from collections.abc import AsyncIterator

import cartesia
import structlog
from pydantic import SecretStr

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


class CartesiaClient:
    """Streaming TTS via Cartesia Sonic-3 — implements :class:`TTSClient`.

    Yields raw S16LE PCM bytes at 16 kHz mono via the SDK's
    ``client.tts.bytes(...)`` async iterator. No buffering — each chunk
    is yielded as soon as the SDK delivers it.
    """

    def __init__(self, config: TtsConfig, api_key: SecretStr) -> None:
        """Build the Cartesia client; voice + model live on the config.

        Args:
            config: Validated :class:`TtsConfig` carrying voice_id,
                default_emotion, and model identifier.
            api_key: Cartesia API key, wrapped in :class:`SecretStr` so
                ``repr(self)`` doesn't leak it.
        """
        self._config = config
        # AsyncCartesia maintains its own httpx connection pool; we
        # construct one per CartesiaClient (lifetime-bound to the
        # pipeline) so connection reuse cuts TLS handshake cost from
        # every turn's TTFB. No verify=False or cert override — TLS
        # validation is locked on per NFR24.
        self._client = cartesia.AsyncCartesia(api_key=api_key.get_secret_value())

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream synthesized audio chunks for ``text``.

        Args:
            text: The text to synthesize. v1 is plain text only; Story
                3.x will pass Cartesia inline emotion tags (parsed +
                stripped earlier in the pipeline by the streaming SSML
                splitter, but the text param here may still carry tag
                remnants in some edge cases — Cartesia treats unknown
                tags as text).

        Yields:
            Raw S16LE PCM bytes at 16 kHz mono. Frame size is the
            SDK's choice (typically a few hundred bytes per chunk).

        Raises:
            CartesiaError: On any ``cartesia.APIError`` subclass during
                stream open OR mid-stream. Cause chain preserved via
                ``raise ... from e``. v1 fail-fast — never caught
                downstream (CLAUDE.md rule #4).
        """
        request_start_ns = time.time_ns()
        first_frame_logged = False
        try:
            # ``tts.generate_sse()`` is Cartesia's true-streaming entry
            # point — returns an ``AsyncSSEEventStream`` of events with
            # ``.type`` ("chunk", "timestamps", etc.) and ``.data`` (raw
            # bytes for chunk events). Empirically delivers audio in
            # multiple chunks as they're synthesized, vs ``.generate()``
            # which buffers the full body before returning.
            #
            # ``.generate()`` (non-streaming) was the obvious first
            # choice but turned out to wait for the full synthesis
            # before yielding any bytes — defeating the streaming
            # contract. Documented inline here so a future contributor
            # doesn't try to "simplify" back to .generate().
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
                # raw audio chunks. ``timestamps`` / ``done`` / etc. are
                # silently dropped (Story 3.x may consume timestamps
                # for splitter alignment, but v1 doesn't need them).
                if getattr(event, "type", None) != "chunk":
                    continue
                # ``event.data`` is a **base64-encoded string** on chunk
                # events (Cartesia's SSE wire format wraps binary audio
                # in base64). Decode to raw S16LE PCM bytes here so
                # ``synthesize()``'s contract — yields ``bytes`` ready
                # to feed into ``LocalAudioTransport`` — stays clean.
                # getattr keeps pyright happy because the SDK's event
                # union has subclasses without ``.data``.
                encoded = getattr(event, "data", "")
                if not encoded:
                    continue
                chunk: bytes = base64.b64decode(encoded)
                if not first_frame_logged:
                    # NFR4 baseline metric: request-sent → first audio
                    # byte. ~200-400 ms p95 target on a healthy network.
                    ttfb_ms = (time.time_ns() - request_start_ns) // 1_000_000
                    log.info(
                        "tts.first_frame",
                        ttfb_ms=ttfb_ms,
                        voice_id=self._config.voice_id,
                        model=self._config.model,
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
