"""Tests for :func:`voice_agent_pipeline.logging.redaction.redact_sensitive_fields`.

Three layers of defense are tested independently:

1. The exact-match denylist (``audio_bytes``, ``audio_data``, ``pcm``).
2. The substring patterns (``api_key``, ``token``, ``password``, ``secret``)
   matched case-insensitively.
3. Transcript gating (``transcript`` / ``user_text`` dropped at INFO+,
   kept at DEBUG only).

Tests pass ``None`` for the structlog ``logger`` and ``"info"`` for the
``method_name`` since the processor doesn't use either.
"""

from typing import Any

from voice_agent_pipeline.logging.redaction import redact_sensitive_fields


def _redact(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Run the processor on a fresh copy of ``event_dict`` and return a plain dict.

    Wrapping the result with ``dict(...)`` covers two concerns at once: it
    decouples our test assertions from structlog's ``EventDict`` alias type,
    and it ensures no shared mutable state survives between tests.
    """
    return dict(redact_sensitive_fields(None, "info", dict(event_dict)))


def test_drops_audio_bytes_field() -> None:
    """Defense layer 1: the exact-match key ``audio_bytes`` is removed at any level."""
    out = _redact({"event": "audio.captured", "audio_bytes": b"\x00\x01\x02"})
    assert "audio_bytes" not in out
    assert out["event"] == "audio.captured"


def test_drops_pcm_and_audio_data() -> None:
    """Defense layer 1, completeness: ``pcm`` and ``audio_data`` also drop."""
    out = _redact({"event": "x", "pcm": b"...", "audio_data": b"..."})
    assert "pcm" not in out
    assert "audio_data" not in out


def test_drops_keys_matching_patterns() -> None:
    """Defense layer 2: snake_case credential-shaped keys are removed."""
    out = _redact(
        {
            "event": "auth",
            "cartesia_api_key": "sk_xxx",
            "bearer_token": "abc",
            "user_password": "p",
            "client_secret": "s",
        }
    )
    for k in ("cartesia_api_key", "bearer_token", "user_password", "client_secret"):
        assert k not in out


def test_pattern_match_is_case_insensitive() -> None:
    """Defense layer 2: camelCase / mixed-case credentials still drop."""
    out = _redact({"event": "x", "Cartesia_API_Key": "sk_xxx", "BearerToken": "abc"})
    assert "Cartesia_API_Key" not in out
    assert "BearerToken" not in out


def test_transcript_dropped_at_info() -> None:
    """Defense layer 3: ``transcript`` does NOT appear at INFO."""
    out = _redact({"event": "stt.final", "level": "INFO", "transcript": "hi there"})
    assert "transcript" not in out


def test_transcript_kept_at_debug() -> None:
    """Defense layer 3 inverse: ``transcript`` IS retained at DEBUG (FR39 opt-in)."""
    out = _redact({"event": "stt.final", "level": "DEBUG", "transcript": "hi there"})
    assert out["transcript"] == "hi there"


def test_user_text_dropped_at_info() -> None:
    """Defense layer 3: ``user_text`` follows the same gating rule as ``transcript``."""
    out = _redact({"event": "router.dispatched", "level": "INFO", "user_text": "hi"})
    assert "user_text" not in out


def test_user_text_kept_at_debug() -> None:
    """Defense layer 3 inverse: ``user_text`` IS retained at DEBUG."""
    out = _redact({"event": "router.dispatched", "level": "DEBUG", "user_text": "hi"})
    assert out["user_text"] == "hi"


def test_unrelated_keys_kept() -> None:
    """Innocent keys (event, count, session_id) pass through untouched."""
    out = _redact({"event": "x", "count": 42, "session_id": "abc-123"})
    assert out["event"] == "x"
    assert out["count"] == 42
    assert out["session_id"] == "abc-123"


def test_default_level_treated_as_info() -> None:
    """A dict with no ``level`` key defaults to INFO behavior — fail-closed.

    If the event_dict somehow reaches us before the ``add_log_level``
    processor (a misconfiguration), we still redact transcripts. Better to
    drop a useful debug breadcrumb than to leak transcripts in production.
    """
    out = _redact({"event": "x", "transcript": "hi"})
    assert "transcript" not in out
