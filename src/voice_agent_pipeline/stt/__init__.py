"""Speech-to-text - on-device transcription via faster-whisper (Story 1.7).

Re-exports :class:`STTBackend` and :class:`TranscriptionResult` from
``backend.py`` so callers can ``from voice_agent_pipeline.stt import STTBackend``.
The concrete :class:`WhisperBackend` lands in Story 1.7 in this package.
"""

from voice_agent_pipeline.stt.backend import STTBackend, TranscriptionResult

__all__ = ["STTBackend", "TranscriptionResult"]
