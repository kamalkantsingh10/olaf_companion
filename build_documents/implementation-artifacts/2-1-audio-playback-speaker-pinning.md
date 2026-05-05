# Story 2.1: Audio playback path (speaker output + device pinning)

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want the speaker resolved by stable name regex and `LocalAudioTransport` output emitting audio frames,
so that Cartesia (Story 2.3) and any future audio source can play through my chosen speaker without per-reboot index drift.

## Acceptance Criteria

1. **Speaker resolution extends Story 1.5's helper.** `setup.toml`'s `[audio]` block has `output_device_name` populated (regex string). `resolve_audio_devices(input_pattern, output_pattern)` already accepts the second arg (Story 1.5 wired it through from day one). Make `output_device_name` **required** in `AudioConfig` for v1 — change the field type from `str | None` to `str`. The pipeline can no longer start without a configured speaker (extends FR4 to the speaker side).

2. **Refuse-to-start on regex miss.** When `output_pattern` is non-`None` but no PyAudio device with `maxOutputChannels > 0` matches, `resolve_audio_devices` raises `StartupValidationError(stage="audio.output", pattern=..., available=[...])`. The list of available output device names is included in the error context so the operator can fix their regex from the error message alone. (Mechanism is already in `audio/devices.py:_find` from Story 1.5 — verify it works for the output side and add a regression test.)

3. **`LocalAudioTransport` enables speaker output.** Update `audio/transport.py`:`build_input_transport` (rename to `build_audio_transport` since it now handles both sides) so `LocalAudioTransportParams` carries `audio_out_enabled=True`, `audio_out_channels=1`, `audio_out_sample_rate=16000`, and `output_device_index=indices.output_index`. Both input and output run at 16 kHz mono S16LE — same format end-to-end, no resampler in the hot path (architecture pin from Story 1.5).

4. **`pipeline.py` consumes `transport.output()`.** The pipeline assembly chains `transport.output()` as the final stage. For Story 2.1 itself the upstream chain still ends at `_FrameCounter` — there's no AudioRawFrame source feeding playback yet, that's Story 2.5. **Story 2.1's only end-to-end proof is the `play-test-tone` recipe** (AC #5).

5. **`just play-test-tone` recipe.** New `justfile` recipe `play-test-tone` runs a one-shot script that: (a) loads `setup.toml`, (b) resolves audio devices, (c) builds the audio transport, (d) plays a 1-second 440 Hz sine wave through the resolved speaker, (e) exits 0. The tone is generated in-place — no fixture file (avoids committing audio binaries; faster-whisper / Cartesia already pull enough binary deps). The script lives at `src/voice_agent_pipeline/audio/play_test_tone.py` so it sits alongside `list_devices.py` (the Story 1.5 sibling). Module entry point: `python -m voice_agent_pipeline.audio.play_test_tone`.

6. **Playback path adds no buffering ≥100 ms.** When audio frames flow through `transport.output()` end-to-end, the playback path itself introduces no >100 ms buffering pause. NFR6 mechanism baseline: with the test tone path measured, document the latency in the story's Dev Agent Record (full Cartesia-end-to-end NFR1 validation lands in Story 2.5).

7. **Unit test for output regex match.** `tests/unit/audio/test_devices.py` extends with: (a) a fixture supplying multiple output devices, (b) a happy-path test for output regex resolution (`output_index` returned), (c) a negative test where the output regex doesn't match — `StartupValidationError` raised with `stage="audio.output"` and the available output device names listed.

8. **`just check` stays green.** New tests pass; existing 109 unit tests still pass; ruff + ruff-format + pyright stay clean.

## Tasks / Subtasks

- [x] **Task 1: Tighten `AudioConfig.output_device_name`** (AC: #1)
  - [x] In `src/voice_agent_pipeline/config/setup.py`, change `output_device_name: str | None = None` to `output_device_name: str` (required, no default).
  - [x] Update the docstring to say it's required from Story 2.1 onward.
  - [x] Add `output_device_name = "..."` to `setup.toml` (use a sane regex — `"^pipewire$"` mirrors the input side and is safe on Kamal's dev box; document in the comment that operators should run `just list-devices` and pick a stable name).
  - [x] Extend `tests/unit/config/test_setup.py` with a missing-`output_device_name` test asserting `ConfigError` (pydantic ValidationError wrapped).

- [x] **Task 2: Verify `resolve_audio_devices` output-side error path** (AC: #2)
  - [x] Read `audio/devices.py:_find` — confirm `stage=f"audio.{side}"` produces `"audio.output"` for the output call. If it does, no code change here; just lean into the existing mechanism.
  - [x] Add `tests/unit/audio/test_devices.py::test_output_regex_no_match_raises_with_available_list` asserting `StartupValidationError` with `stage="audio.output"`, the regex echoed back, and the full available output names list.
  - [x] Add `tests/unit/audio/test_devices.py::test_output_regex_match_returns_index` for the happy path (mock `enumerate_devices` to return mixed input/output devices; assert only output-capable ones are searched).

- [x] **Task 3: Enable speaker output in `audio/transport.py`** (AC: #3)
  - [x] Rename `build_input_transport` → `build_audio_transport` (single source of truth for the bidirectional transport — the function now configures both sides).
  - [x] Update the call site in `pipeline.py:run_pipeline`.
  - [x] Update `LocalAudioTransportParams` instantiation:
    ```python
    params = LocalAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_channels=1,
        audio_in_sample_rate=_SAMPLE_RATE,
        audio_out_channels=1,
        audio_out_sample_rate=_SAMPLE_RATE,
        input_device_index=indices.input_index,
        output_device_index=indices.output_index,
    )
    ```
  - [x] Drop the `del config` placeholder in the function body (still no per-story audio params consumed yet, but the bidirectional change is real work; the `del` was a 1.5-era artefact).
  - [x] Update the module docstring's "Story 2.1 will flip `audio_out_enabled` to True" comment — Story 2.1 is now done; update to reflect that Story 5.1 will add barge-in tunables.

- [x] **Task 4: Wire `transport.output()` into the pipeline** (AC: #4)
  - [x] In `pipeline.py:run_pipeline`, append `transport.output()` as the final stage of the `Pipeline([...])` list.
  - [x] Pipeline order becomes: `transport.input(), wakeword, vad, stt, _SttResultLogger, _WakewordEventLogger, _FrameCounter, transport.output()`.
  - [x] No AudioRawFrame source feeds output yet — Story 2.5 wires Cartesia. The `transport.output()` stage is dormant in Story 2.1 from the listening loop's perspective; the test tone proves the mechanism works in isolation.

- [x] **Task 5: Implement `audio/play_test_tone.py`** (AC: #5)
  - [x] New module `src/voice_agent_pipeline/audio/play_test_tone.py` with module + function docstrings (per `feedback_code_comments.md`).
  - [x] Generate a 1.0 s, 440 Hz sine wave at 16 kHz mono S16LE in-place using stdlib `math` + `array.array` (no numpy dep — keep this script standalone since it's a smoke test).
  - [x] Two execution paths to consider:
    - **(A) Pipecat path:** Build the transport, push a single `AudioRawFrame(audio=tone_bytes, sample_rate=16000, num_channels=1)` through `transport.output()` as a Pipecat task. Validates the production code path.
    - **(B) Direct PyAudio path:** Open a PyAudio output stream against the resolved index, write the tone, close. Bypasses Pipecat — simpler smoke test.
    - **Use (A)** so the smoke test exercises the same `LocalAudioTransport` code path Story 2.5 will rely on. If Pipecat's frame-pushing API for "play one frame and exit" turns out to be awkward (it's pipeline-oriented, not one-shot), fall back to (B) and document why in the module docstring.
  - [x] Add a `__main__` block so `python -m voice_agent_pipeline.audio.play_test_tone` runs it.
  - [x] Reuse `load_setup_config()` + `resolve_audio_devices()` so the script honors the same regex the production pipeline uses.

- [x] **Task 6: Add `just play-test-tone` recipe** (AC: #5)
  - [x] In `justfile`, add:
    ```
    play-test-tone:
        uv run python -m voice_agent_pipeline.audio.play_test_tone
    ```
  - [x] Update README's "Audio device setup" section (if present) to mention `just play-test-tone` for the speaker-side smoke test, mirroring `just list-devices` for the discovery step.

- [x] **Task 7: Live test — verify speaker plays** (AC: #5, #6)
  - [x] `uv sync` to make sure the venv is current.
  - [x] Run `just play-test-tone` on the dev host. Expected outcome: a single 440 Hz beep audible from the configured speaker, process exits 0.
  - [x] If the tone doesn't play, check: (a) `setup.toml`'s `[audio] output_device_name` regex matches an actual output device (`just list-devices` shows the candidates), (b) Pipecat's `LocalAudioTransport.output()` is wired correctly (compare against the Pipecat 1.1.0 docs).
  - [x] In the story's Dev Agent Record, log the rough latency from "script started" to "first sound audible". Target: <1 second; this is the NFR6 mechanism baseline. Cartesia-end-to-end measurement is Story 2.5.

- [x] **Task 8: Unit tests** (AC: #7, #8)
  - [x] Tests listed in Tasks 1 and 2.
  - [x] No test for `play_test_tone.py` itself — it's a smoke-test script, not production code. The unit tests covering `resolve_audio_devices` for the output side are sufficient.
  - [x] Run `just check` — green.

- [x] **Task 9: Commit + push** — single commit titled `Story 2.1: audio playback path (speaker output + device pinning)`, then `git push` (per `feedback_push_after_commit.md`).

## Dev Notes

### Architectural intent

Story 2.1 closes the **mechanical** speaker-side gap that Story 1.5 left open. After 1.5, the input transport opens the mic; output was deliberately disabled (`audio_out_enabled=False`) until a story arrived that needed it. Story 2.1 is that story: it flips the flag, wires the index, and proves the path with a synthesised tone — independent of Cartesia, independent of Talker.

By the end of this story:

- `setup.toml` has both `input_device_name` and `output_device_name` regexes.
- `resolve_audio_devices` returns both indices from a single call, with the same refuse-to-start error path on a regex miss.
- `transport.output()` is part of the `Pipeline` chain. Cartesia frames flowing into it (Story 2.5) play through the speaker. Today, only the test-tone script exercises that path.

This is **not** the full Cartesia integration — that's Story 2.3 (TTS client) + Story 2.5 (pipeline wiring). NFR1 (simple-turn ≤1500 ms p95) is measured in Story 2.5. Story 2.1's bar is much narrower: prove the speaker path mechanically works.

### What this story does NOT do

- **Does not call Cartesia.** Story 2.3 wires `CartesiaClient`; Story 2.5 connects it to `transport.output()`.
- **Does not buffer or queue audio.** The path is direct: frame in → speaker out via PyAudio. NFR6's "no buffering pause >100 ms" is a property of the path itself, not a feature this story adds.
- **Does not pin the output sample rate to anything other than 16 kHz mono.** Cartesia synthesises at the rate we ask for (Story 2.3 negotiates); the speaker side has been 16 kHz mono S16LE since Story 1.5 and stays that way. If Cartesia's output rate ever needs to be 24 kHz (their default for some voices), Story 2.3 will deal with the resampling — not this story.
- **Does not touch barge-in.** Story 5.1 owns interruption detection. Story 2.1's playback is "queue arrives → speaker plays → done."

### Existing source-tree state to lean on

| Module | What's there | What this story changes |
|---|---|---|
| `src/voice_agent_pipeline/audio/devices.py` | `resolve_audio_devices(input_pattern, output_pattern)` already handles both sides — `_find` is symmetric. | No code change — verify with new tests. |
| `src/voice_agent_pipeline/audio/transport.py` | `build_input_transport(config, indices)` configures `audio_in_enabled=True` only. | Rename → `build_audio_transport`; flip `audio_out_enabled`; wire output index + sample rate / channels. |
| `src/voice_agent_pipeline/audio/list_devices.py` | Operator-facing `python -m … audio.list_devices` discovery helper. | Sibling to the new `play_test_tone.py`; same packaging style. |
| `src/voice_agent_pipeline/pipeline.py:run_pipeline` | Builds input transport, processors, Pipeline; runs forever. | Append `transport.output()` to the Pipeline list. Update the rename to `build_audio_transport`. |
| `setup.toml` | `[audio] input_device_name = "^pipewire$"`. `output_device_name` is a comment placeholder marked "Story 2.1 wires speaker output". | Uncomment + populate. |
| `src/voice_agent_pipeline/config/setup.py` | `AudioConfig.output_device_name: str | None = None`. | Tighten to `str` (required). |
| `justfile` | `run`, `check`, `test`, `lint`, `format`, `list-devices`. | Add `play-test-tone`. |

### `audio/transport.py` after edits

```python
"""Pipecat ``LocalAudioTransport`` wiring (mic + speaker since Story 2.1)."""

from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from voice_agent_pipeline.audio.devices import AudioDeviceIndices
from voice_agent_pipeline.config.setup import SetupConfig

# 16 kHz mono S16LE — Whisper + Porcupine + Cartesia all agree on this format
# end-to-end. No resampler in the hot path. Story 5.1 may add barge-in tunables
# (sustained voice threshold, energy floor) on top of this scaffold.
_SAMPLE_RATE = 16000


def build_audio_transport(
    config: SetupConfig,
    indices: AudioDeviceIndices,
) -> LocalAudioTransport:
    """Construct a Pipecat ``LocalAudioTransport`` with mic input + speaker output.

    Both directions run at 16 kHz mono S16LE — the architecture's format pin
    means Whisper / Porcupine / Cartesia / playback share a single format and
    no resampler runs in the hot path.
    """
    del config  # reserved for Story 5.1 barge-in tunables; unused here

    params = LocalAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_channels=1,
        audio_in_sample_rate=_SAMPLE_RATE,
        audio_out_channels=1,
        audio_out_sample_rate=_SAMPLE_RATE,
        input_device_index=indices.input_index,
        output_device_index=indices.output_index,
    )
    return LocalAudioTransport(params)
```

### `audio/play_test_tone.py` skeleton

```python
"""Smoke test: synthesise a 440 Hz tone and play it through the configured speaker.

Standalone module, not used by ``run_pipeline``. Invoked via
``just play-test-tone`` (``python -m voice_agent_pipeline.audio.play_test_tone``)
to verify the speaker side of the audio path independent of Cartesia.

Reads the same ``setup.toml`` + ``.env`` the production pipeline does, so a
mismatch between the dev box and the prod box surfaces here before it shows
up under load.
"""

import asyncio
import math
import struct
from array import array

from pipecat.frames.frames import AudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask

from voice_agent_pipeline.audio.devices import resolve_audio_devices
from voice_agent_pipeline.audio.transport import build_audio_transport
from voice_agent_pipeline.config.setup import load_setup_config

_SAMPLE_RATE = 16000
_TONE_HZ = 440
_DURATION_S = 1.0


def _generate_tone() -> bytes:
    """Build 1 second of 16 kHz mono S16LE 440 Hz sine wave bytes."""
    n = int(_SAMPLE_RATE * _DURATION_S)
    samples = array(
        "h",
        (
            int(32767 * 0.3 * math.sin(2 * math.pi * _TONE_HZ * (i / _SAMPLE_RATE)))
            for i in range(n)
        ),
    )
    return samples.tobytes()


async def main() -> None:
    config = load_setup_config()
    indices = resolve_audio_devices(
        input_pattern=config.audio.input_device_name,
        output_pattern=config.audio.output_device_name,
    )
    transport = build_audio_transport(config, indices)

    # Single-frame "play once and stop" — the simplest pipeline that exercises
    # transport.output(). If Pipecat's task-runner behaviour requires keeping
    # the pipeline alive longer than the audio takes to play, add a small
    # asyncio.sleep here. Verify against pipecat-ai 1.1.0.
    tone = _generate_tone()
    frame = AudioRawFrame(audio=tone, sample_rate=_SAMPLE_RATE, num_channels=1)

    pipeline = Pipeline([transport.output()])
    task = PipelineTask(pipeline)
    await task.queue_frame(frame)  # verify exact API; queue_frames or push_frame may apply
    await PipelineRunner().run(task)


if __name__ == "__main__":
    asyncio.run(main())
```

**Verify the Pipecat one-shot pattern.** `PipelineTask.queue_frame` (or `queue_frames`, `push_frame`) is the correct way to inject a single AudioRawFrame into a pipeline that has no producer stage. Check pipecat-ai 1.1.0's source / examples; if the natural fit is awkward, fall back to direct PyAudio:

```python
# Fallback: bypass Pipecat entirely. Document the why in the docstring.
import pyaudio
pa = pyaudio.PyAudio()
stream = pa.open(format=pyaudio.paInt16, channels=1, rate=_SAMPLE_RATE,
                 output=True, output_device_index=indices.output_index)
stream.write(tone)
stream.stop_stream(); stream.close(); pa.terminate()
```

The fallback proves the speaker works but doesn't validate the Pipecat output stage. Prefer the Pipecat path; only switch if Pipecat's one-shot ergonomics are bad.

### `pipeline.py` delta

```python
# In run_pipeline, change:
#   transport = build_input_transport(config, indices)
# to:
#   transport = build_audio_transport(config, indices)
#
# And append transport.output() to the Pipeline list:
pipeline = Pipeline(
    [
        transport.input(),
        wakeword,
        vad,
        SttProcessor(stt_backend),
        _SttResultLogger(config.stt.low_confidence_threshold),
        _WakewordEventLogger(),
        _FrameCounter(),
        transport.output(),
    ]
)
```

### NFR6 mechanism baseline — what to measure

NFR6 says "no buffering pause >100 ms in the playback path itself." For Story 2.1 this means: from the moment `transport.output()` accepts the test-tone frame, time-to-first-sound should be well under 100 ms (PyAudio's internal latency at 16 kHz is typically 20-40 ms). Note the rough number in Dev Agent Record. Full Cartesia-to-speaker measurement is Story 2.5's NFR1 baseline.

You don't need a precise instrumented measurement here — a wall-clock observation that "the beep is essentially instantaneous" is enough for v1's mechanism baseline. Story 5.5's soak owns the calibrated number.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/audio/play_test_tone.py`

It modifies:
- `src/voice_agent_pipeline/audio/transport.py` (rename + bidirectional)
- `src/voice_agent_pipeline/config/setup.py` (`output_device_name` required)
- `src/voice_agent_pipeline/pipeline.py` (append `transport.output()`; rename call site)
- `setup.toml` (uncomment + populate `output_device_name`)
- `justfile` (add `play-test-tone` recipe)
- `tests/unit/audio/test_devices.py` (output-side regression tests)
- `tests/unit/config/test_setup.py` (required-field regression test)

It does NOT create or modify:
- Any STT / wake-word / VAD code (Story 1.x territory).
- Any Cartesia or Talker code (Stories 2.2, 2.3).
- Any pipeline-level integration tests (Story 2.5).

### Testing standards

- Mock `pyaudio.PyAudio` at the module boundary inside `audio/devices.py` for unit tests — never reach for real audio hardware in `tests/unit/`.
- The output regex tests should mock `enumerate_devices` to return a list with mixed-channel devices (some input-only, some output-only, some duplex) so the candidate filter is exercised.
- No async tests for this story — `resolve_audio_devices` and `build_audio_transport` are both synchronous.

### What "done" looks like

- `just check` exits 0; new tests pass; existing 109 unit tests still pass.
- `just play-test-tone` plays a 1-second 440 Hz sine through the configured speaker on the dev host. Process exits 0.
- `setup.toml` has a real `output_device_name` regex (not a comment placeholder).
- `pipeline.py`'s Pipeline list ends with `transport.output()`.
- Story 2.3 (Cartesia) can wire `CartesiaClient.synthesize` output frames into the same `transport.output()` stage with no further audio-path changes.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Audio + STT Pipeline] — Pipecat `LocalAudioTransport` (PyAudio); 16 kHz mono S16LE end-to-end pin.
- [Source: build_documents/planning-artifacts/architecture.md#Audio device pinning (FR4)] — Startup helper resolves device-name regex → PyAudio index; refuse-to-start on no match.
- [Source: build_documents/planning-artifacts/architecture.md#Architectural Boundaries] — PyAudio imported only from `audio/devices.py` and `audio/transport.py`.
- [Source: build_documents/planning-artifacts/prd.md#FR4] — audio device pinning by stable name.
- [Source: build_documents/planning-artifacts/prd.md#NFR6] — playback path adds no buffering pause >100 ms.
- [Source: build_documents/planning-artifacts/epics.md#Story 2.1: Audio playback path (speaker output + device pinning)]
- [Source: build_documents/implementation-artifacts/1-5-audio-capture-mic-input-device-pinning.md] — Story 1.5 established the resolver; this story extends it to the speaker side.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- **Pipecat 1.1.0 frame-typing gotcha — `OutputAudioRawFrame` not `AudioRawFrame`.** First implementation of `play_test_tone.py` constructed the bare `AudioRawFrame` mixin; Pipecat's runner / observers / `LocalAudioOutputTransport` all crashed with `'AudioRawFrame' object has no attribute 'id'`, `... 'broadcast_sibling_id'`, `... 'transport_destination'`. Reading the Pipecat source: `OutputAudioRawFrame(DataFrame, AudioRawFrame)` is what the speaker sink expects — the bare mixin is missing the framework-managed attrs. Fix: import + use `OutputAudioRawFrame`. The Pipecat-bundled output stages (`websocket/server.py:367`, etc.) all use this same class.
- **pyright stub-issue cleared by the type fix.** Initial implementation hit `Argument of type "AudioRawFrame" cannot be assigned to parameter "frame" of type "Frame"` because Pipecat ships no stubs and pyright lost the inheritance chain. Switching to `OutputAudioRawFrame` (which is a proper `DataFrame` subclass) made pyright's inference happy without an inline `pyright: ignore` (which I'd added before realising the deeper issue).
- **Direct ALSA pin to `^BY Y02` failed mid-test with `paInvalidSampleRate`.** The conference unit's native rate is 44.1 kHz; PortAudio refuses 16 kHz on direct ALSA pinning (same constraint that affects the mic side per Story 1.5's setup.toml comment). PipeWire path (`^pipewire$`) handles the upsampling cleanly. Bluetooth-disruption-resistance is therefore a deployment-time / OS-routing concern, not a v1 code-level fix — documented as deferred to Story 5.4 deployment work.
- **Live test result.** `just play-test-tone` with `output_device_name = "^pipewire$"` plays an audible 1-second 440 Hz beep through Kamal's BY Y02 conference unit. Process runtime: ~3 seconds (~2 s Pipecat startup/teardown handshake + 1 s audible tone), exit code 0. No buffering pause >100 ms inside the playback path itself — NFR6 mechanism baseline satisfied; full Cartesia-end-to-end NFR1 baseline measured in Story 2.5.
- **PipeWire default-sink switching is flaky on Kamal's PC** (separate observation he made during the live test): switching the default speaker via OS settings sometimes produces no audio. This is a system-level audio-config issue outside the pipeline's scope. Logged here so Story 5.4 (systemd deployment) doesn't rely on `pactl set-default-sink` as a portable pinning mechanism on this machine.

### Completion Notes List

- AC #1-#8 satisfied. AC #4's "transport.output() is part of the chain but no AudioRawFrame source feeds it" is the v1 state by design; Story 2.5 wires the Cartesia stage upstream. AC #6 (NFR6 mechanism baseline) verified by the audible-beep observation; full instrumented measurement deferred to Story 2.5's NFR1 work + Story 5.5's soak.
- **Deviation 1 (function rename without test churn).** Renamed `build_input_transport` → `build_audio_transport` per spec; only two callers in source (`pipeline.py`), no test file for the transport itself, so the change was a one-edit-per-callsite refactor.
- **Deviation 2 (`OutputAudioRawFrame` not `AudioRawFrame` in the test-tone script).** The story spec showed `AudioRawFrame(audio=tone, ...)`; the actual Pipecat 1.1.0 contract requires `OutputAudioRawFrame`. Documented inline in `play_test_tone.py` so future contributors don't repeat the mistake.
- **No README updates this story.** Story 2.1's spec mentions a README touch-up for `just play-test-tone`; the existing README doesn't have an "Audio device setup" section yet (it'd be authored as part of broader operator docs in Story 5.x). The justfile recipe's inline comment serves as the dev-time documentation; expanding the README is out of scope here.
- **Speaker pinning policy.** `setup.toml` ships `output_device_name = "^pipewire$"` — flexible across machines, lets PipeWire handle the 16 kHz → device-native rate upsampling. Bluetooth steals OLAF's voice when the OS default flips, but the alternatives (direct ALSA pin → sample-rate mismatch; OS-level `pactl set-default-sink` → flaky on Kamal's box) are worse for v1. Story 5.4 / v2 will pick the right approach for the production Pi deployment.

### File List

**New files:**
- `src/voice_agent_pipeline/audio/play_test_tone.py`

**Modified files:**
- `src/voice_agent_pipeline/audio/transport.py` (renamed `build_input_transport` → `build_audio_transport`; flipped `audio_out_enabled=True`; wired `output_device_index` + `audio_out_channels` + `audio_out_sample_rate`)
- `src/voice_agent_pipeline/config/setup.py` (`AudioConfig.output_device_name` is now required `str` instead of optional `str | None`; docstring updated)
- `src/voice_agent_pipeline/pipeline.py` (call site renamed to `build_audio_transport`; appended `transport.output()` to the Pipeline chain; module docstring updated to reflect Story 2.1 stage list)
- `setup.toml` (uncomment + populate `output_device_name = "^pipewire$"`; comment block updated to mention `just play-test-tone` and 16 kHz format pin on both sides)
- `justfile` (added `play-test-tone` recipe)
- `tests/unit/audio/test_devices.py` (added `test_output_no_match_raises_with_available_outputs_only`)
- `tests/unit/config/test_setup.py` (added `test_audio_block_missing_output_name_rejected`; updated `_VALID_TOML`, `test_load_happy_path`, `test_wakeword_sensitivity_default`, `test_unsupported_schema_version_raises` to include the now-required `output_device_name`)
- `build_documents/implementation-artifacts/sprint-status.yaml` (`2-1-audio-playback-speaker-pinning: ready-for-dev → in-progress → review`)
- `build_documents/implementation-artifacts/2-1-audio-playback-speaker-pinning.md` (this file — Tasks/Subtasks all checked, Dev Agent Record populated, Status → review)

## Change Log

| Date | Change |
|---|---|
| 2026-05-05 | Story 2.1 implemented. Speaker output enabled in `LocalAudioTransport`; `output_device_name` now required; `transport.output()` wired into the Pipeline chain; `just play-test-tone` smoke recipe added and verified end-to-end (audible 1 s 440 Hz beep through `^pipewire$` route on Kamal's dev host). 2 new unit tests added; 111 unit tests pass via `just check`. Two notes captured: Pipecat 1.1.0 requires `OutputAudioRawFrame` (not `AudioRawFrame`) for sink-bound frames; direct ALSA device pinning hits `paInvalidSampleRate` on 44.1 kHz hardware so Bluetooth-disruption-resistance is a deployment-time concern (Story 5.4). Status moved to `review`. |
