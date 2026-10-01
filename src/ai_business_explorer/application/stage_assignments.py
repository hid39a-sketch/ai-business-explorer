"""ステージへのAI社員の割り当て（primary / secondary）。設定の変更は admin のみ（第2回仕様 1章）。

primary は人間が ai_employee_id を指定しなかったときの実行担当。secondary は人間が
secondary_ai_employee_ids で選べる追加の担当（第2回仕様 7章）。
"""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_business_explorer.application.commands import StageAssignmentCreate
from ai_business_explorer.application.common import (
    current_organization_id,
    record_audit,
    require_human,
    snapshot,
)
from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.domain.errors import DomainValidationError, InvalidStateError
from ai_business_explorer.domain.stages import get_stage
from ai_business_explorer.infrastructure.db.models import Actor, StageAssignment
from ai_business_explorer.infrastructure.db.repositories import (
    AIEmployeeRepository,
    StageAssignmentRepository,
)

AUDIT_FIELDS = ["stage_key", "ai_employee_id", "role"]


class StageAssignmentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.assignments = StageAssignmentRepository(session)
        self.employees = AIEmployeeRepository(session)

    def list(self, page: PageRequest, stage_key: str | None = None) -> Page[StageAssignment]:
        criteria = [] if stage_key is None else [StageAssignment.stage_key == stage_key]
        return paginate(
            self.session,
            self.assignments.select(*criteria),
            sort_column=StageAssignment.created_at,
            id_column=StageAssignment.id,
            page=page,
        )

    def create(self, actor: Actor, cmd: StageAssignmentCreate) -> StageAssignment:
        require_human(actor, "change stage assignments")
        stage = get_stage(cmd.stage_key)
        if not stage.executable_by_ai:
            raise DomainValidationError(f"stage '{stage.key}' is human-only")
        employee = self.employees.get_or_raise(cmd.ai_employee_id)
        if employee.stage_key != stage.key:
            raise DomainValidationError("ai_employee is not for this stage")
        assignment = StageAssignment(
            organization_id=current_organization_id(self.session),
            stage_key=stage.key,
            ai_employee_id=employee.id,
            role=cmd.role.value,
        )
        try:
            self.assignments.add(assignment)
        except IntegrityError as exc:  # 同じ社員の重複・primary の重複
            self.session.rollback()
            raise InvalidStateError(
                "the employee is already assigned to this stage, or the stage already has a primary"
            ) from exc
        record_audit(
            self.session,
            organization_id=assignment.organization_id,
            entity_type="stage_assignment",
            entity_id=assignment.id,
            action="created",
            actor_id=actor.id,
            after=snapshot(assignment, AUDIT_FIELDS),
        )
        self.session.commit()
        return assignment

    def delete(self, actor: Actor, assignment_id: UUID) -> None:
        require_human(actor, "change stage assignments")
        assignment = self.assignments.get_or_raise(assignment_id)
        record_audit(
            self.session,
            organization_id=assignment.organization_id,
            entity_type="stage_assignment",
            entity_id=assignment.id,
            action="deleted",
            actor_id=actor.id,
            before=snapshot(assignment, AUDIT_FIELDS),
        )
        self.session.delete(assignment)
        self.session.commit()
