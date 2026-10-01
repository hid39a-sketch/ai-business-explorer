from uuid import UUID

from fastapi import APIRouter, status

from ai_business_explorer.api.v1.deps import MemberDep, SessionDep
from ai_business_explorer.api.v1.schemas import EvidenceOut
from ai_business_explorer.application.commands import EvidenceCreate, EvidenceRetract
from ai_business_explorer.application.evidence import EvidenceService

router = APIRouter(prefix="/evidence", tags=["evidence"])


@router.post(
    "",
    response_model=EvidenceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Evidence を登録する（member 以上の人間のみ。AI 生成情報は登録できない）",
)
def create_evidence(body: EvidenceCreate, actor: MemberDep, session: SessionDep) -> object:
    return EvidenceService(session).create(actor, body)


@router.get("/{evidence_id}", response_model=EvidenceOut)
def get_evidence(evidence_id: UUID, session: SessionDep) -> object:
    return EvidenceService(session).get(evidence_id)


@router.post(
    "/{evidence_id}/retract",
    response_model=EvidenceOut,
    summary="Evidence を撤回する（member 以上の人間のみ）",
)
def retract_evidence(
    evidence_id: UUID, body: EvidenceRetract, actor: MemberDep, session: SessionDep
) -> object:
    return EvidenceService(session).retract(actor, evidence_id, body)
