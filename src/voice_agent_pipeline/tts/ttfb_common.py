"""Shared TTFB-spike helpers (Story 6.1 Cartesia + Story 6.5 Gemini).

Provider-agnostic pieces used by both :mod:`tts.ttfb_spike` (Cartesia,
Story 6.1) and :mod:`tts.gemini_ttfb_spike` (Gemini, Story 6.5): the
transcript pool, length-bucketing, the lightweight per-sample record, and
the percentile / summary / table-row maths. Keeping them here lets the two
spikes produce directly-comparable reports without one importing the
other's internals.

The provider-specific bits (sample counts, inter-request sleep, the API
call loop, the report prose + verdict) live in each spike module.
"""

import platform
import statistics

# Transcript-length bucket boundaries (inclusive lower / exclusive upper
# for short and medium; long is unbounded above).
SHORT_WORDS_MAX = 5  # short = [1, 5]
MEDIUM_WORDS_MAX = 15  # medium = [6, 15]; long = [16, inf)

# Inline transcript pool — covers the realistic Talker reply distribution
# (short / medium / long / question / statement / multi-clause). 20 entries
# so each length bucket has 5-7 phrases and individual transcripts don't
# repeat too often within a multi-hundred-sample run.
TRANSCRIPT_POOL: list[str] = [
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


def bucket_for_word_count(n: int) -> str:
    """Map a transcript word count to its length bucket name."""
    if n <= SHORT_WORDS_MAX:
        return "short"
    if n <= MEDIUM_WORDS_MAX:
        return "medium"
    return "long"


def host_fingerprint() -> str:
    """One-line string identifying the dev host a spike ran on.

    Captured into a report's metadata so future re-runs are comparable; a
    TTFB delta between two reports on different hosts is expected, while a
    delta between two reports on the SAME host indicates a real provider-
    side or network change.
    """
    return f"{platform.system()} {platform.release()} / python {platform.python_version()}"


class Sample:
    """One TTFB observation from a spike.

    Used in lists across cold/warm/length-bucket views without the overhead
    of a full pydantic model (a spike is a single-process operator tool;
    ``Sample`` lives only inside the run).
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


def percentile(samples: list[int], q: float) -> int:
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


def summarise(samples: list[int]) -> dict[str, int]:
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
        "p25": percentile(samples, 25),
        "p50": percentile(samples, 50),
        "p75": percentile(samples, 75),
        "p90": percentile(samples, 90),
        "p95": percentile(samples, 95),
        "p99": percentile(samples, 99),
        "max": max(samples),
    }


def stats_row(label: str, stats: dict[str, int]) -> str:
    """One Markdown table row for the percentile table."""
    return (
        f"| {label} | {stats['n']} | {stats['mean']} +/- {stats['stdev']} "
        f"| {stats['p25']} | {stats['p50']} | {stats['p75']} "
        f"| {stats['p90']} | {stats['p95']} | {stats['p99']} "
        f"| {stats['min']} | {stats['max']} |"
    )
