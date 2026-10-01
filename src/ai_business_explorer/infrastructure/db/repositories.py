"""Repository 層。サービスはここを通じて DB にアクセスする（AI社員はアクセスできない）。"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from ai_business_explorer.domain.errors import NotFoundError
from ai_business_explorer.infrastructure.db.base import Base
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    AIEmployee,
    Analysis,
    AnalysisEvidenceLink,
    AuditEvent,
    Evidence,
    Execution,
    Exploration,
    HumanDecision,
    HumanReview,
    Idea,
    StageRun,
)


class Repository[M: Base]:
    model: type[M]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, id_: UUID) -> M | None:
        return self.session.get(self.model, id_)

    def get_or_raise(self, id_: UUID) -> M:
        obj = self.get(id_)
        if obj is None:
            raise NotFoundError(f"{self.model.__tablename__} not found: {id_}")
        return obj

    def add(self, obj: M) -> M:
        self.session.add(obj)
        self.session.flush()
        return obj

    def list_where(self, *criteria: ColumnElement[bool], order_by: Any = None) -> Sequence[M]:
        stmt = select(self.model).where(*criteria)
        stmt = stmt.order_by(order_by if order_by is not None else self.model.created_at)  # type: ignore[attr-defined]
        return self.session.scalars(stmt).all()


class ActorRepository(Repository[Actor]):
    model = Actor


class AIEmployeeRepository(Repository[AIEmployee]):
    model = AIEmployee

    def get_by_key(self, key: str) -> AIEmployee | None:
        return self.session.scalars(select(AIEmployee).where(AIEmployee.key == key)).one_or_none()


class ExplorationRepository(Repository[Exploration]):
    model = Exploration


class IdeaRepository(Repository[Idea]):
    model = Idea


class StageRunRepository(Repository[StageRun]):
    model = StageRun

    def _scope(self, exploration_id: UUID, idea_id: UUID | None) -> list[ColumnElement[bool]]:
        if idea_id is None:
            return [StageRun.exploration_id == exploration_id, StageRun.idea_id.is_(None)]
        return [StageRun.idea_id == idea_id]

    def current(
        self, exploration_id: UUID, idea_id: UUID | None, stage_key: str
    ) -> StageRun | None:
        """スコープ・ステージの最新（未 supersede）試行。"""
        stmt = select(StageRun).where(
            *self._scope(exploration_id, idea_id),
            StageRun.stage_key == stage_key,
            StageRun.superseded_at.is_(None),
        )
        return self.session.scalars(stmt).one_or_none()

    def current_all(self, exploration_id: UUID, idea_id: UUID | None) -> Sequence[StageRun]:
        stmt = select(StageRun).where(
            *self._scope(exploration_id, idea_id), StageRun.superseded_at.is_(None)
        )
        return self.session.scalars(stmt).all()

    def next_attempt_no(self, exploration_id: UUID, idea_id: UUID | None, stage_key: str) -> int:
        stmt = select(StageRun.attempt_no).where(
            *self._scope(exploration_id, idea_id), StageRun.stage_key == stage_key
        )
        attempts = self.session.scalars(stmt).all()
        return max(attempts, default=0) + 1

    def list_for(self, exploration_id: UUID, idea_id: UUID | None) -> Sequence[StageRun]:
        stmt = select(StageRun).where(*self._scope(exploration_id, idea_id))
        return self.session.scalars(stmt.order_by(StageRun.started_at)).all()

    def latest(self, exploration_id: UUID, idea_id: UUID | None) -> StageRun | None:
        stmt = (
            select(StageRun)
            .where(*self._scope(exploration_id, idea_id))
            .order_by(StageRun.started_at.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()


class ExecutionRepository(Repository[Execution]):
    model = Execution


class AnalysisRepository(Repository[Analysis]):
    model = Analysis

    def latest_for_stage(
        self, exploration_id: UUID, idea_id: UUID | None, stage_key: str
    ) -> Analysis | None:
        scope = (
            [Analysis.exploration_id == exploration_id, Analysis.idea_id.is_(None)]
            if idea_id is None
            else [Analysis.idea_id == idea_id]
        )
        stmt = (
            select(Analysis)
            .where(*scope, Analysis.stage_key == stage_key)
            .order_by(Analysis.version_no.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()


class EvidenceRepository(Repository[Evidence]):
    model = Evidence


class AnalysisEvidenceLinkRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, link: AnalysisEvidenceLink) -> None:
        self.session.add(link)

    def for_analysis(self, analysis_id: UUID) -> Sequence[AnalysisEvidenceLink]:
        stmt = select(AnalysisEvidenceLink).where(AnalysisEvidenceLink.analysis_id == analysis_id)
        return self.session.scalars(stmt).all()


class HumanReviewRepository(Repository[HumanReview]):
    model = HumanReview


class HumanDecisionRepository(Repository[HumanDecision]):
    model = HumanDecision


class AuditEventRepository(Repository[AuditEvent]):
    model = AuditEvent
