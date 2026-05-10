"""``expression_map.yaml`` loader — Cartesia tag → speech_emotion taxonomy.

This module is the **substrate** Story 3.2's mapping resolver consumes.
Story 3.1's contract:

- Parse ``expression_map.yaml`` at startup, validate it against a strict
  pydantic schema, and refuse to load a malformed map.
- Surface the most-specific error first so an operator reading
  ``startup.failed`` knows which key broke.
- Carry only the **vocabulary** (canonical emotion names + Cartesia-tag
  fallback families) — embodiment vocabulary (pose / LED / eye state)
  belongs on the consumer side keyed on the canonical name.

Schema-3 boundary repair (sprint-change-proposal-2026-05-10)
-----------------------------------------------------------

The pre-repair shape carried per-emotion ``expression_data:`` blocks
with ``base_pose``, ``eye_state``, ``led_color``, ``led_intensity`` —
OLAF renderer hints that the loader stamped onto every
:class:`SpeechEmotionPayload` and shipped on the wire. That coupling
violated the project's consumer-agnostic publisher boundary.

Post-repair: ``emotions:`` is a **list of canonical names** (a
vocabulary, not renderer hints). The :class:`EmotionEntry` model is
gone. ``ExpressionMapConfig.emotions: list[str]`` is the typed surface
the resolver consumes; the embodiment project owns its own renderer
mapping keyed on those names.

What this module does NOT do
----------------------------

- Resolve tags into payloads — that's Story 3.2's
  :func:`voice_agent_pipeline.splitter.mapping.resolve`.
- SIGHUP-driven hot reload — Epic 5 (Story 5.2).
- Carry renderer / embodiment vocabulary — that left the wire in the
  schema-3 boundary repair (above).
"""

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import ConfigError

# Module-level logger. Tests assert on the level + event name, not on
# the rendered string. The redaction processor (Story 1.3) already
# strips raw audio and credentials; the loader has no business logging
# either, so the discipline here is simple: counts, not contents.
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants — Story 3.1 architectural surface
# ---------------------------------------------------------------------------

#: Schema version this build of the loader expects in
#: ``expression_map.yaml``. Lockstepped to
#: :data:`voice_agent_pipeline.config.version.SUPPORTED_SCHEMA_VERSION`
#: post-boundary-repair (both at 3); the local constant is preserved as
#: an indirection seam so the two could diverge again later if
#: maintenance cadences ever drift apart.
EXPRESSION_MAP_SCHEMA_VERSION: int = 3

#: The 6 primary emotions every map must define as first-class entries
#: (FR20 — no silent gaps for primary emotions). Tuple, not list — these
#: are immutable architectural constants. Order matches the architecture
#: spec; tests pin the order so a future re-arrangement is visible.
PRIMARY_EMOTIONS: tuple[str, ...] = (
    "neutral",
    "content",
    "excited",
    "sad",
    "angry",
    "scared",
)

#: The 6 secondary emotions every map must define. Distillate v1 maps
#: secondary → primary; the architecture promotes them to first-class
#: in v1 because the embodiment quality bar demands distinct poses /
#: LED colors per name (rendered consumer-side post-boundary-repair).
SECONDARY_EMOTIONS: tuple[str, ...] = (
    "happy",
    "curious",
    "sympathetic",
    "surprised",
    "frustrated",
    "melancholic",
)


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------


class VocalizationEntry(BaseModel):
    """One vocalization tag's row in the YAML (e.g. ``[laughter]``).

    The ``tts_supported`` flag drives Story 3.3's segmenter: when
    ``True`` the literal tag stays in the text sent to Cartesia (so the
    vocalization is rendered as audio); when ``False`` the tag is
    stripped from TTS text but still published to the ``vocalization``
    topic for embodiment.

    Two flavors of vocalization live in the v1 map:

    - **Audio bursts** (``laughter``, ``sigh``, ``gasp``,
      ``clears_throat``) — non-verbal sounds. ``tts_supported=True``
      when Cartesia can render them; otherwise the embodiment is
      expected to fill the gap with its own audio asset (or ignore).
    - **Gesture cues** (``nod``, ``shake``) — pure embodiment hooks
      with ``tts_supported=False`` (they are not sounds, they are
      visual gestures). The Talker prompt teaches the LLM when to emit
      them; the consumer binds them to head-nod / head-shake actions.
    """

    model_config = ConfigDict(extra="forbid")

    tts_supported: bool


class FallbackFamily(BaseModel):
    """A group of Cartesia tags that all map to the same first-class emotion.

    Example: ``high_energy_positive`` collects ``enthusiastic``,
    ``gleeful``, etc. and routes them to ``excited``. The architecture
    requires exactly 7 such families (architecture.md §"`speech_emotion`
    Mapping Completeness").

    ``maps_to`` must be the name of an entry in the top-level
    ``emotions:`` list; the loader's reference-integrity check
    (:func:`_assert_references`) enforces this after pydantic parsing.
    """

    model_config = ConfigDict(extra="forbid")

    members: list[str]
    maps_to: str


class UnknownEntry(BaseModel):
    """The last-resort fallback when no family matches a tag.

    Architecture's "no silent gaps" promise (FR20): every tag —
    first-class, family member, or completely unmapped — resolves to a
    valid first-class emotion. ``unknown.maps_to`` is conventionally
    ``"neutral"`` but the loader allows any first-class name (verified
    by :func:`_assert_references`).
    """

    model_config = ConfigDict(extra="forbid")

    maps_to: str


class ExpressionMapConfig(BaseModel):
    """Top-level pydantic model for ``expression_map.yaml``.

    Story 3.2's resolver consumes instances of this type; Story 3.7's
    pipeline holds one instance for the lifetime of the process (or
    until Epic 5's SIGHUP reload swaps in a fresh one).

    Attributes:
        schema_version: Must equal :data:`EXPRESSION_MAP_SCHEMA_VERSION`
            (=3) — anything else raises :class:`SchemaVersionError` at
            load time per NFR27.
        emotions: First-class emotion names, as a list. Every name in
            :data:`PRIMARY_EMOTIONS` and :data:`SECONDARY_EMOTIONS` must
            be present (FR20). The list shape replaces the pre-repair
            mapping-of-EmotionEntry — the data is a vocabulary now,
            not renderer hints.
        vocalizations: Vocalization tags. v1 ships 4 audio bursts
            (``laughter, sigh, gasp, clears_throat``) plus 2 gesture
            cues (``nod, shake``); the Talker prompt enumerates the
            full set and forbids the LLM from inventing others.
        fallback_families: Groups Cartesia's broader emotion vocabulary
            into 7 families (production map). Each family's
            ``maps_to`` must reference a first-class emotion.
        unknown: The last-resort fallback. ``maps_to`` must reference a
            first-class emotion — typically ``"neutral"``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    emotions: list[str]
    vocalizations: dict[str, VocalizationEntry]
    fallback_families: dict[str, FallbackFamily]
    unknown: UnknownEntry


# ---------------------------------------------------------------------------
# Public loader — `load_from_path`
# ---------------------------------------------------------------------------


def load_from_path(path: Path) -> ExpressionMapConfig:
    """Load + validate ``expression_map.yaml`` into an :class:`ExpressionMapConfig`.

    Steps, in **strict** order so the most-specific error surfaces
    first:

    1. Verify the file exists; ``ConfigError(missing_file=...)`` on miss.
    2. Parse the YAML via :func:`yaml.safe_load` — never ``yaml.load``
       (the latter is a known RCE risk and ruff's ``S506`` rule flags
       it). Wrap any ``yaml.YAMLError`` in ``ConfigError``.
    3. Run pydantic validation — wraps ``ValidationError`` in
       ``ConfigError`` (mirrors Story 1.2's ``setup.py`` pattern).
    4. Cross-check ``schema_version`` via the existing
       :func:`assert_schema_version` helper, passing
       ``supported=EXPRESSION_MAP_SCHEMA_VERSION`` so a future divergence
       between the global and the local constant remains an explicit
       choice rather than silent drift.
    5. Run :func:`_assert_completeness` — every primary + secondary
       emotion must be present in the ``emotions`` list (FR20 — no
       silent gaps).
    6. Run :func:`_assert_references` — every ``maps_to`` resolves to
       a first-class emotion (FR21).
    7. Log ``config.expression_map.loaded`` at INFO with **counts only**
       (no content — vocabulary names themselves aren't sensitive but
       the counts-only discipline is the easier rule to defend).

    Args:
        path: Path to ``expression_map.yaml``. Cwd-relative by default
            when callers pass ``Path("expression_map.yaml")``.

    Returns:
        A fully validated :class:`ExpressionMapConfig` instance.

    Raises:
        ConfigError: For any missing-file, parse-failure, validation,
            completeness, or reference-integrity issue.
        SchemaVersionError: When the file's ``schema_version`` does not
            match :data:`EXPRESSION_MAP_SCHEMA_VERSION`.
    """
    if not path.exists():
        # Wrapping FileNotFoundError as ConfigError keeps __main__.py's
        # `except VoiceAgentError` clause covering this case (Story
        # 1.2's missing-file pattern).
        raise ConfigError(missing_file=str(path))

    try:
        with path.open("r") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        # safe_load raises subclasses of YAMLError on malformed input.
        # Wrap so callers stay on a single error hierarchy.
        raise ConfigError(path=str(path), parse_error=str(e)) from e

    try:
        config = ExpressionMapConfig.model_validate(raw)
    except ValidationError as e:
        # `from e` preserves the cause chain (architecture.md
        # §"Error Handling": `raise X from y`).
        raise ConfigError(path=str(path), validation=str(e)) from e

    # Schema version BEFORE completeness — a wrong version means the
    # rest of the schema may not match this build's expectations, and
    # operators should fix the version mismatch first.
    assert_schema_version(
        config.schema_version,
        supported=EXPRESSION_MAP_SCHEMA_VERSION,
        source="expression_map.yaml",
    )

    _assert_completeness(config)
    _assert_references(config)

    # Counts-only at INFO — the vocabulary itself isn't sensitive, but
    # keeping the loader's logging discipline at "counts not contents"
    # is the easier rule to defend on review.
    log.info(
        "config.expression_map.loaded",
        extra={
            "emotion_count": len(config.emotions),
            "vocalization_count": len(config.vocalizations),
            "family_count": len(config.fallback_families),
        },
    )
    return config


# ---------------------------------------------------------------------------
# Internal helpers — completeness and reference integrity
# ---------------------------------------------------------------------------


def _assert_completeness(config: ExpressionMapConfig) -> None:
    """Verify every primary + secondary emotion is present in the list.

    AC #5: FR20's "no silent gaps" promise hinges on this check. The
    pydantic schema alone can't enforce "the list must contain these
    specific names" — it just enforces that the values are well-typed
    strings.

    Pre-repair this also checked that each entry's ``expression_data``
    was non-empty; that branch is gone with the field. Missing names
    are reported sorted for deterministic operator-facing error output.
    """
    required = set(PRIMARY_EMOTIONS) | set(SECONDARY_EMOTIONS)
    missing = sorted(required - set(config.emotions))
    if missing:
        raise ConfigError(missing_emotions=missing)


def _assert_references(config: ExpressionMapConfig) -> None:
    """Verify every ``maps_to`` reference resolves to a first-class emotion.

    AC #6 — covers ``unknown.maps_to`` and every
    ``fallback_families.<F>.maps_to``. A dangling reference would
    cause Story 3.2's resolver to crash mid-turn (KeyError); catching
    at startup keeps the operator-experience aligned with the
    "fail-fast" v1 posture.

    The function intentionally raises on the **first** dangling
    reference — operators fix one at a time. A "collect and report all"
    version is a future enhancement.
    """
    emotion_set = set(config.emotions)

    if config.unknown.maps_to not in emotion_set:
        raise ConfigError(
            reference="unknown.maps_to",
            target=config.unknown.maps_to,
        )

    for family_name, family in config.fallback_families.items():
        if family.maps_to not in emotion_set:
            raise ConfigError(
                reference=f"fallback_families.{family_name}.maps_to",
                target=family.maps_to,
            )
