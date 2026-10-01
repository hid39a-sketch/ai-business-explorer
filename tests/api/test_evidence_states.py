"""Evidence の版と状態（E-01）、本文の消去（14章）、一覧（15章、R-15）、重複の通知（Q4）。"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_business_explorer.domain.evidence import normalize_url
from ai_business_explorer.infrastructure.db.models import AuditEvent, Evidence, StageRun
from tests.conftest import Api


def _supersede(session: Session, old_id: str, title: str = "更新版") -> str:
    """更新版の Evidence を作る（第2回では Tool 候補の承認で作られる。PR-7）。"""
    old = session.get(Evidence, UUID(old_id))
    assert old is not None
    new = Evidence(
        organization_id=old.organization_id,
        exploration_id=old.exploration_id,
        idea_id=old.idea_id,
        source_type=old.source_type,
        title=title,
        url=old.url,
        quote="新しい数値",
        content_hash="f" * 64,
        source_key=old.source_key,
        supersedes_evidence_id=old.id,
        created_by_actor_id=old.created_by_actor_id,
    )
    session.add(new)
    session.commit()
    return str(new.id)


@pytest.fixture
def states(api: Api, session: Session) -> dict[str, Any]:
    """active / superseded（とその更新版）/ retracted / purged の Evidence を1つずつ用意する。"""
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    ev = {
        name: api.evidence(exp["id"], idea["id"], title=name)["id"]
        for name in ("active", "old", "retracted", "purged")
    }
    ev["new"] = _supersede(session, ev["old"])
    api.post(f"/evidence/{ev['retracted']}/retract", {"reason": "誤り"}, expect=200)
    api.post(f"/evidence/{ev['purged']}/purge", {"reason": "削除要請"}, expect=200)
    return {"exp": exp, "idea": idea, "ev": ev}


def _ids(items: list[dict[str, Any]]) -> set[str]:
    return {i["id"] for i in items}


# ---------------------------------------------------------------- 状態（E-01）


def test_each_state_is_reported_separately(api: Api, states: dict[str, Any]) -> None:
    ev = states["ev"]
    expected = {
        "active": ("active", False, False, False),
        "new": ("active", False, False, False),
        "old": ("superseded", False, True, False),
        "retracted": ("retracted", True, False, False),
        "purged": ("purged", False, False, True),
    }
    for name, (status, retracted, superseded, purged) in expected.items():
        got = api.get(f"/evidence/{ev[name]}")
        assert (
            got["evidence_status"],
            got["is_retracted"],
            got["is_superseded"],
            got["is_purged"],
        ) == (status, retracted, superseded, purged), name
    assert api.get(f"/evidence/{ev['old']}")["superseded_by_id"] == ev["new"]


def test_superseded_is_not_retracted(api: Api, states: dict[str, Any]) -> None:
    old = api.get(f"/evidence/{states['ev']['old']}")
    assert old["retracted_at"] is None
    assert old["retraction_reason"] is None


def test_status_priority_keeps_the_original_states(api: Api, states: dict[str, Any]) -> None:
    """優先順位は purged > retracted > superseded > active。元の各状態も残る。"""
    ev = states["ev"]
    api.post(f"/evidence/{ev['old']}/retract", {"reason": "誤り"}, expect=200)
    old = api.get(f"/evidence/{ev['old']}")
    assert (old["evidence_status"], old["is_retracted"], old["is_superseded"]) == (
        "retracted",
        True,
        True,
    )
    api.post(f"/evidence/{ev['old']}/purge", {"reason": "削除要請"}, expect=200)
    old = api.get(f"/evidence/{ev['old']}")
    assert (old["evidence_status"], old["is_purged"], old["is_retracted"]) == (
        "purged",
        True,
        True,
    )


# ---------------------------------------------------------------- 一覧（R-15）


def test_evidence_list_defaults_to_active(api: Api, states: dict[str, Any]) -> None:
    ev, exp_id = states["ev"], states["exp"]["id"]
    assert _ids(api.items(f"/explorations/{exp_id}/evidence")) == {ev["active"], ev["new"]}
    idea_list = api.items(f"/ideas/{states['idea']['id']}/evidence")
    assert _ids(idea_list) == {ev["active"], ev["new"]}


@pytest.mark.parametrize(
    ("query", "names"),
    [
        ("status=superseded", {"old"}),
        ("status=retracted", {"retracted"}),
        ("status=purged", {"purged"}),
        ("status=active&status=superseded", {"active", "new", "old"}),
        (
            "status=active&status=superseded&status=retracted&status=purged",
            {"active", "new", "old", "retracted", "purged"},
        ),
    ],
)
def test_evidence_list_status_filter(
    api: Api, states: dict[str, Any], query: str, names: set[str]
) -> None:
    ev, exp_id = states["ev"], states["exp"]["id"]
    assert _ids(api.items(f"/explorations/{exp_id}/evidence?{query}")) == {ev[n] for n in names}


def test_include_retracted_is_not_supported(api: Api, states: dict[str, Any]) -> None:
    """include_retracted は廃止（status に一本化）。指定しても撤回済みは返らない。"""
    exp_id = states["exp"]["id"]
    listed = _ids(api.items(f"/explorations/{exp_id}/evidence?include_retracted=true"))
    assert states["ev"]["retracted"] not in listed


def test_unknown_status_is_rejected(api: Api, states: dict[str, Any]) -> None:
    api.get(f"/explorations/{states['exp']['id']}/evidence?status=deleted", expect=422)


# ---------------------------------------------------------------- 消去（14章）


def test_purge_removes_only_the_content(api: Api, session: Session, states: dict[str, Any]) -> None:
    purged = api.get(f"/evidence/{states['ev']['purged']}")
    assert purged["quote"] is None
    assert purged["summary"] is None
    assert purged["title"] == "purged"  # 出典の情報は残る
    assert purged["url"] == "https://example.com/report"
    assert purged["content_hash"]
    assert purged["purge_reason"] == "削除要請"
    assert purged["content_purged_at"].endswith("Z")
    assert purged["purged_by_actor_id"] == api.h["X-Actor-Id"]
    actions = session.scalars(
        select(AuditEvent.action).where(AuditEvent.entity_id == UUID(purged["id"]))
    ).all()
    assert "purged" in actions


def test_purge_rules(api: Api, states: dict[str, Any]) -> None:
    ev = states["ev"]
    api.post(f"/evidence/{ev['purged']}/purge", {"reason": "再度"}, expect=409)
    api.post(f"/evidence/{ev['active']}/purge", {}, expect=422)
    api.post(f"/evidence/{ev['active']}/purge", {"reason": ""}, expect=422)


# ---------------------------------------------------------------- AI の入力（E-01）


def test_ai_input_is_active_evidence_only(
    api: Api, session: Session, states: dict[str, Any]
) -> None:
    ev, idea = states["ev"], states["idea"]
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    stage_run = session.get(StageRun, UUID(run["id"]))
    assert stage_run is not None
    snapshot = stage_run.input_snapshot
    assert set(snapshot["evidence_ids"]) == {ev["active"], ev["new"]}
    assert snapshot["evidence"] == [{"id": i, "status": "active"} for i in snapshot["evidence_ids"]]


def test_past_links_are_not_rewritten_and_show_the_current_state(
    api: Api, session: Session
) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    ev = api.evidence(exp["id"], idea["id"])
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    analysis_id = run["executions"][0]["output"]["analysis_id"]
    before = api.get(f"/analyses/{analysis_id}")["evidence_links"]
    assert before
    assert {link["evidence_status"] for link in before} == {"active"}

    new_id = _supersede(session, ev["id"])
    links = api.get(f"/analyses/{analysis_id}")["evidence_links"]
    assert [(k["claim_id"], k["evidence_id"]) for k in links] == [
        (k["claim_id"], k["evidence_id"]) for k in before
    ]
    assert {k["evidence_status"] for k in links} == {"superseded"}
    assert {k["evidence_superseded_by_id"] for k in links} == {new_id}

    api.post(f"/evidence/{ev['id']}/purge", {"reason": "削除要請"}, expect=200)
    links = api.get(f"/analyses/{analysis_id}")["evidence_links"]
    assert {k["evidence_status"] for k in links} == {"purged"}
    assert all(k["evidence_is_superseded"] for k in links)
    assert all(k["evidence_is_purged"] for k in links)


# ---------------------------------------------------------------- 重複の通知（Q4）


def test_duplicate_human_input_is_accepted_with_a_warning(api: Api) -> None:
    exp = api.exploration()
    body = {"exploration_id": exp["id"], "source_type": "human_input", "title": "統計"}
    first = api.post("/evidence", {**body, "url": "https://Example.com/report?id=1#top"})
    assert first["warnings"] == []
    assert first["source_key"] == "https://example.com/report?id=1"

    # 追跡用パラメータと # 以降が違うだけの同じ出典
    second = api.post(
        "/evidence",
        {**body, "title": "別名", "url": "https://example.com/report?id=1&utm_source=x"},
    )
    assert second["warnings"] == [{"code": "duplicate", "evidence_id": first["id"]}]

    # 撤回済みの Evidence とは重複扱いしない
    api.post(f"/evidence/{first['id']}/retract", {"reason": "誤り"}, expect=200)
    api.post(f"/evidence/{second['id']}/retract", {"reason": "誤り"}, expect=200)
    third = api.post("/evidence", {**body, "url": "https://example.com/report?id=1"})
    assert third["warnings"] == []

    # 別の探索案件とは重複扱いしない
    other = api.exploration()
    fourth = api.post(
        "/evidence",
        {**body, "exploration_id": other["id"], "url": "https://example.com/report?id=1"},
    )
    assert fourth["warnings"] == []


def test_same_content_is_a_duplicate(api: Api) -> None:
    exp = api.exploration()
    first = api.evidence(exp["id"])
    second = api.post(
        "/evidence",
        {
            "exploration_id": exp["id"],
            "source_type": "human_input",
            "title": "統計",
            "url": "https://example.com/report",
            "quote": "市場は拡大している。",
        },
    )
    assert second["warnings"] == [{"code": "duplicate", "evidence_id": first["id"]}]


def test_normalize_url() -> None:
    assert (
        normalize_url("HTTPS://Example.COM/Path?b=2&utm_medium=x&a=1&fbclid=y#frag")
        == "https://example.com/Path?b=2&a=1"
    )


# ---------------------------------------------------------------- ページング（15章）


def test_cursor_pagination_has_no_duplicates_or_gaps(api: Api) -> None:
    created = [api.post("/explorations", {"title": f"E{i}", "theme": "t"})["id"] for i in range(5)]
    first = api.get("/explorations?limit=2")
    assert len(first["items"]) == 2
    assert first["has_more"]
    assert first["next_cursor"]
    # 取得中にデータが増えても、既に返した行が重複・欠落しない
    created.append(api.post("/explorations", {"title": "E5", "theme": "t"})["id"])
    seen = [e["id"] for e in first["items"]]
    cursor = first["next_cursor"]
    while cursor:
        page = api.get(f"/explorations?limit=2&cursor={cursor}")
        seen += [e["id"] for e in page["items"]]
        cursor = page["next_cursor"]
        assert page["has_more"] == (cursor is not None)
    assert seen == created

    desc = [e["id"] for e in api.get("/explorations?order=desc&limit=200")["items"]]
    assert desc == list(reversed(created))


def test_page_parameters_are_validated(api: Api) -> None:
    api.get("/explorations?limit=0", expect=422)
    api.get("/explorations?limit=201", expect=422)
    api.get("/explorations?cursor=not-a-cursor", expect=422)
    assert api.get("/explorations")["has_more"] is False


def test_created_after_and_before(api: Api) -> None:
    a = api.post("/explorations", {"title": "A", "theme": "t"})
    b = api.post("/explorations", {"title": "B", "theme": "t"})
    after_a = api.items(f"/explorations?created_after={a['created_at']}")
    assert _ids(after_a) == {b["id"]}
    before_b = api.items(f"/explorations?created_before={b['created_at']}")
    assert _ids(before_b) == {a["id"]}


def test_datetimes_are_utc_with_z(api: Api) -> None:
    exp = api.exploration()
    assert exp["created_at"].endswith("Z")
    assert exp["updated_at"].endswith("Z")


def test_analysis_list_filters(api: Api) -> None:
    exp = api.exploration()
    api.post(f"/explorations/{exp['id']}/stage-runs", {})
    idea = api.items(f"/explorations/{exp['id']}/ideas")[0]
    api.post(f"/ideas/{idea['id']}/adopt", {}, expect=200)
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    analysis_id = run["executions"][0]["output"]["analysis_id"]
    employee_id = run["executions"][0]["ai_employee_id"]
    base = f"/explorations/{exp['id']}/analyses"
    assert len(api.items(base)) == 2
    assert _ids(api.items(f"{base}?stage_key=market_research")) == {analysis_id}
    assert _ids(api.items(f"{base}?ai_employee_id={employee_id}")) == {analysis_id}
    api.post(f"/analyses/{analysis_id}/human-reviews", {"decision": "approve"})
    assert _ids(api.items(f"{base}?review_status=approved")) == {analysis_id}
    assert api.items(f"/ideas/{idea['id']}/analyses?review_status=rejected") == []
