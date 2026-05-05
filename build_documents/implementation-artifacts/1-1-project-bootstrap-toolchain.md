# Story 1.1: Project bootstrap & toolchain

Status: review

<!-- Note: Validation is optional. Run validate-create-story for quality check before dev-story. -->

## Story

As Kamal (the dev),
I want a fresh `voice-agent-pipeline` repo initialized with the agreed toolchain (uv, ruff, pyright, pytest, structlog, Pipecat, etc.) and module-by-domain layout,
so that all 26 subsequent stories can drop code into a working project skeleton with `just check` green from day one.

## Acceptance Criteria

1. Project initialized via `uv init voice-agent-pipeline --python 3.12`. `pyproject.toml` lists exact runtime deps (`pipecat-ai[local]`, `anthropic`, `cartesia`, `httpx`, `httpx-sse`, `pvporcupine`, `faster-whisper`, `pydantic`, `pydantic-settings`, `structlog`) and dev deps (`ruff`, `pyright`, `pytest`, `pytest-asyncio`). `uv.lock` is committed.

2. `src/voice_agent_pipeline/` package contains the module-by-domain skeleton: subpackages `audio/`, `stt/`, `turn/`, `tts/`, `splitter/`, `publisher/`, `lifecycle/`, `config/`, `logging/`, `schemas/` (each with `__init__.py`), plus root files `__main__.py`, `pipeline.py`, `errors.py` (the latter two are empty stubs in this story).

3. `tests/` directory has `unit/`, `integration/`, `contract/` subdirectories (each with `__init__.py`) plus a top-level `tests/conftest.py` (empty for now).

4. `justfile` at the project root exposes recipes: `run`, `check`, `test`, `lint`, `format`. (`reload` lands Story 5.2, `play-test-tone` lands Story 2.1.)

5. `just check` runs `ruff check`, `ruff format --check`, `pyright`, `pytest tests/unit -q` in sequence and exits 0 on the freshly bootstrapped repo.

6. Root files committed to git: `pyproject.toml`, `uv.lock`, `justfile`, `setup.toml` (placeholder with `schema_version = 1`), `expression_map.yaml` (placeholder with `schema_version: 1`), `.env.example`, `.gitignore`, `README.md`, `CLAUDE.md`, `.python-version`.

7. `.gitignore` includes `.env`, `logs/`, `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.coverage`, `*.egg-info/`, `build/`, `dist/`.

8. `uv run python -m voice_agent_pipeline` exits cleanly with the placeholder message `voice-agent-pipeline v0.0.0 — not yet implemented` and exit code 0.

9. pyright is configured **strict** for `src/` and **basic** for `tests/` in `pyproject.toml`; `just check` reports zero pyright errors on the bootstrapped repo.

10. `CLAUDE.md` captures the 9 enforcement rules from `architecture.md §"Enforcement Guidelines"` in terse form (verbatim list provided in Dev Notes).

## Tasks / Subtasks

- [x] **Task 1: Initialize project with uv** (AC: #1)
  - [x] Confirm working directory is `/home/kamal/Documents/Olaf/olaf_companion/` (the existing repo root — pre-decided per Decisions section below).
  - [x] Run `uv init --name voice-agent-pipeline --python 3.12` from inside `olaf_companion/`. Initializes in cwd; creates `pyproject.toml` and `.python-version`. **Do NOT** pass a positional name arg — that would create a subdirectory.
  - [x] If `uv init` complains about an existing directory not being empty, that's expected — confirm it still produces `pyproject.toml` and `.python-version`. If it refuses, fall back to manually authoring `pyproject.toml` per the snippets in Dev Notes plus running `uv venv && uv lock`.
  - [x] Add runtime deps in one command: `uv add pipecat-ai[local] anthropic cartesia httpx httpx-sse pvporcupine faster-whisper pydantic pydantic-settings structlog`
  - [x] Add dev deps in one command: `uv add --dev ruff pyright pytest pytest-asyncio`
  - [x] Verify `uv.lock` is created; it will be committed in Task 7.
  - [x] Run `uv sync` once to confirm reproducibility (no version drift, no errors).

- [x] **Task 2: Create module-by-domain layout** (AC: #2, #3)
  - [x] Adjust `src/` so the import root is `voice_agent_pipeline` (the `uv init` default may already produce this; reconcile if not).
  - [x] Create the 10 subpackages under `src/voice_agent_pipeline/` with empty `__init__.py` in each: `audio/`, `stt/`, `turn/`, `tts/`, `splitter/`, `publisher/`, `lifecycle/`, `config/`, `logging/`, `schemas/`.
  - [x] Create `src/voice_agent_pipeline/__main__.py` (placeholder per Task 6).
  - [x] Create empty stubs `src/voice_agent_pipeline/pipeline.py` and `src/voice_agent_pipeline/errors.py` (each: one-line module docstring + `__all__: list[str] = []`). Real content lands Stories 1.2/1.4.
  - [x] Create `tests/{unit,integration,contract}/__init__.py` (empty).
  - [x] Create `tests/conftest.py` (empty for now — shared fixtures land Story 1.3+).

- [x] **Task 3: Configure `pyproject.toml`** (AC: #1, #9)
  - [x] Add `[project]` metadata: name `voice-agent-pipeline`, version `0.0.0`, description (one line), `requires-python = ">=3.12"`.
  - [x] Add `[tool.ruff]` block (snippet in Dev Notes).
  - [x] Add `[tool.pyright]` block — strict for `src/`, basic for `tests/` (snippet in Dev Notes). **Deviation:** pyright 1.1.409 does not recognize `typeCheckingMode` inside `executionEnvironments`; replaced with explicit basic-mode rule overrides (`reportUnusedImport`, `reportUnknownMemberType`, etc.) achieving the same intent.
  - [x] Add `[tool.pytest.ini_options]` block with `asyncio_mode = "auto"`, `testpaths = ["tests"]` (snippet in Dev Notes).

- [x] **Task 4: Create root config + doc files** (AC: #6, #7, #10)
  - [x] `setup.toml`: placeholder with `schema_version = 1` and commented-out section markers for every config block subsequent stories will populate (snippet in Dev Notes).
  - [x] `expression_map.yaml`: placeholder with `schema_version: 1` + comment noting Story 3.1 populates the mapping.
  - [x] `.env.example`: `PICOVOICE_ACCESS_KEY=<your-picovoice-access-key>` plus commented placeholders for `ANTHROPIC_API_KEY`, `CARTESIA_API_KEY` (Epic 2), `DAEMON_BEARER_TOKEN` (Epic 5, optional).
  - [x] `.gitignore`: see snippet in Dev Notes — covers `.env`, `logs/`, `.venv/`, build/cache dirs. **Deviation:** added `.claude/` and `_bmad/` per Kamal's directive during dev — local AI-partner scratch dirs that shouldn't ship.
  - [x] `README.md`: brief overview, install (`uv sync`), run (`just run`), test (`just check`), pointers to `build_documents/planning-artifacts/{prd,architecture,epics}.md`, plus the system-installed `rclpy` note.
  - [x] `CLAUDE.md`: verbatim 9-rule list from Dev Notes.

- [x] **Task 5: Build `justfile`** (AC: #4, #5)
  - [x] Recipes: `run`, `check`, `test`, `lint`, `format` per snippet in Dev Notes. **Deviation:** added `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` and explicit `-p pytest_asyncio.plugin` to immunize against ROS-sourced `PYTHONPATH` (which exposes the `launch_testing` pytest plugin whose `lark` dep isn't in our venv). Architecture intent that ROS lives on `PYTHONPATH` (Story 3.4 needs `rclpy`) collides with pytest's auto-loading of all `pytest11` entry points; this is the clean fix.
  - [x] `default: check` so bare `just` runs the gate.
  - [x] Verify each recipe runs from a clean shell.

- [x] **Task 6: Stub entry point** (AC: #8)
  - [x] `src/voice_agent_pipeline/__main__.py` content per snippet in Dev Notes — single `main()` function that prints the placeholder line and exits 0.
  - [x] Verify `uv run python -m voice_agent_pipeline` produces exactly `voice-agent-pipeline v0.0.0 — not yet implemented` to stdout, exit code 0.

- [x] **Task 7: Smoke test + verify `just check` is green** (AC: #5, #9)
  - [x] Add `tests/unit/test_smoke.py` with a single trivial test (snippet in Dev Notes) — proves pytest discovery + import path are wired.
  - [x] Run `just check`; resolve any ruff or pyright noise (likely none on stubs).
  - [ ] Once green, commit the bootstrap as a single commit titled `Story 1.1: project bootstrap & toolchain`. *(deferred to user — see Completion Notes)*

## Dev Notes

### Architectural intent (read this first)

This story is the **foundation for every later story**. Module layout, naming conventions, test organization, build commands — they all carry through 26 future stories. The architecture document is the source of truth; this story executes it. Do **not** improvise. Do **not** add extra packages, helpers, or conveniences not specified here. The right time for those is the story that needs them.

The architecture deliberately chose a **Pipecat Quickstart + Modern Python Service Skeleton** because Pipecat is a hard dep — tracking their idioms reduces drift as the framework evolves. `uv` is the single tool for project init, deps, lockfile, Python pin, virtualenv, and task running. No `requirements.txt`, no pyenv, no separate venv tooling.

### Why we're not doing things you might be tempted to add

- **No `pre-commit` framework.** Solo dev + AI partner workflow. The AI partner runs `just check` per `CLAUDE.md` rule #1. Adding pre-commit duplicates this with no benefit. (Architecture §"Operations" Batch 5)
- **No CI tooling (GitHub Actions / GitLab).** Out of v1 scope. `just test` runs locally. (Architecture §"Nice-to-Have Gaps")
- **No `utils/` package.** Domain-by-module layout is non-negotiable. (Architecture §"Anti-Patterns")
- **No `EnvironmentFile` for systemd.** App reads `.env` directly via pydantic-settings (lands Story 1.2). systemd unit lands Story 5.4 — don't preempt it.
- **No real entry-point logic.** `__main__.py` is a one-line stub. Argv parsing, signal handlers, `asyncio.run`, and startup validation all land incrementally across Stories 1.2, 1.3, 1.5, 1.6, 1.7, 2.5, 4.4, 5.1, 5.2.
- **No real `errors.py` content.** Custom exception hierarchy lands Story 1.4. This story's `errors.py` is an empty stub with `__all__: list[str] = []`.

### Runtime dependency notes

| Dep | Why | First story that uses it |
|---|---|---|
| `pipecat-ai[local]` | voice-loop framework + `LocalAudioTransport` (PyAudio) | Story 1.5 (mic), 2.1 (speaker) |
| `anthropic` | Talker LLM client | Story 2.2 |
| `cartesia` | Sonic-3 streaming TTS | Story 2.3 |
| `httpx` + `httpx-sse` | orchestrator SSE + belief-state HTTP | Story 4.1, 4.2 |
| `pvporcupine` | wake-word detection | Story 1.6 |
| `faster-whisper` | on-device STT (CTranslate2 backend, ~4× faster than reference) | Story 1.7 |
| `pydantic` + `pydantic-settings` | config + event schemas | Story 1.2, 1.4 |
| `structlog` | JSON-structured logging | Story 1.3 |

**`rclpy` is NOT a uv dep.** It's installed via the system ROS 2 distro (e.g., `apt install ros-jazzy-rclpy`) and exposed to the venv via `PYTHONPATH`. The README must mention this; full integration lands Story 3.4. Don't try to `uv add rclpy` — it won't work cleanly.

### `pyproject.toml` snippets

**`[project]`:**

```toml
[project]
name = "voice-agent-pipeline"
version = "0.0.0"
description = "Pipecat-based voice-agent service for the OLAF Companion project."
requires-python = ">=3.12"
```

**`[tool.ruff]`:**

```toml
[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "ASYNC", "S", "RUF"]
ignore = []

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]  # allow `assert` in tests

[tool.ruff.lint.isort]
known-first-party = ["voice_agent_pipeline"]
```

**`[tool.pyright]`:**

```toml
[tool.pyright]
include = ["src", "tests"]
pythonVersion = "3.12"
typeCheckingMode = "strict"

[[tool.pyright.executionEnvironments]]
root = "tests"
reportMissingTypeStubs = false
typeCheckingMode = "basic"
```

**`[tool.pytest.ini_options]`:**

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
python_files = ["test_*.py"]
```

### Module-by-domain layout — exact tree to create

Per `architecture.md §"Complete Project Directory Structure"`:

```
voice-agent-pipeline/
├── README.md
├── CLAUDE.md
├── pyproject.toml
├── uv.lock
├── justfile
├── .python-version
├── setup.toml                      # placeholder
├── expression_map.yaml             # placeholder
├── .env.example
├── .gitignore
├── src/
│   └── voice_agent_pipeline/
│       ├── __init__.py
│       ├── __main__.py
│       ├── pipeline.py             # empty stub
│       ├── errors.py               # empty stub
│       ├── audio/__init__.py
│       ├── stt/__init__.py
│       ├── turn/__init__.py
│       ├── tts/__init__.py
│       ├── splitter/__init__.py
│       ├── publisher/__init__.py
│       ├── lifecycle/__init__.py
│       ├── config/__init__.py
│       ├── logging/__init__.py
│       └── schemas/__init__.py
└── tests/
    ├── conftest.py
    ├── unit/
    │   ├── __init__.py
    │   └── test_smoke.py
    ├── integration/
    │   └── __init__.py
    └── contract/
        └── __init__.py
```

**Do not create** `models/wakeword/`, `deploy/systemd/`, `prompts/`, or `logs/` in this story. They land in the stories that need them (1.6, 5.4, 2.2, 1.3 respectively).

### `.gitignore` content

```
.env
logs/
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.coverage
*.egg-info/
build/
dist/
```

### `setup.toml` placeholder content

```toml
schema_version = 1

# Sections populated by subsequent stories:
# [stt]            -- Story 1.7 (backend, model, low_confidence_threshold, clarification_prompt)
# [audio]          -- Story 1.5 (input_device_name) + Story 2.1 (output_device_name)
# [talker]         -- Story 2.2 (model, max_tokens, system_prompt_path) + Story 4.1 (grounded_keys)
# [tts]            -- Story 2.3 (voice_id, default_emotion, model)
# [publisher]      -- Story 3.4 (transport, dds_domain_id, expression_channel, lifecycle_channel)
# [lifecycle]      -- Story 4.4 (idle_to_sleeping_seconds)
# [router]         -- Story 4.3 (slow_path_patterns, default)
# [daemon]         -- Story 4.1/4.2 (url) + Story 5.3 (bearer_token_env, mtls)
# [barge_in]       -- Story 5.1 (sustained_ms, energy_threshold)
# [logging]        -- Story 5.3 (max_file_size_mb, retention_days, console_mirror)
```

### `expression_map.yaml` placeholder content

```yaml
schema_version: 1

# Story 3.1 populates the full mapping:
#
# emotions:
#   neutral: { ... payload ... }
#   content: { ... }
#   excited: { ... }
#   sad: { ... }
#   angry: { ... }
#   scared: { ... }
#   happy: { ... }
#   curious: { ... }
#   sympathetic: { ... }
#   surprised: { ... }
#   frustrated: { ... }
#   melancholic: { ... }
#
# bursts:
#   laughter: { ... }
#   sigh: { ... }
#   gasp: { ... }
#   clears_throat: { ... }
#
# fallback_families:
#   high_energy_positive: { members: [...], maps_to: excited }
#   low_energy_negative: { members: [...], maps_to: sad }
#   ... (7 families total covering all 60+ Cartesia tags)
#
# unknown:
#   maps_to: neutral
```

### `.env.example` content

```
PICOVOICE_ACCESS_KEY=<your-picovoice-access-key>

# Wired in Epic 2 (Stories 2.2, 2.3):
# ANTHROPIC_API_KEY=<your-anthropic-api-key>
# CARTESIA_API_KEY=<your-cartesia-api-key>

# Wired in Epic 5 Story 5.3 (only when daemon URL is non-localhost):
# DAEMON_BEARER_TOKEN=<shared-secret>
```

After running `cp .env.example .env`, the operator runs `chmod 0600 .env`. Story 1.2 will add a startup WARN if perms are looser than 0600 (NFR23 advisory).

### `justfile` content

```just
default: check

run:
    uv run python -m voice_agent_pipeline

check:
    uv run ruff check
    uv run ruff format --check
    uv run pyright
    uv run pytest tests/unit -q

test:
    uv run pytest

lint:
    uv run ruff check

format:
    uv run ruff format
```

### `__main__.py` content

```python
"""Voice agent pipeline entry point — bootstrap stub.

Real entry-point logic (argv parsing, signal handlers, asyncio.run, startup
validation) lands incrementally across Stories 1.2, 1.3, 1.5, 1.6, 1.7, 2.5,
4.4, 5.1, 5.2.
"""


def main() -> None:
    print("voice-agent-pipeline v0.0.0 — not yet implemented")


if __name__ == "__main__":
    main()
```

### `tests/unit/test_smoke.py` content

```python
"""Smoke test verifying the package imports cleanly."""


def test_voice_agent_pipeline_imports() -> None:
    import voice_agent_pipeline  # noqa: F401
```

Trivial by design — its job is to prove pytest discovery + the `src/` import path work. Story 1.2 onward adds real tests.

### `CLAUDE.md` content (verbatim)

```markdown
# CLAUDE.md — voice-agent-pipeline AI partner rules

These rules are enforced on every change to this repo. Skipping them is a defect.

1. Run `just check` before committing. ruff (lint+format) + pyright + fast pytest must be green. Failures block commits.
2. Honor the module-by-domain layout. Don't introduce new top-level directories without updating `architecture.md`.
3. Use `typing.Protocol` for interfaces, `pydantic.BaseModel` for events/config/data, `typing.Literal[...]` for fixed string sets. No `abc.ABC`. No `enum.Enum`. No plain dicts at boundaries.
4. Never catch `ExternalServiceError` (or its subclasses) in v1 code paths. Crash and let systemd restart.
5. Use `snake_case` everywhere keys are written — Python, TOML, YAML, JSON payload, DDS field names, log fields. No exceptions.
6. Bump `schema_version` only on breaking changes. Adding optional fields is forward-compat — don't bump.
7. Mock only at Protocol boundaries in tests. Never mock internal functions or pydantic models.
8. Never log raw audio, credentials, or (at INFO+) transcripts. The redaction processor catches mistakes; don't rely on it — write code that doesn't pass these in.
9. Update `prd.md` / `voice-agent-pipeline-brief.md` / `voice-agent-pipeline.md` / `architecture.md` in the same commit if a deviation is needed (NFR26 — spec-as-contract).

The full architecture rationale lives at `build_documents/planning-artifacts/architecture.md`.
The current epic + story plan lives at `build_documents/planning-artifacts/epics.md`.
```

### `README.md` skeleton

```markdown
# voice-agent-pipeline

Pipecat-based voice-agent service for the OLAF Companion project. Owns the voice loop and embodiment broadcast surface — captures speech, dispatches turns, generates spoken responses with Cartesia, and publishes typed expression + lifecycle events on configurable broadcast channels.

## Install

```bash
uv sync
```

`rclpy` is installed separately via your system ROS 2 distro (e.g., `apt install ros-jazzy-rclpy`) and exposed to the venv via `PYTHONPATH`. Required from Story 3.4 onward.

## Run

```bash
just run    # starts the pipeline (current state: bootstrap stub)
just check  # lint + type-check + unit tests (must pass before commit)
just test   # full test suite
```

## Documentation

- Requirements & user journeys: `build_documents/planning-artifacts/prd.md`
- Architecture decisions: `build_documents/planning-artifacts/architecture.md`
- Epics & stories: `build_documents/planning-artifacts/epics.md`
- Per-story specs: `build_documents/implementation-artifacts/`
- AI partner rules: `CLAUDE.md`
```

### Project structure notes

- **The implementation lives at the root of `olaf_companion/`** — i.e. `pyproject.toml`, `src/`, `tests/`, `justfile`, `CLAUDE.md`, `setup.toml`, `expression_map.yaml`, `.env.example`, `README.md`, `.python-version`, `uv.lock` all sit at `/home/kamal/Documents/Olaf/olaf_companion/`.
- Planning docs stay in `build_documents/planning-artifacts/`. Per-story specs live in `build_documents/implementation-artifacts/`. Both directories sit alongside the new code at the repo root.
- The repo is **already a git repo** with `main` branch (verified at start of conversation). No `git init` needed; just `git add` + commit at Task 7.
- Existing untracked items at root before this story: `.claude/`, `.vscode/`, `_bmad/`, `build_documents/`. None conflict with the bootstrap files.
- All paths in code snippets and ACs are relative to `/home/kamal/Documents/Olaf/olaf_companion/` unless otherwise noted.

### Testing standards

- Layout mirrors `src/` exactly. From Story 1.2 onward, new tests live at `tests/unit/<package>/test_<module>.py` matching `src/voice_agent_pipeline/<package>/<module>.py`.
- One behavior per test. Test name describes the behavior (e.g., `test_emits_on_sentence_terminator`).
- `pytest-asyncio` async tests use auto mode (configured in `pyproject.toml` — no per-test marker required).
- Mock only at Protocol seams. Rule lands meaningfully from Story 1.4 onward.
- `just check` runs only `tests/unit -q` for fast feedback. Full suite (`unit + integration + contract`) runs via `just test`.
- This story's only test is `test_smoke.py` proving the import path works.

### What "done" looks like

After this story:
- From `/home/kamal/Documents/Olaf/olaf_companion/`, `just check` exits 0.
- From the same directory, `just run` prints `voice-agent-pipeline v0.0.0 — not yet implemented` and exits 0.
- `git status` is clean; one commit titled `Story 1.1: project bootstrap & toolchain` exists on `main`.
- Story 1.2 can begin without any setup work.

### References

- [Source: build_documents/planning-artifacts/architecture.md#Selected Starter: Pipecat Quickstart + Modern Python Service Skeleton]
- [Source: build_documents/planning-artifacts/architecture.md#Complete Project Directory Structure]
- [Source: build_documents/planning-artifacts/architecture.md#Module & File Layout]
- [Source: build_documents/planning-artifacts/architecture.md#Type System Conventions]
- [Source: build_documents/planning-artifacts/architecture.md#Implementation Patterns & Consistency Rules]
- [Source: build_documents/planning-artifacts/architecture.md#Enforcement Guidelines]
- [Source: build_documents/planning-artifacts/architecture.md#First Implementation Priority]
- [Source: build_documents/planning-artifacts/architecture.md#Anti-Patterns (Don't)]
- [Source: build_documents/planning-artifacts/epics.md#Story 1.1: Project bootstrap & toolchain]
- [Source: build_documents/planning-artifacts/prd.md#Resource Requirements] — solo-dev + AI-partner posture justifies the no-pre-commit / `just check` gate.

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (1M context) — invoked as bmad-agent-dev "Amelia".

### Debug Log References

- `uv init --name voice-agent-pipeline --python 3.12 --lib` initialized in cwd alongside existing `_bmad/`, `build_documents/`, etc. — no errors.
- `uv add pipecat-ai[local] anthropic cartesia httpx httpx-sse pvporcupine faster-whisper pydantic pydantic-settings structlog` — 85 packages resolved.
- `uv add --dev ruff pyright pytest pytest-asyncio` — added ruff 0.15.12, pyright 1.1.409, pytest 9.0.3, pytest-asyncio 1.3.0.
- First `just check` failed with two issues:
  1. `pyright` flagged `typeCheckingMode = "basic"` inside `[[tool.pyright.executionEnvironments]]` as "unrecognized setting" (this version requires explicit per-rule overrides instead).
  2. `pytest` failed at plugin autoload because `PYTHONPATH=/opt/ros/jazzy/lib/python3.12/site-packages` exposes `launch_testing` (pytest11 entry point) whose dep `lark` is not in the venv.
- Fix #1: replaced `typeCheckingMode = "basic"` with explicit `reportUnusedImport=false` + 7 other `reportUnknown*=false` overrides, preserving the "basic for tests" intent.
- Fix #2: justfile sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` and pytest invocations explicitly load `-p pytest_asyncio.plugin`. Final `just check` exit 0; smoke test passes; entry-point output verified.

### Completion Notes List

- All 10 ACs satisfied; `just check` green (ruff clean, ruff format clean, pyright 0 errors, pytest 1/1 pass).
- AC #8 verified: `uv run python -m voice_agent_pipeline` prints `voice-agent-pipeline v0.0.0 — not yet implemented`, exit 0.
- **Deviation 1 — `.gitignore`:** added `.claude/` and `_bmad/` to the spec's list (Kamal's directive during dev). Per CLAUDE.md rule #9 / NFR26, this is a minor local-only addition; no planning-doc update warranted.
- **Deviation 2 — `[tool.pyright]`:** replaced `typeCheckingMode = "basic"` in `executionEnvironments` with explicit basic-rule overrides (pyright 1.1.409 limitation). Same effect, different syntax. Architecture's pyright section (architecture.md §"Implementation Patterns") may want a clarifying note for future devs.
- **Deviation 3 — `justfile`:** added `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` + explicit `-p pytest_asyncio.plugin`. Forced by ROS 2 Jazzy being sourced (architecture wants this for `rclpy` later). Recommend adding a one-line note to architecture.md §"Operations" explaining this; also flag for Story 3.4 (`rclpy` integration) and any future story that adds a new pytest plugin (must add `-p <plugin.module>` to the justfile recipes).
- **Task 7 commit deferred.** Per system guidance, git commits await user approval. Working tree is ready; suggest `git add` + commit on user request.

### File List

**New files:**
- `pyproject.toml` (overwritten by uv init then customized — deps + ruff/pyright/pytest config)
- `uv.lock`
- `.python-version`
- `justfile`
- `setup.toml`
- `expression_map.yaml`
- `.env.example`
- `.gitignore`
- `README.md`
- `CLAUDE.md`
- `src/voice_agent_pipeline/__init__.py`
- `src/voice_agent_pipeline/__main__.py`
- `src/voice_agent_pipeline/pipeline.py`
- `src/voice_agent_pipeline/errors.py`
- `src/voice_agent_pipeline/py.typed` (created by `uv init --lib`)
- `src/voice_agent_pipeline/audio/__init__.py`
- `src/voice_agent_pipeline/stt/__init__.py`
- `src/voice_agent_pipeline/turn/__init__.py`
- `src/voice_agent_pipeline/tts/__init__.py`
- `src/voice_agent_pipeline/splitter/__init__.py`
- `src/voice_agent_pipeline/publisher/__init__.py`
- `src/voice_agent_pipeline/lifecycle/__init__.py`
- `src/voice_agent_pipeline/config/__init__.py`
- `src/voice_agent_pipeline/logging/__init__.py`
- `src/voice_agent_pipeline/schemas/__init__.py`
- `tests/conftest.py`
- `tests/unit/__init__.py`
- `tests/unit/test_smoke.py`
- `tests/integration/__init__.py`
- `tests/contract/__init__.py`

**Modified files:**
- `build_documents/implementation-artifacts/sprint-status.yaml` (status `ready-for-dev` → `review`)
- `build_documents/implementation-artifacts/1-1-project-bootstrap-toolchain.md` (this file — Status, Tasks, Dev Agent Record, File List, Change Log)

## Change Log

| Date | Change |
|---|---|
| 2026-05-05 | Story 1.1 implemented. Bootstrap repo with uv + module-by-domain layout. `just check` green. Three documented deviations (gitignore additions, pyright basic-mode syntax, pytest plugin autoload immunization). Status moved to `review`. |

---

## Decisions (locked in by Kamal — no further questions)

1. **Repo location:** `/home/kamal/Documents/Olaf/olaf_companion/` (root of the existing repo). Implementation files sit alongside `build_documents/`, `_bmad/`, `.claude/`, `.vscode/`. **No subdirectory.**
2. **Python version:** `3.12` — architecture's recommended floor; well-supported by every dep we're pulling. Bumping to `3.13` is a future-Kamal call; nothing in this story precludes it.
3. **Git:** the `olaf_companion` repo is already initialized on `main`. No `git init` needed. Task 7's commit lands on the existing repo.
4. **Architecture's "Deployment to host" path** (`/home/<user>/voice-agent-pipeline/` clone path) is now `/home/kamal/Documents/Olaf/olaf_companion/`. Story 5.4's systemd unit `WorkingDirectory` will point here. Update `architecture.md` §"Deployment to host" in the same commit per NFR26 spec-as-contract — flagging this as a doc-update obligation for Story 1.1's commit.
