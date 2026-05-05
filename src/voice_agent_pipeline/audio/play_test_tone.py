"""Smoke test: synthesise a 440 Hz tone and play it through the configured speaker.

Standalone module — not imported by ``run_pipeline``. Invoked via
``just play-test-tone`` (``python -m voice_agent_pipeline.audio.play_test_tone``)
to verify the speaker side of the audio path independent of Cartesia
(Story 2.3) or Talker (Story 2.2) — useful when an operator's set up the
project on a new machine and wants to confirm their ``[audio]
output_device_name`` regex resolves to a speaker that actually makes sound.

Reads the same ``setup.toml`` + ``.env`` the production pipeline does, so
a mismatch between the dev box and the prod box surfaces here before it
shows up under load.

Implementation note (Pipecat 1.1.0):
    The script builds a one-stage ``Pipeline([transport.output()])`` and
    drives a single :class:`AudioRawFrame` through it via
    :meth:`PipelineTask.queue_frame`, followed by :class:`EndFrame` so
    the runner can shut down cleanly. This exercises the same
    ``LocalAudioTransport`` path Story 2.5's Cartesia stage will rely on
    — picking the production code path here means the smoke test
    catches the same Pipecat-version oddities the real pipeline would.
"""

import asyncio
import math
from array import array

from pipecat.frames.frames import EndFrame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask

from voice_agent_pipeline.audio.devices import resolve_audio_devices
from voice_agent_pipeline.audio.transport import build_audio_transport
from voice_agent_pipeline.config.setup import load_setup_config

# 16 kHz mono S16LE — same format the rest of the pipeline pins. 440 Hz is
# concert A; loud enough to be clearly audible without being abrasive.
# 0.3 amplitude scaler keeps the beep at a polite volume even on speakers
# that interpret 16-bit PCM at full scale as VERY loud.
_SAMPLE_RATE = 16000
_TONE_HZ = 440
_DURATION_S = 1.0
_AMPLITUDE = 0.3


def _generate_tone() -> bytes:
    """Build 1 second of 16 kHz mono S16LE 440 Hz sine wave bytes.

    Stdlib-only (``math`` + ``array``) so the smoke test stays dep-free —
    pulling in numpy here would mean a numpy import for an operator who
    just wants to confirm their speaker works.
    """
    n = int(_SAMPLE_RATE * _DURATION_S)
    samples = array(
        "h",
        (
            int(32767 * _AMPLITUDE * math.sin(2 * math.pi * _TONE_HZ * (i / _SAMPLE_RATE)))
            for i in range(n)
        ),
    )
    return samples.tobytes()


async def main() -> None:
    """Resolve the speaker, build the transport, push one tone frame, exit.

    Failure modes (each surfaces a clear message before raising):
    - ``[audio] output_device_name`` regex doesn't match any output device →
      ``StartupValidationError`` from :func:`resolve_audio_devices` listing
      the available device names.
    - PipeWire / ALSA daemon not running → PyAudio raises during transport
      construction. Diagnose with ``just list-devices`` and ``aplay -l``.
    """
    config = load_setup_config()
    indices = resolve_audio_devices(
        input_pattern=config.audio.input_device_name,
        output_pattern=config.audio.output_device_name,
    )
    transport = build_audio_transport(config, indices)

    tone = _generate_tone()
    # OutputAudioRawFrame is the DataFrame subclass that LocalAudioOutputTransport
    # expects; the bare AudioRawFrame mixin lacks the framework-managed attrs
    # (id, transport_destination) that Pipecat's runner / observers / output
    # transport require to route the frame.
    frame = OutputAudioRawFrame(audio=tone, sample_rate=_SAMPLE_RATE, num_channels=1)

    # One-stage pipeline — only the speaker sink. The mic input() end of
    # the same transport is intentionally NOT in the chain; the smoke test
    # is purely "can the speaker make sound", no audio is captured.
    pipeline = Pipeline([transport.output()])
    task = PipelineTask(pipeline)

    # Queue the tone, then EndFrame so the runner shuts down once the
    # tone has been consumed by transport.output() and played out.
    await task.queue_frame(frame)
    await task.queue_frame(EndFrame())

    await PipelineRunner().run(task)


if __name__ == "__main__":
    asyncio.run(main())
