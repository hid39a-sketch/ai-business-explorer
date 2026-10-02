"""前段の分析を、レビューの状態と分析全体の最新のレビュー1件を付けて後続の AI社員に渡す（V-07）。

- 渡すのは review_status と、claim_id のない最新のレビュー（decision・comment・corrections）だけ。
- 渡した内容は実行開始時に input_snapshot に固定し、後でレビューが変わっても書き換えない。
- reject された分析も自動では除外しない（後続の入力に入る）。実際の API には接続しない。
"""

import json
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from ai_business_explorer.application.commands import IdeaStageRunCommand
from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import Actor, StageRun
from ai_business_explorer.prompts.loader import load_prompt
from tests.api.test_claude_provider import _market_reply, _service
from tests.conftest import Api
from tests.fake_claude import FakeClaudeSDK


@pytest.fixture
def setup(api: Api) -> dict[str, Any]:
    """アイデア生成（Fake LLM）の分析と Idea を作り、採用する。市場調査は Claude（Fake SDK）。"""
    mr = next(e for e in api.items("/ai-employees") if e["key"] == "market_researcher")
    api.patch(f"/ai-employees/{mr['id']}", {"llm_config": {"provider": "anthropic"}})
    exp = api.exploration()
    api.post(f"/explorations/{exp['id']}/stage-runs", {})
    [analysis] = api.items(f"/explorations/{exp['id']}/analyses")
    idea = api.items(f"/explorations/{exp['id']}/ideas")[0]
    assert idea["origin_analysis_id"] == analysis["id"]
    api.post(f"/ideas/{idea['id']}/adopt", {"reason": "検証する"}, expect=200)
    return {"idea": idea["id"], "analysis": analysis["id"]}


def _review(api: Api, analysis_id: str, decision: str, **body: Any) -> Any:
    return api.post(f"/analyses/{analysis_id}/human-reviews", {"decision": decision, **body})


def _run_market(
    session: Session, settings: Settings, human: Actor, idea_id: str
) -> tuple[Any, Any, FakeClaudeSDK]:
    sdk = FakeClaudeSDK(replies=[_market_reply({"id": "i1", "text": "推論", "kind": "inference"})])
    service = _service(session, settings, sdk)
    run = service.run_idea_stage(
        human, UUID(idea_id), IdeaStageRunCommand(stage_key="market_research")
    )
    [execution] = service.executions_for(run.id)
    return run, execution, sdk


def _sent_prior(sdk: FakeClaudeSDK) -> list[dict[str, Any]]:
    payload = json.loads(sdk.requests[0]["messages"][0]["content"])
    prior: list[dict[str, Any]] = payload["prior_analyses"]
    return prior


def test_approved_analysis_is_passed_with_its_review(
    api: Api, session: Session, settings: Settings, human: Actor, setup: dict[str, Any]
) -> None:
    _review(api, setup["analysis"], "approve", comment="妥当")
    run, _, sdk = _run_market(session, settings, human, setup["idea"])
    assert run.status == "succeeded"
    [prior] = _sent_prior(sdk)
    assert prior["id"] == setup["analysis"]
    assert prior["review_status"] == "approved"
    assert prior["latest_review"] == {"decision": "approve", "comment": "妥当", "corrections": None}


def test_rejected_analysis_is_still_passed_as_rejected(
    api: Api, session: Session, settings: Settings, human: Actor, setup: dict[str, Any]
) -> None:
    """reject された分析を自動で除外はしない。rejected として、理由と一緒に渡す。"""
    _review(api, setup["analysis"], "reject", comment="前提が誤り：すでに普及している")
    run, _, sdk = _run_market(session, settings, human, setup["idea"])
    assert run.status == "succeeded"
    [prior] = _sent_prior(sdk)
    assert prior["review_status"] == "rejected"
    assert prior["latest_review"]["decision"] == "reject"
    assert prior["latest_review"]["comment"] == "前提が誤り：すでに普及している"


def test_only_the_latest_analysis_level_review_is_passed(
    api: Api, session: Session, settings: Settings, human: Actor, setup: dict[str, Any]
) -> None:
    """分析全体の最新のレビュー1件だけ（古いレビューと主張単位のレビューは渡さない）。"""
    analysis_id = setup["analysis"]
    _review(api, analysis_id, "reject", comment="古い判断")
    corrections = {"summary": "対象は地方の中小企業に限る"}
    _review(api, analysis_id, "request_changes", comment="対象を絞る", corrections=corrections)
    claim_id = api.get(f"/analyses/{analysis_id}")["claims"][0]["id"]
    _review(api, analysis_id, "approve", claim_id=claim_id, comment="この主張は妥当")
    _, _, sdk = _run_market(session, settings, human, setup["idea"])
    [prior] = _sent_prior(sdk)
    assert prior["review_status"] == "changes_requested"
    assert prior["latest_review"] == {
        "decision": "request_changes",
        "comment": "対象を絞る",
        "corrections": corrections,
    }


def test_analysis_without_reviews_is_pending(
    api: Api, session: Session, settings: Settings, human: Actor, setup: dict[str, Any]
) -> None:
    run, execution, sdk = _run_market(session, settings, human, setup["idea"])
    assert (run.status, execution.error_type) == ("succeeded", None)
    [prior] = _sent_prior(sdk)
    assert prior["review_status"] == "pending_review"
    assert prior["latest_review"] is None
    assert run.input_snapshot["analyses"] == [
        {
            "id": setup["analysis"],
            "review_status": "pending_review",
            "review_id": None,
            "review_decision": None,
        }
    ]


def test_input_snapshot_records_the_review_state_at_execution(
    api: Api, session: Session, settings: Settings, human: Actor, setup: dict[str, Any]
) -> None:
    review = _review(api, setup["analysis"], "reject", comment="誤り")
    run, execution, _ = _run_market(session, settings, human, setup["idea"])
    expected = [
        {
            "id": setup["analysis"],
            "review_status": "rejected",
            "review_id": review["id"],
            "review_decision": "reject",
        }
    ]
    assert run.input_snapshot["analyses"] == expected
    # 実行の入力にも同じ内容が残る。既存の analysis_ids も残す
    assert execution.input["analyses"] == expected
    assert run.input_snapshot["analysis_ids"] == [setup["analysis"]]


def test_later_reviews_do_not_change_the_recorded_input(
    api: Api, session: Session, settings: Settings, human: Actor, setup: dict[str, Any]
) -> None:
    _review(api, setup["analysis"], "reject", comment="誤り")
    run, _, _ = _run_market(session, settings, human, setup["idea"])
    before = json.loads(json.dumps(run.input_snapshot))
    # 実行の後に承認しても、過去の実行の入力の記録は変わらない
    _review(api, setup["analysis"], "approve", comment="再確認して妥当")
    session.expire_all()
    stored = session.get(StageRun, run.id)
    assert stored is not None
    assert stored.input_snapshot == before
    assert stored.input_snapshot["analyses"][0]["review_status"] == "rejected"
    via_api = api.get(f"/stage-runs/{run.id}")
    assert via_api["input_snapshot"]["analyses"][0]["review_decision"] == "reject"


def test_market_researcher_uses_prompt_v3(
    api: Api, session: Session, settings: Settings, human: Actor, setup: dict[str, Any]
) -> None:
    _, execution, sdk = _run_market(session, settings, human, setup["idea"])
    v3 = load_prompt("market_researcher", "v3")
    assert (execution.prompt_version, execution.prompt_hash) == ("v3", v3.sha256)
    assert sdk.requests[0]["system"] == v3.text
