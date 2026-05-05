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
