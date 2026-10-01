"""Claude API クライアントの変換と安全装置（実際の API には接続しない）。"""

from pathlib import Path

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from ai_business_explorer.config import Settings
from ai_business_explorer.llm.base import (
    LLMError,
    LLMMessage,
    LLMProviderNotAvailableError,
    LLMRequest,
    LLMResponseError,
)
from ai_business_explorer.llm.claude import ClaudeLLMClient, _sdk_client
from ai_business_explorer.llm.factory import build_llm_client, resolve_llm_config
from tests.fake_claude import FAKE_KEY, FakeClaudeSDK, FakeReply

ROOT = Path(__file__).resolve().parents[2]


def _request(**kwargs: object) -> LLMRequest:
    return LLMRequest(
        model="claude-opus-5-5",
        system="指示",
        messages=[LLMMessage(role="user", content='{"data": "外部の文章"}')],
        prompt_key="p",
        prompt_version="v1",
        **kwargs,  # type: ignore[arg-type]
    )


def _client(sdk: FakeClaudeSDK) -> ClaudeLLMClient:
    return ClaudeLLMClient(SecretStr(FAKE_KEY), client_factory=sdk.factory)


def test_request_and_response_conversion() -> None:
    sdk = FakeClaudeSDK(replies=[FakeReply(input_tokens=120, output_tokens=30)])
    response = _client(sdk).complete(_request(temperature=0.5, timeout_seconds=120))
    [params] = sdk.requests
    # 指示（system）とデータ（user）を分け、Opus 5.5 が受け付けない sampling は送らない
    assert params == {
        "model": "claude-opus-5-5",
        "max_tokens": 16000,
        "system": "指示",
        "messages": [{"role": "user", "content": '{"data": "外部の文章"}'}],
    }
    assert sdk.timeouts == [120]
    assert response.provider == "anthropic"
    assert response.text == '{"ok": true}'
    assert response.structured is None
    assert (response.usage.input_tokens, response.usage.output_tokens) == (120, 30)
    assert response.request_id == "req_fake_123"
    assert sdk.api_keys == [FAKE_KEY]


def test_max_tokens_never_exceeds_the_non_streaming_ceiling() -> None:
    sdk = FakeClaudeSDK()
    _client(sdk).complete(_request(max_tokens=50_000))
    _client(sdk).complete(_request(max_tokens=300))
    assert [r["max_tokens"] for r in sdk.requests] == [16000, 300]


@pytest.mark.parametrize("key", [None, SecretStr(""), SecretStr("   ")])
def test_missing_api_key_is_not_available(key: SecretStr | None) -> None:
    with pytest.raises(LLMProviderNotAvailableError, match="LLM_API_KEY is not set"):
        ClaudeLLMClient(key, client_factory=FakeClaudeSDK().factory)


@pytest.mark.parametrize(
    ("stop_reason", "message"),
    [("refusal", "declined"), ("max_tokens", "truncated")],
)
def test_unusable_responses_carry_usage_for_cost_records(stop_reason: str, message: str) -> None:
    sdk = FakeClaudeSDK(replies=[FakeReply(stop_reason=stop_reason, output_tokens=77)])
    with pytest.raises(LLMResponseError, match=message) as info:
        _client(sdk).complete(_request())
    assert info.value.response.usage.output_tokens == 77


def _http_request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (anthropic.APITimeoutError(request=_http_request()), "timed out"),
        (anthropic.APIConnectionError(request=_http_request()), "connection failed"),
        (
            anthropic.RateLimitError(
                "rate limited",
                response=httpx2.Response(
                    429, request=_http_request(), headers={"request-id": "req_x"}
                ),
                body={"error": {"type": "rate_limit_error", "message": "slow down"}},
            ),
            "429 rate_limit_error: slow down",
        ),
    ],
)
def test_sdk_errors_become_llm_errors_without_secrets(error: Exception, message: str) -> None:
    sdk = FakeClaudeSDK(replies=[FakeReply(error=error)])
    with pytest.raises(LLMError, match=message) as info:
        _client(sdk).complete(_request())
    assert FAKE_KEY not in str(info.value)
    assert FAKE_KEY not in repr(info.value)


def test_sdk_client_does_not_retry() -> None:
    """再試行すると1回の記録・計上と実際の呼び出し回数がずれるので、SDK の再試行は使わない。"""
    client = _sdk_client(FAKE_KEY)
    assert client.max_retries == 0


def test_factory_never_uses_a_real_provider_in_tests() -> None:
    test_settings = Settings(app_env="test", llm_api_key=SecretStr(FAKE_KEY))
    with pytest.raises(LLMProviderNotAvailableError, match="APP_ENV=test"):
        build_llm_client("anthropic", test_settings)
    with pytest.raises(LLMProviderNotAvailableError, match="not available"):
        build_llm_client("openai", test_settings)
    dev = Settings(app_env="development", llm_api_key=SecretStr(FAKE_KEY))
    assert isinstance(build_llm_client("anthropic", dev), ClaudeLLMClient)
    without_key = Settings(app_env="development", llm_api_key=None)
    with pytest.raises(LLMProviderNotAvailableError):
        build_llm_client("anthropic", without_key)


def test_default_models() -> None:
    assert resolve_llm_config({"provider": "anthropic"}, "fake") == ("anthropic", "claude-opus-5-5")
    assert resolve_llm_config({}, "fake") == ("fake", "fake-model-v1")
    assert resolve_llm_config({"provider": "anthropic", "model": "x"}, "fake") == ("anthropic", "x")


def test_settings_hide_the_api_key() -> None:
    settings = Settings(llm_api_key=SecretStr(FAKE_KEY))
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings.model_dump())


def test_smoke_workflow_is_manual_only_and_capped() -> None:
    workflow = (ROOT / ".github/workflows/llm-smoke.yml").read_text()
    on = workflow.split("\non:")[1].split("\npermissions:")[0]
    assert "workflow_dispatch" in on
    assert "pull_request" not in on
    assert "push" not in on
    assert "schedule" not in on
    assert "github.event.inputs.confirm == 'run'" in workflow
    assert "timeout-minutes: 5" in workflow
    # API キーは接続確認のステップでだけ使う
    assert workflow.count("secrets.ANTHROPIC_API_KEY") == 1
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "secrets." not in ci
    assert "ANTHROPIC_API_KEY" not in ci
    assert "LLM_API_KEY" not in ci


def test_smoke_cost_cap() -> None:
    from ai_business_explorer import llm_smoke

    assert llm_smoke.worst_case_cost(llm_smoke.SMOKE_MAX_TOKENS) <= llm_smoke.SMOKE_MAX_COST_USD
    assert llm_smoke.SMOKE_MAX_COST_USD.__class__("0.05") == llm_smoke.SMOKE_MAX_COST_USD


def test_smoke_does_nothing_without_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_business_explorer import llm_smoke

    monkeypatch.delenv("LLM_SMOKE_CONFIRM", raising=False)
    monkeypatch.setattr(
        llm_smoke, "ClaudeLLMClient", lambda *_: pytest.fail("must not create a client")
    )
    assert llm_smoke.main() == 0


# ---------------------------------------------------------------------- 構造化出力


def _schema() -> dict[str, object]:
    from ai_business_explorer.agents.base import output_schema_for
    from ai_business_explorer.agents.employees.market_researcher import MarketResearcherOutput

    return output_schema_for(MarketResearcherOutput, [])


def test_response_schema_is_sent_as_structured_output() -> None:
    sdk = FakeClaudeSDK(replies=[FakeReply(text='{"summary": "s", "market_overview": "m"}')])
    schema = _schema()
    response = _client(sdk).complete(_request(response_schema=schema))
    [params] = sdk.requests
    # SDK の transform_schema で、API が受け付ける形（additionalProperties: false など）にして送る
    assert params["output_config"] == {
        "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
    }
    assert params["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert {k: v for k, v in params.items() if k != "output_config"} == {
        "model": "claude-opus-5-5",
        "max_tokens": 16000,
        "system": "指示",
        "messages": [{"role": "user", "content": '{"data": "外部の文章"}'}],
    }
    assert response.structured == {"summary": "s", "market_overview": "m"}
    assert response.text == '{"summary": "s", "market_overview": "m"}'


def test_transform_schema_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def fake_transform(schema: dict[str, object]) -> dict[str, object]:
        seen.append(schema)
        return {"type": "object", "transformed": True}

    monkeypatch.setattr(anthropic, "transform_schema", fake_transform)
    sdk = FakeClaudeSDK(replies=[FakeReply(text="{}")])
    schema = _schema()
    _client(sdk).complete(_request(response_schema=schema))
    assert seen == [schema]
    assert sdk.requests[0]["output_config"]["format"]["schema"] == {
        "type": "object",
        "transformed": True,
    }


def test_no_structured_output_without_a_schema() -> None:
    sdk = FakeClaudeSDK(replies=[FakeReply(text='{"ok": true}')])
    response = _client(sdk).complete(_request())
    assert "output_config" not in sdk.requests[0]
    assert response.structured is None


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"summary": "s", "market_overview": "m"}\n```',  # 囲みは外さない
        "not json",
        '["a list"]',
    ],
)
def test_non_json_object_is_not_taken_as_structured(text: str) -> None:
    """JSON オブジェクトでない応答は structured にしない（AI社員の検証で失敗する）。"""
    sdk = FakeClaudeSDK(replies=[FakeReply(text=text)])
    response = _client(sdk).complete(_request(response_schema=_schema()))
    assert response.structured is None
    assert response.text == text


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_unusable_structured_responses_still_fail_with_usage(stop_reason: str) -> None:
    sdk = FakeClaudeSDK(replies=[FakeReply(text='{"summary": "s', stop_reason=stop_reason)])
    with pytest.raises(LLMResponseError) as info:
        _client(sdk).complete(_request(response_schema=_schema()))
    assert info.value.response.structured is None
    assert info.value.response.usage.output_tokens == 50
