"""``publisher.smoke`` — self-contained ROS 2 / DDS publish smoke test.

Run via ``just smoke-publisher`` (see the justfile recipe). Answers one
question without needing the OLAF body, real hardware, or any API keys:

    *Does our production publisher emit all four typed events, with the
    contract QoS, on the configured DDS domain, such that a subscriber
    using the body's exact QoS receives them?*

How it works
------------

A single process, one rclpy context:

1. Build a :class:`~voice_agent_pipeline.config.setup.PublisherConfig`
   from ``setup.toml``'s ``[publisher]`` block — so the test exercises
   the **real** configured ``dds_domain_id`` and topic names (no ``.env``
   / secrets required; this is a DDS-only test).
2. Stand up the production
   :class:`~voice_agent_pipeline.publisher.ros2.Ros2EventPublisher`
   (which pins ``rclpy.init(domain_id=...)`` from that config).
3. Create an in-process *witness* subscriber on the same context, using
   the publisher's own QoS profiles
   (:func:`~voice_agent_pipeline.publisher.ros2.build_qos_profiles`) —
   identical to the body's ``contract/INTERFACE.md`` QoS, so a QoS
   mismatch would surface as "nothing received".
4. Spin briefly for DDS discovery, publish one representative event per
   topic (all sharing one ``correlation_id``, mimicking a turn), then
   spin until all four arrive or the timeout elapses.

Exit code is ``0`` iff all four topics were received, ``1`` otherwise —
so the ``just`` recipe / CI can gate on it.

This deliberately does NOT run the body node (that lives in a separate
repo). It proves our *emission* side end-to-end over real DDS; pairing
it with the live body is a separate, hardware-dependent exercise.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import tomllib
from pathlib import Path
from uuid import uuid4

# Importing publisher.ros2 pulls in rclpy at module import — intentional;
# this module is a ROS 2 tool and is never imported by the core pipeline.
from rclpy.node import Node
from std_msgs.msg import String  # type: ignore[import-not-found,import-untyped]

from voice_agent_pipeline.config.setup import PublisherConfig
from voice_agent_pipeline.publisher.ros2 import Ros2EventPublisher, build_qos_profiles
from voice_agent_pipeline.schemas.activity_event import ActivityEvent, ActivityPayload
from voice_agent_pipeline.schemas.mood_event import MoodEvent, MoodPayload
from voice_agent_pipeline.schemas.speech_emotion_event import (
    SpeechEmotionEvent,
    SpeechEmotionPayload,
)
from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
    VocalizationPayload,
)

#: Discovery grace period before the (non-latched) volatile events are
#: published — gives the witness's subscriptions time to match the
#: publisher so the momentary events aren't missed (they have no replay).
_DISCOVERY_SECONDS = 1.5


def _load_publisher_config(toml_path: Path, domain_override: int | None) -> PublisherConfig:
    """Build ``PublisherConfig`` from ``setup.toml``'s ``[publisher]`` block.

    Reads only the ``[publisher]`` table (not the whole ``SetupConfig``)
    so the smoke test needs neither ``.env`` nor any API credentials.
    Falls back to model defaults if the file is absent. A ``--domain``
    override wins over the file value.
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


def _build_witness(config: PublisherConfig) -> tuple[Node, dict[str, str]]:
    """Create the witness node subscribed to all four topics.

    Uses the publisher's OWN QoS profiles (the contract QoS) so a
    successful receive proves QoS compatibility, topic-name match, and
    domain match in one shot. Returns the node plus the dict that its
    callbacks populate ``{topic_key: received_json}``.
    """
    node = Node("olaf_smoke_witness")
    qos_profiles = build_qos_profiles()
    received: dict[str, str] = {}

    # topic_key → configured topic name (operator-tunable in setup.toml).
    topic_names = {
        "mood": config.topics.mood,
        "activity": config.topics.activity,
        "speech_emotion": config.topics.speech_emotion,
        "vocalization": config.topics.vocalization,
    }

    def _make_cb(topic_key: str):
        def _cb(msg: String) -> None:
            # std_msgs ships no stubs; .data is str at runtime. Mirror
            # ros2.py's targeted-suppression style rather than a bare ignore.
            received[topic_key] = str(msg.data)  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
            print(f"  ✓ received on {topic_key:<15} {topic_names[topic_key]}")

        return _cb

    for topic_key, name in topic_names.items():
        node.create_subscription(  # pyright: ignore[reportUnknownMemberType]
            String, name, _make_cb(topic_key), qos_profiles[topic_key]
        )
    return node, received


async def _run(config: PublisherConfig, recv_timeout: float) -> int:
    """Connect, witness, publish, await receipt. Returns a process exit code."""
    print("── OLAF publisher smoke test ──")
    print(f"  domain_id : {config.dds_domain_id}")
    print(f"  adapter   : {config.adapter}")
    print(
        "  topics    : "
        f"{config.topics.mood}, {config.topics.activity}, "
        f"{config.topics.speech_emotion}, {config.topics.vocalization}"
    )

    publisher = Ros2EventPublisher(config)
    await publisher.connect()  # rclpy.init(domain_id=...) happens here.

    # Witness shares the now-initialized default context.
    witness, received = _build_witness(config)
    print("  witness   : subscribed to 4 topics (contract QoS)")

    # Let DDS discovery match the witness before we emit volatile events.
    deadline = time.monotonic() + _DISCOVERY_SECONDS
    while time.monotonic() < deadline:
        await asyncio.to_thread(_spin_once, witness)

    # One turn's worth of representative, contract-valid events.
    cid = uuid4()
    print(f"\n  publishing 4 events (correlation_id={cid}) ...")
    await publisher.publish_mood(
        MoodEvent(correlation_id=cid, payload=MoodPayload(mood="curious", reason="smoke test"))
    )
    await publisher.publish_activity(
        ActivityEvent(correlation_id=cid, payload=ActivityPayload(state="starting"))
    )
    await publisher.publish_speech_emotion(
        SpeechEmotionEvent(
            correlation_id=cid,
            payload=SpeechEmotionPayload(
                emotion="excited",
                source_tag="smoke",
                raw_tag="excited",
                resolved_fallback=None,
            ),
        )
    )
    await publisher.publish_vocalization(
        VocalizationEvent(
            correlation_id=cid,
            payload=VocalizationPayload(tag="laughter", tts_supported=True),
        )
    )

    # Await receipt of all four (or time out).
    deadline = time.monotonic() + recv_timeout
    while len(received) < 4 and time.monotonic() < deadline:
        await asyncio.to_thread(_spin_once, witness)

    witness.destroy_node()
    await publisher.disconnect()  # rclpy.shutdown() happens here.

    got = len(received)
    if got == 4:
        print(f"\n✅ SMOKE TEST PASSED — 4/4 topics received on domain {config.dds_domain_id}.")
        return 0
    missing = {"mood", "activity", "speech_emotion", "vocalization"} - received.keys()
    print(
        f"\n❌ SMOKE TEST FAILED — {got}/4 received; missing: {sorted(missing)}.\n"
        f"   Check: is anything else publishing? domain mismatch? QoS drift?"
    )
    return 1


def _spin_once(node: Node) -> None:
    """Process one round of DDS callbacks (short timeout, returns fast)."""
    import rclpy

    rclpy.spin_once(node, timeout_sec=0.1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m voice_agent_pipeline.publisher.smoke",
        description="Self-contained ROS 2 publish smoke test for the four /olaf topics.",
    )
    parser.add_argument(
        "--domain",
        type=int,
        default=None,
        help="Override the DDS domain id (defaults to setup.toml [publisher].dds_domain_id).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for all four topics after publishing (default: 10).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("setup.toml"),
        help="Path to setup.toml (default: ./setup.toml).",
    )
    args = parser.parse_args()

    config = _load_publisher_config(args.config, args.domain)
    if config.adapter != "ros2":
        print(
            f"⚠️  [publisher].adapter is {config.adapter!r}, not 'ros2' — "
            "this smoke test exercises the ROS 2 path regardless."
        )
    exit_code = asyncio.run(_run(config, recv_timeout=args.timeout))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
