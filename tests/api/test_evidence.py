"""Evidence（根拠・出典）の仕様テスト。

基準:
- 仕様書 11章（Evidence の項目）/ 12章（Evidence・AI Analysis・Human Review の分離）
  / 19章（ユーザー入力の扱い）/ 20章（Evidence を紐付けられる）
- 第1回最終実装仕様（人間のみ登録・不変・撤回で訂正・ai_generated 不可・
  第1回の出典タイプは human_input / document のみ・実行入力の記録）
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.application.commands import IdeaStageRunCommand
from ai_business_explorer.application.stage_runs import StageRunService
from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    Analysis,
    AuditEvent,
    Claim,
    ClaimEvidenceLink,
    Evidence,
)
from ai_business_explorer.llm.fake import FakeLLMClient
from ai_business_explorer.tools.base import ToolRegistry
from tests.conftest import Api

UNKNOWN_ID = "00000000-0000-7000-8000-00000000ffff"


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _body(exploration_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "exploration_id": exploration_id,
        "source_type": "human_input",
        "title": "中小企業白書2025",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------- 登録と項目（11章）


def test_create_and_get_round_trip_with_all_spec_fields(api: Api) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    created = api.post(
        "/evidence",
        _body(
            exp["id"],
            idea_id=idea["id"],
            source_type="document",
            title="業界レポート（日本語タイトル）",
            url="https://example.com/report?page=1",
            quote="中小企業のDX投資意欲は前年比で増加している。",
            summary="人間が書いた要約：競合は大企業向けが中心",
            published_at="2025-04-01T09:00:00+09:00",
            retrieved_at="2026-09-30T12:34:56Z",
            metadata={"publisher": "某省", "page": 12, "tags": ["DX", "中小企業"]},
        ),
    )
    fetched = api.get(f"/evidence/{created['id']}")
    times = ("published_at", "retrieved_at")
    assert {k: v for k, v in fetched.items() if k not in times} == {
        k: v for k, v in created.items() if k not in times
    }
    for key in times:  # 登録時と取得時で同じ時刻を表す
        assert _instant(fetched[key]) == _instant(created[key])
    assert UUID(fetched["id"]).version == 7
    assert fetched["exploration_id"] == exp["id"]
    assert fetched["idea_id"] == idea["id"]
    assert fetched["source_type"] == "document"
    assert fetched["title"] == "業界レポート（日本語タイトル）"
    assert fetched["url"] == "https://example.com/report?page=1"
    assert fetched["quote"] == "中小企業のDX投資意欲は前年比で増加している。"
    assert fetched["summary"] == "人間が書いた要約：競合は大企業向けが中心"
    # 情報の発生日 / 取得日時は UTC で保存される（同一時刻を表す）
    assert fetched["published_at"].startswith("2025-04-01T00:00:00")
    assert fetched["retrieved_at"].startswith("2026-09-30T12:34:56")
    assert fetched["metadata"] == {"publisher": "某省", "page": 12, "tags": ["DX", "中小企業"]}
    assert fetched["created_by_actor_id"] == "00000000-0000-7000-8000-000000000001"
    assert fetched["retracted_at"] is None
    assert fetched["retraction_reason"] is None


def test_minimal_evidence_has_defaults(api: Api) -> None:
    exp = api.exploration()
    ev = api.post("/evidence", _body(exp["id"]))
    assert ev["idea_id"] is None  # 探索案件単位の Evidence
    assert ev["url"] is None
    assert ev["quote"] is None
    assert ev["summary"] is None
    assert ev["published_at"] is None
    assert ev["retrieved_at"] is None
    assert ev["metadata"] == {}


@pytest.mark.parametrize("source_type", ["human_input", "document"])
def test_enabled_source_types_can_be_registered(api: Api, source_type: str) -> None:
    exp = api.exploration()
    assert api.post("/evidence", _body(exp["id"], source_type=source_type))["source_type"] == (
        source_type
    )


@pytest.mark.parametrize("source_type", ["web", "api", "patent_db", "other"])
def test_future_source_types_are_not_enabled_in_phase_1(api: Api, source_type: str) -> None:
    exp = api.exploration()
    api.post("/evidence", _body(exp["id"], source_type=source_type), expect=422)


@pytest.mark.parametrize("source_type", ["ai_generated", "ai", "llm", "AI_GENERATED", ""])
def test_ai_generated_and_unknown_source_types_are_rejected(api: Api, source_type: str) -> None:
    exp = api.exploration()
    api.post("/evidence", _body(exp["id"], source_type=source_type), expect=422)


def test_content_hash_is_recorded_and_content_sensitive(api: Api) -> None:
    # 同じ内容の重複登録を許可するかは未確定（Open Question）のため、ここでは扱わない。
    exp = api.exploration()
    a = api.post("/evidence", _body(exp["id"], quote="引用A"))
    b = api.post("/evidence", _body(exp["id"], quote="引用B"))
    assert all(len(e["content_hash"]) == 64 for e in (a, b))
    int(a["content_hash"], 16)  # 16進数
    assert a["content_hash"] != b["content_hash"]


# ---------------------------------------------------------------- 所属の整合性


def test_unknown_exploration_or_idea_is_404(api: Api) -> None:
    exp = api.exploration()
    api.post("/evidence", _body(UNKNOWN_ID), expect=404)
    api.post("/evidence", _body(exp["id"], idea_id=UNKNOWN_ID), expect=404)


def test_idea_must_belong_to_the_exploration(api: Api) -> None:
    exp_a = api.exploration()
    exp_b = api.exploration()
    idea_b = api.adopted_idea(exp_b["id"])
    api.post("/evidence", _body(exp_a["id"], idea_id=idea_b["id"]), expect=422)


# ---------------------------------------------------------------- 入力検証（19章）


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JAVASCRIPT:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "ftp://example.com/a",
        "example.com/no-scheme",
        "",
    ],
)
def test_non_http_urls_are_rejected(api: Api, url: str) -> None:
    exp = api.exploration()
    api.post("/evidence", _body(exp["id"], url=url), expect=422)


@pytest.mark.parametrize(
    "url", ["http://example.com", "https://example.com", "HTTPS://EXAMPLE.COM"]
)
def test_http_and_https_urls_are_accepted(api: Api, url: str) -> None:
    exp = api.exploration()
    assert api.post("/evidence", _body(exp["id"], url=url))["url"] == url


@pytest.mark.parametrize(
    "override",
    [
        {"title": ""},
        {"title": "あ" * 501},
        {"url": "https://example.com/" + "a" * 2000},
        {"quote": "x" * 20_001},
        {"summary": "x" * 20_001},
        {"published_at": "not-a-date"},
        {"metadata": "not-an-object"},
    ],
)
def test_invalid_field_values_are_rejected(api: Api, override: dict[str, Any]) -> None:
    exp = api.exploration()
    api.post("/evidence", _body(exp["id"], **override), expect=422)


def test_missing_required_fields_are_rejected(api: Api) -> None:
    exp = api.exploration()
    for field in ("exploration_id", "source_type", "title"):
        body = _body(exp["id"])
        del body[field]
        api.post("/evidence", body, expect=422)


@pytest.mark.parametrize(
    "injected",
    [
        {"id": UNKNOWN_ID},
        {"content_hash": "0" * 64},
        {"created_by_actor_id": "00000000-0000-7000-8000-000000000002"},
        {"retracted_at": "2026-01-01T00:00:00Z"},
        {"analysis_id": UNKNOWN_ID},
    ],
)
def test_server_managed_fields_cannot_be_supplied(api: Api, injected: dict[str, Any]) -> None:
    exp = api.exploration()
    api.post("/evidence", _body(exp["id"], **injected), expect=422)


# ---------------------------------------------------------------- 人間のみ（12章・確定仕様）


def test_only_humans_can_register_or_retract(api: Api, system_api: Api) -> None:
    exp = api.exploration()
    system_api.post("/evidence", _body(exp["id"]), expect=403)
    ev = api.post("/evidence", _body(exp["id"]))
    system_api.post(f"/evidence/{ev['id']}/retract", {"reason": "x"}, expect=403)
    assert api.get(f"/evidence/{ev['id']}")["retracted_at"] is None


def test_writes_require_a_known_actor(api: Api) -> None:
    exp = api.exploration()
    res = api.client.post("/api/v1/evidence", json=_body(exp["id"]))
    assert res.status_code == 401
    res = api.client.post(
        "/api/v1/evidence", json=_body(exp["id"]), headers={"X-Actor-Id": UNKNOWN_ID}
    )
    assert res.status_code == 401
    res = api.client.post(
        "/api/v1/evidence", json=_body(exp["id"]), headers={"X-Actor-Id": "not-a-uuid"}
    )
    assert res.status_code == 422


# ---------------------------------------------------------------- 不変性と撤回


def test_evidence_cannot_be_updated_or_deleted_via_api(api: Api) -> None:
    exp = api.exploration()
    ev = api.post("/evidence", _body(exp["id"]))
    for method in ("PATCH", "PUT", "DELETE"):
        res = api.client.request(
            method, f"/api/v1/evidence/{ev['id']}", json={"title": "改ざん"}, headers=api.h
        )
        assert res.status_code == 405, method
    assert api.get(f"/evidence/{ev['id']}")["title"] == "中小企業白書2025"


def test_retraction_keeps_the_record_and_requires_a_reason(api: Api) -> None:
    exp = api.exploration()
    ev = api.post("/evidence", _body(exp["id"]))
    api.post(f"/evidence/{ev['id']}/retract", {}, expect=422)
    api.post(f"/evidence/{ev['id']}/retract", {"reason": ""}, expect=422)
    retracted = api.post(f"/evidence/{ev['id']}/retract", {"reason": "出典の誤り"}, expect=200)
    assert retracted["retracted_at"] is not None
    assert retracted["retraction_reason"] == "出典の誤り"
    # 撤回しても内容は変わらず、履歴として取得できる
    after = api.get(f"/evidence/{ev['id']}")
    assert {k: after[k] for k in ("title", "content_hash", "created_at")} == {
        k: ev[k] for k in ("title", "content_hash", "created_at")
    }
    api.post(f"/evidence/{ev['id']}/retract", {"reason": "再度"}, expect=409)


def test_correction_is_retract_plus_new_registration(api: Api) -> None:
    exp = api.exploration()
    old = api.post("/evidence", _body(exp["id"], quote="誤った数値"))
    api.post(f"/evidence/{old['id']}/retract", {"reason": "数値の誤り"}, expect=200)
    new = api.post("/evidence", _body(exp["id"], quote="正しい数値"))
    assert api.get(f"/evidence/{old['id']}")["retracted_at"] is not None
    assert api.get(f"/evidence/{new['id']}")["retracted_at"] is None
    assert new["id"] != old["id"]


def test_unknown_evidence_is_404(api: Api) -> None:
    api.get(f"/evidence/{UNKNOWN_ID}", expect=404)
    api.post(f"/evidence/{UNKNOWN_ID}/retract", {"reason": "x"}, expect=404)


# ---------------------------------------------------------------- 一覧


def test_lists_are_scoped_to_exploration_and_idea(api: Api) -> None:
    exp = api.exploration()
    other_exp = api.exploration()
    idea_a = api.adopted_idea(exp["id"], title="A")
    idea_b = api.adopted_idea(exp["id"], title="B")
    exp_level = api.evidence(exp["id"], None, title="探索案件全体")
    ev_a = api.evidence(exp["id"], idea_a["id"], title="Aの根拠")
    ev_b = api.evidence(exp["id"], idea_b["id"], title="Bの根拠")
    api.evidence(other_exp["id"], None, title="別案件")

    exp_ids = {e["id"] for e in api.get(f"/explorations/{exp['id']}/evidence")}
    assert exp_ids == {exp_level["id"], ev_a["id"], ev_b["id"]}
    # Idea ごとの一覧: 自分の Evidence は含み、他の Idea・他の案件の Evidence は含まない。
    # 案件全体の Evidence を含めるかは未確定（Open Question）のため確認しない。
    for idea, own, others in ((idea_a, ev_a, ev_b), (idea_b, ev_b, ev_a)):
        ids = {e["id"] for e in api.get(f"/ideas/{idea['id']}/evidence")}
        assert own["id"] in ids
        assert others["id"] not in ids
        assert ids <= exp_ids
    api.get(f"/explorations/{UNKNOWN_ID}/evidence", expect=404)
    api.get(f"/ideas/{UNKNOWN_ID}/evidence", expect=404)


# ---------------------------------------------------------------- 監査ログ


def test_registration_and_retraction_are_audited(api: Api, session: Session) -> None:
    exp = api.exploration()
    ev = api.post("/evidence", _body(exp["id"]))
    api.post(f"/evidence/{ev['id']}/retract", {"reason": "誤り"}, expect=200)
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.entity_type == "evidence", AuditEvent.entity_id == UUID(ev["id"]))
        .order_by(AuditEvent.created_at)
    ).all()
    assert [e.action for e in events] == ["created", "retracted"]
    assert all(str(e.actor_id) == ev["created_by_actor_id"] for e in events)
    assert events[1].after == {"reason": "誤り"}


# ------------------------------------------------ 実行での Evidence の扱い（10・12・20章）


def test_idea_generation_uses_only_exploration_level_evidence(api: Api) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    exp_level = api.evidence(exp["id"], None, title="全体")
    api.evidence(exp["id"], idea["id"], title="個別")
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    assert run["input_snapshot"]["evidence_ids"] == [exp_level["id"]]
    assert run["executions"][0]["input"]["evidence_ids"] == [exp_level["id"]]


def test_idea_stage_uses_exploration_and_own_idea_evidence_only(api: Api) -> None:
    exp = api.exploration()
    idea_a = api.adopted_idea(exp["id"], title="A")
    idea_b = api.adopted_idea(exp["id"], title="B")
    exp_level = api.evidence(exp["id"], None, title="全体")
    ev_a = api.evidence(exp["id"], idea_a["id"], title="Aの根拠")
    api.evidence(exp["id"], idea_b["id"], title="Bの根拠")
    other_exp = api.exploration()
    api.evidence(other_exp["id"], None, title="別案件")

    run = api.post(f"/ideas/{idea_a['id']}/stage-runs", {"stage_key": "market_research"})
    assert set(run["input_snapshot"]["evidence_ids"]) == {exp_level["id"], ev_a["id"]}
    analysis = api.get(f"/analyses/{run['executions'][0]['output']['analysis_id']}")
    assert {link["evidence_id"] for link in analysis["evidence_links"]} == {
        exp_level["id"],
        ev_a["id"],
    }


def test_links_reference_claims_and_survive_later_retraction(api: Api) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    ev = api.evidence(exp["id"], idea["id"])
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    analysis_id = run["executions"][0]["output"]["analysis_id"]
    before = api.get(f"/analyses/{analysis_id}")
    claim_ids = {c["id"] for c in before["body"]["claims"]}
    for link in before["evidence_links"]:
        assert link["claim_ref"] in claim_ids
        assert link["relation"] in {"supports", "contradicts", "context"}

    api.post(f"/evidence/{ev['id']}/retract", {"reason": "誤り"}, expect=200)
    after = api.get(f"/analyses/{analysis_id}")
    assert after["evidence_links"] == before["evidence_links"]  # 過去の分析の根拠は追跡可能
    assert after["body"] == before["body"]


def test_ai_execution_never_creates_evidence(api: Api, session: Session) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    api.evidence(exp["id"], idea["id"])
    before = session.scalar(select(func.count()).select_from(Evidence))
    api.post(f"/explorations/{exp['id']}/stage-runs", {})
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    assert session.scalar(select(func.count()).select_from(Evidence)) == before


# ---------------------------------------------------------------- AI 出力の根拠参照の検証


Responder = Callable[[dict[str, Any]], dict[str, Any]]


def _run_market_research(
    session: Session, settings: Settings, human: Actor, idea_id: str, responder: Responder
) -> tuple[str, Any]:
    llm = FakeLLMClient(responders={"market_researcher": responder})
    service = StageRunService(
        session,
        settings,
        build_default_registry(),
        ToolRegistry(),
        llm_client_factory=lambda _: llm,
    )
    run = service.run_idea_stage(
        human, UUID(idea_id), IdeaStageRunCommand(stage_key="market_research")
    )
    [execution] = service.executions_for(run.id)
    return run.status, execution


def _claims(*claims: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "test", "market_overview": "test", "claims": list(claims)}


def _count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_each_relation_type_is_stored_with_its_claim(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    ev = api.evidence(exp["id"], idea["id"])
    relations = {"c1": "supports", "c2": "contradicts", "c3": "context"}
    # context だけでは evidence_based の根拠にならない（C-09）ので、c3 は inference にする。
    kinds = {"c1": "evidence_based", "c2": "evidence_based", "c3": "inference"}

    def responder(payload: dict[str, Any]) -> dict[str, Any]:
        return _claims(
            *(
                {
                    "id": claim_id,
                    "text": f"{relation} の主張",
                    "kind": kinds[claim_id],
                    "evidence_refs": [{"evidence_id": ev["id"], "relation": relation}],
                }
                for claim_id, relation in relations.items()
            )
        )

    status, execution = _run_market_research(session, settings, human, idea["id"], responder)
    assert status == "succeeded"
    rows = session.execute(
        select(Claim.claim_key, ClaimEvidenceLink.relation, ClaimEvidenceLink.evidence_id)
        .join(ClaimEvidenceLink, ClaimEvidenceLink.claim_id == Claim.id)
        .where(Claim.analysis_id == UUID(execution.output["analysis_id"]))
    ).all()
    assert {(key, relation) for key, relation, _ in rows} == set(relations.items())
    assert {str(evidence_id) for _, _, evidence_id in rows} == {ev["id"]}


@pytest.mark.parametrize("target", ["other_idea", "retracted", "other_exploration", "unknown"])
def test_ai_cannot_cite_evidence_outside_its_input(
    api: Api, session: Session, settings: Settings, human: Actor, target: str
) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"], title="A")
    other = api.adopted_idea(exp["id"], title="B")
    api.evidence(exp["id"], idea["id"])
    outsiders = {
        "other_idea": lambda: api.evidence(exp["id"], other["id"])["id"],
        "retracted": lambda: _retracted(api, exp["id"], idea["id"]),
        "other_exploration": lambda: api.evidence(api.exploration()["id"], None)["id"],
        "unknown": lambda: UNKNOWN_ID,
    }
    cited = outsiders[target]()
    analyses_before = _count(session, Analysis)
    links_before = _count(session, ClaimEvidenceLink)

    def responder(payload: dict[str, Any]) -> dict[str, Any]:
        return _claims(
            {
                "id": "c1",
                "text": "入力外の根拠を引用",
                "kind": "evidence_based",
                "evidence_refs": [{"evidence_id": cited, "relation": "supports"}],
            }
        )

    status, execution = _run_market_research(session, settings, human, idea["id"], responder)
    assert status == "failed"
    assert execution.error_type == "validation_error"
    assert _count(session, Analysis) == analyses_before
    assert _count(session, ClaimEvidenceLink) == links_before


def test_evidence_based_claim_without_refs_fails_the_execution(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])

    def responder(payload: dict[str, Any]) -> dict[str, Any]:
        return _claims({"id": "c1", "text": "根拠なしの事実主張", "kind": "evidence_based"})

    status, execution = _run_market_research(session, settings, human, idea["id"], responder)
    assert status == "failed"
    assert execution.error_type == "validation_error"


def test_inference_and_speculation_claims_need_no_evidence(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])

    def responder(payload: dict[str, Any]) -> dict[str, Any]:
        return _claims(
            {"id": "i1", "text": "推論", "kind": "inference"},
            {"id": "s1", "text": "推測", "kind": "speculation"},
        )

    status, _ = _run_market_research(session, settings, human, idea["id"], responder)
    assert status == "succeeded"


def _retracted(api: Api, exploration_id: str, idea_id: str) -> str:
    ev = api.evidence(exploration_id, idea_id, title="撤回済み")
    api.post(f"/evidence/{ev['id']}/retract", {"reason": "誤り"}, expect=200)
    return str(ev["id"])
