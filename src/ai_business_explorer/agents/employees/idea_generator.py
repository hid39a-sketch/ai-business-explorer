"""IdeaGenerator（Fake）: 探索案件のテーマから Idea 候補を生成する。"""

from pydantic import BaseModel, Field

from ai_business_explorer.agents.base import (
    Agent,
    AgentContext,
    AnalysisDraft,
    Claim,
    ExplorationView,
    IdeaCandidate,
    parse_json_object,
)
from ai_business_explorer.domain.stages import IDEA_GENERATION
from ai_business_explorer.llm.base import LLMMessage, LLMRequest


class IdeaGeneratorInput(BaseModel):
    exploration: ExplorationView
    research_question: str | None = None


class IdeaGeneratorOutput(BaseModel):
    summary: str
    ideas: list[IdeaCandidate] = Field(min_length=1, max_length=20)
    claims: list[Claim] = Field(default_factory=list)


class IdeaGenerator(Agent):
    implementation_key = "idea_generator"
    stage_key = IDEA_GENERATION
    output_schema_version = "idea_generation.v1"
    input_model = IdeaGeneratorInput
    output_model = IdeaGeneratorOutput

    def run(self, ctx: AgentContext) -> AnalysisDraft:
        payload = IdeaGeneratorInput(
            exploration=ctx.exploration, research_question=ctx.research_question
        )
        response = ctx.llm.complete(
            LLMRequest(
                model=ctx.llm_model,
                system=ctx.prompt.text,
                messages=[LLMMessage(role="user", content=payload.model_dump_json())],
                prompt_key=ctx.prompt.key,
                prompt_version=ctx.prompt.version,
                response_schema=IdeaGeneratorOutput.model_json_schema(),
            )
        )
        raw = (
            response.structured
            if response.structured is not None
            else parse_json_object(response.text)
        )
        output = IdeaGeneratorOutput.model_validate(raw)
        return AnalysisDraft(
            summary=output.summary,
            claims=output.claims,
            data={"ideas": [i.model_dump() for i in output.ideas]},
            idea_candidates=output.ideas,
        )
