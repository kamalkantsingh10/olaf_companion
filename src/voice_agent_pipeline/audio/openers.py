"""Cached opener system — function-bucketed pick + timer fallback (Story 6.2).

Supersedes Story 5.5's `audio/filler.py` mood-keyed filler design with
function-bucketed openers (see DR-001 "Decision (frozen)"). The shape-
shift is **function buckets, not mood buckets** — the Talker LLM
knows the question AND its own answer (incl. tool-call shape), so a
function-bucketed selection (`thinking`, `acknowledge`, `look_up`,
`delegate`, `react`) is strictly more accurate than v1's random
mood-keyed filler.

Two pieces:

- :func:`pick_opener` — pure selection logic (no IO). Given a manifest,
  a function bucket, and a small ring buffer of recently-played opener
  hashes, returns the next :class:`CachedAudioEntry` to play. **No
  mood fallback** (unlike `pick_filler`) — if the requested bucket is
  empty (which `OpenersConfig`'s validator prevents in production),
  return `None` and the caller stays silent for this turn.
- :func:`trigger_opener_fallback` — async orchestration. Sleeps up to
  ``timer_fallback_ms``; if the splitter sets ``opener_selected``
  before the threshold (LLM emitted an `<opener bucket="..."/>` tag),
  returns without playing. Otherwise picks from the
  ``timer_fallback_bucket`` and plays it. The ``opener_already_playing``
  event is a race-window check right before the timer fires so the
  fallback doesn't double-fire with a splitter-driven opener that
  just started.

Designed to be spawned as a background task on VAD end-of-speech by
``sequential_loop``. **Unlike Story 5.5's `maybe_play_filler`, the
real-answer path does NOT `await` this task before opening its
output stream** — the Cartesia overlap (Story 6.2 AC #7, deleting
the serialization tax at `sequential_loop.py:701`) means the
network call and the opener playback proceed in parallel; PyAudio's
device-level serialization handles the playback ordering on the
speaker.

Why a dedicated module rather than inside sequential_loop:

- The pure pick logic deserves its own tests (last-N suppression,
  empty-bucket handling, exclusion-empties-then-resets) — keeping it
  here gives those tests a clean import target.
- `OpenerBucket` is the canonical Literal alias other modules
  (`config/setup.py`'s `OpenersConfig`, `audio/cached.py`'s
  `CachedAudioEntry.bucket`) import from here.
- Story 5.5's `audio/filler.py` (now deleted) was the precise
  template — this module mirrors its shape with the function-bucket
  change.
"""

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
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

# Re-export the Literal from the leaf module so spec-compliant call
# sites (`from voice_agent_pipeline.audio.openers import OpenerBucket`)
# keep working. See ``audio/opener_bucket.py`` for the rationale.
from voice_agent_pipeline.audio.opener_bucket import OpenerBucket

__all__ = ["OpenerBucket", "OpenerPlayback", "pick_opener", "trigger_opener_fallback"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class OpenerPlayback:
    """Timing record for a fallback opener that actually played (Story 6.4).

    :func:`trigger_opener_fallback` returns this when the timer fires and a
    cached opener is played, so the runtime can fold the timing into its
    per-turn ``turn.complete`` rollup without ``audio/openers`` needing to
    import the runtime's private ``_TurnTimings`` (which would be a layering
    inversion). Returns ``None`` instead when the fallback does NOT play
    (splitter-driven opener won, or the race-window check bailed).

    All ``_ns`` values are :func:`time.time_ns` readings on the single
    runtime clock (same methodology as ``tts.first_frame.ttfb_ms``).
    """

    bucket: OpenerBucket
    duration_ms: int
    first_frame_ns: int
    last_frame_ns: int


def pick_opener(
    manifest: CachedAudioManifest,
    bucket: OpenerBucket,
    recent: deque[str],
) -> CachedAudioEntry | None:
    """Pick the next opener entry for the requested function bucket.

    Behavior (mirrors `pick_filler` *without* the mood fallback):

    1. Find the opener bucket entries matching ``bucket``. If empty,
       return ``None`` — the caller should treat this as "no opener
       available for this turn" and stay silent. Production prevents
       this via :class:`OpenersConfig`'s model_validator (every
       :data:`OpenerBucket` Literal value carries ≥ 1 phrase).
    2. Exclude any entry whose ``phrase_hash`` is in ``recent``. If
       exclusion empties the candidate list, reset ``recent`` and
       re-try (so short-term variety is preserved on average without
       starving the picker when the bucket is small).
    3. ``random.choice`` over the surviving candidates.

    Args:
        manifest: The loaded cached-audio manifest.
        bucket: The function bucket to pick from. Story 6.2's prompt
            teaches the Talker which bucket to emit per turn shape.
        recent: Ring buffer of recently-played opener phrase_hashes.
            Maintained by the caller; ``maxlen`` should be
            ``max_consecutive_repeat + 1`` (the most-recently-played
            opener is always excluded; longer windows exclude further
            back).

    Returns:
        The chosen :class:`CachedAudioEntry`, or ``None`` if the
        bucket is empty (validator should prevent this in
        production).
    """
    in_bucket = [e for e in manifest.phrases_for_surface("opener") if e.bucket == bucket]
    if not in_bucket:
        return None

    candidates = [e for e in in_bucket if e.phrase_hash not in recent]
    if not candidates:
        # Exclusion emptied the bucket — reset history and pick from
        # the full bucket. Same documented behavior as
        # :func:`pick_filler`: short-term variety preserved on
        # average; only repeat the previous opener when there's
        # literally nothing else to pick.
        recent.clear()
        candidates = in_bucket

    # random.choice on a non-empty list — S311 (bandit weak-PRNG) is
    # fine here; this isn't security-sensitive.
    return random.choice(candidates)  # noqa: S311


async def trigger_opener_fallback(
    pa: pyaudio.PyAudio,
    indices: AudioDeviceIndices,
    manifest: CachedAudioManifest,
    timer_fallback_ms: int,
    timer_fallback_bucket: OpenerBucket,
    opener_selected: asyncio.Event,
    opener_already_playing: asyncio.Event,
    recent: deque[str],
) -> OpenerPlayback | None:
    """Wait up to ``timer_fallback_ms`` for an LLM-tag-selected opener.

    Coroutine spawned as a background task on VAD end-of-speech.
    Completes when any of the following:

    - The splitter sets ``opener_selected`` before ``timer_fallback_ms``
      expires → return without playing (the splitter-side path is
      playing its own LLM-tag-selected opener).
    - The threshold expires AND no LLM tag arrived AND
      ``opener_already_playing`` is still unset → pick from
      ``timer_fallback_bucket`` and play it.
    - ``opener_already_playing`` is set during the wait → return (race
      window — splitter beat the timer by a microsecond).

    NO mood signal at all — the timer fallback is function-bucketed
    (default ``acknowledge`` per :class:`OpenersConfig`). Mood-driven
    selection lives in the greeting path; openers are about question
    shape, not affect.

    Args:
        pa: Live PyAudio instance (owned by ``run_sequential_loop``).
        indices: Resolved audio devices. ``output_index`` must be set.
        manifest: Loaded cached-audio manifest.
        timer_fallback_ms: Threshold for firing the fallback opener.
            From ``config.openers.timer_fallback_ms``. NFR33 caps
            opener onset; this value defaults to 700 ms.
        timer_fallback_bucket: Which bucket to pick from when the
            timer fires. From ``config.openers.timer_fallback_bucket``
            (default ``"acknowledge"`` — operator-curated generic-safe
            choice).
        opener_selected: :class:`asyncio.Event` set by the splitter
            when it sees an ``<opener bucket="..."/>`` tag. Cancels
            the timer-fallback path.
        opener_already_playing: :class:`asyncio.Event` set by the
            splitter-driven opener path once playback starts. Acts as
            the race-window check right before the timer fires.
        recent: Mutable ring buffer of recently-played opener hashes.
            Updated in-place if a fallback opener is played.

    Returns:
        An :class:`OpenerPlayback` timing record when the timer fired and
        a fallback opener was played; ``None`` when it didn't play (the
        splitter-driven opener won, the race-window check bailed, or the
        bucket was empty). The runtime uses the non-``None`` case to
        populate the ``opener_*`` fields of ``turn.complete`` (Story 6.4).
    """
    try:
        # Cheap "LLM tag arrived first" path: wait_for with a timeout.
        # If ``opener_selected`` fires before ``timer_fallback_ms``,
        # this returns normally and we exit without playing.
        await asyncio.wait_for(
            opener_selected.wait(),
            timeout=timer_fallback_ms / 1000,
        )
        return None
    except TimeoutError:
        # Threshold expired without an LLM tag — proceed to the
        # fallback path. Fall through.
        pass

    # Race-window check: between the wait_for timeout firing and us
    # actually picking + playing, the splitter callback might have just
    # set ``opener_already_playing``. If so, abandon the fallback so we
    # don't double-fire.
    if opener_already_playing.is_set():
        return None

    pick = pick_opener(manifest, timer_fallback_bucket, recent)
    if pick is None:
        # No opener available for the fallback bucket. The
        # :class:`OpenersConfig` validator should prevent this in
        # production; defensive log + silent return otherwise.
        log.warning("opener.no_pick", bucket=timer_fallback_bucket)
        return None

    log.info(
        "opener.timer_fallback_picked",
        bucket=timer_fallback_bucket,
        phrase=pick.phrase,
        duration_ms=pick.duration_ms,
    )
    # Claim the playing slot BEFORE actually opening the stream — the
    # splitter callback's check on this same event in
    # ``opener_already_playing`` prevents stacking if the LLM tag
    # arrives at the exact moment the timer fires.
    opener_already_playing.set()
    recent.append(pick.phrase_hash)
    # Story 6.4: bracket the playback with single-clock readings so the
    # runtime can derive opener_onset_ms + dead_air_after_opener_ms.
    first_frame_ns = time.time_ns()
    await play_cached(pa, cast(int, indices.output_index), Path(pick.path))
    return OpenerPlayback(
        bucket=timer_fallback_bucket,
        duration_ms=pick.duration_ms,
        first_frame_ns=first_frame_ns,
        last_frame_ns=time.time_ns(),
    )
