"""主張と根拠リンクの DB レベルの安全装置（第2回仕様 5章・6章、R-14）。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from ai_business_explorer.domain.ids import uuid7
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    AIEmployee,
    Analysis,
    AnalysisEvidenceLink,
    Claim,
    ClaimEvidenceLink,
    Evidence,
    Execution,
    Exploration,
    HumanReview,
    Organization,
    StageRun,
)
from ai_business_explorer.seed import DEFAULT_ORGANIZATION_ID


def _assert_rejected(session: Session, make: Callable[[], object]) -> None:
    session.add(make())
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def _analysis(session: Session, human: Actor, exploration: Exploration) -> Analysis:
    employee = session.query(AIEmployee).filter_by(key="idea_generator").one()
    now = datetime.now(UTC)
    run = StageRun(
        organization_id=exploration.organization_id,
        exploration_id=exploration.id,
        stage_key="idea_generation",
        attempt_no=1,
        trigger="initial",
        triggered_by_actor_id=human.id,
        triggered_by_actor_type="human",
        status="succeeded",
        started_at=now,
    )
    session.add(run)
    session.flush()
    execution = Execution(
        organization_id=run.organization_id,
        stage_run_id=run.id,
        ai_employee_id=employee.id,
        ai_employee_version=1,
        ai_employee_snapshot={},
        implementation_key="idea_generator",
        code_version="test",
        status="succeeded",
        started_at=now,
        cost_currency="USD",
    )
    session.add(execution)
    session.flush()
    analysis = Analysis(
        organization_id=run.organization_id,
        exploration_id=exploration.id,
        stage_run_id=run.id,
        execution_id=execution.id,
        ai_employee_id=employee.id,
        stage_key="idea_generation",
        schema_version="v1",
        version_no=1,
        summary="s",
        body={"claims": [], "data": {}},
        classification="internal",
    )
    session.add(analysis)
    session.commit()
    return analysis


@pytest.fixture
def exploration(session: Session, human: Actor) -> Exploration:
    e = Exploration(
        organization_id=DEFAULT_ORGANIZATION_ID,
        title="t",
        theme="theme",
        created_by_actor_id=human.id,
    )
    session.add(e)
    session.commit()
    return e


@pytest.fixture
def analysis(session: Session, human: Actor, exploration: Exploration) -> Analysis:
    return _analysis(session, human, exploration)


@pytest.fixture
def evidence(session: Session, human: Actor, exploration: Exploration) -> Evidence:
    ev = Evidence(
        organization_id=exploration.organization_id,
        exploration_id=exploration.id,
        source_type="human_input",
        title="統計",
        content_hash="0" * 64,
        created_by_actor_id=human.id,
    )
    session.add(ev)
    session.commit()
    return ev


def _claim(analysis: Analysis, key: str = "c1", **kw: Any) -> Claim:
    return Claim(
        organization_id=kw.pop("organization_id", analysis.organization_id),
        analysis_id=analysis.id,
        claim_key=key,
        ordinal=kw.pop("ordinal", 0),
        kind=kw.pop("kind", "inference"),
        text="主張",
        **kw,
    )


@pytest.fixture
def claim(session: Session, analysis: Analysis) -> Claim:
    c = _claim(analysis)
    session.add(c)
    session.commit()
    return c


# ---------------------------------------------------------------- claims


def test_claim_kind_is_restricted(session: Session, analysis: Analysis) -> None:
    _assert_rejected(session, lambda: _claim(analysis, kind="fact"))


def test_claim_key_is_unique_within_an_analysis(session: Session, claim: Claim) -> None:
    analysis = session.get(Analysis, claim.analysis_id)
    assert analysis is not None
    _assert_rejected(session, lambda: _claim(analysis, key=claim.claim_key, ordinal=1))


def test_claim_must_be_in_the_analysis_organization(session: Session, analysis: Analysis) -> None:
    other = Organization(id=uuid7(), name="Other")
    session.add(other)
    session.commit()
    _assert_rejected(session, lambda: _claim(analysis, organization_id=other.id))


# ---------------------------------------------------------------- claim_evidence_links


def test_same_relation_cannot_be_linked_twice(
    session: Session, claim: Claim, evidence: Evidence
) -> None:
    def link() -> ClaimEvidenceLink:
        return ClaimEvidenceLink(
            organization_id=claim.organization_id,
            claim_id=claim.id,
            evidence_id=evidence.id,
            relation="supports",
        )

    session.add(link())
    session.commit()
    _assert_rejected(session, link)


def test_link_relation_is_restricted(session: Session, claim: Claim, evidence: Evidence) -> None:
    _assert_rejected(
        session,
        lambda: ClaimEvidenceLink(
            organization_id=claim.organization_id,
            claim_id=claim.id,
            evidence_id=evidence.id,
            relation="refutes",
        ),
    )


def test_link_cannot_cross_organizations(session: Session, claim: Claim, human: Actor) -> None:
    other = Organization(id=uuid7(), name="Other")
    session.add(other)
    session.commit()
    other_exploration = Exploration(
        organization_id=other.id, title="t", theme="t", created_by_actor_id=human.id
    )
    session.add(other_exploration)
    session.commit()
    other_evidence = Evidence(
        organization_id=other.id,
        exploration_id=other_exploration.id,
        source_type="human_input",
        title="他組織",
        content_hash="1" * 64,
        created_by_actor_id=human.id,
    )
    session.add(other_evidence)
    session.commit()
    for org_id in (claim.organization_id, other.id):
        _assert_rejected(
            session,
            lambda org_id=org_id: ClaimEvidenceLink(
                organization_id=org_id,
                claim_id=claim.id,
                evidence_id=other_evidence.id,
                relation="supports",
            ),
        )


# ---------------------------------------------------------------- human_reviews.claim_id


def test_claim_review_must_target_a_claim_of_the_same_analysis(
    session: Session, human: Actor, exploration: Exploration, claim: Claim
) -> None:
    other_exploration = Exploration(
        organization_id=exploration.organization_id,
        title="t2",
        theme="theme",
        created_by_actor_id=human.id,
    )
    session.add(other_exploration)
    session.commit()
    other_analysis = _analysis(session, human, other_exploration)
    _assert_rejected(
        session,
        lambda: HumanReview(
            organization_id=other_analysis.organization_id,
            analysis_id=other_analysis.id,
            claim_id=claim.id,
            exploration_id=other_exploration.id,
            reviewer_actor_id=human.id,
            reviewer_actor_type="human",
            decision="approve",
        ),
    )


# ---------------------------------------------------------------- 旧テーブルの凍結（R-14）


def test_old_link_table_is_frozen(session: Session, analysis: Analysis, evidence: Evidence) -> None:
    def params() -> dict[str, UUID]:
        return {"a": analysis.id, "e": evidence.id}

    session.add(
        AnalysisEvidenceLink(
            analysis_id=analysis.id, evidence_id=evidence.id, claim_ref="c1", relation="supports"
        )
    )
    with pytest.raises(DBAPIError, match="frozen"):
        session.commit()
    session.rollback()

    # 移行時に入った行を想定して、トリガーを無効にした状態で1行入れておく
    session.execute(text("SET LOCAL session_replication_role = replica"))
    session.execute(
        text(
            "INSERT INTO analysis_evidence_links (analysis_id, evidence_id, claim_ref, relation) "
            "VALUES (:a, :e, 'c1', 'supports')"
        ),
        params(),
    )
    session.commit()

    for sql in (
        "UPDATE analysis_evidence_links SET relation = 'context'",
        "DELETE FROM analysis_evidence_links",
        "TRUNCATE analysis_evidence_links CASCADE",
    ):
        with pytest.raises(DBAPIError, match="frozen"):
            session.execute(text(sql))
        session.rollback()
    count = session.execute(text("SELECT count(*) FROM analysis_evidence_links")).scalar_one()
    assert count == 1
