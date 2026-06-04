"""TTFB measurement spike — Gemini Live-API TTFB gate (Story 6.5, Task 2).

Run with::

    just gemini-ttfb-spike

This is the **blocking gate** for the Cartesia→Gemini TTS migration. The
dedicated Gemini TTS models (``gemini-2.5-flash-preview-tts``) do NOT
stream — only the Live API (``gemini-3.1-flash-live-preview``) yields
audio incrementally with a low TTFB. This spike measures cold/warm TTFB
and writes a PASS/FAIL verdict against the re-baselined gate
(cold p50 ≤ 1100 ms ≈ Cartesia's real production p50; see ``_GATE_MS``).

If the gate FAILS, Story 6.5 stops here (Tasks 3-9 are NOT started) and
the provider decision is re-opened — exactly the trap the gate exists to
prevent (shipping a half-migrated pipeline that regresses latency).

Relationship to the Story 6.1 Cartesia spike
---------------------------------------------

This module deliberately does NOT touch :mod:`tts.ttfb_spike` (the
Cartesia spike) — the story calls for a fork, not an edit. It DOES reuse
that module's transcript pool + length-bucketing + percentile/summary +
report-row helpers, so the two reports are directly comparable. The
Cartesia baseline (real production p50 ~1067 ms, DR-001 SSE) is printed
alongside the Gemini numbers for the verdict.

Cold vs. warm
-------------

- **Cold** opens a fresh ``client.aio.live.connect(...)`` Live session
  per request — this is what the half-duplex runtime
  (``GeminiClient.synthesize``) does on every turn, so the **gate is on
  COLD p50**. Cold TTFB includes the WebSocket handshake + session setup
  + first synthesis.
- **Warm** reuses ONE Live session across many turns. The cold→warm
  delta is the per-turn session-setup cost a future connection-pool
  change would recover. If cold FAILS the gate but warm PASSES, the
  conclusion is "pool the Live session", not "abandon Gemini".

Methodology mirrors Story 6.1: single-clock intra-request delta —
``t_send`` after the text is sent, ``t_first`` on the first audio chunk,
``ttfb_ms = (t_first - t_send) // 1e6`` — so the number is directly
comparable to the production ``tts.first_frame.ttfb_ms`` log shape.

Sample budget
-------------

Defaults to a modest 50 cold + 50 warm for a fast, cheap **first gate
read** on a preview model (~8-10 min). Bump :data:`_COLD_SAMPLE_COUNT` /
:data:`_WARM_SAMPLE_COUNT` to 250 each for the paper-grade 500-sample run
once the gate trends PASS. p50 at n=50 is directional; if the result
lands near the 1100 ms gate, re-run at higher n before committing.

Like the Cartesia spike, this operator tool catches the provider's API
error per-request (a clean error count + a written report beats a stack
trace on the first transient failure). CLAUDE.md rule #4 governs the
RUNTIME path (Task 3's ``GeminiClient``), not this measurement harness.
"""

import asyncio
import platform
import random
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import structlog
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from voice_agent_pipeline.config.setup import load_setup_config
from voice_agent_pipeline.errors import VoiceAgentError
from voice_agent_pipeline.logging.setup import configure_logging

# Canonical Live-API helpers live with the runtime client (gemini.py) so the
# spike measures exactly what the runtime uses — single source of truth.
from voice_agent_pipeline.tts.gemini import (
    build_live_config,
    content_turn,
    first_audio_bytes,
    is_turn_complete,
)

# Reuse the shared spike helpers (extracted from the Story 6.1 Cartesia
# harness into ttfb_common) so the two reports are directly comparable —
# same transcript distribution, percentile maths, and table shape.
from voice_agent_pipeline.tts.ttfb_common import (
    MEDIUM_WORDS_MAX,
    SHORT_WORDS_MAX,
    TRANSCRIPT_POOL,
    Sample,
    bucket_for_word_count,
    stats_row,
    summarise,
)

log = structlog.get_logger(__name__)


# First-read defaults: small + cheap on a preview model. Bump to 250 each
# for the paper-grade 500-sample run once the gate trends PASS.
_COLD_SAMPLE_COUNT = 50
_WARM_SAMPLE_COUNT = 50

# Preview-tier rate limits are undocumented; a conservative inter-request
# pause keeps the burst from triggering a 429 that would inflate TTFB.
_INTER_REQUEST_SLEEP_SEC = 0.2

# Re-baselined gate (2026-06-04, with Kamal): NFR4's literal "≤ 400 ms
# p95" is stricter than what Cartesia actually shipped — DR-001 measured
# Cartesia's REAL production TTFB at ~1067 ms p50 (the 230 ms figure was a
# best-case warm-WS spike, not production). The meaningful "no perceived-
# latency regression" gate is therefore: cold p50 at or below Cartesia's
# production p50, with Story 6.2 cached openers masking the remainder.
# Rounded to 1100 ms. (Spec reconciliation of NFR4 happens in Task 8.)
_GATE_MS = 1100

# Cartesia reference points for the side-by-side verdict: the real
# production TTFB (DR-001 SSE) is the no-regression bar; the spike's warm
# best-case is shown for context.
_CARTESIA_BASELINE = "production p50 ~1067 ms (DR-001 SSE); warm-WS spike best-case ~230 ms"

_REPORT_PATH = Path(
    "build_documents/implementation-artifacts/6-5-gemini-ttfb-spike-report.md",
)


# ---------------------------------------------------------------------------
# Cold path — fresh Live session per request (matches the runtime turn loop)
# ---------------------------------------------------------------------------


async def _one_cold_sample(
    client: genai.Client,
    *,
    model: str,
    config: types.LiveConnectConfig,
    text: str,
) -> int | None:
    """Open a fresh Live session, send text, measure TTFB to first audio."""
    t_send = time.time_ns()
    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            # send_client_content with turn_complete=True submits a full
            # user turn and asks the model to respond — the documented way
            # to drive TTS-style synthesis over the Live API.
            await session.send_client_content(turns=content_turn(text), turn_complete=True)
            async for response in session.receive():
                if first_audio_bytes(response) is not None:
                    return (time.time_ns() - t_send) // 1_000_000
    except genai_errors.APIError as e:
        log.warning("gemini_ttfb_spike.cold_sample_failed", text=text[:40], reason=str(e))
        return None
    log.warning("gemini_ttfb_spike.cold_no_audio", text=text[:40])
    return None


async def _run_cold_phase(
    client: genai.Client,
    *,
    model: str,
    voice_name: str,
    style_prompt: str,
) -> tuple[list[Sample], int]:
    """Drive ``_COLD_SAMPLE_COUNT`` cold-path samples. Return (samples, errors)."""
    samples: list[Sample] = []
    errors = 0
    config = build_live_config(voice_name, style_prompt)
    for i in range(_COLD_SAMPLE_COUNT):
        transcript = random.choice(TRANSCRIPT_POOL)  # noqa: S311 — defeat caching, not crypto
        word_count = len(transcript.split())
        bucket = bucket_for_word_count(word_count)
        # Raw transcript — the persona lives in the system_instruction.
        ttfb_ms = await _one_cold_sample(client, model=model, config=config, text=transcript)
        if ttfb_ms is None:
            errors += 1
        else:
            samples.append(
                Sample(
                    ttfb_ms=ttfb_ms,
                    transcript=transcript,
                    word_count=word_count,
                    bucket=bucket,
                    mode="cold",
                ),
            )
            log.info("gemini_ttfb_spike.cold_sample", index=i, ttfb_ms=ttfb_ms, bucket=bucket)
        await asyncio.sleep(_INTER_REQUEST_SLEEP_SEC)
    return samples, errors


# ---------------------------------------------------------------------------
# Warm path — one Live session reused across many turns
# ---------------------------------------------------------------------------


async def _run_warm_phase(
    client: genai.Client,
    *,
    model: str,
    voice_name: str,
    style_prompt: str,
) -> tuple[list[Sample], int]:
    """Drive ``_WARM_SAMPLE_COUNT`` samples on ONE persistent Live session.

    The cold→warm delta is the per-turn session-setup cost a future
    connection-pool change would recover. Each turn is fully drained
    (until ``turn_complete``) before the next send so its first-audio
    timing isn't polluted by the previous turn's tail.
    """
    samples: list[Sample] = []
    errors = 0
    config = build_live_config(voice_name, style_prompt)
    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            for i in range(_WARM_SAMPLE_COUNT):
                # Raw transcript — persona lives in the system_instruction.
                text = random.choice(TRANSCRIPT_POOL)  # noqa: S311
                word_count = len(text.split())
                bucket = bucket_for_word_count(word_count)

                ttfb_ms: int | None = None
                t_send = time.time_ns()
                await session.send_client_content(turns=content_turn(text), turn_complete=True)
                async for response in session.receive():
                    if ttfb_ms is None and first_audio_bytes(response) is not None:
                        ttfb_ms = (time.time_ns() - t_send) // 1_000_000
                    if is_turn_complete(response):
                        break

                if ttfb_ms is None:
                    errors += 1
                    log.warning("gemini_ttfb_spike.warm_no_audio", text=text[:40])
                else:
                    samples.append(
                        Sample(
                            ttfb_ms=ttfb_ms,
                            transcript=text,
                            word_count=word_count,
                            bucket=bucket,
                            mode="warm",
                        ),
                    )
                    log.info(
                        "gemini_ttfb_spike.warm_sample",
                        index=i,
                        ttfb_ms=ttfb_ms,
                        bucket=bucket,
                    )
                await asyncio.sleep(_INTER_REQUEST_SLEEP_SEC)
    except genai_errors.APIError as e:
        log.warning("gemini_ttfb_spike.warm_session_failed", reason=str(e))
    return samples, errors


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _build_report(
    *,
    cold_samples: list[Sample],
    warm_samples: list[Sample],
    cold_errors: int,
    warm_errors: int,
    voice_name: str,
    live_model: str,
    style_prompt: str,
    sdk_version: str,
    host: str,
    started_at: datetime,
    finished_at: datetime,
) -> str:
    """Render the gate report with an explicit PASS/FAIL verdict."""
    cold_ms = [s.ttfb_ms for s in cold_samples]
    warm_ms = [s.ttfb_ms for s in warm_samples]
    cold_stats = summarise(cold_ms)
    warm_stats = summarise(warm_ms)

    cold_p50 = cold_stats["p50"]
    warm_p50 = warm_stats["p50"]
    cold_pass = cold_p50 != -1 and cold_p50 <= _GATE_MS
    warm_pass = warm_p50 != -1 and warm_p50 <= _GATE_MS

    if cold_stats["n"] == 0 and warm_stats["n"] == 0:
        # Every request errored — this is an access/auth/transport problem,
        # NOT a latency verdict. Calling it a "GATE FAIL" would wrongly read
        # as "Gemini is too slow"; surface it as inconclusive instead.
        verdict = (
            "## ⛔ INCONCLUSIVE — 0 successful samples "
            f"({cold_errors + warm_errors} errors)\n\n"
            "No TTFB could be measured: every request failed before audio "
            "arrived. This is an access/auth/transport issue (e.g. WebSocket "
            "close 1008 = the API key/project cannot reach the Live API's "
            "`BidiGenerateContent` method), NOT a latency result. **The gate "
            "is neither passed nor failed.** Fix the credential/access "
            "(a valid Google AI Studio key with Live-API access, or the "
            "correct `live_model`) and re-run before deciding Story 6.5. See "
            "the error sample in `logs/voice-agent.log` "
            "(`gemini_ttfb_spike.*_failed`).\n"
        )
    elif cold_pass:
        verdict = (
            f"## ✅ GATE PASS — cold p50 = {cold_p50} ms ≤ {_GATE_MS} ms "
            "(re-baselined no-regression gate)\n\n"
            "Gemini Live-API TTFB on the realistic runtime path (fresh "
            "session per turn) is at or below Cartesia's real production "
            "TTFB, so it does not regress perceived latency — and Story 6.2 "
            "cached openers mask the remainder. **Story 6.5 proceeds to "
            "Task 3** (build `GeminiClient` on the Live API with the "
            "verbatim system_instruction).\n"
        )
    elif warm_pass:
        verdict = (
            f"## ⚠️ CONDITIONAL — cold p50 = {cold_p50} ms > {_GATE_MS} ms, "
            f"but warm p50 = {warm_p50} ms ≤ {_GATE_MS} ms\n\n"
            "Fresh-session-per-turn exceeds the gate, but a reused session "
            "meets it — the gap is Live-session setup cost. Do NOT abandon "
            "Gemini; instead Task 3 must **pool/keep-alive the Live "
            "session** across turns. Decision for Kamal before Task 3.\n"
        )
    else:
        verdict = (
            f"## ❌ GATE FAIL — cold p50 = {cold_p50} ms, warm p50 = {warm_p50} ms "
            f"(both > {_GATE_MS} ms)\n\n"
            "Gemini Live-API TTFB exceeds Cartesia's production baseline even "
            "with a reused session — a real perceived-latency regression. "
            "**Story 6.5 STOPS here** (Tasks 3-9 not started). Re-open the "
            "provider decision: stay on Cartesia, or revisit with a higher-n "
            "run if the result is borderline.\n"
        )

    headline = (
        "## TTFB statistics (by mode)\n\n"
        "All values in milliseconds. Gate is on **cold** p50 (the runtime "
        "opens a fresh Live session per turn) vs the re-baselined "
        f"{_GATE_MS} ms.\n\n"
        "| group | n | mean +/- stdev | p25 | p50 | p75 | p90 | p95 | p99 | min | max |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        f"{stats_row('cold (fresh session/turn)', cold_stats)}\n"
        f"{stats_row('warm (session reused)', warm_stats)}\n"
    )

    methodology = (
        "## Methodology\n\n"
        "- Single-clock per-request delta: `t_send` after "
        "`send_client_content(turn_complete=True)`, `t_first` on the first "
        "`inline_data.data` audio chunk; `ttfb_ms = (t_first - t_send) // 1e6`. "
        "Matches the production `tts.first_frame.ttfb_ms` log shape.\n"
        "- Cold: fresh `client.aio.live.connect()` per request (runtime path).\n"
        "- Warm: one session reused across the phase; each turn drained to "
        "`turn_complete` before the next send.\n"
        f"- Inter-request sleep: {int(_INTER_REQUEST_SLEEP_SEC * 1000)} ms "
        "(conservative for the undocumented preview-tier rate limit).\n"
        f"- Transcript pool: shared with the Story 6.1 Cartesia spike "
        f"(7 short ≤{SHORT_WORDS_MAX} words, 7 medium ≤{MEDIUM_WORDS_MAX}, "
        "6 long) for a like-for-like comparison.\n"
        f"- Cartesia baseline for reference: {_CARTESIA_BASELINE}.\n"
        "- Gemini output is 24 kHz mono s16le; this spike measures arrival "
        "timing only (no resample — that lands in Task 3's `GeminiClient`).\n"
    )

    raw_block = (
        "## Raw samples\n\n"
        "`mode,ttfb_ms,word_count,bucket,transcript` (cold first, then warm).\n\n"
        "```\n"
        + "\n".join(
            f"{s.mode},{s.ttfb_ms},{s.word_count},{s.bucket},{s.transcript[:80]}"
            for s in (*cold_samples, *warm_samples)
        )
        + "\n```\n"
    )

    return (
        "# Story 6.5 — Gemini Live-API TTFB gate\n\n"
        f"Generated: `{started_at.isoformat()}` -> `{finished_at.isoformat()}`\n\n"
        f"{verdict}\n"
        "## Run metadata\n\n"
        f"- live_model: `{live_model}`\n"
        f"- voice_name: `{voice_name}`\n"
        f"- style_prompt: `{style_prompt[:80]}{'…' if len(style_prompt) > 80 else ''}`\n"
        f"- cold samples requested: `{_COLD_SAMPLE_COUNT}` "
        f"(successful: `{cold_stats['n']}`, errors: `{cold_errors}`)\n"
        f"- warm samples requested: `{_WARM_SAMPLE_COUNT}` "
        f"(successful: `{warm_stats['n']}`, errors: `{warm_errors}`)\n"
        f"- gate (re-baselined): cold p50 ≤ `{_GATE_MS} ms` (≈ Cartesia production p50)\n"
        "- narration: verbatim system_instruction (model speaks the input "
        "exactly, no conversational reply)\n"
        f"- dev host: `{host}`\n"
        f"- google-genai SDK: `{sdk_version}`\n\n"
        f"{headline}\n"
        f"{methodology}\n"
        f"{raw_block}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_spike() -> int:
    """Drive the Gemini TTFB gate. Return 0 if any sample succeeded, else 1."""
    config = load_setup_config()
    configure_logging(config)

    if config.gemini_api_key is None:
        # Fail-fast with an operator-actionable message — the spike needs a
        # Google AI Studio key the google-genai SDK reads as GEMINI_API_KEY.
        raise VoiceAgentError(
            reason=(
                "gemini-ttfb-spike requires GEMINI_API_KEY in .env "
                "(Google AI Studio key). It is currently unset."
            ),
        )

    gemini_cfg = config.tts.gemini
    client = genai.Client(api_key=config.gemini_api_key.get_secret_value())

    host_str = f"{platform.system()} {platform.release()} / python {platform.python_version()}"
    try:
        host_str = f"{socket.gethostname()} / {host_str}"
    except OSError:
        pass

    started_at = datetime.now(tz=UTC)
    log.info(
        "gemini_ttfb_spike.start",
        cold_samples=_COLD_SAMPLE_COUNT,
        warm_samples=_WARM_SAMPLE_COUNT,
        live_model=gemini_cfg.live_model,
        voice_name=gemini_cfg.voice_name,
        host=host_str,
    )

    cold_samples, cold_errors = await _run_cold_phase(
        client,
        model=gemini_cfg.live_model,
        voice_name=gemini_cfg.voice_name,
        style_prompt=gemini_cfg.style_prompt,
    )
    log.info("gemini_ttfb_spike.cold_phase_complete", samples=len(cold_samples), errors=cold_errors)

    warm_samples, warm_errors = await _run_warm_phase(
        client,
        model=gemini_cfg.live_model,
        voice_name=gemini_cfg.voice_name,
        style_prompt=gemini_cfg.style_prompt,
    )
    log.info("gemini_ttfb_spike.warm_phase_complete", samples=len(warm_samples), errors=warm_errors)

    finished_at = datetime.now(tz=UTC)
    report = _build_report(
        cold_samples=cold_samples,
        warm_samples=warm_samples,
        cold_errors=cold_errors,
        warm_errors=warm_errors,
        voice_name=gemini_cfg.voice_name,
        live_model=gemini_cfg.live_model,
        style_prompt=gemini_cfg.style_prompt,
        sdk_version=getattr(genai, "__version__", "unknown"),
        host=host_str,
        started_at=started_at,
        finished_at=finished_at,
    )

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text(report, encoding="utf-8")  # noqa: ASYNC240 — bounded end-of-run IO

    log.info(
        "gemini_ttfb_spike.complete",
        total_successful=len(cold_samples) + len(warm_samples),
        total_errors=cold_errors + warm_errors,
        report_path=str(_REPORT_PATH),
    )
    return 0 if (cold_samples or warm_samples) else 1


def main() -> int:
    """CLI entry point — invoke :func:`run_spike` under :func:`asyncio.run`."""
    try:
        return asyncio.run(run_spike())
    except VoiceAgentError as e:
        print(f"gemini-ttfb-spike: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
