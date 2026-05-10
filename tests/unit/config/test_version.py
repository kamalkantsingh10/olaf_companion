"""Tests for :mod:`voice_agent_pipeline.config.version`.

Covers the supported-version constant and :func:`assert_schema_version`'s
match / mismatch behavior. The mismatch path's error context is the
contract: it must surface both versions and the source name so an
operator reading ``startup.failed:`` can fix the right file.
"""

import pytest

from voice_agent_pipeline.config.version import (
    SUPPORTED_SCHEMA_VERSION,
    assert_schema_version,
)
from voice_agent_pipeline.errors import SchemaVersionError


def test_matching_version_does_not_raise() -> None:
    """Matching versions return cleanly with no exception."""
    assert_schema_version(SUPPORTED_SCHEMA_VERSION, source="setup.toml")


def test_mismatched_version_raises_with_both_versions_and_source() -> None:
    """A mismatch surfaces ``found``, ``supported``, and ``source`` in the message.

    Why we assert on the rendered string rather than the context dict: this
    is the only thing the operator sees in stderr / logs, so we test what
    they actually read.

    Bump history: Story 3.4 bumped ``SUPPORTED_SCHEMA_VERSION`` 1 → 2;
    sprint-change-proposal-2026-05-10 bumped 2 → 3 (boundary repair).
    The test drives ``found=1`` to assert the rejected-on-mismatch
    contract against the current supported version.
    """
    with pytest.raises(SchemaVersionError) as exc_info:
        assert_schema_version(1, source="setup.toml")
    msg = str(exc_info.value)
    assert "1" in msg
    assert "3" in msg
    assert "setup.toml" in msg
