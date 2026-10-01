from dataclasses import asdict
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from ai_business_explorer.api.v1.deps import (
    AgentRegistryDep,
    MemberDep,
    PageDep,
    ReviewerDep,
    SessionDep,
    SettingsDep,
    ToolRegistryDep,
)
from ai_business_explorer.api.v1.filters import EvidenceFiltersDep, analysis_filters
from ai_business_explorer.api.v1.routers.evidence import evidence_page
from ai_business_explorer.api.v1.routers.stage_runs import stage_run_detail
from ai_business_explorer.api.v1.schemas import (
    AnalysisOut,
    EvidenceOut,
    HumanDecisionOut,
    IdeaOut,
    PageOut,
    ResearchStatusOut,
    StageRunDetailOut,
    StageRunOut,
)
from ai_business_explorer.application.analyses import AnalysisFilters, AnalysisService
from ai_business_explorer.application.commands import (
    HumanDecisionCreate,
    IdeaAdoptionCommand,
    IdeaStageRunCommand,
    IdeaUpdate,
    SendBackCommand,
)
from ai_business_explorer.application.evidence import EvidenceService
from ai_business_explorer.application.ideas import IdeaService
from ai_business_explorer.application.reviews import DecisionService
from ai_business_explorer.application.stage_runs import StageRunService
from ai_business_explorer.domain.evidence import EvidenceScope
from ai_business_explorer.infrastructure.db.models import Idea

router = APIRouter(prefix="/ideas", tags=["ideas"])


def idea_out(service: IdeaService, idea: Idea) -> IdeaOut:
    out = IdeaOut.model_validate(idea)
    out.research_status = ResearchStatusOut(**asdict(service.research_status(idea)))
    return out


@router.get("/{idea_id}", response_model=IdeaOut)
def get_idea(idea_id: UUID, session: SessionDep) -> object:
    service = IdeaService(session)
    return idea_out(service, service.get(idea_id))


@router.patch(
    "/{idea_id}",
    response_model=IdeaOut,
    summary="Idea の詳細項目を更新する（member 以上の人間のみ）",
)
def update_idea(idea_id: UUID, body: IdeaUpdate, actor: MemberDep, session: SessionDep) -> object:
    service = IdeaService(session)
    return idea_out(service, service.update(actor, idea_id, body))


@router.post(
    "/{idea_id}/adopt",
    response_model=IdeaOut,
    summary="candidate → adopted（reviewer 以上の人間のみ）",
)
def adopt_idea(
    idea_id: UUID, body: IdeaAdoptionCommand, actor: ReviewerDep, session: SessionDep
) -> object:
    service = IdeaService(session)
    return idea_out(service, service.adopt(actor, idea_id, body))


@router.post(
    "/{idea_id}/reject",
    response_model=IdeaOut,
    summary="candidate → rejected（reviewer 以上の人間のみ）",
)
def reject_idea(
    idea_id: UUID, body: IdeaAdoptionCommand, actor: ReviewerDep, session: SessionDep
) -> object:
    service = IdeaService(session)
    return idea_out(service, service.reject(actor, idea_id, body))


@router.post(
    "/{idea_id}/stage-runs",
    response_model=StageRunDetailOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="アイデア単位のステージを実行・再実行する（member 以上の人間のみ）",
)
def run_idea_stage(
    idea_id: UUID,
    body: IdeaStageRunCommand,
    actor: MemberDep,
    session: SessionDep,
    settings: SettingsDep,
    agents: AgentRegistryDep,
    tools: ToolRegistryDep,
) -> object:
    service = StageRunService(session, settings, agents, tools)
    return stage_run_detail(service, service.run_idea_stage(actor, idea_id, body))


@router.post(
    "/{idea_id}/send-back",
    response_model=StageRunDetailOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="前のステージへ差し戻して再実行する（reviewer 以上の人間のみ）",
)
def send_back(
    idea_id: UUID,
    body: SendBackCommand,
    actor: ReviewerDep,
    session: SessionDep,
    settings: SettingsDep,
    agents: AgentRegistryDep,
    tools: ToolRegistryDep,
) -> object:
    service = StageRunService(session, settings, agents, tools)
    return stage_run_detail(service, service.send_back(actor, idea_id, body))


@router.get("/{idea_id}/stage-runs", response_model=PageOut[StageRunOut])
def list_idea_stage_runs(
    idea_id: UUID,
    session: SessionDep,
    settings: SettingsDep,
    agents: AgentRegistryDep,
    tools: ToolRegistryDep,
    page: PageDep,
) -> object:
    return StageRunService(session, settings, agents, tools).list_for_idea(idea_id, page)


@router.get(
    "/{idea_id}/evidence",
    response_model=PageOut[EvidenceOut],
    summary=(
        "Idea の Evidence 一覧（既定は案件全体の Evidence も含む with_exploration、"
        "状態は active のみ）"
    ),
)
def list_idea_evidence(
    idea_id: UUID,
    session: SessionDep,
    page: PageDep,
    filters: EvidenceFiltersDep,
    scope: EvidenceScope = EvidenceScope.WITH_EXPLORATION,
) -> object:
    service = EvidenceService(session)
    return evidence_page(
        service,
        service.list_for_idea(
            idea_id,
            page,
            statuses=filters.statuses,
            scope=scope,
            source_type=filters.source_type,
        ),
    )


@router.get("/{idea_id}/analyses", response_model=PageOut[AnalysisOut])
def list_idea_analyses(
    idea_id: UUID,
    session: SessionDep,
    page: PageDep,
    filters: Annotated[AnalysisFilters, Depends(analysis_filters)],
) -> object:
    return AnalysisService(session).list_for_idea(idea_id, page, filters)


@router.post(
    "/{idea_id}/human-decisions",
    response_model=HumanDecisionOut,
    status_code=status.HTTP_201_CREATED,
    summary="人間による最終的な事業判断を記録する（reviewer 以上の人間のみ）",
)
def create_human_decision(
    idea_id: UUID, body: HumanDecisionCreate, actor: ReviewerDep, session: SessionDep
) -> object:
    return DecisionService(session).create(actor, idea_id, body)


@router.get("/{idea_id}/human-decisions", response_model=PageOut[HumanDecisionOut])
def list_human_decisions(idea_id: UUID, session: SessionDep, page: PageDep) -> object:
    return DecisionService(session).list_for_idea(idea_id, page)
