from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.domain.enums import ReviewStatus
from ai_business_explorer.domain.evidence import EvidenceState
from ai_business_explorer.infrastructure.db.models import (
    Analysis,
    Claim,
    ClaimEvidenceLink,
    Execution,
    HumanReview,
)
from ai_business_explorer.infrastructure.db.repositories import (
    AnalysisRepository,
    ClaimEvidenceLinkRepository,
    ClaimRepository,
    EvidenceRepository,
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
    # 根拠リンクの参照先 Evidence の現在の状態（取得時に算出。リンクは書き換えない。E-01）
    evidence_states: dict[UUID, EvidenceState]


class AnalysisService:
    """AI Analysis の参照のみ。AI Analysis の作成は StageRunService の実行経路に限られる。

    主張と根拠は claims / claim_evidence_links（正本）から返す。analyses.body は
    生成時点のAI出力スナップショットとしてそのまま返す。
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.analyses = AnalysisRepository(session)
        self.claims = ClaimRepository(session)
        self.evidence = EvidenceRepository(session)
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
        linked_ids = sorted(
            {link.evidence_id for links in links_by_claim.values() for link in links}
        )
        linked = [self.evidence.get_or_raise(evidence_id) for evidence_id in linked_ids]
        successors = self.evidence.successors(linked_ids)
        evidence_states = {
            e.id: EvidenceState(
                is_purged=e.content_purged_at is not None,
                is_retracted=e.retracted_at is not None,
                superseded_by_id=successors.get(e.id),
            )
            for e in linked
        }
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
            evidence_states=evidence_states,
        )

    def list_for_idea(
        self, idea_id: UUID, page: PageRequest, filters: "AnalysisFilters"
    ) -> Page[Analysis]:
        self.ideas.get_or_raise(idea_id)
        return self._page(page, filters, Analysis.idea_id == idea_id)

    def list_for_exploration(
        self, exploration_id: UUID, page: PageRequest, filters: "AnalysisFilters"
    ) -> Page[Analysis]:
        self.explorations.get_or_raise(exploration_id)
        return self._page(page, filters, Analysis.exploration_id == exploration_id)

    def _page(
        self, page: PageRequest, filters: "AnalysisFilters", *criteria: Any
    ) -> Page[Analysis]:
        conditions = list(criteria)
        if filters.stage_key is not None:
            conditions.append(Analysis.stage_key == filters.stage_key)
        if filters.review_status is not None:
            conditions.append(Analysis.review_status == filters.review_status.value)
        if filters.ai_employee_id is not None:
            conditions.append(
                Analysis.execution_id.in_(
                    select(Execution.id).where(Execution.ai_employee_id == filters.ai_employee_id)
                )
            )
        return paginate(
            self.session,
            self.analyses.select(*conditions),
            sort_column=Analysis.created_at,
            id_column=Analysis.id,
            page=page,
        )


@dataclass(frozen=True)
class AnalysisFilters:
    stage_key: str | None = None
    review_status: ReviewStatus | None = None
    ai_employee_id: UUID | None = None
