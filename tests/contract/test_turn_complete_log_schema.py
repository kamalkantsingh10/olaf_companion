"""Contract — the Story 6.4 ``turn.complete`` per-turn rollup log event.

`turn.complete` is the first per-turn rollup log event in the pipeline (the
DR-003 dashboard + the v2 soak consume it instead of reconstructing turn
shape from per-action events). This contract pins its field set + types +
None-handling across the four turn shapes, captured via structlog's
``capture_logs`` test helper (no real sink, no file IO).
"""

import structlog

from voice_agent_pipeline.sequential_loop import _emit_turn_complete, _TurnTimings

# The documented field set (architecture.md §Logging Conventions). Every
# emitted `turn.complete` must carry exactly these keys (plus structlog's
# own `event` / `log_level`).
_REQUIRED_FIELDS = {
    "stt_ms",
    "ttft_ms",
    "ttfb_ms",
    "end_to_first_real_audio_ms",
    "opener_source",
    "opener_bucket",
    "opener_duration_ms",
    "opener_onset_ms",
    "dead_air_after_opener_ms",
    "emphasis_count_per_turn",
    "routing",
    "had_tool_call",
}


def _capture(timings: _TurnTimings) -> dict[str, object]:
    """Emit one turn.complete under capture_logs; return the event dict."""
    with structlog.testing.capture_logs() as events:
        _emit_turn_complete(timings)
    turn_events = [e for e in events if e.get("event") == "turn.complete"]
    assert len(turn_events) == 1, f"expected exactly one turn.complete, got {turn_events}"
    return dict(turn_events[0])


def test_full_turn_with_llm_tag_opener_carries_all_fields() -> None:
    """A complete normal turn → every derived field is a real integer ms."""
    # Wall-clock anchors (ns) for a turn that fired an llm_tag opener.
    base = 1_000_000_000_000
    timings = _TurnTimings(
        vad_end_ns=base,
        stt_done_ns=base + 300_000_000,  # +300 ms
        talker_first_token_ns=base + 700_000_000,  # +400 ms after stt
        opener_first_frame_ns=base + 750_000_000,
        opener_last_frame_ns=base + 1_250_000_000,
        real_first_frame_ns=base + 1_400_000_000,
        turn_end_ns=base + 3_000_000_000,
        ttfb_ms=230,
        opener_source="llm_tag",
        opener_bucket="thinking",
        opener_duration_ms=500,
        emphasis_count=2,
        routing="fast_path",
        had_tool_call=False,
    )
    event = _capture(timings)

    assert set(event) - {"event", "log_level"} == _REQUIRED_FIELDS
    assert event["stt_ms"] == 300
    assert event["ttft_ms"] == 400
    assert event["ttfb_ms"] == 230
    assert event["end_to_first_real_audio_ms"] == 1400
    assert event["opener_source"] == "llm_tag"
    assert event["opener_bucket"] == "thinking"
    assert event["opener_duration_ms"] == 500
    assert event["opener_onset_ms"] == 750
    assert event["dead_air_after_opener_ms"] == 150  # 1400 - 1250
    assert event["emphasis_count_per_turn"] == 2
    assert event["routing"] == "fast_path"
    assert event["had_tool_call"] is False


def test_self_gated_turn_has_null_opener_fields() -> None:
    """No opener fired → all opener_* fields are None, not 0."""
    base = 2_000_000_000_000
    timings = _TurnTimings(
        vad_end_ns=base,
        stt_done_ns=base + 250_000_000,
        talker_first_token_ns=base + 600_000_000,
        real_first_frame_ns=base + 900_000_000,
        turn_end_ns=base + 2_500_000_000,
        ttfb_ms=210,
        routing="fast_path",
    )
    event = _capture(timings)

    assert event["opener_source"] is None
    assert event["opener_bucket"] is None
    assert event["opener_duration_ms"] is None
    assert event["opener_onset_ms"] is None
    assert event["dead_air_after_opener_ms"] is None
    assert event["end_to_first_real_audio_ms"] == 900
    assert event["emphasis_count_per_turn"] == 0


def test_clarification_turn_has_no_real_audio_metric() -> None:
    """Clarification short-circuits → no real-answer frame → None metric."""
    base = 3_000_000_000_000
    timings = _TurnTimings(
        vad_end_ns=base,
        stt_done_ns=base + 280_000_000,
        turn_end_ns=base + 1_200_000_000,
        routing="clarification",
    )
    event = _capture(timings)

    assert event["routing"] == "clarification"
    assert event["stt_ms"] == 280
    # No talker token, no real audio → these stay None.
    assert event["ttft_ms"] is None
    assert event["end_to_first_real_audio_ms"] is None
    assert event["ttfb_ms"] is None
    assert event["had_tool_call"] is False


def test_tool_only_reply_records_tool_call_without_real_audio() -> None:
    """A tool-call-only reply has had_tool_call True but no spoken audio."""
    base = 4_000_000_000_000
    timings = _TurnTimings(
        vad_end_ns=base,
        stt_done_ns=base + 260_000_000,
        talker_first_token_ns=base + 500_000_000,
        turn_end_ns=base + 1_500_000_000,
        routing="fast_path",
        had_tool_call=True,
    )
    event = _capture(timings)

    assert event["had_tool_call"] is True
    assert event["end_to_first_real_audio_ms"] is None
    assert event["dead_air_after_opener_ms"] is None
    assert event["emphasis_count_per_turn"] == 0
