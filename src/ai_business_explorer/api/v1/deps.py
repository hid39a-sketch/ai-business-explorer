"""API の依存関係。操作者の解決（X-Actor-Id）とロールの確認はここに集約する。

将来は認証基盤に差し替える。

- `SessionDep` を使うエンドポイントは、必ず操作者を解決し、セッションを操作者の組織に限る。
  ロールを持つ人間（viewer 以上）でなければ 403、他組織のデータは 404。
- 書き込みは `MemberDep` / `ReviewerDep` / `AdminDep` で必要なロールを確認する（仕様書 1章の表）。
- AI・system actor はロールを持てないため、API のどの操作も行えない。人間専用の操作は、
  サービス層の require_human と DB 制約でも二重に守る。
"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import AgentRegistry
from ai_business_explorer.application.actors import ActorService, Principal
from ai_business_explorer.config import Settings
from ai_business_explorer.domain.enums import OrganizationRole
from ai_business_explorer.infrastructure.db.models import Actor
from ai_business_explorer.infrastructure.db.repositories import scope_to_organization
from ai_business_explorer.tools.base import ToolRegistry


def get_db_session(request: Request) -> Iterator[Session]:
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


DbSessionDep = Annotated[Session, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AgentRegistryDep = Annotated[AgentRegistry, Depends(get_agent_registry)]
ToolRegistryDep = Annotated[ToolRegistry, Depends(get_tool_registry)]


def get_principal(
    session: DbSessionDep,
    x_actor_id: Annotated[
        UUID | None,
        Header(description="操作者の actor ID（認証のない簡易方式。外部公開する環境では使わない）"),
    ] = None,
) -> Principal:
    return ActorService(session).resolve_principal(x_actor_id)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def get_session(session: DbSessionDep, principal: PrincipalDep) -> Session:
    """操作者の組織に限ったセッション。閲覧（viewer 以上）の確認も兼ねる。"""
    scope_to_organization(session, principal.organization_id)
    return session


SessionDep = Annotated[Session, Depends(get_session)]


def _require(principal: Principal, role: OrganizationRole) -> Actor:
    principal.require(role, "perform this operation")
    return principal.actor


def require_member(principal: PrincipalDep, _: SessionDep) -> Actor:
    return _require(principal, OrganizationRole.MEMBER)


def require_reviewer(principal: PrincipalDep, _: SessionDep) -> Actor:
    return _require(principal, OrganizationRole.REVIEWER)


def require_admin(principal: PrincipalDep, _: SessionDep) -> Actor:
    return _require(principal, OrganizationRole.ADMIN)


MemberDep = Annotated[Actor, Depends(require_member)]
ReviewerDep = Annotated[Actor, Depends(require_reviewer)]
AdminDep = Annotated[Actor, Depends(require_admin)]
