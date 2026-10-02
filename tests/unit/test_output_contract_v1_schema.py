"""出力契約 v1 の LLM に送るスキーマは、v2 の導入前（main a17e6b2）から変えない（第2回仕様 17章）。

data/output_contract_v1_schemas.json は、a17e6b2 のコードで作ったスキーマ。
次の3段階で一致を確かめる。
1. output_schema_for(...)
2. anthropic.transform_schema(...) の後（API に送る形）
3. Claude のクライアントが実際に送る output_config
   （SDK を Fake に差し替える。実際の API には接続しない）
"""

import json
import uuid
from pathlib import Path
from typing import Any

import anthropic
import pytest
from pydantic import SecretStr

from ai_business_explorer.agents.base import (
    Agent,
    AgentContext,
    EvidenceView,
    ExplorationView,
    IdeaView,
    output_schema_for,
)
from ai_business_explorer.agents.employees.idea_generator import (
    IdeaGenerator,
    IdeaGeneratorOutput,
)
from ai_business_explorer.agents.employees.market_researcher import (
    MarketResearcher,
    MarketResearcherOutput,
)
from ai_business_explorer.llm.claude import ClaudeLLMClient
from ai_business_explorer.prompts.loader import load_prompt
from ai_business_explorer.tools.base import ToolBox, ToolContext, ToolRegistry
from tests.fake_claude import FAKE_KEY, FakeClaudeSDK, FakeReply, idea_reply

SNAPSHOT = json.loads(
    (Path(__file__).parent / "data" / "output_contract_v1_schemas.json").read_text()
)
EVIDENCE_IDS = [uuid.UUID(i) for i in SNAPSHOT["evidence_ids"]]
CASES = {
    "idea_generator_evidence0": (IdeaGeneratorOutput, []),
    "market_researcher_evidence0": (MarketResearcherOutput, []),
    "market_researcher_evidence2": (MarketResearcherOutput, EVIDENCE_IDS),
}


def test_snapshot_is_from_main() -> None:
    assert SNAPSHOT["source"] == "main a17e6b2"
    assert set(SNAPSHOT["output_schema_for"]) == set(CASES)


@pytest.mark.parametrize("case", sorted(CASES))
def test_output_schema_for_is_unchanged(case: str) -> None:
    model, ids = CASES[case]
    assert output_schema_for(model, ids) == SNAPSHOT["output_schema_for"][case]


@pytest.mark.parametrize("case", sorted(CASES))
def test_transformed_schema_is_unchanged(case: str) -> None:
    model, ids = CASES[case]
    sent = anthropic.transform_schema(output_schema_for(model, ids))
    assert sent == SNAPSHOT["transform_schema"][case]


def _ctx(sdk: FakeClaudeSDK, prompt: tuple[str, str], evidence: list[EvidenceView]) -> AgentContext:
    exploration = ExplorationView(id=uuid.uuid4(), title="t", theme="t", description=None)
    return AgentContext(
        exploration=exploration,
        idea=IdeaView(
            id=uuid.uuid4(),
            title="i",
            summary=None,
            problem=None,
            target_customer=None,
            target_market=None,
            revenue_model=None,
        ),
        evidence=evidence,
        prior_analyses=[],
        research_question=None,
        llm=ClaudeLLMClient(SecretStr(FAKE_KEY), client_factory=sdk.factory),
        llm_model="claude-opus-5-5",
        tools=ToolBox(
            ToolRegistry(),
            [],
            [],
            ToolContext(execution_id=uuid.uuid4(), exploration_id=exploration.id),
        ),
        prompt=load_prompt(*prompt),
    )


def _market_reply() -> FakeReply:
    body = {"summary": "s", "market_overview": "m", "claims": []}
    return FakeReply(text=json.dumps(body))


def _evidence() -> list[EvidenceView]:
    return [
        EvidenceView(id=i, source_type="human_input", title="e", url=None, quote="q", summary=None)
        for i in EVIDENCE_IDS
    ]


V1_RUNS: list[tuple[type[Agent], tuple[str, str], str, bool]] = [
    (IdeaGenerator, ("idea_generator", "v1"), "idea_generator_evidence0", False),
    (MarketResearcher, ("market_researcher", "v1"), "market_researcher_evidence0", False),
    (MarketResearcher, ("market_researcher", "v2"), "market_researcher_evidence2", True),
    (MarketResearcher, ("market_researcher", "v3"), "market_researcher_evidence2", True),
    (MarketResearcher, ("market_researcher", "v3"), "market_researcher_evidence0", False),
]


@pytest.mark.parametrize(("agent", "prompt", "case", "with_evidence"), V1_RUNS)
def test_claude_client_sends_the_unchanged_v1_schema(
    agent: type[Agent], prompt: tuple[str, str], case: str, with_evidence: bool
) -> None:
    reply: Any = idea_reply() if agent is IdeaGenerator else _market_reply()
    sdk = FakeClaudeSDK(replies=[reply])
    agent().run(_ctx(sdk, prompt, _evidence() if with_evidence else []))
    [request] = sdk.requests
    assert request["output_config"] == {
        "format": {"type": "json_schema", "schema": SNAPSHOT["transform_schema"][case]}
    }
