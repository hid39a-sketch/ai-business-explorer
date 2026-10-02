"""ステージ実行から Claude API のクライアントを使う（Fake SDK。実際の API には接続しない）。

第2回の仕様（分類の送信上限・予算・回数の上限・タイムアウト・LLM ログ）が実際のプロバイダーでも
そのまま効くことを確かめる。
"""

import json
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.application.commands import (
    ExplorationStageRunCommand,
    IdeaStageRunCommand,
)
from ai_business_explorer.application.stage_runs import StageRunService
from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    Analysis,
    ClaimEvidenceLink,
    LLMCall,
    LLMCallPayload,
)
from ai_business_explorer.infrastructure.db.models import Claim as ClaimModel
from ai_business_explorer.infrastructure.db.repositories import scope_to_organization
from ai_business_explorer.llm.claude import ClaudeLLMClient
from ai_business_explorer.prompts.loader import load_prompt
from ai_business_explorer.seed import DEFAULT_ORGANIZATION_ID
from ai_business_explorer.tools.base import ToolRegistry
from tests.conftest import Api
from tests.fake_claude import FAKE_KEY, FakeClaudeSDK, FakeReply, idea_reply


@pytest.fixture
def claude_employee(api: Api) -> dict[str, Any]:
    ig = next(e for e in api.items("/ai-employees") if e["key"] == "idea_generator")
    return api.patch(f"/ai-employees/{ig['id']}", {"llm_config": {"provider": "anthropic"}})


def _service(
    session: Session, settings: Settings, sdk: FakeClaudeSDK, **update: Any
) -> StageRunService:
    scope_to_organization(session, DEFAULT_ORGANIZATION_ID)
    changed = settings.model_copy(update={"llm_api_key": SecretStr(FAKE_KEY), **update})

    def factory(provider: str) -> ClaudeLLMClient:
        assert provider == "anthropic"
        return ClaudeLLMClient(changed.llm_api_key, client_factory=sdk.factory)

    return StageRunService(
        session, changed, build_default_registry(), ToolRegistry(), llm_client_factory=factory
    )


def _run(service: StageRunService, human: Actor, exp_id: str) -> Any:
    return service.run_exploration_stage(human, UUID(exp_id), ExplorationStageRunCommand())


def test_claude_run_records_cost_usage_and_payload(
    api: Api,
    session: Session,
    settings: Settings,
    human: Actor,
    claude_employee: dict[str, Any],
) -> None:
    sdk = FakeClaudeSDK(replies=[idea_reply(input_tokens=1000, output_tokens=500)])
    exp = api.exploration()
    run = _run(_service(session, settings, sdk), human, exp["id"])
    assert run.status == "succeeded"
    [execution] = _service(session, settings, sdk).executions_for(run.id)
    assert (execution.llm_provider, execution.llm_model) == ("anthropic", "claude-opus-5-5")
    # $4 / $20 per 1M tokens（seed の単価）
    expected = Decimal("0.014")
    assert execution.cost_amount == expected
    [call] = session.scalars(select(LLMCall)).all()
    assert (call.provider, call.model, call.cost_amount) == (
        "anthropic",
        "claude-opus-5-5",
        expected,
    )
    assert (call.input_tokens, call.output_tokens) == (1000, 500)
    assert call.provider_request_id == "req_fake_123"
    assert call.payload_mode == "full"
    # 呼び出しの上限秒数（R-20）と、残りの予算で払える出力数への絞り込み
    assert sdk.timeouts == [120]
    assert sdk.requests[0]["max_tokens"] == 16000
    assert len(api.items(f"/explorations/{exp['id']}/ideas")) == 1


def test_internal_is_the_highest_classification_sent(
    api: Api, session: Session, settings: Settings, human: Actor, claude_employee: dict[str, Any]
) -> None:
    from ai_business_explorer.domain.errors import InvalidStateError

    sdk = FakeClaudeSDK()
    service = _service(session, settings, sdk)
    for classification in ("confidential", "restricted"):
        exp = api.post(
            "/explorations", {"title": "t", "theme": "t", "classification": classification}
        )
        with pytest.raises(InvalidStateError, match="exceeds the send limit 'internal'"):
            _run(service, human, exp["id"])
    assert sdk.requests == []


def test_budget_too_small_for_any_call_stops_before_sending(
    api: Api, session: Session, settings: Settings, human: Actor, claude_employee: dict[str, Any]
) -> None:
    api.patch(
        f"/ai-employees/{claude_employee['id']}",
        {"llm_config": {"provider": "anthropic", "max_cost_per_execution": "0.0001"}},
    )
    sdk = FakeClaudeSDK()
    exp = api.exploration()
    service = _service(session, settings, sdk)
    run = _run(service, human, exp["id"])
    [execution] = service.executions_for(run.id)
    assert (run.status, execution.error_type) == ("failed", "budget_exceeded")
    assert sdk.requests == []


def test_output_is_capped_by_the_remaining_budget(
    api: Api, session: Session, settings: Settings, human: Actor, claude_employee: dict[str, Any]
) -> None:
    api.patch(
        f"/ai-employees/{claude_employee['id']}",
        {"llm_config": {"provider": "anthropic", "max_cost_per_execution": "0.02"}},
    )
    sdk = FakeClaudeSDK(replies=[idea_reply(input_tokens=200, output_tokens=100)])
    _run(_service(session, settings, sdk), human, api.exploration()["id"])
    # 0.02 USD から入力の見積もりを引いた残りで払える出力トークン数（$20 / 1M）
    assert 0 < sdk.requests[0]["max_tokens"] < 1000


def test_refused_call_is_recorded_with_its_cost(
    api: Api, session: Session, settings: Settings, human: Actor, claude_employee: dict[str, Any]
) -> None:
    refusal = FakeReply(stop_reason="refusal", input_tokens=1000, output_tokens=0)
    sdk = FakeClaudeSDK(replies=[refusal])
    service = _service(session, settings, sdk)
    run = _run(service, human, api.exploration()["id"])
    [execution] = service.executions_for(run.id)
    assert (run.status, execution.error_type) == ("failed", "llm_error")
    assert "declined" in (execution.error_message or "")
    [call] = session.scalars(select(LLMCall)).all()
    assert (call.status, call.cost_amount) == ("failed", Decimal("0.004"))
    assert execution.cost_amount == Decimal("0.004")
    assert session.scalars(select(Analysis)).all() == []


def test_api_key_never_reaches_logs_or_payloads(
    api: Api, session: Session, settings: Settings, human: Actor, claude_employee: dict[str, Any]
) -> None:
    sdk = FakeClaudeSDK(replies=[idea_reply()])
    _run(_service(session, settings, sdk), human, api.exploration()["id"])
    assert FAKE_KEY not in str(sdk.requests)
    assert session.scalars(select(LLMCallPayload)).all()
    for table in ("llm_calls", "llm_call_payloads", "executions", "audit_events", "stage_runs"):
        rows = session.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")).scalars()  # noqa: S608  固定のテーブル名
        assert all(FAKE_KEY not in row for row in rows), table


def test_real_provider_is_never_called_in_the_test_environment(
    api: Api, claude_employee: dict[str, Any]
) -> None:
    """既定の LLM クライアント（APP_ENV=test）では、実際の Claude API を使わずに失敗する。"""
    run = api.post(f"/explorations/{api.exploration()['id']}/stage-runs", {})
    [ex] = run["executions"]
    assert (run["status"], ex["error_type"]) == ("failed", "llm_error")
    assert "APP_ENV=test" in ex["error_message"]
    assert Decimal(ex["cost_amount"]) == 0


# ------------------------------------------------------------ 構造化出力と relation（市場調査）


@pytest.fixture
def market_setup(api: Api) -> dict[str, Any]:
    mr = next(e for e in api.items("/ai-employees") if e["key"] == "market_researcher")
    api.patch(f"/ai-employees/{mr['id']}", {"llm_config": {"provider": "anthropic"}})
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    ev1 = api.evidence(exp["id"], idea["id"], title="ヒアリングA")["id"]
    ev2 = api.evidence(exp["id"], idea["id"], title="ヒアリングB")["id"]
    return {"idea": idea["id"], "ev1": ev1, "ev2": ev2}


def _market_reply(*claims: dict[str, Any]) -> FakeReply:
    body = {"summary": "要約", "market_overview": "概要", "claims": list(claims)}
    return FakeReply(text=json.dumps(body, ensure_ascii=False), input_tokens=500, output_tokens=200)


def _run_market(
    session: Session, settings: Settings, human: Actor, idea_id: str, sdk: FakeClaudeSDK
) -> tuple[Any, Any]:
    service = _service(session, settings, sdk)
    run = service.run_idea_stage(
        human, UUID(idea_id), IdeaStageRunCommand(stage_key="market_research")
    )
    [execution] = service.executions_for(run.id)
    return run, execution


def test_market_research_requests_structured_output_limited_to_input_evidence(
    api: Api, session: Session, settings: Settings, human: Actor, market_setup: dict[str, Any]
) -> None:
    ev1, ev2 = market_setup["ev1"], market_setup["ev2"]
    sdk = FakeClaudeSDK(
        replies=[
            _market_reply(
                {
                    "id": "c1",
                    "text": "検査に時間がかかっている",
                    "kind": "evidence_based",
                    "evidence_refs": [{"evidence_id": ev1, "relation": "supports"}],
                },
                {
                    "id": "c2",
                    "text": "どの工場も検査の自動化を望んでいる",
                    "kind": "evidence_based",
                    "evidence_refs": [
                        {"evidence_id": ev2, "relation": "contradicts"},
                        {"evidence_id": ev1, "relation": "context"},
                    ],
                },
            )
        ]
    )
    run, execution = _run_market(session, settings, human, market_setup["idea"], sdk)
    assert run.status == "succeeded"
    # 入力した Evidence の ID だけを enum にしたスキーマを、構造化出力として送っている
    fmt = sdk.requests[0]["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    enum = fmt["schema"]["$defs"]["EvidenceRef"]["properties"]["evidence_id"]["enum"]
    assert sorted(enum) == sorted([ev1, ev2])
    assert fmt["schema"]["$defs"]["EvidenceRef"]["required"] == ["evidence_id", "relation"]
    # プロンプトは seed の v4（版とハッシュを実行に記録する）
    v4 = load_prompt("market_researcher", "v4")
    assert (execution.prompt_version, execution.prompt_hash) == ("v4", v4.sha256)
    assert sdk.requests[0]["system"] == v4.text
    # relation は主張と Evidence の組ごとに保存する（contradicts だけの evidence_based も可）
    links = session.execute(
        select(ClaimModel.claim_key, ClaimEvidenceLink.evidence_id, ClaimEvidenceLink.relation)
        .join(ClaimEvidenceLink, ClaimEvidenceLink.claim_id == ClaimModel.id)
        .order_by(ClaimModel.claim_key, ClaimEvidenceLink.relation)
    ).all()
    assert [(k, str(e), r) for k, e, r in links] == [
        ("c1", ev1, "supports"),
        ("c2", ev1, "context"),
        ("c2", ev2, "contradicts"),
    ]


@pytest.mark.parametrize(
    ("claim", "message"),
    [
        # relation を省いた出力は supports とみなさない
        ({"evidence_refs": [{"evidence_id": "EV1"}]}, "relation"),
        # 同じ主張と Evidence に supports と contradicts（C-09）
        (
            {
                "evidence_refs": [
                    {"evidence_id": "EV1", "relation": "supports"},
                    {"evidence_id": "EV1", "relation": "contradicts"},
                ]
            },
            "both supports and contradicts",
        ),
        # context だけの evidence_based（C-09）
        ({"evidence_refs": [{"evidence_id": "EV1", "relation": "context"}]}, "no supports"),
    ],
)
def test_invalid_relations_fail_with_validation_error(
    api: Api,
    session: Session,
    settings: Settings,
    human: Actor,
    market_setup: dict[str, Any],
    claim: dict[str, Any],
    message: str,
) -> None:
    refs = [{**r, "evidence_id": market_setup["ev1"]} for r in claim["evidence_refs"]]
    sdk = FakeClaudeSDK(
        replies=[
            _market_reply(
                {"id": "c1", "text": "主張", "kind": "evidence_based", "evidence_refs": refs}
            )
        ]
    )
    run, execution = _run_market(session, settings, human, market_setup["idea"], sdk)
    assert (run.status, execution.error_type) == ("failed", "validation_error")
    assert message in (execution.error_message or "")
    # 何も保存しない。費用は記録する（E-07）
    assert session.scalars(select(Analysis)).all() == []
    assert execution.cost_amount > 0


def test_fenced_json_is_not_accepted(
    api: Api, session: Session, settings: Settings, human: Actor, market_setup: dict[str, Any]
) -> None:
    body = json.dumps({"summary": "s", "market_overview": "m", "claims": []})
    sdk = FakeClaudeSDK(replies=[FakeReply(text=f"```json\n{body}\n```")])
    run, execution = _run_market(session, settings, human, market_setup["idea"], sdk)
    assert (run.status, execution.error_type) == ("failed", "validation_error")
    assert "not valid JSON" in (execution.error_message or "")


def test_idea_generation_schema_cannot_express_evidence(
    api: Api, session: Session, settings: Settings, human: Actor, claude_employee: dict[str, Any]
) -> None:
    sdk = FakeClaudeSDK(replies=[idea_reply()])
    _run(_service(session, settings, sdk), human, api.exploration()["id"])
    claim_schema = sdk.requests[0]["output_config"]["format"]["schema"]["$defs"]["Claim"]
    assert "evidence_refs" not in claim_schema["properties"]
    assert claim_schema["properties"]["kind"]["enum"] == ["inference", "speculation"]
