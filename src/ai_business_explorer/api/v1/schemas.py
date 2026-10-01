"""API レスポンスのスキーマ。DB モデルとは分離する。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ActorOut(_Out):
    id: UUID
    actor_type: str
    display_name: str
    created_at: datetime


class AIEmployeeOut(_Out):
    id: UUID
    organization_id: UUID
    key: str
    name: str
    role: str
    description: str | None
    purpose: str | None
    stage_key: str
    implementation_key: str | None
    llm_config: dict[str, Any]
    allowed_tools: list[str]
    input_format: dict[str, Any] | None
    output_format: dict[str, Any] | None
    prompt_key: str | None
    prompt_version: str | None
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class ExplorationOut(_Out):
    id: UUID
    organization_id: UUID
    title: str
    theme: str
    description: str | None
    status: str
    created_by_actor_id: UUID
    created_at: datetime
    updated_at: datetime


class ResearchStatusOut(BaseModel):
    current_stage_key: str | None
    latest_stage_key: str | None
    latest_stage_run_status: str | None


class IdeaOut(_Out):
    id: UUID
    organization_id: UUID
    exploration_id: UUID
    title: str
    summary: str | None
    problem: str | None
    target_customer: str | None
    target_market: str | None
    revenue_model: str | None
    required_technology: str | None
    required_data: str | None
    competitor_info: str | None
    ip_info: str | None
    legal_regulatory_info: str | None
    initial_cost: str | None
    running_cost: str | None
    time_to_revenue: str | None
    scalability: str | None
    imitability: str | None
    ai_advantage: str | None
    origin_type: str
    origin_analysis_id: UUID | None
    adoption_status: str
    current_stage_key: str | None
    created_by_actor_id: UUID | None
    created_at: datetime
    updated_at: datetime
    research_status: ResearchStatusOut | None = None


class EvidenceOut(_Out):
    id: UUID
    organization_id: UUID
    exploration_id: UUID
    idea_id: UUID | None
    source_type: str
    title: str
    url: str | None
    quote: str | None
    summary: str | None
    published_at: datetime | None
    retrieved_at: datetime | None
    content_hash: str
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    created_by_actor_id: UUID
    retracted_at: datetime | None
    retraction_reason: str | None
    created_at: datetime


class ExecutionOut(_Out):
    id: UUID
    organization_id: UUID
    stage_run_id: UUID
    ai_employee_id: UUID
    idea_id: UUID | None
    ai_employee_version: int
    ai_employee_snapshot: dict[str, Any]
    implementation_key: str
    prompt_key: str | None
    prompt_version: str | None
    prompt_hash: str | None
    llm_provider: str | None
    llm_model: str | None
    code_version: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    error_type: str | None
    error_message: str | None
    error_detail: dict[str, Any] | None
    usage: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None
    created_at: datetime


class StageRunOut(_Out):
    id: UUID
    organization_id: UUID
    exploration_id: UUID
    idea_id: UUID | None
    stage_key: str
    attempt_no: int
    trigger: str
    rerun_of_id: UUID | None
    sent_back_from_id: UUID | None
    reason: str | None
    research_question: str | None
    triggered_by_actor_id: UUID
    status: str
    input_snapshot: dict[str, Any]
    superseded_at: datetime | None
    started_at: datetime
    finished_at: datetime | None


class StageRunDetailOut(StageRunOut):
    executions: list[ExecutionOut] = Field(default_factory=list)


class AnalysisOut(_Out):
    id: UUID
    organization_id: UUID
    exploration_id: UUID
    idea_id: UUID | None
    stage_run_id: UUID
    execution_id: UUID
    stage_key: str
    schema_version: str
    version_no: int
    supersedes_id: UUID | None
    summary: str
    body: dict[str, Any]
    review_status: str
    created_at: datetime


class EvidenceLinkOut(_Out):
    evidence_id: UUID
    claim_ref: str
    relation: str


class HumanReviewOut(_Out):
    id: UUID
    organization_id: UUID
    analysis_id: UUID
    exploration_id: UUID
    idea_id: UUID | None
    reviewer_actor_id: UUID
    decision: str
    comment: str | None
    corrections: dict[str, Any] | None
    created_at: datetime


class AnalysisDetailOut(AnalysisOut):
    evidence_links: list[EvidenceLinkOut]
    human_reviews: list[HumanReviewOut]


class HumanDecisionOut(_Out):
    id: UUID
    organization_id: UUID
    idea_id: UUID
    decided_by_actor_id: UUID
    decision: str
    rationale: str
    based_on_review_ids: list[UUID]
    created_at: datetime


class StageOut(BaseModel):
    key: str
    order: int
    label: str
    scope: str
    executable_by_ai: bool
