"""出力契約（第2回仕様 17章）：Prompt の版から出力契約を一意に決める。v1 は変更しない。"""

from typing import Any

import pytest
from pydantic import ValidationError

from ai_business_explorer.agents.base import OutputContract, output_schema_for
from ai_business_explorer.agents.employees.idea_generator import (
    IdeaGenerator,
    IdeaGeneratorOutput,
    IdeaGeneratorOutputV2,
)
from ai_business_explorer.agents.employees.market_researcher import (
    MarketResearcher,
    MarketResearcherOutput,
    MarketResearcherOutputV2,
)
from ai_business_explorer.domain.errors import DomainValidationError
from ai_business_explorer.prompts.loader import prompt_exists


def test_prompt_version_to_contract_table() -> None:
    assert {v: c.schema_version for v, c in IdeaGenerator.output_contracts.items()} == {
        "v1": "idea_generation.v1",
        "v2": "idea_generation.v2",
    }
    assert {v: c.schema_version for v, c in MarketResearcher.output_contracts.items()} == {
        "v1": "market_research.v1",
        "v2": "market_research.v1",
        "v3": "market_research.v1",
        "v4": "market_research.v2",
    }
    assert IdeaGenerator.contract_for("v1") == OutputContract(
        "idea_generation.v1", IdeaGeneratorOutput
    )
    assert IdeaGenerator.contract_for("v2") == OutputContract(
        "idea_generation.v2", IdeaGeneratorOutputV2
    )
    for version in ("v1", "v2", "v3"):
        assert MarketResearcher.contract_for(version).output_model is MarketResearcherOutput
    assert MarketResearcher.contract_for("v4").output_model is MarketResearcherOutputV2


def test_every_contract_has_a_prompt_file_and_every_prompt_has_a_contract() -> None:
    for agent in (IdeaGenerator, MarketResearcher):
        for version in agent.output_contracts:
            assert prompt_exists(agent.implementation_key, version), (agent, version)
    assert prompt_exists("idea_generator", "v2")
    assert prompt_exists("market_researcher", "v4")
    assert not prompt_exists("idea_generator", "v3")
    assert not prompt_exists("market_researcher", "v5")


@pytest.mark.parametrize(
    ("agent", "version"), [(IdeaGenerator, "v3"), (MarketResearcher, "v5"), (IdeaGenerator, "")]
)
def test_unknown_prompt_version_has_no_contract(agent: Any, version: str) -> None:
    with pytest.raises(DomainValidationError, match="no output contract"):
        agent.contract_for(version)


def _claims(n: int) -> list[dict[str, Any]]:
    return [{"id": f"c{i}", "text": "推測", "kind": "speculation"} for i in range(n)]


def _idea_output(claims: int) -> dict[str, Any]:
    return {"summary": "s", "ideas": [{"title": "t"}], "claims": _claims(claims)}


def _market_output(claims: int) -> dict[str, Any]:
    return {"summary": "s", "market_overview": "m", "claims": _claims(claims)}


def test_contract_v2_allows_at_most_ten_claims() -> None:
    IdeaGeneratorOutputV2.model_validate(_idea_output(10))
    MarketResearcherOutputV2.model_validate(_market_output(10))
    with pytest.raises(ValidationError):
        IdeaGeneratorOutputV2.model_validate(_idea_output(11))
    with pytest.raises(ValidationError):
        MarketResearcherOutputV2.model_validate(_market_output(11))


def test_contract_v1_models_are_unchanged() -> None:
    """v1 は claims の上限を持たない（これまでどおり）。"""
    IdeaGeneratorOutput.model_validate(_idea_output(25))
    MarketResearcherOutput.model_validate(_market_output(25))
    assert "maxItems" not in IdeaGeneratorOutput.model_json_schema()["properties"]["claims"]
    assert "maxItems" not in MarketResearcherOutput.model_json_schema()["properties"]["claims"]
    # ideas の件数（1〜20）は v1・v2 で同じ
    for model in (IdeaGeneratorOutput, IdeaGeneratorOutputV2):
        ideas = model.model_json_schema()["properties"]["ideas"]
        assert (ideas["minItems"], ideas["maxItems"]) == (1, 20)


def test_contract_v2_schema_sent_to_the_llm() -> None:
    idea = output_schema_for(IdeaGeneratorOutputV2, [])
    assert idea["properties"]["claims"]["maxItems"] == 10
    market = output_schema_for(MarketResearcherOutputV2, [])
    assert market["properties"]["claims"]["maxItems"] == 10
    # v1 のスキーマには上限がない
    assert "maxItems" not in output_schema_for(MarketResearcherOutput, [])["properties"]["claims"]
