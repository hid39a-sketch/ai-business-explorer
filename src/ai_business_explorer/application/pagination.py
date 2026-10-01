"""一覧のカーソル方式のページング（第2回仕様 15章、D-14）。

- 並び順は「作成日時（stage_runs は開始日時）＋ID」。取得中にデータが増えても重複・欠落しない。
- カーソルは中身を意識しない文字列（内部は並びの値と ID の JSON を base64url にしたもの）。
- 総件数は返さない。
"""

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Select, literal, tuple_
from sqlalchemy.orm import InstrumentedAttribute, Session

from ai_business_explorer.domain.errors import DomainValidationError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class PageRequest:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None
    order: SortOrder = SortOrder.ASC
    created_after: datetime | None = None
    created_before: datetime | None = None


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None
    has_more: bool

    def map[U](self, convert: Callable[[T], U]) -> "Page[U]":
        return Page(
            items=[convert(i) for i in self.items],
            next_cursor=self.next_cursor,
            has_more=self.has_more,
        )

    @classmethod
    def of_all(cls, items: Sequence[T]) -> "Page[T]":
        return cls(items=list(items), next_cursor=None, has_more=False)


def encode_cursor(sort_value: datetime, id_: UUID) -> str:
    raw = json.dumps([sort_value.isoformat(), str(id_)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        sort_value, id_ = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return datetime.fromisoformat(sort_value), UUID(id_)
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
        raise DomainValidationError("invalid cursor") from exc


def paginate[M](
    session: Session,
    stmt: Select[tuple[M]],
    *,
    sort_column: InstrumentedAttribute[datetime],
    id_column: InstrumentedAttribute[UUID],
    page: PageRequest,
) -> Page[M]:
    """stmt（絞り込み済み）を「並びの値＋ID」の順に、limit 件ずつ返す。"""
    if not 1 <= page.limit <= MAX_LIMIT:
        raise DomainValidationError(f"limit must be between 1 and {MAX_LIMIT}")
    if page.created_after is not None:
        stmt = stmt.where(sort_column > page.created_after)
    if page.created_before is not None:
        stmt = stmt.where(sort_column < page.created_before)
    key = tuple_(sort_column, id_column)
    if page.cursor is not None:
        sort_value, id_ = decode_cursor(page.cursor)
        after = tuple_(literal(sort_value, sort_column.type), literal(id_, id_column.type))
        stmt = stmt.where(key > after if page.order is SortOrder.ASC else key < after)
    if page.order is SortOrder.ASC:
        stmt = stmt.order_by(sort_column.asc(), id_column.asc())
    else:
        stmt = stmt.order_by(sort_column.desc(), id_column.desc())
    rows = list(session.scalars(stmt.limit(page.limit + 1)).all())
    has_more = len(rows) > page.limit
    items = rows[: page.limit]
    next_cursor = None
    if has_more:
        last: Any = items[-1]
        next_cursor = encode_cursor(getattr(last, sort_column.key), getattr(last, id_column.key))
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)
