"""AI社員の登録・取得・更新。DB の定義が正本で、implementation_key でコード実装に対応付ける。"""

from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import AgentRegistry
from ai_business_explorer.application.commands import AIEmployeeCreate, AIEmployeeUpdate
from ai_business_explorer.application.common import (
    current_organization_id,
    record_audit,
    require_human,
    snapshot,
)
from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.domain.errors import DomainValidationError, InvalidStateError
from ai_business_explorer.domain.stages import get_stage
from ai_business_explorer.infrastructure.db.models import Actor, AIEmployee
from ai_business_explorer.infrastructure.db.repositories import AIEmployeeRepository
from ai_business_explorer.prompts.loader import prompt_exists

AUDIT_FIELDS = [
    "key",
    "name",
    "role",
    "description",
    "purpose",
    "stage_key",
    "implementation_key",
    "llm_config",
    "allowed_tools",
    "input_format",
    "output_format",
    "prompt_key",
    "prompt_version",
    "status",
    "version",
]


class AIEmployeeService:
    def __init__(self, session: Session, agent_registry: AgentRegistry) -> None:
        self.session = session
        self.employees = AIEmployeeRepository(session)
        self.agent_registry = agent_registry

    def create(self, actor: Actor, cmd: AIEmployeeCreate) -> AIEmployee:
        require_human(actor, "register AI employees")
        organization_id = current_organization_id(self.session)
        if self.employees.get_by_key(organization_id, cmd.key) is not None:
            raise InvalidStateError(f"ai_employee key already exists: {cmd.key}")
        employee = AIEmployee(
            **cmd.model_dump(exclude={"llm_config", "status"}),
            organization_id=organization_id,
            llm_config=cmd.llm_config.model_dump(mode="json"),
            status=cmd.status.value,
            version=1,
        )
        self._validate_and_fill(employee)
        self.employees.add(employee)
        record_audit(
            self.session,
            organization_id=employee.organization_id,
            entity_type="ai_employee",
            entity_id=employee.id,
            action="created",
            actor_id=actor.id,
            after=snapshot(employee, AUDIT_FIELDS),
        )
        self.session.commit()
        return employee

    def get(self, employee_id: UUID) -> AIEmployee:
        return self.employees.get_or_raise(employee_id)

    def list(self, page: PageRequest, stage_key: str | None = None) -> Page[AIEmployee]:
        criteria = [] if stage_key is None else [AIEmployee.stage_key == stage_key]
        return paginate(
            self.session,
            self.employees.select(*criteria),
            sort_column=AIEmployee.created_at,
            id_column=AIEmployee.id,
            page=page,
        )

    def update(self, actor: Actor, employee_id: UUID, cmd: AIEmployeeUpdate) -> AIEmployee:
        require_human(actor, "update AI employees")
        employee = self.employees.get_or_raise(employee_id)
        before = snapshot(employee, AUDIT_FIELDS)
        changes = cmd.model_dump(exclude_unset=True, mode="json")
        if not changes:
            return employee
        for field, value in changes.items():
            setattr(employee, field, value)  # llm_config は dict として格納される
        if cmd.status is not None:
            employee.status = cmd.status.value
        self._validate_and_fill(employee)
        # 定義が変わるたびにバージョンを上げる。
        # 過去の実行は executions のスナップショットで追跡できる。
        employee.version += 1
        record_audit(
            self.session,
            organization_id=employee.organization_id,
            entity_type="ai_employee",
            entity_id=employee.id,
            action="updated",
            actor_id=actor.id,
            before=before,
            after=snapshot(employee, AUDIT_FIELDS),
        )
        self.session.commit()
        return employee

    def _validate_and_fill(self, employee: AIEmployee) -> None:
        stage = get_stage(employee.stage_key)
        if not stage.executable_by_ai:
            raise DomainValidationError(
                f"stage '{stage.key}' is human-only; AI employees cannot be assigned"
            )
        if (employee.prompt_key is None) != (employee.prompt_version is None):
            raise DomainValidationError("prompt_key and prompt_version must be set together")
        if (
            employee.prompt_key
            and employee.prompt_version
            and not prompt_exists(employee.prompt_key, employee.prompt_version)
        ):
            raise DomainValidationError(
                f"prompt not found: {employee.prompt_key}/{employee.prompt_version}"
            )
        if employee.implementation_key is None:
            return
        agent = self.agent_registry.get(employee.implementation_key)
        if agent is None:
            raise DomainValidationError(
                f"unknown implementation_key: {employee.implementation_key}"
            )
        if agent.stage_key != employee.stage_key:
            raise DomainValidationError(
                f"implementation '{agent.implementation_key}' handles stage '{agent.stage_key}', "
                f"not '{employee.stage_key}'"
            )
        if employee.prompt_key is None:
            raise DomainValidationError(
                "an implemented AI employee requires prompt_key/prompt_version"
            )
        if employee.input_format is None:
            employee.input_format = agent.input_model.model_json_schema()
        if employee.output_format is None:
            employee.output_format = agent.output_model.model_json_schema()
