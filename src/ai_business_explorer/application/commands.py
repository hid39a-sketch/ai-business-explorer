"""サービスへの入力（コマンド）。API のリクエストボディとしても使う。"""

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_business_explorer.domain.enums import (
    AIEmployeeStatus,
    EvidenceSourceType,
    ExplorationStatus,
    HumanDecisionValue,
    ReviewDecision,
)

ShortText = Field(min_length=1, max_length=200)
LongText = Field(default=None, max_length=20_000)


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PartialUpdate(_Command):
    """部分更新。NOT NULL 項目への明示的な null を拒否する。"""

    non_nullable: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _reject_null_for_required(self) -> "_PartialUpdate":
        for field in self.model_fields_set & self.non_nullable:
            if getattr(self, field) is None:
                raise ValueError(f"'{field}' cannot be null")
        return self


class LLMConfig(_Command):
    provider: str = Field(default="fake", min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=128)


class AIEmployeeCreate(_Command):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    name: str = ShortText
    role: str = ShortText
    description: str | None = LongText
    purpose: str | None = LongText
    stage_key: str = Field(min_length=1, max_length=64)
    implementation_key: str | None = Field(default=None, max_length=64)
    llm_config: LLMConfig = Field(default_factory=LLMConfig)
    allowed_tools: list[str] = Field(default_factory=list, max_length=50)
    input_format: dict[str, Any] | None = None
    output_format: dict[str, Any] | None = None
    prompt_key: str | None = Field(default=None, max_length=64)
    prompt_version: str | None = Field(default=None, max_length=16)
    status: AIEmployeeStatus = AIEmployeeStatus.DRAFT


class AIEmployeeUpdate(_PartialUpdate):
    non_nullable = frozenset({"name", "role", "llm_config", "allowed_tools", "status"})

    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = LongText
    purpose: str | None = LongText
    implementation_key: str | None = Field(default=None, max_length=64)
    llm_config: LLMConfig | None = None
    allowed_tools: list[str] | None = Field(default=None, max_length=50)
    input_format: dict[str, Any] | None = None
    output_format: dict[str, Any] | None = None
    prompt_key: str | None = Field(default=None, max_length=64)
    prompt_version: str | None = Field(default=None, max_length=16)
    status: AIEmployeeStatus | None = None


class ExplorationCreate(_Command):
    title: str = ShortText
    theme: str = Field(min_length=1, max_length=2000)
    description: str | None = LongText


class ExplorationUpdate(_PartialUpdate):
    non_nullable = frozenset({"title", "theme", "status"})

    title: str | None = Field(default=None, min_length=1, max_length=200)
    theme: str | None = Field(default=None, min_length=1, max_length=2000)
    description: str | None = LongText
    status: ExplorationStatus | None = None


class IdeaProfile(_PartialUpdate):
    """Idea の詳細項目（仕様書9章）。すべて自由記述テキスト。スコアは持たない。"""

    summary: str | None = LongText
    problem: str | None = LongText
    target_customer: str | None = LongText
    target_market: str | None = LongText
    revenue_model: str | None = LongText
    required_technology: str | None = LongText
    required_data: str | None = LongText
    competitor_info: str | None = LongText
    ip_info: str | None = LongText
    legal_regulatory_info: str | None = LongText
    initial_cost: str | None = LongText
    running_cost: str | None = LongText
    time_to_revenue: str | None = LongText
    scalability: str | None = LongText
    imitability: str | None = LongText
    ai_advantage: str | None = LongText


class IdeaCreate(IdeaProfile):
    title: str = ShortText


class IdeaUpdate(IdeaProfile):
    non_nullable = frozenset({"title"})

    title: str | None = Field(default=None, min_length=1, max_length=200)


class IdeaAdoptionCommand(_Command):
    reason: str | None = LongText


class EvidenceCreate(_Command):
    exploration_id: UUID
    idea_id: UUID | None = None
    source_type: EvidenceSourceType
    title: str = Field(min_length=1, max_length=500)
    url: str | None = Field(default=None, max_length=2000)
    quote: str | None = LongText
    summary: str | None = LongText
    published_at: datetime | None = None
    retrieved_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _http_url_only(cls, value: str | None) -> str | None:
        if value is not None and not value.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return value


class EvidenceRetract(_Command):
    reason: str = Field(min_length=1, max_length=2000)


class EvidencePurge(_Command):
    reason: str = Field(min_length=1, max_length=2000)


class ExplorationStageRunCommand(_Command):
    """idea_generation ステージの実行。"""

    research_question: str | None = Field(default=None, max_length=2000)
    rerun_of_id: UUID | None = None
    ai_employee_id: UUID | None = None


class IdeaStageRunCommand(ExplorationStageRunCommand):
    stage_key: str = Field(min_length=1, max_length=64)


class SendBackCommand(_Command):
    to_stage_key: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)
    research_question: str | None = Field(default=None, max_length=2000)
    ai_employee_id: UUID | None = None


class HumanReviewCreate(_Command):
    """claim_id を指定すると主張単位のレビューになる（分析全体の review_status は変えない）。"""

    claim_id: UUID | None = None
    decision: ReviewDecision
    comment: str | None = LongText
    corrections: dict[str, Any] | None = None


class HumanDecisionCreate(_Command):
    decision: HumanDecisionValue
    rationale: str = Field(min_length=1, max_length=20_000)
    based_on_review_ids: list[UUID] = Field(default_factory=list, max_length=100)
