"""Contract — ``setup.toml`` schema_version policy after Story 3.4 bump.

After the coordinated 1 → 2 bump, a setup.toml carrying schema_version=1
must be rejected at load time. This is the AC #10 canary that future
loaders will copy.
"""

from pathlib import Path

import pytest

from voice_agent_pipeline.config.setup import load_setup_config
from voice_agent_pipeline.errors import SchemaVersionError

_VALID_BODY_AT_V2 = (
    "schema_version = 2\n"
    "[audio]\n"
    'input_device_name = "USB.*Mic.*"\n'
    'output_device_name = "USB.*Speaker.*"\n'
    "[wakeword]\n"
    'model_path = "models/wakeword/hey_olaf.ppn"\n'
    "[tts]\n"
    'voice_id = "stub-voice-uuid"\n'
)
_VALID_ENV = "PICOVOICE_ACCESS_KEY=stub\nOPENAI_API_KEY=stub\nCARTESIA_API_KEY=stub\n"


def _write(tmp_path: Path, toml_body: str) -> tuple[Path, Path]:
    toml = tmp_path / "setup.toml"
    env = tmp_path / ".env"
    toml.write_text(toml_body)
    env.write_text(_VALID_ENV)
    return toml, env


def test_setup_toml_at_v2_loads_cleanly(tmp_path: Path) -> None:
    """Sanity: the bumped supported version loads."""
    toml, env = _write(tmp_path, _VALID_BODY_AT_V2)
    config = load_setup_config(toml_path=toml, env_path=env)
    assert config.schema_version == 2


def test_setup_toml_at_v1_rejected_after_bump(tmp_path: Path) -> None:
    """A schema_version=1 file is rejected post-Story-3.4."""
    body = _VALID_BODY_AT_V2.replace("schema_version = 2", "schema_version = 1")
    toml, env = _write(tmp_path, body)
    with pytest.raises(SchemaVersionError) as exc_info:
        load_setup_config(toml_path=toml, env_path=env)
    msg = str(exc_info.value)
    assert "1" in msg
    assert "2" in msg
    assert "setup.toml" in msg
