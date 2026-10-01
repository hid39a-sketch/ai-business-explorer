"""claims and claim_evidence_links

第2回仕様 5章・6章（D-15、B-15、R-14）。

- claims / claim_evidence_links を新設し、正本にする。
- 第1回の既存Analysisは analyses.body.claims から claims を作り、
  analysis_evidence_links から claim_evidence_links を作る（バックフィル）。analyses.body は変えない。
- human_reviews に claim_id（任意）を追加する。主張が対象の分析に属することは複合 FK で保証する。
- バックフィルの後、analysis_evidence_links を凍結する（INSERT・UPDATE・DELETE を拒否する行トリガーと、
  TRUNCATE を拒否する文トリガー）。テーブルは削除せず、履歴と互換性の確認のために残す。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FREEZE_FUNCTION = "reject_frozen_analysis_evidence_links"


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_evidence_id_organization_id", "evidence", ["id", "organization_id"]
    )

    op.create_table(
        "claims",
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("claim_key", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "kind IN ('evidence_based', 'inference', 'speculation')", name=op.f("ck_claims_kind")
        ),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_claims_ordinal_non_negative")),
        sa.ForeignKeyConstraint(
            ["analysis_id", "organization_id"],
            ["analyses.id", "analyses.organization_id"],
            name="fk_claims_analysis_org",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_id"], ["analyses.id"], name=op.f("fk_claims_analysis_id_analyses")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_claims_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
        sa.UniqueConstraint("analysis_id", "claim_key", name="uq_claims_analysis_id_claim_key"),
        sa.UniqueConstraint("id", "analysis_id", name="uq_claims_id_analysis_id"),
        sa.UniqueConstraint("id", "organization_id", name="uq_claims_id_organization_id"),
    )
    op.create_index(op.f("ix_claims_analysis_id"), "claims", ["analysis_id"])
    op.create_index(op.f("ix_claims_organization_id"), "claims", ["organization_id"])

    op.create_table(
        "claim_evidence_links",
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(length=16), nullable=False),
        _created_at(),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "relation IN ('supports', 'contradicts', 'context')",
            name=op.f("ck_claim_evidence_links_relation"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id", "organization_id"],
            ["claims.id", "claims.organization_id"],
            name="fk_claim_evidence_links_claim_org",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claims.id"], name=op.f("fk_claim_evidence_links_claim_id_claims")
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id", "organization_id"],
            ["evidence.id", "evidence.organization_id"],
            name="fk_claim_evidence_links_evidence_org",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence.id"],
            name=op.f("fk_claim_evidence_links_evidence_id_evidence"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_claim_evidence_links_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint(
            "claim_id", "evidence_id", "relation", name=op.f("pk_claim_evidence_links")
        ),
    )
    op.create_index(
        op.f("ix_claim_evidence_links_evidence_id"), "claim_evidence_links", ["evidence_id"]
    )
    op.create_index(
        op.f("ix_claim_evidence_links_organization_id"),
        "claim_evidence_links",
        ["organization_id"],
    )

    op.add_column("human_reviews", sa.Column("claim_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_human_reviews_claim_id"), "human_reviews", ["claim_id"])
    op.create_foreign_key(
        "fk_human_reviews_claim_analysis",
        "human_reviews",
        "claims",
        ["claim_id", "analysis_id"],
        ["id", "analysis_id"],
    )

    # --- バックフィル：body.claims → claims（並び順は配列の順。body は変えない）
    op.execute(
        "INSERT INTO claims (id, organization_id, analysis_id, claim_key, ordinal, kind, text, "
        "created_at) "
        "SELECT gen_random_uuid(), a.organization_id, a.id, c.value->>'id', "
        "       (c.ordinality - 1)::int, c.value->>'kind', c.value->>'text', a.created_at "
        "FROM analyses AS a "
        "CROSS JOIN LATERAL jsonb_array_elements(COALESCE(a.body->'claims', '[]'::jsonb)) "
        "     WITH ORDINALITY AS c(value, ordinality)"
    )
    # --- バックフィル：analysis_evidence_links → claim_evidence_links
    op.execute(
        "INSERT INTO claim_evidence_links (claim_id, evidence_id, relation, organization_id, "
        "created_at) "
        "SELECT c.id, l.evidence_id, l.relation, c.organization_id, c.created_at "
        "FROM analysis_evidence_links AS l "
        "JOIN claims AS c ON c.analysis_id = l.analysis_id AND c.claim_key = l.claim_ref"
    )
    # 旧リンクを1件も取りこぼしていないことを確認する（取りこぼすなら移行を中止する）。
    op.execute(
        "DO $$ DECLARE old_count bigint; new_count bigint; BEGIN "
        "SELECT count(*) INTO old_count FROM analysis_evidence_links; "
        "SELECT count(*) INTO new_count FROM claim_evidence_links; "
        "IF old_count <> new_count THEN "
        "  RAISE EXCEPTION 'claim_evidence_links backfill mismatch: % old links, % new links', "
        "    old_count, new_count; "
        "END IF; END $$"
    )

    # --- 旧リンクテーブルの凍結（R-14）
    op.execute(
        f"CREATE FUNCTION {FREEZE_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION USING "
        "  ERRCODE = 'object_not_in_prerequisite_state', "
        "  MESSAGE = 'analysis_evidence_links is frozen; use claim_evidence_links'; "
        "END $$"
    )
    op.execute(
        "CREATE TRIGGER analysis_evidence_links_frozen_rows "
        "BEFORE INSERT OR UPDATE OR DELETE ON analysis_evidence_links "
        f"FOR EACH ROW EXECUTE FUNCTION {FREEZE_FUNCTION}()"
    )
    op.execute(
        "CREATE TRIGGER analysis_evidence_links_frozen_truncate "
        "BEFORE TRUNCATE ON analysis_evidence_links "
        f"FOR EACH STATEMENT EXECUTE FUNCTION {FREEZE_FUNCTION}()"
    )


def downgrade() -> None:
    """開発用。0003 以降に作った分析の主張と根拠リンクは失われる（旧テーブルには戻さない）。"""
    op.execute("DROP TRIGGER analysis_evidence_links_frozen_truncate ON analysis_evidence_links")
    op.execute("DROP TRIGGER analysis_evidence_links_frozen_rows ON analysis_evidence_links")
    op.execute(f"DROP FUNCTION {FREEZE_FUNCTION}()")

    op.drop_constraint("fk_human_reviews_claim_analysis", "human_reviews", type_="foreignkey")
    op.drop_index(op.f("ix_human_reviews_claim_id"), table_name="human_reviews")
    op.drop_column("human_reviews", "claim_id")

    op.drop_index(
        op.f("ix_claim_evidence_links_organization_id"), table_name="claim_evidence_links"
    )
    op.drop_index(op.f("ix_claim_evidence_links_evidence_id"), table_name="claim_evidence_links")
    op.drop_table("claim_evidence_links")
    op.drop_index(op.f("ix_claims_organization_id"), table_name="claims")
    op.drop_index(op.f("ix_claims_analysis_id"), table_name="claims")
    op.drop_table("claims")
    op.drop_constraint("uq_evidence_id_organization_id", "evidence", type_="unique")
