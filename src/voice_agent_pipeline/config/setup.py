"""``setup.toml`` + ``.env`` loader with ``pydantic-settings`` schema validation.

This module is the **substrate** every later story reads from. The
:class:`SetupConfig` model defines the typed contract for the project's
configuration; subsequent stories extend it by adding fields. The
``extra="forbid"`` rule means a typo in ``setup.toml`` fails loudly at startup
instead of silently at runtime — that is the whole point of v1's fail-fast
posture (architecture.md §"V1 Posture: Hard Dependencies, Fail-Fast").

What this story (1.2) deliberately does **not** do:

- Validate that credentials are reachable. The Picovoice probe lands in
  Story 1.6; Anthropic / Cartesia probes land in Stories 2.2 / 2.3.
- Load ``expression_map.yaml`` (Story 3.1).
- Implement ``SIGHUP``-driven reload (Story 5.2).
- **Hard-fail** on loose ``.env`` permissions. Per NFR23 the policy is
  advisory in v1 — log a WARN and continue.
"""

import logging
import tomllib
from pathlib import Path

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import ConfigError

# Module-level logger. Test fixtures patch this one specifically when
# asserting on the loose-perms warning (see tests/unit/config/test_setup.py).
log = logging.getLogger(__name__)


class SetupConfig(BaseSettings):
    """Typed top-level configuration for the voice-agent pipeline.

    Subsequent stories extend this surface (e.g. Story 1.5 adds an ``audio``
    block, Story 2.2 adds ``talker``, etc.). For now the model only carries
    the bare minimum required by the bootstrap: a schema marker plus the one
    secret needed to verify the ``.env`` plumbing works.

    pydantic-settings populates fields from two sources:

    - The TOML payload passed in via :func:`load_setup_config` (used for
      ``schema_version`` and any future TOML-backed fields).
    - The ``.env`` file pointed at by ``_env_file`` (used for
      ``picovoice_access_key`` and any future credentials).

    Class attributes:
        schema_version: Integer version marker; must match
            :data:`SUPPORTED_SCHEMA_VERSION`. Lives in ``setup.toml``.
        picovoice_access_key: Picovoice / Porcupine access key, stored as
            :class:`SecretStr` so accidental ``repr(config)`` doesn't leak it
            (the redaction processor is the belt; SecretStr is the suspenders).
    """

    # extra="forbid" → unknown TOML keys raise a ValidationError at load time.
    # case_sensitive=False matches the convention that env vars are
    # UPPER_SNAKE_CASE while pydantic field names are lower_snake_case.
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
    """Load + validate ``setup.toml`` and ``.env`` into a :class:`SetupConfig`.

    Steps, in order:

    1. Verify both files exist; ``ConfigError`` on miss.
    2. Parse the TOML via stdlib :mod:`tomllib` (no extra dep).
    3. Pass the parsed dict + ``_env_file`` to :class:`SetupConfig`. pydantic
       runs its full validation pass — including ``extra="forbid"`` for the
       TOML keys and presence checks for required ``.env`` vars. Translate
       any ``ValidationError`` into our project-local :class:`ConfigError`.
    4. Cross-check ``schema_version`` against :data:`SUPPORTED_SCHEMA_VERSION`.
    5. Advisory: warn (don't fail) if ``.env`` permissions are looser than
       ``0o600`` (NFR23).

    Args:
        toml_path: Path to ``setup.toml`` (cwd-relative by default).
        env_path: Path to ``.env`` (cwd-relative by default).

    Returns:
        A fully validated :class:`SetupConfig` instance.

    Raises:
        ConfigError: For any missing-file, parse-failure, or validation issue.
        SchemaVersionError: When ``setup.toml``'s ``schema_version`` does not
            match the value this build supports.
    """
    # Eager existence checks → unambiguous error message naming the missing
    # file. Without these, pydantic's error would mention "env_file not found"
    # which is less obvious to a tired operator.
    if not toml_path.exists():
        raise ConfigError(missing_file=str(toml_path))
    if not env_path.exists():
        raise ConfigError(missing_file=str(env_path))

    # tomllib requires binary mode (it controls its own decoding).
    with toml_path.open("rb") as f:
        toml_data = tomllib.load(f)

    try:
        # _env_file is pydantic-settings' way to override the config-class
        # default at construction time (e.g. for tests using tmp_path).
        config = SetupConfig(**toml_data, _env_file=str(env_path))  # type: ignore[arg-type]
    except ValidationError as e:
        # Wrap the pydantic error rather than re-raising it: callers should
        # only catch our error hierarchy.
        raise ConfigError(toml_path=str(toml_path), validation=str(e)) from e

    # Schema version is intentionally NOT a field-level pydantic validator on
    # SetupConfig — keeping it as a separate call lets Story 1.4 reuse the
    # same helper for event-payload schema_version checks.
    assert_schema_version(config.schema_version, source=str(toml_path))

    _warn_if_env_perms_loose(env_path)
    return config


def _warn_if_env_perms_loose(env_path: Path) -> None:
    """Log a WARN if ``.env``'s POSIX mode bits are looser than ``0o600``.

    Advisory only — v1 deliberately does not refuse to start (NFR23). Story
    1.3 will swap the stdlib :mod:`logging` call for structlog, but the
    semantics stay identical. Silently no-ops on platforms where ``stat()``
    fails (e.g. very tightly-confined containers).
    """
    try:
        # Mask away the type bits / setuid bits — we only care about the
        # owner/group/other permission triplet.
        mode = env_path.stat().st_mode & 0o777
    except OSError:
        # Don't let a stat() failure block startup. The next layer of
        # defense (the redaction processor in Story 1.3) catches the most
        # important case anyway: secrets accidentally hitting log output.
        return

    if mode != 0o600:
        log.warning(
            "config.env.permissions_loose",
            extra={
                "actual_mode": oct(mode),
                "recommended": "0o600",
                "path": str(env_path),
            },
        )
