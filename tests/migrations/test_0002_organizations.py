"""migration 0002（組織とロール）の既存データ移行（第2回仕様 E-05）。

第1回のスキーマ（0001）にデータを入れてから 0002 に上げ、次を確認する。
- 第1回のすべての行が既定組織に移る（行数・内容は変わらない）。
- 既存の人間 actor は admin になり、system actor にはロールが付かない。
- 組織×ステージごとに最も古い active のAI社員が primary になる。
- downgrade で 0001 に戻せる。
"""

import os
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, make_url, text

from tests.conftest import ROOT, TEST_DATABASE_URL

DEFAULT_ORG = "00000000-0000-7000-8000-000000000100"
HUMAN = "00000000-0000-7000-8000-000000000001"
SYSTEM = "00000000-0000-7000-8000-000000000002"
IDEA_GEN = "00000000-0000-7000-8000-00000000a001"
MARKET = "00000000-0000-7000-8000-00000000a002"
MARKET_LATER = "00000000-0000-7000-8000-00000000a003"
MARKET_INACTIVE = "00000000-0000-7000-8000-00000000a004"
EXPLORATION = "00000000-0000-7000-8000-00000000b001"
IDEA = "00000000-0000-7000-8000-00000000b002"
STAGE_RUN = "00000000-0000-7000-8000-00000000b003"
EXECUTION = "00000000-0000-7000-8000-00000000b004"
ANALYSIS = "00000000-0000-7000-8000-00000000b005"
EVIDENCE = "00000000-0000-7000-8000-00000000b006"

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

# 値はすべてこのファイルの定数（外部入力なし）。
ROUND1_DATA = f"""
INSERT INTO actors (id, actor_type, display_name) VALUES
  ('{HUMAN}', 'human', 'Human'), ('{SYSTEM}', 'system', 'System');

INSERT INTO ai_employees (id, key, name, role, stage_key, implementation_key, llm_config,
                          allowed_tools, status, version, created_at, updated_at) VALUES
  ('{IDEA_GEN}', 'idea_generator', 'IG', 'r', 'idea_generation', 'idea_generator', '{{}}', '[]',
   'active', 1, '2026-01-01', '2026-01-01'),
  ('{MARKET}', 'market_researcher', 'MR', 'r', 'market_research', 'market_researcher', '{{}}',
   '[]', 'active', 1, '2026-01-02', '2026-01-02'),
  ('{MARKET_LATER}', 'market_later', 'ML', 'r', 'market_research', NULL, '{{}}', '[]',
   'active', 1, '2026-01-03', '2026-01-03'),
  ('{MARKET_INACTIVE}', 'market_old', 'MO', 'r', 'market_research', NULL, '{{}}', '[]',
   'inactive', 1, '2025-12-31', '2025-12-31');

INSERT INTO explorations (id, title, theme, status, created_by_actor_id)
  VALUES ('{EXPLORATION}', 'E', 'theme', 'active', '{HUMAN}');

INSERT INTO ideas (id, exploration_id, title, origin_type, adoption_status, created_by_actor_id)
  VALUES ('{IDEA}', '{EXPLORATION}', 'I', 'human', 'adopted', '{HUMAN}');

INSERT INTO stage_runs (id, exploration_id, idea_id, stage_key, attempt_no, trigger,
                        triggered_by_actor_id, triggered_by_actor_type, status, input_snapshot,
                        started_at)
  VALUES ('{STAGE_RUN}', '{EXPLORATION}', '{IDEA}', 'market_research', 1, 'initial', '{HUMAN}',
          'human', 'succeeded', '{{}}', now());

INSERT INTO executions (id, stage_run_id, ai_employee_id, idea_id, ai_employee_version,
                        ai_employee_snapshot, implementation_key, code_version, status, input,
                        started_at)
  VALUES ('{EXECUTION}', '{STAGE_RUN}', '{MARKET}', '{IDEA}', 1, '{{}}', 'market_researcher',
          'sha', 'succeeded', '{{}}', now());

INSERT INTO analyses (id, exploration_id, idea_id, stage_run_id, execution_id, stage_key,
                      schema_version, version_no, summary, body, review_status)
  VALUES ('{ANALYSIS}', '{EXPLORATION}', '{IDEA}', '{STAGE_RUN}', '{EXECUTION}',
          'market_research', 'v1', 1, 's', '{{"claims": [], "data": {{}}}}', 'approved');

INSERT INTO evidence (id, exploration_id, idea_id, source_type, title, content_hash, metadata,
                      created_by_actor_id)
  VALUES ('{EVIDENCE}', '{EXPLORATION}', '{IDEA}', 'human_input', 'Ev', 'h', '{{}}', '{HUMAN}');

INSERT INTO human_reviews (id, analysis_id, exploration_id, idea_id, reviewer_actor_id,
                           reviewer_actor_type, decision)
  VALUES (gen_random_uuid(), '{ANALYSIS}', '{EXPLORATION}', '{IDEA}', '{HUMAN}', 'human',
          'approve');

INSERT INTO human_decisions (id, idea_id, decided_by_actor_id, decided_by_actor_type, decision,
                             rationale, based_on_review_ids)
  VALUES (gen_random_uuid(), '{IDEA}', '{HUMAN}', 'human', 'go', 'r', '[]');

INSERT INTO audit_events (id, entity_type, entity_id, action, actor_id)
  VALUES (gen_random_uuid(), 'exploration', '{EXPLORATION}', 'created', '{HUMAN}');
"""  # noqa: S608


@pytest.fixture(scope="module")
def migration_url() -> Iterator[str]:
    """このテスト専用のデータベース（他のテストのスキーマに触れない）。"""
    base = make_url(TEST_DATABASE_URL)
    name = f"{base.database}_migration"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield base.set(database=name).render_as_string(hide_password=False)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="module")
def engine(migration_url: str) -> Iterator[Engine]:
    engine = create_engine(migration_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def alembic_cfg(migration_url: str) -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", migration_url)
    return cfg


def _counts(conn: Connection) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    for table in SCOPED_TABLES:
        query = text(f"SELECT count(*) FROM {table}")  # noqa: S608  固定のテーブル名
        counts[table] = conn.execute(query).scalar_one()
    return counts


@pytest.fixture(scope="module")
def migrated(engine: Engine, alembic_cfg: Config) -> dict[str, Any]:
    command.upgrade(alembic_cfg, "0001")
    with engine.begin() as conn:
        conn.execute(text(ROUND1_DATA))
        before = _counts(conn)
    command.upgrade(alembic_cfg, "0002")
    return before


def test_all_rows_move_to_the_default_organization(
    engine: Engine, migrated: dict[str, Any]
) -> None:
    with engine.connect() as conn:
        assert _counts(conn) == migrated
        for table in SCOPED_TABLES:
            query = text(f"SELECT DISTINCT organization_id::text FROM {table}")  # noqa: S608
            orgs = conn.execute(query)
            assert orgs.scalars().all() == [DEFAULT_ORG], table
        name = conn.execute(
            text("SELECT name FROM organizations WHERE id = CAST(:id AS uuid)"), {"id": DEFAULT_ORG}
        ).scalar_one()
        assert name == "Default Organization"


def test_existing_data_is_unchanged(engine: Engine, migrated: dict[str, Any]) -> None:
    with engine.connect() as conn:
        body = conn.execute(
            text("SELECT body FROM analyses WHERE id = CAST(:id AS uuid)"), {"id": ANALYSIS}
        ).scalar_one()
        assert body == {"claims": [], "data": {}}
        review_status = conn.execute(text("SELECT review_status FROM analyses")).scalar_one()
        assert review_status == "approved"


def test_humans_become_admin_and_system_gets_no_role(
    engine: Engine, migrated: dict[str, Any]
) -> None:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT actor_id::text, role, organization_id::text FROM organization_memberships")
        ).all()
    assert [tuple(r) for r in rows] == [(HUMAN, "admin", DEFAULT_ORG)]


def test_oldest_active_employee_becomes_primary(engine: Engine, migrated: dict[str, Any]) -> None:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT stage_key, ai_employee_id::text, role FROM stage_assignments")
        ).all()
    assert sorted(tuple(r) for r in rows) == [
        ("idea_generation", IDEA_GEN, "primary"),
        ("market_research", MARKET, "primary"),
    ]


def test_downgrade_returns_to_round1_schema(
    engine: Engine, alembic_cfg: Config, migrated: dict[str, Any]
) -> None:
    command.downgrade(alembic_cfg, "0001")
    with engine.connect() as conn:
        assert _counts(conn) == migrated
        has_column = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'explorations' AND column_name = 'organization_id'"
            )
        ).scalar_one()
        assert has_column == 0
    command.upgrade(alembic_cfg, "0002")
