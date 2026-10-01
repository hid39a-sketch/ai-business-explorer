"""費用管理と LLM・Tool のログ（第2回仕様 10章・12章・14章。E-07・R-16・R-20・R-21）。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.application.commands import ExplorationStageRunCommand
from ai_business_explorer.application.costs import (
    ExecutionMeter,
    delete_expired_payloads,
)
from ai_business_explorer.application.stage_runs import StageRunService
from ai_business_explorer.config import Settings
from ai_business_explorer.domain.enums import DataClassification
from ai_business_explorer.domain.errors import BudgetExceededError
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    Analysis,
    Execution,
    LLMCall,
    LLMCallPayload,
    Pricing,
    ToolCall,
)
from ai_business_explorer.infrastructure.db.repositories import scope_to_organization
from ai_business_explorer.llm.base import LLMError
from ai_business_explorer.llm.fake import DEFAULT_RESPONDERS, FAKE_MODEL, FakeLLMClient
from ai_business_explorer.seed import DEFAULT_HUMAN_ACTOR_ID, DEFAULT_ORGANIZATION_ID
from ai_business_explorer.tools.base import ToolBox, ToolContext, ToolRegistry, ToolSideEffect
from ai_business_explorer.worker import run_once
from tests.conftest import Api
from tests.unit.test_tools import EchoTool

SECRET = "sk-test-secret-should-never-be-logged"


def _price(
    session: Session,
    *,
    per_call: str = "0",
    input_per_million: str = "0",
    output_per_million: str = "0",
    currency: str = "USD",
    kind: str = "llm",
    provider: str = "fake",
    model: str = FAKE_MODEL,
) -> Pricing:
    """新しい単価（seed の 0 USD より後に適用）。"""
    pricing = Pricing(
        kind=kind,
        provider=provider,
        model=model,
        input_per_million_tokens=Decimal(input_per_million),
        output_per_million_tokens=Decimal(output_per_million),
        per_call=Decimal(per_call),
        currency=currency,
        effective_from=datetime.now(UTC) - timedelta(seconds=1),
    )
    session.add(pricing)
    session.commit()
    return pricing


def _run(api: Api, exp_id: str, expect: int | None = None) -> Any:
    return api.post(f"/explorations/{exp_id}/stage-runs", {}, expect=expect)


def _employee_limits(api: Api, key: str, **limits: Any) -> None:
    employee = next(e for e in api.items("/ai-employees") if e["key"] == key)
    api.patch(f"/ai-employees/{employee['id']}", {"llm_config": {"provider": "fake", **limits}})


def _set_settings(client: TestClient, settings: Settings, **update: Any) -> Settings:
    changed = settings.model_copy(update=update)
    client.app.state.settings = changed  # type: ignore[attr-defined]
    return changed


# ---------------------------------------------------------------------- 記録


def test_llm_calls_and_costs_are_recorded(
    api: Api, session: Session, as_role: Callable[[str], Api]
) -> None:
    _price(session, per_call="0.01", input_per_million="2", output_per_million="4")
    exp = api.exploration()
    run = _run(api, exp["id"])
    [ex] = run["executions"]
    assert ex["cost_currency"] == "USD"
    assert Decimal(ex["cost_limit"]) == Decimal("1")

    viewer = as_role("viewer")
    [call] = viewer.items(f"/executions/{ex['id']}/llm-calls")
    assert call["provider"] == "fake"
    assert call["model"] == FAKE_MODEL
    assert call["prompt_key"] == "idea_generator"
    assert call["prompt_hash"] == ex["prompt_hash"]
    assert call["status"] == "succeeded"
    assert call["classification"] == "internal"
    assert call["payload_mode"] == "full"
    expected = (
        Decimal("0.01")
        + Decimal(call["input_tokens"]) * 2 / Decimal(1_000_000)
        + Decimal(call["output_tokens"]) * 4 / Decimal(1_000_000)
    ).quantize(Decimal("0.00000001"))
    assert Decimal(call["cost_amount"]) == expected
    assert Decimal(ex["cost_amount"]) == expected
    assert call["pricing_id"] is not None
    assert viewer.items(f"/executions/{ex['id']}/tool-calls") == []

    # 本文は admin のみ。送った内容と応答が残る
    viewer.get(f"/llm-calls/{call['id']}/payload", expect=403)
    as_role("reviewer").get(f"/llm-calls/{call['id']}/payload", expect=403)
    payload = api.get(f"/llm-calls/{call['id']}/payload")
    assert payload["request"]["prompt_key"] == "idea_generator"
    assert payload["request"]["messages"]
    assert payload["response"]["structured"]


def test_cost_summary_by_month_and_exploration(
    api: Api, session: Session, as_role: Callable[[str], Api]
) -> None:
    _price(session, per_call="0.25")
    exp_a = api.exploration()
    exp_b = api.exploration()
    _run(api, exp_a["id"])
    _run(api, exp_b["id"])
    rerun_of = api.items(f"/explorations/{exp_b['id']}/stage-runs")[0]["id"]
    api.post(f"/explorations/{exp_b['id']}/stage-runs", {"rerun_of_id": rerun_of})

    summary = as_role("viewer").get("/costs")
    assert summary["month"] == datetime.now(UTC).strftime("%Y-%m")
    [total] = summary["totals"]
    assert (total["currency"], Decimal(total["amount"]), total["llm_calls"]) == (
        "USD",
        Decimal("0.75"),
        3,
    )
    by_exp = {line["exploration_id"]: Decimal(line["amount"]) for line in summary["by_exploration"]}
    assert by_exp == {exp_a["id"]: Decimal("0.25"), exp_b["id"]: Decimal("0.5")}
    [org_budget] = summary["budgets"]
    assert org_budget["budget_id"] is None  # 行がないので設定の既定値（R-21）
    assert (Decimal(org_budget["monthly_limit"]), org_budget["mode"]) == (Decimal(100), "hard")
    assert Decimal(org_budget["spent"]) == Decimal("0.75")
    assert Decimal(org_budget["remaining"]) == Decimal("99.25")

    assert api.get("/costs?month=2020-01")["totals"] == []
    api.get("/costs?month=2026-13", expect=422)


# ---------------------------------------------------------------------- 起動時の予算


def test_launch_is_rejected_when_the_budget_cannot_cover_the_execution_limit(
    api: Api, session: Session
) -> None:
    exp = api.exploration()
    _put_budget(api, {"monthly_limit": "0.5"})
    res = api.client.post(f"/api/v1/explorations/{exp['id']}/stage-runs", json={}, headers=api.h)
    assert res.status_code == 409
    assert res.json()["error"] == "BudgetExceededError"
    assert res.json()["detail"].startswith("budget_exceeded:")
    assert api.items(f"/explorations/{exp['id']}/stage-runs") == []

    # soft は止めない
    _put_budget(api, {"monthly_limit": "0.5", "mode": "soft"})
    assert _run(api, exp["id"])["status"] == "succeeded"


def test_exploration_budget_is_checked_too(api: Api) -> None:
    exp = api.exploration()
    other = api.exploration()
    _put_budget(api, {"exploration_id": exp["id"], "monthly_limit": "0.1"})
    _run(api, exp["id"], expect=409)
    assert _run(api, other["id"])["status"] == "succeeded"


def test_queued_executions_reserve_their_limit(
    client: TestClient, api: Api, settings: Settings
) -> None:
    _set_settings(client, settings, execution_mode="async")
    _put_budget(api, {"monthly_limit": "1.5"})
    first = api.exploration()
    assert _run(api, first["id"])["status"] == "queued"
    # 待機中の実行の上限（1 USD）を確保した残りは 0.5 USD で、次の実行の上限に足りない
    res = api.client.post(
        f"/api/v1/explorations/{api.exploration()['id']}/stage-runs", json={}, headers=api.h
    )
    assert res.status_code == 409
    statuses = api.get("/costs")["budgets"]
    assert Decimal(statuses[0]["reserved"]) == Decimal(1)


def test_launch_requires_pricing_in_the_budget_currency(api: Api, session: Session) -> None:
    exp = api.exploration()
    _price(session, per_call="0.01", currency="EUR")
    res = api.client.post(f"/api/v1/explorations/{exp['id']}/stage-runs", json={}, headers=api.h)
    assert res.status_code == 409
    assert "currency" in res.json()["detail"]


# ---------------------------------------------------------------------- 実行中の上限


def test_execution_cost_limit_stops_and_fails_the_run(api: Api, session: Session) -> None:
    _price(session, per_call="0.6")
    _employee_limits(api, "idea_generator", max_cost_per_execution="0.5")
    exp = api.exploration()
    run = _run(api, exp["id"])
    assert run["status"] == "failed"
    [ex] = run["executions"]
    assert ex["error_type"] == "budget_exceeded"
    assert Decimal(ex["cost_limit"]) == Decimal("0.5")
    # 発生した費用は記録して計上する（E-07）。出力は保存しない
    assert Decimal(ex["cost_amount"]) == Decimal("0.6")
    assert len(api.items(f"/executions/{ex['id']}/llm-calls")) == 1
    assert session.scalars(select(Analysis)).all() == []
    assert api.items(f"/explorations/{exp['id']}/ideas") == []


def test_monthly_budget_used_up_while_queued_stops_before_calling_the_llm(
    client: TestClient,
    api: Api,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    async_settings = _set_settings(client, settings, execution_mode="async")
    exp = api.exploration()
    run = _run(api, exp["id"])
    _put_budget(api, {"monthly_limit": "0"})
    run_once(
        session_factory,
        async_settings,
        build_default_registry(),
        ToolRegistry(),
        "w",
    )
    done = api.get(f"/stage-runs/{run['id']}")
    assert done["status"] == "failed"
    [ex] = done["executions"]
    assert ex["error_type"] == "budget_exceeded"
    assert api.items(f"/executions/{ex['id']}/llm-calls") == []


def _meter(session: Session, api: Api, **limits: Any) -> ExecutionMeter:
    if limits:
        _employee_limits(api, "idea_generator", **limits)
    exp = api.exploration()
    run = _run(api, exp["id"])
    scope_to_organization(session, DEFAULT_ORGANIZATION_ID)
    execution = session.get(Execution, UUID(run["executions"][0]["id"]))
    assert execution is not None
    return ExecutionMeter(
        session,
        Settings(app_env="test", database_url="postgresql+psycopg://x"),
        execution,
        UUID(exp["id"]),
        DataClassification.INTERNAL,
    )


def test_llm_and_tool_call_counts_are_limited(api: Api, session: Session) -> None:
    meter = _meter(session, api, max_llm_calls=1, max_tool_calls=1)
    llm = meter.wrap(FakeLLMClient())
    request = _request()
    llm.complete(request)
    with pytest.raises(BudgetExceededError, match="LLM call limit 1"):
        llm.complete(request)

    box = _toolbox(meter)
    box.call("echo", {"text": "a"})
    with pytest.raises(BudgetExceededError, match="tool call limit 1"):
        box.call("echo", {"text": "b"})


def test_tool_calls_are_recorded_with_cost(api: Api, session: Session) -> None:
    _price(session, kind="tool", provider="echo", model="", per_call="0.02")
    meter = _meter(session, api)
    before = meter.execution.cost_amount
    _toolbox(meter).call("echo", {"text": "hi"})
    [call] = session.scalars(
        select(ToolCall).where(ToolCall.execution_id == meter.execution.id)
    ).all()
    assert (call.tool_name, call.status, call.cost_amount) == ("echo", "succeeded", Decimal("0.02"))
    assert call.input == {"text": "hi"}
    assert meter.execution.cost_amount == before + Decimal("0.02")
    api.get(f"/executions/{meter.execution.id}/tool-calls")


def test_max_tokens_is_capped(api: Api, session: Session) -> None:
    meter = _meter(session, api, max_tokens=100)
    seen: list[int | None] = []

    class _Recording(FakeLLMClient):
        def complete(self, request: Any) -> Any:
            seen.append(request.max_tokens)
            return super().complete(request)

    meter.wrap(_Recording()).complete(_request(max_tokens=500))
    assert seen == [100]


# ------------------------------------------------------------ 取り消し・失敗の費用（E-07）


def test_failed_llm_calls_are_recorded(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    scope_to_organization(session, DEFAULT_ORGANIZATION_ID)
    service = StageRunService(
        session,
        settings,
        build_default_registry(),
        ToolRegistry(),
        llm_client_factory=lambda _: FakeLLMClient(fail_with=LLMError("upstream 500")),
    )
    run = service.run_exploration_stage(human, UUID(exp["id"]), ExplorationStageRunCommand())
    [execution] = service.executions_for(run.id)
    assert execution.error_type == "llm_error"
    [call] = session.scalars(select(LLMCall).where(LLMCall.execution_id == execution.id)).all()
    assert (call.status, call.error_type, call.error_message) == (
        "failed",
        "llm_error",
        "upstream 500",
    )


def test_cancelled_execution_keeps_the_cost_of_calls_already_sent(
    client: TestClient,
    api: Api,
    session: Session,
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    _price(session, per_call="0.3")
    async_settings = _set_settings(client, settings, execution_mode="async")
    exp = api.exploration()
    run = _run(api, exp["id"])

    def cancel_then_answer(payload: dict[str, Any]) -> Any:
        with session_factory() as other:
            scope_to_organization(other, DEFAULT_ORGANIZATION_ID)
            actor = other.get(Actor, DEFAULT_HUMAN_ACTOR_ID)
            assert actor is not None
            StageRunService(other, async_settings, build_default_registry(), ToolRegistry()).cancel(
                actor, UUID(run["id"])
            )
        return DEFAULT_RESPONDERS["idea_generator"](payload)

    scope_to_organization(session, DEFAULT_ORGANIZATION_ID)
    llm = FakeLLMClient(responders={"idea_generator": cancel_then_answer})
    StageRunService(
        session, async_settings, build_default_registry(), ToolRegistry(), lambda _: llm
    ).execute(UUID(run["id"]), "w")

    done = api.get(f"/stage-runs/{run['id']}")
    assert done["status"] == "cancelled"
    [ex] = done["executions"]
    assert ex["status"] == "cancelled"
    assert Decimal(ex["cost_amount"]) == Decimal("0.3")
    assert Decimal(api.get("/costs")["totals"][0]["amount"]) == Decimal("0.3")


# ------------------------------------------------------------ 本文の保存（R-16）・秘密情報


def test_confidential_payload_is_not_stored(
    client: TestClient, api: Api, session: Session, settings: Settings
) -> None:
    _set_settings(client, settings, llm_max_classification={"fake": "confidential"})
    exp = api.post("/explorations", {"title": "t", "theme": "t", "classification": "confidential"})
    run = _run(api, exp["id"])
    [call] = api.items(f"/executions/{run['executions'][0]['id']}/llm-calls")
    assert (call["classification"], call["payload_mode"]) == ("confidential", "none")
    api.get(f"/llm-calls/{call['id']}/payload", expect=404)
    assert session.scalars(select(LLMCallPayload)).all() == []


def test_payload_mode_none_setting_stores_no_payload(
    client: TestClient, api: Api, session: Session, settings: Settings
) -> None:
    _set_settings(client, settings, llm_payload_mode="none")
    run = _run(api, api.exploration()["id"])
    [call] = api.items(f"/executions/{run['executions'][0]['id']}/llm-calls")
    assert call["payload_mode"] == "none"
    assert session.scalars(select(LLMCallPayload)).all() == []


def test_api_key_is_never_logged(
    client: TestClient, api: Api, session: Session, settings: Settings
) -> None:
    _set_settings(client, settings, llm_api_key=SECRET)
    _run(api, api.exploration()["id"])
    for table in ("llm_calls", "llm_call_payloads", "tool_calls", "executions", "audit_events"):
        rows = session.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")).scalars()  # noqa: S608  固定のテーブル名
        assert all(SECRET not in row for row in rows), table


def test_expired_payloads_are_deleted_and_metadata_is_kept(api: Api, session: Session) -> None:
    run = _run(api, api.exploration()["id"])
    [call] = api.items(f"/executions/{run['executions'][0]['id']}/llm-calls")
    session.execute(text("UPDATE llm_call_payloads SET created_at = now() - interval '91 days'"))
    session.commit()
    assert delete_expired_payloads(session, retention_days=90) == 1
    api.get(f"/llm-calls/{call['id']}/payload", expect=404)
    [kept] = api.items(f"/executions/{run['executions'][0]['id']}/llm-calls")
    assert kept["payload_deleted_at"] is not None
    assert delete_expired_payloads(session, retention_days=90) == 0


# ---------------------------------------------------------------------- 予算の設定（admin）


def _put_budget(api: Api, body: dict[str, Any], expect: int = 200) -> Any:
    res = api.client.put("/api/v1/budgets", json=body, headers=api.h)
    assert res.status_code == expect, res.text
    return res.json()


def test_budgets_are_managed_by_admin(
    api: Api, as_role: Callable[[str], Api], system_api: Api
) -> None:
    body = {"monthly_limit": "50", "currency": "USD", "mode": "hard"}
    for role in ("viewer", "member", "reviewer"):
        _put_budget(as_role(role), body, expect=403)
    _put_budget(system_api, body, expect=403)
    created = _put_budget(api, body)
    updated = _put_budget(api, {**body, "monthly_limit": "60", "mode": "soft"})
    assert updated["id"] == created["id"]
    assert (Decimal(updated["monthly_limit"]), updated["mode"]) == (Decimal(60), "soft")
    assert [b["id"] for b in as_role("viewer").items("/budgets")] == [created["id"]]
    _put_budget(api, {**body, "currency": "usd"}, expect=422)
    _put_budget(api, {**body, "monthly_limit": "-1"}, expect=422)

    member = as_role("member")
    res = member.client.delete(f"/api/v1/budgets/{created['id']}", headers=member.h)
    assert res.status_code == 403
    res = api.client.delete(f"/api/v1/budgets/{created['id']}", headers=api.h)
    assert res.status_code == 204
    assert api.items("/budgets") == []


def test_cost_and_budget_data_are_isolated_by_organization(
    client: TestClient, api: Api, session: Session
) -> None:
    from tests.conftest import make_human

    run = _run(api, api.exploration()["id"])
    [call] = api.items(f"/executions/{run['executions'][0]['id']}/llm-calls")
    session.execute(
        text("INSERT INTO organizations (id, name) VALUES (:id, 'Other')"),
        {"id": "00000000-0000-7000-8000-000000000999"},
    )
    session.commit()
    other = Api(client, make_human(session, "admin", UUID("00000000-0000-7000-8000-000000000999")))
    other.get(f"/executions/{run['executions'][0]['id']}/llm-calls", expect=404)
    other.get(f"/llm-calls/{call['id']}/payload", expect=404)
    assert other.get("/costs")["totals"] == []


def _request(max_tokens: int | None = None) -> Any:
    from ai_business_explorer.llm.base import LLMMessage, LLMRequest

    return LLMRequest(
        model=FAKE_MODEL,
        system="s",
        messages=[LLMMessage(role="user", content='{"x": 1}')],
        prompt_key="test",
        prompt_version="v1",
        max_tokens=max_tokens,
    )


def _toolbox(meter: ExecutionMeter) -> ToolBox:
    registry = ToolRegistry()
    registry.register(EchoTool())
    context = ToolContext(execution_id=meter.execution.id, exploration_id=meter.exploration_id)
    return ToolBox(registry, ["echo"], [ToolSideEffect.READ_ONLY], context, recorder=meter)
