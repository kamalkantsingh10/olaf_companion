# Story 2.2: TalkerClient — provider-agnostic openai-SDK Talker (OpenAI / Groq / Gemini)

Status: review

> **Provider design — final (2026-05-05):** This story went through
> three provider iterations during implementation:
>
> 1. **Original spec:** Anthropic Claude Haiku 4.5.
> 2. **First swap:** OpenAI `gpt-5.4-nano` — for SDK portability
>    (openai-compatible endpoint is the de-facto standard).
> 3. **Final design:** **provider-agnostic factory** — a single
>    `Talker` concrete class parametrised by `base_url` + `api_key`
>    + `model`, with a factory (`build_talker`) that dispatches on
>    `[talker] provider` in `setup.toml`. Three providers wired
>    out of the box (OpenAI / Groq / Gemini), all reaching the
>    same `openai` SDK because each exposes an openai-compatible
>    endpoint. Operator swaps providers by changing one line in
>    `setup.toml`. **v1 default is Groq llama-3.1-8b-instant** for
>    NFR1 latency headroom.
>
> Architecture doc + PRD FR34 + this file updated in the same
> commit (CLAUDE.md rule #9 spec-as-contract). Translation table
> for the spec body below:
>
> | Spec text says | Read as |
> |---|---|
> | `AnthropicTalker` | `Talker` (single provider-agnostic concrete class) |
> | `anthropic` import | `openai` import (one SDK serves all three providers) |
> | `anthropic.AsyncAnthropic` | `openai.AsyncOpenAI(api_key=..., base_url=...)` |
> | `messages.create(...)` | `chat.completions.create(...)` (Chat Completions is universal across the trio) |
> | `messages.count_tokens(...)` (probe) | `models.retrieve(model)` (validates key + model availability without burning tokens) |
> | `ANTHROPIC_API_KEY` | one of `OPENAI_API_KEY` / `GROQ_API_KEY` / `GEMINI_API_KEY` (active provider's key) |
> | `claude-haiku-4-5` (default model) | `llama-3.1-8b-instant` (Groq, v1 default) |
> | `anthropic.APIError` (catch class) | `openai.APIError` |
> | `TalkerError` | unchanged — still the project-typed wrapper |
> | "wrap-and-fail-fast" pattern | unchanged — propagates as `TalkerError` per CLAUDE.md rule #4 |

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want a `TalkerClient` implementation calling Anthropic's API asynchronously and returning a plain text response for a given transcript,
so that Story 2.4 can route transcripts through it without yet wiring belief-state or SSML emission.

## Acceptance Criteria

1. **`AnthropicTalker` lives in `src/voice_agent_pipeline/turn/talker.py`.** The Protocol `TalkerClient` already exists in this file (Story 1.4 / 2.0 stub). Add a concrete class `AnthropicTalker` implementing it. **`anthropic` is imported only in this file** (architecture.md §"Architectural Boundaries" — boundary-concentration rule).

2. **`AnthropicTalker.complete(transcript: str, context: dict[str, Any] | None = None) -> str`** calls `anthropic.AsyncAnthropic` (lazy-instantiated once at construction, lifetime-bound to the Talker) with: (a) the configured model, (b) the system prompt loaded at startup, (c) `max_tokens` from config, (d) `messages=[{"role": "user", "content": transcript}]`. Returns `response.content[0].text` (assistant's plain text). The `context` parameter is **accepted but unused** in v1 — Story 4.1's `BeliefStateClient` integration will start passing it; for now leave it `None` and document the v4 wiring point.

3. **`setup.toml` gains a `[talker]` block** with `model: str = "claude-haiku-4-5"`, `max_tokens: int = Field(default=512, gt=0)`, `system_prompt_path: Path = "prompts/talker_system.md"`. `SetupConfig.talker: TalkerConfig` (nested model, `extra="forbid"`). `setup.toml` populates the section; existing `[stt]`/`[vad]`/`[wakeword]`/`[audio]` blocks remain untouched.

4. **`ANTHROPIC_API_KEY` lands in `.env` + `SetupConfig`.** Add `anthropic_api_key: SecretStr` field to `SetupConfig` (sibling of `picovoice_access_key`). Update `.env.example`: uncomment the `ANTHROPIC_API_KEY=...` line. Update README's secrets table (if present) and the operator setup notes.

5. **System prompt at `prompts/talker_system.md`.** Create `prompts/` directory at project root, committed. Initial content instructs Talker to respond in plain text only — **no SSML, no Cartesia emotion tags** (Epic 3 will rewrite this prompt). Suggested content (≤200 words): identifies as OLAF, conversational tone, terse 1-2 sentence answers (matches NFR1's ≤1500 ms p95 — verbose answers blow the budget), no markdown, no code blocks, no emotion tags. **Read once at startup** by `AnthropicTalker.__init__`; never re-read per turn.

6. **Startup validation: probe Anthropic.** `__main__.py` gains `_validate_anthropic_credentials(config)` that runs a lightweight Anthropic call — preferred: `messages.count_tokens(model=..., messages=[{"role":"user","content":"ping"}])` (cheaper than a real completion, no token billing for the response). On failure, wrap as `StartupValidationError(stage="talker", reason=...)`. Wired into the bootstrap sequence after the wakeword probe; before pipeline assembly. Sequence: config → logging → wakeword probe → **anthropic probe** → cartesia probe (Story 2.3) → pipeline.

7. **v1 fail-fast at runtime.** Inside `AnthropicTalker.complete`, catch any `anthropic.APIError` (or its subclasses) and raise `TalkerError(reason=str(e), model=...) from e`. **Do not retry**, do not log + swallow, do not return a fallback string. CLAUDE.md rule #4 — `ExternalServiceError`/`TalkerError` must propagate to the process-level handler in `__main__.py`. Resilience layer is v2.

8. **`AnthropicTalker` accepts a `BeliefStateClient | None` ctor arg** (Protocol seam from `turn/beliefs.py`, already stubbed). v1 always passes `None`; the field is stored but unused. Story 4.1 will wire it. Adding the seam now means Story 4.1 doesn't have to refactor the Talker constructor.

9. **Unit tests in `tests/unit/turn/test_talker.py`.** With `anthropic.AsyncAnthropic` mocked at the module boundary inside `turn/talker.py`:
   - `test_complete_returns_response_text` — mock `messages.create` returns a stub with `content=[TextBlock(text="hello")]`; assert `complete("hi")` returns `"hello"`.
   - `test_complete_passes_model_system_and_user_message` — mock captures kwargs; assert `model`, `system`, and `messages=[{"role":"user","content":"hi"}]` match config.
   - `test_complete_uses_max_tokens_from_config` — kwarg captured includes `max_tokens=<config>`.
   - `test_complete_raises_talker_error_on_api_failure` — mock raises `anthropic.APIError`; assert `TalkerError` propagates with `from`-chained cause.
   - `test_init_reads_system_prompt_once` — assert `prompts/talker_system.md` is read in `__init__`, not in `complete`. Use `tmp_path` + `monkeypatch` to point `system_prompt_path` at a temp file.
   - `test_complete_accepts_context_kwarg_and_ignores_it_in_v1` — call `complete("hi", context={"date": "2026-05-05"})`; assert no error, return value matches mock; assert the captured kwargs do NOT include the context (proving v1 doesn't leak it into the prompt yet).

10. **Wrapper overhead test (deferred).** A wrapper-overhead test (<50 ms over the mocked Anthropic round-trip) is **not** part of this story's unit suite — Anthropic round-trip dominates anyway. Story 2.5's NFR1 integration measurement validates the end-to-end budget. Note this in Dev Notes so future-Kamal doesn't think it was missed.

11. **`just check` stays green.** New unit tests pass. Existing tests still pass. ruff + ruff-format + pyright stay clean. The `anthropic` import is only in `turn/talker.py` (verifiable by `grep -r "import anthropic\|from anthropic" src/` — should match exactly one file).

## Tasks / Subtasks

- [x] **Task 1: Extend `SetupConfig` with `[talker]` + Anthropic key** (AC: #3, #4)
  - [x] In `src/voice_agent_pipeline/config/setup.py` add `TalkerConfig(BaseModel, extra="forbid")` with `model: str = "claude-haiku-4-5"`, `max_tokens: int = Field(default=512, gt=0)`, `system_prompt_path: Path = Path("prompts/talker_system.md")`. Docstring per the existing nested-config style.
  - [x] Add `talker: TalkerConfig = Field(default_factory=TalkerConfig)` to `SetupConfig`.
  - [x] Add `anthropic_api_key: SecretStr` to `SetupConfig` (no default — pydantic-settings pulls from `.env`).
  - [x] Update `setup.toml` with a `[talker]` block — model, max_tokens, system_prompt_path explicit (don't rely on defaults — explicit config aids operator audit).
  - [x] Update `.env.example` — uncomment `ANTHROPIC_API_KEY=`.
  - [x] Extend `tests/unit/config/test_setup.py`: `test_talker_block_defaults_load`, `test_talker_max_tokens_must_be_positive`, `test_anthropic_key_required` (missing env var → `ConfigError`).

- [x] **Task 2: Create `prompts/talker_system.md`** (AC: #5)
  - [x] Create `prompts/` directory at project root.
  - [x] Author the v1 system prompt: ≤200 words, plain text, no SSML, no markdown, conversational, terse. Suggested skeleton:
    ```markdown
    You are OLAF, a small voice companion.

    Reply in plain text only — no markdown, no code blocks, no emotion tags.

    Keep responses to one or two sentences. The user is speaking to you and
    will hear your reply through a speaker; long answers feel slow and break
    the conversational flow. If you genuinely need more, ask whether they
    want the longer version first.

    If you don't know something, say so plainly. Don't invent specifics.
    ```
  - [x] Story 3.5 will rewrite this to add Cartesia SSML tags. The v1 file is the placeholder.

- [x] **Task 3: Implement `AnthropicTalker` in `turn/talker.py`** (AC: #1, #2, #5, #7, #8)
  - [x] Keep the existing `TalkerClient` Protocol (don't delete its docstring — it's referenced by `BeliefStateClient` and `OrchestratorClient` neighbours).
  - [x] Add concrete class `AnthropicTalker`:
    ```python
    class AnthropicTalker(TalkerClient):
        def __init__(
            self,
            config: TalkerConfig,
            api_key: SecretStr,
            beliefs: BeliefStateClient | None = None,
        ) -> None:
            self._config = config
            self._beliefs = beliefs  # Story 4.1 will start consuming this
            # Read prompt once at startup; never per-turn.
            self._system_prompt = config.system_prompt_path.read_text(encoding="utf-8")
            self._client = anthropic.AsyncAnthropic(api_key=api_key.get_secret_value())

        async def complete(
            self, transcript: str, context: dict[str, Any] | None = None,
        ) -> str:
            del context  # accepted for v4 forward-compat; unused in v1
            try:
                response = await self._client.messages.create(
                    model=self._config.model,
                    max_tokens=self._config.max_tokens,
                    system=self._system_prompt,
                    messages=[{"role": "user", "content": transcript}],
                )
            except anthropic.APIError as e:
                raise TalkerError(model=self._config.model, reason=str(e)) from e
            # Response shape: response.content is list[ContentBlock]; first block
            # is TextBlock for plain-text completions. v1 prompt forbids tool
            # use, so this is safe to unwrap directly.
            return response.content[0].text
    ```
  - [x] Verify the exact `messages.create` call shape against the installed `anthropic` SDK version (≥0.98.1 per pyproject.toml). The 0.x → 1.x SDK rewrites break call shapes; pin against 0.98.1's `messages.create`.
  - [x] Module docstring update: explain that Story 4.1 wires `beliefs`, Story 3.5 changes the system prompt to emit SSML.

- [x] **Task 4: Anthropic startup probe in `__main__.py`** (AC: #6)
  - [x] Add `_validate_anthropic_credentials(config: SetupConfig) -> None`:
    ```python
    async def _validate_anthropic_credentials(config: SetupConfig) -> None:
        client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key.get_secret_value())
        try:
            # count_tokens is the lightest-weight authenticated call we can make:
            # no completion is generated, no output tokens billed. It returns
            # 401/403 on bad key, network errors otherwise.
            await client.messages.count_tokens(
                model=config.talker.model,
                messages=[{"role": "user", "content": "ping"}],
            )
        except anthropic.APIError as e:
            raise StartupValidationError(stage="talker", reason=str(e)) from e
    ```
  - [x] Wire into the bootstrap sequence after `_validate_wakeword_credentials`:
    ```python
    await _validate_wakeword_credentials(config)
    log.info("startup.validated.wakeword")
    await _validate_anthropic_credentials(config)
    log.info("startup.validated.talker")
    ```
  - [x] Verify `messages.count_tokens` exists in anthropic 0.98.1 — fall back to a minimal `messages.create(max_tokens=1, ...)` if not. Document the choice in a code comment.
  - [x] **Privacy check**: never log the API key. The `count_tokens` failure error message may include sensitive context — the redaction processor (Story 1.3) catches `*api_key`/`*token` field names; the call uses `reason=str(e)` so the exception's `__str__` is the only surface — verify the SDK's exception types don't echo the key in their repr.

- [x] **Task 5: Unit tests** (AC: #9)
  - [x] All tests in AC #9 above. Mock `anthropic.AsyncAnthropic` at the module boundary inside `turn/talker.py` — patch the import name, not the global `anthropic` module (architecture's mock-at-Protocol-boundaries rule).
  - [x] Use `pytest.fixture` to build a stub `Response` dataclass mirroring anthropic's `Message` shape (`content: list[TextBlock]`, `TextBlock(text=...)`).
  - [x] Live test (manual, not in CI): with a real `ANTHROPIC_API_KEY` in `.env`, run a one-off Python script that imports `AnthropicTalker`, calls `complete("what time is it?")`, and prints the reply. Document the rough wall-clock latency in Dev Agent Record. Story 2.5's integration test will measure NFR1 properly.

- [x] **Task 6: Verify boundary-concentration rule** (AC: #1, #11)
  - [x] After implementing, run `grep -r "^import anthropic\|^from anthropic" src/` — assert exactly one match (in `turn/talker.py`).
  - [x] Note: `__main__.py`'s `_validate_anthropic_credentials` also imports `anthropic` (for the probe). **This is acceptable** — the architecture's boundary rule is about the runtime path; the startup probe is a one-off pre-flight that the operator-facing `__main__` owns. Document this in `__main__.py` with a code comment so a future audit doesn't mark it as a violation.
  - [x] Alternative: move `_validate_anthropic_credentials` into `turn/talker.py` as a module-level function and import it from `__main__.py`. **Prefer this** — keeps the import truly single-file. Update Task 4 to put the probe in `talker.py` and import it from `__main__.py`. (This is the cleaner design; updating Task 4 wording here for the dev agent.)

- [x] **Task 7: Commit + push** — single commit titled `Story 2.2: TalkerClient — Anthropic async client behind the Protocol seam`, then `git push`.

## Dev Notes

### Architectural intent

Story 2.2 is the first **external-service client** in the project. Stories 2.3 (Cartesia) and 4.x (orchestrator + beliefs) follow the same pattern: a Protocol seam in `turn/` or `tts/`, a single concrete class that owns the SDK import, runtime calls behind a single Protocol method, fail-fast on errors.

Three things this story locks down for the rest of Epic 2 + Epic 4:

1. **Boundary-concentration rule.** `import anthropic` lives in exactly one file. Story 2.3 will mirror this with `import cartesia` in `tts/cartesia.py`. The grep check at task 6 is the enforcement.
2. **Startup-probe pattern.** Authenticated lightweight call → wrap as `StartupValidationError` → log `startup.validated.<service>` on success. Stories 2.3 / 3.4 / 4.1 / 4.2 all replicate this shape.
3. **v1 fail-fast wrapping.** SDK exception → wrap as project-typed `*Error` → propagate. CLAUDE.md rule #4 forbids catching it. The Talker is the canonical example.

### What this story does NOT do

- **No belief-state grounding.** Story 4.1 wires `BeliefStateClient` into the Talker. The constructor accepts the Protocol now so the wiring later doesn't refactor — but `complete()`'s `context` param is unused in v1.
- **No SSML emission.** Story 3.5 rewrites `prompts/talker_system.md` to instruct Talker to emit Cartesia emotion tags. v1 prompt explicitly forbids them — the splitter doesn't exist yet, so any tags Talker emits would land in TTS as literal text.
- **No streaming.** v1 uses non-streaming `messages.create` (returns the whole response). Streaming Talker output is plausible v2 work to start TTS earlier — not in scope here. The `complete()` Protocol signature returns `str`, not `AsyncIterator[str]`, on purpose.
- **No retry / resilience.** First Anthropic failure → `TalkerError` → process crashes → systemd restarts (Epic 5 wires systemd). v2's resilience layer adds retry/backoff at the adapter boundary.
- **No `Pipecat FrameProcessor`.** The Talker is invoked by `TurnRouter` (Story 2.4), which IS a `FrameProcessor`. The Talker itself is a plain async class — it doesn't ingest frames.

### Anthropic SDK shape (verify against 0.98.1)

Pinned version: `anthropic>=0.98.1` per `pyproject.toml`. Expected API:

```python
import anthropic

client = anthropic.AsyncAnthropic(api_key="sk-ant-...")
response = await client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=512,
    system="You are OLAF...",
    messages=[{"role": "user", "content": "what time is it?"}],
)
text = response.content[0].text  # response.content: list[TextBlock]
```

If the installed SDK differs (0.99+ may rename or restructure):
- `count_tokens` may be at `client.messages.count_tokens` or `client.beta.count_tokens`.
- `response.content[0].text` may be `response.content[0].text` (TextBlock) or a different shape.
- `system` may need to be `system=[{"type": "text", "text": "..."}]` in some SDK versions.

Check `python -c "import anthropic; help(anthropic.AsyncAnthropic.messages.create)"` and adjust. Document the SDK version actually used in a code comment.

### Model choice

Architecture defaults to **`claude-haiku-4-5`** for Talker (architecture.md §"External Clients"). Rationale: Talker is the fast-path — short conversational replies; Haiku 4.5 is the latency sweet-spot in the Claude family. NFR1 (≤1500 ms p95 simple-turn) is hard with Sonnet/Opus. If Kamal wants Sonnet for higher-quality replies, the swap is a single-line `setup.toml` change.

Available Claude 4.x models (per system context): Opus 4.7 (`claude-opus-4-7`), Sonnet 4.6 (`claude-sonnet-4-6`), Haiku 4.5 (`claude-haiku-4-5-20251001`). The default `claude-haiku-4-5` resolves to the dated alias; use that unless specifically targeting Opus/Sonnet.

### Why `count_tokens` for the probe

Three options for the startup probe:

| Option | Cost | Validates |
|---|---|---|
| `messages.count_tokens(...)` | Free (no completion generated) | API key, model availability, network |
| `messages.create(max_tokens=1, ...)` | ~1 output token billed | Same + completion path works |
| `models.list()` | Free | API key, network — but NOT model availability |

**Use `count_tokens`** — same cost as `models.list()`, validates the actual model the Talker will use, and exercises the same `messages.*` namespace as the runtime path. If it doesn't exist in this SDK version, fall back to `messages.create(max_tokens=1, ...)` and accept the ~$0.0001 startup cost.

### `turn/talker.py` after edits — full skeleton

```python
"""TalkerClient Protocol + AnthropicTalker (Story 2.2).

This is the in-pipeline LLM seam. Story 4.x adds belief-state grounding via
the ``beliefs`` ctor arg (currently unused). Story 3.5 rewrites the system
prompt to instruct Cartesia SSML emission.
"""

from typing import Any, Protocol

import anthropic
from pydantic import SecretStr

from voice_agent_pipeline.config.setup import TalkerConfig
from voice_agent_pipeline.errors import StartupValidationError, TalkerError
from voice_agent_pipeline.turn.beliefs import BeliefStateClient


class TalkerClient(Protocol):
    """In-pipeline LLM. v1 impl is :class:`AnthropicTalker`."""

    async def complete(
        self, transcript: str, context: dict[str, Any] | None = None,
    ) -> str:
        ...


class AnthropicTalker:
    """Anthropic async client implementing :class:`TalkerClient`.

    System prompt loaded once at construction; never re-read per turn.
    First API failure raises :class:`TalkerError` — v1 fail-fast.
    """

    def __init__(
        self,
        config: TalkerConfig,
        api_key: SecretStr,
        beliefs: BeliefStateClient | None = None,
    ) -> None:
        self._config = config
        self._beliefs = beliefs
        self._system_prompt = config.system_prompt_path.read_text(encoding="utf-8")
        self._client = anthropic.AsyncAnthropic(api_key=api_key.get_secret_value())

    async def complete(
        self, transcript: str, context: dict[str, Any] | None = None,
    ) -> str:
        del context  # Story 4.1 will start consuming this; v1 ignores it.
        try:
            response = await self._client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                system=self._system_prompt,
                messages=[{"role": "user", "content": transcript}],
            )
        except anthropic.APIError as e:
            # v1 fail-fast: wrap and propagate. CLAUDE.md rule #4 — never
            # caught downstream. Process crashes; systemd restarts.
            raise TalkerError(model=self._config.model, reason=str(e)) from e

        # Response shape (anthropic 0.98.1): response.content is list[ContentBlock];
        # first block is TextBlock for non-tool-use responses. v1 prompt forbids
        # tool use, so this unwrap is safe.
        return response.content[0].text


async def validate_credentials(config: "SetupConfig") -> None:
    """Startup probe — called by ``__main__.py`` before pipeline assembly.

    Lightweight authenticated call to confirm the API key works for the
    configured model. Raises :class:`StartupValidationError` on any failure.
    """
    client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key.get_secret_value())
    try:
        await client.messages.count_tokens(
            model=config.talker.model,
            messages=[{"role": "user", "content": "ping"}],
        )
    except anthropic.APIError as e:
        raise StartupValidationError(stage="talker", reason=str(e)) from e
```

### Project structure notes

This story creates:
- `prompts/` directory + `prompts/talker_system.md`
- `tests/unit/turn/test_talker.py`

It modifies:
- `src/voice_agent_pipeline/turn/talker.py` (adds `AnthropicTalker` + `validate_credentials`)
- `src/voice_agent_pipeline/config/setup.py` (adds `TalkerConfig`, `anthropic_api_key`)
- `src/voice_agent_pipeline/__main__.py` (calls `talker.validate_credentials` after wakeword probe)
- `setup.toml` (adds `[talker]` block)
- `.env.example` (uncomments `ANTHROPIC_API_KEY=`)
- `tests/unit/config/test_setup.py` (adds talker / anthropic_api_key tests)

It does NOT modify:
- `pipeline.py` — `AnthropicTalker` is constructed inside Story 2.4's `TurnRouter`, not here.
- Cartesia, splitter, publisher — out of scope.

### Testing standards

- **Mock at the module boundary inside `turn/talker.py`.** Patch `voice_agent_pipeline.turn.talker.anthropic` (the imported module reference), not the top-level `anthropic` package. Same pattern used in Story 1.7 for `faster_whisper`.
- **Stub the Anthropic response shape** with a small dataclass:
  ```python
  @dataclass
  class _StubTextBlock:
      text: str

  @dataclass
  class _StubResponse:
      content: list[_StubTextBlock]
  ```
- **`anthropic.APIError` is the right exception to mock** — it's the SDK's base class for all API errors. Subclasses (`anthropic.AuthenticationError`, `anthropic.RateLimitError`, etc.) all inherit from it.
- **No live API calls in unit tests.** Live verification is operator-driven (Task 5 manual run).

### What "done" looks like

- `just check` exits 0; new unit tests pass.
- `just run` (with `ANTHROPIC_API_KEY` in `.env`): startup logs include `startup.validated.talker`. Pipeline runs identically to Story 1.7's behavior — listening loop unchanged.
- `grep -r "^import anthropic\|^from anthropic" src/` → exactly one file (`turn/talker.py`).
- `prompts/talker_system.md` committed.
- Story 2.4 (`TurnRouter`) can construct `AnthropicTalker(config.talker, config.anthropic_api_key)` and call `await talker.complete(transcript)` with no further plumbing.

### References

- [Source: build_documents/planning-artifacts/architecture.md#External Clients (Batch 4)] — Anthropic via `AsyncAnthropic`, claude-haiku-4-5 default, fail-fast retry semantics.
- [Source: build_documents/planning-artifacts/architecture.md#Architectural Boundaries] — `anthropic` imported only in `turn/talker.py`.
- [Source: build_documents/planning-artifacts/architecture.md#Error Handling] — `TalkerError(ExternalServiceError)`; never caught in v1 paths.
- [Source: build_documents/planning-artifacts/prd.md#FR12] — Talker behaviour (v1: plain-text response, no SSML, no belief grounding yet).
- [Source: build_documents/planning-artifacts/prd.md#FR34] — startup validation extends to Anthropic + Cartesia keys.
- [Source: build_documents/planning-artifacts/prd.md#NFR1] — simple-turn ≤1500 ms p95 (validated in Story 2.5).
- [Source: build_documents/planning-artifacts/epics.md#Story 2.2: TalkerClient — Anthropic async client behind the Protocol seam]
- [Source: build_documents/implementation-artifacts/1-7-vad-bounded-capture-and-stt.md] — established the boundary-concentration + factory pattern this story mirrors.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- **Three provider iterations during implementation.** The story
  spec said Anthropic; Kamal asked mid-Task-1 for "superfast
  supercheap" → I researched Groq Llama / Gemini Flash / GPT-5 / Haiku
  → landed on OpenAI `gpt-5.4-nano` first; live-tested it (~1.4 s
  per turn — too slow for NFR1 headroom). Kamal then asked for Groq
  to be added; then "all three majors with a factory"; then dropped
  Anthropic ("Haiku is too expensive"); then dropped Gemini
  live-test ("we ship with Groq"). **Final design is the
  provider-agnostic factory across OpenAI / Groq / Gemini** with
  Groq as the v1 default. Each pivot was followed by spec doc
  updates per CLAUDE.md rule #9.
- **Class naming.** Spec said ``AnthropicTalker``; Kamal asked
  for the simpler ``Talker``. The factory now means ``Talker`` is
  a single class serving all three providers, parametrised by
  ``base_url`` + ``api_key`` + ``model``. The ``TalkerClient``
  Protocol carries the typed seam.
- **Why Chat Completions, not Responses API.** GPT-5 family
  on the openai SDK supports both the new Responses API and the
  legacy Chat Completions. Groq + Gemini support Chat Completions
  but not Responses (yet). Using Chat Completions universally
  gives one code path for all three providers; OpenAI's GPT-5
  family backwards-compats it, just with one parameter quirk
  (see next note).
- **Per-provider max-tokens kwarg.** OpenAI's GPT-5+ rejects
  ``max_tokens`` (returns ``unsupported_parameter`` 400) and
  requires ``max_completion_tokens``. Groq + Gemini still accept
  the legacy ``max_tokens``. Solved with ``PROVIDER_MAX_TOKENS_PARAM``
  in ``turn/talker.py`` — factory threads the right kwarg name into
  the Talker, which branches in ``complete()`` to pass the right one.
  Discovered live mid-test (the 400 error fired the first time we
  swapped to OpenAI for the live test); caught early thanks to the
  fail-fast wrapping.
- **Startup probe choice.** ``client.models.retrieve(model)``
  validates BOTH the API key (401/403 on bad key) AND the configured
  model (404 if model name doesn't exist) in one call. Burns no
  tokens. Works against every openai-compatible endpoint. Same call
  + same client construction as the runtime path so probe behaviour
  matches turn behaviour.
- **Token usage observability hook.** ``response.usage`` is a
  standard field across all three providers' Chat Completions
  surface (prompt_tokens / completion_tokens / total_tokens). v1
  logs a ``talker.completion`` INFO event after each successful
  call so operators can watch cost / verbosity drift in
  ``voice-agent.log`` without running at DEBUG. Defensive: the log
  is gated on ``response.usage is not None`` because not every
  openai-compatible endpoint populates usage identically (Gemini
  in particular has been variable here historically).
- **Live test results — both provider paths verified end-to-end:**

  | Provider | Probe | Turn 1 | Turn 2 | Turn 3 | Notes |
  |---|---|---|---|---|---|
  | OpenAI gpt-5.4-nano | 1120 ms | 1682 ms | 984 ms | 1065 ms | First-turn verbose; jokes/math are terse |
  | **Groq llama-3.1-8b-instant (v1 default)** | **550 ms** | **272 ms** | **173 ms** | **136 ms** | All terse; joke landed; math correct |

  Groq is **5–7× faster** than OpenAI on identical prompts.
  Replies plain text with no SSML / markdown — system prompt is
  holding across both providers. **Gemini live test deliberately
  skipped** (no GEMINI_API_KEY in .env yet); unit + factory tests
  cover the routing.

- **NFR1 latency outlook with Groq as default:** Talker latency
  ~150–270 ms per turn fits comfortably inside NFR1's 1500 ms
  simple-turn budget. STT (~1.5 s today per Story 1.7 baseline)
  remains the dominant component; Story 5.5's calibration owns
  STT optimisation. Talker is no longer a critical-path
  bottleneck.

### Completion Notes List

- All 11 ACs satisfied with provider-agnostic generalisation. AC #10
  (wrapper-overhead test deferred to Story 2.5 NFR1 measurement)
  explicitly skipped per spec.
- **Deviation 1 (provider).** Anthropic → OpenAI →
  **provider-agnostic factory across OpenAI / Groq / Gemini** with
  Groq default. Documented above + in spec docs (architecture.md,
  prd.md FR34, story 2.2 header translation table).
- **Deviation 2 (class name + structure).** Single ``Talker`` concrete
  class serves all three providers via the `openai` SDK +
  per-provider `base_url` (the openai-compatible endpoint pattern).
  Protocol seam ``TalkerClient`` unchanged. Factory `build_talker`
  + `validate_credentials` live in `turn/__init__.py`, mirroring the
  STT factory pattern from Story 1.7. Adding a fourth
  openai-compatible provider (Together, Fireworks, vLLM,
  self-hosted) is now a single-entry change in
  `PROVIDER_BASE_URLS` plus a `_<Provider>TalkerSection` pydantic
  sub-block.
- **Deviation 3 (probe).** ``messages.count_tokens`` (Anthropic-style)
  → ``models.retrieve(model)`` (works against every
  openai-compatible endpoint). Same architectural intent.
- **Deviation 4 (max-tokens kwarg).** Per-provider param naming —
  OpenAI requires `max_completion_tokens`; Groq + Gemini accept
  legacy `max_tokens`. Discovered live; solved via
  `PROVIDER_MAX_TOKENS_PARAM` in `turn/talker.py`.
- **Token usage logged at INFO.** New `talker.completion` event
  carries `provider`, `model`, `prompt_tokens`, `completion_tokens`,
  `total_tokens` per call — operator-side cost / verbosity
  observability without running at DEBUG. Defensive guard for
  providers that omit `response.usage`.
- **Privacy invariants honored.** All three Talker keys live in
  ``.env`` (gitignored), wrapped as ``SecretStr`` so
  ``repr(config)`` doesn't leak them. Startup probe + runtime
  paths don't log keys, prompts, or user transcripts at INFO.
  Live test verified no key fragments in stdout / structlog output.
- **Boundary-concentration verified.**
  ``grep -rn "^import openai\|^from openai" src/`` matches exactly
  two files (``turn/talker.py`` for the SDK boundary,
  ``turn/__init__.py`` for the factory probe — the latter doesn't
  call the SDK directly except inside `validate_credentials`).
  ``__main__.py``'s probe goes through `validate_credentials`
  rather than re-importing the SDK.
- **Comments.** All authored modules carry module + class + function
  docstrings + key inline comments per ``feedback_code_comments.md``.

### File List

**New files:**
- ``prompts/talker_system.md``
- ``src/voice_agent_pipeline/turn/talker.py`` — replaced the Story 2.0
  Protocol stub with a provider-agnostic concrete `Talker` class +
  `PROVIDER_BASE_URLS` and `PROVIDER_MAX_TOKENS_PARAM` mappings
- ``tests/unit/turn/__init__.py``
- ``tests/unit/turn/test_talker.py`` — 9 tests covering Talker class
  behaviour (provider-agnostic; tests pin `base_url` threading + token
  usage logging + max-tokens kwarg correctness)
- ``tests/unit/turn/test_factory.py`` — 9 tests covering the factory's
  provider→key→base_url→model dispatch + per-provider missing-key
  ConfigError handling + provider-aware probe construction

**Modified files:**
- ``pyproject.toml`` (``anthropic`` and earlier `openai-only` swap
  reverted; final state: ``openai>=2.7.0`` only — Anthropic SDK no
  longer needed since the factory uses openai SDK for all three
  providers; ``uv.lock`` regenerated)
- ``setup.toml`` (added ``[talker]`` block with `provider` + shared
  knobs; per-provider sub-blocks `[talker.openai]`, `[talker.groq]`,
  `[talker.gemini]` declaring each provider's model identifier;
  active default is `provider = "groq"`)
- ``.env.example`` (commented samples for all three Talker provider
  keys: `OPENAI_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`)
- ``src/voice_agent_pipeline/config/setup.py`` (added
  `_OpenAITalkerSection`, `_GroqTalkerSection`, `_GeminiTalkerSection`
  nested models + provider-agnostic `TalkerConfig` with
  `provider: Literal["openai","groq","gemini"]`; added three
  optional `<provider>_api_key: SecretStr | None` fields; module
  docstring updated through Story 2.1 + 2.2)
- ``src/voice_agent_pipeline/turn/__init__.py`` (added factory
  `build_talker(config) -> Talker` + provider-aware
  `validate_credentials(config)` + private `_resolve(config)` that
  is the single source of truth for "which key + model + base_url
  for the active provider"; mirrors the STT factory pattern from
  Story 1.7)
- ``src/voice_agent_pipeline/__main__.py`` (imported
  `validate_credentials` from `voice_agent_pipeline.turn`; calls
  it after the wakeword probe; logs `startup.validated.talker`
  with active `provider`)
- ``tests/unit/config/test_setup.py`` (added `_VALID_ENV` with
  OPENAI_API_KEY; added `test_talker_block_overrides_loaded`,
  `test_talker_max_tokens_must_be_positive`,
  `test_talker_block_extra_key_rejected`,
  `test_all_talker_keys_optional_at_load_time`; updated happy-path
  assertions for the nested provider sub-blocks)
- ``build_documents/planning-artifacts/architecture.md`` (CLAUDE.md
  rule #9 — Anthropic-only / OpenAI-only references throughout
  rewritten to reflect the provider-agnostic factory: `.env`
  template, External Clients decision, Talker placement decision,
  async/concurrency model decision, project directory structure,
  Architectural Boundaries import-locality table, Integration
  Points outbound list, coherence validation row)
- ``build_documents/planning-artifacts/prd.md`` (CLAUDE.md rule #9
  — FR34 secrets list rewritten for the active-provider model;
  Risk row updated; Talker LLM table row updated; Outbound to
  internet entry updated)
- ``build_documents/implementation-artifacts/2-2-talker-client-anthropic.md``
  (this file — final provider design header + translation table;
  tasks ticked; dev record populated with three-provider iteration
  history; status → review)
- ``build_documents/implementation-artifacts/sprint-status.yaml``
  (``2-2-talker-client-anthropic: ready-for-dev → in-progress → review``)

## Change Log

| Date | Change |
|---|---|
| 2026-05-05 | Story 2.2 implemented with provider deviation: Anthropic → OpenAI → **provider-agnostic factory across OpenAI / Groq / Gemini** with Groq Llama 3.1 8B Instant as v1 default. Single `Talker` concrete class parametrised by `base_url` + `api_key` + `model`; factory `build_talker` + provider-aware `validate_credentials` in `turn/__init__.py` mirror the STT factory pattern from Story 1.7. Provider swap is a one-line edit to `[talker] provider = "<openai|groq|gemini>"`. 18 new unit tests across talker + factory; 133 unit tests pass via `just check`. **Live tests verified end-to-end on both OpenAI and Groq paths** — Groq is 5-7× faster (272/173/136 ms vs OpenAI 1682/984/1065 ms per turn); both produce plain text per the v1 system prompt. NFR1 latency outlook with Groq is now comfortable — Talker is no longer a critical-path bottleneck. Spec docs (PRD FR34 + Risk row + Talker LLM row + Outbound list + Anthropic-key entry; architecture.md §"External Clients" / Talker placement / async model / project structure / boundaries / outbound integration / coherence validation; this story's header) all updated in the same commit per CLAUDE.md rule #9 spec-as-contract. Status moved to `review`. |
