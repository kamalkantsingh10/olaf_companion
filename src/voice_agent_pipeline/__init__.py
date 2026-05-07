"""voice-agent-pipeline package root.

Pipecat-based voice-agent service for the OLAF Companion project. This package
captures speech, dispatches turns, generates spoken responses with Cartesia,
and publishes typed expression + lifecycle events on configurable broadcast
channels.

Subpackage map (each populated incrementally across Epics 1-5):

- ``audio``      mic / speaker capture and pinning (Stories 1.5, 2.1)
- ``stt``        on-device STT via faster-whisper (Story 1.7)
- ``turn``       turn router fast/slow path (Stories 2.4, 4.3)
- ``tts``        Cartesia Sonic-3 streaming TTS (Story 2.3)
- ``splitter``   sentence splitter + SSML state machine (Story 3.3)
- ``publisher``  ROS 2 / DDS four-topic event publisher (Story 3.5)
- ``activity``   activity FSM + deferred-sleep + mic-mode signaling (Story 4.3)
- ``config``     ``setup.toml`` + ``.env`` loader (Story 1.2 onward)
- ``logging``    structlog + redaction + rotating files (Story 1.3)
- ``schemas``    pydantic event schemas (Story 1.4)

Entry point: ``python -m voice_agent_pipeline`` → ``__main__.main()``.
"""
