"""Voice-agent pipeline CLI entry point — ``python -m voice_agent_pipeline``.

Bootstrap order (preserved across all later stories):

1. Load and validate ``setup.toml`` + ``.env`` via :func:`load_setup_config`.
   Failures here happen before logging is configured, so we fall back to
   stderr ``print`` for the user-visible startup-failure line.
2. Configure structlog + stdlib logging via :func:`configure_logging`.
   From this point on, every log call goes through the JSON renderer and
   the redaction processor.
3. Story 1.6 onward: validate external dependencies are reachable BEFORE
   the pipeline opens the audio device. Right now the only validated
   dep is Picovoice; Stories 2.2 / 2.3 / 3.4 / 4.1 / 4.2 add probes here.
4. Spin up an asyncio event loop, install signal handlers, and run
   :func:`run_pipeline` until SIGTERM / SIGINT.

Future stories layer on top of this without changing the order:

- Story 5.2: ``SIGHUP`` handler for atomic config reload.

Exit codes:

- ``0`` — clean shutdown (SIGTERM / SIGINT / pipeline finished cleanly).
- ``1`` — startup or pipeline failure (config invalid, audio device missing,
  Picovoice unreachable, or any other :class:`VoiceAgentError`).
"""

import asyncio
import signal
import sys

import pvporcupine  # pyright: ignore[reportMissingTypeStubs]
import structlog

from voice_agent_pipeline.config.setup import SetupConfig, load_setup_config
from voice_agent_pipeline.errors import StartupValidationError, VoiceAgentError
from voice_agent_pipeline.logging.setup import configure_logging
from voice_agent_pipeline.pipeline import run_pipeline
from voice_agent_pipeline.turn import validate_credentials as validate_talker_credentials


async def _validate_wakeword_credentials(config: SetupConfig) -> None:
    """Probe Picovoice with the configured key + .ppn file.

    Constructs a throwaway Porcupine instance and immediately deletes it.
    The pipeline-resident instance is built later inside
    :class:`WakewordProcessor.start_processor`; this probe just makes sure
    the access key is valid and the .ppn file exists + parses, so that a
    bad credential doesn't reveal itself only after the audio loop opens.

    Raises:
        StartupValidationError: Any Picovoice failure — invalid access
            key, missing or malformed ``.ppn`` file, etc.
    """
    try:
        instance = await asyncio.to_thread(
            pvporcupine.create,
            access_key=config.picovoice_access_key.get_secret_value(),
            keyword_paths=[str(config.wakeword.model_path)],
            sensitivities=[config.wakeword.sensitivity],
        )
        # Release the throwaway instance promptly — the pipeline-resident
        # one is opened later. Off-thread because Porcupine's delete() is
        # synchronous native-code teardown.
        await asyncio.to_thread(instance.delete)
    except Exception as e:
        # Wrap any pvporcupine error (PorcupineInvalidArgumentError,
        # PorcupineActivationError, file-not-found, etc.) into our own
        # hierarchy so callers only catch VoiceAgentError descendants.
        raise StartupValidationError(stage="wakeword", reason=str(e)) from e


async def main() -> int:
    """Run the bootstrap sequence; return a process exit code."""
    # Stage 1: load configuration. We catch the project's own error
    # hierarchy specifically so unexpected exceptions still produce a stack
    # trace (a bug deserves visibility, not a swallowed message).
    try:
        config = load_setup_config()
    except VoiceAgentError as e:
        # Logging is not yet configured at this point — write directly to
        # stderr. systemd and a human at the terminal both surface stderr.
        print(f"startup.failed: {e}", file=sys.stderr)
        return 1

    # Stage 2: wire logging once config is known good. Subsequent stories
    # will consume `config` here for log rotation / retention knobs.
    configure_logging(config)
    log = structlog.get_logger(__name__)
    log.info("startup.completed", schema_version=config.schema_version)

    # Stage 3: external-dependency probes. Picovoice is the only one for
    # now; future stories add Anthropic, Cartesia, ROS 2, orchestrator,
    # belief-state. Each probe must wrap its native error in
    # StartupValidationError so the catch-all below stays clean.
    try:
        await _validate_wakeword_credentials(config)
        log.info("startup.validated.wakeword")
        # Story 2.2: probe the active Talker provider (openai/groq/gemini)
        # before opening audio. Bad key / removed model / wrong base_url
        # surfaces here, not on the first turn.
        await validate_talker_credentials(config)
        log.info("startup.validated.talker", provider=config.talker.provider)
    except VoiceAgentError as e:
        log.critical(
            "startup.failed",
            error=str(e),
            error_class=type(e).__name__,
        )
        return 1

    # Stage 4: install SIGTERM handler. ``shutdown`` is an asyncio.Event we
    # await alongside the pipeline task; whichever finishes first wins.
    # SIGINT (Ctrl-C) is handled separately as KeyboardInterrupt at the
    # outer try/except below — that's the asyncio-friendly pattern.
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown.set)

    pipeline_task = asyncio.create_task(run_pipeline(config))
    shutdown_task = asyncio.create_task(shutdown.wait())
    _, pending = await asyncio.wait(
        [pipeline_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel whichever task didn't complete (typically the pipeline when
    # SIGTERM landed first; or shutdown_task when the pipeline crashed).
    for t in pending:
        t.cancel()

    try:
        # Await the pipeline task to surface any exceptions that landed
        # during shutdown — including legitimate failure modes like a USB
        # device disappearing mid-run.
        await pipeline_task
    except asyncio.CancelledError:
        # Expected on the SIGTERM path — not an error.
        pass
    except VoiceAgentError as e:
        log.critical(
            "startup.failed",
            error=str(e),
            error_class=type(e).__name__,
        )
        return 1

    return 0


if __name__ == "__main__":
    # Module-as-script entry. KeyboardInterrupt (Ctrl-C) bypasses
    # asyncio.run's cleanup so we catch it here for a clean exit.
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
