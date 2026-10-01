import json

import pytest

from ai_business_explorer.config import Settings
from ai_business_explorer.llm.base import (
    LLMError,
    LLMMessage,
    LLMProviderNotAvailableError,
    LLMRequest,
    TrackingLLMClient,
)
from ai_business_explorer.llm.factory import build_llm_client, resolve_llm_config
from ai_business_explorer.llm.fake import FakeLLMClient


def _request(prompt_key: str, payload: dict[str, object]) -> LLMRequest:
    return LLMRequest(
        model="fake-model-v1",
        system="system",
        messages=[LLMMessage(role="user", content=json.dumps(payload))],
        prompt_key=prompt_key,
        prompt_version="v1",
    )


def test_fake_llm_is_deterministic() -> None:
    req = _request("idea_generator", {"exploration": {"theme": "物流"}})
    a = FakeLLMClient().complete(req)
    b = FakeLLMClient().complete(req)
    assert a.structured == b.structured
    assert a.provider == "fake"
    assert a.structured is not None
    assert len(a.structured["ideas"]) == 3


def test_fake_llm_unknown_prompt_returns_text_only() -> None:
    res = FakeLLMClient().complete(_request("other", {"x": 1}))
    assert res.structured is None
    assert res.text.startswith("fake:")


def test_fake_llm_can_inject_failure() -> None:
    client = FakeLLMClient(fail_with=LLMError("boom"))
    with pytest.raises(LLMError):
        client.complete(_request("idea_generator", {}))


def test_tracking_client_accumulates_usage() -> None:
    tracker = TrackingLLMClient(FakeLLMClient())
    tracker.complete(_request("idea_generator", {"exploration": {"theme": "A"}}))
    tracker.complete(_request("idea_generator", {"exploration": {"theme": "B"}}))
    assert tracker.call_count == 2
    assert tracker.usage.input_tokens > 0
    assert tracker.last_model == "fake-model-v1"


def test_supported_providers() -> None:
    """fake と anthropic（Claude API）だけ。テスト環境では実際のプロバイダーを使わない。"""
    settings = Settings(app_env="test")
    assert build_llm_client("fake", settings).provider == "fake"
    with pytest.raises(LLMProviderNotAvailableError):
        build_llm_client("openai", settings)
    with pytest.raises(LLMProviderNotAvailableError, match="APP_ENV=test"):
        build_llm_client("anthropic", settings)


def test_resolve_llm_config_defaults() -> None:
    assert resolve_llm_config({}, "fake") == ("fake", "fake-model-v1")
