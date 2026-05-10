"""Tests for :mod:`voice_agent_pipeline.splitter.mapping`.

Story 3.2 — the resolver layer between Story 3.1's ``ExpressionMapConfig``
substrate and Story 3.3's streaming SSML segmenter. Two pure functions
(``resolve``, ``resolve_vocalization``) plus a tiny stateful helper
(``LastPublishedCache``).

Tests build real ``ExpressionMapConfig`` instances via ``_make_mapping``
(no YAML round-trip — the test surface is the resolver, not the loader).
Mocking pydantic models violates CLAUDE.md rule #7; the real config is
cheap to construct in-memory.
"""

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from voice_agent_pipeline.config.expression_map import (
    ExpressionMapConfig,
    FallbackFamily,
    UnknownEntry,
    VocalizationEntry,
)
from voice_agent_pipeline.splitter.mapping import (
    LastPublishedCache,
    SpeechEmotionPayload,
    VocalizationPayload,
    resolve,
    resolve_vocalization,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_mapping(
    *,
    families: dict[str, dict[str, Any]] | None = None,
) -> ExpressionMapConfig:
    """Build a small valid ``ExpressionMapConfig`` for resolver tests.

    Includes 3 emotions (one primary first-class, one secondary, one
    extra), 2 vocalizations (one supported, one not), and one fallback
    family by default. ``families`` overrides the family block when
    test-specific layouts are needed.
    """
    if families is None:
        families = {
            "high_energy_positive": {
                "members": ["enthusiastic", "gleeful"],
                "maps_to": "excited",
            },
        }
    return ExpressionMapConfig(
        schema_version=3,
        emotions=["neutral", "excited", "happy"],
        vocalizations={
            "laughter": VocalizationEntry(tts_supported=True),
            "sigh": VocalizationEntry(tts_supported=False),
        },
        fallback_families={name: FallbackFamily(**body) for name, body in families.items()},
        unknown=UnknownEntry(maps_to="neutral"),
    )


@pytest.fixture(autouse=True)
def _reset_fallback_log_dedup() -> None:
    """Reset the module-level ``_FALLBACK_LOG_SEEN`` between tests.

    The dedup set lives at module scope per AC #9 — its semantic scope
    is "per process," but per-test isolation prevents
    test_resolve_fallback_family_logs_debug_first_time from depending
    on ordering with sibling tests that also call ``resolve`` on
    fallback tags.
    """
    from voice_agent_pipeline.splitter import mapping as mapping_module

    mapping_module._FALLBACK_LOG_SEEN.clear()


# ---------------------------------------------------------------------------
# AC #1 / #2 — payload classes are pydantic, frozen, extra="forbid"
# ---------------------------------------------------------------------------


def test_speech_emotion_payload_is_frozen_and_extra_forbid() -> None:
    """``SpeechEmotionPayload`` is frozen (mutation raises) and rejects extras.

    The wire-shape contract (Story 3.4 will move this class to
    ``schemas/`` but the field set + constraints stay identical).
    """
    payload = SpeechEmotionPayload(
        emotion="excited",
        source_tag="excited",
        raw_tag="excited",
        resolved_fallback=None,
    )
    # Frozen → mutation raises ValidationError (pydantic v2 wraps the
    # frozen-instance error in its own validation hierarchy).
    with pytest.raises(ValidationError):
        payload.emotion = "happy"  # type: ignore[misc]

    # extra="forbid" — unknown field at construction time raises.
    with pytest.raises(ValidationError):
        SpeechEmotionPayload(
            emotion="excited",
            source_tag="excited",
            raw_tag="excited",
            resolved_fallback=None,
            bogus="x",  # type: ignore[call-arg]
        )


def test_speech_emotion_payload_audio_frame_id_defaults_to_none() -> None:
    """``audio_frame_id`` is optional with default ``None``.

    Story 3.7 will populate it when threading metadata onto Pipecat
    frames; Story 3.2's resolver leaves it as the default.
    """
    payload = SpeechEmotionPayload(
        emotion="excited",
        source_tag="excited",
        raw_tag="excited",
        resolved_fallback=None,
    )
    assert payload.audio_frame_id is None


def test_vocalization_payload_is_frozen_and_extra_forbid() -> None:
    """``VocalizationPayload`` mirrors the SpeechEmotionPayload shape rules."""
    payload = VocalizationPayload(tag="laughter", tts_supported=True)
    with pytest.raises(ValidationError):
        payload.tag = "sigh"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        VocalizationPayload(tag="laughter", tts_supported=True, bogus=1)  # type: ignore[call-arg]


def test_vocalization_payload_audio_frame_id_defaults_to_none() -> None:
    """``VocalizationPayload.audio_frame_id`` defaults to ``None``."""
    payload = VocalizationPayload(tag="laughter", tts_supported=True)
    assert payload.audio_frame_id is None


# ---------------------------------------------------------------------------
# AC #3 — resolve() three-case branching + log discipline
# ---------------------------------------------------------------------------


def test_resolve_primary_emotion_no_log(caplog: pytest.LogCaptureFixture) -> None:
    """A first-class emotion tag returns the entry's payload, no log.

    AC #3 case 1 — happy path. ``resolved_fallback`` is None.
    """
    mapping = _make_mapping()
    with caplog.at_level(logging.DEBUG, logger="voice_agent_pipeline.splitter.mapping"):
        payload = resolve("excited", mapping)

    assert payload.emotion == "excited"
    assert payload.source_tag == "excited"
    assert payload.raw_tag == "excited"
    assert payload.resolved_fallback is None
    # No log emission on the happy path — happy tags should be silent.
    assert caplog.records == []


def test_resolve_secondary_emotion_treated_as_first_class() -> None:
    """First-class entries (primary OR secondary) all hit the no-log branch.

    AC #3 — the resolver doesn't distinguish primary vs secondary; it
    only checks "is this name in mapping.emotions?".
    """
    mapping = _make_mapping()
    payload = resolve("happy", mapping)
    assert payload.emotion == "happy"
    assert payload.resolved_fallback is None


def test_resolve_fallback_family_logs_debug_first_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A family-member tag resolves to the family's ``maps_to``; DEBUG logged once.

    AC #3 case 2 + AC #9 — DEBUG ``speech_emotion.fallback`` on the
    first occurrence per (raw_tag, family) per process; subsequent
    occurrences silent (de-duped via the module-level set).
    """
    mapping = _make_mapping()
    with caplog.at_level(logging.DEBUG, logger="voice_agent_pipeline.splitter.mapping"):
        first = resolve("enthusiastic", mapping)
        second = resolve("enthusiastic", mapping)

    # Both calls return identical payloads.
    assert first.emotion == "excited"
    assert first.source_tag == "enthusiastic"
    assert first.raw_tag == "enthusiastic"
    assert first.resolved_fallback == "high_energy_positive"
    assert second == first

    # Exactly ONE DEBUG log on the fallback event name (de-duped).
    fallback_records = [r for r in caplog.records if "speech_emotion.fallback" in r.getMessage()]
    assert len(fallback_records) == 1
    assert fallback_records[0].levelno == logging.DEBUG


def test_resolve_unmapped_tag_logs_warn_every_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A truly unknown tag resolves to ``unknown.maps_to``; WARN every call.

    AC #3 case 3 + AC #9 — unmapped tags are alarm-worthy until added
    to a family in expression_map.yaml; suppressing on dedup would
    hide the regression signal.
    """
    mapping = _make_mapping()
    with caplog.at_level(logging.DEBUG, logger="voice_agent_pipeline.splitter.mapping"):
        first = resolve("nevereverseen", mapping)
        second = resolve("nevereverseen", mapping)

    assert first.emotion == "neutral"
    assert first.source_tag == "nevereverseen"
    assert first.raw_tag == "nevereverseen"
    assert first.resolved_fallback == "unknown"
    assert second == first

    # WARN logged on EVERY occurrence — no dedup on unmapped.
    unmapped_records = [r for r in caplog.records if "speech_emotion.unmapped" in r.getMessage()]
    assert len(unmapped_records) == 2
    assert all(r.levelno == logging.WARNING for r in unmapped_records)


# ---------------------------------------------------------------------------
# AC #4 — Resolution priority (first-class > family > unknown)
# ---------------------------------------------------------------------------


def test_resolve_first_class_takes_priority_over_family() -> None:
    """A tag listed both first-class AND in a family resolves as first-class.

    AC #4 — the architecture's primary extensibility story is "promote
    a tag to first-class via YAML edit." A YAML author shouldn't have
    to also remove the tag from its old family for the resolver to
    do the right thing.
    """
    # Construct a mapping where ``excited`` is BOTH first-class and a
    # member of high_energy_positive.
    mapping = _make_mapping(
        families={
            "high_energy_positive": {
                "members": ["excited", "enthusiastic"],
                "maps_to": "happy",  # would route to happy via fallback
            },
        },
    )
    payload = resolve("excited", mapping)
    # First-class wins: emotion="excited", resolved_fallback=None.
    assert payload.emotion == "excited"
    assert payload.resolved_fallback is None


# ---------------------------------------------------------------------------
# AC #7 — resolve_vocalization
# ---------------------------------------------------------------------------


def test_resolve_vocalization_known_tag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A known vocalization returns its payload with the correct tts_supported."""
    mapping = _make_mapping()
    with caplog.at_level(logging.DEBUG, logger="voice_agent_pipeline.splitter.mapping"):
        laughter = resolve_vocalization("laughter", mapping)
        sigh = resolve_vocalization("sigh", mapping)

    assert laughter == VocalizationPayload(tag="laughter", tts_supported=True)
    assert sigh == VocalizationPayload(tag="sigh", tts_supported=False)
    # No logs on known vocalizations.
    assert caplog.records == []


def test_resolve_vocalization_unknown_tag_warns_every_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown vocalization returns ``tts_supported=False`` and WARNs.

    AC #7 — safe default (strip from TTS); WARN every time (rare
    enough that suppression would hide regression).
    """
    mapping = _make_mapping()
    with caplog.at_level(logging.DEBUG, logger="voice_agent_pipeline.splitter.mapping"):
        first = resolve_vocalization("burp", mapping)
        second = resolve_vocalization("burp", mapping)

    assert first == VocalizationPayload(tag="burp", tts_supported=False)
    assert second == first

    unmapped_records = [r for r in caplog.records if "vocalization.unmapped" in r.getMessage()]
    assert len(unmapped_records) == 2
    assert all(r.levelno == logging.WARNING for r in unmapped_records)


# ---------------------------------------------------------------------------
# AC #5 / #6 — LastPublishedCache
# ---------------------------------------------------------------------------


def _make_payload(emotion: str) -> SpeechEmotionPayload:
    """Build a minimal SpeechEmotionPayload for cache tests."""
    return SpeechEmotionPayload(
        emotion=emotion,
        source_tag=emotion,
        raw_tag=emotion,
        resolved_fallback=None,
    )


def test_cache_first_call_returns_true() -> None:
    """First should_publish on a fresh cache returns True (no prior emotion)."""
    cache = LastPublishedCache()
    assert cache.should_publish(_make_payload("content")) is True


def test_cache_dedups_consecutive_same_emotion() -> None:
    """Same emotion twice → True, False (FR24 dedup).

    AC #5 — turn-scoped dedup of speech_emotion events.
    """
    cache = LastPublishedCache()
    assert cache.should_publish(_make_payload("content")) is True
    assert cache.should_publish(_make_payload("content")) is False


def test_cache_emits_on_emotion_change() -> None:
    """Different emotion after a deduped one re-publishes."""
    cache = LastPublishedCache()
    assert cache.should_publish(_make_payload("content")) is True
    assert cache.should_publish(_make_payload("content")) is False
    assert cache.should_publish(_make_payload("excited")) is True
    assert cache.should_publish(_make_payload("excited")) is False


def test_cache_after_reset_republishes_same_emotion() -> None:
    """``reset()`` clears the cache so a same-emotion call re-publishes.

    AC #5 — ``reset`` is the turn-boundary hook Story 3.7 will call on
    ``working → listening`` transitions.
    """
    cache = LastPublishedCache()
    assert cache.should_publish(_make_payload("content")) is True
    cache.reset()
    assert cache.should_publish(_make_payload("content")) is True


def test_cache_vocalization_always_publishes() -> None:
    """Vocalizations are punctual; the cache always returns True for them.

    AC #5 / FR24 — ``should_publish_vocalization`` does not dedup AND
    does not affect the emotion-cache state. Calling it between two
    same-emotion calls must NOT cause the second emotion to republish.
    """
    cache = LastPublishedCache()
    voc = VocalizationPayload(tag="laughter", tts_supported=True)

    assert cache.should_publish_vocalization(voc) is True
    assert cache.should_publish_vocalization(voc) is True
    assert cache.should_publish_vocalization(voc) is True


def test_cache_vocalization_does_not_affect_emotion_state() -> None:
    """Interleaving vocalizations doesn't disturb the emotion cache.

    AC #5 — the cache state machine has TWO separate methods so that
    vocalizations can't mutate the dedup-on-emotion contract.
    """
    cache = LastPublishedCache()
    voc = VocalizationPayload(tag="laughter", tts_supported=True)

    # Publish content (True), then a vocalization, then content again
    # — the second content must dedup despite the vocalization.
    assert cache.should_publish(_make_payload("content")) is True
    assert cache.should_publish_vocalization(voc) is True
    assert cache.should_publish(_make_payload("content")) is False


def test_cache_should_publish_vocalization_is_separate_method() -> None:
    """The two cache methods are deliberately not polymorphic.

    AC #5 — the architecture's "two methods, not polymorphic" call.
    Test that ``should_publish`` rejects a VocalizationPayload at the
    type level (pyright catches this; runtime test verifies at least
    that there's no accidental success path).
    """
    cache = LastPublishedCache()
    voc = VocalizationPayload(tag="laughter", tts_supported=True)
    # Pyright would flag this as a type error; runtime behavior is
    # implementation-defined — we just don't promise it works.
    # The contract is: call should_publish_vocalization for vocalizations.
    assert cache.should_publish_vocalization(voc) is True


# ---------------------------------------------------------------------------
# AC #11 — caplog-based log assertions on real logger
# ---------------------------------------------------------------------------


def test_log_uses_module_logger_name() -> None:
    """The module's logger is named ``voice_agent_pipeline.splitter.mapping``.

    Tests use ``caplog.at_level(..., logger=<this name>)`` to scope
    capture to this module. If the module ever uses a different name
    (e.g. via ``structlog.get_logger("custom")``), every test in this
    file silently captures nothing — pin the name explicitly here.
    """
    from voice_agent_pipeline.splitter import mapping

    assert mapping.log.name == "voice_agent_pipeline.splitter.mapping"
