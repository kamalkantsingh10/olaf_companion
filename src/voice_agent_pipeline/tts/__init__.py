"""Text-to-speech — streaming TTS clients + provider-selection factory.

Two providers ship behind the :class:`TTSClient` Protocol seam:
:class:`CartesiaClient` (Sonic-3, Story 2.3) and :class:`GeminiClient`
(Live API, Story 6.5). :func:`build_tts_client` is the selection seam —
it dispatches on ``[tts] provider`` and enforces that the active provider's
API key is present, mirroring ``turn.build_talker``.
"""

from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.errors import ConfigError
from voice_agent_pipeline.tts.cartesia import CartesiaClient
from voice_agent_pipeline.tts.client import TTSClient
from voice_agent_pipeline.tts.gemini import GeminiClient


def build_tts_client(config: SetupConfig) -> TTSClient:
    """Construct the configured TTS client — provider dispatch lives here.

    Args:
        config: Validated :class:`SetupConfig` from the loader.

    Returns:
        A :class:`CartesiaClient` or :class:`GeminiClient`, per
        ``config.tts.provider``.

    Raises:
        ConfigError: If the active provider's API key is missing from
            ``.env`` (e.g. ``provider = "gemini"`` but no ``GEMINI_API_KEY``).
            The message names the missing env var so the operator can fix it
            without reading source.
    """
    provider = config.tts.provider
    if provider == "cartesia":
        if config.cartesia_api_key is None:
            raise ConfigError(
                stage="tts",
                provider="cartesia",
                missing_env_var="CARTESIA_API_KEY",
            )
        return CartesiaClient(config.tts, config.cartesia_api_key)

    if provider == "gemini":
        if config.gemini_api_key is None:
            raise ConfigError(
                stage="tts",
                provider="gemini",
                missing_env_var="GEMINI_API_KEY",
            )
        return GeminiClient(config.tts, config.gemini_api_key)

    # Unreachable while ``provider`` is a 2-value Literal — keeps the type
    # checker happy and fails loudly if the Literal is extended without
    # updating this dispatch.
    raise ConfigError(stage="tts", provider=provider, reason="unsupported provider")


__all__ = ["CartesiaClient", "GeminiClient", "TTSClient", "build_tts_client"]
