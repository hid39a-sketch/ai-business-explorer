"""実装と prompt_key の一致（第2回仕様 17章 C1）。実際の LLM には接続しない。

- 新規作成：実装と prompt_key が一致しなければ 422。
- 更新：実装・prompt_key・prompt_version を変えて不一致になるなら 422。一致させる更新は通す。
- 既存の不一致の AI社員（経過措置）：自動では移行しない。v1 契約で実行でき、v2 契約には
  切り替わらない。実装・Prompt を変えない更新は通す。
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.application.commands import ExplorationStageRunCommand
from ai_business_explorer.application.stage_runs import StageRunService
from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import Actor, AIEmployee, Analysis
from ai_business_explorer.llm.fake import FakeLLMClient
from ai_business_explorer.tools.base import ToolRegistry
from tests.conftest import Api

MISMATCH = "does not match implementation 'idea_generator'"


def _payload(key: str, **fields: Any) -> dict[str, Any]:
    return {
        "key": key,
        "name": key,
        "role": "副担当",
        "stage_key": "idea_generation",
        "implementation_key": "idea_generator",
        "prompt_key": "idea_generator",
        "prompt_version": "v1",
        "status": "active",
        "llm_config": {"provider": "fake", "model": "fake-model-v1"},
        **fields,
    }


def _status(api: Api, method: str, path: str, body: dict[str, Any]) -> Any:
    return api.client.request(method, f"/api/v1{path}", json=body, headers=api.h)


def _legacy(api: Api, session: Session, key: str, prompt: tuple[str, str]) -> Any:
    """この規則の前からある、実装と prompt_key が一致しない AI社員（DB で直接作る）。"""
    employee = api.post("/ai-employees", _payload(key))
    row = session.get(AIEmployee, UUID(employee["id"]))
    assert row is not None
    row.prompt_key, row.prompt_version = prompt
    session.commit()
    return employee


def test_create_rejects_a_mismatched_prompt_key(api: Api) -> None:
    for version in ("v1", "v2", "v3", "v4"):
        res = _status(
            api,
            "POST",
            "/ai-employees",
            _payload(f"ig_mr_{version}", prompt_key="market_researcher", prompt_version=version),
        )
        assert res.status_code == 422, version
        assert MISMATCH in res.json()["detail"]
    api.post("/ai-employees", _payload("ig_ok"))


def test_update_rejects_changes_that_make_a_mismatch(api: Api) -> None:
    employee = api.post("/ai-employees", _payload("ig_u"))
    path = f"/ai-employees/{employee['id']}"
    res = _status(api, "PATCH", path, {"prompt_key": "market_researcher"})
    assert res.status_code == 422
    assert MISMATCH in res.json()["detail"]
    res = _status(api, "PATCH", path, {"implementation_key": "market_researcher"})
    assert res.status_code == 422
    after = api.get(path)
    assert (after["implementation_key"], after["prompt_key"], after["prompt_version"]) == (
        "idea_generator",
        "idea_generator",
        "v1",
    )


def test_existing_mismatch_is_kept_but_cannot_be_changed_into_another_mismatch(
    api: Api, session: Session
) -> None:
    employee = _legacy(api, session, "ig_legacy", ("market_researcher", "v1"))
    path = f"/ai-employees/{employee['id']}"
    # 自動では移行しない
    assert api.get(path)["prompt_key"] == "market_researcher"
    # 実装・Prompt を変えない更新は通す
    api.patch(path, {"name": "renamed"})
    # 不一致のまま Prompt の版を変える更新は 422
    res = _status(api, "PATCH", path, {"prompt_version": "v4"})
    assert res.status_code == 422
    assert MISMATCH in res.json()["detail"]
    # 一致させる更新は通す
    api.patch(path, {"prompt_key": "idea_generator", "prompt_version": "v2"})
    after = api.get(path)
    assert (after["prompt_key"], after["prompt_version"]) == ("idea_generator", "v2")
    # prompt_key と prompt_version を変え、output_format を指定しないので、v2 のスキーマになる
    assert after["output_format"]["title"] == "IdeaGeneratorOutputV2"


def test_prompt_key_change_sets_the_contract_schema(api: Api, session: Session) -> None:
    """prompt_key だけを変えて一致させても、output_format は今の契約のスキーマになる。"""
    employee = _legacy(api, session, "ig_key_only", ("market_researcher", "v1"))
    row = session.get(AIEmployee, UUID(employee["id"]))
    assert row is not None
    row.output_format = {"type": "object"}  # 古い記録
    session.commit()
    path = f"/ai-employees/{employee['id']}"
    # Prompt を変えない更新では入れ直さない
    assert api.patch(path, {"name": "renamed"})["output_format"] == {"type": "object"}
    after = api.patch(path, {"prompt_key": "idea_generator"})
    assert after["output_format"]["title"] == "IdeaGeneratorOutput"


def _idea_reply(claims: int) -> dict[str, Any]:
    return {
        "summary": "候補",
        "ideas": [{"title": "候補"}],
        "claims": [{"id": f"c{i}", "text": "推測", "kind": "speculation"} for i in range(claims)],
    }


def test_existing_mismatch_runs_as_contract_v1_and_never_v2(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    """idea_generator の実装に market_researcher v4（契約 v2 の Prompt）でも、v1 契約で動く。"""
    seed_primary = next(e for e in api.items("/ai-employees") if e["key"] == "idea_generator")
    row = session.get(AIEmployee, UUID(seed_primary["id"]))
    assert row is not None
    row.prompt_key, row.prompt_version = "market_researcher", "v4"
    session.commit()
    # Fake の応答は prompt_key で選ばれるので、アイデア生成の形を返すようにする
    llm = FakeLLMClient(responders={"market_researcher": lambda _: _idea_reply(11)})
    service = StageRunService(
        session,
        settings,
        build_default_registry(),
        ToolRegistry(),
        llm_client_factory=lambda _: llm,
    )
    exp = api.exploration()
    run = service.run_exploration_stage(human, UUID(exp["id"]), ExplorationStageRunCommand())
    assert run.status == "succeeded"
    [execution] = service.executions_for(run.id)
    assert (execution.prompt_key, execution.prompt_version) == ("market_researcher", "v4")
    [analysis] = session.scalars(select(Analysis)).all()
    # v2 契約なら claims 11件は失敗する。v1 契約なので成功し、schema_version も v1
    assert analysis.schema_version == "idea_generation.v1"
    assert len(analysis.body["claims"]) == 11
