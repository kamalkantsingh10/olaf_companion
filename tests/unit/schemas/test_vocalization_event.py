"""Tests for :mod:`voice_agent_pipeline.schemas.vocalization_event`."""

import pytest
from pydantic import ValidationError

from voice_agent_pipeline.schemas.vocalization_event import (
    VocalizationEvent,
    VocalizationPayload,
)


def test_minimal_valid_vocalization_event() -> None:
    event = VocalizationEvent(payload=VocalizationPayload(tag="laughter", tts_supported=True))
    assert event.payload.tag == "laughter"
    assert event.payload.tts_supported is True
    assert event.payload.audio_frame_id is None
    assert event.schema_version == 2


def test_payload_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        VocalizationPayload(tag="laughter", tts_supported=True, bogus=1)  # type: ignore[call-arg]


def test_tts_supported_strict_bool() -> None:
    """A non-bool value rejected — pin the strict-typing contract.

    Pydantic v2 default mode coerces ``"yes"``/``"no"`` to True/False;
    use a value that's unambiguously not a bool.
    """
    with pytest.raises(ValidationError):
        VocalizationPayload(tag="laughter", tts_supported="maybe")  # type: ignore[arg-type]


def test_payload_is_frozen() -> None:
    payload = VocalizationPayload(tag="laughter", tts_supported=True)
    with pytest.raises(ValidationError):
        payload.tag = "sigh"  # type: ignore[misc]


def test_audio_frame_id_optional() -> None:
    payload = VocalizationPayload(tag="sigh", tts_supported=False, audio_frame_id="f-7")
    assert payload.audio_frame_id == "f-7"
