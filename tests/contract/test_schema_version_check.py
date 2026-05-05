"""Contract tests demonstrating the schema_version enforcement *pattern*.

The event models declare ``schema_version: int`` without policy. Policy
("we support version 1") is enforced by the **caller** at parse boundaries
via :func:`assert_schema_version` — exactly the same helper Story 1.2 uses
for ``setup.toml``. This module proves the pattern works end-to-end so
Stories 3.4 (publisher) and 4.2 (orchestrator) can copy it confidently.
"""

import pytest

from voice_agent_pipeline.config.version import (
    SUPPORTED_SCHEMA_VERSION,
    assert_schema_version,
)
from voice_agent_pipeline.errors import SchemaVersionError
from voice_agent_pipeline.schemas.expression_event import ExpressionEvent


def _build_event(schema_version: int) -> ExpressionEvent:
    """Helper: construct a minimal ExpressionEvent at the given schema_version."""
    return ExpressionEvent(
        schema_version=schema_version,
        event_type="expression",
        emotion="excited",
        source_tag="<laughs>",
        audio_frame_id=None,
        timestamp_ns=0,
        payload={},
    )


def test_assert_schema_version_passes_on_match() -> None:
    """Sanity: the supported version round-trips without raising."""
    event = _build_event(SUPPORTED_SCHEMA_VERSION)
    assert_schema_version(event.schema_version, source="ExpressionEvent")


def test_parsing_unsupported_schema_version_can_be_rejected_via_helper() -> None:
    """An event with ``schema_version=99`` is parseable but caller can reject it.

    This is the contract: the model is permissive (any int parses), but
    the caller's parse-boundary code applies policy.
    """
    parsed = ExpressionEvent.model_validate_json(_build_event(99).model_dump_json())
    with pytest.raises(SchemaVersionError) as exc_info:
        assert_schema_version(parsed.schema_version, source="ExpressionEvent")
    msg = str(exc_info.value)
    # The error message must surface both versions and the source name so
    # the operator can tell exactly which payload triggered it.
    assert "99" in msg
    assert str(SUPPORTED_SCHEMA_VERSION) in msg
    assert "ExpressionEvent" in msg
