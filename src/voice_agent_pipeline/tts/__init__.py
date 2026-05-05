"""Text-to-speech - Cartesia Sonic-3 streaming TTS client (Story 2.3).

Re-exports :class:`TTSClient` so callers can write
``from voice_agent_pipeline.tts import TTSClient``.
"""

from voice_agent_pipeline.tts.client import TTSClient

__all__ = ["TTSClient"]
