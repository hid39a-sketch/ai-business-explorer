"""Evidence（根拠・出典）の登録・撤回。第1回は人間の入力のみ。AI 生成情報は Evidence にできない。"""

import hashlib
import json
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.application.commands import EvidenceCreate, EvidenceRetract
from ai_business_explorer.application.common import record_audit, require_human, utcnow
from ai_business_explorer.domain.enums import EVIDENCE_SOURCE_TYPES_ENABLED
from ai_business_explorer.domain.errors import DomainValidationError, InvalidStateError
from ai_business_explorer.infrastructure.db.models import Actor, Evidence
from ai_business_explorer.infrastructure.db.repositories import (
    EvidenceRepository,
    ExplorationRepository,
    IdeaRepository,
)


def content_hash(cmd: EvidenceCreate) -> str:
    material = {"title": cmd.title, "url": cmd.url, "quote": cmd.quote, "summary": cmd.summary}
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class EvidenceService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.evidence = EvidenceRepository(session)
        self.explorations = ExplorationRepository(session)
        self.ideas = IdeaRepository(session)

    def create(self, actor: Actor, cmd: EvidenceCreate) -> Evidence:
        require_human(actor, "register evidence")
        if cmd.source_type not in EVIDENCE_SOURCE_TYPES_ENABLED:
            raise DomainValidationError(
                f"source_type '{cmd.source_type}' is not enabled in this phase "
                f"(allowed: {sorted(EVIDENCE_SOURCE_TYPES_ENABLED)})"
            )
        exploration = self.explorations.get_or_raise(cmd.exploration_id)
        if cmd.idea_id is not None:
            idea = self.ideas.get_or_raise(cmd.idea_id)
            if idea.exploration_id != cmd.exploration_id:
                raise DomainValidationError("idea does not belong to the exploration")
        evidence = self.evidence.add(
            Evidence(
                **cmd.model_dump(exclude={"metadata", "source_type"}),
                organization_id=exploration.organization_id,
                source_type=cmd.source_type.value,
                metadata_=cmd.metadata,
                content_hash=content_hash(cmd),
                created_by_actor_id=actor.id,
            )
        )
        record_audit(
            self.session,
            organization_id=evidence.organization_id,
            entity_type="evidence",
            entity_id=evidence.id,
            action="created",
            actor_id=actor.id,
            after={"title": evidence.title, "source_type": evidence.source_type},
        )
        self.session.commit()
        return evidence

    def get(self, evidence_id: UUID) -> Evidence:
        return self.evidence.get_or_raise(evidence_id)

    def list_for_exploration(self, exploration_id: UUID) -> Sequence[Evidence]:
        self.explorations.get_or_raise(exploration_id)
        return self.evidence.list_where(Evidence.exploration_id == exploration_id)

    def list_for_idea(self, idea_id: UUID) -> Sequence[Evidence]:
        self.ideas.get_or_raise(idea_id)
        return self.evidence.list_where(Evidence.idea_id == idea_id)

    def retract(self, actor: Actor, evidence_id: UUID, cmd: EvidenceRetract) -> Evidence:
        require_human(actor, "retract evidence")
        evidence = self.evidence.get_or_raise(evidence_id)
        if evidence.retracted_at is not None:
            raise InvalidStateError("evidence is already retracted")
        evidence.retracted_at = utcnow()
        evidence.retraction_reason = cmd.reason
        record_audit(
            self.session,
            organization_id=evidence.organization_id,
            entity_type="evidence",
            entity_id=evidence.id,
            action="retracted",
            actor_id=actor.id,
            after={"reason": cmd.reason},
        )
        self.session.commit()
        return evidence
