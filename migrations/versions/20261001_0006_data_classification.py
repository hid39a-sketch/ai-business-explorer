"""data classification

第2回仕様 11章（データ分類。E-04・R-08）。

- explorations と evidence に classification（public / internal / confidential / restricted）を
  追加する。既存の行は既定の internal。
- analyses に classification を追加する。値は入力の最も高い分類（算出値）。既存の分析の入力は
  すべて internal（上で付けた既定値）なので、既存の分析も internal になる。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01 16:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("explorations", "evidence", "analyses")
VALUES = "'public', 'internal', 'confidential', 'restricted'"


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "classification",
                sa.String(length=16),
                nullable=False,
                server_default="internal",  # 既存の行（第1回の既定）
            ),
        )
        op.alter_column(table, "classification", server_default=None)
        op.create_check_constraint(
            op.f(f"ck_{table}_classification"), table, f"classification IN ({VALUES})"
        )


def downgrade() -> None:
    """開発用。分類の記録は失われる。"""
    for table in reversed(TABLES):
        op.drop_constraint(op.f(f"ck_{table}_classification"), table, type_="check")
        op.drop_column(table, "classification")
