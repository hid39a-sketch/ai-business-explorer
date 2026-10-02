"""LLM プロバイダー抽象化。

ドメインやAI社員はこのモジュールの型だけに依存する。特定ベンダーの SDK の型を外に漏らさない。
第1回の実装は FakeLLMClient のみ（llm/fake.py）。
"""

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ai_business_explorer.domain.execution import CallGuard


class LLMError(Exception):
    """LLM 呼び出しの失敗（プロバイダー障害、未対応プロバイダー、応答不正など）。"""


class LLMProviderNotAvailableError(LLMError):
    pass


class LLMMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        return LLMUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class LLMRequest(BaseModel):
    model: str
    system: str
    messages: list[LLMMessage]
    prompt_key: str
    prompt_version: str
    response_schema: dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    # 1回の呼び出しの上限秒数（R-20）。実際のプロバイダーのクライアントが守る。
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    provider: str
    model: str
    text: str
    structured: dict[str, Any] | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)
    finish_reason: str = "stop"
    latency_ms: int = 0
    # プロバイダーが返すリクエストID（問い合わせ用。llm_calls に記録する）
    request_id: str | None = None


class LLMResponseError(LLMError):
    """応答は返ったが使えない（断られた・途中で切れた）。費用の記録のために応答を持つ。"""

    def __init__(self, message: str, response: LLMResponse) -> None:
        super().__init__(message)
        self.response = response


class LLMClient(Protocol):
    @property
    def provider(self) -> str: ...

    def complete(self, request: LLMRequest) -> LLMResponse: ...


# 実際に送った設定として記録する項目（第2回仕様 11章 SC候補-9）
SENT_PARAM_NAMES = ("temperature", "thinking", "effort", "max_tokens")


def sent_param(sent: bool, value: Any = None) -> dict[str, Any]:
    """記録する1項目。送っていない項目は {"sent": false}（値を持たない）。"""
    return {"sent": True, "value": value} if sent else {"sent": False}


@runtime_checkable
class ReportsSentParams(Protocol):
    """実際に送る設定を返せるクライアント。返す値は記録に使うので、秘密情報を含めない。"""

    def sent_params(self, request: LLMRequest) -> dict[str, Any]: ...


class TrackingLLMClient:
    """任意の LLMClient を包み、使用量と使用モデルを集計する（実行記録に保存するため）。

    guard があれば呼び出しの前に確認し、call_timeout_seconds を各呼び出しに付ける。
    """

    def __init__(
        self,
        inner: LLMClient,
        guard: CallGuard | None = None,
        call_timeout_seconds: float | None = None,
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._call_timeout_seconds = call_timeout_seconds
        self.usage = LLMUsage()
        self.call_count = 0
        self.last_model: str | None = None

    @property
    def provider(self) -> str:
        return self._inner.provider

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._guard is not None:
            self._guard.check()
        if self._call_timeout_seconds is not None and request.timeout_seconds is None:
            request = request.model_copy(update={"timeout_seconds": self._call_timeout_seconds})
        response = self._inner.complete(request)
        self.usage = self.usage + response.usage
        self.call_count += 1
        self.last_model = response.model
        return response
