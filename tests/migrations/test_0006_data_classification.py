"""migration 0006（データ分類）の既存データ移行。

0005 のスキーマに第1回形式のデータを入れてから 0006 に上げ、既存の探索案件・Evidence・分析が
すべて internal になること、4段階以外の値を拒否することを確認する。
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.migrations.test_0005_async_execution import EXPLORATION, HUMAN, ORG, ROUND1_DATA

EVIDENCE = "00000000-0000-7000-8000-00000000c001"


@pytest.fixture(scope="module")
def migrated(engine: Engine, alembic_cfg: Config) -> Engine:
    command.upgrade(alembic_cfg, "0004")
    with engine.begin() as conn:
        conn.execute(text(ROUND1_DATA))
        conn.execute(
            text(
                "INSERT INTO evidence (id, organization_id, exploration_id, source_type, title, "
                "content_hash, metadata, created_by_actor_id) "
                "VALUES (:id, :org, :exp, 'human_input', 'Ev', 'h', '{}', :human)"
            ),
            {"id": EVIDENCE, "org": ORG, "exp": EXPLORATION, "human": HUMAN},
        )
    command.upgrade(alembic_cfg, "0006")
    return engine


def test_existing_rows_become_internal(migrated: Engine) -> None:
    with migrated.connect() as conn:
        for table in ("explorations", "evidence", "analyses"):
            values = (
                conn.execute(
                    text(f"SELECT DISTINCT classification FROM {table}")  # noqa: S608  固定のテーブル名
                )
                .scalars()
                .all()
            )
            assert values == ["internal"], table


def test_only_the_four_levels_are_allowed(migrated: Engine) -> None:
    with migrated.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(text("UPDATE evidence SET classification = 'secret'"))


def test_downgrade_and_upgrade_again(migrated: Engine, alembic_cfg: Config) -> None:
    command.downgrade(alembic_cfg, "0005")
    command.upgrade(alembic_cfg, "0006")
    with migrated.connect() as conn:
        assert conn.execute(text("SELECT classification FROM explorations")).scalar_one() == (
            "internal"
        )
