"""サービス共通の補助関数。"""

import subprocess
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy.orm import Session

from ai_business_explorer.domain.enums import ActorType
from ai_business_explorer.domain.errors import PermissionDeniedError
from ai_business_explorer.infrastructure.db.models import Actor, AuditEvent
from ai_business_explorer.infrastructure.db.repositories import scoped_organization_id

_json_adapter: TypeAdapter[Any] = TypeAdapter(Any)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_jsonable(value: Any) -> Any:
    return _json_adapter.dump_python(value, mode="json")


def require_human(actor: Actor, action: str) -> None:
    """人間専用操作のガード。DB 側でも human_reviews / human_decisions / stage_runs は
    複合 FK + CHECK 制約により human 以外を拒否する（二重の防御）。"""
    if actor.actor_type != ActorType.HUMAN.value:
        raise PermissionDeniedError(f"only human actors can {action}")


def current_organization_id(session: Session) -> UUID:
    """操作者の組織。API では依存関係（deps）がリクエストごとにセッションへ設定する。"""
    org_id = scoped_organization_id(session)
    if org_id is None:
        raise RuntimeError("organization scope is not set on the session")
    return org_id


def record_audit(
    session: Session,
    *,
    organization_id: UUID,
    entity_type: str,
    entity_id: UUID,
    action: str,
    actor_id: UUID | None = None,
    execution_id: UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            organization_id=organization_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_id=actor_id,
            execution_id=execution_id,
            before=to_jsonable(before) if before is not None else None,
            after=to_jsonable(after) if after is not None else None,
        )
    )


def snapshot(obj: Any, fields: list[str]) -> dict[str, Any]:
    return {f: to_jsonable(getattr(obj, f)) for f in fields}


@lru_cache
def detect_code_version() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607  固定コマンド、ユーザー入力なし
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
