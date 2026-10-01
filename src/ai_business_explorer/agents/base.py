"""AI社員（AI Employee）のコード側の抽象。

- AI社員の「定義」（名前・役割・使用モデル・使用ツール・Prompt など）は DB の ai_employees が正本。
- コード側は「実装」を提供し、ai_employees.implementation_key で対応付けられる。
- AI社員は AgentContext だけを受け取る。DB・Repository・サービスには触れられないため、
  Human Review / Human Decision / 再実行 / 差し戻し / Idea の採否を起動する経路を持たない。
"""

import json
from abc import ABC, abstractmethod
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_business_explorer.domain.enums import ClaimKind, EvidenceRelation
from ai_business_explorer.llm.base import LLMClient
from ai_business_explorer.prompts.loader import PromptTemplate
from ai_business_explorer.tools.base import ToolBox


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ExplorationView(_Frozen):
    id: UUID
    title: str
    theme: str
    description: str | None


class IdeaView(_Frozen):
    id: UUID
    title: str
    summary: str | None
    problem: str | None
    target_customer: str | None
    target_market: str | None
    revenue_model: str | None


class EvidenceView(_Frozen):
    id: UUID
    source_type: str
    title: str
    url: str | None
    quote: str | None
    summary: str | None


class AnalysisView(_Frozen):
    id: UUID
    stage_key: str
    summary: str


class EvidenceRef(BaseModel):
    evidence_id: UUID
    relation: EvidenceRelation = EvidenceRelation.SUPPORTS


class Claim(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1)
    kind: ClaimKind
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_based_requires_refs(self) -> "Claim":
        if self.kind is ClaimKind.EVIDENCE_BASED and not self.evidence_refs:
            raise ValueError(f"claim '{self.id}' is evidence_based but has no evidence_refs")
        return self


class IdeaCandidate(BaseModel):
    """AI が生成する Idea 候補。AI が書けるのは title / summary / problem のみ。"""

    title: str = Field(min_length=1, max_length=200)
    summary: str | None = None
    problem: str | None = None


class AnalysisDraft(BaseModel):
    summary: str = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    idea_candidates: list[IdeaCandidate] = Field(default_factory=list)


class AgentContext:
    """AI社員に渡す読み取り専用の実行コンテキスト。"""

    def __init__(
        self,
        *,
        exploration: ExplorationView,
        idea: IdeaView | None,
        evidence: list[EvidenceView],
        prior_analyses: list[AnalysisView],
        research_question: str | None,
        llm: LLMClient,
        llm_model: str,
        tools: ToolBox,
        prompt: PromptTemplate,
    ) -> None:
        self.exploration = exploration
        self.idea = idea
        self.evidence = tuple(evidence)
        self.prior_analyses = tuple(prior_analyses)
        self.research_question = research_question
        self.llm = llm
        self.llm_model = llm_model
        self.tools = tools
        self.prompt = prompt


class AgentOutputError(Exception):
    """AI社員の出力が出力スキーマや分離ルールに反する。"""


class Agent(ABC):
    implementation_key: ClassVar[str]
    stage_key: ClassVar[str]
    output_schema_version: ClassVar[str]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

    @abstractmethod
    def run(self, ctx: AgentContext) -> AnalysisDraft: ...


def parse_json_object(text: str) -> dict[str, Any]:
    """LLM のテキスト応答を JSON オブジェクトとして解釈する。"""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentOutputError("LLM response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AgentOutputError("LLM response must be a JSON object")
    return value
