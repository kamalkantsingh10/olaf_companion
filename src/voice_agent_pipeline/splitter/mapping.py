"""Cartesia tag → ``SpeechEmotionPayload`` resolver + per-turn dedup cache.

Story 3.2 — the resolver layer between Story 3.1's
:class:`voice_agent_pipeline.config.expression_map.ExpressionMapConfig`
substrate and Story 3.3's streaming SSML segmenter. Two pure functions
plus one tiny stateful helper, no I/O, no async.

Pure-function contract:

- :func:`resolve` — emotion tag → :class:`SpeechEmotionPayload`. Three
  cases in priority order: first-class (no log) → fallback family
  (DEBUG, deduped) → unknown (WARN, every call). FR20 + FR21 + FR38.
- :func:`resolve_vocalization` — vocalization tag →
  :class:`VocalizationPayload`. Two cases: known (no log) → unknown
  (WARN, every call, ``tts_supported=False`` safe default).

Stateful helper:

- :class:`LastPublishedCache` — turn-scoped dedup of
  ``SpeechEmotionEvent`` publishes (FR24). Vocalizations are punctual
  and never deduped; the cache exposes a separate
  :meth:`LastPublishedCache.should_publish_vocalization` so calling
  conventions stay clear at every site.

Payload classes (Story 3.4 migration)
-------------------------------------

Story 3.2 introduced :class:`SpeechEmotionPayload` and
:class:`VocalizationPayload` here as an interim home. Story 3.4 moved
them to ``schemas/speech_emotion_event.py`` and
``schemas/vocalization_event.py`` (where they're now wrapped in
:class:`EventEnvelope`). This module re-exports the classes from their
canonical home so existing imports keep working without code changes
elsewhere.
"""

import logging

from voice_agent_pipeline.config.expression_map import ExpressionMapConfig
from voice_agent_pipeline.schemas.speech_emotion_event import SpeechEmotionPayload
from voice_agent_pipeline.schemas.vocalization_event import VocalizationPayload

# Module logger. Test fixtures pin this name via
# ``caplog.at_level(..., logger="voice_agent_pipeline.splitter.mapping")``;
# rename here means renaming there too.
log = logging.getLogger(__name__)


# Re-export for callers who still import from here. The canonical
# home is now ``schemas/`` per Story 3.4's migration.
__all__ = [
    "LastPublishedCache",
    "SpeechEmotionPayload",
    "VocalizationPayload",
    "resolve",
    "resolve_vocalization",
]


# ---------------------------------------------------------------------------
# Module-level state — the FR38 fallback dedup set
# ---------------------------------------------------------------------------

# Per AC #9 / FR38: DEBUG ``speech_emotion.fallback`` is emitted on the
# **first occurrence per process** of each (raw_tag, family_name) pair.
# Module-level state is the right scope: per-process semantics, no
# plumbing through the call sites. Tests reset this set via the autouse
# fixture in tests/unit/splitter/test_mapping.py.
_FALLBACK_LOG_SEEN: set[tuple[str, str]] = set()


# ---------------------------------------------------------------------------
# resolve() — three-case branching with priority first-class > family > unknown
# ---------------------------------------------------------------------------


def resolve(tag: str, mapping: ExpressionMapConfig) -> SpeechEmotionPayload:
    """Resolve a Cartesia emotion tag to a :class:`SpeechEmotionPayload`.

    Resolution order (AC #4): first-class > fallback family > unknown.
    A tag listed BOTH first-class and in a family resolves as first-class
    — the architecture's "promote a tag to first-class via YAML edit"
    extension story shouldn't require also pruning the old family.

    Logging (AC #9):
    - **No log** on first-class hits (happy path; would be noise).
    - **DEBUG** ``speech_emotion.fallback`` on the first occurrence per
      ``(raw_tag, family_name)`` pair — deduped via
      :data:`_FALLBACK_LOG_SEEN`.
    - **WARN** ``speech_emotion.unmapped`` on every unmapped tag —
      truly unknown tags are alarm-worthy until added to a family.

    Args:
        tag: The emotion tag to resolve. May be primary, secondary,
            family-member, or completely unknown.
        mapping: The loaded :class:`ExpressionMapConfig` (Story 3.1).

    Returns:
        A populated :class:`SpeechEmotionPayload`. The
        ``expression_data`` field is a reference (not a copy) into the
        mapping's entry — safe because pydantic's ``frozen=True`` on
        the entry plus the payload's own ``frozen=True`` mean the dict
        isn't mutable through either path.
    """
    # Case 1 — first-class hit. Highest priority; no log emission.
    if tag in mapping.emotions:
        return SpeechEmotionPayload(
            emotion=tag,
            source_tag=tag,
            raw_tag=tag,
            resolved_fallback=None,
            expression_data=mapping.emotions[tag].expression_data,
        )

    # Case 2 — fallback family hit. Iterate families in
    # insertion order (Python 3.7+ dict semantics); first match wins.
    # If a tag appears in two families, this is a YAML authoring bug;
    # the resolver picks deterministically but Story 3.1's loader
    # could in theory check uniqueness in a future enhancement.
    for family_name, family in mapping.fallback_families.items():
        if tag in family.members:
            # First occurrence → DEBUG. Subsequent → silent.
            seen_key = (tag, family_name)
            if seen_key not in _FALLBACK_LOG_SEEN:
                _FALLBACK_LOG_SEEN.add(seen_key)
                log.debug(
                    "speech_emotion.fallback",
                    extra={
                        "raw_tag": tag,
                        "resolved_fallback": family_name,
                        "emotion": family.maps_to,
                    },
                )
            return SpeechEmotionPayload(
                emotion=family.maps_to,
                source_tag=tag,
                raw_tag=tag,
                resolved_fallback=family_name,
                expression_data=mapping.emotions[family.maps_to].expression_data,
            )

    # Case 3 — unmapped. WARN every time (no dedup); fall through to
    # mapping.unknown.maps_to. Story 3.1's loader already verified
    # this reference resolves to a first-class emotion.
    fallback_emotion = mapping.unknown.maps_to
    log.warning(
        "speech_emotion.unmapped",
        extra={
            "raw_tag": tag,
            "resolved_fallback": "unknown",
            "emotion": fallback_emotion,
        },
    )
    return SpeechEmotionPayload(
        emotion=fallback_emotion,
        source_tag=tag,
        raw_tag=tag,
        resolved_fallback="unknown",
        expression_data=mapping.emotions[fallback_emotion].expression_data,
    )


# ---------------------------------------------------------------------------
# resolve_vocalization() — known-vs-unknown two-case branching
# ---------------------------------------------------------------------------


def resolve_vocalization(tag: str, mapping: ExpressionMapConfig) -> VocalizationPayload:
    """Resolve a vocalization tag to a :class:`VocalizationPayload`.

    Two cases: known (no log) → unknown (WARN every call,
    ``tts_supported=False`` safe default — strip from TTS). No
    fallback families in v1 (architecture.md — vocalizations are a
    flatter surface than emotions).

    Args:
        tag: The vocalization name (e.g. ``"laughter"``).
        mapping: The loaded :class:`ExpressionMapConfig`.

    Returns:
        A :class:`VocalizationPayload` with the correct
        ``tts_supported`` flag.
    """
    if tag in mapping.vocalizations:
        return VocalizationPayload(
            tag=tag,
            tts_supported=mapping.vocalizations[tag].tts_supported,
        )

    # Unknown vocalization — WARN every time (no dedup; rare enough
    # that suppression risks hiding regressions). Safe default
    # `tts_supported=False`: strip from TTS so we don't send garbage
    # tokens to Cartesia, but still publish the event for embodiment.
    log.warning("vocalization.unmapped", extra={"tag": tag})
    return VocalizationPayload(tag=tag, tts_supported=False)


# ---------------------------------------------------------------------------
# LastPublishedCache — per-turn dedup of speech_emotion publishes (FR24)
# ---------------------------------------------------------------------------


class LastPublishedCache:
    """Per-turn dedup of ``SpeechEmotionEvent``s; vocalizations always publish.

    State lifecycle (AC #6): one instance per pipeline (Story 3.7
    constructs it once); :meth:`reset` is called at turn boundaries
    (Story 3.7 wires this to the activity FSM's ``working → listening``
    transition). Story 3.2 just exposes the API and tests it; the
    lifecycle wiring is downstream.

    The two methods are deliberately not polymorphic
    (architecture choice). ``should_publish`` for emotions performs
    dedup; ``should_publish_vocalization`` always returns ``True``.
    Calling the wrong method on the wrong payload type is a type
    error pyright catches; the runtime contract assumes call-site
    discipline.
    """

    def __init__(self) -> None:
        # The most-recently approved-for-publish emotion name. ``None``
        # at start-of-turn (and after reset) means "no prior emotion;
        # next call always publishes."
        self._last: str | None = None

    def should_publish(self, payload: SpeechEmotionPayload) -> bool:
        """Return True iff the resolved emotion differs from the last published.

        State mutation: only on True. A False return leaves
        ``self._last`` untouched, preserving the dedup invariant for
        subsequent calls.
        """
        if payload.emotion == self._last:
            return False
        self._last = payload.emotion
        return True

    def should_publish_vocalization(self, payload: VocalizationPayload) -> bool:
        """Return True for every vocalization (FR24 — never deduped).

        The ``payload`` arg is unused in the body — its only purpose is
        to type-pin the call site to a vocalization payload. A future
        iteration might dedup on ``tag`` if the empirical rate proves
        problematic, but v1 publishes them all (vocalizations are
        infrequent enough that wire-noise isn't a concern).
        """
        del payload  # explicitly unused — see docstring
        return True

    def reset(self) -> None:
        """Clear the cache so the next call always publishes.

        Story 3.7 calls this on turn-boundary transitions
        (``working → listening``). Story 3.2 exposes it; the lifecycle
        wiring is downstream.
        """
        self._last = None
