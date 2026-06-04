"""Provider-neutral per-segment word-timing models (Story 6.1 / 6.5).

``Word`` + ``SegmentTiming`` are the shape every TTS provider exposes via
``TTSClient.last_segment_timing()`` so the Story 6.3 emphasis join
(``sequential_loop._publish_emphasis_events``) is provider-agnostic.

- Cartesia (Story 6.1) fills these from real per-word ``timestamps`` events.
- Gemini (Story 6.5) fills these with an *approximate* even-distribution
  estimate — Gemini returns no word timestamps, and precise per-word timing
  is not required (emphasis fires a ROS event at approximate timing; see the
  Story 6.5 spec). The models are identical so the consumer never branches.

Originally defined in ``tts/cartesia.py``; extracted here in Story 6.5 so
the second provider can produce the same shape without importing the first.
"""

from pydantic import BaseModel, ConfigDict


class Word(BaseModel):
    """One word inside a synthesized segment, with its [start_ms, end_ms].

    Offsets are relative to the start of the segment. The pipeline pins
    integer milliseconds everywhere it talks about audio timing (matches
    ``tts.first_frame.ttfb_ms`` shape + the ``audio_frame_id`` semantics the
    emphasis join uses), so providers convert their native units on capture.

    Frozen + ``extra="forbid"`` per CLAUDE.md rule 3 (pydantic at
    boundaries; no mutation after construction; typos in mock fixtures fail
    loudly).

    Attributes:
        text: The word as the provider tokenised it. The emphasis consumer
            (Story 6.3) joins per-word entries against the LLM's emphasis
            index by ordinal position, not exact text match.
        start_ms: Start offset within the segment, integer milliseconds.
        end_ms: End offset within the segment, integer milliseconds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    start_ms: int
    end_ms: int


class SegmentTiming(BaseModel):
    """Per-call accumulated word timing for one ``synthesize()`` request.

    A single ``synthesize()`` call corresponds to one synthesized segment;
    this holds every :class:`Word` for that segment in spoken order. Frozen +
    ``extra="forbid"`` — once exposed via ``last_segment_timing()`` the model
    is read-only; callers consume the ``words`` list and don't mutate it.

    Attributes:
        words: Ordered list of :class:`Word` entries, in spoken order.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    words: list[Word]
