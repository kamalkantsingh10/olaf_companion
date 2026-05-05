"""Bootstrap smoke test for the ``voice_agent_pipeline`` package.

This file's job is narrow: prove pytest discovery works and the ``src/``
layout is wired correctly. Adding real behavior tests should not happen
here — they belong under ``tests/unit/<package>/test_<module>.py``
matching the source layout.
"""


def test_voice_agent_pipeline_imports() -> None:
    """The package imports without error from a clean test environment."""
    import voice_agent_pipeline  # noqa: F401
