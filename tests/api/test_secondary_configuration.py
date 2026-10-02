"""primary と secondary に同じ構成を使わない（V-08）。

同じ構成＝実装（implementation_key）・Prompt（key と version の両方）・解決後の（provider, model）が
すべて同じ。割り当ての作成時と、ステージ実行の起動時の両方で確かめ、違反は 422
（DomainValidationError）。実際の LLM には接続しない。
"""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_business_explorer.domain.enums import PricingKind
from ai_business_explorer.infrastructure.db.models import Pricing, StageRun
from ai_business_explorer.llm import factory
from ai_business_explorer.seed import FAKE_PRICING_EFFECTIVE_FROM
from tests.conftest import Api

SAME = "has the same implementation, prompt and model as the primary"

# このモジュールで使うモデル。AI社員の作成・更新・割り当てには単価が要る（第2回仕様 10章 SC候補-12）
PRICED_MODELS = [
    ("fake", "fake-model-other"),
    ("fake", "fake-model-b"),
    ("fake", "fake-model-next"),
    ("anthropic", "fake-model-v1"),
]


@pytest.fixture(autouse=True)
def _pricing(session: Session) -> None:
    for provider, model in PRICED_MODELS:
        session.add(
            Pricing(
                kind=PricingKind.LLM.value,
                provider=provider,
                model=model,
                input_per_million_tokens=Decimal(0),
                output_per_million_tokens=Decimal(0),
                per_call=Decimal(0),
                currency="USD",
                effective_from=FAKE_PRICING_EFFECTIVE_FROM,
            )
        )
    session.commit()


# seed の primary の Prompt の版（出力契約 v2。第2回仕様 17章）
SEED_PROMPT_VERSIONS = {"idea_generation": "v2", "market_research": "v4"}


def _employee(api: Api, key: str, stage_key: str = "idea_generation", **fields: Any) -> Any:
    implementation = "idea_generator" if stage_key == "idea_generation" else "market_researcher"
    body = {
        "key": key,
        "name": key,
        "role": "副担当",
        "stage_key": stage_key,
        "implementation_key": implementation,
        "prompt_key": implementation,
        "prompt_version": SEED_PROMPT_VERSIONS[stage_key],
        "status": "active",
        "llm_config": {"provider": "fake", "model": "fake-model-v1"},
        **fields,
    }
    return api.post("/ai-employees", body)


def _assign(api: Api, employee: Any, role: str = "secondary", expect: int = 201) -> Any:
    body = {"stage_key": employee["stage_key"], "ai_employee_id": employee["id"], "role": role}
    return api.post("/stage-assignments", body, expect=expect)


def _primary(api: Api, stage_key: str) -> Any:
    key = "idea_generator" if stage_key == "idea_generation" else "market_researcher"
    return next(e for e in api.items("/ai-employees") if e["key"] == key)


# ---------------------------------------------------------------------- 割り当ての作成時


def test_same_configuration_is_rejected_on_assignment(api: Api) -> None:
    """seed の primary（idea_generator / v2 / fake / fake-model-v1）と同じ構成は割り当てない。"""
    clone = _employee(api, "ig_clone")
    res = api.client.post(
        "/api/v1/stage-assignments",
        json={"stage_key": "idea_generation", "ai_employee_id": clone["id"], "role": "secondary"},
        headers=api.h,
    )
    assert res.status_code == 422
    assert res.json()["error"] == "DomainValidationError"
    assert SAME in res.json()["detail"]
    assert all(a["ai_employee_id"] != clone["id"] for a in api.items("/stage-assignments"))


@pytest.mark.parametrize(
    ("label", "fields"),
    [
        ("provider", {"llm_config": {"provider": "anthropic", "model": "fake-model-v1"}}),
        ("model", {"llm_config": {"provider": "fake", "model": "fake-model-other"}}),
        ("prompt key", {"prompt_key": "market_researcher"}),
    ],
)
def test_any_difference_is_allowed_on_assignment(
    api: Api, label: str, fields: dict[str, Any]
) -> None:
    _assign(api, _employee(api, "ig_" + label.replace(" ", "_"), **fields))


def test_different_prompt_version_is_allowed(api: Api) -> None:
    """同じ Prompt の別の版は別の Prompt（primary の market_researcher は seed の v4）。"""
    assert _primary(api, "market_research")["prompt_version"] == "v4"
    _assign(api, _employee(api, "mr_v3", "market_research", prompt_version="v3"))
    clone = _employee(api, "mr_v4", "market_research", prompt_version="v4")
    _assign(api, clone, expect=422)


def test_omitted_model_is_compared_after_resolution(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """モデルの省略と既定値の明示は、解決後に同じなら同じ構成。既定値が変われば判定も変わる。"""
    omitted = _employee(api, "ig_default", llm_config={"provider": "fake"})
    _assign(api, omitted, expect=422)
    monkeypatch.setitem(factory.DEFAULT_MODELS, "fake", "fake-model-next")
    _assign(api, omitted)


def test_new_primary_is_checked_against_existing_secondaries(api: Api) -> None:
    secondary = _employee(api, "ig_b", llm_config={"provider": "fake", "model": "fake-model-b"})
    _assign(api, secondary)
    primary = _primary(api, "idea_generation")
    [assignment] = [
        a for a in api.items("/stage-assignments") if a["ai_employee_id"] == primary["id"]
    ]
    res = api.client.delete(f"/api/v1/stage-assignments/{assignment['id']}", headers=api.h)
    assert res.status_code == 204
    same_as_secondary = _employee(
        api, "ig_c", llm_config={"provider": "fake", "model": "fake-model-b"}
    )
    _assign(api, same_as_secondary, role="primary", expect=422)
    _assign(api, primary, role="primary")


# ---------------------------------------------------------------------- 起動時


def _stage_runs(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(StageRun)) or 0


def test_existing_invalid_assignment_is_rejected_at_launch(api: Api, session: Session) -> None:
    """割り当て後に設定を変えて同じ構成になった secondary は、起動時に拒否する。"""
    secondary = _employee(api, "ig_b", llm_config={"provider": "fake", "model": "fake-model-b"})
    _assign(api, secondary)
    api.patch(
        f"/ai-employees/{secondary['id']}",
        {"llm_config": {"provider": "fake", "model": "fake-model-v1"}},
    )
    exp = api.exploration()
    before = _stage_runs(session)
    res = api.client.post(
        f"/api/v1/explorations/{exp['id']}/stage-runs",
        json={"secondary_ai_employee_ids": [secondary["id"]]},
        headers=api.h,
    )
    assert res.status_code == 422
    assert res.json()["error"] == "DomainValidationError"
    assert SAME in res.json()["detail"]
    # 実行記録も stage_run も作らない
    session.expire_all()
    assert _stage_runs(session) == before


def test_launch_compares_with_the_primary_actually_used(api: Api) -> None:
    """ai_employee_id で primary を差し替えた場合は、差し替え後の primary と比べる。"""
    secondary = _employee(api, "ig_b", llm_config={"provider": "fake", "model": "fake-model-b"})
    _assign(api, secondary)
    override = _employee(api, "ig_o", llm_config={"provider": "fake", "model": "fake-model-b"})
    exp = api.exploration()
    body = {"ai_employee_id": override["id"], "secondary_ai_employee_ids": [secondary["id"]]}
    api.post(f"/explorations/{exp['id']}/stage-runs", body, expect=422)


def test_idea_stage_launch_is_checked_too(api: Api) -> None:
    """アイデア単位のステージでも同じ（起動・再実行・差し戻しは同じ担当の選び方を通る）。"""
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    api.evidence(exp["id"], idea["id"])
    secondary = _employee(
        api,
        "mr_b",
        "market_research",
        llm_config={"provider": "fake", "model": "fake-model-b"},
    )
    _assign(api, secondary)
    api.patch(
        f"/ai-employees/{secondary['id']}",
        {"llm_config": {"provider": "fake", "model": "fake-model-v1"}},
    )
    body = {"stage_key": "market_research", "secondary_ai_employee_ids": [secondary["id"]]}
    api.post(f"/ideas/{idea['id']}/stage-runs", body, expect=422)
