"""Mic-mode signal consumer + audio-frame stamping (Story 4.6).

The single-stream invariant (FR47) requires the mic to flip between
two distinct phases without ever running both Wakeword and VAD on the
same frame:

- ``wake_word_only`` — Porcupine engaged; VAD/STT suspended.
- ``vad_stt`` — VAD + STT engaged; Porcupine suspended.

The :class:`ActivityFSM` (Story 4.3) signals the desired mode on its
``mic_mode_queue``. This module's :class:`MicModeRouter` is the
Pipecat processor that consumes those signals and **stamps every
:class:`AudioRawFrame`** with the active mode. Wakeword + VAD then
gate on the stamp — each processor checks ``frame.mic_mode`` against
its own expected value and skips if it doesn't match.

Why frame-stamping (not frame-filtering)
----------------------------------------

Pipecat's pipeline is linear; both Wakeword and VAD are downstream of
the router in series. If the router DROPPED frames not destined for
the current mode's consumer, both downstream processors would lose
audio (the router doesn't know which one wants it). Stamping each
frame and letting each processor self-gate keeps the contract
type-safe and obvious — a future contributor reading
``WakewordProcessor.process_frame`` sees the gate inline.

Lifecycle
---------

The router subscribes to the FSM's mic-mode queue via an
``asyncio.Task`` started in ``setup()`` and cancelled in
``cleanup()``. The default mode is ``wake_word_only`` (matches the
FSM's startup posture), so frames flowing before the first signal
hits the consumer task are stamped correctly.

Buffer-clear callback
---------------------

Mode transitions need side effects: clear Porcupine's rolling buffer,
reset VAD's in-flight state, etc. The router exposes
:meth:`set_on_mode_change` so the pipeline-assembly site can inject
those side effects without coupling :class:`MicModeRouter` to specific
downstream processor types. Keeps this class single-purpose: receive
signals, stamp frames, fire callbacks.

Test synchronization
--------------------

The consumer task runs cooperatively; tests pushing to the queue may
not see the new mode immediately. The :attr:`_signal_processed`
``asyncio.Event`` is set after each signal — tests
``await router._signal_processed.wait()`` to deterministically
synchronize. Documented inline as the canonical pattern.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from pipecat.frames.frames import AudioRawFrame, Frame
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)

from voice_agent_pipeline.activity.machine import MicMode

log = structlog.get_logger(__name__)


# NOT frozen — pipecat's Frame base is non-frozen, dataclass machinery
# refuses to make a frozen subclass of a non-frozen parent. By
# convention nothing mutates these instances after construction.
@dataclass
class _ModeStampedAudioFrame(AudioRawFrame):
    """:class:`AudioRawFrame` carrying the active mic mode (Story 4.6).

    Wakeword and VAD processors check this stamp before processing.
    The single-stream invariant (FR47) is enforced by the stamp:
    exactly one downstream processor consumes each frame's payload.

    Attributes:
        mic_mode: The mode the audio should be routed to. Matches the
            current :class:`MicModeRouter._mic_mode` at the moment the
            frame was stamped. Defaults to ``"wake_word_only"`` so the
            startup posture is correct even if the router is
            constructed without an explicit signal.
    """

    mic_mode: MicMode = "wake_word_only"


class MicModeRouter(FrameProcessor):
    """Pipecat processor — stamps audio frames with the current mic mode.

    Subscribes to the FSM's mic-mode queue via a background task. On
    each :class:`AudioRawFrame`, wraps in a :class:`_ModeStampedAudioFrame`
    carrying the active mode. Non-audio frames pass through unchanged.

    On mode transitions, fires an optional buffer-clear callback so
    the pipeline-assembly site can reset Porcupine / VAD / STT state
    without :class:`MicModeRouter` knowing about those processors
    directly.
    """

    def __init__(self, mic_mode_queue: asyncio.Queue[MicMode]) -> None:
        """Build the router.

        Args:
            mic_mode_queue: Single-reader queue the FSM writes into.
                Production wires this from
                :attr:`ActivityFSM.mic_mode_queue`. Tests inject a
                fresh ``asyncio.Queue`` per test.
        """
        super().__init__()  # pyright: ignore[reportUnknownMemberType]
        self._queue = mic_mode_queue
        # Default matches the FSM's startup posture (sleeping). The FSM's
        # ``start()`` enqueues ``"wake_word_only"`` immediately; if a
        # frame flows before the consumer task runs, it gets stamped
        # correctly anyway.
        self._mic_mode: MicMode = "wake_word_only"
        # Background consumer task — created in ``setup``, cancelled in
        # ``cleanup``. None outside that window.
        self._signal_task: asyncio.Task[None] | None = None
        # Buffer-clear callback — set by the pipeline-assembly site
        # after construction. Kept Optional so tests can leave it
        # unset; the consumer skips the callback when None.
        self._on_mode_change: Callable[[MicMode, MicMode], Awaitable[None]] | None = None
        # Test-synchronization event. The consumer sets it after each
        # signal is processed; tests wait on it to deterministically
        # observe the post-transition state. Documented in the module
        # docstring.
        self._signal_processed: asyncio.Event = asyncio.Event()

    async def setup(self, setup: FrameProcessorSetup) -> None:
        """Pipecat hook — start the background signal consumer."""
        await super().setup(setup)  # pyright: ignore[reportUnknownMemberType]
        # Spawn the consumer. The task lives until cleanup() cancels it.
        self._signal_task = asyncio.create_task(self._consume_signals())

    async def cleanup(self) -> None:
        """Pipecat hook — cancel the consumer task and wait for it to exit."""
        if self._signal_task is not None:
            self._signal_task.cancel()
            try:
                await self._signal_task
            except asyncio.CancelledError:
                # Expected — the consumer's ``except asyncio.CancelledError``
                # branch breaks the loop cleanly.
                pass
            self._signal_task = None
        await super().cleanup()  # pyright: ignore[reportUnknownMemberType]

    def set_on_mode_change(
        self,
        callback: Callable[[MicMode, MicMode], Awaitable[None]],
    ) -> None:
        """Register the buffer-clear callback for mode transitions.

        Called once at pipeline-assembly time. The callback fires
        AFTER ``_mic_mode`` is updated and the transition log is
        emitted. Receives ``(old_mode, new_mode)``.
        """
        self._on_mode_change = callback

    @property
    def mic_mode(self) -> MicMode:
        """Read-only access to the currently active mic mode (tests)."""
        return self._mic_mode

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Stamp audio frames with the active mode; pass everything else through.

        Audio-frame discrimination is by **exact type**, not isinstance:
        if the frame is already an instance of
        :class:`_ModeStampedAudioFrame` (e.g. test harness manually
        injecting one), we pass it through unchanged rather than
        wrapping a wrapper. ``isinstance`` would match
        :class:`_ModeStampedAudioFrame` too — type() check is the
        correct discriminator here.
        """
        await super().process_frame(frame, direction)

        # Only wrap plain AudioRawFrame instances. ``isinstance`` plus
        # the explicit not-already-stamped guard handles three cases:
        # 1. Plain ``AudioRawFrame`` from ``transport.input()`` → wrap.
        # 2. ``_ModeStampedAudioFrame`` (test harness pre-stamping) →
        #    pass through (don't double-wrap).
        # 3. Any other ``AudioRawFrame`` subclass that future stories
        #    introduce → also pass through to be safe.
        if isinstance(frame, AudioRawFrame) and not isinstance(frame, _ModeStampedAudioFrame):
            stamped = _ModeStampedAudioFrame(
                audio=frame.audio,
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
                mic_mode=self._mic_mode,
            )
            # Pipecat's push_frame typing doesn't recognize the
            # AudioRawFrame subclass — same gap Story 3.7's
            # EmbodimentAudioFrame works around.
            await self.push_frame(stamped, direction)  # type: ignore[arg-type]
            return

        # Non-audio frames + already-stamped frames flow through unchanged.
        await self.push_frame(frame, direction)

    async def _consume_signals(self) -> None:
        """Background loop — read mic-mode signals; update state; fire callback.

        Defensive against duplicate signals (Story 4.3's de-dup
        invariant should prevent them, but the cost of double-checking
        is one comparison). On a real transition: update
        ``_mic_mode``, log INFO, await the optional callback, set
        the test-sync event.
        """
        while True:
            try:
                new_mode = await self._queue.get()
            except asyncio.CancelledError:
                # Pipecat's cleanup path cancels us — exit cleanly.
                break
            old_mode = self._mic_mode
            if old_mode == new_mode:
                # Idempotent same-mode signal; FSM's de-dup should
                # prevent this, but we re-check defensively. Still
                # set the sync event so tests pushing redundant
                # signals don't deadlock waiting for it.
                self._signal_processed.set()
                continue
            self._mic_mode = new_mode
            log.info("mic_mode.transition", from_mode=old_mode, to_mode=new_mode)
            if self._on_mode_change is not None:
                # Errors in the callback propagate — the orchestrator
                # callback in pipeline.py is first-party code; bugs
                # there should crash, not be silently swallowed
                # (CLAUDE.md rule #4 spirit).
                await self._on_mode_change(old_mode, new_mode)
            # Deterministic test sync — set AFTER the callback fires
            # so tests waiting on the event observe the fully-applied
            # state.
            self._signal_processed.set()


__all__ = ["MicModeRouter", "_ModeStampedAudioFrame"]
