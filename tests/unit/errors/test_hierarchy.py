"""Unit tests for the custom exception hierarchy in :mod:`voice_agent_pipeline.errors`.

Three behaviors are tested:

1. Every exception class accepts arbitrary kwargs at construction.
2. Those kwargs survive on the instance's ``.context`` attribute.
3. The ``isinstance`` chain matches the architecture's documented hierarchy
   (so an ``except ConfigError`` clause catches ``SchemaVersionError``,
   ``except ExternalServiceError`` catches ``CartesiaError``, etc.).

Adding a new exception class to ``errors.py`` requires adding it to the
parametrize list below — that's a deliberate forcing function so the test
suite always reflects the full hierarchy.
"""

import pytest

from voice_agent_pipeline.errors import (
    CartesiaError,
    ConfigError,
    ExternalServiceError,
    OrchestratorError,
    PublisherError,
    SchemaVersionError,
    SplitterError,
    StartupValidationError,
    TalkerError,
    VoiceAgentError,
)

# The 10 exception classes in the v1 hierarchy. Listed in declaration order
# so a reader scanning the file sees the structure at a glance.
ALL_ERRORS: tuple[type[VoiceAgentError], ...] = (
    VoiceAgentError,
    ConfigError,
    SchemaVersionError,
    StartupValidationError,
    ExternalServiceError,
    CartesiaError,
    OrchestratorError,
    TalkerError,
    PublisherError,
    SplitterError,
)


@pytest.mark.parametrize("err_cls", ALL_ERRORS)
def test_each_exception_constructs_with_kwargs(err_cls: type[VoiceAgentError]) -> None:
    """Every class accepts arbitrary kwargs without crashing."""
    err = err_cls(detail="something went wrong", code=42)
    # The rendered message should at least include the class name so log
    # output is unambiguous about which exception fired.
    assert err_cls.__name__ in str(err)


@pytest.mark.parametrize("err_cls", ALL_ERRORS)
def test_kwargs_stored_on_context(err_cls: type[VoiceAgentError]) -> None:
    """Kwargs round-trip through ``e.context`` for programmatic inspection."""
    err = err_cls(detail="msg", code=42, path="/some/path")
    assert err.context == {"detail": "msg", "code": 42, "path": "/some/path"}


@pytest.mark.parametrize("err_cls", ALL_ERRORS)
def test_no_kwargs_yields_bare_class_name(err_cls: type[VoiceAgentError]) -> None:
    """An exception with no context renders as bare ``ClassName``."""
    err = err_cls()
    assert str(err) == err_cls.__name__


# --- Inheritance chain assertions ------------------------------------------
#
# Tested explicitly (rather than via parametrize) because the architecture's
# rule about "never catch ExternalServiceError in v1" depends on the
# inheritance shape being exactly what the architecture document describes.


def test_schema_version_is_a_config_error() -> None:
    """``SchemaVersionError`` is catchable via ``except ConfigError``."""
    assert isinstance(SchemaVersionError(), ConfigError)


def test_cartesia_is_an_external_service_error() -> None:
    """``CartesiaError`` falls under the never-catch external-service branch."""
    assert isinstance(CartesiaError(), ExternalServiceError)


def test_orchestrator_is_an_external_service_error() -> None:
    """``OrchestratorError`` is in the external-service branch."""
    assert isinstance(OrchestratorError(), ExternalServiceError)


def test_talker_is_an_external_service_error() -> None:
    """``TalkerError`` is in the external-service branch."""
    assert isinstance(TalkerError(), ExternalServiceError)


def test_publisher_is_not_external_service() -> None:
    """Publisher failures are internal — explicitly NOT under ExternalServiceError.

    The architecture document is clear that the publisher is an internal
    seam (we own the impl, the transport runs in-process). If this test
    ever flips, it means someone refactored the hierarchy in a way that
    contradicts CLAUDE.md rule #4. Fix the refactor, not the test.
    """
    assert not isinstance(PublisherError(), ExternalServiceError)
    assert isinstance(PublisherError(), VoiceAgentError)


def test_splitter_is_not_external_service() -> None:
    """Splitter failures are internal — same reasoning as PublisherError."""
    assert not isinstance(SplitterError(), ExternalServiceError)
    assert isinstance(SplitterError(), VoiceAgentError)


def test_all_errors_subclass_voice_agent_error() -> None:
    """Every member of the hierarchy is catchable via ``except VoiceAgentError``."""
    for err_cls in ALL_ERRORS:
        assert issubclass(err_cls, VoiceAgentError), f"{err_cls.__name__} broke the chain"
