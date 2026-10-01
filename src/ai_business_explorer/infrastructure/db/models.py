"""DB モデル。

分離の原則:
- evidence        : 外部情報・人間の入力。AI 生成情報（ai_generated）は CHECK 制約で拒否。
- analyses        : AI の分析。必ず executions に紐づく。本体（body）は不変。
- human_reviews   : 人間のみ（複合 FK + CHECK actor_type = 'human'）。追記のみ。
- human_decisions : 人間のみ（同上）。追記のみ。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_business_explorer.domain.enums import (
    ActorType,
    AdoptionStatus,
    AIEmployeeStatus,
    ErrorType,
    EvidenceRelation,
    EvidenceSourceType,
    ExplorationStatus,
    HumanDecisionValue,
    OriginType,
    ReviewDecision,
    ReviewStatus,
    RunStatus,
    StageRunTrigger,
    sql_in,
)
from ai_business_explorer.domain.stages import IDEA_GENERATION
from ai_business_explorer.infrastructure.db.base import (
    Base,
    CreatedAtMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

HUMAN = ActorType.HUMAN.value


class Actor(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """操作者。第1回は X-Actor-Id ヘッダで指定する。

    将来は external_ref を認証基盤の主体に対応付ける。
    """

    __tablename__ = "actors"
    __table_args__ = (
        CheckConstraint(f"actor_type IN ({sql_in(ActorType)})", name="actor_type"),
        # human 限定の複合 FK の参照先
        UniqueConstraint("id", "actor_type", name="uq_actors_id_actor_type"),
    )

    actor_type: Mapped[str] = mapped_column(String(16))
    display_name: Mapped[str] = mapped_column(String(200))
    external_ref: Mapped[str | None] = mapped_column(String(200), unique=True)


class AIEmployee(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """AI社員の定義（正本）。更新のたびに version が増える。"""

    __tablename__ = "ai_employees"
    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(AIEmployeeStatus)})", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    stage_key: Mapped[str] = mapped_column(String(64))
    implementation_key: Mapped[str | None] = mapped_column(String(64))
    llm_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    allowed_tools: Mapped[list[str]] = mapped_column(JSONB, default=list)
    input_format: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_format: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    prompt_key: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=AIEmployeeStatus.DRAFT.value)
    version: Mapped[int] = mapped_column(Integer, default=1)


class Exploration(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "explorations"
    __table_args__ = (CheckConstraint(f"status IN ({sql_in(ExplorationStatus)})", name="status"),)

    title: Mapped[str] = mapped_column(String(200))
    theme: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=ExplorationStatus.ACTIVE.value)
    created_by_actor_id: Mapped[UUID] = mapped_column(ForeignKey("actors.id"))


class Idea(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """事業アイデア。詳細項目は人間のみが更新する（AI の調査結果は analyses に残す）。"""

    __tablename__ = "ideas"
    __table_args__ = (
        CheckConstraint(f"origin_type IN ({sql_in(OriginType)})", name="origin_type"),
        CheckConstraint(f"adoption_status IN ({sql_in(AdoptionStatus)})", name="adoption_status"),
        CheckConstraint(
            "(origin_type = 'ai') = (origin_analysis_id IS NOT NULL)", name="origin_analysis"
        ),
    )

    exploration_id: Mapped[UUID] = mapped_column(ForeignKey("explorations.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    problem: Mapped[str | None] = mapped_column(Text)
    target_customer: Mapped[str | None] = mapped_column(Text)
    target_market: Mapped[str | None] = mapped_column(Text)
    revenue_model: Mapped[str | None] = mapped_column(Text)
    required_technology: Mapped[str | None] = mapped_column(Text)
    required_data: Mapped[str | None] = mapped_column(Text)
    competitor_info: Mapped[str | None] = mapped_column(Text)
    ip_info: Mapped[str | None] = mapped_column(Text)
    legal_regulatory_info: Mapped[str | None] = mapped_column(Text)
    initial_cost: Mapped[str | None] = mapped_column(Text)
    running_cost: Mapped[str | None] = mapped_column(Text)
    time_to_revenue: Mapped[str | None] = mapped_column(Text)
    scalability: Mapped[str | None] = mapped_column(Text)
    imitability: Mapped[str | None] = mapped_column(Text)
    ai_advantage: Mapped[str | None] = mapped_column(Text)
    origin_type: Mapped[str] = mapped_column(String(16))
    origin_analysis_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analyses.id", use_alter=True)
    )
    adoption_status: Mapped[str] = mapped_column(String(16), default=AdoptionStatus.CANDIDATE.value)
    current_stage_key: Mapped[str | None] = mapped_column(String(64))
    created_by_actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("actors.id"))


class StageRun(UUIDPrimaryKeyMixin, Base):
    """ステージ実行の1試行。

    再実行・差し戻しでは新しい行を作り、古い行に superseded_at を記録する。
    """

    __tablename__ = "stage_runs"
    __table_args__ = (
        CheckConstraint(f"trigger IN ({sql_in(StageRunTrigger)})", name="trigger"),
        CheckConstraint(f"status IN ({sql_in(RunStatus)})", name="status"),
        CheckConstraint(
            f"(stage_key = '{IDEA_GENERATION}') = (idea_id IS NULL)", name="scope_matches_stage"
        ),
        CheckConstraint(f"triggered_by_actor_type = '{HUMAN}'", name="triggered_by_human"),
        CheckConstraint("attempt_no >= 1", name="attempt_positive"),
        ForeignKeyConstraint(
            ["triggered_by_actor_id", "triggered_by_actor_type"],
            ["actors.id", "actors.actor_type"],
            name="fk_stage_runs_triggered_by_human",
        ),
        Index(
            "uq_stage_runs_exploration_attempt",
            "exploration_id",
            "stage_key",
            "attempt_no",
            unique=True,
            postgresql_where=text("idea_id IS NULL"),
        ),
        Index(
            "uq_stage_runs_idea_attempt",
            "idea_id",
            "stage_key",
            "attempt_no",
            unique=True,
            postgresql_where=text("idea_id IS NOT NULL"),
        ),
        # 1つのスコープ・ステージで「最新（未 supersede）」の試行は高々1つ。
        Index(
            "uq_stage_runs_exploration_current",
            "exploration_id",
            "stage_key",
            unique=True,
            postgresql_where=text("idea_id IS NULL AND superseded_at IS NULL"),
        ),
        Index(
            "uq_stage_runs_idea_current",
            "idea_id",
            "stage_key",
            unique=True,
            postgresql_where=text("idea_id IS NOT NULL AND superseded_at IS NULL"),
        ),
    )

    exploration_id: Mapped[UUID] = mapped_column(ForeignKey("explorations.id"), index=True)
    idea_id: Mapped[UUID | None] = mapped_column(ForeignKey("ideas.id"), index=True)
    stage_key: Mapped[str] = mapped_column(String(64))
    attempt_no: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(16))
    rerun_of_id: Mapped[UUID | None] = mapped_column(ForeignKey("stage_runs.id"))
    sent_back_from_id: Mapped[UUID | None] = mapped_column(ForeignKey("stage_runs.id"))
    reason: Mapped[str | None] = mapped_column(Text)
    research_question: Mapped[str | None] = mapped_column(Text)
    triggered_by_actor_id: Mapped[UUID] = mapped_column()
    triggered_by_actor_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.RUNNING.value)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Execution(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """AI社員の実行履歴。実行時点の AI社員定義・Prompt・LLM・コードのバージョンを保存する。"""

    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(RunStatus)})", name="status"),
        CheckConstraint(
            f"error_type IS NULL OR error_type IN ({sql_in(ErrorType)})", name="error_type"
        ),
    )

    stage_run_id: Mapped[UUID] = mapped_column(ForeignKey("stage_runs.id"), index=True)
    ai_employee_id: Mapped[UUID] = mapped_column(ForeignKey("ai_employees.id"), index=True)
    idea_id: Mapped[UUID | None] = mapped_column(ForeignKey("ideas.id"), index=True)
    ai_employee_version: Mapped[int] = mapped_column(Integer)
    ai_employee_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    implementation_key: Mapped[str] = mapped_column(String(64))
    prompt_key: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(16))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    llm_provider: Mapped[str | None] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    code_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.RUNNING.value)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_type: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Analysis(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """AI Analysis。Evidence ではない。

    本体は不変で、review_status のみ ReviewService が更新する。
    """

    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(f"review_status IN ({sql_in(ReviewStatus)})", name="review_status"),
        CheckConstraint("version_no >= 1", name="version_positive"),
    )

    exploration_id: Mapped[UUID] = mapped_column(ForeignKey("explorations.id"), index=True)
    idea_id: Mapped[UUID | None] = mapped_column(ForeignKey("ideas.id"), index=True)
    stage_run_id: Mapped[UUID] = mapped_column(ForeignKey("stage_runs.id"), index=True)
    execution_id: Mapped[UUID] = mapped_column(ForeignKey("executions.id"), index=True)
    stage_key: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    version_no: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[UUID | None] = mapped_column(ForeignKey("analyses.id"))
    summary: Mapped[str] = mapped_column(Text)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    review_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.PENDING_REVIEW.value
    )


class Evidence(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """根拠・出典。外部情報または人間の入力のみ。不変（訂正は撤回＋新規登録）。"""

    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint(f"source_type IN ({sql_in(EvidenceSourceType)})", name="source_type"),
        CheckConstraint(
            "(retracted_at IS NULL) = (retraction_reason IS NULL)", name="retraction_reason"
        ),
    )

    exploration_id: Mapped[UUID] = mapped_column(ForeignKey("explorations.id"), index=True)
    idea_id: Mapped[UUID | None] = mapped_column(ForeignKey("ideas.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(String(2000))
    quote: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    # PostgreSQL 予約名との衝突を避けるため属性名は metadata_
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_by_actor_id: Mapped[UUID] = mapped_column(ForeignKey("actors.id"))
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retraction_reason: Mapped[str | None] = mapped_column(Text)


class AnalysisEvidenceLink(Base):
    __tablename__ = "analysis_evidence_links"
    __table_args__ = (
        CheckConstraint(f"relation IN ({sql_in(EvidenceRelation)})", name="relation"),
    )

    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("analyses.id"), primary_key=True)
    evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence.id"), primary_key=True, index=True
    )
    claim_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    relation: Mapped[str] = mapped_column(String(16))


class HumanReview(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """人間によるレビュー。人間以外の actor では DB レベルで挿入できない。追記のみ。"""

    __tablename__ = "human_reviews"
    __table_args__ = (
        CheckConstraint(f"decision IN ({sql_in(ReviewDecision)})", name="decision"),
        CheckConstraint(f"reviewer_actor_type = '{HUMAN}'", name="reviewer_is_human"),
        ForeignKeyConstraint(
            ["reviewer_actor_id", "reviewer_actor_type"],
            ["actors.id", "actors.actor_type"],
            name="fk_human_reviews_reviewer_human",
        ),
    )

    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("analyses.id"), index=True)
    exploration_id: Mapped[UUID] = mapped_column(ForeignKey("explorations.id"))
    idea_id: Mapped[UUID | None] = mapped_column(ForeignKey("ideas.id"), index=True)
    reviewer_actor_id: Mapped[UUID] = mapped_column()
    reviewer_actor_type: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(32))
    comment: Mapped[str | None] = mapped_column(Text)
    corrections: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class HumanDecision(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """人間による最終的な事業判断。AI から書き込む経路はない。追記のみ。"""

    __tablename__ = "human_decisions"
    __table_args__ = (
        CheckConstraint(f"decision IN ({sql_in(HumanDecisionValue)})", name="decision"),
        CheckConstraint(f"decided_by_actor_type = '{HUMAN}'", name="decided_by_human"),
        ForeignKeyConstraint(
            ["decided_by_actor_id", "decided_by_actor_type"],
            ["actors.id", "actors.actor_type"],
            name="fk_human_decisions_decided_by_human",
        ),
    )

    idea_id: Mapped[UUID] = mapped_column(ForeignKey("ideas.id"), index=True)
    decided_by_actor_id: Mapped[UUID] = mapped_column()
    decided_by_actor_type: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(16))
    rationale: Mapped[str] = mapped_column(Text)
    based_on_review_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)


class AuditEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """監査ログ。変更可能なエンティティの更新・状態遷移・レビュー・決定・差し戻しを記録する。"""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_entity", "entity_type", "entity_id"),)

    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[UUID] = mapped_column()
    action: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("actors.id"))
    execution_id: Mapped[UUID | None] = mapped_column(ForeignKey("executions.id"))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
