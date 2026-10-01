"""Evidence（根拠・出典）の登録・撤回・本文の消去。

第2回も API から登録できるのは人間の入力のみ。AI 生成情報は Evidence にできない。
状態（active / superseded / retracted / purged）は保存せず算出する（E-01）。
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.application.commands import (
    EvidenceCreate,
    EvidencePurge,
    EvidenceRetract,
)
from ai_business_explorer.application.common import record_audit, require_human, utcnow
from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.domain.enums import EVIDENCE_SOURCE_TYPES_ENABLED, EvidenceSourceType
from ai_business_explorer.domain.errors import DomainValidationError, InvalidStateError
from ai_business_explorer.domain.evidence import (
    EvidenceScope,
    EvidenceState,
    EvidenceStatus,
    normalize_url,
)
from ai_business_explorer.infrastructure.db.models import Actor, Evidence
from ai_business_explorer.infrastructure.db.repositories import (
    EvidenceRepository,
    ExplorationRepository,
    IdeaRepository,
)

DEFAULT_STATUSES: tuple[EvidenceStatus, ...] = (EvidenceStatus.ACTIVE,)


def content_hash(cmd: EvidenceCreate) -> str:
    material = {"title": cmd.title, "url": cmd.url, "quote": cmd.quote, "summary": cmd.summary}
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


@dataclass(frozen=True)
class CreatedEvidence:
    evidence: Evidence
    duplicate_of: Sequence[UUID]  # 重複している active な Evidence（拒否はしない。Q4）


class EvidenceService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.evidence = EvidenceRepository(session)
        self.explorations = ExplorationRepository(session)
        self.ideas = IdeaRepository(session)

    def create(self, actor: Actor, cmd: EvidenceCreate) -> CreatedEvidence:
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
        source_key = normalize_url(cmd.url) if cmd.url else None
        digest = content_hash(cmd)
        duplicates = self.evidence.duplicates(
            exploration.organization_id, exploration.id, source_key, digest
        )
        evidence = self.evidence.add(
            Evidence(
                **cmd.model_dump(exclude={"metadata", "source_type"}),
                organization_id=exploration.organization_id,
                source_type=cmd.source_type.value,
                metadata_=cmd.metadata,
                content_hash=digest,
                source_key=source_key,
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
            after={
                "title": evidence.title,
                "source_type": evidence.source_type,
                "duplicate_of": [d.id for d in duplicates],
            },
        )
        self.session.commit()
        # 登録時の応答も、保存後に読み直した値を返す（Q1）
        self.session.refresh(evidence)
        return CreatedEvidence(evidence=evidence, duplicate_of=[d.id for d in duplicates])

    def get(self, evidence_id: UUID) -> Evidence:
        """個別取得はどの状態でもできる（purged は本文が空）。"""
        return self.evidence.get_or_raise(evidence_id)

    def states(self, evidence: Sequence[Evidence]) -> dict[UUID, EvidenceState]:
        successors = self.evidence.successors([e.id for e in evidence])
        return {
            e.id: EvidenceState(
                is_purged=e.content_purged_at is not None,
                is_retracted=e.retracted_at is not None,
                superseded_by_id=successors.get(e.id),
            )
            for e in evidence
        }

    def list_for_exploration(
        self,
        exploration_id: UUID,
        page: PageRequest,
        statuses: Sequence[EvidenceStatus] = DEFAULT_STATUSES,
        source_type: EvidenceSourceType | None = None,
    ) -> Page[Evidence]:
        self.explorations.get_or_raise(exploration_id)
        return self._page(page, statuses, source_type, Evidence.exploration_id == exploration_id)

    def list_for_idea(
        self,
        idea_id: UUID,
        page: PageRequest,
        statuses: Sequence[EvidenceStatus] = DEFAULT_STATUSES,
        scope: EvidenceScope = EvidenceScope.WITH_EXPLORATION,
        source_type: EvidenceSourceType | None = None,
    ) -> Page[Evidence]:
        idea = self.ideas.get_or_raise(idea_id)
        if scope is EvidenceScope.IDEA_ONLY:
            in_scope = Evidence.idea_id == idea.id
        else:  # 案件全体の Evidence も含める（AI の入力範囲と同じ）
            in_scope = (Evidence.exploration_id == idea.exploration_id) & (
                Evidence.idea_id.is_(None) | (Evidence.idea_id == idea.id)
            )
        return self._page(page, statuses, source_type, in_scope)

    def _page(
        self,
        page: PageRequest,
        statuses: Sequence[EvidenceStatus],
        source_type: EvidenceSourceType | None,
        *criteria: object,
    ) -> Page[Evidence]:
        conditions = [*criteria, self.evidence.status_criteria(statuses)]
        if source_type is not None:
            conditions.append(Evidence.source_type == source_type.value)
        return paginate(
            self.session,
            self.evidence.select(*conditions),  # type: ignore[arg-type]
            sort_column=Evidence.created_at,
            id_column=Evidence.id,
            page=page,
        )

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

    def purge(self, actor: Actor, evidence_id: UUID, cmd: EvidencePurge) -> Evidence:
        """本文（quote・summary）だけを消去する。行・ID・出典・ハッシュ・来歴・根拠リンクは残す。"""
        require_human(actor, "purge evidence")
        evidence = self.evidence.get_or_raise(evidence_id)
        if evidence.content_purged_at is not None:
            raise InvalidStateError("evidence content is already purged")
        evidence.quote = None
        evidence.summary = None
        evidence.content_purged_at = utcnow()
        evidence.purge_reason = cmd.reason
        evidence.purged_by_actor_id = actor.id
        evidence.purged_by_actor_type = actor.actor_type
        record_audit(
            self.session,
            organization_id=evidence.organization_id,
            entity_type="evidence",
            entity_id=evidence.id,
            action="purged",
            actor_id=actor.id,
            after={"reason": cmd.reason},
        )
        self.session.commit()
        return evidence
