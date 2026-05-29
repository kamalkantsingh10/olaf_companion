"""Unit tests for :mod:`voice_agent_pipeline.audio.openers` (Story 6.2).

Two surfaces under test:

- :func:`pick_opener` — pure selection logic. No IO, no mocks
  required. Exercises last-N suppression, empty-bucket handling, the
  exclusion-empties-then-resets path. Mirrors the deleted
  ``test_filler.py``'s structure but bucket-keyed (no mood
  fallback).
- :func:`trigger_opener_fallback` — async orchestration. Uses
  ``asyncio.Event`` to model the splitter-side signal and a fake
  manifest that yields a no-IO pick (we mock ``play_cached`` at the
  module boundary).

CLAUDE.md rule 7 — mock at the module boundary only. ``play_cached``
is the boundary between the openers module and PyAudio; we patch
the symbol imported into ``openers`` rather than reaching into
PyAudio internals.
"""

from collections import deque
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from voice_agent_pipeline.audio import openers as openers_module
from voice_agent_pipeline.audio.cached import (
    CachedAudioEntry,
    CachedAudioManifest,
)
from voice_agent_pipeline.audio.opener_bucket import OpenerBucket
from voice_agent_pipeline.audio.openers import (
    pick_opener,
    trigger_opener_fallback,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _entry(bucket: OpenerBucket, phrase: str, phrase_hash: str) -> CachedAudioEntry:
    """Build a single opener manifest entry with the given hash."""
    return CachedAudioEntry(
        surface="opener",
        mood=None,
        bucket=bucket,
        phrase_hash=phrase_hash,
        phrase=phrase,
        path=f"/tmp/{phrase_hash}.wav",  # noqa: S108
        duration_ms=500,
    )


def _manifest(entries: list[CachedAudioEntry]) -> CachedAudioManifest:
    return CachedAudioManifest(
        schema_version=2,
        generated_at=datetime.now(tz=UTC),
        voice_id="v",
        tts_model="m",
        entries=entries,
    )


# ---------------------------------------------------------------------------
# pick_opener
# ---------------------------------------------------------------------------


def test_pick_opener_returns_entry_from_requested_bucket() -> None:
    """Happy path — pick a take from the requested bucket; ignore other buckets."""
    e_think = _entry("thinking", "hmm", "h_thinking")
    e_ack = _entry("acknowledge", "yeah", "h_ack")
    manifest = _manifest([e_think, e_ack])
    pick = pick_opener(manifest, "thinking", deque(maxlen=1))
    assert pick == e_think


def test_pick_opener_returns_none_for_empty_bucket() -> None:
    """Story 6.2: NO mood fallback. Empty bucket returns None."""
    manifest = _manifest([_entry("thinking", "hmm", "h_thinking")])
    # Request a bucket with no entries; production prevents this via
    # the OpenersConfig validator. Selector returns None defensively.
    pick = pick_opener(manifest, "look_up", deque(maxlen=1))
    assert pick is None


def test_pick_opener_excludes_recent_phrases() -> None:
    """Last-N suppression — entries in ``recent`` are excluded."""
    e1 = _entry("thinking", "hmm", "h1")
    e2 = _entry("thinking", "let me think", "h2")
    manifest = _manifest([e1, e2])
    # Both candidates exist; recent excludes h1 → selector must return h2.
    recent: deque[str] = deque(["h1"], maxlen=1)
    pick = pick_opener(manifest, "thinking", recent)
    assert pick == e2


def test_pick_opener_resets_recent_when_exclusion_empties_bucket() -> None:
    """If suppression leaves no candidates, reset history and pick from full bucket."""
    only = _entry("thinking", "hmm", "h_only")
    manifest = _manifest([only])
    recent: deque[str] = deque(["h_only"], maxlen=1)
    pick = pick_opener(manifest, "thinking", recent)
    # The bucket only has one entry. Suppression empties candidates;
    # selector resets `recent` and re-picks. Returns the same entry.
    assert pick == only
    # The reset is observable in the caller's deque.
    assert list(recent) == []


# ---------------------------------------------------------------------------
# trigger_opener_fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_opener_fallback_returns_when_splitter_signals_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM-tag-selected path: ``opener_selected`` set before timer → no playback."""
    import asyncio

    play_called: list[bool] = []

    async def _fake_play(*_args: object, **_kwargs: object) -> None:
        play_called.append(True)

    monkeypatch.setattr(openers_module, "play_cached", _fake_play)

    manifest = _manifest([_entry("acknowledge", "yeah", "h_ack")])
    opener_selected = asyncio.Event()
    opener_already_playing = asyncio.Event()

    # Signal "splitter picked an opener" immediately — fallback should bail.
    opener_selected.set()
    await trigger_opener_fallback(
        pa=MagicMock(),
        indices=MagicMock(output_index=0),
        manifest=manifest,
        timer_fallback_ms=50,
        timer_fallback_bucket="acknowledge",
        opener_selected=opener_selected,
        opener_already_playing=opener_already_playing,
        recent=deque(maxlen=1),
    )
    assert play_called == [], "fallback should not play when splitter signals first"
    assert not opener_already_playing.is_set(), "fallback didn't claim the slot"


@pytest.mark.asyncio
async def test_trigger_opener_fallback_plays_when_no_splitter_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timer fires when ``opener_selected`` never sets → cached opener plays."""
    import asyncio

    play_mock = AsyncMock()
    monkeypatch.setattr(openers_module, "play_cached", play_mock)

    manifest = _manifest([_entry("acknowledge", "yeah", "h_ack")])
    opener_selected = asyncio.Event()
    opener_already_playing = asyncio.Event()
    recent: deque[str] = deque(maxlen=1)

    await trigger_opener_fallback(
        pa=MagicMock(),
        indices=MagicMock(output_index=0),
        manifest=manifest,
        timer_fallback_ms=10,
        timer_fallback_bucket="acknowledge",
        opener_selected=opener_selected,
        opener_already_playing=opener_already_playing,
        recent=recent,
    )
    assert play_mock.await_count == 1, "fallback should play exactly one opener"
    assert opener_already_playing.is_set(), "fallback claimed the slot before play"
    assert list(recent) == ["h_ack"], "fallback recorded the phrase hash in `recent`"


@pytest.mark.asyncio
async def test_trigger_opener_fallback_skips_play_if_already_playing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race-window check: ``opener_already_playing`` set during sleep → bail."""
    import asyncio

    play_mock = AsyncMock()
    monkeypatch.setattr(openers_module, "play_cached", play_mock)

    manifest = _manifest([_entry("acknowledge", "yeah", "h_ack")])
    opener_selected = asyncio.Event()
    opener_already_playing = asyncio.Event()
    opener_already_playing.set()  # splitter beat the timer

    await trigger_opener_fallback(
        pa=MagicMock(),
        indices=MagicMock(output_index=0),
        manifest=manifest,
        timer_fallback_ms=10,
        timer_fallback_bucket="acknowledge",
        opener_selected=opener_selected,
        opener_already_playing=opener_already_playing,
        recent=deque(maxlen=1),
    )
    # Timer expired (splitter never signalled), but the race check
    # caught `opener_already_playing` → no play.
    assert play_mock.await_count == 0


@pytest.mark.asyncio
async def test_trigger_opener_fallback_warns_on_empty_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the configured timer_fallback_bucket is empty, log warn + bail."""
    import asyncio

    play_mock = AsyncMock()
    monkeypatch.setattr(openers_module, "play_cached", play_mock)

    # Manifest has 'thinking' entries but no 'acknowledge' entries —
    # production OpenersConfig validator should prevent this; the
    # fallback handles it gracefully nonetheless.
    manifest = _manifest([_entry("thinking", "hmm", "h_thinking")])

    await trigger_opener_fallback(
        pa=MagicMock(),
        indices=MagicMock(output_index=0),
        manifest=manifest,
        timer_fallback_ms=10,
        timer_fallback_bucket="acknowledge",
        opener_selected=asyncio.Event(),
        opener_already_playing=asyncio.Event(),
        recent=deque(maxlen=1),
    )
    assert play_mock.await_count == 0
