"""Hand-rolled streaming SSML state machine.

Story 3.3 — parses three surface forms inline within an otherwise plain-text
token stream:

- **Emotion tag**: ``<emotion value="X"/>`` (Cartesia inline emotion).
- **Vocalization tag**: ``[name]`` (Cartesia inline vocalization burst).
- **Opener tag** (Story 6.2): ``<opener bucket="X"/>`` — selects which
  cached opener take plays at the start of the Talker's reply. Strips
  from the text Cartesia renders (it's a semantic side-effect, not
  spoken content).

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
state buffer (~40 bytes worst case for ``<opener bucket="acknowledge"/>``).

Disambiguating ``<emotion`` and ``<opener`` after the ``<``: when
the parser sees ``<``, it enters ``MAYBE_TAG`` and reads chars until
the prefix is determined. The first non-letter char (space, ``/``,
``>``) tells us which tag (or none — fall back to text). This
generalises the v1's single-tag ``MAYBE_EMOTION_TAG`` state to a
two-way branch without adding parser depth.

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
from typing import Literal, get_args

from voice_agent_pipeline.audio.opener_bucket import OpenerBucket
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
class OpenerTagEvent:
    """A fully-assembled ``<opener bucket="X"/>`` tag's bucket (Story 6.2).

    The bucket is the resolved :data:`OpenerBucket` Literal value —
    invalid bucket names raise :class:`SplitterError` at parse time
    (the parser validates against the canonical Literal so
    downstream consumers can ``Literal``-narrow without re-checking).
    """

    bucket: OpenerBucket


@dataclass(frozen=True)
class EmphasisStartEvent:
    """Sentinel (Story 6.3): the following ``TextEvent`` is inside an emphasis run.

    Emitted when the parser confirms an opening ``*`` emphasis mark. The
    marker char itself is stripped — only this sentinel signals the run.
    The text between the markers arrives as ordinary ``TextEvent``(s); the
    segmenter brackets those words as emphasis-marked using this sentinel
    and its closing :class:`EmphasisEndEvent`.
    """


@dataclass(frozen=True)
class EmphasisEndEvent:
    """Sentinel (Story 6.3): the prior emphasis run ended (closing ``*`` seen)."""


@dataclass(frozen=True)
class EndOfStreamEvent:
    """Sentinel emitted by :meth:`StateMachine.flush`. No payload."""


#: Tagged-union of all parse events. Story 3.7's segmenter pattern-matches
#: on this type. Story 6.3 added the two emphasis sentinels — additive
#: union extension; existing consumers keep working as long as they branch
#: with a default / ``isinstance`` ladder (they do).
ParseEvent = (
    TextEvent
    | EmotionTagEvent
    | VocalizationTagEvent
    | OpenerTagEvent
    | EmphasisStartEvent
    | EmphasisEndEvent
    | EndOfStreamEvent
)


# ---------------------------------------------------------------------------
# State machine — char-by-char, ~80 LOC including comments
# ---------------------------------------------------------------------------


# Internal states. Literal[...] not enum.Enum per CLAUDE.md rule #3.
_State = Literal[
    "TEXT",  # accumulating plain text
    "MAYBE_TAG",  # saw `<`, accumulating to decide: emotion / opener / not-a-tag
    "IN_EMOTION_TAG",  # confirmed `<emotion ...`, reading until `>`
    "IN_OPENER_TAG",  # confirmed `<opener ...`, reading until `>`  (Story 6.2)
    "MAYBE_VOCALIZATION_TAG",  # saw `[`, accumulating identifier chars
    "MAYBE_EMPHASIS_OPEN",  # saw `*` in TEXT, one-char lookahead to confirm  (Story 6.3)
    "IN_EMPHASIS_RUN",  # confirmed opening `*`, accumulating until closing `*`  (Story 6.3)
]


# Tag prefixes the MAYBE_TAG state disambiguates between.
_EMOTION_PREFIX = "<emotion "
_OPENER_PREFIX = "<opener "


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
        if self._state in (
            "MAYBE_TAG",
            "IN_EMOTION_TAG",
            "IN_OPENER_TAG",
            "MAYBE_VOCALIZATION_TAG",
        ):
            raise SplitterError(
                state=self._state,
                partial=self._tag_buf,
                reason="end-of-stream mid-tag",
            )
        if self._state == "IN_EMPHASIS_RUN":
            # Story 6.3: an emphasis run opened (``*word``) but never
            # closed before end-of-stream. Fail-fast — same posture as a
            # mid-tag flush; an unclosed run is a prompt-format defect we
            # want loud, not a silent strip of the trailing text.
            raise SplitterError(
                state=self._state,
                partial=self._tag_buf,
                reason="emphasis run not closed",
            )
        if self._state == "MAYBE_EMPHASIS_OPEN":
            # Story 6.3: a lone trailing ``*`` at end-of-stream never got
            # its lookahead char. It's literal text, not a mark — emit it
            # so we don't drop content. (Distinct from IN_EMPHASIS_RUN,
            # which is a confirmed-but-unclosed run and DOES raise.)
            self._text_buf += "*"
            self._state = "TEXT"
        if self._text_buf:
            yield TextEvent(self._text_buf)
            self._text_buf = ""
        yield EndOfStreamEvent()

    # -- internal step dispatcher -------------------------------------------

    def _step(self, ch: str) -> Iterator[ParseEvent]:
        if self._state == "TEXT":
            yield from self._step_text(ch)
        elif self._state == "MAYBE_TAG":
            yield from self._step_maybe_tag(ch)
        elif self._state == "IN_EMOTION_TAG":
            yield from self._step_in_emotion(ch)
        elif self._state == "IN_OPENER_TAG":
            yield from self._step_in_opener(ch)
        elif self._state == "MAYBE_VOCALIZATION_TAG":
            yield from self._step_maybe_vocalization(ch)
        elif self._state == "MAYBE_EMPHASIS_OPEN":
            yield from self._step_maybe_emphasis_open(ch)
        elif self._state == "IN_EMPHASIS_RUN":
            yield from self._step_in_emphasis_run(ch)

    def _step_text(self, ch: str) -> Iterator[ParseEvent]:
        if ch == "<":
            # Possible start of an emotion / opener tag. Flush text
            # buffer immediately — we know the run of text ended here.
            if self._text_buf:
                yield TextEvent(self._text_buf)
                self._text_buf = ""
            self._tag_buf = "<"
            self._state = "MAYBE_TAG"
        elif ch == "[":
            # Possible start of a vocalization. Same flush.
            if self._text_buf:
                yield TextEvent(self._text_buf)
                self._text_buf = ""
            self._tag_buf = "["
            self._state = "MAYBE_VOCALIZATION_TAG"
        elif ch == "*":
            # Story 6.3: possible opening emphasis mark. Flush the text
            # run that ended here, then enter one-char lookahead — we
            # only confirm the mark once we see the next char (letter /
            # digit → mark; anything else → literal ``*``).
            if self._text_buf:
                yield TextEvent(self._text_buf)
                self._text_buf = ""
            self._state = "MAYBE_EMPHASIS_OPEN"
        else:
            self._text_buf += ch

    def _step_maybe_emphasis_open(self, ch: str) -> Iterator[ParseEvent]:
        """Decide whether the buffered ``*`` opens an emphasis run (Story 6.3).

        Heuristic (AC #3): a ``*`` opens an emphasis run iff the next char
        is a letter or digit (``*r``, ``*a``, ``*1``). A ``*`` followed by
        whitespace, punctuation, another ``*``, ``<`` / ``[``, or
        end-of-stream is literal text (handles the ``2 * 3`` math case and
        stray markdown-style ``**``).

        On confirm: emit :class:`EmphasisStartEvent` and start accumulating
        the run body in ``_tag_buf`` (re-used as the run buffer — the tag
        and emphasis states are mutually exclusive). The first run char is
        ``ch`` itself.

        On reject: emit the literal ``*`` as text, return to ``TEXT``, and
        **re-process** ``ch`` there so a following ``<`` / ``[`` / ``*``
        still triggers its own state transition.
        """
        if ch.isalnum():
            self._tag_buf = ch
            self._state = "IN_EMPHASIS_RUN"
            yield EmphasisStartEvent()
        else:
            # Literal lone ``*``. Emit it, then re-dispatch ``ch`` through
            # the TEXT handler (it may itself open a tag / new mark).
            yield TextEvent("*")
            self._state = "TEXT"
            yield from self._step_text(ch)

    def _step_in_emphasis_run(self, ch: str) -> Iterator[ParseEvent]:
        """Accumulate emphasis-run body until the closing ``*`` (Story 6.3).

        The closing ``*`` ends the run: emit the accumulated body as a
        single bare :class:`TextEvent` (so the segmenter folds the marked
        words into the segment text exactly like surrounding text), then
        :class:`EmphasisEndEvent`. The ``*`` markers are never emitted —
        they're stripped syntax. An empty run (``**``) emits only the
        sentinel pair (no zero-length TextEvent).
        """
        if ch == "*":
            if self._tag_buf:
                yield TextEvent(self._tag_buf)
            self._tag_buf = ""
            self._state = "TEXT"
            yield EmphasisEndEvent()
        else:
            self._tag_buf += ch

    def _step_maybe_tag(self, ch: str) -> Iterator[ParseEvent]:
        """Disambiguate ``<...`` into emotion / opener / not-a-tag (Story 6.2).

        Accumulate until either prefix is confirmed (transition to the
        corresponding ``IN_*_TAG`` state) or ruled out (fall back to
        TEXT, emitting the accumulated chars). Story 6.2 extends the
        v1 single-emotion branch to a two-way branch; the structure
        mirrors the v1 ``_step_maybe_emotion`` exactly.
        """
        self._tag_buf += ch
        # Is the buffer still a strict prefix of EITHER known tag?
        # Once it stops being a prefix of either, we know it's not a
        # recognised tag; fall back to text.
        emotion_match = _EMOTION_PREFIX.startswith(self._tag_buf)
        opener_match = _OPENER_PREFIX.startswith(self._tag_buf)
        if self._tag_buf == _EMOTION_PREFIX:
            # Confirmed emotion — switch to attribute-reading mode.
            self._state = "IN_EMOTION_TAG"
            return
        if self._tag_buf == _OPENER_PREFIX:
            # Confirmed opener — switch to attribute-reading mode.
            self._state = "IN_OPENER_TAG"
            return
        if emotion_match or opener_match:
            # Still building toward one of the known prefixes; no
            # state change needed. Defensive sanity-check: shouldn't
            # be longer than the longest prefix (which would mean we
            # missed the confirmation transition above).
            if len(self._tag_buf) > max(len(_EMOTION_PREFIX), len(_OPENER_PREFIX)):
                yield TextEvent(self._tag_buf)
                self._tag_buf = ""
                self._state = "TEXT"
            return
        # Prefix mismatch — whatever we accumulated isn't a known
        # tag. Fall back to text so we don't lose the chars.
        yield TextEvent(self._tag_buf)
        self._tag_buf = ""
        self._state = "TEXT"

    def _step_in_opener(self, ch: str) -> Iterator[ParseEvent]:
        """Read until ``>`` closes the opener tag; emit :class:`OpenerTagEvent`.

        Story 6.2: mirrors :meth:`_step_in_emotion` exactly — accept
        both self-closing (``/>``) and non-self-closing (``>``) forms
        because LLMs sometimes drop the trailing ``/`` despite a
        self-closing prompt example.

        Invalid bucket values raise :class:`SplitterError` rather than
        falling back to text (the LLM emitted a typed semantic
        instruction; if the bucket is wrong, that's a prompt-drift
        defect we want loud, not a silent skip that would mask the
        regression).
        """
        self._tag_buf += ch
        if ch == ">":
            # Slice off the leading `<opener ` and trailing `>`.
            body = self._tag_buf[len(_OPENER_PREFIX) : -1].strip()
            body = body.rstrip("/").rstrip()
            bucket = _parse_opener_bucket(body)
            if bucket is None:
                # Malformed attribute (e.g., missing bucket="..." form).
                # Same fall-through-to-text behaviour as the emotion
                # branch so we don't drop content on a typo.
                yield TextEvent(self._tag_buf)
            else:
                # Validate against the canonical OpenerBucket Literal —
                # an unknown bucket name is a contract violation that
                # should fail loud per CLAUDE.md rule #4.
                if bucket not in get_args(OpenerBucket):
                    raise SplitterError(
                        state=self._state,
                        partial=self._tag_buf,
                        reason=f"unknown opener bucket {bucket!r}; "
                        f"valid: {sorted(get_args(OpenerBucket))}",
                    )
                # The Literal check above narrows ``bucket`` to
                # :data:`OpenerBucket` semantically; cast keeps pyright
                # quiet (the runtime check happened on the line above).
                yield OpenerTagEvent(bucket=bucket)  # type: ignore[arg-type]
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
            body = self._tag_buf[len(_EMOTION_PREFIX) : -1].strip()
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
    return _parse_quoted_attribute(attr_body, attr_name="value")


def _parse_opener_bucket(attr_body: str) -> str | None:
    """Extract the ``X`` from ``bucket="X"`` (Story 6.2 opener tag form).

    Returns ``None`` on malformed input. Caller falls back to text
    emission for malformed attributes, but validates the extracted
    bucket against the :data:`OpenerBucket` Literal and raises
    :class:`SplitterError` for an unknown-but-well-formed bucket.
    """
    return _parse_quoted_attribute(attr_body, attr_name="bucket")


def _parse_quoted_attribute(attr_body: str, attr_name: str) -> str | None:
    """Generic ``attr_name="X"`` extractor — backs emotion + opener helpers."""
    body = attr_body.strip()
    prefix = f'{attr_name}="'
    if not body.startswith(prefix):
        return None
    rest = body[len(prefix) :]
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
