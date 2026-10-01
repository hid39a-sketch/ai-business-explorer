"""MarketResearcher（Fake）: 採用済み Idea と Evidence から市場調査の分析を作成する。"""

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
    parse_json_object,
)
from ai_business_explorer.llm.base import LLMMessage, LLMRequest


class MarketResearcherInput(BaseModel):
    idea: IdeaView
    evidence: list[EvidenceView]
    prior_analyses: list[AnalysisView]
    research_question: str | None = None


class MarketResearcherOutput(BaseModel):
    summary: str
    market_overview: str
    claims: list[Claim] = Field(default_factory=list)


class MarketResearcher(Agent):
    implementation_key = "market_researcher"
    stage_key = "market_research"
    output_schema_version = "market_research.v1"
    input_model = MarketResearcherInput
    output_model = MarketResearcherOutput

    def run(self, ctx: AgentContext) -> AnalysisDraft:
        if ctx.idea is None:
            raise AgentOutputError("market_research requires an idea")
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
                response_schema=MarketResearcherOutput.model_json_schema(),
            )
        )
        raw = (
            response.structured
            if response.structured is not None
            else parse_json_object(response.text)
        )
        output = MarketResearcherOutput.model_validate(raw)
        return AnalysisDraft(
            summary=output.summary,
            claims=output.claims,
            data={"market_overview": output.market_overview},
        )
