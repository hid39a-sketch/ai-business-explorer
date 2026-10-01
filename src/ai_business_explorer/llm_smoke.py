"""Claude API の接続確認（手動の GitHub Actions ワークフロー llm-smoke.yml からだけ使う）。

起動: LLM_SMOKE_CONFIRM=yes LLM_API_KEY=... uv run python -m ai_business_explorer.llm_smoke

- 実際の API を1回だけ呼ぶ（再試行なし）。送るのは固定の短い文だけで、業務データは送らない。
- 呼ぶ前に、最悪の場合の費用（入力の見積もり＋max_tokens 分の出力）が上限（SMOKE_MAX_COST_USD）
  以下であることを確かめ、呼んだ後も実際の費用を確かめる。
- API キーは表示しない。表示するのはモデル・停止理由・トークン数・費用・リクエストIDだけ。
- 通常のテストと CI からは呼ばない（LLM_SMOKE_CONFIRM=yes がないと何もしないで終わる）。
"""

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


def worst_case_cost(max_tokens: int) -> Decimal:
    # 入力は文字数をトークン数の上限の目安にする（少なく見積もらない）
    input_tokens = len(SYSTEM) + len(PROMPT)
    return (Decimal(input_tokens) * INPUT_PRICE + Decimal(max_tokens) * OUTPUT_PRICE) / MILLION


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
        response = client.complete(
            LLMRequest(
                model=CLAUDE_DEFAULT_MODEL,
                system=SYSTEM,
                messages=[LLMMessage(role="user", content=PROMPT)],
                prompt_key="llm_smoke",
                prompt_version="v1",
                max_tokens=SMOKE_MAX_TOKENS,
                timeout_seconds=SMOKE_TIMEOUT_SECONDS,
            )
        )
    except LLMResponseError as exc:
        usage = exc.response.usage
        print(f"FAILED: {exc} (tokens in={usage.input_tokens} out={usage.output_tokens})")
        return 1
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 1
    cost = cost_of(response.usage.input_tokens, response.usage.output_tokens)
    print(f"model={response.model} finish_reason={response.finish_reason}")
    print(f"tokens in={response.usage.input_tokens} out={response.usage.output_tokens}")
    print(f"cost_usd={cost:.6f} (cap {SMOKE_MAX_COST_USD}) request_id={response.request_id}")
    print(f"reply_chars={len(response.text)}")
    if cost > SMOKE_MAX_COST_USD:
        print("FAILED: the call cost more than the cap", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
