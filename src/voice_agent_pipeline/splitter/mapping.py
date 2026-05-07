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

NOTE — interim home for payload classes
---------------------------------------

Story 3.2 introduces :class:`SpeechEmotionPayload` and
:class:`VocalizationPayload` here as a **temporary** measure. Story 3.4
(event-schema rebuild) will move them to
``schemas/speech_emotion_event.py`` and
``schemas/vocalization_event.py``, wrap them in :class:`EventEnvelope`,
and update every import site. Until then, this module is the canonical
home — the class definitions and field sets stay stable across the
move. **Do not** dual-export from ``schemas/`` until the Story 3.4
migration runs.
"""

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from voice_agent_pipeline.config.expression_map import ExpressionMapConfig

# Module logger. Test fixtures pin this name via
# ``caplog.at_level(..., logger="voice_agent_pipeline.splitter.mapping")``;
# rename here means renaming there too.
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload classes — interim home until Story 3.4
# ---------------------------------------------------------------------------


class SpeechEmotionPayload(BaseModel):
    """Inner payload of the eventual :class:`SpeechEmotionEvent` (Story 3.4).

    Architecture.md §"Stable contracts" — the field set is the wire
    contract. Adding a field is forward-compat (subscribers ignore
    unknowns); renaming or removing requires a ``schema_version`` bump.

    Attributes:
        emotion: Resolved first-class emotion name (always one of the
            keys in ``mapping.emotions``). For a fallback-family hit,
            this is the family's ``maps_to``; for unmapped, this is
            ``mapping.unknown.maps_to``.
        source_tag: The original tag the splitter parsed from the LLM
            stream — same as ``raw_tag`` for first-class hits, the
            family member name for fallback hits, the bogus tag for
            unmapped.
        audio_frame_id: Pipecat audio-frame id this event aligns to.
            Story 3.7 populates it; the resolver leaves it None.
        raw_tag: The verbatim tag the LLM emitted, for audit trail.
            FR20 — consumers see what was asked AND what was rendered.
        resolved_fallback: ``None`` for first-class hits; the family
            name for fallback hits; ``"unknown"`` for the unmapped
            fall-through. FR21.
        expression_data: Opaque dict copied from
            ``ExpressionMapConfig.emotions[<emotion>].expression_data``.
            ``Any``-typed inner values are the documented extensibility
            seam (CLAUDE.md rule #3 carve-out).
    """

    # frozen=True → safe across async tasks; mutation raises.
    # extra="forbid" → typo at construction time fails loudly.
    model_config = ConfigDict(frozen=True, extra="forbid")

    emotion: str
    source_tag: str
    audio_frame_id: str | None = None
    raw_tag: str
    resolved_fallback: str | None
    # The single architecturally-allowed Any seam (architecture.md
    # §"Type System Conventions"). New fields ship via expression_map.yaml
    # edits + this dict — no schema_version bump required.
    expression_data: dict[str, Any]


class VocalizationPayload(BaseModel):
    """Inner payload of the eventual :class:`VocalizationEvent` (Story 3.4).

    Vocalizations are flatter than emotions — no fallback families in
    v1. The ``tts_supported`` flag drives Story 3.3's segmenter:
    ``True`` → keep the literal ``[tag]`` in the TTS text (Cartesia
    renders the audio); ``False`` → strip from TTS, still publish for
    embodiment.

    Attributes:
        tag: The vocalization name (e.g. ``"laughter"``, ``"sigh"``).
        audio_frame_id: Story 3.7 populates; resolver leaves None.
        tts_supported: Whether Cartesia renders audio for this tag,
            sourced from ``mapping.vocalizations[<tag>].tts_supported``.
            Unknown tags get ``False`` (safe default — strip).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    audio_frame_id: str | None = None
    tts_supported: bool


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
