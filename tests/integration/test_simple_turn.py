"""Integration test for Journey 1 (PRD): the simple-turn loop.

Drives 30 simulated turns through the post-STT pipeline chain
(SttProcessor → TurnDispatchProcessor → SegmenterProcessor →
CartesiaSynthesisProcessor) with mocks at the three external-service
Protocol seams (STT, Talker, TTS). Measures end-of-speech → first
``OutputAudioRawFrame`` and reports p50/p95/max as the NFR1 baseline.

Story 3.7 added the SegmenterProcessor stage between the dispatcher
and the synthesizer; the integration test was updated to insert it
in the chain.

Why post-STT, not full pipeline: the wake-word + VAD + audio transport
stages need real audio hardware (or a substantially mocked Pipecat
runner) which adds noise to the latency measurement without testing
anything new. The architectural integration question — "does
TranscriptFrame correctly flow through dispatcher → Cartesia and
produce audio frames downstream?" — is answered exactly by this
chain. The pre-STT half is covered by Story 1.6/1.7's existing tests.

Privacy invariants (NFR25 / FR39): the test scans all captured
structlog records and asserts:
- No ``stt.transcript`` INFO record carries ``transcript`` or ``text``.
- No record at any level carries forbidden field names
  (``audio_bytes``, ``audio_data``, ``pcm``).
"""

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pipecat.frames.frames import Frame, OutputAudioRawFrame

from voice_agent_pipeline.audio.vad import UtteranceCapturedFrame
from voice_agent_pipeline.config.expression_map import (
    EmotionEntry,
    ExpressionMapConfig,
    FallbackFamily,
    UnknownEntry,
    VocalizationEntry,
)
from voice_agent_pipeline.config.setup import SttConfig
from voice_agent_pipeline.pipeline import (
    CartesiaSynthesisProcessor,
    SegmenterProcessor,
    SttProcessor,
    TurnDispatchProcessor,
    _SttResultLogger,
)
from voice_agent_pipeline.splitter.mapping import LastPublishedCache
from voice_agent_pipeline.splitter.segmenter import Segmenter
from voice_agent_pipeline.stt.backend import TranscriptionResult
from voice_agent_pipeline.turn.router import TurnRouter


def _make_mapping() -> ExpressionMapConfig:
    """Minimal ExpressionMapConfig for the integration chain."""
    return ExpressionMapConfig(
        schema_version=2,
        emotions={
            "neutral": EmotionEntry(expression_data={"led_color": "#fff"}),
            "content": EmotionEntry(expression_data={"led_color": "#a0e0a0"}),
        },
        vocalizations={"laughter": VocalizationEntry(tts_supported=True)},
        fallback_families={
            "high_energy_positive": FallbackFamily(members=["enthusiastic"], maps_to="content"),
        },
        unknown=UnknownEntry(maps_to="neutral"),
    )


def _build_synthesizer_chain() -> tuple[SegmenterProcessor, CartesiaSynthesisProcessor]:
    """Build the Story 3.7 segmenter + synthesizer pair sharing a cache.

    Tests drive ``segmenter_processor.process_frame(...)`` then chain
    its push_frame to the synthesizer's process_frame.
    """
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    synthesizer = CartesiaSynthesisProcessor(
        _make_mock_tts(),
        cache,
        segmenter_processor,
    )
    return segmenter_processor, synthesizer


class _StubSTTBackend:
    """STTBackend stub yielding canned transcripts.

    Mirrors the architecture's mock-at-Protocol-boundaries rule.
    The real `WhisperBackend` runs faster-whisper inside
    asyncio.to_thread; the stub returns instantly.
    """

    def __init__(self) -> None:
        self._calls = 0

    async def load(self) -> None:
        return

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        del audio
        self._calls += 1
        # Vary the response to exercise both the high-confidence and
        # low-confidence paths in the integration test.
        if self._calls % 5 == 0:
            return TranscriptionResult(text="mumble mumble", confidence=0.2)
        return TranscriptionResult(
            text="what time is it?",
            confidence=0.85,
        )


_TALKER_RESPONSE = "It's just past three o'clock."
# Synthesize ~3 chunks of fake PCM so the integration test exercises
# multi-chunk streaming through CartesiaSynthesisProcessor without
# burning real Cartesia tokens.
_TTS_CHUNKS = [b"chunk-1" * 32, b"chunk-2" * 32, b"chunk-3" * 32]


def _make_mock_talker() -> MagicMock:
    """Mock TalkerClient — returns the canned response instantly."""
    talker = MagicMock()

    async def _complete(transcript: str, context: dict[str, Any] | None = None) -> str:
        del transcript, context
        return _TALKER_RESPONSE

    talker.complete = _complete
    return talker


def _make_mock_tts() -> MagicMock:
    """Mock TTSClient yielding fake PCM chunks."""
    client = MagicMock()

    def _synthesize(text: str) -> AsyncIterator[bytes]:
        del text

        async def _gen() -> AsyncIterator[bytes]:
            for c in _TTS_CHUNKS:
                yield c

        return _gen()

    client.synthesize = _synthesize
    return client


class _AudioSink:
    """Records the first OutputAudioRawFrame seen for each turn.

    Stores ``(turn_index, first_frame_ns)`` so the test can compute
    ``end_ns -> first_frame_ns`` per turn after the run.
    """

    def __init__(self) -> None:
        self.first_frame_at: dict[int, int] = {}
        self.current_turn_idx: int = -1

    def reset_turn(self, idx: int) -> None:
        self.current_turn_idx = idx

    async def receive(self, frame: Any, _direction: Any = None) -> None:
        if (
            isinstance(frame, OutputAudioRawFrame)
            and self.current_turn_idx not in self.first_frame_at
        ):
            self.first_frame_at[self.current_turn_idx] = time.time_ns()


async def _drive_one_turn(
    stt: SttProcessor,
    stt_logger: _SttResultLogger,
    dispatcher: TurnDispatchProcessor,
    segmenter_processor: SegmenterProcessor,
    synthesizer: CartesiaSynthesisProcessor,
    sink: _AudioSink,
    end_ns: int,
) -> None:
    """Push an UtteranceCapturedFrame through the chain manually.

    Story 3.7 chain order: stt → stt_logger → dispatcher → segmenter
    → synthesizer → sink. Each processor's ``push_frame`` wires to
    the next stage. Minimal Pipecat-free harness — enough to verify
    the integration without standing up the full PipelineRunner.
    """

    # Wire stt → stt_logger
    async def _stt_push(f: Frame, d: Any = None) -> None:
        await stt_logger.process_frame(f, d)

    stt.push_frame = _stt_push  # type: ignore[method-assign]

    # Wire stt_logger → dispatcher
    async def _logger_push(f: Frame, d: Any = None) -> None:
        await dispatcher.process_frame(f, d)

    stt_logger.push_frame = _logger_push  # type: ignore[method-assign]

    # Wire dispatcher → segmenter_processor
    async def _dispatcher_push(f: Frame, d: Any = None) -> None:
        await segmenter_processor.process_frame(f, d)

    dispatcher.push_frame = _dispatcher_push  # type: ignore[method-assign]

    # Wire segmenter_processor → synthesizer
    async def _segmenter_push(f: Frame, d: Any = None) -> None:
        await synthesizer.process_frame(f, d)

    segmenter_processor.push_frame = _segmenter_push  # type: ignore[method-assign]

    # Wire synthesizer → sink
    synthesizer.push_frame = sink.receive  # type: ignore[method-assign]

    # Drive the turn. The UtteranceCapturedFrame triggers
    # segmenter_processor.reset() inside the segmenter — Story 3.7's
    # turn-boundary proxy.
    utterance = UtteranceCapturedFrame(
        audio=b"x" * 1000,
        start_ns=end_ns - 1_000_000_000,
        end_ns=end_ns,
    )
    await stt.process_frame(utterance, direction=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_simple_turn_p95_baseline_30_turns(tmp_path: Path) -> None:
    """30-turn baseline: end-of-speech → first OutputAudioRawFrame, p50/p95/max.

    With all three external Protocols mocked, expect double-digit
    millisecond latencies (no real I/O). If p95 > 100 ms, something
    is leaking real I/O into the test (or a sleep is hidden in a
    processor — which would be a regression).
    """
    # Build the chain under test.
    prompt = tmp_path / "talker_system.md"
    prompt.write_text("You are Ooppi.")
    # TalkerConfig isn't needed in the integration chain — the dispatcher
    # only consumes the TalkerClient Protocol, which we mock. Pinning the
    # tmp_path prompt file is the only reason for the variable above.
    stt_config = SttConfig(low_confidence_threshold=0.5)

    stt_backend = _StubSTTBackend()
    talker = _make_mock_talker()
    tts = _make_mock_tts()

    router = TurnRouter(stt_config, talker)
    # Inject the talker manually — TurnRouter takes the Protocol.
    router.talker = talker  # already set by ctor; explicit for clarity

    stt_processor = SttProcessor(stt_backend)  # type: ignore[arg-type]
    stt_logger = _SttResultLogger(stt_config.low_confidence_threshold)
    dispatcher = TurnDispatchProcessor(router)
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    synthesizer = CartesiaSynthesisProcessor(tts, cache, segmenter_processor)

    sink = _AudioSink()

    latencies_ms: list[int] = []
    for i in range(30):
        sink.reset_turn(i)
        end_ns = time.time_ns()
        await _drive_one_turn(
            stt_processor,
            stt_logger,
            dispatcher,
            segmenter_processor,
            synthesizer,
            sink,
            end_ns,
        )
        # Record latency from end_ns → first audio frame.
        first_frame_ns = sink.first_frame_at.get(i)
        assert first_frame_ns is not None, f"turn {i}: no audio frame received"
        latencies_ms.append((first_frame_ns - end_ns) // 1_000_000)

    latencies_ms.sort()
    p50 = latencies_ms[len(latencies_ms) // 2]
    p95 = latencies_ms[int(len(latencies_ms) * 0.95)]
    max_ms = latencies_ms[-1]

    # Print baseline for the commit message / dev record.
    print(f"\nNFR1 mocked baseline (30 turns): p50={p50}ms p95={p95}ms max={max_ms}ms")

    # All-mocked latency should be far inside NFR1's 1500ms budget.
    # Real-world I/O (Talker + Cartesia + STT) is the dominant term.
    # If p95 > 100ms with mocks, something is leaking real I/O.
    assert p95 < 100, (
        f"NFR1 mocked p95 too high ({p95}ms) — likely a real-I/O leak "
        f"or hidden sleep in a processor"
    )


@pytest.mark.asyncio
async def test_strict_field_names_still_redacted_at_info(tmp_path: Path) -> None:
    """Story 2.5 v1-personal-use policy: strict gated names stay strict.

    Story 1.3 gated ``transcript`` / ``user_text`` at INFO+. Story 2.5
    deliberately surfaces the SAME data under operator-visible aliases
    (``heard``, ``prompt``, ``response``) at INFO for a personal voice
    companion. The policy is:

    - ``transcript`` / ``user_text`` — still gated by Story 1.3's
      redaction processor. Accidental leaks under these names remain
      caught.
    - ``heard`` / ``prompt`` / ``response`` — deliberate operator-
      visible aliases; the `code` emits these at INFO for v1.

    For deployed scenarios (Story 5.3 hardening), the operator either
    drops the deliberate fields or extends the redaction denylist.
    """
    import structlog

    prompt = tmp_path / "talker_system.md"
    prompt.write_text("You are Ooppi.")
    # TalkerConfig isn't needed in the integration chain — the dispatcher
    # only consumes the TalkerClient Protocol, which we mock. Pinning the
    # tmp_path prompt file is the only reason for the variable above.
    stt_config = SttConfig(low_confidence_threshold=0.5)

    stt_backend = _StubSTTBackend()
    talker = _make_mock_talker()
    tts = _make_mock_tts()
    router = TurnRouter(stt_config, talker)
    stt_processor = SttProcessor(stt_backend)  # type: ignore[arg-type]
    stt_logger = _SttResultLogger(stt_config.low_confidence_threshold)
    dispatcher = TurnDispatchProcessor(router)
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    synthesizer = CartesiaSynthesisProcessor(tts, cache, segmenter_processor)
    sink = _AudioSink()

    with structlog.testing.capture_logs() as captured:
        for i in range(5):
            sink.reset_turn(i)
            await _drive_one_turn(
                stt_processor,
                stt_logger,
                dispatcher,
                segmenter_processor,
                synthesizer,
                sink,
                time.time_ns(),
            )

    # The strict-gated field names from Story 1.3 must still NOT
    # appear at INFO+. The deliberate aliases (heard / prompt /
    # response) MAY appear — operator visibility for v1 personal use.
    info_records = [r for r in captured if r.get("log_level") == "info"]
    for rec in info_records:
        assert "transcript" not in rec, (
            f"strict-gated 'transcript' leaked into INFO log "
            f"(use 'heard' for operator visibility): {rec!r}"
        )
        assert "user_text" not in rec, f"strict-gated 'user_text' leaked into INFO log: {rec!r}"

    # talker.responded — same posture; carries latency + clarification only.
    talker_responses = [r for r in captured if r.get("event") == "talker.responded"]
    assert talker_responses, "expected at least one talker.responded"
    for rec in talker_responses:
        assert "text" not in rec
        assert "transcript" not in rec
        assert "response" not in rec


@pytest.mark.asyncio
async def test_no_audio_field_names_in_logs(tmp_path: Path) -> None:
    """Privacy invariant: no log record carries ``audio_bytes`` / ``audio_data`` / ``pcm`` fields.

    These field names are on Story 1.3's redaction denylist; the
    redaction processor strips them. This test verifies the *code*
    never even tries to log them — defense in depth.
    """
    import structlog

    prompt = tmp_path / "talker_system.md"
    prompt.write_text("You are Ooppi.")
    stt_config = SttConfig(low_confidence_threshold=0.5)

    stt_backend = _StubSTTBackend()
    talker = _make_mock_talker()
    tts = _make_mock_tts()
    router = TurnRouter(stt_config, talker)
    stt_processor = SttProcessor(stt_backend)  # type: ignore[arg-type]
    stt_logger = _SttResultLogger(stt_config.low_confidence_threshold)
    dispatcher = TurnDispatchProcessor(router)
    cache = LastPublishedCache()
    segmenter_processor = SegmenterProcessor(Segmenter(_make_mapping()), cache)
    synthesizer = CartesiaSynthesisProcessor(tts, cache, segmenter_processor)
    sink = _AudioSink()

    with structlog.testing.capture_logs() as captured:
        for i in range(3):
            sink.reset_turn(i)
            await _drive_one_turn(
                stt_processor,
                stt_logger,
                dispatcher,
                segmenter_processor,
                synthesizer,
                sink,
                time.time_ns(),
            )

    forbidden = ("audio_bytes", "audio_data", "pcm", "audio")
    for rec in captured:
        for f in forbidden:
            assert f not in rec, (
                f"forbidden field {f!r} leaked into log: {rec.get('event')!r} → {rec!r}"
            )
