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

import json
from typing import Any, Protocol

import openai
import structlog
from openai.types.chat import ChatCompletionMessageFunctionToolCall
from pydantic import BaseModel, ConfigDict, SecretStr

from voice_agent_pipeline.config.setup import TalkerConfig
from voice_agent_pipeline.errors import TalkerError
from voice_agent_pipeline.turn.beliefs import BeliefStateClient
from voice_agent_pipeline.turn.tools import ToolCall, ToolRegistry

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


class TalkerResponse(BaseModel):
    """Typed return value of :meth:`Talker.complete_with_tools` (Story 4.4).

    The tool-using Talker emits two outputs per turn — the
    user-facing text reply (consumed by the splitter / TTS) and a
    list of structured tool calls (dispatched by the
    :class:`~voice_agent_pipeline.turn.tools.ToolRegistry`). They
    are returned together so the dispatcher
    (:class:`TurnDispatchProcessor`) can emit text FIRST and tools
    SECOND — the ordering that preserves FR45 / FR46 semantics
    (user hears the goodbye before mic mode flips).

    Attributes:
        text: Plain-text reply. Empty string when the LLM emitted
            only tool calls (openai returns ``content=None``); the
            dispatcher still pushes a ``TalkerResponseFrame("")``
            downstream so observers see the turn boundary.
        tool_calls: Parsed list of :class:`ToolCall` instances. Each
            already had its ``arguments`` JSON-decoded by the Talker
            — the registry's input-schema validation operates on
            the parsed dict, not raw text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    tool_calls: list[ToolCall]


class TalkerClient(Protocol):
    """In-pipeline LLM seam. v1 impl is :class:`Talker`.

    Two methods. :meth:`complete` is the legacy (Story 2.2) shape —
    text-only response, no tools. Production stopped calling it after
    Story 4.4 wired the dispatcher to :meth:`complete_with_tools`.
    Kept on the Protocol for backward compatibility with Story 2.2 /
    2.4 unit tests; new callers should prefer ``complete_with_tools``.
    """

    async def complete(
        self,
        transcript: str,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Produce a single-shot response to a transcript (Story 2.2 — legacy).

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

    async def complete_with_tools(
        self,
        prompt: str,
        tool_registry: ToolRegistry,
    ) -> TalkerResponse:
        """Tool-using turn — emit text + parsed tool calls (Story 4.4).

        Args:
            prompt: The user's transcript (or, for clarification turns,
                the configured clarification prompt — see Story 2.4).
            tool_registry: The :class:`ToolRegistry` whose
                :meth:`as_openai_tools_param` shape goes to the openai
                SDK as ``tools=``. The registry isn't called from
                within ``complete_with_tools`` — its
                :meth:`~ToolRegistry.dispatch` runs from the
                :class:`TurnDispatchProcessor` in the background,
                AFTER the text frame is pushed downstream.

        Returns:
            :class:`TalkerResponse` carrying the plain-text reply
            and a list of typed :class:`ToolCall` instances.
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

    async def complete_with_tools(
        self,
        prompt: str,
        tool_registry: ToolRegistry,
    ) -> TalkerResponse:
        """Tool-using completion — emit text + parsed :class:`ToolCall` list (Story 4.4).

        Production call site (post-Story-4.4): the
        :class:`TurnDispatchProcessor` calls this once per turn,
        emits the returned ``text`` to the splitter immediately, and
        kicks off ``tool_registry.dispatch`` per tool call as
        background tasks. The dispatcher does NOT await the
        dispatches — text plays first; tools run alongside TTS.

        Belief-state grounding (Story 4.1 + 4.4 integration):

        - When ``self._beliefs is not None`` AND ``self._config
          .grounded_keys`` is non-empty, ``await self._beliefs.read(
          self._config.grounded_keys)`` and append a
          ``## Belief state\\n{json}`` section to the system prompt.
          The belief read is per-turn (no cache); see
          :class:`BeliefStateClient` for the rationale.
        - When ``self._beliefs is None`` (test harness, dev mode
          with ``[daemon] enabled = false``) OR ``grounded_keys`` is
          empty: skip the read; use the plain system prompt
          unchanged.

        Tool-call response parsing:

        The openai SDK ships ``ChatCompletionMessageToolCall.function
        .arguments`` as a JSON STRING (not a parsed dict). We
        ``json.loads`` it before constructing :class:`ToolCall`. JSON
        parse errors drop that specific tool call (log WARN
        ``talker.tool_call_invalid_json``); the rest of the response
        flows through. ``message.content`` may be ``None`` when the
        LLM emitted only tool calls — coerced to ``""`` so callers
        don't see Optional text.

        Raises:
            TalkerError: On any ``openai.APIError`` subclass — same
                wrap-and-propagate pattern as :meth:`complete`.
                CLAUDE.md rule #4: never caught downstream.
        """
        # Story 4.4 belief-state grounding. Build the per-call
        # system prompt locally — DON'T mutate self._system_prompt;
        # the field is set once at __init__ and shared across calls.
        system_prompt = self._system_prompt
        if self._beliefs is not None and self._config.grounded_keys:
            # Per-turn fresh read; no cache by design (Story 4.1).
            beliefs = await self._beliefs.read(self._config.grounded_keys)
            # Format as a JSON-rendered context block. ``indent=2``
            # makes the section human-readable in case operators
            # tail the prompt during debugging — the LLM doesn't
            # care about indentation but the readability tax is
            # negligible.
            system_prompt = (
                self._system_prompt + "\n\n## Belief state\n" + json.dumps(beliefs, indent=2)
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            # Provider-aware max-tokens kwarg — same branch as
            # :meth:`complete`. Splitting into two ``create`` calls
            # (rather than building a kwargs dict) lets pyright
            # resolve the openai SDK's overloaded signature.
            #
            # ``tools=`` carries the registry's openai-formatted tool
            # specs. ``tool_choice="auto"`` lets the LLM decide
            # whether to emit a tool call or just text — we don't
            # force tools every turn; "what's the weather" is fine
            # without any tool calls.
            tools_param = tool_registry.as_openai_tools_param()
            if self._max_tokens_param == "max_completion_tokens":
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,  # type: ignore[arg-type]
                    max_completion_tokens=self._config.max_tokens,
                    tools=tools_param,  # type: ignore[arg-type]
                    tool_choice="auto",
                )
            else:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,  # type: ignore[arg-type]
                    max_tokens=self._config.max_tokens,
                    tools=tools_param,  # type: ignore[arg-type]
                    tool_choice="auto",
                )
        except openai.APIError as e:
            # Same wrap-and-propagate posture as :meth:`complete`.
            # CLAUDE.md rule #4: TalkerError is never caught
            # downstream; process crashes; systemd restarts.
            raise TalkerError(
                provider=self._config.provider,
                model=self._model,
                reason=str(e),
            ) from e

        choice = response.choices[0]
        # ``message.content`` is ``None`` when the LLM emits only
        # tool calls (no narration). Coerce to "" so callers see a
        # straight ``str``, not ``str | None``. The dispatcher still
        # emits a ``TalkerResponseFrame("")`` so downstream observers
        # see the turn boundary; an empty TalkerResponseFrame is
        # harmless to the splitter.
        text = choice.message.content or ""

        # Parse openai's tool calls into our typed shape. The SDK
        # ships ``arguments`` as a JSON STRING; we decode it once
        # here so the registry's input-schema validation works on
        # parsed JSON (not raw text). JSON parse errors drop the
        # specific tool call and continue with the rest — bad
        # arguments shouldn't kill the turn's text emission.
        #
        # The SDK's ``tool_calls`` field is typed as a discriminated
        # union of ``ChatCompletionMessageFunctionToolCall`` (the
        # "function" variant we want) and a "custom" variant we
        # don't use in v1. The ``isinstance`` check narrows pyright
        # AND filters out any non-function tool types if the
        # provider ever surfaces one.
        raw_tool_calls = choice.message.tool_calls or []
        parsed: list[ToolCall] = []
        for tc in raw_tool_calls:
            if not isinstance(tc, ChatCompletionMessageFunctionToolCall):
                # Defensive — v1 only registers function-typed tools,
                # so a non-function tool call shouldn't happen. Log
                # at WARN if a provider ever emits one.
                log.warning("talker.tool_call_unsupported_type")
                continue
            try:
                # Empty arguments string is openai's "no args" sentinel
                # for empty-input tools (e.g. ``go_to_sleep``). Coerce
                # to ``{}`` so ``GoToSleepInput.model_validate({})``
                # succeeds (empty model with extra="forbid" accepts
                # an empty dict).
                arguments_str = tc.function.arguments
                arguments: dict[str, Any] = json.loads(arguments_str) if arguments_str else {}
            except json.JSONDecodeError:
                # Privacy: log only the tool name + raw_length, never
                # the malformed arguments string itself (could carry
                # untrusted LLM output).
                log.warning(
                    "talker.tool_call_invalid_json",
                    tool=tc.function.name,
                    raw_length=len(tc.function.arguments or ""),
                )
                continue
            parsed.append(
                ToolCall(id=tc.id, name=tc.function.name, arguments=arguments),
            )

        # Token usage + tool_call_count observability — mirrors the
        # :meth:`complete` log pattern with one extra field.
        if response.usage is not None:
            log.info(
                "talker.completion",
                provider=self._config.provider,
                model=self._model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                tool_call_count=len(parsed),
                # Same Story 2.5 deviation as :meth:`complete` — the
                # ``prompt`` / ``response`` field names bypass the
                # redaction processor's strict ``transcript`` /
                # ``user_text`` gating. Operator-controlled.
                prompt=prompt,
                response=text,
            )

        return TalkerResponse(text=text, tool_calls=parsed)
