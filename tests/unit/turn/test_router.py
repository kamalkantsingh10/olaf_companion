"""Unit tests for :mod:`voice_agent_pipeline.turn.router`.

The router is **pure logic** — synchronous, no I/O — so no fixtures
beyond a stub Talker (a :class:`MagicMock` typed against the
:class:`TalkerClient` Protocol). Tests pin the routing contract;
the dispatcher's async behaviour lives in
:mod:`tests.unit.turn.test_dispatch`.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pydantic
import pytest

from voice_agent_pipeline.config.setup import SttConfig
from voice_agent_pipeline.turn.router import RouteDecision, TurnRouter


@pytest.fixture
def stt_config() -> SttConfig:
    """SttConfig with the default 0.5 threshold + a unique clarification prompt.

    Pinning the prompt to a recognisable value lets the substitution
    tests assert on identity rather than just "some string".
    """
    return SttConfig(low_confidence_threshold=0.5, clarification_prompts=["please repeat?"])


@pytest.fixture
def stub_talker() -> MagicMock:
    """A MagicMock standing in for a TalkerClient — never invoked by route()."""
    return MagicMock()


def test_high_confidence_routes_to_talker_with_original_text(
    stt_config: SttConfig,
    stub_talker: MagicMock,
) -> None:
    """confidence > threshold → target=talker, text=original transcript, clarification=False."""
    router = TurnRouter(stt_config, stub_talker)
    decision = router.route("what time is it?", confidence=0.9)

    assert decision == RouteDecision(
        target="talker",
        text="what time is it?",
        clarification=False,
    )


def test_low_confidence_routes_to_talker_with_clarification_prompt(
    stt_config: SttConfig,
    stub_talker: MagicMock,
) -> None:
    """confidence < threshold → text replaced with the clarification prompt.

    Documents the v1 design choice: drop the noisy transcript on the
    floor, substitute the clarification prompt, route to Talker. The
    Talker's reply is a "could you repeat that?" question rather than
    a guess at the bad transcript.
    """
    router = TurnRouter(stt_config, stub_talker)
    decision = router.route("hjzz mjy?", confidence=0.3)

    assert decision == RouteDecision(
        target="talker",
        text="please repeat?",  # clarification_prompt from the fixture
        clarification=True,
    )


def test_threshold_boundary_inclusive_at_threshold(
    stt_config: SttConfig,
    stub_talker: MagicMock,
) -> None:
    """confidence == threshold takes the high-confidence path (>= not >).

    Pinning this contract: the boundary is inclusive. An STT call that
    reports exactly the threshold value is treated as confident,
    avoiding spurious clarification dialogs at the edge.
    """
    router = TurnRouter(stt_config, stub_talker)
    decision = router.route("hello", confidence=0.5)

    assert decision.clarification is False
    assert decision.text == "hello"


def test_low_confidence_picks_random_from_clarification_list(
    stub_talker: MagicMock,
) -> None:
    """Story 4.5: ``decision.text`` is one entry from the configured list."""
    config = SttConfig(
        low_confidence_threshold=0.5,
        clarification_prompts=["alpha", "beta", "gamma"],
    )
    router = TurnRouter(config, stub_talker)
    # Drive several routes; every result should be in the configured list.
    for _ in range(20):
        decision = router.route("noisy", confidence=0.2)
        assert decision.text in {"alpha", "beta", "gamma"}
        assert decision.clarification is True


def test_low_confidence_logs_clarification_picked(
    stt_config: SttConfig,
    stub_talker: MagicMock,
) -> None:
    """Story 4.5: low-confidence route logs ``clarification.picked`` with text."""
    import structlog as _structlog

    router = TurnRouter(stt_config, stub_talker)
    with _structlog.testing.capture_logs() as captured:
        decision = router.route("noisy", confidence=0.2)

    matching = [r for r in captured if r.get("event") == "clarification.picked"]
    assert len(matching) == 1
    assert matching[0].get("text") == decision.text


def test_router_escalates_on_keyword_match(stub_talker: MagicMock) -> None:
    """Story 4.7: a slow_path_pattern match → ``target="orchestrator"``."""
    config = SttConfig(low_confidence_threshold=0.5, clarification_prompts=["huh?"])
    router = TurnRouter(config, stub_talker, slow_path_patterns=["calendar"])
    decision = router.route("what's on my calendar today", confidence=0.9)
    assert decision.target == "orchestrator"
    assert decision.text == "what's on my calendar today"
    assert decision.clarification is False


def test_router_escalates_case_insensitive(stub_talker: MagicMock) -> None:
    """Story 4.7: pattern matching is case-insensitive."""
    config = SttConfig(low_confidence_threshold=0.5, clarification_prompts=["huh?"])
    router = TurnRouter(config, stub_talker, slow_path_patterns=["calendar"])
    decision = router.route("CALENDAR check please", confidence=0.9)
    assert decision.target == "orchestrator"


def test_router_does_not_escalate_when_no_match(stub_talker: MagicMock) -> None:
    """Story 4.7: no pattern match → ``default_target`` (Talker by default)."""
    config = SttConfig(low_confidence_threshold=0.5, clarification_prompts=["huh?"])
    router = TurnRouter(config, stub_talker, slow_path_patterns=["calendar"])
    decision = router.route("tell me a joke", confidence=0.9)
    assert decision.target == "talker"


def test_router_default_target_orchestrator(stub_talker: MagicMock) -> None:
    """Story 4.7: ``default_target="orchestrator"`` + empty patterns → orchestrator."""
    config = SttConfig(low_confidence_threshold=0.5, clarification_prompts=["huh?"])
    router = TurnRouter(
        config,
        stub_talker,
        slow_path_patterns=[],
        default_target="orchestrator",
    )
    decision = router.route("anything", confidence=0.9)
    assert decision.target == "orchestrator"


def test_router_low_confidence_routes_to_talker_even_with_pattern_match(
    stub_talker: MagicMock,
) -> None:
    """Clarification beats slow-path: bad input → clarify first."""
    config = SttConfig(low_confidence_threshold=0.5, clarification_prompts=["huh?"])
    router = TurnRouter(config, stub_talker, slow_path_patterns=["calendar"])
    decision = router.route("calendar??", confidence=0.2)
    assert decision.target == "talker"
    assert decision.clarification is True
    assert decision.text == "huh?"


def test_router_invalid_regex_raises_config_error(stub_talker: MagicMock) -> None:
    """Story 4.7: bad regex at construction → ``ConfigError`` with pattern context."""
    from voice_agent_pipeline.errors import ConfigError

    config = SttConfig(low_confidence_threshold=0.5, clarification_prompts=["huh?"])
    with pytest.raises(ConfigError) as exc_info:
        TurnRouter(config, stub_talker, slow_path_patterns=["[invalid"])
    assert exc_info.value.context.get("stage") == "router"
    assert exc_info.value.context.get("pattern") == "[invalid"


def test_router_emits_log_on_escalation(stub_talker: MagicMock) -> None:
    """Story 4.7: ``router.escalated_to_orchestrator`` INFO log fires on match.

    Privacy: log carries the matched pattern + transcript LENGTH only,
    never the transcript itself.
    """
    import structlog as _structlog

    config = SttConfig(low_confidence_threshold=0.5, clarification_prompts=["huh?"])
    router = TurnRouter(config, stub_talker, slow_path_patterns=["calendar"])

    with _structlog.testing.capture_logs() as captured:
        router.route("what's on my calendar today", confidence=0.9)

    matching = [r for r in captured if r.get("event") == "router.escalated_to_orchestrator"]
    assert len(matching) == 1
    rec = matching[0]
    assert rec.get("pattern_matched") == "calendar"
    assert rec.get("transcript_length") == len("what's on my calendar today")
    # Privacy: the transcript text itself NOT in the log.
    assert "what's on my calendar today" not in str(rec)


def test_route_decision_is_frozen() -> None:
    """RouteDecision is immutable — mutating fields raises ValidationError.

    Pydantic enforces ``frozen=True`` on the model_config; the test
    documents the contract so a future refactor doesn't accidentally
    relax it.
    """
    decision = RouteDecision(target="talker", text="hi", clarification=False)
    with pytest.raises(pydantic.ValidationError):
        decision.target = "orchestrator"  # type: ignore[misc]


def test_router_does_not_call_talker(
    stt_config: SttConfig,
    stub_talker: MagicMock,
) -> None:
    """route() is pure routing — it does NOT invoke talker.complete().

    The actual async invocation belongs to the dispatcher
    (:class:`TurnDispatchProcessor`). Pinning this contract keeps
    :class:`TurnRouter` synchronous + unit-testable in isolation.
    """
    router = TurnRouter(stt_config, stub_talker)
    router.route("hi", confidence=0.9)
    router.route("noisy", confidence=0.1)

    stub_talker.complete.assert_not_called()


def test_router_stores_orchestrator_protocol_but_v1_passes_none(
    stt_config: SttConfig,
    stub_talker: MagicMock,
) -> None:
    """Constructor accepts ``orchestrator: OrchestratorClient | None``; v1 default is None.

    Story 4.3 wires the orchestrator path; the seam exists now so 4.3
    doesn't refactor the constructor signature.
    """
    router = TurnRouter(stt_config, stub_talker)
    assert router.orchestrator is None
    assert router.talker is stub_talker

    # Explicit orchestrator passes through too.
    stub_orchestrator = MagicMock()
    router2 = TurnRouter(stt_config, stub_talker, orchestrator=stub_orchestrator)
    assert router2.orchestrator is stub_orchestrator


# Suppress unused-import warning for Path (kept for symmetry with sibling
# test files that all import it).
_ = Path
