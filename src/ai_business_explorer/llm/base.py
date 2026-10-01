"""LLM プロバイダー抽象化。

ドメインやAI社員はこのモジュールの型だけに依存する。特定ベンダーの SDK の型を外に漏らさない。
第1回の実装は FakeLLMClient のみ（llm/fake.py）。
"""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


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
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    provider: str
    model: str
    text: str
    structured: dict[str, Any] | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)
    finish_reason: str = "stop"
    latency_ms: int = 0


class LLMClient(Protocol):
    @property
    def provider(self) -> str: ...

    def complete(self, request: LLMRequest) -> LLMResponse: ...


class TrackingLLMClient:
    """任意の LLMClient を包み、使用量と使用モデルを集計する（実行記録に保存するため）。"""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.usage = LLMUsage()
        self.call_count = 0
        self.last_model: str | None = None

    @property
    def provider(self) -> str:
        return self._inner.provider

    def complete(self, request: LLMRequest) -> LLMResponse:
        response = self._inner.complete(request)
        self.usage = self.usage + response.usage
        self.call_count += 1
        self.last_model = response.model
        return response
