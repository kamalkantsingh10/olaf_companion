# voice-agent-pipeline

Pipecat-based voice-agent service for the OLAF Companion project. Owns the voice loop and embodiment broadcast surface — captures speech, dispatches turns, generates spoken responses with Cartesia, and publishes typed expression + lifecycle events on configurable broadcast channels.

## Install

```bash
uv sync
```

`rclpy` is installed separately via your system ROS 2 distro (e.g., `apt install ros-jazzy-rclpy`) and exposed to the venv via `PYTHONPATH`. Required from Story 3.4 onward.

## Run

```bash
just run    # starts the pipeline (current state: bootstrap stub)
just check  # lint + type-check + unit tests (must pass before commit)
just test   # full test suite
```

## Documentation

- Requirements & user journeys: `build_documents/planning-artifacts/prd.md`
- Architecture decisions: `build_documents/planning-artifacts/architecture.md`
- Epics & stories: `build_documents/planning-artifacts/epics.md`
- Per-story specs: `build_documents/implementation-artifacts/`
- AI partner rules: `CLAUDE.md`
