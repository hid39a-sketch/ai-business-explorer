"""migration 0005（非同期実行・primary / secondary・AI社員ごとの版の連鎖）の既存データ移行。

0004 のスキーマに第1回形式の実行データを入れてから 0005 に上げ、次を確認する。
- 既存の実行はすべて primary になり、状態は変わらない。
- analyses.ai_employee_id は、その分析を出した実行の AI社員で埋まる（版番号は変わらない）。
- 分析の AI社員と実行の AI社員の不一致は拒否される。
- downgrade では queued / cancelled を failed に、未開始の開始日時を作成日時にする。
"""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

ORG = "00000000-0000-7000-8000-000000000100"
HUMAN = "00000000-0000-7000-8000-000000000001"
EMP_A = "00000000-0000-7000-8000-00000000a001"
EMP_B = "00000000-0000-7000-8000-00000000a002"
EXPLORATION = "00000000-0000-7000-8000-00000000b001"
RUN1 = "00000000-0000-7000-8000-00000000b011"
RUN2 = "00000000-0000-7000-8000-00000000b012"
EXEC1 = "00000000-0000-7000-8000-00000000b021"
EXEC2 = "00000000-0000-7000-8000-00000000b022"
AN1 = "00000000-0000-7000-8000-00000000b031"
AN2 = "00000000-0000-7000-8000-00000000b032"


def _run(run_id: str, attempt: int, superseded: str) -> str:
    return (
        f"('{run_id}', '{ORG}', '{EXPLORATION}', 'idea_generation', {attempt}, 'initial', "
        f"'{HUMAN}', 'human', 'succeeded', '{{}}', now(), {superseded})"
    )


def _execution(execution_id: str, run_id: str, employee: str) -> str:
    return (
        f"('{execution_id}', '{ORG}', '{run_id}', '{employee}', 1, '{{}}', 'ig', 'sha', "
        f"'succeeded', '{{}}', now())"
    )


def _analysis(analysis_id: str, run_id: str, execution_id: str, version: int) -> str:
    return (
        f"('{analysis_id}', '{ORG}', '{EXPLORATION}', '{run_id}', '{execution_id}', "
        f"'idea_generation', 'v1', {version}, 's', '{{}}', 'pending_review')"
    )


ROUND1_DATA = f"""
INSERT INTO actors (id, actor_type, display_name) VALUES ('{HUMAN}', 'human', 'Human');
INSERT INTO ai_employees (id, organization_id, key, name, role, stage_key, llm_config,
                          allowed_tools, status, version) VALUES
  ('{EMP_A}', '{ORG}', 'a', 'A', 'r', 'idea_generation', '{{}}', '[]', 'active', 1),
  ('{EMP_B}', '{ORG}', 'b', 'B', 'r', 'idea_generation', '{{}}', '[]', 'active', 1);
INSERT INTO explorations (id, organization_id, title, theme, status, created_by_actor_id)
  VALUES ('{EXPLORATION}', '{ORG}', 'E', 'theme', 'active', '{HUMAN}');
INSERT INTO stage_runs (id, organization_id, exploration_id, stage_key, attempt_no, trigger,
                        triggered_by_actor_id, triggered_by_actor_type, status, input_snapshot,
                        started_at, superseded_at) VALUES
  {_run(RUN1, 1, "now()")}, {_run(RUN2, 2, "NULL")};
INSERT INTO executions (id, organization_id, stage_run_id, ai_employee_id, ai_employee_version,
                        ai_employee_snapshot, implementation_key, code_version, status, input,
                        started_at) VALUES
  {_execution(EXEC1, RUN1, EMP_A)}, {_execution(EXEC2, RUN2, EMP_B)};
INSERT INTO analyses (id, organization_id, exploration_id, stage_run_id, execution_id,
                      stage_key, schema_version, version_no, summary, body, review_status) VALUES
  {_analysis(AN1, RUN1, EXEC1, 1)}, {_analysis(AN2, RUN2, EXEC2, 2)};
"""  # noqa: S608  値はすべてこのファイルの定数（外部入力なし）


@pytest.fixture(scope="module")
def migrated(engine: Engine, alembic_cfg: Config) -> Engine:
    command.upgrade(alembic_cfg, "0004")
    with engine.begin() as conn:
        conn.execute(text(ROUND1_DATA))
    command.upgrade(alembic_cfg, "0005")
    return engine


def test_existing_executions_become_primary_and_keep_status(migrated: Engine) -> None:
    with migrated.connect() as conn:
        rows = conn.execute(
            text("SELECT assignment_role, status FROM executions ORDER BY id")
        ).all()
        runs = conn.execute(text("SELECT status, claimed_at, worker_id FROM stage_runs")).all()
    assert [tuple(r) for r in rows] == [("primary", "succeeded"), ("primary", "succeeded")]
    assert {tuple(r) for r in runs} == {("succeeded", None, None)}


def test_analysis_employee_is_backfilled_from_execution(migrated: Engine) -> None:
    with migrated.connect() as conn:
        rows = conn.execute(
            text("SELECT id::text, ai_employee_id::text, version_no FROM analyses ORDER BY id")
        ).all()
    assert [tuple(r) for r in rows] == [(AN1, EMP_A, 1), (AN2, EMP_B, 2)]


def test_analysis_employee_must_match_execution(migrated: Engine) -> None:
    with migrated.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text("UPDATE analyses SET ai_employee_id = :employee WHERE id = :id"),
            {"employee": EMP_B, "id": AN1},
        )


def test_downgrade_maps_new_statuses(migrated: Engine, alembic_cfg: Config) -> None:
    with migrated.begin() as conn:
        conn.execute(
            text("UPDATE stage_runs SET status = 'cancelled' WHERE id = :id"), {"id": RUN2}
        )
        conn.execute(
            text("UPDATE executions SET status = 'queued', started_at = NULL WHERE id = :id"),
            {"id": EXEC2},
        )
    command.downgrade(alembic_cfg, "0004")
    with migrated.connect() as conn:
        run_status = conn.execute(
            text("SELECT status FROM stage_runs WHERE id = :id"), {"id": RUN2}
        ).scalar_one()
        execution = conn.execute(
            text("SELECT status, started_at = created_at FROM executions WHERE id = :id"),
            {"id": EXEC2},
        ).one()
    assert run_status == "failed"
    assert tuple(execution) == ("failed", True)
    command.upgrade(alembic_cfg, "0005")
