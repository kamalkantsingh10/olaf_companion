"""Operator-facing measurement / maintenance tools (run via ``python -m``).

Siblings to the runtime, NOT part of the hot path: each module here is a
CLI invoked out-of-band (e.g. ``python -m voice_agent_pipeline.tools.soak_v2``).
Unlike runtime code, these MAY catch their own errors for clean operator
reporting (CLAUDE.md rule 4's no-ExternalServiceError-catch rule is about the
runtime crash-and-restart posture, which doesn't apply to a report generator).
"""
