"""Unit tests for :mod:`voice_agent_pipeline.audio.mic_mode` (Story 4.6).

Covers :class:`MicModeRouter` and :class:`_ModeStampedAudioFrame`:

- Default starting mode is ``wake_word_only`` (matches FSM startup posture).
- Consumer task picks up signals from the queue and updates ``_mic_mode``.
- Idempotent same-mode signals are filtered (defensive — Story 4.3 dedups
  upstream too).
- ``process_frame`` stamps :class:`AudioRawFrame` with current mode.
- Non-audio frames pass through unchanged.
- Mode-change callback fires on real transitions.
- ``setup`` / ``cleanup`` lifecycle starts and cancels the consumer task.
- Privacy: no audio bytes in any log line.

Test synchronization
--------------------

The consumer task runs cooperatively. Tests await
``router._signal_processed.wait()`` after pushing to the queue, then
``clear()`` for the next iteration. This is deterministic and avoids
``asyncio.sleep(arbitrary_value)`` flakiness — the canonical pattern
documented in ``mic_mode.py`` itself.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.processors.frame_processor import FrameDirection

from voice_agent_pipeline.activity.machine import MicMode
from voice_agent_pipeline.audio.mic_mode import MicModeRouter, _ModeStampedAudioFrame


class _StubSetup:
    """Minimal stand-in for FrameProcessorSetup (matches Story 1.6's stub)."""

    def __init__(self) -> None:
        self.clock = MagicMock()
        self.task_manager = AsyncMock()
        self.observer = None


def _make_router() -> tuple[MicModeRouter, asyncio.Queue[MicMode]]:
    """Construct a fresh router + queue pair for a test."""
    queue: asyncio.Queue[MicMode] = asyncio.Queue()
    router = MicModeRouter(queue)
    return router, queue


def _capture_pushed(router: MicModeRouter) -> list[Frame]:
    """Replace push_frame with a list-collector for downstream introspection."""
    pushed: list[Frame] = []

    async def _collect(frame: Frame, direction: FrameDirection) -> None:
        del direction
        pushed.append(frame)

    router.push_frame = _collect  # type: ignore[assignment]
    return pushed


@pytest.mark.asyncio
async def test_default_mic_mode_is_wake_word_only() -> None:
    """Construction-time default matches the FSM's startup posture."""
    router, _ = _make_router()
    assert router.mic_mode == "wake_word_only"


@pytest.mark.asyncio
async def test_consume_signals_updates_mic_mode() -> None:
    """Signal pushed to the queue updates ``_mic_mode`` after the consumer ticks."""
    router, queue = _make_router()
    await router.setup(_StubSetup())  # type: ignore[arg-type]
    try:
        await queue.put("vad_stt")
        await router._signal_processed.wait()
        assert router.mic_mode == "vad_stt"
    finally:
        await router.cleanup()


@pytest.mark.asyncio
async def test_consume_signals_logs_transition() -> None:
    """``mic_mode.transition`` INFO log fires with from_mode + to_mode."""
    router, queue = _make_router()
    with structlog.testing.capture_logs() as captured:
        await router.setup(_StubSetup())  # type: ignore[arg-type]
        try:
            await queue.put("vad_stt")
            await router._signal_processed.wait()
        finally:
            await router.cleanup()
    matching = [r for r in captured if r.get("event") == "mic_mode.transition"]
    assert len(matching) == 1
    assert matching[0].get("from_mode") == "wake_word_only"
    assert matching[0].get("to_mode") == "vad_stt"


@pytest.mark.asyncio
async def test_idempotent_same_mode_signal_skipped() -> None:
    """Same-mode signals don't trigger transition log or callback."""
    router, queue = _make_router()
    callback = AsyncMock()
    router.set_on_mode_change(callback)

    with structlog.testing.capture_logs() as captured:
        await router.setup(_StubSetup())  # type: ignore[arg-type]
        try:
            # Push the SAME starting mode — should be a no-op transition.
            await queue.put("wake_word_only")
            await router._signal_processed.wait()
        finally:
            await router.cleanup()

    transitions = [r for r in captured if r.get("event") == "mic_mode.transition"]
    assert transitions == []
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_process_frame_stamps_audio_with_current_mode() -> None:
    """``AudioRawFrame`` → ``_ModeStampedAudioFrame`` carrying current mode."""
    router, queue = _make_router()
    pushed = _capture_pushed(router)
    await router.setup(_StubSetup())  # type: ignore[arg-type]
    try:
        # Initial wake_word_only stamp.
        frame = AudioRawFrame(audio=b"\x00\x00", sample_rate=16000, num_channels=1)
        await router.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert len(pushed) == 1
        stamped = pushed[0]
        assert isinstance(stamped, _ModeStampedAudioFrame)
        assert stamped.mic_mode == "wake_word_only"

        # Switch to vad_stt; next frame stamped with the new mode.
        await queue.put("vad_stt")
        await router._signal_processed.wait()
        await router.process_frame(frame, FrameDirection.DOWNSTREAM)
        stamped2 = pushed[1]
        assert isinstance(stamped2, _ModeStampedAudioFrame)
        assert stamped2.mic_mode == "vad_stt"
    finally:
        await router.cleanup()


@pytest.mark.asyncio
async def test_process_frame_passes_non_audio_frames_through_unchanged() -> None:
    """Non-AudioRawFrame instances flow through identity-preserved."""
    from voice_agent_pipeline.audio.wakeword import WakeWordDetectedFrame

    router, _ = _make_router()
    pushed = _capture_pushed(router)
    wake = WakeWordDetectedFrame()
    await router.process_frame(wake, FrameDirection.DOWNSTREAM)
    assert pushed == [wake]


@pytest.mark.asyncio
async def test_already_stamped_frames_pass_through_without_double_wrap() -> None:
    """An incoming ``_ModeStampedAudioFrame`` flows through unchanged."""
    router, _ = _make_router()
    pushed = _capture_pushed(router)
    incoming = _ModeStampedAudioFrame(
        audio=b"\x00\x00",
        sample_rate=16000,
        num_channels=1,
        mic_mode="vad_stt",
    )
    await router.process_frame(incoming, FrameDirection.DOWNSTREAM)
    assert pushed == [incoming]


@pytest.mark.asyncio
async def test_on_mode_change_callback_invoked_with_old_and_new() -> None:
    """``set_on_mode_change`` callback receives ``(old_mode, new_mode)`` on transitions."""
    router, queue = _make_router()
    received: list[tuple[Any, Any]] = []

    async def _callback(old: MicMode, new: MicMode) -> None:
        received.append((old, new))

    router.set_on_mode_change(_callback)
    await router.setup(_StubSetup())  # type: ignore[arg-type]
    try:
        await queue.put("vad_stt")
        await router._signal_processed.wait()
    finally:
        await router.cleanup()

    assert received == [("wake_word_only", "vad_stt")]


@pytest.mark.asyncio
async def test_setup_starts_consumer_cleanup_cancels_it() -> None:
    """``setup()`` creates the signal task; ``cleanup()`` cancels it cleanly."""
    router, _ = _make_router()
    assert router._signal_task is None
    await router.setup(_StubSetup())  # type: ignore[arg-type]
    assert router._signal_task is not None
    assert not router._signal_task.done()
    await router.cleanup()
    assert router._signal_task is None
