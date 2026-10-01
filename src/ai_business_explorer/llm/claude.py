"""Anthropic Claude API のクライアント（第2回仕様 R-03・PR-9）。

LLMClient の実装の1つ。AI社員・ドメインは llm/base.py の型だけを使い、SDK の型はこのモジュールの
外に出さない。

安全のための決まり：
- API キーは設定（LLM_API_KEY、SecretStr）からだけ受け取り、SDK のクライアントに直接渡す。環境の
  他の認証情報（ANTHROPIC_API_KEY・ログイン済みのプロファイルなど）は使わない。キーはログ・例外の
  メッセージ・LLM ログの本文に入れない。
- SDK の自動再試行はしない（max_retries=0）。1回の呼び出し＝1回の記録・計上にして、呼び出し回数と
  費用の上限（第2回仕様 10章）を正しく効かせるため。
- 呼び出しの上限秒数（R-20 の LLM 1回 120秒）は LLMRequest.timeout_seconds で受け取る。
- 送ってよいデータ分類の確認は、呼び出しの前にステージ実行側で行う（11章）。このモジュールは
  渡されたものを送るだけ。
"""

from collections.abc import Callable
from typing import Any

import anthropic
from pydantic import SecretStr

from ai_business_explorer.llm.base import (
    LLMError,
    LLMProviderNotAvailableError,
    LLMRequest,
    LLMResponse,
    LLMResponseError,
    LLMUsage,
)

CLAUDE_PROVIDER = "anthropic"
CLAUDE_DEFAULT_MODEL = "claude-opus-5-5"
# 指定がないときの出力上限（非ストリーミングで HTTP のタイムアウトにかからない範囲）
DEFAULT_MAX_TOKENS = 16_000
MAX_ERROR_DETAIL = 500


# SDK のクライアント（テストでは messages.create と with_options を持つ Fake に差し替える）
ClientFactory = Callable[[str], Any]


def _sdk_client(api_key: str) -> Any:
    return anthropic.Anthropic(api_key=api_key, max_retries=0)


class ClaudeLLMClient:
    """Messages API を1回呼ぶ。Tool は ToolBox 経由（API の tool use は使わない）。"""

    def __init__(
        self, api_key: SecretStr | None, client_factory: ClientFactory = _sdk_client
    ) -> None:
        if api_key is None or not api_key.get_secret_value().strip():
            raise LLMProviderNotAvailableError(
                "LLM provider 'anthropic' is not configured (LLM_API_KEY is not set)"
            )
        self._client = client_factory(api_key.get_secret_value())

    @property
    def provider(self) -> str:
        return CLAUDE_PROVIDER

    def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._client
        if request.timeout_seconds is not None:
            client = client.with_options(timeout=request.timeout_seconds)
        params: dict[str, Any] = {
            "model": request.model,
            # 非ストリーミングの上限（SDK の HTTP タイムアウトにかからない範囲）を超えない
            "max_tokens": min(request.max_tokens or DEFAULT_MAX_TOKENS, DEFAULT_MAX_TOKENS),
            # 指示（system）と、外部由来のデータを含む入力（user の JSON）を分けて渡す
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        try:
            message = client.messages.create(**params)
        except anthropic.APITimeoutError as exc:
            raise LLMError("Claude API request timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Claude API connection failed") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(_status_error_message(exc)) from exc
        except anthropic.AnthropicError as exc:
            raise LLMError(f"Claude API error: {exc.__class__.__name__}") from exc
        return _to_response(message, request)


def _status_error_message(exc: anthropic.APIStatusError) -> str:
    """状態コードとエラーの種類・本文だけを返す（ヘッダー・キーは含めない）。"""
    detail = ""
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            detail = f"{error.get('type', '')}: {error.get('message', '')}"
    request_id = getattr(exc, "request_id", None)
    suffix = f" (request_id={request_id})" if request_id else ""
    return f"Claude API returned {exc.status_code} {detail[:MAX_ERROR_DETAIL]}{suffix}".strip()


def _to_response(message: Any, request: LLMRequest) -> LLMResponse:
    stop_reason = getattr(message, "stop_reason", None)
    request_id = getattr(message, "_request_id", None)
    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    usage = message.usage
    # キャッシュの書き込み・読み込みも入力として数える（費用を少なく見積もらない側に倒す）
    input_tokens = (
        (usage.input_tokens or 0)
        + (getattr(usage, "cache_creation_input_tokens", None) or 0)
        + (getattr(usage, "cache_read_input_tokens", None) or 0)
    )
    response = LLMResponse(
        provider=CLAUDE_PROVIDER,
        model=getattr(message, "model", None) or request.model,
        text=text,
        structured=None,
        usage=LLMUsage(input_tokens=input_tokens, output_tokens=usage.output_tokens or 0),
        finish_reason=str(stop_reason or "end_turn"),
        request_id=request_id,
    )
    # 断られた・途中で切れた応答も費用は発生しているので、応答を付けて失敗にする（記録は計測側）
    if stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise LLMResponseError(f"Claude declined the request (category={category})", response)
    if stop_reason == "max_tokens":
        raise LLMResponseError("Claude response was truncated at max_tokens", response)
    return response
