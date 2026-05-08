"""File-descriptor-level stderr suppression for PyAudio init noise.

PyAudio's PortAudio backend probes every ALSA / JACK / PulseAudio
device on the host during initialization. For the devices that don't
exist on this machine (most of them, on a typical desktop), PortAudio's
C code writes diagnostic lines like::

    ALSA lib pcm_dsnoop.c:567:(snd_pcm_dsnoop_open) unable to open slave
    Cannot connect to server socket err = No such file or directory
    jack server is not running or cannot be started

directly to file descriptor 2 (libc ``stderr``). They aren't real
errors — every PyAudio user sees them; the working device gets opened
fine right after. They also bypass Python's logging entirely, so
``sys.stderr`` redirection and ``logging.captureWarnings`` are no-ops.
The only way to silence them is at the OS level: ``dup2`` fd 2 to
``/dev/null`` for the duration of the noisy call, then restore.

Scope
-----

This is intentionally a tiny module. It exists for one reason — to be
wrapped around the ``resolve_audio_devices`` + ``build_audio_transport``
calls in ``pipeline.run_pipeline`` so the operator-facing startup
checklist (:class:`~voice_agent_pipeline.logging.startup.StartupReporter`)
stays clean. It must not be applied broadly: any real crash that
writes a useful Python traceback to stderr would also be swallowed if
the suppressor's scope were too wide.

The pattern (saved fd → ``dup2(devnull, 2)`` → ``yield`` → restore)
is the canonical PyAudio quieting recipe; the project just packages it
as a module so the call sites are self-documenting.
"""

import os
from collections.abc import Generator
from contextlib import contextmanager


@contextmanager
def suppress_native_stderr() -> Generator[None]:
    """Redirect file descriptor 2 to ``/dev/null`` for the block duration.

    Catches output from C code (PyAudio / PortAudio / ALSA / JACK) that
    bypasses Python's :data:`sys.stderr`. Restores fd 2 on exit, including
    on the exception path — so the :class:`StartupReporter`'s ``[ ✗ ]``
    line still reaches the operator's terminal if a wrapped call raises.

    Usage::

        with suppress_native_stderr():
            indices = resolve_audio_devices(...)
            transport = build_audio_transport(config, indices)

    Implementation notes:

    - ``os.dup(2)`` saves a copy of the current fd 2 *before* we
      overwrite it. The saved descriptor is closed in the ``finally``
      block to avoid leaking file descriptors across repeated startups
      (relevant for tests; the production process only calls this once).
    - The block is intentionally narrow — sub-second wall time. We do
      NOT want to suppress real Python tracebacks (which write to
      ``sys.stderr`` via Python's I/O layer, ultimately landing on
      fd 2). Any wrapped call that raises will have its exception
      propagated *after* fd 2 is restored, so reporter ``[ ✗ ]`` lines
      and uncaught traceback prints both render normally.
    - Not async-safe in the sense that two concurrent ``async with``
      blocks would race on fd 2; startup is single-task by design so
      this isn't a real concern. Documented here to forestall the
      copy-paste-into-a-handler temptation later.
    """
    # Open /dev/null for write — destination for the suppressed bytes.
    devnull = os.open(os.devnull, os.O_WRONLY)
    # Duplicate fd 2 so we can restore it on exit. This is *not*
    # ``sys.stderr.fileno()`` indirected; we operate on the literal
    # fd 2 that libc's ``stderr`` is wired to.
    saved_stderr_fd = os.dup(2)
    try:
        # Point fd 2 at /dev/null. Any C-level write to stderr goes
        # to the bit bucket from this point until the finally block.
        os.dup2(devnull, 2)
        yield
    finally:
        # Restore the original fd 2. ``dup2`` atomically replaces
        # the target descriptor, so there's no window where fd 2
        # would be invalid.
        os.dup2(saved_stderr_fd, 2)
        # Close both helper descriptors. Not closing them would leak
        # one fd per call — invisible in production (one call per
        # process) but a leak in any future per-test invocation.
        os.close(saved_stderr_fd)
        os.close(devnull)
