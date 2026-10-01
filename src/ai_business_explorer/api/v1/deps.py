"""API の依存関係。操作者の解決（X-Actor-Id）はここに集約し、将来は認証基盤に差し替える。"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import AgentRegistry
from ai_business_explorer.application.actors import ActorService
from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import Actor
from ai_business_explorer.tools.base import ToolRegistry


def get_session(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_agent_registry(request: Request) -> AgentRegistry:
    registry: AgentRegistry = request.app.state.agent_registry
    return registry


def get_tool_registry(request: Request) -> ToolRegistry:
    registry: ToolRegistry = request.app.state.tool_registry
    return registry


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AgentRegistryDep = Annotated[AgentRegistry, Depends(get_agent_registry)]
ToolRegistryDep = Annotated[ToolRegistry, Depends(get_tool_registry)]


def get_actor(
    session: SessionDep,
    x_actor_id: Annotated[
        UUID | None,
        Header(description="操作者の actor ID（第1回の簡易方式。本番利用不可）"),
    ] = None,
) -> Actor:
    return ActorService(session).authenticate(x_actor_id)


ActorDep = Annotated[Actor, Depends(get_actor)]
