"""migration 0003（claims と claim_evidence_links）の既存データ移行（第2回仕様 D-15・R-14、16章）。

0002 のスキーマに第1回の分析データを入れてから 0003 に上げ、次を確認する。
- 各Analysisの claims の件数と内容が body.claims と一致する（並び順も）。
- claim_evidence_links が旧 analysis_evidence_links の全行を漏れなく含む。
- analyses.body（body.claims を含む）が変わらない。
- 旧テーブルは削除されず、移行後は INSERT・UPDATE・DELETE・TRUNCATE が拒否される。
- 旧リンクを取りこぼす場合は移行が中止される。
"""

from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

ORG = "00000000-0000-7000-8000-000000000100"
HUMAN = "00000000-0000-7000-8000-000000000001"
EMPLOYEE = "00000000-0000-7000-8000-00000000a002"
EXPLORATION = "00000000-0000-7000-8000-00000000b001"
IDEA = "00000000-0000-7000-8000-00000000b002"
STAGE_RUN = "00000000-0000-7000-8000-00000000b003"
EXECUTION = "00000000-0000-7000-8000-00000000b004"
ANALYSIS = "00000000-0000-7000-8000-00000000b005"
EV1 = "00000000-0000-7000-8000-00000000c001"
EV2 = "00000000-0000-7000-8000-00000000c002"

BODY = {
    "claims": [
        {
            "id": "e1",
            "text": "市場は拡大している",
            "kind": "evidence_based",
            "evidence_refs": [
                {"evidence_id": EV1, "relation": "supports"},
                {"evidence_id": EV2, "relation": "contradicts"},
            ],
        },
        {"id": "i1", "text": "需要が見込める", "kind": "inference", "evidence_refs": []},
        {
            "id": "s1",
            "text": "競合が参入するかもしれない",
            "kind": "speculation",
            "evidence_refs": [{"evidence_id": EV1, "relation": "context"}],
        },
    ],
    "data": {"market_overview": "x"},
}
OLD_LINKS = {(EV1, "e1", "supports"), (EV2, "e1", "contradicts"), (EV1, "s1", "context")}

ROUND1_DATA = f"""
INSERT INTO actors (id, actor_type, display_name) VALUES ('{HUMAN}', 'human', 'Human');
INSERT INTO ai_employees (id, organization_id, key, name, role, stage_key, llm_config,
                          allowed_tools, status, version)
  VALUES ('{EMPLOYEE}', '{ORG}', 'mr', 'MR', 'r', 'market_research', '{{}}', '[]', 'active', 1);
INSERT INTO explorations (id, organization_id, title, theme, status, created_by_actor_id)
  VALUES ('{EXPLORATION}', '{ORG}', 'E', 'theme', 'active', '{HUMAN}');
INSERT INTO ideas (id, organization_id, exploration_id, title, origin_type, adoption_status)
  VALUES ('{IDEA}', '{ORG}', '{EXPLORATION}', 'I', 'human', 'adopted');
INSERT INTO stage_runs (id, organization_id, exploration_id, idea_id, stage_key, attempt_no,
                        trigger, triggered_by_actor_id, triggered_by_actor_type, status,
                        input_snapshot, started_at)
  VALUES ('{STAGE_RUN}', '{ORG}', '{EXPLORATION}', '{IDEA}', 'market_research', 1, 'initial',
          '{HUMAN}', 'human', 'succeeded', '{{}}', now());
INSERT INTO executions (id, organization_id, stage_run_id, ai_employee_id, idea_id,
                        ai_employee_version, ai_employee_snapshot, implementation_key,
                        code_version, status, input, started_at)
  VALUES ('{EXECUTION}', '{ORG}', '{STAGE_RUN}', '{EMPLOYEE}', '{IDEA}', 1, '{{}}', 'mr', 'sha',
          'succeeded', '{{}}', now());
INSERT INTO evidence (id, organization_id, exploration_id, idea_id, source_type, title,
                      content_hash, metadata, created_by_actor_id) VALUES
  ('{EV1}', '{ORG}', '{EXPLORATION}', '{IDEA}', 'human_input', 'Ev1', 'h1', '{{}}', '{HUMAN}'),
  ('{EV2}', '{ORG}', '{EXPLORATION}', '{IDEA}', 'human_input', 'Ev2', 'h2', '{{}}', '{HUMAN}');
"""  # noqa: S608  値はすべてこのファイルの定数（外部入力なし）

INSERT_ANALYSIS = text(
    "INSERT INTO analyses (id, organization_id, exploration_id, idea_id, stage_run_id, "
    "execution_id, stage_key, schema_version, version_no, summary, body, review_status) "
    "VALUES (:id, :org, :exp, :idea, :run, :exe, 'market_research', 'v1', 1, 's', "
    "CAST(:body AS jsonb), 'pending_review')"
)
INSERT_OLD_LINK = text(
    "INSERT INTO analysis_evidence_links (analysis_id, evidence_id, claim_ref, relation) "
    "VALUES (:analysis, :evidence, :claim, :relation)"
)


@pytest.fixture(scope="module")
def migrated(engine: Engine, alembic_cfg: Config) -> None:
    import json

    command.upgrade(alembic_cfg, "0002")
    with engine.begin() as conn:
        conn.execute(text(ROUND1_DATA))
        conn.execute(
            INSERT_ANALYSIS,
            {
                "id": ANALYSIS,
                "org": ORG,
                "exp": EXPLORATION,
                "idea": IDEA,
                "run": STAGE_RUN,
                "exe": EXECUTION,
                "body": json.dumps(BODY, ensure_ascii=False),
            },
        )
        for evidence, claim, relation in OLD_LINKS:
            conn.execute(
                INSERT_OLD_LINK,
                {"analysis": ANALYSIS, "evidence": evidence, "claim": claim, "relation": relation},
            )
    command.upgrade(alembic_cfg, "0003")


def _rows(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql)).all()]


def test_claims_match_body_claims(engine: Engine, migrated: None) -> None:
    rows = _rows(
        engine,
        "SELECT claim_key, ordinal, kind, text, organization_id::text FROM claims ORDER BY ordinal",
    )
    expected = [
        (c["id"], i, c["kind"], c["text"], ORG)
        for i, c in enumerate(BODY["claims"])  # type: ignore[arg-type]
    ]
    assert rows == expected


def test_claim_evidence_links_contain_every_old_link(engine: Engine, migrated: None) -> None:
    rows = _rows(
        engine,
        "SELECT l.evidence_id::text, c.claim_key, l.relation FROM claim_evidence_links AS l "
        "JOIN claims AS c ON c.id = l.claim_id",
    )
    assert set(rows) == OLD_LINKS
    assert len(rows) == len(OLD_LINKS)


def test_body_is_unchanged(engine: Engine, migrated: None) -> None:
    [(body,)] = _rows(engine, "SELECT body FROM analyses")
    assert body == BODY


def test_old_table_is_kept_and_frozen(engine: Engine, migrated: None) -> None:
    old_select = "SELECT evidence_id::text, claim_ref, relation FROM analysis_evidence_links"
    assert set(_rows(engine, old_select)) == OLD_LINKS
    for sql in (
        f"INSERT INTO analysis_evidence_links VALUES ('{ANALYSIS}', '{EV2}', 'i1', 'context')",  # noqa: S608
        "UPDATE analysis_evidence_links SET relation = 'context'",
        "DELETE FROM analysis_evidence_links",
        "TRUNCATE analysis_evidence_links CASCADE",
    ):
        with engine.begin() as conn, pytest.raises(DBAPIError, match="frozen"):
            conn.execute(text(sql))
    assert set(_rows(engine, old_select)) == OLD_LINKS


def test_migration_aborts_if_an_old_link_would_be_lost(
    engine: Engine, alembic_cfg: Config, migrated: None
) -> None:
    command.downgrade(alembic_cfg, "0002")  # 0003 の後に作られた主張・根拠は失われる（開発用）
    with engine.begin() as conn:
        # body.claims に存在しない主張を指す旧リンク（移行すると取りこぼす）
        conn.execute(
            INSERT_OLD_LINK,
            {"analysis": ANALYSIS, "evidence": EV2, "claim": "missing", "relation": "supports"},
        )
    with pytest.raises(DBAPIError, match="backfill mismatch"):
        command.upgrade(alembic_cfg, "0003")
    assert _rows(engine, "SELECT version_num FROM alembic_version") == [("0002",)]
