"""Speech-to-text - on-device transcription via faster-whisper (Story 1.7).

Re-exports the Protocol surface and the factory for selecting concrete
backends. The factory is the **selection seam** — Story v2 will add a
``"hailo-whisper"`` branch with no caller changes.
"""

from voice_agent_pipeline.config.setup import SttConfig
from voice_agent_pipeline.errors import ConfigError
from voice_agent_pipeline.stt.backend import STTBackend, TranscriptionResult
from voice_agent_pipeline.stt.whisper_cpu import WhisperBackend

# Currently supported backend identifiers. v2 adds "hailo-whisper".
_SUPPORTED_BACKENDS: tuple[str, ...] = ("whisper-cpu",)


def build_stt_backend(config: SttConfig) -> STTBackend:
    """Construct the configured STT backend.

    Story v1 supports ``"whisper-cpu"`` only. v2 adds ``"hailo-whisper"``
    (Pi 5 + Hailo-8L NPU); the Protocol from Story 1.4 keeps callers
    unchanged across that swap.

    Args:
        config: Validated :class:`SttConfig` from the loader.

    Returns:
        A constructed (but **not yet loaded**) backend. The caller must
        ``await backend.load()`` before the first ``transcribe`` call.

    Raises:
        ConfigError: If ``config.backend`` isn't in the supported set.
    """
    if config.backend == "whisper-cpu":
        return WhisperBackend(
            model_size=config.model,
            compute_type=config.compute_type,
            device=_resolve_device(config.device),
        )
    raise ConfigError(stt_backend=config.backend, supported=list(_SUPPORTED_BACKENDS))


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
    "STTBackend",
    "TranscriptionResult",
    "WhisperBackend",
    "build_stt_backend",
]
