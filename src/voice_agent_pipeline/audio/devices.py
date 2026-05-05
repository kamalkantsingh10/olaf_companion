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
