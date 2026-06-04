"""TTSClient Protocol — the streaming TTS seam.

v1 impl is :class:`CartesiaClient` (Story 2.3, Sonic-3 streaming). v2 may
swap to a self-hosted TTS engine or a different vendor — the Protocol
exists to make that swap a one-file change.
"""

from collections.abc import AsyncIterator
from typing import Protocol

from voice_agent_pipeline.tts.timing import SegmentTiming


class TTSClient(Protocol):
    """Streaming TTS. v1 impls: CartesiaClient (Story 2.3), GeminiClient (6.5).

    Note the unusual signature: ``synthesize`` is declared as a normal
    ``def`` returning ``AsyncIterator[bytes]``, NOT ``async def`` —
    even though the concrete implementation uses ``async def ... yield``.
    Python distinguishes async generator functions (with ``yield``)
    from coroutine functions (without); calling an async generator
    function returns an ``AsyncIterator[bytes]`` synchronously, so the
    Protocol's signature must match that calling convention. Marking
    it ``async def`` here would describe a coroutine returning an
    iterator (i.e., needs ``await``) — wrong shape for our consumers.
    """

    def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Stream audio frames for the given text.

        Args:
            text: The text (or SSML, depending on the impl) to synthesize.

        Yields:
            Raw audio frame bytes — sample rate / format are impl-specific
            but the pipeline's audio output stage (Story 2.1) and every v1
            client agree on 16kHz mono S16LE.
        """
        ...

    def last_segment_timing(self) -> SegmentTiming | None:
        """Per-word timing for the most recently-synthesized segment.

        Set after a ``synthesize()`` generator fully drains; ``None`` before
        the first call or when no timing is available. Consumed by the
        Story 6.3 emphasis join (``sequential_loop._publish_emphasis_events``).
        Cartesia fills it from real word ``timestamps``; Gemini (Story 6.5)
        fills it with an even-distribution approximation.
        """
        ...
