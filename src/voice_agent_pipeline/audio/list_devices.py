"""Operator-facing CLI: print every PyAudio device with index/name/channels.

Run via ``just list-devices`` or ``uv run python -m voice_agent_pipeline.audio.list_devices``.
Use this on a fresh machine to discover the right regex for the
``[audio] input_device_name`` field in ``setup.toml``.

Output format (tab-separated, easy to ``grep``)::

    idx  in_ch  out_ch  default_sr  name
    0    1      0       48000.0    USB Audio Mic Array
    1    0      2       48000.0    USB Audio Speaker

Idiom: copy the *name* column for the device you want, escape regex
metachars if any, and put it in ``setup.toml``::

    [audio]
    input_device_name = "USB.*Mic.*Array"
"""

import sys
from typing import Any

from voice_agent_pipeline.audio.devices import enumerate_devices

# Tab-separated output keeps the script grep / awk friendly while still
# being readable by a human at the terminal. Header row matches the
# tab-spaced layout below for clean column alignment.
_HEADER = "idx\tin_ch\tout_ch\tdefault_sr\tname"


def _format_row(index: int, info: dict[str, Any]) -> str:
    """Render one device's enumeration tuple as a tab-separated line."""
    in_ch = info.get("maxInputChannels", "?")
    out_ch = info.get("maxOutputChannels", "?")
    default_sr = info.get("defaultSampleRate", "?")
    name = info.get("name", "?")
    return f"{index}\t{in_ch}\t{out_ch}\t{default_sr}\t{name}"


def main() -> int:
    """Print every PyAudio device on stdout.

    Returns:
        ``0`` on success. ``1`` if PyAudio enumeration fails (very rare —
        usually only on machines without any audio subsystem at all).
    """
    try:
        devices = enumerate_devices()
    except Exception as e:
        print(f"audio.list_devices failed: {e}", file=sys.stderr)
        return 1

    print(_HEADER)
    for index, info in devices:
        print(_format_row(index, info))

    if not devices:
        print(
            "(no devices enumerated — check that audio drivers are installed)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
