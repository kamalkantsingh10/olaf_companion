"""Custom exception hierarchy for the voice-agent-pipeline.

Story 1.2 lands the **subset** required by config loading. Story 1.4 will
extend the hierarchy with ``StartupValidationError``, ``ExternalServiceError``
(plus subclasses for each external dep), ``PublisherError``, and
``SplitterError``.

Design conventions (architecture.md §"Error Handling"):

- Every error stores its context as keyword arguments on the instance, never
  as f-string-baked text. Callers can inspect ``err.context`` programmatically
  (handy in tests) and the ``str(err)`` rendering is uniform.
- The hierarchy is shallow — one root, one tier of named subclasses, one
  level of specialization where useful. Avoid deep inheritance chains.
- Per ``CLAUDE.md`` rule #4: ``ExternalServiceError`` (lands Story 1.4) is
  **never caught** in v1 code paths. Crash, let systemd restart.
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


class ConfigError(VoiceAgentError):
    """Raised when ``setup.toml`` or ``.env`` is missing, malformed, or rejected by validation."""


class SchemaVersionError(ConfigError):
    """Raised when a config or event ``schema_version`` is unsupported by this build.

    Subclass of :class:`ConfigError` so existing ``except ConfigError`` blocks
    still catch it; specialization is for callers who want to print a more
    targeted "please regenerate your setup.toml" message.
    """


# Public API — kept alphabetical to satisfy ruff RUF022 and aid grep.
__all__ = ["ConfigError", "SchemaVersionError", "VoiceAgentError"]
