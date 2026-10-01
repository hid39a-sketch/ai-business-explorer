"""Evidence / AI Analysis / Human Review / Human Decision の分離。"""

from typing import Any

from tests.conftest import Api


def _market_research(api: Api) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    ev1 = api.evidence(exp["id"], idea["id"], title="市場統計2025")
    ev2 = api.evidence(exp["id"], None, title="業界レポート")
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    analysis = api.get(f"/analyses/{run['executions'][0]['output']['analysis_id']}")
    return idea, analysis, [ev1, ev2]


def test_evidence_fields_and_retraction(api: Api) -> None:
    exp = api.exploration()
    ev = api.post(
        "/evidence",
        {
            "exploration_id": exp["id"],
            "source_type": "document",
            "title": "白書",
            "url": "https://example.com/whitepaper",
            "quote": "引用文",
            "summary": "人間が書いた要約",
            "published_at": "2025-04-01T00:00:00Z",
            "retrieved_at": "2026-09-30T00:00:00Z",
            "metadata": {"publisher": "某省"},
        },
    )
    assert ev["metadata"] == {"publisher": "某省"}
    assert len(ev["content_hash"]) == 64
    retracted = api.post(f"/evidence/{ev['id']}/retract", {"reason": "誤り"}, expect=200)
    assert retracted["retracted_at"] is not None
    api.post(f"/evidence/{ev['id']}/retract", {"reason": "again"}, expect=409)


def test_ai_generated_or_auto_sources_cannot_be_registered_as_evidence(api: Api) -> None:
    exp = api.exploration()
    base = {"exploration_id": exp["id"], "title": "x"}
    api.post("/evidence", {**base, "source_type": "ai_generated"}, expect=422)
    api.post("/evidence", {**base, "source_type": "web"}, expect=422)  # 第1回は無効


def test_evidence_url_must_be_http(api: Api) -> None:
    exp = api.exploration()
    body = {"exploration_id": exp["id"], "source_type": "human_input", "title": "x"}
    api.post("/evidence", {**body, "url": "javascript:alert(1)"}, expect=422)
    api.post("/evidence", {**body, "url": "file:///etc/passwd"}, expect=422)
    api.post("/evidence", {**body, "url": "https://example.com"})


def test_analysis_links_evidence_and_separates_claim_kinds(api: Api) -> None:
    _, analysis, evidence = _market_research(api)
    linked = {link["evidence_id"] for link in analysis["evidence_links"]}
    assert linked == {e["id"] for e in evidence}
    kinds = {c["kind"] for c in analysis["body"]["claims"]}
    assert kinds == {"evidence_based", "inference"}
    assert analysis["human_reviews"] == []
    assert analysis["review_status"] == "pending_review"


def test_retracted_evidence_is_not_used(api: Api) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    ev = api.evidence(exp["id"], idea["id"])
    api.post(f"/evidence/{ev['id']}/retract", {"reason": "誤り"}, expect=200)
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    assert run["input_snapshot"]["evidence_ids"] == []


def test_human_review_is_separate_and_updates_review_status_only(api: Api) -> None:
    _, analysis, _ = _market_research(api)
    review = api.post(
        f"/analyses/{analysis['id']}/human-reviews",
        {
            "decision": "request_changes",
            "comment": "根拠が不足",
            "corrections": {"i1": "市場規模は別資料で確認する"},
        },
    )
    after = api.get(f"/analyses/{analysis['id']}")
    assert after["review_status"] == "changes_requested"
    assert after["body"] == analysis["body"]  # AI Analysis 本体は不変
    assert after["human_reviews"][0]["id"] == review["id"]
    assert after["human_reviews"][0]["corrections"] == {"i1": "市場規模は別資料で確認する"}
    api.post(f"/analyses/{analysis['id']}/human-reviews", {"decision": "approve"})
    assert api.get(f"/analyses/{analysis['id']}")["review_status"] == "approved"
    assert len(api.get(f"/analyses/{analysis['id']}/human-reviews")) == 2


def test_idea_generation_analysis_can_be_reviewed(api: Api) -> None:
    exp = api.exploration()
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    analysis_id = run["executions"][0]["output"]["analysis_id"]
    review = api.post(f"/analyses/{analysis_id}/human-reviews", {"decision": "needs_more_evidence"})
    assert review["idea_id"] is None
    assert api.get(f"/analyses/{analysis_id}")["review_status"] == "needs_more_evidence"


def test_review_decisions_are_restricted(api: Api) -> None:
    _, analysis, _ = _market_research(api)
    api.post(f"/analyses/{analysis['id']}/human-reviews", {"decision": "auto_approve"}, expect=422)


def test_only_humans_can_review_and_decide(api: Api, system_api: Api) -> None:
    idea, analysis, _ = _market_research(api)
    system_api.post(
        f"/analyses/{analysis['id']}/human-reviews", {"decision": "approve"}, expect=403
    )
    system_api.post(
        f"/ideas/{idea['id']}/human-decisions", {"decision": "go", "rationale": "x"}, expect=403
    )


def test_human_decision(api: Api) -> None:
    idea, analysis, _ = _market_research(api)
    review = api.post(f"/analyses/{analysis['id']}/human-reviews", {"decision": "approve"})
    decision = api.post(
        f"/ideas/{idea['id']}/human-decisions",
        {
            "decision": "hold",
            "rationale": "競合調査の結果を待つ",
            "based_on_review_ids": [review["id"]],
        },
    )
    assert decision["decision"] == "hold"
    assert decision["based_on_review_ids"] == [review["id"]]
    assert len(api.get(f"/ideas/{idea['id']}/human-decisions")) == 1
    api.post(
        f"/ideas/{idea['id']}/human-decisions",
        {"decision": "approve", "rationale": "x"},
        expect=422,
    )
    api.post(f"/ideas/{idea['id']}/human-decisions", {"decision": "go"}, expect=422)


def test_decision_rejects_unrelated_reviews(api: Api) -> None:
    _, analysis, _ = _market_research(api)
    review = api.post(f"/analyses/{analysis['id']}/human-reviews", {"decision": "approve"})
    exp = api.exploration()
    other = api.adopted_idea(exp["id"], title="別案")
    api.post(
        f"/ideas/{other['id']}/human-decisions",
        {"decision": "go", "rationale": "x", "based_on_review_ids": [review["id"]]},
        expect=422,
    )
