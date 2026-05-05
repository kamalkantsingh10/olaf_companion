"""``setup.toml`` + ``.env`` loader with ``pydantic-settings`` schema validation.

This module is the **substrate** every later story reads from. The
:class:`SetupConfig` model defines the typed contract for the project's
configuration; subsequent stories extend it by adding nested sub-models.
The ``extra="forbid"`` rule means a typo in ``setup.toml`` fails loudly at
startup instead of silently at runtime — that is the whole point of v1's
fail-fast posture (architecture.md §"V1 Posture: Hard Dependencies, Fail-Fast").

Story progression for this module:

- Story 1.2 — landed the model + ``schema_version`` + ``picovoice_access_key``.
- Story 1.5 — added nested ``AudioConfig`` for mic/speaker device names.
- Story 1.6 — adds nested ``WakewordConfig`` for Porcupine model + sensitivity.
- Story 1.7 — adds nested ``VadConfig`` (Silero VAD timing) and ``SttConfig``
  (faster-whisper backend selection + confidence threshold).
- Stories 2.x / 3.x / 4.x / 5.x — add their respective nested sections.

What this module deliberately does **not** do:

- Validate that credentials are reachable (per-service startup probes do this).
- Load ``expression_map.yaml`` (Story 3.1).
- Implement ``SIGHUP``-driven reload (Story 5.2).
- **Hard-fail** on loose ``.env`` permissions (NFR23 advisory in v1).
"""

import logging
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import ConfigError

# Module-level logger. Test fixtures patch this one specifically when
# asserting on the loose-perms warning (see tests/unit/config/test_setup.py).
log = logging.getLogger(__name__)


class AudioConfig(BaseModel):
    """Mic + speaker device name regexes (Story 1.5 / 2.1).

    Both are regex strings matched (case-insensitive, ``re.search`` semantics)
    against PyAudio's enumerated device names. Pinning by name regex is the
    architecture's standard fix for PyAudio's index instability across
    reboots and USB hot-plug events (architecture.md §"Audio + STT
    Pipeline").

    Attributes:
        input_device_name: Regex for the microphone. Required from Story 1.5
            onward — without it, the pipeline can't capture audio.
        output_device_name: Regex for the speaker. **Required from Story 2.1**
            (when speaker output landed via ``transport.output()``). Operators
            discover the right pattern with ``just list-devices`` and verify
            it with ``just play-test-tone``.
    """

    # extra="forbid" so a typo like ``input_device_namee`` fails loudly at
    # startup instead of silently selecting the default device.
    model_config = ConfigDict(extra="forbid")

    input_device_name: str
    output_device_name: str


class WakewordConfig(BaseModel):
    """Picovoice Porcupine wake-word knobs (Story 1.6).

    Attributes:
        model_path: Path to the trained ``.ppn`` file (project-root relative).
            The file is committed under ``models/wakeword/`` per
            architecture.md §"Architectural Boundaries". The Picovoice
            access key itself lives in ``.env`` as ``PICOVOICE_ACCESS_KEY``
            and is loaded onto :attr:`SetupConfig.picovoice_access_key`.
        sensitivity: Detection threshold in ``[0.0, 1.0]``. Higher = more
            sensitive (more true positives, more false positives). Default
            ``0.5`` is the conservative starting point per architecture's
            "favor FN over FP" guidance; Story 5.5's soak finalizes the
            value.
    """

    model_config = ConfigDict(extra="forbid")

    model_path: Path
    # ge/le bounds match Porcupine's API; pydantic enforces at parse time.
    sensitivity: float = Field(default=0.5, ge=0.0, le=1.0)


class VadConfig(BaseModel):
    """Silero VAD timing knobs — bounds the captured utterance (Story 1.7).

    The pipeline's :class:`VadProcessor` consumes pipecat's bundled Silero
    VAD analyzer. The analyzer reports per-chunk voice probability; this
    config controls how that probability is converted into "speech started"
    and "speech stopped" decisions, plus the minimum utterance length we
    accept (filters out short noises / cough / "uh").

    Attributes:
        silence_duration_ms: How long we wait in continuous silence after
            speech ends before emitting the captured utterance. Tuned
            empirically; 700ms covers natural between-word pauses without
            cutting people off.
        min_speech_duration_ms: Utterances shorter than this are silently
            dropped. Filters cough, throat-clearing, and accidental key
            presses on the mic.
        start_threshold: Silero confidence value at which we consider
            speech to have started. ``0.5`` is the model's neutral point.
        end_threshold: Silero confidence below which we consider speech
            to have stopped. Lower than ``start_threshold`` (0.35) gives
            a hysteresis band that prevents flapping at the start/stop
            boundary.
    """

    model_config = ConfigDict(extra="forbid")

    silence_duration_ms: int = Field(default=700, gt=0)
    min_speech_duration_ms: int = Field(default=250, gt=0)
    start_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    end_threshold: float = Field(default=0.35, ge=0.0, le=1.0)


class SttConfig(BaseModel):
    """STT backend selection + tuning knobs (Story 1.7).

    The :func:`build_stt_backend` factory in
    :mod:`voice_agent_pipeline.stt` reads :attr:`backend` and switches on it.
    v1 supports only ``"whisper-cpu"``; v2 adds ``"hailo-whisper"`` for
    Pi 5 + Hailo-8L deployments — the Protocol seam from Story 1.4 keeps
    callers unchanged across that swap.

    Attributes:
        backend: Backend identifier. v1: ``"whisper-cpu"``.
        model: faster-whisper model size — ``"tiny" / "base" / "small" /
            "medium" / "large-v3"``. ``"small"`` is the dev-host default
            (~500 MB, ~150ms inference per 1s audio on a modern CPU).
        compute_type: faster-whisper compute precision. ``"int8"`` is the
            CPU sweet-spot — ~3x faster than ``"float16"`` with negligible
            accuracy loss for English transcription.
        device: ``"cpu" / "cuda" / "auto"``. ``"auto"`` consults
            ``torch.cuda.is_available()`` if torch is importable; falls
            back to ``"cpu"`` otherwise. v2's Hailo backend ignores this
            field.
        low_confidence_threshold: Transcripts with confidence below this
            value emit an additional ``stt.low_confidence`` WARN log,
            and (in Story 2.4 onward) trigger a clarification prompt.
            ``exp(avg_logprob)`` units; 0.5 is conservative.
    """

    model_config = ConfigDict(extra="forbid")

    backend: str = "whisper-cpu"
    model: str = "small"
    compute_type: str = "int8"
    device: str = "auto"
    low_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class SetupConfig(BaseSettings):
    """Typed top-level configuration for the voice-agent pipeline.

    pydantic-settings populates fields from two sources:

    - The TOML payload passed in via :func:`load_setup_config` (used for
      ``schema_version``, the nested config blocks like ``audio``, and any
      future TOML-backed fields).
    - The ``.env`` file pointed at by ``_env_file`` (used for
      ``picovoice_access_key`` and any future credentials).

    Class attributes:
        schema_version: Integer version marker; must match
            :data:`SUPPORTED_SCHEMA_VERSION`. Lives in ``setup.toml``.
        picovoice_access_key: Picovoice / Porcupine access key, stored as
            :class:`SecretStr` so accidental ``repr(config)`` doesn't leak it.
        audio: Nested :class:`AudioConfig` carrying mic + speaker regexes.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )

    schema_version: int
    picovoice_access_key: SecretStr
    audio: AudioConfig
    wakeword: WakewordConfig
    vad: VadConfig = Field(default_factory=VadConfig)
    stt: SttConfig = Field(default_factory=SttConfig)


def load_setup_config(
    toml_path: Path = Path("setup.toml"),
    env_path: Path = Path(".env"),
) -> SetupConfig:
    """Load + validate ``setup.toml`` and ``.env`` into a :class:`SetupConfig`.

    Steps, in order:

    1. Verify both files exist; ``ConfigError`` on miss.
    2. Parse the TOML via stdlib :mod:`tomllib` (no extra dep).
    3. Pass the parsed dict + ``_env_file`` to :class:`SetupConfig`. pydantic
       runs its full validation pass — including ``extra="forbid"`` for the
       TOML keys (top-level and nested) and presence checks for required
       ``.env`` vars. Translate any ``ValidationError`` into
       :class:`ConfigError`.
    4. Cross-check ``schema_version`` against :data:`SUPPORTED_SCHEMA_VERSION`.
    5. Advisory: warn (don't fail) if ``.env`` permissions are looser than
       ``0o600`` (NFR23).

    Args:
        toml_path: Path to ``setup.toml`` (cwd-relative by default).
        env_path: Path to ``.env`` (cwd-relative by default).

    Returns:
        A fully validated :class:`SetupConfig` instance.

    Raises:
        ConfigError: For any missing-file, parse-failure, or validation issue.
        SchemaVersionError: When ``setup.toml``'s ``schema_version`` does not
            match the value this build supports.
    """
    if not toml_path.exists():
        raise ConfigError(missing_file=str(toml_path))
    if not env_path.exists():
        raise ConfigError(missing_file=str(env_path))

    # tomllib requires binary mode (it controls its own decoding).
    with toml_path.open("rb") as f:
        toml_data = tomllib.load(f)

    try:
        # _env_file is pydantic-settings' way to override the config-class
        # default at construction time (e.g. for tests using tmp_path).
        config = SetupConfig(**toml_data, _env_file=str(env_path))  # type: ignore[arg-type]
    except ValidationError as e:
        # Wrap the pydantic error so callers only catch our error hierarchy.
        raise ConfigError(toml_path=str(toml_path), validation=str(e)) from e

    # Schema version is intentionally NOT a field-level pydantic validator.
    # Keeping it as a separate call lets Story 1.4 reuse the same helper for
    # event-payload schema_version checks at parse boundaries.
    assert_schema_version(config.schema_version, source=str(toml_path))

    _warn_if_env_perms_loose(env_path)
    return config


def _warn_if_env_perms_loose(env_path: Path) -> None:
    """Log a WARN if ``.env``'s POSIX mode bits are looser than ``0o600``.

    Advisory only — v1 deliberately does not refuse to start (NFR23). Silently
    no-ops on platforms where ``stat()`` fails (e.g. tightly-confined containers).
    """
    try:
        # Mask away type / setuid bits — we only care about the
        # owner/group/other permission triplet.
        mode = env_path.stat().st_mode & 0o777
    except OSError:
        return

    if mode != 0o600:
        log.warning(
            "config.env.permissions_loose",
            extra={
                "actual_mode": oct(mode),
                "recommended": "0o600",
                "path": str(env_path),
            },
        )
