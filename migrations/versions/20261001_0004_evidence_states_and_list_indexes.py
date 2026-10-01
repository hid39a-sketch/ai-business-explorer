"""evidence states and list indexes

第2回仕様 4章・14章・15章（E-01、R-15、R-20）。

- evidence に source_key / snapshot_hash / supersedes_evidence_id（更新版の連鎖）と、
  本文の消去（purge）の記録列を追加する。消去した人は人間に限る（複合 FK + CHECK）。
- 状態（active / superseded / retracted / purged）は保存せず、取得時に算出する。
- 一覧のカーソル方式のための複合インデックス（組織・親のID・作成日時・ID）を追加する。
- 既存の Evidence の source_key は NULL のまま（第1回は Tool 取得がない）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01 14:59:25.994209
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_ai_employees_list",
        "ai_employees",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_analyses_list_exploration",
        "analyses",
        ["organization_id", "exploration_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_analyses_list_idea",
        "analyses",
        ["organization_id", "idea_id", "created_at", "id"],
        unique=False,
    )
    op.add_column("evidence", sa.Column("source_key", sa.String(length=2000), nullable=True))
    op.add_column("evidence", sa.Column("snapshot_hash", sa.String(length=64), nullable=True))
    op.add_column("evidence", sa.Column("supersedes_evidence_id", sa.Uuid(), nullable=True))
    op.add_column(
        "evidence", sa.Column("content_purged_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("evidence", sa.Column("purge_reason", sa.Text(), nullable=True))
    op.add_column("evidence", sa.Column("purged_by_actor_id", sa.Uuid(), nullable=True))
    op.add_column(
        "evidence", sa.Column("purged_by_actor_type", sa.String(length=16), nullable=True)
    )
    op.create_index(
        "ix_evidence_list_exploration",
        "evidence",
        ["organization_id", "exploration_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_list_idea",
        "evidence",
        ["organization_id", "idea_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_source_key",
        "evidence",
        ["organization_id", "exploration_id", "source_key"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_evidence_supersedes_evidence_id", "evidence", ["supersedes_evidence_id"]
    )
    op.create_foreign_key(
        "fk_evidence_supersedes_org",
        "evidence",
        "evidence",
        ["supersedes_evidence_id", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_foreign_key(
        "fk_evidence_purged_by_human",
        "evidence",
        "actors",
        ["purged_by_actor_id", "purged_by_actor_type"],
        ["id", "actor_type"],
    )
    op.create_check_constraint(
        op.f("ck_evidence_purged_by_human"),
        "evidence",
        "purged_by_actor_type IS NULL OR purged_by_actor_type = 'human'",
    )
    op.create_check_constraint(
        op.f("ck_evidence_purge_record"),
        "evidence",
        "(content_purged_at IS NULL) = (purge_reason IS NULL) "
        "AND (content_purged_at IS NULL) = (purged_by_actor_id IS NULL) "
        "AND (content_purged_at IS NULL) = (purged_by_actor_type IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_evidence_not_self_superseding"), "evidence", "supersedes_evidence_id <> id"
    )
    op.create_index(
        "ix_explorations_list",
        "explorations",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_human_decisions_list",
        "human_decisions",
        ["organization_id", "idea_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_human_reviews_list",
        "human_reviews",
        ["organization_id", "analysis_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_ideas_list_exploration",
        "ideas",
        ["organization_id", "exploration_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_stage_runs_list_exploration",
        "stage_runs",
        ["organization_id", "exploration_id", "started_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_stage_runs_list_idea",
        "stage_runs",
        ["organization_id", "idea_id", "started_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_evidence_not_self_superseding"), "evidence", type_="check")
    op.drop_constraint(op.f("ck_evidence_purge_record"), "evidence", type_="check")
    op.drop_constraint(op.f("ck_evidence_purged_by_human"), "evidence", type_="check")
    op.drop_index("ix_stage_runs_list_idea", table_name="stage_runs")
    op.drop_index("ix_stage_runs_list_exploration", table_name="stage_runs")
    op.drop_index("ix_ideas_list_exploration", table_name="ideas")
    op.drop_index("ix_human_reviews_list", table_name="human_reviews")
    op.drop_index("ix_human_decisions_list", table_name="human_decisions")
    op.drop_index("ix_explorations_list", table_name="explorations")
    op.drop_constraint("fk_evidence_purged_by_human", "evidence", type_="foreignkey")
    op.drop_constraint("fk_evidence_supersedes_org", "evidence", type_="foreignkey")
    op.drop_constraint("uq_evidence_supersedes_evidence_id", "evidence", type_="unique")
    op.drop_index("ix_evidence_source_key", table_name="evidence")
    op.drop_index("ix_evidence_list_idea", table_name="evidence")
    op.drop_index("ix_evidence_list_exploration", table_name="evidence")
    op.drop_column("evidence", "purged_by_actor_type")
    op.drop_column("evidence", "purged_by_actor_id")
    op.drop_column("evidence", "purge_reason")
    op.drop_column("evidence", "content_purged_at")
    op.drop_column("evidence", "supersedes_evidence_id")
    op.drop_column("evidence", "snapshot_hash")
    op.drop_column("evidence", "source_key")
    op.drop_index("ix_analyses_list_idea", table_name="analyses")
    op.drop_index("ix_analyses_list_exploration", table_name="analyses")
    op.drop_index("ix_ai_employees_list", table_name="ai_employees")
