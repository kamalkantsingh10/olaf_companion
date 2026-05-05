"""structlog processor that drops sensitive fields before JSON serialization.

This processor sits in the middle of the structlog pipeline (between
``_require_event_field`` and the JSON renderer in :mod:`logging.setup`).
Every log line is funneled through here, so this is the single chokepoint
for keeping credentials, raw audio, and (at INFO+) transcripts out of the
log files.

Three defenses, applied in order to every event_dict key:

1. **Exact-match denylist** — never log opaque audio buffers. Even at DEBUG.
2. **Substring pattern match** (case-insensitive) — catches the common
   ``*_api_key``, ``*_token``, ``*_password``, ``*_secret`` shapes regardless
   of which provider the credential is for.
3. **Transcript gating** — ``transcript`` and ``user_text`` are dropped at
   INFO and above; they only appear in ``debug.log`` when ``LOG_LEVEL=DEBUG``
   (FR39).

The ``logger`` and ``method_name`` parameters are required by structlog's
processor signature even though we don't use them — keep them, don't rename.
"""

import re
from typing import Any

from structlog.types import EventDict

# Exact-match denylist. These keys are dropped at every log level; even DEBUG
# shouldn't dump raw PCM into ``debug.log`` (file size, privacy, and
# usefulness all push the same direction).
DENYLIST_EXACT: frozenset[str] = frozenset({"audio_bytes", "audio_data", "pcm"})

# Substring patterns. Any key whose name *contains* any of these (case-
# insensitive) is dropped. Matches both naming styles: ``cartesia_api_key``
# (snake) and ``CartesiaApiKey`` (camel). Architecture mandates snake_case
# everywhere (CLAUDE.md rule #5) but defenders shouldn't trust developers.
DENYLIST_PATTERNS: tuple[str, ...] = ("api_key", "token", "password", "secret")

# Gated keys: present in DEBUG output, dropped everywhere else. FR39's intent
# is "logs are useful for post-mortem but never leak conversation content
# unless the operator opts in via LOG_LEVEL=DEBUG".
TRANSCRIPT_KEYS: frozenset[str] = frozenset({"transcript", "user_text"})

# Pre-compile the alternation regex once at import time. ``re.escape`` guards
# against future patterns that might contain regex metachars.
_PATTERN_RE = re.compile(
    "|".join(re.escape(p) for p in DENYLIST_PATTERNS),
    re.IGNORECASE,
)


def redact_sensitive_fields(
    logger: Any,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Return a *copy* of ``event_dict`` with sensitive keys removed.

    Args:
        logger: Unused; required by structlog's processor signature.
        method_name: Unused; required by structlog's processor signature.
        event_dict: Mutable mapping built up by earlier processors. We treat
            it as read-only and return a fresh dict to avoid surprising any
            downstream processor that might still hold a reference.

    Returns:
        A new event dict with denylist keys removed and transcripts gated by
        the current log level.
    """
    out: dict[str, Any] = {}
    # Read level out of the dict (placed there by structlog.stdlib.add_log_level
    # earlier in the pipeline). Default to INFO so a misconfigured pipeline
    # still errs on the side of redacting.
    level = str(event_dict.get("level", "INFO")).upper()

    for k, v in event_dict.items():
        # Defense 1: opaque audio buffers — always drop.
        if k in DENYLIST_EXACT:
            continue
        # Defense 2: anything that looks like a credential — always drop.
        if _PATTERN_RE.search(k):
            continue
        # Defense 3: conversation content — drop unless DEBUG.
        if k in TRANSCRIPT_KEYS and level != "DEBUG":
            continue
        out[k] = v
    return out
