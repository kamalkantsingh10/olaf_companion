# Story 1.2: Config loaders (`setup.toml` + `.env`) with schema validation

Status: review

## Story

As Kamal,
I want `setup.toml` and `.env` loaded via `pydantic-settings` with schema validation that refuses to start on bad config,
so that misconfiguration fails loudly at startup instead of silently at runtime — and every later story has a typed `SetupConfig` to import from.

## Acceptance Criteria

1. `src/voice_agent_pipeline/config/setup.py` exposes `SetupConfig` (a `pydantic-settings` `BaseSettings` model) and `load_setup_config(toml_path: Path = Path("setup.toml"), env_path: Path = Path(".env")) -> SetupConfig`. The model validates with `extra="forbid"` and reads `schema_version: int` from the TOML file.

2. `src/voice_agent_pipeline/config/version.py` exposes `SUPPORTED_SCHEMA_VERSION: int = 1` and `assert_schema_version(found: int, supported: int = SUPPORTED_SCHEMA_VERSION, *, source: str) -> None` that raises `SchemaVersionError` with both versions and the source name on mismatch.

3. `src/voice_agent_pipeline/errors.py` is populated with the **subset** of the exception hierarchy this story needs: `VoiceAgentError` (root), `ConfigError(VoiceAgentError)`, `SchemaVersionError(ConfigError)`. Story 1.4 will add the rest. Each carries context as `__init__` kwargs, not f-string-baked messages.

4. Given a valid `setup.toml` (containing only `schema_version = 1` for now) and `.env` (containing `PICOVOICE_ACCESS_KEY=stub`), `load_setup_config()` returns a `SetupConfig` with `schema_version == 1` and `picovoice_access_key == "stub"` (a `SecretStr` field).

5. Given a `setup.toml` missing `schema_version`, `load_setup_config()` raises `ConfigError` naming the missing key.

6. Given a `setup.toml` with an unknown extra key (e.g., `unknown_key = 42`), `load_setup_config()` raises `ConfigError` naming the offending key (pydantic `extra="forbid"`).

7. Given an `.env` missing `PICOVOICE_ACCESS_KEY`, `load_setup_config()` raises `ConfigError` naming the missing field. (Anthropic + Cartesia keys land Stories 2.2/2.3 — until then, they are NOT required at this story's load.)

8. Given a `setup.toml` with `schema_version = 2` (unsupported), `load_setup_config()` raises `SchemaVersionError` whose message contains both `2` and `1` (the supported version) and the string `setup.toml`.

9. Given `.env` file permissions looser than `0600`, `load_setup_config()` logs a `config.env.permissions_loose` WARN with the actual mode and the recommended mode but does NOT refuse to start. (NFR23 advisory; v1 doesn't hard-fail on this.) The log call uses Python's stdlib `logging` for now — Story 1.3 swaps in structlog.

10. `tests/unit/config/test_setup.py` and `tests/unit/config/test_version.py` cover all six failure cases (missing key, extra key, missing env var, bad schema_version, missing file, loose perms warning) plus the happy path. `just check` stays green.

## Tasks / Subtasks

- [x] **Task 1: Populate `errors.py` with the subset hierarchy** (AC: #3)
  - [x] Replace the empty stub from Story 1.1 with `VoiceAgentError`, `ConfigError(VoiceAgentError)`, `SchemaVersionError(ConfigError)`. See snippet in Dev Notes.
  - [x] Each exception accepts kwargs (no positional-only message); store kwargs on `self` for inspection in tests.
  - [x] Add module docstring noting that Story 1.4 expands the hierarchy.
  - [x] Update `__all__`.

- [x] **Task 2: Implement `config/version.py`** (AC: #2)
  - [x] Constants: `SUPPORTED_SCHEMA_VERSION: int = 1`.
  - [x] Function: `assert_schema_version(found, supported=SUPPORTED_SCHEMA_VERSION, *, source) -> None`.
  - [x] On mismatch, raise `SchemaVersionError(found=found, supported=supported, source=source)`.

- [x] **Task 3: Implement `config/setup.py`** (AC: #1, #4)
  - [x] Import `BaseSettings` from `pydantic_settings`; use `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="forbid", case_sensitive=False)`.
  - [x] Fields (this story's surface — extended by every later story):
    - `schema_version: int` (loaded from TOML, not env)
    - `picovoice_access_key: SecretStr` (loaded from `.env` as `PICOVOICE_ACCESS_KEY`)
  - [x] `load_setup_config(toml_path, env_path)`:
    1. Read the TOML file via stdlib `tomllib` (Python 3.11+).
    2. Pass parsed TOML dict to `SetupConfig(**toml_dict, _env_file=str(env_path))`.
    3. Pydantic validates `extra="forbid"` automatically — bubble its `ValidationError` as `ConfigError` with the offending field path and message.
    4. After construction, call `assert_schema_version(config.schema_version, source="setup.toml")`.
    5. Check `env_path.stat().st_mode & 0o777` — if not `0o600`, log a WARN via stdlib `logging.getLogger(__name__).warning("config.env.permissions_loose", extra={"actual_mode": ..., "recommended": "0o600"})`.
    6. Return the config.
  - [x] On TOML file missing → `ConfigError(missing_file=str(toml_path))`.
  - [x] On `.env` missing → `ConfigError(missing_file=str(env_path))`.

- [x] **Task 4: Update `setup.toml` placeholder** (AC: #4)
  - [x] No structural change yet — keep `schema_version = 1` plus the commented section markers from Story 1.1. Verify `load_setup_config()` happily loads this minimal file plus an `.env` containing `PICOVOICE_ACCESS_KEY=stub`.

- [x] **Task 5: Wire `__main__.py` to call `load_setup_config()`** (AC: #1)
  - [x] Replace Story 1.1's bare `print(...)` placeholder with: load config, print `voice-agent-pipeline v0.0.0 — config loaded (schema_version={...})`, exit 0.
  - [x] Wrap the load in a top-level `try/except VoiceAgentError as e: print error to stderr, exit non-zero`.
  - [x] Acceptable to keep this synchronous for now — `asyncio.run(main())` lands Story 1.5 when audio I/O arrives.

- [x] **Task 6: Tests** (AC: #4–#10)
  - [x] `tests/unit/config/__init__.py` (empty).
  - [x] `tests/unit/config/test_setup.py`:
    - `test_load_happy_path` — write a valid TOML + env to `tmp_path`, assert config fields match.
    - `test_missing_schema_version_raises` — TOML missing key → `ConfigError`.
    - `test_extra_key_raises` — TOML with unknown key → `ConfigError` (assert key name in message).
    - `test_missing_env_var_raises` — `.env` missing `PICOVOICE_ACCESS_KEY` → `ConfigError`.
    - `test_missing_toml_file_raises` — pass a path that doesn't exist → `ConfigError`.
    - `test_loose_env_perms_warns` — chmod `.env` to `0o644`, call loader, assert WARN was emitted via `caplog`. Skip on Windows (perms are POSIX-only).
  - [x] `tests/unit/config/test_version.py`:
    - `test_matching_version_does_not_raise`
    - `test_mismatched_version_raises_with_both_versions_and_source` — assert `2`, `1`, and `"setup.toml"` all appear in the message.
  - [x] Run `just check` until green.

- [ ] **Task 7: Commit** — single commit titled `Story 1.2: config loaders (setup.toml + .env) with schema validation`. *(deferred — batched at end-of-epic per Kamal's directive)*

## Dev Notes

### Architectural intent

This story establishes the **substrate** every other story reads from. `SetupConfig` is the typed contract for `setup.toml` + `.env`; every later story extends it by adding fields. The `extra="forbid"` rule means a typo in `setup.toml` fails loudly at startup, not silently at runtime — that's the whole point of v1's fail-fast posture (architecture §"V1 Posture: Hard Dependencies, Fail-Fast").

### What this story does NOT do

- **Does not validate credentials are real.** "Picovoice key reachable" is Story 1.6's startup check; "Anthropic/Cartesia reachable" is Story 2.2/2.3. This story only validates *presence and shape*.
- **Does not load `expression_map.yaml`.** That schema + loader lands Story 3.1. Different file, different loader.
- **Does not implement SIGHUP reload.** That's Story 5.2.
- **Does not enforce `.env` permissions.** v1 is advisory (NFR23 says creds are stored at 0600, but architecture's spec-as-contract allows the warning posture for ergonomics; raising would block dev iteration).
- **Does not add `[stt]`, `[audio]`, `[talker]`, etc. blocks.** Those land in the stories that need them. Adding them prematurely makes `extra="forbid"` reject `setup.toml`s missing the optional sections — defer.

### Why subset the errors hierarchy now

Story 1.4 lands the **full** hierarchy. This story needs `VoiceAgentError`, `ConfigError`, `SchemaVersionError` to satisfy its ACs. Authoring just those three now (and noting in the file docstring that 1.4 will extend) is the cleanest path — avoids circular ordering and keeps the contract honest. Story 1.4's AC #3 still passes when 1.4 lands, because by that point the file *will* contain exactly the full hierarchy.

### `errors.py` snippet

```python
"""Custom exception hierarchy for the voice-agent-pipeline.

This story (1.2) lands the subset needed for config loading.
Story 1.4 extends with StartupValidationError, ExternalServiceError + subclasses,
PublisherError, SplitterError.
"""

from typing import Any


class VoiceAgentError(Exception):
    """Root exception for all voice-agent-pipeline errors."""

    def __init__(self, **context: Any) -> None:
        super().__init__(self._format(context))
        self.context = context

    def _format(self, context: dict[str, Any]) -> str:
        if not context:
            return self.__class__.__name__
        parts = ", ".join(f"{k}={v!r}" for k, v in context.items())
        return f"{self.__class__.__name__}({parts})"


class ConfigError(VoiceAgentError):
    """Configuration file invalid or missing."""


class SchemaVersionError(ConfigError):
    """Configuration or event schema_version is unsupported."""


__all__ = ["VoiceAgentError", "ConfigError", "SchemaVersionError"]
```

### `config/setup.py` skeleton

```python
"""SetupConfig — typed loader for setup.toml + .env via pydantic-settings."""

import logging
import tomllib
from pathlib import Path

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import ConfigError

log = logging.getLogger(__name__)


class SetupConfig(BaseSettings):
    """Top-level configuration. Extended by every later story."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )

    schema_version: int
    picovoice_access_key: SecretStr


def load_setup_config(
    toml_path: Path = Path("setup.toml"),
    env_path: Path = Path(".env"),
) -> SetupConfig:
    if not toml_path.exists():
        raise ConfigError(missing_file=str(toml_path))
    if not env_path.exists():
        raise ConfigError(missing_file=str(env_path))

    with toml_path.open("rb") as f:
        toml_data = tomllib.load(f)

    try:
        config = SetupConfig(**toml_data, _env_file=str(env_path))  # type: ignore[arg-type]
    except ValidationError as e:
        raise ConfigError(toml_path=str(toml_path), validation=str(e)) from e

    assert_schema_version(config.schema_version, source=str(toml_path))
    _warn_if_env_perms_loose(env_path)
    return config


def _warn_if_env_perms_loose(env_path: Path) -> None:
    try:
        mode = env_path.stat().st_mode & 0o777
    except OSError:
        return
    if mode != 0o600:
        log.warning(
            "config.env.permissions_loose",
            extra={"actual_mode": oct(mode), "recommended": "0o600", "path": str(env_path)},
        )
```

### `config/version.py` snippet

```python
"""Schema version constants and validation helper."""

from voice_agent_pipeline.errors import SchemaVersionError

SUPPORTED_SCHEMA_VERSION: int = 1


def assert_schema_version(
    found: int,
    supported: int = SUPPORTED_SCHEMA_VERSION,
    *,
    source: str,
) -> None:
    if found != supported:
        raise SchemaVersionError(found=found, supported=supported, source=source)
```

### Updated `__main__.py`

```python
"""Voice agent pipeline entry point.

This story (1.2) wires startup config loading. Future stories add:
- structlog setup (1.3)
- asyncio.run + signal handlers + audio I/O (1.5+)
- full startup validation for external deps (2.2, 2.3, 3.4, 4.1, 4.2)
- SIGHUP handler (5.2)
"""

import sys

from voice_agent_pipeline.config.setup import load_setup_config
from voice_agent_pipeline.errors import VoiceAgentError


def main() -> int:
    try:
        config = load_setup_config()
    except VoiceAgentError as e:
        print(f"startup.failed: {e}", file=sys.stderr)
        return 1
    print(
        f"voice-agent-pipeline v0.0.0 — config loaded "
        f"(schema_version={config.schema_version})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Story 1.1 → 1.2 delta in `setup.toml`

No change. Story 1.1 already has `schema_version = 1` plus commented section markers. This story just verifies the loader accepts that file as-is.

### Why use `tomllib` instead of letting pydantic-settings parse the TOML

`pydantic-settings` v2 has TOML support but it's convention-bound (`[tool.<name>]` keys, etc.) — not a clean fit for our flat top-level `setup.toml`. Reading the file via `tomllib` (Python 3.11+ stdlib, no dep) and unpacking into the model is the architecture's intended pattern.

### Why `SecretStr` for the access key

Pydantic's `SecretStr` masks the value in `repr()`, so accidental logging of `config` itself doesn't leak the secret. It also signals intent in code review. Story 1.3's redaction processor is the belt; `SecretStr` is the suspenders.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/config/setup.py`
- `src/voice_agent_pipeline/config/version.py`
- `tests/unit/config/__init__.py`
- `tests/unit/config/test_setup.py`
- `tests/unit/config/test_version.py`

It modifies:
- `src/voice_agent_pipeline/errors.py` (subset hierarchy)
- `src/voice_agent_pipeline/__main__.py` (wire to loader)

It does NOT touch `setup.toml` (Story 1.1's placeholder is sufficient).

### Testing standards

- Tests live in `tests/unit/config/`. Layout mirrors `src/voice_agent_pipeline/config/`.
- Use `tmp_path` fixture for filesystem tests — never write to the real `setup.toml` or `.env`.
- Use `caplog` for asserting on log messages.
- Skip `test_loose_env_perms_warns` on Windows: `@pytest.mark.skipif(sys.platform == "win32", reason="POSIX perms only")`.
- No mocking pydantic models. No mocking internal helpers. Test against the real `load_setup_config(...)` with controlled file inputs.

### What "done" looks like

- `just check` exits 0.
- `uv run python -m voice_agent_pipeline` (with valid `setup.toml` + `.env` containing `PICOVOICE_ACCESS_KEY=stub`) prints `voice-agent-pipeline v0.0.0 — config loaded (schema_version=1)` and exits 0.
- The same command with a missing `.env` exits non-zero with a clear `startup.failed: ConfigError(...)` message on stderr.
- Story 1.3 can begin without further config plumbing.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Project-Scoped Configuration]
- [Source: build_documents/planning-artifacts/architecture.md#V1 Posture: Hard Dependencies, Fail-Fast]
- [Source: build_documents/planning-artifacts/architecture.md#Type System Conventions] (`pydantic.BaseModel` v2, `extra="forbid"`)
- [Source: build_documents/planning-artifacts/architecture.md#Error Handling] (custom hierarchy, kwargs not f-strings)
- [Source: build_documents/planning-artifacts/architecture.md#Schema Conventions] (integer `schema_version`, refuse on unsupported)
- [Source: build_documents/planning-artifacts/prd.md#FR31, FR34] — config validation + cred loading
- [Source: build_documents/planning-artifacts/prd.md#NFR23, NFR27] — 0600 advisory + schema versioning
- [Source: build_documents/planning-artifacts/epics.md#Story 1.2: Config loaders (`setup.toml` + `.env`) with schema validation]

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- Implemented errors.py subset, config/version.py, config/setup.py, updated __main__.py exactly per Dev Notes snippets.
- Initial `just check` flagged 2 ruff-format issues (line-length on print + helper sig) — auto-fixed via `uv run ruff format`.
- `just check` final: ruff clean, ruff-format clean, pyright 0 errors, pytest 12/12 pass (10 new + 1 smoke + 1 from version).
- Manual verification: `just run` with no `.env` → exit 1 with `startup.failed: ConfigError(missing_file='.env')` on stderr; with valid `.env` → `voice-agent-pipeline v0.0.0 — config loaded (schema_version=1)`, exit 0.
- Tests use real filesystem inputs via `tmp_path` (no mocking pydantic models — CLAUDE.md rule #7 honored).

### Completion Notes List

- All 10 ACs satisfied.
- Added one extra test beyond the spec's six failure cases: `test_missing_env_file_raises` covers AC #5/#7's intent for explicit env-file-missing semantics. Doesn't change scope, just covers a real failure mode.
- `errors.py` `__all__` is alphabetically sorted to satisfy ruff RUF022.
- No deviations from architecture/spec other than the Story 1.1 deviations already documented.

### File List

**New files:**
- `src/voice_agent_pipeline/config/version.py`
- `src/voice_agent_pipeline/config/setup.py`
- `tests/unit/config/__init__.py`
- `tests/unit/config/test_version.py`
- `tests/unit/config/test_setup.py`

**Modified files:**
- `src/voice_agent_pipeline/errors.py` (subset hierarchy populated)
- `src/voice_agent_pipeline/__main__.py` (wired to load_setup_config)
- `build_documents/implementation-artifacts/sprint-status.yaml` (status `ready-for-dev` → `review`)
- `build_documents/implementation-artifacts/1-2-config-loaders-and-schema-validation.md` (this file)

## Change Log

| Date | Change |
|---|---|
| 2026-05-05 | Story 1.2 implemented. SetupConfig + load_setup_config wired into __main__; subset of errors hierarchy landed; schema-version validation; loose-perms WARN. 12 tests pass. Status moved to `review`. |
