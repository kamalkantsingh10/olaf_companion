"""Turn router - fast/slow-path dispatch for user utterances (Stories 2.2, 2.4, 4.3).

Re-exports the three Protocol seams that the router will consume:
:class:`TalkerClient` (fast path), :class:`OrchestratorClient` (slow path),
:class:`BeliefStateClient` (per-turn belief read).

Story 2.2 adds the :func:`build_talker` factory and :func:`validate_credentials`
helper — the **selection seam** for swapping the active LLM provider via
``setup.toml``'s ``[talker] provider`` field. Three providers are wired
out of the box (OpenAI / Groq / Gemini); all three reach the same
``openai`` SDK because each exposes an openai-compatible endpoint.
"""

import openai
from pydantic import SecretStr

from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.errors import ConfigError, StartupValidationError
from voice_agent_pipeline.turn.beliefs import BeliefStateClient
from voice_agent_pipeline.turn.orchestrator import OrchestratorClient
from voice_agent_pipeline.turn.talker import (
    PROVIDER_BASE_URLS,
    PROVIDER_MAX_TOKENS_PARAM,
    Talker,
    TalkerClient,
)


def _resolve(config: SetupConfig) -> tuple[SecretStr, str, str | None]:
    """Look up (api_key, model, base_url) for the configured provider.

    Single source of truth for provider-specific routing — the
    :func:`build_talker` factory and :func:`validate_credentials` probe
    both call this so they can never disagree on which key / URL goes
    with which provider.

    Returns:
        Three-tuple of ``(api_key, model_id, base_url)``. ``base_url``
        is ``None`` for OpenAI (SDK default).

    Raises:
        ConfigError: If the active provider's API key is missing from
            ``.env`` (e.g., ``provider = "groq"`` but no ``GROQ_API_KEY``).
            The error message names the missing env var so the operator
            can fix it without reading source.
    """
    provider = config.talker.provider
    if provider == "openai":
        if config.openai_api_key is None:
            raise ConfigError(
                stage="talker",
                provider="openai",
                missing_env_var="OPENAI_API_KEY",
            )
        return config.openai_api_key, config.talker.openai.model, PROVIDER_BASE_URLS["openai"]

    if provider == "groq":
        if config.groq_api_key is None:
            raise ConfigError(
                stage="talker",
                provider="groq",
                missing_env_var="GROQ_API_KEY",
            )
        return config.groq_api_key, config.talker.groq.model, PROVIDER_BASE_URLS["groq"]

    if provider == "gemini":
        if config.gemini_api_key is None:
            raise ConfigError(
                stage="talker",
                provider="gemini",
                missing_env_var="GEMINI_API_KEY",
            )
        return (
            config.gemini_api_key,
            config.talker.gemini.model,
            PROVIDER_BASE_URLS["gemini"],
        )

    # Pydantic's Literal validator should make this branch unreachable;
    # the explicit error keeps the type checker happy and gives a clean
    # message if the literal is ever extended without updating this
    # function.
    raise ConfigError(stage="talker", provider=provider, reason="unsupported provider")


def build_talker(config: SetupConfig) -> Talker:
    """Construct the configured Talker — provider dispatch lives here.

    Story 2.2 supports ``"openai"`` / ``"groq"`` / ``"gemini"`` —
    all three reach the same ``openai`` SDK via their openai-compatible
    endpoints. Adding a fourth openai-compatible provider (Together,
    Fireworks, vLLM, self-hosted, etc.) is a one-line entry in
    :data:`PROVIDER_BASE_URLS` plus a sub-block on :class:`TalkerConfig`.

    Args:
        config: Validated :class:`SetupConfig` from the loader.

    Returns:
        A constructed :class:`Talker` ready to call ``complete(...)``.

    Raises:
        ConfigError: If the active provider's API key is missing in
            ``.env`` — see :func:`_resolve`.
    """
    api_key, model, base_url = _resolve(config)
    return Talker(
        config=config.talker,
        api_key=api_key,
        model=model,
        base_url=base_url,
        max_tokens_param=PROVIDER_MAX_TOKENS_PARAM[config.talker.provider],
    )


async def validate_credentials(config: SetupConfig) -> None:
    """Startup probe — confirm the active provider's key + model work.

    Called by ``__main__.py`` before pipeline assembly. Uses
    :meth:`AsyncOpenAI.models.retrieve` because:

    1. It validates **both** the API key (401/403 on bad key) and the
       configured model (404 if the model name doesn't exist), in one
       call. Same call works against every openai-compatible endpoint.
    2. It burns no tokens — unlike a token-1 ``chat.completions.create``
       probe, which would generate billable output.

    Raises:
        StartupValidationError: On any ``openai.APIError`` — the
            operator sees a clean ``startup.failed`` log + non-zero
            exit, not a stack trace from inside the SDK.
        ConfigError: If the active provider's API key is missing.
    """
    api_key, model, base_url = _resolve(config)
    client = openai.AsyncOpenAI(api_key=api_key.get_secret_value(), base_url=base_url)
    try:
        await client.models.retrieve(model)
    except openai.APIError as e:
        raise StartupValidationError(
            stage="talker",
            provider=config.talker.provider,
            reason=str(e),
        ) from e


__all__ = [
    "BeliefStateClient",
    "OrchestratorClient",
    "Talker",
    "TalkerClient",
    "build_talker",
    "validate_credentials",
]
