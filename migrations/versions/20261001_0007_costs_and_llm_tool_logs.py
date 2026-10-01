"""costs and llm tool logs

第2回仕様 10章・12章・14章（費用管理と LLM・Tool のログ。E-07・R-16・R-20・R-21）。

- pricing（単価表。組織をまたいで共通）、budgets（組織・探索案件ごとの月額予算）を作る。
- llm_calls（メタデータ）と llm_call_payloads（本文）、tool_calls（メタデータ）を作る。
- executions に費用の合計（cost_amount、cost_currency）と、受付時の1実行あたりの上限
  （cost_limit）を追加する。既存の実行の費用は 0 USD（第1回は Fake LLM のみで費用は発生していない）、
  上限は NULL。
- error_type に budget_exceeded を追加する（CHECK の変更）。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01 16:21:45.680180
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ERROR_TYPES_OLD = (
    "'validation_error', 'llm_error', 'tool_error', 'timeout', 'storage_error', 'unexpected'"
)
ERROR_TYPES_NEW = f"{ERROR_TYPES_OLD}, 'budget_exceeded'"


def upgrade() -> None:
    # 既存の実行（第1回・第2回の Fake LLM）は費用 0 USD
    op.add_column(
        "executions",
        sa.Column(
            "cost_amount", sa.Numeric(precision=18, scale=8), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "executions",
        sa.Column("cost_currency", sa.String(length=3), nullable=False, server_default="USD"),
    )
    op.alter_column("executions", "cost_amount", server_default=None)
    op.alter_column("executions", "cost_currency", server_default=None)
    op.add_column(
        "executions", sa.Column("cost_limit", sa.Numeric(precision=18, scale=8), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_executions_cost_non_negative"), "executions", "cost_amount >= 0"
    )
    op.create_check_constraint(
        op.f("ck_executions_cost_limit_non_negative"),
        "executions",
        "cost_limit IS NULL OR cost_limit >= 0",
    )
    op.create_unique_constraint(
        "uq_executions_id_organization_id", "executions", ["id", "organization_id"]
    )
    op.drop_constraint(op.f("ck_executions_error_type"), "executions", type_="check")
    op.create_check_constraint(
        op.f("ck_executions_error_type"),
        "executions",
        f"error_type IS NULL OR error_type IN ({ERROR_TYPES_NEW})",
    )

    op.create_table(
        "pricing",
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("input_per_million_tokens", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("output_per_million_tokens", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("per_call", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_pricing_currency")),
        sa.CheckConstraint("kind IN ('llm', 'tool')", name=op.f("ck_pricing_kind")),
        sa.CheckConstraint(
            "input_per_million_tokens >= 0 AND output_per_million_tokens >= 0 AND per_call >= 0",
            name=op.f("ck_pricing_non_negative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pricing")),
        sa.UniqueConstraint(
            "kind", "provider", "model", "effective_from", name="uq_pricing_kind_provider_model"
        ),
    )
    op.create_table(
        "budgets",
        sa.Column("exploration_id", sa.Uuid(), nullable=True),
        sa.Column("monthly_limit", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_budgets_currency")),
        sa.CheckConstraint("mode IN ('hard', 'soft')", name=op.f("ck_budgets_mode")),
        sa.CheckConstraint("monthly_limit >= 0", name=op.f("ck_budgets_limit_non_negative")),
        sa.ForeignKeyConstraint(
            ["exploration_id", "organization_id"],
            ["explorations.id", "explorations.organization_id"],
            name="fk_budgets_exploration_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_budgets_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budgets")),
    )
    op.create_index(
        op.f("ix_budgets_organization_id"), "budgets", ["organization_id"], unique=False
    )
    op.create_index(
        "uq_budgets_exploration",
        "budgets",
        ["exploration_id"],
        unique=True,
        postgresql_where=sa.text("exploration_id IS NOT NULL"),
    )
    op.create_index(
        "uq_budgets_organization",
        "budgets",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("exploration_id IS NULL"),
    )
    op.create_table(
        "llm_calls",
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_key", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_amount", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("pricing_id", sa.Uuid(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_type", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("provider_request_id", sa.String(length=256), nullable=True),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("payload_mode", sa.String(length=16), nullable=False),
        sa.Column("payload_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "classification IN ('public', 'internal', 'confidential', 'restricted')",
            name=op.f("ck_llm_calls_classification"),
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_llm_calls_currency")),
        sa.CheckConstraint(
            "error_type IS NULL OR error_type IN ('validation_error', 'llm_error', 'tool_error', 'timeout', 'storage_error', 'unexpected', 'budget_exceeded')",
            name=op.f("ck_llm_calls_error_type"),
        ),
        sa.CheckConstraint(
            "payload_mode IN ('full', 'none')", name=op.f("ck_llm_calls_payload_mode")
        ),
        sa.CheckConstraint("status IN ('succeeded', 'failed')", name=op.f("ck_llm_calls_status")),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_amount >= 0",
            name=op.f("ck_llm_calls_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["executions.id", "executions.organization_id"],
            name="fk_llm_calls_execution_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_llm_calls_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["pricing_id"], ["pricing.id"], name=op.f("fk_llm_calls_pricing_id_pricing")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
        sa.UniqueConstraint("id", "organization_id", name="uq_llm_calls_id_organization_id"),
    )
    op.create_index(op.f("ix_llm_calls_execution_id"), "llm_calls", ["execution_id"], unique=False)
    op.create_index(
        "ix_llm_calls_list", "llm_calls", ["organization_id", "created_at", "id"], unique=False
    )
    op.create_index(
        op.f("ix_llm_calls_organization_id"), "llm_calls", ["organization_id"], unique=False
    )
    op.create_table(
        "tool_calls",
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column("side_effect", sa.String(length=32), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("urls", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cost_amount", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("pricing_id", sa.Uuid(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_type", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_tool_calls_currency")),
        sa.CheckConstraint(
            "error_type IS NULL OR error_type IN ('validation_error', 'llm_error', 'tool_error', 'timeout', 'storage_error', 'unexpected', 'budget_exceeded')",
            name=op.f("ck_tool_calls_error_type"),
        ),
        sa.CheckConstraint("status IN ('succeeded', 'failed')", name=op.f("ck_tool_calls_status")),
        sa.CheckConstraint("cost_amount >= 0", name=op.f("ck_tool_calls_non_negative")),
        sa.ForeignKeyConstraint(
            ["execution_id", "organization_id"],
            ["executions.id", "executions.organization_id"],
            name="fk_tool_calls_execution_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_tool_calls_organization_id_organizations"),
        ),
        sa.ForeignKeyConstraint(
            ["pricing_id"], ["pricing.id"], name=op.f("fk_tool_calls_pricing_id_pricing")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
    )
    op.create_index(
        op.f("ix_tool_calls_execution_id"), "tool_calls", ["execution_id"], unique=False
    )
    op.create_index(
        "ix_tool_calls_list", "tool_calls", ["organization_id", "created_at", "id"], unique=False
    )
    op.create_index(
        op.f("ix_tool_calls_organization_id"), "tool_calls", ["organization_id"], unique=False
    )
    op.create_table(
        "llm_call_payloads",
        sa.Column("llm_call_id", sa.Uuid(), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id", "organization_id"],
            ["llm_calls.id", "llm_calls.organization_id"],
            name="fk_llm_call_payloads_call_org",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_llm_call_payloads_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("llm_call_id", name=op.f("pk_llm_call_payloads")),
    )
    op.create_index(
        op.f("ix_llm_call_payloads_organization_id"),
        "llm_call_payloads",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    """開発用。費用と LLM・Tool のログは失われる。budget_exceeded の実行は unexpected にする。"""
    op.drop_index(op.f("ix_llm_call_payloads_organization_id"), table_name="llm_call_payloads")
    op.drop_table("llm_call_payloads")
    op.drop_index(op.f("ix_tool_calls_organization_id"), table_name="tool_calls")
    op.drop_index("ix_tool_calls_list", table_name="tool_calls")
    op.drop_index(op.f("ix_tool_calls_execution_id"), table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index(op.f("ix_llm_calls_organization_id"), table_name="llm_calls")
    op.drop_index("ix_llm_calls_list", table_name="llm_calls")
    op.drop_index(op.f("ix_llm_calls_execution_id"), table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_index(
        "uq_budgets_organization",
        table_name="budgets",
        postgresql_where=sa.text("exploration_id IS NULL"),
    )
    op.drop_index(
        "uq_budgets_exploration",
        table_name="budgets",
        postgresql_where=sa.text("exploration_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_budgets_organization_id"), table_name="budgets")
    op.drop_table("budgets")
    op.drop_table("pricing")
    op.drop_constraint(op.f("ck_executions_error_type"), "executions", type_="check")
    op.execute(
        "UPDATE executions SET error_type = 'unexpected' WHERE error_type = 'budget_exceeded'"
    )
    op.create_check_constraint(
        op.f("ck_executions_error_type"),
        "executions",
        f"error_type IS NULL OR error_type IN ({ERROR_TYPES_OLD})",
    )
    op.drop_constraint(op.f("ck_executions_cost_limit_non_negative"), "executions", type_="check")
    op.drop_constraint(op.f("ck_executions_cost_non_negative"), "executions", type_="check")
    op.drop_constraint("uq_executions_id_organization_id", "executions", type_="unique")
    op.drop_column("executions", "cost_limit")
    op.drop_column("executions", "cost_currency")
    op.drop_column("executions", "cost_amount")
