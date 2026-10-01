"""費用・予算・LLM と Tool のログ（第2回仕様 10章・12章）。

閲覧（費用の集計、予算、呼び出しのメタデータ）は viewer 以上。予算の設定と、LLM ログの本文の
閲覧は admin のみ。
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from ai_business_explorer.api.v1.deps import (
    AdminDep,
    PageDep,
    PrincipalDep,
    SessionDep,
    SettingsDep,
)
from ai_business_explorer.api.v1.schemas import (
    BudgetOut,
    BudgetStatusOut,
    CostLineOut,
    CostSummaryOut,
    LLMCallOut,
    LLMCallPayloadOut,
    PageOut,
    ToolCallOut,
)
from ai_business_explorer.application.commands import BudgetSet
from ai_business_explorer.application.costs import (
    BudgetService,
    BudgetStatus,
    CostLine,
    CostService,
    current_month,
)

router = APIRouter(tags=["costs"])

MonthQuery = Annotated[
    str | None,
    Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="対象の月（UTC、YYYY-MM）。既定は当月"),
]


def _line(line: CostLine) -> CostLineOut:
    return CostLineOut(
        exploration_id=line.exploration_id,
        currency=line.currency,
        amount=line.amount,
        llm_amount=line.llm_amount,
        tool_amount=line.tool_amount,
        llm_calls=line.llm_calls,
        tool_calls=line.tool_calls,
    )


def _status(status_: BudgetStatus) -> BudgetStatusOut:
    return BudgetStatusOut(
        budget_id=status_.budget_id,
        exploration_id=status_.exploration_id,
        monthly_limit=status_.monthly_limit,
        currency=status_.currency,
        mode=status_.mode.value,
        spent=status_.spent,
        reserved=status_.reserved,
        remaining=status_.remaining,
    )


@router.get(
    "/costs",
    response_model=CostSummaryOut,
    summary="月ごとの費用（組織の合計・探索案件ごと）と予算の状況",
)
def cost_summary(
    principal: PrincipalDep, session: SessionDep, settings: SettingsDep, month: MonthQuery = None
) -> object:
    target = date.fromisoformat(f"{month}-01") if month else current_month()
    totals, by_exploration = CostService(session, settings).monthly(target)
    budgets = BudgetService(session, settings).all_statuses(principal.organization_id, target)
    return CostSummaryOut(
        month=target.strftime("%Y-%m"),
        totals=[_line(line) for line in totals],
        by_exploration=[_line(line) for line in by_exploration],
        budgets=[_status(b) for b in budgets],
    )


@router.get("/budgets", response_model=PageOut[BudgetOut], summary="設定済みの予算")
def list_budgets(session: SessionDep, settings: SettingsDep, page: PageDep) -> object:
    return BudgetService(session, settings).list(page)


@router.put(
    "/budgets",
    response_model=BudgetOut,
    summary="組織（exploration_id なし）または探索案件の月額予算を設定する（admin のみ）",
)
def set_budget(
    body: BudgetSet, actor: AdminDep, session: SessionDep, settings: SettingsDep
) -> object:
    return BudgetService(session, settings).set(actor, body)


@router.delete(
    "/budgets/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="予算の設定を消す（admin のみ。組織の予算は既定値に戻る）",
)
def delete_budget(
    budget_id: UUID, actor: AdminDep, session: SessionDep, settings: SettingsDep
) -> None:
    BudgetService(session, settings).delete(actor, budget_id)


@router.get(
    "/executions/{execution_id}/llm-calls",
    response_model=PageOut[LLMCallOut],
    summary="実行の LLM 呼び出し（メタデータのみ）",
)
def list_llm_calls(
    execution_id: UUID, session: SessionDep, settings: SettingsDep, page: PageDep
) -> object:
    return CostService(session, settings).llm_calls_for(execution_id, page)


@router.get(
    "/executions/{execution_id}/tool-calls",
    response_model=PageOut[ToolCallOut],
    summary="実行の Tool 呼び出し（メタデータのみ）",
)
def list_tool_calls(
    execution_id: UUID, session: SessionDep, settings: SettingsDep, page: PageDep
) -> object:
    return CostService(session, settings).tool_calls_for(execution_id, page)


@router.get(
    "/llm-calls/{llm_call_id}/payload",
    response_model=LLMCallPayloadOut,
    summary="LLM 呼び出しの本文（admin のみ。保存しなかった・消した本文は 404）",
)
def get_llm_call_payload(
    llm_call_id: UUID, actor: AdminDep, session: SessionDep, settings: SettingsDep
) -> object:
    call, payload = CostService(session, settings).payload(llm_call_id)
    return LLMCallPayloadOut(
        llm_call_id=call.id,
        request=payload.request,
        response=payload.response,
        created_at=payload.created_at,
    )
