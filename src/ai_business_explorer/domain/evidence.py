"""Evidence の状態と出典の同一性（第2回仕様 4章・6章、E-01）。"""

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID


class EvidenceStatus(StrEnum):
    """Evidence の状態。保存せず算出する。superseded・retracted・purged は別の概念。"""

    ACTIVE = "active"
    SUPERSEDED = "superseded"  # 更新版に置き換わった（自動。人間の撤回ではない）
    RETRACTED = "retracted"  # 人間が理由を付けて撤回した
    PURGED = "purged"  # 本文を消去した（admin、理由必須）


class EvidenceScope(StrEnum):
    """Idea ごとの Evidence 一覧の範囲。with_exploration は AI の入力範囲と同じ。"""

    WITH_EXPLORATION = "with_exploration"
    IDEA_ONLY = "idea_only"


@dataclass(frozen=True)
class EvidenceState:
    """状態（優先順位 purged > retracted > superseded > active）と、元の各状態。"""

    is_purged: bool
    is_retracted: bool
    superseded_by_id: UUID | None

    @property
    def is_superseded(self) -> bool:
        return self.superseded_by_id is not None

    @property
    def status(self) -> EvidenceStatus:
        if self.is_purged:
            return EvidenceStatus.PURGED
        if self.is_retracted:
            return EvidenceStatus.RETRACTED
        if self.is_superseded:
            return EvidenceStatus.SUPERSEDED
        return EvidenceStatus.ACTIVE


# 除去する追跡用パラメータ（暫定。第2回仕様 3分類「暫定でよい」）
TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
TRACKING_PREFIXES = ("utm_",)


def normalize_url(url: str) -> str:
    """Web の source_key。スキームとホストを小文字にし、# 以降と追跡用パラメータを除く。"""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in TRACKING_PARAMS and not k.startswith(TRACKING_PREFIXES)
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), "")
    )
