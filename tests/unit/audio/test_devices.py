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
    probe_devices_openable,
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


def test_output_no_match_raises_with_available_outputs_only(fake_pyaudio: None) -> None:
    """Story 2.1: output-side miss raises StartupValidationError with the available list.

    Symmetric to :func:`test_input_no_match_raises_with_available_list` —
    when the operator's output regex doesn't match any output-capable
    device, the error context lists every device with
    ``maxOutputChannels > 0`` (so input-only mics are excluded), echoes
    the regex back, and tags ``stage="audio.output"`` so log readers
    can distinguish input vs output failures at a glance.
    """
    with pytest.raises(StartupValidationError) as exc_info:
        resolve_audio_devices(input_pattern=None, output_pattern="NonExistent.*")
    available = exc_info.value.context.get("available")
    assert isinstance(available, list)
    # Output-capable devices: the speaker (idx 1) + the duplex HDA Intel (idx 2).
    assert "USB Audio Speaker" in available
    assert "HDA Intel PCH (hw:0,0)" in available
    # Input-only mics should NOT appear — the candidate filter is output-side.
    assert "USB Audio Mic Array" not in available
    assert "Other USB Microphone" not in available
    # Stage tag identifies which side failed; pattern echoed back for debugging.
    assert exc_info.value.context.get("stage") == "audio.output"
    assert exc_info.value.context.get("pattern") == "NonExistent.*"


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


# ---------------------------------------------------------------------------
# probe_devices_openable — startup-time mic + speaker openability check
# ---------------------------------------------------------------------------
#
# The probe constructs its own PyAudio() instance and calls open(...) on
# the configured device indices. The fakes below mirror just enough of
# the PyAudio API surface — open(), stream.read(), stream.stop_stream(),
# stream.close(), the paInt16 constant — to exercise the probe end-to-end
# without real audio hardware.


class _FakeStream:
    """Drop-in for ``pyaudio.Stream``. Records read calls, optionally raises."""

    def __init__(
        self,
        open_kwargs: dict[str, Any],
        read_error: Exception | None = None,
    ) -> None:
        self.open_kwargs = open_kwargs
        self.read_calls: int = 0
        self.stopped = False
        self.closed = False
        self._read_error = read_error

    def read(self, frames: int, exception_on_overflow: bool = True) -> bytes:
        del exception_on_overflow
        self.read_calls += 1
        if self._read_error is not None:
            raise self._read_error
        # Return silence — 16-bit mono samples == 2 bytes per frame.
        return b"\x00\x00" * frames

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _FakeOpenablePyAudio:
    """Extended fake supporting open(); records every open call.

    Configurable failure modes: ``open_error`` raises on open;
    ``read_error`` raises on read.
    """

    def __init__(
        self,
        open_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.open_calls: list[dict[str, Any]] = []
        self.streams: list[_FakeStream] = []
        self.terminate_count = 0
        self._open_error = open_error
        self._read_error = read_error

    # Match the resolver-side surface so the same fake can serve both tests.
    def get_device_count(self) -> int:
        return 0

    def get_device_info_by_index(self, index: int) -> dict[str, Any]:
        raise IndexError(index)

    def open(self, **kwargs: Any) -> _FakeStream:
        self.open_calls.append(kwargs)
        if self._open_error is not None:
            raise self._open_error
        stream = _FakeStream(kwargs, read_error=self._read_error)
        self.streams.append(stream)
        return stream

    def terminate(self) -> None:
        self.terminate_count += 1


class _FakeOpenableModule:
    """Fake pyaudio module exposing PyAudio + the paInt16 format constant."""

    paInt16 = 8  # noqa: N815 -- mimic real pyaudio.paInt16 attribute (mixed-case is the SDK's choice)

    def __init__(self, pa: _FakeOpenablePyAudio) -> None:
        self._pa = pa

    def PyAudio(self) -> _FakeOpenablePyAudio:  # noqa: N802 -- mimic real PyAudio() name
        return self._pa


@pytest.fixture
def fake_openable_pyaudio(monkeypatch: pytest.MonkeyPatch) -> _FakeOpenablePyAudio:
    """Install a fake pyaudio whose open() succeeds; return the singleton for inspection."""
    pa = _FakeOpenablePyAudio()
    monkeypatch.setattr(devices_mod, "pyaudio", _FakeOpenableModule(pa))
    return pa


def test_probe_opens_mic_and_speaker_at_configured_format(
    fake_openable_pyaudio: _FakeOpenablePyAudio,
) -> None:
    """Both sides open at 16 kHz mono S16LE; mic reads 3 chunks; both close cleanly."""
    probe_devices_openable(AudioDeviceIndices(input_index=2, output_index=5))

    # Two open calls — mic first, speaker second.
    assert len(fake_openable_pyaudio.open_calls) == 2
    mic_open, spk_open = fake_openable_pyaudio.open_calls

    # Mic side carries input=True + the configured device index.
    assert mic_open["input"] is True
    assert mic_open["input_device_index"] == 2
    assert mic_open["rate"] == 16_000
    assert mic_open["channels"] == 1
    assert mic_open["format"] == 8  # paInt16

    # Speaker side carries output=True + the configured device index.
    assert spk_open["output"] is True
    assert spk_open["output_device_index"] == 5
    assert spk_open["rate"] == 16_000

    # Both streams closed.
    assert all(s.closed for s in fake_openable_pyaudio.streams)
    # Mic stream specifically read 3 chunks.
    mic_stream = fake_openable_pyaudio.streams[0]
    assert mic_stream.read_calls == 3
    # Speaker stream NEVER reads (it's an output stream) and NEVER writes
    # (avoids audible noise at startup).
    spk_stream = fake_openable_pyaudio.streams[1]
    assert spk_stream.read_calls == 0
    # PyAudio() instance terminated exactly once.
    assert fake_openable_pyaudio.terminate_count == 1


def test_probe_skips_none_input(fake_openable_pyaudio: _FakeOpenablePyAudio) -> None:
    """``input_index=None`` skips the mic probe entirely; output still runs."""
    probe_devices_openable(AudioDeviceIndices(input_index=None, output_index=3))
    # Only one open call — the speaker's.
    assert len(fake_openable_pyaudio.open_calls) == 1
    assert fake_openable_pyaudio.open_calls[0]["output"] is True


def test_probe_skips_none_output(fake_openable_pyaudio: _FakeOpenablePyAudio) -> None:
    """``output_index=None`` skips the speaker probe; mic still runs."""
    probe_devices_openable(AudioDeviceIndices(input_index=4, output_index=None))
    assert len(fake_openable_pyaudio.open_calls) == 1
    assert fake_openable_pyaudio.open_calls[0]["input"] is True


def test_probe_skips_both_when_indices_are_none(
    fake_openable_pyaudio: _FakeOpenablePyAudio,
) -> None:
    """All-None indices: no opens, but PyAudio() is still terminated."""
    probe_devices_openable(AudioDeviceIndices(input_index=None, output_index=None))
    assert fake_openable_pyaudio.open_calls == []
    assert fake_openable_pyaudio.terminate_count == 1


def test_probe_mic_open_failure_wraps_in_startup_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PyAudio OSError on mic open → StartupValidationError(stage="audio.mic")."""
    pa = _FakeOpenablePyAudio(open_error=OSError("device busy"))
    monkeypatch.setattr(devices_mod, "pyaudio", _FakeOpenableModule(pa))

    with pytest.raises(StartupValidationError) as exc_info:
        probe_devices_openable(AudioDeviceIndices(input_index=1, output_index=2))
    assert exc_info.value.context["stage"] == "audio.mic"
    assert exc_info.value.context["device_index"] == 1
    assert "device busy" in exc_info.value.context["reason"]
    # Even on failure, PyAudio() must terminate (cleanup discipline).
    assert pa.terminate_count == 1


def test_probe_mic_read_failure_wraps_in_startup_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mic opens but read() raises → StartupValidationError(stage="audio.mic").

    Catches the "opens but yields no samples" failure on flaky USB mics —
    a read() that raises rather than blocking forever.
    """
    pa = _FakeOpenablePyAudio(read_error=OSError("input overflowed"))
    monkeypatch.setattr(devices_mod, "pyaudio", _FakeOpenableModule(pa))

    with pytest.raises(StartupValidationError) as exc_info:
        probe_devices_openable(AudioDeviceIndices(input_index=1, output_index=None))
    assert exc_info.value.context["stage"] == "audio.mic"
    # Stream that failed mid-read must still close (probe owns cleanup).
    assert pa.streams[0].closed


def test_probe_speaker_failure_wraps_in_startup_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Speaker open OSError → StartupValidationError(stage="audio.speaker").

    Only the speaker side fails: mic succeeds and gets cleaned up first.
    """

    class _PaFailOutputOnly(_FakeOpenablePyAudio):
        def open(self, **kwargs: Any) -> _FakeStream:
            if kwargs.get("output"):
                raise OSError("speaker mute")
            return super().open(**kwargs)

    pa = _PaFailOutputOnly()
    monkeypatch.setattr(devices_mod, "pyaudio", _FakeOpenableModule(pa))

    with pytest.raises(StartupValidationError) as exc_info:
        probe_devices_openable(AudioDeviceIndices(input_index=1, output_index=2))
    assert exc_info.value.context["stage"] == "audio.speaker"
    assert exc_info.value.context["device_index"] == 2
    assert "speaker mute" in exc_info.value.context["reason"]
    # Mic stream from before the speaker failure must have closed cleanly.
    assert pa.streams[0].closed
    # PyAudio() terminated despite the speaker failure.
    assert pa.terminate_count == 1
