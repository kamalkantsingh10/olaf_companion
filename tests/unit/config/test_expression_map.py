# ruff: noqa: E501
# YAML fixture lines naturally exceed 100 chars (mapping shorthand on a
# single line is more readable than block-style for these test bodies).
"""Tests for :mod:`voice_agent_pipeline.config.expression_map`.

Mirrors Story 1.2's ``test_setup.py`` pattern (tmp-path-only — no test
reads the project's real ``expression_map.yaml`` except the canary
``test_load_real_project_map_succeeds`` which validates the committed
file as a regression check).

Each test exercises one acceptance criterion from Story 3.1 — the file
covers AC #3 (parse / pydantic failures), AC #4 (schema_version
mismatch), AC #5 (completeness), AC #6 (reference integrity), AC #7
(extensibility), and AC #8 (the test surface itself).
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

# Minimal-but-complete YAML covering all 12 emotions + 4 vocalizations +
# 1 family + unknown. Tests that need to mutate just one field do string
# surgery (mirrors Story 1.2's _VALID_TOML pattern).
_VALID_YAML = dedent(
    """\
    schema_version: 2
    emotions:
      neutral:
        expression_data: { base_pose: { yaw: 0, pitch: 0 }, eye_state: open, led_color: "#ffffff", led_intensity: 0.4 }
      content:
        expression_data: { base_pose: { yaw: 0, pitch: 0 }, eye_state: open, led_color: "#a0e0a0", led_intensity: 0.5 }
      excited:
        expression_data: { base_pose: { yaw: 0, pitch: 5 }, eye_state: wide, led_color: "#ffa040", led_intensity: 0.9 }
      sad:
        expression_data: { base_pose: { yaw: 0, pitch: -10 }, eye_state: squint, led_color: "#4060a0", led_intensity: 0.3 }
      angry:
        expression_data: { base_pose: { yaw: 0, pitch: -3 }, eye_state: squint, led_color: "#a02020", led_intensity: 0.8 }
      scared:
        expression_data: { base_pose: { yaw: -5, pitch: 0 }, eye_state: wide, led_color: "#a040a0", led_intensity: 0.7 }
      happy:
        expression_data: { base_pose: { yaw: 0, pitch: 3 }, eye_state: open, led_color: "#ffd060", led_intensity: 0.7 }
      curious:
        expression_data: { base_pose: { yaw: 5, pitch: 2 }, eye_state: open, led_color: "#60c0e0", led_intensity: 0.6 }
      sympathetic:
        expression_data: { base_pose: { yaw: 0, pitch: -2 }, eye_state: open, led_color: "#c0a0e0", led_intensity: 0.4 }
      surprised:
        expression_data: { base_pose: { yaw: 0, pitch: 5 }, eye_state: wide, led_color: "#ffff80", led_intensity: 0.8 }
      frustrated:
        expression_data: { base_pose: { yaw: 0, pitch: -2 }, eye_state: squint, led_color: "#e07040", led_intensity: 0.6 }
      melancholic:
        expression_data: { base_pose: { yaw: 0, pitch: -8 }, eye_state: squint, led_color: "#506080", led_intensity: 0.3 }
    vocalizations:
      laughter: { tts_supported: true }
      sigh: { tts_supported: false }
      gasp: { tts_supported: false }
      clears_throat: { tts_supported: false }
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


def test_schema_version_constant_is_2() -> None:
    """``EXPRESSION_MAP_SCHEMA_VERSION`` is the int ``2`` per AC #2.

    Story 3.4 will bump the global ``SUPPORTED_SCHEMA_VERSION`` to match;
    this story keeps the constant module-local so the bump is decoupled
    from ``setup.toml``'s loader.
    """
    assert EXPRESSION_MAP_SCHEMA_VERSION == 2


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
    assert config.schema_version == 2
    # All 12 emotions present.
    assert set(config.emotions) == set(PRIMARY_EMOTIONS) | set(SECONDARY_EMOTIONS)
    # All 4 vocalizations present.
    assert set(config.vocalizations) == {"laughter", "sigh", "gasp", "clears_throat"}
    assert config.vocalizations["laughter"].tts_supported is True
    assert config.vocalizations["sigh"].tts_supported is False
    # Fallback family + unknown wired up.
    assert "high_energy_positive" in config.fallback_families
    assert config.fallback_families["high_energy_positive"].maps_to == "excited"
    assert "enthusiastic" in config.fallback_families["high_energy_positive"].members
    assert config.unknown.maps_to == "neutral"
    # expression_data is opaque dict[str, Any] — should round-trip with
    # whatever the YAML had.
    assert config.emotions["excited"].expression_data["led_intensity"] == 0.9


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
    body = _VALID_YAML.replace("vocalizations:\n", "")
    # Also strip the four child entries that lived under it.
    for tag in ("laughter", "sigh", "gasp", "clears_throat"):
        body = body.replace(f"  {tag}: {{ tts_supported: true }}\n", "")
        body = body.replace(f"  {tag}: {{ tts_supported: false }}\n", "")
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    assert "vocalizations" in str(exc_info.value)


def test_extra_key_at_nested_level_raises_config_error(tmp_path: Path) -> None:
    """``extra="forbid"`` catches typos at any nested level.

    AC #3 — ``emotions.content.bogus_key`` violates ``EmotionEntry``'s
    forbid rule. Pydantic surfaces the offending key in the error.
    """
    # Append a stray key to the content emotion's entry. The YAML's
    # indentation is 2 spaces under emotions:, then 4 spaces for
    # expression_data — so a sibling key sits at 4 spaces too.
    needle = "led_intensity: 0.5 }"
    assert _VALID_YAML.count(needle) == 1, "needle must be unique for safe surgery"
    body = _VALID_YAML.replace(needle, "led_intensity: 0.5 }\n    bogus_key: 1", 1)
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    assert "bogus_key" in str(exc_info.value)


def test_wrong_type_raises_config_error(tmp_path: Path) -> None:
    """Replacing ``expression_data`` (mapping) with a string raises.

    AC #3 — pydantic's type coercion fails; the wrap surfaces the
    validation message.
    """
    body = _VALID_YAML.replace(
        'expression_data: { base_pose: { yaw: 0, pitch: 0 }, eye_state: open, led_color: "#a0e0a0", led_intensity: 0.5 }',
        'expression_data: "string instead of mapping"',
        1,  # only the first occurrence (content) — keep others valid
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
    ``config.version.assert_schema_version(supported=2, ...)`` so the
    helper isn't duplicated here.
    """
    body = _VALID_YAML.replace("schema_version: 2", "schema_version: 1")
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(SchemaVersionError) as exc_info:
        load_from_path(yaml_path)
    msg = str(exc_info.value)
    assert "expression_map.yaml" in msg
    assert "1" in msg
    assert "2" in msg


# ---------------------------------------------------------------------------
# AC #5 — Completeness check
# ---------------------------------------------------------------------------


def test_missing_primary_emotion_raises_config_error(tmp_path: Path) -> None:
    """Dropping ``excited`` raises ``ConfigError`` listing it as missing.

    AC #5 — FR20's "no silent gaps" is enforced for both primary and
    secondary emotion sets.
    """
    # Remove the 2-line excited block (the key line + its
    # expression_data line). 2-space outer indent / 4-space inner.
    excited_block = (
        "  excited:\n"
        '    expression_data: { base_pose: { yaw: 0, pitch: 5 }, eye_state: wide, led_color: "#ffa040", led_intensity: 0.9 }\n'
    )
    assert excited_block in _VALID_YAML, "excited block layout drifted"
    body = _VALID_YAML.replace(excited_block, "")
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    assert "excited" in str(exc_info.value)


def test_missing_secondary_emotion_raises_config_error(tmp_path: Path) -> None:
    """Dropping ``melancholic`` raises ``ConfigError`` listing it.

    AC #5 — completeness covers secondary emotions too.
    """
    melancholic_block = (
        "  melancholic:\n"
        '    expression_data: { base_pose: { yaw: 0, pitch: -8 }, eye_state: squint, led_color: "#506080", led_intensity: 0.3 }\n'
    )
    assert melancholic_block in _VALID_YAML, "melancholic block layout drifted"
    body = _VALID_YAML.replace(melancholic_block, "")
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    assert "melancholic" in str(exc_info.value)


def test_empty_expression_data_raises_config_error(tmp_path: Path) -> None:
    """An empty ``expression_data: {}`` raises ``ConfigError(emotion=...)``.

    AC #5 — the FR20 "every emotion has expression_data" check covers
    both presence and non-emptiness.
    """
    content_block = (
        "  content:\n"
        '    expression_data: { base_pose: { yaw: 0, pitch: 0 }, eye_state: open, led_color: "#a0e0a0", led_intensity: 0.5 }'
    )
    assert content_block in _VALID_YAML, "content block layout drifted"
    body = _VALID_YAML.replace(
        content_block,
        "  content:\n    expression_data: {}",
    )
    yaml_path = _write_yaml(tmp_path, body)
    with pytest.raises(ConfigError) as exc_info:
        load_from_path(yaml_path)
    assert "content" in str(exc_info.value)


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
    # Family entries are at 4-space indent: 2 for the family name,
    # 4 for members/maps_to.
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
    """
    body = _VALID_YAML + dedent(
        """\
          serene:
            expression_data: { base_pose: { yaw: 0, pitch: 0 }, eye_state: open, led_color: "#80c0e0", led_intensity: 0.3 }
        """
    )
    # The serene block needs to land under emotions:, not at the top
    # level — re-construct properly by inserting before vocalizations:.
    body = _VALID_YAML.replace(
        "vocalizations:",
        '  serene:\n        expression_data: { base_pose: { yaw: 0, pitch: 0 }, eye_state: open, led_color: "#80c0e0", led_intensity: 0.3 }\nvocalizations:',
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
