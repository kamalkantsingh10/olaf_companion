"""WhisperBackend - faster-whisper implementation of the STTBackend Protocol.

This module is the **only** place ``faster_whisper`` is imported (architecture
boundary-concentration rule from CLAUDE.md). Other modules speak through the
:class:`STTBackend` Protocol from Story 1.4.

Model loading happens once at startup via :meth:`WhisperBackend.load`. The
per-turn :meth:`transcribe` call wraps the synchronous CTranslate2 inference
in :func:`asyncio.to_thread` so the event loop stays responsive.

Confidence formula:

    confidence = exp(mean(avg_logprob_per_segment))

faster-whisper exposes per-segment ``avg_logprob`` in roughly ``[-3.0, 0.0]``;
``exp(...)`` projects back to ``(0, 1]``. Averaging the log-probs first then
``exp`` is the geometric mean of segment confidences — more honest than the
arithmetic mean of the per-segment exp values, which would over-weight
high-confidence segments. Document the choice here so future-Kamal doesn't
second-guess it during a calibration pass (Story 5.5).
"""

import asyncio
from math import exp

import numpy as np
import structlog
from faster_whisper import WhisperModel  # pyright: ignore[reportMissingTypeStubs]

from voice_agent_pipeline.stt.backend import STTBackend, TranscriptionResult

log = structlog.get_logger(__name__)


class WhisperBackend(STTBackend):
    """faster-whisper implementation of :class:`STTBackend`.

    Lifecycle:

    1. ``__init__`` — store args. Does NOT load the model.
    2. ``await load()`` — load the model in a thread (called once at startup).
    3. ``await transcribe(audio)`` — per-turn inference, also off-thread.

    The pipeline calls ``load()`` from ``run_pipeline`` before the audio
    loop opens. Loading takes 1-30s depending on model size and disk cache.
    """

    def __init__(self, model_size: str, compute_type: str, device: str) -> None:
        """Configure backend; defer model load to :meth:`load`.

        Args:
            model_size: faster-whisper model identifier. ``"tiny" /
                "base" / "small" / "medium" / "large-v3"``.
            compute_type: ``"int8" / "float16" / "float32"``. ``"int8"``
                is the CPU sweet spot.
            device: ``"cpu" / "cuda"``. Set by the factory in
                :mod:`voice_agent_pipeline.stt`.
        """
        self._model_size = model_size
        self._compute_type = compute_type
        self._device = device
        # WhisperModel instance; populated by load(). Optional[...] until then.
        self._model: WhisperModel | None = None

    async def load(self) -> None:
        """Load the faster-whisper model off the event-loop thread.

        First call on a new machine triggers a HuggingFace download (~500 MB
        for "small", more for larger sizes); subsequent loads hit the cache.
        Logged at INFO so the operator sees the startup pause is expected.
        """
        log.info(
            "stt.model_loading",
            model=self._model_size,
            device=self._device,
            compute_type=self._compute_type,
        )
        self._model = await asyncio.to_thread(
            WhisperModel,
            self._model_size,
            device=self._device,
            compute_type=self._compute_type,
        )
        log.info("stt.model_loaded", model=self._model_size, device=self._device)

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        """Run STT on a 16 kHz mono S16LE buffer.

        Args:
            audio: Raw PCM bytes from :class:`UtteranceCapturedFrame`.

        Returns:
            :class:`TranscriptionResult` with ``text`` (concatenated segment
            text, stripped) and ``confidence`` (geometric mean of per-segment
            ``exp(avg_logprob)``).

        Raises:
            RuntimeError: If :meth:`load` was not called first.
        """
        if self._model is None:
            # Don't try to lazy-load here — the latency cost would land on
            # the first turn. Failing fast surfaces the bug at dev time.
            raise RuntimeError("WhisperBackend.load() must be called before transcribe()")

        # int16 PCM (-32768..32767) → float32 [-1.0, 1.0] which is what
        # faster-whisper's ndarray path expects. Avoids writing to a
        # tempfile on every turn.
        np_audio = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

        # beam_size=1 (greedy) — much faster than default beam_size=5 with
        # negligible accuracy loss for short conversational utterances.
        # language="en" pins the language so faster-whisper skips its
        # built-in language-detect prologue (saves ~50ms per turn).
        # pyright can't fully resolve faster-whisper's transcribe signature
        # because some kwargs use untyped library defaults; the call itself
        # is sound (we pass only documented parameters).
        segments, _info = await asyncio.to_thread(
            self._model.transcribe,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            np_audio,
            language="en",
            beam_size=1,
        )

        # faster-whisper returns a generator; materialize so we can iterate
        # twice (text concat + confidence aggregate).
        seg_list = list(segments)
        text = "".join(s.text for s in seg_list).strip()
        if not seg_list:
            # Empty audio or pure silence — confidence 0 makes the low-conf
            # log path fire, which is the right behavior (operator sees that
            # capture was empty).
            confidence = 0.0
        else:
            mean_logprob = sum(s.avg_logprob for s in seg_list) / len(seg_list)
            confidence = exp(mean_logprob)
        return TranscriptionResult(text=text, confidence=confidence)
