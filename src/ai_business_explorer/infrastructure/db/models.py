"""DB モデル。

組織:
- すべての業務テーブルに organization_id を持たせる（親から分かる場合も冗長に持つ）。
- 親子の組織の一致は複合 FK（子の (親ID, organization_id) → 親の (id, organization_id)）で保証する。
- organization_memberships のロールは人間のみ（複合 FK + CHECK actor_type = 'human'）。

分離の原則:
- evidence        : 外部情報・人間の入力。AI 生成情報（ai_generated）は CHECK 制約で拒否。
- analyses        : AI の分析。必ず executions に紐づく。本体（body）は不変。
- human_reviews   : 人間のみ（複合 FK + CHECK actor_type = 'human'）。追記のみ。
- human_decisions : 人間のみ（同上）。追記のみ。

主張と根拠（第2回 D-15）:
- claims / claim_evidence_links が正本。analyses.body.claims は生成時点のAI出力スナップショット。
- analysis_evidence_links（第1回）は移行後に凍結。DB トリガーで書き込みを拒否し、アプリも書かない。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_business_explorer.domain.enums import (
    ActorType,
    AdoptionStatus,
    AIEmployeeStatus,
    BudgetMode,
    CallStatus,
    ClaimKind,
    DataClassification,
    ErrorType,
    EvidenceRelation,
    EvidenceSourceType,
    ExplorationStatus,
    HumanDecisionValue,
    OrganizationRole,
    OriginType,
    PayloadMode,
    PricingKind,
    ReviewDecision,
    ReviewStatus,
    RunStatus,
    StageAssignmentRole,
    StageRunTrigger,
    sql_in,
)
from ai_business_explorer.domain.stages import HUMAN_REVIEW, IDEA_GENERATION
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


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """組織。第2回は既定組織1つで運用する（組織を作る API はない）。"""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200))


class OrganizationScopedMixin:
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)


class OrganizationMembership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """人間の組織への所属とロール。AI・system actor には DB レベルでロールを付けられない。

    第2回は 1 actor = 1 組織。
    """

    __tablename__ = "organization_memberships"
    __table_args__ = (
        CheckConstraint(f"role IN ({sql_in(OrganizationRole)})", name="role"),
        CheckConstraint(f"actor_type = '{HUMAN}'", name="member_is_human"),
        ForeignKeyConstraint(
            ["actor_id", "actor_type"],
            ["actors.id", "actors.actor_type"],
            name="fk_organization_memberships_actor_human",
        ),
        UniqueConstraint("actor_id", name="uq_organization_memberships_actor_id"),
    )

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column()
    actor_type: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(16))


class AIEmployee(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """AI社員の定義（正本）。組織ごとに持ち、更新のたびに version が増える。"""

    __tablename__ = "ai_employees"
    __table_args__ = (
        Index("ix_ai_employees_list", "organization_id", "created_at", "id"),
        CheckConstraint(f"status IN ({sql_in(AIEmployeeStatus)})", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("organization_id", "key", name="uq_ai_employees_organization_id_key"),
        # stage_assignments の複合 FK の参照先（担当ステージと組織の一致を保証する）
        UniqueConstraint(
            "id",
            "stage_key",
            "organization_id",
            name="uq_ai_employees_id_stage_key_organization_id",
        ),
    )

    key: Mapped[str] = mapped_column(String(64))
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


class StageAssignment(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """ステージへのAI社員の割り当て。primary は組織×ステージごとに最大1人。

    human_review にはAI社員を割り当てられない。
    AI社員の担当ステージ・組織との一致は複合 FK で保証する。
    """

    __tablename__ = "stage_assignments"
    __table_args__ = (
        CheckConstraint(f"role IN ({sql_in(StageAssignmentRole)})", name="role"),
        CheckConstraint(f"stage_key <> '{HUMAN_REVIEW}'", name="not_human_review"),
        ForeignKeyConstraint(
            ["ai_employee_id", "stage_key", "organization_id"],
            ["ai_employees.id", "ai_employees.stage_key", "ai_employees.organization_id"],
            name="fk_stage_assignments_employee_stage_org",
        ),
        UniqueConstraint(
            "organization_id",
            "stage_key",
            "ai_employee_id",
            name="uq_stage_assignments_org_stage_employee",
        ),
        Index(
            "uq_stage_assignments_primary",
            "organization_id",
            "stage_key",
            unique=True,
            postgresql_where=text(f"role = '{StageAssignmentRole.PRIMARY.value}'"),
        ),
    )

    stage_key: Mapped[str] = mapped_column(String(64))
    ai_employee_id: Mapped[UUID] = mapped_column(index=True)
    role: Mapped[str] = mapped_column(String(16))


class Exploration(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    __tablename__ = "explorations"
    __table_args__ = (
        Index("ix_explorations_list", "organization_id", "created_at", "id"),
        CheckConstraint(f"status IN ({sql_in(ExplorationStatus)})", name="status"),
        CheckConstraint(f"classification IN ({sql_in(DataClassification)})", name="classification"),
        UniqueConstraint("id", "organization_id", name="uq_explorations_id_organization_id"),
    )

    title: Mapped[str] = mapped_column(String(200))
    theme: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=ExplorationStatus.ACTIVE.value)
    # データ分類（第2回仕様 11章）。Idea はこの分類に従う
    classification: Mapped[str] = mapped_column(
        String(16), default=DataClassification.INTERNAL.value
    )
    created_by_actor_id: Mapped[UUID] = mapped_column(ForeignKey("actors.id"))


class Idea(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """事業アイデア。詳細項目は人間のみが更新する（AI の調査結果は analyses に残す）。"""

    __tablename__ = "ideas"
    __table_args__ = (
        Index("ix_ideas_list_exploration", "organization_id", "exploration_id", "created_at", "id"),
        UniqueConstraint("id", "organization_id", name="uq_ideas_id_organization_id"),
        ForeignKeyConstraint(
            ["exploration_id", "organization_id"],
            ["explorations.id", "explorations.organization_id"],
            name="fk_ideas_exploration_org",
        ),
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


class StageRun(UUIDPrimaryKeyMixin, OrganizationScopedMixin, Base):
    """ステージ実行の1試行。

    再実行・差し戻しでは新しい行を作り、古い行に superseded_at を記録する。
    """

    __tablename__ = "stage_runs"
    __table_args__ = (
        Index(
            "ix_stage_runs_list_exploration",
            "organization_id",
            "exploration_id",
            "started_at",
            "id",
        ),
        Index("ix_stage_runs_list_idea", "organization_id", "idea_id", "started_at", "id"),
        UniqueConstraint("id", "organization_id", name="uq_stage_runs_id_organization_id"),
        ForeignKeyConstraint(
            ["exploration_id", "organization_id"],
            ["explorations.id", "explorations.organization_id"],
            name="fk_stage_runs_exploration_org",
        ),
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
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED.value)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 人間が起動（受付）した日時。一覧の並び順にも使う。
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 非同期実行（第2回仕様 9章）：ワーカーが取り出した日時・ワーカーID・生存確認の時刻
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Execution(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """AI社員の実行履歴。実行時点の AI社員定義・Prompt・LLM・コードのバージョンを保存する。"""

    __tablename__ = "executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["stage_run_id", "organization_id"],
            ["stage_runs.id", "stage_runs.organization_id"],
            name="fk_executions_stage_run_org",
        ),
        CheckConstraint(f"status IN ({sql_in(RunStatus)})", name="status"),
        CheckConstraint(
            f"error_type IS NULL OR error_type IN ({sql_in(ErrorType)})", name="error_type"
        ),
        CheckConstraint(
            f"assignment_role IN ({sql_in(StageAssignmentRole)})", name="assignment_role"
        ),
        # 1つのステージ実行の primary は1つだけ
        Index(
            "uq_executions_primary_per_stage_run",
            "stage_run_id",
            unique=True,
            postgresql_where=text(f"assignment_role = '{StageAssignmentRole.PRIMARY.value}'"),
        ),
        # 1つのステージ実行で同じ AI社員は1回だけ
        UniqueConstraint("stage_run_id", "ai_employee_id", name="uq_executions_stage_run_employee"),
        # analyses の AI社員と実行の AI社員の一致を複合 FK で保証するための一意制約
        UniqueConstraint("id", "ai_employee_id", name="uq_executions_id_ai_employee_id"),
        UniqueConstraint("id", "organization_id", name="uq_executions_id_organization_id"),
        CheckConstraint("cost_amount >= 0", name="cost_non_negative"),
        CheckConstraint("cost_limit IS NULL OR cost_limit >= 0", name="cost_limit_non_negative"),
    )

    stage_run_id: Mapped[UUID] = mapped_column(ForeignKey("stage_runs.id"), index=True)
    ai_employee_id: Mapped[UUID] = mapped_column(ForeignKey("ai_employees.id"), index=True)
    # primary の結果がステージの成果。secondary は追加の視点として記録するだけ（7章）
    assignment_role: Mapped[str] = mapped_column(
        String(16), default=StageAssignmentRole.PRIMARY.value
    )
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
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED.value)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_type: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 実際に実行を始めた日時（queued の間は空）
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 費用の合計（LLM・Tool の呼び出しごとに加算。取り消し・失敗でも残る。第2回仕様 10章・E-07）
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    cost_currency: Mapped[str] = mapped_column(String(3))
    # 受け付けた時点の1実行あたりの費用上限（予算の確保にも使う）。第2回以前の実行は NULL
    cost_limit: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))


class Analysis(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """AI Analysis。Evidence ではない。

    本体は不変で、review_status のみ ReviewService が更新する。
    """

    __tablename__ = "analyses"
    __table_args__ = (
        # 版の連鎖は「範囲 × ステージ × AI社員」単位（第2回仕様 7章）
        Index(
            "uq_analyses_exploration_version",
            "exploration_id",
            "stage_key",
            "ai_employee_id",
            "version_no",
            unique=True,
            postgresql_where=text("idea_id IS NULL"),
        ),
        Index(
            "uq_analyses_idea_version",
            "idea_id",
            "stage_key",
            "ai_employee_id",
            "version_no",
            unique=True,
            postgresql_where=text("idea_id IS NOT NULL"),
        ),
        Index(
            "ix_analyses_list_exploration", "organization_id", "exploration_id", "created_at", "id"
        ),
        Index("ix_analyses_list_idea", "organization_id", "idea_id", "created_at", "id"),
        UniqueConstraint("id", "organization_id", name="uq_analyses_id_organization_id"),
        ForeignKeyConstraint(
            ["exploration_id", "organization_id"],
            ["explorations.id", "explorations.organization_id"],
            name="fk_analyses_exploration_org",
        ),
        # 分析の AI社員は、その分析を出した実行の AI社員と一致する
        ForeignKeyConstraint(
            ["execution_id", "ai_employee_id"],
            ["executions.id", "executions.ai_employee_id"],
            name="fk_analyses_execution_employee",
        ),
        CheckConstraint(f"review_status IN ({sql_in(ReviewStatus)})", name="review_status"),
        CheckConstraint("version_no >= 1", name="version_positive"),
        CheckConstraint(f"classification IN ({sql_in(DataClassification)})", name="classification"),
    )

    exploration_id: Mapped[UUID] = mapped_column(ForeignKey("explorations.id"), index=True)
    idea_id: Mapped[UUID | None] = mapped_column(ForeignKey("ideas.id"), index=True)
    stage_run_id: Mapped[UUID] = mapped_column(ForeignKey("stage_runs.id"), index=True)
    execution_id: Mapped[UUID] = mapped_column(ForeignKey("executions.id"), index=True)
    ai_employee_id: Mapped[UUID] = mapped_column(ForeignKey("ai_employees.id"), index=True)
    stage_key: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(64))
    version_no: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[UUID | None] = mapped_column(ForeignKey("analyses.id"))
    summary: Mapped[str] = mapped_column(Text)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    review_status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.PENDING_REVIEW.value
    )
    # 入力（探索案件・Evidence・前段の分析）の最も高い分類。算出値で、人間も変更できない（E-04）
    classification: Mapped[str] = mapped_column(String(16))


class Evidence(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """根拠・出典。外部情報または人間の入力のみ。不変（訂正は撤回＋新規登録）。

    状態（第2回 E-01。保存せず算出する）：
    - superseded：更新版（supersedes_evidence_id でこの行を指す Evidence）がある
    - retracted：人間が理由を付けて撤回した
    - purged：本文（quote・summary）を消去した（admin のみ。行・ID・来歴・根拠リンクは残る）
    """

    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_evidence_id_organization_id"),
        UniqueConstraint("supersedes_evidence_id", name="uq_evidence_supersedes_evidence_id"),
        ForeignKeyConstraint(
            ["supersedes_evidence_id", "organization_id"],
            ["evidence.id", "evidence.organization_id"],
            name="fk_evidence_supersedes_org",
        ),
        ForeignKeyConstraint(
            ["purged_by_actor_id", "purged_by_actor_type"],
            ["actors.id", "actors.actor_type"],
            name="fk_evidence_purged_by_human",
        ),
        CheckConstraint(
            f"purged_by_actor_type IS NULL OR purged_by_actor_type = '{HUMAN}'",
            name="purged_by_human",
        ),
        CheckConstraint(
            "(content_purged_at IS NULL) = (purge_reason IS NULL) "
            "AND (content_purged_at IS NULL) = (purged_by_actor_id IS NULL) "
            "AND (content_purged_at IS NULL) = (purged_by_actor_type IS NULL)",
            name="purge_record",
        ),
        CheckConstraint("supersedes_evidence_id <> id", name="not_self_superseding"),
        Index(
            "ix_evidence_list_exploration", "organization_id", "exploration_id", "created_at", "id"
        ),
        Index("ix_evidence_list_idea", "organization_id", "idea_id", "created_at", "id"),
        Index("ix_evidence_source_key", "organization_id", "exploration_id", "source_key"),
        ForeignKeyConstraint(
            ["exploration_id", "organization_id"],
            ["explorations.id", "explorations.organization_id"],
            name="fk_evidence_exploration_org",
        ),
        CheckConstraint(f"source_type IN ({sql_in(EvidenceSourceType)})", name="source_type"),
        CheckConstraint(
            "(retracted_at IS NULL) = (retraction_reason IS NULL)", name="retraction_reason"
        ),
        CheckConstraint(f"classification IN ({sql_in(DataClassification)})", name="classification"),
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
    # 出典の同一性（Web は正規化した URL）と、取得した本文のハッシュ（第2回 4章）
    source_key: Mapped[str | None] = mapped_column(String(2000))
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    supersedes_evidence_id: Mapped[UUID | None] = mapped_column()
    content_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_reason: Mapped[str | None] = mapped_column(Text)
    purged_by_actor_id: Mapped[UUID | None] = mapped_column()
    purged_by_actor_type: Mapped[str | None] = mapped_column(String(16))
    # データ分類（第2回仕様 11章）
    classification: Mapped[str] = mapped_column(
        String(16), default=DataClassification.INTERNAL.value
    )


class AnalysisEvidenceLink(Base):
    """第1回の根拠リンク。移行後は凍結（読み取り専用の履歴）。

    新しい処理は claim_evidence_links を使う。

    INSERT / UPDATE / DELETE / TRUNCATE は DB トリガーで拒否される（migration 0003）。
    """

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


class Claim(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """AI Analysis の主張（正本）。不変。claim_key は AI が付けたID（例：c1）。"""

    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(f"kind IN ({sql_in(ClaimKind)})", name="kind"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        UniqueConstraint("analysis_id", "claim_key", name="uq_claims_analysis_id_claim_key"),
        UniqueConstraint("id", "organization_id", name="uq_claims_id_organization_id"),
        # human_reviews.claim_id の複合 FK の参照先（主張が対象の分析に属することを保証する）
        UniqueConstraint("id", "analysis_id", name="uq_claims_id_analysis_id"),
        ForeignKeyConstraint(
            ["analysis_id", "organization_id"],
            ["analyses.id", "analyses.organization_id"],
            name="fk_claims_analysis_org",
        ),
    )

    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("analyses.id"), index=True)
    claim_key: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)


class ClaimEvidenceLink(OrganizationScopedMixin, Base):
    """主張と Evidence の関係（正本）。主キーは（主張・Evidence・relation）。不変。"""

    __tablename__ = "claim_evidence_links"
    __table_args__ = (
        CheckConstraint(f"relation IN ({sql_in(EvidenceRelation)})", name="relation"),
        ForeignKeyConstraint(
            ["claim_id", "organization_id"],
            ["claims.id", "claims.organization_id"],
            name="fk_claim_evidence_links_claim_org",
        ),
        ForeignKeyConstraint(
            ["evidence_id", "organization_id"],
            ["evidence.id", "evidence.organization_id"],
            name="fk_claim_evidence_links_evidence_org",
        ),
    )

    claim_id: Mapped[UUID] = mapped_column(ForeignKey("claims.id"), primary_key=True)
    evidence_id: Mapped[UUID] = mapped_column(
        ForeignKey("evidence.id"), primary_key=True, index=True
    )
    relation: Mapped[str] = mapped_column(String(16), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class HumanReview(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """人間によるレビュー。人間以外の actor では DB レベルで挿入できない。追記のみ。"""

    __tablename__ = "human_reviews"
    __table_args__ = (
        Index("ix_human_reviews_list", "organization_id", "analysis_id", "created_at", "id"),
        ForeignKeyConstraint(
            ["analysis_id", "organization_id"],
            ["analyses.id", "analyses.organization_id"],
            name="fk_human_reviews_analysis_org",
        ),
        ForeignKeyConstraint(
            ["claim_id", "analysis_id"],
            ["claims.id", "claims.analysis_id"],
            name="fk_human_reviews_claim_analysis",
        ),
        CheckConstraint(f"decision IN ({sql_in(ReviewDecision)})", name="decision"),
        CheckConstraint(f"reviewer_actor_type = '{HUMAN}'", name="reviewer_is_human"),
        ForeignKeyConstraint(
            ["reviewer_actor_id", "reviewer_actor_type"],
            ["actors.id", "actors.actor_type"],
            name="fk_human_reviews_reviewer_human",
        ),
    )

    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("analyses.id"), index=True)
    # 主張単位のレビュー（任意）。分析全体の review_status は変えない（B-15）。
    claim_id: Mapped[UUID | None] = mapped_column(index=True)
    exploration_id: Mapped[UUID] = mapped_column(ForeignKey("explorations.id"))
    idea_id: Mapped[UUID | None] = mapped_column(ForeignKey("ideas.id"), index=True)
    reviewer_actor_id: Mapped[UUID] = mapped_column()
    reviewer_actor_type: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(32))
    comment: Mapped[str | None] = mapped_column(Text)
    corrections: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class HumanDecision(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """人間による最終的な事業判断。AI から書き込む経路はない。追記のみ。"""

    __tablename__ = "human_decisions"
    __table_args__ = (
        Index("ix_human_decisions_list", "organization_id", "idea_id", "created_at", "id"),
        ForeignKeyConstraint(
            ["idea_id", "organization_id"],
            ["ideas.id", "ideas.organization_id"],
            name="fk_human_decisions_idea_org",
        ),
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


class AuditEvent(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
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


# ------------------------------------------------------------ 費用とログ（第2回 10・12章）

_CURRENCY = "~ '^[A-Z]{3}$'"


class Pricing(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """単価表。LLM（プロバイダー × モデル）と Tool（名前）の単価と、適用開始日時。

    プロバイダーの価格表なので組織をまたいで共通。単価の変更は新しい行で行い、履歴を残す。
    """

    __tablename__ = "pricing"
    __table_args__ = (
        UniqueConstraint(
            "kind", "provider", "model", "effective_from", name="uq_pricing_kind_provider_model"
        ),
        CheckConstraint(f"kind IN ({sql_in(PricingKind)})", name="kind"),
        CheckConstraint(f"currency {_CURRENCY}", name="currency"),
        CheckConstraint(
            "input_per_million_tokens >= 0 AND output_per_million_tokens >= 0 AND per_call >= 0",
            name="non_negative",
        ),
    )

    kind: Mapped[str] = mapped_column(String(16))
    # LLM はプロバイダー名、Tool は Tool の名前
    provider: Mapped[str] = mapped_column(String(64))
    # LLM のモデル。Tool は空文字
    model: Mapped[str] = mapped_column(String(128), default="")
    input_per_million_tokens: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    output_per_million_tokens: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    per_call: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Budget(UUIDPrimaryKeyMixin, OrganizationScopedMixin, TimestampMixin, Base):
    """月単位の予算。exploration_id が NULL なら組織全体、指定があればその探索案件。"""

    __tablename__ = "budgets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["exploration_id", "organization_id"],
            ["explorations.id", "explorations.organization_id"],
            name="fk_budgets_exploration_org",
        ),
        Index(
            "uq_budgets_organization",
            "organization_id",
            unique=True,
            postgresql_where=text("exploration_id IS NULL"),
        ),
        Index(
            "uq_budgets_exploration",
            "exploration_id",
            unique=True,
            postgresql_where=text("exploration_id IS NOT NULL"),
        ),
        CheckConstraint(f"mode IN ({sql_in(BudgetMode)})", name="mode"),
        CheckConstraint(f"currency {_CURRENCY}", name="currency"),
        CheckConstraint("monthly_limit >= 0", name="limit_non_negative"),
    )

    exploration_id: Mapped[UUID | None] = mapped_column()
    monthly_limit: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(String(3))
    mode: Mapped[str] = mapped_column(String(16), default=BudgetMode.HARD.value)


class LLMCall(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """LLM 呼び出しのメタデータ（永続）。本文は llm_call_payloads に分ける。秘密情報は持たない。"""

    __tablename__ = "llm_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["executions.id", "executions.organization_id"],
            name="fk_llm_calls_execution_org",
        ),
        UniqueConstraint("id", "organization_id", name="uq_llm_calls_id_organization_id"),
        Index("ix_llm_calls_list", "organization_id", "created_at", "id"),
        CheckConstraint(f"status IN ({sql_in(CallStatus)})", name="status"),
        CheckConstraint(
            f"error_type IS NULL OR error_type IN ({sql_in(ErrorType)})", name="error_type"
        ),
        CheckConstraint(f"payload_mode IN ({sql_in(PayloadMode)})", name="payload_mode"),
        CheckConstraint(f"classification IN ({sql_in(DataClassification)})", name="classification"),
        CheckConstraint(f"currency {_CURRENCY}", name="currency"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_amount >= 0", name="non_negative"
        ),
    )

    execution_id: Mapped[UUID] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    prompt_key: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(16))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3))
    pricing_id: Mapped[UUID | None] = mapped_column(ForeignKey("pricing.id"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))
    error_type: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    provider_request_id: Mapped[str | None] = mapped_column(String(256))
    # 送ったデータの最も高い分類と、本文の保存方式（confidential 以上は保存しない。R-16）
    classification: Mapped[str] = mapped_column(String(16))
    payload_mode: Mapped[str] = mapped_column(String(16))
    # 本文を保存期間の満了などで消した日時
    payload_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LLMCallPayload(OrganizationScopedMixin, CreatedAtMixin, Base):
    """LLM 呼び出しの本文（送ったメッセージと応答）。閲覧は admin のみ。保存期間で消す。"""

    __tablename__ = "llm_call_payloads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["llm_call_id", "organization_id"],
            ["llm_calls.id", "llm_calls.organization_id"],
            name="fk_llm_call_payloads_call_org",
        ),
    )

    llm_call_id: Mapped[UUID] = mapped_column(primary_key=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ToolCall(UUIDPrimaryKeyMixin, OrganizationScopedMixin, CreatedAtMixin, Base):
    """Tool 呼び出しのメタデータ（永続）。"""

    __tablename__ = "tool_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["executions.id", "executions.organization_id"],
            name="fk_tool_calls_execution_org",
        ),
        Index("ix_tool_calls_list", "organization_id", "created_at", "id"),
        CheckConstraint(f"status IN ({sql_in(CallStatus)})", name="status"),
        CheckConstraint(
            f"error_type IS NULL OR error_type IN ({sql_in(ErrorType)})", name="error_type"
        ),
        CheckConstraint(f"currency {_CURRENCY}", name="currency"),
        CheckConstraint("cost_amount >= 0", name="non_negative"),
    )

    execution_id: Mapped[UUID] = mapped_column(index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    tool_version: Mapped[str] = mapped_column(String(32))
    side_effect: Mapped[str] = mapped_column(String(32))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # 取得した URL の一覧（Web 取得 Tool。第2回 PR-8 以降）
    urls: Mapped[list[str]] = mapped_column(JSONB, default=list)
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3))
    pricing_id: Mapped[UUID | None] = mapped_column(ForeignKey("pricing.id"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))
    error_type: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
