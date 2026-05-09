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
- Story 2.1 — promotes ``output_device_name`` to required (speaker output landed).
- Story 2.2 — adds nested ``TalkerConfig`` (provider-agnostic Talker
  with per-provider model sub-blocks for OpenAI / Groq / Gemini — all
  three reach the same ``openai`` SDK via their openai-compatible
  endpoints) and the corresponding ``.env`` keys (``OPENAI_API_KEY`` /
  ``GROQ_API_KEY`` / ``GEMINI_API_KEY``); only the active provider's
  key is required at startup. Operator swaps providers by changing one
  line in ``setup.toml``: ``[talker] provider = "<openai|groq|gemini>"``.
- Story 2.3 — adds nested ``TtsConfig`` (Cartesia Sonic-3 streaming
  TTS knobs) and the ``CARTESIA_API_KEY`` ``.env`` field.
- Stories 3.x / 4.x / 5.x — add their respective nested sections.

What this module deliberately does **not** do:

- Validate that credentials are reachable (per-service startup probes do this).
- Load ``expression_map.yaml`` (Story 3.1).
- Implement ``SIGHUP``-driven reload (Story 5.2).
- **Hard-fail** on loose ``.env`` permissions (NFR23 advisory in v1).
"""

import logging
import tomllib
from pathlib import Path
from typing import Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import ConfigError
from voice_agent_pipeline.schemas.mood_event import Mood

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


class _OpenAITalkerSection(BaseModel):
    """Per-provider sub-block: OpenAI model identifier (Story 2.2)."""

    model_config = ConfigDict(extra="forbid")

    model: str = "gpt-5.4-nano"


class _GroqTalkerSection(BaseModel):
    """Per-provider sub-block: Groq model identifier (Story 2.2).

    Groq hosts Llama 3.x family + others on their custom inference
    hardware — TTFB ~50-150 ms typical for the 8B Instant variant.
    Cost ~$0.05/M input, $0.08/M output: cheapest of the three majors.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = "llama-3.1-8b-instant"


class _GeminiTalkerSection(BaseModel):
    """Per-provider sub-block: Gemini model identifier (Story 2.2).

    Gemini exposes an openai-compatible endpoint at
    ``https://generativelanguage.googleapis.com/v1beta/openai/`` so the
    Talker reaches it via the same ``openai`` SDK Groq + OpenAI use —
    no separate ``google-genai`` dependency needed. Flash 2.5 is the
    latency / cost / reliability sweet spot in the Gemini family.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = "gemini-2.5-flash"


class TalkerConfig(BaseModel):
    """Provider-agnostic Talker (fast-path LLM) tuning knobs (Story 2.2).

    The Talker handles short conversational replies. Story 4.1 extends
    this config with belief-state grounded keys; Story 3.5 will rewrite
    the system prompt to instruct Cartesia SSML emission. v1 keeps the
    prompt plain-text-only so the listening half-loop ships before the
    splitter lands.

    Provider design (Story 2.2): three providers — OpenAI, Groq, Gemini
    — all reach the same ``openai`` Python SDK because each exposes an
    openai-compatible endpoint. A single concrete class
    (:class:`Talker`) parameterised by ``base_url`` + ``api_key`` +
    model identifier handles all three; the factory ``build_talker``
    dispatches on :attr:`provider` and supplies the matching values.

    The operator picks a provider by changing :attr:`provider`; each
    provider's model lives in its own sub-block so all three configs
    stay declared in ``setup.toml`` and the swap is a one-line edit.

    Attributes:
        provider: Which provider's endpoint to talk to. The factory
            ``build_talker`` indexes into the matching sub-block for
            model name + reads the matching ``.env`` API key.
        max_tokens: Generation length cap. 512 is enough for the "1-2
            sentence" reply style the system prompt enforces. Higher
            values risk verbose answers that blow NFR1.
        system_prompt_path: Path (project-root relative by convention)
            to the markdown file the Talker reads ONCE at construction.
            v1 ships ``prompts/talker_system.md``; the file is committed
            so the prompt evolves through git history rather than
            env-var twiddling.
        openai / groq / gemini: Per-provider model identifiers. Only
            the active provider's sub-block is consumed; the others
            stay around as ready-to-swap configurations.
        grounded_keys: Story 4.1 — list of belief-state keys the Talker
            requests via :class:`BeliefStateClient` at the start of each
            turn for grounded fast-path responses. Story 4.4 wires the
            actual call site (``complete_with_tools``); Story 4.1 ships
            the field. Empty list ≡ no grounding (v1 default). Operators
            opt in by setting e.g. ``grounded_keys = ["time",
            "calendar_today"]`` in ``setup.toml``'s ``[talker]`` block.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "groq", "gemini"] = "openai"
    max_tokens: int = Field(default=512, gt=0)
    system_prompt_path: Path = Path("prompts/talker_system.md")
    openai: _OpenAITalkerSection = Field(default_factory=_OpenAITalkerSection)
    groq: _GroqTalkerSection = Field(default_factory=_GroqTalkerSection)
    gemini: _GeminiTalkerSection = Field(default_factory=_GeminiTalkerSection)
    # Story 4.1: belief-state grounded keys. See class docstring above.
    # Story 4.4 plumbs ``BeliefStateClient.read(grounded_keys)`` into
    # ``complete_with_tools``; Story 4.1 only exposes the config field
    # so 4.4's wiring is purely a code change at the call site, no
    # config refactor.
    grounded_keys: list[str] = Field(default_factory=list)


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
            and (Story 2.4 onward) trigger a clarification prompt.
            ``exp(avg_logprob)`` units; 0.5 is conservative.
        clarification_prompts: List of operator-authored static strings
            used when STT confidence is below
            ``low_confidence_threshold``. Story 4.5 replaces Story 2.4's
            singular ``clarification_prompt`` with this list — picked
            at random per turn for variety. Story 3.7's `cdf3618`
            short-circuit emits the picked phrase verbatim as a
            ``TalkerResponseFrame`` (no LLM round-trip; same emission
            path as before, just a different string each time).

            Source-of-truth (2026-05-08): the actual strings live in
            ``setup.toml`` under ``[stt] clarification_prompts``, NOT
            as a Python constant. Defaults to ``[]`` so a missing TOML
            entry fails the model_validator with a clear startup
            error rather than silently using hidden Python defaults.
    """

    model_config = ConfigDict(extra="forbid")

    backend: str = "whisper-cpu"
    model: str = "small"
    compute_type: str = "int8"
    device: str = "auto"
    low_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    clarification_prompts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_clarification_prompts_non_empty(self) -> "SttConfig":
        """Reject ``clarification_prompts = []`` — ``random.choice`` would crash.

        The strings live in ``setup.toml`` (no Python defaults). An
        empty list at startup means the operator didn't populate the
        ``[stt] clarification_prompts`` block; raising here catches
        that before it manifests as a ``random.choice`` ``IndexError``
        on the first low-confidence turn.
        """
        if len(self.clarification_prompts) == 0:
            raise ValueError(
                "stt.clarification_prompts must contain at least one entry. "
                "Populate the [stt] clarification_prompts list in setup.toml.",
            )
        return self


class TtsConfig(BaseModel):
    """Cartesia Sonic-3 streaming TTS knobs (Story 2.3).

    The TTS client streams audio frames back as the model synthesizes,
    so the speaker can begin playing within ~200-400 ms of the
    request (NFR4 target). v1 ships against Cartesia's Sonic-3 model;
    a v2 swap to a self-hosted TTS engine would land behind the same
    :class:`TTSClient` Protocol with no caller changes.

    Attributes:
        voice_id: Cartesia voice ID. **Required** — operator picks one
            from https://play.cartesia.ai/voices and pastes the GUID
            here. No default; the project doesn't ship with an
            opinion about which voice OLAF should sound like.
        default_emotion: Cartesia emotion modifier applied to every
            request via the SDK's ``generation_config`` field. v1 ships
            "neutral"; Story 3.x will pass per-segment emotion from
            the streaming SSML splitter and override this field
            per-call. Allowed values are Cartesia's emotion catalog —
            see ``cartesia.types.GenerationConfigParam`` for the full
            list (60+ entries: ``neutral``, ``happy``, ``excited``,
            etc.).
        model: Cartesia model identifier. ``sonic-3`` is the v1
            default — Cartesia's flagship low-latency model.
        speed: Speech rate multiplier passed to Cartesia via
            ``generation_config.speed``. ``1.0`` is the model's
            natural rate; ``<1.0`` slows it down (better
            intelligibility), ``>1.0`` speeds up. Tessa voice
            specifically reads slightly fast at 1.0; ``0.9`` is a
            comfortable default.
    """

    model_config = ConfigDict(extra="forbid")

    voice_id: str
    default_emotion: str = "neutral"
    model: str = "sonic-3"
    speed: float = 0.9


class MoodConfig(BaseModel):
    """Mood module knobs (Story 3.6).

    The mood **enum** itself (the ``Mood`` Literal) is code-level —
    additions require a code change + Talker prompt update per
    architecture.md §"Mood enum lifecycle". Only the cooldown rate +
    initial value are operator-tunable here.

    Attributes:
        cooldown_publishes_per_hour: Sliding-window publish budget for
            the ``mood`` topic (NFR31, default 4). Bound to ``[1, 20]``
            — values outside that range either trivialize the cooldown
            (>20/hr defeats the rate-limit intent) or starve legitimate
            mood transitions (<1/hr). Story 5.5 calibration may tune.
        initial: Mood OLAF starts in. Default ``"calm"`` per the
            architecture's "neutral baseline" intent. Operators can
            override per machine, but the value must be one of the
            ``Mood`` Literal — pydantic rejects unknown values at
            startup.
    """

    model_config = ConfigDict(extra="forbid")

    cooldown_publishes_per_hour: int = Field(default=4, ge=1, le=20)
    initial: Mood = "calm"


class TopicNames(BaseModel):
    """Per-topic name mapping for the four-topic event publisher (Story 3.5).

    Operator-tunable per the agnostic-publisher boundary
    (memory: ``project_pipeline_scope_boundary.md``) — embodiment teams
    can wire their subscribers to whatever ROS 2 topic names match their
    naming conventions.

    Attributes:
        mood: Topic for :class:`MoodEvent`. Latched/transient_local
            durability per architecture.md §"Per-topic QoS".
        activity: Topic for :class:`ActivityEvent`. Latched.
        speech_emotion: Topic for :class:`SpeechEmotionEvent`. Volatile.
        vocalization: Topic for :class:`VocalizationEvent`. Volatile.
    """

    model_config = ConfigDict(extra="forbid")

    mood: str = "/olaf/mood"
    activity: str = "/olaf/activity"
    speech_emotion: str = "/olaf/speech_emotion"
    vocalization: str = "/olaf/vocalization"


class PublisherConfig(BaseModel):
    """Four-topic event publisher knobs (Story 3.5).

    Attributes:
        adapter: Which :class:`EventPublisher` implementation to wire
            up. ``"ros2"`` uses :class:`Ros2EventPublisher` (production).
            ``"log"`` uses :class:`LogEventPublisher` (in-memory; for
            local dev without a ROS 2 stack installed).
        dds_domain_id: ROS 2 DDS domain id. Must match the subscriber's
            domain. ``0`` is the conventional default.
        topics: Per-topic name mapping (defaults to the v1 production
            ``/olaf/<topic>`` paths).
    """

    model_config = ConfigDict(extra="forbid")

    adapter: Literal["ros2", "log"] = "ros2"
    dds_domain_id: int = 0
    topics: TopicNames = Field(default_factory=TopicNames)


class DaemonConfig(BaseModel):
    """Orchestrator daemon endpoint config (Story 4.1).

    The pipeline reads belief-state from the daemon (``GET /beliefs``,
    Story 4.1) and dispatches complex turns over SSE (``POST /turn``,
    Story 4.2) against this URL. v1 ships with localhost-only;
    LAN-reachable URLs require Story 5.3's shared-secret / mTLS
    hardening before the pipeline accepts them at startup.

    The :class:`field_validator` enforces:

    - Scheme is ``http://`` or ``https://`` (no other transports).
    - Trailing ``/`` is stripped at parse time so callers can write
      ``f"{config.daemon.url}/beliefs"`` without worrying about
      double-slashes.

    Why ``str`` not :class:`pydantic.HttpUrl`: ``HttpUrl`` enforces a
    trailing slash on serialization and stringifies awkwardly (URL
    object vs str), which makes ``f"{base}/beliefs"`` formatting
    surprising. A small ``field_validator`` gives the same checks
    with cleaner ergonomics.

    Attributes:
        url: Base URL of the orchestrator daemon. v1 default
            ``http://localhost:8001``; operators override per-machine.
        enabled: Dev-mode escape hatch. When ``False``, the pipeline
            skips the startup ``/health`` probe, constructs no
            orchestrator client, and passes ``beliefs=None`` to the
            Talker — which Story 4.4 already handles by skipping the
            belief-grounding section of the system prompt. Useful when
            the sibling orchestrator project isn't running locally and
            the operator only wants to exercise the simple-turn loop
            (Stories 1-3 + 4.4 / 4.5 / 4.6 paths). Story 4.7's slow-
            path branch raises a clear ``ConfigError`` if a turn ever
            escalates to the orchestrator with this flag off; until
            4.7 lands the existing ``NotImplementedError`` already
            gates that branch. Defaults to ``True`` so production
            deployments fail-fast per CLAUDE.md rule #4.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = "http://localhost:8001"
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        # Reject anything that's not http/https — file:// or arbitrary
        # schemes would silently break httpx; the operator should see a
        # clean ConfigError at startup instead.
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(f"daemon.url must start with http:// or https://; got {v!r}")
        # Strip a single trailing slash. Story 4.1's HttpBeliefStateClient
        # also rstrips defensively, but normalising at parse time keeps
        # the stored value canonical (helps tests + log readability).
        return v.rstrip("/")


class GoodbyeConfig(BaseModel):
    """Hardcoded goodbye phrases — played before the bot sleeps (Story 4.6+).

    When the LLM dispatches the ``go_to_sleep`` tool, the bot first
    plays its LLM-generated reply (which usually IS a goodbye-shaped
    phrase already), then plays one entry from this list as a
    predictable, deterministic signoff. Same static-random pattern as
    the wake greetings and clarification prompts — operator-authored
    text in setup.toml, picked at random per call.

    Why bolt on a hardcoded phrase when the LLM already said
    goodbye: the user wants a CONSISTENT signoff. The LLM's reply
    varies; the hardcoded phrase is the audible "I'm now going
    quiet" cue. Slight redundancy acceptable — both phrases are
    short.

    Validation: list must be non-empty (random.choice would
    crash). The model_validator catches that at startup.
    """

    model_config = ConfigDict(extra="forbid")

    phrases: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_phrases_non_empty(self) -> "GoodbyeConfig":
        """Reject ``phrases = []`` — random.choice would crash."""
        if len(self.phrases) == 0:
            raise ValueError(
                "goodbye.phrases must contain at least one entry. "
                "Populate the [goodbye] phrases list in setup.toml.",
            )
        return self


class RouterConfig(BaseModel):
    """TurnRouter routing configuration (Story 4.7).

    The :class:`TurnRouter` (in :mod:`turn.router`) decides per-turn
    whether a transcript routes to the in-pipeline Talker (fast path)
    or escalates to the orchestrator daemon (slow path). v1 uses a
    simple regex-pattern match against the transcript:

    - If the transcript matches any pattern in
      :attr:`slow_path_patterns`, route to ``"orchestrator"``.
    - Otherwise, route to :attr:`default` (v1 default ``"talker"``).

    The patterns are case-insensitive and operator-tunable. Patterns
    are compiled once at :class:`TurnRouter` construction; invalid
    regexes raise :class:`ConfigError` at startup.

    Attributes:
        slow_path_patterns: Operator-defined regex strings. Empty
            default — operators opt in by adding entries. Each pattern
            is compiled with ``re.IGNORECASE``.
        default: Fallback target when no slow-path pattern matches.
            v1 default ``"talker"``; setting to ``"orchestrator"``
            means EVERY high-confidence turn goes to the orchestrator
            (sometimes useful for offline-LLM dev or for soak-testing
            the slow path).
    """

    model_config = ConfigDict(extra="forbid")

    slow_path_patterns: list[str] = Field(default_factory=list)
    default: Literal["talker", "orchestrator"] = "talker"


class GreetingConfig(BaseModel):
    """Per-mood wake-greeting bucket lists (Story 4.5).

    The wake greeting fires automatically on every ``sleeping →
    waking`` FSM transition. The 2026-05-07 redesign replaced the
    earlier Talker-driven approach (LLM call + 800ms timeout +
    word-count gate + fallback list) with a static random pick from
    per-mood bucket lists. Trade-off: lose LLM novelty per call;
    gain zero per-call latency, zero cost, zero hallucination risk,
    zero "LLM treated my prompt as a question" failure mode (Story
    3.7's `cdf3618` had to work around the same class of bug for
    clarification).

    The :func:`trigger_greeting` function in
    :mod:`voice_agent_pipeline.activity.greeting` reads from this
    dict at runtime — no caching, no preprocessing.

    Source-of-truth design (2026-05-08): the actual greeting strings
    live in ``setup.toml`` under ``[greeting.greetings_by_mood]``,
    NOT as a Python constant. This keeps operator-tunable text in
    the config file where operators expect it, and removes data
    duplication. The committed ``setup.toml`` ships with starter
    lists (~10 entries per mood) so the project works out of the
    box; operators expand to 30-40 over time.

    Validation:

    - The :class:`model_validator` asserts every value in the
      :data:`Mood` Literal has at least one entry. Missing moods or
      empty buckets raise :class:`ConfigError` at startup so
      operators don't hit a silent fallback in production.
    - pydantic's ``dict[Mood, list[str]]`` annotation rejects keys
      that aren't in the :data:`Mood` Literal — typos like
      ``"ecstatic"`` fail loudly at parse time.

    Attributes:
        greetings_by_mood: Mapping of mood → list of greeting
            strings. Defaults to an empty dict so a missing TOML
            block fails the model_validator with a clear error
            (rather than silently using hidden Python defaults).
            Operators populate via ``setup.toml``.
    """

    model_config = ConfigDict(extra="forbid")

    # Annotated empty-dict default. Explicit annotation here lets
    # pyright resolve ``greetings_by_mood``'s type as
    # ``dict[Mood, list[str]]`` rather than ``dict[Unknown, Unknown]``
    # (which is what ``Field(default_factory=dict)`` infers).
    greetings_by_mood: dict[Mood, list[str]] = Field(
        default_factory=lambda: dict[Mood, list[str]](),
    )

    @model_validator(mode="after")
    def _validate_all_moods_have_entries(self) -> "GreetingConfig":
        """Every :data:`Mood` Literal value must have ≥1 greeting."""
        all_moods = set(get_args(Mood))
        present = {m for m, entries in self.greetings_by_mood.items() if entries}
        missing = all_moods - present
        if missing:
            raise ValueError(
                f"greeting.greetings_by_mood missing or empty entries for mood(s): "
                f"{sorted(missing)}. Populate them under [greeting.greetings_by_mood] "
                f"in setup.toml.",
            )
        return self


class ToolsConfig(BaseModel):
    """Toggle Talker's individual tools on/off (Story 4.4).

    The Talker tool registry surfaces a small fixed set of tools to the
    LLM via the openai SDK's ``tools=`` parameter. Operators sometimes
    want to disable a tool — e.g., during early dev when the
    ``go_to_sleep`` deferred-sleep chain isn't fully wired, or while
    iterating on the mood enum. Toggling here removes the tool from
    ``ToolRegistry.as_openai_tools_param()`` so the LLM never sees it
    and can never emit a tool call for it.

    Both default ``True``: production wants the full surface live.
    The architecture's tool registry (architecture.md §"Tool registry")
    intentionally exposes ``go_to_sleep`` and ``set_mood`` as the v1
    set; this config flag is operational, not architectural.

    Attributes:
        enable_go_to_sleep: When ``False``, the registry omits the
            ``go_to_sleep`` tool. The LLM cannot trigger the FR46
            deferred-sleep chain via natural-language goodbye. Useful
            for testing the simple-turn loop without sleep
            interactions.
        enable_set_mood: When ``False``, the registry omits the
            ``set_mood`` tool. Mood transitions can still happen
            through other paths (calibration commits, future story
            hooks); only the LLM's natural-language mood-shift hook
            is gated off.
    """

    model_config = ConfigDict(extra="forbid")

    enable_go_to_sleep: bool = True
    enable_set_mood: bool = True


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
    # Provider-agnostic Talker (Story 2.2): all three are optional in the
    # SetupConfig. The factory ``build_talker`` enforces "the matching key
    # is present for the active provider" at startup, so a misconfigured
    # combo (e.g., provider="groq" but no GROQ_API_KEY in .env) fails fast
    # with a clear ConfigError naming the missing field.
    openai_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    # Cartesia (Story 2.3): single TTS provider in v1; required for
    # the Cartesia client + startup probe.
    cartesia_api_key: SecretStr
    audio: AudioConfig
    wakeword: WakewordConfig
    vad: VadConfig = Field(default_factory=VadConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    talker: TalkerConfig = Field(default_factory=TalkerConfig)
    tts: TtsConfig
    # Story 3.5: four-topic event publisher. Required at startup —
    # the broadcast bus is a hard dep (architecture.md §"V1 Posture:
    # Hard Dependencies, Fail-Fast"). Operators set adapter="log" for
    # dev runs without a ROS 2 stack installed.
    publisher: PublisherConfig = Field(default_factory=PublisherConfig)
    # Story 3.6: mood module knobs. Optional with defaults — operators
    # don't have to declare [mood] in setup.toml unless they want to
    # tune the cooldown rate or starting mood.
    mood: MoodConfig = Field(default_factory=MoodConfig)
    # Story 4.4: Talker tool registry toggles. Optional with defaults
    # (both tools enabled). Operators disable individual tools to remove
    # them from the LLM's openai-tools surface.
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    # Story 4.5: wake-greeting buckets. Required at startup —
    # the model_validator catches missing/empty mood buckets. The
    # actual strings live in setup.toml, not in Python defaults.
    greeting: GreetingConfig = Field(default_factory=GreetingConfig)
    # Story 4.7: TurnRouter slow-path escalation. Optional; empty
    # default means every high-confidence turn routes to Talker (v1
    # original behavior). Operators add patterns to enable orchestrator
    # escalation.
    router: RouterConfig = Field(default_factory=RouterConfig)
    # 2026-05-09: hardcoded goodbye phrases played before the bot
    # sleeps. The actual strings live in setup.toml; an empty list
    # fails the model_validator at startup.
    goodbye: GoodbyeConfig = Field(default_factory=GoodbyeConfig)
    # Story 4.1: orchestrator daemon endpoint. Optional with defaults
    # (localhost:8001). Story 4.1 wires the BeliefStateClient against
    # this URL; Story 4.2 adds the orchestrator slow-path SSE consumer
    # against the same URL. Operators only override [daemon] when the
    # daemon runs on a non-default host/port.
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)


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
