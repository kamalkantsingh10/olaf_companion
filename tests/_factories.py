"""Shared test-only factory helpers (Story 4.5).

Story 4.5 introduced two ``model_validator``-gated config fields with
no Python defaults — the canonical operator-edited copies live in
``setup.toml``:

- :attr:`SttConfig.clarification_prompts` — must be non-empty.
- :attr:`GreetingConfig.greetings_by_mood` — must have ≥1 entry per
  :data:`Mood` Literal value.

Tests that construct these configs directly (without going through
the TOML loader) need minimal valid values to satisfy the
validators. The helpers below provide one-entry-per-list defaults.

Why a regular module rather than pytest fixtures: callers include
both fixtures (which can use other fixtures) and plain helpers
inside ``model_construct`` calls, where pytest fixtures aren't in
scope. A regular Python module fits both.
"""

from voice_agent_pipeline.config.setup import GreetingConfig, SttConfig
from voice_agent_pipeline.schemas.mood_event import Mood


def minimal_clarification_prompts() -> list[str]:
    """Return a one-entry clarification list — the smallest valid value."""
    return ["huh?"]


def minimal_greetings_by_mood() -> dict[Mood, list[str]]:
    """Return one greeting per mood — the smallest valid bucket dict."""
    return {
        "calm": ["hi"],
        "happy": ["hi!"],
        "playful": ["yo"],
        "curious": ["yeah?"],
        "thoughtful": ["mm"],
        "sleepy": ["mmh"],
        "grumpy": ["yeah"],
        "excited": ["hey!"],
    }


def minimal_stt_config(**overrides: object) -> SttConfig:
    """Build a :class:`SttConfig` with the smallest-valid clarification list."""
    overrides.setdefault("clarification_prompts", minimal_clarification_prompts())
    return SttConfig(**overrides)  # type: ignore[arg-type]


def minimal_greeting_config(**overrides: object) -> GreetingConfig:
    """Build a :class:`GreetingConfig` with one greeting per mood."""
    overrides.setdefault("greetings_by_mood", minimal_greetings_by_mood())
    return GreetingConfig(**overrides)  # type: ignore[arg-type]
