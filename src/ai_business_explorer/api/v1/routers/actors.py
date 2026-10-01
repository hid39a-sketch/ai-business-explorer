from uuid import UUID

from fastapi import APIRouter

from ai_business_explorer.api.v1.deps import PageDep, PrincipalDep, SessionDep
from ai_business_explorer.api.v1.schemas import ActorOut, PageOut
from ai_business_explorer.application.actors import ActorService

router = APIRouter(prefix="/actors", tags=["actors"])


@router.get("", response_model=PageOut[ActorOut], summary="同じ組織に所属する actor の一覧")
def list_actors(principal: PrincipalDep, session: SessionDep, page: PageDep) -> object:
    return ActorService(session).list_in_organization(principal.organization_id, page)


@router.get("/{actor_id}", response_model=ActorOut)
def get_actor(actor_id: UUID, principal: PrincipalDep, session: SessionDep) -> object:
    return ActorService(session).get_in_organization(principal.organization_id, actor_id)
