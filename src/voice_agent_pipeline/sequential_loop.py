"""Half-duplex sequential voice loop — record → transcribe → reply → speak.

The Pipecat streaming pipeline (in :mod:`pipeline`) opens mic and
speaker concurrently. Without acoustic echo cancellation (AEC), the
bot's own audio plays through the speaker, gets captured by the mic,
gets transcribed back as input — infinite babble loop. The
production answer is system-level AEC (PipeWire's
``module-echo-cancel``); until that lands, the simpler answer is
**half-duplex**: mic and speaker are never active simultaneously.

This module is the half-duplex implementation. The shape is the same
as a one-page voice agent:

::

    while is_awake:
        wait_for_wake()                  # Porcupine; mic open
        await speak(greeting)            # Cartesia → speaker; mic CLOSED
        while not sleep_pending:
            audio = record_with_vad()    # mic open; speaker silent
            text = stt.transcribe(audio)
            reply = talker.complete(text)
            tools.dispatch(reply.tools)
            await speak(reply.text)      # mic CLOSED; speaker active
        # FSM transitions back to sleeping via deferred-sleep chain

Reuses the v1 components — :class:`ActivityFSM`, :class:`MoodController`,
:class:`Talker`, :class:`ToolRegistry`, :class:`CartesiaClient`,
``trigger_greeting``, ``clarification_prompts`` — but coordinates them
serially instead of through Pipecat's frame-graph.

Future Phase 2 (when AEC lands): switch the entry point back to
:func:`pipeline.run_pipeline`. The Pipecat assembly stays parked-but-
tested; no rewrite needed to flip back.
"""

from __future__ import annotations

import asyncio
import random
import struct
import time
from typing import Any

import pvporcupine  # pyright: ignore[reportMissingTypeStubs]
import pyaudio  # pyright: ignore[reportMissingTypeStubs]
import structlog
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

from voice_agent_pipeline.activity import trigger_greeting
from voice_agent_pipeline.activity.machine import ActivityFSM
from voice_agent_pipeline.audio._silence import suppress_native_stderr
from voice_agent_pipeline.audio.devices import resolve_audio_devices
from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.mood.controller import MoodController
from voice_agent_pipeline.mood.state import MoodState
from voice_agent_pipeline.publisher import build_publisher
from voice_agent_pipeline.stt import build_stt_backend
from voice_agent_pipeline.tts.cartesia import CartesiaClient
from voice_agent_pipeline.turn import build_talker, build_tool_registry

log = structlog.get_logger(__name__)


# 16 kHz mono S16LE — matches Whisper / Porcupine / Cartesia exactly.
_SAMPLE_RATE = 16000
# Silero VAD operates on fixed 512-sample chunks at 16 kHz (32 ms).
_SILERO_FRAME_SAMPLES = 512
_SILERO_FRAME_BYTES = _SILERO_FRAME_SAMPLES * 2
_SILERO_FRAME_MS = 32

# After Cartesia finishes streaming, the OS audio buffer may still have
# 100-200 ms of audio queued. Sleep this long before closing the output
# stream so trailing audio isn't truncated. Also acts as the "natural
# beat between turns" — without it, the loop snaps back to recording
# the instant Cartesia stops streaming, which feels unnaturally fast.
_AUDIO_DRAIN_TAIL_MS = 250

# Per-utterance recording cap. If the user trails off for >30s with
# no speech, abort and re-prompt. Story 5.4 calibrates this.
_MAX_UTTERANCE_SECONDS = 30.0


async def run_sequential_loop(config: SetupConfig) -> None:
    """Run the half-duplex sequential voice loop until cancelled.

    Builds all v1 components in the same order as
    ``pipeline.run_pipeline`` so the operator-facing setup checklist
    matches: publisher → mood → FSM → talker → STT → cartesia.
    Then enters the wake-greet-converse-sleep loop.
    """
    # PyAudio's `__init__` probes every device — emits ALSA/JACK noise
    # on stderr. Silence the C-level output (same trick the Pipecat
    # path uses in pipeline.py).
    with suppress_native_stderr():
        indices = resolve_audio_devices(
            input_pattern=config.audio.input_device_name,
            output_pattern=config.audio.output_device_name,
        )
        pa = pyaudio.PyAudio()

    # Build event publisher first — every other component publishes
    # through it. Connect failures crash, systemd restarts.
    event_publisher = build_publisher(config.publisher)
    await event_publisher.connect()

    try:
        # Mood + FSM. Mood publishes the latched startup event on
        # ``publish_initial``; FSM publishes the first ``starting →
        # sleeping`` transition on ``start``.
        mood_state = MoodState(initial=config.mood.initial)
        mood_controller = MoodController(
            mood_state,
            event_publisher,
            cooldown_publishes_per_hour=config.mood.cooldown_publishes_per_hour,
        )
        await mood_controller.publish_initial()

        # No greeting callback wired — the sequential loop drives
        # greeting playback directly (it knows when the bot's audio
        # is about to start, which the Pipecat callback didn't).
        fsm = ActivityFSM(publisher=event_publisher)
        await fsm.start()

        # Talker (no beliefs in dev mode — daemon disabled toggle
        # short-circuits the belief read; the orchestrator slow path
        # is parked too).
        talker = build_talker(config, beliefs=None)
        tool_registry = build_tool_registry(config.tools, fsm, mood_controller)

        # Pre-load STT — the model's first inference would otherwise
        # add ~2s to the first turn's latency.
        stt = build_stt_backend(config.stt)
        await stt.load()

        # Cartesia TTS client. Streaming SSE happens per ``speak`` call.
        tts = CartesiaClient(config.tts, config.cartesia_api_key)

        log.info("sequential_loop.ready")

        # Main loop: wake → greet → conversation → sleep → repeat.
        while True:
            await _wait_for_wake(pa, indices, config)
            await fsm.on_wake_detected()

            # Brief pause before greeting — feels less robotic than
            # snapping into a reply the instant the wake-word fires.
            await asyncio.sleep(0.2)

            greeting = trigger_greeting(
                mood_state.current,
                config.greeting.greetings_by_mood,
            )
            await _speak(pa, indices, tts, greeting)
            # Note: greeting plays during ``waking`` state. We do NOT
            # call ``on_first_audio_frame`` / ``on_last_audio_frame``
            # for the greeting — those FSM transitions only apply to
            # bot replies (working → speaking → listening). The
            # greeting is a pre-turn nicety.

            # Conversation loop — runs until the bot calls
            # ``go_to_sleep``, which sets ``fsm.sleep_pending``; the
            # next ``on_last_audio_frame`` flushes the deferred-sleep
            # chain and FSM ends up in ``sleeping``.
            while True:
                audio = await _record_with_vad(pa, indices, config)
                if audio is None:
                    # No speech detected within the window. Log + retry.
                    # An idle-auto-sleep timeout could land here in v1.5.
                    log.info("sequential_loop.no_speech_retry")
                    continue

                # FSM transitions: waking → listening → working[thinking]
                # on the first iteration; listening → working[thinking]
                # on subsequent iterations (on_speech_started is a
                # no-op when already in listening).
                await fsm.on_speech_started()
                await fsm.on_speech_ended()

                stt_result = await stt.transcribe(audio)
                # Privacy: heard text logged at INFO under ``heard``
                # (Story 2.5 deviation). The redaction processor
                # strips ``transcript`` / ``user_text`` field names
                # at INFO+ — ``heard`` is the deliberate operator
                # alias.
                log.info(
                    "stt.transcript",
                    confidence=stt_result.confidence,
                    end_to_transcript_ms=0,
                    heard=stt_result.text,
                )

                # Low-confidence: clarification short-circuit. No LLM
                # round-trip — just pick a canned phrase and play it.
                if stt_result.confidence < config.stt.low_confidence_threshold:
                    text_to_speak = random.choice(  # noqa: S311 — UX, not security
                        config.stt.clarification_prompts,
                    )
                    log.info(
                        "clarification.picked",
                        text=text_to_speak,
                    )
                    tool_calls: list[Any] = []
                else:
                    # Normal turn: Talker with tools.
                    response = await talker.complete_with_tools(
                        stt_result.text,
                        tool_registry,
                    )
                    log.info(
                        "talker.responded",
                        clarification=False,
                        tool_call_count=len(response.tool_calls),
                    )
                    text_to_speak = response.text
                    tool_calls = list(response.tool_calls)

                # Dispatch tool calls BEFORE speaking. In streaming
                # mode FR45/FR46 demanded text-first parallel dispatch
                # so audio frames weren't blocked. In half-duplex
                # mode the user can't hear anything until ``_speak``
                # finishes anyway, so the dispatch order doesn't
                # affect the user-perceived "text first" property.
                # Sequential dispatch is simpler. ``go_to_sleep`` just
                # sets ``sleep_pending``; ``set_mood`` publishes a
                # mood event — both fast.
                for tc in tool_calls:
                    try:
                        await tool_registry.dispatch(tc)
                    except Exception:
                        log.exception("tool.dispatch_error")

                # Bot replies (working → speaking).
                await fsm.on_first_audio_frame()
                await _speak(pa, indices, tts, text_to_speak)
                # Bot finishes (speaking → listening, OR via deferred-
                # sleep chain → going_to_sleep → sleeping if a
                # ``go_to_sleep`` tool call set ``sleep_pending``).
                await fsm.on_last_audio_frame()

                if fsm.current_state == "sleeping":
                    # Deferred-sleep fired. Break inner loop, go back
                    # to wait_for_wake.
                    log.info("sequential_loop.sleeping")
                    break
    finally:
        # Cleanup order matches construction (reverse): publisher last
        # so any pending mood/activity events flush before the bus
        # disconnects.
        try:
            await event_publisher.disconnect()
        except Exception:
            log.exception("publisher.disconnect_error")
        pa.terminate()
        log.info("sequential_loop.stopped")


async def _wait_for_wake(
    pa: pyaudio.PyAudio,
    indices: Any,
    config: SetupConfig,
) -> None:
    """Block until the wake-word fires.

    Opens a fresh PyAudio input stream sized to Porcupine's frame
    length (512 samples = ~32ms at 16kHz) and feeds chunks to
    Porcupine in a thread (the SDK's ``process()`` is sync C).
    Returns when ``process()`` returns a non-negative keyword index.

    The stream is closed on return so the next ``_record_with_vad``
    or ``_speak`` call can open a fresh one — keeps the device-busy
    semantics simple and avoids contention with the speaker side.
    """
    porcupine = await asyncio.to_thread(
        pvporcupine.create,
        access_key=config.picovoice_access_key.get_secret_value(),
        keyword_paths=[str(config.wakeword.model_path)],
        sensitivities=[config.wakeword.sensitivity],
    )
    try:
        with suppress_native_stderr():
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=porcupine.sample_rate,
                input=True,
                input_device_index=indices.input_index,
                frames_per_buffer=porcupine.frame_length,
            )
        try:
            log.info("wakeword.waiting")
            while True:
                pcm_bytes = await asyncio.to_thread(
                    stream.read,
                    porcupine.frame_length,
                    False,  # exception_on_overflow=False
                )
                pcm = struct.unpack_from("h" * porcupine.frame_length, pcm_bytes)
                result = await asyncio.to_thread(porcupine.process, pcm)
                if result >= 0:
                    log.info("wakeword.detected", keyword_index=result)
                    return
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        await asyncio.to_thread(porcupine.delete)


async def _record_with_vad(
    pa: pyaudio.PyAudio,
    indices: Any,
    config: SetupConfig,
    max_seconds: float = _MAX_UTTERANCE_SECONDS,
) -> bytes | None:
    """Record one utterance via Silero VAD; return raw PCM or ``None``.

    Half-duplex contract: this function is called ONLY when the
    speaker is silent. Opens its own input stream, drives Silero in
    a per-chunk loop, returns when end-of-speech is detected
    (``silence_duration_ms`` of continuous silence after first
    voiced chunk) OR ``max_seconds`` elapses.

    Returns ``None`` if no speech was detected (timeout) or if the
    captured audio was shorter than ``min_speech_duration_ms``
    (probably a cough or accidental tap).
    """
    silero = SileroVADAnalyzer(
        sample_rate=_SAMPLE_RATE,
        params=VADParams(
            confidence=config.vad.start_threshold,
            start_secs=0.2,
            stop_secs=0.2,
            min_volume=0.6,
        ),
    )
    silero.set_sample_rate(_SAMPLE_RATE)

    with suppress_native_stderr():
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=_SAMPLE_RATE,
            input=True,
            input_device_index=indices.input_index,
            frames_per_buffer=_SILERO_FRAME_SAMPLES,
        )
    try:
        utterance_buffer = bytearray()
        chunk_buffer = bytearray()
        silence_run_ms = 0
        speech_seen = False
        deadline = time.monotonic() + max_seconds

        while time.monotonic() < deadline:
            pcm = await asyncio.to_thread(
                stream.read,
                _SILERO_FRAME_SAMPLES,
                False,  # exception_on_overflow=False
            )
            utterance_buffer.extend(pcm)
            chunk_buffer.extend(pcm)

            # Drain whole Silero frames; one mic read might be smaller
            # or larger than 512 samples depending on PyAudio scheduling.
            while len(chunk_buffer) >= _SILERO_FRAME_BYTES:
                chunk = bytes(chunk_buffer[:_SILERO_FRAME_BYTES])
                del chunk_buffer[:_SILERO_FRAME_BYTES]
                conf = await asyncio.to_thread(silero.voice_confidence, chunk)
                if conf >= config.vad.start_threshold:
                    speech_seen = True
                    silence_run_ms = 0
                else:
                    # Treating "not speech" as silence is more reliable
                    # than the start/end hysteresis band — same
                    # rationale as VadProcessor's per-chunk gate
                    # (audio/vad.py).
                    silence_run_ms += _SILERO_FRAME_MS

            if speech_seen and silence_run_ms >= config.vad.silence_duration_ms:
                speech_ms = len(utterance_buffer) * 1000 // (_SAMPLE_RATE * 2)
                if speech_ms < config.vad.min_speech_duration_ms:
                    log.debug(
                        "vad.utterance.dropped_short",
                        duration_ms=speech_ms,
                    )
                    return None
                log.info(
                    "vad.utterance.captured",
                    duration_ms=speech_ms,
                    silence_run_ms=silence_run_ms,
                )
                return bytes(utterance_buffer)

        log.info("vad.timeout", elapsed_seconds=max_seconds)
        return None
    finally:
        stream.stop_stream()
        stream.close()


async def _speak(
    pa: pyaudio.PyAudio,
    indices: Any,
    tts: CartesiaClient,
    text: str,
) -> None:
    """Synthesize ``text`` via Cartesia and play through the speaker.

    Half-duplex contract: this function blocks until audio playback
    finishes. Mic input is NOT recorded during this window — the
    caller must have closed any open input stream before calling.

    Empty ``text`` is a valid no-op (used when the Talker emits only
    tool calls with no text response). Just returns.
    """
    if not text:
        return
    log.info("tts.speak.start", text=text)

    with suppress_native_stderr():
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=_SAMPLE_RATE,
            output=True,
            output_device_index=indices.output_index,
        )
    try:
        chunk_count = 0
        byte_total = 0
        async for chunk in tts.synthesize(text):
            chunk_count += 1
            byte_total += len(chunk)
            # PyAudio's blocking write returns when there's space in
            # the OS audio buffer — naturally rate-limits us to
            # playback speed so we don't pile up audio mid-segment.
            await asyncio.to_thread(stream.write, chunk)
        # Trailing drain — let the OS finish playing what's already
        # in the buffer before we close the stream.
        await asyncio.sleep(_AUDIO_DRAIN_TAIL_MS / 1000)
        log.info(
            "tts.speak.complete",
            chunk_count=chunk_count,
            byte_total=byte_total,
        )
    finally:
        stream.stop_stream()
        stream.close()
