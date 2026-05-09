"""Static-random wake-greeting picker (Story 4.5).

Fired automatically on every ``sleeping → waking`` FSM transition. The
picked phrase is what the user hears — these are the actual spoken
strings, not instructions to an LLM.

Why static-random (the 2026-05-07 redesign)
-------------------------------------------

The earlier Talker-driven design called the LLM with a "greet the
user briefly" prompt + 800ms timeout + word-count gate + fallback
list. Three forces pushed toward static-random:

1. **Latency**: per-call LLM TTFB (50-400ms) eats most of NFR30's
   800ms greeting budget. Static-random is sub-microsecond.
2. **Failure-mode familiarity**: Story 3.7's `cdf3618` commit fixed
   the same class of bug for the clarification flow — Groq's small
   Llama treated instruction prompts as questions and answered
   them literally. Greeting's LLM round-trip would re-introduce
   that risk.
3. **Operator authoring is one-time work**: 30-40 entries x 8
   moods is a single curation pass. Per-mood buckets preserve the
   architectural mood-tinting (FR44) without the LLM call.

Trade-off: lose LLM novelty per call (every wake might say "yeah?"
twice in a session). With 30-40 per bucket, repetition over a
typical session feels naturally varied.

Source-of-truth
---------------

The actual greeting strings live in ``setup.toml`` under
``[greeting.greetings_by_mood]``. This module reads them via the
:class:`GreetingConfig` model passed in by the pipeline assembly.
No Python-side default lists.

Fallback chain (operator misconfiguration safety)
-------------------------------------------------

Even though :class:`GreetingConfig.model_validator` rejects empty
buckets at startup, :func:`trigger_greeting` keeps a defensive
fallback chain in case a future story injects greetings dynamically:

1. Pick from ``greetings_by_mood[mood]`` if non-empty.
2. Else pick from ``greetings_by_mood["calm"]`` if non-empty.
3. Else return literal ``"hey"`` — last-resort safety.

The chain is idempotent under repeated calls; ``random.choice`` is
the only randomness source.
"""

import random

import structlog

from voice_agent_pipeline.schemas.mood_event import Mood

log = structlog.get_logger(__name__)


def trigger_greeting(mood: Mood, greetings_by_mood: dict[Mood, list[str]]) -> str:
    """Pick a random greeting for ``mood``. Sub-microsecond.

    Args:
        mood: The current mood — drives which bucket to pick from.
            Bounded by the :data:`Mood` Literal so a typo at the
            call site is a static error.
        greetings_by_mood: Mapping of mood → list of greeting
            strings. Sourced from ``setup.toml`` via
            :class:`GreetingConfig.greetings_by_mood`.

    Returns:
        A randomly-picked greeting string. Empty buckets fall through
        to the ``calm`` bucket; missing ``calm`` falls through to
        ``"hey"``. The fallback chain only fires for operator
        misconfiguration that bypassed startup validation.
    """
    # The `or` short-circuits on empty list (treats `[]` as falsy)
    # AND on missing key (`.get` returns `None`, which is falsy).
    # Both cases trigger the next link in the chain. Pyright sees
    # the return as `list[str]` because the literal `["hey"]` pins
    # the union members.
    bucket: list[str] = greetings_by_mood.get(mood) or greetings_by_mood.get("calm") or ["hey"]
    # ruff S311: random.choice is fine here — greeting variety is a
    # UX concern, not a cryptographic one. No security boundary.
    text = random.choice(bucket)  # noqa: S311
    # Greeting text is short (2-8 words), operator-authored, no
    # privacy concern — same logging precedent as Story 2.5's
    # ``prompt`` / ``response`` fields on ``talker.completion``.
    log.info("greeting.picked", mood=mood, text=text)
    return text
