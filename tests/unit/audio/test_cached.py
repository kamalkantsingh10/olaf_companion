"""Unit tests for :mod:`voice_agent_pipeline.audio.cached`.

Covers:

- :func:`compute_phrase_hash` — stable + sensitive to all three inputs.
- :class:`CachedAudioManifest` — round-trip + lookup happy path + miss raises.
- :func:`load_manifest` — happy path + missing file + bad JSON + bad schema_version.
- :func:`load_and_validate_manifest` — happy path + voice mismatch + missing phrase +
  missing file invariants.
- :func:`play_cached` — format validation (rejects wrong sample-rate / channels).

PyAudio is mocked at the module boundary; we never touch real audio hardware.
WAV files are created on the fly under ``tmp_path`` via stdlib ``wave``.
"""

import json
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from tests._factories import (
    minimal_filler_config,
    minimal_goodbye_config,
    minimal_greeting_config,
    minimal_stt_config,
)
from voice_agent_pipeline.audio.cached import (
    CachedAudioEntry,
    CachedAudioManifest,
    compute_phrase_hash,
    load_and_validate_manifest,
    load_manifest,
    play_cached,
)
from voice_agent_pipeline.config.setup import (
    AudioConfig,
    SetupConfig,
    TtsConfig,
    WakewordConfig,
)
from voice_agent_pipeline.errors import StartupValidationError

# ---------------------------------------------------------------------------
# compute_phrase_hash
# ---------------------------------------------------------------------------


def test_compute_phrase_hash_is_stable() -> None:
    """Same inputs → same hash, every time."""
    a = compute_phrase_hash("hello", "voice-1", "sonic-3", "calm")
    b = compute_phrase_hash("hello", "voice-1", "sonic-3", "calm")
    assert a == b
    assert len(a) == 16  # truncated to 16 hex chars


def test_compute_phrase_hash_changes_with_phrase() -> None:
    """Different phrase → different hash (the obvious case)."""
    a = compute_phrase_hash("hello", "voice-1", "sonic-3", "calm")
    b = compute_phrase_hash("hi", "voice-1", "sonic-3", "calm")
    assert a != b


def test_compute_phrase_hash_changes_with_voice_id() -> None:
    """Different voice_id → different hash (forces regen on voice swap)."""
    a = compute_phrase_hash("hello", "voice-1", "sonic-3", "calm")
    b = compute_phrase_hash("hello", "voice-2", "sonic-3", "calm")
    assert a != b


def test_compute_phrase_hash_changes_with_model() -> None:
    """Different tts_model → different hash (forces regen on model swap)."""
    a = compute_phrase_hash("hello", "voice-1", "sonic-3", "calm")
    b = compute_phrase_hash("hello", "voice-1", "sonic-4", "calm")
    assert a != b


def test_compute_phrase_hash_changes_with_mood() -> None:
    """Different mood bucket → different hash.

    Catches the 2026-05-12 regression: phrases like "yeah?" appear in
    multiple mood buckets (calm + curious + sleepy greetings). Before
    mood was part of the hash, those collapsed to one manifest entry
    and runtime lookups failed for the missing buckets at first fire.
    """
    a = compute_phrase_hash("yeah?", "voice-1", "sonic-3", "calm")
    b = compute_phrase_hash("yeah?", "voice-1", "sonic-3", "sleepy")
    assert a != b


def test_compute_phrase_hash_none_mood_differs_from_named_mood() -> None:
    """Mood=None (flat surfaces) doesn't collide with the sentinel string."""
    a = compute_phrase_hash("bye", "voice-1", "sonic-3", None)
    # The sentinel is "__none__"; this would only collide if someone
    # added a Mood Literal value with the exact string "__none__".
    b = compute_phrase_hash("bye", "voice-1", "sonic-3", "calm")
    assert a != b


# ---------------------------------------------------------------------------
# CachedAudioManifest.lookup
# ---------------------------------------------------------------------------


def _entry(
    surface: str,
    phrase: str,
    mood: str | None = None,
    path: str = "x.wav",
) -> CachedAudioEntry:
    return CachedAudioEntry(
        surface=surface,  # type: ignore[arg-type]
        mood=mood,  # type: ignore[arg-type]
        phrase_hash=compute_phrase_hash(phrase, "v", "m", mood),  # type: ignore[arg-type]
        phrase=phrase,
        path=path,
        duration_ms=500,
    )


def _manifest(entries: list[CachedAudioEntry]) -> CachedAudioManifest:
    return CachedAudioManifest(
        schema_version=1,
        generated_at=datetime.now(tz=UTC),
        voice_id="v",
        tts_model="m",
        entries=entries,
    )


def test_lookup_finds_matching_entry() -> None:
    """Happy path — entry with matching surface+phrase+mood is returned."""
    e = _entry("greeting", "hi", mood="calm")
    m = _manifest([e])
    assert m.lookup("greeting", "hi", mood="calm") == e


def test_lookup_misses_when_phrase_not_present() -> None:
    """A phrase not in the manifest raises KeyError."""
    m = _manifest([_entry("greeting", "hi", mood="calm")])
    with pytest.raises(KeyError):
        m.lookup("greeting", "missing", mood="calm")


def test_lookup_distinguishes_mood() -> None:
    """Same surface+phrase but different mood is a miss."""
    m = _manifest([_entry("greeting", "hi", mood="calm")])
    with pytest.raises(KeyError):
        m.lookup("greeting", "hi", mood="happy")


def test_lookup_flat_surface_requires_none_mood() -> None:
    """Goodbye / clarification entries store mood=None; lookup must match."""
    m = _manifest([_entry("goodbye", "bye", mood=None)])
    assert m.lookup("goodbye", "bye", mood=None).phrase == "bye"
    # Passing a mood when the entry has mood=None is a miss.
    with pytest.raises(KeyError):
        m.lookup("goodbye", "bye", mood="calm")


def test_phrases_for_surface_filters_by_surface() -> None:
    """Helper returns entries for one surface, preserving order."""
    e1 = _entry("greeting", "hi", mood="calm")
    e2 = _entry("goodbye", "bye")
    e3 = _entry("greeting", "hello", mood="happy")
    m = _manifest([e1, e2, e3])
    assert m.phrases_for_surface("greeting") == [e1, e3]
    assert m.phrases_for_surface("goodbye") == [e2]


# ---------------------------------------------------------------------------
# load_manifest
# ---------------------------------------------------------------------------


def _write_valid_manifest(tmp_path: Path, **overrides: Any) -> Path:
    """Write a minimal valid manifest under tmp_path; return the path."""
    body = {
        "schema_version": 1,
        "generated_at": "2026-05-12T00:00:00+00:00",
        "voice_id": "v",
        "tts_model": "m",
        "entries": [],
    }
    body.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(body))
    return path


def test_load_manifest_happy_path(tmp_path: Path) -> None:
    """A valid manifest file parses into a CachedAudioManifest."""
    path = _write_valid_manifest(tmp_path)
    m = load_manifest(path)
    assert m.schema_version == 1
    assert m.voice_id == "v"


def test_load_manifest_missing_file_raises_startup_error(tmp_path: Path) -> None:
    """A missing manifest file surfaces as StartupValidationError, not FileNotFound."""
    with pytest.raises(StartupValidationError) as exc_info:
        load_manifest(tmp_path / "does-not-exist.json")
    assert exc_info.value.context["stage"] == "audio_assets"
    # Operator-actionable message.
    assert "regenerate-audio" in exc_info.value.context["action"]


def test_load_manifest_malformed_json_raises(tmp_path: Path) -> None:
    """A non-JSON file raises StartupValidationError with the parse error."""
    path = tmp_path / "manifest.json"
    path.write_text("{ not json")
    with pytest.raises(StartupValidationError) as exc_info:
        load_manifest(path)
    assert "valid JSON" in exc_info.value.context["reason"]


def test_load_manifest_wrong_schema_version_raises(tmp_path: Path) -> None:
    """A manifest from a future build version is rejected with a clear error."""
    path = _write_valid_manifest(tmp_path, schema_version=999)
    with pytest.raises(StartupValidationError) as exc_info:
        load_manifest(path)
    assert "schema_version" in exc_info.value.context["reason"]


# ---------------------------------------------------------------------------
# load_and_validate_manifest — full Stage 3 probe
# ---------------------------------------------------------------------------


def _make_setup_config(tmp_path: Path) -> SetupConfig:
    """Build a SetupConfig with minimal-valid surface lists for the probe."""
    return SetupConfig.model_construct(
        schema_version=3,
        picovoice_access_key=SecretStr("stub"),
        cartesia_api_key=SecretStr("stub"),
        audio=AudioConfig(input_device_name="m", output_device_name="s"),
        wakeword=WakewordConfig(model_path=tmp_path / "x.ppn"),
        tts=TtsConfig(voice_id="v", model="m"),
        stt=minimal_stt_config(),
        greeting=minimal_greeting_config(),
        goodbye=minimal_goodbye_config(),
        filler=minimal_filler_config(),
    )


def _all_required_entries(config: SetupConfig, tmp_path: Path) -> list[dict[str, Any]]:
    """Build manifest-shaped entries covering every phrase in setup.toml."""
    entries: list[dict[str, Any]] = []

    def _push(surface: str, phrase: str, mood: str | None) -> None:
        h = compute_phrase_hash(phrase, config.tts.voice_id, config.tts.model, mood)  # type: ignore[arg-type]
        # The probe checks ``Path(entry.path).exists()`` — we touch
        # each WAV path so the file-presence invariant passes.
        path = tmp_path / f"{h}.wav"
        path.touch()
        entries.append(
            {
                "surface": surface,
                "mood": mood,
                "phrase_hash": h,
                "phrase": phrase,
                "path": str(path),
                "duration_ms": 400,
            },
        )

    for mood, bucket in config.greeting.greetings_by_mood.items():
        for phrase in bucket:
            _push("greeting", phrase, mood)
    for phrase in config.goodbye.phrases:
        _push("goodbye", phrase, None)
    for phrase in config.stt.clarification_prompts:
        _push("clarification", phrase, None)
    for mood, bucket in config.filler.phrases_by_mood.items():
        for phrase in bucket:
            _push("filler", phrase, mood)
    return entries


def test_load_and_validate_manifest_happy_path(tmp_path: Path) -> None:
    """When the manifest has every required phrase and every file exists, return it."""
    config = _make_setup_config(tmp_path)
    entries = _all_required_entries(config, tmp_path)
    manifest_path = _write_valid_manifest(tmp_path, entries=entries)

    result = load_and_validate_manifest(config, manifest_path)

    assert isinstance(result, CachedAudioManifest)
    assert len(result.entries) == len(entries)


def test_load_and_validate_manifest_voice_id_mismatch_raises(tmp_path: Path) -> None:
    """Manifest's voice_id ≠ config.tts.voice_id → StartupValidationError."""
    config = _make_setup_config(tmp_path)
    entries = _all_required_entries(config, tmp_path)
    manifest_path = _write_valid_manifest(
        tmp_path,
        voice_id="different-voice",  # ← drift
        entries=entries,
    )
    with pytest.raises(StartupValidationError) as exc_info:
        load_and_validate_manifest(config, manifest_path)
    assert "voice_id" in exc_info.value.context["reason"]


def test_load_and_validate_manifest_tts_model_mismatch_raises(tmp_path: Path) -> None:
    """Manifest's tts_model ≠ config.tts.model → StartupValidationError."""
    config = _make_setup_config(tmp_path)
    entries = _all_required_entries(config, tmp_path)
    manifest_path = _write_valid_manifest(
        tmp_path,
        tts_model="different-model",  # ← drift
        entries=entries,
    )
    with pytest.raises(StartupValidationError) as exc_info:
        load_and_validate_manifest(config, manifest_path)
    assert "tts_model" in exc_info.value.context["reason"]


def test_load_and_validate_manifest_missing_phrase_raises(tmp_path: Path) -> None:
    """A phrase in setup.toml with no matching manifest entry fails the probe."""
    config = _make_setup_config(tmp_path)
    entries = _all_required_entries(config, tmp_path)
    # Drop one entry from the manifest while leaving setup.toml unchanged.
    entries.pop()
    manifest_path = _write_valid_manifest(tmp_path, entries=entries)
    with pytest.raises(StartupValidationError) as exc_info:
        load_and_validate_manifest(config, manifest_path)
    assert "no cached audio" in exc_info.value.context["reason"]


def test_load_and_validate_manifest_missing_file_raises(tmp_path: Path) -> None:
    """Manifest references a path that doesn't exist → StartupValidationError."""
    config = _make_setup_config(tmp_path)
    entries = _all_required_entries(config, tmp_path)
    # Delete one of the touched WAV files post-hoc.
    Path(entries[0]["path"]).unlink()
    manifest_path = _write_valid_manifest(tmp_path, entries=entries)
    with pytest.raises(StartupValidationError) as exc_info:
        load_and_validate_manifest(config, manifest_path)
    assert "WAV file" in exc_info.value.context["reason"]


# ---------------------------------------------------------------------------
# play_cached — format validation + PyAudio interaction
# ---------------------------------------------------------------------------


def _write_test_wav(
    path: Path,
    *,
    rate: int = 16_000,
    channels: int = 1,
    sample_width: int = 2,
    duration_ms: int = 200,
) -> None:
    """Synthesize a silence WAV at the given format."""
    n_frames = int(rate * duration_ms / 1000)
    silence = b"\x00" * (n_frames * channels * sample_width)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(rate)
        wav.writeframes(silence)


class _FakeStream:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.stopped = False
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class _FakePyAudio:
    def __init__(self) -> None:
        self.open_calls: list[dict[str, Any]] = []
        self.streams: list[_FakeStream] = []

    def open(self, **kwargs: Any) -> _FakeStream:
        self.open_calls.append(kwargs)
        s = _FakeStream()
        self.streams.append(s)
        return s


async def test_play_cached_happy_path(tmp_path: Path) -> None:
    """Valid 16 kHz mono S16LE WAV is read + streamed through PyAudio."""
    wav_path = tmp_path / "ok.wav"
    _write_test_wav(wav_path, duration_ms=500)
    pa = _FakePyAudio()

    await play_cached(pa, output_index=2, path=wav_path)  # type: ignore[arg-type]

    # PyAudio.open was called with the expected format.
    assert len(pa.open_calls) == 1
    call = pa.open_calls[0]
    assert call["rate"] == 16_000
    assert call["channels"] == 1
    assert call["output"] is True
    assert call["output_device_index"] == 2

    # Stream cleaned up.
    assert pa.streams[0].stopped
    assert pa.streams[0].closed
    # Bytes were written.
    total = sum(len(b) for b in pa.streams[0].writes)
    # 500 ms of mono int16 16 kHz = 16_000 bytes
    assert total == 16_000


async def test_play_cached_rejects_wrong_sample_rate(tmp_path: Path) -> None:
    """A 44.1 kHz WAV raises wave.Error before any PyAudio call."""
    wav_path = tmp_path / "bad-rate.wav"
    _write_test_wav(wav_path, rate=44_100)
    pa = _FakePyAudio()

    with pytest.raises(wave.Error) as exc_info:
        await play_cached(pa, output_index=2, path=wav_path)  # type: ignore[arg-type]
    assert "rate" in str(exc_info.value)
    # PyAudio.open should NOT have been called — format check is first.
    assert pa.open_calls == []


async def test_play_cached_rejects_stereo(tmp_path: Path) -> None:
    """A 2-channel WAV is rejected."""
    wav_path = tmp_path / "bad-channels.wav"
    _write_test_wav(wav_path, channels=2)
    pa = _FakePyAudio()

    with pytest.raises(wave.Error) as exc_info:
        await play_cached(pa, output_index=2, path=wav_path)  # type: ignore[arg-type]
    assert "channels" in str(exc_info.value)


async def test_play_cached_rejects_wrong_sample_width(tmp_path: Path) -> None:
    """A 24-bit (3-byte) WAV is rejected — pipeline expects 16-bit."""
    wav_path = tmp_path / "bad-width.wav"
    _write_test_wav(wav_path, sample_width=3)
    pa = _FakePyAudio()

    with pytest.raises(wave.Error) as exc_info:
        await play_cached(pa, output_index=2, path=wav_path)  # type: ignore[arg-type]
    assert "sample width" in str(exc_info.value)
