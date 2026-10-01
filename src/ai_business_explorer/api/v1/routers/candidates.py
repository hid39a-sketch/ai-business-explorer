"""Evidence 候補と AI生成の補助情報（第2回仕様 2章）。

閲覧は viewer 以上。承認・却下・一括承認は member 以上の人間のみ。補助情報は読み取りのみ。
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from ai_business_explorer.api.v1.deps import MemberDep, PageDep, SessionDep
from ai_business_explorer.api.v1.routers.evidence import evidence_outs
from ai_business_explorer.api.v1.schemas import (
    CandidateAcceptedOut,
    CandidateAINoteOut,
    CandidateBulkAcceptedOut,
    EvidenceCandidateOut,
    PageOut,
)
from ai_business_explorer.application.candidates import CandidateService
from ai_business_explorer.application.commands import (
    CandidateAccept,
    CandidateBulkAccept,
    CandidateReject,
)
from ai_business_explorer.application.evidence import EvidenceService
from ai_business_explorer.domain.enums import CandidateStatus

router = APIRouter(tags=["evidence_candidates"])


@router.get(
    "/explorations/{exploration_id}/evidence-candidates",
    response_model=PageOut[EvidenceCandidateOut],
    summary="探索案件の Evidence 候補（status で絞り込める。既定はすべて）",
)
def list_candidates(
    exploration_id: UUID,
    session: SessionDep,
    page: PageDep,
    status_: Annotated[list[CandidateStatus] | None, Query(alias="status")] = None,
) -> object:
    return CandidateService(session).list(exploration_id, page, status_)


@router.get("/evidence-candidates/{candidate_id}", response_model=EvidenceCandidateOut)
def get_candidate(candidate_id: UUID, session: SessionDep) -> object:
    return CandidateService(session).get(candidate_id)


@router.get(
    "/evidence-candidates/{candidate_id}/ai-notes",
    response_model=PageOut[CandidateAINoteOut],
    summary="AI生成の補助情報（読み取りのみ。Evidence ではない）",
)
def list_ai_notes(candidate_id: UUID, session: SessionDep, page: PageDep) -> object:
    return CandidateService(session).ai_notes(candidate_id, page)


@router.post(
    "/evidence-candidates/{candidate_id}/accept",
    response_model=CandidateAcceptedOut,
    status_code=status.HTTP_201_CREATED,
    summary="候補を承認して Evidence にする（member 以上の人間のみ。承認者が登録者になる）",
)
def accept_candidate(
    candidate_id: UUID, body: CandidateAccept, actor: MemberDep, session: SessionDep
) -> object:
    service = CandidateService(session)
    evidence = service.accept(actor, candidate_id, body)
    [out] = evidence_outs(EvidenceService(session), [evidence])
    return CandidateAcceptedOut(
        candidate=EvidenceCandidateOut.model_validate(service.get(candidate_id)), evidence=out
    )


@router.post(
    "/evidence-candidates/{candidate_id}/reject",
    response_model=EvidenceCandidateOut,
    summary="候補を却下する（member 以上の人間のみ。理由必須）",
)
def reject_candidate(
    candidate_id: UUID, body: CandidateReject, actor: MemberDep, session: SessionDep
) -> object:
    return CandidateService(session).reject(actor, candidate_id, body)


@router.post(
    "/evidence-candidates/bulk-accept",
    response_model=CandidateBulkAcceptedOut,
    status_code=status.HTTP_201_CREATED,
    summary="候補をまとめて承認する（member 以上。1つでも pending でなければ何も承認しない）",
)
def bulk_accept_candidates(
    body: CandidateBulkAccept, actor: MemberDep, session: SessionDep
) -> object:
    result = CandidateService(session).bulk_accept(actor, body)
    return CandidateBulkAcceptedOut(
        accepted=evidence_outs(EvidenceService(session), result.accepted),
        duplicates=[EvidenceCandidateOut.model_validate(c) for c in result.duplicates],
    )
