"""Tests for :mod:`voice_agent_pipeline.splitter.segmenter`.

The segmenter wraps the state machine + Story 3.2's resolver. Tests
build a real ``ExpressionMapConfig`` via ``_make_mapping`` (no mocks
of internal pure functions per CLAUDE.md rule #7).

Boundary contract: a ``Segment`` emits when the first of {sentence
terminator, emotion change, end-of-stream} arrives. Vocalizations
attach to the current segment but do NOT trigger emission.
"""

from typing import Any

import pytest

from voice_agent_pipeline.config.expression_map import (
    EmotionEntry,
    ExpressionMapConfig,
    FallbackFamily,
    UnknownEntry,
    VocalizationEntry,
)
from voice_agent_pipeline.splitter.mapping import SpeechEmotionPayload, VocalizationPayload
from voice_agent_pipeline.splitter.segmenter import Segment, Segmenter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mapping(
    *,
    families: dict[str, dict[str, Any]] | None = None,
) -> ExpressionMapConfig:
    """Small valid ExpressionMapConfig — same shape as Story 3.2's helper."""
    if families is None:
        families = {"high_energy_positive": {"members": ["enthusiastic"], "maps_to": "excited"}}
    return ExpressionMapConfig(
        schema_version=2,
        emotions={
            "neutral": EmotionEntry(expression_data={"led_color": "#ffffff"}),
            "content": EmotionEntry(expression_data={"led_color": "#a0e0a0"}),
            "excited": EmotionEntry(expression_data={"led_color": "#ffa040"}),
            "happy": EmotionEntry(expression_data={"led_color": "#ffd060"}),
        },
        vocalizations={
            "laughter": VocalizationEntry(tts_supported=True),
            "sigh": VocalizationEntry(tts_supported=False),
        },
        fallback_families={name: FallbackFamily(**body) for name, body in families.items()},
        unknown=UnknownEntry(maps_to="neutral"),
    )


def _drain(seg: Segmenter, *tokens: str) -> list[Segment]:
    """Feed tokens then flush; return all emitted Segments."""
    out: list[Segment] = []
    for token in tokens:
        out.extend(seg.consume(token))
    out.extend(seg.flush())
    return out


# ---------------------------------------------------------------------------
# AC #5 — sentence-terminator boundary
# ---------------------------------------------------------------------------


def test_sentence_terminator_emits_segment() -> None:
    """A `.` closes the current segment; further text starts a new one.

    AC #5 — period is a primary cadence boundary.
    """
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, "Hello there. Next sentence.")
    assert len(segments) == 2
    assert segments[0].text == "Hello there."
    assert segments[1].text == " Next sentence."


def test_question_mark_terminates_segment() -> None:
    """`?` is a sentence terminator."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, "Really? Yes.")
    assert [s.text for s in segments] == ["Really?", " Yes."]


def test_exclamation_terminates_segment() -> None:
    """`!` is a sentence terminator."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, "Wow! OK.")
    assert [s.text for s in segments] == ["Wow!", " OK."]


# ---------------------------------------------------------------------------
# AC #5, #7 — emotion-change boundary
# ---------------------------------------------------------------------------


def test_emotion_change_closes_prior_segment() -> None:
    """An emotion tag closes the current segment AND starts the next.

    AC #7 — `<emotion value="content"/> Hello there. <emotion
    value="excited"/> Great news!` produces two segments with the
    expected emotion payloads.
    """
    seg = Segmenter(_make_mapping())
    segments = _drain(
        seg,
        '<emotion value="content"/> Hello there. <emotion value="excited"/> Great news!',
    )
    assert len(segments) == 2

    s0 = segments[0]
    assert s0.text == " Hello there."
    assert s0.speech_emotion_payload is not None
    assert s0.speech_emotion_payload.emotion == "content"
    assert s0.vocalization_payloads == []

    s1 = segments[1]
    assert s1.text == " Great news!"
    assert s1.speech_emotion_payload is not None
    assert s1.speech_emotion_payload.emotion == "excited"


def test_no_emotion_segment_carries_none_payload() -> None:
    """Plain text with no emotion tag → ``speech_emotion_payload is None``."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, "Hello there.")
    assert len(segments) == 1
    assert segments[0].speech_emotion_payload is None


# ---------------------------------------------------------------------------
# AC #5, #8 — vocalization handling
# ---------------------------------------------------------------------------


def test_vocalization_attaches_to_segment_but_does_not_emit() -> None:
    """`[laughter]` mid-segment adds to vocalization_payloads; segment
    emits only at the next sentence terminator."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, "Ha [laughter] that's funny.")
    assert len(segments) == 1
    s = segments[0]
    assert len(s.vocalization_payloads) == 1
    assert s.vocalization_payloads[0].tag == "laughter"
    assert s.vocalization_payloads[0].tts_supported is True


def test_supported_vocalization_kept_in_segment_text() -> None:
    """`[laughter]` (tts_supported=True) stays in `Segment.text`.

    AC #8 — Cartesia renders the tag as audio when sent in the TTS
    payload, so we keep the literal characters in the text.
    """
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, "Ha [laughter] funny.")
    assert "[laughter]" in segments[0].text


def test_unsupported_vocalization_stripped_from_segment_text() -> None:
    """`[sigh]` (tts_supported=False) removed from `Segment.text`.

    AC #8 — Cartesia doesn't render this tag; sending it as literal
    text would have Cartesia speak ``s i g h``. Strip from TTS, still
    publish for embodiment.
    """
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, "Hmm [sigh] right.")
    assert "[sigh]" not in segments[0].text
    # Vocalization payload still attached.
    assert len(segments[0].vocalization_payloads) == 1
    assert segments[0].vocalization_payloads[0].tag == "sigh"


def test_multiple_vocalizations_in_segment() -> None:
    """Both vocalizations attach in order; supported kept, unsupported stripped."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, "[laughter] Hi [sigh] world.")
    assert len(segments) == 1
    s = segments[0]
    assert len(s.vocalization_payloads) == 2
    assert s.vocalization_payloads[0].tag == "laughter"
    assert s.vocalization_payloads[1].tag == "sigh"
    # [laughter] kept, [sigh] stripped.
    assert "[laughter]" in s.text
    assert "[sigh]" not in s.text


# ---------------------------------------------------------------------------
# AC #9 — segmenter reports payloads; cache (Story 3.2) does dedup
# ---------------------------------------------------------------------------


def test_consecutive_same_emotion_segments_each_carry_payload() -> None:
    """The segmenter emits a payload for every emotion-tagged segment.

    AC #9 — dedup is the cache's job (Story 3.2's LastPublishedCache),
    not the segmenter's. Two segments tagged with the same emotion
    each carry the resolver-produced payload; downstream the cache
    decides whether the second actually publishes.
    """
    seg = Segmenter(_make_mapping())
    segments = _drain(
        seg,
        '<emotion value="content"/> First. <emotion value="content"/> Second.',
    )
    assert len(segments) == 2
    assert segments[0].speech_emotion_payload is not None
    assert segments[0].speech_emotion_payload.emotion == "content"
    assert segments[1].speech_emotion_payload is not None
    assert segments[1].speech_emotion_payload.emotion == "content"


# ---------------------------------------------------------------------------
# AC #10 — reset() clears state
# ---------------------------------------------------------------------------


def test_reset_clears_buffer_and_emotion() -> None:
    """`reset()` clears state so the next stream starts clean.

    AC #10 — Story 3.7 calls this on `working → listening` boundaries.
    """
    seg = Segmenter(_make_mapping())
    list(seg.consume('<emotion value="excited"/> Half-finished'))
    seg.reset()

    # After reset, a fresh stream emits cleanly with no leftover emotion.
    segments = _drain(seg, "Plain text.")
    assert len(segments) == 1
    assert segments[0].speech_emotion_payload is None
    assert segments[0].vocalization_payloads == []


# ---------------------------------------------------------------------------
# AC #12 — fallback resolution flows through the resolver
# ---------------------------------------------------------------------------


def test_fallback_family_tag_resolves_via_resolver() -> None:
    """A family-member tag (`enthusiastic`) resolves to the family's
    `maps_to` (`excited`) via Story 3.2's resolver."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, '<emotion value="enthusiastic"/> Yay!')
    assert len(segments) == 1
    payload = segments[0].speech_emotion_payload
    assert payload is not None
    assert payload.emotion == "excited"
    assert payload.source_tag == "enthusiastic"
    assert payload.raw_tag == "enthusiastic"
    assert payload.resolved_fallback == "high_energy_positive"


def test_unknown_emotion_tag_resolves_to_unknown_neutral() -> None:
    """A truly unmapped emotion tag falls through to `unknown.maps_to`."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, '<emotion value="nevereverseen"/> Hi.')
    assert len(segments) == 1
    payload = segments[0].speech_emotion_payload
    assert payload is not None
    assert payload.emotion == "neutral"
    assert payload.resolved_fallback == "unknown"


def test_segment_is_frozen_pydantic_model() -> None:
    """`Segment` is a frozen pydantic v2 BaseModel (mutation raises)."""
    from pydantic import ValidationError

    seg = Segment(text="x", speech_emotion_payload=None, vocalization_payloads=[])
    with pytest.raises(ValidationError):
        seg.text = "y"  # type: ignore[misc]


def test_segment_carries_correct_payload_types() -> None:
    """`Segment` fields use the right types from Story 3.2."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, '<emotion value="content"/> Ha [laughter] cool.')
    s = segments[0]
    assert s.speech_emotion_payload is None or isinstance(
        s.speech_emotion_payload, SpeechEmotionPayload
    )
    for v in s.vocalization_payloads:
        assert isinstance(v, VocalizationPayload)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_segmenter_drives_state_machine_across_token_boundary() -> None:
    """Segmenter forwards token-split tags correctly via the state machine."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, '<emotion value="conte', 'nt"/> Hello.')
    assert len(segments) == 1
    assert segments[0].speech_emotion_payload is not None
    assert segments[0].speech_emotion_payload.emotion == "content"


def test_flush_emits_buffered_partial_segment() -> None:
    """Final segment without a terminator emits on flush."""
    seg = Segmenter(_make_mapping())
    segments = _drain(seg, '<emotion value="content"/> No terminator')
    assert len(segments) == 1
    assert "No terminator" in segments[0].text
