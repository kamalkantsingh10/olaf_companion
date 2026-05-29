"""v2 soak-report generator — aggregates ``turn.complete`` log events (Story 6.4).

Reads the pipeline's JSON-line structured log, parses the Story-6.4
``turn.complete`` rollup events, and writes a Markdown report with latency
percentiles + opener/routing distributions + a target-comparison table
against NFR33 / NFR34 / DR-001's projection.

Operator tool (sibling to ``tts/ttfb_spike.py`` + ``audio/regenerate.py``),
run out-of-band::

    python -m voice_agent_pipeline.tools.soak_v2 --log ./logs/voice-agent.log
    python -m voice_agent_pipeline.tools.soak_v2 --minutes 30 --out report.md

Scope note (carried into the report preamble): this report is the sign-off
for the **instrumentation** — that `turn.complete` fires with sane numbers
across turn shapes. It is NOT the v1 latency sign-off; the full 7-day soak
runs as part of Story 5-4 once Epic 5 hardening lands. A 30-minute live run
with mixed turn shapes is sufficient evidence the instrumentation works.

Pure core (``aggregate``) is unit-tested; the file IO + CLI wrapper around it
is thin. Percentile method matches ``tts/ttfb_spike.py`` exactly (linear
interpolation) so the two reports are directly comparable.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

# The numeric metrics aggregated as percentile distributions. Order is the
# report's row order. p95 is included (beyond AC #7's p25/50/75/90) because
# NFR33 / NFR34's pass criteria are stated at p95.
_METRICS: tuple[str, ...] = (
    "stt_ms",
    "ttft_ms",
    "ttfb_ms",
    "end_to_first_real_audio_ms",
    "opener_onset_ms",
    "dead_air_after_opener_ms",
    "emphasis_count_per_turn",
)

_PERCENTILES: tuple[int, ...] = (25, 50, 75, 90, 95)

# Target-comparison rows: (metric, percentile, label, target_ms, comparator).
_TARGETS: tuple[tuple[str, int, str, int, str], ...] = (
    ("opener_onset_ms", 95, "NFR33 opener onset", 700, "<="),
    ("dead_air_after_opener_ms", 95, "NFR34 dead-air-after-opener", 250, "<="),
)


def _percentile(samples: list[int], q: float) -> int:
    """Linear-interpolation percentile (q in [0, 100]) over an int list.

    Identical method to ``tts/ttfb_spike.py:_percentile`` so soak numbers and
    spike numbers are computed the same way. Empty input returns ``-1`` (a
    sentinel the report renders as ``n/a``).
    """
    if not samples:
        return -1
    sorted_s = sorted(samples)
    k = (q / 100.0) * (len(sorted_s) - 1)
    f = int(k)
    c = min(f + 1, len(sorted_s) - 1)
    if f == c:
        return sorted_s[f]
    return round(sorted_s[f] + (sorted_s[c] - sorted_s[f]) * (k - f))


@dataclass
class SoakAggregates:
    """Aggregated soak metrics — the pure output of :func:`aggregate`."""

    n_turns: int
    # metric -> {"p25": int, ..., "n": int (non-null sample count)}
    metrics: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    opener_source_dist: dict[str, int] = field(default_factory=dict[str, int])
    routing_dist: dict[str, int] = field(default_factory=dict[str, int])
    index_mismatch_count: int = 0


def aggregate(turns: list[dict[str, object]], index_mismatch_count: int = 0) -> SoakAggregates:
    """Aggregate parsed ``turn.complete`` events into percentile distributions.

    Pure function (no IO) — the unit-testable core. ``None``-valued metric
    fields are excluded from that metric's sample set (so ``ttft_ms`` over a
    run where one provider buffered still aggregates only the real values).

    Args:
        turns: Parsed ``turn.complete`` event dicts.
        index_mismatch_count: Count of ``emphasis.index_mismatch`` WARN events
            seen in the same log (should be 0 in a healthy run).

    Returns:
        A :class:`SoakAggregates` with per-metric percentiles + distributions.
    """
    metrics: dict[str, dict[str, int]] = {}
    for metric in _METRICS:
        samples = [int(v) for t in turns if isinstance((v := t.get(metric)), (int, float))]
        row: dict[str, int] = {f"p{p}": _percentile(samples, p) for p in _PERCENTILES}
        row["n"] = len(samples)
        metrics[metric] = row

    opener_source_dist = Counter(
        # None opener_source → "self_gated" bucket for the distribution.
        str(t.get("opener_source") or "self_gated")
        for t in turns
    )
    routing_dist = Counter(str(t.get("routing", "unknown")) for t in turns)

    return SoakAggregates(
        n_turns=len(turns),
        metrics=metrics,
        opener_source_dist=dict(opener_source_dist),
        routing_dist=dict(routing_dist),
        index_mismatch_count=index_mismatch_count,
    )


def parse_log(lines: Iterable[str]) -> tuple[list[dict[str, object]], int]:
    """Parse JSON-line log → (turn.complete events, index_mismatch count).

    Non-JSON lines and non-relevant events are skipped silently — a real log
    interleaves many event types. A malformed JSON line is not fatal (the
    soak tool reports on what it can parse).
    """
    turns: list[dict[str, object]] = []
    index_mismatch = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            loaded = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(loaded, dict):
            continue
        record = cast("dict[str, object]", loaded)
        event = record.get("event")
        if event == "turn.complete":
            turns.append(record)
        elif event == "emphasis.index_mismatch":
            index_mismatch += 1
    return turns, index_mismatch


def _within_window(turns: list[dict[str, object]], minutes: int) -> list[dict[str, object]]:
    """Keep only turns whose ``timestamp`` is within the last N minutes.

    Uses the most-recent parseable ISO ``timestamp`` as the window anchor (so
    a log replayed offline still windows correctly). If no turn carries a
    parseable timestamp, returns all turns unchanged (the field is optional in
    some structlog configs).
    """
    stamped: list[tuple[datetime, dict[str, object]]] = []
    for t in turns:
        ts = t.get("timestamp")
        if not isinstance(ts, str):
            continue
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        stamped.append((parsed, t))
    if not stamped:
        return turns
    anchor = max(p for p, _ in stamped)
    cutoff = anchor - timedelta(minutes=minutes)
    return [t for p, t in stamped if p >= cutoff]


def _fmt(value: int) -> str:
    """Render a percentile cell — the -1 sentinel becomes ``n/a``."""
    return "n/a" if value < 0 else str(value)


def render_report(agg: SoakAggregates, *, log_path: str, window_note: str) -> str:
    """Render the soak aggregates as a Markdown report."""
    lines: list[str] = []
    lines.append("# Story 6.4 — v2 soak report")
    lines.append("")
    lines.append(
        "**Scope:** instrumentation sign-off — confirms `turn.complete` fires "
        "with sane numbers across turn shapes. NOT the v1 latency sign-off; "
        "the full 7-day soak runs in Story 5-4 once Epic 5 hardening lands."
    )
    lines.append("")
    lines.append(f"- Source log: `{log_path}`")
    lines.append(f"- Window: {window_note}")
    lines.append(f"- Turns analysed: **{agg.n_turns}**")
    lines.append(f"- `emphasis.index_mismatch` WARNs: **{agg.index_mismatch_count}** (healthy = 0)")
    lines.append("")

    lines.append("## Latency percentiles (integer ms; `emphasis_count_per_turn` is a count)")
    lines.append("")
    header = "| metric | n | " + " | ".join(f"p{p}" for p in _PERCENTILES) + " |"
    sep = "|---|---|" + "|".join("---" for _ in _PERCENTILES) + "|"
    lines.append(header)
    lines.append(sep)
    for metric in _METRICS:
        row = agg.metrics.get(metric, {})
        cells = " | ".join(_fmt(row.get(f"p{p}", -1)) for p in _PERCENTILES)
        lines.append(f"| {metric} | {row.get('n', 0)} | {cells} |")
    lines.append("")

    lines.append("## Distributions")
    lines.append("")
    lines.append("**opener_source:**")
    lines.append("")
    for source, count in sorted(agg.opener_source_dist.items()):
        lines.append(f"- `{source}`: {count}")
    lines.append("")
    lines.append("**routing:**")
    lines.append("")
    for route, count in sorted(agg.routing_dist.items()):
        lines.append(f"- `{route}`: {count}")
    lines.append("")

    lines.append("## Target comparison (NFR33 / NFR34)")
    lines.append("")
    lines.append("| target | metric @ pNN | measured | target | verdict |")
    lines.append("|---|---|---|---|---|")
    for metric, pct, label, target_ms, comparator in _TARGETS:
        measured = agg.metrics.get(metric, {}).get(f"p{pct}", -1)
        if measured < 0:
            verdict = "n/a (no samples)"
            measured_str = "n/a"
        else:
            ok = measured <= target_ms if comparator == "<=" else measured >= target_ms
            verdict = "✅ pass" if ok else "❌ over"
            measured_str = f"{measured} ms"
        lines.append(
            f"| {label} | `{metric}` @ p{pct} | {measured_str} | "
            f"{comparator} {target_ms} ms | {verdict} |"
        )
    lines.append("")
    lines.append(
        "> NFR1 (simple-turn p95 ≤ 1500 ms) predates Epic 6 and may need a v2 "
        "revision; the `end_to_first_real_audio_ms` distribution above is the "
        "input to that Story 5-4 sign-off conversation — not this report's call."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="soak_v2",
        description="Aggregate turn.complete events into a v2 soak report.",
    )
    parser.add_argument(
        "--log",
        default="./logs/voice-agent.log",
        help="Path to the JSON-line structured log (default ./logs/voice-agent.log).",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="Keep only turns within the last N minutes (by event timestamp). "
        "Default: all turns in the file.",
    )
    parser.add_argument(
        "--out",
        default="build_documents/implementation-artifacts/6-4-soak-report.md",
        help="Output Markdown report path.",
    )
    args = parser.parse_args(argv)

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"soak_v2: log file not found: {log_path}", file=sys.stderr)
        return 1

    with log_path.open("r") as f:
        turns, index_mismatch = parse_log(f)

    if args.minutes is not None:
        turns = _within_window(turns, args.minutes)
        window_note = f"last {args.minutes} min (by event timestamp)"
    else:
        window_note = "entire log file"

    agg = aggregate(turns, index_mismatch_count=index_mismatch)
    report = render_report(agg, log_path=str(log_path), window_note=window_note)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"soak_v2: wrote {out_path} ({agg.n_turns} turns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
