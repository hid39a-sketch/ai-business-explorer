from collections.abc import Sequence
from uuid import UUID

from fastapi import APIRouter, status

from ai_business_explorer.api.v1.deps import AdminDep, MemberDep, SessionDep
from ai_business_explorer.api.v1.schemas import (
    EvidenceCreatedOut,
    EvidenceOut,
    EvidenceWarningOut,
    PageOut,
    ProvenanceOut,
)
from ai_business_explorer.application.commands import (
    EvidenceCreate,
    EvidencePurge,
    EvidenceRetract,
)
from ai_business_explorer.application.evidence import EvidenceService
from ai_business_explorer.application.pagination import Page
from ai_business_explorer.domain.evidence import EvidenceState
from ai_business_explorer.infrastructure.db.models import Evidence, ToolCall

router = APIRouter(prefix="/evidence", tags=["evidence"])


def _provenance(evidence: Evidence, call: ToolCall | None) -> ProvenanceOut:
    return ProvenanceOut(
        acquisition_method=evidence.acquisition_method,
        registered_by_actor_id=evidence.created_by_actor_id,
        candidate_id=evidence.candidate_id,
        execution_id=evidence.execution_id,
        tool_call_id=evidence.tool_call_id,
        tool_name=call.tool_name if call else None,
        tool_version=call.tool_version if call else None,
        tool_input=call.input if call else None,
        retrieved_at=evidence.retrieved_at,
    )


def _with_state(evidence: Evidence, state: EvidenceState, call: ToolCall | None) -> EvidenceOut:
    return EvidenceOut.model_validate(evidence).model_copy(
        update={
            "evidence_status": state.status.value,
            "is_retracted": state.is_retracted,
            "is_superseded": state.is_superseded,
            "is_purged": state.is_purged,
            "superseded_by_id": state.superseded_by_id,
            "provenance": _provenance(evidence, call),
        }
    )


def evidence_outs(service: EvidenceService, evidence: Sequence[Evidence]) -> list[EvidenceOut]:
    states = service.states(evidence)
    calls = service.tool_calls_for(evidence)
    return [
        _with_state(e, states[e.id], calls.get(e.tool_call_id) if e.tool_call_id else None)
        for e in evidence
    ]


def evidence_page(service: EvidenceService, page: Page[Evidence]) -> PageOut[EvidenceOut]:
    return PageOut[EvidenceOut](
        items=evidence_outs(service, page.items),
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post(
    "",
    response_model=EvidenceCreatedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Evidence を登録する（member 以上の人間のみ。AI 生成情報は登録できない）",
)
def create_evidence(body: EvidenceCreate, actor: MemberDep, session: SessionDep) -> object:
    service = EvidenceService(session)
    created = service.create(actor, body)
    [out] = evidence_outs(service, [created.evidence])
    return EvidenceCreatedOut(
        **out.model_dump(),
        warnings=[
            EvidenceWarningOut(code="duplicate", evidence_id=evidence_id)
            for evidence_id in created.duplicate_of
        ],
    )


@router.get(
    "/{evidence_id}",
    response_model=EvidenceOut,
    summary="Evidence を取得する（状態に関係なく取得できる。purged は本文が空）",
)
def get_evidence(evidence_id: UUID, session: SessionDep) -> object:
    service = EvidenceService(session)
    return evidence_outs(service, [service.get(evidence_id)])[0]


@router.post(
    "/{evidence_id}/retract",
    response_model=EvidenceOut,
    summary="Evidence を撤回する（member 以上の人間のみ）",
)
def retract_evidence(
    evidence_id: UUID, body: EvidenceRetract, actor: MemberDep, session: SessionDep
) -> object:
    service = EvidenceService(session)
    return evidence_outs(service, [service.retract(actor, evidence_id, body)])[0]


@router.post(
    "/{evidence_id}/purge",
    response_model=EvidenceOut,
    summary="Evidence の本文（quote・summary）を消去する（admin のみ。理由必須）",
)
def purge_evidence(
    evidence_id: UUID, body: EvidencePurge, actor: AdminDep, session: SessionDep
) -> object:
    service = EvidenceService(session)
    return evidence_outs(service, [service.purge(actor, evidence_id, body)])[0]
