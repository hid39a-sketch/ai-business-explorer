from uuid import UUID

from fastapi import APIRouter

from ai_business_explorer.api.v1.deps import SessionDep
from ai_business_explorer.api.v1.schemas import ActorOut
from ai_business_explorer.application.actors import ActorService

router = APIRouter(prefix="/actors", tags=["actors"])


@router.get("", response_model=list[ActorOut])
def list_actors(session: SessionDep) -> object:
    return ActorService(session).list()


@router.get("/{actor_id}", response_model=ActorOut)
def get_actor(actor_id: UUID, session: SessionDep) -> object:
    return ActorService(session).get(actor_id)
