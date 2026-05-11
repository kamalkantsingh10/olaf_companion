"""GroqAsrBackend — cloud STT against Groq's openai-compatible audio endpoint.

This module is the **only** place :mod:`openai` is imported on the STT side
(boundary-concentration rule from architecture.md §"Architectural Boundaries"
and from ``CLAUDE.md`` rule #4 hygiene). It mirrors the design of
:class:`voice_agent_pipeline.stt.whisper_cpu.WhisperBackend` — both speak
through the :class:`STTBackend` Protocol from Story 1.4. The Protocol seam
is what makes the v1 default-backend flip (Whisper → Groq, sprint-change-
proposal-2026-05-12) a contained, reversible change.

Why Groq (sprint-change-proposal-2026-05-12.md §Section 3):

- Cheapest tier among the cloud-STT alternatives — Whisper-Large-V3-Turbo at
  ~$0.04/hr of audio vs Deepgram (~$0.26/hr) and OpenAI Whisper-1 ($0.36/hr).
- Same ``openai`` SDK Talker already uses (``base_url`` swap), so no new
  dependency and the boundary-concentration rule still holds — one SDK,
  two callers (``turn/talker.py`` for chat, this file for audio).
- 216x-real-time inference on Groq's LPU — a 5-second utterance transcribes
  in ~25 ms of compute plus ~50-150 ms network RTT. Comfortably under NFR3's
  500 ms p95 end-of-speech → transcript-ready budget.

Confidence formula (mirrors the WhisperBackend choice — see
``whisper_cpu.py`` module docstring for the rationale):

    confidence = exp(mean(avg_logprob_per_segment))

Groq's ``audio/transcriptions`` endpoint with ``response_format="verbose_json"``
returns per-segment ``avg_logprob`` in the same shape as openai/Whisper. We
average the log-probs first and then exponentiate — geometric mean of segment
confidences, more honest than the arithmetic mean of per-segment ``exp``
values (which would over-weight high-confidence segments).

Why we WRITE A WAV FILE INTO MEMORY rather than send raw PCM: Groq's audio
endpoint matches OpenAI's content-type expectations and rejects arbitrary
``application/octet-stream`` raw-PCM payloads. The cheapest valid container
is a 44-byte WAV header in front of the same 16 kHz mono S16LE samples we
already have — built in a :class:`io.BytesIO` with the stdlib :mod:`wave`
module, no new dep, no temp file on disk.
"""

import asyncio
import io
import wave
from math import exp
from typing import Any, cast

import openai
import structlog
from pydantic import SecretStr

from voice_agent_pipeline.errors import GroqAsrError
from voice_agent_pipeline.stt.backend import STTBackend, TranscriptionResult

log = structlog.get_logger(__name__)


# WAV-encoding constants. Match the pipeline-wide audio format (16 kHz mono
# S16LE) used by :class:`voice_agent_pipeline.audio.transport` and the
# Pipecat audio pipeline. If those change, update here in lockstep — there
# is no run-time format negotiation between transport and STT.
_WAV_SAMPLE_RATE_HZ = 16_000
_WAV_CHANNELS = 1
_WAV_SAMPLE_WIDTH_BYTES = 2  # int16

# Default Groq audio endpoint. Mirrors :data:`PROVIDER_BASE_URLS["groq"]`
# from ``turn/talker.py`` — kept duplicated here rather than imported because
# the STT side shouldn't reach across packages into a Talker constant.
_GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class GroqAsrBackend(STTBackend):
    """:class:`STTBackend` implementation against Groq's audio endpoint.

    Lifecycle mirrors :class:`WhisperBackend`:

    1. ``__init__`` — store args, build the long-lived :class:`AsyncOpenAI`
       client. Does NOT call the network.
    2. ``await load()`` — no-op. The Groq backend has no model weights to
       download; credential + reachability validation lives in the
       module-level ``validate_credentials`` probe in ``stt/__init__.py``
       (called from ``__main__.py`` Stage 3 before pipeline assembly).
    3. ``await transcribe(audio)`` — per-turn POST to
       ``audio/transcriptions``. Wraps the synchronous WAV-encode step in
       :func:`asyncio.to_thread` so the event loop stays responsive when
       packing larger utterances; the SDK call itself is already async.

    Confidence calibration: the existing ``low_confidence_threshold = 0.5``
    in :class:`SttConfig` is calibrated against faster-whisper's
    ``exp(avg_logprob)``. Groq returns the same shape; the threshold
    transfers directly. Story 5.5's soak captures any drift.
    """

    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        base_url: str | None = None,
    ) -> None:
        """Build the openai client and stash the model identifier.

        Args:
            api_key: Groq API key wrapped in :class:`SecretStr` so
                ``repr(self)`` doesn't leak it. The factory in
                :mod:`voice_agent_pipeline.stt` resolves this from
                :class:`SetupConfig.groq_api_key`.
            model: Groq audio model identifier — ``"whisper-large-v3-turbo"``
                is the v1 default. Configurable via
                :class:`SttConfig.groq_model`.
            base_url: Groq's openai-compatible base URL. Defaults to
                :data:`_GROQ_BASE_URL`; overridable for tests that mock
                against a local stub.
        """
        self._model = model
        # AsyncOpenAI maintains a long-lived httpx connection pool; we build
        # one per backend (lifetime-bound to the pipeline) rather than per
        # call so TLS handshake doesn't land on every turn's latency budget.
        # Same pattern as ``turn/talker.py``.
        self._client = openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url or _GROQ_BASE_URL,
        )

    async def load(self) -> None:
        """No-op for the cloud backend.

        The Protocol shape mandates this method; for an HTTP-backed backend
        there's nothing to load locally. Credential validation happens once
        at startup in ``stt.validate_credentials``, not here, so that a bad
        ``GROQ_API_KEY`` is surfaced during the same Stage 3 probes as
        Cartesia / Talker / Picovoice — not on the first turn.
        """
        log.info("stt.groq.load.noop", model=self._model)

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        """Transcribe raw 16 kHz mono S16LE PCM via Groq's ``audio/transcriptions``.

        Args:
            audio: Raw PCM bytes from
                :class:`voice_agent_pipeline.audio.frames.UtteranceCapturedFrame`.
                Format is implicit (16 kHz mono S16LE) — same contract as
                the WhisperBackend.

        Returns:
            :class:`TranscriptionResult` with ``text`` (the model's reply,
            stripped) and ``confidence`` (geometric mean of per-segment
            ``exp(avg_logprob)`` when ``verbose_json`` returns segments;
            ``0.0`` when the response has no segments — e.g., pure silence).

        Raises:
            GroqAsrError: On any ``openai.APIError`` subclass — same
                wrap-and-propagate posture as
                :class:`voice_agent_pipeline.turn.talker.Talker.complete`.
                CLAUDE.md rule #4 forbids catching this downstream.
        """
        # Pack raw PCM into a WAV container in memory. The PyAudio /
        # Pipecat capture path produces int16 mono samples already; we
        # just need the 44-byte WAV header. ``wave.open`` insists on a
        # writable binary stream — :class:`io.BytesIO` satisfies that
        # without touching disk. Off-thread because :mod:`wave` is
        # synchronous and large utterances (several hundred kilobytes)
        # would briefly block the event loop otherwise.
        wav_bytes = await asyncio.to_thread(_encode_wav, audio)

        try:
            # ``response_format="verbose_json"`` returns the same shape as
            # openai's Whisper API: top-level ``text`` plus ``segments``
            # with per-segment ``avg_logprob``. Plain ``"json"`` would
            # return only text, costing us the confidence signal that
            # Story 1.7's low-confidence routing relies on. The openai
            # SDK accepts a (filename, bytes) tuple for ``file=``; the
            # filename is required by the API but doesn't have to exist
            # on disk.
            #
            # Pyright can't fully resolve the openai SDK's overloaded
            # ``transcriptions.create`` (the response_format literal
            # narrows the return type to ``TranscriptionVerbose``, but
            # the overload resolution is fiddly). Cast to ``Any`` once
            # here and access fields defensively below.
            response = cast(
                Any,
                await self._client.audio.transcriptions.create(
                    file=("utterance.wav", wav_bytes),
                    model=self._model,
                    response_format="verbose_json",
                ),
            )
        except openai.APIError as e:
            # v1 fail-fast: wrap and propagate. CLAUDE.md rule #4 — never
            # caught downstream. Process crashes; systemd restarts. Same
            # posture as ``TalkerError`` / ``CartesiaError``.
            raise GroqAsrError(
                provider="groq",
                model=self._model,
                reason=str(e),
            ) from e

        # ``response.text`` is the canonical Whisper-compatible accessor —
        # populated regardless of response_format. Strip leading/trailing
        # whitespace because Groq (like openai) sometimes returns a single
        # leading space from the tokenizer.
        text = str(getattr(response, "text", "") or "").strip()

        # Segments may be absent on pure silence or when the model decides
        # the utterance was entirely non-speech. Mirror WhisperBackend's
        # behavior: confidence=0.0 makes the low-conf log path fire, which
        # is the right behavior (operator sees that capture was empty).
        segments: list[Any] = list(getattr(response, "segments", None) or [])
        if not segments:
            confidence = 0.0
        else:
            # Geometric mean of per-segment exp(avg_logprob). Same formula
            # as WhisperBackend — operators have one threshold to think
            # about, not two.
            logprobs = [float(getattr(s, "avg_logprob", 0.0)) for s in segments]
            mean_logprob = sum(logprobs) / len(logprobs)
            confidence = exp(mean_logprob)

        return TranscriptionResult(text=text, confidence=confidence)


def _encode_wav(pcm: bytes) -> bytes:
    """Wrap raw int16 PCM in a WAV container.

    Stdlib :mod:`wave` writes the canonical 44-byte WAV header plus the
    samples. Used only because Groq's ``audio/transcriptions`` endpoint
    expects a recognized container (WAV / FLAC / MP3 / …) — raw PCM is
    rejected. WAV adds ~44 bytes overhead per utterance, negligible.

    Args:
        pcm: Raw int16 mono 16 kHz LE samples — the format produced by the
            Pipecat audio capture path.

    Returns:
        WAV-encoded bytes ready to upload as ``file=("utterance.wav", ...)``.
    """
    buf = io.BytesIO()
    # ``wave.open(io_stream, "wb")`` requires the stream to support
    # ``tell()`` + ``seek()`` for the trailing-size header fixup —
    # BytesIO does both natively.
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(_WAV_CHANNELS)
        wav.setsampwidth(_WAV_SAMPLE_WIDTH_BYTES)
        wav.setframerate(_WAV_SAMPLE_RATE_HZ)
        wav.writeframes(pcm)
    return buf.getvalue()
