"""``expression_map.yaml`` loader — Cartesia tag → speech_emotion mapping table.

This module is the **substrate** Story 3.2's mapping resolver consumes.
Story 3.1's contract:

- Parse ``expression_map.yaml`` at startup, validate it against a strict
  pydantic schema, and refuse to load a malformed map.
- Surface the most-specific error first so an operator reading
  ``startup.failed`` knows which key broke.
- Keep the schema **open-ended on the inner ``expression_data`` dict** so
  adding a new emotion is forever a YAML edit (architecture.md
  §"Extensibility — Adding a New `speech_emotion` Must Stay Simple").

Why ``EXPRESSION_MAP_SCHEMA_VERSION`` is module-local
-----------------------------------------------------

Architecture.md §"Schema Conventions" says every config file lands at
``schema_version=2`` post-Epic-3. But Story 3.4 is the coordinated
migration that bumps ``setup.toml``'s schema_version + the four event
types together. Bumping the global :data:`SUPPORTED_SCHEMA_VERSION`
during Story 3.1 alone would break ``setup.toml``'s loader (still at
version 1) and Stories 1.2 / 1.4's tests, which would block the commit
under ``just check``.

Solution: a **module-local** :data:`EXPRESSION_MAP_SCHEMA_VERSION = 2`
passed explicitly to the existing
:func:`voice_agent_pipeline.config.version.assert_schema_version`
helper. After Story 3.4 lands and the global is bumped to 2, this
module's local constant becomes redundant in practice — but the
indirection is harmless (it points at the same value) and explicitly
leaves room for the two schemas to diverge again if the maintenance
cadences ever drift apart.

What this module does NOT do
----------------------------

- Resolve tags into payloads — that's Story 3.2's
  :func:`voice_agent_pipeline.splitter.mapping.resolve`.
- SIGHUP-driven hot reload — Epic 5 (Story 5.2).
- Validate per-emotion ``expression_data`` *shape* — only that the
  mapping is non-empty. The wire-schema (``SpeechEmotionPayload``,
  Story 3.4) treats it as opaque ``dict[str, Any]``.
"""

import logging
from pathlib import Path
from typing import Any

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
#: ``expression_map.yaml``. **Decoupled** from the global
#: :data:`voice_agent_pipeline.config.version.SUPPORTED_SCHEMA_VERSION`
#: until Story 3.4 lands the coordinated bump. See module docstring.
EXPRESSION_MAP_SCHEMA_VERSION: int = 2

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
#: LED colors per name.
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


class EmotionEntry(BaseModel):
    """One first-class emotion's row in the YAML.

    The single ``expression_data`` field is the **documented
    open-extensibility seam** — the ``dict[str, Any]`` typing matches
    the wire-schema field on
    :class:`voice_agent_pipeline.schemas.speech_emotion_event.SpeechEmotionPayload`
    (which Story 3.4 lands). Architecture.md §"Type System Conventions"
    explicitly endorses ``Any`` here; CLAUDE.md rule #3 carves out this
    one exception.

    The ``extra="forbid"`` rule on this wrapper is what catches typos
    like ``expresion_data`` (single 's') at startup instead of at
    runtime.
    """

    # extra="forbid" → typos in YAML keys fail at startup with a useful
    # pydantic message. The inner expression_data dict is open
    # (no extra="forbid") because new keys ship via YAML edits.
    model_config = ConfigDict(extra="forbid")

    # The architecturally-allowed dict[str, Any] seam. Pydantic doesn't
    # validate the inner shape — that's by design (architecture.md
    # §"Extensibility").
    expression_data: dict[str, Any]


class VocalizationEntry(BaseModel):
    """One vocalization tag's row in the YAML (e.g. ``[laughter]``).

    The ``tts_supported`` flag drives Story 3.3's segmenter: when
    ``True`` the literal tag stays in the text sent to Cartesia (so the
    vocalization is rendered as audio); when ``False`` the tag is
    stripped from TTS text but still published to the ``vocalization``
    topic for embodiment.
    """

    model_config = ConfigDict(extra="forbid")

    tts_supported: bool


class FallbackFamily(BaseModel):
    """A group of Cartesia tags that all map to the same first-class emotion.

    Example: ``high_energy_positive`` collects ``enthusiastic``,
    ``gleeful``, etc. and routes them to ``excited``. The architecture
    requires exactly 7 such families (architecture.md §"`speech_emotion`
    Mapping Completeness").

    ``maps_to`` must be the name of an entry under the top-level
    ``emotions:`` block; the loader's reference-integrity check
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
            (=2) — anything else raises :class:`SchemaVersionError` at
            load time per NFR27.
        emotions: First-class emotion entries. Every name in
            :data:`PRIMARY_EMOTIONS` and :data:`SECONDARY_EMOTIONS` must
            be present (FR20).
        vocalizations: The four v1 vocalization tags
            (``laughter, sigh, gasp, clears_throat``). Adding more is
            currently a YAML edit + a corresponding tag in the LLM's
            system prompt (Story 3.7).
        fallback_families: Groups Cartesia's broader emotion vocabulary
            into 7 families (production map). Each family's
            ``maps_to`` must reference a first-class emotion.
        unknown: The last-resort fallback. ``maps_to`` must reference a
            first-class emotion — typically ``"neutral"``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    emotions: dict[str, EmotionEntry]
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
       ``supported=EXPRESSION_MAP_SCHEMA_VERSION`` so the global
       constant in ``config/version.py`` (still 1 until Story 3.4) is
       NOT touched. ``SchemaVersionError`` propagates.
    5. Run :func:`_assert_completeness` — every primary + secondary
       emotion must be present with non-empty ``expression_data``
       (FR20 — no silent gaps).
    6. Run :func:`_assert_references` — every ``maps_to`` resolves to
       a first-class emotion (FR21).
    7. Log ``config.expression_map.loaded`` at INFO with **counts only**
       (no content — payload values may carry operator-private device
       addresses).

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

    # Counts-only at INFO — never log emotion or family contents (the
    # expression_data values may include operator-private fields).
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
    """Verify every primary + secondary emotion is present and populated.

    AC #5: FR20's "no silent gaps" promise hinges on this check. The
    pydantic schema alone can't enforce "the dict must contain these
    specific keys" — it just enforces that the values are well-typed.
    """
    required = set(PRIMARY_EMOTIONS) | set(SECONDARY_EMOTIONS)
    missing = sorted(required - set(config.emotions))
    if missing:
        # Sort the list for deterministic error rendering — operators
        # see the same ordering on every run.
        raise ConfigError(missing_emotions=missing)

    # Empty expression_data on a present entry is also a gap — the
    # publisher would emit an empty payload, which the embodiment
    # subscriber can't render. Catch at startup.
    for name in required:
        entry = config.emotions[name]
        if not entry.expression_data:
            raise ConfigError(
                emotion=name,
                reason="expression_data empty",
            )


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
    if config.unknown.maps_to not in config.emotions:
        raise ConfigError(
            reference="unknown.maps_to",
            target=config.unknown.maps_to,
        )

    for family_name, family in config.fallback_families.items():
        if family.maps_to not in config.emotions:
            raise ConfigError(
                reference=f"fallback_families.{family_name}.maps_to",
                target=family.maps_to,
            )
