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

# Story 6.1: Cartesia TTFB measurement spike on the WebSocket transport
# (100+ requests, mixed transcript lengths). Writes a Markdown report to
# `build_documents/implementation-artifacts/6-1-ttfb-spike-report.md`
# with p25/p50/p75/p90 and the DR-001 Option D viability call-out.
# Idempotent — re-running overwrites the previous report at the same
# path (older reports are in git history if needed). Burns ~100
# Cartesia synthesis tokens per run; expect ~3-5 minutes wall-clock.
ttfb-spike:
    uv run python -m voice_agent_pipeline.tts.ttfb_spike

# Story 6.4: v2 soak report. Aggregates `turn.complete` rollup events from
# the JSON-line structured log into latency percentiles + opener/routing
# distributions + an NFR33/NFR34 target-comparison table. Writes Markdown to
# `build_documents/implementation-artifacts/6-4-soak-report.md`. Read-only on
# the log — run it after (or during) a live session. Flags pass through, e.g.
# `just soak-v2-report --minutes 30` or `--log ./logs/voice-agent.log`.
soak-v2-report *FLAGS:
    uv run python -m voice_agent_pipeline.tools.soak_v2 {{FLAGS}}

# Self-contained ROS 2 / DDS publish smoke test. Stands up the production
# Ros2EventPublisher on the configured `[publisher].dds_domain_id` and an
# in-process witness subscriber using the body's exact contract QoS, then
# publishes one event per topic and confirms all four are received. Needs
# a sourced ROS 2 env (rclpy) but NO `.env`/API keys and NO OLAF body —
# it verifies our emission side conforms to `contract/INTERFACE.md`.
# Exits non-zero if any topic is missed (domain mismatch, QoS drift, etc.).
#
# Flags pass through after `--`, e.g. `just smoke-publisher --domain 0`
# or `just smoke-publisher --timeout 20`.
smoke-publisher *FLAGS:
    uv run python -m voice_agent_pipeline.publisher.smoke {{FLAGS}}

# Stream a scripted sequence of MOCK voice-agent events to the four
# `/olaf/*` topics, one per second, so a running body (the expression_engine
# subscriber) can be watched reacting to each. Pure producer — no witness;
# you monitor the receiver. Walks a full session (boot→sleep→wake→listen→
# think→speak→delegate→goodbye→sleep) covering every activity state, mood,
# emotion, and vocalization. Uses the configured `[publisher].dds_domain_id`.
#
# Flags pass through after `--`, e.g. `just simulate-agent --loop`,
# `just simulate-agent --delay 2`, or `just simulate-agent --domain 0`.
simulate-agent *FLAGS:
    uv run python -m voice_agent_pipeline.publisher.simulate {{FLAGS}}
