"""Unit tests for the :func:`build_stt_backend` factory + :func:`validate_credentials`.

The factory is the v1/v2 selection seam — sprint-change-proposal-2026-05-12
added the ``"groq"`` branch and flipped the v1 default to it. These tests
guard the contract: each supported backend identifier returns the right
concrete type, missing credentials raise :class:`ConfigError`, and
``validate_credentials`` dispatches correctly.
"""

from pathlib import Path

import openai
import pytest
from pydantic import SecretStr

from tests._factories import minimal_goodbye_config, minimal_greeting_config
from voice_agent_pipeline.config.setup import (
    AudioConfig,
    SetupConfig,
    SttConfig,
    WakewordConfig,
)
from voice_agent_pipeline.errors import ConfigError, StartupValidationError
from voice_agent_pipeline.stt import (
    GroqAsrBackend,
    build_stt_backend,
    validate_credentials,
)
from voice_agent_pipeline.stt.whisper_cpu import WhisperBackend


def _stt(backend: str = "groq", **overrides: object) -> SttConfig:
    """Build a SttConfig with non-empty clarification_prompts (validator gate)."""
    base: dict[str, object] = {
        "backend": backend,
        "model": "small",
        "compute_type": "int8",
        "device": "cpu",
        "low_confidence_threshold": 0.5,
        "clarification_prompts": ["huh?"],
    }
    base.update(overrides)
    return SttConfig(**base)  # type: ignore[arg-type]


_DEFAULT_GROQ_KEY = SecretStr("test")


def _setup(stt: SttConfig, groq_api_key: SecretStr | None = _DEFAULT_GROQ_KEY) -> SetupConfig:
    """Build a SetupConfig good enough for the factory + probe.

    ``model_construct`` bypasses validation — fine for these tests because
    we're exercising the factory's own dispatch logic, not the loader's
    field requirements.
    """
    return SetupConfig.model_construct(
        schema_version=3,
        picovoice_access_key=SecretStr("stub-pico"),
        groq_api_key=groq_api_key,
        cartesia_api_key=SecretStr("stub-cartesia"),
        audio=AudioConfig(input_device_name="m", output_device_name="s"),
        wakeword=WakewordConfig(model_path=Path("models/x.ppn")),
        stt=stt,
        # Explicit greeting + goodbye configs: their default_factory hits
        # a model_validator that requires non-empty buckets; passing the
        # minimal valid shape here avoids that during model_construct.
        greeting=minimal_greeting_config(),
        goodbye=minimal_goodbye_config(),
    )


def test_factory_returns_groq_backend_for_groq_default() -> None:
    """v1 default ``backend = "groq"`` yields a GroqAsrBackend."""
    result = build_stt_backend(_setup(_stt("groq")))
    assert isinstance(result, GroqAsrBackend)


def test_factory_returns_whisper_backend_for_whisper_cpu() -> None:
    """Offline ``backend = "whisper-cpu"`` yields a WhisperBackend."""
    result = build_stt_backend(_setup(_stt("whisper-cpu")))
    assert isinstance(result, WhisperBackend)


def test_factory_raises_for_groq_with_missing_api_key() -> None:
    """``backend = "groq"`` without GROQ_API_KEY in .env raises ConfigError."""
    with pytest.raises(ConfigError) as exc_info:
        build_stt_backend(_setup(_stt("groq"), groq_api_key=None))
    msg = str(exc_info.value)
    # Operator-friendly error names the missing env var.
    assert "GROQ_API_KEY" in msg
    assert "groq" in msg


def test_resolve_device_passes_through_explicit_value() -> None:
    """An explicit device value (not ``"auto"``) reaches WhisperBackend unchanged."""
    backend = build_stt_backend(_setup(_stt("whisper-cpu", device="cpu")))
    assert isinstance(backend, WhisperBackend)
    # Internal field is the backend's intended extensibility surface.
    assert backend._device == "cpu"


async def test_validate_credentials_noop_for_whisper_cpu() -> None:
    """The on-device backend has no network surface; the probe is a no-op."""
    # No mocking required — the function should simply return without
    # touching anything. If it accidentally instantiates an openai client,
    # that's a bug.
    await validate_credentials(_setup(_stt("whisper-cpu")))


async def test_validate_credentials_calls_groq_models_retrieve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Groq probe calls AsyncOpenAI.models.retrieve with the configured model."""
    captured: dict[str, object] = {}

    class _StubModels:
        async def retrieve(self, model: str) -> object:
            captured["model"] = model
            return object()

    class _StubAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured["init_kwargs"] = kwargs
            self.models = _StubModels()

    # Monkeypatch only the SDK class — Protocol-boundary mocking per
    # CLAUDE.md rule #7.
    monkeypatch.setattr(openai, "AsyncOpenAI", _StubAsyncOpenAI)

    await validate_credentials(_setup(_stt("groq", groq_model="whisper-large-v3-turbo")))

    assert captured["model"] == "whisper-large-v3-turbo"
    init_kwargs = captured["init_kwargs"]
    assert isinstance(init_kwargs, dict)
    assert init_kwargs["base_url"] == "https://api.groq.com/openai/v1"


async def test_validate_credentials_wraps_api_error_for_groq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An openai.APIError surfaces as StartupValidationError, not the raw SDK error."""

    class _StubModels:
        async def retrieve(self, model: str) -> object:
            del model
            # APIError requires (message, request, body); shorthand using a
            # bare BadRequestError is simpler but the base APIError is what
            # the production code catches.
            raise openai.APIError("nope", request=None, body=None)  # type: ignore[arg-type]

    class _StubAsyncOpenAI:
        def __init__(self, **kwargs: object) -> None:
            del kwargs
            self.models = _StubModels()

    monkeypatch.setattr(openai, "AsyncOpenAI", _StubAsyncOpenAI)

    with pytest.raises(StartupValidationError) as exc_info:
        await validate_credentials(_setup(_stt("groq")))
    assert exc_info.value.context["stage"] == "stt"
    assert exc_info.value.context["backend"] == "groq"
