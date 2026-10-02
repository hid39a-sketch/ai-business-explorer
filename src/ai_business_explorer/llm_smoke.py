"""Claude API の接続確認（手動の GitHub Actions ワークフロー llm-smoke.yml からだけ使う）。

起動: LLM_SMOKE_CONFIRM=yes LLM_API_KEY=... uv run python -m ai_business_explorer.llm_smoke

- 実際の API を2回だけ呼ぶ（再試行なし）。1回目は通常の呼び出し、2回目は最小の構造化出力
  （output_config.format＝json_schema。{"ok": true} を返させる）で、本番と同じ ClaudeLLMClient の
  経路を確かめる。2回目の structured が {"ok": true} でなければ失敗。送るのは固定の短い文だけで、
  業務データは送らない。
- 呼ぶ前に、2回分の最悪の場合の費用（入力の見積もり＋max_tokens 分の出力）の合計が上限
  （SMOKE_MAX_COST_USD）以下であることを確かめ、呼んだ後も実際の費用の合計を確かめる。
- API キーは表示しない。表示するのはモデル・停止理由・トークン数・費用・リクエストIDだけ。
- 通常のテストと CI からは呼ばない（LLM_SMOKE_CONFIRM=yes がないと何もしないで終わる）。
"""

import json
import os
import sys
from decimal import Decimal

from ai_business_explorer.config import Settings
from ai_business_explorer.llm.base import LLMError, LLMMessage, LLMRequest, LLMResponseError
from ai_business_explorer.llm.claude import CLAUDE_DEFAULT_MODEL, ClaudeLLMClient

SMOKE_MAX_COST_USD = Decimal("0.05")
SMOKE_MAX_TOKENS = 1024
SMOKE_TIMEOUT_SECONDS = 60.0
# claude-opus-5-5 の公開価格（USD / 100万トークン）。seed の単価と同じ
INPUT_PRICE = Decimal(4)
OUTPUT_PRICE = Decimal(20)
MILLION = Decimal(1_000_000)
SYSTEM = 'You are a connection check. Reply with exactly this JSON and nothing else: {"ok": true}'
PROMPT = '{"check": "connectivity"}'
# 2回目（構造化出力）の指示とスキーマ。応答は {"ok": true} だけ
STRUCTURED_SYSTEM = "You are a connection check. Return ok set to true."
STRUCTURED_PROMPT = '{"check": "structured_output"}'
SMOKE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def worst_case_cost(max_tokens: int) -> Decimal:
    """2回分の最悪の場合の費用の合計。入力は文字数をトークン数の上限の目安にする（スキーマも数える）。"""
    input_tokens = (
        len(SYSTEM)
        + len(PROMPT)
        + len(STRUCTURED_SYSTEM)
        + len(STRUCTURED_PROMPT)
        + len(json.dumps(SMOKE_SCHEMA))
    )
    return (Decimal(input_tokens) * INPUT_PRICE + 2 * Decimal(max_tokens) * OUTPUT_PRICE) / MILLION


def cost_of(input_tokens: int, output_tokens: int) -> Decimal:
    return (Decimal(input_tokens) * INPUT_PRICE + Decimal(output_tokens) * OUTPUT_PRICE) / MILLION


def main() -> int:
    if os.environ.get("LLM_SMOKE_CONFIRM") != "yes":
        print("LLM_SMOKE_CONFIRM=yes is not set; nothing was sent.")
        return 0
    settings = Settings()
    if settings.app_env == "test":
        print("refusing to call a real LLM with APP_ENV=test", file=sys.stderr)
        return 2
    estimate = worst_case_cost(SMOKE_MAX_TOKENS)
    if estimate > SMOKE_MAX_COST_USD:
        print(f"worst-case cost {estimate} USD exceeds the cap", file=sys.stderr)
        return 2
    try:
        client = ClaudeLLMClient(settings.llm_api_key)
        response = client.complete(_request(SYSTEM, PROMPT, None))
        structured = client.complete(_request(STRUCTURED_SYSTEM, STRUCTURED_PROMPT, SMOKE_SCHEMA))
    except LLMResponseError as exc:
        usage = exc.response.usage
        print(f"FAILED: {exc} (tokens in={usage.input_tokens} out={usage.output_tokens})")
        return 1
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 1
    total = Decimal(0)
    for label, result in (("plain", response), ("structured", structured)):
        cost = cost_of(result.usage.input_tokens, result.usage.output_tokens)
        total += cost
        print(f"[{label}] model={result.model} finish_reason={result.finish_reason}")
        print(f"[{label}] tokens in={result.usage.input_tokens} out={result.usage.output_tokens}")
        print(f"[{label}] cost_usd={cost:.6f} request_id={result.request_id}")
    print(f"reply_chars={len(response.text)}")
    structured_ok = structured.structured == {"ok": True}
    print(f"structured_ok={structured_ok}")
    print(f"total_cost_usd={total:.6f} (cap {SMOKE_MAX_COST_USD})")
    if total > SMOKE_MAX_COST_USD:
        print("FAILED: the calls cost more than the cap", file=sys.stderr)
        return 1
    if not structured_ok:
        print('FAILED: the structured output was not {"ok": true}', file=sys.stderr)
        return 1
    print("OK")
    return 0


def _request(system: str, prompt: str, schema: dict[str, object] | None) -> LLMRequest:
    return LLMRequest(
        model=CLAUDE_DEFAULT_MODEL,
        system=system,
        messages=[LLMMessage(role="user", content=prompt)],
        prompt_key="llm_smoke",
        prompt_version="v1",
        response_schema=schema,
        max_tokens=SMOKE_MAX_TOKENS,
        timeout_seconds=SMOKE_TIMEOUT_SECONDS,
    )


if __name__ == "__main__":
    sys.exit(main())
