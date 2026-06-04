"""Cached audio: pre-rendered WAVs for deterministic-text surfaces (Story 5.5).

This module is the third allowed ``pyaudio`` import site in the codebase
(alongside ``audio/devices.py`` and ``audio/transport.py``) per the
architecture's boundary-concentration rule. Callers (``sequential_loop``,
``__main__.py``'s Stage 3 probe) speak through this module's typed surface
rather than touching :mod:`pyaudio` directly.

Story 5.5 trades runtime Cartesia calls for cached WAV playback on
four deterministic-text surfaces:

- **Wake greetings** — ``[greeting.greetings_by_mood]`` per Story 4.5
- **Goodbyes** — ``[goodbye] phrases`` per the 2026-05-09 commit
- **Clarifications** — ``[stt] clarification_prompts`` per Story 2.4
- **Openers** — ``[openers.phrases_by_bucket]`` per Story 6.2
  (function-bucketed; supersedes Story 5.5's mood-bucketed
  ``[filler]`` surface, which was retired in Story 6.2 — manifest
  schema bumped 1 → 2)

Each phrase is rendered once via ``just regenerate-audio`` (which calls
:class:`voice_agent_pipeline.tts.cartesia.CartesiaClient` per phrase),
written to ``assets/audio/<surface>[/<mood>]/NN.wav``, and indexed in
``assets/audio/manifest.json``. The Stage 3 startup probe
(:func:`load_and_validate_manifest`) refuses to start if any phrase from
``setup.toml`` is missing from the manifest or if any manifest entry's
file is missing from disk — operators see a clean "run
``just regenerate-audio``" instruction.

Confidence on stale assets: the manifest records the ``voice_id`` and
``tts_model`` used to generate. If either changes in ``setup.toml`` (an
operator picks a different Cartesia voice, or Cartesia ships a new model
version they want to use), the probe rejects until regeneration runs.
Same goes for added/removed/edited phrases — each entry carries a
``phrase_hash = sha256(phrase + voice_id + tts_model)`` so a phrase
edit is detectable without comparing all the strings.

Audio format pinned to 16 kHz mono S16LE — the pipeline-wide format
shared by :mod:`audio.transport`, :mod:`audio.devices`'s probe, and
:mod:`stt.groq`'s WAV-wrap step. Regeneration MUST emit this format;
the WAV-header validator in :func:`load_manifest` checks it.
"""

import asyncio
import hashlib
import json
import wave
from datetime import datetime
from pathlib import Path
from typing import Literal

import pyaudio
import structlog
from pydantic import BaseModel, ConfigDict, Field, model_validator

from voice_agent_pipeline.audio._silence import suppress_native_stderr
from voice_agent_pipeline.audio.opener_bucket import OpenerBucket
from voice_agent_pipeline.config.setup import SetupConfig
from voice_agent_pipeline.errors import StartupValidationError
from voice_agent_pipeline.schemas.mood_event import Mood

log = structlog.get_logger(__name__)


# Audio format pin — MUST match `audio/transport.py:_SAMPLE_RATE`. Any
# drift here vs there produces audible glitches (resample mismatch) or
# a `paFormatNotSupported` from the device. Kept duplicated rather than
# imported because reaching across modules for a constant adds coupling
# without a real benefit.
_SAMPLE_RATE_HZ = 16_000
_CHANNELS = 1
_SAMPLE_WIDTH_BYTES = 2  # paInt16

# Default manifest location — overridable by tests.
_DEFAULT_MANIFEST_PATH = Path("assets/audio/manifest.json")

# Manifest schema version. Bumped on any field rename/removal in
# :class:`CachedAudioEntry` or :class:`CachedAudioManifest`. Operators
# whose manifest's version doesn't match the build's expected version
# must regenerate — the validator rejects mismatched versions.
#
# Story 6.2 bumped 1 -> 2: dropped the ``"filler"`` surface, added
# ``"opener"`` surface + ``bucket`` field on entries. Operators
# pulling Story 6.2 see a StartupValidationError on first restart
# until they run ``just regenerate-audio``.
_MANIFEST_SCHEMA_VERSION = 2

# Stream chunk size for streaming WAV bytes to PyAudio. 4096 frames @
# 16 kHz = ~256 ms per write — small enough to keep the event loop
# responsive (each ``stream.write`` blocks until the OS buffer has
# room, naturally rate-limiting to playback speed) but large enough
# that the ``asyncio.to_thread`` overhead doesn't dominate.
_PLAYBACK_CHUNK_FRAMES = 4096

# Trailing drain so the OS finishes the audio buffer before stream
# close. Mirrors ``sequential_loop._AUDIO_DRAIN_TAIL_MS``.
_AUDIO_DRAIN_TAIL_MS = 250


# Story 6.2: "filler" dropped, "opener" added. Manifest schema bumped
# 1 -> 2 in the same story (see ``_MANIFEST_SCHEMA_VERSION`` above).
CachedAudioSurface = Literal["greeting", "goodbye", "clarification", "opener"]


class CachedAudioEntry(BaseModel):
    """Single phrase → rendered WAV file mapping.

    Frozen pydantic model — manifest entries are immutable once
    loaded. The :attr:`phrase_hash` is the cache key; it changes when
    any of phrase / voice_id / tts_model / (mood | bucket) changes,
    forcing regeneration.

    Surface / mood / bucket relationship (Story 6.2):

    - ``surface = "greeting"`` → mood-bucketed: ``mood is not None``
      and ``bucket is None``.
    - ``surface = "opener"`` → function-bucketed: ``bucket is not
      None`` and ``mood is None``.
    - ``surface in ("goodbye", "clarification")`` → flat: both
      ``mood`` and ``bucket`` are ``None``.

    The ``model_validator`` below enforces this exactly so the
    constraint can't drift silently.

    Attributes:
        surface: Which surface this phrase belongs to. Determines the
            on-disk path prefix (``assets/audio/<surface>/...``).
        mood: For mood-bucketed surfaces (greeting) the current
            :data:`Mood` Literal value. ``None`` for openers + the
            flat surfaces (goodbye, clarification).
        bucket: For function-bucketed surfaces (opener) the
            :data:`OpenerBucket` Literal value. ``None`` for
            mood-bucketed + flat surfaces. Added in Story 6.2.
        phrase_hash: ``sha256(phrase + voice_id + tts_model + (mood
            OR bucket))`` — cache invalidation key. Hex-encoded; first
            16 chars are sufficient for collision-resistance at our
            scale (~200 phrases) and keep the manifest readable.
        phrase: The literal text. Stored alongside the hash for
            human-readability and to support the regenerator's
            "add/remove/edit" diff at next run.
        path: Repo-relative path to the WAV file. Kept relative so
            the manifest is location-independent (assets/ can move
            into a submodule or sibling repo without breaking the
            manifest).
        duration_ms: Wall-clock duration of the rendered audio.
            Recorded so opener-timing logic can decide whether the
            opener will fit the expected gap before the real response
            arrives.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: CachedAudioSurface
    mood: Mood | None = None
    bucket: OpenerBucket | None = None
    phrase_hash: str
    phrase: str
    path: str
    duration_ms: int

    @model_validator(mode="after")
    def _validate_surface_mood_bucket_consistency(self) -> "CachedAudioEntry":
        """Enforce the (surface, mood, bucket) one-of-the-other rule."""
        if self.surface == "greeting":
            if self.mood is None or self.bucket is not None:
                raise ValueError(
                    "surface='greeting' requires mood set and bucket None; "
                    f"got mood={self.mood!r}, bucket={self.bucket!r}",
                )
        elif self.surface == "opener":
            if self.bucket is None or self.mood is not None:
                raise ValueError(
                    "surface='opener' requires bucket set and mood None; "
                    f"got mood={self.mood!r}, bucket={self.bucket!r}",
                )
        else:  # goodbye, clarification
            if self.mood is not None or self.bucket is not None:
                raise ValueError(
                    f"surface={self.surface!r} requires mood=None and bucket=None; "
                    f"got mood={self.mood!r}, bucket={self.bucket!r}",
                )
        return self


class CachedAudioManifest(BaseModel):
    """Top-level manifest of all rendered cached audio.

    Loaded once at startup by :func:`load_manifest` and threaded down
    to the call sites. Immutable thereafter — regeneration produces a
    new manifest file, the next process restart loads it.

    Attributes:
        schema_version: Manifest format version. Currently 1; bumps
            when entry/manifest fields are renamed or removed in a way
            that breaks JSON round-trip.
        generated_at: UTC timestamp of the last regeneration. Surfaces
            in startup logs for diagnostics.
        voice_id: Cartesia voice id used to render every entry.
            Recorded so :func:`load_and_validate_manifest` can refuse
            startup if the operator changes ``[tts] voice_id`` in
            ``setup.toml`` without regenerating.
        tts_model: Cartesia model identifier (e.g. ``"sonic-3"``).
            Recorded for the same staleness-detection reason.
        entries: List of :class:`CachedAudioEntry`. The lookup methods
            below scan this list.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int
    generated_at: datetime
    voice_id: str
    tts_model: str
    entries: list[CachedAudioEntry] = Field(default_factory=lambda: [])

    def lookup(
        self,
        surface: CachedAudioSurface,
        phrase: str,
        mood: Mood | None = None,
        bucket: OpenerBucket | None = None,
    ) -> CachedAudioEntry:
        """Find the entry matching ``(surface, phrase, mood, bucket)``.

        Linear scan — at ~200 entries the cost is microseconds and
        avoids carrying a duplicate index dict. Called once per
        greeting/goodbye/clarification/opener emission.

        Args:
            surface: Which surface the phrase belongs to.
            phrase: The literal text to look up. Must match the
                manifest entry's ``phrase`` exactly (case-sensitive,
                whitespace-sensitive). Operators editing ``setup.toml``
                MUST run ``just regenerate-audio`` after any text
                change for this lookup to succeed.
            mood: For mood-bucketed surfaces (greeting), the bucket
                to look in. For other surfaces, must be ``None``.
            bucket: For function-bucketed surfaces (opener), the
                bucket to look in. For other surfaces, must be ``None``.

        Raises:
            KeyError: If no entry matches. The error names the missing
                tuple so callers can produce a useful log/error. This
                is a *programming error* at the call site OR a stale
                manifest — the startup probe should have caught the
                latter, so a runtime miss means the call site is
                asking for a phrase that isn't in setup.toml.
        """
        for entry in self.entries:
            if (
                entry.surface == surface
                and entry.phrase == phrase
                and entry.mood == mood
                and entry.bucket == bucket
            ):
                return entry
        raise KeyError(
            f"no cached audio for surface={surface!r}, phrase={phrase!r}, "
            f"mood={mood!r}, bucket={bucket!r}",
        )

    def phrases_for_surface(
        self,
        surface: CachedAudioSurface,
    ) -> list[CachedAudioEntry]:
        """Return every entry for the given surface — used by validation.

        Order matches the manifest's ``entries`` order, which is the
        regenerator's traversal order over the TOML lists. Not stable
        across regenerations if phrases are added in the middle, but
        the random-pick logic in callers doesn't depend on stability.
        """
        return [e for e in self.entries if e.surface == surface]


def compute_phrase_hash(
    phrase: str,
    voice_id: str,
    tts_model: str,
    mood: Mood | None,
    bucket: OpenerBucket | None = None,
) -> str:
    """Compute the cache key for a (phrase, mood, bucket) rendered with a voice + model.

    Hex-encoded SHA-256, truncated to 16 chars. At our scale (~200
    phrases) collision probability is negligible (~10^-19) and a short
    hash keeps the manifest readable. The full SHA-256 would buy
    cryptographic-strength uniqueness we don't need.

    Why mood / bucket is part of the hash: phrases legitimately repeat
    across buckets — "yeah?" is in ``calm``, ``curious``, and
    ``sleepy`` greeting buckets; ``"yeah"`` is in the ``acknowledge``
    opener bucket. Without the discriminator in the hash, those
    collapse to a single manifest entry, and the runtime lookup
    (which keys on ``(surface, phrase, mood | bucket)``) fails for
    the missing slots at first fire.

    Story 6.2 added ``bucket`` to the hash for the opener surface.
    The hash input encodes whichever discriminator is non-None
    (greetings pass ``mood``; openers pass ``bucket``; flat surfaces
    pass neither and the ``__none__`` sentinel lands).

    Args:
        phrase: The literal text to render.
        voice_id: Cartesia voice id from ``[tts] voice_id``.
        tts_model: Cartesia model from ``[tts] model``.
        mood: Mood bucket the phrase belongs to (for greetings).
            ``None`` for openers + flat surfaces.
        bucket: Opener bucket the phrase belongs to (for openers).
            ``None`` for greetings + flat surfaces.

    Returns:
        16-char hex string suitable as a manifest key.
    """
    h = hashlib.sha256()
    # NUL separator prevents any cross-field collision (e.g., phrase
    # ending in the voice_id prefix). Belt-and-braces given the input
    # space is well-behaved (ASCII text), but the cost is one byte.
    h.update(phrase.encode("utf-8"))
    h.update(b"\x00")
    h.update(voice_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(tts_model.encode("utf-8"))
    h.update(b"\x00")
    # Encode the discriminator: mood OR bucket OR the sentinel. The
    # Mood and OpenerBucket Literals have disjoint value sets (mood
    # values are emotions; bucket values are function names) so even
    # if a future contributor accidentally passed both, the hash
    # would still discriminate. Pass exactly one in practice.
    discriminator = mood or bucket or "__none__"
    h.update(discriminator.encode("utf-8"))
    return h.hexdigest()[:16]


def load_manifest(path: Path = _DEFAULT_MANIFEST_PATH) -> CachedAudioManifest:
    """Parse ``manifest.json`` into a :class:`CachedAudioManifest`.

    Args:
        path: Path to the manifest file. Defaults to the canonical
            location ``assets/audio/manifest.json``. Tests pass a
            ``tmp_path``-based override.

    Returns:
        Validated :class:`CachedAudioManifest`.

    Raises:
        StartupValidationError: If the file is missing, malformed, or
            its ``schema_version`` doesn't match this build's expected
            version. The error names the action ("run
            ``just regenerate-audio``") so operators don't have to dig.
    """
    if not path.exists():
        raise StartupValidationError(
            stage="audio_assets",
            reason=f"manifest not found at {path}",
            action="run `just regenerate-audio`",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StartupValidationError(
            stage="audio_assets",
            reason=f"manifest at {path} is not valid JSON: {e}",
            action="run `just regenerate-audio`",
        ) from e
    try:
        manifest = CachedAudioManifest.model_validate(raw)
    except Exception as e:
        # ValidationError or any other pydantic error; we don't need
        # to type-narrow because both surface the same way to the
        # operator — schema doesn't match, regenerate.
        raise StartupValidationError(
            stage="audio_assets",
            reason=f"manifest at {path} failed schema validation: {e}",
            action="run `just regenerate-audio`",
        ) from e
    if manifest.schema_version != _MANIFEST_SCHEMA_VERSION:
        raise StartupValidationError(
            stage="audio_assets",
            reason=(
                f"manifest schema_version={manifest.schema_version} but this "
                f"build expects {_MANIFEST_SCHEMA_VERSION}"
            ),
            action="run `just regenerate-audio`",
        )
    return manifest


def load_and_validate_manifest(
    config: SetupConfig,
    manifest_path: Path = _DEFAULT_MANIFEST_PATH,
) -> CachedAudioManifest:
    """Startup probe — load the manifest and verify it matches ``setup.toml``.

    Called from ``__main__.py`` Stage 3 after the
    ``audio devices openable`` probe. Verifies four invariants:

    1. **Manifest loads** (file exists, valid JSON, schema_version match).
    2. **Voice + model match** — ``manifest.voice_id`` equals
       ``config.tts.effective_voice_id()`` and ``manifest.tts_model`` equals
       ``config.tts.effective_model()``. Any drift means assets were rendered with
       a different voice and would sound wrong; regenerate to fix.
    3. **Every required phrase has an entry** — for each phrase in the
       four ``setup.toml`` surface lists, a manifest entry with the
       matching ``phrase_hash`` exists. Catches "operator added a
       phrase, forgot to regenerate".
    4. **Every manifest entry's file exists** — catches "manifest was
       committed but some WAVs were lost / weren't committed".

    Args:
        config: Validated :class:`SetupConfig` from the loader.
        manifest_path: Where to find the manifest. Tests override.

    Returns:
        The validated :class:`CachedAudioManifest`. Caller threads
        this through to ``run_sequential_loop`` so the runtime
        lookups don't re-parse JSON.

    Raises:
        StartupValidationError: Any of the four invariants violated.
            The error's ``context`` carries enough detail (missing
            phrases, mismatched voice id, ...) for the operator to
            diagnose without reading source.
    """
    manifest = load_manifest(manifest_path)

    # Invariant 2: voice + model match.
    if manifest.voice_id != config.tts.effective_voice_id():
        raise StartupValidationError(
            stage="audio_assets",
            reason=(
                f"manifest voice_id={manifest.voice_id!r} but setup.toml has "
                f"voice_id={config.tts.effective_voice_id()!r}"
            ),
            action="run `just regenerate-audio`",
        )
    if manifest.tts_model != config.tts.effective_model():
        raise StartupValidationError(
            stage="audio_assets",
            reason=(
                f"manifest tts_model={manifest.tts_model!r} but setup.toml has "
                f"model={config.tts.effective_model()!r}"
            ),
            action="run `just regenerate-audio`",
        )

    # Invariant 3: every required phrase has an entry. Build the set
    # of expected phrase_hashes from setup.toml; the diff against the
    # manifest is the operator-actionable list. Story 6.2 swapped the
    # filler enumeration for the opener enumeration; greetings/
    # goodbyes/clarifications unchanged.
    expected_hashes: dict[str, tuple[str, str, str | None]] = {}
    for mood, bucket in config.greeting.greetings_by_mood.items():
        for phrase in bucket:
            h = compute_phrase_hash(
                phrase, config.tts.effective_voice_id(), config.tts.effective_model(), mood
            )
            expected_hashes[h] = ("greeting", phrase, mood)
    for phrase in config.goodbye.phrases:
        h = compute_phrase_hash(
            phrase, config.tts.effective_voice_id(), config.tts.effective_model(), None
        )
        expected_hashes[h] = ("goodbye", phrase, None)
    for phrase in config.stt.clarification_prompts:
        h = compute_phrase_hash(
            phrase, config.tts.effective_voice_id(), config.tts.effective_model(), None
        )
        expected_hashes[h] = ("clarification", phrase, None)
    for opener_bucket, phrases in config.openers.phrases_by_bucket.items():
        for phrase in phrases:
            h = compute_phrase_hash(
                phrase,
                config.tts.effective_voice_id(),
                config.tts.effective_model(),
                mood=None,
                bucket=opener_bucket,
            )
            expected_hashes[h] = ("opener", phrase, opener_bucket)

    have_hashes = {e.phrase_hash for e in manifest.entries}
    missing = set(expected_hashes.keys()) - have_hashes
    if missing:
        # Report the first few missing phrases in the error — full
        # list logged separately so the message stays readable.
        sample = [expected_hashes[h] for h in list(missing)[:5]]
        raise StartupValidationError(
            stage="audio_assets",
            reason=(
                f"{len(missing)} phrase(s) in setup.toml have no cached audio. Sample: {sample}"
            ),
            action="run `just regenerate-audio`",
        )

    # Invariant 4: every manifest entry's file exists.
    missing_files: list[str] = []
    for entry in manifest.entries:
        if not Path(entry.path).exists():
            missing_files.append(entry.path)
    if missing_files:
        raise StartupValidationError(
            stage="audio_assets",
            reason=(
                f"{len(missing_files)} WAV file(s) referenced by the manifest are "
                f"missing on disk. Sample: {missing_files[:5]}"
            ),
            action="run `just regenerate-audio`",
        )

    log.info(
        "audio_assets.manifest_loaded",
        entries=len(manifest.entries),
        voice_id=manifest.voice_id,
        tts_model=manifest.tts_model,
    )
    return manifest


async def play_cached(
    pa: pyaudio.PyAudio,
    output_index: int,
    path: Path,
) -> None:
    """Stream a cached WAV file to the configured speaker.

    Mirrors :func:`voice_agent_pipeline.sequential_loop._speak`'s shape
    so the call-site swap (Story 5.5 Task 5) is mechanical. Half-duplex
    contract: blocks until playback finishes. Mic input is NOT recorded
    during this window — the caller must have closed any open input
    stream before calling.

    Args:
        pa: Live ``PyAudio`` instance. Stays open across calls —
            the loop owns its lifecycle.
        output_index: Resolved speaker device index from
            :func:`audio.devices.resolve_audio_devices`.
        path: Repo-relative or absolute path to the WAV file.

    Raises:
        FileNotFoundError: If ``path`` doesn't exist. The startup
            probe should have caught this; runtime hits mean the
            file was deleted post-startup.
        wave.Error: If the file isn't a valid WAV or doesn't match
            the 16 kHz mono S16LE format. Same provenance — startup
            probe should catch format mismatches, but defensive in
            case the regenerator wrote an unexpected format.
    """
    log.info("cached_audio.play.start", path=str(path))

    # Open WAV outside the suppress block — header validation errors
    # should be visible. Stream.open inside the suppress block
    # because PyAudio prints "ALSA lib pcm.c:..." noise to stderr on
    # device open.
    with wave.open(str(path), "rb") as wav:
        # Defensive format check — should never trip in production
        # since the regenerator produces matching format, but cheap
        # to verify and useful when debugging a manual asset edit.
        if wav.getframerate() != _SAMPLE_RATE_HZ:
            raise wave.Error(
                f"cached audio {path} has rate {wav.getframerate()} Hz, "
                f"expected {_SAMPLE_RATE_HZ} Hz",
            )
        if wav.getnchannels() != _CHANNELS:
            raise wave.Error(
                f"cached audio {path} has {wav.getnchannels()} channels, expected {_CHANNELS}",
            )
        if wav.getsampwidth() != _SAMPLE_WIDTH_BYTES:
            raise wave.Error(
                f"cached audio {path} has sample width {wav.getsampwidth()} bytes, "
                f"expected {_SAMPLE_WIDTH_BYTES}",
            )

        with suppress_native_stderr():
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=_CHANNELS,
                rate=_SAMPLE_RATE_HZ,
                output=True,
                output_device_index=output_index,
            )
        try:
            byte_total = 0
            chunk_count = 0
            while True:
                # readframes returns up to N frames, possibly fewer at
                # end of file (returns b"" when exhausted).
                pcm = wav.readframes(_PLAYBACK_CHUNK_FRAMES)
                if not pcm:
                    break
                chunk_count += 1
                byte_total += len(pcm)
                # Same off-thread blocking write as _speak — PyAudio's
                # write blocks until the OS audio buffer has room.
                await asyncio.to_thread(stream.write, pcm)
            # Trailing drain — same constant as _speak so the perceived
            # tail length matches between cached and runtime-TTS paths.
            await asyncio.sleep(_AUDIO_DRAIN_TAIL_MS / 1000)
            log.info(
                "cached_audio.play.complete",
                chunk_count=chunk_count,
                byte_total=byte_total,
            )
        finally:
            stream.stop_stream()
            stream.close()
