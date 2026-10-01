from uuid import UUID

from fastapi import APIRouter, status

from ai_business_explorer.api.v1.deps import (
    ActorDep,
    AgentRegistryDep,
    SessionDep,
    SettingsDep,
    ToolRegistryDep,
)
from ai_business_explorer.api.v1.routers.ideas import idea_out
from ai_business_explorer.api.v1.routers.stage_runs import stage_run_detail
from ai_business_explorer.api.v1.schemas import (
    AnalysisOut,
    EvidenceOut,
    ExplorationOut,
    IdeaOut,
    StageRunDetailOut,
    StageRunOut,
)
from ai_business_explorer.application.analyses import AnalysisService
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
def create_exploration(body: ExplorationCreate, actor: ActorDep, session: SessionDep) -> object:
    return ExplorationService(session).create(actor, body)


@router.get("", response_model=list[ExplorationOut])
def list_explorations(session: SessionDep) -> object:
    return ExplorationService(session).list()


@router.get("/{exploration_id}", response_model=ExplorationOut)
def get_exploration(exploration_id: UUID, session: SessionDep) -> object:
    return ExplorationService(session).get(exploration_id)


@router.patch("/{exploration_id}", response_model=ExplorationOut)
def update_exploration(
    exploration_id: UUID, body: ExplorationUpdate, actor: ActorDep, session: SessionDep
) -> object:
    return ExplorationService(session).update(actor, exploration_id, body)


@router.post("/{exploration_id}/ideas", response_model=IdeaOut, status_code=status.HTTP_201_CREATED)
def create_idea(
    exploration_id: UUID, body: IdeaCreate, actor: ActorDep, session: SessionDep
) -> object:
    service = IdeaService(session)
    return idea_out(service, service.create(actor, exploration_id, body))


@router.get("/{exploration_id}/ideas", response_model=list[IdeaOut])
def list_ideas(exploration_id: UUID, session: SessionDep) -> object:
    service = IdeaService(session)
    return [idea_out(service, i) for i in service.list_for_exploration(exploration_id)]


@router.post(
    "/{exploration_id}/stage-runs",
    response_model=StageRunDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="idea_generation ステージを実行する（人間のみ）",
)
def run_idea_generation(
    exploration_id: UUID,
    body: ExplorationStageRunCommand,
    actor: ActorDep,
    session: SessionDep,
    settings: SettingsDep,
    agents: AgentRegistryDep,
    tools: ToolRegistryDep,
) -> object:
    service = StageRunService(session, settings, agents, tools)
    return stage_run_detail(service, service.run_exploration_stage(actor, exploration_id, body))


@router.get("/{exploration_id}/stage-runs", response_model=list[StageRunOut])
def list_exploration_stage_runs(
    exploration_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    agents: AgentRegistryDep,
    tools: ToolRegistryDep,
) -> object:
    return StageRunService(session, settings, agents, tools).list_for_exploration(exploration_id)


@router.get("/{exploration_id}/evidence", response_model=list[EvidenceOut])
def list_exploration_evidence(exploration_id: UUID, session: SessionDep) -> object:
    return EvidenceService(session).list_for_exploration(exploration_id)


@router.get("/{exploration_id}/analyses", response_model=list[AnalysisOut])
def list_exploration_analyses(exploration_id: UUID, session: SessionDep) -> object:
    return AnalysisService(session).list_for_exploration(exploration_id)
