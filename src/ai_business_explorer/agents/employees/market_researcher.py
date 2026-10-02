"""MarketResearcher（Fake）: 採用済み Idea と Evidence から市場調査の分析を作成する。"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

from pydantic import BaseModel, Field

from ai_business_explorer.agents.base import (
    Agent,
    AgentContext,
    AgentOutputError,
    AnalysisDraft,
    AnalysisView,
    Claim,
    EvidenceView,
    IdeaView,
    OutputContract,
    output_schema_for,
    parse_json_object,
)
from ai_business_explorer.llm.base import LLMMessage, LLMRequest


class MarketResearcherInput(BaseModel):
    idea: IdeaView
    evidence: list[EvidenceView]
    prior_analyses: list[AnalysisView]
    research_question: str | None = None


# 出力契約 v1（Prompt market_researcher v1〜v3）。変更しない（第2回仕様 17章）。
# docstring は JSON Schema の description になり LLM に送られるので、注記はコメントに書く。
class MarketResearcherOutput(BaseModel):
    summary: str
    market_overview: str
    claims: list[Claim] = Field(default_factory=list)


# 出力契約 v2（Prompt market_researcher v4。第2回仕様 17章）。claims は最大10件。
class MarketResearcherOutputV2(BaseModel):
    summary: str
    market_overview: str
    claims: list[Claim] = Field(default_factory=list, max_length=10)


_V1 = OutputContract("market_research.v1", MarketResearcherOutput)


class MarketResearcher(Agent):
    implementation_key = "market_researcher"
    stage_key = "market_research"
    output_schema_version = "market_research.v1"
    input_model = MarketResearcherInput
    output_model = MarketResearcherOutput
    output_contracts: ClassVar[Mapping[str, OutputContract]] = MappingProxyType(
        {
            "v1": _V1,
            "v2": _V1,
            "v3": _V1,
            "v4": OutputContract("market_research.v2", MarketResearcherOutputV2),
        }
    )

    def run(self, ctx: AgentContext) -> AnalysisDraft:
        if ctx.idea is None:
            raise AgentOutputError("market_research requires an idea")
        output_model = self.contract_for(ctx.prompt.version).output_model
        payload = MarketResearcherInput(
            idea=ctx.idea,
            evidence=list(ctx.evidence),
            prior_analyses=list(ctx.prior_analyses),
            research_question=ctx.research_question,
        )
        response = ctx.llm.complete(
            LLMRequest(
                model=ctx.llm_model,
                system=ctx.prompt.text,
                messages=[LLMMessage(role="user", content=payload.model_dump_json())],
                prompt_key=ctx.prompt.key,
                prompt_version=ctx.prompt.version,
                # 入力した Evidence の ID だけを参照できる（0件なら evidence_based を出せない）
                response_schema=output_schema_for(output_model, [e.id for e in ctx.evidence]),
            )
        )
        raw = (
            response.structured
            if response.structured is not None
            else parse_json_object(response.text)
        )
        output = output_model.model_validate(raw)
        if not isinstance(output, MarketResearcherOutput | MarketResearcherOutputV2):
            raise AgentOutputError(f"unexpected output model: {type(output).__name__}")
        return AnalysisDraft(
            summary=output.summary,
            claims=output.claims,
            data={"market_overview": output.market_overview},
        )
