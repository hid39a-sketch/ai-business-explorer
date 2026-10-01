"""migration 0007（費用管理と LLM・Tool のログ）の既存データ移行。

0006 のスキーマに既存の実行を入れてから 0007 に上げ、既存の実行の費用が 0 USD（上限なし）に
なること、budget_exceeded を記録できること、downgrade で unexpected に戻ることを確認する。
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from tests.migrations.test_0005_async_execution import EXEC1, ROUND1_DATA


@pytest.fixture(scope="module")
def migrated(engine: Engine, alembic_cfg: Config) -> Engine:
    command.upgrade(alembic_cfg, "0004")
    with engine.begin() as conn:
        conn.execute(text(ROUND1_DATA))
    command.upgrade(alembic_cfg, "0007")
    return engine


def test_existing_executions_have_zero_cost(migrated: Engine) -> None:
    with migrated.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT cost_amount, cost_currency, cost_limit FROM executions")
        ).all()
    assert [tuple(r) for r in rows] == [(0, "USD", None)]


def test_budget_exceeded_is_recorded_and_downgraded(migrated: Engine, alembic_cfg: Config) -> None:
    with migrated.begin() as conn:
        conn.execute(
            text(
                "UPDATE executions SET status = 'failed', error_type = 'budget_exceeded' "
                "WHERE id = :id"
            ),
            {"id": EXEC1},
        )
    command.downgrade(alembic_cfg, "0006")
    with migrated.connect() as conn:
        error_type = conn.execute(
            text("SELECT error_type FROM executions WHERE id = :id"), {"id": EXEC1}
        ).scalar_one()
    assert error_type == "unexpected"
    command.upgrade(alembic_cfg, "0007")
