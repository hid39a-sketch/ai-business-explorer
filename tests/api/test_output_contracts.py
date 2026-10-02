"""出力契約 v1・v2 の実行（第2回仕様 17章）。実際の LLM には接続しない。

- Prompt の版で出力契約が決まり、分析の schema_version に記録される。
- 契約 v2 は claims が最大10件。超えたら validation_error で、費用は記録する（E-07）。
- 契約 v1（idea_generator v1・market_researcher v1〜v3）の挙動は変えない。
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.application import ai_employees
from ai_business_explorer.application.commands import ExplorationStageRunCommand
from ai_business_explorer.application.stage_runs import StageRunService
from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import Actor, Analysis, Execution, LLMCall
from ai_business_explorer.llm.fake import FakeLLMClient
from ai_business_explorer.tools.base import ToolRegistry
from tests.api.test_costs import _price
from tests.conftest import Api


def _employee(api: Api, key: str) -> Any:
    return next(e for e in api.items("/ai-employees") if e["key"] == key)


def _set_prompt(api: Api, key: str, version: str) -> None:
    api.patch(f"/ai-employees/{_employee(api, key)['id']}", {"prompt_version": version})


def _idea_reply(claims: int) -> dict[str, Any]:
    return {
        "summary": "候補を作成した",
        "ideas": [{"title": "候補", "summary": "推定で2割程度", "problem": "仮説"}],
        "claims": [{"id": f"c{i}", "text": "推測", "kind": "speculation"} for i in range(claims)],
    }


def _run_ideas(session: Session, settings: Settings, human: Actor, api: Api, claims: int) -> Any:
    exp = api.exploration()
    llm = FakeLLMClient(responders={"idea_generator": lambda _: _idea_reply(claims)})
    service = StageRunService(
        session,
        settings,
        build_default_registry(),
        ToolRegistry(),
        llm_client_factory=lambda _: llm,
    )
    run = service.run_exploration_stage(human, UUID(exp["id"]), ExplorationStageRunCommand())
    [execution] = service.executions_for(run.id)
    return run, execution


def test_contract_v2_rejects_eleven_claims_and_records_the_cost(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    _price(session, input_per_million="2", output_per_million="4")
    assert _employee(api, "idea_generator")["prompt_version"] == "v2"
    run, execution = _run_ideas(session, settings, human, api, claims=11)
    assert run.status == "failed"
    assert execution.error_type == "validation_error"
    # 何も保存しない
    assert session.scalars(select(Analysis)).all() == []
    # LLM は呼ばれたので、費用は記録して予算に計上する（E-07）
    [call] = session.scalars(select(LLMCall).where(LLMCall.execution_id == execution.id)).all()
    assert call.cost_amount > Decimal(0)
    assert session.get(Execution, execution.id).cost_amount == call.cost_amount  # type: ignore[union-attr]


def test_contract_v2_accepts_ten_claims_and_records_schema_v2(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    run, execution = _run_ideas(session, settings, human, api, claims=10)
    assert run.status == "succeeded"
    [analysis] = session.scalars(select(Analysis)).all()
    assert analysis.schema_version == "idea_generation.v2"
    assert execution.prompt_version == "v2"


def test_contract_v1_keeps_its_behavior(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    """Prompt v1 の AI社員は契約 v1 のまま（claims の上限なし、schema_version は v1）。"""
    _set_prompt(api, "idea_generator", "v1")
    run, execution = _run_ideas(session, settings, human, api, claims=11)
    assert run.status == "succeeded"
    [analysis] = session.scalars(select(Analysis)).all()
    assert analysis.schema_version == "idea_generation.v1"
    assert execution.prompt_version == "v1"
    assert len(analysis.body["claims"]) == 11


def _market_run(api: Api, evidence: int) -> Any:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    for i in range(evidence):
        api.post(
            "/evidence",
            {
                "exploration_id": exp["id"],
                "idea_id": idea["id"],
                "source_type": "human_input",
                "title": f"統計{i}",
                "url": f"https://example.com/report/{i}",
                "quote": "市場は拡大している。",
            },
        )
    return api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})


def _market_analysis(api: Api, run: Any) -> Any:
    return api.get(f"/analyses/{run['executions'][0]['output']['analysis_id']}")


def test_market_researcher_contract_follows_the_prompt_version(api: Api) -> None:
    # seed の v4 → 契約 v2
    run = _market_run(api, evidence=2)
    assert run["status"] == "succeeded"
    assert _market_analysis(api, run)["schema_version"] == "market_research.v2"
    # v3（V-07）→ 契約 v1。v1・v2 も同じ
    for version in ("v3", "v2", "v1"):
        _set_prompt(api, "market_researcher", version)
        run = _market_run(api, evidence=2)
        assert run["status"] == "succeeded", version
        assert _market_analysis(api, run)["schema_version"] == "market_research.v1", version


def test_market_researcher_claim_limit_applies_only_to_contract_v2(api: Api) -> None:
    """Fake の市場調査は Evidence 1件ごとに主張を1つ作り、推論を1つ足す（10件で主張11件）。"""
    run = _market_run(api, evidence=10)
    assert run["status"] == "failed"
    assert run["executions"][0]["error_type"] == "validation_error"
    _set_prompt(api, "market_researcher", "v3")
    run = _market_run(api, evidence=10)
    assert run["status"] == "succeeded"
    assert len(_market_analysis(api, run)["claims"]) == 11


def test_prompt_version_without_contract_is_rejected(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prompt の版に対応する出力契約がなければ、AI社員に設定できない（422）。"""
    # Prompt のファイルはある、という状態にして、出力契約の確認だけを効かせる
    monkeypatch.setattr(ai_employees, "prompt_exists", lambda key, version: True)
    employee = _employee(api, "idea_generator")
    res = api.client.patch(
        f"/api/v1/ai-employees/{employee['id']}", json={"prompt_version": "v9"}, headers=api.h
    )
    assert res.status_code == 422
    assert "no output contract for idea_generator prompt idea_generator/v9" in res.json()["detail"]
    assert _employee(api, "idea_generator")["prompt_version"] == "v2"
