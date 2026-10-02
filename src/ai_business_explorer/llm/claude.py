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
- 出力の形は構造化出力（output_config.format＝json_schema）で指定する。LLMRequest.response_schema
  （AI社員の出力モデルの JSON Schema）を anthropic.transform_schema で API が受け付ける形にして
  送り、応答の JSON を LLMResponse.structured に入れる。「```json」の囲みなどを外す処理はしない。
  スキーマで表せない制約（C-09 など）は、これまでどおり AI社員とステージ実行側で検証する。
- temperature は、実際に送るモデル ID が Haiku 4.5 のときだけ 0 を送る（第2回仕様 11章、
  2026-10-02 の確定）。それ以外のモデル（Opus 5.5・Sonnet 5.5 など）には送らない。
  LLMRequest.temperature は使わない。
"""

import json
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
    sent_param,
)

CLAUDE_PROVIDER = "anthropic"
CLAUDE_DEFAULT_MODEL = "claude-opus-5-5"
# 指定がないときの出力上限（非ストリーミングで HTTP のタイムアウトにかからない範囲）
DEFAULT_MAX_TOKENS = 16_000
MAX_ERROR_DETAIL = 500
# temperature=0 を送るモデル（Haiku 4.5 の別名と日付付きの版）。送るモデル ID の完全一致で判定する
ZERO_TEMPERATURE_MODELS = frozenset({"claude-haiku-4-5", "claude-haiku-4-5-20251001"})


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

    def sent_params(self, request: LLMRequest) -> dict[str, Any]:
        """実際に送る temperature・thinking・effort・max_tokens（11章 SC候補-9。記録用）。"""
        return describe_sent_params(_request_params(request))

    def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._client
        if request.timeout_seconds is not None:
            client = client.with_options(timeout=request.timeout_seconds)
        params = _request_params(request)
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


def _request_params(request: LLMRequest) -> dict[str, Any]:
    """messages.create に渡す引数。sent_params の記録も、この引数から作る。"""
    params: dict[str, Any] = {
        "model": request.model,
        # 非ストリーミングの上限（SDK の HTTP タイムアウトにかからない範囲）を超えない
        "max_tokens": min(request.max_tokens or DEFAULT_MAX_TOKENS, DEFAULT_MAX_TOKENS),
        # 指示（system）と、外部由来のデータを含む入力（user の JSON）を分けて渡す
        "system": request.system,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
    }
    if request.model in ZERO_TEMPERATURE_MODELS:
        # SDK（1.11）の messages.create には temperature の引数がないので、本文に直接足す
        params["extra_body"] = {"temperature": 0}
    if request.response_schema is not None:
        params["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": anthropic.transform_schema(request.response_schema),
            }
        }
    return params


def describe_sent_params(params: dict[str, Any]) -> dict[str, Any]:
    """messages.create の引数から、記録する設定を作る。extra_body で本文に入るものも含める。

    API キー・ヘッダー・本文（system・messages）は含めない。
    """
    body = {**params, **params.get("extra_body", {})}
    output_config = params.get("output_config", {})
    return {
        "temperature": sent_param("temperature" in body, body.get("temperature")),
        "thinking": sent_param("thinking" in body, body.get("thinking")),
        "effort": sent_param("effort" in output_config, output_config.get("effort")),
        "max_tokens": sent_param("max_tokens" in body, body.get("max_tokens")),
    }


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
    if request.response_schema is not None:
        response = response.model_copy(update={"structured": _structured(text)})
    return response


def _structured(text: str) -> dict[str, Any] | None:
    """構造化出力の応答（JSON オブジェクト）。JSON でなければ None（AI社員の検証で失敗になる）。"""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
