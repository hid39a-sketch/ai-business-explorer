"""API レスポンスのスキーマ。DB モデルとは分離する。

日時はすべて UTC（末尾 Z）で返す（第2回仕様 15章 Q1）。
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer


def _utc_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UTCDateTime = Annotated[datetime, PlainSerializer(_utc_z, return_type=str)]


class PageOut[T](BaseModel):
    """一覧の応答（カーソル方式）。総件数は返さない。"""

    items: list[T]
    next_cursor: str | None
    has_more: bool


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class ActorOut(_Out):
    id: UUID
    actor_type: str
    display_name: str
    created_at: UTCDateTime


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
    created_at: UTCDateTime
    updated_at: UTCDateTime


class ExplorationOut(_Out):
    id: UUID
    organization_id: UUID
    title: str
    theme: str
    description: str | None
    status: str
    created_by_actor_id: UUID
    created_at: UTCDateTime
    updated_at: UTCDateTime


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
    created_at: UTCDateTime
    updated_at: UTCDateTime
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
    published_at: UTCDateTime | None
    retrieved_at: UTCDateTime | None
    content_hash: str
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    created_by_actor_id: UUID
    retracted_at: UTCDateTime | None
    retraction_reason: str | None
    source_key: str | None
    snapshot_hash: str | None
    supersedes_evidence_id: UUID | None
    content_purged_at: UTCDateTime | None
    purge_reason: str | None
    purged_by_actor_id: UUID | None
    created_at: UTCDateTime
    # 状態（取得時に算出。優先順位 purged > retracted > superseded > active）と、元の各状態
    evidence_status: str = "active"
    is_retracted: bool = False
    is_superseded: bool = False
    is_purged: bool = False
    superseded_by_id: UUID | None = None


class EvidenceWarningOut(BaseModel):
    code: str  # duplicate
    evidence_id: UUID


class EvidenceCreatedOut(EvidenceOut):
    """登録の応答。重複は拒否せず warnings で知らせる（Q4）。"""

    warnings: list[EvidenceWarningOut] = Field(default_factory=list)


class StageAssignmentOut(_Out):
    id: UUID
    organization_id: UUID
    stage_key: str
    ai_employee_id: UUID
    role: str
    created_at: UTCDateTime


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
    # primary（ステージの状態を決める）/ secondary（追加の視点）
    assignment_role: str
    started_at: UTCDateTime | None
    finished_at: UTCDateTime | None
    created_at: UTCDateTime


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
    superseded_at: UTCDateTime | None
    # 受け付けた時刻（並び順に使う）。ワーカーが取り出した時刻は claimed_at
    started_at: UTCDateTime
    claimed_at: UTCDateTime | None
    worker_id: str | None
    heartbeat_at: UTCDateTime | None
    finished_at: UTCDateTime | None


class StageRunDetailOut(StageRunOut):
    executions: list[ExecutionOut] = Field(default_factory=list)


class AnalysisOut(_Out):
    id: UUID
    organization_id: UUID
    exploration_id: UUID
    idea_id: UUID | None
    stage_run_id: UUID
    execution_id: UUID
    ai_employee_id: UUID
    stage_key: str
    schema_version: str
    version_no: int
    supersedes_id: UUID | None
    summary: str
    body: dict[str, Any]
    review_status: str
    created_at: UTCDateTime


class EvidenceLinkOut(BaseModel):
    """主張と Evidence の関係。claim_ref は AI が付けた主張のID（claims.claim_key）。"""

    claim_id: UUID
    claim_ref: str
    evidence_id: UUID
    relation: str
    # 参照先 Evidence の現在の状態（リンク自体は書き換えない。E-01）
    evidence_status: str
    evidence_is_retracted: bool
    evidence_is_superseded: bool
    evidence_is_purged: bool
    evidence_superseded_by_id: UUID | None


class HumanReviewOut(_Out):
    id: UUID
    organization_id: UUID
    analysis_id: UUID
    claim_id: UUID | None
    exploration_id: UUID
    idea_id: UUID | None
    reviewer_actor_id: UUID
    decision: str
    comment: str | None
    corrections: dict[str, Any] | None
    created_at: UTCDateTime


class ClaimOut(BaseModel):
    id: UUID
    claim_key: str
    ordinal: int
    kind: str
    text: str
    evidence_links: list[EvidenceLinkOut]
    latest_review: HumanReviewOut | None


class AnalysisDetailOut(AnalysisOut):
    """主張と根拠は claims / claim_evidence_links（正本）から返す。

    body は生成時点のAI出力スナップショット。
    """

    claims: list[ClaimOut]
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
    created_at: UTCDateTime


class StageOut(BaseModel):
    key: str
    order: int
    label: str
    scope: str
    executable_by_ai: bool
