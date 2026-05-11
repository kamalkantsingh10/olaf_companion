"""Audio device resolution by stable name regex.

PyAudio assigns numeric indices to devices, but those indices shift across
reboots and USB hot-plug events — so pinning a device by index is fragile.
The architecture's standard mitigation is to pin by name regex: enumerate
devices at startup, find the first one whose name matches, and use the
index it happens to have *for this run*. Pinning by name is reliable across
the failure modes that matter (NFR11).

This module is one of two places PyAudio is imported (the other is
:mod:`audio.transport`). All other modules speak through the typed
:class:`AudioDeviceIndices` dataclass.

Per-machine config: see :mod:`audio.list_devices` for the operator-facing
discovery helper. The README documents the workflow.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pyaudio
import structlog

from voice_agent_pipeline.errors import StartupValidationError

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AudioDeviceIndices:
    """The resolved input + output device indices for this run.

    ``None`` means "no pattern was provided for this side" — distinct from
    "pattern provided but no device matched" (which raises
    :class:`StartupValidationError` instead).
    """

    input_index: int | None
    output_index: int | None


def resolve_audio_devices(
    input_pattern: str | None,
    output_pattern: str | None,
) -> AudioDeviceIndices:
    """Match the patterns against PyAudio's enumeration and return numeric indices.

    Both patterns are matched case-insensitively via :func:`re.search` — so
    ``"usb"`` matches ``"USB Audio Mic Array"``. Input candidates must have
    ``maxInputChannels > 0``; output candidates must have
    ``maxOutputChannels > 0``.

    Args:
        input_pattern: Regex string for the mic device name. ``None`` skips
            input resolution and returns ``input_index=None``.
        output_pattern: Regex string for the speaker. Same ``None`` semantic.

    Returns:
        :class:`AudioDeviceIndices` carrying both indices.

    Raises:
        StartupValidationError: If a non-``None`` pattern fails to match any
            candidate device. The error context lists every candidate device
            name so the operator can fix their regex without a stack trace.
    """
    devices = enumerate_devices()
    input_index = _find(devices, input_pattern, "input", _is_input)
    output_index = _find(devices, output_pattern, "output", _is_output)
    log.info(
        "audio.devices.resolved",
        input_pattern=input_pattern,
        input_index=input_index,
        output_pattern=output_pattern,
        output_index=output_index,
    )
    return AudioDeviceIndices(input_index=input_index, output_index=output_index)


def probe_devices_openable(indices: AudioDeviceIndices) -> None:
    """Open + immediately close each resolved device to confirm it's actually usable.

    Why this is a separate probe from :func:`resolve_audio_devices`:
    name-regex resolution only proves "PyAudio's enumeration table contains
    a device whose name matches" — it does NOT prove the device is currently
    openable. A USB mic that's enumerated but in use by another process, an
    output device whose format is mis-claimed, or a regex matching a
    phantom device that disappears between enumeration and open all fall in
    that gap. Catching those at startup (before the audio loop starts and
    before the operator hears nothing) is the whole point of NFR26's
    "validate everything at startup" posture.

    What the probe does:

    - **Mic side:** opens an input stream at 16 kHz mono S16LE (the
      pipeline-wide audio format), reads ~300 ms of samples (3 chunks of
      1024 frames at 16 kHz), closes. Catches busy / wrong-format /
      unplugged-after-enum failures. Reading frames is what catches the
      subtle "device opens but produces no samples" case (some USB mics
      do this when the OS-side driver is mid-restart).
    - **Speaker side:** opens an output stream at the same format and
      closes immediately, **without writing**. Writing would produce
      audible noise at startup; the open-then-close still confirms the
      driver accepts the format. A speaker that's actually broken (no
      output device wired) will fail to open with a clear PyAudio error.

    Idempotency: this is a *separate* PyAudio instance from the
    production one — each call constructs a fresh ``PyAudio()`` and
    terminates it. The audio loop later opens its own streams against
    the same indices.

    Args:
        indices: Resolved :class:`AudioDeviceIndices` from
            :func:`resolve_audio_devices`. ``None`` slots are skipped —
            an operator who didn't configure one side (e.g., output-only
            tests) shouldn't be forced to provide it here.

    Raises:
        StartupValidationError: With ``stage="audio.mic"`` or
            ``stage="audio.speaker"`` on any PyAudio failure during open
            or read. ``reason`` carries the underlying error text so the
            operator sees what went wrong (driver-busy, format-rejected,
            etc.) without a stack trace.
    """
    # Constants — these MUST match the pipeline-wide audio format used by
    # :mod:`voice_agent_pipeline.audio.transport` (16 kHz mono S16LE).
    # The probe-format mismatch failure mode would be insidious: probe
    # passes, real open fails. Kept hard-coded here rather than threading
    # through config because the format isn't actually configurable.
    sample_rate = 16_000
    channels = 1
    frame_format = pyaudio.paInt16
    frames_per_chunk = 1024  # ~64 ms at 16 kHz — three chunks ≈ 192 ms read
    read_chunks = 3

    pa = pyaudio.PyAudio()
    try:
        # Input probe — only if the operator configured an input device.
        if indices.input_index is not None:
            _probe_input(
                pa=pa,
                device_index=indices.input_index,
                sample_rate=sample_rate,
                channels=channels,
                frame_format=frame_format,
                frames_per_chunk=frames_per_chunk,
                read_chunks=read_chunks,
            )
        # Output probe — same gating logic.
        if indices.output_index is not None:
            _probe_output(
                pa=pa,
                device_index=indices.output_index,
                sample_rate=sample_rate,
                channels=channels,
                frame_format=frame_format,
                frames_per_chunk=frames_per_chunk,
            )
    finally:
        # Always terminate the throwaway PyAudio instance, even if a
        # probe raised mid-call — PortAudio holds OS resources that
        # don't auto-release on GC alone.
        pa.terminate()

    log.info(
        "audio.devices.probed",
        input_index=indices.input_index,
        output_index=indices.output_index,
    )


def _probe_input(
    pa: pyaudio.PyAudio,
    device_index: int,
    sample_rate: int,
    channels: int,
    frame_format: int,
    frames_per_chunk: int,
    read_chunks: int,
) -> None:
    """Open input stream, read N chunks, close. Wrap failure in StartupValidationError.

    Reads frames (not just opens) because the "stream opens but yields no
    samples" failure mode is real on some USB mics — driver accepts the
    format but the device returns silence indefinitely. A quick read
    catches that without burdening startup latency (~200 ms cost).
    """
    stream = None
    try:
        stream = pa.open(
            format=frame_format,
            channels=channels,
            rate=sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=frames_per_chunk,
        )
        # Read a few chunks to confirm samples actually flow. ``read``
        # blocks until the buffer fills — ~64 ms per chunk at 16 kHz.
        # exception_on_overflow=False so transient overruns during the
        # probe don't fail startup; the production pipeline manages
        # overruns separately.
        for _ in range(read_chunks):
            stream.read(frames_per_chunk, exception_on_overflow=False)
    except OSError as e:
        # PyAudio raises OSError (PortAudio errors surface that way) for
        # device-busy, unsupported-format, device-disappeared, etc.
        raise StartupValidationError(
            stage="audio.mic",
            device_index=device_index,
            reason=str(e),
        ) from e
    finally:
        # Stream cleanup — stop_stream before close per PortAudio
        # convention; both are idempotent so guarding with ``if stream``
        # is enough.
        if stream is not None:
            try:
                stream.stop_stream()
            except OSError:
                # Best-effort cleanup; the underlying error is already
                # being raised (or we're on the happy path and don't
                # care about a teardown hiccup).
                pass
            stream.close()


def _probe_output(
    pa: pyaudio.PyAudio,
    device_index: int,
    sample_rate: int,
    channels: int,
    frame_format: int,
    frames_per_chunk: int,
) -> None:
    """Open output stream, close immediately — no write (avoids audible noise at startup).

    The format-acceptance check is in the ``pa.open(...)`` call itself —
    PortAudio raises if the output device doesn't support the requested
    format/rate. We deliberately do NOT write samples: writing zeros
    would produce no audible noise, but writing any non-zero buffer
    would. The "open succeeds, write fails" failure mode is vanishingly
    rare on output streams; reading samples (as we do for the mic) is
    the more revealing check there.
    """
    stream = None
    try:
        stream = pa.open(
            format=frame_format,
            channels=channels,
            rate=sample_rate,
            output=True,
            output_device_index=device_index,
            frames_per_buffer=frames_per_chunk,
        )
    except OSError as e:
        raise StartupValidationError(
            stage="audio.speaker",
            device_index=device_index,
            reason=str(e),
        ) from e
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
            except OSError:
                pass
            stream.close()


def enumerate_devices() -> list[tuple[int, dict[str, Any]]]:
    """Snapshot PyAudio's device enumeration as ``(index, info_dict)`` tuples.

    Factored out so :mod:`audio.list_devices` (the operator-facing helper)
    and the production resolver share the same enumeration code path. PyAudio
    requires holding the instance live during enumeration; we take the
    snapshot then immediately ``terminate()`` so callers don't have to
    juggle PyAudio lifecycle.

    PyAudio's ``get_device_info_by_index`` returns a private ``_PaDeviceInfo``
    TypedDict; we coerce to a plain ``dict[str, Any]`` so downstream callers
    don't depend on PyAudio's private types.
    """
    pa = pyaudio.PyAudio()
    try:
        return [(i, dict(pa.get_device_info_by_index(i))) for i in range(pa.get_device_count())]
    finally:
        pa.terminate()


def _is_input(device: dict[str, Any]) -> bool:
    """Predicate: device has at least one input channel."""
    channels = device.get("maxInputChannels")
    return isinstance(channels, int) and channels > 0


def _is_output(device: dict[str, Any]) -> bool:
    """Predicate: device has at least one output channel."""
    channels = device.get("maxOutputChannels")
    return isinstance(channels, int) and channels > 0


def _find(
    devices: list[tuple[int, dict[str, Any]]],
    pattern: str | None,
    side: str,
    candidate_filter: Callable[[dict[str, Any]], bool],
) -> int | None:
    """Resolve a single side (input or output) against ``pattern``.

    Args:
        devices: Snapshot from :func:`enumerate_devices`.
        pattern: Regex string, or ``None`` to skip.
        side: ``"input"`` or ``"output"`` — only used in the error stage tag.
        candidate_filter: Predicate filtering devices to the relevant side.

    Returns:
        Matched device index, or ``None`` if ``pattern`` was ``None``.

    Raises:
        StartupValidationError: If ``pattern`` is non-``None`` but no
            candidate device matched. ``available`` carries the device name
            list so the operator can correct the regex (or run
            ``just list-devices`` to see the full picture).
    """
    if pattern is None:
        return None

    rx = re.compile(pattern, re.IGNORECASE)
    # Build the available-list eagerly so the error message is complete even
    # if iteration short-circuits on a match.
    available = [str(d.get("name", "")) for _, d in devices if candidate_filter(d)]

    for idx, d in devices:
        if not candidate_filter(d):
            continue
        if rx.search(str(d.get("name", ""))):
            return idx

    raise StartupValidationError(stage=f"audio.{side}", pattern=pattern, available=available)
