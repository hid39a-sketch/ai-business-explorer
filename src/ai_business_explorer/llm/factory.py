"""AI社員の llm_config から LLMClient を生成する。

使えるプロバイダーは fake（Fake LLM。テスト・CI 用）と anthropic（Claude API。第2回仕様 R-03）。
APP_ENV=test では実際のプロバイダーを使わない（テストや CI が実 API を呼ばないための安全装置）。
"""

from typing import Any

from ai_business_explorer.config import Settings
from ai_business_explorer.llm.base import LLMClient, LLMProviderNotAvailableError
from ai_business_explorer.llm.claude import CLAUDE_DEFAULT_MODEL, CLAUDE_PROVIDER, ClaudeLLMClient
from ai_business_explorer.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient

SUPPORTED_PROVIDERS = frozenset({FAKE_PROVIDER, CLAUDE_PROVIDER})
DEFAULT_MODELS = {FAKE_PROVIDER: FAKE_MODEL, CLAUDE_PROVIDER: CLAUDE_DEFAULT_MODEL}


def resolve_llm_config(llm_config: dict[str, Any], default_provider: str) -> tuple[str, str]:
    provider = str(llm_config.get("provider") or default_provider)
    model = str(llm_config.get("model") or DEFAULT_MODELS.get(provider, ""))
    return provider, model


def build_llm_client(provider: str, settings: Settings) -> LLMClient:
    if provider == FAKE_PROVIDER:
        return FakeLLMClient()
    if provider == CLAUDE_PROVIDER:
        if settings.app_env == "test":
            raise LLMProviderNotAvailableError("real LLM providers are disabled when APP_ENV=test")
        return ClaudeLLMClient(settings.llm_api_key)
    raise LLMProviderNotAvailableError(
        f"LLM provider '{provider}' is not available "
        f"(supported: {', '.join(sorted(SUPPORTED_PROVIDERS))})"
    )
