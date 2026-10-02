"""LLM の単価の登録と確認（第2回仕様 10章・7章 SC候補-12）。実際の LLM には接続しない。

- 単価は seed で登録する（公式にある別名と日付付き ID。架空の ID は作らない）。
- AI社員の作成、provider か model が変わる更新、割り当ての作成で、解決後の（provider, model）に
  有効な単価がなければ 422。provider と model が変わらない更新では確かめない。
- 起動時の確認（409）は変えない（test_execution で確認）。
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_business_explorer.domain.enums import PricingKind
from ai_business_explorer.infrastructure.db.models import AIEmployee, Pricing
from ai_business_explorer.llm.factory import DEFAULT_MODELS, resolve_llm_config
from ai_business_explorer.seed import seed
from tests.conftest import Api

SEEDED_CLAUDE_PRICES = {
    "claude-opus-5-5": (Decimal(4), Decimal(20)),
    "claude-sonnet-5-5": (Decimal(2), Decimal(10)),
    "claude-haiku-4-5": (Decimal(1), Decimal(5)),
    "claude-haiku-4-5-20251001": (Decimal(1), Decimal(5)),
}


def _claude_prices(session: Session) -> dict[str, tuple[Decimal, Decimal]]:
    rows = session.scalars(
        select(Pricing).where(
            Pricing.kind == PricingKind.LLM.value, Pricing.provider == "anthropic"
        )
    ).all()
    return {r.model: (r.input_per_million_tokens, r.output_per_million_tokens) for r in rows}


def test_seed_registers_claude_prices_without_invented_ids(session: Session) -> None:
    assert _claude_prices(session) == SEEDED_CLAUDE_PRICES
    # seed は行がなければ足すだけ（2回目で増えない）
    seed(session)
    assert len(_claude_prices(session)) == len(SEEDED_CLAUDE_PRICES)


def test_default_models_have_seeded_prices(session: Session) -> None:
    for provider in DEFAULT_MODELS:
        resolved_provider, model = resolve_llm_config({"provider": provider}, "fake")
        row = session.scalars(
            select(Pricing).where(
                Pricing.kind == PricingKind.LLM.value,
                Pricing.provider == resolved_provider,
                Pricing.model == model,
            )
        ).first()
        assert row is not None, (provider, model)


def _payload(key: str, **llm_config: Any) -> dict[str, Any]:
    return {
        "key": key,
        "name": key,
        "role": "副担当",
        "stage_key": "idea_generation",
        "implementation_key": "idea_generator",
        "prompt_key": "idea_generator",
        "prompt_version": "v1",
        "status": "active",
        "llm_config": llm_config,
    }


def _error(api: Api, method: str, path: str, body: dict[str, Any]) -> Any:
    res = api.client.request(method, f"/api/v1{path}", json=body, headers=api.h)
    assert res.status_code == 422, res.text
    return res.json()


def test_create_requires_pricing(api: Api) -> None:
    body = _error(api, "POST", "/ai-employees", _payload("ig_x", provider="fake", model="nope"))
    assert "no pricing for LLM fake/nope" in body["detail"]
    for i, model in enumerate(SEEDED_CLAUDE_PRICES):
        api.post("/ai-employees", _payload(f"ig_claude_{i}", provider="anthropic", model=model))
    # model を省略したら既定のモデル（claude-opus-5-5）で確かめる
    api.post("/ai-employees", _payload("ig_default", provider="anthropic"))


def test_update_checks_pricing_only_when_provider_or_model_changes(
    api: Api, session: Session
) -> None:
    employee = api.post("/ai-employees", _payload("ig_u", provider="fake"))
    path = f"/ai-employees/{employee['id']}"
    body = _error(api, "PATCH", path, {"llm_config": {"provider": "fake", "model": "nope"}})
    assert "no pricing for LLM fake/nope" in body["detail"]
    assert api.get(path)["llm_config"]["model"] is None
    api.patch(path, {"llm_config": {"provider": "anthropic", "model": "claude-sonnet-5-5"}})
    # 単価のないモデルの AI社員（この確認の前からある既存のデータ）は、DB で直接作る
    row = session.get(AIEmployee, UUID(employee["id"]))
    assert row is not None
    row.llm_config = {"provider": "fake", "model": "legacy-model"}
    session.commit()
    # provider・model が変わらない更新では確かめない（成功する）
    api.patch(path, {"name": "renamed"})
    api.patch(path, {"llm_config": {"provider": "fake", "model": "legacy-model", "max_tokens": 10}})
    # 変わる更新では確かめる
    _error(api, "PATCH", path, {"llm_config": {"provider": "fake", "model": "other-unpriced"}})


def test_assignment_requires_pricing(api: Api, session: Session) -> None:
    employee = api.post(
        "/ai-employees", _payload("ig_a", provider="anthropic", model="claude-haiku-4-5")
    )
    row = session.get(AIEmployee, UUID(employee["id"]))
    assert row is not None
    row.llm_config = {"provider": "anthropic", "model": "claude-unpriced"}
    session.commit()
    body = _error(
        api,
        "POST",
        "/stage-assignments",
        {"stage_key": "idea_generation", "ai_employee_id": employee["id"], "role": "secondary"},
    )
    assert "no pricing for LLM anthropic/claude-unpriced" in body["detail"]
    api.patch(
        f"/ai-employees/{employee['id']}",
        {"llm_config": {"provider": "anthropic", "model": "claude-haiku-4-5"}},
    )
    api.post(
        "/stage-assignments",
        {"stage_key": "idea_generation", "ai_employee_id": employee["id"], "role": "secondary"},
    )
