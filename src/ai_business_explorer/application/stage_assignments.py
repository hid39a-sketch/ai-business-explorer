"""ステージへのAI社員の割り当て（primary / secondary）。設定の変更は admin のみ（第2回仕様 1章）。

primary は人間が ai_employee_id を指定しなかったときの実行担当。secondary は人間が
secondary_ai_employee_ids で選べる追加の担当（第2回仕様 7章）。

実装・Prompt（key と version）・モデル（解決後の provider と model）がすべて同じ AI社員を、
primary と secondary にすることはできない（V-08）。割り当ての作成時と、ステージ実行の起動時の
両方で確かめる。
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
from ai_business_explorer.application.costs import ensure_llm_pricing
from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.domain.enums import StageAssignmentRole
from ai_business_explorer.domain.errors import DomainValidationError, InvalidStateError
from ai_business_explorer.domain.stages import get_stage
from ai_business_explorer.infrastructure.db.models import Actor, AIEmployee, StageAssignment
from ai_business_explorer.infrastructure.db.repositories import (
    AIEmployeeRepository,
    StageAssignmentRepository,
)
from ai_business_explorer.llm.factory import resolve_llm_config

AUDIT_FIELDS = ["stage_key", "ai_employee_id", "role"]


def same_configuration(a: AIEmployee, b: AIEmployee, default_provider: str) -> bool:
    """実装・Prompt（key と version の両方）・解決後の（provider, model）がすべて同じか（V-08）。

    モデルは resolve_llm_config で解決してから比べる（省略と既定値の明示は同じに扱い、既定値が
    変わればその時点の解決結果で判定する）。
    """
    return (
        a.implementation_key == b.implementation_key
        and (a.prompt_key, a.prompt_version) == (b.prompt_key, b.prompt_version)
        and resolve_llm_config(a.llm_config, default_provider)
        == resolve_llm_config(b.llm_config, default_provider)
    )


def ensure_different_configuration(
    secondary: AIEmployee, primary: AIEmployee, default_provider: str
) -> None:
    if same_configuration(secondary, primary, default_provider):
        raise DomainValidationError(
            f"secondary ai_employee {secondary.id} has the same implementation, prompt and model "
            f"as the primary ai_employee {primary.id}"
        )


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

    def create(
        self, actor: Actor, cmd: StageAssignmentCreate, default_provider: str
    ) -> StageAssignment:
        require_human(actor, "change stage assignments")
        stage = get_stage(cmd.stage_key)
        if not stage.executable_by_ai:
            raise DomainValidationError(f"stage '{stage.key}' is human-only")
        employee = self.employees.get_or_raise(cmd.ai_employee_id)
        if employee.stage_key != stage.key:
            raise DomainValidationError("ai_employee is not for this stage")
        # 担当の AI社員の解決後の（provider, model）に有効な単価がなければ割り当てない（SC候補-12）
        ensure_llm_pricing(self.session, *resolve_llm_config(employee.llm_config, default_provider))
        self._ensure_not_same_as_counterpart(employee, stage.key, cmd.role, default_provider)
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

    def _ensure_not_same_as_counterpart(
        self,
        employee: AIEmployee,
        stage_key: str,
        role: StageAssignmentRole,
        default_provider: str,
    ) -> None:
        """secondary はそのステージの primary と、primary は既存の secondary と比べる（V-08）。"""
        other_role = (
            StageAssignmentRole.SECONDARY
            if role is StageAssignmentRole.PRIMARY
            else StageAssignmentRole.PRIMARY
        )
        counterparts = self.assignments.list_where(
            StageAssignment.organization_id == current_organization_id(self.session),
            StageAssignment.stage_key == stage_key,
            StageAssignment.role == other_role.value,
        )
        for assignment in counterparts:
            if assignment.ai_employee_id == employee.id:
                continue  # 同じ社員の重複は一意制約で 409（これまでどおり）
            other = self.employees.get_or_raise(assignment.ai_employee_id)
            if role is StageAssignmentRole.PRIMARY:
                ensure_different_configuration(other, employee, default_provider)
            else:
                ensure_different_configuration(employee, other, default_provider)

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
