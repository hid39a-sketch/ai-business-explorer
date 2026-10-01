from uuid import UUID

from fastapi import APIRouter, status

from ai_business_explorer.api.v1.deps import AdminDep, PageDep, SessionDep
from ai_business_explorer.api.v1.schemas import PageOut, StageAssignmentOut
from ai_business_explorer.application.commands import StageAssignmentCreate
from ai_business_explorer.application.stage_assignments import StageAssignmentService

router = APIRouter(prefix="/stage-assignments", tags=["stage_assignments"])


@router.get("", response_model=PageOut[StageAssignmentOut])
def list_stage_assignments(
    session: SessionDep, page: PageDep, stage_key: str | None = None
) -> object:
    return StageAssignmentService(session).list(page, stage_key)


@router.post(
    "",
    response_model=StageAssignmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="ステージにAI社員を割り当てる（admin のみ）",
)
def create_stage_assignment(
    body: StageAssignmentCreate, actor: AdminDep, session: SessionDep
) -> object:
    return StageAssignmentService(session).create(actor, body)


@router.delete(
    "/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="ステージへの割り当てを外す（admin のみ）",
)
def delete_stage_assignment(assignment_id: UUID, actor: AdminDep, session: SessionDep) -> None:
    StageAssignmentService(session).delete(actor, assignment_id)
