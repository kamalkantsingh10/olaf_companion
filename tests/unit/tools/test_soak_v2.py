"""Unit tests for the Story 6.4 v2 soak aggregator (`tools/soak_v2`).

Exercises the pure core — `parse_log` + `aggregate` + `render_report` — over
a small in-memory set of known `turn.complete` events. No file IO beyond a
tmp_path round-trip for the JSON-line parser; no running pipeline.
"""

import json

from voice_agent_pipeline.tools.soak_v2 import (
    aggregate,
    parse_log,
    render_report,
)


def _turn(**overrides: object) -> dict[str, object]:
    """A turn.complete event with sane defaults; override per test."""
    base: dict[str, object] = {
        "event": "turn.complete",
        "stt_ms": 300,
        "ttft_ms": 400,
        "ttfb_ms": 230,
        "end_to_first_real_audio_ms": 1400,
        "opener_source": "llm_tag",
        "opener_bucket": "thinking",
        "opener_duration_ms": 500,
        "opener_onset_ms": 600,
        "dead_air_after_opener_ms": 200,
        "emphasis_count_per_turn": 1,
        "routing": "fast_path",
        "had_tool_call": False,
    }
    base.update(overrides)
    return base


def test_parse_log_filters_turn_complete_and_counts_index_mismatch() -> None:
    """Only turn.complete events are collected; index_mismatch WARNs counted."""
    lines = [
        json.dumps(_turn()),
        json.dumps({"event": "stt.transcript", "stt_ms": 280}),  # ignored
        json.dumps({"event": "emphasis.index_mismatch", "requested_index": 5}),
        "not json at all",  # skipped, not fatal
        json.dumps(_turn(stt_ms=320)),
        json.dumps({"event": "emphasis.index_mismatch", "requested_index": 9}),
    ]
    turns, mismatch = parse_log(lines)
    assert len(turns) == 2
    assert mismatch == 2


def test_aggregate_percentiles_and_sample_counts() -> None:
    """Percentiles computed over non-None samples; n reflects the count."""
    turns = [_turn(stt_ms=v) for v in (100, 200, 300, 400, 500)]
    agg = aggregate(turns, index_mismatch_count=0)
    assert agg.n_turns == 5
    stt = agg.metrics["stt_ms"]
    assert stt["n"] == 5
    # Linear-interpolation percentile over [100,200,300,400,500].
    assert stt["p50"] == 300
    assert stt["p25"] == 200
    assert stt["p75"] == 400


def test_aggregate_excludes_none_valued_metrics() -> None:
    """A None ttft_ms (buffering provider) is excluded from that metric's n."""
    turns = [_turn(ttft_ms=400), _turn(ttft_ms=None), _turn(ttft_ms=600)]
    agg = aggregate(turns)
    assert agg.metrics["ttft_ms"]["n"] == 2  # the None dropped out


def test_aggregate_distributions() -> None:
    """opener_source and routing distributions count correctly; None→self_gated."""
    turns = [
        _turn(opener_source="llm_tag", routing="fast_path"),
        _turn(opener_source="timer_fallback", routing="fast_path"),
        _turn(opener_source=None, routing="clarification"),
    ]
    agg = aggregate(turns)
    assert agg.opener_source_dist == {"llm_tag": 1, "timer_fallback": 1, "self_gated": 1}
    assert agg.routing_dist == {"fast_path": 2, "clarification": 1}


def test_render_report_contains_targets_and_counts() -> None:
    """The Markdown report carries the headline counts + target verdicts."""
    turns = [_turn(opener_onset_ms=600, dead_air_after_opener_ms=200)]
    agg = aggregate(turns, index_mismatch_count=0)
    report = render_report(agg, log_path="/tmp/x.log", window_note="entire log file")  # noqa: S108
    assert "Turns analysed: **1**" in report
    assert "NFR33 opener onset" in report
    assert "NFR34 dead-air-after-opener" in report
    # 600 ≤ 700 and 200 ≤ 250 → both pass.
    assert "✅ pass" in report


def test_render_report_flags_over_target() -> None:
    """A p95 onset above 700 ms renders as over-target."""
    turns = [_turn(opener_onset_ms=900) for _ in range(3)]
    agg = aggregate(turns)
    report = render_report(agg, log_path="/tmp/x.log", window_note="x")  # noqa: S108
    assert "❌ over" in report


def test_full_roundtrip_parse_then_aggregate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: write a JSON-line log, parse it, aggregate."""
    log_file = tmp_path / "voice-agent.log"
    log_file.write_text("\n".join(json.dumps(_turn(stt_ms=v)) for v in (150, 250, 350)) + "\n")
    with log_file.open() as f:
        turns, mismatch = parse_log(f)
    agg = aggregate(turns, index_mismatch_count=mismatch)
    assert agg.n_turns == 3
    assert agg.metrics["stt_ms"]["p50"] == 250
