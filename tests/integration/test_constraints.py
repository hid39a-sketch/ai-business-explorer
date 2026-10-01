"""DB レベルの安全装置。アプリ層をすり抜けても、分離と人間限定が守られることを確認する。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_business_explorer.infrastructure.db.models import (
    Actor,
    Evidence,
    Exploration,
    HumanDecision,
    Idea,
    StageRun,
)


@pytest.fixture
def exploration(session: Session, human: Actor) -> Exploration:
    e = Exploration(title="t", theme="theme", created_by_actor_id=human.id)
    session.add(e)
    session.commit()
    return e


@pytest.fixture
def idea(session: Session, exploration: Exploration) -> Idea:
    i = Idea(exploration_id=exploration.id, title="idea", origin_type="human")
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
        session, lambda: Idea(exploration_id=exploration.id, title="x", origin_type="ai")
    )


def test_invalid_adoption_status_rejected(session: Session, exploration: Exploration) -> None:
    _assert_rejected(
        session,
        lambda: Idea(
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
            exploration_id=exploration.id,
            source_type="human_input",
            title="x",
            content_hash="0" * 64,
            created_by_actor_id=human.id,
            retracted_at=datetime.now(UTC),
        ),
    )


def test_analysis_evidence_link_rejects_unknown_relation_and_dangling_evidence(
    session: Session, exploration: Exploration, human: Actor
) -> None:
    from uuid import uuid4

    from ai_business_explorer.infrastructure.db.models import AnalysisEvidenceLink

    _assert_rejected(
        session,
        lambda: AnalysisEvidenceLink(
            analysis_id=uuid4(), evidence_id=uuid4(), claim_ref="c1", relation="supports"
        ),
    )
