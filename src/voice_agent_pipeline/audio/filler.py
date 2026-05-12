"""Thinking-filler picker + timer (Story 5.5).

Two pieces:

- :func:`pick_filler` — pure selection logic (no IO). Given a manifest,
  the current mood, and a small ring buffer of recently-played filler
  hashes, returns the next :class:`CachedAudioEntry` to play. Mood
  fallback, last-N suppression, and the random pick all live here so
  they're directly unit-testable without spinning up PyAudio.
- :func:`maybe_play_filler` — async orchestration. Sleeps up to
  ``min_pause_ms``; if the real-audio ``audio_started`` event fires
  before the threshold, returns without playing. Otherwise picks a
  filler via :func:`pick_filler` and plays it through PyAudio.

Designed to be spawned as a background task on VAD end-of-speech by
``sequential_loop``. The same task is later awaited by the real-audio
path right before it opens its output stream — that's how we serialize
filler vs real audio cleanly without sharing a stream.

Why this lives in a dedicated module rather than inside sequential_loop:

- The pure pick logic deserves its own tests (last-N suppression, mood
  fallback, exhaustion-reset behavior) — keeping it here gives those
  tests a clean import target.
- Story 4.5's :func:`activity.greeting.trigger_greeting` is the
  parallel for the greeting side; this module is its filler twin.
"""

import asyncio
import random
from collections import deque
from pathlib import Path
from typing import cast

import pyaudio
import structlog

from voice_agent_pipeline.audio.cached import (
    CachedAudioEntry,
    CachedAudioManifest,
    play_cached,
)
from voice_agent_pipeline.audio.devices import AudioDeviceIndices
from voice_agent_pipeline.schemas.mood_event import Mood

log = structlog.get_logger(__name__)


def pick_filler(
    manifest: CachedAudioManifest,
    mood: Mood,
    recent: deque[str],
) -> CachedAudioEntry | None:
    """Pick the next filler entry given current mood + recent history.

    Behavior:

    1. Find the filler bucket for ``mood``. If empty, fall back to the
       ``"calm"`` bucket (parallel to ``trigger_greeting``'s fallback).
       If both are empty, return ``None`` — the caller should treat
       this as "no filler available; just stay silent for this turn".
       (Production prevents this via the FillerConfig model_validator.)
    2. Exclude any entry whose ``phrase_hash`` is in ``recent``. If
       the exclusion empties the bucket, reset ``recent`` and re-try
       (this is the spec's "if the bucket is empty after exclusion,
       reset and pick again" behavior).
    3. ``random.choice`` over the surviving candidates.

    Args:
        manifest: The loaded cached-audio manifest.
        mood: Current mood from ``MoodController.current``.
        recent: Ring buffer of recently-played filler phrase_hashes.
            Maintained by the caller; its ``maxlen`` should be
            ``max_consecutive_repeat + 1`` (the most-recently-played
            filler is always excluded; longer windows exclude further
            back).

    Returns:
        The chosen :class:`CachedAudioEntry`, or ``None`` if both the
        mood-specific bucket and the calm fallback bucket are empty.
    """
    all_fillers = manifest.phrases_for_surface("filler")
    in_mood = [e for e in all_fillers if e.mood == mood]
    if not in_mood:
        in_mood = [e for e in all_fillers if e.mood == "calm"]
        if not in_mood:
            return None

    candidates = [e for e in in_mood if e.phrase_hash not in recent]
    if not candidates:
        # Exclusion emptied the bucket — reset history and pick from
        # the full bucket. Documented behavior: short-term variety is
        # preserved on average; the only way to repeat the previous
        # filler is when there's literally nothing else to pick.
        recent.clear()
        candidates = in_mood

    # random.choice on a non-empty list — S311 (bandit weak-PRNG) is
    # fine here; this isn't security-sensitive.
    return random.choice(candidates)  # noqa: S311


async def maybe_play_filler(
    pa: pyaudio.PyAudio,
    indices: AudioDeviceIndices,
    mood: Mood,
    manifest: CachedAudioManifest,
    min_pause_ms: int,
    audio_started: asyncio.Event,
    recent: deque[str],
) -> None:
    """Wait up to ``min_pause_ms`` for real audio; play a filler if it doesn't arrive.

    Coroutine spawned as a background task on VAD end-of-speech. The
    coroutine completes when either:

    - The real audio path sets ``audio_started`` before the threshold
      expires (fast turn, no filler played).
    - The threshold expires AND a filler entry is picked AND the
      cached WAV finishes playing.

    The real-audio path AWAITS this task before opening its output
    stream — that serializes the filler against the real reply
    without needing to share a PyAudio output stream.

    Args:
        pa: Live PyAudio instance (owned by ``run_sequential_loop``).
        indices: Resolved audio devices. ``output_index`` must be set
            (FR4 + Stage 3 audio probe both guarantee this in
            production).
        mood: Current mood — picks the matching filler bucket.
        manifest: Loaded cached-audio manifest.
        min_pause_ms: Threshold below which the filler is suppressed.
            From ``config.filler.min_pause_ms``.
        audio_started: :class:`asyncio.Event` set by the real-audio
            path right before its output stream opens. The filler
            checks this both via ``wait_for`` (during the threshold
            sleep) and again after the sleep returns (race-window
            check).
        recent: Mutable ring buffer of recently-played filler hashes.
            Updated in-place if a filler is played.
    """
    try:
        # The cheap "real audio came in first" path: wait_for with a
        # timeout. If the event fires before ``min_pause_ms`` expires,
        # this returns normally and we exit without playing.
        await asyncio.wait_for(audio_started.wait(), timeout=min_pause_ms / 1000)
        return
    except TimeoutError:
        # Threshold expired without real audio — proceed to picking
        # a filler. Fall through.
        pass

    # Race-window check: between the wait_for timeout firing and us
    # actually picking + playing, the real audio path might have just
    # set ``audio_started``. If so, abandon the filler.
    if audio_started.is_set():
        return

    pick = pick_filler(manifest, mood, recent)
    if pick is None:
        # No filler available (both mood + calm buckets empty). The
        # FillerConfig validator should prevent this in production —
        # this branch exists for defensive correctness.
        log.warning("filler.no_pick", mood=mood)
        return

    log.info(
        "filler.picked",
        mood=mood,
        phrase=pick.phrase,
        duration_ms=pick.duration_ms,
    )
    recent.append(pick.phrase_hash)
    await play_cached(pa, cast(int, indices.output_index), Path(pick.path))
