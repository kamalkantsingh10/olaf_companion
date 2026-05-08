"""Stderr-only startup-phase reporter — operator-facing checklist UX.

The structlog/JSON pipeline in :mod:`logging.setup` is optimized for
post-mortem grep — every line is a self-describing JSON record. That's
the right shape for ``logs/voice-agent.log`` but a poor shape for the
operator running ``just run`` who wants to see, at a glance:

- Did config load?
- Did each external dependency probe pass?
- If something failed, *which* stage and *why*?

Without this module, the operator reads a wall of JSON and has to
mentally parse ``startup.validated.X`` events interleaved with
``httpx`` HTTP request lines. With it, they see a clean checklist::

    ─── STARTUP ─────────────────────────────
    [ ✓ ] config loaded                   (2ms)
    [ ✓ ] wakeword validated              (12ms)
    [ ✓ ] talker validated   (groq)       (570ms)
    [ ✓ ] cartesia validated              (157ms)
    [ ✓ ] audio devices                   (268ms)
    [ ✓ ] stt model loaded                (1.9s)
    [ ✓ ] expression map loaded            (22ms)
    [ ✓ ] publisher connected              (317ms)
    [ ✗ ] orchestrator daemon       FAILED — ConnectError
              url: http://localhost:8001/health
    ─────────────────────────────────────────

Design constraints
------------------

- **Stderr-only.** The structlog console renderer (when
  ``LOG_CONSOLE=true``) writes to stdout. By writing the checklist to
  stderr we avoid any visual interleaving when both go to the same TTY,
  and we also keep stdout clean for any future "machine-readable" use.
- **Files unaffected.** Every step still emits its existing structlog
  ``startup.validated.X`` event into ``voice-agent.log``. The reporter
  is purely an additional human-readable surface, not a replacement.
- **Stdout console handler quieted during startup.** When
  ``LOG_CONSOLE=true``, we'd otherwise see *both* the checklist on
  stderr AND the structlog console output on stdout, mixed together.
  ``__aenter__`` raises any stdout :class:`~logging.StreamHandler` to
  ``WARNING`` for the duration of startup; ``mark_startup_complete``
  restores the level. WARN/ERROR-level events still surface — they
  matter even during startup.
- **Failure context is unpacked.** ``StartupValidationError(stage=...,
  reason=..., url=...)`` carries its diagnostic data in
  :attr:`VoiceAgentError.context`. The reporter pulls those fields out
  of the exception so the operator sees ``reason`` on the headline and
  every other key as an indented ``  url: ...`` detail line.

Lifetime
--------

Use as an ``async with`` context manager spanning the whole startup
phase::

    async with StartupReporter() as reporter:
        async with reporter.stage("config", "config loaded"):
            config = load_setup_config()
        async with reporter.stage("wakeword", "wakeword validated"):
            await _validate_wakeword_credentials(config)
        # ... more stages ...
        await run_pipeline(config, reporter=reporter)
        # run_pipeline calls reporter.mark_startup_complete() right
        # before the pipecat runner starts, so the closing rule prints
        # before the first runtime log line.

If a stage raises, the ``stage()`` context manager prints the ``[ ✗ ]``
line and re-raises. ``__aexit__`` then prints the closing rule (in red)
and restores the suppressed log levels. If everything succeeds and
``mark_startup_complete()`` was already called, ``__aexit__`` is a no-op.
"""

import logging as _stdlib_logging
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import IO

# Description column width for [ ✓ ] / [ ✗ ] lines. Wide enough to fit
# all current stage descriptions ("orchestrator daemon" is the longest
# at ~20 chars, "talker validated   (groq)" with provider suffix peaks
# around 26) without wrapping; keeps the timing column at a stable
# horizontal position for visual scanning.
_DESCRIPTION_WIDTH = 32

# ANSI color codes. Used only when stderr is a TTY — pipes/journals get
# plain text. ``DIM`` is used for the rules; ``GREEN`` / ``RED`` for the
# checkmarks.
_ANSI = {
    "green": "\x1b[32m",
    "red": "\x1b[31m",
    "dim": "\x1b[2m",
    "reset": "\x1b[0m",
}


class StartupReporter:
    """Stderr-only checklist printer for the process startup phase.

    Independent of structlog — writes plain text directly to its own
    stream. JSON file logs (via ``configure_logging``) continue to
    capture everything in parallel; this class is purely additive.

    Not thread-safe — startup is single-task by design. The async
    context manager surface is for symmetry with the pipeline's
    ``async with`` style; the actual writes are synchronous.
    """

    def __init__(self, *, stream: IO[str] | None = None) -> None:
        """Create a reporter writing to ``stream`` (default ``sys.stderr``).

        Args:
            stream: Where to write the banner / checklist / footer.
                Tests pass an ``io.StringIO`` to inspect output;
                production gets ``sys.stderr``.
        """
        # Resolve sys.stderr at call time, not module load time, so
        # tests that monkeypatch sys.stderr (e.g. capsys) see the
        # current value.
        self._stream: IO[str] = stream if stream is not None else sys.stderr
        # TTY detection drives both color output and Unicode glyph
        # choice. Falls back to plain text if isatty() raises (some
        # captured streams in pytest do).
        try:
            self._tty = self._stream.isatty()
        except (AttributeError, ValueError):
            self._tty = False
        # Saved levels for stdout/stderr StreamHandlers — restored on
        # ``mark_startup_complete`` / ``__aexit__``.
        self._suppressed_handlers: list[tuple[_stdlib_logging.Handler, int]] = []
        # Idempotency flag — guards the closing footer / restoration
        # against double-calls (e.g. when ``__aexit__`` runs after an
        # explicit ``mark_startup_complete`` call).
        self._closed = False

    async def __aenter__(self) -> "StartupReporter":
        """Print the opening banner and quiet stdout console handlers."""
        self._begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the checklist on exit (success or failure)."""
        # If ``mark_startup_complete`` was called explicitly during
        # ``run_pipeline``, this is a no-op. Otherwise we close here —
        # success when no exception is propagating, failure otherwise.
        if not self._closed:
            self._end(success=exc is None)

    def mark_startup_complete(self) -> None:
        """Render the closing rule and restore suppressed log levels.

        Called by ``run_pipeline`` immediately before the pipecat
        runner starts, so the operator sees the footer line before any
        runtime log noise. Idempotent — subsequent calls (including the
        ``__aexit__`` cleanup) are no-ops.
        """
        if self._closed:
            return
        self._end(success=True)

    def refresh_console_suppression(self) -> None:
        """Re-quiet stdout/stderr handlers after new ones attach.

        Called by ``__main__`` after :func:`configure_logging` runs —
        that step may attach a fresh ``LOG_CONSOLE`` stdout
        ``StreamHandler`` that didn't exist when ``__aenter__``
        originally ran. Without this refresh, structlog's
        pretty-printed ``startup.validated.X`` events would mix with
        the stderr checklist.

        Idempotent: handlers already in the suppressed list keep their
        saved level; newly-attached handlers are added with their
        current level captured.
        """
        # Avoid double-saving the same handler — a re-suppress should
        # not overwrite the *original* level with the already-WARNING
        # level we set last time.
        already_seen = {h for h, _ in self._suppressed_handlers}
        root = _stdlib_logging.getLogger()
        for handler in root.handlers:
            if handler in already_seen:
                continue
            stream = getattr(handler, "stream", None)
            if stream in (sys.stdout, sys.stderr):
                self._suppressed_handlers.append((handler, handler.level))
                handler.setLevel(_stdlib_logging.WARNING)

    @asynccontextmanager
    async def stage(self, code: str, description: str) -> AsyncGenerator[None]:
        """Time and report a single startup stage.

        Args:
            code: Short identifier (e.g. ``"orchestrator"``). Echoed in
                the failure detail block so operators can cross-
                reference with the structlog ``stage`` field in
                ``voice-agent.log``.
            description: Human-readable description rendered on the
                checklist line (e.g. ``"orchestrator daemon"``).

        Usage::

            async with reporter.stage("config", "config loaded"):
                config = load_setup_config()
        """
        started_ns = time.perf_counter_ns()
        try:
            yield
        except BaseException as exc:
            elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            self._render_failure(code, description, exc, elapsed_ms)
            # Re-raise — the reporter is observational, not a swallower.
            # The structlog ``startup.failed`` CRITICAL still fires from
            # the caller in __main__.py.
            raise
        else:
            elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            self._render_success(description, elapsed_ms)

    # ------------------------------------------------------------------
    # Internal rendering / lifecycle helpers
    # ------------------------------------------------------------------

    def _begin(self) -> None:
        """Print the opening banner and suppress stdout/stderr handlers."""
        # Opening rule — same width as the closing rule in
        # ``_end``. Dim color on TTY so the rules recede visually
        # behind the bracketed checklist items.
        self._write(self._color("─── STARTUP ─────────────────────────────\n", "dim"))
        self._suppress_console_handlers()

    def _end(self, *, success: bool) -> None:
        """Print the closing rule and restore handler levels.

        ``success`` controls only the rule color — green/dim on success,
        red on failure. The detail content (the [ ✗ ] line and indented
        context lines) was already rendered inline by ``_render_failure``.
        """
        self._closed = True
        color = "dim" if success else "red"
        self._write(self._color("─────────────────────────────────────────\n", color))
        self._restore_console_handlers()

    def _render_success(self, description: str, elapsed_ms: float) -> None:
        """Print one ``[ ✓ ] description (timing)`` line."""
        check = self._color(self._glyph_check(), "green")
        timing = _format_timing(elapsed_ms)
        # Pad ``description`` to the fixed column width so the timing
        # column stays vertically aligned across rows.
        line = f"[ {check} ] {description:<{_DESCRIPTION_WIDTH}} ({timing})\n"
        self._write(line)

    def _render_failure(
        self,
        code: str,
        description: str,
        exc: BaseException,
        elapsed_ms: float,
    ) -> None:
        """Print the ``[ ✗ ] description FAILED — reason`` line + context.

        Pulls ``reason``, ``url``, ``stage``, etc. out of
        ``exc.context`` (a :class:`VoiceAgentError` attribute). For
        non-VoiceAgentError exceptions the context dict will be empty
        and we fall back to the exception class name as the reason.
        """
        # Grab the exception's structured context if present. ``getattr``
        # with default ``{}`` covers the case where ``stage()`` wraps a
        # callable that raised a non-VoiceAgentError (Python builtins,
        # third-party libs) — those don't have ``.context`` and we still
        # render something useful.
        context: dict[str, object] = getattr(exc, "context", {}) or {}
        reason = str(context.get("reason") or type(exc).__name__)
        cross = self._color(self._glyph_cross(), "red")
        # Headline: same column layout as the success line so visual
        # alignment is preserved when failures appear mid-checklist.
        headline = (
            f"[ {cross} ] {description:<{_DESCRIPTION_WIDTH}} "
            f"{self._color('FAILED', 'red')} — {reason}\n"
        )
        self._write(headline)
        # Detail block — every other context key, indented under the
        # headline. Skip ``reason`` (already on the headline) and
        # ``stage`` (the operator already knows which stage failed —
        # the description above is enough), surface the rest.
        for key, value in context.items():
            if key in {"reason", "stage"}:
                continue
            self._write(f"          {key}: {value}\n")
        # If the exception had no context at all, surface str(exc) as
        # a single ``detail:`` line so operators have *something* to go
        # on. VoiceAgentError instances always have non-empty context
        # (its ``_format`` method renders the same data into str(exc))
        # so this branch only fires for unexpected non-project exceptions.
        if not context:
            self._write(f"          detail: {exc}\n")
        # Discard ``code`` — kept in the signature for symmetry with
        # the success path's structlog ``stage`` field, but the visual
        # output already conveys which stage failed via ``description``.
        del code, elapsed_ms

    # ------------------------------------------------------------------
    # Console-handler suppression — keeps stdout from drowning the
    # checklist when LOG_CONSOLE=true is set (the default in
    # ``just run``).
    # ------------------------------------------------------------------

    def _suppress_console_handlers(self) -> None:
        """Raise stdout/stderr StreamHandlers to WARNING for startup.

        Walks every handler attached to the root logger. ``StreamHandler``
        instances whose underlying stream is ``sys.stdout`` or ``sys.stderr``
        get bumped to WARNING; their original level is saved for
        restoration in ``_restore_console_handlers``.

        ``RotatingFileHandler`` (a subclass of ``FileHandler``, itself a
        subclass of ``StreamHandler``) is excluded by the explicit stream
        check — file handlers wrap a file object, not stdout/stderr, so
        the ``stream is sys.stdout / sys.stderr`` test correctly skips them.

        Implementation shares the discovery loop with
        :meth:`refresh_console_suppression` so callers can re-quiet the
        console after new handlers attach mid-startup (e.g. after
        ``configure_logging`` runs).
        """
        # Delegate to the refresh path — the discovery + level-save
        # logic is identical; the only difference is whether anything
        # was suppressed beforehand. ``refresh`` handles the "nothing
        # yet" case naturally.
        self.refresh_console_suppression()

    def _restore_console_handlers(self) -> None:
        """Restore the levels saved in ``_suppress_console_handlers``."""
        for handler, level in self._suppressed_handlers:
            handler.setLevel(level)
        self._suppressed_handlers.clear()

    # ------------------------------------------------------------------
    # I/O + decoration helpers
    # ------------------------------------------------------------------

    def _write(self, text: str) -> None:
        """Write to the configured stream and flush.

        Flushing on every line is fine — startup is at most ~10 lines
        and we want each one to surface immediately, not buffered until
        process end (which can hide the [ ✗ ] line in a crash).
        """
        self._stream.write(text)
        self._stream.flush()

    def _color(self, text: str, code: str) -> str:
        """Wrap ``text`` in ANSI codes when the stream is a TTY.

        Pipes / journalctl capture / pytest's capsys all return
        ``False`` from ``isatty()`` and get plain text — no escape
        sequences leak into log files or test snapshots.
        """
        if not self._tty:
            return text
        prefix = _ANSI.get(code, "")
        return f"{prefix}{text}{_ANSI['reset']}"

    def _glyph_check(self) -> str:
        """Unicode check on TTY, ASCII fallback otherwise."""
        # Modern terminals and journalctl handle ✓/✗ fine. The ASCII
        # fallback is for the rare case of an output stream that
        # rejects non-ASCII (e.g. a pytest captured stream forced to
        # 'ascii' encoding in some CI configs).
        return "✓" if self._stream_supports_unicode() else "OK"

    def _glyph_cross(self) -> str:
        """Unicode cross on TTY, ASCII fallback otherwise."""
        return "✗" if self._stream_supports_unicode() else "X"

    def _stream_supports_unicode(self) -> bool:
        """Return True if the stream's encoding can carry ✓/✗.

        Defaults to True when the encoding can't be determined — UTF-8
        is the modern default everywhere we deploy.
        """
        encoding = getattr(self._stream, "encoding", None) or "utf-8"
        try:
            "✓".encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return False
        return True


def _format_timing(elapsed_ms: float) -> str:
    """Format an elapsed millisecond count compactly.

    Sub-second elapsed times render as integer milliseconds (``"42ms"``);
    longer elapsed times collapse to one-decimal seconds (``"1.9s"``).
    Keeps the timing column visually narrow regardless of magnitude.
    """
    if elapsed_ms < 1000:
        return f"{int(elapsed_ms)}ms"
    return f"{elapsed_ms / 1000:.1f}s"
