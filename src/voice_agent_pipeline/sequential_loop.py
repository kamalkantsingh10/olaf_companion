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
from pathlib import Path
from typing import Any, cast
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
from voice_agent_pipeline.audio.filler import maybe_play_filler
from voice_agent_pipeline.config.expression_map import load_from_path
from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.mood.controller import MoodController
from voice_agent_pipeline.mood.state import MoodState
from voice_agent_pipeline.publisher import build_publisher
from voice_agent_pipeline.publisher.interface import EventPublisher
from voice_agent_pipeline.schemas.speech_emotion_event import SpeechEmotionEvent
from voice_agent_pipeline.schemas.vocalization_event import VocalizationEvent
from voice_agent_pipeline.splitter.mapping import LastPublishedCache
from voice_agent_pipeline.splitter.segmenter import Segment, Segmenter
from voice_agent_pipeline.stt import build_stt_backend
from voice_agent_pipeline.tts.cartesia import CartesiaClient
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

        # Cartesia TTS client. Streaming SSE happens per ``speak`` call.
        tts = CartesiaClient(config.tts, config.cartesia_api_key)

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

            # Story 5.5: ring buffer of recently-played filler hashes.
            # Persists across turns within a single wake session so the
            # last-N suppression actually suppresses across turns.
            # maxlen = max_consecutive_repeat + 1 → at minimum the
            # immediately-previous filler is excluded.
            recent_fillers: deque[str] = deque(
                maxlen=config.filler.max_consecutive_repeat + 1,
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
                    log.info("sequential_loop.no_speech_retry")
                    continue

                # FSM transitions: waking → listening → working[thinking]
                # on the first iteration; listening → working[thinking]
                # on subsequent iterations (on_speech_started is a
                # no-op when already in listening).
                await fsm.on_speech_started()
                await fsm.on_speech_ended()

                # Story 5.5: spawn the filler task immediately after
                # VAD end-of-speech. The task sleeps up to
                # `min_pause_ms`; if `audio_started` fires before then
                # (fast turn), it returns without playing. The
                # downstream paths (clarification + real Talker reply)
                # both signal `audio_started` and `await filler_task`
                # before opening their output streams — that's how we
                # serialize filler vs real audio without sharing a
                # stream.
                audio_started = asyncio.Event()
                filler_task = asyncio.create_task(
                    maybe_play_filler(
                        pa=pa,
                        indices=indices,
                        mood=mood_state.current,
                        manifest=manifest,
                        min_pause_ms=config.filler.min_pause_ms,
                        audio_started=audio_started,
                        recent=recent_fillers,
                    ),
                )

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
                # No streaming needed; the phrase is already a single
                # short sentence. Clarification turns DO go into
                # history (the bot said "say again?", the user's
                # next turn should make sense in that context).
                if stt_result.confidence < config.stt.low_confidence_threshold:
                    clarification_text = random.choice(  # noqa: S311
                        config.stt.clarification_prompts,
                    )
                    log.info("clarification.picked", text=clarification_text)
                    # Story 5.5: signal "real audio is about to start"
                    # to the filler task, then await it. If the filler
                    # hadn't started yet, it returns immediately; if
                    # it had, we wait for it to finish playing so the
                    # output stream is free before we open ours.
                    audio_started.set()
                    await filler_task
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
                    audio_started=audio_started,
                    filler_task=filler_task,
                    publisher=event_publisher,
                    segmenter=segmenter,
                    emotion_cache=emotion_cache,
                    history=conversation_history,
                )

                # Append this turn to the history BEFORE dispatching
                # tools / firing on_last_audio_frame. Order: user
                # turn, then assistant turn — even if assistant text
                # is empty (tool-call-only reply), append an empty
                # assistant message so the LLM sees the alternation.
                conversation_history.append(
                    {"role": "user", "content": stt_result.text},
                )
                conversation_history.append(
                    {"role": "assistant", "content": full_text},
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


async def _stream_and_speak(
    pa: pyaudio.PyAudio,
    indices: Any,
    tts: CartesiaClient,
    talker: Talker,
    tool_registry: ToolRegistry,
    prompt: str,
    fsm: ActivityFSM,
    audio_started: asyncio.Event,
    filler_task: asyncio.Task[None],
    publisher: EventPublisher,
    segmenter: Segmenter,
    emotion_cache: LastPublishedCache,
    history: list[dict[str, str]] | None = None,
) -> tuple[str, list[ToolCall]]:
    """Stream Talker tokens; publish embodiment events + speak each segment.

    Token-streaming for snappier perceived latency (the canonical
    voice-AI win): we don't wait for the full LLM response before
    starting Cartesia. As tokens arrive, they feed the ``segmenter``,
    which parses the ``<emotion .../>`` and ``[vocalization]`` tags and
    yields :class:`Segment`s on sentence / emotion boundaries. For each
    segment we publish its ``speech_emotion`` + ``vocalization`` events
    and then synthesize its (tag-cleaned) text — so the body reacts in
    lockstep with the spoken audio. (The pipecat assembly did this in
    ``CartesiaSynthesisProcessor`` + ``_PrePublishProcessor``; the
    half-duplex migration had dropped it.)

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

    # Story 5.5: the output stream is opened LAZILY on the first
    # segment's first chunk. This serializes against any concurrent
    # filler audio: we signal `audio_started` + await `filler_task`
    # right before opening, so the filler stream is closed before ours.
    out_stream: Any = None

    full_text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    fsm_speaking_fired = False

    async def _speak_segment(segment: Segment) -> None:
        """Publish a segment's events, then synthesize its text via Cartesia."""
        nonlocal fsm_speaking_fired, out_stream
        # Publish embodiment events FIRST — they lead the audio by the
        # TTS synthesis latency (consumer side of NFR5). Fires even for
        # a text-less, vocalization-only segment (e.g. a bare ``[nod]``)
        # so a silent gesture still reaches the body.
        await _publish_segment_events(publisher, emotion_cache, segment, turn_id)

        text = segment.text
        if not text.strip():
            # No spoken audio for this segment (its events, if any, were
            # published above). Don't open the stream or fire the
            # speaking transition just for an eventless beat.
            return

        # Fire the FSM transition right before the first segment's first
        # chunk lands — the "user hears the bot" moment.
        if not fsm_speaking_fired:
            # Story 5.5: signal the filler task that real audio is about
            # to start; await it so its output stream is closed before
            # we open ours.
            audio_started.set()
            await filler_task
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
        async for chunk in tts.synthesize(text):
            await asyncio.to_thread(out_stream.write, chunk)

    try:
        async for event in talker.complete_with_tools_streaming(
            prompt,
            tool_registry,
            history=history,
        ):
            if isinstance(event, TalkerTextDelta):
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

        # If the LLM produced no spoken text (tool-call-only reply, or a
        # reply that was all stripped tags), we never fired
        # ``on_first_audio_frame`` and never opened the output stream.
        # Fire the FSM transition + await the filler task (in case it's
        # still running) so the caller's ``on_last_audio_frame`` lands
        # cleanly. Story 5.5: the filler await handles the "Talker
        # emitted only tools, but a filler is mid-playback" edge case.
        if not fsm_speaking_fired:
            audio_started.set()
            await filler_task
            await fsm.on_first_audio_frame()

        # Trailing drain — let the OS finish playing buffered audio.
        # Only if we actually opened the stream (out_stream is None
        # for tool-only replies).
        if out_stream is not None:
            await asyncio.sleep(_AUDIO_DRAIN_TAIL_MS / 1000)
    finally:
        if out_stream is not None:
            out_stream.stop_stream()
            out_stream.close()

    full_text = "".join(full_text_parts)
    return full_text, tool_calls


# Story 5.5 removed ``_speak`` — the only callers were the three
# deterministic-text surfaces (greeting/goodbye/clarification) which
# now play from cached WAVs via ``audio.cached.play_cached``. Real
# Talker replies stream per-segment through ``_speak_segment``
# (defined inside ``_stream_and_speak`` above) — the streaming path
# was always the production surface for conversational replies.
