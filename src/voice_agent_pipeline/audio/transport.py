"""Pipecat ``LocalAudioTransport`` wiring.

This module is the second of two places PyAudio is imported (the other is
:mod:`audio.devices`). Pipecat's :class:`LocalAudioTransport` wraps PyAudio
internally and exposes a frame-based async interface that the pipeline
assembly (``pipeline.py``) consumes.

The architecture pins the v1 sample rate at 16 kHz mono S16LE — Whisper
and Porcupine both expect this format, so we match here and avoid
resampling in the hot path.
"""

# Verified import path against pipecat-ai 1.1.0 (the version pinned in
# pyproject.toml). If you bump pipecat, re-verify; the package layout has
# shifted between 0.x and 1.x.
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from voice_agent_pipeline.audio.devices import AudioDeviceIndices
from voice_agent_pipeline.config.setup import SetupConfig

# 16 kHz mono S16LE — Whisper + Porcupine standard. Pinning here means the
# whole pipeline runs in this format end-to-end; no resampler in the hot path.
_SAMPLE_RATE = 16000


def build_input_transport(
    config: SetupConfig,
    indices: AudioDeviceIndices,
) -> LocalAudioTransport:
    """Construct a Pipecat ``LocalAudioTransport`` configured for mic-only capture.

    Story 1.5 enables input only; Story 2.1 will flip ``audio_out_enabled``
    to True and wire ``output_device_index`` from ``indices.output_index``.

    Args:
        config: Validated :class:`SetupConfig`. Reserved for future per-story
            audio params (e.g. Story 5.1's barge-in tunables); not consumed
            here yet.
        indices: Resolved device indices from
            :func:`resolve_audio_devices`. ``input_index`` may be ``None``
            on platforms where PyAudio's default device is acceptable; in
            that case Pipecat picks the system default.

    Returns:
        A configured :class:`LocalAudioTransport`. The pipeline assembly
        layer (``pipeline.py``) wires its ``input()`` end into the
        :class:`Pipeline`.
    """
    del config  # reserved for Story 5.1 barge-in tunables; ignored for now

    params = LocalAudioTransportParams(
        audio_in_enabled=True,
        # Story 2.1 will set this to True and wire output_device_index.
        audio_out_enabled=False,
        audio_in_channels=1,
        audio_in_sample_rate=_SAMPLE_RATE,
        input_device_index=indices.input_index,
    )
    return LocalAudioTransport(params)
