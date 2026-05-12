default: check

# Disable pytest plugin autoload so ROS-sourced PYTHONPATH (which exposes
# launch_testing as a pytest11 entry point and depends on `lark`) doesn't
# poison test collection. Explicitly enable the plugins we actually use.
export PYTEST_DISABLE_PLUGIN_AUTOLOAD := "1"
PYTEST_PLUGINS := "-p pytest_asyncio.plugin"

# Mirror logs to stdout via structlog's ConsoleRenderer (human-readable,
# not JSON). Files still get strict JSON for grep tooling. Set
# LOG_CONSOLE=false explicitly if you want production-silent stdout
# (e.g. when running under systemd in Story 5.4).
run:
    LOG_CONSOLE=true uv run python -m voice_agent_pipeline

check:
    uv run ruff check
    uv run ruff format --check
    uv run pyright
    uv run pytest {{PYTEST_PLUGINS}} tests/unit -q

test:
    uv run pytest {{PYTEST_PLUGINS}}

lint:
    uv run ruff check

format:
    uv run ruff format

# Print every PyAudio device on this machine. Use this output to find the
# right regex for `[audio] input_device_name` and `output_device_name` in
# setup.toml. See README "Audio device setup" for the workflow.
list-devices:
    uv run python -m voice_agent_pipeline.audio.list_devices

# Play a 1-second 440Hz beep through the speaker resolved from setup.toml's
# `[audio] output_device_name` regex. Use after `list-devices` to confirm
# your speaker regex matches a working output device — sanity-checks the
# Story 2.1 playback path independent of Cartesia.
play-test-tone:
    uv run python -m voice_agent_pipeline.audio.play_test_tone

# Story 5.5: pre-render cached WAVs for deterministic-text surfaces
# (greetings, goodbyes, clarifications, thinking fillers) via Cartesia.
# Writes WAVs under `assets/audio/` and refreshes `manifest.json`.
# Idempotent — phrases whose phrase_hash is already in the manifest +
# whose file exists are skipped. Run after editing any phrase list in
# `setup.toml` or after changing `[tts] voice_id` / `[tts] model` —
# the Stage 3 startup probe will refuse to start otherwise.
#
# Flags: `--force` regenerates every entry; `--dry-run` prints the
# plan without API calls. Pass them after `--`, e.g.
# `just regenerate-audio --force`.
regenerate-audio *FLAGS:
    uv run python -m voice_agent_pipeline.audio.regenerate {{FLAGS}}
