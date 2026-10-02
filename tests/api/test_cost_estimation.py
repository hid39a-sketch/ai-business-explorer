"""呼ぶ前の入力トークンの見積もりに、構造化出力のスキーマ（response_schema）を含める（案Ⅱ）。

見積もり＝system の文字数＋メッセージの文字数＋スキーマ（JSON）の文字数。見積もりは呼ぶ前に
max_tokens を残りの予算で払える数に絞るためだけに使い、実際の費用は API の使用量で記録する。
予算の予約は実行の上限額で計算し、見積もりは使わない。実際の LLM には接続しない。
"""

import json
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from ai_business_explorer.application.costs import (
    TOKENS_PER_UNIT,
    BudgetService,
    ExecutionMeter,
    estimated_input_tokens,
)
from ai_business_explorer.config import Settings
from ai_business_explorer.llm.base import LLMMessage, LLMRequest, LLMResponse, LLMUsage
from ai_business_explorer.llm.claude import CLAUDE_DEFAULT_MODEL, CLAUDE_PROVIDER
from tests.api.test_costs import _meter
from tests.conftest import Api

SYSTEM = "指示" * 50
USER = '{"data": "' + "あ" * 300 + '"}'
# claude-opus-5-5 の単価（seed）：入力 $4・出力 $20 / 100万トークン
INPUT_PRICE = Decimal(4)
OUTPUT_PRICE = Decimal(20)
# 出力の上限（16,000）より少ない数しか払えないように、実行の上限を小さくする
COST_LIMIT = Decimal("0.01")


def _request(schema: dict[str, Any] | None = None) -> LLMRequest:
    return LLMRequest(
        model=CLAUDE_DEFAULT_MODEL,
        system=SYSTEM,
        messages=[LLMMessage(role="user", content=USER)],
        prompt_key="test",
        prompt_version="v1",
        response_schema=schema,
    )


def _schema(properties: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {f"field_{i}": {"type": "string"} for i in range(properties)},
        "additionalProperties": False,
    }


def _limited_meter(session: Session, api: Api) -> ExecutionMeter:
    return _meter(session, api, max_cost_per_execution=str(COST_LIMIT))


def _expected_max_tokens(tokens: int) -> int:
    input_cost = Decimal(tokens) * INPUT_PRICE / TOKENS_PER_UNIT
    return int((COST_LIMIT - input_cost) * TOKENS_PER_UNIT / OUTPUT_PRICE)


def test_without_schema_the_estimate_is_unchanged() -> None:
    assert estimated_input_tokens(_request()) == len(SYSTEM) + len(USER)


def test_schema_size_is_added_to_the_estimate() -> None:
    schema = _schema(3)
    size = len(json.dumps(schema, ensure_ascii=False))
    assert estimated_input_tokens(_request(schema)) == len(SYSTEM) + len(USER) + size


def test_larger_schema_means_a_larger_estimate() -> None:
    small, large = _request(_schema(2)), _request(_schema(40))
    assert estimated_input_tokens(large) > estimated_input_tokens(small)


def test_empty_and_missing_schema() -> None:
    """None は数えない。空の dict は送られる（送る条件は None でないこと）ので数える。"""
    assert estimated_input_tokens(_request(None)) == len(SYSTEM) + len(USER)
    assert estimated_input_tokens(_request({})) == len(SYSTEM) + len(USER) + len("{}")


def test_max_tokens_is_capped_using_the_estimate_with_schema(api: Api, session: Session) -> None:
    meter = _limited_meter(session, api)
    assert meter.cost_limit == COST_LIMIT
    without = meter.before_llm_call(_request(), CLAUDE_PROVIDER)
    with_schema = meter.before_llm_call(_request(_schema(40)), CLAUDE_PROVIDER)
    assert without.max_tokens == _expected_max_tokens(len(SYSTEM) + len(USER))
    assert with_schema.max_tokens == _expected_max_tokens(
        estimated_input_tokens(_request(_schema(40)))
    )
    # スキーマの分だけ入力の見積もりが増え、払える出力の数は減る
    assert with_schema.max_tokens is not None
    assert without.max_tokens is not None
    assert with_schema.max_tokens < without.max_tokens


class _Reply:
    """API の使用量（見積もりと違う値）を返す、実際の API には接続しないクライアント。"""

    provider = CLAUDE_PROVIDER

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            provider=CLAUDE_PROVIDER,
            model=CLAUDE_DEFAULT_MODEL,
            text="{}",
            structured={},
            usage=LLMUsage(input_tokens=1234, output_tokens=56),
        )


def test_recorded_cost_and_reservation_do_not_use_the_estimate(
    api: Api, session: Session, settings: Settings
) -> None:
    meter = _limited_meter(session, api)
    before = meter.execution.cost_amount
    meter.wrap(_Reply()).complete(_request(_schema(40)))
    # 記録する費用は API の使用量から計算する（見積もりではない）
    actual = (Decimal(1234) * INPUT_PRICE + Decimal(56) * OUTPUT_PRICE) / TOKENS_PER_UNIT
    assert meter.execution.cost_amount - before == actual
    # 予算の予約は「実行の上限 − 使った額」で計算する（見積もりは使わない）
    execution = meter.execution
    execution.status = "running"
    session.flush()
    reserved = BudgetService(session, settings).reserved(
        execution.organization_id, meter.exploration_id, execution.cost_currency
    )
    assert reserved == COST_LIMIT - execution.cost_amount
    session.rollback()
