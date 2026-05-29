"""Contract — :class:`VocalizationEvent` JSON round-trip + schema_version.

Story 6.3 extends this with the ``emphasis`` tag: it round-trips like any
other vocalization (``tag`` is an open string on the wire — no schema bump),
and the production ``expression_map.yaml`` loader accepts it as the 7th
vocabulary entry (the constraint lives in the YAML, not the pydantic model).
"""

from pathlib import Path

import pytest

from voice_agent_pipeline.config.expression_map import load_from_path
from voice_agent_pipeline.config.version import assert_schema_version
from voice_agent_pipeline.errors import SchemaVersionError
from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
    VocalizationPayload,
)
from voice_agent_pipeline.splitter.mapping import resolve_vocalization

#: The production map at the repo root — the same file the pipeline loads at
#: startup. Loading it here is the contract check that ``emphasis`` shipped.
_EXPRESSION_MAP = Path(__file__).resolve().parents[2] / "expression_map.yaml"


def test_vocalization_event_round_trips_through_json() -> None:
    original = VocalizationEvent(payload=VocalizationPayload(tag="laughter", tts_supported=True))
    wire = original.model_dump_json()
    rebuilt = VocalizationEvent.model_validate_json(wire)

    assert rebuilt.payload.tag == "laughter"
    assert rebuilt.payload.tts_supported is True


def test_vocalization_event_old_schema_version_rejected() -> None:
    event = VocalizationEvent(
        payload=VocalizationPayload(tag="sigh", tts_supported=False),
        schema_version=1,
    )
    with pytest.raises(SchemaVersionError):
        assert_schema_version(event.schema_version, source="VocalizationEvent")


def test_emphasis_payload_round_trips_with_audio_frame_id() -> None:
    """Story 6.3 — `emphasis` is just another vocalization on the wire.

    ``tag`` is an open string, so no schema bump is needed; the
    ``audio_frame_id`` anchor (word-offset form) survives the round trip.
    """
    original = VocalizationEvent(
        payload=VocalizationPayload(
            tag="emphasis", audio_frame_id="seg-1-w-240", tts_supported=False
        )
    )
    rebuilt = VocalizationEvent.model_validate_json(original.model_dump_json())
    assert rebuilt.payload.tag == "emphasis"
    assert rebuilt.payload.audio_frame_id == "seg-1-w-240"
    assert rebuilt.payload.tts_supported is False


def test_production_map_includes_emphasis_vocalization() -> None:
    """The shipped expression_map.yaml carries `emphasis` (7th vocalization)."""
    config = load_from_path(_EXPRESSION_MAP)
    assert "emphasis" in config.vocalizations
    assert config.vocalizations["emphasis"].tts_supported is False


def test_resolve_emphasis_is_known_unknown_tag_safe_defaults() -> None:
    """`emphasis` resolves as a known tag; a truly unknown tag safe-defaults.

    The YAML — not the pydantic model — is the vocabulary gate. A known
    tag resolves verbatim; an unmapped tag falls through to the
    ``tts_supported=False`` safe default (strip from TTS, still publish).
    """
    config = load_from_path(_EXPRESSION_MAP)
    known = resolve_vocalization("emphasis", config)
    assert known.tag == "emphasis"
    assert known.tts_supported is False

    unknown = resolve_vocalization("definitely_not_a_real_tag", config)
    assert unknown.tts_supported is False
