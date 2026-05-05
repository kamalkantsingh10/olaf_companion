# Story 1.5: Audio capture path (mic input + device pinning)

Status: ready-for-dev

## Story

As Kamal,
I want the mic resolved by stable name regex and audio frames flowing into a Pipecat input pipeline,
so that Stories 1.6 (wake-word) and 1.7 (VAD + STT) have a working audio source — and `setup.toml` survives reboots and unrelated USB hot-plug events without breaking.

## Acceptance Criteria

1. `src/voice_agent_pipeline/audio/devices.py` exposes `resolve_audio_devices(input_pattern: str | None, output_pattern: str | None) -> AudioDeviceIndices` (output side stays `None` in this story; Story 2.1 wires speaker side). Returns a frozen dataclass `AudioDeviceIndices(input_index: int | None, output_index: int | None)`.

2. `resolve_audio_devices` enumerates PyAudio devices and matches `input_pattern` (a regex string) against device names (case-insensitive, `re.search` semantics). On match, returns the numeric `index`. On no match (when a non-`None` pattern is provided), raises `StartupValidationError(stage="audio.input", pattern=..., available=[...device names...])`.

3. `setup.toml` gains an `[audio]` block with at minimum `input_device_name = "..."` (regex string). The `SetupConfig` model from Story 1.2 is extended with a typed `audio: AudioConfig` nested model (`pydantic.BaseModel` with `extra="forbid"`), where `AudioConfig.input_device_name: str` and `AudioConfig.output_device_name: str | None = None` (optional now; Story 2.1 makes it required when speaker output lands).

4. `src/voice_agent_pipeline/audio/transport.py` exposes `build_input_transport(config: SetupConfig, indices: AudioDeviceIndices) -> LocalAudioTransport` returning a Pipecat `LocalAudioTransport` configured with the resolved input device index. Output side is left disabled in this story (Pipecat's `LocalAudioTransportParams(audio_in_enabled=True, audio_out_enabled=False)`).

5. Pipeline assembly: `src/voice_agent_pipeline/pipeline.py` exposes `async run_pipeline(config: SetupConfig) -> None` that builds the input transport, wires it to a no-op terminal stage that increments a frame counter (debug-level log every 1000 frames: `event="audio.frame_counter"`, `count=N`), and runs the Pipecat pipeline indefinitely until SIGTERM.

6. `__main__.py` is restructured: `main()` becomes `async def main()` invoked via `asyncio.run`. It loads config → configures logging → installs SIGTERM handler (sets a shutdown event) → resolves audio devices → calls `run_pipeline(config)`. On `VoiceAgentError` (any subclass), it logs `event="startup.failed"` at CRITICAL and exits non-zero. On `KeyboardInterrupt`/SIGTERM, it cleanly cancels the pipeline task and exits 0.

7. Given `setup.toml` `[audio] input_device_name = "USB.*Mic.*"` and a real or mocked PyAudio device matching the regex, `just run` starts and audio frames begin flowing (verifiable via the debug counter when `LOG_LEVEL=DEBUG`).

8. Given no PyAudio device matches the regex, `just run` exits non-zero within ~1s with a CRITICAL `event="startup.failed"` log including the regex and the list of available device names.

9. Given a USB hot-plug event on an *unrelated* device (verified manually — see Dev Notes), the pipeline does not crash and the existing input frames continue to flow. (Full NFR11 soak validation is Story 5.5; this story validates the mechanism on the dev host.)

10. Tests: `tests/unit/audio/test_devices.py` covers regex match, no-match raises with available-list, and case-insensitivity. No INFO-level log line in the test output contains the substring `audio_bytes` (redaction enforcement check). `just check` stays green; `just test` includes the unit test and excludes the live audio integration (gated behind `RUN_LIVE_AUDIO=true`).

## Tasks / Subtasks

- [ ] **Task 1: Extend `SetupConfig` with `[audio]` block** (AC: #3)
  - [ ] In `src/voice_agent_pipeline/config/setup.py`, add nested `AudioConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")` and fields `input_device_name: str`, `output_device_name: str | None = None`.
  - [ ] Add `audio: AudioConfig` field to `SetupConfig`.
  - [ ] Update `setup.toml`: add `[audio]` block with `input_device_name = "USB.*Mic.*"` (placeholder regex Kamal will tune to his actual mic).
  - [ ] Extend `tests/unit/config/test_setup.py` with `test_audio_block_loads`, `test_audio_block_extra_key_rejected`, `test_audio_block_missing_input_name_rejected`.

- [ ] **Task 2: Implement `audio/devices.py`** (AC: #1, #2)
  - [ ] `AudioDeviceIndices` frozen dataclass.
  - [ ] `resolve_audio_devices(input_pattern, output_pattern)`:
    1. Open a PyAudio instance.
    2. Enumerate devices via `pa.get_device_count()` + `pa.get_device_info_by_index(i)`.
    3. For each pattern, find the first device whose `name` matches (`re.search(pattern, name, re.IGNORECASE)`).
    4. Filter: input candidates must have `maxInputChannels > 0`; output candidates must have `maxOutputChannels > 0`.
    5. On match → record index. On no match for a non-`None` pattern → raise `StartupValidationError(stage=..., pattern=..., available=[...])`.
    6. Close PyAudio (use `try/finally` or context-manage if convenient).
    7. Return `AudioDeviceIndices`.
  - [ ] Snippet in Dev Notes.

- [ ] **Task 3: Implement `audio/transport.py`** (AC: #4)
  - [ ] `build_input_transport(config, indices) -> LocalAudioTransport`:
    1. Import `LocalAudioTransport` and `LocalAudioTransportParams` from `pipecat.transports.local.audio` (verify exact path against installed pipecat-ai version).
    2. Construct params with `audio_in_enabled=True`, `audio_out_enabled=False`, `audio_in_channels=1`, `audio_in_sample_rate=16000` (Whisper/Porcupine standard), `input_device_index=indices.input_index`.
    3. Return the constructed transport.
  - [ ] Add a TODO comment that Story 2.1 enables `audio_out_enabled=True` and wires `output_device_index`.

- [ ] **Task 4: Implement minimal `pipeline.py`** (AC: #5)
  - [ ] `run_pipeline(config: SetupConfig) -> None`:
    1. Resolve devices (input only).
    2. Build input transport.
    3. Build a `Pipeline([input_transport, _FrameCounter()])` where `_FrameCounter` is a Pipecat `FrameProcessor` subclass that counts incoming `AudioRawFrame` and logs every 1000 frames at DEBUG.
    4. Build a `PipelineRunner` and `PipelineTask`; `await runner.run(task)` until cancellation.
  - [ ] `_FrameCounter` lives in `pipeline.py` as a private inner class for now; if it needs sharing, promote to `audio/frame_counter.py` (defer until needed).
  - [ ] Snippet in Dev Notes.

- [ ] **Task 5: Restructure `__main__.py`** (AC: #6)
  - [ ] `main()` becomes `async def main() -> int`.
  - [ ] Wire SIGTERM via `loop.add_signal_handler(signal.SIGTERM, shutdown_event.set)`.
  - [ ] Top-level: `asyncio.run(main())`; bubble exit code.
  - [ ] On `KeyboardInterrupt` (Ctrl-C local), cancel the pipeline task; exit 0.
  - [ ] On `VoiceAgentError` (any subclass), log `event="startup.failed", error=str(e), error_class=type(e).__name__` at CRITICAL via the structlog logger; exit 1.
  - [ ] Snippet in Dev Notes.

- [ ] **Task 6: Tests** (AC: #10)
  - [ ] `tests/unit/audio/__init__.py` (empty).
  - [ ] `tests/unit/audio/test_devices.py`:
    - Mock PyAudio enumeration via a fixture that returns a curated list of fake devices (input-only, output-only, neither, both).
    - `test_input_regex_matches_returns_index` — regex `"USB.*Mic.*"` matches device named `"USB Audio Mic Array"` → returns its index.
    - `test_input_no_match_raises_with_available_list` — no match → `StartupValidationError` with `available` field listing the names.
    - `test_match_is_case_insensitive` — pattern `"usb"` matches `"USB Audio Mic"`.
    - `test_input_only_devices_not_chosen_for_output` (parametrized with output_pattern path) — though output side is None in this story, the code path should still not pick input-only devices for output if invoked.
    - `test_no_input_pattern_returns_none_index` — passing `None` for `input_pattern` returns `AudioDeviceIndices(input_index=None, output_index=None)`.
    - `test_no_audio_bytes_in_logs` — emit a few logs during the resolve flow; assert no log line contains `audio_bytes` field. (Sanity check that this story's code never logs raw audio.)
  - [ ] `tests/integration/test_audio_capture.py` (live, gated):
    - `@pytest.mark.skipif(os.environ.get("RUN_LIVE_AUDIO") != "true", reason="requires real mic")`
    - Build the full pipeline, run for 2 seconds, assert that at least one `audio.frame_counter` DEBUG event appears in `debug.log`.

- [ ] **Task 7: Manual USB hot-plug verification** (AC: #9)
  - [ ] Start the pipeline with `LOG_LEVEL=DEBUG just run`.
  - [ ] Plug in or unplug an *unrelated* USB device (keyboard, drive — NOT the mic).
  - [ ] Verify the pipeline keeps running and the frame counter keeps incrementing.
  - [ ] Document in commit message that manual NFR11 mechanism check passed; full soak is Story 5.5.

- [ ] **Task 8: Commit** — single commit titled `Story 1.5: audio capture path (mic input + device pinning)`.

## Dev Notes

### Architectural intent

This story makes the pipeline **alive** for the first time — until now it loaded config + logged "startup.completed" and exited. Now `just run` starts an asyncio event loop, opens the mic, and stays running until SIGTERM. It's the moment the project transitions from "library" to "service."

The device-pinning-by-name pattern (regex match against PyAudio's enumeration) is the architecture's standard fix for the well-known PyAudio gotcha: numeric device indices shift across reboots and USB hot-plug events. Pinning by name (regex, since OS sometimes appends `(hw:N,M)` suffixes) is reliable.

### What this story does NOT do

- **Does not enable speaker output.** `audio_out_enabled=False`. Story 2.1 enables it.
- **Does not detect wake-word.** Frames flow into a no-op counter; wake-word detection is Story 1.6.
- **Does not run VAD or STT.** Story 1.7.
- **Does not validate Picovoice/Anthropic/Cartesia credentials.** Their respective stories add startup validation. This story only validates audio device presence.
- **Does not implement barge-in or full lifecycle.** Stories 5.1 and 4.4.

### Pipecat version compatibility

The exact import paths for `LocalAudioTransport` and `LocalAudioTransportParams` may shift across pipecat-ai versions. Check the installed version (`uv pip show pipecat-ai`) and the corresponding examples in the pipecat-ai repo. As of the architecture's Batch 1 decisions, the package is `pipecat.transports.local.audio` — verify and adjust if needed. **Document the verified import path in a comment in `audio/transport.py`** for future-Kamal.

### `audio/devices.py` snippet

```python
"""Audio device resolution by stable name regex."""

import re
from dataclasses import dataclass

import pyaudio
import structlog

from voice_agent_pipeline.errors import StartupValidationError

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AudioDeviceIndices:
    input_index: int | None
    output_index: int | None


def resolve_audio_devices(
    input_pattern: str | None,
    output_pattern: str | None,
) -> AudioDeviceIndices:
    pa = pyaudio.PyAudio()
    try:
        devices = [
            (i, pa.get_device_info_by_index(i))
            for i in range(pa.get_device_count())
        ]
    finally:
        pa.terminate()

    input_index = _find(devices, input_pattern, "input", lambda d: d["maxInputChannels"] > 0)
    output_index = _find(devices, output_pattern, "output", lambda d: d["maxOutputChannels"] > 0)
    log.info(
        "audio.devices.resolved",
        input_pattern=input_pattern,
        input_index=input_index,
        output_pattern=output_pattern,
        output_index=output_index,
    )
    return AudioDeviceIndices(input_index=input_index, output_index=output_index)


def _find(devices, pattern, side, candidate_filter):
    if pattern is None:
        return None
    rx = re.compile(pattern, re.IGNORECASE)
    available = [d["name"] for _, d in devices if candidate_filter(d)]
    for idx, d in devices:
        if candidate_filter(d) and rx.search(str(d["name"])):
            return idx
    raise StartupValidationError(stage=f"audio.{side}", pattern=pattern, available=available)
```

### `audio/transport.py` snippet

```python
"""Pipecat LocalAudioTransport wiring."""

# NOTE: Verify the import path against the installed pipecat-ai version.
# As of architecture's Batch 1: pipecat.transports.local.audio
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from voice_agent_pipeline.audio.devices import AudioDeviceIndices
from voice_agent_pipeline.config.setup import SetupConfig

_SAMPLE_RATE = 16000  # Whisper + Porcupine standard


def build_input_transport(
    config: SetupConfig,
    indices: AudioDeviceIndices,
) -> LocalAudioTransport:
    params = LocalAudioTransportParams(
        audio_in_enabled=True,
        audio_out_enabled=False,  # Story 2.1 flips this on
        audio_in_channels=1,
        audio_in_sample_rate=_SAMPLE_RATE,
        input_device_index=indices.input_index,
    )
    return LocalAudioTransport(params)
```

### `pipeline.py` snippet

```python
"""Pipecat pipeline assembly + lifecycle orchestration."""

import structlog
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_agent_pipeline.audio.devices import resolve_audio_devices
from voice_agent_pipeline.audio.transport import build_input_transport
from voice_agent_pipeline.config.setup import SetupConfig

log = structlog.get_logger(__name__)


class _FrameCounter(FrameProcessor):
    def __init__(self, log_every: int = 1000) -> None:
        super().__init__()
        self._count = 0
        self._log_every = log_every

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, AudioRawFrame):
            self._count += 1
            if self._count % self._log_every == 0:
                log.debug("audio.frame_counter", count=self._count)
        await self.push_frame(frame, direction)


async def run_pipeline(config: SetupConfig) -> None:
    indices = resolve_audio_devices(
        input_pattern=config.audio.input_device_name,
        output_pattern=config.audio.output_device_name,
    )
    transport = build_input_transport(config, indices)
    pipeline = Pipeline([transport.input(), _FrameCounter()])
    task = PipelineTask(pipeline)
    runner = PipelineRunner()
    log.info("pipeline.started")
    try:
        await runner.run(task)
    finally:
        log.info("pipeline.stopped")
```

### `__main__.py` snippet (restructured)

```python
"""Voice agent pipeline entry point."""

import asyncio
import signal
import sys

import structlog

from voice_agent_pipeline.config.setup import load_setup_config
from voice_agent_pipeline.errors import VoiceAgentError
from voice_agent_pipeline.logging.setup import configure_logging
from voice_agent_pipeline.pipeline import run_pipeline


async def main() -> int:
    try:
        config = load_setup_config()
    except VoiceAgentError as e:
        print(f"startup.failed: {e}", file=sys.stderr)
        return 1

    configure_logging(config)
    log = structlog.get_logger(__name__)
    log.info("startup.completed", schema_version=config.schema_version)

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown.set)

    pipeline_task = asyncio.create_task(run_pipeline(config))
    shutdown_task = asyncio.create_task(shutdown.wait())
    done, pending = await asyncio.wait(
        [pipeline_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    try:
        await pipeline_task
    except asyncio.CancelledError:
        pass
    except VoiceAgentError as e:
        log.critical("startup.failed", error=str(e), error_class=type(e).__name__)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
```

### Why `extra="forbid"` on `AudioConfig`

Same reason as `SetupConfig`: a typo like `input_device_namee = "..."` should fail loudly at startup, not silently fall through to `None`. Architecture rule (CLAUDE.md #5 + #6 spirit).

### Manual NFR11 verification

Full 7-day soak with hot-plug events lives in Story 5.5. This story's check is the *mechanism*: prove that PyAudio's enumeration is read once at startup and the frame stream is not bound to PyAudio's device list at runtime. The simplest verification is to start the pipeline, plug in a USB device that is NOT the mic, and confirm no crash. Document the test in the commit message.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/audio/devices.py`
- `src/voice_agent_pipeline/audio/transport.py`
- `src/voice_agent_pipeline/pipeline.py` (real content, replacing Story 1.1's empty stub)
- `tests/unit/audio/__init__.py`
- `tests/unit/audio/test_devices.py`
- `tests/integration/test_audio_capture.py` (live-gated)

It modifies:
- `src/voice_agent_pipeline/config/setup.py` (nested `AudioConfig`)
- `setup.toml` (add `[audio]` block)
- `src/voice_agent_pipeline/__main__.py` (asyncio + signal handlers + run_pipeline)
- `tests/unit/config/test_setup.py` (extend with audio-block tests)

### Testing standards

- Mock PyAudio at the module boundary — use `monkeypatch` to replace `pyaudio.PyAudio` with a stub class providing the same enumerate API.
- Live audio test gated behind `RUN_LIVE_AUDIO=true` — keeps `just test` hermetic in CI/AI loops, but available for human dev runs.
- Test-fake device records: `{"name": "USB Audio Mic Array", "maxInputChannels": 1, "maxOutputChannels": 0, "defaultSampleRate": 48000.0}` etc.
- `tests/unit/audio/test_transport.py` is **not** required this story — testing Pipecat's transport behavior is integration territory, not unit. The `build_input_transport` function is too thin to warrant a unit test (would just assert call args).

### What "done" looks like

- `just check` exits 0.
- `just run` (with valid `setup.toml`, `.env`, and a real mic matching the regex) starts the pipeline; SIGINT or SIGTERM cleanly stops it with `event="pipeline.stopped"` log.
- `LOG_LEVEL=DEBUG just run` shows `audio.frame_counter` events every 1000 frames.
- Wrong regex → exits non-zero in <1s with the available device list in the log.
- Story 1.6 can begin and insert a wake-word stage between input transport and the frame counter.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Audio + STT Pipeline] — LocalAudioTransport, PyAudio
- [Source: build_documents/planning-artifacts/architecture.md#Module & File Layout] — `audio/transport.py`, `audio/devices.py`
- [Source: build_documents/planning-artifacts/architecture.md#Implementation Sequence] — item 7 (audio I/O)
- [Source: build_documents/planning-artifacts/architecture.md#Architectural Boundaries] — PyAudio imported only in `audio/transport.py` and `audio/devices.py`
- [Source: build_documents/planning-artifacts/architecture.md#Async Patterns] — sync libs wrapped in `asyncio.to_thread` (PyAudio enumeration is one-shot at startup, not on the hot path — no `to_thread` needed for `get_device_info_by_index`)
- [Source: build_documents/planning-artifacts/prd.md#FR4, FR42, FR43] — device pinning + no audio persistence + no telemetry
- [Source: build_documents/planning-artifacts/prd.md#NFR11] — USB hot-plug survival
- [Source: build_documents/planning-artifacts/epics.md#Story 1.5: Audio capture path (mic input + device pinning)]

## Dev Agent Record

### Agent Model Used

(to be filled by dev agent)

### Debug Log References

### Completion Notes List

### File List
