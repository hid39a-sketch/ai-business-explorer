from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.domain.errors import AuthenticationRequiredError
from ai_business_explorer.infrastructure.db.models import Actor
from ai_business_explorer.infrastructure.db.repositories import ActorRepository


class ActorService:
    def __init__(self, session: Session) -> None:
        self.actors = ActorRepository(session)

    def authenticate(self, actor_id: UUID | None) -> Actor:
        """第1回の簡易方式: X-Actor-Id をそのまま信頼する。将来はここを認証基盤に置き換える。"""
        if actor_id is None:
            raise AuthenticationRequiredError("X-Actor-Id header is required")
        actor = self.actors.get(actor_id)
        if actor is None:
            raise AuthenticationRequiredError("unknown actor")
        return actor

    def get(self, actor_id: UUID) -> Actor:
        return self.actors.get_or_raise(actor_id)

    def list(self) -> Sequence[Actor]:
        return self.actors.list_where()
