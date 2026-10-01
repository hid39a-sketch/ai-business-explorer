"""evidence candidates and collect only

第2回仕様 2章・3章・12章・14章（Evidence 候補、AI生成の補助情報、来歴、収集のみ。B-21・E-02・R-07）。

- evidence_candidates（Tool が取得した原情報だけを持つ候補）、evidence_candidate_ai_notes
  （AI生成の補助情報。元の候補と生成した実行を必ず参照する）、tool_call_outputs（Tool の生の出力。
  90日で消す）を作る。
- evidence に来歴の列（acquisition_method、candidate_id、tool_call_id、execution_id）を追加する。
  既存の Evidence はすべて human_input。Tool 取得なら候補・Tool 呼び出し・実行・取得日時が必須。
- stage_runs に mode（analyze / collect_only）を追加する。既存の試行はすべて analyze。
  「最新の試行は1つ」の部分一意インデックスを analyze に限る（collect_only は数えない）。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01 16:55:00.253332
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CURRENT_INDEXES = (
    ("uq_stage_runs_exploration_current", "exploration_id", "idea_id IS NULL"),
    ("uq_stage_runs_idea_current", "idea_id", "idea_id IS NOT NULL"),
)


def _current_indexes(extra: str) -> None:
    for name, column, scope in CURRENT_INDEXES:
        op.drop_index(name, table_name="stage_runs")
        op.create_index(
            name,
            "stage_runs",
            [column, "stage_key"],
            unique=True,
            postgresql_where=sa.text(f"{scope} AND superseded_at IS NULL{extra}"),
        )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_tool_calls_id_organization_id", "tool_calls", ["id", "organization_id"]
    )
    op.create_table(
        "evidence_candidates",
        sa.Column("exploration_id", sa.Uuid(), nullable=False),
        sa.Column("idea_id", sa.Uuid(), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=True),
        sa.Column("source_key", sa.String(length=2000), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("snapshot", sa.Text(), nullable=True),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("snapshot_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duplicate_of_evidence_id", sa.Uuid(), nullable=True),
        sa.Column("updates_evidence_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by_actor_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by_actor_type", sa.String(length=16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'duplicate') = (duplicate_of_evidence_id IS NOT NULL)",
            name=op.f("ck_evidence_candidates_duplicate_of"),
        ),
        sa.CheckConstraint(
            "(status IN ('accepted', 'rejected')) = (decided_by_actor_id IS NOT NULL) AND (decided_by_actor_id IS NULL) = (decided_at IS NULL) AND (decided_by_actor_id IS NULL) = (decided_by_actor_type IS NULL)",
            name=op.f("ck_evidence_candidates_decision_record"),
        ),
        sa.CheckConstraint(
            "decided_by_actor_type IS NULL OR decided_by_actor_type = 'human'",
            name=op.f("ck_evidence_candidates_decided_by_human"),
        ),
        sa.CheckConstraint(
            "source_type IN ('human_input', 'document', 'web', 'api', 'patent_db', 'other')",
            name=op.f("ck_evidence_candidates_source_type"),
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR decision_reason IS NOT NULL",
            name=op.f("ck_evidence_candidates_reject_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'duplicate')",
            name=op.f("ck_evidence_candidates_status"),
        ),
        sa.CheckConstraint(
            "char_length(quote) <= 2000", name=op.f("ck_evidence_candidates_quote_excerpt_length")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_actor_id", "decided_by_actor_type"],
            ["actors.id", "actors.actor_type"],
            name="fk_evidence_candidates_decided_by_human",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_evidence_id"],
            ["evidence.id"],
            name=op.f("fk_evidence_candidates_duplicate_of_evidence_id_evidence"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["executions.id", "executions.organization_id"],
            name="fk_evidence_candidates_execution_org",
        ),
        sa.ForeignKeyConstraint(
            ["exploration_id", "organization_id"],
            ["explorations.id", "explorations.organization_id"],
            name="fk_evidence_candidates_exploration_org",
        ),
        sa.ForeignKeyConstraint(
            ["idea_id", "organization_id"],
            ["ideas.id", "ideas.organization_id"],
            name="fk_evidence_candidates_idea_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_evidence_candidates_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id", "organization_id"],
            ["tool_calls.id", "tool_calls.organization_id"],
            name="fk_evidence_candidates_tool_call_org",
        ),
        sa.ForeignKeyConstraint(
            ["updates_evidence_id"],
            ["evidence.id"],
            name=op.f("fk_evidence_candidates_updates_evidence_id_evidence"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_candidates")),
        sa.UniqueConstraint(
            "id", "organization_id", name="uq_evidence_candidates_id_organization_id"
        ),
    )
    op.create_index(
        op.f("ix_evidence_candidates_execution_id"),
        "evidence_candidates",
        ["execution_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_candidates_exploration_id"),
        "evidence_candidates",
        ["exploration_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_candidates_list",
        "evidence_candidates",
        ["organization_id", "exploration_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_candidates_organization_id"),
        "evidence_candidates",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "evidence_candidate_ai_notes",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("llm_provider", sa.String(length=64), nullable=True),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
        sa.Column("prompt_key", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=16), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "organization_id"],
            ["evidence_candidates.id", "evidence_candidates.organization_id"],
            name="fk_candidate_ai_notes_candidate_org",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["executions.id", "executions.organization_id"],
            name="fk_candidate_ai_notes_execution_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_evidence_candidate_ai_notes_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_candidate_ai_notes")),
    )
    op.create_index(
        op.f("ix_evidence_candidate_ai_notes_candidate_id"),
        "evidence_candidate_ai_notes",
        ["candidate_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_candidate_ai_notes_execution_id"),
        "evidence_candidate_ai_notes",
        ["execution_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_evidence_candidate_ai_notes_organization_id"),
        "evidence_candidate_ai_notes",
        ["organization_id"],
        unique=False,
    )
    op.create_table(
        "tool_call_outputs",
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_tool_call_outputs_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["tool_call_id", "organization_id"],
            ["tool_calls.id", "tool_calls.organization_id"],
            name="fk_tool_call_outputs_call_org",
        ),
        sa.PrimaryKeyConstraint("tool_call_id", name=op.f("pk_tool_call_outputs")),
    )
    op.create_index(
        op.f("ix_tool_call_outputs_organization_id"),
        "tool_call_outputs",
        ["organization_id"],
        unique=False,
    )
    op.add_column(
        "evidence",
        sa.Column(
            "acquisition_method",
            sa.String(length=16),
            nullable=False,
            server_default="human_input",  # 既存の Evidence はすべて人間の入力
        ),
    )
    op.alter_column("evidence", "acquisition_method", server_default=None)
    op.add_column("evidence", sa.Column("candidate_id", sa.Uuid(), nullable=True))
    op.add_column("evidence", sa.Column("tool_call_id", sa.Uuid(), nullable=True))
    op.add_column("evidence", sa.Column("execution_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint("uq_evidence_candidate_id", "evidence", ["candidate_id"])
    op.create_foreign_key(
        "fk_evidence_tool_call_org",
        "evidence",
        "tool_calls",
        ["tool_call_id", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_foreign_key(
        "fk_evidence_execution_org",
        "evidence",
        "executions",
        ["execution_id", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_foreign_key(
        "fk_evidence_candidate_org",
        "evidence",
        "evidence_candidates",
        ["candidate_id", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_check_constraint(
        op.f("ck_evidence_acquisition_method"),
        "evidence",
        "acquisition_method IN ('human_input', 'tool')",
    )
    op.create_check_constraint(
        op.f("ck_evidence_tool_provenance"),
        "evidence",
        "(acquisition_method = 'tool') = (candidate_id IS NOT NULL AND tool_call_id IS NOT NULL "
        "AND execution_id IS NOT NULL AND retrieved_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_evidence_human_input_has_no_tool_provenance"),
        "evidence",
        "acquisition_method = 'tool' OR "
        "(candidate_id IS NULL AND tool_call_id IS NULL AND execution_id IS NULL)",
    )

    op.add_column(
        "stage_runs",
        sa.Column(
            "mode", sa.String(length=16), nullable=False, server_default="analyze"
        ),  # 既存の試行はすべて analyze
    )
    op.alter_column("stage_runs", "mode", server_default=None)
    op.create_check_constraint(
        op.f("ck_stage_runs_mode"), "stage_runs", "mode IN ('analyze', 'collect_only')"
    )
    op.create_check_constraint(
        op.f("ck_stage_runs_collect_only_is_initial"),
        "stage_runs",
        "mode = 'analyze' OR trigger = 'initial'",
    )
    _current_indexes(" AND mode = 'analyze'")


def downgrade() -> None:
    """開発用。候補・補助情報・Tool の生の出力と、Evidence の来歴は失われる。

    collect_only の試行は「最新の試行は1つ」の制約に反しないよう superseded にする。
    """
    op.execute(
        "UPDATE stage_runs SET superseded_at = COALESCE(finished_at, started_at) "
        "WHERE mode = 'collect_only' AND superseded_at IS NULL"
    )
    _current_indexes("")
    op.drop_constraint(op.f("ck_stage_runs_collect_only_is_initial"), "stage_runs", type_="check")
    op.drop_constraint(op.f("ck_stage_runs_mode"), "stage_runs", type_="check")
    op.drop_column("stage_runs", "mode")
    op.drop_constraint(
        op.f("ck_evidence_human_input_has_no_tool_provenance"), "evidence", type_="check"
    )
    op.drop_constraint(op.f("ck_evidence_tool_provenance"), "evidence", type_="check")
    op.drop_constraint(op.f("ck_evidence_acquisition_method"), "evidence", type_="check")
    op.drop_constraint("fk_evidence_candidate_org", "evidence", type_="foreignkey")
    op.drop_constraint("fk_evidence_execution_org", "evidence", type_="foreignkey")
    op.drop_constraint("fk_evidence_tool_call_org", "evidence", type_="foreignkey")
    op.drop_constraint("uq_evidence_candidate_id", "evidence", type_="unique")
    op.drop_column("evidence", "execution_id")
    op.drop_column("evidence", "tool_call_id")
    op.drop_column("evidence", "candidate_id")
    op.drop_column("evidence", "acquisition_method")
    op.drop_index(op.f("ix_tool_call_outputs_organization_id"), table_name="tool_call_outputs")
    op.drop_table("tool_call_outputs")
    op.drop_index(
        op.f("ix_evidence_candidate_ai_notes_organization_id"),
        table_name="evidence_candidate_ai_notes",
    )
    op.drop_index(
        op.f("ix_evidence_candidate_ai_notes_execution_id"),
        table_name="evidence_candidate_ai_notes",
    )
    op.drop_index(
        op.f("ix_evidence_candidate_ai_notes_candidate_id"),
        table_name="evidence_candidate_ai_notes",
    )
    op.drop_table("evidence_candidate_ai_notes")
    op.drop_index(op.f("ix_evidence_candidates_organization_id"), table_name="evidence_candidates")
    op.drop_index("ix_evidence_candidates_list", table_name="evidence_candidates")
    op.drop_index(op.f("ix_evidence_candidates_exploration_id"), table_name="evidence_candidates")
    op.drop_index(op.f("ix_evidence_candidates_execution_id"), table_name="evidence_candidates")
    op.drop_table("evidence_candidates")
    op.drop_constraint("uq_tool_calls_id_organization_id", "tool_calls", type_="unique")
