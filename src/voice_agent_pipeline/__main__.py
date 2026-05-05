"""Voice-agent pipeline CLI entry point — ``python -m voice_agent_pipeline``.

Bootstrap order (preserved across all later stories):

1. Load and validate ``setup.toml`` + ``.env`` via :func:`load_setup_config`.
   Failures here happen before logging is configured, so we fall back to
   stderr ``print`` for the user-visible startup-failure line.
2. Configure structlog + stdlib logging via :func:`configure_logging`.
   From this point on, every log call goes through the JSON renderer and
   the redaction processor.
3. Emit ``startup.completed`` so the operator sees the pipeline came up
   cleanly. Future stories add the actual voice loop after this point.

Future stories layer on top of this without changing the order:

- Story 1.5: ``asyncio.run(...)`` + signal handlers + audio I/O.
- Stories 2.2 / 2.3 / 3.4 / 4.1 / 4.2: full external-dependency probes.
- Story 5.2: ``SIGHUP`` handler for atomic config reload.

Exit codes:

- ``0`` — startup succeeded, pipeline ran to clean shutdown.
- ``1`` — startup failed (config invalid, missing creds, schema mismatch).
- (later) signal-based exits land in Stories 1.5 / 5.4.
"""

import sys

import structlog

from voice_agent_pipeline.config.setup import load_setup_config
from voice_agent_pipeline.errors import VoiceAgentError
from voice_agent_pipeline.logging.setup import configure_logging


def main() -> int:
    """Run the bootstrap sequence; return a process exit code."""
    # Stage 1: load configuration. We catch the project's own error hierarchy
    # specifically so that arbitrary uncaught exceptions still produce a stack
    # trace (a developer bug deserves visibility, not a swallowed message).
    try:
        config = load_setup_config()
    except VoiceAgentError as e:
        # Logging is not yet configured at this point, so we have no choice but
        # to write directly to stderr. systemd and a human at the terminal both
        # surface stderr clearly.
        print(f"startup.failed: {e}", file=sys.stderr)
        return 1

    # Stage 2: wire logging once config is known good. Subsequent stories will
    # consume `config` here for log rotation / retention knobs (Story 5.3).
    configure_logging(config)

    # Stage 3: emit one structured "we made it" line. The `event` field is
    # required by the redaction pipeline (every log call must carry one).
    log = structlog.get_logger(__name__)
    log.info("startup.completed", schema_version=config.schema_version)
    return 0


if __name__ == "__main__":
    # Standard "module-as-script" entry: forward main()'s return code to the OS.
    sys.exit(main())
