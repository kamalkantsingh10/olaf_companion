"""Gemini Live-API streaming TTS client (Story 6.5).

The second :class:`~voice_agent_pipeline.tts.client.TTSClient` implementation
(alongside :class:`~voice_agent_pipeline.tts.cartesia.CartesiaClient`),
selected by ``[tts] provider = "gemini"``.

Design (decided in Story 6.5's TTFB-gate investigation, 2026-06-04):

- **Live API, not the dedicated TTS models.** The ``*-preview-tts`` models
  only support one-shot ``generateContent`` (no streaming); only the Live
  API (``bidiGenerateContent``) yields audio incrementally for a low TTFB.
  Model: ``gemini-3.1-flash-live-preview`` (~836 ms cold p50, beats
  Cartesia's ~1067 ms production p50).
- **Verbatim narration via system_instruction.** Live models are
  conversational by default; :data:`VERBATIM_INSTRUCTION` (plus the persona
  ``style_prompt``) flips them to speak the input exactly. The persona rides
  in the system instruction, NOT prepended to each turn — prepending it blew
  TTFB to ~5 s.
- **Cold path: a fresh session per ``synthesize()`` call.** Live billing is
  per-token, not per-connection, and reusing a session accumulates
  conversation context (rising per-turn input cost + verbatim-correctness
  risk). A fresh session per turn is stateless, cheapest, and already meets
  the gate; Story 6.2 cached openers mask the occasional cold-tail spike.
- **24 kHz → 16 kHz resample in-client.** Gemini emits 24 kHz mono s16le;
  the rest of the pipeline is pinned to 16 kHz, so we downsample here
  (``audioop.ratecv`` with threaded state across chunks) and the seam stays
  a one-file change.
- **Approximate word timing.** Gemini returns no word timestamps, and
  precise timing is not required (Story 6.5): :meth:`last_segment_timing`
  returns an even-distribution estimate so the Story 6.3 emphasis join keeps
  firing ("around the word" is good enough).

CLAUDE.md rule #4: any ``google.genai`` API error (or a dropped Live
session) is wrapped in :class:`GeminiTtsError` and propagates — never caught
in v1 paths. The process crashes and systemd restarts it.
"""

import audioop
from collections.abc import AsyncIterator
from typing import Any

import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import SecretStr

from voice_agent_pipeline.config.setup import SetupConfig, TtsConfig
from voice_agent_pipeline.errors import GeminiTtsError, StartupValidationError
from voice_agent_pipeline.tts.timing import SegmentTiming, Word

log = structlog.get_logger(__name__)

# Audio format constants. Gemini Live emits 24 kHz mono s16le; the pipeline
# is pinned to 16 kHz mono s16le (see audio/transport.py, sequential_loop.py).
_GEMINI_RATE_HZ = 24_000
_TARGET_RATE_HZ = 16_000
_SAMPLE_WIDTH_BYTES = 2  # s16le
_CHANNELS = 1

# Flips a conversational Live model into a verbatim text-to-speech reader.
# Measured 2026-06-04: with this, the model spoke the input EXACTLY (no
# reply). Kept short — even a fresh session re-sends this as input each turn,
# so its length is a small fixed per-turn token cost.
VERBATIM_INSTRUCTION = (
    "You are a text-to-speech engine. Speak the user message aloud VERBATIM, "
    "exactly as written. Do not reply, answer, summarize, or add words."
)


def content_turn(text: str) -> types.Content:
    """Wrap text as one completed user turn for ``send_client_content``."""
    return types.Content(role="user", parts=[types.Part(text=text)])


def build_live_config(voice_name: str, style_prompt: str) -> types.LiveConnectConfig:
    """Build the audio-out Live config: verbatim-TTS instruction + voice.

    The persona ``style_prompt`` rides in the ``system_instruction`` (one-time
    delivery steer), NOT prepended to each user turn — prepending the long
    persona as turn text made the model chew on it and blew TTFB to ~5 s.

    Args:
        voice_name: A Gemini prebuilt voice (e.g. ``"Fenrir"``).
        style_prompt: Optional persona/delivery brief; appended to the
            verbatim instruction when non-empty.

    Returns:
        A ``LiveConnectConfig`` requesting audio output in the given voice.
    """
    instruction = VERBATIM_INSTRUCTION
    if style_prompt:
        instruction = f"{VERBATIM_INSTRUCTION}\n\nVoice / delivery: {style_prompt}"
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=instruction)]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name),
            ),
        ),
    )


def first_audio_bytes(response: Any) -> bytes | None:
    """Return the first inline audio payload in a Live-API response, else None.

    The Live API nests audio at
    ``response.server_content.model_turn.parts[].inline_data.data``; any
    missing link in that chain (a setup ack, a transcription-only part)
    yields None so the caller keeps reading. Pure attribute walking so it is
    unit-testable without a live session.
    """
    server_content = getattr(response, "server_content", None)
    if server_content is None:
        return None
    model_turn = getattr(server_content, "model_turn", None)
    if model_turn is None:
        return None
    parts: list[Any] = getattr(model_turn, "parts", None) or []
    for part in parts:
        inline = getattr(part, "inline_data", None)
        data = getattr(inline, "data", None) if inline is not None else None
        if data:
            return data
    return None


def is_turn_complete(response: Any) -> bool:
    """True when a Live-API response signals the model turn has finished."""
    server_content = getattr(response, "server_content", None)
    return bool(server_content is not None and getattr(server_content, "turn_complete", False))


def _approx_segment_timing(text: str, total_pcm_bytes: int) -> SegmentTiming:
    """Estimate per-word timing by spreading the words over the audio duration.

    Gemini returns no word timestamps. We split the spoken text into words
    and distribute ``[start_ms, end_ms]`` evenly across the measured segment
    duration (derived from the resampled 16 kHz byte count). This is the
    "around the word is good enough" approximation Story 6.5 settled on, so
    ``sequential_loop._publish_emphasis_events`` keeps emitting one emphasis
    event per marked word without code changes.

    Args:
        text: The spoken segment text (same string passed to ``synthesize``).
        total_pcm_bytes: Total resampled 16 kHz mono s16le bytes yielded.

    Returns:
        A :class:`SegmentTiming` with one :class:`Word` per ``text.split()``
        token, or empty if there is no text / no audio.
    """
    words = text.split()
    if not words or total_pcm_bytes <= 0:
        return SegmentTiming(words=[])
    frame_count = total_pcm_bytes // (_SAMPLE_WIDTH_BYTES * _CHANNELS)
    duration_ms = int(frame_count * 1000 / _TARGET_RATE_HZ)
    per_word = duration_ms / len(words)
    return SegmentTiming(
        words=[
            Word(text=w, start_ms=int(i * per_word), end_ms=int((i + 1) * per_word))
            for i, w in enumerate(words)
        ],
    )


class GeminiClient:
    """Streaming TTS over the Gemini Live API (Story 6.5 ``TTSClient`` impl).

    Implements the same Protocol as :class:`CartesiaClient`:
    :meth:`synthesize` streams 16 kHz mono s16le frames; :meth:`last_segment_timing`
    exposes the most recent segment's (approximate) per-word timing for the
    emphasis join. Construct one per pipeline; each :meth:`synthesize` opens
    its own short-lived Live session (the cold path — see module docstring).
    """

    def __init__(self, config: TtsConfig, api_key: SecretStr) -> None:
        """Build the Gemini client; voice + models live on ``config.gemini``.

        Args:
            config: The ``[tts]`` config; ``config.gemini`` carries the
                ``voice_name`` / ``live_model`` / ``style_prompt``.
            api_key: The Google AI Studio key (``GEMINI_API_KEY``).
        """
        self._config = config
        gemini = config.gemini
        self._live_model = gemini.live_model
        # Built once — the verbatim instruction + persona + voice are static
        # for the lifetime of the client.
        self._live_config = build_live_config(gemini.voice_name, gemini.style_prompt)
        # genai.Client maintains its own connection machinery; one per client.
        self._client = genai.Client(api_key=api_key.get_secret_value())
        self._last_segment_timing: SegmentTiming | None = None

    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream 16 kHz mono s16le audio frames for ``text``.

        Opens a fresh Live session (cold path), sends ``text`` as one
        completed user turn, and yields resampled PCM chunks as they arrive.
        After the stream drains, :meth:`last_segment_timing` holds this
        segment's approximate per-word timing.

        Declared ``def`` (not ``async def``) to match the ``TTSClient``
        Protocol's calling convention — calling it returns the async
        generator synchronously.

        Args:
            text: Cleaned segment text (tags already stripped by the splitter).

        Yields:
            16 kHz mono s16le PCM frame bytes.

        Raises:
            GeminiTtsError: On any ``google.genai`` API error / dropped Live
                session during synthesis (CLAUDE.md rule #4 — wrap + propagate).
        """
        return self._synthesize(text)

    async def _synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Async-generator body for :meth:`synthesize` (see its docstring)."""
        # Reset per-call so a consumer reading last_segment_timing() after a
        # failed/empty call never sees a stale prior segment's words.
        self._last_segment_timing = None
        total_pcm_bytes = 0
        # audioop.ratecv state threads partial-frame carry across chunks so
        # resampling has no boundary clicks; None on the first chunk.
        rate_state: Any = None
        try:
            async with self._client.aio.live.connect(
                model=self._live_model,
                config=self._live_config,
            ) as session:
                await session.send_client_content(turns=content_turn(text), turn_complete=True)
                async for response in session.receive():
                    chunk_24k = first_audio_bytes(response)
                    if chunk_24k:
                        chunk_16k, rate_state = audioop.ratecv(
                            chunk_24k,
                            _SAMPLE_WIDTH_BYTES,
                            _CHANNELS,
                            _GEMINI_RATE_HZ,
                            _TARGET_RATE_HZ,
                            rate_state,
                        )
                        total_pcm_bytes += len(chunk_16k)
                        yield chunk_16k
                    if is_turn_complete(response):
                        break
        except genai_errors.APIError as e:
            # v1 fail-fast: wrap and propagate. Never catch downstream.
            raise GeminiTtsError(reason=str(e), model=self._live_model) from e
        # Stream drained cleanly — publish this segment's approximate timing
        # for the emphasis join (sequential_loop reads it after draining us).
        self._last_segment_timing = _approx_segment_timing(text, total_pcm_bytes)

    def last_segment_timing(self) -> SegmentTiming | None:
        """Return the most recent segment's approximate per-word timing.

        Set after each :meth:`synthesize` generator fully drains; ``None``
        before the first call or after a synthesis that yielded no audio.
        Unlike Cartesia's real timestamps these are evenly-distributed
        estimates (Gemini emits no word timing) — sufficient for the Story
        6.3 emphasis join per the Story 6.5 decision.
        """
        return self._last_segment_timing


async def validate_credentials(config: SetupConfig) -> None:
    """Startup probe — confirm the Gemini key + Live model are usable (Story 6.5).

    Mirrors ``turn.validate_credentials``: a ``models.list`` membership check
    validates BOTH the API key (auth error on a bad key) and that the
    configured ``live_model`` exists in the account's catalog — without
    burning synthesis tokens (no ``generate_content`` / Live session).

    Raises:
        StartupValidationError: On any ``google.genai`` API error, a missing
            ``GEMINI_API_KEY``, or when ``live_model`` is not in the catalog.
    """
    if config.gemini_api_key is None:
        raise StartupValidationError(
            stage="gemini_tts",
            reason="GEMINI_API_KEY is not set but the Gemini provider is active",
        )
    client = genai.Client(api_key=config.gemini_api_key.get_secret_value())
    live_model = config.tts.gemini.live_model
    try:
        names: set[str] = set()
        async for model in await client.aio.models.list():
            name = getattr(model, "name", None)
            if name:
                names.add(name.removeprefix("models/"))
    except genai_errors.APIError as e:
        raise StartupValidationError(stage="gemini_tts", reason=str(e)) from e
    if live_model not in names:
        raise StartupValidationError(
            stage="gemini_tts",
            model=live_model,
            reason=f"live_model not in Gemini catalog (sample: {sorted(names)[:5]})",
        )
