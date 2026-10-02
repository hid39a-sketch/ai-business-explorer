"""費用管理と LLM・Tool のログ（第2回仕様 10章・12章・14章。E-07・R-16・R-20・R-21）。

- 単価は pricing（組織共通）。呼び出しの時点の単価で費用を計算し、使った単価の ID を残す。
- 費用は呼び出しのたびに記録してすぐ確定する。取り消し・失敗・タイムアウトで終わった実行の
  費用も残り、予算に計上する（E-07）。費用はプロバイダーの請求通貨のまま記録する（R-21）。
- 予算は月単位（UTC の暦月）。組織全体（行がなければ設定の既定値）と、探索案件ごと（任意）。
  hard は超えたら止める（既定）、soft は止めない。
- 起動時：残りの予算（上限 − 当月の費用 − 実行中・待機中の実行の確保分）が、新しい実行の
  上限の合計より少なければ 409（budget_exceeded）。
- 実行中：LLM・Tool を呼ぶ前に、実行ごとの上限（費用・回数）と予算を確認する。超えていれば
  以降の呼び出しを止めて failed（budget_exceeded）。
- LLM ログは、メタデータ（llm_calls。永続）と本文（llm_call_payloads。admin のみ閲覧、
  保存期間で消す）に分ける。confidential 以上の本文は保存しない。秘密情報は保存しない。
"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, literal, select, union_all
from sqlalchemy.orm import Session

from ai_business_explorer.application.candidates import store_tool_candidates
from ai_business_explorer.application.commands import BudgetSet
from ai_business_explorer.application.common import (
    current_organization_id,
    record_audit,
    require_human,
    snapshot,
    to_jsonable,
    utcnow,
)
from ai_business_explorer.application.pagination import Page, PageRequest, paginate
from ai_business_explorer.config import Settings
from ai_business_explorer.domain.enums import (
    ACTIVE_RUN_STATUSES,
    BudgetMode,
    CallStatus,
    DataClassification,
    ErrorType,
    PayloadMode,
    PricingKind,
)
from ai_business_explorer.domain.errors import (
    BudgetExceededError,
    DomainValidationError,
    InvalidStateError,
    NotFoundError,
)
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    Budget,
    Execution,
    LLMCall,
    LLMCallPayload,
    Pricing,
    StageRun,
    ToolCall,
    ToolCallOutput,
)
from ai_business_explorer.infrastructure.db.repositories import (
    BudgetRepository,
    ExecutionRepository,
    ExplorationRepository,
    LLMCallRepository,
    ToolCallRepository,
)
from ai_business_explorer.llm.base import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMResponseError,
)
from ai_business_explorer.tools.base import Tool, ToolError, ToolResult

MONEY_QUANTUM = Decimal("0.00000001")
TOKENS_PER_UNIT = Decimal(1_000_000)
MAX_ERROR_MESSAGE = 2000
BUDGET_AUDIT_FIELDS = ["exploration_id", "monthly_limit", "currency", "mode"]


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM)


def month_range(month: date) -> tuple[datetime, datetime]:
    """UTC の暦月の [開始, 終了)。"""
    start = datetime(month.year, month.month, 1, tzinfo=UTC)
    following = (start + timedelta(days=32)).replace(day=1)
    return start, following


def current_month() -> date:
    now = utcnow()
    return date(now.year, now.month, 1)


def estimated_input_tokens(request: LLMRequest) -> int:
    """呼ぶ前の入力トークン数の見積もり（文字数をトークン数の目安にする。少なく見積もらない側）。

    system とメッセージに加えて、構造化出力のスキーマ（response_schema）も入力として数える。
    スキーマは API への入力になり、送る形（Claude のクライアントは response_schema が None で
    なければ送る）と同じ条件で数える。実際の費用は、呼んだ後に API の使用量で記録する。
    """
    chars = len(request.system) + sum(len(m.content) for m in request.messages)
    if request.response_schema is not None:
        chars += len(json.dumps(request.response_schema, ensure_ascii=False))
    return chars


def find_pricing(
    session: Session, kind: PricingKind, provider: str, model: str, at: datetime
) -> Pricing | None:
    """その時点で適用される単価（適用開始日時が最も新しいもの）。"""
    stmt = (
        select(Pricing)
        .where(
            Pricing.kind == kind.value,
            Pricing.provider == provider,
            Pricing.model == model,
            Pricing.effective_from <= at,
        )
        .order_by(Pricing.effective_from.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


@dataclass(frozen=True)
class ExecutionLimits:
    """1回の実行ごとの上限（AI社員の llm_config。未指定なら設定の既定値）。"""

    max_cost: Decimal
    max_llm_calls: int
    max_tool_calls: int
    max_tokens: int | None

    @classmethod
    def from_llm_config(cls, llm_config: dict[str, Any], settings: Settings) -> "ExecutionLimits":
        cost = llm_config.get("max_cost_per_execution")
        llm_calls = llm_config.get("max_llm_calls")
        tool_calls = llm_config.get("max_tool_calls")
        tokens = llm_config.get("max_tokens")
        return cls(
            max_cost=Decimal(str(cost)) if cost is not None else settings.execution_max_cost,
            max_llm_calls=int(llm_calls)
            if llm_calls is not None
            else settings.execution_max_llm_calls,
            max_tool_calls=int(tool_calls)
            if tool_calls is not None
            else settings.execution_max_tool_calls,
            max_tokens=int(tokens) if tokens is not None else None,
        )


@dataclass(frozen=True)
class BudgetStatus:
    """当月の予算の状況。"""

    budget_id: UUID | None  # 行がない組織の予算（設定の既定値）は None
    exploration_id: UUID | None
    monthly_limit: Decimal
    currency: str
    mode: BudgetMode
    spent: Decimal
    reserved: Decimal

    @property
    def remaining(self) -> Decimal:
        return self.monthly_limit - self.spent - self.reserved


class BudgetService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.budgets = BudgetRepository(session)
        self.explorations = ExplorationRepository(session)

    # ------------------------------------------------------------------ 集計

    def _budget_rows(self, organization_id: UUID, exploration_id: UUID | None) -> list[Budget]:
        rows = self.session.scalars(
            select(Budget).where(
                Budget.organization_id == organization_id,
                Budget.exploration_id.is_(None)
                if exploration_id is None
                else (Budget.exploration_id.is_(None) | (Budget.exploration_id == exploration_id)),
            )
        ).all()
        return list(rows)

    def statuses(
        self, organization_id: UUID, exploration_id: UUID | None, month: date | None = None
    ) -> list[BudgetStatus]:
        """組織の予算（行がなければ既定値）と、探索案件の予算（あれば）の当月の状況。"""
        month = month or current_month()
        rows = self._budget_rows(organization_id, exploration_id)
        org_row = next((r for r in rows if r.exploration_id is None), None)
        result = [
            self._status(
                organization_id,
                None,
                org_row.id if org_row else None,
                org_row.monthly_limit if org_row else self.settings.organization_monthly_budget,
                org_row.currency if org_row else self.settings.budget_currency,
                BudgetMode(org_row.mode if org_row else self.settings.organization_budget_mode),
                month,
            )
        ]
        for row in rows:
            if row.exploration_id is not None:
                result.append(
                    self._status(
                        organization_id,
                        row.exploration_id,
                        row.id,
                        row.monthly_limit,
                        row.currency,
                        BudgetMode(row.mode),
                        month,
                    )
                )
        return result

    def all_statuses(self, organization_id: UUID, month: date) -> list[BudgetStatus]:
        """組織の予算と、予算を設定したすべての探索案件の状況。"""
        result = self.statuses(organization_id, None, month)
        rows = self.session.scalars(
            select(Budget)
            .where(Budget.organization_id == organization_id, Budget.exploration_id.is_not(None))
            .order_by(Budget.created_at, Budget.id)
        ).all()
        for row in rows:
            result.append(
                self._status(
                    organization_id,
                    row.exploration_id,
                    row.id,
                    row.monthly_limit,
                    row.currency,
                    BudgetMode(row.mode),
                    month,
                )
            )
        return result

    def _status(
        self,
        organization_id: UUID,
        exploration_id: UUID | None,
        budget_id: UUID | None,
        monthly_limit: Decimal,
        currency: str,
        mode: BudgetMode,
        month: date,
    ) -> BudgetStatus:
        return BudgetStatus(
            budget_id=budget_id,
            exploration_id=exploration_id,
            monthly_limit=monthly_limit,
            currency=currency,
            mode=mode,
            spent=self.spent(organization_id, exploration_id, currency, month),
            reserved=self.reserved(organization_id, exploration_id, currency),
        )

    def _calls(self, organization_id: UUID, exploration_id: UUID | None) -> Select[Any]:
        """LLM・Tool の呼び出し（費用・通貨・日時・実行・探索案件）を1つにまとめる。"""

        def part(model: type[LLMCall] | type[ToolCall], kind: str) -> Select[Any]:
            stmt = (
                select(
                    literal(kind).label("kind"),
                    model.cost_amount.label("cost_amount"),
                    model.currency.label("currency"),
                    model.created_at.label("created_at"),
                    StageRun.exploration_id.label("exploration_id"),
                )
                .join(Execution, Execution.id == model.execution_id)
                .join(StageRun, StageRun.id == Execution.stage_run_id)
                .where(model.organization_id == organization_id)
            )
            if exploration_id is not None:
                stmt = stmt.where(StageRun.exploration_id == exploration_id)
            return stmt

        return union_all(part(LLMCall, "llm"), part(ToolCall, "tool")).subquery()  # type: ignore[return-value]

    def spent(
        self, organization_id: UUID, exploration_id: UUID | None, currency: str, month: date
    ) -> Decimal:
        start, end = month_range(month)
        calls = self._calls(organization_id, exploration_id)
        total = self.session.execute(
            select(func.coalesce(func.sum(calls.c.cost_amount), 0)).where(
                calls.c.currency == currency,
                calls.c.created_at >= start,
                calls.c.created_at < end,
            )
        ).scalar_one()
        return Decimal(total)

    def reserved(
        self, organization_id: UUID, exploration_id: UUID | None, currency: str
    ) -> Decimal:
        """待機中・実行中の実行が、これから使いうる額（上限 − 使った額）。"""
        stmt = (
            select(
                func.coalesce(
                    func.sum(func.greatest(Execution.cost_limit - Execution.cost_amount, 0)), 0
                )
            )
            .join(StageRun, StageRun.id == Execution.stage_run_id)
            .where(
                Execution.organization_id == organization_id,
                Execution.status.in_([s.value for s in ACTIVE_RUN_STATUSES]),
                Execution.cost_currency == currency,
                Execution.cost_limit.is_not(None),
            )
        )
        if exploration_id is not None:
            stmt = stmt.where(StageRun.exploration_id == exploration_id)
        return Decimal(self.session.execute(stmt).scalar_one())

    # ------------------------------------------------------------------ 確認

    def check_launch(
        self, organization_id: UUID, exploration_id: UUID, needed: Decimal, currency: str
    ) -> None:
        """新しい実行の上限の合計（needed）を、残りの予算で賄えるか（hard のみ止める）。"""
        for status in self.statuses(organization_id, exploration_id):
            if status.currency != currency:
                # 通貨が違うと費用を予算に計上できず、上限を守れない
                raise InvalidStateError(
                    f"pricing currency '{currency}' differs from the budget currency "
                    f"'{status.currency}'"
                )
            if status.mode is BudgetMode.HARD and status.remaining < needed:
                raise BudgetExceededError(
                    f"{_scope(status)} budget remaining {money(status.remaining)} {currency} "
                    f"is less than the execution limit {money(needed)} {currency}"
                )

    def check_running(self, organization_id: UUID, exploration_id: UUID) -> None:
        """実行中：当月の費用が上限に達していれば、以降の呼び出しを止める（hard のみ）。"""
        for status in self.statuses(organization_id, exploration_id):
            if status.mode is BudgetMode.HARD and status.spent >= status.monthly_limit:
                raise BudgetExceededError(
                    f"{_scope(status)} monthly budget {money(status.monthly_limit)} "
                    f"{status.currency} is used up"
                )

    # ------------------------------------------------------------------ 設定（admin）

    def list(self, page: PageRequest) -> Page[Budget]:
        return paginate(
            self.session,
            self.budgets.select(),
            sort_column=Budget.created_at,
            id_column=Budget.id,
            page=page,
        )

    def set(self, actor: Actor, cmd: BudgetSet) -> Budget:
        """組織（exploration_id なし）または探索案件の予算を作成・更新する。"""
        require_human(actor, "set budgets")
        organization_id = current_organization_id(self.session)
        if cmd.exploration_id is not None:
            self.explorations.get_or_raise(cmd.exploration_id)
        existing = self.session.scalars(
            select(Budget).where(
                Budget.organization_id == organization_id,
                Budget.exploration_id.is_(None)
                if cmd.exploration_id is None
                else Budget.exploration_id == cmd.exploration_id,
            )
        ).first()
        before = snapshot(existing, BUDGET_AUDIT_FIELDS) if existing else None
        budget = existing or Budget(
            organization_id=organization_id, exploration_id=cmd.exploration_id
        )
        budget.monthly_limit = cmd.monthly_limit
        budget.currency = cmd.currency
        budget.mode = cmd.mode.value
        if existing is None:
            self.budgets.add(budget)
        record_audit(
            self.session,
            organization_id=organization_id,
            entity_type="budget",
            entity_id=budget.id,
            action="updated" if existing else "created",
            actor_id=actor.id,
            before=before,
            after=snapshot(budget, BUDGET_AUDIT_FIELDS),
        )
        self.session.commit()
        return budget

    def delete(self, actor: Actor, budget_id: UUID) -> None:
        """予算の行を消す（組織の予算は設定の既定値に戻る）。"""
        require_human(actor, "delete budgets")
        budget = self.budgets.get_or_raise(budget_id)
        record_audit(
            self.session,
            organization_id=budget.organization_id,
            entity_type="budget",
            entity_id=budget.id,
            action="deleted",
            actor_id=actor.id,
            before=snapshot(budget, BUDGET_AUDIT_FIELDS),
        )
        self.session.delete(budget)
        self.session.commit()


def _scope(status: BudgetStatus) -> str:
    if status.exploration_id is None:
        return "organization"
    return f"exploration {status.exploration_id}"


# ---------------------------------------------------------------------- 実行中の計測


class ExecutionMeter:
    """1回の実行（execution）の LLM・Tool 呼び出しを計測・記録し、上限で止める。

    記録は呼び出しのたびにコミットする（後で実行が失敗・取り消しになっても費用は残る）。
    AI社員の実行中、このセッションには他の未確定の変更がない（ステージ実行側で保証）。
    """

    def __init__(
        self,
        session: Session,
        settings: Settings,
        execution: Execution,
        exploration_id: UUID,
        classification: DataClassification,
    ) -> None:
        self.session = session
        self.settings = settings
        self.execution = execution
        self.exploration_id = exploration_id
        self.classification = classification
        self.limits = ExecutionLimits.from_llm_config(
            execution.ai_employee_snapshot.get("llm_config") or {}, settings
        )
        self.budgets = BudgetService(session, settings)
        self.llm_call_count = 0
        self.tool_call_count = 0
        self.tool_call_counts: dict[str, int] = {}
        # confidential 以上の本文は保存しない。設定で全体を none にもできる（R-16）
        sensitive = classification.rank >= DataClassification.CONFIDENTIAL.rank
        self.payload_mode = (
            PayloadMode.NONE
            if sensitive or PayloadMode(settings.llm_payload_mode) is PayloadMode.NONE
            else PayloadMode.FULL
        )

    @property
    def cost_limit(self) -> Decimal:
        limit = self.execution.cost_limit
        return limit if limit is not None else self.limits.max_cost

    def _check_cost(self) -> None:
        if self.execution.cost_amount >= self.cost_limit:
            raise BudgetExceededError(
                f"execution cost {money(self.execution.cost_amount)} reached its limit "
                f"{money(self.cost_limit)} {self.execution.cost_currency}"
            )
        self.budgets.check_running(self.execution.organization_id, self.exploration_id)

    def _add_cost(self, amount: Decimal) -> None:
        self.execution.cost_amount = money(self.execution.cost_amount + amount)

    def _check_cost_after_call(self) -> None:
        if self.execution.cost_amount > self.cost_limit:
            raise BudgetExceededError(
                f"execution cost {money(self.execution.cost_amount)} exceeded its limit "
                f"{money(self.cost_limit)} {self.execution.cost_currency}"
            )

    # ------------------------------------------------------------------ LLM

    def wrap(self, inner: LLMClient) -> LLMClient:
        return _MeteredLLMClient(self, inner)

    def before_llm_call(self, request: LLMRequest, provider: str) -> LLMRequest:
        if self.llm_call_count >= self.limits.max_llm_calls:
            raise BudgetExceededError(
                f"LLM call limit {self.limits.max_llm_calls} per execution reached"
            )
        self._check_cost()
        caps = [c for c in (request.max_tokens, self.limits.max_tokens) if c is not None]
        affordable = self._affordable_output_tokens(request, provider)
        if affordable is not None:
            if affordable < 1:
                raise BudgetExceededError(
                    "the remaining execution budget cannot cover another LLM call"
                )
            caps.append(affordable)
        self.llm_call_count += 1
        if caps:
            request = request.model_copy(update={"max_tokens": min(caps)})
        return request

    def _affordable_output_tokens(self, request: LLMRequest, provider: str) -> int | None:
        """残りの費用上限で払える出力トークン数（単価が 0 なら制限しない）。

        1回の呼び出しで上限を超えないよう、出力の最大数を絞る。入力は文字数をトークン数の上限の
        目安として見積もる（少なく見積もらない側に倒す）。
        """
        pricing = find_pricing(self.session, PricingKind.LLM, provider, request.model, utcnow())
        if pricing is None or pricing.output_per_million_tokens <= 0:
            return None
        tokens = estimated_input_tokens(request)
        input_cost = (
            Decimal(tokens) * pricing.input_per_million_tokens / TOKENS_PER_UNIT + pricing.per_call
        )
        remaining = self.cost_limit - self.execution.cost_amount - input_cost
        return int(remaining * TOKENS_PER_UNIT / pricing.output_per_million_tokens)

    def record_llm_call(
        self,
        provider: str,
        request: LLMRequest,
        response: LLMResponse | None,
        error: BaseException | None,
        latency_ms: int,
    ) -> None:
        model = (response.model if response else None) or request.model
        pricing = find_pricing(self.session, PricingKind.LLM, provider, model, utcnow())
        if pricing is None and model != request.model:
            pricing = find_pricing(self.session, PricingKind.LLM, provider, request.model, utcnow())
        input_tokens = response.usage.input_tokens if response else 0
        output_tokens = response.usage.output_tokens if response else 0
        cost = Decimal(0)
        if pricing is not None:
            cost = money(
                Decimal(input_tokens) * pricing.input_per_million_tokens / TOKENS_PER_UNIT
                + Decimal(output_tokens) * pricing.output_per_million_tokens / TOKENS_PER_UNIT
                + pricing.per_call
            )
        call = LLMCall(
            organization_id=self.execution.organization_id,
            execution_id=self.execution.id,
            provider=provider,
            model=model,
            prompt_key=request.prompt_key,
            prompt_version=request.prompt_version,
            prompt_hash=self.execution.prompt_hash,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_amount=cost,
            currency=pricing.currency if pricing else self.execution.cost_currency,
            pricing_id=pricing.id if pricing else None,
            latency_ms=latency_ms,
            status=(CallStatus.FAILED if error else CallStatus.SUCCEEDED).value,
            error_type=_call_error_type(error),
            error_message=str(error)[:MAX_ERROR_MESSAGE] if error else None,
            provider_request_id=response.request_id if response else None,
            classification=self.classification.value,
            payload_mode=self.payload_mode.value,
        )
        self.session.add(call)
        self.session.flush()
        if self.payload_mode is PayloadMode.FULL:
            # 送った内容と応答だけを残す。API キー・認証ヘッダーなどは持たない（記録しない）
            self.session.add(
                LLMCallPayload(
                    organization_id=call.organization_id,
                    llm_call_id=call.id,
                    request=request.model_dump(
                        mode="json",
                        include={
                            "model",
                            "system",
                            "messages",
                            "prompt_key",
                            "prompt_version",
                            "response_schema",
                            "temperature",
                            "max_tokens",
                        },
                    ),
                    response=response.model_dump(
                        mode="json", include={"model", "text", "structured", "finish_reason"}
                    )
                    if response
                    else None,
                )
            )
        self._add_cost(cost)
        self.session.commit()
        if pricing is None and error is None:
            # 単価が分からないと予算を守れないので、以降の呼び出しを止める
            raise BudgetExceededError(f"no pricing for LLM {provider}/{model}")

    # ------------------------------------------------------------------ Tool

    def before_tool_call(self, tool: Tool) -> None:
        if self.tool_call_count >= self.limits.max_tool_calls:
            raise BudgetExceededError(
                f"tool call limit {self.limits.max_tool_calls} per execution reached"
            )
        used = self.tool_call_counts.get(tool.name, 0)
        if tool.max_calls_per_execution is not None and used >= tool.max_calls_per_execution:
            # Tool ごとの上限（Web 取得は1実行 20件。R-20）
            raise ToolError(
                f"tool '{tool.name}' call limit {tool.max_calls_per_execution} "
                "per execution reached"
            )
        self._check_cost()
        self.tool_call_count += 1
        self.tool_call_counts[tool.name] = used + 1

    def after_tool_call(
        self,
        tool: Tool,
        tool_input: dict[str, Any],
        result: ToolResult | None,
        error: BaseException | None,
        latency_ms: int,
    ) -> ToolResult | None:
        # Tool の単価がなければ費用 0（内部の読み取り専用 Tool。外部 Tool は単価を登録する）
        pricing = find_pricing(self.session, PricingKind.TOOL, tool.name, "", utcnow())
        cost = money(pricing.per_call) if pricing else Decimal(0)
        call = ToolCall(
            organization_id=self.execution.organization_id,
            execution_id=self.execution.id,
            tool_name=tool.name,
            tool_version=tool.version,
            side_effect=tool.side_effect.value,
            input=tool_input,
            urls=[c.url for c in result.evidence_candidates if c.url] if result else [],
            cost_amount=cost,
            currency=pricing.currency if pricing else self.execution.cost_currency,
            pricing_id=pricing.id if pricing else None,
            latency_ms=latency_ms,
            status=(CallStatus.FAILED if error else CallStatus.SUCCEEDED).value,
            error_type=_call_error_type(error),
            error_message=str(error)[:MAX_ERROR_MESSAGE] if error else None,
        )
        self.session.add(call)
        self.session.flush()
        recorded: ToolResult | None = None
        if result is not None:
            # 生の出力は本文として別に保存し（90日で消す）、取得した情報は Evidence 候補にする
            self.session.add(
                ToolCallOutput(
                    organization_id=call.organization_id,
                    tool_call_id=call.id,
                    output=to_jsonable(result.output),
                )
            )
            recorded = store_tool_candidates(
                self.session, self.execution, self.exploration_id, call, result
            )
        self._add_cost(cost)
        self.session.commit()
        if error is None:
            self._check_cost_after_call()
        return recorded


class _MeteredLLMClient:
    """実際の LLM クライアントを包み、呼び出しの前に上限を確認し、後に記録する。"""

    def __init__(self, meter: ExecutionMeter, inner: LLMClient) -> None:
        self._meter = meter
        self._inner = inner

    @property
    def provider(self) -> str:
        return self._inner.provider

    def complete(self, request: LLMRequest) -> LLMResponse:
        request = self._meter.before_llm_call(request, self.provider)
        started = time.monotonic()
        try:
            response = self._inner.complete(request)
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            # 応答が返っていれば（断られた・途中で切れた）その使用量で費用を記録する（E-07）
            returned = exc.response if isinstance(exc, LLMResponseError) else None
            self._meter.record_llm_call(self.provider, request, returned, exc, elapsed)
            raise
        elapsed = int((time.monotonic() - started) * 1000)
        self._meter.record_llm_call(self.provider, request, response, None, elapsed)
        self._meter._check_cost_after_call()
        return response


def _call_error_type(error: BaseException | None) -> str | None:
    if error is None:
        return None
    if isinstance(error, LLMError):
        return ErrorType.LLM_ERROR.value
    if isinstance(error, ToolError):
        return ErrorType.TOOL_ERROR.value
    if isinstance(error, TimeoutError):
        return ErrorType.TIMEOUT.value
    return ErrorType.UNEXPECTED.value


# ---------------------------------------------------------------------- 参照（集計・ログ）


@dataclass(frozen=True)
class CostLine:
    exploration_id: UUID | None
    currency: str
    llm_amount: Decimal
    tool_amount: Decimal
    llm_calls: int
    tool_calls: int

    @property
    def amount(self) -> Decimal:
        return self.llm_amount + self.tool_amount


class CostService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.llm_calls = LLMCallRepository(session)
        self.tool_calls = ToolCallRepository(session)
        self.executions = ExecutionRepository(session)

    def monthly(self, month: date) -> tuple[list[CostLine], list[CostLine]]:
        """当月の費用。組織の合計（通貨ごと）と、探索案件ごと。"""
        organization_id = current_organization_id(self.session)
        start, end = month_range(month)
        calls = BudgetService(self.session, self.settings)._calls(organization_id, None)
        window = (calls.c.created_at >= start, calls.c.created_at < end)

        def amount(kind: str) -> Any:
            return func.coalesce(func.sum(calls.c.cost_amount).filter(calls.c.kind == kind), 0)

        def count(kind: str) -> Any:
            return func.count().filter(calls.c.kind == kind)

        columns = (amount("llm"), amount("tool"), count("llm"), count("tool"))
        totals = self.session.execute(
            select(calls.c.currency, *columns)
            .where(*window)
            .group_by(calls.c.currency)
            .order_by(calls.c.currency)
        ).all()
        per_exploration = self.session.execute(
            select(calls.c.exploration_id, calls.c.currency, *columns)
            .where(*window)
            .group_by(calls.c.exploration_id, calls.c.currency)
            .order_by(calls.c.exploration_id, calls.c.currency)
        ).all()
        return (
            [CostLine(None, r[0], Decimal(r[1]), Decimal(r[2]), r[3], r[4]) for r in totals],
            [
                CostLine(r[0], r[1], Decimal(r[2]), Decimal(r[3]), r[4], r[5])
                for r in per_exploration
            ],
        )

    def llm_calls_for(self, execution_id: UUID, page: PageRequest) -> Page[LLMCall]:
        self.executions.get_or_raise(execution_id)
        return paginate(
            self.session,
            self.llm_calls.select(LLMCall.execution_id == execution_id),
            sort_column=LLMCall.created_at,
            id_column=LLMCall.id,
            page=page,
        )

    def tool_calls_for(self, execution_id: UUID, page: PageRequest) -> Page[ToolCall]:
        self.executions.get_or_raise(execution_id)
        return paginate(
            self.session,
            self.tool_calls.select(ToolCall.execution_id == execution_id),
            sort_column=ToolCall.created_at,
            id_column=ToolCall.id,
            page=page,
        )

    def payload(self, llm_call_id: UUID) -> tuple[LLMCall, LLMCallPayload]:
        """LLM ログの本文（admin のみ）。保存しなかった・消した本文は 404。"""
        call = self.llm_calls.get_or_raise(llm_call_id)
        payload = self.session.get(LLMCallPayload, call.id)
        if payload is None:
            raise NotFoundError(
                f"llm_call {call.id} has no stored payload (payload_mode={call.payload_mode})"
            )
        return call, payload


# ---------------------------------------------------------------------- 保存期間（R-20）


def delete_expired_payloads(session: Session, retention_days: int) -> int:
    """保存期間を過ぎた LLM ログの本文を消す。メタデータ（llm_calls）は残す。"""
    if retention_days < 1:
        raise DomainValidationError("retention_days must be at least 1")
    threshold = utcnow() - timedelta(days=retention_days)
    expired = session.scalars(
        select(LLMCallPayload).where(LLMCallPayload.created_at < threshold)
    ).all()
    now = utcnow()
    for payload in expired:
        call = session.get(LLMCall, payload.llm_call_id)
        if call is not None:
            call.payload_deleted_at = now
        session.delete(payload)
    session.commit()
    return len(expired)
