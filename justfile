default: check

# Disable pytest plugin autoload so ROS-sourced PYTHONPATH (which exposes
# launch_testing as a pytest11 entry point and depends on `lark`) doesn't
# poison test collection. Explicitly enable the plugins we actually use.
export PYTEST_DISABLE_PLUGIN_AUTOLOAD := "1"
PYTEST_PLUGINS := "-p pytest_asyncio.plugin"

run:
    uv run python -m voice_agent_pipeline

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
# right regex for `[audio] input_device_name` (and later, output_device_name)
# in setup.toml. See README "Audio device setup" for the workflow.
list-devices:
    uv run python -m voice_agent_pipeline.audio.list_devices
