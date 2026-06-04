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
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pvporcupine  # pyright: ignore[reportMissingTypeStubs]
import pyaudio  # pyright: ignore[reportMissingTypeStubs]
import structlog
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

from voice_agent_pipeline.activity import trigger_greeting
from voice_agent_pipeline.activity.machine import ActivityFSM
from voice_agent_pipeline.audio._silence import suppress_native_stderr
from voice_agent_pipeline.audio.cached import CachedAudioManifest, play_cached
from voice_agent_pipeline.audio.devices import resolve_audio_devices
from voice_agent_pipeline.audio.opener_bucket import OpenerBucket
from voice_agent_pipeline.audio.openers import (
    OpenerPlayback,
    pick_opener,
    trigger_opener_fallback,
)
from voice_agent_pipeline.config.expression_map import load_from_path
from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.mood.controller import MoodController
from voice_agent_pipeline.mood.state import MoodState
from voice_agent_pipeline.publisher import build_publisher
from voice_agent_pipeline.publisher.interface import EventPublisher
from voice_agent_pipeline.schemas.speech_emotion_event import SpeechEmotionEvent
from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
    VocalizationPayload,
)
from voice_agent_pipeline.splitter.mapping import LastPublishedCache
from voice_agent_pipeline.splitter.segmenter import Segment, Segmenter
from voice_agent_pipeline.stt import build_stt_backend
from voice_agent_pipeline.tts import build_tts_client
from voice_agent_pipeline.tts.client import TTSClient
from voice_agent_pipeline.turn import build_talker, build_tool_registry
from voice_agent_pipeline.turn.talker import Talker, TalkerTextDelta
from voice_agent_pipeline.turn.tools import ToolCall, ToolRegistry

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


async def run_sequential_loop(
    config: SetupConfig,
    manifest: CachedAudioManifest,
) -> None:
    """Run the half-duplex sequential voice loop until cancelled.

    Builds all v1 components in the same order as
    ``pipeline.run_pipeline`` so the operator-facing setup checklist
    matches: publisher → mood → FSM → talker → STT → cartesia.
    Then enters the wake-greet-converse-sleep loop.

    Args:
        config: Validated :class:`SetupConfig` from the loader.
        manifest: Story 5.5 cached-audio manifest from the Stage 3
            startup probe. Greeting, goodbye, and clarification call
            sites do ``manifest.lookup(...)`` + ``play_cached(...)``
            instead of hitting Cartesia at runtime. Required —
            ``__main__.py`` always passes it; the Stage 3 probe
            refuses to start if the manifest isn't loadable.
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
        # 2026-05-12: factory now takes the full SetupConfig (was config.stt)
        # because the "groq" backend needs config.groq_api_key in addition
        # to the nested stt block. Mirrors how ``build_talker(config)``
        # works on the Talker side.
        stt = build_stt_backend(config)
        await stt.load()

        # TTS client (Cartesia or Gemini per [tts] provider). Streaming
        # happens per ``speak`` call; the factory enforces the provider key.
        tts = build_tts_client(config)

        # Embodiment-event publishing on the half-duplex path. The
        # pipecat assembly published speech_emotion + vocalization via
        # CartesiaSynthesisProcessor + _PrePublishProcessor; the
        # half-duplex migration carried over mood + activity (their
        # sources are MoodController / ActivityFSM) but DROPPED these
        # two, whose source was the splitter chain. We rebuild that
        # chain here from the SAME components: the segmenter parses the
        # Talker's ``<emotion .../>`` + ``[vocalization]`` tags out of
        # the reply stream into Segments, and ``_stream_and_speak``
        # publishes the per-segment events. ``load_from_path`` is
        # fail-fast (ConfigError on a missing/invalid map) — matches the
        # v1 no-silent-defaults posture (CLAUDE.md rule #4).
        expression_map = load_from_path(Path("expression_map.yaml"))
        segmenter = Segmenter(expression_map)
        emotion_cache = LastPublishedCache()

        log.info("sequential_loop.ready")

        # Main loop: wake → greet → conversation → sleep → repeat.
        while True:
            await _wait_for_wake(pa, indices, config)
            await fsm.on_wake_detected()

            # Per-wake conversation history. Reset on every wake —
            # each wake starts a fresh conversation. The desk-
            # companion mental model: "Hey OLAF" is a new
            # conversation, prior chat is gone. Each entry is an
            # openai-format message: {"role": "user|assistant",
            # "content": "..."}. Tool-call messages are NOT included
            # in history (v1 simplicity); the bot's text reply is
            # what the LLM sees on subsequent turns.
            conversation_history: list[dict[str, str]] = []

            # Brief pause before greeting — feels less robotic than
            # snapping into a reply the instant the wake-word fires.
            await asyncio.sleep(0.2)

            greeting = trigger_greeting(
                mood_state.current,
                config.greeting.greetings_by_mood,
            )
            # Story 5.5: play from the cached WAV instead of hitting
            # Cartesia at runtime. The Stage 3 probe guarantees a
            # cached entry exists for this (mood, greeting) tuple, so
            # the lookup never misses in production. output_index is
            # required in v1 (FR4) and was validated by the audio probe
            # at startup; cast narrows the type for play_cached.
            greeting_entry = manifest.lookup("greeting", greeting, mood=mood_state.current)
            await play_cached(
                pa,
                cast(int, indices.output_index),
                Path(greeting_entry.path),
            )
            # Note: greeting plays during ``waking`` state. We do NOT
            # call ``on_first_audio_frame`` / ``on_last_audio_frame``
            # for the greeting — those FSM transitions only apply to
            # bot replies (working → speaking → listening). The
            # greeting is a pre-turn nicety.

            # Story 6.2: ring buffer of recently-played opener hashes.
            # Persists across turns within a single wake session so the
            # last-N suppression actually suppresses across turns.
            # maxlen = max_consecutive_repeat + 1 → at minimum the
            # immediately-previous opener is excluded.
            recent_openers: deque[str] = deque(
                maxlen=config.openers.max_consecutive_repeat + 1,
            )

            # Conversation loop — runs until the bot calls
            # ``go_to_sleep``, which sets ``fsm.sleep_pending``; the
            # next ``on_last_audio_frame`` flushes the deferred-sleep
            # chain and FSM ends up in ``sleeping``.
            while True:
                audio = await _record_with_vad(pa, indices, config)
                if audio is None:
                    # No speech detected within the window. Log + retry.
                    # An idle-auto-sleep timeout could land here in v1.5.
                    # NOT a turn — no `turn.complete` for an empty window.
                    log.info("sequential_loop.no_speech_retry")
                    continue

                # Story 6.4: VAD end-of-speech is the anchor for every
                # `vad_end → X` latency delta. One `_TurnTimings` per turn,
                # emitted as `turn.complete` at turn-end.
                timings = _TurnTimings(vad_end_ns=time.time_ns())

                # FSM transitions: waking → listening → working[thinking]
                # on the first iteration; listening → working[thinking]
                # on subsequent iterations (on_speech_started is a
                # no-op when already in listening).
                await fsm.on_speech_started()
                await fsm.on_speech_ended()

                # Story 6.2: spawn the opener-fallback timer task on
                # VAD end-of-speech. Two events coordinate with the
                # splitter-driven opener path:
                #
                # - `opener_selected` — set by the splitter callback
                #   when the LLM emits `<opener bucket="..."/>`. The
                #   fallback task `wait_for`s on this; if set before
                #   `timer_fallback_ms` expires, the fallback exits
                #   without playing.
                # - `opener_already_playing` — set by EITHER the
                #   splitter callback OR the fallback task itself
                #   right before each starts playback. Acts as the
                #   race-window check: whichever path commits first
                #   wins, the other silently bails.
                #
                # Unlike Story 5.5's filler design, the real-answer
                # path does NOT await this task before opening its
                # output stream — that's the Cartesia overlap
                # deletion (DR-001's serialization-tax fix). PyAudio's
                # device-level serialization handles the playback
                # ordering on the speaker.
                opener_selected = asyncio.Event()
                opener_already_playing = asyncio.Event()
                # Story 6.4 follow-up: when the operator disables the
                # timer fallback, openers play ONLY on an explicit LLM
                # `<opener .../>` tag (the splitter-driven path still
                # runs). We skip spawning the fallback task entirely so
                # untagged turns stay silent rather than firing a generic
                # filler. `opener_fallback_task` stays None; the await
                # site below tolerates that.
                opener_fallback_task: asyncio.Task[OpenerPlayback | None] | None = None
                if config.openers.timer_fallback_enabled:
                    opener_fallback_task = asyncio.create_task(
                        trigger_opener_fallback(
                            pa=pa,
                            indices=indices,
                            manifest=manifest,
                            timer_fallback_ms=config.openers.timer_fallback_ms,
                            timer_fallback_bucket=config.openers.timer_fallback_bucket,
                            opener_selected=opener_selected,
                            opener_already_playing=opener_already_playing,
                            recent=recent_openers,
                        ),
                    )

                stt_result = await stt.transcribe(audio)
                # Story 6.4: stamp STT-done + replace the long-standing
                # hardcoded `end_to_transcript_ms=0` placeholder with the
                # real measurement, renamed `stt_ms` (vad_end → transcript).
                timings.stt_done_ns = time.time_ns()
                stt_ms = (timings.stt_done_ns - timings.vad_end_ns) // 1_000_000
                # Privacy: heard text logged at INFO under ``heard``
                # (Story 2.5 deviation). The redaction processor
                # strips ``transcript`` / ``user_text`` field names
                # at INFO+ — ``heard`` is the deliberate operator
                # alias.
                log.info(
                    "stt.transcript",
                    confidence=stt_result.confidence,
                    stt_ms=stt_ms,
                    heard=stt_result.text,
                )

                # Low-confidence: clarification short-circuit. No LLM
                # round-trip — just pick a canned phrase and play it.
                # No streaming needed; the phrase is already a single
                # short sentence. Clarification turns DO go into
                # history (the bot said "say again?", the user's
                # next turn should make sense in that context).
                if stt_result.confidence < config.stt.low_confidence_threshold:
                    timings.routing = "clarification"
                    clarification_text = random.choice(  # noqa: S311
                        config.stt.clarification_prompts,
                    )
                    log.info("clarification.picked", text=clarification_text)
                    # Story 6.2: clarification short-circuits to a
                    # cached phrase, so we don't want a fallback opener
                    # firing on top. Cancel the timer (set
                    # `opener_selected` so the wait_for returns) and
                    # drain the task. If the fallback already started
                    # playing, `await opener_fallback_task` waits for
                    # it to finish so the speaker is free before we
                    # play the clarification.
                    opener_selected.set()
                    # `opener_fallback_task` is None when the timer
                    # fallback is disabled (no task spawned); nothing to
                    # drain in that case.
                    clar_fallback_playback = (
                        await opener_fallback_task if opener_fallback_task is not None else None
                    )
                    # Story 6.4: a fallback opener may have raced in before
                    # the cancel landed — record its timing if so.
                    if clar_fallback_playback is not None:
                        _record_opener_playback(timings, clar_fallback_playback)
                    await fsm.on_first_audio_frame()
                    # Clarifications are deterministic text; play from
                    # cached WAV instead of hitting Cartesia. No mood
                    # bucket — clarifications are a flat list.
                    clar_entry = manifest.lookup(
                        "clarification",
                        clarification_text,
                        mood=None,
                    )
                    await play_cached(
                        pa,
                        cast(int, indices.output_index),
                        Path(clar_entry.path),
                    )
                    await fsm.on_last_audio_frame()
                    # Story 6.4: clarification is a complete turn — emit its
                    # rollup. No real-answer audio (so end_to_first_real_audio_ms
                    # is None); had_tool_call stays False.
                    timings.turn_end_ns = time.time_ns()
                    _emit_turn_complete(timings)
                    conversation_history.append(
                        {"role": "user", "content": stt_result.text},
                    )
                    conversation_history.append(
                        {"role": "assistant", "content": clarification_text},
                    )
                    if fsm.current_state == "sleeping":
                        log.info("sequential_loop.sleeping")
                        break
                    continue

                # Normal turn: stream Talker tokens, segment into
                # sentences, fire Cartesia per sentence so playback
                # starts as soon as the FIRST sentence is ready (LLM
                # is usually still emitting later sentences when
                # we're playing the first one). FSM working→speaking
                # transition fires JUST before the first sentence's
                # audio starts to land — see ``_stream_and_speak``
                # for the timing.
                full_text, tool_calls = await _stream_and_speak(
                    pa,
                    indices,
                    tts,
                    talker,
                    tool_registry,
                    stt_result.text,
                    fsm,
                    manifest=manifest,
                    opener_selected=opener_selected,
                    opener_already_playing=opener_already_playing,
                    opener_fallback_task=opener_fallback_task,
                    recent_openers=recent_openers,
                    publisher=event_publisher,
                    segmenter=segmenter,
                    emotion_cache=emotion_cache,
                    error_filler_enabled=config.openers.error_filler_enabled,
                    error_filler_bucket=config.openers.error_filler_bucket,
                    history=conversation_history,
                    timings=timings,
                )
                # Story 6.4: a tool-call-only reply still counts the tool
                # call (and leaves real_first_frame_ns None → the rollup's
                # end_to_first_real_audio_ms is None).
                timings.had_tool_call = bool(tool_calls)

                # Append this turn to the history BEFORE dispatching
                # tools / firing on_last_audio_frame. Order: user turn,
                # then assistant turn.
                #
                # History hygiene (2026-05-31): only record the turn when
                # the assistant actually produced SOMETHING — speakable
                # text OR a tool call. A fully-empty reply (no text, no
                # tools — the gpt-oss reasoning-overflow failure mode) is
                # dropped from history entirely. Appending an empty
                # assistant message there poisons the next turn's context:
                # the model sees a malformed conversation and keeps
                # returning empty, so the whole session spirals into
                # silence (observed live 2026-05-31). Dropping the turn
                # keeps the alternation clean — the next turn appends its
                # own user+assistant pair. A tool-call-only reply STILL
                # records (empty text is intentional there — the tool call
                # is the turn).
                if full_text or tool_calls:
                    conversation_history.append(
                        {"role": "user", "content": stt_result.text},
                    )
                    conversation_history.append(
                        {"role": "assistant", "content": full_text},
                    )
                else:
                    log.warning(
                        "talker.empty_reply_dropped",
                        heard=stt_result.text,
                    )

                # Dispatch tool calls AFTER speech finishes. In half-
                # duplex mode the user can't hear anything until the
                # speak completes anyway, so the dispatch order
                # doesn't affect text-first UX. ``go_to_sleep`` just
                # sets ``sleep_pending`` (consumed by
                # on_last_audio_frame below); ``set_mood`` publishes
                # the mood event.
                for tc in tool_calls:
                    try:
                        await tool_registry.dispatch(tc)
                    except Exception:
                        log.exception("tool.dispatch_error")

                # If go_to_sleep was dispatched in this turn,
                # ``fsm.sleep_pending`` is now True. Play a hardcoded
                # goodbye phrase before letting the FSM transition
                # — the LLM's reply was already spoken, but we want
                # a CONSISTENT signoff (the user requested this:
                # the LLM's goodbye varies; the hardcoded phrase is
                # the audible "I'm going quiet" cue). Same static-
                # random pattern as the wake greeting.
                if fsm.sleep_pending:
                    goodbye_text = random.choice(  # noqa: S311
                        config.goodbye.phrases,
                    )
                    log.info("goodbye.picked", text=goodbye_text)
                    # Story 5.5: goodbye is deterministic, mood-less;
                    # play from cached WAV.
                    goodbye_entry = manifest.lookup(
                        "goodbye",
                        goodbye_text,
                        mood=None,
                    )
                    await play_cached(
                        pa,
                        cast(int, indices.output_index),
                        Path(goodbye_entry.path),
                    )

                # Bot finishes (speaking → listening, OR via deferred-
                # sleep chain → going_to_sleep → sleeping if a
                # ``go_to_sleep`` tool call set ``sleep_pending``).
                await fsm.on_last_audio_frame()

                # Story 6.4: turn boundary reached — emit the per-turn
                # rollup. Routing stays the default "fast_path": the
                # sequential loop runs the Talker directly (the
                # orchestrator slow-path is parked in v1), so every
                # non-clarification turn is fast-path.
                timings.turn_end_ns = time.time_ns()
                _emit_turn_complete(timings)

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


def _is_speakable(text: str) -> bool:
    """True iff ``text`` has content Cartesia will synthesize (≥1 alnum char).

    Cartesia returns a 400 ("transcript is empty or contains only
    punctuation") for empty / whitespace / punctuation-only text. The
    :class:`Segmenter` splits on EVERY terminator, so streams like
    ``"Wait..."`` or ``"Really?!"`` — or a stripped non-TTS vocalization
    like ``"[nod]."`` — yield punctuation-only segments (``"."`` / ``"!"``)
    that must be skipped for TTS. Their embodiment events (if any) still
    publish; there is simply no audio to render for that beat.
    """
    return any(ch.isalnum() for ch in text)


@dataclass
class _TurnTimings:
    """Per-turn latency accumulator for the ``turn.complete`` rollup (Story 6.4).

    Internal-only (private to this module), mutable — values land at
    different points in the turn's async flow, then the derived
    integer-millisecond fields are computed once at turn-end. All ``_ns``
    values are :func:`time.time_ns` readings on the single runtime clock
    (the ``tts.first_frame.ttfb_ms`` methodology — DR-001 comparability).

    One instance per turn: constructed at VAD end-of-speech, discarded
    after :func:`_emit_turn_complete`. ``None``-valued ``_ns`` anchors mean
    "that beat didn't happen this turn" (e.g. a tool-only reply has no
    ``real_first_frame_ns``); the derivation guards each with an ``if`` so
    the emitted field is ``None`` rather than a bogus delta.
    """

    vad_end_ns: int
    stt_done_ns: int | None = None
    talker_first_token_ns: int | None = None
    opener_first_frame_ns: int | None = None
    opener_last_frame_ns: int | None = None
    real_first_frame_ns: int | None = None
    turn_end_ns: int | None = None

    # Annotations gathered from the turn's events.
    ttfb_ms: int | None = None
    opener_source: Literal["llm_tag", "timer_fallback"] | None = None
    opener_bucket: OpenerBucket | None = None
    opener_duration_ms: int | None = None
    emphasis_count: int = 0
    routing: Literal["fast_path", "slow_path", "clarification"] = "fast_path"
    had_tool_call: bool = False


def _emit_turn_complete(timings: _TurnTimings) -> None:
    """Emit the once-per-turn ``turn.complete`` rollup log event (Story 6.4).

    Collapses the per-action log events (``stt.transcript``,
    ``tts.first_frame``, ``activity.transition``, ...) into a single
    per-turn record so the DR-003 dashboard + the v2 soak don't have to
    reconstruct turn shape by grouping. Additive — the per-action events
    still fire. Schema documented in architecture.md §Logging Conventions;
    contract-tested in ``tests/contract/test_turn_complete_log_schema.py``.

    Nullable derived fields use ``None`` (never ``0``) when their source
    anchor is unset, so a missing beat is distinguishable from a genuine
    zero-millisecond delta.
    """
    t = timings

    def _delta_ms(end_ns: int | None, start_ns: int | None) -> int | None:
        if end_ns is None or start_ns is None:
            return None
        return (end_ns - start_ns) // 1_000_000

    log.info(
        "turn.complete",
        # Latency decomposition (NFR35).
        stt_ms=_delta_ms(t.stt_done_ns, t.vad_end_ns),
        ttft_ms=_delta_ms(t.talker_first_token_ns, t.stt_done_ns),
        ttfb_ms=t.ttfb_ms,
        end_to_first_real_audio_ms=_delta_ms(t.real_first_frame_ns, t.vad_end_ns),
        # Opener accounting (NFR33 / NFR34).
        opener_source=t.opener_source,
        opener_bucket=t.opener_bucket,
        opener_duration_ms=t.opener_duration_ms,
        opener_onset_ms=_delta_ms(t.opener_first_frame_ns, t.vad_end_ns),
        dead_air_after_opener_ms=_delta_ms(t.real_first_frame_ns, t.opener_last_frame_ns),
        # Emphasis accounting (DR-004 / Story 6.3).
        emphasis_count_per_turn=t.emphasis_count,
        # Turn shape.
        routing=t.routing,
        had_tool_call=t.had_tool_call,
    )


def _record_opener_playback(timings: _TurnTimings | None, playback: OpenerPlayback) -> None:
    """Fold a fallback :class:`OpenerPlayback` into the turn's timings.

    Only writes if no opener has been recorded yet — the splitter-driven
    (``llm_tag``) path records inline and wins; the timer fallback is the
    second-choice source. At most one opener plays per turn (the events
    coordinate), so in practice there's no contention.
    """
    if timings is None or timings.opener_source is not None:
        return
    timings.opener_source = "timer_fallback"
    timings.opener_bucket = playback.bucket
    timings.opener_duration_ms = playback.duration_ms
    timings.opener_first_frame_ns = playback.first_frame_ns
    timings.opener_last_frame_ns = playback.last_frame_ns


async def _publish_segment_events(
    publisher: EventPublisher,
    cache: LastPublishedCache,
    segment: Segment,
    turn_id: UUID,
) -> None:
    """Publish a segment's embodiment events: speech_emotion + vocalizations.

    Mirrors the pipecat ``CartesiaSynthesisProcessor`` event construction
    (pipeline.py): emit the ``speech_emotion`` event FIRST (so the body
    sets the pose before any punctual burst), gated by the
    :class:`LastPublishedCache` dedup; then every ``vocalization`` in
    order (FR24 — vocalizations are never deduped). All events from one
    spoken turn share ``turn_id`` as their ``correlation_id``.

    Sentence segmentation already lives in the :class:`Segmenter`; this
    helper is split out as the pure publish step so it is unit-testable
    against the :class:`EventPublisher` Protocol without PyAudio / TTS.
    """
    payload = segment.speech_emotion_payload
    if payload is not None and cache.should_publish(payload):
        await publisher.publish_speech_emotion(
            SpeechEmotionEvent(payload=payload, correlation_id=turn_id)
        )
    for vocalization in segment.vocalization_payloads:
        await publisher.publish_vocalization(
            VocalizationEvent(payload=vocalization, correlation_id=turn_id)
        )


async def _publish_emphasis_events(
    publisher: EventPublisher,
    tts: TTSClient,
    segment: Segment,
    turn_id: UUID,
    seg_index: int,
) -> int:
    """Publish one ``emphasis`` vocalization per marked word (Story 6.3).

    Called AFTER a segment's ``synthesize`` generator drains — that's when
    :meth:`CartesiaClient.last_segment_timing` is populated with the
    per-word ``timestamps`` Cartesia reported for THIS segment (Story 6.1's
    WebSocket capture). For each word index the splitter recorded in
    ``segment.emphasis_word_indices``, we look up the carrier word's
    ``start_ms`` and publish a ``vocalization(tag="emphasis", ...)`` whose
    ``audio_frame_id`` is the word's audio anchor. The body owns the
    render-side anticipation lead (NFR5) relative to that anchor — the
    pipeline ships the anchor, not the motion (DR-002 / DR-004
    producer/consumer split).

    ``audio_frame_id`` shape: ``seg-{seg_index}-w-{start_ms}`` — a
    per-segment-unique, monotonic-within-turn identifier carrying the
    word's millisecond offset within the segment's audio. (Pre-Story-6.3
    vocalization events carried ``audio_frame_id=None`` on the half-duplex
    path; emphasis is the first event that populates it.)

    Defensive (AC #5):

    - ``timing is None`` (e.g. ``[tts] transport = "sse"`` — the SSE path
      drops timestamps) → log ``emphasis.no_timing`` DEBUG and skip the
      whole segment's emphasis publish. No crash.
    - ``index >= len(timing.words)`` (splitter / Cartesia disagreed on word
      count — should not happen) → log ``emphasis.index_mismatch`` WARN with
      both counts and skip just that one index. **Never raises** — a single
      misaligned word must not crash a turn (CLAUDE.md rule 4 covers
      external faults; this is a defensive intra-turn skip, not a swallow of
      ExternalServiceError).

    Returns:
        The number of ``emphasis`` events actually published (Story 6.4
        folds this into ``turn.complete.emphasis_count_per_turn``).
    """
    if not segment.emphasis_word_indices:
        return 0
    timing = tts.last_segment_timing()
    if timing is None:
        log.debug(
            "emphasis.no_timing",
            seg_index=seg_index,
            marked=len(segment.emphasis_word_indices),
        )
        return 0
    word_count = len(timing.words)
    published = 0
    for index in segment.emphasis_word_indices:
        if index >= word_count:
            log.warning(
                "emphasis.index_mismatch",
                seg_index=seg_index,
                requested_index=index,
                word_count=word_count,
            )
            continue
        word = timing.words[index]
        frame_id = f"seg-{seg_index}-w-{word.start_ms}"
        await publisher.publish_vocalization(
            VocalizationEvent(
                payload=VocalizationPayload(
                    tag="emphasis",
                    audio_frame_id=frame_id,
                    tts_supported=False,
                ),
                correlation_id=turn_id,
            )
        )
        published += 1
    return published


async def _stream_and_speak(
    pa: pyaudio.PyAudio,
    indices: Any,
    tts: TTSClient,
    talker: Talker,
    tool_registry: ToolRegistry,
    prompt: str,
    fsm: ActivityFSM,
    manifest: CachedAudioManifest,
    opener_selected: asyncio.Event,
    opener_already_playing: asyncio.Event,
    opener_fallback_task: asyncio.Task[OpenerPlayback | None] | None,
    recent_openers: deque[str],
    publisher: EventPublisher,
    segmenter: Segmenter,
    emotion_cache: LastPublishedCache,
    error_filler_enabled: bool,
    error_filler_bucket: OpenerBucket,
    history: list[dict[str, str]] | None = None,
    timings: _TurnTimings | None = None,
) -> tuple[str, list[ToolCall]]:
    """Stream Talker tokens; publish embodiment events + speak each segment.

    Token-streaming for snappier perceived latency (the canonical
    voice-AI win): we don't wait for the full LLM response before
    starting Cartesia. As tokens arrive, they feed the ``segmenter``,
    which parses the ``<emotion .../>``, ``<opener .../>`` (Story
    6.2), and ``[vocalization]`` tags and yields :class:`Segment`s on
    sentence / emotion boundaries. For each segment we publish its
    ``speech_emotion`` + ``vocalization`` events and then synthesize
    its (tag-cleaned) text — so the body reacts in lockstep with the
    spoken audio.

    Story 6.2 — opener + Cartesia overlap
    -------------------------------------

    Register an opener callback on the segmenter that fires when the
    LLM emits ``<opener bucket="..."/>``: cancel the fallback timer,
    claim the playing slot, and spawn a background cached-opener
    playback task. **The real-answer path does NOT await opener
    playback before opening its own output stream** — that was the
    Story-5.5 serialization tax DR-001 surfaced. The Cartesia
    network call is unblocked the moment the splitter has the first
    non-tag text; PyAudio's device-level serialization handles the
    audio ordering on the speaker.

    The bot's ``working → speaking`` FSM transition fires just before
    the FIRST segment's audio starts — that's when the user starts
    hearing anything. Subsequent segments play continuously into the
    same PyAudio output stream.

    Returns the accumulated full text + tool calls so the caller can
    update history and dispatch tools post-speech (half-duplex
    ordering — speech completes, then tool side effects fire).
    """
    # Fresh per-turn embodiment state: clear any buffered text / carried
    # emotion from the prior turn, reset the dedup cache, and bind a new
    # correlation id so THIS turn's speech_emotion + vocalization events
    # group together (mirrors SegmenterProcessor's per-turn reset).
    segmenter.reset()
    emotion_cache.reset()
    turn_id = uuid4()

    # Story 6.2: track all opener-related playback tasks so a clean
    # shutdown (and the tool-only-reply branch below) can await them
    # without the asyncio cleanup-warning noise.
    opener_play_tasks: list[asyncio.Task[None]] = []

    # The output stream is opened LAZILY on the first segment's first
    # chunk. Real-answer audio and opener audio open DIFFERENT
    # PyAudio output streams (the opener path is `play_cached`, which
    # owns its own stream); PyAudio serialises them on the device.
    out_stream: Any = None

    full_text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    fsm_speaking_fired = False
    # Story 6.3: monotonic per-turn index over SPOKEN segments — the stable
    # half of each emphasis event's ``audio_frame_id``. Only segments that
    # actually reach Cartesia advance it, so the id maps 1:1 to a synthesized
    # segment's word-timing table.
    spoken_segment_index = 0

    async def _play_opener_from_tag(bucket: OpenerBucket) -> None:
        """Splitter-driven opener playback. Picks + plays one take."""
        pick = pick_opener(manifest, bucket, recent_openers)
        if pick is None:
            # Bucket empty (the OpenersConfig validator should
            # prevent this in production). Defensive log + bail.
            log.warning("opener.no_pick_from_splitter", bucket=bucket)
            return
        log.info(
            "opener.splitter_picked",
            bucket=bucket,
            phrase=pick.phrase,
            duration_ms=pick.duration_ms,
        )
        recent_openers.append(pick.phrase_hash)
        # Story 6.4: this is the `llm_tag` opener source — it wins over the
        # timer fallback. Bracket playback with single-clock readings so
        # turn.complete can derive opener_onset_ms + dead_air_after_opener_ms.
        if timings is not None:
            timings.opener_source = "llm_tag"
            timings.opener_bucket = bucket
            timings.opener_duration_ms = pick.duration_ms
            timings.opener_first_frame_ns = time.time_ns()
        await play_cached(pa, cast(int, indices.output_index), Path(pick.path))
        if timings is not None:
            timings.opener_last_frame_ns = time.time_ns()

    def _on_opener_selected(bucket: OpenerBucket) -> None:
        """Sync callback the segmenter invokes on every OpenerTagEvent.

        - Cancel the fallback timer (set ``opener_selected``).
        - Race-window check: if the fallback already started playing
          (``opener_already_playing.is_set()``), silently drop the
          tag so we don't double-play.
        - Claim the slot and spawn the background playback task.
        """
        if opener_already_playing.is_set():
            log.debug("opener.late_tag_or_fallback_won", bucket=bucket)
            opener_selected.set()  # still cancel the timer if pending
            return
        opener_selected.set()
        opener_already_playing.set()
        opener_play_tasks.append(asyncio.create_task(_play_opener_from_tag(bucket)))

    # Story 6.2: rebind the segmenter's opener callback every turn.
    # The segmenter is shared across turns (its `.reset()` is the
    # per-turn boundary), so we have to overwrite the callback rather
    # than construct a new segmenter — keeps the state machine's
    # implementation detail private.
    segmenter.opener_callback = _on_opener_selected

    async def _error_filler_watcher() -> None:
        """Cover transient-error retry backoff with a cached filler (2026-05-31).

        The openai SDK retries 429 / 5xx internally — a single
        ``complete_with_tools_streaming`` call can sit silent for several
        seconds during the backoff (a ~9 s Groq 429 stall is what
        prompted this). The Talker's httpx hook sets
        ``talker.transient_error_event`` on each such response; this
        watcher consumes it and plays a take from ``error_filler_bucket``
        so the user hears "still working on it" instead of dead air.

        Gating + lifecycle:

        - Only fires while real-answer audio hasn't started
          (``not fsm_speaking_fired``). Once the bot is actually
          speaking, the gap is filled — drop the signal.
        - Re-arms after each play (the event is cleared on consume), so a
          multi-retry stall gets successive fillers rather than one.
        - Once it plays, it CLAIMS the opener slot
          (``opener_already_playing`` / ``opener_selected``) so the
          LLM's own ``<opener .../>`` tag — which arrives once the
          stalled stream finally resumes — doesn't stack a second opener
          on top of the filler we already played.
        - Runs as a background task for the life of the stream; the
          ``finally`` below cancels it.
        """
        while True:
            await talker.transient_error_event.wait()
            talker.transient_error_event.clear()
            # Real answer already underway → the gap is covered; ignore.
            if fsm_speaking_fired:
                continue
            pick = pick_opener(manifest, error_filler_bucket, recent_openers)
            if pick is None:
                # Bucket empty — the OpenersConfig validator should make
                # this unreachable in production. Defensive log + bail.
                log.warning("error_filler.no_pick", bucket=error_filler_bucket)
                continue
            recent_openers.append(pick.phrase_hash)
            # Claim the opener slot so the splitter-driven opener and the
            # timer fallback both stand down — we've covered the beat.
            opener_already_playing.set()
            opener_selected.set()
            log.info(
                "error_filler.play",
                bucket=error_filler_bucket,
                phrase=pick.phrase,
                duration_ms=pick.duration_ms,
            )
            await play_cached(pa, cast(int, indices.output_index), Path(pick.path))

    async def _speak_segment(segment: Segment) -> None:
        """Publish a segment's events, then synthesize its text via Cartesia."""
        nonlocal fsm_speaking_fired, out_stream, spoken_segment_index
        # Publish embodiment events FIRST — they lead the audio by the
        # TTS synthesis latency (consumer side of NFR5). Fires even for
        # a text-less, vocalization-only segment (e.g. a bare ``[nod]``)
        # so a silent gesture still reaches the body.
        await _publish_segment_events(publisher, emotion_cache, segment, turn_id)

        text = segment.text
        if not _is_speakable(text):
            # No speakable audio — empty, whitespace, or punctuation-only
            # (e.g. a stripped ``[nod]``, or a ``"."`` split out of
            # ``"Wait..."``). Cartesia 400s on a punctuation-only
            # transcript. The segment's events (if any) were published
            # above; just don't open the stream or fire the speaking
            # transition for an eventless/audio-less beat.
            return

        # Fire the FSM transition right before the first segment's first
        # chunk lands — the "user hears the bot" moment.
        #
        # Story 6.2 (overlap deletion): we DO NOT await the opener
        # fallback task here. The Cartesia synth call fires
        # immediately; opener playback (if any) and the real-answer
        # playback land on the same physical speaker — PyAudio's
        # `stream.write` blocks until the OS buffer has room, which
        # serialises them naturally. The Story 5.5 serialization tax
        # (await filler_task before opening output stream) is gone;
        # that's the whole point of this story.
        if not fsm_speaking_fired:
            await fsm.on_first_audio_frame()
            fsm_speaking_fired = True
            with suppress_native_stderr():
                out_stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=_SAMPLE_RATE,
                    output=True,
                    output_device_index=indices.output_index,
                )
        log.info("tts.sentence_speak", text=text.strip())
        # Story 6.4: measure TTFB (synth-request-sent → first audio frame
        # received) and stamp the real-answer first-frame anchor. Both land
        # on the FIRST chunk of the FIRST spoken segment of the turn
        # (guarded by ``real_first_frame_ns is None``); subsequent segments'
        # TTFBs are amortised by streaming and not carried.
        synth_request_ns = time.time_ns()
        first_chunk = True
        async for chunk in tts.synthesize(text):
            if first_chunk:
                first_chunk = False
                if timings is not None and timings.real_first_frame_ns is None:
                    now_ns = time.time_ns()
                    timings.real_first_frame_ns = now_ns
                    timings.ttfb_ms = (now_ns - synth_request_ns) // 1_000_000
            await asyncio.to_thread(out_stream.write, chunk)

        # Story 6.3 — emphasis join. The synthesize generator has drained,
        # so ``tts.last_segment_timing()`` now holds THIS segment's per-word
        # timestamps. Publish one ``emphasis`` event per marked word,
        # anchored to its audio offset. Done after the write loop because
        # the timing is only reliably available post-drain; the body
        # anchors its motion to the carrier word's ``audio_frame_id``.
        published = await _publish_emphasis_events(
            publisher, tts, segment, turn_id, spoken_segment_index
        )
        if timings is not None:
            # Story 6.4: emphasis_count is per-turn — accumulate across all
            # of the turn's segments.
            timings.emphasis_count += published
        spoken_segment_index += 1

    # Error-filler watcher (2026-05-31). Clear any stale signal left by a
    # prior turn, then run the watcher concurrently with the LLM stream so
    # it can play over a retry backoff while the `async for` below is still
    # awaiting its first event. Skipped entirely when disabled — the signal
    # then goes unconsumed (harmless) and the turn keeps its bare-silence
    # behavior on retries.
    error_filler_task: asyncio.Task[None] | None = None
    if error_filler_enabled:
        talker.transient_error_event.clear()
        error_filler_task = asyncio.create_task(_error_filler_watcher())

    try:
        async for event in talker.complete_with_tools_streaming(
            prompt,
            tool_registry,
            history=history,
        ):
            if isinstance(event, TalkerTextDelta):
                # Story 6.4: TTFT (best-effort) — stamp the first non-empty
                # text delta. Providers that buffer produce a TTFT ≈
                # time-to-completion (accurate, if not useful); providers
                # with no token streaming would leave this None.
                if timings is not None and timings.talker_first_token_ns is None and event.text:
                    timings.talker_first_token_ns = time.time_ns()
                # Accumulate the RAW reply (tags included) for history —
                # the LLM sees its own tag format on subsequent turns.
                full_text_parts.append(event.text)
                # Feed the delta through the segmenter; publish + speak
                # each segment it yields.
                for segment in segmenter.consume(event.text):
                    await _speak_segment(segment)
            else:
                # Discriminated union: only TalkerStreamEnd remains
                # after the TalkerTextDelta branch above. Pyright
                # narrows this without the explicit isinstance check.
                tool_calls = list(event.tool_calls)

        # Stream finished. Drain the segmenter's final partial segment
        # (the last sentence often lacks a trailing terminator/space).
        for segment in segmenter.flush():
            await _speak_segment(segment)

        # If the LLM produced no spoken text (tool-call-only reply, or
        # a reply that was all stripped tags), we never fired
        # ``on_first_audio_frame``. Cancel the opener fallback (the
        # tool-only path doesn't want a stranded "hmm" with no follow-
        # up speech) and drain any in-flight opener playback before
        # the caller's ``on_last_audio_frame`` lands. Story 6.2
        # parallel to the Story 5.5 edge-case handling.
        if not fsm_speaking_fired:
            opener_selected.set()
            # Empty-reply filler (2026-05-31). A FULLY empty reply — no
            # speakable text AND no tool call — is the gpt-oss
            # reasoning-overflow failure: the model gave us nothing and
            # the turn would otherwise be dead silence (the "it stopped
            # guessing and went silent" symptom). Cover it with a cached
            # take so the bot at least acknowledges. Skipped for
            # tool-call-only replies (a goodbye/mood action follows — a
            # stranded "hmm" would be wrong) and when the error-filler
            # watcher already claimed the slot (a 429 this turn).
            if error_filler_enabled and not tool_calls and not opener_already_playing.is_set():
                pick = pick_opener(manifest, error_filler_bucket, recent_openers)
                if pick is not None:
                    recent_openers.append(pick.phrase_hash)
                    opener_already_playing.set()
                    log.info(
                        "empty_reply_filler.play",
                        bucket=error_filler_bucket,
                        phrase=pick.phrase,
                        duration_ms=pick.duration_ms,
                    )
                    await fsm.on_first_audio_frame()
                    fsm_speaking_fired = True
                    await play_cached(pa, cast(int, indices.output_index), Path(pick.path))
            if not fsm_speaking_fired:
                await fsm.on_first_audio_frame()

        # Always drain the opener fallback + any splitter-driven
        # opener playback tasks before returning, so cleanup is
        # deterministic and we don't leak Task warnings.
        # None when the timer fallback is disabled — nothing to drain.
        fallback_playback = await opener_fallback_task if opener_fallback_task is not None else None
        # Story 6.4: if the timer fallback played (rather than the
        # splitter-driven path), fold its timing into the turn rollup.
        # `_record_opener_playback` no-ops when an llm_tag opener already
        # recorded — the splitter path wins.
        if fallback_playback is not None:
            _record_opener_playback(timings, fallback_playback)
        for play_task in opener_play_tasks:
            await play_task

        # Trailing drain — let the OS finish playing buffered audio.
        # Only if we actually opened the stream (out_stream is None
        # for tool-only replies).
        if out_stream is not None:
            await asyncio.sleep(_AUDIO_DRAIN_TAIL_MS / 1000)
    finally:
        # Tear down the error-filler watcher. It loops forever on the
        # transient-error event, so it's still pending on every clean
        # turn — cancel and await it (swallowing the CancelledError) so
        # no orphaned task leaks. A filler mid-`play_cached` is cut here,
        # but by this point the real answer (if any) has already drained.
        if error_filler_task is not None:
            error_filler_task.cancel()
            with suppress(asyncio.CancelledError):
                await error_filler_task
        if out_stream is not None:
            out_stream.stop_stream()
            out_stream.close()
        # Best-effort callback unbind — keep the segmenter from
        # holding a closure over this turn's state past the function
        # boundary. The next turn rebinds.
        segmenter.opener_callback = None

    full_text = "".join(full_text_parts)
    return full_text, tool_calls


# Story 5.5 removed ``_speak`` — the only callers were the three
# deterministic-text surfaces (greeting/goodbye/clarification) which
# now play from cached WAVs via ``audio.cached.play_cached``. Real
# Talker replies stream per-segment through ``_speak_segment``
# (defined inside ``_stream_and_speak`` above) — the streaming path
# was always the production surface for conversational replies.
