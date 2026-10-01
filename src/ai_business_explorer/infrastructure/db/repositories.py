"""Repository 層。サービスはここを通じて DB にアクセスする（AI社員はアクセスできない）。

組織の範囲:
API はリクエストごとに `scope_to_organization` でセッションに操作者の組織を設定する。
設定されたセッションでは、organization_id を持つモデルの取得・一覧はその組織に限られ、
他組織の行は「存在しない」（NotFoundError → 404）として扱われる。
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, exists, false, or_, select
from sqlalchemy.orm import Session, aliased

from ai_business_explorer.domain.errors import NotFoundError
from ai_business_explorer.domain.evidence import EvidenceStatus
from ai_business_explorer.infrastructure.db.base import Base
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    AIEmployee,
    Analysis,
    AuditEvent,
    Claim,
    ClaimEvidenceLink,
    Evidence,
    Execution,
    Exploration,
    HumanDecision,
    HumanReview,
    Idea,
    OrganizationMembership,
    StageAssignment,
    StageRun,
)

_ORGANIZATION_KEY = "organization_id"


def scope_to_organization(session: Session, organization_id: UUID) -> None:
    """このセッションの取得・一覧を、指定した組織に限る。"""
    session.info[_ORGANIZATION_KEY] = organization_id


def scoped_organization_id(session: Session) -> UUID | None:
    value = session.info.get(_ORGANIZATION_KEY)
    return value if isinstance(value, UUID) else None


class Repository[M: Base]:
    model: type[M]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, id_: UUID) -> M | None:
        obj = self.session.get(self.model, id_)
        if obj is None:
            return None
        org_id = scoped_organization_id(self.session)
        if org_id is not None and getattr(obj, "organization_id", org_id) != org_id:
            return None
        return obj

    def get_or_raise(self, id_: UUID) -> M:
        obj = self.get(id_)
        if obj is None:
            raise NotFoundError(f"{self.model.__tablename__} not found: {id_}")
        return obj

    def add(self, obj: M) -> M:
        self.session.add(obj)
        self.session.flush()
        return obj

    def select(self, *criteria: ColumnElement[bool]) -> Select[tuple[M]]:
        """組織の範囲で絞り込んだ select（ページングなどに使う）。"""
        return select(self.model).where(*criteria, *self._organization_criteria())

    def list_where(self, *criteria: ColumnElement[bool], order_by: Any = None) -> Sequence[M]:
        stmt = self.select(*criteria)
        stmt = stmt.order_by(order_by if order_by is not None else self.model.created_at)  # type: ignore[attr-defined]
        return self.session.scalars(stmt).all()

    def _organization_criteria(self) -> list[ColumnElement[bool]]:
        org_id = scoped_organization_id(self.session)
        column = getattr(self.model, "organization_id", None)
        if org_id is None or column is None:
            return []
        return [column == org_id]


class ActorRepository(Repository[Actor]):
    model = Actor

    def select_in_organization(self, organization_id: UUID) -> Select[tuple[Actor]]:
        return (
            select(Actor)
            .join(OrganizationMembership, OrganizationMembership.actor_id == Actor.id)
            .where(OrganizationMembership.organization_id == organization_id)
        )


class OrganizationMembershipRepository(Repository[OrganizationMembership]):
    model = OrganizationMembership

    def for_actor(self, actor_id: UUID) -> OrganizationMembership | None:
        stmt = select(OrganizationMembership).where(OrganizationMembership.actor_id == actor_id)
        return self.session.scalars(stmt).one_or_none()


class AIEmployeeRepository(Repository[AIEmployee]):
    model = AIEmployee

    def get_by_key(self, organization_id: UUID, key: str) -> AIEmployee | None:
        stmt = select(AIEmployee).where(
            AIEmployee.organization_id == organization_id, AIEmployee.key == key
        )
        return self.session.scalars(stmt).one_or_none()


class StageAssignmentRepository(Repository[StageAssignment]):
    model = StageAssignment


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
        self, exploration_id: UUID, idea_id: UUID | None, stage_key: str, ai_employee_id: UUID
    ) -> Analysis | None:
        """版の連鎖は「範囲 × ステージ × AI社員」単位（第2回仕様 7章）。"""
        scope = (
            [Analysis.exploration_id == exploration_id, Analysis.idea_id.is_(None)]
            if idea_id is None
            else [Analysis.idea_id == idea_id]
        )
        stmt = (
            select(Analysis)
            .where(
                *scope, Analysis.stage_key == stage_key, Analysis.ai_employee_id == ai_employee_id
            )
            .order_by(Analysis.version_no.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()


class EvidenceRepository(Repository[Evidence]):
    model = Evidence

    @staticmethod
    def _has_successor() -> ColumnElement[bool]:
        newer = aliased(Evidence)
        return exists().where(newer.supersedes_evidence_id == Evidence.id)

    @classmethod
    def status_criteria(cls, statuses: Sequence[EvidenceStatus]) -> ColumnElement[bool]:
        """状態（優先順位 purged > retracted > superseded > active）で絞る条件。"""
        purged = Evidence.content_purged_at.is_not(None)
        not_purged = Evidence.content_purged_at.is_(None)
        retracted = Evidence.retracted_at.is_not(None)
        not_retracted = Evidence.retracted_at.is_(None)
        superseded = cls._has_successor()
        by_status: dict[EvidenceStatus, ColumnElement[bool]] = {
            EvidenceStatus.PURGED: purged,
            EvidenceStatus.RETRACTED: and_(not_purged, retracted),
            EvidenceStatus.SUPERSEDED: and_(not_purged, not_retracted, superseded),
            EvidenceStatus.ACTIVE: and_(not_purged, not_retracted, ~superseded),
        }
        if not statuses:
            return false()
        return or_(*(by_status[s] for s in statuses))

    def successors(self, evidence_ids: Sequence[UUID]) -> dict[UUID, UUID]:
        """旧版の ID → 更新版の ID。"""
        if not evidence_ids:
            return {}
        stmt = select(Evidence.supersedes_evidence_id, Evidence.id).where(
            Evidence.supersedes_evidence_id.in_(evidence_ids)
        )
        return {old: new for old, new in self.session.execute(stmt).tuples() if old is not None}

    def duplicates(
        self, organization_id: UUID, exploration_id: UUID, source_key: str | None, content_hash: str
    ) -> Sequence[Evidence]:
        """同じ組織・探索案件の active な Evidence のうち、出典か内容が同じもの（第2回 4章）。"""
        same = Evidence.content_hash == content_hash
        if source_key is not None:
            same = or_(same, Evidence.source_key == source_key)
        stmt = select(Evidence).where(
            Evidence.organization_id == organization_id,
            Evidence.exploration_id == exploration_id,
            self.status_criteria([EvidenceStatus.ACTIVE]),
            same,
        )
        return self.session.scalars(stmt.order_by(Evidence.created_at, Evidence.id)).all()


class ClaimRepository(Repository[Claim]):
    model = Claim

    def for_analysis(self, analysis_id: UUID) -> Sequence[Claim]:
        return self.list_where(Claim.analysis_id == analysis_id, order_by=Claim.ordinal)


class ClaimEvidenceLinkRepository:
    """主張と Evidence の関係（正本）。

    第1回の analysis_evidence_links は凍結済みで、書き込まない。
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, link: ClaimEvidenceLink) -> None:
        self.session.add(link)

    def for_claims(self, claim_ids: Sequence[UUID]) -> Sequence[ClaimEvidenceLink]:
        if not claim_ids:
            return []
        stmt = select(ClaimEvidenceLink).where(ClaimEvidenceLink.claim_id.in_(claim_ids))
        return self.session.scalars(stmt).all()


class HumanReviewRepository(Repository[HumanReview]):
    model = HumanReview


class HumanDecisionRepository(Repository[HumanDecision]):
    model = HumanDecision


class AuditEventRepository(Repository[AuditEvent]):
    model = AuditEvent
