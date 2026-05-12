"""Offline asset-regenerator CLI: pre-render cached WAVs via Cartesia (Story 5.5).

Run with::

    just regenerate-audio                  # idempotent — skips unchanged phrases
    just regenerate-audio --force          # regenerate every entry
    just regenerate-audio --dry-run        # print what would change, no API calls

Pipeline lifecycle:

1. Load ``setup.toml`` + ``.env`` via :func:`load_setup_config`.
2. Enumerate every phrase across four surfaces: greetings (mood-bucketed),
   goodbyes, clarifications, fillers (mood-bucketed).
3. Compute ``phrase_hash = sha256(phrase + voice_id + tts_model)[:16]``.
4. Compare against the existing ``manifest.json`` (if present).

   - Phrases whose hash is already in the manifest AND whose file exists
     → SKIP (unless ``--force``).
   - Phrases not in the manifest → RENDER via Cartesia, write WAV, update
     manifest.
   - Manifest entries whose phrase is no longer in ``setup.toml`` → PRUNE
     (manifest entry removed; WAV file deleted).

5. Write the updated ``manifest.json`` to ``assets/audio/manifest.json``.

This CLI is intentionally NOT part of the pipeline runtime — it's an
operator tool. CLAUDE.md rule #4 ("never catch ExternalServiceError in
v1 code paths") applies to the runtime; here we DO catch
:class:`CartesiaError` because a clean error message ("Cartesia returned
402; top up at <url>") is more useful to the operator than a stack trace
and a non-zero exit.

Boundary-concentration: this module is the second Cartesia caller
(alongside :mod:`tts.cartesia`). It reuses :class:`CartesiaClient` —
no new SDK surface, no duplicate API code.
"""

import argparse
import asyncio
import json
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path

import structlog

from voice_agent_pipeline.audio.cached import (
    CachedAudioEntry,
    CachedAudioManifest,
    compute_phrase_hash,
)
from voice_agent_pipeline.config.setup import SetupConfig, load_setup_config
from voice_agent_pipeline.errors import CartesiaError, VoiceAgentError
from voice_agent_pipeline.logging.setup import configure_logging
from voice_agent_pipeline.schemas.mood_event import Mood
from voice_agent_pipeline.tts.cartesia import CartesiaClient

log = structlog.get_logger(__name__)


# Audio format pin — matches ``audio/cached.py:_SAMPLE_RATE_HZ`` and the
# pipeline-wide format. The recipe writes WAVs in this format; the Stage 3
# probe verifies playback files match it.
_SAMPLE_RATE_HZ = 16_000
_CHANNELS = 1
_SAMPLE_WIDTH_BYTES = 2

# Canonical paths. ``ASSETS_ROOT`` is repo-relative so the manifest stays
# location-independent.
_ASSETS_ROOT = Path("assets/audio")
_MANIFEST_PATH = _ASSETS_ROOT / "manifest.json"

# Per-surface subdirectory names. Plural because the surface refers to a
# collection (greetings, goodbyes, ...).
_SURFACE_SUBDIRS: dict[str, str] = {
    "greeting": "greetings",
    "goodbye": "goodbyes",
    "clarification": "clarifications",
    "filler": "fillers",
}


def _plan_phrases(
    config: SetupConfig,
) -> list[tuple[str, str, Mood | None]]:
    """Enumerate every (surface, phrase, mood) tuple in setup.toml.

    Returns a stable order: greetings first (by mood), then goodbyes,
    clarifications, fillers. Within each mood bucket, the order
    matches the TOML list order. The regenerator uses this order
    to assign NN sequence numbers in filenames, so the same TOML
    yields the same filenames across runs.
    """
    plan: list[tuple[str, str, Mood | None]] = []
    # Greetings — mood-bucketed. Sort by mood key for determinism
    # across runs (Python dict iteration order is insertion-order
    # but the TOML loader may surprise us across pydantic versions).
    for mood in sorted(config.greeting.greetings_by_mood.keys()):
        for phrase in config.greeting.greetings_by_mood[mood]:
            plan.append(("greeting", phrase, mood))
    # Flat lists — goodbye + clarification.
    for phrase in config.goodbye.phrases:
        plan.append(("goodbye", phrase, None))
    for phrase in config.stt.clarification_prompts:
        plan.append(("clarification", phrase, None))
    # Fillers — mood-bucketed, same pattern as greetings.
    for mood in sorted(config.filler.phrases_by_mood.keys()):
        for phrase in config.filler.phrases_by_mood[mood]:
            plan.append(("filler", phrase, mood))
    return plan


def _path_for(
    surface: str,
    mood: Mood | None,
    sequence_in_bucket: int,
) -> Path:
    """Build the canonical on-disk path for a (surface, mood, N) triple.

    Format: ``assets/audio/<surface-plural>/[<mood>/]NN.wav`` —
    zero-padded 2-digit sequence number within the mood bucket (or
    within the flat surface for goodbye/clarification).
    """
    subdir = _SURFACE_SUBDIRS[surface]
    if mood is None:
        return _ASSETS_ROOT / subdir / f"{sequence_in_bucket:02d}.wav"
    return _ASSETS_ROOT / subdir / mood / f"{sequence_in_bucket:02d}.wav"


async def _render_phrase_to_wav(
    cartesia_client: CartesiaClient,
    phrase: str,
    out_path: Path,
) -> int:
    """Call Cartesia, accumulate PCM, write a WAV. Returns duration_ms.

    The PCM is buffered fully before write — fine for ~200 short phrases.
    For long-form rendering we'd stream straight to a WAV file via
    ``wave.Wave_write`` updating the header at close, but that adds
    complexity we don't need here.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pcm_chunks: list[bytes] = []
    async for chunk in cartesia_client.synthesize(phrase):
        pcm_chunks.append(chunk)
    pcm = b"".join(pcm_chunks)

    # Frame count → duration. Frame = one sample across all channels =
    # SAMPLE_WIDTH * CHANNELS bytes. 16 kHz mono int16 → 2 bytes/frame.
    frame_count = len(pcm) // (_SAMPLE_WIDTH_BYTES * _CHANNELS)
    duration_ms = int(frame_count * 1000 / _SAMPLE_RATE_HZ)

    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(_CHANNELS)
        wav.setsampwidth(_SAMPLE_WIDTH_BYTES)
        wav.setframerate(_SAMPLE_RATE_HZ)
        wav.writeframes(pcm)
    return duration_ms


def _load_existing_manifest() -> dict[str, CachedAudioEntry]:
    """Load ``manifest.json`` if present; return a hash→entry dict.

    Missing or malformed manifest is treated as "no cache" — every
    phrase will be rendered. This is the right behavior for a fresh
    clone or a corrupted manifest.
    """
    if not _MANIFEST_PATH.exists():
        return {}
    try:
        raw = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest = CachedAudioManifest.model_validate(raw)
    except Exception as e:
        log.warning(
            "regenerate.existing_manifest_invalid",
            reason=str(e),
        )
        return {}
    return {e.phrase_hash: e for e in manifest.entries}


async def regenerate(
    *,
    force: bool = False,
    dry_run: bool = False,
) -> int:
    """Render any missing/changed cached audio; prune stale entries.

    Args:
        force: When True, re-render every entry even if the hash matches
            an existing file. Useful after a Cartesia model update where
            the model id didn't change but the voice quality did.
        dry_run: When True, only print the plan; no API calls, no writes.

    Returns:
        Process exit code — 0 on success, 1 on any per-phrase render
        failure (the script attempts every phrase before returning so
        operators see a complete list of failures, not the first one).
    """
    config = load_setup_config()
    configure_logging(config)

    plan = _plan_phrases(config)
    existing = _load_existing_manifest()

    voice_id = config.tts.voice_id
    tts_model = config.tts.model
    log.info(
        "regenerate.start",
        total_phrases=len(plan),
        existing_entries=len(existing),
        voice_id=voice_id,
        tts_model=tts_model,
        force=force,
        dry_run=dry_run,
    )

    # Build the new manifest entries from the plan. Per-surface/mood
    # sequence counters give stable NN.wav filenames across runs.
    counters: dict[tuple[str, Mood | None], int] = {}
    new_entries: list[CachedAudioEntry] = []
    to_render: list[tuple[CachedAudioEntry, str]] = []  # (entry, phrase)

    for surface, phrase, mood in plan:
        key = (surface, mood)
        counters[key] = counters.get(key, 0) + 1
        seq = counters[key]
        path = _path_for(surface, mood, seq)
        phrase_hash = compute_phrase_hash(phrase, voice_id, tts_model, mood)

        # Cache hit logic, two paths:
        #
        # (a) hash hit — existing manifest entry has this exact hash
        #     AND its referenced file still exists. Reuse the entry,
        #     possibly relocating its file to the canonical path if
        #     the per-bucket sequence number shifted.
        # (b) path hit — a file ALREADY exists at the canonical path
        #     for this (surface, mood, seq) slot, but the manifest's
        #     hash is from a stale schema (e.g., the 2026-05-12
        #     mood-included hash migration). Reuse the file in place.
        #
        # The second path is what makes the mood-included-hash
        # migration cheap: we don't re-render audio that was already
        # synthesized at the right path, even though the hash key
        # changed.
        # ruff ASYNC240 warns about pathlib in async functions; this is
        # an operator-tool, not a runtime hot path — blocking IO is
        # intentional and bounded.
        cached = existing.get(phrase_hash)
        if cached is not None and Path(cached.path).exists() and not force:  # noqa: ASYNC240
            # Reuse the existing entry but update path to the canonical
            # slot (in case sequence shifted due to phrase add/remove).
            # If the path differs from canonical, MOVE the WAV in place.
            if cached.path != str(path):
                if not dry_run:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    Path(cached.path).rename(path)  # noqa: ASYNC240
                log.info(
                    "regenerate.move",
                    phrase=phrase,
                    from_path=cached.path,
                    to_path=str(path),
                )
            entry = CachedAudioEntry(
                surface=surface,  # type: ignore[arg-type]
                mood=mood,
                phrase_hash=phrase_hash,
                phrase=phrase,
                path=str(path),
                duration_ms=cached.duration_ms,
            )
            new_entries.append(entry)
            continue

        # Path-hit fallback (Story 5.5 migration path): the manifest
        # doesn't have this exact hash (e.g., hash schema changed), but
        # a file already lives at the canonical path. Reuse it without
        # calling Cartesia. Probe the WAV header for duration so the
        # new manifest entry carries the right value.
        if path.exists() and not force:
            with wave.open(str(path), "rb") as wav:
                duration_ms = int(wav.getnframes() * 1000 / wav.getframerate())
            entry = CachedAudioEntry(
                surface=surface,  # type: ignore[arg-type]
                mood=mood,
                phrase_hash=phrase_hash,
                phrase=phrase,
                path=str(path),
                duration_ms=duration_ms,
            )
            new_entries.append(entry)
            log.info(
                "regenerate.reuse_by_path",
                phrase=phrase,
                path=str(path),
                duration_ms=duration_ms,
            )
            continue

        # Cache miss — needs rendering.
        entry = CachedAudioEntry(
            surface=surface,  # type: ignore[arg-type]
            mood=mood,
            phrase_hash=phrase_hash,
            phrase=phrase,
            path=str(path),
            duration_ms=0,  # filled in after render
        )
        to_render.append((entry, phrase))
        log.info(
            "regenerate.render_queued",
            phrase=phrase,
            path=str(path),
            reason="forced" if force else "missing",
        )

    # Dry-run early exit — print the plan summary.
    if dry_run:
        log.info(
            "regenerate.dry_run_summary",
            to_render=len(to_render),
            to_skip=len(plan) - len(to_render),
        )
        return 0

    # Render the missing phrases. Sequential rather than parallel —
    # Cartesia's TTFB is the bottleneck and rate-limiting is gentler
    # serially. ~1-2 minutes for a fresh ~200-phrase regeneration.
    failure_count = 0
    if to_render:
        cartesia_client = CartesiaClient(config.tts, config.cartesia_api_key)
        for entry, phrase in to_render:
            try:
                duration_ms = await _render_phrase_to_wav(
                    cartesia_client,
                    phrase,
                    Path(entry.path),
                )
            except CartesiaError as e:
                # Operator tool — catching here gives a clean per-phrase
                # error instead of crashing on the first failure.
                log.error(
                    "regenerate.render_failed",
                    phrase=phrase,
                    reason=str(e),
                )
                failure_count += 1
                continue
            # Replace the placeholder entry with one carrying the real
            # duration. model_copy mirrors pydantic's frozen-model idiom.
            final_entry = entry.model_copy(update={"duration_ms": duration_ms})
            # Swap the placeholder in new_entries with the final.
            for i, e in enumerate(new_entries):
                if e.phrase_hash == final_entry.phrase_hash:
                    new_entries[i] = final_entry
                    break
            else:
                new_entries.append(final_entry)
            log.info(
                "regenerate.rendered",
                phrase=phrase,
                duration_ms=duration_ms,
                path=entry.path,
            )

    # Prune stale entries — manifest entries whose hash isn't in the
    # new plan AND whose phrase is gone from setup.toml.
    new_hashes = {e.phrase_hash for e in new_entries}
    for stale_hash, stale_entry in existing.items():
        if stale_hash in new_hashes:
            continue
        log.info(
            "regenerate.prune",
            phrase=stale_entry.phrase,
            path=stale_entry.path,
        )
        # Remove the WAV file if it exists. Best-effort — if the file
        # is gone we don't care. Same noqa rationale as the cache-hit
        # rename above (operator-tool, bounded IO).
        try:
            Path(stale_entry.path).unlink(missing_ok=True)  # noqa: ASYNC240
        except OSError as e:
            log.warning(
                "regenerate.prune_failed",
                path=stale_entry.path,
                reason=str(e),
            )

    # Write the new manifest. Atomic write via tmp file + rename so a
    # crash mid-write doesn't leave a half-written manifest.
    manifest = CachedAudioManifest(
        schema_version=1,
        generated_at=datetime.now(tz=UTC),
        voice_id=voice_id,
        tts_model=tts_model,
        entries=new_entries,
    )
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _MANIFEST_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    tmp_path.rename(_MANIFEST_PATH)

    log.info(
        "regenerate.complete",
        entries=len(new_entries),
        rendered=len(to_render) - failure_count,
        skipped=len(plan) - len(to_render),
        pruned=len(existing) - sum(1 for h in existing if h in new_hashes),
        failed=failure_count,
    )
    return 1 if failure_count > 0 else 0


def main() -> int:
    """CLI entry point — parse args, run :func:`regenerate`, return exit code."""
    parser = argparse.ArgumentParser(
        prog="regenerate-audio",
        description=(
            "Pre-render Story 5.5 cached audio (greetings, goodbyes, "
            "clarifications, thinking fillers) via Cartesia. Idempotent: "
            "skips phrases whose hash is already in the manifest."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-render every phrase regardless of cache state",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan, make no API calls or filesystem changes",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(regenerate(force=args.force, dry_run=args.dry_run))
    except VoiceAgentError as e:
        # Top-level VoiceAgentError (most likely ConfigError from the
        # setup loader). Print to stderr and exit non-zero — operator
        # sees a clean message without a Python traceback.
        print(f"regenerate-audio: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
