# Story 2.3: CartesiaClient — Sonic-3 streaming TTS behind the Protocol seam

Status: ready-for-dev

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want a `CartesiaClient` streaming text to Cartesia Sonic-3 and yielding audio frames to the speaker,
so that Story 2.5 can connect Talker output to spoken output end-to-end.

## Acceptance Criteria

1. **`CartesiaClient` lives in `src/voice_agent_pipeline/tts/cartesia.py`.** The Protocol `TTSClient` is already declared in `tts/client.py` (Story 2.0 stub). Add the concrete `CartesiaClient` class implementing it. **`cartesia` is imported only in `tts/cartesia.py`** (architecture's boundary-concentration rule).

2. **`CartesiaClient.synthesize(text: str) -> AsyncIterator[bytes]`** opens a streaming session to Cartesia Sonic-3 with the configured voice ID and yields audio frame bytes incrementally as Cartesia returns them (FR15). **No buffering of the full stream** — yield each chunk as soon as the SDK delivers it. Real-time contract.

3. **The `TTSClient` Protocol's return type matches.** `tts/client.py`'s current signature is `async def synthesize(self, text: str) -> AsyncIterator[bytes]: ...`. **Verify this matches** what `CartesiaClient` actually returns — bytes, not raw `AudioRawFrame`s. The wrapping into `AudioRawFrame` happens in Story 2.5's pipeline assembly (or in a thin adapter). Don't re-declare the Protocol unless the existing signature genuinely doesn't fit.

4. **`setup.toml` gains a `[tts]` block:** `voice_id: str` (required — no default; operator must pick a Cartesia voice from their console), `default_emotion: str = "neutral"`, `model: str = "sonic-3"`. `SetupConfig.tts: TtsConfig` (nested model, `extra="forbid"`).

5. **Audio format pinned to 16 kHz mono S16LE.** The Cartesia request specifies `output_format = {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}` (verify exact field names against `cartesia` SDK 3.0.2). This matches Story 2.1's speaker pin and Story 1.5's transport pin — single format end-to-end, no resampler in the hot path.

6. **`CARTESIA_API_KEY` lands in `.env` + `SetupConfig`.** Add `cartesia_api_key: SecretStr` to `SetupConfig`. Update `.env.example`: uncomment the `CARTESIA_API_KEY=...` line.

7. **TLS validation cannot be disabled.** The Cartesia SDK uses `httpx` under the hood; ensure no config knob in `setup.toml` or `CartesiaClient.__init__` accepts a `verify=False` / `disable_tls=True` / equivalent parameter. NFR24: refuse-to-start if config attempts it. v1 hard-codes TLS-on; future config additions must preserve this — document explicitly in the module docstring.

8. **Startup probe — `cartesia.validate_credentials(config)`.** Mirrors Story 2.2's pattern: lightweight authenticated call (preferred: `client.voices.list(limit=1)` — small response, validates key + voice catalog access). Wrap failure as `StartupValidationError(stage="cartesia", reason=...)`. `__main__.py` calls this **after** the Anthropic probe and **before** pipeline assembly. Sequence: config → logging → wakeword → talker → **cartesia** → pipeline.

9. **TTFB metric — log first-frame latency.** Inside `synthesize`, record `request_start_ns = time.time_ns()` before opening the stream and `first_frame_ns = time.time_ns()` on the first yielded chunk. Emit `log.info("tts.first_frame", ttfb_ms=..., voice_id=...)` once per call. Architecture target: ≤400 ms p95 (NFR4). v1 just records the baseline; calibration in Story 5.5.

10. **v1 fail-fast at runtime.** Catch any `cartesia` SDK exception (find the SDK's base error class — likely `cartesia.CartesiaError` or `httpx.HTTPError` raised through it) and raise `CartesiaError(reason=str(e), voice_id=..., model=...) from e`. **No retry**, no fallback. CLAUDE.md rule #4. Stream-stall detection (e.g., a `> N seconds` no-frames timeout) is **not in scope** for v1 — `httpx`'s default read timeout fires and the exception propagates. Story 5.x or v2 resilience layer can add explicit stall watchdogs.

11. **Default-emotion field included in the request payload but tags-in-text NOT parsed.** Cartesia's request schema accepts an emotion / voice modifier (verify exact field name in SDK 3.0.2 — likely `voice_experimental_controls` or similar). Pass `default_emotion` through. **Inline `<emotion value="..."/>` tags inside the text are NOT parsed by this story** — Story 3.3 builds the streaming SSML splitter that strips/dispatches tags. v1 Talker prompt forbids tags in the text (Story 2.2), so this is consistent.

12. **Unit tests in `tests/unit/tts/test_cartesia.py`.** With the `cartesia` SDK mocked at the module boundary inside `tts/cartesia.py`:
    - `test_synthesize_yields_frames_in_order` — mock SDK yields `[b"chunk1", b"chunk2", b"chunk3"]`; assert async iterator yields the same in order.
    - `test_synthesize_does_not_buffer_full_stream` — mock SDK yields chunks lazily; assert each yielded chunk reaches the consumer **before** the next is generated (no full-stream collection).
    - `test_synthesize_passes_voice_id_and_model` — captured kwargs include configured `voice_id`, `model="sonic-3"`, output_format spec.
    - `test_synthesize_passes_default_emotion` — captured kwargs include the configured emotion.
    - `test_synthesize_raises_cartesia_error_on_sdk_failure` — mock SDK raises; assert `CartesiaError` propagates with `from`-chained cause.
    - `test_first_frame_logged_with_ttfb_ms` — capture log emissions; assert `tts.first_frame` event with `ttfb_ms` integer field after first chunk.
    - `test_validate_credentials_calls_voices_list` — captures call, returns success; no exception.
    - `test_validate_credentials_wraps_sdk_failure_as_startup_validation_error` — mock raises; assert `StartupValidationError(stage="cartesia", ...)`.

13. **`just check` stays green.** New unit tests pass; existing tests still pass. ruff + ruff-format + pyright stay clean. `grep -r "import cartesia\|from cartesia" src/` returns exactly one file (`tts/cartesia.py`).

## Tasks / Subtasks

- [ ] **Task 1: Extend `SetupConfig` with `[tts]` + `CARTESIA_API_KEY`** (AC: #4, #6)
  - [ ] In `src/voice_agent_pipeline/config/setup.py` add `TtsConfig(BaseModel, extra="forbid")` with `voice_id: str` (no default), `default_emotion: str = "neutral"`, `model: str = "sonic-3"`. Docstring per the existing nested-config style — call out that `voice_id` is operator-supplied (browse https://play.cartesia.ai/voices to pick one).
  - [ ] Add `tts: TtsConfig` to `SetupConfig` (no default — `voice_id` is required).
  - [ ] Add `cartesia_api_key: SecretStr` to `SetupConfig` (no default — pulled from `.env`).
  - [ ] Update `setup.toml`'s `[tts]` placeholder block:
    ```toml
    [tts]
    voice_id = "..."           # pick from https://play.cartesia.ai/voices
    default_emotion = "neutral"
    model = "sonic-3"
    ```
  - [ ] Update `.env.example` — uncomment `CARTESIA_API_KEY=`.
  - [ ] Extend `tests/unit/config/test_setup.py`: `test_tts_block_with_voice_id_loads`, `test_tts_block_missing_voice_id_raises_config_error`, `test_cartesia_key_required` (missing env var → `ConfigError`).

- [ ] **Task 2: Implement `CartesiaClient`** (AC: #1, #2, #5, #7, #10, #11)
  - [ ] Create `src/voice_agent_pipeline/tts/cartesia.py` with module + class + method docstrings (per `feedback_code_comments.md`).
  - [ ] Skeleton (verify exact SDK API against `cartesia` 3.0.2):
    ```python
    """CartesiaClient — Sonic-3 streaming TTS implementation of TTSClient Protocol.

    This module is the single import boundary for the ``cartesia`` SDK
    (architecture.md §"Architectural Boundaries"). Other modules speak through
    the :class:`TTSClient` Protocol from ``tts/client.py``.

    TLS posture (NFR24): the Cartesia SDK uses ``httpx`` internally with
    cert validation on by default. v1 deliberately exposes no knob to
    disable validation — a future contributor adding such a knob is
    introducing a security regression.
    """

    import time
    from collections.abc import AsyncIterator

    import cartesia
    import structlog
    from pydantic import SecretStr

    from voice_agent_pipeline.config.setup import SetupConfig, TtsConfig
    from voice_agent_pipeline.errors import CartesiaError, StartupValidationError

    log = structlog.get_logger(__name__)

    _OUTPUT_FORMAT = {
        "container": "raw",
        "encoding": "pcm_s16le",
        "sample_rate": 16000,
    }


    class CartesiaClient:
        """Streaming TTS via Cartesia Sonic-3.

        Yields raw S16LE PCM bytes at 16 kHz mono — same format the
        ``LocalAudioTransport`` output stage (Story 2.1) consumes, so no
        resampler runs in the hot path.
        """

        def __init__(self, config: TtsConfig, api_key: SecretStr) -> None:
            self._config = config
            self._client = cartesia.AsyncCartesia(api_key=api_key.get_secret_value())

        async def synthesize(self, text: str) -> AsyncIterator[bytes]:
            request_start_ns = time.time_ns()
            first_frame_logged = False
            try:
                # VERIFY: cartesia 3.0.2 SDK call. The streaming entrypoint may be
                # client.tts.bytes(...) or client.tts.sse(...) or client.tts.websocket(...).
                # Pick the async-iterator-of-bytes form; document the chosen path.
                stream = self._client.tts.bytes(
                    model_id=self._config.model,
                    voice={"id": self._config.voice_id},
                    transcript=text,
                    output_format=_OUTPUT_FORMAT,
                    # default_emotion field name varies by SDK; verify and set.
                )
                async for chunk in stream:
                    if not first_frame_logged:
                        ttfb_ms = (time.time_ns() - request_start_ns) // 1_000_000
                        log.info(
                            "tts.first_frame",
                            ttfb_ms=ttfb_ms,
                            voice_id=self._config.voice_id,
                            model=self._config.model,
                        )
                        first_frame_logged = True
                    yield chunk
            except Exception as e:
                # Catch broad here intentionally — wrap *any* SDK or transport
                # error as CartesiaError. CLAUDE.md rule #4 prevents swallowing
                # downstream; the error MUST crash the process.
                if isinstance(e, CartesiaError):
                    raise
                raise CartesiaError(
                    voice_id=self._config.voice_id,
                    model=self._config.model,
                    reason=str(e),
                ) from e


    async def validate_credentials(config: SetupConfig) -> None:
        """Startup probe — small authenticated call to verify the API key works."""
        client = cartesia.AsyncCartesia(
            api_key=config.cartesia_api_key.get_secret_value()
        )
        try:
            # voices.list is a small read; validates key + network + service health.
            # Verify exact API: may be client.voices.list(limit=1) or client.voices.list().
            await client.voices.list()
        except Exception as e:
            raise StartupValidationError(stage="cartesia", reason=str(e)) from e
    ```

  - [ ] **Verify SDK shapes against the installed `cartesia==3.0.2`:**
    - Streaming call (`client.tts.bytes` vs `.sse` vs `.websocket`). Pick the async-iterator-of-bytes form.
    - Voice argument (`voice={"id": ...}` vs `voice_id=...`).
    - Output format keys (`container` / `encoding` / `sample_rate`).
    - Default emotion field (likely `voice_experimental_controls` or similar — search the SDK source).
    - Voices listing endpoint (`client.voices.list(...)`).
    - Base exception class (`cartesia.CartesiaError`? or generic `httpx.HTTPError`?).
    - Document the SDK version + chosen API path in a code comment so future bumps are auditable.

- [ ] **Task 3: Cartesia startup probe in `__main__.py`** (AC: #8)
  - [ ] Import `voice_agent_pipeline.tts.cartesia` at the top of `__main__.py` (don't re-import `cartesia` directly here — the boundary rule).
  - [ ] Add the probe call after the Anthropic one:
    ```python
    await talker_module.validate_credentials(config)
    log.info("startup.validated.talker")
    await cartesia_module.validate_credentials(config)
    log.info("startup.validated.cartesia")
    ```
  - [ ] Module-rename for clarity: import as `from voice_agent_pipeline.tts import cartesia as cartesia_module` to keep the call site readable.

- [ ] **Task 4: Verify boundary-concentration rule** (AC: #1, #13)
  - [ ] After implementing, run `grep -r "^import cartesia\|^from cartesia" src/` — expect exactly one file (`tts/cartesia.py`).
  - [ ] Same applies to test files: `grep -r "^import cartesia\|^from cartesia" tests/` should match only `tests/unit/tts/test_cartesia.py` (which patches the import inside `tts/cartesia.py`'s namespace, NOT importing `cartesia` directly).

- [ ] **Task 5: TLS posture documentation** (AC: #7)
  - [ ] Add a top-of-file comment block in `tts/cartesia.py` explaining: TLS validation is on by default in `httpx`/`cartesia`; v1 exposes no config knob to disable it; this is a security invariant — additions to `TtsConfig` MUST preserve it.
  - [ ] Add an explicit assertion/comment in the `__init__` showing that no `verify=False` is being passed to the SDK.

- [ ] **Task 6: Unit tests** (AC: #12)
  - [ ] Mock `voice_agent_pipeline.tts.cartesia.cartesia` (the module reference inside the file, not the global `cartesia` package).
  - [ ] Build stub async iterators for the streaming response. Pattern:
    ```python
    async def _stub_stream(chunks):
        for c in chunks:
            yield c

    @pytest.fixture
    def mock_cartesia(monkeypatch):
        mock_client = MagicMock()
        mock_client.tts.bytes = MagicMock(
            return_value=_stub_stream([b"chunk1", b"chunk2"])
        )
        monkeypatch.setattr(
            "voice_agent_pipeline.tts.cartesia.cartesia.AsyncCartesia",
            MagicMock(return_value=mock_client),
        )
        return mock_client
    ```
  - [ ] Use `caplog`-equivalent for structlog assertion (Story 1.3's logging tests have the working pattern; mirror that).
  - [ ] No live API calls in `tests/unit/`. Live verification is a manual one-off (Task 7).

- [ ] **Task 7: Live test — verify Cartesia plays through speaker** (AC: #2, #5, #9)
  - [ ] One-off Python script (NOT committed — `/tmp/test_cartesia.py` or similar; or a pytest mark gated behind `RUN_LIVE_TTS=true`):
    ```python
    import asyncio
    from voice_agent_pipeline.config.setup import load_setup_config
    from voice_agent_pipeline.tts.cartesia import CartesiaClient

    async def main():
        config = load_setup_config()
        client = CartesiaClient(config.tts, config.cartesia_api_key)
        chunks = []
        async for chunk in client.synthesize("Hello, this is OLAF speaking."):
            chunks.append(chunk)
        # Write to a wav file or pipe to play_test_tone-style speaker output.
        with open("/tmp/cartesia_out.pcm", "wb") as f:
            for c in chunks:
                f.write(c)
        # Then: aplay -f S16_LE -r 16000 -c 1 /tmp/cartesia_out.pcm

    asyncio.run(main())
    ```
  - [ ] Document in Dev Agent Record: TTFB observed (NFR4 baseline), audible quality assessment, total bytes received vs. expected duration. Story 2.5 will measure NFR1 end-to-end; Story 5.5 calibrates.

- [ ] **Task 8: Commit + push** — single commit titled `Story 2.3: CartesiaClient — Sonic-3 streaming TTS behind the Protocol seam`, then `git push`.

## Dev Notes

### Architectural intent

Story 2.3 is the **second external client** in the project — same shape as Story 2.2's Talker:

| Concern | Story 2.2 (Talker) | Story 2.3 (Cartesia) |
|---|---|---|
| Protocol seam | `TalkerClient` in `turn/talker.py` | `TTSClient` in `tts/client.py` |
| Concrete impl | `AnthropicTalker` | `CartesiaClient` |
| Boundary import | `import anthropic` only in `turn/talker.py` | `import cartesia` only in `tts/cartesia.py` |
| Startup probe | `validate_credentials()` in same file | `validate_credentials()` in same file |
| Error wrapping | `TalkerError` from any SDK error | `CartesiaError` from any SDK error |
| v1 retry policy | None — fail-fast | None — fail-fast |
| Returns | `str` (whole response) | `AsyncIterator[bytes]` (streamed) |

**Streaming is the key difference.** Talker is request-response (block on `messages.create`); Cartesia yields audio chunks as they arrive. NFR4 (TTFB ≤400 ms p95) and NFR1 (simple-turn ≤1500 ms p95) both depend on the streaming path — buffering the full audio stream before yielding would push p95 above the budget. The async iterator contract enforces this at the type level.

### What this story does NOT do

- **No SSML / emotion-tag parsing.** Story 3.3 builds the streaming splitter that strips Cartesia's `<emotion>...<emotion>` tags from text and dispatches them as `ExpressionEvent`s. v1 passes plain text through; the v1 system prompt forbids the Talker from emitting tags.
- **No frame wrapping.** `synthesize` yields `bytes`. Story 2.5's pipeline wraps each chunk into a Pipecat `AudioRawFrame` for `transport.output()`. Keep it simple: bytes in, bytes out — wrapping is a one-line `AudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1)` per chunk.
- **No barge-in.** Story 5.1 wires interruption. v1 plays the synthesized audio in full; barging in mid-sentence isn't supported.
- **No retry / stall watchdog.** First failure → process crashes → systemd restarts (Epic 5). v2 resilience layer adds these.
- **No voice cloning, no per-turn voice switching.** One voice, configured at startup.

### Cartesia SDK shape — what to verify

`cartesia==3.0.2` is the pinned version in `pyproject.toml`. The SDK has rewritten its API a few times; verify against the installed version. Key questions:

1. **Streaming entrypoint.** Probable candidates (check `cartesia.AsyncCartesia` attributes):
   - `client.tts.bytes(...)` — async iterator of bytes (what we want)
   - `client.tts.sse(...)` — SSE-flavored stream
   - `client.tts.websocket(...)` — websocket-based
   - `client.tts.stream(...)` — generic streaming

   Pick the async-iterator-of-bytes form. If only websocket is offered, that's fine — wrap it.

2. **Output format spec.** Probable shape:
   ```python
   output_format={"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}
   ```
   Some SDK versions use `OutputFormat(...)` typed objects instead of dicts. Adapt.

3. **Voice argument.** Possible:
   - `voice={"id": "<voice-id>"}`
   - `voice_id="<voice-id>"`
   - `voice=cartesia.Voice(id="...")`

4. **Default emotion.** Cartesia accepts emotion modifiers; the field name in 3.0.2 is likely:
   - `voice_experimental_controls={"emotion": ["positivity:high"]}`
   - `experimental_voice_controls={"emotion": [...]}`
   - or part of the `voice` object

   If unclear, omit `default_emotion` from the request and document. v1 doesn't strictly need it (Talker's plain-text reply will get whatever default Cartesia picks); Story 3.x will revisit anyway.

5. **Voices list.** `client.voices.list()` is the conventional name; verify.

6. **Exception types.** Find the SDK's base error class. Likely `cartesia.CartesiaError` or `cartesia.exceptions.CartesiaError`. If unclear, catch broad `Exception` in `synthesize` and wrap — better to over-catch than miss an SDK-specific subclass.

**Document the SDK version actually used and the chosen API path** in a code comment at the top of `tts/cartesia.py`. When Story 5.5 / future bumps land, that comment is the audit trail.

### Streaming + async iterator pattern — Python gotchas

`async def synthesize(self, text: str) -> AsyncIterator[bytes]:` with `yield` inside makes the function an **async generator**, not a coroutine that returns an iterator. Subtle but matters:

```python
# CORRECT — async generator
async def synthesize(self, text: str) -> AsyncIterator[bytes]:
    async for chunk in self._client.tts.bytes(...):
        yield chunk

# Caller usage:
async for chunk in client.synthesize("hello"):
    transport.send(chunk)
```

The `try: ... except Exception as e: raise CartesiaError(...) from e` block must wrap the **whole** loop. A bare `yield` inside a `try` is fine in Python; the exception handler fires if the upstream SDK raises during iteration.

**One subtle thing:** if the consumer (Story 2.5's pipeline) `break`s early or stops awaiting the iterator, Python's `aclose()` machinery will raise `GeneratorExit` inside the generator. Don't catch `GeneratorExit` — let it propagate so the SDK's stream cleanup runs.

### NFR4 baseline — first-frame latency

Story 5.5 will calibrate. For Story 2.3, just record the value:

- `request_start_ns = time.time_ns()` immediately before opening the stream.
- `first_frame_ns = time.time_ns()` on the first yielded chunk.
- Log once per call: `log.info("tts.first_frame", ttfb_ms=..., voice_id=..., model=...)`.

The architecture target is **≤400 ms p95**. On the dev host with a good network, expect 200-400 ms typical; first-call cold-start may be higher (TLS handshake, DNS).

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/tts/cartesia.py`
- `tests/unit/tts/__init__.py`
- `tests/unit/tts/test_cartesia.py`

It modifies:
- `src/voice_agent_pipeline/config/setup.py` (`TtsConfig`, `cartesia_api_key`)
- `src/voice_agent_pipeline/__main__.py` (Cartesia probe wired)
- `setup.toml` (`[tts]` block populated)
- `.env.example` (uncomment `CARTESIA_API_KEY`)
- `tests/unit/config/test_setup.py` (TTS block tests)

It does NOT modify:
- `pipeline.py` — Story 2.5 wires `CartesiaClient` into the pipeline.
- `tts/client.py` — Protocol already exists; only verify the signature, don't touch unless it doesn't fit.

### Testing standards

- **Mock at the module boundary inside `tts/cartesia.py`.** Patch `voice_agent_pipeline.tts.cartesia.cartesia` (the imported module reference), not the top-level package.
- **Use async generators in stub fixtures**, not lists, to model real Cartesia behavior:
  ```python
  async def _stub_stream(chunks):
      for c in chunks:
          yield c
  ```
- **Test the lazy yield property explicitly.** A test that asserts each chunk is delivered before the next is generated catches the "buffer the whole stream then yield" anti-pattern at the unit level.
- **No live API calls in `tests/unit/`.** Operator-driven live test (Task 7); Story 2.5 / Story 5.5 add live integration coverage.

### What "done" looks like

- `just check` exits 0; new unit tests pass.
- `just run` (with both `ANTHROPIC_API_KEY` and `CARTESIA_API_KEY` in `.env`): startup logs include `startup.validated.talker` AND `startup.validated.cartesia`. Pipeline still runs Story 1.7's listening loop unchanged (Cartesia isn't wired into the pipeline yet — that's Story 2.5).
- `grep -r "^import cartesia\|^from cartesia" src/` → exactly one file (`tts/cartesia.py`).
- One-off live test (Task 7): TTFB recorded; synthesized audio plays back through `aplay` or speaker.
- Story 2.5 can construct `CartesiaClient(config.tts, config.cartesia_api_key)` and consume `async for chunk in client.synthesize(text)` with no further plumbing.

### References

- [Source: build_documents/planning-artifacts/architecture.md#External Clients (Batch 4)] — Cartesia via SDK, async streaming, fail-fast, no retry.
- [Source: build_documents/planning-artifacts/architecture.md#Architectural Boundaries] — `cartesia` imported only in `tts/cartesia.py`.
- [Source: build_documents/planning-artifacts/architecture.md#Error Handling] — `CartesiaError(ExternalServiceError)`; never caught in v1 paths.
- [Source: build_documents/planning-artifacts/prd.md#FR15, FR17] — streaming TTS + voice ID config.
- [Source: build_documents/planning-artifacts/prd.md#FR34] — startup validation extends to Cartesia key.
- [Source: build_documents/planning-artifacts/prd.md#NFR4] — Cartesia TTFB ≤400 ms p95.
- [Source: build_documents/planning-artifacts/prd.md#NFR24] — TLS validation cannot be disabled.
- [Source: build_documents/planning-artifacts/epics.md#Story 2.3: CartesiaClient — Sonic-3 streaming TTS behind the Protocol seam]
- [Source: build_documents/implementation-artifacts/2-2-talker-client-anthropic.md] — sibling external-client story; mirror its boundary + probe + error-wrapping pattern.

## Dev Agent Record

### Agent Model Used

{{agent_model_name_version}}

### Debug Log References

### Completion Notes List

### File List
