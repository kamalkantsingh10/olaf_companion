"""Pipecat ``LocalAudioTransport`` wiring (mic input + speaker output since Story 2.1).

This module is the second of two places PyAudio is imported (the other is
:mod:`audio.devices`). Pipecat's :class:`LocalAudioTransport` wraps PyAudio
internally and exposes a frame-based async interface that the pipeline
assembly (``pipeline.py``) consumes — both ``transport.input()`` (mic) and
``transport.output()`` (speaker) hang off the same transport object.

The architecture pins the v1 sample rate at 16 kHz mono S16LE — Whisper,
Porcupine, and Cartesia all agree on this format, so we match here on
both directions and avoid a resampler in the hot path. Story 2.1 wired
the speaker side; Story 5.1 may add barge-in tunables (sustained-voice
threshold, energy floor) on top of this scaffold.
"""

# Verified import path against pipecat-ai 1.1.0 (the version pinned in
# pyproject.toml). If you bump pipecat, re-verify; the package layout has
# shifted between 0.x and 1.x.
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from voice_agent_pipeline.audio.devices import AudioDeviceIndices
from voice_agent_pipeline.config.setup import SetupConfig

# 16 kHz mono S16LE — Whisper + Porcupine + Cartesia standard. Pinning here
# means the whole pipeline runs in this format end-to-end on both directions;
# no resampler in the hot path.
_SAMPLE_RATE = 16000


def build_audio_transport(
    config: SetupConfig,
    indices: AudioDeviceIndices,
) -> LocalAudioTransport:
    """Construct a Pipecat ``LocalAudioTransport`` configured for mic + speaker.

    Story 1.5 enabled mic input only; Story 2.1 turned ``audio_out_enabled``
    on and wired ``output_device_index`` from ``indices.output_index`` so
    Cartesia (Story 2.3) and the test-tone smoke check (Story 2.1) can play
    through the configured speaker.

    Args:
        config: Validated :class:`SetupConfig`. Reserved for future per-story
            audio params (e.g. Story 5.1's barge-in tunables); not consumed
            here yet beyond the implicit format pin.
        indices: Resolved device indices from :func:`resolve_audio_devices`.
            ``input_index`` / ``output_index`` may be ``None`` on platforms
            where PyAudio's default device is acceptable; in that case
            Pipecat picks the system default for that side.

    Returns:
        A configured :class:`LocalAudioTransport`. The pipeline assembly
        layer (``pipeline.py``) wires its ``input()`` end as the source of
        the chain and its ``output()`` end as the sink.
    """
    del config  # reserved for Story 5.1 barge-in tunables; ignored for now

    params = LocalAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_channels=1,
        audio_out_channels=1,
        audio_in_sample_rate=_SAMPLE_RATE,
        audio_out_sample_rate=_SAMPLE_RATE,
        input_device_index=indices.input_index,
        output_device_index=indices.output_index,
    )
    return LocalAudioTransport(params)
