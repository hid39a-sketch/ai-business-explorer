from uuid import UUID

from fastapi import APIRouter, status

from ai_business_explorer.api.v1.deps import ReviewerDep, SessionDep
from ai_business_explorer.api.v1.schemas import (
    AnalysisDetailOut,
    AnalysisOut,
    EvidenceLinkOut,
    HumanReviewOut,
)
from ai_business_explorer.application.analyses import AnalysisService
from ai_business_explorer.application.commands import HumanReviewCreate
from ai_business_explorer.application.reviews import ReviewService

router = APIRouter(prefix="/analyses", tags=["analyses"])


@router.get("/{analysis_id}", response_model=AnalysisDetailOut)
def get_analysis(analysis_id: UUID, session: SessionDep) -> object:
    detail = AnalysisService(session).get(analysis_id)
    return AnalysisDetailOut(
        **AnalysisOut.model_validate(detail.analysis).model_dump(),
        evidence_links=[EvidenceLinkOut.model_validate(link) for link in detail.evidence_links],
        human_reviews=[HumanReviewOut.model_validate(r) for r in detail.reviews],
    )


@router.post(
    "/{analysis_id}/human-reviews",
    response_model=HumanReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="AI Analysis をレビューする（reviewer 以上の人間のみ）",
)
def create_human_review(
    analysis_id: UUID, body: HumanReviewCreate, actor: ReviewerDep, session: SessionDep
) -> object:
    return ReviewService(session).create(actor, analysis_id, body)


@router.get("/{analysis_id}/human-reviews", response_model=list[HumanReviewOut])
def list_human_reviews(analysis_id: UUID, session: SessionDep) -> object:
    return ReviewService(session).list_for_analysis(analysis_id)
