"""Evidence 候補と AI生成の補助情報（第2回仕様 2章・3章・4章。B-21・E-02）。

- Tool が取得した情報は、まず Evidence 候補として保存する。候補が持つのは原情報（URL・title・
  取得日時・メタデータ・抜粋・全文）だけで、AI が書いた文章は持たない。
- AI が書いた要約・解釈は「AI生成の補助情報」として別テーブルに保存する。元の候補と、生成した
  実行を必ず参照する。承認でも補助情報は読まないので、Evidence に移る経路はない。
- 承認・却下は人間（member 以上）だけ。承認で Evidence が作られ、承認した人間が登録者になる。
  人間の要約（summary）は承認時に受け取り、原情報（quote）と分けて保存する。
- 重複判定（4章）：同じ探索案件に、source_key と snapshot_hash が同じ active な Evidence があれば
  duplicate（承認不要）。source_key だけが同じなら更新版の候補で、承認すると前の版を置き換える
  （前の版は superseded。撤回はしない。E-01）。
- 未承認の候補と補助情報は、AI の入力にも根拠にもならない。
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_business_explorer.application.commands import (
    CandidateAccept,
    CandidateBulkAccept,
    CandidateReject,
)
from ai_business_explorer.application.common import record_audit, require_human, utcnow
from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.domain.enums import (
    AcquisitionMethod,
    CandidateStatus,
    EvidenceSourceType,
)
from ai_business_explorer.domain.errors import DomainValidationError, InvalidStateError
from ai_business_explorer.domain.evidence import EvidenceStatus, normalize_url
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    Evidence,
    EvidenceCandidate,
    EvidenceCandidateAINote,
    Execution,
    ToolCall,
    ToolCallOutput,
)
from ai_business_explorer.infrastructure.db.repositories import (
    EvidenceCandidateAINoteRepository,
    EvidenceCandidateRepository,
    EvidenceRepository,
    ExplorationRepository,
)
from ai_business_explorer.tools.base import ToolError, ToolResult

EXCERPT_MAX_CHARS = 2000  # 抜粋の上限（R-20）
MAX_BULK_ACCEPT = 100


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _active_with_source_key(
    session: Session, exploration_id: UUID, source_key: str
) -> list[Evidence]:
    """同じ探索案件で、同じ出典の active な Evidence（重複判定の範囲。4章）。"""
    return list(
        session.scalars(
            select(Evidence)
            .where(
                Evidence.exploration_id == exploration_id,
                Evidence.source_key == source_key,
                EvidenceRepository.status_criteria([EvidenceStatus.ACTIVE]),
            )
            .order_by(Evidence.created_at.desc(), Evidence.id.desc())
        ).all()
    )


def _classify_against_evidence(
    session: Session, candidate: EvidenceCandidate
) -> tuple[Evidence | None, Evidence | None]:
    """(重複している Evidence, 更新版として置き換える Evidence) を返す。"""
    if candidate.source_key is None:
        return None, None
    actives = _active_with_source_key(session, candidate.exploration_id, candidate.source_key)
    if candidate.snapshot_hash is not None:
        same = next((e for e in actives if e.snapshot_hash == candidate.snapshot_hash), None)
        if same is not None:
            return same, None
    return None, (actives[0] if actives else None)


def store_tool_candidates(
    session: Session,
    execution: Execution,
    exploration_id: UUID,
    tool_call: ToolCall,
    result: ToolResult,
) -> ToolResult:
    """Tool の結果の候補を保存し、candidate_id を付けた結果を返す（コミットは呼び出し側）。"""
    now = utcnow()
    annotated = []
    for item in result.evidence_candidates:
        try:
            source_type = EvidenceSourceType(item.source_type)
        except ValueError as exc:
            raise ToolError(f"unknown evidence source_type '{item.source_type}'") from exc
        source_key = item.source_key or (normalize_url(item.url) if item.url else None)
        body = item.snapshot if item.snapshot is not None else item.quote
        excerpt = item.quote if item.quote is not None else item.snapshot
        candidate = EvidenceCandidate(
            organization_id=execution.organization_id,
            exploration_id=exploration_id,
            idea_id=execution.idea_id,
            execution_id=execution.id,
            tool_call_id=tool_call.id,
            source_type=source_type.value,
            title=item.title[:500],
            url=item.url,
            source_key=source_key,
            quote=excerpt[:EXCERPT_MAX_CHARS] if excerpt is not None else None,
            snapshot=item.snapshot,
            snapshot_hash=_sha256(body) if body is not None else None,
            published_at=item.published_at,
            retrieved_at=item.retrieved_at or now,
            metadata_=item.metadata,
            status=CandidateStatus.PENDING.value,
        )
        duplicate, updates = _classify_against_evidence(session, candidate)
        if duplicate is not None:
            candidate.status = CandidateStatus.DUPLICATE.value
            candidate.duplicate_of_evidence_id = duplicate.id
        candidate.updates_evidence_id = updates.id if updates else None
        session.add(candidate)
        session.flush()
        record_audit(
            session,
            organization_id=candidate.organization_id,
            entity_type="evidence_candidate",
            entity_id=candidate.id,
            action="created",
            execution_id=execution.id,
            after={
                "status": candidate.status,
                "tool_call_id": tool_call.id,
                "source_key": candidate.source_key,
                "duplicate_of_evidence_id": candidate.duplicate_of_evidence_id,
                "updates_evidence_id": candidate.updates_evidence_id,
            },
        )
        annotated.append(item.model_copy(update={"candidate_id": candidate.id}))
    return result.model_copy(update={"evidence_candidates": annotated})


def store_ai_notes(
    session: Session, execution: Execution, notes: Sequence[tuple[UUID, str]]
) -> list[EvidenceCandidateAINote]:
    """AI生成の補助情報を保存する。参照できるのは、この実行が集めた候補だけ。"""
    if not notes:
        return []
    own = set(
        session.scalars(
            select(EvidenceCandidate.id).where(EvidenceCandidate.execution_id == execution.id)
        ).all()
    )
    saved = []
    for candidate_id, text in notes:
        if candidate_id not in own:
            raise DomainValidationError(
                f"AI note refers to candidate {candidate_id} not collected by this execution"
            )
        note = EvidenceCandidateAINote(
            organization_id=execution.organization_id,
            candidate_id=candidate_id,
            execution_id=execution.id,
            note=text,
            llm_provider=execution.llm_provider,
            llm_model=execution.llm_model,
            prompt_key=execution.prompt_key,
            prompt_version=execution.prompt_version,
        )
        session.add(note)
        saved.append(note)
    session.flush()
    return saved


@dataclass
class BulkAcceptResult:
    accepted: list[Evidence] = field(default_factory=list)
    # 承認の時点で重複と分かった候補（duplicate にした。Evidence は作らない）
    duplicates: list[EvidenceCandidate] = field(default_factory=list)


class CandidateService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.candidates = EvidenceCandidateRepository(session)
        self.notes = EvidenceCandidateAINoteRepository(session)
        self.explorations = ExplorationRepository(session)

    # ------------------------------------------------------------------ 参照

    def list(
        self,
        exploration_id: UUID,
        page: PageRequest,
        statuses: Sequence[CandidateStatus] | None = None,
    ) -> Page[EvidenceCandidate]:
        self.explorations.get_or_raise(exploration_id)
        criteria = [EvidenceCandidate.exploration_id == exploration_id]
        if statuses:
            criteria.append(EvidenceCandidate.status.in_([s.value for s in statuses]))
        return paginate(
            self.session,
            self.candidates.select(*criteria),
            sort_column=EvidenceCandidate.created_at,
            id_column=EvidenceCandidate.id,
            page=page,
        )

    def get(self, candidate_id: UUID) -> EvidenceCandidate:
        return self.candidates.get_or_raise(candidate_id)

    def ai_notes(self, candidate_id: UUID, page: PageRequest) -> Page[EvidenceCandidateAINote]:
        """補助情報の参照（読み取りのみ）。"""
        self.candidates.get_or_raise(candidate_id)
        return paginate(
            self.session,
            self.notes.select(EvidenceCandidateAINote.candidate_id == candidate_id),
            sort_column=EvidenceCandidateAINote.created_at,
            id_column=EvidenceCandidateAINote.id,
            page=page,
        )

    # ------------------------------------------------------------------ 承認・却下（人間）

    def accept(self, actor: Actor, candidate_id: UUID, cmd: CandidateAccept) -> Evidence:
        require_human(actor, "accept evidence candidates")
        candidate = self._lock_pending(candidate_id)
        evidence = self._accept_one(actor, candidate, cmd)
        self._commit()
        if evidence is None:
            raise InvalidStateError(
                f"candidate duplicates active evidence {candidate.duplicate_of_evidence_id}"
            )
        return evidence

    def bulk_accept(self, actor: Actor, cmd: CandidateBulkAccept) -> BulkAcceptResult:
        """まとめて承認する。どれか1つでも承認できない状態なら、何も承認しない。"""
        require_human(actor, "accept evidence candidates")
        if len(set(cmd.candidate_ids)) != len(cmd.candidate_ids):
            raise DomainValidationError("candidate_ids must be unique")
        locked = [self._lock_pending(cid) for cid in cmd.candidate_ids]
        result = BulkAcceptResult()
        single = CandidateAccept(classification=cmd.classification)
        for candidate in locked:
            evidence = self._accept_one(actor, candidate, single)
            if evidence is None:
                result.duplicates.append(candidate)
            else:
                result.accepted.append(evidence)
        self._commit()
        return result

    def reject(self, actor: Actor, candidate_id: UUID, cmd: CandidateReject) -> EvidenceCandidate:
        require_human(actor, "reject evidence candidates")
        candidate = self._lock_pending(candidate_id)
        self._decide(actor, candidate, CandidateStatus.REJECTED, cmd.reason)
        self._commit()
        return candidate

    def _lock_pending(self, candidate_id: UUID) -> EvidenceCandidate:
        candidate = self.candidates.get_or_raise(candidate_id)
        self.session.refresh(candidate, with_for_update=True)
        if candidate.status != CandidateStatus.PENDING.value:
            raise InvalidStateError(
                f"candidate {candidate.id} is '{candidate.status}' and cannot be decided"
            )
        return candidate

    def _decide(
        self,
        actor: Actor,
        candidate: EvidenceCandidate,
        status: CandidateStatus,
        reason: str | None,
        evidence_id: UUID | None = None,
    ) -> None:
        candidate.status = status.value
        candidate.decided_by_actor_id = actor.id
        candidate.decided_by_actor_type = actor.actor_type
        candidate.decided_at = utcnow()
        candidate.decision_reason = reason
        record_audit(
            self.session,
            organization_id=candidate.organization_id,
            entity_type="evidence_candidate",
            entity_id=candidate.id,
            action=status.value,
            actor_id=actor.id,
            before={"status": CandidateStatus.PENDING.value},
            after={"status": status.value, "reason": reason, "evidence_id": evidence_id},
        )

    def _accept_one(
        self, actor: Actor, candidate: EvidenceCandidate, cmd: CandidateAccept
    ) -> Evidence | None:
        # 候補を作った後に Evidence が増えている場合があるので、承認の時点で判定し直す
        duplicate, updates = _classify_against_evidence(self.session, candidate)
        if duplicate is not None:
            candidate.status = CandidateStatus.DUPLICATE.value
            candidate.duplicate_of_evidence_id = duplicate.id
            record_audit(
                self.session,
                organization_id=candidate.organization_id,
                entity_type="evidence_candidate",
                entity_id=candidate.id,
                action="duplicate",
                actor_id=actor.id,
                after={"duplicate_of_evidence_id": duplicate.id},
            )
            return None
        material = {
            "title": candidate.title,
            "url": candidate.url,
            "quote": candidate.quote,
            "summary": cmd.summary,
        }
        evidence = Evidence(
            organization_id=candidate.organization_id,
            exploration_id=candidate.exploration_id,
            idea_id=candidate.idea_id,
            source_type=candidate.source_type,
            title=candidate.title,
            url=candidate.url,
            # 原情報（抜粋）と人間の要約を分けて保存する。AI生成の補助情報は読まない（B-21）
            quote=candidate.quote,
            summary=cmd.summary,
            published_at=candidate.published_at,
            retrieved_at=candidate.retrieved_at,
            content_hash=_sha256(json.dumps(material, ensure_ascii=False, sort_keys=True)),
            metadata_=candidate.metadata_,
            source_key=candidate.source_key,
            snapshot_hash=candidate.snapshot_hash,
            supersedes_evidence_id=updates.id if updates else None,
            classification=cmd.classification.value,
            acquisition_method=AcquisitionMethod.TOOL.value,
            candidate_id=candidate.id,
            tool_call_id=candidate.tool_call_id,
            execution_id=candidate.execution_id,
            created_by_actor_id=actor.id,
        )
        self.session.add(evidence)
        self.session.flush()
        candidate.updates_evidence_id = updates.id if updates else None
        self._decide(actor, candidate, CandidateStatus.ACCEPTED, None, evidence.id)
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
                "classification": evidence.classification,
                "acquisition_method": evidence.acquisition_method,
                "candidate_id": candidate.id,
                "supersedes_evidence_id": evidence.supersedes_evidence_id,
            },
        )
        if updates is not None:
            record_audit(
                self.session,
                organization_id=updates.organization_id,
                entity_type="evidence",
                entity_id=updates.id,
                action="superseded",
                actor_id=actor.id,
                after={"superseded_by_id": evidence.id},
            )
        return evidence

    def _commit(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as exc:  # 同じ版を同時に置き換えた場合など
            self.session.rollback()
            raise InvalidStateError("the candidate was decided concurrently; retry") from exc


# ---------------------------------------------------------------------- 保存期間（R-20）


def delete_expired_tool_data(
    session: Session, output_retention_days: int, snapshot_retention_days: int
) -> tuple[int, int]:
    """Tool の生の出力（90日）と、候補の全文（180日）を消す。メタデータ・抜粋・ハッシュは残す。"""
    if output_retention_days < 1 or snapshot_retention_days < 1:
        raise DomainValidationError("retention days must be at least 1")
    now = utcnow()
    outputs = session.scalars(
        select(ToolCallOutput).where(
            ToolCallOutput.created_at < now - timedelta(days=output_retention_days)
        )
    ).all()
    for output in outputs:
        session.delete(output)
    snapshots = session.scalars(
        select(EvidenceCandidate).where(
            EvidenceCandidate.snapshot.is_not(None),
            EvidenceCandidate.created_at < now - timedelta(days=snapshot_retention_days),
        )
    ).all()
    for candidate in snapshots:
        candidate.snapshot = None
        candidate.snapshot_deleted_at = now
    session.commit()
    return len(outputs), len(snapshots)
