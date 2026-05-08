"""Tests for :class:`voice_agent_pipeline.logging.startup.StartupReporter`.

Verifies the operator-facing startup checklist:

- Banner + closing rule frame the checklist.
- Each ``stage()`` produces one ``[ ✓ ]`` line on success with timing.
- Failures render ``[ ✗ ]`` plus indented context (stage/reason/url
  unpacked from :class:`VoiceAgentError.context`) and re-raise.
- The closing rule prints exactly once (idempotent
  ``mark_startup_complete``).
- Stdout/stderr console handlers get quieted during startup and
  restored after — file handlers are untouched.

All output is captured through an in-memory :class:`io.StringIO` so
tests can assert on the literal stream content. Test isolation for
stdlib :mod:`logging` state is provided by the autouse fixture in
``conftest.py``.
"""

import asyncio
import io
import logging as _stdlib_logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from voice_agent_pipeline.errors import StartupValidationError, VoiceAgentError
from voice_agent_pipeline.logging.startup import StartupReporter

# Strip ANSI escape codes so assertions can stay simple regardless of
# whether the stream's ``isatty()`` returned True. StringIO returns
# False so codes aren't actually emitted in these tests, but the regex
# is here for safety if a future test forces TTY mode.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _decolor(text: str) -> str:
    """Drop ANSI codes for content-only assertions."""
    return _ANSI_RE.sub("", text)


@pytest.mark.asyncio
async def test_success_renders_banner_check_and_closing_rule() -> None:
    """A successful single-stage run produces banner + ✓ line + closing rule."""
    stream = io.StringIO()
    async with StartupReporter(stream=stream) as reporter:
        async with reporter.stage("config", "config loaded"):
            pass  # Synchronous no-op; we only care about the rendered output.
    out = _decolor(stream.getvalue())
    # Three line groups: opening banner, success line, closing rule.
    assert "─── STARTUP" in out
    assert "[ ✓ ] config loaded" in out
    # Closing rule is the line of horizontal-bar characters with no
    # ``STARTUP`` text. We just check at least one rule line exists
    # *after* the success line.
    success_idx = out.index("[ ✓ ]")
    assert "─" in out[success_idx:]


@pytest.mark.asyncio
async def test_success_line_includes_timing_in_milliseconds() -> None:
    """Sub-second stage times render as ``(Xms)``."""
    stream = io.StringIO()
    async with StartupReporter(stream=stream) as reporter:
        async with reporter.stage("config", "config loaded"):
            await asyncio.sleep(0)  # Millisecond-scale; doesn't reach 1s.
    out = _decolor(stream.getvalue())
    # Match e.g. "(0ms)" / "(2ms)" — anything ending in ms.
    assert re.search(r"\(\d+ms\)", out), out


@pytest.mark.asyncio
async def test_long_stage_renders_seconds_with_one_decimal() -> None:
    """≥ 1 second stage times render as ``(X.Ys)``.

    Uses a fake clock via ``monkeypatch``-style direct manipulation —
    we don't actually want to sleep 1+ seconds in a unit test.
    """
    # Simulate a long stage by constructing the reporter, calling
    # the renderer directly, and checking the output. Bypasses the
    # real timing path — that's fine because ``_format_timing`` is
    # the formatting we're verifying.
    stream = io.StringIO()
    reporter = StartupReporter(stream=stream)
    reporter._render_success("stt model loaded", elapsed_ms=1900.0)
    out = _decolor(stream.getvalue())
    assert "(1.9s)" in out
    assert "stt model loaded" in out


@pytest.mark.asyncio
async def test_failure_renders_x_line_with_unpacked_context_and_reraises() -> None:
    """Stage failure: [ ✗ ] line + indented context lines + re-raise.

    Uses a real :class:`StartupValidationError` so the test exercises
    the actual ``VoiceAgentError.context`` attribute path.
    """
    stream = io.StringIO()
    with pytest.raises(StartupValidationError):
        async with StartupReporter(stream=stream) as reporter:
            async with reporter.stage("orchestrator", "orchestrator daemon"):
                raise StartupValidationError(
                    stage="orchestrator",
                    reason="ConnectError",
                    url="http://localhost:8001/health",
                )
    out = _decolor(stream.getvalue())
    # Headline: description + FAILED + reason. Reason comes from the
    # exception's context; class name should NOT appear when reason is set.
    assert "[ ✗ ] orchestrator daemon" in out
    assert "FAILED" in out
    assert "ConnectError" in out
    # Detail block: ``url`` indented under the headline. ``stage`` and
    # ``reason`` should NOT appear as detail lines (they're already
    # represented in description + headline).
    assert "url: http://localhost:8001/health" in out
    assert "stage: orchestrator" not in out
    assert "reason: ConnectError" not in out


@pytest.mark.asyncio
async def test_failure_without_context_falls_back_to_class_name_and_str() -> None:
    """Non-VoiceAgentError exceptions still produce something useful."""
    stream = io.StringIO()
    with pytest.raises(RuntimeError):
        async with StartupReporter(stream=stream) as reporter:
            async with reporter.stage("oops", "doomed step"):
                raise RuntimeError("kaboom")
    out = _decolor(stream.getvalue())
    # Reason falls back to the exception class name when no context.
    assert "[ ✗ ] doomed step" in out
    assert "RuntimeError" in out
    # ``detail:`` line carries str(exc) since context is empty.
    assert "detail: kaboom" in out


@pytest.mark.asyncio
async def test_mark_startup_complete_is_idempotent() -> None:
    """Calling ``mark_startup_complete`` twice prints only one closing rule."""
    stream = io.StringIO()
    async with StartupReporter(stream=stream) as reporter:
        async with reporter.stage("config", "config loaded"):
            pass
        reporter.mark_startup_complete()
        reporter.mark_startup_complete()  # Should be a no-op.
    out = _decolor(stream.getvalue())
    # One closing rule = one line consisting of only ─ characters and
    # whitespace. Count those.
    rule_lines = [line for line in out.splitlines() if line.strip() and set(line.strip()) == {"─"}]
    # One opening rule (banner has STARTUP text so doesn't match) +
    # one closing rule = exactly one match here.
    assert len(rule_lines) == 1


@pytest.mark.asyncio
async def test_console_handlers_are_quieted_then_restored() -> None:
    """Stdout/stderr StreamHandlers attached to root get raised to WARNING during startup."""
    # Attach a stdout StreamHandler at INFO — mimics the LOG_CONSOLE=true
    # path in ``configure_logging``. We don't go through configure_logging
    # itself because the autouse conftest fixture clears the root logger
    # between tests; this test owns the handler lifecycle explicitly.
    handler = _stdlib_logging.StreamHandler(sys.stdout)
    handler.setLevel(_stdlib_logging.INFO)
    _stdlib_logging.getLogger().addHandler(handler)

    try:
        stream = io.StringIO()
        async with StartupReporter(stream=stream) as reporter:
            # Mid-startup: handler should be at WARNING.
            assert handler.level == _stdlib_logging.WARNING
            async with reporter.stage("config", "config loaded"):
                pass
        # After context exit: handler restored to its original INFO level.
        assert handler.level == _stdlib_logging.INFO
    finally:
        _stdlib_logging.getLogger().removeHandler(handler)


@pytest.mark.asyncio
async def test_file_handlers_are_not_quieted(tmp_path: Path) -> None:
    """:class:`RotatingFileHandler` is left alone — only stdout/stderr get raised.

    File handlers don't write to stdout/stderr, so the reporter's
    suppression must skip them. Otherwise the JSON post-mortem log
    would lose INFO-level startup events while the operator stares at
    the checklist.
    """
    log_path = tmp_path / "voice-agent.log"
    file_handler = RotatingFileHandler(log_path, maxBytes=1024, backupCount=1)
    file_handler.setLevel(_stdlib_logging.INFO)
    _stdlib_logging.getLogger().addHandler(file_handler)

    try:
        stream = io.StringIO()
        async with StartupReporter(stream=stream):
            # File handler keeps its level even while reporter is active.
            assert file_handler.level == _stdlib_logging.INFO
        assert file_handler.level == _stdlib_logging.INFO
    finally:
        file_handler.close()
        _stdlib_logging.getLogger().removeHandler(file_handler)


@pytest.mark.asyncio
async def test_refresh_console_suppression_picks_up_late_handlers() -> None:
    """Handlers attached after ``__aenter__`` get suppressed via ``refresh_console_suppression``.

    Models the production sequence: ``__main__`` opens the reporter
    BEFORE ``configure_logging`` runs, then calls
    ``refresh_console_suppression`` after configure_logging attaches
    the LOG_CONSOLE stdout handler.
    """
    stream = io.StringIO()
    async with StartupReporter(stream=stream) as reporter:
        # Late attach — simulates configure_logging adding a stdout handler.
        late_handler = _stdlib_logging.StreamHandler(sys.stdout)
        late_handler.setLevel(_stdlib_logging.INFO)
        _stdlib_logging.getLogger().addHandler(late_handler)
        try:
            reporter.refresh_console_suppression()
            assert late_handler.level == _stdlib_logging.WARNING
        finally:
            _stdlib_logging.getLogger().removeHandler(late_handler)
        # Restore happens via __aexit__ even though we removed the handler
        # ourselves — the reporter's saved-level list still references it
        # and it must not crash on cleanup. (The level we set wouldn't
        # roundtrip because we removed the handler, but the cleanup path
        # itself must not raise.)


@pytest.mark.asyncio
async def test_aexit_on_unhandled_failure_uses_red_closing_rule() -> None:
    """When the body raises, ``__aexit__`` still closes the checklist.

    We can't easily assert ANSI red on a StringIO (isatty=False), but
    we can assert that the closing rule WAS written — i.e. cleanup
    happens even on the exception path.
    """
    stream = io.StringIO()
    with pytest.raises(VoiceAgentError):
        async with StartupReporter(stream=stream) as reporter:
            async with reporter.stage("config", "config loaded"):
                raise VoiceAgentError(detail="explosion")
    out = _decolor(stream.getvalue())
    # The [ ✗ ] line plus a closing rule line both appear in the output.
    assert "[ ✗ ] config loaded" in out
    rule_lines = [line for line in out.splitlines() if line.strip() and set(line.strip()) == {"─"}]
    assert len(rule_lines) == 1
