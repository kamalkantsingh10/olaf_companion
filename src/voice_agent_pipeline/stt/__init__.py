"""Speech-to-text - configurable STT backend behind the :class:`STTBackend` Protocol.

This package exposes the **selection seam** for STT inference. Story 1.4
defined the :class:`STTBackend` Protocol; Story 1.7 landed the original
``"whisper-cpu"`` implementation; sprint-change-proposal-2026-05-12 adds
``"groq"`` and flips the v1 default to it. The Protocol seam means callers
(audio capture, sequential loop, pipeline assembly) see no change across
the flip — only the factory and the config switch.

Backend matrix:

- ``"groq"`` — v1 default. Cloud STT via Groq's openai-compatible
  ``audio/transcriptions`` endpoint. See :mod:`voice_agent_pipeline.stt.groq`.
- ``"whisper-cpu"`` — on-device alternative. faster-whisper running on
  CPU/GPU. See :mod:`voice_agent_pipeline.stt.whisper_cpu`.

The startup-time credential probe (``validate_credentials``) is structured
to mirror :func:`voice_agent_pipeline.turn.validate_credentials` — one
helper, dispatches on the active backend, raises
:class:`StartupValidationError` on any failure. ``__main__.py`` Stage 3
calls it after the Talker + Cartesia probes, before the audio loop opens.
"""

import openai

from voice_agent_pipeline.config.setup import SetupConfig, SttConfig
from voice_agent_pipeline.errors import ConfigError, StartupValidationError
from voice_agent_pipeline.stt.backend import STTBackend, TranscriptionResult
from voice_agent_pipeline.stt.groq import GroqAsrBackend
from voice_agent_pipeline.stt.whisper_cpu import WhisperBackend

# Supported backend identifiers. Kept as a constant so tests can assert the
# set is exactly what the factory dispatches on, catching the "added a
# Literal value but forgot a factory branch" failure mode.
_SUPPORTED_BACKENDS: tuple[str, ...] = ("groq", "whisper-cpu")


def build_stt_backend(config: SetupConfig) -> STTBackend:
    """Construct the configured STT backend.

    Two backends ship in v1: ``"groq"`` (cloud, default) and
    ``"whisper-cpu"`` (on-device, offline alternative). The Protocol seam
    from Story 1.4 keeps callers unchanged when operators flip between
    them via ``[stt] backend`` in ``setup.toml``.

    Args:
        config: Full :class:`SetupConfig` — we read both the nested
            ``stt`` block AND the top-level ``groq_api_key`` field, so
            the factory takes the parent config rather than just the
            sub-block (parallel to ``turn.build_talker``).

    Returns:
        A constructed (but not yet ``load()``-ed) backend. The caller
        must ``await backend.load()`` before the first ``transcribe``
        call.

    Raises:
        ConfigError: If ``backend = "groq"`` is selected but the matching
            API key env var is missing from ``.env``. The error names the
            missing variable so the operator can fix it without reading
            source.
    """
    stt = config.stt
    if stt.backend == "groq":
        # GROQ_API_KEY (or whichever env var ``api_key_env`` points at)
        # must be present. We resolve from ``SetupConfig.groq_api_key``
        # rather than re-reading os.environ — pydantic-settings has
        # already loaded the env file at config-load time and the
        # secret lives on the SetupConfig field. The api_key_env field
        # exists so a future-Kamal who wants distinct STT/Talker Groq
        # keys can wire them through; for v1 the default matches
        # Talker's key reuse.
        api_key = config.groq_api_key
        if api_key is None:
            raise ConfigError(
                stage="stt",
                backend="groq",
                missing_env_var=stt.api_key_env,
            )
        return GroqAsrBackend(
            api_key=api_key,
            model=stt.groq_model,
        )

    if stt.backend == "whisper-cpu":
        return WhisperBackend(
            model_size=stt.model,
            compute_type=stt.compute_type,
            device=_resolve_device(stt.device),
        )

    # Defensive — the SttConfig Literal validator should make this branch
    # unreachable. The explicit error keeps the type checker happy and
    # gives a clean message if the Literal is ever extended without
    # updating this function.
    raise ConfigError(stt_backend=stt.backend, supported=list(_SUPPORTED_BACKENDS))


async def validate_credentials(config: SetupConfig) -> None:
    """Startup probe — confirm the active STT backend's creds + reachability.

    Called by ``__main__.py`` Stage 3 before pipeline assembly. Dispatches
    on the active backend:

    - ``"groq"``: probe Groq's ``models.retrieve(<model>)`` via the openai
      SDK. Same pattern as the Talker probe — validates the API key
      (401/403 on bad key) AND the model identifier (404 if the model
      isn't in Groq's catalog) in one call, with no token burn.
    - ``"whisper-cpu"``: no network surface to probe. The faster-whisper
      load can take 1-30 s and isn't free to validate "for real" without
      that cost, so we keep this branch a no-op and let
      :meth:`WhisperBackend.load` surface any startup failure. The
      operator still sees the model load in the structured logs.

    Raises:
        StartupValidationError: On any ``openai.APIError`` from the Groq
            probe — operator sees a clean ``startup.failed`` log + non-zero
            exit, not a stack trace from inside the SDK.
        ConfigError: If the active backend requires a key that's missing.
    """
    stt = config.stt
    if stt.backend == "groq":
        api_key = config.groq_api_key
        if api_key is None:
            raise ConfigError(
                stage="stt",
                backend="groq",
                missing_env_var=stt.api_key_env,
            )
        # Same probe shape as ``turn.validate_credentials`` (Talker side).
        # We don't reuse the Talker client because they may use different
        # api_keys / base_urls in principle, and constructing a tiny
        # throwaway client at startup costs nothing.
        client = openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url="https://api.groq.com/openai/v1",
        )
        try:
            await client.models.retrieve(stt.groq_model)
        except openai.APIError as e:
            raise StartupValidationError(
                stage="stt",
                backend="groq",
                model=stt.groq_model,
                reason=str(e),
            ) from e
        return

    if stt.backend == "whisper-cpu":
        # On-device path — nothing to probe over the network. WhisperBackend
        # .load() is the place a bad model id / corrupt cache will surface
        # at startup.
        return

    # Defensive — Literal validator should prevent this, but mirror the
    # factory's safety net.
    raise ConfigError(stt_backend=stt.backend, supported=list(_SUPPORTED_BACKENDS))


def _resolve_device(s: str) -> str:
    """Translate the config's device string into a faster-whisper device id.

    ``"auto"`` consults torch's CUDA availability if torch is importable;
    otherwise falls back to ``"cpu"``. torch is intentionally NOT a hard
    dep — faster-whisper uses CTranslate2, not torch — so a missing torch
    just means "no CUDA detection, default to CPU."
    """
    if s != "auto":
        return s
    try:
        import torch  # type: ignore[import-not-found]  # pyright: ignore[reportMissingImports]

        # torch's typing is partial without stubs in our env; the call is
        # well-defined regardless.
        return (
            "cuda"
            if torch.cuda.is_available()  # pyright: ignore[reportUnknownMemberType]
            else "cpu"
        )
    except ImportError:
        # torch isn't installed — that's fine, CTranslate2 doesn't need it.
        # We just can't detect CUDA, so default to CPU.
        return "cpu"


__all__ = [
    "GroqAsrBackend",
    "STTBackend",
    "SttConfig",
    "TranscriptionResult",
    "WhisperBackend",
    "build_stt_backend",
    "validate_credentials",
]
