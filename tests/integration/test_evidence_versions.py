"""Evidence の版の連鎖と消去の記録の DB レベルの安全装置（第2回仕様 4章・14章）。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_business_explorer.infrastructure.db.models import Actor, Evidence, Exploration
from ai_business_explorer.seed import DEFAULT_ORGANIZATION_ID


def _assert_rejected(session: Session, make: Callable[[], object]) -> None:
    session.add(make())
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


@pytest.fixture
def exploration(session: Session, human: Actor) -> Exploration:
    e = Exploration(
        organization_id=DEFAULT_ORGANIZATION_ID, title="t", theme="t", created_by_actor_id=human.id
    )
    session.add(e)
    session.commit()
    return e


def _evidence(exploration: Exploration, human: Actor, **kw: Any) -> Evidence:
    return Evidence(
        organization_id=exploration.organization_id,
        exploration_id=exploration.id,
        source_type="human_input",
        title=kw.pop("title", "統計"),
        content_hash="0" * 64,
        created_by_actor_id=human.id,
        **kw,
    )


@pytest.fixture
def evidence(session: Session, exploration: Exploration, human: Actor) -> Evidence:
    ev = _evidence(exploration, human)
    session.add(ev)
    session.commit()
    return ev


def test_purge_must_be_recorded_with_reason_and_human(
    session: Session, exploration: Exploration, human: Actor, system_actor: Actor
) -> None:
    now = datetime.now(UTC)
    _assert_rejected(session, lambda: _evidence(exploration, human, content_purged_at=now))
    _assert_rejected(
        session,
        lambda: _evidence(
            exploration,
            human,
            content_purged_at=now,
            purge_reason="r",
            purged_by_actor_id=system_actor.id,
            purged_by_actor_type="system",
        ),
    )
    # actor_type を 'human' と偽っても、複合 FK により実在の human actor 以外は拒否される
    _assert_rejected(
        session,
        lambda: _evidence(
            exploration,
            human,
            content_purged_at=now,
            purge_reason="r",
            purged_by_actor_id=system_actor.id,
            purged_by_actor_type="human",
        ),
    )


def test_an_evidence_has_at_most_one_newer_version(
    session: Session, exploration: Exploration, human: Actor, evidence: Evidence
) -> None:
    session.add(_evidence(exploration, human, title="v2", supersedes_evidence_id=evidence.id))
    session.commit()
    _assert_rejected(
        session,
        lambda: _evidence(exploration, human, title="v2b", supersedes_evidence_id=evidence.id),
    )


def test_evidence_cannot_supersede_itself(session: Session, evidence: Evidence) -> None:
    evidence.supersedes_evidence_id = evidence.id
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
