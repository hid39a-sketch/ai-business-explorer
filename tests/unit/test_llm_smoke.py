"""手動の接続確認（llm_smoke）：通常の呼び出しのあとに、最小の構造化出力を1回確かめる。

SDK を Fake に差し替えて実行する（実際の API には接続しない）。
"""

from decimal import Decimal
from typing import Any

import anthropic
import pytest
from pydantic import SecretStr

from ai_business_explorer import llm_smoke
from ai_business_explorer.llm.claude import ClaudeLLMClient
from tests.fake_claude import FAKE_KEY, FakeClaudeSDK, FakeReply


@pytest.fixture
def run_smoke(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("LLM_SMOKE_CONFIRM", "yes")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LLM_API_KEY", FAKE_KEY)

    def run(*replies: FakeReply) -> tuple[int, FakeClaudeSDK]:
        sdk = FakeClaudeSDK(replies=list(replies))

        def client(key: SecretStr | None) -> ClaudeLLMClient:
            return ClaudeLLMClient(key, client_factory=sdk.factory)

        monkeypatch.setattr(llm_smoke, "ClaudeLLMClient", client)
        return llm_smoke.main(), sdk

    return run


def _plain() -> FakeReply:
    return FakeReply(text='{"ok": true}', input_tokens=40, output_tokens=9)


def _structured(text: str = '{"ok": true}') -> FakeReply:
    return FakeReply(text=text, input_tokens=200, output_tokens=8)


def test_plain_call_comes_first_and_structured_output_second(
    run_smoke: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    code, sdk = run_smoke(_plain(), _structured())
    assert code == 0
    plain, structured = sdk.requests
    # 1回目は通常の呼び出し（構造化出力の指定なし）
    assert plain["system"] == llm_smoke.SYSTEM
    assert "output_config" not in plain
    # 2回目だけ、最小のスキーマを構造化出力として送る
    assert structured["system"] == llm_smoke.STRUCTURED_SYSTEM
    assert structured["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": anthropic.transform_schema(llm_smoke.SMOKE_SCHEMA),
        }
    }
    assert [r["max_tokens"] for r in sdk.requests] == [llm_smoke.SMOKE_MAX_TOKENS] * 2
    out = capsys.readouterr().out
    assert "structured_ok=True" in out
    assert out.rstrip().endswith("OK")
    assert FAKE_KEY not in out


@pytest.mark.parametrize(
    "text",
    ['{"ok": false}', "{}", "not json", '```json\n{"ok": true}\n```', '["ok"]'],
)
def test_unexpected_structured_output_fails(
    run_smoke: Any, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    code, sdk = run_smoke(_plain(), _structured(text))
    assert code == 1
    assert len(sdk.requests) == 2
    captured = capsys.readouterr()
    assert "structured_ok=False" in captured.out
    assert "FAILED" in captured.err


def test_cost_cap_covers_both_calls(run_smoke: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """呼ぶ前に2回分の最悪の場合の費用を確かめ、呼んだ後も実際の費用の合計を確かめる。"""
    assert Decimal("0.05") == llm_smoke.SMOKE_MAX_COST_USD
    assert llm_smoke.worst_case_cost(llm_smoke.SMOKE_MAX_TOKENS) <= llm_smoke.SMOKE_MAX_COST_USD
    one_call = Decimal(llm_smoke.SMOKE_MAX_TOKENS) * llm_smoke.OUTPUT_PRICE / llm_smoke.MILLION
    assert llm_smoke.worst_case_cost(llm_smoke.SMOKE_MAX_TOKENS) > 2 * one_call
    # 実際の費用の合計が上限を超えたら失敗にする（ここでは使用量を大きくして再現する）
    code, _ = run_smoke(
        _plain(), FakeReply(text='{"ok": true}', input_tokens=10_000, output_tokens=1024)
    )
    assert code == 1
    assert "cost more than the cap" in capsys.readouterr().err


def test_a_failed_call_stops_the_smoke(run_smoke: Any) -> None:
    refusal = FakeReply(stop_reason="refusal")
    code, sdk = run_smoke(refusal)
    assert code == 1
    assert len(sdk.requests) == 1
