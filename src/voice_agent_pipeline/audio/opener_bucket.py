"""OpenerBucket Literal type — extracted to a leaf module (Story 6.2).

The Story 6.2 spec calls for ``OpenerBucket`` to live in
:mod:`voice_agent_pipeline.audio.openers` alongside the selector
functions, but doing so creates a circular import:

    config/setup.py  →  audio/openers.py  →  audio/cached.py  →  config/setup.py
                       (OpenersConfig)      (CachedAudioEntry)   (SetupConfig)

Extracting just the Literal to a leaf module breaks the cycle.
This file deliberately has **no project-internal imports** so it
sits at the root of the dependency graph (same shape as
:mod:`voice_agent_pipeline.schemas.mood_event` for the ``Mood``
Literal).

Story 6.2's :mod:`voice_agent_pipeline.audio.openers` re-exports
``OpenerBucket`` for spec compliance — callers can keep writing
``from voice_agent_pipeline.audio.openers import OpenerBucket``;
:mod:`voice_agent_pipeline.config.setup` and
:mod:`voice_agent_pipeline.audio.cached` import directly from here
to avoid the cycle.
"""

from typing import Literal

# Function buckets per DR-001's frozen design + Story 6.2 AC #2.
# Order matches the AC's table for grep-ability.
OpenerBucket = Literal[
    "thinking",  # generic "I'm processing" — default for medium replies
    "acknowledge",  # quick affirmation before a short reply
    "look_up",  # READ-shaped tool calls (belief-state / short fetches)
    "delegate",  # WRITE / multi-step orchestrator dispatches
    "react",  # mirroring user emotion / meta-comments
]
