"""ステージ実行（第1回は同期）。

- 1ステージずつ人間が API から起動する。自動でステージを連鎖させない。
- 再実行・差し戻しは人間のみ。対象ステージ以降の最新試行に superseded_at を記録する
  （履歴は消さない）。
- 失敗しても実行記録が残るよう、記録の確定と AI社員の実行を別トランザクションに分ける。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_business_explorer.agents.base import (
    Agent,
    AgentContext,
    AgentOutputError,
    AnalysisDraft,
    AnalysisView,
    EvidenceView,
    ExplorationView,
    IdeaView,
)
from ai_business_explorer.agents.base import (
    Claim as AgentClaim,
)
from ai_business_explorer.agents.registry import AgentRegistry
from ai_business_explorer.application.commands import (
    ExplorationStageRunCommand,
    IdeaStageRunCommand,
    SendBackCommand,
)
from ai_business_explorer.application.common import (
    detect_code_version,
    record_audit,
    require_human,
    to_jsonable,
    utcnow,
)
from ai_business_explorer.config import Settings
from ai_business_explorer.domain.enums import (
    AdoptionStatus,
    AIEmployeeStatus,
    ErrorType,
    ExplorationStatus,
    OriginType,
    ReviewStatus,
    RunStatus,
    StageRunTrigger,
)
from ai_business_explorer.domain.errors import DomainValidationError, InvalidStateError
from ai_business_explorer.domain.stages import (
    IDEA_GENERATION,
    STAGES,
    StageDefinition,
    StageScope,
    get_stage,
    previous_idea_stage,
    stages_from,
)
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    AIEmployee,
    Analysis,
    Claim,
    ClaimEvidenceLink,
    Evidence,
    Execution,
    Exploration,
    Idea,
    StageRun,
)
from ai_business_explorer.infrastructure.db.repositories import (
    AIEmployeeRepository,
    AnalysisRepository,
    ClaimEvidenceLinkRepository,
    ClaimRepository,
    EvidenceRepository,
    ExecutionRepository,
    ExplorationRepository,
    IdeaRepository,
    StageRunRepository,
)
from ai_business_explorer.llm.base import LLMClient, LLMError, TrackingLLMClient
from ai_business_explorer.llm.factory import build_llm_client, resolve_llm_config
from ai_business_explorer.prompts.loader import load_prompt, prompt_exists
from ai_business_explorer.tools.base import ToolBox, ToolContext, ToolError, ToolRegistry

LLMClientFactory = Callable[[str], LLMClient]

MAX_ERROR_MESSAGE = 2000


@dataclass
class _Plan:
    exploration: Exploration
    idea: Idea | None
    stage: StageDefinition
    trigger: StageRunTrigger
    employee: AIEmployee
    agent: Agent
    research_question: str | None
    rerun_of_id: UUID | None = None
    sent_back_from_id: UUID | None = None
    reason: str | None = None


def classify_error(exc: BaseException) -> ErrorType:
    if isinstance(exc, LLMError):
        return ErrorType.LLM_ERROR
    if isinstance(exc, ToolError):
        return ErrorType.TOOL_ERROR
    if isinstance(exc, AgentOutputError | ValidationError | DomainValidationError):
        return ErrorType.VALIDATION_ERROR
    if isinstance(exc, TimeoutError):
        return ErrorType.TIMEOUT
    if isinstance(exc, SQLAlchemyError):
        return ErrorType.STORAGE_ERROR
    return ErrorType.UNEXPECTED


class StageRunService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        agent_registry: AgentRegistry,
        tool_registry: ToolRegistry,
        llm_client_factory: LLMClientFactory = build_llm_client,
    ) -> None:
        self.session = session
        self.settings = settings
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry
        self.llm_client_factory = llm_client_factory
        self.explorations = ExplorationRepository(session)
        self.ideas = IdeaRepository(session)
        self.employees = AIEmployeeRepository(session)
        self.stage_runs = StageRunRepository(session)
        self.executions = ExecutionRepository(session)
        self.analyses = AnalysisRepository(session)
        self.evidence = EvidenceRepository(session)
        self.claims = ClaimRepository(session)
        self.links = ClaimEvidenceLinkRepository(session)

    # ------------------------------------------------------------------ 公開操作（人間のみ）

    def run_exploration_stage(
        self, actor: Actor, exploration_id: UUID, cmd: ExplorationStageRunCommand
    ) -> StageRun:
        require_human(actor, "run stages")
        exploration = self._active_exploration(exploration_id)
        stage = get_stage(IDEA_GENERATION)
        trigger = self._check_rerun(exploration.id, None, stage, cmd.rerun_of_id)
        employee, agent = self._select_employee(stage, cmd.ai_employee_id)
        plan = _Plan(
            exploration=exploration,
            idea=None,
            stage=stage,
            trigger=trigger,
            employee=employee,
            agent=agent,
            research_question=cmd.research_question,
            rerun_of_id=cmd.rerun_of_id,
        )
        return self._run(actor, plan)

    def run_idea_stage(self, actor: Actor, idea_id: UUID, cmd: IdeaStageRunCommand) -> StageRun:
        require_human(actor, "run stages")
        idea = self.ideas.get_or_raise(idea_id)
        exploration = self._active_exploration(idea.exploration_id)
        stage = self._idea_stage(cmd.stage_key)
        self._check_idea_prerequisites(idea, stage)
        trigger = self._check_rerun(exploration.id, idea.id, stage, cmd.rerun_of_id)
        employee, agent = self._select_employee(stage, cmd.ai_employee_id)
        plan = _Plan(
            exploration=exploration,
            idea=idea,
            stage=stage,
            trigger=trigger,
            employee=employee,
            agent=agent,
            research_question=cmd.research_question,
            rerun_of_id=cmd.rerun_of_id,
        )
        return self._run(actor, plan)

    def send_back(self, actor: Actor, idea_id: UUID, cmd: SendBackCommand) -> StageRun:
        require_human(actor, "send back stages")
        idea = self.ideas.get_or_raise(idea_id)
        exploration = self._active_exploration(idea.exploration_id)
        target = self._idea_stage(cmd.to_stage_key)
        if idea.current_stage_key is None:
            raise InvalidStateError("idea has no completed stage to send back from")
        current = get_stage(idea.current_stage_key)
        if target.order >= current.order:
            raise InvalidStateError(
                f"send-back target '{target.key}' must precede current stage '{current.key}'"
            )
        self._check_idea_prerequisites(idea, target)
        from_run = self.stage_runs.current(exploration.id, idea.id, current.key)
        employee, agent = self._select_employee(target, cmd.ai_employee_id)
        plan = _Plan(
            exploration=exploration,
            idea=idea,
            stage=target,
            trigger=StageRunTrigger.SEND_BACK,
            employee=employee,
            agent=agent,
            research_question=cmd.research_question,
            sent_back_from_id=from_run.id if from_run else None,
            reason=cmd.reason,
        )
        return self._run(actor, plan)

    # ------------------------------------------------------------------ 参照

    def get(self, stage_run_id: UUID) -> StageRun:
        return self.stage_runs.get_or_raise(stage_run_id)

    def executions_for(self, stage_run_id: UUID) -> list[Execution]:
        return list(self.executions.list_where(Execution.stage_run_id == stage_run_id))

    def get_execution(self, execution_id: UUID) -> Execution:
        return self.executions.get_or_raise(execution_id)

    def list_for_exploration(self, exploration_id: UUID) -> list[StageRun]:
        self.explorations.get_or_raise(exploration_id)
        return list(self.stage_runs.list_for(exploration_id, None))

    def list_for_idea(self, idea_id: UUID) -> list[StageRun]:
        idea = self.ideas.get_or_raise(idea_id)
        return list(self.stage_runs.list_for(idea.exploration_id, idea.id))

    # ------------------------------------------------------------------ 事前検証

    def _active_exploration(self, exploration_id: UUID) -> Exploration:
        exploration = self.explorations.get_or_raise(exploration_id)
        if exploration.status != ExplorationStatus.ACTIVE.value:
            raise InvalidStateError("exploration is not active")
        return exploration

    def _idea_stage(self, stage_key: str) -> StageDefinition:
        stage = get_stage(stage_key)
        if stage.scope is not StageScope.IDEA:
            raise DomainValidationError(f"stage '{stage.key}' is not an idea-level stage")
        if not stage.executable_by_ai:
            raise DomainValidationError(
                f"stage '{stage.key}' is human-only and cannot be run by AI"
            )
        return stage

    def _check_idea_prerequisites(self, idea: Idea, stage: StageDefinition) -> None:
        if idea.adoption_status != AdoptionStatus.ADOPTED.value:
            raise InvalidStateError("only adopted ideas can run idea-level stages")
        prev = previous_idea_stage(stage.key)
        if prev is None:
            return
        prev_run = self.stage_runs.current(idea.exploration_id, idea.id, prev.key)
        if prev_run is None or prev_run.status != RunStatus.SUCCEEDED.value:
            raise InvalidStateError(f"previous stage '{prev.key}' has no succeeded run")

    def _check_rerun(
        self,
        exploration_id: UUID,
        idea_id: UUID | None,
        stage: StageDefinition,
        rerun_of_id: UUID | None,
    ) -> StageRunTrigger:
        current = self.stage_runs.current(exploration_id, idea_id, stage.key)
        if rerun_of_id is None:
            if current is not None:
                raise InvalidStateError(
                    f"stage '{stage.key}' already has a run ({current.id}); "
                    "pass rerun_of_id to rerun"
                )
            return StageRunTrigger.INITIAL
        if current is None or current.id != rerun_of_id:
            raise InvalidStateError(
                "rerun_of_id must be the latest (non-superseded) run of the stage"
            )
        return StageRunTrigger.RERUN

    def _select_employee(
        self, stage: StageDefinition, ai_employee_id: UUID | None
    ) -> tuple[AIEmployee, Agent]:
        if ai_employee_id is not None:
            employee = self.employees.get_or_raise(ai_employee_id)
            if employee.stage_key != stage.key:
                raise DomainValidationError("ai_employee is not assigned to this stage")
            candidates = [employee]
        else:
            candidates = list(
                self.employees.list_where(
                    AIEmployee.stage_key == stage.key,
                    AIEmployee.status == AIEmployeeStatus.ACTIVE.value,
                    AIEmployee.implementation_key.is_not(None),
                )
            )
            if not candidates:
                raise InvalidStateError(
                    f"no active AI employee with an implementation for '{stage.key}'"
                )
            if len(candidates) > 1:
                raise InvalidStateError(
                    f"multiple active AI employees for '{stage.key}'; specify ai_employee_id"
                )
        employee = candidates[0]
        if employee.status != AIEmployeeStatus.ACTIVE.value:
            raise InvalidStateError("ai_employee is not active")
        if employee.implementation_key is None:
            raise InvalidStateError("ai_employee has no implementation and cannot be executed")
        agent = self.agent_registry.get(employee.implementation_key)
        if agent is None:
            raise InvalidStateError(f"implementation not found: {employee.implementation_key}")
        if not (employee.prompt_key and employee.prompt_version) or not prompt_exists(
            employee.prompt_key, employee.prompt_version
        ):
            raise InvalidStateError("ai_employee prompt is missing")
        return employee, agent

    # ------------------------------------------------------------------ 実行

    def _run(self, actor: Actor, plan: _Plan) -> StageRun:
        evidence = self._evidence_for(plan)
        prior = self._prior_analyses_for(plan)
        exploration_view = ExplorationView.model_validate(plan.exploration, from_attributes=True)
        idea_view = IdeaView.model_validate(plan.idea, from_attributes=True) if plan.idea else None
        evidence_views = [EvidenceView.model_validate(e, from_attributes=True) for e in evidence]
        prior_views = [AnalysisView.model_validate(a, from_attributes=True) for a in prior]

        stage_run, execution = self._open_records(actor, plan, evidence, prior)
        try:
            provider, model = resolve_llm_config(
                plan.employee.llm_config, self.settings.llm_provider
            )
            llm = TrackingLLMClient(self.llm_client_factory(provider))
            prompt = load_prompt(execution.prompt_key or "", execution.prompt_version or "")
            tools = ToolBox(
                self.tool_registry,
                allowed_tools=list(plan.employee.allowed_tools),
                allowed_side_effects=list(self.settings.tool_allowed_side_effects),
                context=ToolContext(
                    execution_id=execution.id,
                    exploration_id=plan.exploration.id,
                    idea_id=plan.idea.id if plan.idea else None,
                ),
            )
            ctx = AgentContext(
                exploration=exploration_view,
                idea=idea_view,
                evidence=evidence_views,
                prior_analyses=prior_views,
                research_question=plan.research_question,
                llm=llm,
                llm_model=model,
                tools=tools,
                prompt=prompt,
            )
            draft = plan.agent.run(ctx)
            self._validate_draft(draft, plan.stage, {e.id for e in evidence})
            self._persist_success(plan, stage_run, execution, draft, llm, tools)
            self.session.commit()
        except Exception as exc:
            self.session.rollback()
            self._persist_failure(plan, stage_run.id, execution.id, exc)
        self.session.refresh(stage_run)
        return stage_run

    def _open_records(
        self, actor: Actor, plan: _Plan, evidence: list[Evidence], prior: list[Analysis]
    ) -> tuple[StageRun, Execution]:
        idea_id = plan.idea.id if plan.idea else None
        now = utcnow()
        if plan.trigger is not StageRunTrigger.INITIAL:
            self._supersede_from(plan.exploration.id, idea_id, plan.stage, now)
        input_snapshot = {
            "evidence_ids": [str(e.id) for e in evidence],
            "analysis_ids": [str(a.id) for a in prior],
            "research_question": plan.research_question,
        }
        stage_run = StageRun(
            organization_id=plan.exploration.organization_id,
            exploration_id=plan.exploration.id,
            idea_id=idea_id,
            stage_key=plan.stage.key,
            attempt_no=self.stage_runs.next_attempt_no(
                plan.exploration.id, idea_id, plan.stage.key
            ),
            trigger=plan.trigger.value,
            rerun_of_id=plan.rerun_of_id,
            sent_back_from_id=plan.sent_back_from_id,
            reason=plan.reason,
            research_question=plan.research_question,
            triggered_by_actor_id=actor.id,
            triggered_by_actor_type=actor.actor_type,
            status=RunStatus.RUNNING.value,
            input_snapshot=input_snapshot,
            started_at=now,
        )
        self.stage_runs.add(stage_run)
        employee = plan.employee
        provider, model = resolve_llm_config(employee.llm_config, self.settings.llm_provider)
        prompt_hash = None
        if employee.prompt_key and employee.prompt_version:
            prompt_hash = load_prompt(employee.prompt_key, employee.prompt_version).sha256
        execution = Execution(
            organization_id=stage_run.organization_id,
            stage_run_id=stage_run.id,
            ai_employee_id=employee.id,
            idea_id=idea_id,
            ai_employee_version=employee.version,
            ai_employee_snapshot=_employee_snapshot(employee),
            implementation_key=plan.agent.implementation_key,
            prompt_key=employee.prompt_key,
            prompt_version=employee.prompt_version,
            prompt_hash=prompt_hash,
            llm_provider=provider,
            llm_model=model,
            code_version=self.settings.code_version or detect_code_version(),
            status=RunStatus.RUNNING.value,
            input=input_snapshot,
            started_at=now,
        )
        self.executions.add(execution)
        action = "send_back" if plan.trigger is StageRunTrigger.SEND_BACK else "started"
        record_audit(
            self.session,
            organization_id=stage_run.organization_id,
            entity_type="stage_run",
            entity_id=stage_run.id,
            action=action,
            actor_id=actor.id,
            after={
                "stage_key": plan.stage.key,
                "trigger": plan.trigger.value,
                "idea_id": idea_id,
                "rerun_of_id": plan.rerun_of_id,
                "sent_back_from_id": plan.sent_back_from_id,
                "reason": plan.reason,
            },
        )
        try:
            self.session.commit()
        except IntegrityError as exc:  # 同時実行で「最新試行は1つ」の一意制約に違反した場合
            self.session.rollback()
            raise InvalidStateError("a concurrent run for this stage already exists") from exc
        return stage_run, execution

    def _supersede_from(
        self, exploration_id: UUID, idea_id: UUID | None, stage: StageDefinition, now: Any
    ) -> None:
        keys = {s.key for s in stages_from(stage.key)}
        for run in self.stage_runs.current_all(exploration_id, idea_id):
            if run.stage_key in keys:
                run.superseded_at = now
        self.session.flush()

    def _evidence_for(self, plan: _Plan) -> list[Evidence]:
        criteria = [
            Evidence.exploration_id == plan.exploration.id,
            Evidence.retracted_at.is_(None),
        ]
        if plan.idea is None:
            criteria.append(Evidence.idea_id.is_(None))
        else:
            criteria.append(Evidence.idea_id.is_(None) | (Evidence.idea_id == plan.idea.id))
        return list(self.evidence.list_where(*criteria))

    def _prior_analyses_for(self, plan: _Plan) -> list[Analysis]:
        if plan.idea is None:
            return []
        result: list[Analysis] = []
        if plan.idea.origin_type == OriginType.AI.value and plan.idea.origin_analysis_id:
            origin = self.analyses.get(plan.idea.origin_analysis_id)
            if origin is not None:
                result.append(origin)
        earlier = {s.key for s in STAGES if s.order < plan.stage.order}
        runs = [
            r
            for r in self.stage_runs.current_all(plan.exploration.id, plan.idea.id)
            if r.stage_key in earlier and r.status == RunStatus.SUCCEEDED.value
        ]
        if runs:
            stmt = select(Analysis).where(Analysis.stage_run_id.in_([r.id for r in runs]))
            result.extend(self.session.scalars(stmt.order_by(Analysis.created_at)).all())
        return result

    @staticmethod
    def _validate_draft(
        draft: AnalysisDraft, stage: StageDefinition, evidence_ids: set[UUID]
    ) -> None:
        claim_ids = [c.id for c in draft.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise AgentOutputError("claim ids must be unique")
        for claim in draft.claims:
            # 実装が検証を経ずに出力を組み立てた場合に備え、relation の規則（C-09）を再検証する。
            AgentClaim.model_validate(claim.model_dump())
            for ref in claim.evidence_refs:
                if ref.evidence_id not in evidence_ids:
                    raise AgentOutputError(
                        f"claim '{claim.id}' references evidence outside the execution input"
                    )
        if draft.idea_candidates and stage.key != IDEA_GENERATION:
            raise AgentOutputError("only idea_generation may produce idea candidates")

    def _persist_success(
        self,
        plan: _Plan,
        stage_run: StageRun,
        execution: Execution,
        draft: AnalysisDraft,
        llm: TrackingLLMClient,
        tools: ToolBox,
    ) -> None:
        idea_id = plan.idea.id if plan.idea else None
        previous = self.analyses.latest_for_stage(plan.exploration.id, idea_id, plan.stage.key)
        analysis = self.analyses.add(
            Analysis(
                organization_id=plan.exploration.organization_id,
                exploration_id=plan.exploration.id,
                idea_id=idea_id,
                stage_run_id=stage_run.id,
                execution_id=execution.id,
                stage_key=plan.stage.key,
                schema_version=plan.agent.output_schema_version,
                version_no=(previous.version_no + 1) if previous else 1,
                supersedes_id=previous.id if previous else None,
                summary=draft.summary,
                body=to_jsonable({"claims": draft.claims, "data": draft.data}),
                review_status=ReviewStatus.PENDING_REVIEW.value,
            )
        )
        # 主張と根拠は claims / claim_evidence_links が正本。
        # body.claims は生成時点のスナップショットとして別に残す。
        # 重複や矛盾する relation は出力の検証（C-09）で拒否済みなので、ここでは捨てない。
        for ordinal, draft_claim in enumerate(draft.claims):
            claim = self.claims.add(
                Claim(
                    organization_id=analysis.organization_id,
                    analysis_id=analysis.id,
                    claim_key=draft_claim.id,
                    ordinal=ordinal,
                    kind=draft_claim.kind.value,
                    text=draft_claim.text,
                )
            )
            for ref in draft_claim.evidence_refs:
                self.links.add(
                    ClaimEvidenceLink(
                        organization_id=analysis.organization_id,
                        claim_id=claim.id,
                        evidence_id=ref.evidence_id,
                        relation=ref.relation.value,
                    )
                )
        created_ideas: list[Idea] = []
        for candidate in draft.idea_candidates:
            idea = self.ideas.add(
                Idea(
                    organization_id=plan.exploration.organization_id,
                    exploration_id=plan.exploration.id,
                    title=candidate.title,
                    summary=candidate.summary,
                    problem=candidate.problem,
                    origin_type=OriginType.AI.value,
                    origin_analysis_id=analysis.id,
                    adoption_status=AdoptionStatus.CANDIDATE.value,
                )
            )
            created_ideas.append(idea)
            record_audit(
                self.session,
                organization_id=idea.organization_id,
                entity_type="idea",
                entity_id=idea.id,
                action="created",
                execution_id=execution.id,
                after={"title": idea.title, "origin_type": "ai", "adoption_status": "candidate"},
            )
        now = utcnow()
        execution.status = RunStatus.SUCCEEDED.value
        execution.llm_model = llm.last_model or execution.llm_model
        execution.output = to_jsonable(
            {
                "analysis_id": analysis.id,
                "idea_ids": [i.id for i in created_ideas],
                "tool_calls": [c.model_dump() for c in tools.calls],
            }
        )
        execution.usage = {**llm.usage.model_dump(), "llm_calls": llm.call_count}
        execution.finished_at = now
        stage_run.status = RunStatus.SUCCEEDED.value
        stage_run.finished_at = now
        self.session.flush()
        if plan.idea is not None:
            self._recompute_current_stage(plan.idea)
        record_audit(
            self.session,
            organization_id=stage_run.organization_id,
            entity_type="stage_run",
            entity_id=stage_run.id,
            action="succeeded",
            execution_id=execution.id,
        )

    def _persist_failure(
        self, plan: _Plan, stage_run_id: UUID, execution_id: UUID, exc: BaseException
    ) -> None:
        stage_run = self.stage_runs.get_or_raise(stage_run_id)
        execution = self.executions.get_or_raise(execution_id)
        now = utcnow()
        error_type = classify_error(exc)
        execution.status = RunStatus.FAILED.value
        execution.error_type = error_type.value
        execution.error_message = str(exc)[:MAX_ERROR_MESSAGE] or exc.__class__.__name__
        execution.error_detail = {"exception_class": exc.__class__.__name__}
        execution.finished_at = now
        stage_run.status = RunStatus.FAILED.value
        stage_run.finished_at = now
        if plan.idea is not None:
            idea = self.ideas.get(plan.idea.id)
            if idea is not None:
                self._recompute_current_stage(idea)
        record_audit(
            self.session,
            organization_id=stage_run.organization_id,
            entity_type="stage_run",
            entity_id=stage_run.id,
            action="failed",
            execution_id=execution.id,
            after={"error_type": error_type.value},
        )
        self.session.commit()

    def _recompute_current_stage(self, idea: Idea) -> None:
        """current_stage_key = 最新（未 supersede）かつ成功した試行のうち最も後ろのステージ。"""
        succeeded = [
            get_stage(r.stage_key)
            for r in self.stage_runs.current_all(idea.exploration_id, idea.id)
            if r.status == RunStatus.SUCCEEDED.value
        ]
        idea.current_stage_key = max(succeeded, key=lambda s: s.order).key if succeeded else None


def _employee_snapshot(employee: AIEmployee) -> dict[str, Any]:
    fields = [
        "id",
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
    return {f: to_jsonable(getattr(employee, f)) for f in fields}
