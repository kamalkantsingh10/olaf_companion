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

_VALID_TOML = 'schema_version = 1\n[audio]\ninput_device_name = "USB.*Mic.*"\n'


def _write_files(
    tmp_path: Path,
    toml_body: str = _VALID_TOML,
    env_body: str = "PICOVOICE_ACCESS_KEY=stub\n",
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
    """A minimal valid pair loads into a SetupConfig with the expected values."""
    toml_path, env_path = _write_files(tmp_path)
    config = load_setup_config(toml_path=toml_path, env_path=env_path)
    assert config.schema_version == 1
    # SecretStr requires explicit unwrap — that's the whole point of using it.
    assert config.picovoice_access_key.get_secret_value() == "stub"
    # Story 1.5: AudioConfig nested model loads from the [audio] block.
    assert config.audio.input_device_name == "USB.*Mic.*"
    assert config.audio.output_device_name is None


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
    # Use a fully valid TOML (with [audio]) so schema_version is the only
    # problem — otherwise pydantic surfaces the missing audio block first
    # and SchemaVersionError never fires.
    bad_toml = 'schema_version = 2\n[audio]\ninput_device_name = "USB.*Mic.*"\n'
    toml_path, env_path = _write_files(tmp_path, toml_body=bad_toml)
    with pytest.raises(SchemaVersionError) as exc_info:
        load_setup_config(toml_path=toml_path, env_path=env_path)
    msg = str(exc_info.value)
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
