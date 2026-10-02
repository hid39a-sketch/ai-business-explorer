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
from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.domain.errors import DomainValidationError
from ai_business_explorer.prompts.loader import prompt_exists

# 正規の対応（第2回仕様 17章）：（prompt_key, prompt_version）→ 出力契約
CANONICAL = {
    (IdeaGenerator, "idea_generator", "v1"): "idea_generation.v1",
    (IdeaGenerator, "idea_generator", "v2"): "idea_generation.v2",
    (MarketResearcher, "market_researcher", "v1"): "market_research.v1",
    (MarketResearcher, "market_researcher", "v2"): "market_research.v1",
    (MarketResearcher, "market_researcher", "v3"): "market_research.v1",
    (MarketResearcher, "market_researcher", "v4"): "market_research.v2",
}


def test_prompt_key_and_version_to_contract_table() -> None:
    for (agent, key, version), schema_version in CANONICAL.items():
        assert agent.contract_for(key, version).schema_version == schema_version, (key, version)
    tables = {
        (agent.implementation_key, *pair): c.schema_version
        for agent in (IdeaGenerator, MarketResearcher)
        for pair, c in agent.output_contracts.items()
    }
    assert tables == {
        (agent.implementation_key, key, version): sv
        for (agent, key, version), sv in CANONICAL.items()
    }
    assert IdeaGenerator.contract_for("idea_generator", "v1") == OutputContract(
        "idea_generation.v1", IdeaGeneratorOutput
    )
    assert IdeaGenerator.contract_for("idea_generator", "v2") == OutputContract(
        "idea_generation.v2", IdeaGeneratorOutputV2
    )
    for version in ("v1", "v2", "v3"):
        contract = MarketResearcher.contract_for("market_researcher", version)
        assert contract.output_model is MarketResearcherOutput
    assert (
        MarketResearcher.contract_for("market_researcher", "v4").output_model
        is MarketResearcherOutputV2
    )


@pytest.mark.parametrize(
    ("agent", "key", "versions", "v1"),
    [
        (IdeaGenerator, "market_researcher", ("v1", "v2", "v3", "v4"), "idea_generation.v1"),
        (MarketResearcher, "idea_generator", ("v1", "v2"), "market_research.v1"),
    ],
)
def test_mismatched_prompt_key_is_v1_and_never_v2(
    agent: Any, key: str, versions: tuple[str, ...], v1: str
) -> None:
    """実装と prompt_key が一致しない既存の AI社員は、経過措置で v1 契約（17章 C1）。"""
    for version in versions:
        assert agent.contract_for(key, version).schema_version == v1, (key, version)


@pytest.mark.parametrize(
    ("agent", "key", "version"),
    [
        (IdeaGenerator, "idea_generator", "v3"),
        (MarketResearcher, "market_researcher", "v5"),
        (IdeaGenerator, "idea_generator", ""),
    ],
)
def test_unknown_version_of_the_matching_prompt_has_no_contract(
    agent: Any, key: str, version: str
) -> None:
    with pytest.raises(DomainValidationError, match="no output contract"):
        agent.contract_for(key, version)


def test_every_contract_has_a_prompt_file() -> None:
    for agent in (IdeaGenerator, MarketResearcher):
        for key, version in agent.output_contracts:
            assert key == agent.implementation_key
            assert prompt_exists(key, version), (key, version)
    assert not prompt_exists("idea_generator", "v3")
    assert not prompt_exists("market_researcher", "v5")


def test_production_agents_require_matching_prompt_keys() -> None:
    """本番のレジストリの実装はすべて出力契約の表を持つ（prompt_key の一致が必須になる）。"""
    registry = build_default_registry()
    implementation_keys = registry.keys()
    assert implementation_keys == ["idea_generator", "market_researcher"]
    for key in implementation_keys:
        agent = registry.get(key)
        assert agent is not None
        assert agent.requires_matching_prompt_key(), key
        legacy = agent.legacy_mismatch_contract
        assert legacy is not None
        assert legacy.schema_version.endswith(".v1"), key


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
