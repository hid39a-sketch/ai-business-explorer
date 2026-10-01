"""Human Review と Human Decision。どちらも人間のみ・追記のみ。AI社員からの経路はない。"""

from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.application.commands import HumanDecisionCreate, HumanReviewCreate
from ai_business_explorer.application.common import record_audit, require_human, to_jsonable
from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.domain.enums import REVIEW_STATUS_BY_DECISION, AdoptionStatus
from ai_business_explorer.domain.errors import DomainValidationError, InvalidStateError
from ai_business_explorer.infrastructure.db.models import Actor, HumanDecision, HumanReview
from ai_business_explorer.infrastructure.db.repositories import (
    AnalysisRepository,
    ClaimRepository,
    HumanDecisionRepository,
    HumanReviewRepository,
    IdeaRepository,
)


class ReviewService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.reviews = HumanReviewRepository(session)
        self.analyses = AnalysisRepository(session)
        self.claims = ClaimRepository(session)

    def create(self, actor: Actor, analysis_id: UUID, cmd: HumanReviewCreate) -> HumanReview:
        require_human(actor, "review analyses")
        analysis = self.analyses.get_or_raise(analysis_id)
        if cmd.claim_id is not None:
            claim = self.claims.get_or_raise(cmd.claim_id)
            if claim.analysis_id != analysis.id:
                raise DomainValidationError("claim does not belong to the analysis")
        review = self.reviews.add(
            HumanReview(
                organization_id=analysis.organization_id,
                analysis_id=analysis.id,
                claim_id=cmd.claim_id,
                exploration_id=analysis.exploration_id,
                idea_id=analysis.idea_id,
                reviewer_actor_id=actor.id,
                reviewer_actor_type=actor.actor_type,
                decision=cmd.decision.value,
                comment=cmd.comment,
                corrections=cmd.corrections,
            )
        )
        if cmd.claim_id is not None:
            # 主張単位のレビューは分析全体の review_status を変えない（B-15）。
            record_audit(
                self.session,
                organization_id=analysis.organization_id,
                entity_type="analysis",
                entity_id=analysis.id,
                action="claim_reviewed",
                actor_id=actor.id,
                after={
                    "claim_id": cmd.claim_id,
                    "decision": cmd.decision.value,
                    "human_review_id": review.id,
                },
            )
            self.session.commit()
            return review
        # AI Analysis の本体は変更しない。レビュー状態のみ最新レビュー（claim_id なし）に合わせる。
        before = analysis.review_status
        analysis.review_status = REVIEW_STATUS_BY_DECISION[cmd.decision].value
        record_audit(
            self.session,
            organization_id=analysis.organization_id,
            entity_type="analysis",
            entity_id=analysis.id,
            action="reviewed",
            actor_id=actor.id,
            before={"review_status": before},
            after={"review_status": analysis.review_status, "human_review_id": review.id},
        )
        self.session.commit()
        return review

    def list_for_analysis(self, analysis_id: UUID, page: PageRequest) -> Page[HumanReview]:
        self.analyses.get_or_raise(analysis_id)
        return paginate(
            self.session,
            self.reviews.select(HumanReview.analysis_id == analysis_id),
            sort_column=HumanReview.created_at,
            id_column=HumanReview.id,
            page=page,
        )


class DecisionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.decisions = HumanDecisionRepository(session)
        self.reviews = HumanReviewRepository(session)
        self.ideas = IdeaRepository(session)

    def create(self, actor: Actor, idea_id: UUID, cmd: HumanDecisionCreate) -> HumanDecision:
        require_human(actor, "make business decisions")
        idea = self.ideas.get_or_raise(idea_id)
        # 最終判断は、人間が採用（adopt）して調査対象にした Idea に対してのみ記録できる。
        if idea.adoption_status != AdoptionStatus.ADOPTED.value:
            raise InvalidStateError(
                f"idea is '{idea.adoption_status}'; human decisions require an 'adopted' idea"
            )
        for review_id in cmd.based_on_review_ids:
            review = self.reviews.get_or_raise(review_id)
            related = review.idea_id == idea.id or (
                idea.origin_analysis_id is not None
                and review.analysis_id == idea.origin_analysis_id
            )
            if not related:
                raise DomainValidationError(f"human_review {review_id} is not related to this idea")
        decision = self.decisions.add(
            HumanDecision(
                organization_id=idea.organization_id,
                idea_id=idea.id,
                decided_by_actor_id=actor.id,
                decided_by_actor_type=actor.actor_type,
                decision=cmd.decision.value,
                rationale=cmd.rationale,
                based_on_review_ids=to_jsonable(cmd.based_on_review_ids),
            )
        )
        record_audit(
            self.session,
            organization_id=idea.organization_id,
            entity_type="idea",
            entity_id=idea.id,
            action="human_decision",
            actor_id=actor.id,
            after={"decision": decision.decision, "human_decision_id": decision.id},
        )
        self.session.commit()
        return decision

    def list_for_idea(self, idea_id: UUID, page: PageRequest) -> Page[HumanDecision]:
        self.ideas.get_or_raise(idea_id)
        return paginate(
            self.session,
            self.decisions.select(HumanDecision.idea_id == idea_id),
            sort_column=HumanDecision.created_at,
            id_column=HumanDecision.id,
            page=page,
        )
