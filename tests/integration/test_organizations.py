"""組織・ロール・担当の DB レベルの安全装置（第2回仕様 1章・7章）。

アプリ層をすり抜けても、AI・system actor にロールが付かないこと、組織をまたいだ参照ができないこと、
human_review にAI社員を割り当てられないことを確認する。
"""

from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_business_explorer.domain.ids import uuid7
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    AIEmployee,
    Exploration,
    Idea,
    Organization,
    OrganizationMembership,
    StageAssignment,
)
from ai_business_explorer.seed import DEFAULT_HUMAN_ACTOR_ID, DEFAULT_ORGANIZATION_ID


def _assert_rejected(session: Session, make: Callable[[], object]) -> None:
    session.add(make())
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


@pytest.fixture
def other_org(session: Session) -> Organization:
    org = Organization(id=uuid7(), name="Other")
    session.add(org)
    session.commit()
    return org


@pytest.fixture
def employee(session: Session) -> AIEmployee:
    found = session.scalars(
        select(AIEmployee).where(
            AIEmployee.organization_id == DEFAULT_ORGANIZATION_ID,
            AIEmployee.key == "market_researcher",
        )
    ).one()
    return found


def _membership(actor_id: object, actor_type: str, role: str = "admin") -> OrganizationMembership:
    return OrganizationMembership(
        organization_id=DEFAULT_ORGANIZATION_ID,
        actor_id=actor_id,
        actor_type=actor_type,
        role=role,
    )


# ---------------------------------------------------------------- ロールは人間だけ


def test_system_actor_cannot_have_a_role(session: Session, system_actor: Actor) -> None:
    _assert_rejected(session, lambda: _membership(system_actor.id, "system"))


def test_system_actor_cannot_spoof_human_to_get_a_role(
    session: Session, system_actor: Actor
) -> None:
    """actor_type を 'human' と偽っても、複合 FK により実在の human actor 以外は拒否される。"""
    _assert_rejected(session, lambda: _membership(system_actor.id, "human"))


def test_unknown_role_is_rejected(session: Session) -> None:
    actor = Actor(id=uuid7(), actor_type="human", display_name="x")
    session.add(actor)
    session.commit()
    _assert_rejected(session, lambda: _membership(actor.id, "human", role="owner"))


def test_actor_belongs_to_one_organization(session: Session, other_org: Organization) -> None:
    _assert_rejected(
        session,
        lambda: OrganizationMembership(
            organization_id=other_org.id,
            actor_id=DEFAULT_HUMAN_ACTOR_ID,
            actor_type="human",
            role="viewer",
        ),
    )


def test_seed_roles(session: Session, human: Actor, system_actor: Actor) -> None:
    memberships = session.scalars(select(OrganizationMembership)).all()
    assert [(m.actor_id, m.role) for m in memberships] == [(human.id, "admin")]


# ---------------------------------------------------------------- 担当（B-05）


def test_seed_primary_assignments(session: Session) -> None:
    rows = session.scalars(select(StageAssignment)).all()
    assert {(r.stage_key, r.role) for r in rows} == {
        ("idea_generation", "primary"),
        ("market_research", "primary"),
    }


def test_ai_employee_cannot_be_assigned_to_human_review(
    session: Session, employee: AIEmployee
) -> None:
    _assert_rejected(
        session,
        lambda: StageAssignment(
            organization_id=DEFAULT_ORGANIZATION_ID,
            stage_key="human_review",
            ai_employee_id=employee.id,
            role="secondary",
        ),
    )


def test_assignment_stage_must_match_employee_stage(session: Session, employee: AIEmployee) -> None:
    _assert_rejected(
        session,
        lambda: StageAssignment(
            organization_id=DEFAULT_ORGANIZATION_ID,
            stage_key="competitor_research",
            ai_employee_id=employee.id,
            role="secondary",
        ),
    )


def test_assignment_organization_must_match_employee(
    session: Session, employee: AIEmployee, other_org: Organization
) -> None:
    _assert_rejected(
        session,
        lambda: StageAssignment(
            organization_id=other_org.id,
            stage_key=employee.stage_key,
            ai_employee_id=employee.id,
            role="primary",
        ),
    )


def test_one_primary_per_stage(session: Session, employee: AIEmployee) -> None:
    second = AIEmployee(
        organization_id=DEFAULT_ORGANIZATION_ID,
        key="second_researcher",
        name="Second",
        role="調査",
        stage_key=employee.stage_key,
        status="active",
    )
    session.add(second)
    session.commit()
    _assert_rejected(
        session,
        lambda: StageAssignment(
            organization_id=DEFAULT_ORGANIZATION_ID,
            stage_key=employee.stage_key,
            ai_employee_id=second.id,
            role="primary",
        ),
    )
    session.add(
        StageAssignment(
            organization_id=DEFAULT_ORGANIZATION_ID,
            stage_key=employee.stage_key,
            ai_employee_id=second.id,
            role="secondary",
        )
    )
    session.commit()


# ---------------------------------------------------------------- 組織（R-01・B-03）


def test_ai_employee_key_unique_within_organization(
    session: Session, employee: AIEmployee, other_org: Organization
) -> None:
    def copy(org_id: object) -> AIEmployee:
        return AIEmployee(
            organization_id=org_id,
            key=employee.key,
            name="copy",
            role="調査",
            stage_key=employee.stage_key,
        )

    _assert_rejected(session, lambda: copy(DEFAULT_ORGANIZATION_ID))
    session.add(copy(other_org.id))
    session.commit()


def test_child_must_be_in_parent_organization(
    session: Session, human: Actor, other_org: Organization
) -> None:
    exploration = Exploration(
        organization_id=DEFAULT_ORGANIZATION_ID,
        title="t",
        theme="theme",
        created_by_actor_id=human.id,
    )
    session.add(exploration)
    session.commit()
    _assert_rejected(
        session,
        lambda: Idea(
            organization_id=other_org.id,
            exploration_id=exploration.id,
            title="x",
            origin_type="human",
        ),
    )
