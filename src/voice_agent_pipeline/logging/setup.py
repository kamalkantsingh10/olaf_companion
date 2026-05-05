"""structlog + stdlib :mod:`logging` + ``RotatingFileHandler`` wiring.

:func:`configure_logging` is the **one-shot setup entry point** called from
``__main__.py`` immediately after :func:`load_setup_config` succeeds. From
that call onward, every ``structlog.get_logger(...)`` call in the codebase
emits JSON-serialized lines into one or more of three rotating files:

- ``./logs/voice-agent.log`` — INFO and above (the day-to-day app log).
- ``./logs/errors.log``      — WARNING and above (fast post-mortem scan).
- ``./logs/debug.log``       — DEBUG only (opt-in via ``LOG_LEVEL=DEBUG``).

The split is deliberate: errors.log stays uncluttered; debug.log captures the
full DEBUG-only stream including transcripts (FR39, gated by the redaction
processor); voice-agent.log is the developer's ``tail -f`` target.

Story 5.3 makes the rotation/retention/console knobs read from
``setup.toml``. This story hard-codes the defaults — see
:data:`_MAX_BYTES` / :data:`_BACKUP_COUNT`.
"""

import json
import logging as _stdlib_logging
import sys
from logging.handlers import RotatingFileHandler
from os import environ
from pathlib import Path
from typing import Any

import structlog
from structlog.types import EventDict

from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.logging.redaction import redact_sensitive_fields

# Per-file rotation cap. 50 MB per file x 7 backups ≈ ~350 MB worst-case
# steady-state per stream — fits comfortably on every target host. Story 5.3
# will let users tune these via ``[logging]`` in ``setup.toml``.
_MAX_BYTES = 50 * 1024 * 1024
_BACKUP_COUNT = 7

# Whitelist of accepted LOG_LEVEL values. An invalid LOG_LEVEL silently
# falls back to INFO rather than crashing — operator typos shouldn't take
# the pipeline down.
_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# A dedicated stdlib logger for the missing-event bypass path. Using a fixed
# name (rather than ``__name__``) keeps the bypass identifiable in log search
# tooling regardless of which module the bug originated from.
_meta_logger = _stdlib_logging.getLogger("voice_agent_pipeline._missing_event_bug")


def configure_logging(config: SetupConfig, *, base_path: Path = Path("logs")) -> None:
    """Wire structlog → stdlib :mod:`logging` → three :class:`RotatingFileHandler`s.

    Idempotent at the *handler* level: each call clears the root logger's
    handlers first, so test fixtures can call this repeatedly without piling
    up duplicate streams.

    Args:
        config: Validated :class:`SetupConfig`. Currently unused — accepted
            now for API stability so Story 5.3 can read its
            ``[logging]`` block here without changing every callsite.
        base_path: Directory to write the three log files into. Defaults to
            ``Path("logs")`` for production; tests pass a ``tmp_path`` to
            isolate filesystem state.
    """
    # Discard the parameter explicitly (rather than silencing the linter)
    # to make "we'll consume this in 5.3" obvious to a reader.
    del config

    # Resolve LOG_LEVEL from the environment. Anything unexpected falls back
    # to INFO to keep the pipeline resilient against operator typos.
    log_level_name = environ.get("LOG_LEVEL", "INFO").upper()
    if log_level_name not in _VALID_LEVELS:
        log_level_name = "INFO"
    log_level = getattr(_stdlib_logging, log_level_name)

    # LOG_CONSOLE is read once at configure time. If the operator changes the
    # env var while the process runs, we don't pick that up — by design, log
    # routing should not flip mid-run.
    log_console = environ.get("LOG_CONSOLE", "false").lower() == "true"

    # Ensure the log directory exists. ``parents=True`` covers the case where
    # someone passes ``tmp_path / "deeper/logs"``.
    base_path.mkdir(parents=True, exist_ok=True)

    root = _stdlib_logging.getLogger()
    # Wipe any inherited handlers so test fixtures get a clean slate. This is
    # safe in production too — configure_logging is called exactly once
    # there, immediately after startup.
    root.handlers.clear()
    # Set the root level to DEBUG so all records reach our handlers; the
    # per-handler level filters then decide which file each line lands in.
    # If we set the root to INFO instead, debug.log would never see anything.
    root.setLevel(_stdlib_logging.DEBUG)

    info_handler = _file_handler(base_path / "voice-agent.log", _stdlib_logging.INFO)
    error_handler = _file_handler(base_path / "errors.log", _stdlib_logging.WARNING)
    debug_handler = _file_handler(base_path / "debug.log", _stdlib_logging.DEBUG)
    # debug.log: keep it pure-DEBUG. Without this filter, every INFO/WARN
    # would also land here (RotatingFileHandler's level filter is "≥ level").
    debug_handler.addFilter(_only_debug)

    root.addHandler(info_handler)
    root.addHandler(error_handler)
    root.addHandler(debug_handler)

    if log_console:
        # Mirror to stdout *in addition to* the files. Production stays silent
        # by default — systemd captures stderr lifecycle messages directly.
        stream = _stdlib_logging.StreamHandler(sys.stdout)
        stream.setLevel(log_level)
        root.addHandler(stream)

    # structlog processor pipeline. Order matters:
    #
    #  1. add_log_level         → puts ``level`` in event_dict (used by redaction).
    #  2. add_logger_name       → puts ``logger`` in event_dict (JSON convenience).
    #  3. TimeStamper           → ISO-8601 UTC ``timestamp`` for grep-friendly sort.
    #  4. _require_event_field  → enforces every call carries ``event=...``.
    #  5. redact_sensitive_fields → drops secrets / audio / gated transcripts.
    #  6. JSONRenderer          → final string handed to stdlib logging.
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _require_event_field,
            redact_sensitive_fields,
            structlog.processors.JSONRenderer(serializer=json.dumps),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Disable cache so test fixtures (which call configure_logging once
        # per test) reliably observe the *new* configuration on subsequent
        # calls rather than a cached BoundLogger from a previous setup.
        cache_logger_on_first_use=False,
    )


def _file_handler(path: Path, level: int) -> RotatingFileHandler:
    """Build a :class:`RotatingFileHandler` with the project's standard caps."""
    h = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT)
    h.setLevel(level)
    return h


def _only_debug(record: _stdlib_logging.LogRecord) -> bool:
    """Filter callable: return True only for records at exactly DEBUG level.

    Used on debug.log so it doesn't accumulate INFO/WARN/ERROR noise.
    """
    return record.levelno == _stdlib_logging.DEBUG


def _require_event_field(
    logger: Any,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Enforce that every structlog call passes ``event="verb.subject"``.

    Why this matters: ``event`` is the searchable atom in JSON logs. Without
    it, two log lines from different sites are indistinguishable. The
    architecture mandates an ``event`` on every call.

    Implementation note: structlog 25.x does *not* auto-catch processor
    exceptions, so raising here would crash the caller — a developer bug
    in one log line would take down the pipeline. Instead, we emit a
    CRITICAL via stdlib logging directly (which lands in ``errors.log``)
    and synthesize a placeholder ``event`` so the original call still
    serializes. Bugs become loud without becoming fatal.
    """
    if "event" not in event_dict:
        # Bypass structlog and write directly via stdlib so we don't recurse
        # back through this same processor. The CRITICAL level guarantees
        # the message reaches errors.log (filtered at WARNING+).
        _meta_logger.critical(
            "logging.missing_event_bug | method=%s passed_keys=%s",
            method_name,
            sorted(event_dict.keys()),
        )
        # Synthesize an event so downstream processors (redaction, JSON)
        # don't trip on its absence.
        event_dict["event"] = "logging.missing_event_bug"
    return event_dict
