"""Schema-version constants and a tiny validation helper.

The voice-agent-pipeline embraces *spec-as-contract* (NFR26): every config
file and every published event carries an integer ``schema_version`` field.
Bumps are reserved for **breaking** changes — adding optional fields stays
forward-compatible (CLAUDE.md rule #6).

This module is the single source of truth for "what version does this build
of the code support?". Story 1.4 will reuse :func:`assert_schema_version`
for event-payload validation; Story 3.1 will reuse it for
``expression_map.yaml``.
"""

from voice_agent_pipeline.errors import SchemaVersionError

# Bump only on breaking changes. When you bump, update every .toml/.yaml that
# references a schema_version in lockstep.
SUPPORTED_SCHEMA_VERSION: int = 1


def assert_schema_version(
    found: int,
    supported: int = SUPPORTED_SCHEMA_VERSION,
    *,
    source: str,
) -> None:
    """Raise :class:`SchemaVersionError` if ``found`` doesn't match ``supported``.

    Args:
        found: The schema_version actually read from the file or payload.
        supported: The version this build of the code understands. Defaults
            to :data:`SUPPORTED_SCHEMA_VERSION`; override only in tests that
            want to simulate a different supported version.
        source: Human-readable source name (e.g. ``"setup.toml"``,
            ``"expression_map.yaml"``, ``"WordEvent"``). Surfaces in the
            error message so the operator knows which file to fix.

    Raises:
        SchemaVersionError: If the versions disagree. The error context
            captures all three values for downstream inspection.
    """
    if found != supported:
        raise SchemaVersionError(found=found, supported=supported, source=source)
