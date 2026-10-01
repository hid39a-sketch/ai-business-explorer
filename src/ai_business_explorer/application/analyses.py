from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.infrastructure.db.models import (
    Analysis,
    Claim,
    ClaimEvidenceLink,
    HumanReview,
)
from ai_business_explorer.infrastructure.db.repositories import (
    AnalysisRepository,
    ClaimEvidenceLinkRepository,
    ClaimRepository,
    ExplorationRepository,
    HumanReviewRepository,
    IdeaRepository,
)


@dataclass(frozen=True)
class ClaimDetail:
    claim: Claim
    evidence_links: Sequence[ClaimEvidenceLink]
    latest_review: HumanReview | None


@dataclass(frozen=True)
class AnalysisDetail:
    analysis: Analysis
    claims: Sequence[ClaimDetail]
    reviews: Sequence[HumanReview]


class AnalysisService:
    """AI Analysis の参照のみ。AI Analysis の作成は StageRunService の実行経路に限られる。

    主張と根拠は claims / claim_evidence_links（正本）から返す。analyses.body は
    生成時点のAI出力スナップショットとしてそのまま返す。
    """

    def __init__(self, session: Session) -> None:
        self.analyses = AnalysisRepository(session)
        self.claims = ClaimRepository(session)
        self.links = ClaimEvidenceLinkRepository(session)
        self.reviews = HumanReviewRepository(session)
        self.ideas = IdeaRepository(session)
        self.explorations = ExplorationRepository(session)

    def get(self, analysis_id: UUID) -> AnalysisDetail:
        analysis = self.analyses.get_or_raise(analysis_id)
        claims = self.claims.for_analysis(analysis.id)
        links_by_claim: dict[UUID, list[ClaimEvidenceLink]] = {}
        for link in self.links.for_claims([c.id for c in claims]):
            links_by_claim.setdefault(link.claim_id, []).append(link)
        reviews = self.reviews.list_where(HumanReview.analysis_id == analysis.id)
        latest_by_claim: dict[UUID, HumanReview] = {}
        for review in reviews:  # 作成順。後のレビューで上書きして最新を残す
            if review.claim_id is not None:
                latest_by_claim[review.claim_id] = review
        return AnalysisDetail(
            analysis=analysis,
            claims=[
                ClaimDetail(
                    claim=c,
                    evidence_links=sorted(
                        links_by_claim.get(c.id, []), key=lambda x: (str(x.evidence_id), x.relation)
                    ),
                    latest_review=latest_by_claim.get(c.id),
                )
                for c in claims
            ],
            reviews=reviews,
        )

    def list_for_idea(self, idea_id: UUID) -> Sequence[Analysis]:
        self.ideas.get_or_raise(idea_id)
        return self.analyses.list_where(Analysis.idea_id == idea_id)

    def list_for_exploration(self, exploration_id: UUID) -> Sequence[Analysis]:
        self.explorations.get_or_raise(exploration_id)
        return self.analyses.list_where(Analysis.exploration_id == exploration_id)
