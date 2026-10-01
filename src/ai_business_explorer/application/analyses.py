from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.infrastructure.db.models import (
    Analysis,
    AnalysisEvidenceLink,
    HumanReview,
)
from ai_business_explorer.infrastructure.db.repositories import (
    AnalysisEvidenceLinkRepository,
    AnalysisRepository,
    ExplorationRepository,
    HumanReviewRepository,
    IdeaRepository,
)


@dataclass(frozen=True)
class AnalysisDetail:
    analysis: Analysis
    evidence_links: Sequence[AnalysisEvidenceLink]
    reviews: Sequence[HumanReview]


class AnalysisService:
    """AI Analysis の参照のみ。AI Analysis の作成は StageRunService の実行経路に限られる。"""

    def __init__(self, session: Session) -> None:
        self.analyses = AnalysisRepository(session)
        self.links = AnalysisEvidenceLinkRepository(session)
        self.reviews = HumanReviewRepository(session)
        self.ideas = IdeaRepository(session)
        self.explorations = ExplorationRepository(session)

    def get(self, analysis_id: UUID) -> AnalysisDetail:
        analysis = self.analyses.get_or_raise(analysis_id)
        return AnalysisDetail(
            analysis=analysis,
            evidence_links=self.links.for_analysis(analysis.id),
            reviews=self.reviews.list_where(HumanReview.analysis_id == analysis.id),
        )

    def list_for_idea(self, idea_id: UUID) -> Sequence[Analysis]:
        self.ideas.get_or_raise(idea_id)
        return self.analyses.list_where(Analysis.idea_id == idea_id)

    def list_for_exploration(self, exploration_id: UUID) -> Sequence[Analysis]:
        self.explorations.get_or_raise(exploration_id)
        return self.analyses.list_where(Analysis.exploration_id == exploration_id)
