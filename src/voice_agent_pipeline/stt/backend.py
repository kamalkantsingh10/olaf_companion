"""STTBackend Protocol — the v1/v2 swap point for speech-to-text inference.

The architecture (architecture.md §"Internal seams") names this as one of
the six Protocol interfaces that survive across the v1 → v2 transition.
v1 implementation is :class:`WhisperBackend` (Story 1.7, on-device
faster-whisper). v2 will swap in a Hailo-accelerated backend on Pi 5
without touching call sites.

Why a Protocol and not an ABC: we want structural typing — a backend just
has to *match the shape*, not inherit. Mocks in tests don't have to import
the Protocol either; they just expose ``async transcribe(...)``.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TranscriptionResult:
    """The output of a single STT inference call.

    Attributes:
        text: Best-guess transcript. May be empty for silent or unintelligible
            audio — callers decide how to handle that (Story 1.7 will gate on
            ``confidence`` to trigger a clarification prompt).
        confidence: Backend-reported confidence in ``[0.0, 1.0]``. Lower
            bound depends on the backend; faster-whisper's "logprob" is
            normalized into this range by :class:`WhisperBackend`.
    """

    text: str
    confidence: float


class STTBackend(Protocol):
    """Async STT inference behind a stable interface for v1/v2 backend swap."""

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        """Transcribe a chunk of raw PCM audio bytes into text + confidence.

        Args:
            audio: Raw PCM bytes (sample rate / channel count are backend-
                specified — typically 16kHz mono S16LE for Whisper-family
                backends).

        Returns:
            A :class:`TranscriptionResult` with the best-guess text and a
            normalized confidence score.
        """
        ...
