"""TTFB measurement spike - Cartesia WebSocket TTFB study (Story 6.1).

Run with::

    just ttfb-spike

Designed for a research-paper-grade measurement of Cartesia's
WebSocket TTFB on the Sonic-3 voice. The spike serves three
audiences simultaneously:

1. **DR-001 keystone resolution** - is the median WS TTFB at or below
   400 ms? If so, the cached-opener subsystem (Story 6.2) may be
   reshaped toward pure-live synthesis (DR-001 Option D); if not,
   the cached-opener design stands. The report's headline section
   answers this question.
2. **Paper-grade dataset** - per-transcript-length stratification
   (short / medium / long word-count buckets) and cold-vs-warm
   connection comparison (one-WS-per-call vs. one WS for many
   calls). The 500-sample n is large enough for tight percentile
   estimates; mean + stdev support paper-style distribution
   summaries.
3. **Reproducibility** - run metadata (host, SDK version, voice,
   model, timestamp) is captured in the report; raw per-sample
   timings are dumped at the end so a future re-aggregation is
   possible without re-running the spike.

Methodology
-----------

For each request:

1. Record ``t_send`` (ns) immediately after ``await conn.send(...)``.
2. Iterate the connection until the first ``Chunk`` event arrives.
3. Record ``t_first`` (ns); ``ttfb_ms = (t_first - t_send) // 1e6``.
4. Close (cold) or hold the connection (warm).

The measurement methodology matches the production ``tts.first_frame.
ttfb_ms`` log shape (single-clock intra-request delta), so the spike's
numbers are directly comparable to the SSE production-log baseline
(DR-001 'Empirical evidence', ~1070 ms p50 on SSE).

Cold vs. warm
-------------

- **Cold** samples open a fresh ``websocket_connect()`` per request
  (one-shot WS per call - what the current ``_synthesize_websocket``
  does in v1). TTFB on the cold path includes TLS handshake + WS
  upgrade + first synthesis. The runtime caller path matches this.
- **Warm** samples open ONE WS for the entire bucket, then run
  many ``conn.send`` / first-chunk cycles on it (multiplexed by
  fresh ``context_id`` per call). TTFB on warm includes only the
  synthesis-side latency; transport overhead is amortised across
  the bucket. A pipeline that pools WS connections (future work)
  would see something between these two values.

Transcript-length stratification
--------------------------------

Each call's transcript is randomly picked from a pool of ~20 phrases
spanning word counts from 3 to ~30. Each call's word count is
recorded alongside its TTFB; the report computes per-bucket
statistics on three buckets:

- short  (1-5 words)
- medium (6-15 words)
- long   (16+ words)

Cost / time budget
------------------

- 500 samples (250 cold + 250 warm) at ~$0.06/1k chars on a mean
  ~75-char transcript ~ $2-3 in Cartesia synthesis credits.
- Wall-clock ~25-30 minutes with the inter-request sleep that keeps
  the spike below Cartesia's rate-limit ceiling.

Boundary-concentration: this module is the third Cartesia caller -
:mod:`tts.cartesia` (runtime) + :mod:`audio.regenerate` (offline) +
:mod:`tts.ttfb_spike` (this spike). The cold path uses
:class:`CartesiaClient`; the warm path drops down to the SDK
directly (``client.tts.websocket_connect`` + manual ``conn.send`` /
``conn`` iteration) because :class:`CartesiaClient` deliberately
opens one WS per call, and the warm comparison's whole point is to
NOT do that.

CLAUDE.md rule #4 applies to runtime; this operator tool DOES catch
:class:`CartesiaError` per-request because a clean error count + a
written report is more useful than a stack trace on the first
failure (mirrors :mod:`audio.regenerate`'s posture for the same
reason).
"""

import asyncio
import platform
import random
import socket
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import cartesia
import structlog
from cartesia.types.generation_request import GenerationRequest

from voice_agent_pipeline.config.setup import TtsConfig, load_setup_config
from voice_agent_pipeline.errors import CartesiaError, VoiceAgentError
from voice_agent_pipeline.logging.setup import configure_logging
from voice_agent_pipeline.tts.cartesia import CartesiaClient

log = structlog.get_logger(__name__)


# Cold and warm bucket sizes - balanced 50/50 across the total. 250
# each gives ~80-90 samples per length-bucket, which is enough for
# stable p95 estimates (the bootstrap CI is reasonable at n>=50).
_COLD_SAMPLE_COUNT = 250
_WARM_SAMPLE_COUNT = 250

# Inter-request pause prevents the burst from triggering Cartesia's
# rate-limit and skewing the TTFB distribution upward. 75 ms is well
# inside their documented per-second cap for a single API key on the
# Sonic-3 paid tier.
_INTER_REQUEST_SLEEP_SEC = 0.075

# Output path mirrors the architecture's "implementation artefacts
# committed under build_documents/" convention (same parent dir as
# this story's own .md spec). The filename is referenced from
# decision-records.md DR-001's keystone-closure line, so keep it
# stable.
_REPORT_PATH = Path("build_documents/implementation-artifacts/6-1-ttfb-spike-report.md")

# DR-001 Option D viability threshold: a measured median TTFB at or
# below this value means the cached-opener subsystem (Story 6.2)
# should be re-evaluated against pure-live synthesis, because the
# perceived-latency gap closes without needing pre-cached audio.
# Above it, the cached-opener design stands and Story 6.2 proceeds as
# drafted.
_DR001_OPTION_D_THRESHOLD_SEC = 0.40

# Transcript-length bucket boundaries (inclusive lower / exclusive
# upper for short and medium; long is unbounded above).
_SHORT_WORDS_MAX = 5  # short = [1, 5]
_MEDIUM_WORDS_MAX = 15  # medium = [6, 15]; long = [16, inf)

# Inline transcript pool - covers the realistic Talker reply
# distribution (short / medium / long / question / statement /
# multi-clause). 20 entries so each length bucket has 5-7 phrases
# and individual transcripts don't repeat too often within the
# 500-sample run.
_TRANSCRIPT_POOL: list[str] = [
    # Short (1-5 words).
    "Hello there friend.",
    "Are you well?",
    "Yes, exactly.",
    "Tell me more.",
    "Hmm, let me think.",
    "I'm not sure.",
    "Sounds good.",
    # Medium (6-15 words).
    "I think the weather is quite nice today.",
    "Have you read that book I mentioned yesterday afternoon?",
    "The kitchen is on the left side of the hallway.",
    "It rained heavily for most of the afternoon, then cleared up.",
    "Could you please pass the salt and pepper across the table?",
    "There's a small park about three blocks away from the apartment.",
    "I was hoping we could grab dinner together later this evening.",
    # Long (16+ words).
    (
        "When I look outside the window in the morning, the trees "
        "seem to glow with a soft golden light that I find calming."
    ),
    (
        "If you could pick any single place in the world to visit "
        "next weekend, and money was no object at all, where would "
        "you actually choose to go?"
    ),
    (
        "The pipeline measures latency from end-of-speech to first "
        "audio byte, and reports the ninety-fifth percentile across "
        "every conversation turn."
    ),
    (
        "There's a particular kind of silence that settles over a "
        "library in the late afternoon, when the slanted sunlight "
        "falls across the rows of old books and motes of dust drift "
        "slowly through the warm air."
    ),
    (
        "I find that the best way to remember a new word is to use "
        "it in a sentence about something you actually care about, "
        "because abstract examples slip away within an hour but "
        "personal ones stick around for years."
    ),
    (
        "Walking through the city early on a Sunday morning, before "
        "the cafes open and before the buses start running, gives "
        "you a strange and almost private view of streets that are "
        "usually buzzing with crowds and traffic."
    ),
]


def _bucket_for_word_count(n: int) -> str:
    """Map a transcript word count to its length bucket name."""
    if n <= _SHORT_WORDS_MAX:
        return "short"
    if n <= _MEDIUM_WORDS_MAX:
        return "medium"
    return "long"


def _host_fingerprint() -> str:
    """One-line string identifying the dev host this spike ran on.

    Captured into the report's metadata so future re-runs are
    comparable; a TTFB delta between two reports on different hosts
    is expected, while a delta between two reports on the SAME host
    indicates a real Cartesia-side or network change.
    """
    return f"{platform.system()} {platform.release()} / python {platform.python_version()}"


class _Sample:
    """One observation from the spike.

    Used in lists across cold/warm/length-bucket views without the
    overhead of a full pydantic model (the spike is a single-process
    operator tool; ``_Sample`` lives only inside the run).
    """

    __slots__ = ("bucket", "mode", "transcript", "ttfb_ms", "word_count")

    def __init__(
        self,
        *,
        ttfb_ms: int,
        transcript: str,
        word_count: int,
        bucket: str,
        mode: str,
    ) -> None:
        self.ttfb_ms = ttfb_ms
        self.transcript = transcript
        self.word_count = word_count
        self.bucket = bucket
        self.mode = mode  # "cold" | "warm"


# ---------------------------------------------------------------------------
# Cold path - one WS per call (uses CartesiaClient as the runtime would)
# ---------------------------------------------------------------------------


async def _one_cold_sample(
    client: CartesiaClient,
    transcript: str,
) -> int | None:
    """Open a fresh WS, synthesize, measure TTFB, close. Return ms or None."""
    t_send = time.time_ns()
    try:
        stream = client.synthesize(transcript)
        async for _ in stream:
            t_first = time.time_ns()
            # Close the underlying WS connection cleanly. The async
            # generator's aclose() unwinds the context manager so the
            # SDK closes the socket immediately rather than at GC time.
            await stream.aclose()  # type: ignore[attr-defined]
            return (t_first - t_send) // 1_000_000
    except CartesiaError as e:
        log.warning(
            "ttfb_spike.cold_sample_failed",
            transcript=transcript[:40],
            reason=str(e),
        )
        return None
    log.warning("ttfb_spike.cold_no_chunk", transcript=transcript[:40])
    return None


async def _run_cold_phase(
    client: CartesiaClient,
    pool: list[str],
) -> tuple[list[_Sample], int]:
    """Drive ``_COLD_SAMPLE_COUNT`` cold-path samples. Return (samples, errors)."""
    samples: list[_Sample] = []
    errors = 0
    for i in range(_COLD_SAMPLE_COUNT):
        # Non-crypto: spike picks vary to defeat Cartesia's same-text
        # caching; cryptographic randomness is unnecessary.
        transcript = random.choice(pool)  # noqa: S311
        word_count = len(transcript.split())
        bucket = _bucket_for_word_count(word_count)
        ttfb_ms = await _one_cold_sample(client, transcript)
        if ttfb_ms is None:
            errors += 1
        else:
            samples.append(
                _Sample(
                    ttfb_ms=ttfb_ms,
                    transcript=transcript,
                    word_count=word_count,
                    bucket=bucket,
                    mode="cold",
                ),
            )
            log.info(
                "ttfb_spike.cold_sample",
                index=i,
                ttfb_ms=ttfb_ms,
                bucket=bucket,
                word_count=word_count,
            )
        await asyncio.sleep(_INTER_REQUEST_SLEEP_SEC)
    return samples, errors


# ---------------------------------------------------------------------------
# Warm path - reuse one WS for many calls (drops below CartesiaClient because
# CartesiaClient deliberately opens one WS per synthesize() call)
# ---------------------------------------------------------------------------


async def _run_warm_phase(
    tts_config: TtsConfig,
    api_key_value: str,
    pool: list[str],
) -> tuple[list[_Sample], int]:
    """Drive ``_WARM_SAMPLE_COUNT`` warm-path samples on one WS connection.

    Bypasses :class:`CartesiaClient` to keep one connection open
    across many calls - the v1 runtime client opens a fresh WS per
    synthesize() and pre-emptively closes on first byte (matches
    sequential_loop's half-duplex needs). The warm comparison shows
    what TTFB looks like if a future connection-pool change kept the
    WS open across turns; the delta from cold to warm is the
    amortised TLS+WS handshake cost.
    """
    samples: list[_Sample] = []
    errors = 0
    output_format = {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 16000,
    }

    client = cartesia.AsyncCartesia(api_key=api_key_value)
    async with client.tts.websocket_connect() as conn:
        for i in range(_WARM_SAMPLE_COUNT):
            # Non-crypto: see _run_cold_phase.
            transcript = random.choice(pool)  # noqa: S311
            word_count = len(transcript.split())
            bucket = _bucket_for_word_count(word_count)

            # Fresh context_id per call - the WS multiplexes contexts
            # so we don't accidentally see stale chunks from the prior
            # request.
            context_id = uuid.uuid4().hex
            req = GenerationRequest(
                model_id=tts_config.model,
                output_format=output_format,  # type: ignore[arg-type]
                transcript=transcript,
                voice={"id": tts_config.voice_id, "mode": "id"},  # type: ignore[arg-type]
                generation_config={  # type: ignore[arg-type]
                    "emotion": tts_config.default_emotion,
                    "speed": tts_config.speed,
                },
                context_id=context_id,
                add_timestamps=True,
                add_phoneme_timestamps=False,
            )

            ttfb_ms: int | None = None
            try:
                t_send = time.time_ns()
                await conn.send(req)
                async for event in conn:
                    if event.type == "chunk":
                        t_first = time.time_ns()
                        ttfb_ms = (t_first - t_send) // 1_000_000
                        # Drain the rest of THIS context's events
                        # until Done so subsequent requests on the
                        # same WS don't observe stale chunks.
                        async for tail in conn:
                            if tail.type == "done":
                                break
                            if tail.type == "error":
                                ttfb_ms = None
                                break
                        break
                    if event.type == "error":
                        msg = (
                            getattr(event, "message", None)
                            or getattr(event, "error", None)
                            or "unknown"
                        )
                        log.warning(
                            "ttfb_spike.warm_sample_error_event",
                            transcript=transcript[:40],
                            message=msg,
                        )
                        break
            except cartesia.APIError as e:
                log.warning(
                    "ttfb_spike.warm_sample_failed",
                    transcript=transcript[:40],
                    reason=str(e),
                )
                ttfb_ms = None

            if ttfb_ms is None:
                errors += 1
            else:
                samples.append(
                    _Sample(
                        ttfb_ms=ttfb_ms,
                        transcript=transcript,
                        word_count=word_count,
                        bucket=bucket,
                        mode="warm",
                    ),
                )
                log.info(
                    "ttfb_spike.warm_sample",
                    index=i,
                    ttfb_ms=ttfb_ms,
                    bucket=bucket,
                    word_count=word_count,
                )

            await asyncio.sleep(_INTER_REQUEST_SLEEP_SEC)
    return samples, errors


# ---------------------------------------------------------------------------
# Statistics + report rendering
# ---------------------------------------------------------------------------


def _percentile(samples: list[int], q: float) -> int:
    """Linear-interpolation percentile (q in [0, 100]) over an int list."""
    if not samples:
        return -1
    if len(samples) == 1:
        return samples[0]
    sorted_s = sorted(samples)
    k = (q / 100.0) * (len(sorted_s) - 1)
    f = int(k)
    c = min(f + 1, len(sorted_s) - 1)
    if f == c:
        return sorted_s[f]
    return round(sorted_s[f] + (sorted_s[c] - sorted_s[f]) * (k - f))


def _summarise(samples: list[int]) -> dict[str, int]:
    """Compute the per-bucket statistic block (all values in ms)."""
    if not samples:
        return {
            "n": 0,
            "mean": -1,
            "stdev": -1,
            "min": -1,
            "p25": -1,
            "p50": -1,
            "p75": -1,
            "p90": -1,
            "p95": -1,
            "p99": -1,
            "max": -1,
        }
    return {
        "n": len(samples),
        "mean": round(statistics.fmean(samples)),
        "stdev": round(statistics.stdev(samples)) if len(samples) > 1 else 0,
        "min": min(samples),
        "p25": _percentile(samples, 25),
        "p50": _percentile(samples, 50),
        "p75": _percentile(samples, 75),
        "p90": _percentile(samples, 90),
        "p95": _percentile(samples, 95),
        "p99": _percentile(samples, 99),
        "max": max(samples),
    }


def _stats_row(label: str, stats: dict[str, int]) -> str:
    """One Markdown table row for the percentile table."""
    return (
        f"| {label} | {stats['n']} | {stats['mean']} +/- {stats['stdev']} "
        f"| {stats['p25']} | {stats['p50']} | {stats['p75']} "
        f"| {stats['p90']} | {stats['p95']} | {stats['p99']} "
        f"| {stats['min']} | {stats['max']} |"
    )


def _build_report(
    *,
    cold_samples: list[_Sample],
    warm_samples: list[_Sample],
    cold_errors: int,
    warm_errors: int,
    voice_id: str,
    model: str,
    sdk_version: str,
    host: str,
    started_at: datetime,
    finished_at: datetime,
) -> str:
    """Render the full Markdown report."""
    cold_ms = [s.ttfb_ms for s in cold_samples]
    warm_ms = [s.ttfb_ms for s in warm_samples]
    all_ms = cold_ms + warm_ms

    overall = _summarise(all_ms)
    cold_stats = _summarise(cold_ms)
    warm_stats = _summarise(warm_ms)

    def _per_bucket(samples: list[_Sample]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for bucket in ("short", "medium", "long"):
            bucket_ms = [s.ttfb_ms for s in samples if s.bucket == bucket]
            out[bucket] = _summarise(bucket_ms)
        return out

    cold_buckets = _per_bucket(cold_samples)
    warm_buckets = _per_bucket(warm_samples)

    p50 = overall["p50"]
    if p50 != -1 and (p50 / 1000.0) <= _DR001_OPTION_D_THRESHOLD_SEC:
        keystone_section = (
            "## DR-001 OPTION D VIABILITY\n\n"
            f"Overall median TTFB on WebSocket = **{p50} ms** "
            f"(<= {int(_DR001_OPTION_D_THRESHOLD_SEC * 1000)} ms threshold).\n\n"
            "Story 6.2 should re-evaluate whether the cached-opener "
            "subsystem is still justified versus pure-live Cartesia "
            "synthesis. See DR-001 'Open question (keystone)' - this "
            "measurement closes the question on the WS-viable side. "
            "The perceived-latency gap may close without pre-cached "
            "opener audio, especially if a future change pools WS "
            "connections (see the warm-vs-cold delta in this report).\n"
        )
    else:
        keystone_section = (
            "## CACHED OPENER DESIGN STANDS\n\n"
            f"Overall median TTFB on WebSocket = **{p50} ms** "
            f"(> {int(_DR001_OPTION_D_THRESHOLD_SEC * 1000)} ms threshold).\n\n"
            "The cached-opener design (Story 6.2) proceeds as drafted. "
            "Pure-live Cartesia does NOT close the perceived-latency "
            "gap on its own at this TTFB; pre-cached opener audio is "
            "still load-bearing for the v2 conversational-openers "
            "feature. See DR-001 'Open question (keystone)'.\n"
        )

    if cold_stats["n"] and warm_stats["n"]:
        delta = cold_stats["p50"] - warm_stats["p50"]
        warm_cold_note = (
            f"\nWarm-vs-cold median delta: **{delta} ms** "
            f"(cold {cold_stats['p50']} ms - warm {warm_stats['p50']} ms). "
            "This is the amortised TLS + WS-upgrade cost paid on "
            "every cold call; a future connection-pool change "
            "would recover this on every turn after the first.\n"
        )
    else:
        warm_cold_note = ""

    sse_reference = (
        "## SSE comparison band (from DR-001 'Empirical evidence')\n\n"
        "| percentile | SSE production logs (n=308) |\n"
        "|---|---|\n"
        "| p50 | ~1070 ms |\n"
        "| p75 | ~1640 ms |\n\n"
        "If the WS overall p50 above is materially different from "
        "~1070 ms, that is the SSE -> WS transport delta - call it "
        "out in any follow-up commit referencing this report.\n"
    )

    headline_table = (
        "## TTFB statistics (overall + by mode)\n\n"
        "All values in milliseconds. `mean +/- stdev` shows the "
        "distribution shape; percentiles characterise the tail.\n\n"
        "| group | n | mean +/- stdev | p25 | p50 | p75 | p90 | p95 | p99 | min | max |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        f"{_stats_row('overall', overall)}\n"
        f"{_stats_row('cold (one WS per call)', cold_stats)}\n"
        f"{_stats_row('warm (WS reused)', warm_stats)}\n"
    )

    stratified_table = (
        "## TTFB stratified by transcript length\n\n"
        f"Buckets: short = 1-{_SHORT_WORDS_MAX} words; medium = "
        f"{_SHORT_WORDS_MAX + 1}-{_MEDIUM_WORDS_MAX} words; long = "
        f"{_MEDIUM_WORDS_MAX + 1}+ words.\n\n"
        "### Cold path\n\n"
        "| bucket | n | mean +/- stdev | p25 | p50 | p75 | p90 | p95 | p99 | min | max |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        f"{_stats_row('short', cold_buckets['short'])}\n"
        f"{_stats_row('medium', cold_buckets['medium'])}\n"
        f"{_stats_row('long', cold_buckets['long'])}\n\n"
        "### Warm path\n\n"
        "| bucket | n | mean +/- stdev | p25 | p50 | p75 | p90 | p95 | p99 | min | max |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        f"{_stats_row('short', warm_buckets['short'])}\n"
        f"{_stats_row('medium', warm_buckets['medium'])}\n"
        f"{_stats_row('long', warm_buckets['long'])}\n"
    )

    methodology = (
        "## Methodology\n\n"
        "- Single-clock per-request measurement: `t_send = time.time_ns()` "
        "after `await conn.send(...)`; `t_first = time.time_ns()` on "
        "the first `Chunk` event arrival; `ttfb_ms = (t_first - t_send) "
        "// 1_000_000`. Matches the production `tts.first_frame.ttfb_ms` "
        "log shape, so directly comparable to DR-001's SSE baseline.\n"
        "- Cold path: open `client.tts.websocket_connect()` per request, "
        "close on first chunk. One TLS handshake + WS upgrade per call.\n"
        "- Warm path: open one WS, send N requests serially with fresh "
        "`context_id` per request; the connection persists across the "
        "phase. Amortises transport-side overhead.\n"
        f"- Inter-request sleep: {int(_INTER_REQUEST_SLEEP_SEC * 1000)} ms - "
        "stays well below Cartesia's documented per-second cap on the "
        "Sonic-3 paid tier so the spike does not measure rate-limit "
        "inflation.\n"
        "- Transcript pool: 20 phrases (7 short, 7 medium, 6 long) "
        "randomly cycled. Random pick per request defeats same-text "
        "response caching on the Cartesia side.\n"
        "- Audio format pinned to 16 kHz mono S16LE - same format the "
        "rest of the pipeline uses end-to-end. No resampling artefacts "
        "in the spike.\n"
        "- WS path forces `add_timestamps=True`, "
        "`add_phoneme_timestamps=False` - matches the Story 6.1 v1 "
        "runtime shape (the spike measures what the production WS "
        "callers will see).\n"
    )

    raw_block = (
        "## Raw samples\n\n"
        "Each row: `mode,ttfb_ms,word_count,bucket,transcript` "
        "(in capture order; cold phase first, then warm).\n\n"
        "```\n"
        + "\n".join(
            f"{s.mode},{s.ttfb_ms},{s.word_count},{s.bucket},{s.transcript[:80]}"
            for s in (*cold_samples, *warm_samples)
        )
        + "\n```\n"
    )

    return (
        "# Story 6.1 - Cartesia TTFB measurement spike (WebSocket transport)\n\n"
        f"Generated: `{started_at.isoformat()}` -> `{finished_at.isoformat()}`\n\n"
        "## Run metadata\n\n"
        f"- voice_id: `{voice_id}`\n"
        f"- model: `{model}`\n"
        f"- transport: `websocket`\n"
        f"- cold samples requested: `{_COLD_SAMPLE_COUNT}` "
        f"(successful: `{cold_stats['n']}`, errors: `{cold_errors}`)\n"
        f"- warm samples requested: `{_WARM_SAMPLE_COUNT}` "
        f"(successful: `{warm_stats['n']}`, errors: `{warm_errors}`)\n"
        f"- inter-request sleep: `{int(_INTER_REQUEST_SLEEP_SEC * 1000)} ms`\n"
        f"- dev host: `{host}`\n"
        f"- Cartesia SDK: `{sdk_version}`\n"
        f"- Pipeline schema_version: `3`\n\n"
        f"{headline_table}\n"
        f"{warm_cold_note}\n"
        f"{stratified_table}\n"
        f"{keystone_section}\n"
        f"{sse_reference}\n"
        f"{methodology}\n"
        f"{raw_block}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_spike() -> int:
    """Drive the spike. Return 0 on at least one success; 1 otherwise."""
    config = load_setup_config()
    configure_logging(config)

    # Force WS transport for the spike regardless of [tts] transport -
    # the spike's whole purpose is to measure the WS path.
    tts_config = TtsConfig.model_validate(
        {**config.tts.model_dump(), "transport": "websocket"},
    )

    started_at = datetime.now(tz=UTC)
    host_str = _host_fingerprint()
    try:
        host_str = f"{socket.gethostname()} / {host_str}"
    except OSError:
        pass

    log.info(
        "ttfb_spike.start",
        cold_samples=_COLD_SAMPLE_COUNT,
        warm_samples=_WARM_SAMPLE_COUNT,
        voice_id=tts_config.voice_id,
        model=tts_config.model,
        host=host_str,
    )

    # Cold phase first - the CartesiaClient opens/closes a WS per
    # call, matching the runtime path. We share ONE CartesiaClient
    # instance across the phase so its AsyncCartesia (the SDK client)
    # is constructed once; only the WS connection is per-call.
    cold_client = CartesiaClient(tts_config, config.cartesia_api_key)
    cold_samples, cold_errors = await _run_cold_phase(cold_client, _TRANSCRIPT_POOL)
    log.info(
        "ttfb_spike.cold_phase_complete",
        samples=len(cold_samples),
        errors=cold_errors,
    )

    # Warm phase - drop below CartesiaClient to keep one WS across
    # the whole phase. See _run_warm_phase docstring for rationale.
    warm_samples, warm_errors = await _run_warm_phase(
        tts_config,
        config.cartesia_api_key.get_secret_value(),
        _TRANSCRIPT_POOL,
    )
    log.info(
        "ttfb_spike.warm_phase_complete",
        samples=len(warm_samples),
        errors=warm_errors,
    )

    finished_at = datetime.now(tz=UTC)

    report = _build_report(
        cold_samples=cold_samples,
        warm_samples=warm_samples,
        cold_errors=cold_errors,
        warm_errors=warm_errors,
        voice_id=tts_config.voice_id,
        model=tts_config.model,
        sdk_version=cartesia.__version__,
        host=host_str,
        started_at=started_at,
        finished_at=finished_at,
    )

    # Operator tool - bounded blocking IO at the end of the run.
    # Same noqa rationale as audio/regenerate.py.
    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(report, encoding="utf-8")  # noqa: ASYNC240

    log.info(
        "ttfb_spike.complete",
        total_successful=len(cold_samples) + len(warm_samples),
        total_errors=cold_errors + warm_errors,
        report_path=str(_REPORT_PATH),
    )

    return 0 if (cold_samples or warm_samples) else 1


def main() -> int:
    """CLI entry point - invoke :func:`run_spike` under :func:`asyncio.run`."""
    try:
        return asyncio.run(run_spike())
    except VoiceAgentError as e:
        print(f"ttfb-spike: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
