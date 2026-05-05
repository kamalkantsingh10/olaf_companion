# voice-agent-pipeline

Pipecat-based voice-agent service for the OLAF Companion project. Owns the voice loop and embodiment broadcast surface — captures speech, dispatches turns, generates spoken responses with Cartesia, and publishes typed expression + lifecycle events on configurable broadcast channels.

## Install

```bash
uv sync
```

`rclpy` is installed separately via your system ROS 2 distro (e.g., `apt install ros-jazzy-rclpy`) and exposed to the venv via `PYTHONPATH`. Required from Story 3.4 onward.

## Run

```bash
just run    # starts the pipeline (mic capture from Story 1.5 onward)
just check  # lint + type-check + unit tests (must pass before commit)
just test   # full test suite
```

## Audio device setup (per machine)

The pipeline pins the microphone (and later the speaker) by **regex match
against PyAudio's device names** — names are stable across reboots and USB
hot-plug events, but the right name varies by machine.

On a new host, run:

```bash
just list-devices
```

This prints every audio device PyAudio sees, with columns:

```
idx  in_ch  out_ch  default_sr  name
0    1      0       48000.0    USB Audio Mic Array
1    0      2       48000.0    USB Audio Speaker
2    2      2       44100.0    HDA Intel PCH (hw:0,0)
...
```

Pick the row for your mic (non-zero `in_ch`) and copy something distinctive
from the `name` column into `setup.toml`:

```toml
[audio]
input_device_name = "USB.*Mic.*Array"  # regex, matched case-insensitively
```

The match uses Python's `re.search` semantics, so partial matches work.
If no device matches the regex at startup, the pipeline exits within ~1s
and prints the available device names — no need to dig through stack traces.

## Documentation

- Requirements & user journeys: `build_documents/planning-artifacts/prd.md`
- Architecture decisions: `build_documents/planning-artifacts/architecture.md`
- Epics & stories: `build_documents/planning-artifacts/epics.md`
- Per-story specs: `build_documents/implementation-artifacts/`
- AI partner rules: `CLAUDE.md`
