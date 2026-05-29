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

from voice_agent_pipeline.audio.opener_bucket import OpenerBucket
from voice_agent_pipeline.config.setup import (
    GoodbyeConfig,
    GreetingConfig,
    OpenersConfig,
    SttConfig,
)
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


def minimal_goodbye_phrases() -> list[str]:
    """Return a one-entry goodbye list — the smallest valid value."""
    return ["bye"]


def minimal_goodbye_config(**overrides: object) -> GoodbyeConfig:
    """Build a :class:`GoodbyeConfig` with a single dummy goodbye."""
    overrides.setdefault("phrases", minimal_goodbye_phrases())
    return GoodbyeConfig(**overrides)  # type: ignore[arg-type]


def minimal_openers_by_bucket() -> dict[OpenerBucket, list[str]]:
    """Return one opener per function bucket — the smallest valid bucket dict.

    Story 6.2: every :data:`OpenerBucket` Literal value must have
    ≥1 opener entry or the OpenersConfig model_validator raises.
    Tests that construct configs via ``model_construct`` need this.
    """
    return {
        "thinking": ["hmm"],
        "acknowledge": ["yeah"],
        "look_up": ["let me check"],
        "delegate": ["let me look that up for you"],
        "react": ["oh"],
    }


def minimal_openers_config(**overrides: object) -> OpenersConfig:
    """Build an :class:`OpenersConfig` with one opener per bucket (Story 6.2)."""
    overrides.setdefault("phrases_by_bucket", minimal_openers_by_bucket())
    return OpenersConfig(**overrides)  # type: ignore[arg-type]
