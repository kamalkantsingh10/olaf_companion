"""Story 6.3 integration test — emphasis events end-to-end.

Drives the real ``_stream_and_speak`` runtime with a real ``Segmenter`` +
state machine, a scripted fake Talker, and a fake Cartesia client whose
``synthesize`` yields chunks and whose ``last_segment_timing`` returns the
per-word ``Timestamps`` Cartesia would have reported. PyAudio + FSM are
mocked; the publisher is the in-memory :class:`LogEventPublisher` so the
emitted ``emphasis`` vocalization events can be inspected.

Contract:

- The Talker emits ``I'm *really* glad to *see* you.`` (two emphasis marks).
- After playback, exactly **two** ``vocalization(tag="emphasis")`` events
  are published — one for ``really`` (word index 1), one for ``see`` (word
  index 4).
- Each event's ``audio_frame_id`` carries the carrier word's ``start_ms``
  from the scripted timing.
- No emphasis events for the non-marked words.
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from voice_agent_pipeline.audio.cached import CachedAudioManifest
from voice_agent_pipeline.config.expression_map import (
    ExpressionMapConfig,
    FallbackFamily,
    UnknownEntry,
    VocalizationEntry,
)
from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher
from voice_agent_pipeline.schemas.vocalization_event import VocalizationEvent
from voice_agent_pipeline.sequential_loop import _stream_and_speak
from voice_agent_pipeline.splitter.mapping import LastPublishedCache
from voice_agent_pipeline.splitter.segmenter import Segmenter
from voice_agent_pipeline.tts.cartesia import SegmentTiming, Word
from voice_agent_pipeline.turn.talker import TalkerStreamEnd, TalkerTextDelta


def _expression_mapping() -> ExpressionMapConfig:
    return ExpressionMapConfig(
        schema_version=3,
        emotions=["neutral", "content", "excited", "happy"],
        vocalizations={"emphasis": VocalizationEntry(tts_supported=False)},
        fallback_families={
            "neutral": FallbackFamily(members=["calm"], maps_to="neutral"),
        },
        unknown=UnknownEntry(maps_to="neutral"),
    )


def _empty_manifest() -> CachedAudioManifest:
    """No opener entries — this turn emits no opener tag, so none are needed."""
    from datetime import UTC, datetime

    return CachedAudioManifest(
        schema_version=2,
        generated_at=datetime.now(tz=UTC),
        voice_id="v",
        tts_model="m",
        entries=[],
    )


class _FakeTalker:
    """Streams a scripted reply with two emphasis marks across one sentence."""

    async def complete_with_tools_streaming(
        self,
        _prompt: str,
        _tool_registry: Any,
        history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[Any]:
        # Split mid-mark across deltas to exercise cross-stream parsing.
        yield TalkerTextDelta(text="I'm *real")
        yield TalkerTextDelta(text="ly* glad to *see* you.")
        yield TalkerStreamEnd(tool_calls=[])


@pytest.mark.asyncio
async def test_emphasis_events_published_with_word_anchors() -> None:
    """Two marks → two anchored emphasis events; none for unmarked words."""
    # Scripted Cartesia word timing for "I'm really glad to see you."
    # (start_ms / end_ms rounded as the WS path would store them).
    timing = SegmentTiming(
        words=[
            Word(text="I'm", start_ms=0, end_ms=180),
            Word(text="really", start_ms=180, end_ms=560),
            Word(text="glad", start_ms=560, end_ms=880),
            Word(text="to", start_ms=880, end_ms=990),
            Word(text="see", start_ms=990, end_ms=1180),
            Word(text="you.", start_ms=1180, end_ms=1400),
        ]
    )

    async def _fake_synthesize(_text: str) -> AsyncIterator[bytes]:
        for chunk in [b"\x00" * 16, b"\x00" * 16]:
            yield chunk

    fake_tts = MagicMock()
    fake_tts.synthesize = _fake_synthesize
    fake_tts.last_segment_timing = MagicMock(return_value=timing)

    fake_stream = MagicMock()
    fake_stream.write = MagicMock()
    fake_pa = MagicMock()
    fake_pa.open = MagicMock(return_value=fake_stream)

    fake_fsm = MagicMock()
    fake_fsm.on_first_audio_frame = AsyncMock()

    publisher = LogEventPublisher()
    segmenter = Segmenter(_expression_mapping())
    emotion_cache = LastPublishedCache()

    opener_selected = asyncio.Event()
    opener_already_playing = asyncio.Event()

    async def _noop_fallback() -> None:
        return

    opener_fallback_task = asyncio.create_task(_noop_fallback())

    await _stream_and_speak(
        pa=fake_pa,
        indices=MagicMock(output_index=0),
        tts=fake_tts,
        talker=_FakeTalker(),
        tool_registry=MagicMock(),
        prompt="hello",
        fsm=fake_fsm,
        manifest=_empty_manifest(),
        opener_selected=opener_selected,
        opener_already_playing=opener_already_playing,
        opener_fallback_task=opener_fallback_task,
        recent_openers=deque(maxlen=1),
        publisher=publisher,
        segmenter=segmenter,
        emotion_cache=emotion_cache,
        error_filler_enabled=False,
        error_filler_bucket="thinking",
    )

    emphasis_events = [
        e
        for topic, e in publisher.published
        if topic == "vocalization"
        and isinstance(e, VocalizationEvent)
        and e.payload.tag == "emphasis"
    ]
    # Exactly TWO emphasis events — one per marked word.
    assert len(emphasis_events) == 2, (
        f"expected 2 emphasis events, got {len(emphasis_events)}: "
        f"{[e.payload.audio_frame_id for e in emphasis_events]}"
    )
    frame_ids = [e.payload.audio_frame_id for e in emphasis_events]
    # "really" is word index 1 (start_ms 180); "see" is word index 4
    # (start_ms 990). Both in the single spoken segment (seg index 0).
    assert frame_ids == ["seg-0-w-180", "seg-0-w-990"]
    # Every emphasis event is a non-TTS gesture cue.
    assert all(e.payload.tts_supported is False for e in emphasis_events)


# ─────────── Story 6.5: emphasis join fires with Gemini's approximate timing ───────────


def test_gemini_emphasis_fires_with_approximate_timing() -> None:
    """The provider-agnostic emphasis join publishes one event per marked word
    when driven by ``GeminiClient`` (approximate, no real timestamps).

    Story 6.5: Gemini returns no word timestamps, so ``last_segment_timing()``
    is an even-distribution estimate. ``_publish_emphasis_events`` must still
    emit exactly one ``vocalization(tag="emphasis")`` per marked index, with a
    plausible ``seg-{i}-w-{ms}`` anchor — "around the word is good enough".
    """
    from types import SimpleNamespace
    from uuid import uuid4

    from pydantic import SecretStr

    from voice_agent_pipeline.config.setup import TtsConfig
    from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher
    from voice_agent_pipeline.sequential_loop import _publish_emphasis_events
    from voice_agent_pipeline.splitter.segmenter import Segment
    from voice_agent_pipeline.tts.gemini import GeminiClient

    # Fake Live session: two 24 kHz audio chunks then turn_complete.
    chunk_24k = b"\x10\x00" * 2400  # 2400 samples = 100 ms @ 24 kHz

    def _audio_resp() -> object:
        part = SimpleNamespace(inline_data=SimpleNamespace(data=chunk_24k))
        model_turn = SimpleNamespace(parts=[part])
        return SimpleNamespace(
            server_content=SimpleNamespace(model_turn=model_turn, turn_complete=False),
        )

    def _done_resp() -> object:
        return SimpleNamespace(
            server_content=SimpleNamespace(model_turn=None, turn_complete=True),
        )

    class _Session:
        async def send_client_content(self, *, turns: object, turn_complete: bool) -> None:
            pass

        async def receive(self) -> object:
            for r in (_audio_resp(), _audio_resp(), _done_resp()):
                yield r

    class _ConnectCM:
        async def __aenter__(self) -> "_Session":
            return _Session()

        async def __aexit__(self, *_: object) -> bool:
            return False

    def _connect(*, model: str, config: object) -> _ConnectCM:
        return _ConnectCM()

    client = GeminiClient(TtsConfig(provider="gemini", voice_id="x"), SecretStr("fake"))
    client._client = SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=_connect)))  # type: ignore[assignment]

    async def _run() -> list[VocalizationEvent]:
        text = "alpha beta gamma delta"  # 4 words
        async for _ in client.synthesize(text):
            pass
        timing = client.last_segment_timing()
        assert timing is not None
        assert len(timing.words) == 4

        publisher = LogEventPublisher()
        segment = Segment(
            text=text,
            speech_emotion_payload=None,
            vocalization_payloads=[],
            emphasis_word_indices=[0, 2],  # two marks
        )
        published = await _publish_emphasis_events(publisher, client, segment, uuid4(), 3)
        assert published == 2
        return [
            cast(VocalizationEvent, e)
            for topic, e in publisher.published
            if topic == "vocalization"
        ]

    events = asyncio.run(_run())
    assert len(events) == 2
    for e in events:
        assert e.payload.tag == "emphasis"
        assert e.payload.audio_frame_id is not None
        assert e.payload.audio_frame_id.startswith("seg-3-w-")
