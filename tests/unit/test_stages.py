import pytest

from ai_business_explorer.domain.errors import DomainValidationError
from ai_business_explorer.domain.stages import (
    STAGE_KEYS,
    StageScope,
    get_stage,
    previous_idea_stage,
    stages_from,
)


def test_nine_stages_in_order() -> None:
    assert STAGE_KEYS == (
        "idea_generation",
        "market_research",
        "competitor_research",
        "technology_research",
        "patent_ip_research",
        "monetization_analysis",
        "risk_legal_analysis",
        "overall_analysis",
        "human_review",
    )


def test_idea_generation_is_exploration_scope_and_human_review_is_human_only() -> None:
    assert get_stage("idea_generation").scope is StageScope.EXPLORATION
    assert get_stage("human_review").executable_by_ai is False


def test_previous_idea_stage() -> None:
    assert previous_idea_stage("market_research") is None
    prev = previous_idea_stage("competitor_research")
    assert prev is not None
    assert prev.key == "market_research"


def test_stages_from_returns_target_and_later_stages_of_same_scope() -> None:
    keys = [s.key for s in stages_from("technology_research")]
    assert keys[0] == "technology_research"
    assert "human_review" in keys
    assert "market_research" not in keys


def test_unknown_stage() -> None:
    with pytest.raises(DomainValidationError):
        get_stage("unknown")
