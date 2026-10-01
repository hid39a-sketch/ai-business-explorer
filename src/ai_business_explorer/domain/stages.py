"""調査パイプラインのステージ定義。

第1回は直列。各ステージは「探索案件単位」か「アイデア単位」のどちらかで実行される。
human_review は人間専用のステージであり、AI社員を割り当てられない。
"""

from dataclasses import dataclass
from enum import StrEnum

from ai_business_explorer.domain.errors import DomainValidationError


class StageScope(StrEnum):
    EXPLORATION = "exploration"
    IDEA = "idea"


@dataclass(frozen=True)
class StageDefinition:
    key: str
    order: int
    label: str
    scope: StageScope
    executable_by_ai: bool


IDEA_GENERATION = "idea_generation"
HUMAN_REVIEW = "human_review"

STAGES: tuple[StageDefinition, ...] = (
    StageDefinition(IDEA_GENERATION, 1, "Idea Generation", StageScope.EXPLORATION, True),
    StageDefinition("market_research", 2, "Market Research", StageScope.IDEA, True),
    StageDefinition("competitor_research", 3, "Competitor Research", StageScope.IDEA, True),
    StageDefinition("technology_research", 4, "Technology Research", StageScope.IDEA, True),
    StageDefinition("patent_ip_research", 5, "Patent / IP Research", StageScope.IDEA, True),
    StageDefinition("monetization_analysis", 6, "Monetization Analysis", StageScope.IDEA, True),
    StageDefinition("risk_legal_analysis", 7, "Risk / Legal Analysis", StageScope.IDEA, True),
    StageDefinition("overall_analysis", 8, "Overall Analysis", StageScope.IDEA, True),
    StageDefinition(HUMAN_REVIEW, 9, "Human Review", StageScope.IDEA, False),
)

_BY_KEY: dict[str, StageDefinition] = {s.key: s for s in STAGES}
STAGE_KEYS: tuple[str, ...] = tuple(s.key for s in STAGES)


def get_stage(key: str) -> StageDefinition:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise DomainValidationError(f"unknown stage_key: {key}") from None


def previous_idea_stage(key: str) -> StageDefinition | None:
    """直前のアイデア単位ステージ。存在しなければ None（＝前提は「採用済み」であることのみ）。"""
    stage = get_stage(key)
    prev = [s for s in STAGES if s.order < stage.order and s.scope is StageScope.IDEA]
    return prev[-1] if prev else None


def stages_from(key: str) -> list[StageDefinition]:
    """指定ステージとそれ以降のステージ（再実行・差し戻し時に無効化する範囲）。"""
    stage = get_stage(key)
    return [s for s in STAGES if s.order >= stage.order and s.scope is stage.scope]
