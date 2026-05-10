# ruff: noqa: E501
# YAML fixture lines naturally exceed 100 chars (the canonical 12-name
# emotions list and its surgical-replace targets in test bodies).
"""Tests for :mod:`voice_agent_pipeline.config.expression_map`.

Mirrors Story 1.2's ``test_setup.py`` pattern (tmp-path-only — no test
reads the project's real ``expression_map.yaml`` except the canary
``test_load_real_project_map_succeeds`` which validates the committed
file as a regression check).

Each test exercises one acceptance criterion from Story 3.1 — the file
covers AC #3 (parse / pydantic failures), AC #4 (schema_version
mismatch), AC #5 (completeness), AC #6 (reference integrity), AC #7
(extensibility), and AC #8 (the test surface itself).

Schema-3 boundary repair (sprint-change-proposal-2026-05-10): the
per-emotion ``expression_data`` block is gone, ``emotions:`` is now a
list of canonical names (a vocabulary, not renderer hints), and the
``EmotionEntry`` model has been deleted. Tests reflect the new shape
end-to-end.
"""

from pathlib import Path
from textwrap import dedent

import pytest

from voice_agent_pipeline.config.expression_map import (
    EXPRESSION_MAP_SCHEMA_VERSION,
    PRIMARY_EMOTIONS,
    SECONDARY_EMOTIONS,
    ExpressionMapConfig,
    load_from_path,
)
from voice_agent_pipeline.errors import ConfigError, SchemaVersionError

# Minimal-but-complete YAML covering all 12 emotions + 6 vocalizations +
# 1 family + unknown. Tests that need to mutate just one field do string
# surgery (mirrors Story 1.2's _VALID_TOML pattern).
_VALID_YAML = dedent(
    """\
    schema_version: 3
    emotions: [neutral, content, excited, sad, angry, scared, happy, curious, sympathetic, surprised, frustrated, melancholic]
    vocalizations:
      laughter: { tts_supported: true }
      sigh: { tts_supported: false }
      gasp: { tts_supported: false }
      clears_throat: { tts_supported: false }
      nod: { tts_supported: false }
      shake: { tts_supported: false }
    fallback_families:
      high_energy_positive:
        members: [enthusiastic, gleeful]
        maps_to: excited
    unknown:
      maps_to: neutral
    """
)


def _write_yaml(tmp_path: Path, body: str = _VALID_YAML) -> Path:
    """Write a YAML file under ``tmp_path`` and return the path.

    Mirrors Story 1.2's ``_write_files`` helper — every test gets its
    own tmp_path-scoped YAML, no shared filesystem state.
    """
    path = tmp_path / "expression_map.yaml"
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_schema_version_constant_is_3() -> None:
    """``EXPRESSION_MAP_SCHEMA_VERSION`` is the int ``3`` post-boundary-repair.

    Bumped from 2 to 3 in sprint-change-proposal-2026-05-10 because the
    ``SpeechEmotionPayload.expression_data`` field was removed from the
    wire — a breaking change to subscribers, even though no subscribers
    exist yet.
    """
    assert EXPRESSION_MAP_SCHEMA_VERSION == 3


def test_primary_and_secondary_emotion_lists_match_ac_1() -> None:
    """The 6+6 emotion names are the architectural quality bar (AC #1).

    The lists are tuples (immutable architectural constants) and contain
    exactly the names the story spec calls out.
    """
    assert PRIMARY_EMOTIONS == (
        "neutral",
        "content",
        "excited",
        "sad",
        "angry",
        "scared",
    )
    assert SECONDARY_EMOTIONS == (
        "happy",
        "curious",
        "sympathetic",
        "surprised",
        "frustrated",
        "melancholic",
    )
    # Tuples, not lists — they're constants.
    assert isinstance(PRIMARY_EMOTIONS, tuple)
    assert isinstance(SECONDARY_EMOTIONS, tuple)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_happy_path(tmp_path: Path) -> None:
    """A minimal valid YAML loads into a populated ``ExpressionMapConfig``.

    Canary for the whole loader: every nested model must validate, the
    completeness + reference checks must pass, and the resulting model
    is the typed surface Story 3.2's resolver will consume.
    """
    yaml_path = _write_yaml(tmp_path)
    config = load_from_path(yaml_path)

    assert isinstance(config, ExpressionMapConfig)
    assert config.schema_version == 3
    # All 12 emotions present — emotions is now a list of canonical
    # names; set comparison ignores order, which is fine because the
    # vocabulary has no inherent order.
    assert set(config.emotions) == set(PRIMARY_EMOTIONS) | set(SECONDARY_EMOTIONS)
    # All 6 vocalizations present (4 audio bursts + 2 gesture cues).
    assert set(config.vocalizations) == {
        "laughter",
        "sigh",
        "gasp",
        "clears_throat",
        "nod",
        "shake",
    }
    assert config.vocalizations["laughter"].tts_supported is True
    assert config.vocalizations["sigh"].tts_supported is False
    # nod / shake are gesture cues — never rendered as audio.
    assert config.vocalizations["nod"].tts_supported is False
    assert config.vocalizations["shake"].tts_supported is False
    # Fallback family + unknown wired up.
    assert "high_energy_positive" in config.fallback_families
    assert config.fallback_families["high_energy_positive"].maps_to == "excited"
    assert "enthusiastic" in config.fallback_families["high_energy_positive"].members
    assert config.unknown.maps_to == "neutral"


def test_load_real_project_map_succeeds() -> None:
    """The committed ``expression_map.yaml`` validates against the loader.

    AC #8 canary. Never delete this test — it's the regression alarm
    when the production map drifts (typo, dangling reference, missing
    family member). Story 3.x's SIGHUP reload story will rely on this
    invariant continuing to hold.
    """
    project_root_yaml = Path("expression_map.yaml")
    config = load_from_path(project_root_yaml)
    # The production map is exhaustive — assert the primary emotions
    # are all there + the 7 fallback families.
    for emotion in PRIMARY_EMOTIONS + SECONDARY_EMOTIONS:
        assert emotion in config.emotions, f"{emotion} missing from production map"
    assert len(config.fallback_families) == 7, "production map must have 7 families"
    # Every family's maps_to references a real first-class emotion (the
    # reference-integrity check would have raised, but assert as well).
    for name, family in config.fallback_families.items():
        assert family.maps_to in config.emotions, (
            f"family {name} maps_to {family.maps_to} which is not first-class"
        )


# ---------------------------------------------------------------------------
# AC #3 — Malformed YAML → ConfigError
# ---------------------------------------------------------------------------


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    """A nonexistent path raises ``ConfigError(missing_file=...)``.

    Mirrors Story 1.2's setup-loader contract: the loader's first step
    is existence; pre-empts ``FileNotFoundError`` from ``open()``.
    """
    missing = tmp_path / "not-here.yaml"
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(missing)
    assert str(missing) in str(exc_info.value)


def test_yaml_syntax_error_raises_config_error(tmp_path: Path) -> None:
    """Garbage YAML raises ``ConfigError`` (wraps ``yaml.YAMLError``).

    The wrap keeps callers on a single error hierarchy (``ConfigError``)
    so they don't need a separate ``yaml.YAMLError`` clause.
    """
    yaml_path = _write_yaml(tmp_path, "{not: valid: yaml: at: all")
    with pytest.raises(ConfigError):
        load_from_path(yaml_path)


def test_missing_required_block_raises_config_error(tmp_path: Path) -> None:
    """Dropping ``vocalizations:`` raises ``ConfigError`` mentioning it.

    AC #3 — the operator needs to know which top-level key is absent.
    """
    # Strip the entire vocalizations block (header + 6 entries).
    body = _VALID_YAML.replace("vocalizations:\n", "")
    for tag in ("laughter", "sigh", "gasp", "clears_throat", "nod", "shake"):
        body = body.replace(f"  {tag}: {{ tts_supported: true }}\n", "")
        body = body.replace(f"  {tag}: {{ tts_supported: false }}\n", "")
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    assert "vocalizations" in str(exc_info.value)


def test_extra_key_at_nested_level_raises_config_error(tmp_path: Path) -> None:
    """``extra="forbid"`` catches typos at any nested level.

    AC #3 — adding a stray key to a VocalizationEntry violates the
    forbid rule. Pydantic surfaces the offending key in the error.
    Pre-boundary-repair this test exercised EmotionEntry; post-repair
    EmotionEntry is gone, so the test moves to VocalizationEntry which
    still has a strict shape.
    """
    body = _VALID_YAML.replace(
        "laughter: { tts_supported: true }",
        "laughter: { tts_supported: true, bogus_key: 1 }",
    )
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    assert "bogus_key" in str(exc_info.value)


def test_wrong_type_raises_config_error(tmp_path: Path) -> None:
    """Replacing a typed field with the wrong YAML type raises.

    AC #3 — pydantic's type coercion fails; the wrap surfaces the
    validation message. Pre-repair this test pointed at
    ``expression_data`` (the dict-typed field); post-repair the
    ``emotions:`` list itself is the obvious target — passing a string
    where a list is expected.
    """
    body = _VALID_YAML.replace(
        "emotions: [neutral, content, excited, sad, angry, scared, happy, curious, sympathetic, surprised, frustrated, melancholic]",
        'emotions: "string instead of list"',
    )
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError):
        load_from_path(yaml_path)


# ---------------------------------------------------------------------------
# AC #4 — Schema-version mismatch → SchemaVersionError
# ---------------------------------------------------------------------------


def test_schema_version_mismatch_raises_schema_version_error(tmp_path: Path) -> None:
    """A non-matching ``schema_version`` raises ``SchemaVersionError``.

    AC #4 — operator-readable message names the file, the actual
    version, and the supported version. The check delegates to
    ``config.version.assert_schema_version(supported=3, ...)`` so the
    helper isn't duplicated here.
    """
    body = _VALID_YAML.replace("schema_version: 3", "schema_version: 1")
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(SchemaVersionError) as exc_info:
        load_from_path(yaml_path)
    msg = str(exc_info.value)
    assert "expression_map.yaml" in msg
    assert "1" in msg
    assert "3" in msg


# ---------------------------------------------------------------------------
# AC #5 — Completeness check
# ---------------------------------------------------------------------------


def test_missing_primary_emotion_raises_config_error(tmp_path: Path) -> None:
    """Dropping ``excited`` raises ``ConfigError`` listing it as missing.

    AC #5 — FR20's "no silent gaps" is enforced for both primary and
    secondary emotion sets. Post-boundary-repair, "removing an emotion"
    is a list-element deletion, not a block deletion.
    """
    body = _VALID_YAML.replace(", excited,", ",")
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    assert "excited" in str(exc_info.value)


def test_missing_secondary_emotion_raises_config_error(tmp_path: Path) -> None:
    """Dropping ``melancholic`` raises ``ConfigError`` listing it.

    AC #5 — completeness covers secondary emotions too.
    """
    body = _VALID_YAML.replace(", melancholic", "")
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    assert "melancholic" in str(exc_info.value)


def test_empty_emotions_list_raises_config_error(tmp_path: Path) -> None:
    """An empty ``emotions: []`` list raises ``ConfigError``.

    AC #5 — the new boundary-repair-era equivalent of the old
    ``test_empty_expression_data_raises_config_error``. The
    completeness check fires when the required set isn't a subset of
    the loaded vocabulary — an empty list is the maximally degenerate
    case and reports all 12 names as missing.
    """
    body = _VALID_YAML.replace(
        "emotions: [neutral, content, excited, sad, angry, scared, happy, curious, sympathetic, surprised, frustrated, melancholic]",
        "emotions: []",
    )
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    msg = str(exc_info.value)
    # All 12 required names should appear in the missing-emotions list.
    for required in (*PRIMARY_EMOTIONS, *SECONDARY_EMOTIONS):
        assert required in msg, f"{required} missing from error message"


# ---------------------------------------------------------------------------
# AC #6 — Reference-integrity check
# ---------------------------------------------------------------------------


def test_dangling_unknown_reference_raises_config_error(tmp_path: Path) -> None:
    """``unknown.maps_to: ghost`` raises ``ConfigError(reference=...)``.

    AC #6 — references must point at first-class emotions; the operator
    needs to know which reference is wrong AND its bogus target.
    """
    body = _VALID_YAML.replace(
        "unknown:\n  maps_to: neutral",
        "unknown:\n  maps_to: ghost",
    )
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    msg = str(exc_info.value)
    assert "unknown.maps_to" in msg
    assert "ghost" in msg


def test_dangling_family_reference_raises_config_error(tmp_path: Path) -> None:
    """A family's ``maps_to: ghost`` raises ``ConfigError`` naming the family.

    AC #6 — fallback_families.<family>.maps_to gets the same check as
    unknown.maps_to.
    """
    family_block = (
        "  high_energy_positive:\n    members: [enthusiastic, gleeful]\n    maps_to: excited"
    )
    assert family_block in _VALID_YAML, "family block layout drifted"
    body = _VALID_YAML.replace(
        family_block,
        family_block.replace("maps_to: excited", "maps_to: ghost"),
    )
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    msg = str(exc_info.value)
    assert "high_energy_positive" in msg
    assert "ghost" in msg


# ---------------------------------------------------------------------------
# AC #7 — Extensibility (architecture's "two-step extension story")
# ---------------------------------------------------------------------------


def test_extensibility_new_emotion_loads(tmp_path: Path) -> None:
    """Appending a new first-class emotion entry loads cleanly.

    AC #7 — architecture.md §"Extensibility — Adding a New
    `speech_emotion` Must Stay Simple" is the architectural promise:
    YAML edit alone makes a new tag first-class. The loader must accept
    the addition with no code changes.

    Post-boundary-repair, "adding a new emotion" is a one-token list
    append — the cleanest possible expression of the extensibility
    contract. (Pre-repair, it required a full ``expression_data:`` block
    with at least one renderer-hint key; that yaml ceremony is gone.)
    """
    body = _VALID_YAML.replace(
        ", melancholic]",
        ", melancholic, serene]",
    )
    yaml_path = _write_yaml(tmp_path, body)
    config = load_from_path(yaml_path)
    assert "serene" in config.emotions


# ---------------------------------------------------------------------------
# AC #8 — vocalization tts_supported strict-bool behavior
# ---------------------------------------------------------------------------


def test_vocalization_tts_supported_typed_as_bool(tmp_path: Path) -> None:
    """A non-bool ``tts_supported`` raises ``ConfigError``.

    Pydantic v2's default mode coerces ``"yes"`` and ``"no"`` to True/False
    for back-compat with YAML 1.1 booleans, but ``"maybe"`` (or an int
    other than 0/1) is rejected. The story spec calls out this behavior:
    if the test discovers pydantic accepts a coercion you didn't expect,
    switch the value to one that's definitely invalid.
    """
    # "maybe" is unambiguously not a bool — pydantic rejects.
    body = _VALID_YAML.replace(
        "laughter: { tts_supported: true }",
        "laughter: { tts_supported: maybe }",
    )
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError):
        load_from_path(yaml_path)
