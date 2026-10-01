"""データ分類（第2回仕様 11章。E-04・R-03・R-08）。

- 探索案件と Evidence に分類を付ける（既定 internal）。Idea は探索案件の分類に従う。
- 分析の分類は入力（探索案件・Evidence・前段の分析）の最も高い分類で、後続の確認にも使う。
- 入力の最も高い分類が LLM プロバイダーの送信上限（既定 internal）を超える起動は 409 で、
  実行記録も作らない。restricted はどの LLM にも送らない。
- 分類を下げる変更は admin のみ。
"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.config import Settings
from ai_business_explorer.domain.enums import DataClassification, highest_classification
from ai_business_explorer.infrastructure.db.models import Analysis, AuditEvent, Exploration
from ai_business_explorer.tools.base import default_tool_registry
from ai_business_explorer.worker import run_once
from tests.api.test_rerun_send_back import _TestCompetitorResearcher
from tests.conftest import Api


def _allow(client: TestClient, settings: Settings, limit: str) -> Settings:
    """Fake プロバイダーの送信上限を変える（このテストのアプリだけ）。"""
    changed = settings.model_copy(update={"llm_max_classification": {"fake": limit}})
    client.app.state.settings = changed  # type: ignore[attr-defined]
    return changed


def _evidence(api: Api, exp_id: str, idea_id: str | None, classification: str) -> Any:
    return api.post(
        "/evidence",
        {
            "exploration_id": exp_id,
            "idea_id": idea_id,
            "source_type": "human_input",
            "title": f"{classification} の資料",
            "classification": classification,
        },
    )


def _competitor_employee(client: TestClient, api: Api) -> None:
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


def test_classification_order_and_highest() -> None:
    order = ["public", "internal", "confidential", "restricted"]
    assert [DataClassification(v).rank for v in order] == [0, 1, 2, 3]
    assert highest_classification(["internal", "public", "confidential"]) == "confidential"
    assert highest_classification([]) == "public"
    assert DataClassification.RESTRICTED.exceeds(DataClassification.CONFIDENTIAL)
    assert not DataClassification.INTERNAL.exceeds(DataClassification.INTERNAL)


def test_settings_never_allow_restricted(settings: Settings) -> None:
    assert settings.llm_send_limit("fake") == "internal"
    with pytest.raises(ValidationError):
        Settings(llm_max_classification={"fake": "restricted"})
    # 検証を経ずに作った設定でも restricted は送れない
    unchecked = settings.model_copy(update={"llm_max_classification": {"fake": "restricted"}})
    assert unchecked.llm_send_limit("fake") == "confidential"


def test_default_is_internal_and_analysis_inherits_it(api: Api) -> None:
    exp = api.exploration()
    assert exp["classification"] == "internal"
    idea = api.adopted_idea(exp["id"])
    ev = api.evidence(exp["id"], idea["id"])
    assert ev["classification"] == "internal"
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    assert run["input_snapshot"]["classification"] == "internal"
    analysis = api.get(f"/analyses/{run['executions'][0]['output']['analysis_id']}")
    assert analysis["classification"] == "internal"


def test_public_inputs_make_a_public_analysis(api: Api) -> None:
    exp = api.post(
        "/explorations", {"title": "公開情報の調査", "theme": "t", "classification": "public"}
    )
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    analysis = api.get(f"/analyses/{run['executions'][0]['output']['analysis_id']}")
    assert analysis["classification"] == "public"
    # internal の Evidence を足すと、次の分析は internal になる（自動で下がることはない）
    _evidence(api, exp["id"], None, "internal")
    rerun = api.post(f"/explorations/{exp['id']}/stage-runs", {"rerun_of_id": run["id"]})
    analysis = api.get(f"/analyses/{rerun['executions'][0]['output']['analysis_id']}")
    assert analysis["classification"] == "internal"


@pytest.mark.parametrize("source", ["exploration", "evidence"])
@pytest.mark.parametrize("classification", ["confidential", "restricted"])
def test_launch_over_the_send_limit_is_rejected_without_records(
    api: Api, session: Session, source: str, classification: str
) -> None:
    exp = api.post(
        "/explorations",
        {
            "title": "案件",
            "theme": "t",
            "classification": classification if source == "exploration" else "internal",
        },
    )
    if source == "evidence":
        _evidence(api, exp["id"], None, classification)
    res = api.client.post(f"/api/v1/explorations/{exp['id']}/stage-runs", json={}, headers=api.h)
    assert res.status_code == 409, res.text
    assert "exceeds the send limit" in res.json()["detail"]
    assert api.items(f"/explorations/{exp['id']}/stage-runs") == []
    assert session.scalars(select(Analysis)).all() == []


def test_restricted_is_rejected_even_when_confidential_is_allowed(
    client: TestClient, api: Api, settings: Settings
) -> None:
    _allow(client, settings, "confidential")
    exp = api.exploration()
    _evidence(api, exp["id"], None, "confidential")
    api.post(f"/explorations/{exp['id']}/stage-runs", {})
    _evidence(api, exp["id"], None, "restricted")
    exp2 = api.exploration()
    _evidence(api, exp2["id"], None, "restricted")
    api.post(f"/explorations/{exp2['id']}/stage-runs", {}, expect=409)


def test_inputs_that_are_not_sent_do_not_count(api: Api) -> None:
    """撤回した Evidence や他の Idea の Evidence は入力にならないので、分類の確認にも数えない。"""
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    other = api.adopted_idea(exp["id"], title="別のアイデア")
    _evidence(api, exp["id"], other["id"], "confidential")
    retracted = _evidence(api, exp["id"], idea["id"], "confidential")
    api.post(f"/evidence/{retracted['id']}/retract", {"reason": "誤登録"}, expect=200)
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    assert run["status"] == "succeeded"


def test_analysis_classification_carries_to_later_stages(
    client: TestClient, api: Api, settings: Settings
) -> None:
    _competitor_employee(client, api)
    _allow(client, settings, "confidential")
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    secret = _evidence(api, exp["id"], idea["id"], "confidential")
    mr = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    mr_analysis = api.get(f"/analyses/{mr['executions'][0]['output']['analysis_id']}")
    assert mr_analysis["classification"] == "confidential"

    # confidential の Evidence を撤回しても、前段の分析を通じて後続の分析は confidential
    api.post(f"/evidence/{secret['id']}/retract", {"reason": "差し替え"}, expect=200)
    cr = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "competitor_research"})
    assert mr_analysis["id"] in cr["input_snapshot"]["analysis_ids"]
    cr_analysis = api.get(f"/analyses/{cr['executions'][0]['output']['analysis_id']}")
    assert cr_analysis["classification"] == "confidential"

    # 送信上限が internal に戻ると、confidential の分析を入力にする起動は拒否される
    _allow(client, settings, "internal")
    res = api.client.post(
        f"/api/v1/ideas/{idea['id']}/stage-runs",
        json={"stage_key": "competitor_research", "rerun_of_id": cr["id"]},
        headers=api.h,
    )
    assert res.status_code == 409
    assert "'confidential'" in res.json()["detail"]


def test_raised_classification_is_rechecked_when_the_worker_starts(
    client: TestClient,
    api: Api,
    settings: Settings,
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """受付の後に分類が上がった場合、ワーカーは LLM に送らずに失敗にする。"""
    async_settings = settings.model_copy(update={"execution_mode": "async"})
    client.app.state.settings = async_settings  # type: ignore[attr-defined]
    exp = api.exploration()
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    assert run["status"] == "queued"
    api.patch(f"/explorations/{exp['id']}", {"classification": "confidential"})

    processed = run_once(
        session_factory, async_settings, build_default_registry(), default_tool_registry, "w"
    )
    assert processed is True
    done = api.get(f"/stage-runs/{run['id']}")
    assert done["status"] == "failed"
    assert done["input_snapshot"]["classification"] == "confidential"
    [ex] = done["executions"]
    assert ex["error_type"] == "validation_error"
    assert "exceeds the send limit" in ex["error_message"]
    assert ex["usage"] is None
    assert session.scalars(select(Analysis)).all() == []


def test_only_admin_can_lower_the_classification(
    api: Api, as_role: Callable[[str], Api], session: Session
) -> None:
    member = as_role("member")
    exp = member.post("/explorations", {"title": "案件", "theme": "t"})
    path = f"/explorations/{exp['id']}"
    # 上げるのは member でよい
    assert member.patch(path, {"classification": "confidential"})["classification"] == (
        "confidential"
    )
    for role in ("member", "reviewer"):
        res = as_role(role).client.patch(
            f"/api/v1{path}", json={"classification": "internal"}, headers=as_role(role).h
        )
        assert res.status_code == 403, res.text
    as_role("viewer").patch(path, {"classification": "restricted"}, expect=403)
    assert member.get(path)["classification"] == "confidential"
    # 同じ値・他の項目の更新は下げる操作ではない
    member.patch(path, {"classification": "confidential", "description": "d"})
    assert as_role("admin").patch(path, {"classification": "public"})["classification"] == (
        "public"
    )
    member.patch(path, {"classification": None}, expect=422)
    member.patch(path, {"classification": "secret"}, expect=422)

    events = session.scalars(
        select(AuditEvent).where(
            AuditEvent.entity_id == UUID(exp["id"]), AuditEvent.action == "updated"
        )
    ).all()
    assert ("confidential", "public") in {
        ((e.before or {}).get("classification"), (e.after or {}).get("classification"))
        for e in events
    }


def test_invalid_classification_is_rejected(api: Api, session: Session) -> None:
    api.post("/explorations", {"title": "t", "theme": "t", "classification": "secret"}, expect=422)
    exp = api.exploration()
    res = api.client.post(
        "/api/v1/evidence",
        json={
            "exploration_id": exp["id"],
            "source_type": "human_input",
            "title": "t",
            "classification": "top_secret",
        },
        headers=api.h,
    )
    assert res.status_code == 422
    # DB でも4段階以外は拒否する
    row = session.get(Exploration, UUID(exp["id"]))
    assert row is not None
    row.classification = "secret"
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
