"""Pipeline assembly — top-level Pipecat pipeline construction.

Story 1.1 lands this as an empty stub. Real assembly arrives in Story 2.5
(simple turn pipeline) and gets extended in Stories 4.5 (slow-path turn) and
5.1 (barge-in). Keeping the file present from day one means later stories
can ``from voice_agent_pipeline.pipeline import build_pipeline`` without
touching the package layout.
"""

# Public surface is defined explicitly so wildcard imports remain sensible
# as the module grows. Empty list = nothing exported yet.
__all__: list[str] = []
