# voice-agent-pipeline

Pipecat-based voice-agent service for the OLAF Companion project. Owns the voice loop and embodiment broadcast surface — captures speech, dispatches turns, generates spoken responses with Cartesia, and publishes typed expression + lifecycle events on configurable broadcast channels.

## Install

```bash
uv sync
```

`rclpy` is installed separately via your system ROS 2 distro (e.g., `apt install ros-jazzy-rclpy`) and exposed to the venv via `PYTHONPATH`. Required from Story 3.4 onward.

## Run

```bash
just run             # starts the pipeline; full simple-turn loop is alive (Story 2.5)
just check           # lint + type-check + unit tests (must pass before commit)
just test            # full test suite (including the integration test)
just list-devices    # print PyAudio devices for setup.toml regex tuning
just play-test-tone  # 1-second 440Hz beep through the configured speaker
```

After `just run`, say **"Hey OLAF, what time is it?"** — within ~1.5s
Ooppi's voice (Cartesia Tessa) responds through the configured
speaker. Expected log flow per turn (in `./logs/voice-agent.log`):

```
wakeword.detected  keyword="hey olaf"
stt.transcript     confidence=0.85 end_to_transcript_ms=...
talker.responded   latency_ms=... clarification=false
talker.completion  provider=groq prompt_tokens=... completion_tokens=...
tts.first_frame    ttfb_ms=... voice_id=...
tts.synthesis_complete  chunk_count=... byte_total=...
```

Low-confidence transcripts (mumbled, distant, masked) trigger the
clarification dialog instead — see the `stt.low_confidence` WARN with
`action="clarify"`.

## Required secrets (`.env`)

Three external services power the v1 simple-turn loop. Keys live in
`.env` (gitignored, `chmod 0600`):

| Variable | Service | Purpose |
|---|---|---|
| `PICOVOICE_ACCESS_KEY` | Picovoice | Wake-word ("Hey OLAF") detection |
| `OPENAI_API_KEY` / `GROQ_API_KEY` / `GEMINI_API_KEY` | OpenAI / Groq / Gemini | Talker fast-path LLM (active provider only) |
| `CARTESIA_API_KEY` | Cartesia | Streaming TTS (Sonic-3 + Tessa voice) |

`setup.toml` `[talker] provider = "<openai|groq|gemini>"` picks which
Talker key is active. v1 default is **Groq** (fastest TTFB on this
hardware: ~150 ms vs OpenAI's ~1 s). See `.env.example` for the
template.

## NFR1 baseline (mocked integration)

Story 2.5's integration test (`tests/integration/test_simple_turn.py`)
drives 30 simulated turns through the post-STT chain
(`SttProcessor` → `TurnDispatchProcessor` →
`CartesiaSynthesisProcessor`) with the three external Protocols
mocked, measuring end-of-speech → first audio frame:

```
NFR1 mocked baseline (30 turns): p50=0ms p95=0ms max=0ms
```

Sub-millisecond integration overhead, exactly as expected with all
I/O mocked — confirms no hidden sleeps / real-I/O leaks in the
pipeline assembly. Real-world latency is dominated by STT (~1.5s
today; Story 5.5 calibration territory) + Talker round-trip (~150 ms
on Groq) + Cartesia TTFB (~700 ms). Story 5.5 owns the calibration
sprint where the real-world p95 gets nailed against NFR1's 1500 ms
target.

## What's deferred

- **Epic 3 (embodiment):** Cartesia inline emotion tags + streaming
  SSML splitter + ROS 2 `ExpressionEvent` publisher. The Talker's
  reply currently has no emotion modulation beyond Cartesia's
  default-emotion config knob.
- **Epic 4 (complex questions + lifecycle):** orchestrator slow-path
  for grounded queries; `LifecycleEvent` publisher; belief-state
  client. v1 routes everything to Talker.
- **Epic 5 (production hardening):** barge-in, SIGHUP atomic config
  swap, systemd unit, 7-day soak, NFR1 calibration.

## ROS 2 / rclpy setup (Story 3.5)

The four-topic event publisher (`mood`, `activity`, `speech_emotion`,
`vocalization`) needs `rclpy` available to the venv at runtime. ROS 2
is **not** installed via `uv` — it lives in your system's package
manager.

### System install (Ubuntu 24.04 / Pi OS)

```bash
sudo apt install ros-jazzy-rclpy ros-jazzy-std-msgs
```

Use whatever ROS 2 distro your platform supports. v1 is tested
against Jazzy.

### Make `rclpy` visible to the venv

ROS 2's `setup.bash` adds the system-installed `rclpy` to
`PYTHONPATH`. Source it before invoking the pipeline:

```bash
source /opt/ros/jazzy/setup.bash
just run
```

Verify the bridge works:

```bash
source /opt/ros/jazzy/setup.bash
uv run python -c "import rclpy; rclpy.init(); print('ok')"
```

### Dev mode without ROS 2

If you want to run the pipeline without a ROS 2 stack installed, edit
`setup.toml`:

```toml
[publisher]
adapter = "log"
```

`LogEventPublisher` records every publish in-memory (useful for unit
tests + local dev without DDS subscribers). The pipeline starts
identically; only the wire-side broadcast is no-op.

### Subscribing to the topics

With ROS 2 sourced and the pipeline running, in a second terminal:

```bash
ros2 topic echo /olaf/speech_emotion
ros2 topic echo /olaf/mood
ros2 topic echo /olaf/activity
ros2 topic echo /olaf/vocalization
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

## Wake-word setup (per project)

The pipeline uses **Picovoice Porcupine** for on-device wake-word detection.
Two pieces are required:

1. A **runtime access key** (per machine, kept in `.env`).
2. A trained `.ppn` **wake-word model file** (committed under `models/wakeword/`).

### 1. Get a Picovoice access key

1. Sign up at https://console.picovoice.ai/ (free tier covers personal use).
2. From the dashboard, copy your **AccessKey**.
3. Add it to `.env` at the project root:

   ```bash
   PICOVOICE_ACCESS_KEY=<paste-the-key>
   ```

   Then `chmod 0600 .env` so the loose-perms WARN doesn't fire (NFR23).

The key authenticates the runtime SDK; it is **not** the same thing as the
`.ppn` file below. The `.env` is gitignored — never commit it.

### 2. Train and download the `.ppn` wake-word file

The committed wake-word phrase is "Hey OLAF". To regenerate or retrain:

1. Open https://console.picovoice.ai/ → **Wake Word** → **Train Wake Word**.
2. **Phrase:** `Hey OLAF` (or your preferred trigger).
3. **Platform:** match your **target deployment host** (NOT necessarily the
   machine you're running the console from):
   - Linux x86_64 desktop → **Linux (x86_64)**
   - macOS (Intel) → **macOS (x86_64)**
   - macOS (Apple Silicon) → **macOS (arm64)**
   - Windows → **Windows (x86_64)**
   - Raspberry Pi 5 (v2 deployment) → **Raspberry Pi**
4. **Language:** English.
5. Click **Train** — wait ~30s for the build.
6. **Download** the resulting `.ppn` file.
7. Save it to `models/wakeword/hey_olaf.ppn` (replacing any existing file).
8. Restart the pipeline (`Ctrl-C` + `just run`) — the new model is picked
   up at startup. Story 5.4's systemd unit will swap to `systemctl restart
   voice-agent-pipeline` once it lands.

> **Free-tier limit.** Picovoice's free tier caps wake-word training at
> **one model per month** (subject to change — check the console for your
> current quota). Plan retraining iterations accordingly: nail the phrase
> and accent samples in one sitting rather than burning the monthly
> allowance on tweaks. For day-to-day false-positive/false-negative tuning,
> prefer the `sensitivity` knob in `setup.toml` (no retrain required).

> **Per-platform `.ppn` files.** A model file trained for Linux x86_64
> will refuse to load on macOS / Windows / Pi. Each contributor running
> the pipeline on a different OS needs to either (a) train their own
> `.ppn` for their platform and swap it in locally, or (b) we eventually
> commit one `.ppn` per supported platform and pick by host. v1 commits
> the Linux x86_64 build only.

If the wake-word model file is missing or the access key is invalid, the
pipeline fails fast at startup with a `startup.failed` log line naming
which check tripped — no silent fall-through.

### Tuning

`setup.toml` exposes one knob: `[wakeword] sensitivity` (range `0.0`–`1.0`,
default `0.5`). Higher values are more sensitive (catch more wakes, more
false positives). Final calibration lives in Story 5.5's soak; until then,
default `0.5` is the conservative starting point.

## Documentation

- Requirements & user journeys: `build_documents/planning-artifacts/prd.md`
- Architecture decisions: `build_documents/planning-artifacts/architecture.md`
- Epics & stories: `build_documents/planning-artifacts/epics.md`
- Per-story specs: `build_documents/implementation-artifacts/`
- AI partner rules: `CLAUDE.md`
