from uuid import UUID

from fastapi import APIRouter, status

from ai_business_explorer.api.v1.deps import AdminDep, AgentRegistryDep, PageDep, SessionDep
from ai_business_explorer.api.v1.schemas import AIEmployeeOut, PageOut
from ai_business_explorer.application.ai_employees import AIEmployeeService
from ai_business_explorer.application.commands import AIEmployeeCreate, AIEmployeeUpdate

router = APIRouter(prefix="/ai-employees", tags=["ai_employees"])


@router.post(
    "",
    response_model=AIEmployeeOut,
    status_code=status.HTTP_201_CREATED,
    summary="AI社員を登録する（admin のみ）",
)
def create_ai_employee(
    body: AIEmployeeCreate, actor: AdminDep, session: SessionDep, registry: AgentRegistryDep
) -> object:
    return AIEmployeeService(session, registry).create(actor, body)


@router.get("", response_model=PageOut[AIEmployeeOut])
def list_ai_employees(
    session: SessionDep, registry: AgentRegistryDep, page: PageDep, stage_key: str | None = None
) -> object:
    return AIEmployeeService(session, registry).list(page, stage_key)


@router.get("/{employee_id}", response_model=AIEmployeeOut)
def get_ai_employee(employee_id: UUID, session: SessionDep, registry: AgentRegistryDep) -> object:
    return AIEmployeeService(session, registry).get(employee_id)


@router.patch(
    "/{employee_id}", response_model=AIEmployeeOut, summary="AI社員を更新する（admin のみ）"
)
def update_ai_employee(
    employee_id: UUID,
    body: AIEmployeeUpdate,
    actor: AdminDep,
    session: SessionDep,
    registry: AgentRegistryDep,
) -> object:
    return AIEmployeeService(session, registry).update(actor, employee_id, body)
