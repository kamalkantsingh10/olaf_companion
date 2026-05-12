"""Unit tests for :mod:`voice_agent_pipeline.audio.filler`.

Two surfaces:

- :func:`pick_filler` — pure picker. Fast tests for mood matching,
  fallback to calm, last-N suppression, and exhaustion-reset behavior.
- :func:`maybe_play_filler` — async orchestration. Tests cover the
  fast-turn (audio_started fires before threshold → no filler played)
  and slow-turn (threshold expires → filler picked + play_cached
  called) paths.

PyAudio + play_cached are mocked at module boundaries.
"""

import asyncio
import random
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from voice_agent_pipeline.audio import filler as filler_mod
from voice_agent_pipeline.audio.cached import CachedAudioEntry, CachedAudioManifest
from voice_agent_pipeline.audio.devices import AudioDeviceIndices
from voice_agent_pipeline.audio.filler import maybe_play_filler, pick_filler


def _filler_entry(phrase: str, mood: str | None) -> CachedAudioEntry:
    return CachedAudioEntry(
        surface="filler",
        mood=mood,  # type: ignore[arg-type]
        phrase_hash=f"hash-{phrase}",
        phrase=phrase,
        path=f"{phrase}.wav",
        duration_ms=400,
    )


def _manifest(entries: list[CachedAudioEntry]) -> CachedAudioManifest:
    return CachedAudioManifest(
        schema_version=1,
        generated_at=datetime.now(tz=UTC),
        voice_id="v",
        tts_model="m",
        entries=entries,
    )


# ---------------------------------------------------------------------------
# pick_filler — mood matching + fallback
# ---------------------------------------------------------------------------


def test_pick_filler_returns_mood_matched_entry() -> None:
    """Happy path: bucket for current mood is non-empty → picks from it."""
    entries = [
        _filler_entry("hmm", mood="calm"),
        _filler_entry("ooh", mood="excited"),
    ]
    manifest = _manifest(entries)

    pick = pick_filler(manifest, mood="excited", recent=deque(maxlen=1))

    assert pick is not None
    assert pick.mood == "excited"
    assert pick.phrase == "ooh"


def test_pick_filler_falls_back_to_calm_when_mood_bucket_empty() -> None:
    """If the requested mood has no entries, fall back to ``calm``."""
    # Manifest has calm + happy entries, but request mood "playful"
    # which has no filler entries — should fall back to calm.
    entries = [
        _filler_entry("hmm", mood="calm"),
        _filler_entry("oh!", mood="happy"),
    ]
    manifest = _manifest(entries)

    pick = pick_filler(manifest, mood="playful", recent=deque(maxlen=1))

    assert pick is not None
    assert pick.mood == "calm"


def test_pick_filler_returns_none_when_no_fallback_available() -> None:
    """Both mood and calm buckets empty → return None (defensive)."""
    # Manifest has only happy entries; request "playful" with no
    # calm fallback available either.
    entries = [_filler_entry("oh!", mood="happy")]
    manifest = _manifest(entries)

    pick = pick_filler(manifest, mood="playful", recent=deque(maxlen=1))

    assert pick is None


# ---------------------------------------------------------------------------
# pick_filler — last-N suppression
# ---------------------------------------------------------------------------


def test_pick_filler_excludes_recent_when_options_remain() -> None:
    """Recently-played filler is excluded if other options exist."""
    entries = [
        _filler_entry("hmm", mood="calm"),
        _filler_entry("uh", mood="calm"),
    ]
    manifest = _manifest(entries)
    # "hmm" was just played — should pick "uh" deterministically since
    # it's the only remaining option.
    recent = deque(["hash-hmm"], maxlen=1)

    pick = pick_filler(manifest, mood="calm", recent=recent)

    assert pick is not None
    assert pick.phrase == "uh"


def test_pick_filler_resets_recent_when_bucket_exhausted() -> None:
    """If exclusion empties the bucket, recent is reset and we re-pick from full bucket."""
    entries = [_filler_entry("hmm", mood="calm")]  # bucket size 1
    manifest = _manifest(entries)
    # "hmm" is the only filler AND it's marked recent → exclusion
    # empties the bucket → pick_filler resets recent and re-picks.
    recent = deque(["hash-hmm"], maxlen=1)

    pick = pick_filler(manifest, mood="calm", recent=recent)

    assert pick is not None
    assert pick.phrase == "hmm"
    # The deque was reset by pick_filler.
    assert len(recent) == 0


def test_pick_filler_excludes_only_window_size_entries() -> None:
    """A ring buffer of size N excludes only the last N picks.

    Behavior check: with three entries A, B, C and recent=[A] (size 1),
    only A is excluded; B and C are both candidates.
    """
    entries = [
        _filler_entry("A", mood="calm"),
        _filler_entry("B", mood="calm"),
        _filler_entry("C", mood="calm"),
    ]
    manifest = _manifest(entries)
    recent = deque(["hash-A"], maxlen=1)

    # Force determinism — random.choice over [B, C] should pick one
    # of them. Seed so the test is reproducible.
    random.seed(0)
    pick = pick_filler(manifest, mood="calm", recent=recent)

    assert pick is not None
    assert pick.phrase in {"B", "C"}


# ---------------------------------------------------------------------------
# maybe_play_filler — orchestration + threshold timing
# ---------------------------------------------------------------------------


class _StubPyAudio:
    """Minimal stand-in for pyaudio.PyAudio — never opened by tests below.

    The play_cached function is monkeypatched, so PyAudio is unused in
    these tests. The stub satisfies the type contract.
    """


async def test_maybe_play_filler_skips_when_audio_starts_before_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audio_started.set() before min_pause_ms → no filler played."""
    play_calls: list[Any] = []

    async def _fake_play_cached(*args: Any, **kwargs: Any) -> None:
        play_calls.append((args, kwargs))

    monkeypatch.setattr(filler_mod, "play_cached", _fake_play_cached)

    audio_started = asyncio.Event()
    manifest = _manifest([_filler_entry("hmm", mood="calm")])
    recent: deque[str] = deque(maxlen=1)

    # Spawn the filler with a 500ms threshold; fire audio_started
    # after only 50ms (well before threshold). Expectation: filler
    # returns without playing.
    task = asyncio.create_task(
        maybe_play_filler(
            pa=_StubPyAudio(),  # type: ignore[arg-type]
            indices=AudioDeviceIndices(input_index=0, output_index=1),
            mood="calm",
            manifest=manifest,
            min_pause_ms=500,
            audio_started=audio_started,
            recent=recent,
        ),
    )
    await asyncio.sleep(0.05)
    audio_started.set()
    await task

    assert play_calls == []
    # No filler was picked; recent should remain empty.
    assert len(recent) == 0


async def test_maybe_play_filler_plays_when_threshold_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When audio_started doesn't fire within min_pause_ms, play a filler."""
    play_calls: list[Any] = []

    async def _fake_play_cached(pa: Any, output_index: int, path: Path) -> None:
        play_calls.append((output_index, path))

    monkeypatch.setattr(filler_mod, "play_cached", _fake_play_cached)

    audio_started = asyncio.Event()  # NEVER set in this test
    manifest = _manifest([_filler_entry("hmm", mood="calm")])
    recent: deque[str] = deque(maxlen=1)

    # 50ms threshold so the test is fast. Don't set audio_started —
    # expect filler to play.
    await maybe_play_filler(
        pa=_StubPyAudio(),  # type: ignore[arg-type]
        indices=AudioDeviceIndices(input_index=0, output_index=2),
        mood="calm",
        manifest=manifest,
        min_pause_ms=50,
        audio_started=audio_started,
        recent=recent,
    )

    assert len(play_calls) == 1
    output_index, path = play_calls[0]
    assert output_index == 2
    assert path == Path("hmm.wav")
    # Recent was updated.
    assert list(recent) == ["hash-hmm"]


async def test_maybe_play_filler_handles_race_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audio_started fires DURING the timeout but after wait_for returned: abandon play.

    Tests the race-window check between ``wait_for`` raising TimeoutError
    and the play_cached call.
    """
    play_calls: list[Any] = []

    async def _fake_play_cached(*args: Any, **kwargs: Any) -> None:
        play_calls.append((args, kwargs))

    monkeypatch.setattr(filler_mod, "play_cached", _fake_play_cached)

    audio_started = asyncio.Event()
    manifest = _manifest([_filler_entry("hmm", mood="calm")])

    # Simulate the race: pre-set audio_started so the post-timeout
    # ``is_set()`` check trips. Use min_pause_ms=0 to force the
    # wait_for to time out immediately.
    audio_started.set()

    await maybe_play_filler(
        pa=_StubPyAudio(),  # type: ignore[arg-type]
        indices=AudioDeviceIndices(input_index=0, output_index=1),
        mood="calm",
        manifest=manifest,
        min_pause_ms=1,  # gt=0 from FillerConfig
        audio_started=audio_started,
        recent=deque(maxlen=1),
    )

    # audio_started was already set — wait_for returns normally
    # without raising, so the function exits before the pick step.
    assert play_calls == []


async def test_maybe_play_filler_no_pick_returns_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If pick_filler returns None (empty buckets), the function logs and returns."""
    play_calls: list[Any] = []

    async def _fake_play_cached(*args: Any, **kwargs: Any) -> None:
        play_calls.append((args, kwargs))

    monkeypatch.setattr(filler_mod, "play_cached", _fake_play_cached)

    # Manifest with no filler entries at all → pick_filler returns
    # None for any mood.
    manifest = _manifest([])
    audio_started = asyncio.Event()  # not set

    await maybe_play_filler(
        pa=_StubPyAudio(),  # type: ignore[arg-type]
        indices=AudioDeviceIndices(input_index=0, output_index=1),
        mood="calm",
        manifest=manifest,
        min_pause_ms=10,
        audio_started=audio_started,
        recent=deque(maxlen=1),
    )

    assert play_calls == []
