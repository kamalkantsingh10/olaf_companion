"""Story 6.2 integration test — Cartesia overlap timing contract.

The headline win of Story 6.2 is **deleting the serialization tax**
DR-001 surfaced: the v1 runtime awaited `filler_task` before opening
its output stream, blocking the Cartesia network call on opener
playback (~1s tax on ~75% of turns per DR-001's log analysis). Story
6.2 removed that await; this test asserts the contract holds.

What's tested
-------------

A single end-to-end contract on `_stream_and_speak`:

1. Build a fake Talker that streams ``<opener bucket="thinking"/>
   Hello there.``.
2. Mock Cartesia's `synthesize` to record `time.monotonic()` at
   first call AND return a one-chunk bytes iterator.
3. Mock `play_cached` (the opener playback) to take a controllable
   200 ms, recording its own `time.monotonic()` at both start and
   end.
4. Mock PyAudio's `stream.write` so we don't try to actually play.
5. Assert `synthesize_call_time < play_cached_end_time` — the
   Cartesia call must fire **before** the opener finishes playing.

If a future refactor reintroduces an `await opener_task` in the
real-answer path, this assertion catches it as a regression.

What's NOT tested here
----------------------

- The full PyAudio device serialisation that handles the audible
  ordering on the speaker — that needs real audio hardware and lives
  in Story 6.4's soak.
- NFR33 (opener onset ≤ 700 ms p95) — covered by the
  ``trigger_opener_fallback`` unit tests and Story 6.4 soak.
- NFR34 (dead-air-after-opener ≤ 250 ms p95) — soak-only; requires
  real audio.
- The opener_path "Cartesia NEVER called for the opener phrase"
  assertion — already covered by the unit tests on the splitter
  callback + ``pick_opener`` + the segmenter's tag-strip behavior.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voice_agent_pipeline.audio.cached import CachedAudioEntry, CachedAudioManifest
from voice_agent_pipeline.audio.opener_bucket import OpenerBucket
from voice_agent_pipeline.config.expression_map import (
    ExpressionMapConfig,
    FallbackFamily,
    UnknownEntry,
)
from voice_agent_pipeline.sequential_loop import _stream_and_speak
from voice_agent_pipeline.splitter.mapping import LastPublishedCache
from voice_agent_pipeline.splitter.segmenter import Segmenter
from voice_agent_pipeline.turn.talker import TalkerStreamEnd, TalkerTextDelta


def _expression_mapping() -> ExpressionMapConfig:
    return ExpressionMapConfig(
        schema_version=3,
        emotions=["neutral", "content", "excited", "happy"],
        vocalizations={},
        fallback_families={
            "neutral": FallbackFamily(members=["calm"], maps_to="neutral"),
        },
        unknown=UnknownEntry(maps_to="neutral"),
    )


def _opener_manifest() -> CachedAudioManifest:
    """Manifest with one entry per OpenerBucket so pick_opener succeeds."""
    from datetime import UTC, datetime

    buckets: list[OpenerBucket] = [
        "thinking",
        "acknowledge",
        "look_up",
        "delegate",
        "react",
    ]
    return CachedAudioManifest(
        schema_version=2,
        generated_at=datetime.now(tz=UTC),
        voice_id="v",
        tts_model="m",
        entries=[
            CachedAudioEntry(
                surface="opener",
                mood=None,
                bucket=b,
                phrase_hash=f"h_{b}",
                phrase=f"phrase for {b}",
                path=f"/tmp/{b}.wav",  # noqa: S108
                duration_ms=500,
            )
            for b in buckets
        ],
    )


class _FakeTalker:
    """Streams a scripted Talker reply: opener tag + plain sentence."""

    async def complete_with_tools_streaming(
        self,
        _prompt: str,
        _tool_registry: Any,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[Any]:
        # Emit the opener tag first, then a sentence. This is exactly
        # the shape Story 6.2's Talker prompt teaches.
        yield TalkerTextDelta(text='<opener bucket="thinking"/> ')
        yield TalkerTextDelta(text="Hello there friend.")
        yield TalkerStreamEnd(tool_calls=[])


@pytest.mark.asyncio
async def test_cartesia_synthesize_fires_before_opener_playback_completes() -> None:
    """The Cartesia network call must NOT wait for opener playback.

    This is the core Story 6.2 overlap-deletion contract. The v1
    runtime did ``audio_started.set() + await filler_task`` before
    opening its output stream — the test catches a regression where
    a future change reintroduces a serialising await.
    """
    # Timing recorders.
    synth_started_at: list[float] = []
    play_started_at: list[float] = []
    play_ended_at: list[float] = []

    # Mock Cartesia: record call time, return a tiny bytes iterator.
    async def _fake_synthesize(_text: str) -> AsyncIterator[bytes]:
        synth_started_at.append(time.monotonic())
        for chunk in [b"\x00" * 16, b"\x00" * 16]:
            yield chunk

    fake_tts = MagicMock()
    fake_tts.synthesize = _fake_synthesize

    # Mock play_cached: take a controllable 150 ms (simulates a real
    # opener WAV playing). Start + end are recorded so the assertion
    # can compare against synth_started_at.
    async def _slow_play_cached(*_args: object, **_kwargs: object) -> None:
        play_started_at.append(time.monotonic())
        # 150 ms is long enough to exceed both the timer fallback and
        # any plausible scheduler jitter; well shorter than a real
        # opener but enough to make the contract assertion meaningful.
        await asyncio.sleep(0.15)
        play_ended_at.append(time.monotonic())

    # Mock PyAudio's stream so we don't try to actually play audio.
    fake_stream = MagicMock()
    fake_stream.write = MagicMock()
    fake_pa = MagicMock()
    fake_pa.open = MagicMock(return_value=fake_stream)

    # Mock FSM transitions — we don't care about their internals.
    fake_fsm = MagicMock()
    fake_fsm.on_first_audio_frame = AsyncMock()

    # Real Segmenter (we want the real opener-callback wiring).
    segmenter = Segmenter(_expression_mapping())
    emotion_cache = LastPublishedCache()

    # Build the events the runtime would build per turn.
    opener_selected = asyncio.Event()
    opener_already_playing = asyncio.Event()

    # The opener fallback task — we don't care if it fires (the
    # splitter callback will set opener_selected first); we just need
    # SOMETHING awaitable to pass as the parameter.
    async def _noop_fallback() -> None:
        try:
            await asyncio.wait_for(opener_selected.wait(), timeout=2.0)
        except TimeoutError:
            pass

    opener_fallback_task = asyncio.create_task(_noop_fallback())

    # Mock publisher — we only need the publish methods to exist.
    fake_publisher = MagicMock()
    fake_publisher.publish_speech_emotion = AsyncMock()
    fake_publisher.publish_vocalization = AsyncMock()

    # Patch play_cached at the import site inside sequential_loop —
    # the runtime calls it as `play_cached(...)` after the
    # `from voice_agent_pipeline.audio.cached import ... play_cached`
    # import, so we patch THAT binding.
    from collections import deque

    with patch(
        "voice_agent_pipeline.sequential_loop.play_cached",
        side_effect=_slow_play_cached,
    ):
        await _stream_and_speak(
            pa=fake_pa,
            indices=MagicMock(output_index=0),
            tts=fake_tts,
            talker=_FakeTalker(),
            tool_registry=MagicMock(),
            prompt="hello",
            fsm=fake_fsm,
            manifest=_opener_manifest(),
            opener_selected=opener_selected,
            opener_already_playing=opener_already_playing,
            opener_fallback_task=opener_fallback_task,
            recent_openers=deque(maxlen=1),
            publisher=fake_publisher,
            segmenter=segmenter,
            emotion_cache=emotion_cache,
        )

    # Sanity: both paths actually ran.
    assert synth_started_at, "Cartesia.synthesize must have been called"
    assert play_started_at, "opener play_cached must have been called"
    assert play_ended_at, "opener play_cached must have completed"

    # The headline contract: Cartesia fires BEFORE opener playback
    # finishes. If the v1 serialization tax were reintroduced,
    # `synth_started_at[0]` would be >= `play_ended_at[0]`.
    synth_at = synth_started_at[0]
    play_end_at = play_ended_at[0]
    assert synth_at < play_end_at, (
        f"Cartesia.synthesize fired at {synth_at:.3f}s but opener playback "
        f"didn't finish until {play_end_at:.3f}s — the Story 5.5 "
        f"serialization tax appears to have been reintroduced "
        f"(delta {(synth_at - play_end_at) * 1000:.1f} ms past play end)."
    )

    # Tight bound: Cartesia should fire within ~100 ms of opener start —
    # this is the spec's "synthesize call within ≤ 100 ms of splitter
    # buffering first non-tag text segment" assertion (AC #8). In a
    # well-behaved scheduler, it fires within a few ms.
    play_at = play_started_at[0]
    assert synth_at - play_at < 0.1, (
        f"Cartesia.synthesize fired {(synth_at - play_at) * 1000:.1f} ms "
        f"after opener playback started — should be ≤ 100 ms; "
        f"larger means something serialised."
    )
