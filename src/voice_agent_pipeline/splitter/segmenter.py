"""Boundary-based segment emitter wrapping the streaming SSML state machine.

Story 3.3's segmenter consumes :class:`StateMachine`'s ``ParseEvent``
stream and emits :class:`Segment` instances on whichever boundary
arrives first:

- **Sentence terminator** (``.``, ``?``, ``!``) — primary cadence.
- **Emotion-tag change** — emits prior segment, then starts a new one
  carrying the resolver's payload for the new emotion.
- **Vocalization tag** — adds payload to the current segment's
  ``vocalization_payloads``; does **not** trigger emission.

Vocalization text handling (FR25):
- ``tts_supported=True`` → keep literal ``[tag]`` in segment text so
  Cartesia renders the audio.
- ``tts_supported=False`` → strip from text; still publish the event
  for embodiment.

What this module does NOT do:

- Dedup speech_emotion publishes — that's Story 3.2's
  :class:`LastPublishedCache`. The segmenter reports every
  emotion-tagged segment's payload; the cache (downstream) decides
  whether to actually publish.
- Audio-frame metadata threading — Story 3.7 owns the Pipecat
  integration.
- Turn-boundary lifecycle — Story 3.7 calls :meth:`Segmenter.reset` on
  ``working → listening`` transitions.
"""

from collections.abc import Iterator

from pydantic import BaseModel, ConfigDict

from voice_agent_pipeline.config.expression_map import ExpressionMapConfig
from voice_agent_pipeline.splitter.mapping import (
    SpeechEmotionPayload,
    VocalizationPayload,
    resolve,
    resolve_vocalization,
)
from voice_agent_pipeline.splitter.state_machine import (
    EmotionTagEvent,
    EndOfStreamEvent,
    StateMachine,
    TextEvent,
    VocalizationTagEvent,
)

# Sentence-terminator characters. v1 punts on the false-positive cases
# (decimals like ``3.14``, abbreviations like ``Mr.``) per Story 3.3
# Dev Notes — Cartesia's prompts produce conversational speech where
# these are rare enough that NFR1 latency dominates segmentation
# nuance. Story 5.5 calibration owns refinement.
_TERMINATORS = frozenset(".?!")


class Segment(BaseModel):
    """One segment of the LLM response, ready for TTS + event publish.

    Frozen pydantic v2 model — safe to pass between async tasks
    without defensive copies. ``extra="forbid"`` so any future field
    addition lands explicitly.

    Attributes:
        text: The segment's plain text **with vocalization tags
            already kept-or-stripped per ``tts_supported``**. This is
            the string Story 3.7's ``CartesiaSynthesisProcessor``
            sends to TTS.
        speech_emotion_payload: Set when this segment carries an
            emotion change (resolver-produced payload). ``None`` when
            the segment continues the prior emotion. Story 3.2's
            :class:`LastPublishedCache` decides whether non-None
            payloads actually publish (dedup is the cache's job, not
            the segmenter's).
        vocalization_payloads: Every vocalization seen during this
            segment, in order. May be empty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    speech_emotion_payload: SpeechEmotionPayload | None
    vocalization_payloads: list[VocalizationPayload]


class Segmenter:
    """Drives a :class:`StateMachine`, emits :class:`Segment`s on boundaries.

    Stateful across calls: ``current_emotion`` retains the most-recent
    resolved emotion, ``_buffer`` accumulates text until a boundary,
    ``_pending_vocalizations`` collects vocalization payloads for the
    current segment.

    One instance per pipeline (Story 3.7 owns lifecycle); :meth:`reset`
    is the turn-boundary hook.
    """

    def __init__(self, mapping: ExpressionMapConfig) -> None:
        self._mapping = mapping
        self._machine = StateMachine()
        self._buffer: str = ""
        # When `current_emotion` is set, the **next** segment we emit
        # will carry it as its payload. This is how an emotion tag
        # before any text (the common case — `<emotion value="X"/> ...`)
        # attaches to the segment that follows.
        self._current_emotion: SpeechEmotionPayload | None = None
        self._pending_vocalizations: list[VocalizationPayload] = []
        # Tracks whether the current buffer's segment has had its
        # emotion attached yet — prevents re-attaching on subsequent
        # buffer-flushes within the same segment.
        self._emotion_attached: bool = False

    def consume(self, token: str) -> Iterator[Segment]:
        """Process a token chunk; yield zero or more Segments."""
        for event in self._machine.consume(token):
            yield from self._handle_event(event)

    def flush(self) -> Iterator[Segment]:
        """Drain the state machine + emit any final segment."""
        for event in self._machine.flush():
            yield from self._handle_event(event)

    def reset(self) -> None:
        """Clear all state. Story 3.7 calls this at turn boundaries.

        After reset, the next consumed stream starts with no buffered
        text and no carried emotion — the first segment will emit
        cleanly even if the prior turn ended mid-sentence.
        """
        self._machine = StateMachine()
        self._buffer = ""
        self._current_emotion = None
        self._pending_vocalizations = []
        self._emotion_attached = False

    # ------------------------------------------------------------------
    # Internal event handler
    # ------------------------------------------------------------------

    def _handle_event(self, event: object) -> Iterator[Segment]:
        if isinstance(event, TextEvent):
            yield from self._handle_text(event.text)
        elif isinstance(event, EmotionTagEvent):
            yield from self._handle_emotion_change(event.value)
        elif isinstance(event, VocalizationTagEvent):
            yield from self._handle_vocalization(event.name)
        elif isinstance(event, EndOfStreamEvent):
            yield from self._flush_buffer()
        # No `else` — ParseEvent is a closed union; pyright catches
        # missing cases.

    def _handle_text(self, text: str) -> Iterator[Segment]:
        """Append text to the buffer; emit on each sentence terminator.

        Scans char-by-char so a terminator mid-text closes the segment
        cleanly even if more text follows in the same token.
        """
        for ch in text:
            self._buffer += ch
            if ch in _TERMINATORS:
                yield self._build_segment()
                self._reset_segment_state()

    def _handle_emotion_change(self, raw_tag: str) -> Iterator[Segment]:
        """Resolve the new emotion, emit the prior segment, attach to next.

        AC #5 — emotion-change is a boundary. The current segment
        closes with whatever it has accumulated; the new segment will
        carry the resolver's payload for ``raw_tag``.

        **Whitespace-only buffer suppression**: between a sentence
        terminator and the next emotion tag, the buffer typically
        contains just a space (e.g. ``"Hello." <emotion ...> ...`` →
        the space after the period). Emitting that as its own segment
        would produce a ``Segment(text=" ", emotion=None)`` with no
        signal — wire-noise. Skip emission; let the whitespace fold
        into the next segment naturally.
        """
        if self._buffer.strip() or self._pending_vocalizations:
            yield self._build_segment()
            self._reset_segment_state()
        else:
            # No signal in the in-flight buffer (just whitespace).
            # Drop it — the whitespace was inter-tag spacing, not
            # part of any segment's spoken content. Without this
            # drop, ``"X." <emotion .../> Y`` produces a segment
            # with two leading spaces ("  Y") because the post-`.`
            # space accumulates AND the post-tag space accumulates.
            self._buffer = ""

        self._current_emotion = resolve(raw_tag, self._mapping)
        self._emotion_attached = False

    def _handle_vocalization(self, name: str) -> Iterator[Segment]:
        """Resolve the vocalization, attach to current segment, decide TTS text.

        FR25 — the literal ``[name]`` is kept in segment text iff
        ``tts_supported``. Vocalizations never trigger segment
        emission; they accumulate alongside the buffer until the next
        sentence/emotion boundary.
        """
        payload = resolve_vocalization(name, self._mapping)
        self._pending_vocalizations.append(payload)
        if payload.tts_supported:
            # Cartesia renders the literal characters as audio — keep
            # them in the TTS text.
            self._buffer += f"[{name}]"
        # else: strip from TTS. The vocalization event still publishes
        # via the segment's vocalization_payloads list.
        # No yield — vocalizations don't trigger segment emission.
        return
        yield  # pragma: no cover  (placates pyright on the generator type)

    def _flush_buffer(self) -> Iterator[Segment]:
        """Emit the final partial segment on end-of-stream, if any.

        Same whitespace-only suppression as :meth:`_handle_emotion_change`
        — a final segment containing just a trailing space carries no
        signal worth publishing.
        """
        if self._buffer.strip() or self._pending_vocalizations:
            yield self._build_segment()
            self._reset_segment_state()

    def _build_segment(self) -> Segment:
        """Materialize the current buffer + state into a :class:`Segment`."""
        # Attach the staged emotion exactly once per segment — the
        # latch prevents re-attachment if a buffer flushes mid-segment
        # (e.g., on a sentence terminator within an emotion's span).
        emotion: SpeechEmotionPayload | None = None
        if self._current_emotion is not None and not self._emotion_attached:
            emotion = self._current_emotion
            self._emotion_attached = True
        return Segment(
            text=self._buffer,
            speech_emotion_payload=emotion,
            vocalization_payloads=list(self._pending_vocalizations),
        )

    def _reset_segment_state(self) -> None:
        """Clear per-segment state after emission. Keeps emotion across emits.

        ``_current_emotion`` survives the per-segment reset — it stays
        the "active" emotion until a new emotion tag arrives. The
        ``_emotion_attached`` latch prevents re-emitting the same
        payload on every subsequent segment within the same emotion's
        span (the segmenter reports the change once; subsequent
        segments carry ``None`` and the cache handles dedup).
        """
        self._buffer = ""
        self._pending_vocalizations = []
