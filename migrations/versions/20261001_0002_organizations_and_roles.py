"""organizations and roles

第2回仕様 1章・7章（R-01、A-07、B-03、B-05、E-05）。

- organizations / organization_memberships / stage_assignments を新設する。
- 既定組織（固定ID）を作り、第1回のすべての行をこの組織に移す。
- 既存の人間 actor は admin として所属させる。system actor にはロールを付けない。
- 既存のAI社員は、組織×ステージごとに最も古い active の社員を primary にする
  （第1回の seed では idea_generator と market_researcher）。
- 第1回の人間専用の制約（複合 FK + CHECK）はそのまま残す。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01 14:20:41.140241
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 既定組織。seed.DEFAULT_ORGANIZATION_ID と同じ値（migration は値を固定で持つ）。
DEFAULT_ORGANIZATION_ID = "00000000-0000-7000-8000-000000000100"
DEFAULT_ORGANIZATION_NAME = "Default Organization"

# organization_id を持たせる既存の業務テーブル。
SCOPED_TABLES = (
    "ai_employees",
    "explorations",
    "ideas",
    "stage_runs",
    "executions",
    "analyses",
    "evidence",
    "human_reviews",
    "human_decisions",
    "audit_events",
)

# 複合 FK の参照先にする (id, organization_id) の一意制約。
PARENT_UNIQUES = ("explorations", "ideas", "stage_runs", "analyses")

# 子の (親ID, organization_id) → 親の (id, organization_id)。親子の組織の一致を保証する。
ORG_CONSISTENCY_FKS = (
    ("fk_ideas_exploration_org", "ideas", "exploration_id", "explorations"),
    ("fk_stage_runs_exploration_org", "stage_runs", "exploration_id", "explorations"),
    ("fk_analyses_exploration_org", "analyses", "exploration_id", "explorations"),
    ("fk_evidence_exploration_org", "evidence", "exploration_id", "explorations"),
    ("fk_executions_stage_run_org", "executions", "stage_run_id", "stage_runs"),
    ("fk_human_reviews_analysis_org", "human_reviews", "analysis_id", "analyses"),
    ("fk_human_decisions_idea_org", "human_decisions", "idea_id", "ideas"),
)


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    # --- 組織と既定組織
    op.create_table(
        "organizations",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )
    op.execute(
        sa.text(
            "INSERT INTO organizations (id, name) VALUES (CAST(:id AS uuid), :name)"
        ).bindparams(id=DEFAULT_ORGANIZATION_ID, name=DEFAULT_ORGANIZATION_NAME)
    )

    # --- 既存の業務テーブルに organization_id を追加し、既定組織に移す
    for table in SCOPED_TABLES:
        op.add_column(table, sa.Column("organization_id", sa.Uuid(), nullable=True))
        backfill = f"UPDATE {table} SET organization_id = CAST(:id AS uuid)"  # noqa: S608  固定のテーブル名
        op.execute(sa.text(backfill).bindparams(id=DEFAULT_ORGANIZATION_ID))
        op.alter_column(table, "organization_id", nullable=False)
        op.create_index(op.f(f"ix_{table}_organization_id"), table, ["organization_id"])
        op.create_foreign_key(
            op.f(f"fk_{table}_organization_id_organizations"),
            table,
            "organizations",
            ["organization_id"],
            ["id"],
        )

    for table in PARENT_UNIQUES:
        op.create_unique_constraint(
            f"uq_{table}_id_organization_id", table, ["id", "organization_id"]
        )
    for name, child, parent_column, parent in ORG_CONSISTENCY_FKS:
        op.create_foreign_key(
            name, child, parent, [parent_column, "organization_id"], ["id", "organization_id"]
        )

    # --- AI社員：key の一意を「組織 × key」に変える（B-03）
    op.drop_constraint(op.f("uq_ai_employees_key"), "ai_employees", type_="unique")
    op.create_unique_constraint(
        "uq_ai_employees_organization_id_key", "ai_employees", ["organization_id", "key"]
    )
    op.create_unique_constraint(
        "uq_ai_employees_id_stage_key_organization_id",
        "ai_employees",
        ["id", "stage_key", "organization_id"],
    )

    # --- 所属とロール（A-07）。人間だけがロールを持てる
    op.create_table(
        "organization_memberships",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "actor_type = 'human'", name=op.f("ck_organization_memberships_member_is_human")
        ),
        sa.CheckConstraint(
            "role IN ('viewer', 'member', 'reviewer', 'admin')",
            name=op.f("ck_organization_memberships_role"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id", "actor_type"],
            ["actors.id", "actors.actor_type"],
            name="fk_organization_memberships_actor_human",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_memberships_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_memberships")),
        sa.UniqueConstraint("actor_id", name="uq_organization_memberships_actor_id"),
    )
    op.create_index(
        op.f("ix_organization_memberships_organization_id"),
        "organization_memberships",
        ["organization_id"],
    )
    # 既存の人間 actor は admin（E-05）。gen_random_uuid() は PostgreSQL 13 以降の組み込み。
    op.execute(
        sa.text(
            "INSERT INTO organization_memberships (id, organization_id, actor_id, actor_type, role) "
            "SELECT gen_random_uuid(), CAST(:org AS uuid), id, actor_type, 'admin' "
            "FROM actors WHERE actor_type = 'human'"
        ).bindparams(org=DEFAULT_ORGANIZATION_ID)
    )

    # --- ステージへのAI社員の割り当て（B-05）
    op.create_table(
        "stage_assignments",
        sa.Column("stage_key", sa.String(length=64), nullable=False),
        sa.Column("ai_employee_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "role IN ('primary', 'secondary')", name=op.f("ck_stage_assignments_role")
        ),
        sa.CheckConstraint(
            "stage_key <> 'human_review'", name=op.f("ck_stage_assignments_not_human_review")
        ),
        sa.ForeignKeyConstraint(
            ["ai_employee_id", "stage_key", "organization_id"],
            ["ai_employees.id", "ai_employees.stage_key", "ai_employees.organization_id"],
            name="fk_stage_assignments_employee_stage_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_stage_assignments_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stage_assignments")),
        sa.UniqueConstraint(
            "organization_id",
            "stage_key",
            "ai_employee_id",
            name="uq_stage_assignments_org_stage_employee",
        ),
    )
    op.create_index(
        op.f("ix_stage_assignments_ai_employee_id"), "stage_assignments", ["ai_employee_id"]
    )
    op.create_index(
        op.f("ix_stage_assignments_organization_id"), "stage_assignments", ["organization_id"]
    )
    op.create_index(
        "uq_stage_assignments_primary",
        "stage_assignments",
        ["organization_id", "stage_key"],
        unique=True,
        postgresql_where=sa.text("role = 'primary'"),
    )
    # 組織×ステージごとに最も古い active の社員を primary にする（E-05）。
    op.execute(
        "INSERT INTO stage_assignments (id, organization_id, stage_key, ai_employee_id, role) "
        "SELECT gen_random_uuid(), organization_id, stage_key, id, 'primary' FROM ("
        "  SELECT DISTINCT ON (organization_id, stage_key) organization_id, stage_key, id "
        "  FROM ai_employees "
        "  WHERE status = 'active' AND stage_key <> 'human_review' "
        "  ORDER BY organization_id, stage_key, created_at, id"
        ") AS first_active"
    )


def downgrade() -> None:
    """開発用。複数の組織で同じ key のAI社員がある場合は失敗する。"""
    op.drop_index(
        "uq_stage_assignments_primary",
        table_name="stage_assignments",
        postgresql_where=sa.text("role = 'primary'"),
    )
    op.drop_index(op.f("ix_stage_assignments_organization_id"), table_name="stage_assignments")
    op.drop_index(op.f("ix_stage_assignments_ai_employee_id"), table_name="stage_assignments")
    op.drop_table("stage_assignments")
    op.drop_index(
        op.f("ix_organization_memberships_organization_id"), table_name="organization_memberships"
    )
    op.drop_table("organization_memberships")

    op.drop_constraint(
        "uq_ai_employees_id_stage_key_organization_id", "ai_employees", type_="unique"
    )
    op.drop_constraint("uq_ai_employees_organization_id_key", "ai_employees", type_="unique")
    op.create_unique_constraint(op.f("uq_ai_employees_key"), "ai_employees", ["key"])

    for name, child, _, _ in ORG_CONSISTENCY_FKS:
        op.drop_constraint(name, child, type_="foreignkey")
    for table in PARENT_UNIQUES:
        op.drop_constraint(f"uq_{table}_id_organization_id", table, type_="unique")
    for table in SCOPED_TABLES:
        op.drop_constraint(
            op.f(f"fk_{table}_organization_id_organizations"), table, type_="foreignkey"
        )
        op.drop_index(op.f(f"ix_{table}_organization_id"), table_name=table)
        op.drop_column(table, "organization_id")
    op.drop_table("organizations")
