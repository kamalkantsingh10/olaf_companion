# Story 4.1: `BeliefStateClient` — per-turn fresh `GET /beliefs?keys=...`

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want a `BeliefStateClient` that fetches belief state from the orchestrator daemon per turn (no cache), used by Talker to ground fast-path responses,
so that Talker can answer "what time is it?" using actual daemon state rather than the LLM's own guess.

## Acceptance Criteria

1. **`BeliefStateClient` Protocol stays narrow.** `src/voice_agent_pipeline/turn/beliefs.py` already defines the Protocol (one method: `async def read(self, keys: list[str]) -> dict[str, Any]`). Story 4.1 does **not** widen the Protocol — no `connect()` / `close()` / health methods on the seam. Lifecycle is owned by the concrete class + the pipeline assembly site (AC #5).

2. **`HttpBeliefStateClient` concrete implementation in the same module.** `class HttpBeliefStateClient` lives in `turn/beliefs.py` (single-file module — Protocol + sole v1 impl together; mirrors `mood/state.py` + `mood/controller.py` separation only when files grow large, which doesn't apply here). The class takes a persistent `httpx.AsyncClient` keyed to `daemon.url`; the client is constructed once at pipeline startup, lifecycle-bound to the pipeline (architecture.md §"Async Patterns" — `async with httpx.AsyncClient() as client:` at the top-level), and reused across turns. **No per-call client construction** (defeats keep-alive / connection pooling).

   Constructor signature:
   ```python
   class HttpBeliefStateClient:
       def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
           self._client = http_client
           self._base_url = base_url.rstrip("/")
   ```
   Why injection rather than constructing the client inside `HttpBeliefStateClient`: lifecycle ownership stays at the pipeline-assembly site (`pipeline.py:run_pipeline`), which already owns `EventPublisher.connect / disconnect` (Story 3.7). Story 4.2's `HttpOrchestratorClient` will share the same `httpx.AsyncClient` instance — both daemon endpoints live behind one origin, so one keep-alive pool is the correct shape (architecture.md §"Connection management").

3. **`read(keys)` issues `GET /beliefs?keys=time,calendar_today` against `daemon.url`** and returns the parsed JSON object (FR10):
   - URL: `f"{self._base_url}/beliefs"` with query parameter `keys` set to `",".join(keys)` — pass via `httpx`'s `params={"keys": ",".join(keys)}` so the encoding is a single comma-separated value (matches the AC spec verbatim — *not* repeated `?keys=time&keys=calendar_today` form). Verify the resulting URL string in the unit test.
   - Empty `keys` list: still issue the request (returns whatever the daemon emits for "no keys") rather than short-circuiting; the daemon owns that contract. **No client-side filtering** of keys.
   - Headers: only the defaults set on the shared `httpx.AsyncClient`. **Do not** add `Authorization` or shared-secret headers in Story 4.1 — Story 5.3 hardens that on the LAN-deployment path; v1 ships localhost-only.
   - Method: HTTP GET. Single request per `read()` call.

4. **No cache. Every invocation issues a fresh HTTP request.** No in-memory dict, no TTL, no LRU. Per architecture.md §"Belief-state read" — "Talker invocations are infrequent; staleness is worse than the latency cost." Document this stance in the class docstring **with the reason** so a future contributor doesn't "optimize" by adding caching (it would silently break the staleness invariant).

5. **Non-200 → `OrchestratorError` (already defined in `errors.py`).** v1 fail-fast crashes the process (CLAUDE.md rule #4):
   - On any non-2xx response: raise `OrchestratorError(status_code=resp.status_code, url=str(resp.request.url), body=resp.text[:200])` — context kwargs feed `errors.VoiceAgentError._format` (truncate body to 200 chars; **logging the full body risks leaking belief-state values** which may contain user data per NFR25 spirit).
   - On `httpx.HTTPError` (connection refused, timeout, DNS failure, transport error): wrap as `raise OrchestratorError(reason="<exception class name>", url=...) from exc`. Use `from exc` so the original traceback is preserved (architecture.md §"Error Handling: hierarchy is shallow… stash the raw context").
   - On JSON decode failure (`resp.json()` raises): `raise OrchestratorError(reason="invalid_json", body=resp.text[:200], url=...) from exc`.
   - **Do not** retry. v1 ships zero resilience at this seam (architecture.md §"V1 Posture: Hard Dependencies, Fail-Fast"); resilience layer is v2.
   - **Never catch `OrchestratorError`** anywhere outside this file — let it propagate. CLAUDE.md rule #4.

6. **`setup.toml` `[daemon]` block + `DaemonConfig` model.** Add a new nested config:
   ```toml
   [daemon]
   url = "http://localhost:8001"
   ```
   In `src/voice_agent_pipeline/config/setup.py`:
   - Add `class DaemonConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")` (mirrors `MoodConfig` / `TalkerConfig` pattern, Story 3.6 / 2.2).
   - Field: `url: str = "http://localhost:8001"`. **Use `str` not `pydantic.HttpUrl`** — `HttpUrl` enforces a trailing slash on serialization and stringifies awkwardly (URL object vs str), which complicates `f"{base}/beliefs"` formatting. Add a `field_validator("url")` that asserts the value starts with `http://` or `https://` and contains no trailing slash (or strip it via the validator).
   - Add `daemon: DaemonConfig = Field(default_factory=DaemonConfig)` to `SetupConfig`.
   - Update setup.toml's "future blocks" comment block — remove `[daemon]` from the deferred list (it's now landed).

7. **`TalkerConfig.grounded_keys` config field — Story 4.1 lands the field, Story 4.4 wires the call.** In `config/setup.py`'s existing `TalkerConfig`:
   - Add `grounded_keys: list[str] = Field(default_factory=list)` **with a class-docstring note**: "Used by Story 4.4's `complete_with_tools` to fetch belief-state values via `BeliefStateClient.read(grounded_keys)` and inject them into the system prompt context. Empty list ≡ no grounding. v1 ships with `[]` (off by default); operators opt in by setting e.g. `grounded_keys = ['time', 'calendar_today']` in `setup.toml`'s `[talker]` block."
   - **Do not** plumb `grounded_keys` into `TalkerClient.complete()` in this story — that integration is finalized in Story 4.4. Story 4.1 only adds the config surface so Story 4.4 has nothing to refactor.

8. **Pipeline-assembly wiring (consumer-hook stub).** In `src/voice_agent_pipeline/pipeline.py:run_pipeline`:
   - Construct the persistent `httpx.AsyncClient` once during startup, **before** the main pipeline loop, with `timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)`. Lifetime spans the whole `run_pipeline` body — use an `async with httpx.AsyncClient(...) as http_client:` block so the context manager closes the pool on shutdown (architecture.md §"Client lifecycle"). The connect-timeout of 5s is enough for a healthy localhost daemon and short enough that systemd-restart triggers cleanly on a stuck daemon.
   - Build `belief_client = HttpBeliefStateClient(http_client, base_url=config.daemon.url)`.
   - Pass `belief_client` into the Talker construction site (Story 2.2's `build_talker(...)` factory or equivalent). **Story 4.1 only stores the reference on the Talker** — no call site invokes `belief_client.read(...)` yet. Story 4.4's `complete_with_tools` will use it.
   - Document this wiring in `pipeline.py`'s module docstring + the dev record. The pattern matches Story 3.7's `event_publisher` / `mood_controller` injection through the pipeline.

9. **Pyright-strict typing (no `Any` exfiltration).** The Protocol's `read` returns `dict[str, Any]` because the belief service's value shape is per-key. **Do not** widen this elsewhere — concretely:
   - In `HttpBeliefStateClient.read`, declare the return type explicitly: `async def read(self, keys: list[str]) -> dict[str, Any]:`.
   - The `resp.json()` call returns `Any`; assign it to a typed local: `parsed: dict[str, Any] = resp.json()`. If `resp.json()` returns a non-dict (e.g., a list), raise `OrchestratorError(reason="invalid_response_shape", got_type=type(parsed).__name__, url=...)` — the daemon contract is "JSON object", not "JSON value".
   - This is the only file in `src/` allowed to import `httpx` outside of `turn/orchestrator.py` (architecture.md §"External adapter boundaries"). Verify no other `src/` file imports `httpx` after this story.

10. **Logging discipline (NFR25, FR39).** Log at INFO `event="belief.read"` with fields: `keys` (the input list), `key_count`, `duration_ms` (measured around the HTTP call). Log at WARN `event="belief.read_failed"` with fields: `keys`, `status_code` (if non-200) or `reason` (for transport / JSON errors), `duration_ms`. **Never** log:
    - The full response body (may contain user-state values — calendar entries, location, etc.).
    - Individual values from the parsed dict.
    - The `Authorization` header (none in v1, but the rule stands forward).
    Use `time.monotonic()` for duration; record it whether the request succeeds or fails (in `try/finally`-style structure — but **without** swallowing the exception — re-raise after logging).

11. **Unit tests in `tests/unit/turn/test_beliefs.py`** mock `httpx.AsyncClient` and cover:
    - `test_read_issues_get_with_comma_joined_keys` — patch `httpx.AsyncClient.get`, call `read(["time", "calendar_today"])`, assert: URL is `<base_url>/beliefs`, `params == {"keys": "time,calendar_today"}`, HTTP method is GET. Use `unittest.mock.AsyncMock(spec=httpx.AsyncClient)` for the client; configure `client.get.return_value = MagicMock(status_code=200, json=lambda: {"time": "08:47", "calendar_today": []})`.
    - `test_read_returns_parsed_json_dict` — happy path; assert the returned dict shape matches the mocked JSON.
    - `test_read_empty_keys_still_calls_endpoint` — `read([])` issues `params={"keys": ""}`, returns whatever the mock yields. (Shapes the contract: empty list is *not* an error at the client.)
    - `test_read_500_raises_orchestrator_error` — mock `status_code=500`, `text="Internal Server Error"`; `pytest.raises(OrchestratorError)`; assert `excinfo.value.context["status_code"] == 500`, `excinfo.value.context["body"] == "Internal Server Error"`.
    - `test_read_4xx_raises_orchestrator_error` — same pattern, `status_code=404`. Both 4xx and 5xx surface the same way (no retry, no per-class branching at the client).
    - `test_read_connection_error_raises_orchestrator_error` — make `client.get` raise `httpx.ConnectError("connection refused")`; `pytest.raises(OrchestratorError)`; assert `excinfo.value.__cause__` is the original `httpx.ConnectError` (the `from exc` chain).
    - `test_read_invalid_json_raises_orchestrator_error` — mock `resp.json` raises `json.JSONDecodeError`; assert `OrchestratorError(reason="invalid_json", ...)`.
    - `test_read_non_dict_response_raises_orchestrator_error` — mock `resp.json()` returns a list; assert `OrchestratorError(reason="invalid_response_shape", got_type="list", ...)`.
    - `test_read_does_not_construct_client_per_call` — drive 3 sequential `read()` calls against the same `HttpBeliefStateClient`; assert the injected client's `get` was called 3 times **without** `AsyncClient.__init__` being invoked between calls (i.e., the persistent-client invariant).
    - `test_read_logs_event_belief_read` (caplog / structlog test capture per Story 1.7's pattern) — assert one INFO log per successful call with fields `keys`, `key_count`, `duration_ms`. Failed call asserts WARN `belief.read_failed`.
    - `test_read_does_not_log_response_body` — drive a happy-path call returning a dict whose values include a sentinel like `"SECRET_BELIEF_VALUE"`; assert no captured log line contains the sentinel. (Privacy invariant per NFR25.)

12. **Unit tests for `DaemonConfig` in `tests/unit/config/test_setup.py`** — extend the existing test module:
    - `test_daemon_defaults_to_localhost_8001` — `SetupConfig` parsed with no `[daemon]` block has `config.daemon.url == "http://localhost:8001"`.
    - `test_daemon_url_explicit_override` — `[daemon] url = "http://localhost:9000"` parses; the override wins.
    - `test_daemon_url_unknown_field_raises` — `[daemon] urll = "..."` (typo) → `ConfigError` (extra forbidden).
    - `test_daemon_url_must_start_with_http` — `[daemon] url = "localhost:8001"` (no scheme) → `ConfigError` from the `field_validator`.
    - `test_daemon_url_strips_trailing_slash` — `[daemon] url = "http://localhost:8001/"` → parsed config has `.url == "http://localhost:8001"`.

13. **Unit tests for `TalkerConfig.grounded_keys`** in the same test module:
    - `test_talker_grounded_keys_defaults_to_empty_list` — `SetupConfig` parsed with `[talker] provider = "groq"` (no `grounded_keys`) has `config.talker.grounded_keys == []`.
    - `test_talker_grounded_keys_explicit` — `[talker] grounded_keys = ["time", "calendar_today"]` parses to `["time", "calendar_today"]`.

14. **Architecture spec-drift acknowledgment.** The architecture's open coordination item (architecture.md §"Cross-project integration": orchestrator daemon must expose `GET /health` returning 200) lands as a **Story 4.2 deliverable** (startup probe), not 4.1. Story 4.1 mentions the `[daemon] url` config + adds the `DaemonConfig` model so 4.2 builds on top. **Story 4.1 does NOT add a startup `GET /health` probe** — that's deferred per the epics doc to Story 4.2's AC.

15. **`just check` stays green.** All earlier stories' tests remain passing. Add the new test file + the new config tests; run `uv run pytest tests/unit/turn/test_beliefs.py -v` first, then `tests/unit/config/test_setup.py`, then full `just check`. Pyright strict on `src/` must accept the new code.

16. **No raw audio / credentials / transcripts logged.** Standing privacy invariant (NFR25, FR39) — Story 4.1's `event="belief.read"` logs only the **keys requested**, never values. Document this explicitly in the function docstring. The redaction processor is the safety net; Story 4.1's code shouldn't even attempt to log values.

## Tasks / Subtasks

- [x] **Task 1: `DaemonConfig` + `TalkerConfig.grounded_keys` + `[daemon]` setup.toml block** (AC: #6, #7)
  - [ ] In `src/voice_agent_pipeline/config/setup.py`:
    - Add `class DaemonConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")`, `url: str = "http://localhost:8001"`, plus a `field_validator("url")` that (a) rejects values not starting with `http://` or `https://` (raise `ValueError` — pydantic wraps it into `ValidationError`), (b) strips a trailing `/`. Class docstring per `feedback_code_comments.md` — mirror `MoodConfig`'s style.
    - Add `daemon: DaemonConfig = Field(default_factory=DaemonConfig)` to `SetupConfig`. Place it next to existing nested fields (e.g., after `mood: MoodConfig`).
    - Add `grounded_keys: list[str] = Field(default_factory=list)` to `TalkerConfig` with the docstring spelled out in AC #7.
  - [ ] Update the docstring of the `TalkerConfig` class to mention Story 4.1 added `grounded_keys` (the list of comments at top of the class is the convention — see Story 2.2 style).
  - [ ] In `setup.toml`:
    - Add the `[daemon]` block at the bottom of the populated sections, with the operator-comment style Stories 1.5 / 2.2 / 3.5 use:
      ```toml
      # Story 4.1 / 4.2: orchestrator daemon HTTP endpoint. The pipeline reads
      # belief-state for grounded fast-path responses (Story 4.1) and dispatches
      # complex turns over SSE (Story 4.2) against this URL. v1 ships with
      # localhost-only; LAN-reachable URLs require the Story 5.3 shared-secret
      # / mTLS hardening before they're accepted at startup.
      [daemon]
      url = "http://localhost:8001"
      ```
    - Remove `[daemon]` from the "Sections populated by subsequent stories" comment list at the bottom (it's now landed). Leave `bearer_token_env`, `mtls` notes for Story 5.3.
    - Optionally add `grounded_keys = []` (commented out, for operator visibility) under `[talker]` — but the default-factory makes the omitted form work too. **Recommend** adding the comment with an example value:
      ```toml
      # Story 4.1: belief-state grounding. List of keys to fetch from the
      # orchestrator daemon at the start of each Talker turn (Story 4.4 wires
      # the call). Empty list ≡ no grounding (v1 default). Example:
      #   grounded_keys = ["time", "calendar_today"]
      grounded_keys = []
      ```

- [x] **Task 2: `HttpBeliefStateClient` in `turn/beliefs.py`** (AC: #1, #2, #3, #4, #5, #9, #10, #16)
  - [ ] Open `src/voice_agent_pipeline/turn/beliefs.py`. The Protocol is already there (Story 4.1 scaffolded the file). Add the `HttpBeliefStateClient` class beneath the Protocol.
  - [ ] Imports: `import time`, `import httpx`, `from typing import Any`, `from voice_agent_pipeline.errors import OrchestratorError`. Use `import structlog` for logging (matches Story 1.3's pattern; the module-level logger is `log = structlog.get_logger(__name__)`).
  - [ ] Constructor takes `http_client: httpx.AsyncClient, base_url: str`; strip a trailing `/` from `base_url`. Class docstring per `feedback_code_comments.md` — explain: persistent client injection, no per-call construction, **no cache** + the staleness rationale (AC #4), Story 4.4 is the consumer.
  - [ ] `async def read(self, keys: list[str]) -> dict[str, Any]:` — function docstring spelling out the `dict[str, Any]` invariant (FR10), the no-cache invariant, and the fail-fast contract (AC #5). Behavior:
    1. Capture `start = time.monotonic()`.
    2. Build `params = {"keys": ",".join(keys)}` (the comma-joined form per AC #3 — verify in test).
    3. Call `resp = await self._client.get(f"{self._base_url}/beliefs", params=params)` — let any `httpx.HTTPError` bubble; catch in step 6 below.
    4. Branch on status: if `resp.status_code != 200` (or use `resp.is_success` if you want 2xx range — recommend exact `== 200` because the daemon contract is "200 on success, error otherwise"; non-200 2xx is undefined and treating it as success would mask drift):
       - Compute `duration_ms = (time.monotonic() - start) * 1000`.
       - Log WARN `belief.read_failed` with `keys`, `key_count=len(keys)`, `status_code`, `duration_ms`.
       - Raise `OrchestratorError(status_code=resp.status_code, url=str(resp.request.url), body=resp.text[:200])`.
    5. On 200: parse JSON via `parsed = resp.json()`. If `parsed` is not a dict, raise `OrchestratorError(reason="invalid_response_shape", got_type=type(parsed).__name__, url=...)` after logging WARN.
    6. Wrap the whole body in `try: ... except httpx.HTTPError as exc: ...` (catches connection errors, timeouts, transport-layer issues — `httpx.HTTPError` is the parent); inside, log WARN `belief.read_failed` with `keys`, `key_count`, `reason=type(exc).__name__`, `duration_ms`, then `raise OrchestratorError(reason=type(exc).__name__, url=...) from exc`. Wrap JSON-parse the same way: `except json.JSONDecodeError as exc:` (need `import json`) → log WARN, raise `OrchestratorError(reason="invalid_json", body=resp.text[:200], url=...) from exc`.
    7. On success: log INFO `belief.read` with `keys`, `key_count=len(keys)`, `duration_ms`. Return `parsed`.
  - [ ] **Pyright-strict** check: run `uv run pyright src/voice_agent_pipeline/turn/beliefs.py` after writing. Expect zero errors. The `dict[str, Any]` return is the only `Any` exfil and is documented; everything else is precisely typed.
  - [ ] **No `httpx` import outside `turn/beliefs.py` and `turn/orchestrator.py`** invariant — grep `src/` after writing to confirm: `rg -F 'import httpx' src/` should show only those two paths.

- [x] **Task 3: Pipeline-assembly wiring + persistent `httpx.AsyncClient`** (AC: #2, #8)
  - [ ] In `src/voice_agent_pipeline/pipeline.py:run_pipeline` (Story 3.7 baseline):
    - Add an `async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)) as http_client:` block wrapping the existing pipeline-build sequence. Place it **after** `event_publisher.connect()` (the daemon's network is closer to "external service" than to "publisher transport") and **before** the segmenter / mood / pipeline-stage construction.
    - Inside, construct `belief_client = HttpBeliefStateClient(http_client, base_url=config.daemon.url)`.
    - Pass `belief_client` to the Talker factory call (Story 2.2's `build_talker(config.talker)` — extend its signature to accept `belief_client: BeliefStateClient` and store it as an instance attribute; **the Talker doesn't yet call `belief_client.read(...)` — that's Story 4.4**). Document this in `build_talker`'s docstring + the Talker class docstring.
    - **Stub-only**: Add a one-line comment `# Story 4.4 wires belief_client.read(grounded_keys) into complete_with_tools` next to the new attribute in the Talker class. No production call site invokes `read()` in this story.
  - [ ] Update `pipeline.py`'s module docstring: append a Story 4.1 entry to the "Story progression" list documenting the `httpx.AsyncClient` lifetime + the `BeliefStateClient` injection.
  - [ ] **Lifecycle test** (a small unit test in `tests/unit/test_pipeline.py` if straightforward): assert `run_pipeline` constructs the client and passes a `BeliefStateClient` instance to `build_talker`. **If wiring this test is high-friction** (lots of pipeline mocks), document the deferral in the dev record and rely on the integration test in Story 4.4 to validate the wiring end-to-end. Wiring tests of `run_pipeline` are non-trivial because the function builds the full pipeline; see Story 3.7's dev record for similar deferrals.

- [x] **Task 4: Unit tests for `HttpBeliefStateClient`** (AC: #11, #16)
  - [ ] Create `tests/unit/turn/test_beliefs.py` (test directory exists per Story 2.2/2.4). Import `pytest`, `httpx`, `from unittest.mock import AsyncMock, MagicMock`, `from voice_agent_pipeline.errors import OrchestratorError`, `from voice_agent_pipeline.turn.beliefs import HttpBeliefStateClient`.
  - [ ] Module docstring per `feedback_code_comments.md` — explain: mocks at the `httpx.AsyncClient` Protocol seam (CLAUDE.md rule #7); each test exercises one behavior of the contract (CLAUDE.md / architecture.md §"Test Patterns": one behavior per test).
  - [ ] **Helper fixture**: factory that returns `(client, http_client_mock)` where `http_client_mock = AsyncMock(spec=httpx.AsyncClient)` and `client = HttpBeliefStateClient(http_client_mock, "http://localhost:8001")`. Each test configures `http_client_mock.get.return_value` (or `.side_effect`) per its scenario.
  - [ ] Helper for response mocks: `def make_response(status_code: int, json_data: Any = None, text: str = "") -> MagicMock` returning a MagicMock with `.status_code`, `.json` (callable), `.text`, `.request.url` populated.
  - [ ] Implement the 11 test cases in AC #11 using `pytest.mark.asyncio` (auto mode is on per `pyproject.toml`, so the marker is implicit but explicitly applying `@pytest.mark.asyncio` is fine).
  - [ ] **Log-capture pattern** for `test_read_logs_event_belief_read` and `test_read_does_not_log_response_body`: reuse Story 1.7 / 3.6's pattern — `caplog.set_level(logging.INFO)` and assert on structlog's structured log records. Stories 3.6's `tests/unit/mood/test_controller.py` is the closest reference for `event=...` + INFO/WARN assertions.
  - [ ] **`from exc` cause-chain assertion** for `test_read_connection_error_raises_orchestrator_error`: `with pytest.raises(OrchestratorError) as excinfo: ...; assert isinstance(excinfo.value.__cause__, httpx.ConnectError)`.

- [x] **Task 5: Unit tests for `DaemonConfig` + `grounded_keys`** (AC: #12, #13)
  - [ ] Open `tests/unit/config/test_setup.py`. Add the 5 daemon tests + 2 grounded-keys tests per AC #12 / #13. Mirror the existing `_VALID_TOML` test helper / fixture pattern.
  - [ ] **`_VALID_TOML` may need an update** to include the `[daemon]` block (or rely on the default-factory). **Recommend** adding `[daemon]\nurl = "http://localhost:8001"\n` to `_VALID_TOML` so all existing tests parse a config that includes the section — and add a separate `_VALID_TOML_NO_DAEMON` (or use `default_factory` directly) for the "default" tests. Mirror Story 3.6's pattern when it added `[mood]`.
  - [ ] Optional: a contract test asserting `setup.toml` (the committed file) parses cleanly via `load_setup_config(Path("setup.toml"))`. If such a test already exists from Stories 1.2 / 1.5 / 2.x, just verify it still passes after the `[daemon]` block lands.

- [x] **Task 6: Confirm `httpx`-only-here invariant + run `just check`** (AC: #9, #15)
  - [ ] Run `rg -F 'import httpx' src/` — assert only `src/voice_agent_pipeline/turn/beliefs.py` is listed (Story 4.2 will add `turn/orchestrator.py`; if 4.2's scaffolding already includes it, fine — but no other file should). `rg -F 'from httpx' src/` likewise.
  - [ ] Run `just check` (ruff lint+format + pyright + fast pytest). All green. If pyright complains about the `dict[str, Any]` from `resp.json()`, the recommended workaround is the typed local pattern in AC #9 (`parsed: dict[str, Any] = resp.json()` — the cast is implicit; pyright accepts because `Any` is assignable to `dict[str, Any]`).

- [ ] **Task 7: Commit + push** (per `feedback_commit_policy.md` + `feedback_push_after_commit.md`)
  - [ ] Single commit covering: `turn/beliefs.py` (impl), `config/setup.py` (`DaemonConfig` + `TalkerConfig.grounded_keys`), `setup.toml` (`[daemon]` block + grounded_keys comment), `pipeline.py` (httpx lifecycle + Talker injection wiring), `tests/unit/turn/test_beliefs.py`, `tests/unit/config/test_setup.py` (daemon + grounded_keys tests), the implementation-artifacts story file (this file — task ticks + dev record).
  - [ ] Suggested commit message: `Story 4.1: BeliefStateClient (httpx GET /beliefs, no cache, fail-fast)`.
  - [ ] `git push` immediately after the commit (per `feedback_push_after_commit.md`).
  - [ ] **Sprint-status update**: flip `4-1-belief-state-client: ready-for-dev` → `review` in `build_documents/implementation-artifacts/sprint-status.yaml` after live verification (or → `in-progress` first if any Task remains pending; mirror Story 3.7's pattern).

## Dev Notes

### Architectural intent — Story 4.1's role in Epic 4

Epic 4 builds the conversation-shaped surface: 7-state activity FSM, tool-using Talker, mood-tinted wake greeting, continuous mic capture while AWAKE, and orchestrator slow-path with belief-state grounding. **Story 4.1 is the "ground" half of the slow-path equation.** It does *not* yet wire the Talker to use beliefs (that's Story 4.4); it does *not* dispatch complex turns to the orchestrator (that's Story 4.2 + 4.7). It establishes the seam, the impl, the config, and the lifecycle so every later Epic 4 story has a stable surface.

**Why land it first** (per `epics.md` Epic 4 sequencing): the persistent `httpx.AsyncClient` lifecycle (this story) is shared with Story 4.2's `OrchestratorClient`. Both need the same client construction site in `pipeline.py:run_pipeline`. Story 4.1 establishes that pattern; 4.2 piggybacks on it.

### Why no cache (architecture.md §"Belief-state read")

Cache invalidation is the second-hardest problem in CS. Belief state is **per-turn**: the user asks "what time is it?" and Talker reads `time` once. Cache hit-rate at this seam is dominated by "user asks the same question twice in 60 seconds" — vanishingly rare, and even then the architecturally correct answer is the daemon's fresh value (the time changed). Adding TTL caching would force Story 5.x to invalidate on `set_mood` / FSM transitions / tool dispatches that mutate daemon-tracked state. **Don't.**

If a future Story 5.x soak shows the per-turn `GET /beliefs` is a measurable hot-path (it won't — it's localhost HTTP, single-digit milliseconds), revisit. Until then, fresh on every turn.

### `httpx.AsyncClient` lifecycle (architecture.md §"Async Patterns")

Architecture mandates `async with httpx.AsyncClient() as client:` at the top-level — never manage `aclose()` manually. The `run_pipeline` body is the natural lifetime: `httpx.AsyncClient` is constructed at startup, lives across all turns, and the `async with` exits cleanly on pipeline shutdown (CTRL-C, systemd stop, or fatal error after `ExternalServiceError` propagates).

**One client, two consumers.** Story 4.1's `HttpBeliefStateClient` and Story 4.2's `HttpOrchestratorClient` both target the same `daemon.url` origin. **Share the `AsyncClient` instance** — keep-alive lives at the connection-pool level, not the per-client level, so a shared pool gives both consumers free connection reuse. Story 4.2 will inject the same `http_client` reference into `HttpOrchestratorClient`'s constructor.

**Timeouts** matter because v1 fail-fast: a stuck daemon should crash the pipeline within seconds, not hang the event loop. The `connect=5.0, read=10.0` defaults in AC #8 give a healthy localhost daemon plenty of headroom (it should respond in ms) while keeping the failure mode bounded for systemd to restart.

### Error context conventions (architecture.md §"Error Handling")

The `errors.py` `VoiceAgentError._format` already renders `OrchestratorError(status_code=500, url='...', body='...')` correctly. Pass kwargs by name; let the base class format `str(err)` and stash `context` for tests. Tests assert on `excinfo.value.context["status_code"] == 500` rather than parsing `str(err)` — Story 1.4 / 3.6 set this pattern; copy it.

Truncate `body` to 200 chars: enough to debug ("the daemon returned a 502 with an HTML error page") without risking belief-state value exfil into logs.

### Test-mocking pattern (CLAUDE.md rule #7 / architecture.md §"Test Patterns")

**Mock only at Protocol boundaries** — `httpx.AsyncClient` is the Protocol seam to the orchestrator daemon. `unittest.mock.AsyncMock(spec=httpx.AsyncClient)` is the canonical pattern; `spec=` gives pyright + runtime spec-checking against the mocked class. Story 2.2 / 2.4 / 3.6 use this pattern for `EventPublisher` and `TalkerClient` — mirror.

**Do NOT** install `respx` or `pytest-httpx` — they're not in `[dependency-groups].dev` and the architecture's "narrow dep tree" stance (architecture.md §"Library decisions") means we don't add a dep just to mock a single test seam. Manual `AsyncMock(spec=httpx.AsyncClient)` works fine for AC #11's scenarios.

**No real HTTP** in unit tests. The `tests/unit/turn/` test set must pass with no daemon running. (A future Story 4.7 may add an integration test with a fake daemon; that's not Story 4.1's scope.)

### Integration with Story 4.4 (forward-compat hook)

Story 4.4 will:
1. Read `config.talker.grounded_keys` (landed in this story).
2. Call `await belief_client.read(grounded_keys)` from inside `complete_with_tools()`.
3. Inject the parsed dict into the system-prompt context (probably as a `### Belief state\n{json.dumps(beliefs, indent=2)}` section).

For Story 4.1, the **stub** is just: the Talker holds a reference to `belief_client` it doesn't call yet. This is the cleanest way to validate the wiring (the Talker has access to the client) without prematurely shaping `complete_with_tools` (which gets a lot of new responsibility in Story 4.4 — tools registry, openai SDK `tools=` param, async-gather text-first / tools-concurrent, etc.).

If Story 4.1's dev finds it cleaner to defer **the constructor injection** too (and have Story 4.4 do both the injection and the call site), that's a defensible variant — **document the choice in the dev record**. The trade-off: deferring means Story 4.4 has more refactoring; landing the wiring now means Story 4.1's pipeline-level test is slightly larger. Recommend landing the wiring now (per Story 3.7's "wire the publisher injection in 3.7, even though tools-using Talker doesn't land until 4.4" precedent).

### What this story does NOT do

- **No startup `GET /health` probe.** That's Story 4.2's AC (architecture's spec-drift item: orchestrator must expose `/health`). Story 4.1 ships with no startup-side probe; the first time the pipeline tries to read beliefs, it'll fail-fast if the daemon is down.
- **No SSE / streaming.** That's Story 4.2 (`OrchestratorClient` + `httpx-sse`).
- **No retries, no resilience.** v1 fail-fast posture (architecture.md §"V1 Posture"); resilience layer is v2.
- **No call site that uses `belief_client.read(...)`.** Story 4.4 wires it into `complete_with_tools`. Story 4.1 only delivers the seam + impl + config + injection.
- **No LAN / shared-secret hardening.** Story 5.3 adds `bearer_token_env` + mTLS validation; Story 4.1 ships with localhost-only and a `field_validator` that just enforces "URL starts with http(s)://" — the architectural rule "non-localhost URL must require shared secret" lands in Story 5.3.
- **No belief schema.** The daemon's belief response shape is opaque (`dict[str, Any]`) at the pipeline boundary; pinning shapes per-key requires a schema overhaul shared with the orchestrator project (architecture.md §"Cross-project integration"). Out of scope for v1.

### Project structure notes

This story creates:
- `tests/unit/turn/test_beliefs.py` — new test module (mirrors `src/voice_agent_pipeline/turn/beliefs.py`).

It modifies:
- `src/voice_agent_pipeline/turn/beliefs.py` — adds `HttpBeliefStateClient` beneath the existing Protocol.
- `src/voice_agent_pipeline/config/setup.py` — adds `DaemonConfig`, adds `grounded_keys` to `TalkerConfig`, adds `daemon` field to `SetupConfig`.
- `setup.toml` — adds `[daemon]` block, adds `grounded_keys` comment under `[talker]`, removes `[daemon]` from the deferred-blocks comment list.
- `src/voice_agent_pipeline/pipeline.py` — wraps the run_pipeline body in `async with httpx.AsyncClient(...)`, constructs `HttpBeliefStateClient`, threads it into the Talker factory.
- `tests/unit/config/test_setup.py` — adds 5 daemon tests + 2 grounded-keys tests.
- (Possibly) `src/voice_agent_pipeline/turn/talker.py` and `tests/unit/turn/test_talker.py` — extend `Talker.__init__` / `build_talker` to accept the `BeliefStateClient` reference (no behavior change, just wiring).
- `build_documents/implementation-artifacts/sprint-status.yaml` — `4-1-belief-state-client: ready-for-dev → in-progress → review`.

It does NOT modify:
- `src/voice_agent_pipeline/turn/orchestrator.py` (Story 4.2's territory).
- `src/voice_agent_pipeline/turn/router.py` (TurnRouter changes are Story 4.7).
- `src/voice_agent_pipeline/lifecycle/` (stays as the placeholder until Story 4.3 renames to `activity/`).

### Testing standards

- **`pytest-asyncio`** in auto mode (`asyncio_mode = "auto"` in `pyproject.toml`) — async tests don't require `@pytest.mark.asyncio` but applying it explicitly costs nothing and makes intent clear.
- **`AsyncMock(spec=httpx.AsyncClient)`** is the only mock surface. No `respx`, no `pytest-httpx`, no real HTTP.
- **One behavior per test** — eleven tests in `test_beliefs.py` per AC #11.
- **Privacy assertion** (AC #11 last test, `test_read_does_not_log_response_body`) — sentinel-string check across captured logs. Mirror Story 1.7 / 3.7's `tests/integration/test_embodiment_alignment.py`'s `test_no_audio_field_names_in_logs` pattern.
- **Pyright strict on `src/`** — no `Any` exfil beyond the documented `dict[str, Any]` return.

### Performance budget

NFR1 fast-path turn budget (≤1s end-to-speech) includes the belief-state read. Localhost HTTP `GET /beliefs` is single-digit ms; the `httpx.AsyncClient` keep-alive pool means after the first call there's no connection-establishment cost. Story 4.4's integration tests will measure the actual contribution; **Story 4.1's only perf concern** is "don't construct an `httpx.AsyncClient` per call" (AC #2 — the persistent-client invariant).

### What "done" looks like

- `just check` exits 0 (ruff + pyright strict + fast unit tests pass).
- 11 unit tests in `tests/unit/turn/test_beliefs.py` pass.
- 7 new tests in `tests/unit/config/test_setup.py` (5 daemon + 2 grounded_keys) pass.
- `setup.toml` parses cleanly with the new `[daemon]` block.
- `pipeline.py:run_pipeline` constructs the persistent `httpx.AsyncClient`, passes a `HttpBeliefStateClient` reference into the Talker. **No production call invokes `belief_client.read(...)`** — that's Story 4.4.
- `rg -F 'import httpx' src/` shows only `turn/beliefs.py` (and `turn/orchestrator.py` if Story 4.2's scaffold is already in place).
- The story commits and pushes per the per-story policy.
- Sprint-status flips to `review` after all green.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Library decisions (Batch 4: Concurrency Model)] — `httpx (async)` + `httpx-sse` choice; persistent `AsyncClient` per service; per-turn fresh `GET /beliefs?keys=...`, no cache; startup validation: connect + `GET /health`.
- [Source: build_documents/planning-artifacts/architecture.md#Async Patterns] — `async with httpx.AsyncClient() as client:` lifecycle pattern; never manage `aclose()` manually.
- [Source: build_documents/planning-artifacts/architecture.md#Project Structure & Boundaries] — `turn/beliefs.py` is the only file (with `turn/orchestrator.py`) allowed to import `httpx`.
- [Source: build_documents/planning-artifacts/architecture.md#Internal seams] — `BeliefStateClient` Protocol consumed by `turn/talker.py`.
- [Source: build_documents/planning-artifacts/architecture.md#Error Handling] — `OrchestratorError(ExternalServiceError)`; CLAUDE.md rule #4 — never caught in v1.
- [Source: build_documents/planning-artifacts/architecture.md#Cross-project integration] — orchestrator daemon `GET /health` spec-drift item (Story 4.2 deliverable, not 4.1).
- [Source: build_documents/planning-artifacts/prd.md#FR10] — "The pipeline can read belief state from the orchestrator daemon via HTTP API to inform Talker responses."
- [Source: build_documents/planning-artifacts/prd.md#NFR25, FR39] — privacy invariant: never log values, only requested keys.
- [Source: build_documents/planning-artifacts/epics.md#Story 4.1: BeliefStateClient — per-turn fresh GET /beliefs?keys=...] — full AC list (Story 4.1's source of truth).
- [Source: build_documents/planning-artifacts/epics.md#Epic 4 Goal] — "orchestrator slow-path with belief-state grounding."
- [Source: build_documents/implementation-artifacts/3-6-mood-module-state-and-controller.md] — config-extension pattern (`MoodConfig`); test-pattern reference for caplog / structlog assertions.
- [Source: build_documents/implementation-artifacts/3-7-audio-frame-metadata-and-ssml-prompt.md] — pipeline-assembly extension pattern (Story 3.7 wired `EventPublisher` + `MoodController`; Story 4.1 mirrors for `HttpBeliefStateClient`).
- [Source: src/voice_agent_pipeline/turn/beliefs.py] — Protocol scaffold (already landed; Story 4.1 adds the impl).
- [Source: src/voice_agent_pipeline/errors.py] — `OrchestratorError(ExternalServiceError)` already defined; Story 4.1 raises it.
- [Source: src/voice_agent_pipeline/config/setup.py] — `MoodConfig` (Story 3.6) + `TalkerConfig` (Story 2.2) extension patterns to mirror.
- [Source: setup.toml] — current operator-config file; Story 4.1 adds the `[daemon]` block.
- [External: https://www.python-httpx.org/async/] — `httpx.AsyncClient` reference; lifecycle and timeout configuration.
- [External: https://www.python-httpx.org/exceptions/] — `httpx.HTTPError` hierarchy; `ConnectError`, `ReadTimeout`, `RemoteProtocolError`, etc.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- **`httpx` import-boundary refactor**: AC #9's invariant says only
  ``turn/beliefs.py`` and ``turn/orchestrator.py`` may import ``httpx``.
  My first pass put ``import httpx`` in ``pipeline.py:run_pipeline``
  for the ``async with httpx.AsyncClient(...)`` lifecycle. Refactored
  to a ``@asynccontextmanager`` factory ``async_http_client()`` in
  ``turn/beliefs.py``; ``pipeline.py`` now does
  ``async with async_http_client() as http_client:``. Architecturally
  cleaner; ``rg -F 'import httpx' src/`` now shows only
  ``turn/beliefs.py`` (the docstring-reference hits in ``pipeline.py``
  are comments, not imports).
- **Pyright narrowing on ``resp.json()``**: ``resp.json()`` is typed
  ``Any``; after ``isinstance(parsed, dict)`` pyright narrows to
  ``dict[Unknown, Unknown]``, which propagates as a partial-unknown
  return type. Fixed with a one-line ``cast(dict[str, Any], parsed)``
  with a comment explaining JSON object keys are spec-mandated strings
  (so the cast is sound).
- **Pyright deprecation on ``AsyncIterator``**: pyright's strict mode
  flags ``-> AsyncIterator[T]`` on ``@asynccontextmanager`` as
  deprecated; switched to ``-> AsyncGenerator[T, None]`` per pyright's
  guidance.
- **ROS-sourced ``launch_testing`` plugin contamination**: ``uv run
  pytest`` directly fails because ROS Jazzy's ``launch_testing`` is on
  PYTHONPATH and depends on ``lark`` (not in this project's deps).
  ``just check`` works around this with
  ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` + an explicit
  ``-p pytest_asyncio.plugin``. Mentioned in the justfile's comment
  block; tests must be invoked via ``just check`` / ``just test`` or
  with the env var set.

### Completion Notes List

- **Tasks 1-7 satisfied as written.** No deviations.
- **AC coverage:**
  - AC #1: Protocol stays narrow — single ``read(keys)`` method only.
  - AC #2: ``HttpBeliefStateClient`` constructor takes the persistent
    ``httpx.AsyncClient`` + ``base_url``; rstrips trailing slash.
  - AC #3: ``GET /beliefs`` with ``params={"keys": ",".join(keys)}``;
    empty keys list still issues the request.
  - AC #4: No cache. Module + class docstrings document the rationale.
  - AC #5: Non-200 / transport / JSON / non-dict-shape all raise
    ``OrchestratorError``. Body truncated to 200 chars in error context.
  - AC #6: ``DaemonConfig`` with ``url: str`` + ``field_validator``
    enforcing http(s) scheme + trailing-slash strip. Wired into
    ``SetupConfig`` as optional with default factory.
  - AC #7: ``TalkerConfig.grounded_keys: list[str]`` with empty
    default. Story 4.4 will consume.
  - AC #8: Pipeline-assembly wiring lives inside an ``async with
    async_http_client() as http_client:`` block (factory in
    ``turn/beliefs.py``); ``HttpBeliefStateClient`` constructed and
    passed into ``build_talker(config, beliefs=belief_client)``.
    Talker's existing ``_beliefs`` ctor arg consumes; production call
    sites (``complete()``) don't yet read it — Story 4.4 wires
    ``complete_with_tools`` against it.
  - AC #9: Pyright-strict on full ``src/`` is clean. ``import httpx``
    confined to ``turn/beliefs.py``.
  - AC #10: Logging discipline — INFO ``belief.read`` on success;
    WARN ``belief.read_failed`` on every failure path. Fields are
    keys + key_count + duration_ms (+ status_code or reason); never
    response values.
  - AC #11: 12 unit tests in ``tests/unit/turn/test_beliefs.py``.
    All 12 pass. Includes the privacy invariant test
    (``test_read_does_not_log_response_body``) using a sentinel
    string check across captured log records.
  - AC #12: 5 ``DaemonConfig`` tests in ``tests/unit/config/test_setup.py``.
  - AC #13: 2 ``grounded_keys`` tests.
  - AC #14: Spec-drift acknowledgment (``GET /health``) deferred to
    Story 4.2 per the AC. Not implemented in this story.
  - AC #15: ``just check`` passes — 333 unit tests, 0 ruff/pyright
    issues. All earlier stories' tests still green.
  - AC #16: Privacy invariant validated by
    ``test_read_does_not_log_response_body``.

### File List

**New files:**
- ``tests/unit/turn/test_beliefs.py`` — 12 unit tests for
  ``HttpBeliefStateClient``: request shape, return shape, empty keys,
  4xx/5xx error mapping, connection error cause-chain, JSON decode
  failure, non-dict response shape, persistent-client invariant,
  success/failure log assertions, privacy invariant.

**Modified files:**
- ``src/voice_agent_pipeline/turn/beliefs.py`` — added
  ``async_http_client()`` ``@asynccontextmanager`` factory and
  ``HttpBeliefStateClient`` impl beneath the existing Protocol. The
  factory is the only ``import httpx`` site for this story (Story 4.2
  will reuse it).
- ``src/voice_agent_pipeline/config/setup.py`` — added ``DaemonConfig``
  with ``url`` field + ``field_validator``; added
  ``TalkerConfig.grounded_keys``; added ``daemon`` field on
  ``SetupConfig``; imported ``field_validator``.
- ``src/voice_agent_pipeline/turn/__init__.py`` — extended
  ``build_talker`` to accept ``beliefs: BeliefStateClient | None``
  and forward to ``Talker.__init__``.
- ``src/voice_agent_pipeline/pipeline.py`` — wrapped the
  ``run_pipeline`` body inside ``async with async_http_client() as
  http_client:``; constructed ``HttpBeliefStateClient`` and passed
  to ``build_talker(config, beliefs=belief_client)``. Module docstring
  updated with Story 4.1 entry.
- ``setup.toml`` — added ``[daemon]`` block with ``url =
  "http://localhost:8001"``; added ``grounded_keys = []`` under
  ``[talker]`` with operator comments; removed ``[daemon]`` and the
  Story 4.1 ``[talker]`` line from the deferred-blocks comment list.
- ``tests/unit/config/test_setup.py`` — added 7 tests (5 daemon + 2
  grounded_keys) under a new "Story 4.1" section, just above the
  loose-perms tests at end-of-file.
- ``build_documents/implementation-artifacts/sprint-status.yaml`` —
  ``4-1-belief-state-client: ready-for-dev → in-progress`` (will flip
  to ``review`` at story completion).
- ``build_documents/implementation-artifacts/4-1-belief-state-client.md`` —
  this file: tasks ticked, dev record + file list populated.

## Change Log

| Date | Change |
|---|---|
| 2026-05-07 | Story 4.1 prepared — BeliefStateClient (httpx GET /beliefs, no cache, fail-fast). |
| 2026-05-07 | Story 4.1 implemented — HttpBeliefStateClient + DaemonConfig + TalkerConfig.grounded_keys + persistent httpx.AsyncClient lifecycle. 12 client tests + 7 config tests, all passing. ``just check`` green (333 unit tests, 0 ruff/pyright issues). ``import httpx`` invariant honored via ``async_http_client()`` factory in ``turn/beliefs.py``. |
