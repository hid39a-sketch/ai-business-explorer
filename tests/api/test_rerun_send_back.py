"""再実行・差し戻し（人間のみ、履歴は保持）。"""

from typing import Any

from fastapi.testclient import TestClient
from pydantic import BaseModel

from ai_business_explorer.agents.base import Agent, AgentContext, AnalysisDraft, Claim
from ai_business_explorer.domain.enums import ClaimKind
from tests.conftest import Api


def _two_stages(api: Api) -> tuple[dict[str, Any], dict[str, Any]]:
    """採用済み Idea で market_research を成功させる。"""
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    api.evidence(exp["id"], idea["id"])
    mr = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    return idea, mr


def test_rerun_supersedes_previous_attempt_and_keeps_history(api: Api) -> None:
    idea, first = _two_stages(api)
    second = api.post(
        f"/ideas/{idea['id']}/stage-runs",
        {"stage_key": "market_research", "rerun_of_id": first["id"], "research_question": "再調査"},
    )
    assert second["trigger"] == "rerun"
    assert second["attempt_no"] == 2
    assert second["rerun_of_id"] == first["id"]
    runs = api.items(f"/ideas/{idea['id']}/stage-runs")
    assert len(runs) == 2
    old = next(r for r in runs if r["id"] == first["id"])
    assert old["superseded_at"] is not None
    analyses = api.items(f"/ideas/{idea['id']}/analyses")
    assert [a["version_no"] for a in analyses] == [1, 2]
    assert analyses[1]["supersedes_id"] == analyses[0]["id"]
    # 古い試行を指定した再実行は拒否
    api.post(
        f"/ideas/{idea['id']}/stage-runs",
        {"stage_key": "market_research", "rerun_of_id": first["id"]},
        expect=409,
    )
    assert api.get(f"/ideas/{idea['id']}")["research_status"]["current_stage_key"] == (
        "market_research"
    )


def test_send_back_requires_a_later_current_stage(api: Api) -> None:
    idea, _ = _two_stages(api)
    # 現在 market_research が最終ステージなので、それより前への差し戻し先はない
    api.post(
        f"/ideas/{idea['id']}/send-back",
        {"to_stage_key": "market_research", "reason": "根拠不足"},
        expect=409,
    )


def test_send_back_target_validation(api: Api) -> None:
    idea, _ = _two_stages(api)
    for target in ("human_review", "idea_generation"):
        api.post(
            f"/ideas/{idea['id']}/send-back", {"to_stage_key": target, "reason": "x"}, expect=422
        )
    api.post(f"/ideas/{idea['id']}/send-back", {"to_stage_key": "market_research"}, expect=422)


class _Empty(BaseModel):
    pass


class _TestCompetitorResearcher(Agent):
    """テスト専用の AI社員実装（本番レジストリには登録しない）。"""

    implementation_key = "test_competitor_researcher"
    stage_key = "competitor_research"
    output_schema_version = "test.v1"
    input_model = _Empty
    output_model = _Empty

    def run(self, ctx: AgentContext) -> AnalysisDraft:
        return AnalysisDraft(
            summary="test competitor analysis",
            claims=[Claim(id="s1", text="推測", kind=ClaimKind.SPECULATION)],
        )


def test_send_back_supersedes_target_and_later_stages(client: TestClient, api: Api) -> None:
    client.app.state.agent_registry.register(_TestCompetitorResearcher())  # type: ignore[attr-defined]
    api.post(
        "/ai-employees",
        {
            "key": "test_competitor",
            "name": "TestCompetitor",
            "role": "test",
            "stage_key": "competitor_research",
            "implementation_key": "test_competitor_researcher",
            "prompt_key": "market_researcher",
            "prompt_version": "v1",
            "status": "active",
        },
    )
    idea, mr = _two_stages(api)
    cr = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "competitor_research"})
    assert cr["status"] == "succeeded"
    assert api.get(f"/ideas/{idea['id']}")["current_stage_key"] == "competitor_research"

    sent = api.post(
        f"/ideas/{idea['id']}/send-back",
        {"to_stage_key": "market_research", "reason": "根拠不足のため市場調査をやり直す"},
    )
    assert sent["trigger"] == "send_back"
    assert sent["stage_key"] == "market_research"
    assert sent["sent_back_from_id"] == cr["id"]
    assert sent["reason"] == "根拠不足のため市場調査をやり直す"
    assert sent["attempt_no"] == 2

    runs = {r["id"]: r for r in api.items(f"/ideas/{idea['id']}/stage-runs")}
    assert runs[mr["id"]]["superseded_at"] is not None
    assert runs[cr["id"]]["superseded_at"] is not None  # 後続ステージも無効化
    assert runs[sent["id"]]["superseded_at"] is None
    assert len(runs) == 3  # 履歴は消えない
    assert api.get(f"/ideas/{idea['id']}")["current_stage_key"] == "market_research"
    # 後続ステージは再び initial で実行できる
    again = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "competitor_research"})
    assert again["attempt_no"] == 2


def test_only_humans_can_rerun_or_send_back(api: Api, system_api: Api) -> None:
    idea, mr = _two_stages(api)
    system_api.post(
        f"/ideas/{idea['id']}/stage-runs",
        {"stage_key": "market_research", "rerun_of_id": mr["id"]},
        expect=403,
    )
    system_api.post(
        f"/ideas/{idea['id']}/send-back",
        {"to_stage_key": "market_research", "reason": "x"},
        expect=403,
    )
