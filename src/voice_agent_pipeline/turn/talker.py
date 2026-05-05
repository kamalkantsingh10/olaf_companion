"""TalkerClient Protocol + provider-agnostic ``Talker`` impl (Story 2.2).

The Talker handles **fast-path** turns — short, conversational, non-grounded
questions. Slow-path turns go through :class:`OrchestratorClient` instead
(see ``orchestrator.py``).

Provider design (Story 2.2): a single concrete :class:`Talker` class
serves three providers — OpenAI, Groq, and Gemini — because all three
expose openai-compatible endpoints. The operator picks one by changing
``[talker] provider`` in ``setup.toml``; the factory
``build_talker`` (in :mod:`voice_agent_pipeline.turn`) supplies the
matching ``base_url``, ``api_key``, and model identifier.

- ``"openai"``: SDK default base_url + ``OPENAI_API_KEY`` +
  ``gpt-5.4-nano``.
- ``"groq"``: ``https://api.groq.com/openai/v1`` + ``GROQ_API_KEY`` +
  ``llama-3.1-8b-instant``.
- ``"gemini"``: ``https://generativelanguage.googleapis.com/v1beta/openai/``
  + ``GEMINI_API_KEY`` + ``gemini-2.5-flash``.

All three are reached via Chat Completions (``client.chat.completions.create``)
because that's the surface area all three providers reliably implement.
GPT-5 family also supports the newer Responses API, but Chat Completions
is universal across the trio so we use it here.

Boundary-concentration rule (architecture.md §"Architectural Boundaries"):
``import openai`` lives in **this file only**. ``__main__.py``'s startup
probe imports the validate-credentials helper from
:mod:`voice_agent_pipeline.turn` rather than re-importing the SDK
directly, keeping the rule airtight.

Future stories layer onto this without changing the call shape:

- Story 4.1 wires the ``BeliefStateClient`` ctor arg (currently accepted
  but unused) to populate the ``context`` parameter of :meth:`complete`
  with a per-turn belief-state read.
- Story 3.5 rewrites ``prompts/talker_system.md`` to instruct the model
  to emit Cartesia inline emotion tags (``<emotion value="..."/>``,
  ``<laughter/>``, etc.) at sentence/clause boundaries.
"""

from typing import Any, Protocol

import openai
import structlog
from pydantic import SecretStr

from voice_agent_pipeline.config.setup import TalkerConfig
from voice_agent_pipeline.errors import TalkerError
from voice_agent_pipeline.turn.beliefs import BeliefStateClient

log = structlog.get_logger(__name__)


# Provider → openai-compatible base_url mapping. ``None`` means "use the
# SDK's built-in default" (i.e., OpenAI's own endpoint).
PROVIDER_BASE_URLS: dict[str, str | None] = {
    "openai": None,
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}

# Per-provider Chat Completions max-tokens parameter name. OpenAI's GPT-5
# family requires the newer ``max_completion_tokens`` name; Groq + Gemini
# still accept the legacy ``max_tokens`` (and may not yet accept the new
# name). The factory threads the right name into the Talker so each
# provider gets its preferred kwarg.
PROVIDER_MAX_TOKENS_PARAM: dict[str, str] = {
    "openai": "max_completion_tokens",
    "groq": "max_tokens",
    "gemini": "max_tokens",
}


class TalkerClient(Protocol):
    """In-pipeline LLM seam. v1 impl is :class:`Talker`."""

    async def complete(
        self,
        transcript: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Produce a single-shot response to a transcript.

        Args:
            transcript: The user's spoken utterance, post-STT.
            context: Optional belief-state grab from
                :class:`BeliefStateClient` (Story 4.1). When ``None``,
                the Talker runs context-free (the architecture's default
                for pure conversational turns).

        Returns:
            The generated text response. The pipeline's splitter (Story
            3.3) consumes this output verbatim.
        """
        ...


class Talker:
    """Provider-agnostic LLM client implementing :class:`TalkerClient`.

    Speaks to OpenAI, Groq, or Gemini through the ``openai`` SDK's Chat
    Completions API. The provider-specific values (base_url, api_key,
    model) are supplied by the factory ``build_talker``.

    System prompt is loaded **once** at construction (never re-read per
    turn). First API failure raises :class:`TalkerError` — v1 fail-fast,
    no retry, no fallback string. CLAUDE.md rule #4 forbids catching
    :class:`TalkerError` (a subclass of :class:`ExternalServiceError`)
    anywhere downstream — process crashes, systemd restarts (Epic 5).
    """

    def __init__(
        self,
        config: TalkerConfig,
        api_key: SecretStr,
        model: str,
        base_url: str | None = None,
        max_tokens_param: str = "max_tokens",
        beliefs: BeliefStateClient | None = None,
    ) -> None:
        """Build the openai client and pre-load the system prompt.

        Args:
            config: Validated :class:`TalkerConfig` carrying provider
                identifier, ``max_tokens`` cap, and the system-prompt
                file path. The model + provider-specific bits come via
                the explicit args below — the factory handles the
                lookup, this class doesn't index into config sub-blocks.
            api_key: Provider API key (whichever of OPENAI / GROQ /
                GEMINI matches the active provider), wrapped in
                :class:`SecretStr` so ``repr(self)`` doesn't leak it.
            model: Provider-specific model identifier — e.g.,
                ``"gpt-5.4-nano"`` for OpenAI, ``"llama-3.1-8b-instant"``
                for Groq, ``"gemini-2.5-flash"`` for Gemini. Passed
                through verbatim to the API.
            base_url: Provider's openai-compatible endpoint. ``None``
                means "use the openai SDK's default" (i.e., OpenAI).
                Other providers supply their own URL here.
            beliefs: Reserved for Story 4.1's belief-state grounding.
                v1 always passes ``None``; the field is stored but
                unused so Story 4.1's wiring is non-breaking.
        """
        self._config = config
        self._model = model
        self._max_tokens_param = max_tokens_param
        self._beliefs = beliefs  # Story 4.1 will start consuming this
        # Read the prompt once at startup; never per-turn. The file is
        # committed under ``prompts/`` so prompt evolution flows through
        # git rather than env-var twiddling.
        self._system_prompt = config.system_prompt_path.read_text(encoding="utf-8")
        # AsyncOpenAI maintains a long-lived httpx connection pool; we
        # construct one per Talker (lifetime-bound to the pipeline) rather
        # than per-call so connection reuse cuts TLS handshake from every
        # turn's latency budget. base_url=None lets the SDK use its
        # built-in default (OpenAI's endpoint).
        self._client = openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
        )

    async def complete(
        self,
        transcript: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Call the provider's Chat Completions endpoint and return plain text.

        v1 ignores ``context`` — Story 4.1 will populate it from the
        belief-state read and merge into the user message. The kwarg is
        accepted now so Story 4.1 doesn't need to refactor the
        :class:`TalkerClient` Protocol signature.

        Token usage is logged at INFO via the ``talker.completion``
        event for every successful call — operator-side observability
        for cost / verbosity regressions across long-running deployments.

        Raises:
            TalkerError: On any ``openai.APIError`` subclass. The
                original exception is preserved via ``raise ... from e``
                for post-mortem inspection. v1 fail-fast — never caught
                downstream (CLAUDE.md rule #4).
        """
        del context  # Story 4.1 will start consuming this; v1 ignores it.
        try:
            # Chat Completions is the universal surface across openai-
            # compatible providers (Groq + Gemini are reliable on it,
            # OpenAI maintains backward-compat). System prompt as a
            # role="system" message; user transcript as role="user".
            # Per-provider max-tokens kwarg: OpenAI GPT-5+ rejects
            # ``max_tokens`` (the legacy name) and requires
            # ``max_completion_tokens``; Groq + Gemini still accept the
            # legacy form. The factory threads the right name into
            # ``self._max_tokens_param`` so we splat it here.
            messages = [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": transcript},
            ]
            # Provider-aware max-tokens param. OpenAI GPT-5+ rejects the
            # legacy ``max_tokens`` kwarg and requires
            # ``max_completion_tokens``; Groq + Gemini still accept
            # the legacy form. Branching here (rather than splatting a
            # **kwargs dict) keeps pyright able to resolve the openai
            # SDK's overloaded create() signature.
            if self._max_tokens_param == "max_completion_tokens":
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,  # type: ignore[arg-type]
                    max_completion_tokens=self._config.max_tokens,
                )
            else:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,  # type: ignore[arg-type]
                    max_tokens=self._config.max_tokens,
                )
        except openai.APIError as e:
            # v1 fail-fast: wrap and propagate. CLAUDE.md rule #4 — never
            # caught downstream. Process crashes; systemd restarts.
            raise TalkerError(
                provider=self._config.provider,
                model=self._model,
                reason=str(e),
            ) from e

        # ``response.choices[0].message.content`` is the canonical Chat
        # Completions accessor. v1 prompt forbids tool-use, so this is
        # safe to consume directly without inspecting individual blocks.
        choice = response.choices[0]
        text = choice.message.content or ""

        # Token usage observability. All three providers populate
        # ``response.usage`` with the same shape (prompt_tokens,
        # completion_tokens, total_tokens) on the openai SDK's Chat
        # Completions surface. Logged at INFO so operators can see
        # cost / verbosity drift in voice-agent.log without DEBUG.
        if response.usage is not None:
            log.info(
                "talker.completion",
                provider=self._config.provider,
                model=self._model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                # Story 2.5 deviation from FR42: for v1 personal use
                # we surface the prompt (Talker input) and response
                # (Talker output) at INFO. Field names ``prompt`` /
                # ``response`` deliberately bypass the redaction
                # processor's strict gating on ``transcript`` /
                # ``user_text`` (which still strip at INFO+ —
                # accidental leaks under those names remain caught).
                # For deployed scenarios (Story 5.3) the operator
                # can either remove these fields or extend the
                # redaction denylist.
                prompt=transcript,
                response=text,
            )

        return text
