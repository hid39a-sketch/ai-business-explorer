from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from ai_business_explorer.api.v1.deps import (
    AgentRegistryDep,
    MemberDep,
    PageDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
    ToolRegistryDep,
)
from ai_business_explorer.api.v1.filters import EvidenceFiltersDep, analysis_filters
from ai_business_explorer.api.v1.routers.evidence import evidence_page
from ai_business_explorer.api.v1.routers.ideas import idea_out
from ai_business_explorer.api.v1.routers.stage_runs import stage_run_detail
from ai_business_explorer.api.v1.schemas import (
    AnalysisOut,
    EvidenceOut,
    ExplorationOut,
    IdeaOut,
    PageOut,
    StageRunDetailOut,
    StageRunOut,
)
from ai_business_explorer.application.analyses import AnalysisFilters, AnalysisService
from ai_business_explorer.application.commands import (
    ExplorationCreate,
    ExplorationStageRunCommand,
    ExplorationUpdate,
    IdeaCreate,
)
from ai_business_explorer.application.evidence import EvidenceService
from ai_business_explorer.application.explorations import ExplorationService
from ai_business_explorer.application.ideas import IdeaService
from ai_business_explorer.application.stage_runs import StageRunService

router = APIRouter(prefix="/explorations", tags=["explorations"])


@router.post("", response_model=ExplorationOut, status_code=status.HTTP_201_CREATED)
def create_exploration(body: ExplorationCreate, actor: MemberDep, session: SessionDep) -> object:
    return ExplorationService(session).create(actor, body)


@router.get("", response_model=PageOut[ExplorationOut])
def list_explorations(session: SessionDep, page: PageDep) -> object:
    return ExplorationService(session).list(page)


@router.get("/{exploration_id}", response_model=ExplorationOut)
def get_exploration(exploration_id: UUID, session: SessionDep) -> object:
    return ExplorationService(session).get(exploration_id)


@router.patch(
    "/{exploration_id}",
    response_model=ExplorationOut,
    summary="探索案件を更新する（member 以上。データ分類を下げるのは admin のみ）",
)
def update_exploration(
    exploration_id: UUID,
    body: ExplorationUpdate,
    actor: MemberDep,
    principal: PrincipalDep,
    session: SessionDep,
) -> object:
    return ExplorationService(session).update(actor, principal.role, exploration_id, body)


@router.post("/{exploration_id}/ideas", response_model=IdeaOut, status_code=status.HTTP_201_CREATED)
def create_idea(
    exploration_id: UUID, body: IdeaCreate, actor: MemberDep, session: SessionDep
) -> object:
    service = IdeaService(session)
    return idea_out(service, service.create(actor, exploration_id, body))


@router.get("/{exploration_id}/ideas", response_model=PageOut[IdeaOut])
def list_ideas(exploration_id: UUID, session: SessionDep, page: PageDep) -> object:
    service = IdeaService(session)
    return service.list_for_exploration(exploration_id, page).map(lambda i: idea_out(service, i))


@router.post(
    "/{exploration_id}/stage-runs",
    response_model=StageRunDetailOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="idea_generation ステージを実行・再実行する（member 以上の人間のみ）",
)
def run_idea_generation(
    exploration_id: UUID,
    body: ExplorationStageRunCommand,
    actor: MemberDep,
    session: SessionDep,
    settings: SettingsDep,
    agents: AgentRegistryDep,
    tools: ToolRegistryDep,
) -> object:
    service = StageRunService(session, settings, agents, tools)
    return stage_run_detail(service, service.run_exploration_stage(actor, exploration_id, body))


@router.get("/{exploration_id}/stage-runs", response_model=PageOut[StageRunOut])
def list_exploration_stage_runs(
    exploration_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    agents: AgentRegistryDep,
    tools: ToolRegistryDep,
    page: PageDep,
) -> object:
    service = StageRunService(session, settings, agents, tools)
    return service.list_for_exploration(exploration_id, page)


@router.get(
    "/{exploration_id}/evidence",
    response_model=PageOut[EvidenceOut],
    summary="探索案件の Evidence 一覧（既定は active のみ。status で状態を指定できる）",
)
def list_exploration_evidence(
    exploration_id: UUID, session: SessionDep, page: PageDep, filters: EvidenceFiltersDep
) -> object:
    service = EvidenceService(session)
    return evidence_page(
        service,
        service.list_for_exploration(
            exploration_id, page, statuses=filters.statuses, source_type=filters.source_type
        ),
    )


@router.get("/{exploration_id}/analyses", response_model=PageOut[AnalysisOut])
def list_exploration_analyses(
    exploration_id: UUID,
    session: SessionDep,
    page: PageDep,
    filters: Annotated[AnalysisFilters, Depends(analysis_filters)],
) -> object:
    return AnalysisService(session).list_for_exploration(exploration_id, page, filters)
