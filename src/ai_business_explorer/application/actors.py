from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.domain.enums import ActorType, OrganizationRole
from ai_business_explorer.domain.errors import (
    AuthenticationRequiredError,
    NotFoundError,
    PermissionDeniedError,
)
from ai_business_explorer.infrastructure.db.models import Actor
from ai_business_explorer.infrastructure.db.repositories import (
    ActorRepository,
    OrganizationMembershipRepository,
)


@dataclass(frozen=True)
class Principal:
    """API の操作者。人間の actor と、その所属組織・ロール。"""

    actor: Actor
    organization_id: UUID
    role: OrganizationRole

    def require(self, required: OrganizationRole, action: str) -> None:
        if not self.role.includes(required):
            raise PermissionDeniedError(
                f"role '{required.value}' or higher is required to {action} "
                f"(current role: '{self.role.value}')"
            )


class ActorService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.actors = ActorRepository(session)
        self.memberships = OrganizationMembershipRepository(session)

    def authenticate(self, actor_id: UUID | None) -> Actor:
        """X-Actor-Id をそのまま信頼する簡易方式（第2回も認証なし）。将来は認証基盤に置き換える。"""
        if actor_id is None:
            raise AuthenticationRequiredError("X-Actor-Id header is required")
        actor = self.actors.get(actor_id)
        if actor is None:
            raise AuthenticationRequiredError("unknown actor")
        return actor

    def resolve_principal(self, actor_id: UUID | None) -> Principal:
        """操作者の組織とロールを解決する。

        ロールを持てるのは人間だけ。AI・system actor と、どの組織にも所属していない人間は、
        API のどの操作も行えない（403）。
        """
        actor = self.authenticate(actor_id)
        if actor.actor_type != ActorType.HUMAN.value:
            raise PermissionDeniedError(
                "only human actors with an organization role can use the API"
            )
        membership = self.memberships.for_actor(actor.id)
        if membership is None:
            raise PermissionDeniedError("actor does not belong to any organization")
        return Principal(
            actor=actor,
            organization_id=membership.organization_id,
            role=OrganizationRole(membership.role),
        )

    def get_in_organization(self, organization_id: UUID, actor_id: UUID) -> Actor:
        """同じ組織の actor だけを返す。他組織や所属のない actor は「存在しない」（404）。"""
        membership = self.memberships.for_actor(actor_id)
        if membership is None or membership.organization_id != organization_id:
            raise NotFoundError(f"actors not found: {actor_id}")
        return self.actors.get_or_raise(actor_id)

    def list_in_organization(self, organization_id: UUID, page: PageRequest) -> Page[Actor]:
        return paginate(
            self.session,
            self.actors.select_in_organization(organization_id),
            sort_column=Actor.created_at,
            id_column=Actor.id,
            page=page,
        )
