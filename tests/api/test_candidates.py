"""Evidence 候補・AI生成の補助情報・収集のみ（第2回仕様 2章・3章・4章。B-21・E-02・R-07）。

実際の Tool には接続しない。記録した応答を返す Fake Tool と、テスト専用の AI社員を使う。
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_business_explorer.agents.base import (
    Agent,
    AgentContext,
    AnalysisDraft,
    CandidateNote,
    Claim,
    EvidenceRef,
)
from ai_business_explorer.application.candidates import delete_expired_tool_data
from ai_business_explorer.domain.enums import ClaimKind
from ai_business_explorer.infrastructure.db.models import (
    Analysis,
    Evidence,
    EvidenceCandidate,
    ToolCallOutput,
)
from ai_business_explorer.seed import DEFAULT_ORGANIZATION_ID
from ai_business_explorer.tools.base import (
    EvidenceCandidate as ToolCandidate,
)
from ai_business_explorer.tools.base import (
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
)
from tests.conftest import Api, make_human

AI_NOTE = "AIの解釈：この市場は急成長している（AI生成）"
PAGE_TEXT = "市場規模は2025年に1兆円に達した。" * 10


class _SearchIn(BaseModel):
    query: str


class _SearchOut(BaseModel):
    hits: int


class FakeSearchTool(Tool):
    """記録した応答を返す Fake Tool（外部には接続しない）。"""

    name: ClassVar[str] = "fake_search"
    version: ClassVar[str] = "1"
    description: ClassVar[str] = "test only"
    side_effect: ClassVar[ToolSideEffect] = ToolSideEffect.READ_ONLY
    input_model: ClassVar[type[BaseModel]] = _SearchIn
    output_model: ClassVar[type[BaseModel]] = _SearchOut

    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = [
            {"url": "https://Example.com/report?utm_source=x", "title": "市場レポート"}
        ]
        self.body = PAGE_TEXT

    def execute(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(
            output={"hits": len(self.pages)},
            evidence_candidates=[
                ToolCandidate(
                    source_type="web",
                    title=page["title"],
                    url=page["url"],
                    snapshot=self.body,
                    retrieved_at=datetime(2026, 9, 30, tzinfo=UTC),
                    metadata={"rank": i},
                )
                for i, page in enumerate(self.pages)
            ],
        )


class _Empty(BaseModel):
    pass


class _Collector(Agent):
    """Tool で候補を集め、候補ごとに AI生成の補助情報を付けるテスト専用の AI社員。"""

    implementation_key = "test_collector"
    stage_key = "idea_generation"
    output_schema_version = "test.v1"
    input_model = _Empty
    output_model = _Empty
    cite_candidate = False
    note_for: UUID | None = None

    def run(self, ctx: AgentContext) -> AnalysisDraft:
        result = ctx.tools.call("fake_search", {"query": f"{ctx.exploration.theme} 市場規模"})
        ids = [c.candidate_id for c in result.evidence_candidates if c.candidate_id]
        claims = []
        if self.cite_candidate and ids:
            # 未承認の候補を根拠に指定する（検証エラーになるはず）
            claims = [
                Claim(
                    id="e1",
                    text="市場は大きい",
                    kind=ClaimKind.EVIDENCE_BASED,
                    evidence_refs=[EvidenceRef(evidence_id=ids[0])],
                )
            ]
        targets = [self.note_for] if self.note_for else ids
        return AnalysisDraft(
            summary=f"{ctx.mode}: collected {len(ids)}",
            claims=claims,
            candidate_notes=[CandidateNote(candidate_id=cid, note=AI_NOTE) for cid in targets],
        )


@pytest.fixture
def tool(client: TestClient) -> FakeSearchTool:
    tool = FakeSearchTool()
    registry = ToolRegistry()
    registry.register(tool)
    client.app.state.tool_registry = registry  # type: ignore[attr-defined]
    return tool


@pytest.fixture
def collector(client: TestClient, api: Api, tool: FakeSearchTool) -> dict[str, Any]:
    client.app.state.agent_registry.register(_Collector())  # type: ignore[attr-defined]
    return api.post(
        "/ai-employees",
        {
            "key": "test_collector",
            "name": "Collector",
            "role": "収集",
            "stage_key": "idea_generation",
            "implementation_key": "test_collector",
            "prompt_key": "idea_generator",
            "prompt_version": "v1",
            "allowed_tools": ["fake_search"],
            "status": "active",
        },
    )


def _collect(api: Api, exp_id: str, employee: dict[str, Any], **extra: Any) -> Any:
    return api.post(
        f"/explorations/{exp_id}/stage-runs",
        {"mode": "collect_only", "ai_employee_id": employee["id"], **extra},
    )


def _accept(api: Api, candidate_id: str, expect: int = 201, **body: Any) -> Any:
    return api.post(f"/evidence-candidates/{candidate_id}/accept", body, expect=expect)


# ---------------------------------------------------------------------- 収集のみ（E-02）


def test_collect_only_stores_candidates_and_notes_but_no_analysis(
    api: Api, collector: dict[str, Any], session: Session
) -> None:
    exp = api.exploration()
    run = _collect(api, exp["id"], collector)
    assert (run["mode"], run["status"], run["trigger"]) == ("collect_only", "succeeded", "initial")
    [ex] = run["executions"]
    assert ex["output"]["analysis_id"] is None
    assert session.scalars(select(Analysis)).all() == []
    assert api.items(f"/explorations/{exp['id']}/ideas") == []

    [candidate] = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    assert ex["output"]["candidate_ids"] == [candidate["id"]]
    assert candidate["status"] == "pending"
    assert candidate["execution_id"] == ex["id"]
    assert candidate["source_type"] == "web"
    assert candidate["source_key"] == "https://example.com/report"
    assert candidate["snapshot"] == PAGE_TEXT
    assert len(candidate["quote"]) <= 2000
    assert candidate["retrieved_at"] == "2026-09-30T00:00:00Z"
    # 候補は原情報だけで、AI生成の文章を含まない（B-21）
    assert AI_NOTE not in str(candidate)
    [note] = api.items(f"/evidence-candidates/{candidate['id']}/ai-notes")
    assert (note["note"], note["execution_id"]) == (AI_NOTE, ex["id"])
    assert note["prompt_key"] == "idea_generator"

    [call] = api.items(f"/executions/{ex['id']}/tool-calls")
    assert call["tool_name"] == "fake_search"
    assert call["input"]["query"].endswith("市場規模")
    assert call["urls"] == ["https://Example.com/report?utm_source=x"]
    assert session.get(ToolCallOutput, UUID(call["id"])) is not None


def test_collect_only_is_not_counted_as_the_latest_run(api: Api, collector: dict[str, Any]) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    _collect(api, exp["id"], collector)  # 何度でも集められる
    res = api.client.post(
        f"/api/v1/explorations/{exp['id']}/stage-runs",
        json={"mode": "collect_only", "rerun_of_id": str(UUID(int=1))},
        headers=api.h,
    )
    assert res.status_code == 422
    # 分析は rerun_of_id なしで始められる（collect_only は最新の試行に数えない）
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    assert (run["mode"], run["trigger"]) == ("analyze", "initial")
    runs = api.items(f"/explorations/{exp['id']}/stage-runs")
    assert sorted(r["mode"] for r in runs) == ["analyze", "collect_only", "collect_only"]
    assert all(r["superseded_at"] is None for r in runs)


# ---------------------------------------------------------------------- 承認・却下（人間）


def test_accepting_a_candidate_creates_tool_evidence_with_provenance(
    api: Api, collector: dict[str, Any], as_role: Callable[[str], Api]
) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    [candidate] = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    member = as_role("member")
    accepted = _accept(member, candidate["id"], summary="人間の要約", classification="public")
    evidence = accepted["evidence"]
    assert accepted["candidate"]["status"] == "accepted"
    assert accepted["candidate"]["decided_by_actor_id"] == evidence["created_by_actor_id"]
    assert evidence["acquisition_method"] == "tool"
    assert evidence["summary"] == "人間の要約"
    assert evidence["quote"] == candidate["quote"]
    assert evidence["classification"] == "public"
    assert evidence["source_key"] == candidate["source_key"]
    assert evidence["retrieved_at"] == candidate["retrieved_at"]
    provenance = evidence["provenance"]
    assert provenance["acquisition_method"] == "tool"
    assert provenance["tool_name"] == "fake_search"
    assert provenance["tool_input"]["query"].endswith("市場規模")
    assert provenance["candidate_id"] == candidate["id"]
    assert provenance["execution_id"] == candidate["execution_id"]
    assert provenance["registered_by_actor_id"] == evidence["created_by_actor_id"]
    # AI生成の補助情報は Evidence に移らない（B-21）
    assert AI_NOTE not in str(evidence)
    # 承認済みの候補は2回承認・却下できない
    _accept(api, candidate["id"], expect=409)
    api.post(f"/evidence-candidates/{candidate['id']}/reject", {"reason": "x"}, expect=409)
    # 人間の入力した Evidence の来歴
    manual = api.evidence(exp["id"])
    assert api.get(f"/evidence/{manual['id']}")["provenance"]["acquisition_method"] == (
        "human_input"
    )


def test_only_humans_with_member_role_can_decide(
    api: Api, collector: dict[str, Any], as_role: Callable[[str], Api], system_api: Api
) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    [candidate] = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    viewer = as_role("viewer")
    _accept(viewer, candidate["id"], expect=403)
    _accept(system_api, candidate["id"], expect=403)
    viewer.post(f"/evidence-candidates/{candidate['id']}/reject", {"reason": "x"}, expect=403)
    viewer.post(
        "/evidence-candidates/bulk-accept", {"candidate_ids": [candidate["id"]]}, expect=403
    )
    assert viewer.get(f"/evidence-candidates/{candidate['id']}")["status"] == "pending"
    api.post(f"/evidence-candidates/{candidate['id']}/reject", {}, expect=422)
    rejected = as_role("member").post(
        f"/evidence-candidates/{candidate['id']}/reject", {"reason": "無関係"}, expect=200
    )
    assert (rejected["status"], rejected["decision_reason"]) == ("rejected", "無関係")
    _accept(api, candidate["id"], expect=409)
    assert api.items(f"/explorations/{exp['id']}/evidence") == []


def test_bulk_accept_is_all_or_nothing(
    api: Api, collector: dict[str, Any], tool: FakeSearchTool
) -> None:
    exp = api.exploration()
    tool.pages = [
        {"url": "https://example.com/a", "title": "A"},
        {"url": "https://example.com/b", "title": "B"},
        {"url": "https://example.com/c", "title": "C"},
    ]
    _collect(api, exp["id"], collector)
    a, b, c = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    api.post(f"/evidence-candidates/{c['id']}/reject", {"reason": "不要"}, expect=200)
    api.post(
        "/evidence-candidates/bulk-accept",
        {"candidate_ids": [a["id"], b["id"], c["id"]]},
        expect=409,
    )
    assert api.items(f"/explorations/{exp['id']}/evidence") == []
    result = api.post("/evidence-candidates/bulk-accept", {"candidate_ids": [a["id"], b["id"]]})
    assert sorted(e["title"] for e in result["accepted"]) == ["A", "B"]
    assert result["duplicates"] == []
    pending = api.items(f"/explorations/{exp['id']}/evidence-candidates?status=pending")
    assert pending == []


# ---------------------------------------------------------------------- 重複・更新版（4章・E-01）


def test_duplicate_and_updated_versions(
    api: Api, collector: dict[str, Any], tool: FakeSearchTool
) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    [first] = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    v1 = _accept(api, first["id"])["evidence"]

    # 同じ出典・同じ内容 → duplicate（承認不要）
    _collect(api, exp["id"], collector)
    dup = api.items(f"/explorations/{exp['id']}/evidence-candidates?status=duplicate")
    assert [d["duplicate_of_evidence_id"] for d in dup] == [v1["id"]]
    _accept(api, dup[0]["id"], expect=409)

    # 同じ出典で内容が変わった → 更新版の候補。承認すると前の版は superseded（撤回ではない）
    tool.body = PAGE_TEXT + "（改訂）"
    _collect(api, exp["id"], collector)
    [update] = api.items(f"/explorations/{exp['id']}/evidence-candidates?status=pending")
    assert update["updates_evidence_id"] == v1["id"]
    v2 = _accept(api, update["id"])["evidence"]
    assert v2["supersedes_evidence_id"] == v1["id"]
    old = api.get(f"/evidence/{v1['id']}")
    assert (old["evidence_status"], old["retracted_at"]) == ("superseded", None)
    # 新しい分析の入力は最新版だけ
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    assert run["input_snapshot"]["evidence_ids"] == [v2["id"]]


def test_duplicate_detected_at_acceptance(api: Api, collector: dict[str, Any]) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    _collect(api, exp["id"], collector)
    first, second = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    assert {first["status"], second["status"]} == {"pending"}
    evidence = _accept(api, first["id"])["evidence"]
    res = api.client.post(
        f"/api/v1/evidence-candidates/{second['id']}/accept", json={}, headers=api.h
    )
    assert res.status_code == 409
    after = api.get(f"/evidence-candidates/{second['id']}")
    assert (after["status"], after["duplicate_of_evidence_id"]) == ("duplicate", evidence["id"])


# ---------------------------------------------------------------------- AI の入力・根拠にしない


def test_unapproved_candidates_are_not_inputs(api: Api, collector: dict[str, Any]) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    assert run["input_snapshot"]["evidence_ids"] == []


def test_candidates_cannot_be_cited_as_evidence(api: Api, collector: dict[str, Any]) -> None:
    _Collector.cite_candidate = True
    try:
        exp = api.exploration()
        run = api.post(f"/explorations/{exp['id']}/stage-runs", {"ai_employee_id": collector["id"]})
    finally:
        _Collector.cite_candidate = False
    [ex] = run["executions"]
    assert (run["status"], ex["error_type"]) == ("failed", "validation_error")
    # 集めた候補（原情報）は残る。分析は作らない
    assert len(api.items(f"/explorations/{exp['id']}/evidence-candidates")) == 1
    assert api.items(f"/explorations/{exp['id']}/analyses") == []


def test_ai_notes_can_only_refer_to_candidates_of_the_same_execution(
    api: Api, collector: dict[str, Any]
) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    [other] = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    _Collector.note_for = UUID(other["id"])
    try:
        run = _collect(api, exp["id"], collector)
    finally:
        _Collector.note_for = None
    assert run["executions"][0]["error_type"] == "validation_error"
    assert len(api.items(f"/evidence-candidates/{other['id']}/ai-notes")) == 1


# ---------------------------------------------------------------------- DB の安全装置・保存期間


def test_tool_evidence_requires_provenance_in_db(api: Api, session: Session, human: Any) -> None:
    exp = api.exploration()
    session.add(
        Evidence(
            organization_id=DEFAULT_ORGANIZATION_ID,
            exploration_id=UUID(exp["id"]),
            source_type="web",
            title="t",
            content_hash="0" * 64,
            created_by_actor_id=human.id,
            acquisition_method="tool",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_decisions_are_recorded_by_humans_in_db(
    api: Api, collector: dict[str, Any], session: Session, system_actor: Any
) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    [candidate] = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    row = session.get(EvidenceCandidate, UUID(candidate["id"]))
    assert row is not None
    row.status = "rejected"
    row.decision_reason = "x"
    row.decided_by_actor_id = system_actor.id
    row.decided_by_actor_type = "system"
    row.decided_at = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_tool_outputs_and_snapshots_expire(
    api: Api, collector: dict[str, Any], session: Session
) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    session.execute(text("UPDATE tool_call_outputs SET created_at = now() - interval '91 days'"))
    session.execute(text("UPDATE evidence_candidates SET created_at = now() - interval '181 days'"))
    session.commit()
    assert delete_expired_tool_data(session, 90, 180) == (1, 1)
    [candidate] = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    assert candidate["snapshot"] is None
    assert candidate["snapshot_deleted_at"] is not None
    # 抜粋・ハッシュ・来歴は残る
    assert candidate["quote"]
    assert candidate["snapshot_hash"]
    assert delete_expired_tool_data(session, 90, 180) == (0, 0)


def test_candidates_are_isolated_by_organization(
    client: TestClient, api: Api, collector: dict[str, Any], session: Session
) -> None:
    exp = api.exploration()
    _collect(api, exp["id"], collector)
    [candidate] = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    session.execute(
        text("INSERT INTO organizations (id, name) VALUES (:id, 'Other')"),
        {"id": "00000000-0000-7000-8000-000000000999"},
    )
    session.commit()
    other = Api(client, make_human(session, "admin", UUID("00000000-0000-7000-8000-000000000999")))
    other.get(f"/evidence-candidates/{candidate['id']}", expect=404)
    other.get(f"/explorations/{exp['id']}/evidence-candidates", expect=404)
    _accept(other, candidate["id"], expect=404)
