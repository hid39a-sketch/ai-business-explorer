from uuid import UUID

from fastapi import APIRouter, status

from ai_business_explorer.api.v1.deps import PageDep, ReviewerDep, SessionDep
from ai_business_explorer.api.v1.schemas import (
    AnalysisDetailOut,
    AnalysisOut,
    ClaimOut,
    EvidenceLinkOut,
    HumanReviewOut,
    PageOut,
)
from ai_business_explorer.application.analyses import AnalysisService, ClaimDetail
from ai_business_explorer.application.commands import HumanReviewCreate
from ai_business_explorer.application.reviews import ReviewService
from ai_business_explorer.domain.evidence import EvidenceState

router = APIRouter(prefix="/analyses", tags=["analyses"])


@router.get("/{analysis_id}", response_model=AnalysisDetailOut)
def get_analysis(analysis_id: UUID, session: SessionDep) -> object:
    detail = AnalysisService(session).get(analysis_id)
    claims = [_claim_out(c, detail.evidence_states) for c in detail.claims]
    return AnalysisDetailOut(
        **AnalysisOut.model_validate(detail.analysis).model_dump(),
        claims=claims,
        evidence_links=[link for c in claims for link in c.evidence_links],
        human_reviews=[HumanReviewOut.model_validate(r) for r in detail.reviews],
    )


def _claim_out(detail: ClaimDetail, states: dict[UUID, EvidenceState]) -> ClaimOut:
    claim = detail.claim
    return ClaimOut(
        id=claim.id,
        claim_key=claim.claim_key,
        ordinal=claim.ordinal,
        kind=claim.kind,
        text=claim.text,
        evidence_links=[
            EvidenceLinkOut(
                claim_id=claim.id,
                claim_ref=claim.claim_key,
                evidence_id=link.evidence_id,
                relation=link.relation,
                evidence_status=states[link.evidence_id].status.value,
                evidence_is_retracted=states[link.evidence_id].is_retracted,
                evidence_is_superseded=states[link.evidence_id].is_superseded,
                evidence_is_purged=states[link.evidence_id].is_purged,
                evidence_superseded_by_id=states[link.evidence_id].superseded_by_id,
            )
            for link in detail.evidence_links
        ],
        latest_review=(
            HumanReviewOut.model_validate(detail.latest_review) if detail.latest_review else None
        ),
    )


@router.post(
    "/{analysis_id}/human-reviews",
    response_model=HumanReviewOut,
    status_code=status.HTTP_201_CREATED,
    summary="AI Analysis または主張（claim_id）をレビューする（reviewer 以上の人間のみ）",
)
def create_human_review(
    analysis_id: UUID, body: HumanReviewCreate, actor: ReviewerDep, session: SessionDep
) -> object:
    return ReviewService(session).create(actor, analysis_id, body)


@router.get("/{analysis_id}/human-reviews", response_model=PageOut[HumanReviewOut])
def list_human_reviews(analysis_id: UUID, session: SessionDep, page: PageDep) -> object:
    return ReviewService(session).list_for_analysis(analysis_id, page)
