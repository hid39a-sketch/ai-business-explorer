"""llm call request params

第2回仕様 11章（SC候補-9）。LLM の呼び出しごとに、実際に送った設定（temperature・thinking・
effort・max_tokens）を llm_calls.request_params（JSONB）に記録する。送っていない項目は
{"sent": false} で記録する。秘密情報（API キー・認証ヘッダーなど）は入れない。

既存の行は null のまま（記録がない）。値を推測して埋めることはしない。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-02 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_calls",
        sa.Column("request_params", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_calls", "request_params")
