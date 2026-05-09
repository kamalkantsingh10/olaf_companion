"""Unit tests for the Talker factory in :mod:`voice_agent_pipeline.turn`.

The factory's job is **provider routing** — given a fully-loaded
:class:`SetupConfig`, pick the right (api_key, model, base_url) tuple
for the configured provider and construct a :class:`Talker`. These
tests pin that routing contract; the per-class behaviour lives in
:mod:`tests.unit.turn.test_talker`.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from tests._factories import minimal_greeting_config, minimal_stt_config
from voice_agent_pipeline import turn as turn_module
from voice_agent_pipeline.config.setup import (
    AudioConfig,
    SetupConfig,
    TalkerConfig,
    WakewordConfig,
    _GeminiTalkerSection,
    _GroqTalkerSection,
    _OpenAITalkerSection,
)
from voice_agent_pipeline.errors import ConfigError, StartupValidationError
from voice_agent_pipeline.turn import build_talker, validate_credentials
from voice_agent_pipeline.turn import talker as talker_module


@pytest.fixture
def system_prompt_file(tmp_path: Path) -> Path:
    p = tmp_path / "talker_system.md"
    p.write_text("You are OLAF.", encoding="utf-8")
    return p


def _build_setup(
    *,
    provider: str,
    openai_key: str | None = None,
    groq_key: str | None = None,
    gemini_key: str | None = None,
    system_prompt_path: Path,
) -> SetupConfig:
    """Construct a :class:`SetupConfig` with the requested provider + keys.

    Bypasses ``load_setup_config`` (no TOML / .env round-trip) — the
    factory only consumes config attributes, so direct construction
    keeps the tests focused on routing rather than file loading.
    """
    return SetupConfig.model_construct(
        schema_version=2,
        picovoice_access_key=SecretStr("stub-pico"),
        openai_api_key=SecretStr(openai_key) if openai_key else None,
        groq_api_key=SecretStr(groq_key) if groq_key else None,
        gemini_api_key=SecretStr(gemini_key) if gemini_key else None,
        audio=AudioConfig(input_device_name="m", output_device_name="s"),
        wakeword=WakewordConfig(model_path=Path("models/x.ppn")),
        # Story 4.5: stt + greeting have no Python defaults; provide
        # minimal valid values so SetupConfig's default_factory chain
        # doesn't trip on validation.
        stt=minimal_stt_config(),
        greeting=minimal_greeting_config(),
        talker=TalkerConfig(
            provider=provider,  # type: ignore[arg-type]
            max_tokens=128,
            system_prompt_path=system_prompt_path,
            openai=_OpenAITalkerSection(model="gpt-5.4-nano"),
            groq=_GroqTalkerSection(model="llama-3.1-8b-instant"),
            gemini=_GeminiTalkerSection(model="gemini-2.5-flash"),
        ),
    )


def _stub_openai(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the talker module's openai with a recorder; return the kwargs sink."""
    sink: dict[str, Any] = {}

    def _construct(**kw: Any) -> Any:
        sink.update(kw)
        return MagicMock()

    fake_module = MagicMock()
    fake_module.AsyncOpenAI = MagicMock(side_effect=_construct)
    fake_module.APIError = type("APIError", (Exception,), {})
    monkeypatch.setattr(talker_module, "openai", fake_module)
    return sink


def test_build_talker_for_openai_provider(
    system_prompt_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider='openai' → AsyncOpenAI gets the OpenAI key + base_url=None (SDK default)."""
    init_kwargs = _stub_openai(monkeypatch)

    config = _build_setup(
        provider="openai",
        openai_key="real-openai-key",
        system_prompt_path=system_prompt_file,
    )
    talker = build_talker(config)

    assert init_kwargs["api_key"] == "real-openai-key"
    assert init_kwargs.get("base_url") is None
    assert talker._model == "gpt-5.4-nano"  # type: ignore[attr-defined]


def test_build_talker_for_groq_provider(
    system_prompt_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider='groq' → Groq key + Groq's openai-compatible base_url."""
    init_kwargs = _stub_openai(monkeypatch)

    config = _build_setup(
        provider="groq",
        groq_key="real-groq-key",
        system_prompt_path=system_prompt_file,
    )
    talker = build_talker(config)

    assert init_kwargs["api_key"] == "real-groq-key"
    assert init_kwargs["base_url"] == "https://api.groq.com/openai/v1"
    assert talker._model == "llama-3.1-8b-instant"  # type: ignore[attr-defined]


def test_build_talker_for_gemini_provider(
    system_prompt_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider='gemini' → Gemini key + Gemini's openai-compatible base_url."""
    init_kwargs = _stub_openai(monkeypatch)

    config = _build_setup(
        provider="gemini",
        gemini_key="real-gemini-key",
        system_prompt_path=system_prompt_file,
    )
    talker = build_talker(config)

    assert init_kwargs["api_key"] == "real-gemini-key"
    assert init_kwargs["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert talker._model == "gemini-2.5-flash"  # type: ignore[attr-defined]


def test_build_talker_raises_when_active_provider_key_missing(
    system_prompt_file: Path,
) -> None:
    """provider='groq' but GROQ_API_KEY missing → ConfigError naming the missing var.

    The error context includes the env-var name so the operator can
    fix `.env` from the error message alone (no source-spelunking).
    """
    config = _build_setup(
        provider="groq",
        # No groq_key supplied — but openai key IS set, proving the
        # factory ignores non-active provider keys.
        openai_key="some-openai-key",
        system_prompt_path=system_prompt_file,
    )
    with pytest.raises(ConfigError) as exc_info:
        build_talker(config)
    assert exc_info.value.context.get("missing_env_var") == "GROQ_API_KEY"
    assert exc_info.value.context.get("provider") == "groq"


def test_build_talker_raises_when_openai_key_missing(
    system_prompt_file: Path,
) -> None:
    """provider='openai' but OPENAI_API_KEY missing → ConfigError naming OPENAI_API_KEY."""
    config = _build_setup(
        provider="openai",
        # All three keys absent.
        system_prompt_path=system_prompt_file,
    )
    with pytest.raises(ConfigError) as exc_info:
        build_talker(config)
    assert exc_info.value.context.get("missing_env_var") == "OPENAI_API_KEY"


def test_build_talker_raises_when_gemini_key_missing(
    system_prompt_file: Path,
) -> None:
    """provider='gemini' but GEMINI_API_KEY missing → ConfigError naming GEMINI_API_KEY."""
    config = _build_setup(
        provider="gemini",
        groq_key="not-the-active-provider",
        system_prompt_path=system_prompt_file,
    )
    with pytest.raises(ConfigError) as exc_info:
        build_talker(config)
    assert exc_info.value.context.get("missing_env_var") == "GEMINI_API_KEY"


# --- validate_credentials ---


def test_validate_credentials_calls_models_retrieve_with_active_model(
    system_prompt_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe issues ``models.retrieve(<active_provider_model>)`` against the right base_url."""
    import asyncio

    sink: dict[str, Any] = {}
    fake_module = MagicMock()
    fake_client = MagicMock()
    retrieve_calls: list[Any] = []

    async def _retrieve(model_id: str) -> Any:
        retrieve_calls.append(model_id)
        return MagicMock()

    fake_client.models.retrieve = _retrieve

    def _construct_client(**kw: Any) -> Any:
        sink.update(kw)
        return fake_client

    fake_client.models.retrieve = _retrieve
    fake_module.AsyncOpenAI = MagicMock(side_effect=_construct_client)
    fake_module.APIError = type("APIError", (Exception,), {})
    # ``validate_credentials`` lives in turn/__init__.py and imports openai
    # at module level there — so the patch must hit ``turn_module.openai``,
    # not ``talker_module.openai``.
    monkeypatch.setattr(turn_module, "openai", fake_module)

    config = _build_setup(
        provider="groq",
        groq_key="real-groq-key",
        system_prompt_path=system_prompt_file,
    )
    asyncio.run(validate_credentials(config))

    # Right base_url + key for Groq, AND the retrieve was called with
    # the Groq sub-block's model — proves _resolve picks the matching
    # model identifier per provider.
    assert sink["base_url"] == "https://api.groq.com/openai/v1"
    assert sink["api_key"] == "real-groq-key"
    assert retrieve_calls == ["llama-3.1-8b-instant"]


def test_validate_credentials_wraps_failure_as_startup_validation_error(
    system_prompt_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad key / removed model surfaces as StartupValidationError, not raw SDK error."""
    import asyncio

    fake_module = MagicMock()
    fake_client = MagicMock()

    fake_api_error = type("APIError", (Exception,), {})
    boom = fake_api_error("401 unauthorized")

    async def _retrieve(model_id: str) -> Any:
        raise boom

    fake_client.models.retrieve = _retrieve
    fake_module.AsyncOpenAI = MagicMock(return_value=fake_client)
    fake_module.APIError = fake_api_error
    monkeypatch.setattr(turn_module, "openai", fake_module)

    config = _build_setup(
        provider="openai",
        openai_key="bad-key",
        system_prompt_path=system_prompt_file,
    )
    with pytest.raises(StartupValidationError) as exc_info:
        asyncio.run(validate_credentials(config))

    assert exc_info.value.__cause__ is boom
    assert exc_info.value.context.get("stage") == "talker"
    assert exc_info.value.context.get("provider") == "openai"
    assert "401" in exc_info.value.context.get("reason", "")


def test_validate_credentials_propagates_missing_key_error(
    system_prompt_file: Path,
) -> None:
    """If active provider's key is missing, the probe raises ConfigError before SDK contact."""
    import asyncio

    config = _build_setup(
        provider="gemini",
        # No gemini_key.
        system_prompt_path=system_prompt_file,
    )
    with pytest.raises(ConfigError) as exc_info:
        asyncio.run(validate_credentials(config))
    assert exc_info.value.context.get("missing_env_var") == "GEMINI_API_KEY"
