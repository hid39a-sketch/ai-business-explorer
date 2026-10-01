from uuid import UUID

from fastapi import APIRouter

from ai_business_explorer.api.v1.deps import (
    AgentRegistryDep,
    SessionDep,
    SettingsDep,
    ToolRegistryDep,
)
from ai_business_explorer.api.v1.schemas import ExecutionOut, PageOut, StageOut, StageRunDetailOut
from ai_business_explorer.application.stage_runs import StageRunService
from ai_business_explorer.domain.stages import STAGES
from ai_business_explorer.infrastructure.db.models import StageRun

router = APIRouter(tags=["stage_runs"])


def stage_run_detail(service: StageRunService, stage_run: StageRun) -> StageRunDetailOut:
    out = StageRunDetailOut.model_validate(stage_run)
    out.executions = [ExecutionOut.model_validate(e) for e in service.executions_for(stage_run.id)]
    return out


@router.get("/stages", response_model=PageOut[StageOut])
def list_stages() -> object:
    """ステージの定義（固定の小さな一覧なので、常に1ページで返す）。"""
    items = [
        StageOut(
            key=s.key,
            order=s.order,
            label=s.label,
            scope=s.scope.value,
            executable_by_ai=s.executable_by_ai,
        )
        for s in STAGES
    ]
    return PageOut[StageOut](items=items, next_cursor=None, has_more=False)


@router.get("/stage-runs/{stage_run_id}", response_model=StageRunDetailOut)
def get_stage_run(
    stage_run_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    agents: AgentRegistryDep,
    tools: ToolRegistryDep,
) -> object:
    service = StageRunService(session, settings, agents, tools)
    return stage_run_detail(service, service.get(stage_run_id))


@router.get("/executions/{execution_id}", response_model=ExecutionOut)
def get_execution(
    execution_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    agents: AgentRegistryDep,
    tools: ToolRegistryDep,
) -> object:
    return StageRunService(session, settings, agents, tools).get_execution(execution_id)
