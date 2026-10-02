"""実際に送った設定の記録（llm_calls.request_params。第2回仕様 11章 SC候補-9）。

実際の LLM には接続しない（Fake LLM と、SDK を差し替えた Claude のクライアント）。
- temperature・thinking・effort・max_tokens を記録し、送っていない項目は {"sent": false}。
- max_tokens は上限で絞った後の実際の値。temperature は extra_body で送ったものも含む。
- 失敗した呼び出しでも記録する。API キーなどの秘密情報は入らない。
"""

import json
from typing import Any

import anthropic
import httpx2
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import Actor, LLMCall
from ai_business_explorer.llm.base import SENT_PARAM_NAMES
from tests.api.test_claude_provider import _run, _service, claude_employee  # noqa: F401
from tests.api.test_costs import _price
from tests.conftest import Api
from tests.fake_claude import FAKE_KEY, FakeClaudeSDK, FakeReply, idea_reply

NOT_SENT = {"sent": False}


def _calls(session: Session) -> list[LLMCall]:
    return list(session.scalars(select(LLMCall).order_by(LLMCall.created_at)).all())


def _assert_no_secrets(params: dict[str, Any]) -> None:
    assert set(params) == set(SENT_PARAM_NAMES)
    dumped = json.dumps(params)
    assert FAKE_KEY not in dumped
    for word in ("api_key", "authorization", "x-api-key", "headers"):
        assert word not in dumped.lower()


def test_fake_llm_records_the_same_shape(api: Api, session: Session) -> None:
    exp = api.exploration()
    api.post(f"/explorations/{exp['id']}/stage-runs", {})
    [call] = _calls(session)
    params = call.request_params
    assert params is not None
    _assert_no_secrets(params)
    assert params["temperature"] == NOT_SENT
    assert params["thinking"] == NOT_SENT
    assert params["effort"] == NOT_SENT
    assert params["max_tokens"]["sent"] in (True, False)


def test_opus_records_temperature_as_not_sent_and_the_capped_max_tokens(
    api: Api,
    session: Session,
    settings: Settings,
    human: Actor,
    claude_employee: dict[str, Any],  # noqa: F811
) -> None:
    sdk = FakeClaudeSDK(replies=[idea_reply()])
    run = _run(_service(session, settings, sdk), human, api.exploration()["id"])
    assert run.status == "succeeded"
    [call] = _calls(session)
    params = call.request_params
    assert params is not None
    _assert_no_secrets(params)
    assert params == {
        "temperature": NOT_SENT,
        "thinking": NOT_SENT,
        "effort": NOT_SENT,
        # 実際に送った値（非ストリーミングの上限・残りの予算で絞った後）
        "max_tokens": {"sent": True, "value": sdk.requests[0]["max_tokens"]},
    }


def test_haiku_records_temperature_zero_sent_in_extra_body(
    api: Api,
    session: Session,
    settings: Settings,
    human: Actor,
    claude_employee: dict[str, Any],  # noqa: F811
) -> None:
    _price(
        session,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        input_per_million="1",
        output_per_million="5",
    )
    api.patch(
        f"/ai-employees/{claude_employee['id']}",
        {"llm_config": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}},
    )
    sdk = FakeClaudeSDK(replies=[idea_reply(model="claude-haiku-4-5-20251001")])
    run = _run(_service(session, settings, sdk), human, api.exploration()["id"])
    assert run.status == "succeeded"
    assert sdk.requests[0]["extra_body"] == {"temperature": 0}
    [call] = _calls(session)
    assert call.request_params is not None
    assert call.request_params["temperature"] == {"sent": True, "value": 0}
    assert call.request_params["max_tokens"] == {
        "sent": True,
        "value": sdk.requests[0]["max_tokens"],
    }


def test_failed_calls_are_recorded_with_request_params(
    api: Api,
    session: Session,
    settings: Settings,
    human: Actor,
    claude_employee: dict[str, Any],  # noqa: F811
) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    for reply in (
        FakeReply(error=anthropic.APIConnectionError(request=request)),  # 応答なし
        FakeReply(stop_reason="refusal"),  # 応答はあるが使えない
    ):
        sdk = FakeClaudeSDK(replies=[reply])
        run = _run(_service(session, settings, sdk), human, api.exploration()["id"])
        assert run.status == "failed"
    calls = _calls(session)
    assert [c.status for c in calls] == ["failed", "failed"]
    for call in calls:
        assert call.request_params is not None
        _assert_no_secrets(call.request_params)
        assert call.request_params["temperature"] == NOT_SENT
        assert call.request_params["max_tokens"]["sent"] is True
