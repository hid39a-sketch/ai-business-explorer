"""ステージ実行（第2回は非同期。第2回仕様 7章・9章）。

- 1ステージずつ人間が API から起動する。自動でステージを連鎖させない。自動の再実行もしない。
- 起動すると stage_run と execution を queued で記録して受け付ける（202）。ワーカーが取り出して
  実行する（EXECUTION_MODE=sync では応答の前に同じ処理で実行する）。
- 1回の起動で primary と、人間が選んだ secondary の execution を作る。stage_run の状態は primary の
  execution で決まり、secondary が失敗しても stage_run は失敗にしない。secondary の分析は
  追加の視点として記録するだけで、後続ステージの入力と Idea 候補には使わない。
- Analysis の版の連鎖は「範囲 × ステージ × AI社員」単位。
- 人間は queued と running の実行を取り消せる。取り消し・タイムアウトは、以降の LLM・Tool の
  呼び出しの前に確認して止める（すでに送った呼び出しは止められない）。
- 再実行・差し戻しは人間のみ。対象ステージ以降の最新試行に superseded_at を記録する
  （履歴は消さない）。
- 失敗しても実行記録が残るよう、記録の確定と AI社員の実行を別トランザクションに分ける。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
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
    ReviewView,
)
from ai_business_explorer.agents.base import (
    Claim as AgentClaim,
)
from ai_business_explorer.agents.registry import AgentRegistry
from ai_business_explorer.application.candidates import store_ai_notes
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
from ai_business_explorer.application.costs import (
    BudgetService,
    ExecutionLimits,
    ExecutionMeter,
    find_pricing,
)
from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.application.stage_assignments import ensure_different_configuration
from ai_business_explorer.config import Settings
from ai_business_explorer.domain.enums import (
    ACTIVE_RUN_STATUSES,
    AdoptionStatus,
    AIEmployeeStatus,
    DataClassification,
    ErrorType,
    ExplorationStatus,
    OriginType,
    PricingKind,
    ReviewStatus,
    RunStatus,
    StageAssignmentRole,
    StageRunMode,
    StageRunTrigger,
    highest_classification,
)
from ai_business_explorer.domain.errors import (
    BudgetExceededError,
    DomainValidationError,
    InvalidStateError,
)
from ai_business_explorer.domain.evidence import EvidenceStatus
from ai_business_explorer.domain.execution import ExecutionCancelledError
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
    EvidenceCandidate,
    EvidenceCandidateAINote,
    Execution,
    Exploration,
    HumanReview,
    Idea,
    StageAssignment,
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
    HumanReviewRepository,
    IdeaRepository,
    StageAssignmentRepository,
    StageRunRepository,
)
from ai_business_explorer.llm.base import LLMClient, LLMError, TrackingLLMClient
from ai_business_explorer.llm.factory import build_llm_client, resolve_llm_config
from ai_business_explorer.prompts.loader import load_prompt, prompt_exists
from ai_business_explorer.tools.base import ToolBox, ToolContext, ToolError, ToolRegistry
from ai_business_explorer.tools.config import load_tool_config

LLMClientFactory = Callable[[str], LLMClient]

MAX_ERROR_MESSAGE = 2000
HEARTBEAT_LOST_MESSAGE = "worker heartbeat lost"


@dataclass
class _Member:
    employee: AIEmployee
    agent: Agent
    role: StageAssignmentRole


@dataclass
class _Plan:
    exploration: Exploration
    idea: Idea | None
    stage: StageDefinition
    trigger: StageRunTrigger
    members: list[_Member]  # primary が先頭
    research_question: str | None
    mode: StageRunMode = StageRunMode.ANALYZE
    rerun_of_id: UUID | None = None
    sent_back_from_id: UUID | None = None
    reason: str | None = None
    # AI社員ごとの1実行あたりの費用上限と通貨（起動時の予算の確認で決める）
    cost_limits: dict[UUID, tuple[Decimal, str]] = field(default_factory=dict)


def classify_error(exc: BaseException) -> ErrorType:
    if isinstance(exc, BudgetExceededError):
        return ErrorType.BUDGET_EXCEEDED
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


class _RunGuard:
    """LLM・Tool を呼ぶ前に、取り消しとステージ実行全体のタイムアウトを確認する。"""

    def __init__(self, session: Session, stage_run_id: UUID, deadline: datetime) -> None:
        self._session = session
        self._stage_run_id = stage_run_id
        self._deadline = deadline

    def check(self) -> None:
        if utcnow() > self._deadline:
            raise TimeoutError("stage run exceeded its time limit")
        status = self._session.execute(
            select(StageRun.status).where(StageRun.id == self._stage_run_id)
        ).scalar_one()
        if status == RunStatus.CANCELLED.value:
            raise ExecutionCancelledError("stage run was cancelled")
        if status != RunStatus.RUNNING.value:  # heartbeat 途絶として failed にされた等
            raise ExecutionCancelledError(f"stage run is {status}")


class StageRunService:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        agent_registry: AgentRegistry,
        tool_registry: ToolRegistry,
        llm_client_factory: LLMClientFactory | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.agent_registry = agent_registry
        self.tool_registry = tool_registry
        self.llm_client_factory: LLMClientFactory = llm_client_factory or (
            lambda provider: build_llm_client(provider, settings)
        )
        self.explorations = ExplorationRepository(session)
        self.ideas = IdeaRepository(session)
        self.employees = AIEmployeeRepository(session)
        self.assignments = StageAssignmentRepository(session)
        self.stage_runs = StageRunRepository(session)
        self.executions = ExecutionRepository(session)
        self.analyses = AnalysisRepository(session)
        self.evidence = EvidenceRepository(session)
        self.claims = ClaimRepository(session)
        self.links = ClaimEvidenceLinkRepository(session)
        self.reviews = HumanReviewRepository(session)

    # ------------------------------------------------------------------ 公開操作（人間のみ）

    def run_exploration_stage(
        self, actor: Actor, exploration_id: UUID, cmd: ExplorationStageRunCommand
    ) -> StageRun:
        require_human(actor, "run stages")
        exploration = self._active_exploration(exploration_id)
        stage = get_stage(IDEA_GENERATION)
        trigger = self._check_trigger(exploration.id, None, stage, cmd)
        plan = _Plan(
            exploration=exploration,
            idea=None,
            stage=stage,
            trigger=trigger,
            members=self._select_members(
                exploration, stage, cmd.ai_employee_id, cmd.secondary_ai_employee_ids
            ),
            research_question=cmd.research_question,
            mode=cmd.mode,
            rerun_of_id=cmd.rerun_of_id,
        )
        return self._start(actor, plan)

    def run_idea_stage(self, actor: Actor, idea_id: UUID, cmd: IdeaStageRunCommand) -> StageRun:
        require_human(actor, "run stages")
        idea = self.ideas.get_or_raise(idea_id)
        exploration = self._active_exploration(idea.exploration_id)
        stage = self._idea_stage(cmd.stage_key)
        self._check_idea_prerequisites(idea, stage)
        trigger = self._check_trigger(exploration.id, idea.id, stage, cmd)
        plan = _Plan(
            exploration=exploration,
            idea=idea,
            stage=stage,
            trigger=trigger,
            members=self._select_members(
                exploration, stage, cmd.ai_employee_id, cmd.secondary_ai_employee_ids
            ),
            research_question=cmd.research_question,
            mode=cmd.mode,
            rerun_of_id=cmd.rerun_of_id,
        )
        return self._start(actor, plan)

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
        self._check_no_active_run_from(exploration.id, idea.id, target)
        from_run = self.stage_runs.current(exploration.id, idea.id, current.key)
        plan = _Plan(
            exploration=exploration,
            idea=idea,
            stage=target,
            trigger=StageRunTrigger.SEND_BACK,
            members=self._select_members(
                exploration, target, cmd.ai_employee_id, cmd.secondary_ai_employee_ids
            ),
            research_question=cmd.research_question,
            sent_back_from_id=from_run.id if from_run else None,
            reason=cmd.reason,
        )
        return self._start(actor, plan)

    def cancel(self, actor: Actor, stage_run_id: UUID) -> StageRun:
        """queued / running の実行を取り消す（人間のみ）。自動の再実行はしない。"""
        require_human(actor, "cancel stage runs")
        stage_run = self.stage_runs.get_or_raise(stage_run_id)
        # ワーカーの確定処理と競合しないよう、行をロックしてから状態を見る
        self.session.refresh(stage_run, with_for_update=True)
        if stage_run.status not in ACTIVE_RUN_STATUSES:
            raise InvalidStateError(f"stage run is '{stage_run.status}' and cannot be cancelled")
        now = utcnow()
        before = stage_run.status
        stage_run.status = RunStatus.CANCELLED.value
        stage_run.finished_at = now
        for execution in self.executions_for(stage_run.id):
            if execution.status in ACTIVE_RUN_STATUSES:
                execution.status = RunStatus.CANCELLED.value
                execution.finished_at = now
        self._recompute_idea_stage(stage_run)
        record_audit(
            self.session,
            organization_id=stage_run.organization_id,
            entity_type="stage_run",
            entity_id=stage_run.id,
            action="cancelled",
            actor_id=actor.id,
            before={"status": before},
            after={"status": RunStatus.CANCELLED.value},
        )
        self.session.commit()
        return stage_run

    # ------------------------------------------------------------------ 参照

    def get(self, stage_run_id: UUID) -> StageRun:
        return self.stage_runs.get_or_raise(stage_run_id)

    def executions_for(self, stage_run_id: UUID) -> list[Execution]:
        """primary を先頭に、作成順で返す。"""
        executions = self.executions.list_where(Execution.stage_run_id == stage_run_id)
        return sorted(
            executions, key=lambda e: e.assignment_role != StageAssignmentRole.PRIMARY.value
        )

    def get_execution(self, execution_id: UUID) -> Execution:
        return self.executions.get_or_raise(execution_id)

    def list_for_exploration(self, exploration_id: UUID, page: PageRequest) -> Page[StageRun]:
        self.explorations.get_or_raise(exploration_id)
        return self._page(
            page, StageRun.exploration_id == exploration_id, StageRun.idea_id.is_(None)
        )

    def list_for_idea(self, idea_id: UUID, page: PageRequest) -> Page[StageRun]:
        idea = self.ideas.get_or_raise(idea_id)
        return self._page(page, StageRun.idea_id == idea.id)

    def _page(self, page: PageRequest, *criteria: Any) -> Page[StageRun]:
        return paginate(
            self.session,
            self.stage_runs.select(*criteria),
            sort_column=StageRun.started_at,
            id_column=StageRun.id,
            page=page,
        )

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

    def _check_trigger(
        self,
        exploration_id: UUID,
        idea_id: UUID | None,
        stage: StageDefinition,
        cmd: ExplorationStageRunCommand,
    ) -> StageRunTrigger:
        if cmd.mode is StageRunMode.COLLECT_ONLY:
            # collect_only は「最新の試行」に数えないので、再実行の指定はない（毎回新しく集める）
            if cmd.rerun_of_id is not None:
                raise DomainValidationError("collect_only runs cannot have rerun_of_id")
            return StageRunTrigger.INITIAL
        return self._check_rerun(exploration_id, idea_id, stage, cmd.rerun_of_id)

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
        self._check_no_active_run_from(exploration_id, idea_id, stage)
        return StageRunTrigger.RERUN

    def _check_no_active_run_from(
        self, exploration_id: UUID, idea_id: UUID | None, stage: StageDefinition
    ) -> None:
        """再実行・差し戻しで置き換える試行に、まだ終わっていないものがあれば受け付けない。"""
        keys = {s.key for s in stages_from(stage.key)}
        for run in self.stage_runs.current_all(exploration_id, idea_id):
            if run.stage_key in keys and run.status in ACTIVE_RUN_STATUSES:
                raise InvalidStateError(
                    f"stage '{run.stage_key}' has a {run.status} run ({run.id}); "
                    "wait for it to finish or cancel it first"
                )

    def _select_members(
        self,
        exploration: Exploration,
        stage: StageDefinition,
        ai_employee_id: UUID | None,
        secondary_ids: Sequence[UUID],
    ) -> list[_Member]:
        primary = self._primary_employee(exploration, stage, ai_employee_id)
        members = [_Member(primary, self._agent_for(primary), StageAssignmentRole.PRIMARY)]
        if len(set(secondary_ids)) != len(secondary_ids):
            raise DomainValidationError("secondary_ai_employee_ids must be unique")
        assigned = {
            a.ai_employee_id
            for a in self.assignments.list_where(
                StageAssignment.organization_id == exploration.organization_id,
                StageAssignment.stage_key == stage.key,
                StageAssignment.role == StageAssignmentRole.SECONDARY.value,
            )
        }
        for employee_id in secondary_ids:
            if employee_id == primary.id:
                raise DomainValidationError("the primary AI employee cannot also be secondary")
            if employee_id not in assigned:
                raise DomainValidationError(
                    f"ai_employee {employee_id} is not assigned as secondary to '{stage.key}'"
                )
            employee = self.employees.get_or_raise(employee_id)
            # 一緒に動く primary と同じ構成の secondary は使えない（V-08。割り当て時に加えて、
            # primary の差し替えや設定の変更があっても、起動の直前に必ず確かめる）
            ensure_different_configuration(employee, primary, self.settings.llm_provider)
            members.append(
                _Member(employee, self._agent_for(employee), StageAssignmentRole.SECONDARY)
            )
        return members

    def _primary_employee(
        self, exploration: Exploration, stage: StageDefinition, ai_employee_id: UUID | None
    ) -> AIEmployee:
        """人間の指定 → 担当（primary）の割り当て → 有効な社員が1人だけならその社員、の順。"""
        if ai_employee_id is not None:
            employee = self.employees.get_or_raise(ai_employee_id)
            if employee.stage_key != stage.key:
                raise DomainValidationError("ai_employee is not assigned to this stage")
            return employee
        primary = self.assignments.list_where(
            StageAssignment.organization_id == exploration.organization_id,
            StageAssignment.stage_key == stage.key,
            StageAssignment.role == StageAssignmentRole.PRIMARY.value,
        )
        if primary:
            return self.employees.get_or_raise(primary[0].ai_employee_id)
        candidates = list(
            self.employees.list_where(
                AIEmployee.organization_id == exploration.organization_id,
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
                f"multiple active AI employees for '{stage.key}' and no primary; "
                "specify ai_employee_id"
            )
        return candidates[0]

    def _agent_for(self, employee: AIEmployee) -> Agent:
        if employee.status != AIEmployeeStatus.ACTIVE.value:
            raise InvalidStateError(f"ai_employee {employee.id} is not active")
        if employee.implementation_key is None:
            raise InvalidStateError(f"ai_employee {employee.id} has no implementation")
        agent = self.agent_registry.get(employee.implementation_key)
        if agent is None:
            raise InvalidStateError(f"implementation not found: {employee.implementation_key}")
        if not (employee.prompt_key and employee.prompt_version) or not prompt_exists(
            employee.prompt_key, employee.prompt_version
        ):
            raise InvalidStateError(f"ai_employee {employee.id} prompt is missing")
        try:
            # Prompt の版に対応する出力契約がなければ起動しない（17章）
            agent.contract_for(employee.prompt_version)
        except DomainValidationError as exc:
            raise InvalidStateError(str(exc)) from exc
        return agent

    # ------------------------------------------------------------------ 受付

    def _check_classification(self, plan: _Plan) -> None:
        """入力の最も高い分類が、担当AI社員の LLM プロバイダーの送信上限を超えたら起動しない。

        第2回仕様 11章：409 で拒否し、実行記録も作らない。入力は探索案件（Idea はこれに従う）・
        Evidence（active）・前段の分析。
        """
        highest = self._input_classification(
            plan.exploration,
            self._evidence_for(plan.exploration, plan.idea),
            self._prior_analyses_for(plan.exploration, plan.idea, plan.stage),
        )
        for member in plan.members:
            provider, _ = resolve_llm_config(member.employee.llm_config, self.settings.llm_provider)
            limit = self.settings.llm_send_limit(provider)
            if highest.exceeds(limit):
                raise InvalidStateError(
                    f"input classification '{highest.value}' exceeds the send limit "
                    f"'{limit.value}' of LLM provider '{provider}' "
                    f"(ai_employee {member.employee.id})"
                )

    @staticmethod
    def _input_classification(
        exploration: Exploration, evidence: Sequence[Evidence], prior: Sequence[Analysis]
    ) -> DataClassification:
        return highest_classification(
            [exploration.classification]
            + [e.classification for e in evidence]
            + [a.classification for a in prior]
        )

    def _check_budget(self, plan: _Plan) -> None:
        """起動時の予算の確認（第2回仕様 10章）。足りなければ 409（budget_exceeded）。

        各 AI社員の LLM に単価があること、単価の通貨が予算の通貨と同じことも確認する
        （単価が分からない・通貨が違うと、費用を予算に計上できず上限を守れないため）。
        """
        now = utcnow()
        currencies: set[str] = set()
        for member in plan.members:
            employee = member.employee
            provider, model = resolve_llm_config(employee.llm_config, self.settings.llm_provider)
            pricing = find_pricing(self.session, PricingKind.LLM, provider, model, now)
            if pricing is None:
                raise InvalidStateError(f"no pricing for LLM {provider}/{model}")
            limits = ExecutionLimits.from_llm_config(employee.llm_config, self.settings)
            plan.cost_limits[employee.id] = (limits.max_cost, pricing.currency)
            currencies.add(pricing.currency)
        if len(currencies) != 1:
            raise InvalidStateError(f"AI employees use different currencies: {sorted(currencies)}")
        needed = sum((limit for limit, _ in plan.cost_limits.values()), Decimal(0))
        BudgetService(self.session, self.settings).check_launch(
            plan.exploration.organization_id, plan.exploration.id, needed, currencies.pop()
        )

    def _start(self, actor: Actor, plan: _Plan) -> StageRun:
        self._check_classification(plan)
        self._check_budget(plan)
        stage_run = self._queue(actor, plan)
        if self.settings.execution_mode == "sync":
            self.execute(stage_run.id, worker_id="sync")
        self.session.refresh(stage_run)
        return stage_run

    def _queue(self, actor: Actor, plan: _Plan) -> StageRun:
        idea_id = plan.idea.id if plan.idea else None
        now = utcnow()
        if plan.trigger is not StageRunTrigger.INITIAL:
            self._supersede_from(plan.exploration.id, idea_id, plan.stage, now)
        stage_run = StageRun(
            organization_id=plan.exploration.organization_id,
            exploration_id=plan.exploration.id,
            idea_id=idea_id,
            stage_key=plan.stage.key,
            attempt_no=self.stage_runs.next_attempt_no(
                plan.exploration.id, idea_id, plan.stage.key
            ),
            trigger=plan.trigger.value,
            mode=plan.mode.value,
            rerun_of_id=plan.rerun_of_id,
            sent_back_from_id=plan.sent_back_from_id,
            reason=plan.reason,
            research_question=plan.research_question,
            triggered_by_actor_id=actor.id,
            triggered_by_actor_type=actor.actor_type,
            status=RunStatus.QUEUED.value,
            input_snapshot={"research_question": plan.research_question},
            started_at=now,
        )
        self.stage_runs.add(stage_run)
        for member in plan.members:
            employee = member.employee
            provider, model = resolve_llm_config(employee.llm_config, self.settings.llm_provider)
            prompt_hash = None
            if employee.prompt_key and employee.prompt_version:
                prompt_hash = load_prompt(employee.prompt_key, employee.prompt_version).sha256
            self.executions.add(
                Execution(
                    organization_id=stage_run.organization_id,
                    stage_run_id=stage_run.id,
                    ai_employee_id=employee.id,
                    assignment_role=member.role.value,
                    idea_id=idea_id,
                    ai_employee_version=employee.version,
                    ai_employee_snapshot=_employee_snapshot(employee),
                    implementation_key=member.agent.implementation_key,
                    prompt_key=employee.prompt_key,
                    prompt_version=employee.prompt_version,
                    prompt_hash=prompt_hash,
                    llm_provider=provider,
                    llm_model=model,
                    code_version=self.settings.code_version or detect_code_version(),
                    status=RunStatus.QUEUED.value,
                    input={},
                    cost_amount=Decimal(0),
                    cost_limit=plan.cost_limits[employee.id][0],
                    cost_currency=plan.cost_limits[employee.id][1],
                )
            )
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
                "mode": plan.mode.value,
                "idea_id": idea_id,
                "rerun_of_id": plan.rerun_of_id,
                "sent_back_from_id": plan.sent_back_from_id,
                "reason": plan.reason,
                "ai_employee_ids": [m.employee.id for m in plan.members],
            },
        )
        try:
            self.session.commit()
        except IntegrityError as exc:  # 同時実行で「最新試行は1つ」の一意制約に違反した場合
            self.session.rollback()
            raise InvalidStateError("a concurrent run for this stage already exists") from exc
        return stage_run

    def _supersede_from(
        self, exploration_id: UUID, idea_id: UUID | None, stage: StageDefinition, now: Any
    ) -> None:
        keys = {s.key for s in stages_from(stage.key)}
        for run in self.stage_runs.current_all(exploration_id, idea_id):
            if run.stage_key in keys:
                run.superseded_at = now
        self.session.flush()

    # ------------------------------------------------------------------ 実行（ワーカー）

    def execute(self, stage_run_id: UUID, worker_id: str) -> None:
        """queued の stage_run を実行する。primary を先に、続いて secondary を実行する。"""
        stage_run = self.stage_runs.get_or_raise(stage_run_id)
        self.session.refresh(stage_run, with_for_update=True)
        if stage_run.status != RunStatus.QUEUED.value:
            self.session.rollback()
            return
        now = utcnow()
        stage_run.status = RunStatus.RUNNING.value
        stage_run.claimed_at = now
        stage_run.heartbeat_at = now
        stage_run.worker_id = worker_id
        exploration = self.explorations.get_or_raise(stage_run.exploration_id)
        idea = self.ideas.get(stage_run.idea_id) if stage_run.idea_id else None
        stage = get_stage(stage_run.stage_key)
        evidence = self._evidence_for(exploration, idea)
        prior = self._prior_analyses_for(exploration, idea, stage)
        # 前段の分析は、この時点のレビューの状態と分析全体の最新のレビューを付けて渡す（V-07）。
        # 渡した内容は input_snapshot に固定し、後でレビューが変わっても書き換えない
        reviews = self.reviews.latest_for_analyses([a.id for a in prior])
        prior_views = [_analysis_view(a, reviews.get(a.id)) for a in prior]
        stage_run.input_snapshot = {
            "evidence_ids": [str(e.id) for e in evidence],
            # 渡した Evidence と、その時点の状態（E-01。入力は active だけ）
            "evidence": [
                {"id": str(e.id), "status": EvidenceStatus.ACTIVE.value} for e in evidence
            ],
            "analysis_ids": [str(a.id) for a in prior],
            # 渡した分析と、その時点のレビューの状態・最新のレビュー（レビューは追記のみ）
            "analyses": [
                {
                    "id": str(a.id),
                    "review_status": a.review_status,
                    "review_id": str(reviews[a.id].id) if a.id in reviews else None,
                    "review_decision": reviews[a.id].decision if a.id in reviews else None,
                }
                for a in prior
            ],
            "research_question": stage_run.research_question,
            # 入力の最も高い分類（第2回仕様 11章）。分析の分類になる
            "classification": self._input_classification(exploration, evidence, prior).value,
        }
        self.session.commit()

        deadline = now + timedelta(seconds=self.settings.stage_run_timeout_seconds)
        primary_status = RunStatus.FAILED
        for execution in self.executions_for(stage_run.id):
            status = self._execute_one(
                stage_run, execution, exploration, idea, stage, evidence, prior_views, deadline
            )
            if execution.assignment_role == StageAssignmentRole.PRIMARY.value:
                primary_status = status
        self._finish(stage_run.id, primary_status)

    def _execute_one(
        self,
        stage_run: StageRun,
        execution: Execution,
        exploration: Exploration,
        idea: Idea | None,
        stage: StageDefinition,
        evidence: list[Evidence],
        prior: list[AnalysisView],
        deadline: datetime,
    ) -> RunStatus:
        execution_id = execution.id
        self.session.refresh(execution)
        if execution.status != RunStatus.QUEUED.value:  # 取り消し済み
            return RunStatus(execution.status)
        guard = _RunGuard(self.session, stage_run.id, deadline)
        try:
            guard.check()
        except (ExecutionCancelledError, TimeoutError) as exc:
            return self._persist_failure(execution_id, exc)
        employee = self.employees.get_or_raise(execution.ai_employee_id)
        execution.status = RunStatus.RUNNING.value
        execution.started_at = utcnow()
        execution.input = stage_run.input_snapshot
        self.session.commit()
        try:
            # 受付の後に分類が上がった場合に備え、実行の直前にも送信上限を確認する
            classification = DataClassification(stage_run.input_snapshot["classification"])
            limit = self.settings.llm_send_limit(execution.llm_provider or "")
            if classification.exceeds(limit):
                raise DomainValidationError(
                    f"input classification '{classification.value}' exceeds the send limit "
                    f"'{limit.value}' of LLM provider '{execution.llm_provider}'"
                )
            agent = self._agent_for(employee)
            _, model = resolve_llm_config(employee.llm_config, self.settings.llm_provider)
            # 組織ごとの Tool 設定（外部 Tool の有効化・ドメインの許可リスト。第2回仕様 13章）
            tool_config = load_tool_config(self.settings.tool_config_path)
            allowed_domains, blocked_domains = tool_config.web_fetch_domains(
                execution.organization_id
            )
            # 費用と呼び出しの記録・上限（第2回仕様 10・12章）。取り消しの確認の後に計測する
            meter = ExecutionMeter(
                self.session, self.settings, execution, stage_run.exploration_id, classification
            )
            llm = TrackingLLMClient(
                meter.wrap(
                    self.llm_client_factory(execution.llm_provider or self.settings.llm_provider)
                ),
                guard=guard,
                call_timeout_seconds=self.settings.llm_call_timeout_seconds,
            )
            prompt = load_prompt(execution.prompt_key or "", execution.prompt_version or "")
            tools = ToolBox(
                self.tool_registry,
                allowed_tools=list(employee.allowed_tools),
                allowed_side_effects=list(self.settings.tool_allowed_side_effects),
                context=ToolContext(
                    execution_id=execution.id,
                    exploration_id=exploration.id,
                    idea_id=idea.id if idea else None,
                    organization_id=execution.organization_id,
                    allowed_domains=allowed_domains,
                    blocked_domains=blocked_domains,
                ),
                guard=guard,
                recorder=meter,
                enabled_external_tools=tool_config.enabled_tools(execution.organization_id),
            )
            ctx = AgentContext(
                exploration=ExplorationView.model_validate(exploration, from_attributes=True),
                idea=IdeaView.model_validate(idea, from_attributes=True) if idea else None,
                evidence=[EvidenceView.model_validate(e, from_attributes=True) for e in evidence],
                prior_analyses=prior,
                research_question=stage_run.research_question,
                llm=llm,
                llm_model=model,
                tools=tools,
                prompt=prompt,
                mode=stage_run.mode,
            )
            draft = agent.run(ctx)
            self._validate_draft(draft, stage, {e.id for e in evidence})
            self._persist_success(stage_run, execution, employee, agent, draft, llm, tools)
            # 確定の直前に取り消されていないことを確認する（取り消し済みなら保存しない）
            locked_status = self.session.execute(
                select(StageRun.status).where(StageRun.id == stage_run.id).with_for_update()
            ).scalar_one()
            if locked_status != RunStatus.RUNNING.value:
                raise ExecutionCancelledError(f"stage run is {locked_status}")
            self.session.commit()
            return RunStatus.SUCCEEDED
        except Exception as exc:
            self.session.rollback()
            return self._persist_failure(execution_id, exc)

    def _evidence_for(self, exploration: Exploration, idea: Idea | None) -> list[Evidence]:
        # 通常の新規Analysisに渡すのは active の Evidence だけ（E-01）。
        # superseded（更新版がある）・retracted・purged は渡さない。
        criteria = [
            Evidence.exploration_id == exploration.id,
            self.evidence.status_criteria([EvidenceStatus.ACTIVE]),
        ]
        if idea is None:
            criteria.append(Evidence.idea_id.is_(None))
        else:
            criteria.append(Evidence.idea_id.is_(None) | (Evidence.idea_id == idea.id))
        return list(self.evidence.list_where(*criteria))

    def _prior_analyses_for(
        self, exploration: Exploration, idea: Idea | None, stage: StageDefinition
    ) -> list[Analysis]:
        """前段の成功した試行の primary の分析（secondary は後続の入力に使わない。7章）。"""
        if idea is None:
            return []
        result: list[Analysis] = []
        if idea.origin_type == OriginType.AI.value and idea.origin_analysis_id:
            origin = self.analyses.get(idea.origin_analysis_id)
            if origin is not None:
                result.append(origin)
        earlier = {s.key for s in STAGES if s.order < stage.order}
        runs = [
            r
            for r in self.stage_runs.current_all(exploration.id, idea.id)
            if r.stage_key in earlier and r.status == RunStatus.SUCCEEDED.value
        ]
        if runs:
            stmt = (
                select(Analysis)
                .join(Execution, Execution.id == Analysis.execution_id)
                .where(
                    Analysis.stage_run_id.in_([r.id for r in runs]),
                    Execution.assignment_role == StageAssignmentRole.PRIMARY.value,
                )
            )
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
        stage_run: StageRun,
        execution: Execution,
        employee: AIEmployee,
        agent: Agent,
        draft: AnalysisDraft,
        llm: TrackingLLMClient,
        tools: ToolBox,
    ) -> None:
        execution.llm_model = llm.last_model or execution.llm_model
        # AI生成の補助情報は候補とは別に保存する（この実行が集めた候補だけを参照できる。B-21）
        notes = store_ai_notes(
            self.session, execution, [(n.candidate_id, n.note) for n in draft.candidate_notes]
        )
        candidate_ids = self.session.scalars(
            select(EvidenceCandidate.id).where(EvidenceCandidate.execution_id == execution.id)
        ).all()
        if stage_run.mode == StageRunMode.COLLECT_ONLY.value:
            # 収集のみ：分析・主張・Idea 候補は作らない（第2回仕様 2章・E-02）
            self._complete_execution(execution, llm, tools, None, [], candidate_ids, notes)
            return
        previous = self.analyses.latest_for_stage(
            stage_run.exploration_id, stage_run.idea_id, stage_run.stage_key, employee.id
        )
        analysis = self.analyses.add(
            Analysis(
                organization_id=stage_run.organization_id,
                exploration_id=stage_run.exploration_id,
                idea_id=stage_run.idea_id,
                stage_run_id=stage_run.id,
                execution_id=execution.id,
                ai_employee_id=employee.id,
                stage_key=stage_run.stage_key,
                # 出力契約の版は、この実行の Prompt の版から決まる（17章）
                schema_version=agent.contract_for(execution.prompt_version or "").schema_version,
                version_no=(previous.version_no + 1) if previous else 1,
                supersedes_id=previous.id if previous else None,
                summary=draft.summary,
                body=to_jsonable({"claims": draft.claims, "data": draft.data}),
                review_status=ReviewStatus.PENDING_REVIEW.value,
                classification=stage_run.input_snapshot["classification"],
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
        # Idea 候補は primary の出力だけから作る（secondary は追加の視点の記録だけ）
        if execution.assignment_role == StageAssignmentRole.PRIMARY.value:
            for candidate in draft.idea_candidates:
                idea = self.ideas.add(
                    Idea(
                        organization_id=stage_run.organization_id,
                        exploration_id=stage_run.exploration_id,
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
                    after={
                        "title": idea.title,
                        "origin_type": "ai",
                        "adoption_status": "candidate",
                    },
                )
        self._complete_execution(
            execution,
            llm,
            tools,
            analysis.id,
            [i.id for i in created_ideas],
            candidate_ids,
            notes,
        )

    def _complete_execution(
        self,
        execution: Execution,
        llm: TrackingLLMClient,
        tools: ToolBox,
        analysis_id: UUID | None,
        idea_ids: Sequence[UUID],
        candidate_ids: Sequence[UUID],
        notes: Sequence[EvidenceCandidateAINote],
    ) -> None:
        execution.status = RunStatus.SUCCEEDED.value
        execution.output = to_jsonable(
            {
                "analysis_id": analysis_id,
                "idea_ids": list(idea_ids),
                "candidate_ids": list(candidate_ids),
                "ai_note_ids": [n.id for n in notes],
                "tool_calls": [c.model_dump() for c in tools.calls],
            }
        )
        execution.usage = {**llm.usage.model_dump(), "llm_calls": llm.call_count}
        execution.finished_at = utcnow()
        self.session.flush()

    def _persist_failure(self, execution_id: UUID, exc: BaseException) -> RunStatus:
        execution = self.executions.get_or_raise(execution_id)
        self.session.refresh(execution, with_for_update=True)
        if execution.status not in ACTIVE_RUN_STATUSES:
            # 取り消し・heartbeat 途絶ですでに確定した状態は上書きしない
            self.session.commit()
            return RunStatus(execution.status)
        now = utcnow()
        if isinstance(exc, ExecutionCancelledError):
            status = RunStatus.CANCELLED
        else:
            status = RunStatus.FAILED
            error_type = classify_error(exc)
            execution.error_type = error_type.value
            execution.error_message = str(exc)[:MAX_ERROR_MESSAGE] or exc.__class__.__name__
            execution.error_detail = {"exception_class": exc.__class__.__name__}
        execution.status = status.value
        execution.finished_at = now
        self.session.commit()
        return status

    def _finish(self, stage_run_id: UUID, primary_status: RunStatus) -> None:
        """stage_run の状態は primary の結果で決まる（secondary の失敗では失敗にしない）。"""
        stage_run = self.stage_runs.get_or_raise(stage_run_id)
        self.session.refresh(stage_run, with_for_update=True)
        if stage_run.status != RunStatus.RUNNING.value:  # 取り消し済み・途絶として処理済み
            self.session.commit()
            return
        status = RunStatus.SUCCEEDED if primary_status is RunStatus.SUCCEEDED else RunStatus.FAILED
        stage_run.status = status.value
        stage_run.finished_at = utcnow()
        self._recompute_idea_stage(stage_run)
        primary = self.executions_for(stage_run.id)[0]
        record_audit(
            self.session,
            organization_id=stage_run.organization_id,
            entity_type="stage_run",
            entity_id=stage_run.id,
            action=status.value,
            execution_id=primary.id,
            after={"error_type": primary.error_type} if primary.error_type else None,
        )
        self.session.commit()

    def _recompute_idea_stage(self, stage_run: StageRun) -> None:
        if stage_run.idea_id is None:
            return
        idea = self.ideas.get(stage_run.idea_id)
        if idea is not None:
            self._recompute_current_stage(idea)

    def _recompute_current_stage(self, idea: Idea) -> None:
        """current_stage_key = 最新（未 supersede）かつ成功した試行のうち最も後ろのステージ。"""
        self.session.flush()
        succeeded = [
            get_stage(r.stage_key)
            for r in self.stage_runs.current_all(idea.exploration_id, idea.id)
            if r.status == RunStatus.SUCCEEDED.value
        ]
        idea.current_stage_key = max(succeeded, key=lambda s: s.order).key if succeeded else None


# ---------------------------------------------------------------------- ワーカー用の操作


def claim_next_queued(session: Session) -> UUID | None:
    """最も古い queued の stage_run を1つ選ぶ（他のワーカーがロック中のものは飛ばす）。"""
    stmt = (
        select(StageRun.id)
        .where(StageRun.status == RunStatus.QUEUED.value)
        .order_by(StageRun.started_at, StageRun.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    stage_run_id = session.execute(stmt).scalar_one_or_none()
    session.rollback()  # ロックは execute() の中で取り直す
    return stage_run_id


def touch_heartbeat(session: Session, stage_run_id: UUID) -> None:
    stage_run = session.get(StageRun, stage_run_id)
    if stage_run is not None and stage_run.status == RunStatus.RUNNING.value:
        stage_run.heartbeat_at = utcnow()
    session.commit()


def fail_stale_runs(session: Session, heartbeat_timeout_seconds: float) -> list[UUID]:
    """heartbeat が途絶えた running の実行を failed にする（自動の再実行はしない）。"""
    threshold = utcnow() - timedelta(seconds=heartbeat_timeout_seconds)
    stale = session.scalars(
        select(StageRun)
        .where(StageRun.status == RunStatus.RUNNING.value, StageRun.heartbeat_at < threshold)
        .with_for_update(skip_locked=True)
    ).all()
    now = utcnow()
    for stage_run in stale:
        stage_run.status = RunStatus.FAILED.value
        stage_run.finished_at = now
        executions = session.scalars(
            select(Execution).where(Execution.stage_run_id == stage_run.id)
        ).all()
        for execution in executions:
            if execution.status in ACTIVE_RUN_STATUSES:
                execution.status = RunStatus.FAILED.value
                execution.error_type = ErrorType.UNEXPECTED.value
                execution.error_message = HEARTBEAT_LOST_MESSAGE
                execution.finished_at = now
        if stage_run.idea_id is not None:
            idea = session.get(Idea, stage_run.idea_id)
            if idea is not None:
                session.flush()
                succeeded = [
                    get_stage(r.stage_key)
                    for r in StageRunRepository(session).current_all(idea.exploration_id, idea.id)
                    if r.status == RunStatus.SUCCEEDED.value
                ]
                idea.current_stage_key = (
                    max(succeeded, key=lambda s: s.order).key if succeeded else None
                )
        record_audit(
            session,
            organization_id=stage_run.organization_id,
            entity_type="stage_run",
            entity_id=stage_run.id,
            action="failed",
            after={"error_type": ErrorType.UNEXPECTED.value, "reason": HEARTBEAT_LOST_MESSAGE},
        )
    session.commit()
    return [r.id for r in stale]


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


def _analysis_view(analysis: Analysis, review: HumanReview | None) -> AnalysisView:
    """後続の AI社員に渡す前段の分析（V-07：レビューの状態と分析全体の最新のレビュー1件）。"""
    return AnalysisView(
        id=analysis.id,
        stage_key=analysis.stage_key,
        summary=analysis.summary,
        review_status=analysis.review_status,
        latest_review=(
            ReviewView(
                decision=review.decision,
                comment=review.comment,
                corrections=review.corrections,
            )
            if review is not None
            else None
        ),
    )
