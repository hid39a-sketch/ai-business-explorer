"""migration 0008（Evidence 候補・来歴・収集のみ）の既存データ移行。

既存の Evidence はすべて human_input（Tool の来歴なし）、既存の試行はすべて analyze になり、
「最新の試行は1つ」の制約は analyze に限られることを確認する。
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.migrations.test_0005_async_execution import EXPLORATION, HUMAN, ORG, ROUND1_DATA

EVIDENCE = "00000000-0000-7000-8000-00000000c001"
COLLECT = "00000000-0000-7000-8000-00000000d001"
ANALYZE = "00000000-0000-7000-8000-00000000d002"


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
    command.upgrade(alembic_cfg, "0008")
    return engine


def test_existing_rows_get_defaults(migrated: Engine) -> None:
    with migrated.connect() as conn:
        evidence = conn.execute(
            text("SELECT acquisition_method, candidate_id, tool_call_id FROM evidence")
        ).one()
        modes = conn.execute(text("SELECT DISTINCT mode FROM stage_runs")).scalars().all()
    assert tuple(evidence) == ("human_input", None, None)
    assert modes == ["analyze"]


def test_collect_only_runs_do_not_count_as_current(migrated: Engine) -> None:
    insert = text(
        "INSERT INTO stage_runs (id, organization_id, exploration_id, stage_key, attempt_no, "
        "trigger, mode, triggered_by_actor_id, triggered_by_actor_type, status, input_snapshot, "
        "started_at) VALUES (:id, :org, :exp, 'idea_generation', :attempt, 'initial', :mode, "
        ":human, 'human', 'succeeded', '{}', now())"
    )
    params = {"org": ORG, "exp": EXPLORATION, "human": HUMAN}
    with migrated.begin() as conn:
        conn.execute(insert, {**params, "id": COLLECT, "attempt": 3, "mode": "collect_only"})
    # analyze の最新の試行（ROUND1_DATA の2回目）がすでにあるので、analyze は2つ目を作れない
    with migrated.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(
            insert,
            {**params, "id": ANALYZE, "attempt": 4, "mode": "analyze"},
        )


def test_tool_evidence_needs_provenance(migrated: Engine) -> None:
    with migrated.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(text("UPDATE evidence SET acquisition_method = 'tool'"))


def test_downgrade_and_upgrade_again(migrated: Engine, alembic_cfg: Config) -> None:
    command.downgrade(alembic_cfg, "0007")
    with migrated.connect() as conn:
        superseded = conn.execute(
            text("SELECT superseded_at IS NOT NULL FROM stage_runs WHERE id = :id"),
            {"id": COLLECT},
        ).scalar_one()
    assert superseded is True
    command.upgrade(alembic_cfg, "0008")
