"""migration 0009（llm_calls.request_params。第2回仕様 11章 SC候補-9）。

既存の LLM 呼び出しの行は null のまま（値を推測して埋めない）。新しい値を保存でき、downgrade で
列が消え、もう一度 upgrade すると null に戻ることを確認する。
"""

import json

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from tests.migrations.test_0005_async_execution import EXEC1, ORG, ROUND1_DATA

CALL = "00000000-0000-7000-8000-00000000c001"
PARAMS = {
    "temperature": {"sent": True, "value": 0},
    "thinking": {"sent": False},
    "effort": {"sent": False},
    "max_tokens": {"sent": True, "value": 4000},
}


@pytest.fixture(scope="module")
def migrated(engine: Engine, alembic_cfg: Config) -> Engine:
    command.upgrade(alembic_cfg, "0004")
    with engine.begin() as conn:
        conn.execute(text(ROUND1_DATA))
    command.upgrade(alembic_cfg, "0008")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO llm_calls (id, organization_id, execution_id, provider, model, "
                "prompt_key, prompt_version, input_tokens, output_tokens, cost_amount, currency, "
                "latency_ms, status, classification, payload_mode) "
                "VALUES (:id, :org, :execution, 'fake', 'fake-model-v1', 'ig', 'v1', 0, 0, 0, "
                "'USD', 0, 'succeeded', 'internal', 'full')"
            ),
            {"id": CALL, "org": ORG, "execution": EXEC1},
        )
    command.upgrade(alembic_cfg, "0009")
    return engine


def _params(engine: Engine) -> object:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT request_params FROM llm_calls WHERE id = :id"), {"id": CALL}
        ).scalar_one()


def test_existing_calls_have_no_request_params(migrated: Engine) -> None:
    assert _params(migrated) is None


def test_request_params_round_trip_and_downgrade(migrated: Engine, alembic_cfg: Config) -> None:
    with migrated.begin() as conn:
        conn.execute(
            text("UPDATE llm_calls SET request_params = CAST(:p AS jsonb) WHERE id = :id"),
            {"p": json.dumps(PARAMS), "id": CALL},
        )
    assert _params(migrated) == PARAMS
    command.downgrade(alembic_cfg, "0008")
    columns = {c["name"] for c in inspect(migrated).get_columns("llm_calls")}
    assert "request_params" not in columns
    command.upgrade(alembic_cfg, "0009")
    assert _params(migrated) is None
