"""Idea の登録・更新・採否。採否と詳細項目の更新は人間のみ。"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.application.commands import IdeaAdoptionCommand, IdeaCreate, IdeaUpdate
from ai_business_explorer.application.common import record_audit, require_human, snapshot
from ai_business_explorer.domain.enums import AdoptionStatus, OriginType
from ai_business_explorer.domain.errors import InvalidStateError
from ai_business_explorer.infrastructure.db.models import Actor, Idea
from ai_business_explorer.infrastructure.db.repositories import (
    ExplorationRepository,
    IdeaRepository,
    StageRunRepository,
)

PROFILE_FIELDS = list(IdeaUpdate.model_fields)


@dataclass(frozen=True)
class ResearchStatus:
    """調査ステータス（保存せず算出する）。"""

    current_stage_key: str | None
    latest_stage_key: str | None
    latest_stage_run_status: str | None


class IdeaService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ideas = IdeaRepository(session)
        self.explorations = ExplorationRepository(session)
        self.stage_runs = StageRunRepository(session)

    def create(self, actor: Actor, exploration_id: UUID, cmd: IdeaCreate) -> Idea:
        require_human(actor, "create ideas")
        exploration = self.explorations.get_or_raise(exploration_id)
        idea = self.ideas.add(
            Idea(
                organization_id=exploration.organization_id,
                exploration_id=exploration_id,
                **cmd.model_dump(),
                origin_type=OriginType.HUMAN.value,
                adoption_status=AdoptionStatus.CANDIDATE.value,
                created_by_actor_id=actor.id,
            )
        )
        record_audit(
            self.session,
            organization_id=idea.organization_id,
            entity_type="idea",
            entity_id=idea.id,
            action="created",
            actor_id=actor.id,
            after=snapshot(idea, [*PROFILE_FIELDS, "adoption_status", "origin_type"]),
        )
        self.session.commit()
        return idea

    def get(self, idea_id: UUID) -> Idea:
        return self.ideas.get_or_raise(idea_id)

    def list_for_exploration(self, exploration_id: UUID) -> Sequence[Idea]:
        self.explorations.get_or_raise(exploration_id)
        return self.ideas.list_where(Idea.exploration_id == exploration_id)

    def update(self, actor: Actor, idea_id: UUID, cmd: IdeaUpdate) -> Idea:
        require_human(actor, "update ideas")
        idea = self.ideas.get_or_raise(idea_id)
        changes = cmd.model_dump(exclude_unset=True)
        if not changes:
            return idea
        before = snapshot(idea, list(changes))
        for field, value in changes.items():
            setattr(idea, field, value)
        record_audit(
            self.session,
            organization_id=idea.organization_id,
            entity_type="idea",
            entity_id=idea.id,
            action="updated",
            actor_id=actor.id,
            before=before,
            after=snapshot(idea, list(changes)),
        )
        self.session.commit()
        return idea

    def adopt(self, actor: Actor, idea_id: UUID, cmd: IdeaAdoptionCommand) -> Idea:
        return self._transition(actor, idea_id, AdoptionStatus.ADOPTED, cmd.reason)

    def reject(self, actor: Actor, idea_id: UUID, cmd: IdeaAdoptionCommand) -> Idea:
        return self._transition(actor, idea_id, AdoptionStatus.REJECTED, cmd.reason)

    def _transition(
        self, actor: Actor, idea_id: UUID, to: AdoptionStatus, reason: str | None
    ) -> Idea:
        require_human(actor, "adopt or reject ideas")
        idea = self.ideas.get_or_raise(idea_id)
        if idea.adoption_status != AdoptionStatus.CANDIDATE.value:
            raise InvalidStateError(
                f"idea is '{idea.adoption_status}'; "
                "only 'candidate' ideas can be adopted or rejected"
            )
        idea.adoption_status = to.value
        record_audit(
            self.session,
            organization_id=idea.organization_id,
            entity_type="idea",
            entity_id=idea.id,
            action=f"adoption_status.{to.value}",
            actor_id=actor.id,
            before={"adoption_status": AdoptionStatus.CANDIDATE.value},
            after={"adoption_status": to.value, "reason": reason},
        )
        self.session.commit()
        return idea

    def research_status(self, idea: Idea) -> ResearchStatus:
        latest = self.stage_runs.latest(idea.exploration_id, idea.id)
        return ResearchStatus(
            current_stage_key=idea.current_stage_key,
            latest_stage_key=latest.stage_key if latest else None,
            latest_stage_run_status=latest.status if latest else None,
        )
