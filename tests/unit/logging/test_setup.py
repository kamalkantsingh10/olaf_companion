"""Tests for :func:`voice_agent_pipeline.logging.setup.configure_logging`.

Covers AC #1, #4-#8 of Story 1.3:

- Three log files exist after setup (#7).
- INFO/WARN/DEBUG land in the right files (#1).
- ``LOG_LEVEL=DEBUG`` opens the debug stream (#3).
- Missing ``event`` field surfaces a CRITICAL bug log (#4).
- ``LOG_CONSOLE`` toggles stdout mirroring (#6).
- Output is JSON with required fields (#5).
- Redaction works end-to-end through the configured pipeline (sanity check
  that :mod:`logging.redaction` is in fact wired into the chain).

All filesystem state lives under ``tmp_path``; structlog/stdlib state is
reset by the autouse fixture in ``conftest.py``.
"""

import json
import logging as _stdlib_logging
from pathlib import Path

import pytest
import structlog
from pydantic import SecretStr

from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.logging.setup import configure_logging


@pytest.fixture
def stub_config() -> SetupConfig:
    """Build a SetupConfig without going through file I/O.

    ``model_construct`` skips validation — fine for these tests since the
    model is unused inside ``configure_logging`` (Story 5.3 is what reads
    fields off it). Using a real load_setup_config call would force the
    test to write a TOML + .env pair that's irrelevant here.
    """
    return SetupConfig.model_construct(
        schema_version=1,
        picovoice_access_key=SecretStr("stub"),
    )


def _read_json_lines(path: Path) -> list[dict[str, object]]:
    """Read a log file and return its lines as parsed JSON dicts.

    Skips blank lines and lines that don't parse cleanly. The bypass-CRITICAL
    line written by ``_require_event_field`` is plain text (not JSON) and
    will be skipped here — tests that need to check it read the raw file.
    """
    if not path.exists():
        return []
    text = path.read_text().strip()
    if not text:
        return []
    out: list[dict[str, object]] = []
    for line in text.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _flush_handlers() -> None:
    """Force every root-logger handler to flush.

    RotatingFileHandler is line-buffered, but pytest sometimes asserts on the
    file before the buffer drains naturally. Calling flush() removes the race.
    """
    for h in _stdlib_logging.getLogger().handlers:
        h.flush()


def test_three_log_files_created(stub_config: SetupConfig, tmp_path: Path) -> None:
    """All three rotation streams exist after the first ``log.info`` call."""
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("test")
    log.info("startup.completed")
    _flush_handlers()
    assert (base / "voice-agent.log").exists()
    assert (base / "errors.log").exists()
    assert (base / "debug.log").exists()


def test_info_log_appears_in_voice_agent_log_only(stub_config: SetupConfig, tmp_path: Path) -> None:
    """INFO routing: voice-agent.log captures the line, errors.log does not."""
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("test")
    log.info("info.event")
    _flush_handlers()

    info_lines = _read_json_lines(base / "voice-agent.log")
    error_lines = _read_json_lines(base / "errors.log")

    assert any(line.get("event") == "info.event" for line in info_lines)
    assert all(line.get("event") != "info.event" for line in error_lines)


def test_warn_log_appears_in_both(stub_config: SetupConfig, tmp_path: Path) -> None:
    """WARN routing: lands in BOTH voice-agent.log AND errors.log."""
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("test")
    log.warning("warn.event")
    _flush_handlers()

    info_lines = _read_json_lines(base / "voice-agent.log")
    error_lines = _read_json_lines(base / "errors.log")

    assert any(line.get("event") == "warn.event" for line in info_lines)
    assert any(line.get("event") == "warn.event" for line in error_lines)


def test_debug_log_only_in_debug_log_when_log_level_debug(
    stub_config: SetupConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEBUG routing under ``LOG_LEVEL=DEBUG``: lands in debug.log ONLY."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("test")
    log.debug("debug.event")
    _flush_handlers()

    info_lines = _read_json_lines(base / "voice-agent.log")
    debug_lines = _read_json_lines(base / "debug.log")

    assert any(line.get("event") == "debug.event" for line in debug_lines)
    assert all(line.get("event") != "debug.event" for line in info_lines)


def test_missing_event_field_emits_critical(stub_config: SetupConfig, tmp_path: Path) -> None:
    """A log call with no ``event`` field surfaces a CRITICAL bug message in errors.log.

    See ``_require_event_field`` in logging/setup.py for why this routes
    through stdlib directly rather than raising.
    """
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("test")
    log.info(some_msg="no event provided")
    _flush_handlers()

    error_text = (base / "errors.log").read_text()
    assert "logging.missing_event_bug" in error_text


def test_log_console_true_mirrors_to_stdout(
    stub_config: SetupConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``LOG_CONSOLE=true`` mirrors the JSON log line to stdout in addition to files."""
    monkeypatch.setenv("LOG_CONSOLE", "true")
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("test")
    log.info("console.event")
    _flush_handlers()
    out = capsys.readouterr().out
    assert "console.event" in out


def test_log_console_false_silent_stdout(
    stub_config: SetupConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default (``LOG_CONSOLE`` unset) keeps stdout silent — production posture."""
    monkeypatch.delenv("LOG_CONSOLE", raising=False)
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("test")
    log.info("silent.event")
    _flush_handlers()
    out = capsys.readouterr().out
    assert "silent.event" not in out


def test_json_shape_includes_required_fields(stub_config: SetupConfig, tmp_path: Path) -> None:
    """Every emitted line carries timestamp, level, event, logger, plus caller kwargs."""
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("voice_agent_pipeline.test")
    log.info("shape.check", session_id="abc-123")
    _flush_handlers()

    lines = _read_json_lines(base / "voice-agent.log")
    matching = [line for line in lines if line.get("event") == "shape.check"]
    assert matching, f"no matching log line: {lines}"
    record = matching[0]
    assert "timestamp" in record
    assert record.get("level") == "info"
    assert record.get("event") == "shape.check"
    assert record.get("logger") == "voice_agent_pipeline.test"
    assert record.get("session_id") == "abc-123"


def test_secret_keys_redacted_in_output(stub_config: SetupConfig, tmp_path: Path) -> None:
    """End-to-end sanity: redaction is wired into the configured pipeline.

    Asserts that secret-shaped keys never reach disk while innocent
    bystanders (``session_id``) survive.
    """
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("test")
    log.info(
        "auth.attempt",
        cartesia_api_key="sk_xxx",
        bearer_token="abc",  # noqa: S106  -- ruff can't tell this is fake test data
        session_id="ok-123",
    )
    _flush_handlers()
    text = (base / "voice-agent.log").read_text()
    assert "sk_xxx" not in text
    # "abc" is too short to be unique — assert the full innocent value DID
    # land, which implies redaction touched ONLY the secret-shaped keys.
    assert "ok-123" in text


def test_audio_bytes_redacted_in_output(stub_config: SetupConfig, tmp_path: Path) -> None:
    """End-to-end sanity: opaque audio buffers never reach disk, even at INFO."""
    base = tmp_path / "logs"
    configure_logging(stub_config, base_path=base)
    log = structlog.get_logger("test")
    log.info("audio.captured", audio_bytes="\\x00\\x01STAYOUT", duration_ms=100)
    _flush_handlers()
    text = (base / "voice-agent.log").read_text()
    assert "STAYOUT" not in text
    assert "duration_ms" in text
