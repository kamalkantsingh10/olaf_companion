"""Custom exception hierarchy for the voice-agent-pipeline.

Story 1.4 lands the **complete** hierarchy. Story 1.2 introduced a subset
(``VoiceAgentError``, ``ConfigError``, ``SchemaVersionError``) for the
config loader; this module now adds startup, external-service, publisher,
and splitter branches on top of the same base.

Hierarchy (read top-down, indentation marks subclass relationships)::

    VoiceAgentError
    ├── ConfigError
    │   └── SchemaVersionError
    ├── StartupValidationError
    ├── ExternalServiceError      (CLAUDE.md rule #4: NEVER caught in v1)
    │   ├── CartesiaError
    │   ├── GroqAsrError
    │   ├── OrchestratorError
    │   └── TalkerError
    ├── PublisherError
    └── SplitterError

Design conventions (architecture.md §"Error Handling"):

- Every error stores its context as keyword arguments on the instance, never
  as f-string-baked text. Callers can inspect ``err.context`` programmatically
  (handy in tests) and the ``str(err)`` rendering is uniform.
- The hierarchy is shallow — one root, one tier of named subclasses, one
  level of specialization where useful.
- Per ``CLAUDE.md`` rule #4: ``ExternalServiceError`` (and its subclasses)
  is **never caught** in v1 code paths. Crash, let systemd restart. The
  resilience layer that handles those errors gracefully is a v2 deferral.
"""

from typing import Any


class VoiceAgentError(Exception):
    """Root exception for every error raised by this package.

    Subclassing this base (rather than ``Exception`` directly) lets callers
    write a single ``except VoiceAgentError`` clause to handle "anything our
    code threw" while still letting genuinely unexpected exceptions propagate.

    Attributes:
        context: Mapping of keyword arguments captured at construction time.
            Tests typically assert on individual entries here; production
            renders ``str(err)`` which formats the same data.
    """

    def __init__(self, **context: Any) -> None:
        # Build the str() representation eagerly so logging/printing the
        # exception is cheap and deterministic, then stash the raw context for
        # programmatic inspection (mostly tests).
        super().__init__(self._format(context))
        self.context = context

    def _format(self, context: dict[str, Any]) -> str:
        """Render ``ClassName(k=v, ...)`` deterministically.

        Empty context → bare class name. Otherwise comma-separated
        ``key=repr(value)`` pairs in insertion order. ``repr`` ensures values
        with whitespace, quotes, or special chars stay unambiguous in log
        output and in str(...) form.
        """
        if not context:
            return self.__class__.__name__
        parts = ", ".join(f"{k}={v!r}" for k, v in context.items())
        return f"{self.__class__.__name__}({parts})"


# ---------------------------------------------------------------------------
# Configuration branch
# ---------------------------------------------------------------------------


class ConfigError(VoiceAgentError):
    """Raised when ``setup.toml`` or ``.env`` is missing, malformed, or rejected by validation."""


class SchemaVersionError(ConfigError):
    """Raised when a config or event ``schema_version`` is unsupported by this build.

    Subclass of :class:`ConfigError` so existing ``except ConfigError`` blocks
    still catch it; specialization is for callers who want to print a more
    targeted "please regenerate your setup.toml" message.
    """


# ---------------------------------------------------------------------------
# Startup branch
# ---------------------------------------------------------------------------


class StartupValidationError(VoiceAgentError):
    """A required external dependency failed validation at startup.

    Distinct from :class:`ExternalServiceError` because the failure happens
    during pre-flight probes (Stories 1.6/2.2/2.3/3.4/4.1/4.2) — not during
    a turn. The pipeline never finished initializing, so there's no in-flight
    work to roll back.
    """


# ---------------------------------------------------------------------------
# External-service branch — NEVER caught in v1 (CLAUDE.md rule #4)
# ---------------------------------------------------------------------------


class ExternalServiceError(VoiceAgentError):
    """Base class for failures of external services.

    Per CLAUDE.md rule #4, no v1 code path may catch this exception or its
    subclasses. Crash + systemd restart is the v1 resilience strategy. v2
    will introduce a wrapper layer that translates these into recoverable
    pipeline events (see architecture.md §"V2 Deferred Concerns").
    """


class CartesiaError(ExternalServiceError):
    """Cartesia TTS API failure (Story 2.3 + downstream)."""


class GroqAsrError(ExternalServiceError):
    """Groq STT API failure (sprint-change-proposal-2026-05-12, Cloud STT swap).

    Raised by :class:`voice_agent_pipeline.stt.groq.GroqAsrBackend` on any
    ``openai.APIError`` subclass from Groq's ``audio/transcriptions``
    endpoint. Same fail-fast posture as :class:`TalkerError` /
    :class:`CartesiaError` — never caught in v1 code paths.
    """


class OrchestratorError(ExternalServiceError):
    """Orchestrator daemon failure — HTTP 4xx/5xx, SSE stream broken, etc. (Stories 4.1, 4.2)."""


class TalkerError(ExternalServiceError):
    """Anthropic Talker API failure (Story 2.2 + downstream)."""


# ---------------------------------------------------------------------------
# Internal-component branch
# ---------------------------------------------------------------------------


class PublisherError(VoiceAgentError):
    """Broadcast publisher (ROS 2 / DDS) failure — connect, healthcheck, or publish (Story 3.4).

    NOT a subclass of :class:`ExternalServiceError` because the publisher is
    an **internal** seam: we own the implementation, the transport library
    runs in-process, and a publish failure is recoverable in principle (the
    voice loop can keep running even if expression broadcasts are dropped).
    """


class SplitterError(VoiceAgentError):
    """Streaming SSML splitter / state machine failure (Story 3.3).

    Internal component — bugs here mean a malformed SSML token or an
    unrecoverable state-machine transition. Not from an external service,
    so it's not under :class:`ExternalServiceError`.
    """


# Public API — listed in hierarchy order rather than alphabetical so a
# reader scanning the list can see the structure at a glance.
__all__ = [
    "CartesiaError",
    "ConfigError",
    "ExternalServiceError",
    "GroqAsrError",
    "OrchestratorError",
    "PublisherError",
    "SchemaVersionError",
    "SplitterError",
    "StartupValidationError",
    "TalkerError",
    "VoiceAgentError",
]
