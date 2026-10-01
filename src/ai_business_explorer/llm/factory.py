"""AI社員の llm_config から LLMClient を生成する。第1回は fake のみ対応。"""

from typing import Any

from ai_business_explorer.llm.base import LLMClient, LLMProviderNotAvailableError
from ai_business_explorer.llm.fake import FAKE_MODEL, FAKE_PROVIDER, FakeLLMClient

SUPPORTED_PROVIDERS = frozenset({FAKE_PROVIDER})


def resolve_llm_config(llm_config: dict[str, Any], default_provider: str) -> tuple[str, str]:
    provider = str(llm_config.get("provider") or default_provider)
    model = str(llm_config.get("model") or (FAKE_MODEL if provider == FAKE_PROVIDER else ""))
    return provider, model


def build_llm_client(provider: str) -> LLMClient:
    if provider == FAKE_PROVIDER:
        return FakeLLMClient()
    raise LLMProviderNotAvailableError(
        f"LLM provider '{provider}' is not available (only 'fake' is supported in this phase)"
    )
