"""Unit tests for :mod:`voice_agent_pipeline.audio.devices`.

PyAudio is mocked at the module boundary via :func:`monkeypatch.setattr` —
we replace ``pyaudio.PyAudio`` with a fake class whose enumeration returns
a curated list of device records. This keeps the tests hermetic (no real
audio hardware required, runs in CI / AI loops) while exercising the same
code path production uses.
"""

from typing import Any

import pytest

from voice_agent_pipeline.audio import devices as devices_mod
from voice_agent_pipeline.audio.devices import (
    AudioDeviceIndices,
    enumerate_devices,
    resolve_audio_devices,
)
from voice_agent_pipeline.errors import StartupValidationError


class _FakePyAudio:
    """Drop-in replacement for ``pyaudio.PyAudio`` returning a fixed device list.

    The real PyAudio API exposes ``get_device_count()`` and
    ``get_device_info_by_index(i)``; these are the only methods the
    resolver calls, so the fake mocks just those plus the no-op
    ``terminate()``.
    """

    def __init__(self, devices: list[dict[str, Any]]) -> None:
        self._devices = devices

    def get_device_count(self) -> int:
        return len(self._devices)

    def get_device_info_by_index(self, index: int) -> dict[str, Any]:
        return self._devices[index]

    def terminate(self) -> None:
        # No-op in the fake. Real PyAudio frees ALSA / PortAudio resources here.
        pass


# Curated set of devices covering the four audio device shapes we care about:
# input-only USB mic, output-only USB speaker, combo built-in, and a
# second input mic (so name-disambiguation tests have something to bite on).
_FAKE_DEVICES: list[dict[str, Any]] = [
    {"name": "USB Audio Mic Array", "maxInputChannels": 1, "maxOutputChannels": 0},
    {"name": "USB Audio Speaker", "maxInputChannels": 0, "maxOutputChannels": 2},
    {"name": "HDA Intel PCH (hw:0,0)", "maxInputChannels": 2, "maxOutputChannels": 2},
    {"name": "Other USB Microphone", "maxInputChannels": 1, "maxOutputChannels": 0},
]


@pytest.fixture
def fake_pyaudio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace pyaudio.PyAudio with the deterministic fake for one test."""
    monkeypatch.setattr(devices_mod, "pyaudio", _FakeModule(_FAKE_DEVICES))


class _FakeModule:
    """Fake the ``pyaudio`` module so ``devices_mod.pyaudio.PyAudio()`` works."""

    def __init__(self, device_list: list[dict[str, Any]]) -> None:
        self._device_list = device_list

    def PyAudio(self) -> _FakePyAudio:  # noqa: N802 -- mimic real PyAudio() name
        return _FakePyAudio(self._device_list)


def test_input_regex_matches_returns_index(fake_pyaudio: None) -> None:
    """Regex matches first input device by name and returns its index."""
    indices = resolve_audio_devices(input_pattern="USB.*Mic.*Array", output_pattern=None)
    # The "USB Audio Mic Array" device is at index 0 in _FAKE_DEVICES.
    assert indices == AudioDeviceIndices(input_index=0, output_index=None)


def test_input_no_match_raises_with_available_list(fake_pyaudio: None) -> None:
    """Non-matching regex raises StartupValidationError with the available names."""
    with pytest.raises(StartupValidationError) as exc_info:
        resolve_audio_devices(input_pattern="NonExistent.*", output_pattern=None)
    available = exc_info.value.context.get("available")
    assert isinstance(available, list)
    # All three input-capable devices should be listed; output-only excluded.
    assert "USB Audio Mic Array" in available
    assert "HDA Intel PCH (hw:0,0)" in available
    assert "Other USB Microphone" in available
    assert "USB Audio Speaker" not in available
    # Stage tag identifies which side failed.
    assert exc_info.value.context.get("stage") == "audio.input"


def test_match_is_case_insensitive(fake_pyaudio: None) -> None:
    """Lowercase pattern matches mixed-case device name."""
    indices = resolve_audio_devices(input_pattern="usb.*mic", output_pattern=None)
    # First match wins — index 0 is "USB Audio Mic Array".
    assert indices.input_index == 0


def test_input_only_devices_not_chosen_for_output(fake_pyaudio: None) -> None:
    """Output resolution skips input-only devices.

    Even if an input-only device's name matches, it shouldn't be selected
    for the output side — the channel-count predicate filters it out.
    """
    with pytest.raises(StartupValidationError) as exc_info:
        # Mic Array has maxOutputChannels=0; should NOT match for output.
        resolve_audio_devices(input_pattern=None, output_pattern="USB.*Mic.*Array")
    assert exc_info.value.context.get("stage") == "audio.output"


def test_no_input_pattern_returns_none_index(fake_pyaudio: None) -> None:
    """Passing None for both patterns returns AudioDeviceIndices(None, None)."""
    indices = resolve_audio_devices(input_pattern=None, output_pattern=None)
    assert indices == AudioDeviceIndices(input_index=None, output_index=None)


def test_output_regex_matches(fake_pyaudio: None) -> None:
    """Output-side resolution finds the speaker by name."""
    indices = resolve_audio_devices(input_pattern=None, output_pattern="USB.*Speaker")
    # Speaker is at index 1.
    assert indices.output_index == 1


def test_first_match_wins(fake_pyaudio: None) -> None:
    """When multiple input devices match, the first by enumeration order wins.

    This documents the contract: device order matters. Operators with two
    matching mics need to write a more specific regex for disambiguation.
    """
    # Both "USB Audio Mic Array" (idx 0) and "Other USB Microphone" (idx 3)
    # match this pattern. We expect index 0 (the first match).
    indices = resolve_audio_devices(input_pattern="USB.*Mic", output_pattern=None)
    assert indices.input_index == 0


def test_enumerate_devices_returns_full_list(fake_pyaudio: None) -> None:
    """The lower-level enumerator returns every device, regardless of channel count."""
    devices = enumerate_devices()
    assert len(devices) == len(_FAKE_DEVICES)
    names = [info["name"] for _, info in devices]
    assert names == [d["name"] for d in _FAKE_DEVICES]


def test_no_audio_bytes_in_logs(
    fake_pyaudio: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanity: nothing in this module's logs ever contains an `audio_bytes` field.

    Story 1.3's redaction processor catches accidental leaks at log time;
    this test catches them at write time too — the resolver should never
    have a reason to log raw audio.
    """
    import logging

    with caplog.at_level(logging.DEBUG, logger="voice_agent_pipeline.audio.devices"):
        resolve_audio_devices(input_pattern="USB.*Mic.*", output_pattern=None)
    for record in caplog.records:
        assert "audio_bytes" not in record.getMessage()
        assert "audio_bytes" not in str(getattr(record, "args", ""))
