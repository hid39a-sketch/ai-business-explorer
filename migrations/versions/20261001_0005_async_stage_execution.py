"""async stage execution

第2回仕様 7章・9章（非同期実行、primary / secondary、AI社員ごとの版の連鎖）。

- stage_runs / executions の状態に queued と cancelled を追加する（CHECK の変更）。
- stage_runs にワーカーの列（claimed_at、worker_id、heartbeat_at）を追加する。
- executions に assignment_role（primary / secondary）を追加する。既存の実行はすべて primary。
  1つのステージ実行の primary は1つだけ、同じ AI社員は1回だけ。
  executions.started_at は queued の間は空になる。
- analyses に ai_employee_id を追加する（既存は実行から埋め、実行の AI社員との一致を複合 FK で
  保証する）。版の連鎖を「範囲 × ステージ × AI社員」単位にする（一意インデックス）。
  既存の版番号はそのまま。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01 15:32:23.084153
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RUN_STATUSES_OLD = "'running', 'succeeded', 'failed'"
RUN_STATUSES_NEW = "'queued', 'running', 'succeeded', 'failed', 'cancelled'"


def _replace_status_check(table: str, values: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_status"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_status"), table, f"status IN ({values})")


def upgrade() -> None:
    for table in ("stage_runs", "executions"):
        _replace_status_check(table, RUN_STATUSES_NEW)

    op.add_column("stage_runs", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("stage_runs", sa.Column("worker_id", sa.String(length=128), nullable=True))
    op.add_column(
        "stage_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column(
        "executions",
        sa.Column(
            "assignment_role",
            sa.String(length=16),
            nullable=False,
            server_default="primary",  # 既存の実行はすべて primary
        ),
    )
    op.alter_column("executions", "assignment_role", server_default=None)
    op.create_check_constraint(
        op.f("ck_executions_assignment_role"),
        "executions",
        "assignment_role IN ('primary', 'secondary')",
    )
    op.create_index(
        "uq_executions_primary_per_stage_run",
        "executions",
        ["stage_run_id"],
        unique=True,
        postgresql_where=sa.text("assignment_role = 'primary'"),
    )
    op.create_unique_constraint(
        "uq_executions_stage_run_employee", "executions", ["stage_run_id", "ai_employee_id"]
    )
    op.create_unique_constraint(
        "uq_executions_id_ai_employee_id", "executions", ["id", "ai_employee_id"]
    )
    op.alter_column(
        "executions", "started_at", existing_type=postgresql.TIMESTAMP(timezone=True), nullable=True
    )

    op.add_column("analyses", sa.Column("ai_employee_id", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE analyses AS a SET ai_employee_id = e.ai_employee_id "
        "FROM executions AS e WHERE e.id = a.execution_id"
    )
    op.alter_column("analyses", "ai_employee_id", nullable=False)
    op.create_index(op.f("ix_analyses_ai_employee_id"), "analyses", ["ai_employee_id"])
    op.create_foreign_key(
        op.f("fk_analyses_ai_employee_id_ai_employees"),
        "analyses",
        "ai_employees",
        ["ai_employee_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_analyses_execution_employee",
        "analyses",
        "executions",
        ["execution_id", "ai_employee_id"],
        ["id", "ai_employee_id"],
    )
    op.create_index(
        "uq_analyses_exploration_version",
        "analyses",
        ["exploration_id", "stage_key", "ai_employee_id", "version_no"],
        unique=True,
        postgresql_where=sa.text("idea_id IS NULL"),
    )
    op.create_index(
        "uq_analyses_idea_version",
        "analyses",
        ["idea_id", "stage_key", "ai_employee_id", "version_no"],
        unique=True,
        postgresql_where=sa.text("idea_id IS NOT NULL"),
    )


def downgrade() -> None:
    """開発用。queued / cancelled の実行は failed に、未開始の実行の開始日時は作成日時にする。"""
    op.drop_index(
        "uq_analyses_idea_version",
        table_name="analyses",
        postgresql_where=sa.text("idea_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_analyses_exploration_version",
        table_name="analyses",
        postgresql_where=sa.text("idea_id IS NULL"),
    )
    op.drop_constraint("fk_analyses_execution_employee", "analyses", type_="foreignkey")
    op.drop_constraint(
        op.f("fk_analyses_ai_employee_id_ai_employees"), "analyses", type_="foreignkey"
    )
    op.drop_index(op.f("ix_analyses_ai_employee_id"), table_name="analyses")
    op.drop_column("analyses", "ai_employee_id")

    op.execute("UPDATE executions SET started_at = created_at WHERE started_at IS NULL")
    op.alter_column(
        "executions",
        "started_at",
        existing_type=postgresql.TIMESTAMP(timezone=True),
        nullable=False,
    )
    op.drop_constraint("uq_executions_id_ai_employee_id", "executions", type_="unique")
    op.drop_constraint("uq_executions_stage_run_employee", "executions", type_="unique")
    op.drop_index(
        "uq_executions_primary_per_stage_run",
        table_name="executions",
        postgresql_where=sa.text("assignment_role = 'primary'"),
    )
    op.drop_constraint(op.f("ck_executions_assignment_role"), "executions", type_="check")
    op.drop_column("executions", "assignment_role")

    op.drop_column("stage_runs", "heartbeat_at")
    op.drop_column("stage_runs", "worker_id")
    op.drop_column("stage_runs", "claimed_at")
    for table in ("stage_runs", "executions"):
        op.execute(
            f"UPDATE {table} SET status = 'failed' "  # noqa: S608  固定のテーブル名
            "WHERE status IN ('queued', 'cancelled')"
        )
        _replace_status_check(table, RUN_STATUSES_OLD)
