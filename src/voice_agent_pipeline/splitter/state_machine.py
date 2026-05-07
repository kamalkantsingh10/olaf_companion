"""Hand-rolled streaming SSML state machine.

Story 3.3 — parses two surface forms inline within an otherwise plain-text
token stream:

- **Emotion tag**: ``<emotion value="X"/>`` (Cartesia inline emotion).
- **Vocalization tag**: ``[name]`` (Cartesia inline vocalization burst).

The architectural promise (FR18): char-by-char streaming, **zero
external dependencies** (no regex, no XML parser — both buffer the
full stream and break the pipeline's incremental contract). Tags may
split across token boundaries; the machine preserves enough state to
assemble them on the next ``consume`` call.

Why hand-rolled
---------------

A regex like ``r'<emotion value="([^"]+)"/>'`` against the streaming
buffer needs to accumulate text from "no match yet" to "match found"
— that's the full stream, defeating streaming. An XML parser wants the
full document. A char-by-char state machine emits ``TextEvent``s as
soon as text is "definitely not part of a tag," and uses a tiny per-
state buffer (~32 bytes worst case for ``<emotion value="..."/>``).

Surface
-------

- :func:`StateMachine.consume(token)` — generator yielding
  :data:`ParseEvent`s from the consumed chunk.
- :func:`StateMachine.flush()` — emit any buffered text + an
  :class:`EndOfStreamEvent` sentinel. Raises :class:`SplitterError`
  if mid-tag at end-of-stream.

Out of scope (Story 3.3 explicitly does NOT do):

- Single-quote attribute support — Cartesia uses double quotes.
- Multi-attribute tags — only ``<emotion value="X"/>``.
- Non-self-closing tags — ``<emotion>X</emotion>`` is plain text in
  v1 (would fall through to Cartesia and likely render literally;
  Story 5.5 calibration territory).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from voice_agent_pipeline.errors import SplitterError

# ---------------------------------------------------------------------------
# Parse event types — internal-only dataclasses (not pydantic).
# ---------------------------------------------------------------------------
#
# These types don't cross a wire boundary; they're emitted, consumed,
# and discarded inside the splitter. ``@dataclass(frozen=True)`` is the
# right shape per architecture.md §"Type System Conventions" — pydantic
# validation overhead is wasted on internal-only types.


@dataclass(frozen=True)
class TextEvent:
    """Plain text chunk between tags (or at start/end of stream)."""

    text: str


@dataclass(frozen=True)
class EmotionTagEvent:
    """A fully-assembled ``<emotion value="X"/>`` tag's value."""

    value: str


@dataclass(frozen=True)
class VocalizationTagEvent:
    """A fully-assembled ``[name]`` vocalization tag."""

    name: str


@dataclass(frozen=True)
class EndOfStreamEvent:
    """Sentinel emitted by :meth:`StateMachine.flush`. No payload."""


#: Tagged-union of all parse events. Story 3.7's segmenter pattern-matches
#: on this type.
ParseEvent = TextEvent | EmotionTagEvent | VocalizationTagEvent | EndOfStreamEvent


# ---------------------------------------------------------------------------
# State machine — char-by-char, ~80 LOC including comments
# ---------------------------------------------------------------------------


# Internal states. Literal[...] not enum.Enum per CLAUDE.md rule #3.
_State = Literal[
    "TEXT",  # accumulating plain text
    "MAYBE_EMOTION_TAG",  # saw `<`, accumulating in case it's `<emotion ...`
    "IN_EMOTION_TAG",  # confirmed `<emotion ...`, reading until `/>`
    "MAYBE_VOCALIZATION_TAG",  # saw `[`, accumulating identifier chars
]


class StateMachine:
    """Char-by-char streaming SSML parser.

    Stateful across :meth:`consume` calls — partial tags survive token
    boundaries until the closing chars arrive. Caller drives via
    repeated ``consume(token)`` then a final ``flush()``.

    Not thread-safe; one instance per stream.
    """

    def __init__(self) -> None:
        self._state: _State = "TEXT"
        self._text_buf: str = ""
        # Holds the chars accumulated during a tag-in-progress so we can
        # either emit them (tag confirmed) or fall back to text (tag
        # turned out not to be a tag).
        self._tag_buf: str = ""

    def consume(self, token: str) -> Iterator[ParseEvent]:
        """Process a token chunk; yield zero or more parse events.

        Char-by-char dispatch. Each branch maintains the invariant:
        on exit, ``self._state`` + ``self._text_buf`` + ``self._tag_buf``
        capture every char consumed so far. No char is dropped.
        """
        for ch in token:
            yield from self._step(ch)

    def flush(self) -> Iterator[ParseEvent]:
        """Drain any buffered text and emit :class:`EndOfStreamEvent`.

        Raises :class:`SplitterError` if the machine is mid-tag at
        end-of-stream — the LLM emitted an incomplete tag, which is a
        protocol violation. v1 fail-fast (architecture.md §"Error
        Handling"); the process crashes and systemd restarts.
        """
        if self._state in ("MAYBE_EMOTION_TAG", "IN_EMOTION_TAG", "MAYBE_VOCALIZATION_TAG"):
            raise SplitterError(
                state=self._state,
                partial=self._tag_buf,
                reason="end-of-stream mid-tag",
            )
        if self._text_buf:
            yield TextEvent(self._text_buf)
            self._text_buf = ""
        yield EndOfStreamEvent()

    # -- internal step dispatcher -------------------------------------------

    def _step(self, ch: str) -> Iterator[ParseEvent]:
        if self._state == "TEXT":
            yield from self._step_text(ch)
        elif self._state == "MAYBE_EMOTION_TAG":
            yield from self._step_maybe_emotion(ch)
        elif self._state == "IN_EMOTION_TAG":
            yield from self._step_in_emotion(ch)
        elif self._state == "MAYBE_VOCALIZATION_TAG":
            yield from self._step_maybe_vocalization(ch)

    def _step_text(self, ch: str) -> Iterator[ParseEvent]:
        if ch == "<":
            # Possible start of an emotion tag. Flush text buffer
            # immediately — we know the run of text ended here.
            if self._text_buf:
                yield TextEvent(self._text_buf)
                self._text_buf = ""
            self._tag_buf = "<"
            self._state = "MAYBE_EMOTION_TAG"
        elif ch == "[":
            # Possible start of a vocalization. Same flush.
            if self._text_buf:
                yield TextEvent(self._text_buf)
                self._text_buf = ""
            self._tag_buf = "["
            self._state = "MAYBE_VOCALIZATION_TAG"
        else:
            self._text_buf += ch

    def _step_maybe_emotion(self, ch: str) -> Iterator[ParseEvent]:
        # Accumulate until we either confirm `<emotion ` (transition to
        # IN_EMOTION_TAG) or rule it out (fall back to TEXT, emitting
        # the accumulated chars as text).
        self._tag_buf += ch
        prefix = "<emotion "
        if len(self._tag_buf) <= len(prefix):
            # Still validating prefix.
            if self._tag_buf == prefix[: len(self._tag_buf)]:
                if self._tag_buf == prefix:
                    # Confirmed — switch to attribute-reading mode.
                    self._state = "IN_EMOTION_TAG"
                # else: still building prefix, no state change.
            else:
                # Prefix mismatch — what we accumulated isn't an emotion
                # tag. Fall back to text.
                yield TextEvent(self._tag_buf)
                self._tag_buf = ""
                self._state = "TEXT"
        else:
            # Should not be reachable — the prefix check transitions to
            # IN_EMOTION_TAG when complete. Defensive fallback.
            yield TextEvent(self._tag_buf)
            self._tag_buf = ""
            self._state = "TEXT"

    def _step_in_emotion(self, ch: str) -> Iterator[ParseEvent]:
        # Reading attribute payload until the close.
        #
        # Both close forms are accepted (Story 3.7 live-test
        # discovery — Groq's llama-3.1-8b-instant inconsistently emits
        # the non-self-closing form despite a self-closing prompt
        # example):
        #
        #   `<emotion value="X"/>`  — self-closing (XML-ish, ideal)
        #   `<emotion value="X">`   — non-self-closing (LLM-emitted)
        #   `<emotion value="X" />` — whitespace before `/>`
        #
        # We close on the first `>` after the value's closing quote.
        # The leading `<emotion ` is fixed; we strip it plus a
        # possible trailing `/` (and any surrounding whitespace) to
        # extract the attribute body before parsing.
        self._tag_buf += ch
        if ch == ">":
            # Slice off the leading `<emotion ` and trailing `>`.
            body = self._tag_buf[len("<emotion ") : -1].strip()
            # Strip optional trailing `/` from the self-closing form.
            body = body.rstrip("/").rstrip()
            value = _parse_emotion_value(body)
            if value is None:
                # Malformed attribute — emit accumulated chars as text
                # so we don't lose them; downstream Cartesia receives
                # the literal which it'll render or ignore. v1 punt.
                yield TextEvent(self._tag_buf)
            else:
                yield EmotionTagEvent(value)
            self._tag_buf = ""
            self._state = "TEXT"

    def _step_maybe_vocalization(self, ch: str) -> Iterator[ParseEvent]:
        # We're inside a `[...]` candidate. Identifier chars (alpha,
        # digit, underscore) accumulate; `]` closes; anything else
        # falls back to text.
        if ch == "]":
            # Close — extract the identifier between brackets.
            name = self._tag_buf[1:]  # strip leading `[`
            if name and _is_valid_identifier(name):
                yield VocalizationTagEvent(name)
            else:
                # `[]` or `[non-identifier]` — emit as text so we don't
                # silently drop content.
                yield TextEvent(self._tag_buf + "]")
            self._tag_buf = ""
            self._state = "TEXT"
        elif ch.isalnum() or ch == "_":
            self._tag_buf += ch
        else:
            # Non-identifier char inside `[` — this isn't a vocalization
            # tag. Fall back to text, emitting what we accumulated plus
            # this char.
            yield TextEvent(self._tag_buf + ch)
            self._tag_buf = ""
            self._state = "TEXT"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_emotion_value(attr_body: str) -> str | None:
    """Extract the ``X`` from ``value="X"`` (Cartesia double-quote form).

    Returns ``None`` on malformed input. The state machine's caller
    falls back to plain-text emission so we don't drop content.
    """
    # Expected form: `value="X"`. Strip whitespace, validate prefix,
    # extract the quoted value.
    body = attr_body.strip()
    if not body.startswith('value="'):
        return None
    # Find the closing quote.
    rest = body[len('value="') :]
    if '"' not in rest:
        return None
    return rest.split('"', 1)[0]


def _is_valid_identifier(name: str) -> bool:
    """Vocalization names are Python-style identifiers.

    First char: letter or underscore (NOT digit). Rest: alphanumeric or
    underscore. Excludes pure-digit content like ``[3]`` (from text
    like "Section [3]"), which is plain text not a vocalization.
    """
    if not name:
        return False
    if not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in name[1:])
