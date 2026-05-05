# Story 2.2: TalkerClient — Anthropic async client behind the Protocol seam

Status: ready-for-dev

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

- [ ] **Task 1: Extend `SetupConfig` with `[talker]` + Anthropic key** (AC: #3, #4)
  - [ ] In `src/voice_agent_pipeline/config/setup.py` add `TalkerConfig(BaseModel, extra="forbid")` with `model: str = "claude-haiku-4-5"`, `max_tokens: int = Field(default=512, gt=0)`, `system_prompt_path: Path = Path("prompts/talker_system.md")`. Docstring per the existing nested-config style.
  - [ ] Add `talker: TalkerConfig = Field(default_factory=TalkerConfig)` to `SetupConfig`.
  - [ ] Add `anthropic_api_key: SecretStr` to `SetupConfig` (no default — pydantic-settings pulls from `.env`).
  - [ ] Update `setup.toml` with a `[talker]` block — model, max_tokens, system_prompt_path explicit (don't rely on defaults — explicit config aids operator audit).
  - [ ] Update `.env.example` — uncomment `ANTHROPIC_API_KEY=`.
  - [ ] Extend `tests/unit/config/test_setup.py`: `test_talker_block_defaults_load`, `test_talker_max_tokens_must_be_positive`, `test_anthropic_key_required` (missing env var → `ConfigError`).

- [ ] **Task 2: Create `prompts/talker_system.md`** (AC: #5)
  - [ ] Create `prompts/` directory at project root.
  - [ ] Author the v1 system prompt: ≤200 words, plain text, no SSML, no markdown, conversational, terse. Suggested skeleton:
    ```markdown
    You are OLAF, a small voice companion.

    Reply in plain text only — no markdown, no code blocks, no emotion tags.

    Keep responses to one or two sentences. The user is speaking to you and
    will hear your reply through a speaker; long answers feel slow and break
    the conversational flow. If you genuinely need more, ask whether they
    want the longer version first.

    If you don't know something, say so plainly. Don't invent specifics.
    ```
  - [ ] Story 3.5 will rewrite this to add Cartesia SSML tags. The v1 file is the placeholder.

- [ ] **Task 3: Implement `AnthropicTalker` in `turn/talker.py`** (AC: #1, #2, #5, #7, #8)
  - [ ] Keep the existing `TalkerClient` Protocol (don't delete its docstring — it's referenced by `BeliefStateClient` and `OrchestratorClient` neighbours).
  - [ ] Add concrete class `AnthropicTalker`:
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
  - [ ] Verify the exact `messages.create` call shape against the installed `anthropic` SDK version (≥0.98.1 per pyproject.toml). The 0.x → 1.x SDK rewrites break call shapes; pin against 0.98.1's `messages.create`.
  - [ ] Module docstring update: explain that Story 4.1 wires `beliefs`, Story 3.5 changes the system prompt to emit SSML.

- [ ] **Task 4: Anthropic startup probe in `__main__.py`** (AC: #6)
  - [ ] Add `_validate_anthropic_credentials(config: SetupConfig) -> None`:
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
  - [ ] Wire into the bootstrap sequence after `_validate_wakeword_credentials`:
    ```python
    await _validate_wakeword_credentials(config)
    log.info("startup.validated.wakeword")
    await _validate_anthropic_credentials(config)
    log.info("startup.validated.talker")
    ```
  - [ ] Verify `messages.count_tokens` exists in anthropic 0.98.1 — fall back to a minimal `messages.create(max_tokens=1, ...)` if not. Document the choice in a code comment.
  - [ ] **Privacy check**: never log the API key. The `count_tokens` failure error message may include sensitive context — the redaction processor (Story 1.3) catches `*api_key`/`*token` field names; the call uses `reason=str(e)` so the exception's `__str__` is the only surface — verify the SDK's exception types don't echo the key in their repr.

- [ ] **Task 5: Unit tests** (AC: #9)
  - [ ] All tests in AC #9 above. Mock `anthropic.AsyncAnthropic` at the module boundary inside `turn/talker.py` — patch the import name, not the global `anthropic` module (architecture's mock-at-Protocol-boundaries rule).
  - [ ] Use `pytest.fixture` to build a stub `Response` dataclass mirroring anthropic's `Message` shape (`content: list[TextBlock]`, `TextBlock(text=...)`).
  - [ ] Live test (manual, not in CI): with a real `ANTHROPIC_API_KEY` in `.env`, run a one-off Python script that imports `AnthropicTalker`, calls `complete("what time is it?")`, and prints the reply. Document the rough wall-clock latency in Dev Agent Record. Story 2.5's integration test will measure NFR1 properly.

- [ ] **Task 6: Verify boundary-concentration rule** (AC: #1, #11)
  - [ ] After implementing, run `grep -r "^import anthropic\|^from anthropic" src/` — assert exactly one match (in `turn/talker.py`).
  - [ ] Note: `__main__.py`'s `_validate_anthropic_credentials` also imports `anthropic` (for the probe). **This is acceptable** — the architecture's boundary rule is about the runtime path; the startup probe is a one-off pre-flight that the operator-facing `__main__` owns. Document this in `__main__.py` with a code comment so a future audit doesn't mark it as a violation.
  - [ ] Alternative: move `_validate_anthropic_credentials` into `turn/talker.py` as a module-level function and import it from `__main__.py`. **Prefer this** — keeps the import truly single-file. Update Task 4 to put the probe in `talker.py` and import it from `__main__.py`. (This is the cleaner design; updating Task 4 wording here for the dev agent.)

- [ ] **Task 7: Commit + push** — single commit titled `Story 2.2: TalkerClient — Anthropic async client behind the Protocol seam`, then `git push`.

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

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List
