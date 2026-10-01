"""一覧の絞り込みパラメータ（第2回仕様 15章）。"""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query

from ai_business_explorer.application.analyses import AnalysisFilters
from ai_business_explorer.domain.enums import EvidenceSourceType, ReviewStatus
from ai_business_explorer.domain.evidence import EvidenceStatus


@dataclass(frozen=True)
class EvidenceFilters:
    statuses: list[EvidenceStatus]
    source_type: EvidenceSourceType | None


def evidence_filters(
    status: Annotated[
        list[EvidenceStatus] | None,
        Query(
            description=(
                "取得する状態（繰り返し指定できる。例：?status=active&status=superseded）。"
                "省略時は active だけ"
            )
        ),
    ] = None,
    source_type: EvidenceSourceType | None = None,
) -> EvidenceFilters:
    return EvidenceFilters(
        statuses=list(dict.fromkeys(status)) if status else [EvidenceStatus.ACTIVE],
        source_type=source_type,
    )


EvidenceFiltersDep = Annotated[EvidenceFilters, Depends(evidence_filters)]


def analysis_filters(
    stage_key: str | None = None,
    review_status: ReviewStatus | None = None,
    ai_employee_id: UUID | None = None,
) -> AnalysisFilters:
    return AnalysisFilters(
        stage_key=stage_key, review_status=review_status, ai_employee_id=ai_employee_id
    )
