"""主張と根拠の正本（D-15）、relation の検証（C-09）、主張単位のレビュー（B-15）。"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    Analysis,
    AnalysisEvidenceLink,
    Claim,
    ClaimEvidenceLink,
)
from tests.api.test_evidence import _claims, _run_market_research
from tests.conftest import Api


def _count(session: Session, model: type[Any]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
def ctx(api: Api) -> dict[str, Any]:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    ev1 = api.evidence(exp["id"], idea["id"], title="統計1")
    ev2 = api.evidence(exp["id"], idea["id"], title="統計2")
    return {"exp": exp, "idea": idea, "ev1": ev1["id"], "ev2": ev2["id"]}


def _ref(evidence_id: str, relation: str) -> dict[str, str]:
    return {"evidence_id": evidence_id, "relation": relation}


def _claim(claim_id: str, kind: str, *refs: dict[str, str]) -> dict[str, Any]:
    return {"id": claim_id, "text": f"{claim_id} の主張", "kind": kind, "evidence_refs": list(refs)}


def _run(
    session: Session,
    settings: Settings,
    human: Actor,
    ctx: dict[str, Any],
    *claims: dict[str, Any],
) -> tuple[str, Any]:
    return _run_market_research(
        session, settings, human, ctx["idea"]["id"], lambda _: _claims(*claims)
    )


# ---------------------------------------------------------------- 正本（D-15）


def test_analysis_claims_come_from_the_claims_table(
    api: Api, session: Session, settings: Settings, human: Actor, ctx: dict[str, Any]
) -> None:
    status, execution = _run(
        session,
        settings,
        human,
        ctx,
        _claim("e1", "evidence_based", _ref(ctx["ev1"], "supports"), _ref(ctx["ev2"], "context")),
        _claim("i1", "inference"),
    )
    assert status == "succeeded"
    analysis = api.get(f"/analyses/{execution.output['analysis_id']}")

    assert [(c["claim_key"], c["ordinal"], c["kind"]) for c in analysis["claims"]] == [
        ("e1", 0, "evidence_based"),
        ("i1", 1, "inference"),
    ]
    rows = {
        (row.claim_key, str(row.evidence_id), row.relation)
        for row in session.execute(
            select(Claim.claim_key, ClaimEvidenceLink.evidence_id, ClaimEvidenceLink.relation)
            .join(ClaimEvidenceLink, ClaimEvidenceLink.claim_id == Claim.id)
            .where(Claim.analysis_id == UUID(analysis["id"]))
        )
    }
    assert rows == {("e1", ctx["ev1"], "supports"), ("e1", ctx["ev2"], "context")}
    links = analysis["evidence_links"]
    assert {(link["claim_ref"], link["evidence_id"], link["relation"]) for link in links} == rows
    e1 = analysis["claims"][0]
    assert {link["claim_id"] for link in e1["evidence_links"]} == {e1["id"]}

    # body.claims は生成時点のAI出力スナップショットとして残る（書き換えない）。
    assert [c["id"] for c in analysis["body"]["claims"]] == ["e1", "i1"]


def test_new_analyses_never_write_the_frozen_table(
    session: Session, settings: Settings, human: Actor, ctx: dict[str, Any]
) -> None:
    status, _ = _run(
        session, settings, human, ctx, _claim("e1", "evidence_based", _ref(ctx["ev1"], "supports"))
    )
    assert status == "succeeded"
    assert _count(session, AnalysisEvidenceLink) == 0
    assert _count(session, ClaimEvidenceLink) == 1


# ---------------------------------------------------------------- relation の検証（C-09）


@pytest.mark.parametrize(
    ("case", "claim"),
    [
        (
            "duplicate relation",
            lambda ev1, ev2: _claim(
                "e1", "evidence_based", _ref(ev1, "supports"), _ref(ev1, "supports")
            ),
        ),
        (
            "supports and contradicts on the same evidence",
            lambda ev1, ev2: _claim(
                "e1", "evidence_based", _ref(ev1, "supports"), _ref(ev1, "contradicts")
            ),
        ),
        (
            "evidence_based with context only",
            lambda ev1, ev2: _claim("e1", "evidence_based", _ref(ev1, "context")),
        ),
        (
            "duplicate context on a non evidence_based claim",
            lambda ev1, ev2: _claim(
                "s1", "speculation", _ref(ev1, "context"), _ref(ev1, "context")
            ),
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_invalid_relations_fail_with_validation_error_and_save_nothing(
    session: Session,
    settings: Settings,
    human: Actor,
    ctx: dict[str, Any],
    case: str,
    claim: Any,
) -> None:
    before = (_count(session, Analysis), _count(session, Claim), _count(session, ClaimEvidenceLink))
    status, execution = _run(session, settings, human, ctx, claim(ctx["ev1"], ctx["ev2"]))
    assert status == "failed", case
    assert execution.error_type == "validation_error"
    session.expire_all()
    after = (_count(session, Analysis), _count(session, Claim), _count(session, ClaimEvidenceLink))
    assert after == before


@pytest.mark.parametrize(
    "refs",
    [
        lambda ev1, ev2: [_ref(ev1, "contradicts")],
        lambda ev1, ev2: [_ref(ev1, "supports"), _ref(ev1, "context")],
        lambda ev1, ev2: [_ref(ev1, "supports"), _ref(ev2, "contradicts")],
    ],
    ids=[
        "contradicts only",
        "supports with context",
        "supports and contradicts on different evidence",
    ],
)
def test_valid_evidence_based_claims_are_stored(
    session: Session, settings: Settings, human: Actor, ctx: dict[str, Any], refs: Any
) -> None:
    expected = refs(ctx["ev1"], ctx["ev2"])
    status, _ = _run(session, settings, human, ctx, _claim("e1", "evidence_based", *expected))
    assert status == "succeeded"
    assert _count(session, ClaimEvidenceLink) == len(expected)


# ---------------------------------------------------------------- 主張単位のレビュー（B-15）


def _analysis(
    api: Api, session: Session, settings: Settings, human: Actor, ctx: dict[str, Any]
) -> Any:
    _, execution = _run(
        session,
        settings,
        human,
        ctx,
        _claim("e1", "evidence_based", _ref(ctx["ev1"], "supports")),
        _claim("i1", "inference"),
    )
    return api.get(f"/analyses/{execution.output['analysis_id']}")


def test_claim_review_does_not_change_the_analysis_review_status(
    api: Api, session: Session, settings: Settings, human: Actor, ctx: dict[str, Any]
) -> None:
    analysis = _analysis(api, session, settings, human, ctx)
    e1, i1 = analysis["claims"]
    path = f"/analyses/{analysis['id']}/human-reviews"

    review = api.post(path, {"claim_id": e1["id"], "decision": "reject", "comment": "根拠が弱い"})
    assert review["claim_id"] == e1["id"]
    after = api.get(f"/analyses/{analysis['id']}")
    assert after["review_status"] == "pending_review"
    assert after["claims"][0]["latest_review"]["decision"] == "reject"
    assert after["claims"][1]["latest_review"] is None

    api.post(path, {"claim_id": e1["id"], "decision": "approve"})
    api.post(path, {"decision": "approve"})  # claim_id なしは分析全体のレビュー
    after = api.get(f"/analyses/{analysis['id']}")
    assert after["review_status"] == "approved"
    assert after["claims"][0]["latest_review"]["decision"] == "approve"
    assert [r["claim_id"] for r in after["human_reviews"]] == [e1["id"], e1["id"], None]
    assert i1["latest_review"] is None


def test_claim_review_must_target_a_claim_of_the_analysis(
    api: Api, session: Session, settings: Settings, human: Actor, ctx: dict[str, Any]
) -> None:
    first = _analysis(api, session, settings, human, ctx)
    other_claim = first["claims"][0]["id"]
    # 再実行で別の分析を作り、最初の分析の主張を指定する
    run = api.get(f"/ideas/{ctx['idea']['id']}/stage-runs")[-1]
    rerun = api.post(
        f"/ideas/{ctx['idea']['id']}/stage-runs",
        {"stage_key": "market_research", "rerun_of_id": run["id"]},
    )
    second_id = rerun["executions"][0]["output"]["analysis_id"]
    api.post(
        f"/analyses/{second_id}/human-reviews",
        {"claim_id": other_claim, "decision": "approve"},
        expect=422,
    )
    api.post(
        f"/analyses/{second_id}/human-reviews",
        {"claim_id": "00000000-0000-7000-8000-0000000fffff", "decision": "approve"},
        expect=404,
    )
