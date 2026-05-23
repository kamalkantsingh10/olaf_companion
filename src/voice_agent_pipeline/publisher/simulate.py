"""``publisher.simulate`` — fire a scripted stream of mock voice-agent
events at the four ``/olaf/*`` topics so a running body (the
``expression_engine`` subscriber) can be watched reacting to each one.

Run via ``just simulate-agent`` (see the justfile recipe).

This is a **pure producer** — unlike ``publisher.smoke`` (which has its
own in-process witness and exits in ~1.5 s), this stays alive and emits a
realistic, varied sequence with a delay between messages so you can
monitor the receiver and see each event land. It mimics one OLAF
"session": boot → sleep → wake → listen → think → speak (with emotions
and vocalizations) → delegate → goodbye → sleep.

The sequence deliberately covers the full vocabulary so every renderer
path on the body gets exercised:

- all 7 activity states + both working submodes (``thinking`` /
  ``delegating``),
- several moods (slow base layer),
- several canonical speech emotions (per-segment overlay),
- all 6 vocalizations (audio bursts ``laughter``/``sigh``/``gasp``/
  ``clears_throat`` and gesture cues ``nod``/``shake``).

It uses the production :class:`Ros2EventPublisher` on the configured
``[publisher].dds_domain_id`` (default from ``setup.toml``), so this is
the same wire path the real pipeline uses — only the *content* is canned.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tomllib
from pathlib import Path
from uuid import uuid4

from voice_agent_pipeline.config.setup import PublisherConfig
from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher
from voice_agent_pipeline.schemas.activity_event import ActivityEvent, ActivityPayload
from voice_agent_pipeline.schemas.envelope import EventEnvelope
from voice_agent_pipeline.schemas.mood_event import MoodEvent, MoodPayload
from voice_agent_pipeline.schemas.speech_emotion_event import (
    SpeechEmotionEvent,
    SpeechEmotionPayload,
)
from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
    VocalizationPayload,
)

#: Grace period after connect for the body to be discovered before the
#: first (non-latched) volatile event is sent — those have no replay.
_DISCOVERY_SECONDS = 2.0

#: Grace period after the last event before teardown, so the body has
#: time to render it (and the final latched state lingers briefly).
_TEARDOWN_SECONDS = 2.0


def _build_sequence() -> list[EventEnvelope]:
    """Build one believable OLAF session as an ordered event list.

    correlation_ids group events into logical turns the way the real
    pipeline would (a watcher can see which speech emotions / gestures
    belong to which spoken turn).
    """
    boot = uuid4()
    turn1 = uuid4()
    turn2 = uuid4()
    bye = uuid4()

    return [
        # ── Boot: comes up, holds a sleepy mood, settles into sleep ──
        ActivityEvent(correlation_id=boot, payload=ActivityPayload(state="starting")),
        MoodEvent(correlation_id=boot, payload=MoodPayload(mood="sleepy", reason="startup")),
        ActivityEvent(
            correlation_id=boot,
            payload=ActivityPayload(
                state="sleeping", from_state="starting", transition_reason="boot_complete"
            ),
        ),
        # ── Wake word: stirs, gets curious, starts listening ──
        ActivityEvent(
            correlation_id=turn1,
            payload=ActivityPayload(
                state="waking", from_state="sleeping", transition_reason="wake_word"
            ),
        ),
        MoodEvent(correlation_id=turn1, payload=MoodPayload(mood="curious", reason="woke_up")),
        ActivityEvent(
            correlation_id=turn1,
            payload=ActivityPayload(
                state="listening", from_state="waking", transition_reason="awake"
            ),
        ),
        # ── Turn 1: hears a question, thinks (fast path), then answers ──
        ActivityEvent(
            correlation_id=turn1,
            payload=ActivityPayload(
                state="working",
                working_submode="thinking",
                from_state="listening",
                transition_reason="received_question",
            ),
        ),
        SpeechEmotionEvent(
            correlation_id=turn1,
            payload=SpeechEmotionPayload(
                emotion="curious",
                source_tag="llm:sentiment",
                raw_tag="curious",
                resolved_fallback=None,
            ),
        ),
        ActivityEvent(
            correlation_id=turn1,
            payload=ActivityPayload(
                state="speaking", from_state="working", transition_reason="answer_ready"
            ),
        ),
        SpeechEmotionEvent(
            correlation_id=turn1,
            payload=SpeechEmotionPayload(
                emotion="excited", source_tag="llm", raw_tag="excited", resolved_fallback=None
            ),
        ),
        VocalizationEvent(
            correlation_id=turn1, payload=VocalizationPayload(tag="laughter", tts_supported=True)
        ),
        SpeechEmotionEvent(
            correlation_id=turn1,
            payload=SpeechEmotionPayload(
                emotion="content", source_tag="llm", raw_tag="content", resolved_fallback=None
            ),
        ),
        VocalizationEvent(
            correlation_id=turn1, payload=VocalizationPayload(tag="nod", tts_supported=False)
        ),
        MoodEvent(correlation_id=turn1, payload=MoodPayload(mood="happy", reason="good_chat")),
        ActivityEvent(
            correlation_id=turn1,
            payload=ActivityPayload(
                state="listening", from_state="speaking", transition_reason="done_speaking"
            ),
        ),
        # ── Turn 2: a harder ask — delegates (slow path), reacts more ──
        ActivityEvent(
            correlation_id=turn2,
            payload=ActivityPayload(
                state="working",
                working_submode="delegating",
                from_state="listening",
                transition_reason="complex_task",
            ),
        ),
        SpeechEmotionEvent(
            correlation_id=turn2,
            payload=SpeechEmotionPayload(
                emotion="surprised", source_tag="llm", raw_tag="surprised", resolved_fallback=None
            ),
        ),
        ActivityEvent(
            correlation_id=turn2,
            payload=ActivityPayload(
                state="speaking", from_state="working", transition_reason="result_ready"
            ),
        ),
        VocalizationEvent(
            correlation_id=turn2, payload=VocalizationPayload(tag="gasp", tts_supported=True)
        ),
        SpeechEmotionEvent(
            correlation_id=turn2,
            payload=SpeechEmotionPayload(
                emotion="sympathetic",
                source_tag="llm",
                raw_tag="sympathetic",
                resolved_fallback=None,
            ),
        ),
        VocalizationEvent(
            correlation_id=turn2, payload=VocalizationPayload(tag="sigh", tts_supported=True)
        ),
        SpeechEmotionEvent(
            correlation_id=turn2,
            payload=SpeechEmotionPayload(
                emotion="neutral", source_tag="llm", raw_tag="neutral", resolved_fallback=None
            ),
        ),
        VocalizationEvent(
            correlation_id=turn2,
            payload=VocalizationPayload(tag="clears_throat", tts_supported=True),
        ),
        MoodEvent(
            correlation_id=turn2, payload=MoodPayload(mood="thoughtful", reason="deep_topic")
        ),
        ActivityEvent(
            correlation_id=turn2,
            payload=ActivityPayload(
                state="listening", from_state="speaking", transition_reason="done_speaking"
            ),
        ),
        # ── Goodbye: a head shake, winds down to sleep ──
        VocalizationEvent(
            correlation_id=bye, payload=VocalizationPayload(tag="shake", tts_supported=False)
        ),
        ActivityEvent(
            correlation_id=bye,
            payload=ActivityPayload(
                state="going_to_sleep", from_state="listening", transition_reason="goodbye"
            ),
        ),
        MoodEvent(correlation_id=bye, payload=MoodPayload(mood="sleepy", reason="winding_down")),
        ActivityEvent(
            correlation_id=bye,
            payload=ActivityPayload(
                state="sleeping", from_state="going_to_sleep", transition_reason="idle_timeout"
            ),
        ),
    ]


def _describe(event: EventEnvelope) -> str:
    """One-line human label for the console as each event is sent.

    Each branch reads ``event.payload`` *after* the ``isinstance`` narrow,
    so the payload resolves to the topic-specific model (the envelope's
    base ``payload`` is just ``BaseModel``).
    """
    if isinstance(event, MoodEvent):
        return f"mood           → {event.payload.mood:<12} ({event.payload.reason})"
    if isinstance(event, ActivityEvent):
        ap = event.payload
        sub = f"[{ap.working_submode}]" if ap.working_submode else ""
        frm = f"  (from {ap.from_state})" if ap.from_state else ""
        return f"activity       → {ap.state}{sub}{frm}"
    if isinstance(event, SpeechEmotionEvent):
        return f"speech_emotion → {event.payload.emotion}"
    if isinstance(event, VocalizationEvent):
        vp = event.payload
        kind = "audio" if vp.tts_supported else "gesture"
        return f"vocalization   → {vp.tag:<13} ({kind})"
    return f"event          → {type(event).__name__}"


async def _publish_one(publisher: Ros2EventPublisher, event: EventEnvelope) -> None:
    """Dispatch one event to the matching topic-specific publish method."""
    if isinstance(event, MoodEvent):
        await publisher.publish_mood(event)
    elif isinstance(event, ActivityEvent):
        await publisher.publish_activity(event)
    elif isinstance(event, SpeechEmotionEvent):
        await publisher.publish_speech_emotion(event)
    elif isinstance(event, VocalizationEvent):
        await publisher.publish_vocalization(event)


def _load_publisher_config(toml_path: Path, domain_override: int | None) -> PublisherConfig:
    """Build ``PublisherConfig`` from ``setup.toml``'s ``[publisher]`` block.

    DDS-only — needs neither ``.env`` nor credentials. ``--domain`` wins
    over the file value; missing file falls back to model defaults.
    """
    if toml_path.exists():
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
        config = PublisherConfig(**data.get("publisher", {}))
    else:
        print(f"⚠️  {toml_path} not found — using PublisherConfig defaults.")
        config = PublisherConfig()
    if domain_override is not None:
        config = config.model_copy(update={"dds_domain_id": domain_override})
    return config


async def _run(config: PublisherConfig, delay: float, loop: bool) -> None:
    print("── OLAF voice-agent simulator ──")
    print(f"  domain_id : {config.dds_domain_id}")
    print(
        "  topics    : "
        f"{config.topics.mood}, {config.topics.activity}, "
        f"{config.topics.speech_emotion}, {config.topics.vocalization}"
    )
    print(f"  delay     : {delay}s between events    loop: {loop}")

    publisher = Ros2EventPublisher(config)
    await publisher.connect()
    print(f"  connected; waiting {_DISCOVERY_SECONDS}s for the body to be discovered ...\n")
    await asyncio.sleep(_DISCOVERY_SECONDS)

    sequence = _build_sequence()
    try:
        pass_num = 0
        while True:
            pass_num += 1
            if loop:
                print(f"── pass {pass_num} ──")
            for i, event in enumerate(sequence, 1):
                await _publish_one(publisher, event)
                print(f"  [{i:>2}/{len(sequence)}] {_describe(event)}", flush=True)
                await asyncio.sleep(delay)
            if not loop:
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n  interrupted — tearing down.")
    finally:
        await asyncio.sleep(_TEARDOWN_SECONDS)
        await publisher.disconnect()
        print("  done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m voice_agent_pipeline.publisher.simulate",
        description="Stream a scripted sequence of mock voice-agent events to the /olaf topics.",
    )
    parser.add_argument(
        "--delay", type=float, default=1.0, help="Seconds between events (default: 1.0)."
    )
    parser.add_argument("--loop", action="store_true", help="Repeat the sequence until Ctrl-C.")
    parser.add_argument(
        "--domain",
        type=int,
        default=None,
        help="Override the DDS domain id (defaults to setup.toml [publisher].dds_domain_id).",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("setup.toml"), help="Path to setup.toml."
    )
    args = parser.parse_args()

    config = _load_publisher_config(args.config, args.domain)
    try:
        asyncio.run(_run(config, delay=args.delay, loop=args.loop))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
