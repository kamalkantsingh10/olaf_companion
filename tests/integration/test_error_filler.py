"""Integration test — transient-error filler over retry backoff (2026-05-31).

The openai SDK retries 429 / 5xx responses internally, so a single
``complete_with_tools_streaming`` call can sit silent for seconds during
the backoff (a ~9 s Groq 429 stall is what prompted this feature). The
Talker's httpx response hook sets ``transient_error_event`` on each such
response; ``_stream_and_speak`` runs a watcher that plays a cached take
from ``error_filler_bucket`` so the user hears "still working on it"
instead of dead air.

What's tested
-------------

1. **Enabled path**: a fake Talker that flags a transient error and then
   stalls before yielding any text → the watcher plays a filler from the
   ``thinking`` bucket BEFORE the real answer starts, and the real answer
   still synthesizes afterwards.
2. **Disabled path**: with ``error_filler_enabled=False`` the same
   transient-error signal produces NO filler playback (bare silence — the
   pre-2026-05-31 behavior).

Mocking honors CLAUDE.md rule #7 — ``play_cached`` (the audio-device
boundary, same seam the Story 6.2 overlap test patches) and Cartesia's
``synthesize`` are mocked; the Talker is a hand-written fake at the
``complete_with_tools_streaming`` Protocol-shaped seam.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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


class _ErrorThenReplyTalker:
    """Fake Talker that flags a transient error, stalls, then replies.

    Mirrors the production shape: the httpx hook would set
    ``transient_error_event`` while the SDK retries; here we set it
    directly and ``await`` a short "backoff" so the concurrently-running
    watcher gets a window to play its filler before any text arrives.
    """

    def __init__(self, backoff_s: float = 0.05) -> None:
        self.transient_error_event = asyncio.Event()
        self._backoff_s = backoff_s

    async def complete_with_tools_streaming(
        self,
        _prompt: str,
        _tool_registry: Any,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[Any]:
        # Simulate a 429 the SDK is retrying: raise the signal, then
        # "back off" (yielding control so the watcher runs) before the
        # real answer streams.
        self.transient_error_event.set()
        await asyncio.sleep(self._backoff_s)
        yield TalkerTextDelta(text="Here is the answer.")
        yield TalkerStreamEnd(tool_calls=[])


def _build_turn_scaffolding() -> dict[str, Any]:
    """Shared fakes for both directions of the contract."""

    async def _fake_synthesize(_text: str) -> AsyncIterator[bytes]:
        for chunk in [b"\x00" * 16, b"\x00" * 16]:
            yield chunk

    fake_tts = MagicMock()
    fake_tts.synthesize = _fake_synthesize

    fake_stream = MagicMock()
    fake_stream.write = MagicMock()
    fake_pa = MagicMock()
    fake_pa.open = MagicMock(return_value=fake_stream)

    fake_fsm = MagicMock()
    fake_fsm.on_first_audio_frame = AsyncMock()

    return {
        "pa": fake_pa,
        "tts": fake_tts,
        "fsm": fake_fsm,
        "segmenter": Segmenter(_expression_mapping()),
        "emotion_cache": LastPublishedCache(),
    }


@pytest.mark.asyncio
async def test_error_filler_fires_on_transient_error_before_real_audio() -> None:
    """A transient-error signal plays a thinking-bucket filler over the stall."""
    from collections import deque

    played_paths: list[str] = []

    async def _record_play_cached(*args: object, **_kwargs: object) -> None:
        # play_cached(pa, output_index, Path(pick.path)) — the 3rd
        # positional is the WAV path. Record it so we can assert the
        # filler drew from the error_filler_bucket.
        played_paths.append(str(args[2]))

    scaffolding = _build_turn_scaffolding()
    talker = _ErrorThenReplyTalker()

    with patch(
        "voice_agent_pipeline.sequential_loop.play_cached",
        side_effect=_record_play_cached,
    ):
        full_text, tool_calls = await _stream_and_speak(
            pa=scaffolding["pa"],
            indices=MagicMock(output_index=0),
            tts=scaffolding["tts"],
            talker=talker,
            tool_registry=MagicMock(),
            prompt="why are you slow?",
            fsm=scaffolding["fsm"],
            manifest=_opener_manifest(),
            opener_selected=asyncio.Event(),
            opener_already_playing=asyncio.Event(),
            opener_fallback_task=None,
            recent_openers=deque(maxlen=1),
            publisher=MagicMock(),
            segmenter=scaffolding["segmenter"],
            emotion_cache=scaffolding["emotion_cache"],
            error_filler_enabled=True,
            error_filler_bucket="thinking",
        )

    # The filler fired, drawing from the thinking bucket.
    assert "/tmp/thinking.wav" in played_paths  # noqa: S108
    # ...and the real answer still made it through.
    assert full_text == "Here is the answer."
    assert tool_calls == []


@pytest.mark.asyncio
async def test_no_filler_when_error_filler_disabled() -> None:
    """With the filler disabled, a transient error produces no playback."""
    from collections import deque

    played_paths: list[str] = []

    async def _record_play_cached(*args: object, **_kwargs: object) -> None:
        played_paths.append(str(args[2]))

    scaffolding = _build_turn_scaffolding()
    talker = _ErrorThenReplyTalker()

    with patch(
        "voice_agent_pipeline.sequential_loop.play_cached",
        side_effect=_record_play_cached,
    ):
        full_text, _tool_calls = await _stream_and_speak(
            pa=scaffolding["pa"],
            indices=MagicMock(output_index=0),
            tts=scaffolding["tts"],
            talker=talker,
            tool_registry=MagicMock(),
            prompt="why are you slow?",
            fsm=scaffolding["fsm"],
            manifest=_opener_manifest(),
            opener_selected=asyncio.Event(),
            opener_already_playing=asyncio.Event(),
            opener_fallback_task=None,
            recent_openers=deque(maxlen=1),
            publisher=MagicMock(),
            segmenter=scaffolding["segmenter"],
            emotion_cache=scaffolding["emotion_cache"],
            error_filler_enabled=False,
            error_filler_bucket="thinking",
        )

    # No filler — the disabled path leaves the retry backoff silent.
    assert played_paths == []
    # The real answer is unaffected.
    assert full_text == "Here is the answer."


class _EmptyReplyTalker:
    """Fake Talker that returns a fully-empty reply: no text, no tool call.

    This is the gpt-oss reasoning-overflow failure mode that made the
    bot "stop guessing and go silent" — the model streamed nothing.
    """

    def __init__(self) -> None:
        self.transient_error_event = asyncio.Event()

    async def complete_with_tools_streaming(
        self,
        _prompt: str,
        _tool_registry: Any,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[Any]:
        # No text deltas at all; just the end-of-stream with no tool calls.
        yield TalkerStreamEnd(tool_calls=[])


@pytest.mark.asyncio
async def test_empty_reply_plays_filler_instead_of_silence() -> None:
    """A fully-empty LLM reply plays a filler rather than going silent."""
    from collections import deque

    played_paths: list[str] = []

    async def _record_play_cached(*args: object, **_kwargs: object) -> None:
        played_paths.append(str(args[2]))

    scaffolding = _build_turn_scaffolding()

    with patch(
        "voice_agent_pipeline.sequential_loop.play_cached",
        side_effect=_record_play_cached,
    ):
        full_text, tool_calls = await _stream_and_speak(
            pa=scaffolding["pa"],
            indices=MagicMock(output_index=0),
            tts=scaffolding["tts"],
            talker=_EmptyReplyTalker(),
            tool_registry=MagicMock(),
            prompt="next guess?",
            fsm=scaffolding["fsm"],
            manifest=_opener_manifest(),
            opener_selected=asyncio.Event(),
            opener_already_playing=asyncio.Event(),
            opener_fallback_task=None,
            recent_openers=deque(maxlen=1),
            publisher=MagicMock(),
            segmenter=scaffolding["segmenter"],
            emotion_cache=scaffolding["emotion_cache"],
            error_filler_enabled=True,
            error_filler_bucket="thinking",
        )

    # The empty reply triggered a thinking-bucket filler (no dead air).
    assert "/tmp/thinking.wav" in played_paths  # noqa: S108
    # The reply really was empty (caller drops it from history — the
    # spiral fix — but _stream_and_speak just surfaces the emptiness).
    assert full_text == ""
    assert tool_calls == []


@pytest.mark.asyncio
async def test_empty_reply_silent_when_filler_disabled() -> None:
    """With the filler off, an empty reply is silent (pre-2026-05-31 behavior)."""
    from collections import deque

    played_paths: list[str] = []

    async def _record_play_cached(*args: object, **_kwargs: object) -> None:
        played_paths.append(str(args[2]))

    scaffolding = _build_turn_scaffolding()

    with patch(
        "voice_agent_pipeline.sequential_loop.play_cached",
        side_effect=_record_play_cached,
    ):
        full_text, _tool_calls = await _stream_and_speak(
            pa=scaffolding["pa"],
            indices=MagicMock(output_index=0),
            tts=scaffolding["tts"],
            talker=_EmptyReplyTalker(),
            tool_registry=MagicMock(),
            prompt="next guess?",
            fsm=scaffolding["fsm"],
            manifest=_opener_manifest(),
            opener_selected=asyncio.Event(),
            opener_already_playing=asyncio.Event(),
            opener_fallback_task=None,
            recent_openers=deque(maxlen=1),
            publisher=MagicMock(),
            segmenter=scaffolding["segmenter"],
            emotion_cache=scaffolding["emotion_cache"],
            error_filler_enabled=False,
            error_filler_bucket="thinking",
        )

    assert played_paths == []
    assert full_text == ""
