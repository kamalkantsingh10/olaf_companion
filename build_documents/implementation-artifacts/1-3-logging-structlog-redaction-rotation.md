# Story 1.3: Logging — structlog + redaction + rotating files

Status: review

## Story

As Kamal,
I want JSON-structured logs landing in `./logs/` with three rotation streams and a redaction processor enforced before serialization,
so that I can post-mortem any session without leaking credentials, raw audio, or transcripts — and every later story emits logs through one consistent setup.

## Acceptance Criteria

1. `src/voice_agent_pipeline/logging/setup.py` exposes `configure_logging(config: SetupConfig) -> None` that wires structlog → stdlib `logging` → `RotatingFileHandler` with three streams: `./logs/voice-agent.log` (INFO+), `./logs/errors.log` (WARN+), `./logs/debug.log` (DEBUG, opt-in via `LOG_LEVEL=DEBUG` env var).

2. `src/voice_agent_pipeline/logging/redaction.py` exposes a structlog processor `redact_sensitive_fields(logger, method_name, event_dict)` that drops keys: `audio_bytes`, `audio_data`, `pcm`, plus any key matching the patterns `*api_key`, `*token`, `*password`, `*secret` (case-insensitive substring match). The processor runs **before** the JSON serializer.

3. `LOG_LEVEL=INFO` (default) → keys `transcript` and `user_text` are dropped from the serialized output. `LOG_LEVEL=DEBUG` → those keys appear only in `debug.log` (FR39).

4. Every log call MUST include an `event` field in `verb.subject` form (e.g., `event="config.loaded"`, `event="lifecycle.transition"`). A structlog processor enforces this at runtime — calls without `event` cause the processor to raise (caught by structlog and surfaced as a CRITICAL log so the bug is visible).

5. JSON-only output (NFR29). Each log line is a parseable JSON object with at minimum: `timestamp` (ISO-8601 UTC), `level`, `event`, `logger`, plus any contextual kwargs the caller passed.

6. `LOG_CONSOLE=true` env var (read at `configure_logging` time, NOT re-read at runtime) mirrors logs to stdout in addition to file output. Default is false; production stays silent except for systemd lifecycle messages.

7. `logs/` directory is created at startup if missing. `RotatingFileHandler` defaults: `maxBytes = 50 * 1024 * 1024` (50MB), `backupCount = 7` (7-day rolling retention assumes ≤1 file/day at default volume). These defaults are **hard-coded constants** in this story; Story 5.3 makes them configurable.

8. `__main__.py` calls `configure_logging(config)` immediately after `load_setup_config()` succeeds (and before any other code path runs). The placeholder print from Story 1.2 is replaced with `log.info("startup.completed", schema_version=config.schema_version)`.

9. `tests/unit/logging/test_setup.py` and `tests/unit/logging/test_redaction.py` cover: redaction denylist exact-match, regex-substring match, transcript gating per LOG_LEVEL, missing-event detection, JSON shape, console mirror toggle. All tests use a `caplog`-style fixture that intercepts structlog output. `just check` stays green.

10. `.gitkeep` is added to `logs/` directory? **No** — `logs/` is gitignored (Story 1.1 already excluded). The directory is created at runtime by `configure_logging`.

## Tasks / Subtasks

- [x] **Task 1: Implement `logging/redaction.py`** (AC: #2, #3)
  - [x] Module-level constants: `DENYLIST_EXACT = frozenset({"audio_bytes", "audio_data", "pcm"})`, `DENYLIST_PATTERNS = ("api_key", "token", "password", "secret")`, `TRANSCRIPT_KEYS = frozenset({"transcript", "user_text"})`.
  - [x] Function `redact_sensitive_fields(logger, method_name, event_dict)` returning the modified dict.
  - [x] Drop keys in `DENYLIST_EXACT` outright.
  - [x] Drop keys whose lowercased name contains any pattern in `DENYLIST_PATTERNS`.
  - [x] Drop `TRANSCRIPT_KEYS` unless the log level (read from `event_dict.get("level", "INFO")`) is `DEBUG`.
  - [x] See snippet in Dev Notes.

- [x] **Task 2: Implement `logging/setup.py`** (AC: #1, #4–#8)
  - [x] `configure_logging(config: SetupConfig) -> None`:
    1. Read `LOG_LEVEL` env var (default `INFO`); validate it's one of the standard names.
    2. Read `LOG_CONSOLE` env var as bool (`"true"`/`"false"`, case-insensitive).
    3. Ensure `./logs/` exists (`Path("logs").mkdir(parents=True, exist_ok=True)`).
    4. Build three `RotatingFileHandler`s with 50MB / 7 backups for the file streams; wire each to its own `logging.Logger` filter or use a single root logger with handler-level filters (the latter is simpler — see snippet).
    5. Configure structlog processors in this order: `add_log_level` → `add_logger_name` → `TimeStamper(fmt="iso", utc=True)` → `_require_event_field` → `redact_sensitive_fields` → `JSONRenderer(serializer=json.dumps)`.
    6. Set `structlog.configure(...)` with `wrapper_class=structlog.stdlib.BoundLogger` and `logger_factory=structlog.stdlib.LoggerFactory()`.
    7. If `LOG_CONSOLE` is true, attach a `StreamHandler(sys.stdout)` to the root logger at the same level as `LOG_LEVEL`.
  - [x] Helper processor `_require_event_field(logger, method_name, event_dict)` that raises `ValueError("missing 'event' field")` if `event` is not present. structlog catches this and emits a CRITICAL note.
  - [x] See snippet in Dev Notes.

- [x] **Task 3: Update `__main__.py` to call `configure_logging` early** (AC: #8)
  - [x] After `load_setup_config()` succeeds, call `configure_logging(config)`.
  - [x] Replace the placeholder `print(...)` with `log.info("startup.completed", schema_version=config.schema_version)`.
  - [x] On `VoiceAgentError` during config load, `configure_logging` has not yet run — fall back to `print(f"startup.failed: {e}", file=sys.stderr)` (same as Story 1.2).

- [x] **Task 4: Tests** (AC: #9)
  - [x] `tests/unit/logging/__init__.py` (empty).
  - [x] `tests/unit/logging/test_redaction.py`:
    - `test_drops_audio_bytes_field` — input dict with `audio_bytes`, assert dropped.
    - `test_drops_pcm_and_audio_data` — same for siblings.
    - `test_drops_keys_matching_patterns` — keys like `cartesia_api_key`, `bearer_token`, `user_password`, `client_secret` all dropped.
    - `test_pattern_match_is_case_insensitive` — `Cartesia_API_Key` also dropped.
    - `test_transcript_dropped_at_info` — `transcript="hi"` with `level="INFO"` → dropped.
    - `test_transcript_kept_at_debug` — same with `level="DEBUG"` → kept.
    - `test_user_text_dropped_at_info` — likewise.
    - `test_unrelated_keys_kept` — `event="x"`, `count=42` survive.
  - [x] `tests/unit/logging/test_setup.py`:
    - `test_three_log_files_created` — call `configure_logging(stub_config)`, assert `logs/voice-agent.log`, `logs/errors.log`, `logs/debug.log` exist.
    - `test_info_log_appears_in_voice_agent_log_only` — emit one INFO, read both files, assert it's in `voice-agent.log` and NOT in `errors.log`.
    - `test_warn_log_appears_in_both` — emit one WARN, assert it's in `voice-agent.log` AND `errors.log`.
    - `test_debug_log_only_in_debug_log_when_log_level_debug` — set `LOG_LEVEL=DEBUG`, emit DEBUG, assert it's in `debug.log` and NOT in `voice-agent.log`.
    - `test_missing_event_field_emits_critical` — call `log.info(some_msg=...)` without `event`, assert a CRITICAL appears in `errors.log`.
    - `test_log_console_true_mirrors_to_stdout` — `LOG_CONSOLE=true`, capture stdout, assert log appears.
    - `test_log_console_false_silent_stdout` — default, capture stdout, assert no log appears.
    - `test_json_shape_includes_required_fields` — emit one log, parse the line, assert `timestamp`, `level`, `event`, `logger` all present.
  - [x] All tests use `tmp_path` for `logs/` (monkeypatch `Path.cwd()` or pass an explicit base path to `configure_logging` — see Dev Notes for the seam).
  - [x] Run `just check` until green.

- [x] **Task 5: Add log-volume sanity check** (AC: #7)
  - [x] No code change — just verify in a manual smoke run that calling `log.info(...)` 1000 times produces a single file under 50MB and rotation triggers at the boundary. Document in commit message.

- [ ] **Task 6: Commit** — single commit titled `Story 1.3: logging — structlog + redaction + rotation`. *(pending Kamal's go-ahead — see Completion Notes for retroactive-split question)*

## Dev Notes

### Architectural intent

This story lands the **logging substrate** every later story emits through. structlog handles JSON shaping + the redaction processor pipeline; stdlib `logging` handles file rotation (well-trodden, process-safe, no extra dep). Logs go to `./logs/` at the project root — **not** journald, **not** `/var/log` — because the production deployment (Story 5.4) uses systemd but app-owned logs make local post-mortem and `tail -f` trivial.

The `event` field requirement is non-obvious but critical: it's the searchable atom in JSON logs. `event="lifecycle.transition" from_state="LISTENING" to_state="THINKING"` is grep-friendly across runs; a free-text message is not.

### What this story does NOT do

- **Does not add `[logging]` block to `setup.toml`.** Story 5.3 makes rotation/retention/console configurable. This story's defaults (50MB, 7 backups, console-via-env-var) are hard-coded.
- **Does not implement systemd journald integration.** Story 5.4 wires the systemd unit; only systemd's own lifecycle messages hit journald — app logs stay in `./logs/`.
- **Does not bind contextvars at startup.** `bind_contextvars(session_id=..., audio_frame_id=...)` is a per-turn pattern that lands when sessions exist (Story 4.5).
- **Does not add log shipping or remote aggregation.** Out of v1 scope (no telemetry — FR43).

### `redaction.py` snippet

```python
"""structlog processor that drops sensitive fields before JSON serialization."""

import re
from typing import Any

DENYLIST_EXACT: frozenset[str] = frozenset({"audio_bytes", "audio_data", "pcm"})
DENYLIST_PATTERNS: tuple[str, ...] = ("api_key", "token", "password", "secret")
TRANSCRIPT_KEYS: frozenset[str] = frozenset({"transcript", "user_text"})

_PATTERN_RE = re.compile(
    "|".join(re.escape(p) for p in DENYLIST_PATTERNS),
    re.IGNORECASE,
)


def redact_sensitive_fields(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    level = str(event_dict.get("level", "INFO")).upper()
    for k, v in event_dict.items():
        if k in DENYLIST_EXACT:
            continue
        if _PATTERN_RE.search(k):
            continue
        if k in TRANSCRIPT_KEYS and level != "DEBUG":
            continue
        out[k] = v
    return out
```

### `setup.py` skeleton

```python
"""structlog + stdlib logging + RotatingFileHandler wiring."""

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.logging.redaction import redact_sensitive_fields

_MAX_BYTES = 50 * 1024 * 1024
_BACKUP_COUNT = 7


def configure_logging(config: SetupConfig, *, base_path: Path = Path("logs")) -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_console = os.environ.get("LOG_CONSOLE", "false").lower() == "true"
    base_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(log_level)

    info_handler = _file_handler(base_path / "voice-agent.log", logging.INFO)
    error_handler = _file_handler(base_path / "errors.log", logging.WARNING)
    debug_handler = _file_handler(base_path / "debug.log", logging.DEBUG)
    debug_handler.addFilter(lambda r: r.levelno == logging.DEBUG)

    root.addHandler(info_handler)
    root.addHandler(error_handler)
    root.addHandler(debug_handler)

    if log_console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(log_level)
        root.addHandler(stream)

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
        cache_logger_on_first_use=True,
    )


def _file_handler(path: Path, level: int) -> RotatingFileHandler:
    h = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT)
    h.setLevel(level)
    return h


def _require_event_field(
    logger: object,
    method_name: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    if "event" not in event_dict:
        raise ValueError("missing 'event' field — every log call must pass event='verb.subject'")
    return event_dict
```

### Updated `__main__.py`

```python
"""Voice agent pipeline entry point."""

import sys

import structlog

from voice_agent_pipeline.config.setup import load_setup_config
from voice_agent_pipeline.errors import VoiceAgentError
from voice_agent_pipeline.logging.setup import configure_logging


def main() -> int:
    try:
        config = load_setup_config()
    except VoiceAgentError as e:
        print(f"startup.failed: {e}", file=sys.stderr)
        return 1

    configure_logging(config)
    log = structlog.get_logger(__name__)
    log.info("startup.completed", schema_version=config.schema_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Why three file streams?

- `voice-agent.log` — main app log (INFO+) — what you `tail -f` during dev.
- `errors.log` — WARN+ only — fast post-mortem scan ("did anything go wrong in the last hour?").
- `debug.log` — DEBUG only — opt-in via `LOG_LEVEL=DEBUG`. Includes transcripts (FR39 — gated, off by default).

This split reduces noise in errors.log while keeping the full picture available when needed.

### Why hard-code rotation values

Story 5.3 makes them configurable. Doing it here forces a `[logging]` block into `setup.toml` that this story has no other reason to introduce. Hard-coded constants now, configurable in 5.3 — that's the right deferral order.

### Why `_require_event_field` raises (and structlog catches)

structlog's processor pipeline is fail-soft: a processor that raises causes structlog to emit a CRITICAL `event_processing_failed` log. That's exactly what we want — bugs (missing event field) become loud, but they don't crash the process.

### `base_path` parameter on `configure_logging`

The keyword arg `base_path` defaults to `Path("logs")` for production but lets tests inject `tmp_path` cleanly. Without this seam, tests have to monkeypatch `Path.cwd()` which is fragile. Architecture's "testability via Protocol seams" rule (NFR28) extends to "give tests injection points where the API allows it without harming production ergonomics."

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/logging/setup.py`
- `src/voice_agent_pipeline/logging/redaction.py`
- `tests/unit/logging/__init__.py`
- `tests/unit/logging/test_setup.py`
- `tests/unit/logging/test_redaction.py`

It modifies:
- `src/voice_agent_pipeline/__main__.py` (call `configure_logging` after `load_setup_config`)

It creates at runtime (gitignored):
- `logs/` directory
- `logs/voice-agent.log`, `logs/errors.log`, `logs/debug.log`

### Testing standards

- All filesystem tests use `tmp_path` and `base_path=tmp_path / "logs"` to isolate.
- Reading log lines back: open the file, split lines, `json.loads(each)`, assert on dict shape — DON'T regex against raw text.
- For `LOG_LEVEL` and `LOG_CONSOLE` tests, use `monkeypatch.setenv(...)` and reset between tests via fixture.
- Reset structlog config between tests via `structlog.reset_defaults()` in a fixture so each test runs against a clean slate.
- The `test_missing_event_field_emits_critical` test is subtle: structlog's CRITICAL appears in `errors.log` (WARN+). Verify by reading that file.

### What "done" looks like

- `just check` exits 0.
- Running `uv run python -m voice_agent_pipeline` creates `logs/voice-agent.log` containing one JSON line: `{"timestamp": "...", "level": "info", "event": "startup.completed", "logger": "...", "schema_version": 1}`.
- `errors.log` and `debug.log` exist but are empty.
- With `LOG_CONSOLE=true uv run python -m voice_agent_pipeline`, the same JSON line also appears on stdout.
- Story 1.4 can begin without further logging plumbing.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Logging — mature, project-rooted, file-first strategy]
- [Source: build_documents/planning-artifacts/architecture.md#Logging Conventions]
- [Source: build_documents/planning-artifacts/architecture.md#Operations: systemd, Redaction, Tests] (redaction processor)
- [Source: build_documents/planning-artifacts/architecture.md#Cross-Cutting Concerns Identified] (#3 Observability)
- [Source: build_documents/planning-artifacts/prd.md#FR37, FR39, FR40, FR42, FR43] — log discipline + no persistence + no telemetry
- [Source: build_documents/planning-artifacts/prd.md#NFR25, NFR29] — credential/audio/transcript redaction + JSON-readable
- [Source: build_documents/planning-artifacts/epics.md#Story 1.3: Logging — structlog + redaction + rotating files]

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- Initial impl had two pyright issues: `dict[str, Any]` not assignable to structlog's `EventDict` (`MutableMapping[str, Any]`), and an autouse fixture flagged as `reportUnusedFunction`. Fixes: (a) imported `EventDict` from `structlog.types` and updated signatures; (b) added `reportUnusedFunction = false` to the tests/ pyright executionEnvironment.
- One test failure on first run: `test_log_console_true_mirrors_to_stdout`. My initial `captured_stdout` fixture replaced `sys.stdout` *after* `configure_logging` had already captured the original. Switched to pytest's `capsys` which intercepts at the lower level. Test passed.
- ruff RUF002/RUF003 flagged en-dashes and `×` in docstrings/comments after I added prose; replaced with ASCII equivalents.
- `_require_event_field` does NOT raise (the spec said "structlog catches" but structlog 25.x propagates processor exceptions). Implemented as a stdlib-bypass CRITICAL on missing event + synthesized event placeholder. See module docstring in `logging/setup.py` for the full rationale.

End-to-end smoke verified manually: `just run` with a stub `.env` produces exactly one JSON line in `voice-agent.log`:
`{"schema_version": 1, "event": "startup.completed", "level": "info", "logger": "__main__", "timestamp": "2026-05-05T13:03:40.287958Z"}`. `errors.log` and `debug.log` exist and are empty (correct).

### Completion Notes List

- All 10 ACs satisfied; `just check` green; 32 tests pass (10 redaction + 11 setup + 11 config/smoke).
- **Deviation from spec:** `_require_event_field` does NOT raise. structlog 25.x propagates processor exceptions to callers, so raising would crash the pipeline on a developer typo — exactly the opposite of what we want. Replaced with a stdlib-bypass CRITICAL log + synthesized event field. Net behavior matches the spec's *intent* (missing event = loud, visible bug; pipeline survives).
- **Comment density:** All authored modules and tests now carry module / class / function docstrings plus inline comments per Kamal's mid-session direction (see feedback memory `feedback_code_comments.md`). Story 1.1 / 1.2 files were retroactively beefed up.
- **Pyright config delta:** Added `reportUnusedFunction = false` to the tests/ executionEnvironment so autouse fixtures don't trip strict mode.
- **Retroactive-comments scope:** Stories 1.1 + 1.2 source/tests were updated with full comments in this story's working tree. They will land in this story's commit (or whatever commit slicing Kamal chooses) since they were not yet in any commit.

### File List

**New files (Story 1.3):**
- `src/voice_agent_pipeline/logging/redaction.py`
- `src/voice_agent_pipeline/logging/setup.py`
- `tests/unit/logging/__init__.py`
- `tests/unit/logging/conftest.py`
- `tests/unit/logging/test_redaction.py`
- `tests/unit/logging/test_setup.py`

**Modified files (Story 1.3):**
- `src/voice_agent_pipeline/__main__.py` (calls `configure_logging` after `load_setup_config`; replaces print with `log.info("startup.completed", ...)`)
- `pyproject.toml` (added `reportUnusedFunction = false` to tests/ pyright executionEnvironment)
- `build_documents/implementation-artifacts/sprint-status.yaml` (1-3 status `ready-for-dev` → `review`)
- `build_documents/implementation-artifacts/1-3-logging-structlog-redaction-rotation.md` (this file)

**Comment-density updates (in this commit too — see Completion Notes):**
- `src/voice_agent_pipeline/__init__.py`
- `src/voice_agent_pipeline/__main__.py`
- `src/voice_agent_pipeline/pipeline.py`
- `src/voice_agent_pipeline/errors.py`
- `src/voice_agent_pipeline/config/__init__.py` (and 9 sibling subpackage `__init__.py` files)
- `src/voice_agent_pipeline/config/setup.py`
- `src/voice_agent_pipeline/config/version.py`
- `tests/conftest.py`
- `tests/unit/test_smoke.py`
- `tests/unit/config/test_setup.py`
- `tests/unit/config/test_version.py`

## Change Log

| Date | Change |
|---|---|
| 2026-05-05 | Story 1.3 implemented. structlog + stdlib logging + 3 RotatingFileHandlers; redaction processor (denylist exact + pattern + transcript gate); JSON output; `LOG_LEVEL` and `LOG_CONSOLE` env-var driven; `_require_event_field` enforces every call carries an `event=`. 21 new tests pass. End-to-end verified via `just run`. Status moved to `review`. Inline-comment pass applied across stories 1.1–1.3 source per Kamal's request. |
