"""Unit tests for the :func:`build_stt_backend` factory.

The factory is the v1/v2 selection seam — Story v2 will add a
``"hailo-whisper"`` branch with no caller changes. These tests guard the
contract: ``"whisper-cpu"`` returns a :class:`WhisperBackend`; anything
else raises :class:`ConfigError` naming the unsupported value.
"""

import pytest

from voice_agent_pipeline.config.setup import SttConfig
from voice_agent_pipeline.errors import ConfigError
from voice_agent_pipeline.stt import build_stt_backend
from voice_agent_pipeline.stt.whisper_cpu import WhisperBackend


def _config(backend: str = "whisper-cpu", device: str = "cpu") -> SttConfig:
    """Build a SttConfig for the factory; defaults pin to CPU to avoid CUDA probing."""
    return SttConfig(
        backend=backend,
        model="small",
        compute_type="int8",
        device=device,
        low_confidence_threshold=0.5,
    )


def test_factory_returns_whisper_backend_for_whisper_cpu() -> None:
    """The supported ``"whisper-cpu"`` identifier yields a WhisperBackend."""
    result = build_stt_backend(_config("whisper-cpu"))
    assert isinstance(result, WhisperBackend)


def test_factory_raises_for_unknown_backend() -> None:
    """An unrecognized backend identifier raises ConfigError listing the supported set."""
    with pytest.raises(ConfigError) as exc_info:
        build_stt_backend(_config("openai-whisper"))
    msg = str(exc_info.value)
    assert "openai-whisper" in msg
    assert "whisper-cpu" in msg


def test_resolve_device_passes_through_explicit_value() -> None:
    """An explicit device value (not ``"auto"``) is forwarded as-is.

    We can't easily assert what _resolve_device returns for "auto" without
    knowing torch availability, so the deterministic check is "explicit
    device passes through unchanged."
    """
    backend = build_stt_backend(_config("whisper-cpu", device="cpu"))
    assert isinstance(backend, WhisperBackend)
    # Internal access — the field name is part of the package's intended
    # extensibility surface (Story 4.4 / Hailo backend will read it).
    assert backend._device == "cpu"
