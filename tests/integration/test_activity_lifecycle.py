"""Integration test for Story 4.3 — activity FSM driven by audio frames.

Validates the FSM + ``_FsmEventBridge`` couple end-to-end at the
processor level: drive ``WakeWordDetectedFrame`` and
``UtteranceCapturedFrame`` through the bridge; assert the FSM
publishes the correct ``ActivityEvent`` sequence on the
:class:`LogEventPublisher`.

This is a focused processor-level integration test (not a full
``run_pipeline`` test) — same shape as Story 3.7's embodiment-
alignment test. Story 4.7's complex-turn integration test will
exercise the full pipeline with the orchestrator slow-path; Story
4.3's test pins the FSM + bridge contract in isolation.
"""

from __future__ import annotations

import pytest
from pipecat.processors.frame_processor import FrameDirection

from voice_agent_pipeline.activity.machine import ActivityFSM
from voice_agent_pipeline.audio.vad import UtteranceCapturedFrame
from voice_agent_pipeline.audio.wakeword import WakeWordDetectedFrame
from voice_agent_pipeline.pipeline import _FsmEventBridge
from voice_agent_pipeline.publisher.log_adapter import LogEventPublisher
from voice_agent_pipeline.schemas.activity_event import ActivityEvent


@pytest.mark.asyncio
async def test_fsm_event_bridge_drives_wake_to_working_thinking() -> None:
    """Wake + utterance frames drive the FSM through ``sleeping → working[thinking]``.

    Mirrors what happens in production when:
    1. The wake-word fires (``WakeWordDetectedFrame``).
    2. The user speaks an utterance (``UtteranceCapturedFrame``).

    Asserts the published ``ActivityEvent`` sequence covers every
    transition end-to-end via the bridge processor — not just the
    FSM in isolation.
    """
    publisher = LogEventPublisher()
    fsm = ActivityFSM(publisher=publisher)
    await fsm.start()

    bridge = _FsmEventBridge(fsm)
    # Drive the bridge's parent-class lifecycle hook so process_frame
    # works without a live Pipecat pipeline. The base class's
    # ``process_frame`` checks lifecycle state; we satisfy it by
    # invoking once with a no-op frame to settle internal state.
    # (Pipecat's ``FrameProcessor`` is documented to allow
    # ``process_frame`` calls outside a running pipeline for testing.)

    # 1. Wake-word arrives.
    wake_frame = WakeWordDetectedFrame(keyword="hey_olaf", keyword_index=0)
    await bridge.process_frame(wake_frame, FrameDirection.DOWNSTREAM)

    # 2. User speaks; VAD emits the utterance.
    utterance = UtteranceCapturedFrame(
        audio=b"\x00\x00" * 16000,  # 1s of silence-shaped PCM
        start_ns=0,
        end_ns=1_000_000_000,
        sample_rate=16000,
    )
    await bridge.process_frame(utterance, FrameDirection.DOWNSTREAM)

    # Assert the FSM walked through every transition in order.
    states = [e.payload.state for _, e in publisher.published if isinstance(e, ActivityEvent)]
    assert states == [
        "sleeping",  # start()
        "waking",  # wake-word detected
        "listening",  # waking → listening (chained from utterance)
        "working",  # listening → working[thinking]
    ]
    # The last transition's sub-mode is "thinking".
    final = [e.payload for _, e in publisher.published if isinstance(e, ActivityEvent)][-1]
    assert final.working_submode == "thinking"


@pytest.mark.asyncio
async def test_fsm_event_bridge_cancels_pending_sleep_on_wake() -> None:
    """A wake-word firing during deferred-sleep clears ``sleep_pending``.

    FR46's race-window protection: if the user wakes OLAF before the
    last audio frame of the goodbye plays, we don't want
    ``on_last_audio_frame`` to silently flip back to sleeping.
    """
    publisher = LogEventPublisher()
    fsm = ActivityFSM(publisher=publisher)
    await fsm.start()

    bridge = _FsmEventBridge(fsm)

    # Manually set sleep_pending to simulate the race-window scenario.
    fsm.on_tool_call_go_to_sleep()
    assert fsm.sleep_pending is True

    # Wake-word arrives mid-deferred-sleep.
    wake_frame = WakeWordDetectedFrame(keyword="hey_olaf", keyword_index=0)
    await bridge.process_frame(wake_frame, FrameDirection.DOWNSTREAM)

    # The bridge cancelled the pending flag before transitioning.
    assert fsm.sleep_pending is False
    assert fsm.current_state == "waking"


@pytest.mark.asyncio
async def test_correlation_id_per_activity_event_unique() -> None:
    """Each published ``ActivityEvent`` carries its own ``correlation_id``.

    The envelope's ``correlation_id`` defaults to ``uuid4()`` per
    construction. Story 3.7's per-turn binding uses contextvars to
    bind a single id across all events of a turn; Story 4.3's FSM
    transitions don't yet bind contextvars, so each event has a
    unique id. (Story 3.7's binding will land for the FSM's
    transitions when Story 4.3's ``_TurnBoundaryFrame`` migration
    happens — deferred to Story 4.6 / 4.7.)
    """
    publisher = LogEventPublisher()
    fsm = ActivityFSM(publisher=publisher)
    await fsm.start()
    await fsm.on_wake_detected()
    await fsm.on_speech_started()
    await fsm.on_speech_ended()

    ids = {e.correlation_id for _, e in publisher.published if isinstance(e, ActivityEvent)}
    # Without contextvar binding, every event has a unique uuid4.
    assert len(ids) == 4  # one per published transition
