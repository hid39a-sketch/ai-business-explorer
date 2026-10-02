"""IdeaGenerator（Fake）: 探索案件のテーマから Idea 候補を生成する。"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import ClassVar

from pydantic import BaseModel, Field

from ai_business_explorer.agents.base import (
    Agent,
    AgentContext,
    AgentOutputError,
    AnalysisDraft,
    Claim,
    ExplorationView,
    IdeaCandidate,
    OutputContract,
    output_schema_for,
    parse_json_object,
)
from ai_business_explorer.domain.stages import IDEA_GENERATION
from ai_business_explorer.llm.base import LLMMessage, LLMRequest


class IdeaGeneratorInput(BaseModel):
    exploration: ExplorationView
    research_question: str | None = None


class IdeaGeneratorOutput(BaseModel):
    """出力契約 v1（Prompt v1）。変更しない。"""

    summary: str
    ideas: list[IdeaCandidate] = Field(min_length=1, max_length=20)
    claims: list[Claim] = Field(default_factory=list)


class IdeaGeneratorOutputV2(BaseModel):
    """出力契約 v2（Prompt v2。第2回仕様 17章）。claims は最大10件。"""

    summary: str
    ideas: list[IdeaCandidate] = Field(min_length=1, max_length=20)
    claims: list[Claim] = Field(default_factory=list, max_length=10)


class IdeaGenerator(Agent):
    implementation_key = "idea_generator"
    stage_key = IDEA_GENERATION
    output_schema_version = "idea_generation.v1"
    input_model = IdeaGeneratorInput
    output_model = IdeaGeneratorOutput
    output_contracts: ClassVar[Mapping[str, OutputContract]] = MappingProxyType(
        {
            "v1": OutputContract("idea_generation.v1", IdeaGeneratorOutput),
            "v2": OutputContract("idea_generation.v2", IdeaGeneratorOutputV2),
        }
    )

    def run(self, ctx: AgentContext) -> AnalysisDraft:
        output_model = self.contract_for(ctx.prompt.version).output_model
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
                # Evidence を受け取らないので、Evidence 0件のスキーマ（evidence_based を出せない）
                response_schema=output_schema_for(output_model, []),
            )
        )
        raw = (
            response.structured
            if response.structured is not None
            else parse_json_object(response.text)
        )
        output = output_model.model_validate(raw)
        if not isinstance(output, IdeaGeneratorOutput | IdeaGeneratorOutputV2):
            raise AgentOutputError(f"unexpected output model: {type(output).__name__}")
        return AnalysisDraft(
            summary=output.summary,
            claims=output.claims,
            data={"ideas": [i.model_dump() for i in output.ideas]},
            idea_candidates=output.ideas,
        )
