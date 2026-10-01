"""DB レベルの安全装置。アプリ層をすり抜けても、分離と人間限定が守られることを確認する。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_business_explorer.infrastructure.db.models import (
    Actor,
    AIEmployee,
    Evidence,
    Execution,
    Exploration,
    HumanDecision,
    Idea,
    StageRun,
)
from ai_business_explorer.seed import DEFAULT_ORGANIZATION_ID


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
def idea(session: Session, exploration: Exploration) -> Idea:
    i = Idea(
        organization_id=exploration.organization_id,
        exploration_id=exploration.id,
        title="idea",
        origin_type="human",
    )
    session.add(i)
    session.commit()
    return i


def _assert_rejected(session: Session, make: Callable[[], object]) -> None:
    session.add(make())
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_human_decision_rejects_non_human_actor(
    session: Session, idea: Idea, system_actor: Actor
) -> None:
    _assert_rejected(
        session,
        lambda: HumanDecision(
            organization_id=idea.organization_id,
            idea_id=idea.id,
            decided_by_actor_id=system_actor.id,
            decided_by_actor_type="system",
            decision="go",
            rationale="x",
        ),
    )


def test_human_decision_rejects_spoofed_actor_type(
    session: Session, idea: Idea, system_actor: Actor
) -> None:
    """actor_type を 'human' と偽っても、複合 FK により実在の human actor 以外は拒否される。"""
    _assert_rejected(
        session,
        lambda: HumanDecision(
            organization_id=idea.organization_id,
            idea_id=idea.id,
            decided_by_actor_id=system_actor.id,
            decided_by_actor_type="human",
            decision="go",
            rationale="x",
        ),
    )


def test_stage_run_must_be_triggered_by_human(
    session: Session, exploration: Exploration, system_actor: Actor
) -> None:
    _assert_rejected(
        session,
        lambda: StageRun(
            organization_id=exploration.organization_id,
            exploration_id=exploration.id,
            stage_key="idea_generation",
            attempt_no=1,
            trigger="initial",
            triggered_by_actor_id=system_actor.id,
            triggered_by_actor_type="system",
            status="running",
            started_at=datetime.now(UTC),
        ),
    )


def test_stage_run_scope_must_match_stage(
    session: Session, exploration: Exploration, human: Actor
) -> None:
    _assert_rejected(
        session,
        lambda: StageRun(
            organization_id=exploration.organization_id,
            exploration_id=exploration.id,
            idea_id=None,
            stage_key="market_research",
            attempt_no=1,
            trigger="initial",
            triggered_by_actor_id=human.id,
            triggered_by_actor_type="human",
            status="running",
            started_at=datetime.now(UTC),
        ),
    )


def test_evidence_rejects_ai_generated_source_type(
    session: Session, exploration: Exploration, human: Actor
) -> None:
    _assert_rejected(
        session,
        lambda: Evidence(
            organization_id=exploration.organization_id,
            exploration_id=exploration.id,
            source_type="ai_generated",
            title="AI summary",
            content_hash="0" * 64,
            created_by_actor_id=human.id,
        ),
    )


def test_ai_origin_idea_requires_origin_analysis(
    session: Session, exploration: Exploration
) -> None:
    _assert_rejected(
        session,
        lambda: Idea(
            organization_id=exploration.organization_id,
            exploration_id=exploration.id,
            title="x",
            origin_type="ai",
        ),
    )


def test_invalid_adoption_status_rejected(session: Session, exploration: Exploration) -> None:
    _assert_rejected(
        session,
        lambda: Idea(
            organization_id=exploration.organization_id,
            exploration_id=exploration.id,
            title="x",
            origin_type="human",
            adoption_status="approved_by_ai",
        ),
    )


def test_seeded_actor_ids(human: Actor, system_actor: Actor) -> None:
    assert human.actor_type == "human"
    assert system_actor.actor_type == "system"
    assert isinstance(human.id, UUID)


def test_evidence_retraction_requires_reason(
    session: Session, exploration: Exploration, human: Actor
) -> None:
    _assert_rejected(
        session,
        lambda: Evidence(
            organization_id=exploration.organization_id,
            exploration_id=exploration.id,
            source_type="human_input",
            title="x",
            content_hash="0" * 64,
            created_by_actor_id=human.id,
            retracted_at=datetime.now(UTC),
        ),
    )


def test_claim_evidence_link_rejects_dangling_claim_and_evidence(
    session: Session, exploration: Exploration, human: Actor
) -> None:
    """根拠リンクの正本（claim_evidence_links）は、実在しない主張・Evidence を参照できない。

    第1回の analysis_evidence_links は凍結済み（tests/integration/test_claims.py）。
    """
    from uuid import uuid4

    from ai_business_explorer.infrastructure.db.models import ClaimEvidenceLink

    _assert_rejected(
        session,
        lambda: ClaimEvidenceLink(
            organization_id=exploration.organization_id,
            claim_id=uuid4(),
            evidence_id=uuid4(),
            relation="supports",
        ),
    )


def _execution(stage_run: StageRun, employee: AIEmployee, role: str) -> Execution:
    return Execution(
        organization_id=stage_run.organization_id,
        stage_run_id=stage_run.id,
        ai_employee_id=employee.id,
        assignment_role=role,
        ai_employee_version=1,
        ai_employee_snapshot={},
        implementation_key="idea_generator",
        code_version="test",
    )


def test_stage_run_has_one_primary_and_each_employee_once(
    session: Session, exploration: Exploration, human: Actor
) -> None:
    """primary は1つの実行に1つだけ、同じ AI社員は1回だけ（第2回仕様 7章）。"""
    stage_run = StageRun(
        organization_id=exploration.organization_id,
        exploration_id=exploration.id,
        stage_key="idea_generation",
        attempt_no=1,
        trigger="initial",
        triggered_by_actor_id=human.id,
        triggered_by_actor_type="human",
        started_at=datetime.now(UTC),
    )
    session.add(stage_run)
    session.commit()
    assert stage_run.status == "queued"
    ig = session.query(AIEmployee).filter_by(key="idea_generator").one()
    other = AIEmployee(
        organization_id=exploration.organization_id,
        key="ig_b",
        name="b",
        role="r",
        stage_key="idea_generation",
        llm_config={},
        allowed_tools=[],
        status="active",
        version=1,
    )
    session.add(other)
    session.add(_execution(stage_run, ig, "primary"))
    session.commit()
    _assert_rejected(session, lambda: _execution(stage_run, other, "primary"))
    _assert_rejected(session, lambda: _execution(stage_run, ig, "secondary"))
    _assert_rejected(session, lambda: _execution(stage_run, other, "observer"))
    session.add(_execution(stage_run, other, "secondary"))
    session.commit()
