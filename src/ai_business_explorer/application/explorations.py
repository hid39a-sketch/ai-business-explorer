from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.application.commands import ExplorationCreate, ExplorationUpdate
from ai_business_explorer.application.common import (
    current_organization_id,
    record_audit,
    require_human,
    snapshot,
)
from ai_business_explorer.infrastructure.db.models import Actor, Exploration
from ai_business_explorer.infrastructure.db.repositories import ExplorationRepository

AUDIT_FIELDS = ["title", "theme", "description", "status"]


class ExplorationService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.explorations = ExplorationRepository(session)

    def create(self, actor: Actor, cmd: ExplorationCreate) -> Exploration:
        require_human(actor, "create explorations")
        exploration = self.explorations.add(
            Exploration(
                **cmd.model_dump(),
                organization_id=current_organization_id(self.session),
                created_by_actor_id=actor.id,
            )
        )
        record_audit(
            self.session,
            organization_id=exploration.organization_id,
            entity_type="exploration",
            entity_id=exploration.id,
            action="created",
            actor_id=actor.id,
            after=snapshot(exploration, AUDIT_FIELDS),
        )
        self.session.commit()
        return exploration

    def get(self, exploration_id: UUID) -> Exploration:
        return self.explorations.get_or_raise(exploration_id)

    def list(self) -> Sequence[Exploration]:
        return self.explorations.list_where()

    def update(self, actor: Actor, exploration_id: UUID, cmd: ExplorationUpdate) -> Exploration:
        require_human(actor, "update explorations")
        exploration = self.explorations.get_or_raise(exploration_id)
        changes = cmd.model_dump(exclude_unset=True)
        if not changes:
            return exploration
        before = snapshot(exploration, AUDIT_FIELDS)
        for field, value in changes.items():
            setattr(exploration, field, value)
        record_audit(
            self.session,
            organization_id=exploration.organization_id,
            entity_type="exploration",
            entity_id=exploration.id,
            action="updated",
            actor_id=actor.id,
            before=before,
            after=snapshot(exploration, AUDIT_FIELDS),
        )
        self.session.commit()
        return exploration
