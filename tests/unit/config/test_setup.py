"""Tests for :func:`voice_agent_pipeline.config.setup.load_setup_config`.

Covers all six failure paths called out in Story 1.2's AC #5-#8 plus #9
(loose-perms WARN), plus the happy path. All filesystem state is built
under pytest's ``tmp_path`` — no test ever touches the project's real
``setup.toml`` or ``.env``.
"""

import logging
import os
import sys
from pathlib import Path

import pytest

from voice_agent_pipeline.config.setup import load_setup_config
from voice_agent_pipeline.errors import ConfigError, SchemaVersionError

_VALID_TOML = (
    "schema_version = 1\n"
    "[audio]\n"
    'input_device_name = "USB.*Mic.*"\n'
    'output_device_name = "USB.*Speaker.*"\n'
    "[wakeword]\n"
    'model_path = "models/wakeword/hey_olaf.ppn"\n'
    "sensitivity = 0.5\n"
)


_VALID_ENV = (
    "PICOVOICE_ACCESS_KEY=stub\n"
    # Story 2.2 added three Talker provider keys, all optional in the
    # SetupConfig (the factory enforces "matching provider has a key").
    # The default test provider is "openai", so most tests need
    # OPENAI_API_KEY present; provider-specific tests override env_body.
    "OPENAI_API_KEY=stub-openai\n"
)


def _write_files(
    tmp_path: Path,
    toml_body: str = _VALID_TOML,
    env_body: str = _VALID_ENV,
    env_mode: int = 0o600,
) -> tuple[Path, Path]:
    """Write a ``setup.toml`` + ``.env`` pair under ``tmp_path``.

    Args:
        tmp_path: pytest fixture providing a unique per-test directory.
        toml_body: TOML file content (defaults to a minimal valid file).
        env_body: ``.env`` file content (defaults to a minimal valid file).
        env_mode: POSIX mode bits to chmod the ``.env`` file to. Skipped on
            Windows where ``os.chmod`` semantics differ.

    Returns:
        Two-tuple ``(toml_path, env_path)``.
    """
    toml_path = tmp_path / "setup.toml"
    env_path = tmp_path / ".env"
    toml_path.write_text(toml_body)
    env_path.write_text(env_body)
    if sys.platform != "win32":
        os.chmod(env_path, env_mode)
    return toml_path, env_path


def test_load_happy_path(tmp_path: Path) -> None:
    """A minimal valid pair loads into a SetupConfig with the expected values.

    This is the canary for the whole loader — covers TOML parsing, env-var
    substitution, schema_version pass-through, and every nested-config
    block landed so far. Each new story that adds a `[<block>]` to
    SetupConfig should extend the asserts at the bottom of this test.
    """
    toml_path, env_path = _write_files(tmp_path)
    config = load_setup_config(toml_path=toml_path, env_path=env_path)
    assert config.schema_version == 1
    # SecretStr requires explicit unwrap — that's the whole point of using it.
    # If you ever see a test asserting on `str(config.picovoice_access_key)`,
    # that's a bug: SecretStr renders as `**********` in str/repr by design.
    assert config.picovoice_access_key.get_secret_value() == "stub"
    # Story 1.5: AudioConfig nested model loads from the [audio] block.
    # Story 2.1: output_device_name is now required (speaker output landed).
    assert config.audio.input_device_name == "USB.*Mic.*"
    assert config.audio.output_device_name == "USB.*Speaker.*"
    # Story 2.2: three optional provider keys. Default test env sets
    # OPENAI_API_KEY only; the other two are None unless a test sets them.
    assert config.openai_api_key is not None
    assert config.openai_api_key.get_secret_value() == "stub-openai"
    assert config.groq_api_key is None
    assert config.gemini_api_key is None
    # Story 2.2: TalkerConfig nested defaults — the [talker] block is
    # optional (default_factory=TalkerConfig), so a TOML without it
    # picks up the architecture defaults across all sub-blocks.
    assert config.talker.provider == "openai"
    assert config.talker.max_tokens == 512
    assert str(config.talker.system_prompt_path) == "prompts/talker_system.md"
    assert config.talker.openai.model == "gpt-5.4-nano"
    assert config.talker.groq.model == "llama-3.1-8b-instant"
    assert config.talker.gemini.model == "gemini-2.5-flash"
    # Story 1.6: WakewordConfig nested model loads from the [wakeword] block.
    # model_path is parsed as pathlib.Path (TOML strings → Path coercion);
    # str() round-trip below is the cheap way to assert without depending
    # on platform-specific path normalization (we just want "did it land").
    assert str(config.wakeword.model_path) == "models/wakeword/hey_olaf.ppn"
    assert config.wakeword.sensitivity == 0.5


def test_wakeword_block_extra_key_rejected(tmp_path: Path) -> None:
    """Story 1.6 AC #4: extra='forbid' applies to the nested WakewordConfig too.

    A typo in the TOML (e.g. `sensitivty = 0.5` for `sensitivity`) must
    fail at startup, not silently fall through to the default and ship
    a misconfigured wake-word. Pydantic raises ValidationError; our
    loader wraps it as ConfigError per the project's error hierarchy.
    """
    bad_toml = (
        "schema_version = 1\n"
        "[audio]\n"
        'input_device_name = "USB.*Mic.*"\n'
        "[wakeword]\n"
        'model_path = "models/wakeword/hey_olaf.ppn"\n'
        "sensitivity = 0.5\n"
        # Deliberate unknown key — should make the loader bail rather than
        # silently treating it as unrelated metadata.
        'unknown_wakeword_field = "x"\n'
    )
    toml_path, env_path = _write_files(tmp_path, toml_body=bad_toml)
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    # Assert the operator-facing message names the offender so they can fix
    # it without spelunking through a stack trace.
    assert "unknown_wakeword_field" in str(exc_info.value)


def test_wakeword_sensitivity_out_of_range_rejected(tmp_path: Path) -> None:
    """Story 1.6: sensitivity must lie in [0.0, 1.0] per Porcupine's API contract.

    pydantic's `Field(ge=0.0, le=1.0)` enforces the bounds at parse time,
    which means we catch a bad sensitivity *before* opening the audio
    pipeline — same fail-fast posture as the rest of the loader. If we
    deferred validation to Porcupine itself, the operator would see a
    cryptic native-code error message instead of a clean ConfigError.
    """
    bad_toml = (
        "schema_version = 1\n"
        "[audio]\n"
        'input_device_name = "USB.*Mic.*"\n'
        "[wakeword]\n"
        'model_path = "models/wakeword/hey_olaf.ppn"\n'
        # 2.5 is out of [0.0, 1.0] — pydantic should reject.
        "sensitivity = 2.5\n"
    )
    toml_path, env_path = _write_files(tmp_path, toml_body=bad_toml)
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    # Lowercase to be robust against pydantic's preferred capitalization.
    assert "sensitivity" in str(exc_info.value).lower()


def test_wakeword_sensitivity_default(tmp_path: Path) -> None:
    """Omitting `sensitivity` falls back to the documented default of 0.5.

    The default lives in WakewordConfig as `Field(default=0.5, ge=..., le=...)`
    — operators with a working setup don't have to memorize the threshold;
    they only override when Story 5.5's soak says so.
    """
    toml_with_default = (
        "schema_version = 1\n"
        "[audio]\n"
        'input_device_name = "USB.*Mic.*"\n'
        # Story 2.1: output_device_name is now required.
        'output_device_name = "USB.*Speaker.*"\n'
        "[wakeword]\n"
        # Note: NO sensitivity line — should pick up the default.
        'model_path = "models/wakeword/hey_olaf.ppn"\n'
    )
    toml_path, env_path = _write_files(tmp_path, toml_body=toml_with_default)
    config = load_setup_config(toml_path=toml_path, env_path=env_path)
    assert config.wakeword.sensitivity == 0.5


def test_missing_wakeword_block_rejected(tmp_path: Path) -> None:
    """Omitting `[wakeword]` entirely raises ConfigError naming the block.

    `wakeword: WakewordConfig` is a required field on SetupConfig (no
    default factory) — the wake-word gate is non-optional in v1. If/when
    a future "headless" mode lands, this assertion needs to flip.
    """
    toml_no_wakeword = (
        'schema_version = 1\n[audio]\ninput_device_name = "USB.*Mic.*"\n'
        # No [wakeword] block at all.
    )
    toml_path, env_path = _write_files(tmp_path, toml_body=toml_no_wakeword)
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    # pydantic surfaces missing required fields by name; case-insensitive
    # check makes the assertion robust to phrasing changes.
    assert "wakeword" in str(exc_info.value).lower()


def test_audio_block_extra_key_rejected(tmp_path: Path) -> None:
    """Story 1.5 AC #3: `extra='forbid'` applies to the nested AudioConfig too."""
    bad_toml = (
        'schema_version = 1\n[audio]\ninput_device_name = "USB.*Mic.*"\nunknown_audio_field = 42\n'
    )
    toml_path, env_path = _write_files(tmp_path, toml_body=bad_toml)
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    assert "unknown_audio_field" in str(exc_info.value)


def test_audio_block_missing_input_name_rejected(tmp_path: Path) -> None:
    """Story 1.5 AC #3: `input_device_name` is required."""
    bad_toml = "schema_version = 1\n[audio]\n"
    toml_path, env_path = _write_files(tmp_path, toml_body=bad_toml)
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    assert "input_device_name" in str(exc_info.value)


def test_talker_block_overrides_loaded(tmp_path: Path) -> None:
    """Story 2.2: explicit [talker] + sub-blocks override the defaults.

    The defaults are validated in :func:`test_load_happy_path`; this test
    proves that operators can override them per-machine in setup.toml,
    including the per-provider model sub-blocks.
    """
    toml_with_talker = (
        "schema_version = 1\n"
        "[audio]\n"
        'input_device_name = "USB.*Mic.*"\n'
        'output_device_name = "USB.*Speaker.*"\n'
        "[wakeword]\n"
        'model_path = "models/wakeword/hey_olaf.ppn"\n'
        "[talker]\n"
        'provider = "groq"\n'
        "max_tokens = 1024\n"
        'system_prompt_path = "prompts/custom.md"\n'
        "[talker.openai]\n"
        'model = "gpt-5-mini"\n'
        "[talker.groq]\n"
        'model = "llama-3.3-70b-versatile"\n'
    )
    # Provider is "groq" so the test puts GROQ_API_KEY in env (and
    # OPENAI_API_KEY absent — proves the loader is happy with whichever
    # subset of provider keys are present).
    env_body = "PICOVOICE_ACCESS_KEY=stub\nGROQ_API_KEY=stub-groq\n"
    toml_path, env_path = _write_files(tmp_path, toml_body=toml_with_talker, env_body=env_body)
    config = load_setup_config(toml_path=toml_path, env_path=env_path)
    assert config.talker.provider == "groq"
    assert config.talker.max_tokens == 1024
    assert str(config.talker.system_prompt_path) == "prompts/custom.md"
    assert config.talker.openai.model == "gpt-5-mini"
    assert config.talker.groq.model == "llama-3.3-70b-versatile"
    # Gemini sub-block left at default.
    assert config.talker.gemini.model == "gemini-2.5-flash"
    assert config.openai_api_key is None
    assert config.groq_api_key is not None
    assert config.groq_api_key.get_secret_value() == "stub-groq"


def test_talker_max_tokens_must_be_positive(tmp_path: Path) -> None:
    """Story 2.2: ``max_tokens = 0`` (or negative) is rejected at parse time.

    Pydantic enforces ``Field(gt=0)`` at validation time so misconfigured
    Talker max_tokens fails startup rather than silently failing every
    Anthropic call with a 400.
    """
    bad_toml = (
        "schema_version = 1\n"
        "[audio]\n"
        'input_device_name = "USB.*Mic.*"\n'
        'output_device_name = "USB.*Speaker.*"\n'
        "[wakeword]\n"
        'model_path = "models/wakeword/hey_olaf.ppn"\n'
        "[talker]\n"
        # 0 is non-positive — pydantic should reject.
        "max_tokens = 0\n"
    )
    toml_path, env_path = _write_files(tmp_path, toml_body=bad_toml)
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    assert "max_tokens" in str(exc_info.value).lower()


def test_talker_block_extra_key_rejected(tmp_path: Path) -> None:
    """Story 2.2: ``extra='forbid'`` applies to the nested TalkerConfig too."""
    bad_toml = (
        "schema_version = 1\n"
        "[audio]\n"
        'input_device_name = "USB.*Mic.*"\n'
        'output_device_name = "USB.*Speaker.*"\n'
        "[wakeword]\n"
        'model_path = "models/wakeword/hey_olaf.ppn"\n'
        "[talker]\n"
        'unknown_talker_field = "x"\n'
    )
    toml_path, env_path = _write_files(tmp_path, toml_body=bad_toml)
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    assert "unknown_talker_field" in str(exc_info.value)


def test_all_talker_keys_optional_at_load_time(tmp_path: Path) -> None:
    """Story 2.2: SetupConfig accepts all-three-Talker-keys-missing.

    The factory ``build_talker`` (in turn/__init__.py) is what enforces
    "the active provider's key must be present"; the loader itself
    accepts any combination of the three keys, including none. Tests
    for the factory's missing-key handling live in tests/unit/turn/.
    """
    # Only PICOVOICE present — no Talker keys at all.
    toml_path, env_path = _write_files(tmp_path, env_body="PICOVOICE_ACCESS_KEY=stub\n")
    config = load_setup_config(toml_path=toml_path, env_path=env_path)
    assert config.openai_api_key is None
    assert config.groq_api_key is None
    assert config.gemini_api_key is None


def test_audio_block_missing_output_name_rejected(tmp_path: Path) -> None:
    """Story 2.1: `output_device_name` is required now that speaker output is wired.

    Before Story 2.1, ``output_device_name`` was ``str | None`` because the
    pipeline only opened the mic side. Story 2.1 wires ``transport.output()``
    + the test-tone smoke check, so the field is required from this story
    onward — a config without a speaker regex cannot start.
    """
    bad_toml = (
        "schema_version = 1\n"
        "[audio]\n"
        # Note: input present, output deliberately missing.
        'input_device_name = "USB.*Mic.*"\n'
        "[wakeword]\n"
        'model_path = "models/wakeword/hey_olaf.ppn"\n'
    )
    toml_path, env_path = _write_files(tmp_path, toml_body=bad_toml)
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    assert "output_device_name" in str(exc_info.value)


def test_missing_audio_block_rejected(tmp_path: Path) -> None:
    """Omitting the `[audio]` block entirely raises ConfigError naming it."""
    toml_path, env_path = _write_files(tmp_path, toml_body="schema_version = 1\n")
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    assert "audio" in str(exc_info.value).lower()


def test_missing_schema_version_raises(tmp_path: Path) -> None:
    """An empty TOML missing ``schema_version`` raises ConfigError naming the field."""
    toml_path, env_path = _write_files(tmp_path, toml_body="")
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    assert "schema_version" in str(exc_info.value)


def test_extra_key_raises(tmp_path: Path) -> None:
    """An unknown TOML key raises ConfigError naming the offender (extra='forbid')."""
    toml_path, env_path = _write_files(
        tmp_path,
        toml_body="schema_version = 1\nunknown_key = 42\n",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    assert "unknown_key" in str(exc_info.value)


def test_missing_env_var_raises(tmp_path: Path) -> None:
    """A ``.env`` missing the required PICOVOICE_ACCESS_KEY raises ConfigError.

    Asserts on the lowercased message because pydantic surfaces the field
    name via ``picovoice_access_key`` (the python attribute), not the env
    var name. Lowercasing makes the assertion robust to either rendering.
    """
    # Use the full valid TOML so the missing env var is the only error.
    toml_path, env_path = _write_files(tmp_path, env_body="")
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    assert "picovoice_access_key" in str(exc_info.value).lower()


def test_missing_toml_file_raises(tmp_path: Path) -> None:
    """A nonexistent TOML path raises ConfigError naming the missing file."""
    _, env_path = _write_files(tmp_path)
    missing_toml = tmp_path / "does_not_exist.toml"
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=missing_toml, env_path=env_path)
    assert "does_not_exist.toml" in str(exc_info.value)


def test_missing_env_file_raises(tmp_path: Path) -> None:
    """A nonexistent ``.env`` path raises ConfigError naming the missing file."""
    toml_path, _ = _write_files(tmp_path)
    missing_env = tmp_path / "does_not_exist.env"
    with pytest.raises(ConfigError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=missing_env)
    assert "does_not_exist.env" in str(exc_info.value)


def test_unsupported_schema_version_raises(tmp_path: Path) -> None:
    """A schema_version not equal to ``SUPPORTED_SCHEMA_VERSION`` raises SchemaVersionError.

    The error message must surface both versions and the source name —
    this is the AC #8 contract.
    """
    # Use a fully valid TOML (with [audio] AND [wakeword]) so schema_version
    # is the only problem. Otherwise pydantic surfaces the missing nested
    # blocks *first* and we never get to the schema_version policy check.
    # Each new story that lands a required nested block must extend this
    # TOML to keep the test focused on its contract (schema_version policy).
    bad_toml = (
        "schema_version = 2\n"
        "[audio]\n"
        'input_device_name = "USB.*Mic.*"\n'
        # Story 2.1 made output_device_name required; include it so
        # pydantic validation passes and the schema_version policy check
        # is the only path that can fire.
        'output_device_name = "USB.*Speaker.*"\n'
        "[wakeword]\n"
        'model_path = "models/wakeword/hey_olaf.ppn"\n'
    )
    toml_path, env_path = _write_files(tmp_path, toml_body=bad_toml)
    with pytest.raises(SchemaVersionError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    msg = str(exc_info.value)
    # The error message must surface BOTH versions and the source name —
    # this is the AC #8 contract from Story 1.2 and survives every later
    # story that touches the loader.
    assert "2" in msg
    assert "1" in msg
    assert "setup.toml" in msg


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX perms only")
def test_loose_env_perms_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A ``.env`` with looser-than-0600 perms emits a WARN but does NOT raise.

    NFR23 is advisory in v1: warn loudly, never block startup. The WARN
    carries ``actual_mode`` and ``recommended`` fields so the operator
    can see exactly what to ``chmod`` to.
    """
    toml_path, env_path = _write_files(tmp_path, env_mode=0o644)
    with caplog.at_level(logging.WARNING, logger="voice_agent_pipeline.config.setup"):
        load_setup_config(toml_path=toml_path, env_path=env_path)
    matching = [r for r in caplog.records if r.message == "config.env.permissions_loose"]
    assert matching, f"expected loose-perms warning, got: {[r.message for r in caplog.records]}"
    rec = matching[0]
    assert getattr(rec, "actual_mode", None) == "0o644"
    assert getattr(rec, "recommended", None) == "0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX perms only")
def test_correct_env_perms_does_not_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The loose-perms WARN must NOT fire for a correctly-chmodded ``.env``."""
    toml_path, env_path = _write_files(tmp_path, env_mode=0o600)
    with caplog.at_level(logging.WARNING, logger="voice_agent_pipeline.config.setup"):
        load_setup_config(toml_path=toml_path, env_path=env_path)
    matching = [r for r in caplog.records if r.message == "config.env.permissions_loose"]
    assert not matching
