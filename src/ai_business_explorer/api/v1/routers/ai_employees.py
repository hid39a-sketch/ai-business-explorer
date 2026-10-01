from uuid import UUID

from fastapi import APIRouter, status

from ai_business_explorer.api.v1.deps import ActorDep, AgentRegistryDep, SessionDep
from ai_business_explorer.api.v1.schemas import AIEmployeeOut
from ai_business_explorer.application.ai_employees import AIEmployeeService
from ai_business_explorer.application.commands import AIEmployeeCreate, AIEmployeeUpdate

router = APIRouter(prefix="/ai-employees", tags=["ai_employees"])


@router.post("", response_model=AIEmployeeOut, status_code=status.HTTP_201_CREATED)
def create_ai_employee(
    body: AIEmployeeCreate, actor: ActorDep, session: SessionDep, registry: AgentRegistryDep
) -> object:
    return AIEmployeeService(session, registry).create(actor, body)


@router.get("", response_model=list[AIEmployeeOut])
def list_ai_employees(
    session: SessionDep, registry: AgentRegistryDep, stage_key: str | None = None
) -> object:
    return AIEmployeeService(session, registry).list(stage_key)


@router.get("/{employee_id}", response_model=AIEmployeeOut)
def get_ai_employee(employee_id: UUID, session: SessionDep, registry: AgentRegistryDep) -> object:
    return AIEmployeeService(session, registry).get(employee_id)


@router.patch("/{employee_id}", response_model=AIEmployeeOut)
def update_ai_employee(
    employee_id: UUID,
    body: AIEmployeeUpdate,
    actor: ActorDep,
    session: SessionDep,
    registry: AgentRegistryDep,
) -> object:
    return AIEmployeeService(session, registry).update(actor, employee_id, body)
