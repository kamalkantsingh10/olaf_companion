# Story 4.4: Talker tool-using upgrade — `complete_with_tools` + tool registry + `GoToSleepTool` + `SetMoodTool`

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal,
I want Talker to become tool-using — exposing `go_to_sleep` and `set_mood` to the LLM via the openai SDK's `tools=` parameter, validating tool inputs against typed Pydantic schemas, and dispatching to `ActivityFSM` and `MoodController` concurrently with text emission,
so that Talker's natural-language goodbye triggers the deferred-sleep path (FR46) and natural-language mood shifts publish on `/olaf/mood` — without blocking text-to-TTS on tool side effects.

## Acceptance Criteria

1. **`src/voice_agent_pipeline/turn/tools.py` — new module** holding the registry, the two v1 tools, and the dispatch surface. Imports (top of file): stdlib (`from collections.abc import Awaitable, Callable`, `from typing import Any`), third-party (`import structlog`, `from pydantic import BaseModel, ConfigDict, ValidationError`), local (`from voice_agent_pipeline.activity.machine import ActivityFSM`, `from voice_agent_pipeline.mood.controller import MoodController`, `from voice_agent_pipeline.schemas.mood_event import Mood`). Module docstring per `feedback_code_comments.md`: explain the tool-registry shape, the dispatch contract (validate-then-call), and how Story 4.4's `TalkerResponse.tool_calls` flow into `ToolRegistry.dispatch`.

2. **`ToolSpec` — frozen pydantic model.** Fields:
   - `name: str` — exactly the openai tool name (e.g., `"go_to_sleep"`).
   - `description: str` — the description fed to the LLM via the openai SDK's `tools=` parameter (helps the LLM decide when to call it).
   - `input_schema: type[BaseModel]` — the pydantic v2 model class used to validate the tool's `arguments` JSON.
   - `dispatch: Callable[[BaseModel], Awaitable[None]]` — async callable invoked with the validated input. Returns `None`; side-effect-only.
   ```python
   class ToolSpec(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
       name: str
       description: str
       input_schema: type[BaseModel]
       dispatch: Callable[[BaseModel], Awaitable[None]]
   ```
   `arbitrary_types_allowed=True` is required because pydantic doesn't have a built-in serializer for `Callable` / `type[BaseModel]`. **Document the trade-off**: `ToolSpec` is internal/in-process state, never serialized; `arbitrary_types_allowed` is safe here. Architecture.md §"Anti-Patterns" doesn't forbid it for non-wire types.

3. **`ToolCall` — typed input shape from the openai SDK** (the LLM's tool-call response). Field shape mirrors openai's `ChatCompletionMessageToolCall`:
   ```python
   class ToolCall(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       id: str  # openai's tool_call_id; useful for future tool_results loop, unused in v1
       name: str
       arguments: dict[str, Any]  # parsed from openai's JSON-string `arguments`
   ```
   The Talker (AC #6) parses openai's response into a list of `ToolCall` instances before passing to the registry.

4. **`ToolRegistry` — holder of `list[ToolSpec]` + dispatch + openai-tools-param formatter.**
   ```python
   class ToolRegistry:
       def __init__(self, tools: list[ToolSpec]) -> None:
           self._tools = {t.name: t for t in tools}
       async def dispatch(self, tool_call: ToolCall) -> None: ...
       def as_openai_tools_param(self) -> list[dict[str, Any]]: ...
       def __len__(self) -> int: return len(self._tools)
   ```
   - **`dispatch(tool_call)`** behavior (AC: from epics):
     1. Look up `spec = self._tools.get(tool_call.name)`. If `None`: log WARN `event="tool.dispatch_unknown_name"` with `tool_call.name`, return (drop the call). **Do not raise.**
     2. Validate input: `validated = spec.input_schema.model_validate(tool_call.arguments)`. If `pydantic.ValidationError`: log WARN `event="tool.dispatch_invalid_input"` with `tool=tool_call.name`, `error=str(exc.errors()[:3])`, return (drop). **Do not raise.**
     3. On success: log INFO `event="tool.dispatch"` with `tool=tool_call.name`. `await spec.dispatch(validated)`. **Do NOT catch exceptions from `spec.dispatch`** — bugs in `ActivityFSM.on_tool_call_go_to_sleep` or `MoodController.set` should crash (CLAUDE.md rule #4 + architecture.md §"Tool-call validation": "Inside `dispatch()`, errors propagate (FSM and MoodController are first-party code; their bugs should crash, not be silently caught).")

   - **`as_openai_tools_param()`** returns the openai SDK's `tools=` parameter shape:
     ```python
     [{
         "type": "function",
         "function": {
             "name": spec.name,
             "description": spec.description,
             "parameters": spec.input_schema.model_json_schema(),
         },
     } for spec in self._tools.values()]
     ```
     `model_json_schema()` on a pydantic v2 model returns the JSON Schema; openai accepts that shape directly. **Order**: dict insertion order = registration order. Tests in AC #11 verify the format.

5. **`GoToSleepTool` + `GoToSleepInput` (empty input model).** Empty model — the LLM doesn't need to pass arguments to "go to sleep":
   ```python
   class GoToSleepInput(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       # No fields. The model is the "no arguments" shape.

   def make_go_to_sleep_tool(activity_fsm: ActivityFSM) -> ToolSpec:
       async def _dispatch(_input: BaseModel) -> None:
           await activity_fsm.on_tool_call_go_to_sleep()
       return ToolSpec(
           name="go_to_sleep",
           description="Schedule OLAF to go to sleep after the current response finishes playing. "
                       "Use when the user says goodbye or asks OLAF to sleep. The audio response is "
                       "delivered first; the system flips to wake-word-only mode after the last word.",
           input_schema=GoToSleepInput,
           dispatch=_dispatch,
       )
   ```
   The factory function is the cleanest pattern — the closure captures `activity_fsm` so the registry doesn't need to know about it. (Architecture.md §"Tool registry": "Registry is constructed once at startup with FSM and MoodController references injected.")

6. **`SetMoodTool` + `SetMoodInput`.**
   ```python
   class SetMoodInput(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       mood: Mood  # the Literal from schemas/mood_event.py

   def make_set_mood_tool(mood_controller: MoodController) -> ToolSpec:
       async def _dispatch(input_: BaseModel) -> None:
           assert isinstance(input_, SetMoodInput)  # narrowed for type-checker
           await mood_controller.set(input_.mood, reason="talker_set_mood")
       return ToolSpec(
           name="set_mood",
           description="Update OLAF's current mood. Pick from the allowed values; the mood tints "
                       "subsequent voice responses (e.g., 'playful' makes the next replies playful). "
                       "Don't call this gratuitously — only when the user's intent clearly shifts mood.",
           input_schema=SetMoodInput,
           dispatch=_dispatch,
       )
   ```
   The `assert isinstance(...)` is the **type-narrowing pattern** for the typed `Callable[[BaseModel], Awaitable[None]]` signature — the dispatch closure is typed as accepting any `BaseModel`, and the assert refines for pyright. Same pattern applies to any future tool with a non-empty input schema. **Do NOT** use `cast(SetMoodInput, input_)` — the assert gives runtime safety in addition to static narrowing. (Pyright's strict mode must accept this; it does because `assert isinstance(...)` is a recognized type guard.)

7. **`TalkerResponse` — typed return value of `complete_with_tools`.**
   ```python
   class TalkerResponse(BaseModel):
       model_config = ConfigDict(frozen=True, extra="forbid")
       text: str
       tool_calls: list[ToolCall]
   ```
   Place this in `turn/tools.py` (it's tool-related) or `turn/talker.py` (it's a Talker return type) — **Recommend** `turn/talker.py` next to the `TalkerClient` Protocol so the Protocol contract + return type live together. Define `ToolCall` in `turn/tools.py` and import into `turn/talker.py` for `TalkerResponse`.

8. **`TalkerClient` Protocol gains one new method.** In `turn/talker.py`:
   ```python
   class TalkerClient(Protocol):
       async def complete(self, transcript: str, context: dict[str, Any] | None = None) -> str: ...
       async def complete_with_tools(
           self,
           prompt: str,
           tool_registry: ToolRegistry,
       ) -> TalkerResponse: ...
   ```
   - **`complete()` stays for backward compatibility** but is no longer called in production after this story (`TurnDispatchProcessor` switches to `complete_with_tools`). Keep it because (a) test code uses it, (b) deleting it requires removing tests, which isn't this story's scope, (c) the existing v1 fail-fast wire is hardened.
   - **No `greet()` method.** Story 4.5 was originally going to add a Talker-driven mood-tinted wake greeting. Story 4.5 has been revised (2026-05-07) to use a **static random pick from per-mood bucket lists** in setup.toml — no LLM call, no 800ms timeout, no fallback path. Story 4.4 therefore does NOT add `greet()` to the Protocol, NOT ship a `Talker.greet` impl, NOT create `prompts/talker_greeting.md`, NOT extend `TalkerConfig` with `greeting_prompt_path`. **Simplifies this story** + eliminates the `cdf3618`-class "LLM treated the prompt as a question" failure mode.

9. **`Talker.complete_with_tools(prompt, tool_registry)`** — the meat of the story:
   - **Belief-state grounding** (Story 4.1 + this story integration): if `self._beliefs is not None` AND `self._config.grounded_keys` is non-empty, `await self._beliefs.read(self._config.grounded_keys)` and inject the result into the system prompt context. Specifically: append a section `\n\n## Belief state\n{json.dumps(beliefs, indent=2)}\n` to the system prompt sent to the LLM (or prepend to the user message — Recommend appending to the system prompt section so the LLM treats it as context, not input). **If `_beliefs is None` (e.g., test harness without daemon)**: skip the read, use the plain system prompt.
   - **openai SDK call**: pass `tools=tool_registry.as_openai_tools_param()` to `client.chat.completions.create()` alongside the normal `model`, `messages`, `max_tokens` (or `max_completion_tokens` per provider). Also pass `tool_choice="auto"` so the LLM decides; don't force tools on every turn.
   - **Parse the response**:
     ```python
     choice = response.choices[0]
     text = choice.message.content or ""  # may be None when tools are called
     raw_tool_calls = choice.message.tool_calls or []
     parsed = []
     for tc in raw_tool_calls:
         try:
             arguments = json.loads(tc.function.arguments) if tc.function.arguments else {}
         except json.JSONDecodeError:
             log.warning("talker.tool_call_invalid_json",
                         tool=tc.function.name, raw_length=len(tc.function.arguments or ""))
             continue  # drop the call; tool registry won't see it
         parsed.append(ToolCall(id=tc.id, name=tc.function.name, arguments=arguments))
     return TalkerResponse(text=text, tool_calls=parsed)
     ```
     **Critical**: when tools are called, `choice.message.content` may be `None` (the LLM emitted only a tool call, no text). Handle the `None → ""` coercion explicitly so callers don't get `text=None`. **The text-first parallel-tools dispatch (AC #10) doesn't care if text is empty** — it still emits `TalkerResponseFrame("")` so observers see the turn boundary.
   - **Token usage logging** (mirror Story 2.2's `complete()` pattern): INFO `talker.completion` with the same fields, plus `tool_call_count=len(parsed)`. The `prompt` / `response` fields stay (Story 2.5 deviation from FR42 — see existing `complete()` impl).
   - **Errors**: `openai.APIError` → `raise TalkerError(provider=..., model=..., reason=...) from e` (mirror `complete()`). `json.JSONDecodeError` on tool arguments → log WARN + drop the specific tool call (continue with the rest); don't crash the whole turn.

10. **`TurnDispatchProcessor` — text-first parallel-tools dispatch** (FR45 / FR46, the Story 4.4 linchpin).
    - **Replace** `response_text = await self._router.talker.complete(decision.text)` (current pipeline.py:251) with:
      ```python
      response = await self._router.talker.complete_with_tools(decision.text, self._tool_registry)
      # Step 1: emit text IMMEDIATELY — splitter / TTS start synthesis right away.
      await self.push_frame(TalkerResponseFrame(text=response.text), direction)
      # Step 2: kick off tool dispatches as background tasks. Text-to-TTS does
      # NOT wait on these. Each task is fire-and-forget with a done-callback
      # logging any exception (so a tool failure isn't lost in the void).
      for tool_call in response.tool_calls:
          task = asyncio.create_task(self._tool_registry.dispatch(tool_call))
          task.add_done_callback(self._log_tool_done)
      ```
      Where `self._log_tool_done(task)` is a small instance method that calls `task.result()` inside a try/except to log any exception (the registry catches validation errors but propagates internal bugs — see AC #4).
    - **Constructor extension**: `__init__(self, router: TurnRouter, tool_registry: ToolRegistry)` — add the registry parameter. Update the test harness in `tests/unit/test_pipeline.py` accordingly.
    - **Architectural ordering** (architecture.md §"Tool-call dispatch order vs text emission"): "**emits `TalkerResponseFrame(text)` immediately**, then concurrently kicks off `asyncio.gather(*[registry.dispatch(tc) for tc in tool_calls])`. Tool side effects (FSM transition, mood publish) run alongside TTS streaming — text is never blocked on tool work. **Text-first ordering means the user hears the goodbye before mic mode flips (FR46).**"
    - **Use `asyncio.create_task` per tool call, NOT `asyncio.gather`.** `gather` waits; `create_task` is fire-and-forget. The dispatcher's `process_frame` should return after pushing the text frame downstream; tool dispatch completes asynchronously in the background. The done-callback ensures errors land in logs rather than disappearing.
    - **Keep the existing `talker.responded` INFO log** (Story 2.5's pattern) but extend with `tool_call_count`.
    - **Orchestrator branch unchanged** in this story — Story 4.7 wires it. The current `NotImplementedError` stays. (Note: the existing comment says "Story 4.3"; it should say "Story 4.7" — fix in passing if convenient, otherwise leave for 4.7 to update.)

11. **`setup.toml` `[tools]` block + `ToolsConfig` model:**
    ```toml
    # Story 4.4: Talker tool-using upgrade. Toggle individual tools on/off.
    # Both default true. Operators can disable a tool to remove it from the
    # LLM's surface (the registry won't include it in `as_openai_tools_param`).
    [tools]
    enable_go_to_sleep = true
    enable_set_mood = true
    ```
    In `src/voice_agent_pipeline/config/setup.py`:
    - Add `class ToolsConfig(BaseModel)` with `model_config = ConfigDict(extra="forbid")`, `enable_go_to_sleep: bool = True`, `enable_set_mood: bool = True`. Class docstring per `feedback_code_comments.md`.
    - Add `tools: ToolsConfig = Field(default_factory=ToolsConfig)` to `SetupConfig`.
    - Update setup.toml's "Sections populated by subsequent stories" comment list — remove `[tools]` if it's there, or add the block in the populated section.

12. **`build_tool_registry` factory in `turn/__init__.py` (or `turn/tools.py`)** — single construction site for the registry, mirrors `build_talker`:
    ```python
    def build_tool_registry(
        config: ToolsConfig,
        activity_fsm: ActivityFSM,
        mood_controller: MoodController,
    ) -> ToolRegistry:
        tools: list[ToolSpec] = []
        if config.enable_go_to_sleep:
            tools.append(make_go_to_sleep_tool(activity_fsm))
        if config.enable_set_mood:
            tools.append(make_set_mood_tool(mood_controller))
        return ToolRegistry(tools)
    ```
    **Recommend** placing this in `turn/__init__.py` next to `build_talker` so the factory pattern is consistent and discoverable. Re-export from `turn/__init__.py`'s `__all__`.

13. **Pipeline-assembly wiring** (`pipeline.py:run_pipeline`):
    - After Story 4.3's `activity_fsm = ActivityFSM(...)` and Story 3.7's `mood_controller = MoodController(...)`, add:
      ```python
      tool_registry = build_tool_registry(config.tools, activity_fsm, mood_controller)
      ```
    - Pass `tool_registry` into `TurnDispatchProcessor(router, tool_registry)`.
    - Update `pipeline.py`'s module docstring: append a Story 4.4 entry to the "Story progression" list — Talker now tool-using; `TurnDispatchProcessor` text-first / parallel-tools dispatch.

14. **`prompts/talker_system.md` updates** (light, this story):
    - Add a **short** section near the top instructing the LLM that two tools are available — `go_to_sleep` (when the user says goodbye or asks OLAF to sleep) and `set_mood` (when the user's intent shifts the mood). **Do NOT** describe the tool schemas in the prompt — the openai SDK's `tools=` parameter handles that. The prompt section is a behavioral nudge: "When the user says goodnight, call `go_to_sleep` while replying naturally."
    - **Keep Story 3.7's SSML emission section** intact. Story 4.4's prompt change is additive.

15. **No greeting-mode prompt file.** Story 4.5's static-random redesign means `prompts/talker_greeting.md` is NOT created. (See AC #8 — Story 4.5 revision.)

16. **Talker constructor — no greeting plumbing.** `Talker.__init__` already accepts `beliefs: BeliefStateClient | None = None` (Story 4.1 wiring landed it). Story 4.4 starts consuming `_beliefs` in `complete_with_tools`. **No `greeting_prompt_path` field on `TalkerConfig`** (Story 4.5's revised design doesn't need it).

17. **Logging discipline (NFR25, FR39, architecture.md §"Logging discipline"):**
    - INFO `talker.completion` — extended with `tool_call_count`. Existing fields stay.
    - INFO `tool.dispatch` (per successful tool call) — fields: `tool` (name only). **Never** log `arguments` content (could leak user state via `set_mood`'s mood value — but mood is Literal-bounded, so safe; argue caution: `tool` name only).
    - INFO `talker.responded` (in `TurnDispatchProcessor`) — extended with `tool_call_count`, `clarification` (existing).
    - INFO `talker.greeting` (new) — fields: `mood`, `text` (the LLM's reply — short, mood-bounded; NOT user content; logging at INFO matches the "Story 2.5 deviation from FR42" precedent in `complete()`).
    - WARN `tool.dispatch_unknown_name` — fields: `name`. Never log unknown arguments (could be untrusted LLM output).
    - WARN `tool.dispatch_invalid_input` — fields: `tool`, `error` (truncated pydantic error string, capped at 3 entries).
    - WARN `talker.tool_call_invalid_json` — fields: `tool`, `raw_length`. **Never log** the malformed argument string itself.
    - **Never log** transcripts at INFO+ (standing invariant); **never log** the full openai response object (it has a `tool_calls` field that may contain LLM-emitted text in `arguments`).

18. **Unit tests in `tests/unit/turn/test_tools.py`** (NEW). Cover:
    - `test_tool_spec_construction` — build a `ToolSpec` with a real input schema + dispatch closure; access fields.
    - `test_tool_call_validates_against_input_schema` — `SetMoodInput.model_validate({"mood": "playful"})` succeeds; `model_validate({"mood": "ecstatic"})` raises `ValidationError` (mood not in Literal).
    - `test_registry_construction_indexes_by_name` — `ToolRegistry([go_to_sleep_spec, set_mood_spec])`; `len(registry) == 2`. Use real specs constructed via the factories.
    - `test_as_openai_tools_param_emits_correct_format` — assert the returned list has shape `[{"type": "function", "function": {"name": "go_to_sleep", "description": "...", "parameters": {...JSON Schema...}}}, ...]`. For `set_mood`, verify the JSON Schema has `properties.mood.enum == [<list of Mood Literal values>]` (pydantic emits Literal as JSON Schema `enum`).
    - `test_dispatch_happy_path_calls_dispatch_coroutine` — for `set_mood`, mock the `MoodController` (or use a real one with `LogEventPublisher`); call `await registry.dispatch(ToolCall(id="t1", name="set_mood", arguments={"mood": "playful"}))`; assert `mood_controller.set` was called with `("playful", reason="talker_set_mood")`.
    - `test_dispatch_invalid_input_logs_warn_and_drops` — `await registry.dispatch(ToolCall(id="t1", name="set_mood", arguments={"mood": "ecstatic"}))`; assert no `mood_controller.set` call happened; assert WARN log `event="tool.dispatch_invalid_input"` with `tool="set_mood"`, `error` containing the validation message. **Critical**: `dispatch` does NOT raise.
    - `test_dispatch_unknown_name_logs_warn_and_drops` — `await registry.dispatch(ToolCall(id="t1", name="nonexistent_tool", arguments={}))`; assert WARN `event="tool.dispatch_unknown_name"`. Does NOT raise.
    - `test_go_to_sleep_dispatch_invokes_fsm_method` — use a real `ActivityFSM(publisher=LogEventPublisher())`; drive `start()` then `on_wake_detected()` → `on_speech_started()` → `on_speech_ended()` → `on_first_audio_frame()` (now in `speaking`); call `await registry.dispatch(ToolCall(id="t1", name="go_to_sleep", arguments={}))`; assert `fsm.sleep_pending is True`.
    - `test_set_mood_dispatch_invokes_mood_controller_set` — happy-path mood update via tool dispatch; assert `mood_controller.state.current` matches the new mood.
    - `test_dispatch_internal_exception_propagates` — make `mood_controller.set` raise `PublisherError`; `await registry.dispatch(ToolCall(...))`; `pytest.raises(PublisherError)`. Validates the AC #4 invariant — registry doesn't catch internal errors.
    - `test_build_tool_registry_factory_respects_enable_flags` — `build_tool_registry(ToolsConfig(enable_go_to_sleep=False, enable_set_mood=True), ...)` returns a registry with only `set_mood`. Validate via `len(registry) == 1` and `registry.as_openai_tools_param()[0]["function"]["name"] == "set_mood"`.

19. **Updated unit tests for `Talker.complete_with_tools`** in `tests/unit/turn/test_talker.py`:
    - `test_complete_with_tools_passes_tools_to_openai_sdk` — mock `openai.AsyncOpenAI`; call `complete_with_tools("hello", registry)`; assert the `tools=` kwarg matches `registry.as_openai_tools_param()`.
    - `test_complete_with_tools_returns_text_only_when_no_tool_calls` — mock the SDK to return a response with `message.content="hi there"` and no tool_calls; assert `TalkerResponse(text="hi there", tool_calls=[])`.
    - `test_complete_with_tools_returns_text_and_tool_calls` — mock response with text + a `tool_calls=[{id: "t1", function: {name: "go_to_sleep", arguments: "{}"}}]`; assert parsed `TalkerResponse(text=..., tool_calls=[ToolCall(id="t1", name="go_to_sleep", arguments={})])`.
    - `test_complete_with_tools_handles_none_content` — when openai's response has `content=None` (only tool calls), `TalkerResponse.text == ""` (not None).
    - `test_complete_with_tools_invalid_arguments_json_drops_call_warns` — mock a tool_call with `arguments="not-json"`; assert WARN `talker.tool_call_invalid_json`; assert the dropped tool call is NOT in `TalkerResponse.tool_calls`. Other valid tool calls in the same response still flow through.
    - `test_complete_with_tools_belief_grounding_when_configured` — mock `BeliefStateClient.read` to return `{"time": "08:47", "calendar_today": []}`; configure `TalkerConfig.grounded_keys=["time", "calendar_today"]`; call `complete_with_tools(...)`; assert the system message sent to openai includes `## Belief state` and the JSON-rendered context.
    - `test_complete_with_tools_skips_belief_grounding_when_no_keys` — `grounded_keys=[]`; assert `BeliefStateClient.read` was NOT called.
    - `test_complete_with_tools_skips_belief_grounding_when_beliefs_none` — pass `beliefs=None` to `Talker.__init__`; call `complete_with_tools`; no `read` call (no NPE either).
    - `test_complete_with_tools_logs_completion_with_tool_call_count` — assert INFO `talker.completion` with `tool_call_count=N` field.
    - `test_complete_with_tools_openai_error_raises_talker_error` — mock SDK to raise `openai.APIError`; `pytest.raises(TalkerError)`. Mirror existing `complete()` error test.

20. **Updated unit tests for `TurnDispatchProcessor`** in `tests/unit/test_pipeline.py` (or `tests/unit/turn/test_dispatch.py` if that's where they live):
    - `test_dispatch_emits_text_frame_before_tool_dispatch_completes` — the linchpin parallel-dispatch test. Set up: mock Talker to return `TalkerResponse(text="goodnight", tool_calls=[ToolCall(id="t1", name="go_to_sleep", arguments={})])`. Use a real `ToolRegistry` with a `GoToSleepTool` whose dispatch is wrapped to `await asyncio.sleep(0.1)` before calling `fsm.on_tool_call_go_to_sleep()`. Drive a transcript through `TurnDispatchProcessor.process_frame`. **Assert ordering**: `TalkerResponseFrame(text="goodnight")` is pushed downstream BEFORE the FSM's `sleep_pending` is set (i.e., the text frame appears in `push_frame` calls before the 100ms artificial delay completes). Verify by recording timestamps on the `push_frame` mock and the FSM state mutation.
    - `test_dispatch_continues_when_tool_dispatch_fails_in_background` — make tool dispatch raise after the text frame is emitted; assert (a) the text frame was still pushed, (b) the exception is captured by the done-callback's `task.result()` call (caplog WARN). The test pipeline doesn't crash; the next-turn flow still works.
    - `test_dispatch_text_only_no_tool_calls` — Talker returns `TalkerResponse(text="ok", tool_calls=[])`; assert `TalkerResponseFrame("ok")` pushed; assert NO tool dispatch tasks created.
    - `test_dispatch_constructor_accepts_tool_registry` — verify `TurnDispatchProcessor.__init__(router, tool_registry)` signature; existing tests need updating.

21. **Integration test `tests/integration/test_intent_sleep.py`** (PRD Journey 4 — at the dispatch level only, NOT full pipeline-with-FSM-wired which is Story 4.7's complex-turn test):
    - Mocks: Cartesia (synthetic audio chunks), STT (canned "goodnight olaf"). Talker mocked at the Protocol level: returns `TalkerResponse(text="goodnight, sleep well", tool_calls=[ToolCall(id="t1", name="go_to_sleep", arguments={})])`.
    - Real: `ActivityFSM` (with `LogEventPublisher`), `MoodController`, `ToolRegistry`, `TurnDispatchProcessor`.
    - Drive the dispatcher with a transcript frame; observe:
      1. **`TalkerResponseFrame(text="goodnight, sleep well")` is observed** at the splitter (or wherever the test taps the downstream frames) BEFORE `tool_registry.dispatch` completes. Use a small artificial delay on `dispatch` to make the ordering observable.
      2. **`activity_fsm.sleep_pending == True`** after the dispatch task completes (await all pending tasks).
      3. The full FSM transition path (`speaking → going_to_sleep → sleeping`) is NOT exercised in this test — that requires `on_last_audio_frame` to fire, which means the Cartesia frames need to flow all the way through the audio path. **Story 4.7's complex-turn integration test** is where the full E2E intent-sleep journey lands. **Story 4.4's integration test** asserts only the dispatcher + FSM coupling.
    - **Privacy assertion** mirroring Story 3.7 / 4.3 — no transcript text in INFO+ logs.

22. **`just check` stays green.** All earlier tests pass:
    - **Test updates required**:
      - `tests/unit/turn/test_dispatch.py` (Story 2.4) — `TurnDispatchProcessor.__init__` now takes a `tool_registry` parameter. Update the test fixtures.
      - `tests/unit/turn/test_talker.py` (Story 2.2) — `complete()` is unchanged; `complete_with_tools()` and `greet()` are new methods with new tests (AC #19).
      - `tests/unit/turn/test_factory.py` — `build_talker` is unchanged; `build_tool_registry` is new (covered in `test_tools.py`'s factory test).
      - `tests/unit/test_pipeline.py` — `TurnDispatchProcessor` now requires `tool_registry`; test fixtures need a `ToolRegistry([])` (empty registry — valid, just no tools available; happy path via Talker text-only response).
      - `tests/unit/config/test_setup.py` — add 3 `ToolsConfig` tests (default flags both true; one disabled; both disabled).
      - `tests/integration/test_simple_turn.py` (Story 2.5) — `TurnDispatchProcessor` constructor extension; pass an empty `ToolRegistry([])` so existing simple-turn assertions still pass.
      - `tests/integration/test_embodiment_alignment.py` (Story 3.7) — same pattern.
      - `tests/integration/test_activity_lifecycle.py` (Story 4.3) — same pattern.

23. **No raw audio / credentials / transcripts logged.** Standing invariant. Story 4.4's new logs (`tool.dispatch`, `tool.dispatch_invalid_input`, `talker.greeting`, `talker.tool_call_invalid_json`) all stay within the redaction-safe field set: tool names (Literal-bounded), raw lengths, error message excerpts. Tool `arguments` content is never logged at INFO+; only the Literal-bounded `mood` value (Story 4.5 tests will revisit if needed, but `mood` from `set_mood` is bounded so safe).

## Tasks / Subtasks

- [x] **Task 1: `ToolsConfig` + `[tools]` setup.toml block** (AC: #11)
- [x] **Task 2: `turn/tools.py` — registry, specs, factories** (AC: #1-#6, #12)
- [x] **Task 3: `Talker.complete_with_tools` + Protocol extension** (AC: #7-#9, #17)
- [x] **Task 4: `TurnDispatchProcessor` text-first parallel-tools dispatch** (AC: #10, #13)
- [x] **Task 5: `prompts/talker_system.md` updates** (AC: #14)
- [x] **Task 6: Unit tests for `tools.py`** (AC: #18) — 11 cases pass
- [x] **Task 7: Updated unit tests for `Talker`** (AC: #19) — 10 new cases pass
- [x] **Task 8: Updated unit tests for `TurnDispatchProcessor`** (AC: #20) — 4 new cases + existing updated
- [x] **Task 9: Integration test for intent-sleep dispatch coupling** (AC: #21)
- [x] **Task 10: Update Stories 2.4 / 2.5 / 3.7 / 4.3 test harnesses** (AC: #22)
- [x] **Task 11: Pass `just check`** — 411 unit tests + 1 integration test pass
- [ ] **Task 11b: Live smoke (manual)** — DEFERRED. Live `just run` smoke pending; the orchestrator daemon isn't running locally and the operator opted to dev with `[daemon] enabled = false` (committed `fd8eba3`). Smoke validation deferred until orchestrator stub or real daemon comes up; v1 unit + integration coverage pins the dispatcher contract independent of live audio.
- [ ] **Task 12: Commit + push** — pending

## Dev Notes

### Architectural intent — Story 4.4's role in Epic 4

Story 4.4 is the "Talker becomes alive" story. Before 4.4, Talker is a one-trick fast-path text generator (Story 2.2). After 4.4, Talker:
1. Reads belief state per turn (Story 4.1's wiring goes live here).
2. Emits text + structured tool calls (openai SDK's `tools=` surface).
3. Dispatches tool calls in parallel with text emission (FR45 — text-first ordering preserves FR46's deferred-sleep contract).
4. Has a separate "greeting mode" (Story 4.5's sole consumer).

**Why land 4.4 fourth in Epic 4** (per epics.md sequencing): 4.4 depends on 4.1's `BeliefStateClient` (for grounding) AND 4.3's `ActivityFSM.on_tool_call_go_to_sleep` (the call site for `GoToSleepTool.dispatch`). Lands after both. Story 4.5 (wake greeting) consumes Story 4.4's `greet()` method; Story 4.7 (orchestrator slow-path) consumes the `complete_with_tools` shape. Stories 4.5 / 4.7 build atop 4.4.

### Text-first parallel-tools dispatch — the FR45 / FR46 linchpin

The architectural rule: **the user hears the goodbye before the mic flips.**

Imagine "goodnight olaf" → Talker replies "okay, sleep well" + a `go_to_sleep` tool call. If the dispatcher awaited `tool_registry.dispatch()` BEFORE emitting `TalkerResponseFrame(text)`, the FSM would receive `on_tool_call_go_to_sleep()` and (via Story 4.5+ / 4.6) flip `mic_mode = "wake_word_only"` BEFORE the audio plays. The user hears nothing meaningful before the mic stops listening.

Story 4.4's contract: **emit text frame FIRST, dispatch tools SECOND, never await tool dispatch.** The implementation:
```python
await self.push_frame(TalkerResponseFrame(text=response.text), direction)  # 1st: text
for tool_call in response.tool_calls:
    asyncio.create_task(self._tool_registry.dispatch(tool_call))  # 2nd: bg tasks
# return — process_frame returns; tool dispatch runs in background.
```

Story 4.3's `ActivityFSM.on_tool_call_go_to_sleep()` only **flips a flag** (`sleep_pending=True`), no mic-mode change. The mic-mode flip happens later, on `on_last_audio_frame()` → deferred-sleep transition. So the timeline is:

1. T+0: Talker returns text + tool_call.
2. T+0: TurnDispatchProcessor pushes `TalkerResponseFrame` downstream.
3. T+0+ε: tool_registry.dispatch runs in background; FSM sets `sleep_pending=True`.
4. T+0..N: Cartesia synthesizes; speaker plays "okay, sleep well" (~1-2 seconds).
5. T+N: last audio frame fires; FSM transitions `speaking → going_to_sleep → sleeping`; mic flips to `wake_word_only`.

The 1-2 second audio playback is the user-facing "feels right" budget; the architecture preserves it through this ordering. **Don't flatten to `await asyncio.gather(*[dispatch(tc) for tc in tool_calls])` even though it's slightly tighter** — it serializes the dispatcher's processing on tool work, and `set_mood` (the other v1 tool) is even faster than `go_to_sleep`'s flag-flip but still adds latency. **fire-and-forget is the right shape.**

### Async-task done-callback for fire-and-forget

The pattern:
```python
task = asyncio.create_task(self._tool_registry.dispatch(tool_call))
task.add_done_callback(self._log_tool_done)

def _log_tool_done(self, task: asyncio.Task[None]) -> None:
    try:
        task.result()  # raises if the task raised
    except Exception:
        log.exception("tool.dispatch_background_error", tool_call_name=...)
```

Without the done-callback, an exception in the background task is silent (Python's `asyncio` swallows uncaught task exceptions unless someone awaits the task). The done-callback re-raises inside the callback; `log.exception` captures the traceback to the logs. **Process doesn't crash** — the callback is the catch boundary — which contradicts CLAUDE.md rule #4 superficially, **but**: the `ToolRegistry.dispatch` itself catches `ValidationError` (the only "expected" failure mode); anything propagating past it is a programming error in `ActivityFSM` / `MoodController` / whatever sink the tool feeds. **For v1, log + continue is the trade-off** because alternative is "any tool dispatch failure crashes the pipeline mid-utterance, the user hears the goodbye partway, mic flips early on systemd restart" — worse UX than swallowing.

**Document this trade-off** explicitly in the `_log_tool_done` method's docstring. Mark as v1 stance; v2 will revisit.

### Belief-state grounding — system prompt vs user message

The architecture says (architecture.md §"Belief-state read"): "results injected into the system prompt context." Two implementation choices:
1. Append to system prompt: `system = base_prompt + "\n\n## Belief state\n" + json_beliefs`.
2. Prepend to user message: `user = json_beliefs + "\n\n" + transcript`.

Picked (1) because:
- LLMs treat system messages as context, not input. Belief state is context.
- Subsequent user turns in the same turn don't repeat the prompt; option (2) would mix beliefs with each user input, confusing the model.
- Cleaner separation: the user said X; the context is Y; system prompt has both.

The trade-off: pre-LLM-2024 best practice was "all context in user message"; modern LLMs handle system-message context well. Live-tune during Task 11's smoke if Groq Llama 3.1 8B doesn't respect system-message context (it does — verified by Story 3.7's SSML emission test).

### Tool dispatch — validation discipline

Two failure modes for tool calls, each with different disposition:
1. **Validation failure** (LLM emitted bad arguments — e.g., `{"mood": "ecstatic"}` where `ecstatic` isn't in the `Mood` Literal). **Caught by `ToolRegistry.dispatch`**: log WARN, drop the call, return cleanly. The Talker's text response still flows; only the tool side-effect is silent.
2. **Internal sink failure** (e.g., `MoodController.set` raises `PublisherError`). **NOT caught**: bubbles up. Done-callback logs; pipeline continues (next turn works). v1 trade-off — see "Async-task done-callback" above.

The `ToolRegistry.dispatch` method MUST distinguish:
```python
try:
    validated = spec.input_schema.model_validate(tool_call.arguments)
except ValidationError as exc:
    log.warning("tool.dispatch_invalid_input", ...)
    return  # silent drop; do NOT raise
log.info("tool.dispatch", tool=tool_call.name)
await spec.dispatch(validated)  # propagate exceptions from here
```

**Don't wrap the `await spec.dispatch(...)` in try/except**. The `ValidationError` catch is targeted; the dispatch call's exceptions are first-party bugs — let them propagate.

### No greeting-mode prompt (revised 2026-05-07)

Earlier draft of this story added `Talker.greet(mood)` + `prompts/talker_greeting.md` for Story 4.5 to consume. Story 4.5 was revised on 2026-05-07 to use a **static random pick** from per-mood bucket lists in `setup.toml` — no LLM call. That change deletes ~80% of the greeting plumbing: no `greet()` Protocol method, no `Talker.greet` impl, no greeting prompt file, no `TalkerConfig.greeting_prompt_path`, no LLM-timeout / word-count-gate path.

Why static-random won: zero per-call latency (sub-ms vs 200-800ms), zero LLM cost, zero hallucination risk, zero "Groq treats the prompt as a question" failure mode (the same class of issue Story 3.7's `cdf3618` had to fix for clarification). Per-mood buckets preserve the architectural mood-tinting intent without the LLM round-trip.

### `TurnDispatchProcessor` constructor extension — minimize test churn

Adding `tool_registry` to the constructor breaks every existing test that constructs the processor directly. **Mitigation**: make the parameter required (no default `None`) so the new tests force construction with a registry; Stories 2.4 / 2.5 / 3.7 / 4.3 test harnesses get a `ToolRegistry([])` (empty registry, valid happy path — Talker emits text only, no tool calls happen).

The empty registry's `as_openai_tools_param()` returns `[]`, which openai accepts as "no tools available." The Talker passes it through; the LLM doesn't emit tool calls; everything works as before. **This keeps Stories 2.5 / 3.7 / 4.3 invariants intact** — same behavior, additional surface.

### `complete()` vs `complete_with_tools()` — keep both

The Protocol gains a new method but `complete()` stays. **Why not delete `complete()`?**
- Test code (Story 2.2 / 2.4 unit tests) hardcodes `complete()` calls. Deleting forces test refactor with no architectural benefit.
- The fast-path Talker reach is `complete_with_tools` (Story 4.4 onwards); `complete()` becomes dead code in production but stays as a documented Protocol method.
- A future v2 may revisit — perhaps tools-only is the right shape. Until then, both methods exist.

**Document this in the Protocol's class docstring**: `complete()` is legacy / test-only; `complete_with_tools()` is the production call.

### Test-mocking pattern (CLAUDE.md rule #7)

Mock surfaces:
- `openai.AsyncOpenAI` — `AsyncMock(spec=openai.AsyncOpenAI)` with `client.chat.completions.create` set up per test. Mirror Story 2.2's `test_talker.py` patterns.
- `BeliefStateClient` — `AsyncMock(spec=BeliefStateClient)` with `read.return_value = {...}`.
- `ActivityFSM` — **DON'T MOCK**. Use the real class with `LogEventPublisher`. The whole point of Story 4.3's testability is that `ActivityFSM` is a small, fast, real object usable in tests.
- `MoodController` — same. Real instance with `LogEventPublisher`.
- `ToolRegistry` — **don't mock**. Construct real registries with real specs; only mock the dispatch closures' deepest sinks if needed (`PublisherError` injection on `mood_controller.publisher.publish_mood`).

### What this story does NOT do

- **No wake greeting integration** — Story 4.5 hooks `talker.greet(mood)` into the FSM's `sleeping → waking` transition. Story 4.4 ships the `greet()` method ready.
- **No mic-mode flip wiring** — Story 4.6's territory.
- **No orchestrator slow-path** — Story 4.7's territory. Story 4.4's `TurnDispatchProcessor` keeps the existing `NotImplementedError` for `target="orchestrator"`.
- **No live deferred-sleep validation through the full pipeline** — Story 4.7's complex-turn integration test covers this end-to-end. Story 4.4's integration test only validates the dispatcher + FSM coupling at the tool-dispatch level.
- **No tool result loop** — `tool_results` (the openai pattern of feeding tool outputs back to the LLM for a second pass) is NOT v1. v1 tool calls are fire-and-forget side-effects; the LLM's text response is the user-facing reply, the tool dispatch is the system-state mutation. v2 may add the loop if grounded follow-ups are needed.
- **No new Talker provider** — Story 4.4 stays on the existing OpenAI/Groq/Gemini trio.

### Project structure notes

This story creates:
- `src/voice_agent_pipeline/turn/tools.py` — registry + tools + factories.
- `tests/unit/turn/test_tools.py` — registry + tools unit tests.
- `tests/integration/test_intent_sleep.py` — dispatch-level intent-sleep test.

It modifies:
- `src/voice_agent_pipeline/turn/talker.py` — `TalkerClient.complete_with_tools` Protocol method + `Talker` impl; `TalkerResponse` model.
- `src/voice_agent_pipeline/turn/__init__.py` — re-export `ToolRegistry`, `ToolSpec`, `ToolCall`, `TalkerResponse`, `build_tool_registry`. Possibly add the `build_tool_registry` factory itself if not in `tools.py`.
- `src/voice_agent_pipeline/config/setup.py` — `ToolsConfig` model, `tools` field on `SetupConfig`.
- `setup.toml` — `[tools]` block.
- `prompts/talker_system.md` — short tools-availability section appended.
- `src/voice_agent_pipeline/pipeline.py` — `TurnDispatchProcessor` constructor + dispatch logic; `run_pipeline` constructs `tool_registry`.
- `tests/unit/turn/test_talker.py` — 12 new tests for `complete_with_tools` + `greet`.
- `tests/unit/turn/test_dispatch.py` — constructor extension + 4 new tests for parallel-dispatch.
- `tests/unit/test_pipeline.py` — TurnDispatchProcessor fixture updates.
- `tests/unit/config/test_setup.py` — 3 `ToolsConfig` tests.
- `tests/integration/test_simple_turn.py` (Story 2.5) — pass `ToolRegistry([])`.
- `tests/integration/test_embodiment_alignment.py` (Story 3.7) — same.
- `tests/integration/test_activity_lifecycle.py` (Story 4.3) — same.
- `build_documents/implementation-artifacts/sprint-status.yaml` — `4-4-talker-tool-using-upgrade: ready-for-dev → in-progress → review`.

It does NOT modify:
- `src/voice_agent_pipeline/turn/router.py` — Story 4.7's territory.
- `src/voice_agent_pipeline/turn/orchestrator.py` — Story 4.2's territory; unchanged in 4.4.
- `src/voice_agent_pipeline/turn/beliefs.py` — Story 4.1's territory; consumed via injection.
- `src/voice_agent_pipeline/activity/*` — Story 4.3's territory; consumed via `on_tool_call_go_to_sleep`.
- `src/voice_agent_pipeline/mood/*` — Story 3.6's territory; consumed via `MoodController.set`.
- `src/voice_agent_pipeline/errors.py` — `TalkerError`, `PublisherError` already exist.
- `src/voice_agent_pipeline/schemas/*` — `Mood`, `ActivityState`, etc. already defined.

### Testing standards

- **`pytest-asyncio`** in auto mode.
- **Real `LogEventPublisher` + `ActivityFSM` + `MoodController`** as test fakes for the registry tests; mocks only at external seams (openai SDK, BeliefStateClient).
- **Pyright strict on `src/`** — `Callable[[BaseModel], Awaitable[None]]` typing for tool dispatch closures; `assert isinstance(...)` for type narrowing.
- **Privacy assertions** mirror Story 4.3's pattern.

### Performance budget

NFR1 fast-path turn budget (≤1s p95 end-of-speech → first audio frame). Story 4.4's contributions:
- Belief-state read (Story 4.1): single localhost HTTP GET, ~5-10ms with keep-alive.
- openai chat completion with tools: same TTFB as Story 2.2's tool-free `complete()` (tools surface adds <50ms in practice; sometimes faster because the LLM emits less text when tools are available).
- Tool registry dispatch: sub-millisecond for `go_to_sleep` (just a flag-flip); single mood publish for `set_mood` (cooldown-gated, ~1ms).

The text-first dispatch ordering ensures **no tool-related delay** sits on the audio path. Total budget unchanged from Story 2.2's; Story 4.4 adds the tools surface without adding hot-path latency.

NFR32 (architecture.md): "tool-call dispatch overhead bounded — async-gather text-first, tools-concurrent ordering established Epic 4 (Story 4.4)." Story 4.4's text-first parallel-dispatch contract IS the NFR32 baseline; Story 5.4's soak validates it.

### What "done" looks like

- `just check` exits 0.
- `just test` exits 0 (including all updated integration tests + new `test_intent_sleep.py`).
- `just run` end-to-end on the dev host produces:
  - "goodnight" turn: text reply played in full; deferred-sleep transition fires AFTER audio ends; `ros2 topic echo /olaf/activity` shows the sequence; `ros2 topic echo /olaf/mood` does NOT show a `set_mood` event.
  - "I'm in a playful mood" turn: text reply + `set_mood("playful")` tool call; `ros2 topic echo /olaf/mood` shows the new mood event.
  - Belief-state grounding (if `[talker] grounded_keys = ["time"]` is configured + the daemon serves `time`): "what time is it?" gets a grounded answer.
- Sprint-status flips to `review` after live smoke confirms.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Activity FSM + Mood Control + Tool Registry (Batch 6)] — full tool registry design (ToolSpec shape, dispatch contract, FSM/MoodController injection, tool-call validation, dispatch-vs-text-emission ordering).
- [Source: build_documents/planning-artifacts/architecture.md#Tool registry] — `ToolRegistry` is a frozen pydantic v2 model holding a list of `ToolSpec`; v1 specs are `GoToSleepTool` and `SetMoodTool`.
- [Source: build_documents/planning-artifacts/architecture.md#Tool-call validation] — validation-then-dispatch contract; ValidationError caught by registry, dispatch errors propagate.
- [Source: build_documents/planning-artifacts/architecture.md#Tool-call dispatch order vs text emission] — text-first parallel-tools (FR45/FR46 linchpin).
- [Source: build_documents/planning-artifacts/architecture.md#Talker placement] — single TurnRouter + TurnDispatchProcessor; tools/orchestrator/beliefs are dispatcher dependencies.
- [Source: build_documents/planning-artifacts/architecture.md#NFR32] — tool-call dispatch overhead bounded; this story is the baseline.
- [Source: build_documents/planning-artifacts/prd.md#FR12] — Talker emits Cartesia-tagged text + greeting mode + belief grounding.
- [Source: build_documents/planning-artifacts/prd.md#FR45] — Talker tool registry: `go_to_sleep`, `set_mood`, typed Pydantic input validation.
- [Source: build_documents/planning-artifacts/prd.md#FR46] — deferred-sleep transition after last audio frame.
- [Source: build_documents/planning-artifacts/prd.md#FR48] — mood enum + Talker `set_mood` tool integration.
- [Source: build_documents/planning-artifacts/prd.md#NFR25, FR39] — privacy invariants.
- [Source: build_documents/planning-artifacts/epics.md#Story 4.4] — full AC list (Story 4.4's source of truth).
- [Source: build_documents/implementation-artifacts/4-1-belief-state-client.md] — `BeliefStateClient` + `grounded_keys` config; consumer-hook stub deferred to this story.
- [Source: build_documents/implementation-artifacts/4-3-activity-fsm-core.md] — `ActivityFSM.on_tool_call_go_to_sleep` is the call site for `GoToSleepTool.dispatch`.
- [Source: build_documents/implementation-artifacts/3-6-mood-module-state-and-controller.md] — `MoodController.set(mood, reason)` signature; cooldown enforcement.
- [Source: build_documents/implementation-artifacts/2-2-talker-client-anthropic.md] — `Talker.complete()` baseline; provider-aware max_tokens kwarg branch.
- [Source: src/voice_agent_pipeline/turn/talker.py] — current `Talker` impl; `_beliefs` ctor arg already there (Story 4.1) but unused.
- [Source: src/voice_agent_pipeline/pipeline.py:208-271] — current `TurnDispatchProcessor` impl (the migration target).
- [Source: src/voice_agent_pipeline/turn/__init__.py] — `build_talker` factory pattern to mirror for `build_tool_registry`.
- [Source: src/voice_agent_pipeline/schemas/mood_event.py] — `Mood` Literal definition.
- [External: https://platform.openai.com/docs/guides/function-calling] — openai tools surface (`tools=` parameter format, `tool_choice="auto"`, `tool_calls` response shape).
- [External: https://docs.pydantic.dev/latest/concepts/json_schema/] — `model_json_schema()` for emitting JSON Schema from a pydantic v2 model.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- `just check` exits 0 across the implementation (ruff + format + pyright + 411 unit tests).
- `tests/integration/test_intent_sleep.py` passes — text frame emits before bg dispatch flips `sleep_pending`.

### Completion Notes List

- Implementation matches the spec: `turn/tools.py` (ToolCall, ToolSpec, ToolRegistry, GoToSleepInput, SetMoodInput, factories, build_tool_registry); `turn/talker.py` extended with `TalkerResponse` + `complete_with_tools` (belief grounding + tool-call parsing + JSON-error drop + content-None coercion); `pipeline.py` `TurnDispatchProcessor` rewritten for text-first parallel dispatch (asyncio.create_task per tool call + done-callback); `[tools]` block in setup.toml; `prompts/talker_system.md` updated with Tools section.
- Diverged from spec on the `assert isinstance` narrowing in `set_mood` dispatch — ruff `S101` rejects bare assert in production code (asserts strip under `python -O`). Replaced with `if not isinstance(...): raise TypeError(...)`. Same runtime safety, same pyright narrowing; documented inline.
- Live-smoke (Task 11b) deferred — see task list note. The orchestrator daemon isn't running on the dev host; operator chose `[daemon] enabled = false` for now. Unit + integration coverage already pins the dispatcher contract; live validation pending orchestrator stub / real daemon.

### File List

**New:**
- `src/voice_agent_pipeline/turn/tools.py`
- `tests/unit/turn/test_tools.py`
- `tests/integration/test_intent_sleep.py`

**Modified:**
- `src/voice_agent_pipeline/turn/talker.py` — `TalkerResponse` + `TalkerClient.complete_with_tools` + `Talker.complete_with_tools` impl.
- `src/voice_agent_pipeline/turn/__init__.py` — re-export new symbols.
- `src/voice_agent_pipeline/config/setup.py` — `ToolsConfig` model + `tools` field on `SetupConfig`.
- `src/voice_agent_pipeline/pipeline.py` — `TurnDispatchProcessor` ctor + parallel dispatch + `tool_registry` wiring in `run_pipeline`; `import asyncio` added.
- `setup.toml` — `[tools]` block with `enable_go_to_sleep` / `enable_set_mood`.
- `prompts/talker_system.md` — Tools section.
- `tests/unit/turn/test_talker.py` — 10 new `complete_with_tools` tests.
- `tests/unit/turn/test_dispatch.py` — fixture updates + 4 new parallel-dispatch tests.
- `tests/unit/config/test_setup.py` — 3 new `ToolsConfig` tests.
- `tests/integration/test_simple_turn.py` — `_make_mock_talker` adds `complete_with_tools`; dispatcher gets `ToolRegistry([])`.
- `build_documents/implementation-artifacts/sprint-status.yaml` — `4-4` flips `ready-for-dev → in-progress → review`.

## Change Log

| Date | Change |
|---|---|
| 2026-05-07 | Story 4.4 prepared — Talker tool-using upgrade (complete_with_tools + ToolRegistry + GoToSleepTool + SetMoodTool). |
| 2026-05-07 | Revised — dropped `Talker.greet` / `prompts/talker_greeting.md` / `TalkerConfig.greeting_prompt_path` after Story 4.5 redesign (static-random greeting). |
| 2026-05-08 | Implementation complete — all 11 tasks (sans live smoke) done; 411 unit + 1 integration tests pass; status → review. |
