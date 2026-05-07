"""Tests for :mod:`voice_agent_pipeline.splitter.state_machine`.

Hand-rolled streaming parser. The hard work is **token-boundary
safety** — any tag may split across `consume(token)` calls and must
still emit exactly once when assembled. Most regressions hide there;
the test surface should be heavy on split cases.
"""

import pytest

from voice_agent_pipeline.errors import SplitterError
from voice_agent_pipeline.splitter.state_machine import (
    EmotionTagEvent,
    EndOfStreamEvent,
    StateMachine,
    TextEvent,
    VocalizationTagEvent,
)


def _drain(machine: StateMachine, *tokens: str) -> list[object]:
    """Feed tokens then flush; return the flat ParseEvent list."""
    out: list[object] = []
    for token in tokens:
        out.extend(machine.consume(token))
    out.extend(machine.flush())
    return out


# ---------------------------------------------------------------------------
# AC #1 — basic surface forms
# ---------------------------------------------------------------------------


def test_plain_text_emits_text_event() -> None:
    """Plain text with no tags passes through as a single TextEvent."""
    machine = StateMachine()
    events = _drain(machine, "Hello world.")
    assert events == [TextEvent("Hello world."), EndOfStreamEvent()]


def test_emotion_tag_emits_emotion_event() -> None:
    """A self-closing emotion tag emits exactly one EmotionTagEvent."""
    machine = StateMachine()
    events = _drain(machine, '<emotion value="excited"/>')
    assert events == [EmotionTagEvent("excited"), EndOfStreamEvent()]


def test_vocalization_tag_emits_vocalization_event() -> None:
    """A bracket vocalization emits exactly one VocalizationTagEvent."""
    machine = StateMachine()
    events = _drain(machine, "[laughter]")
    assert events == [VocalizationTagEvent("laughter"), EndOfStreamEvent()]


# ---------------------------------------------------------------------------
# AC #2 — token-boundary safety
# ---------------------------------------------------------------------------


def test_emotion_tag_split_across_token_boundary() -> None:
    """A tag split mid-syntax assembles correctly.

    AC #2 — `<emoti` then `on value="excited"/>` produces one
    EmotionTagEvent at the close, plus surrounding text.
    """
    machine = StateMachine()
    events = _drain(machine, "Hello <emoti", 'on value="excited"/> Great')
    assert events == [
        TextEvent("Hello "),
        EmotionTagEvent("excited"),
        TextEvent(" Great"),
        EndOfStreamEvent(),
    ]


def test_vocalization_split_across_token_boundary() -> None:
    """A bracket tag split mid-name assembles correctly."""
    machine = StateMachine()
    events = _drain(machine, "Ha[laug", "hter] there")
    assert events == [
        TextEvent("Ha"),
        VocalizationTagEvent("laughter"),
        TextEvent(" there"),
        EndOfStreamEvent(),
    ]


def test_tag_split_at_every_byte_position() -> None:
    """Splitting `<emotion value="excited"/>` at every position works.

    The state machine must be robust to ANY split point — pick a few
    representative positions, including the most-fragile (mid-attribute,
    mid-quote, just before `/>`).
    """
    full = '<emotion value="excited"/>'
    for split_at in [1, 5, 10, 15, 20, 23, 25]:
        machine = StateMachine()
        events = _drain(machine, full[:split_at], full[split_at:])
        assert events == [EmotionTagEvent("excited"), EndOfStreamEvent()], (
            f"failed for split_at={split_at}"
        )


# ---------------------------------------------------------------------------
# AC #3 — multiple events in one stream
# ---------------------------------------------------------------------------


def test_multiple_emotion_tags_in_stream() -> None:
    """Three emotion changes emit three events in order."""
    machine = StateMachine()
    events = _drain(
        machine,
        '<emotion value="content"/> A. <emotion value="excited"/> B. <emotion value="sad"/> C.',
    )
    expected_types = [
        EmotionTagEvent,
        TextEvent,
        EmotionTagEvent,
        TextEvent,
        EmotionTagEvent,
        TextEvent,
        EndOfStreamEvent,
    ]
    assert [type(e) for e in events] == expected_types
    emotion_events = [e for e in events if isinstance(e, EmotionTagEvent)]
    assert [e.value for e in emotion_events] == ["content", "excited", "sad"]


def test_mixed_emotion_and_vocalization_in_stream() -> None:
    """Emotion + vocalization in the same stream emit in order."""
    machine = StateMachine()
    events = _drain(
        machine,
        '<emotion value="happy"/> Ha [laughter]. <emotion value="content"/> So...',
    )
    types = [type(e) for e in events]
    assert types == [
        EmotionTagEvent,
        TextEvent,
        VocalizationTagEvent,
        TextEvent,
        EmotionTagEvent,
        TextEvent,
        EndOfStreamEvent,
    ]


# ---------------------------------------------------------------------------
# AC #4 — malformed tag at end-of-stream raises SplitterError
# ---------------------------------------------------------------------------


def test_malformed_emotion_tag_raises_at_flush() -> None:
    """Mid-tag end-of-stream raises SplitterError."""
    machine = StateMachine()
    list(machine.consume("Hello <emoti"))
    with pytest.raises(SplitterError) as exc_info:
        list(machine.flush())
    msg = str(exc_info.value)
    # Error context names the partial buffer so an operator can see
    # what was being parsed when the stream ended.
    assert "<emoti" in msg or "emoti" in msg


def test_malformed_vocalization_tag_raises_at_flush() -> None:
    """Mid-bracket end-of-stream raises SplitterError."""
    machine = StateMachine()
    list(machine.consume("Hello [laug"))
    with pytest.raises(SplitterError):
        list(machine.flush())


# ---------------------------------------------------------------------------
# AC #11 — additional cases from the story spec
# ---------------------------------------------------------------------------


def test_self_closing_tag_with_extra_whitespace() -> None:
    """`<emotion value="excited" />` (space before `/>`) parses OK."""
    machine = StateMachine()
    events = _drain(machine, '<emotion value="excited" />')
    assert events == [EmotionTagEvent("excited"), EndOfStreamEvent()]


def test_attribute_with_double_quote() -> None:
    """v1 supports `value="X"` (double quote). Single-quote not required."""
    machine = StateMachine()
    events = _drain(machine, '<emotion value="curious"/>')
    assert events == [EmotionTagEvent("curious"), EndOfStreamEvent()]


def test_open_bracket_without_close_in_text() -> None:
    """A literal `[` not followed by an identifier-then-`]` is plain text.

    AC #11 — defends against false-positive vocalization triggers on
    bracketed text that isn't a valid vocalization. The state machine
    starts buffering a potential vocalization after `[` but emits
    accumulated chars as text if it then sees a non-identifier char.
    """
    machine = StateMachine()
    # `[3]` — digit then close — not a valid vocalization (identifiers
    # are alphabetic + underscore in our grammar).
    events = _drain(machine, "Section [3] continues.")
    # Whatever the exact emission shape is, the digits must NOT be
    # treated as a vocalization tag.
    assert not any(isinstance(e, VocalizationTagEvent) for e in events)
    # And the text content survives somewhere.
    text_events = [e for e in events if isinstance(e, TextEvent)]
    full_text = "".join(e.text for e in text_events)
    assert "Section" in full_text
    assert "continues." in full_text


def test_flush_emits_buffered_text() -> None:
    """`flush()` drains any buffered plain text + then emits EndOfStream."""
    machine = StateMachine()
    list(machine.consume("hello"))  # text only, no terminator
    flushed = list(machine.flush())
    assert flushed == [TextEvent("hello"), EndOfStreamEvent()]


def test_flush_on_empty_machine_emits_only_end_of_stream() -> None:
    """A flush with no buffered text emits just the sentinel."""
    machine = StateMachine()
    flushed = list(machine.flush())
    assert flushed == [EndOfStreamEvent()]


def test_consume_emits_text_before_tag() -> None:
    """Text before a tag emits before the tag event (incremental)."""
    machine = StateMachine()
    # Within a single token: text, then a tag.
    events = list(machine.consume('Hello <emotion value="excited"/>'))
    # Text emits as soon as the parser sees `<` (which closes the
    # text buffer).
    assert events[0] == TextEvent("Hello ")
    assert events[1] == EmotionTagEvent("excited")


def test_underscore_in_vocalization_name() -> None:
    """`[clears_throat]` parses correctly (underscore is a valid char)."""
    machine = StateMachine()
    events = _drain(machine, "[clears_throat]")
    assert events == [VocalizationTagEvent("clears_throat"), EndOfStreamEvent()]
