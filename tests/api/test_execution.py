"""ステージ実行と実行履歴（成功・失敗）、バージョン追跡。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.application.commands import ExplorationStageRunCommand
from ai_business_explorer.application.stage_runs import StageRunService
from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import Actor, Analysis, AuditEvent, Idea
from ai_business_explorer.llm.base import LLMError
from ai_business_explorer.llm.fake import FakeLLMClient
from ai_business_explorer.prompts.loader import load_prompt
from ai_business_explorer.tools.base import ToolRegistry
from tests.conftest import Api


def test_idea_generation_success_records_execution_and_candidates(api: Api) -> None:
    exp = api.exploration()
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {"research_question": "何が有望か"})
    assert run["status"] == "succeeded"
    assert run["trigger"] == "initial"
    assert run["attempt_no"] == 1
    ex = run["executions"][0]
    assert ex["status"] == "succeeded"
    assert ex["error_type"] is None
    assert ex["prompt_key"] == "idea_generator"
    assert ex["prompt_version"] == "v1"
    assert ex["prompt_hash"] == load_prompt("idea_generator", "v1").sha256
    assert ex["llm_provider"] == "fake"
    assert ex["llm_model"] == "fake-model-v1"
    assert ex["code_version"] == "test-sha"
    assert ex["ai_employee_version"] == 1
    assert ex["ai_employee_snapshot"]["key"] == "idea_generator"
    assert ex["usage"]["llm_calls"] == 1
    assert ex["input"]["research_question"] == "何が有望か"

    ideas = api.items(f"/explorations/{exp['id']}/ideas")
    assert len(ideas) == 3
    assert {i["adoption_status"] for i in ideas} == {"candidate"}
    assert {i["origin_type"] for i in ideas} == {"ai"}
    analysis_id = ex["output"]["analysis_id"]
    assert {i["origin_analysis_id"] for i in ideas} == {analysis_id}
    analysis = api.get(f"/analyses/{analysis_id}")
    assert analysis["review_status"] == "pending_review"
    assert analysis["schema_version"] == "idea_generation.v1"


def test_ai_generated_candidate_cannot_run_stages_until_adopted(api: Api) -> None:
    exp = api.exploration()
    api.post(f"/explorations/{exp['id']}/stage-runs", {})
    idea = api.items(f"/explorations/{exp['id']}/ideas")[0]
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"}, expect=409)
    api.post(f"/ideas/{idea['id']}/adopt", {}, expect=200)
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    assert run["status"] == "succeeded"
    # AI 生成元の分析が前段の入力として記録される
    assert idea["origin_analysis_id"] in run["input_snapshot"]["analysis_ids"]


def test_rejected_idea_cannot_run_stages(api: Api) -> None:
    exp = api.exploration()
    idea = api.post(f"/explorations/{exp['id']}/ideas", {"title": "A"})
    api.post(f"/ideas/{idea['id']}/reject", {}, expect=200)
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"}, expect=409)


def test_stage_rules(api: Api, system_api: Api) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    # 前段が未実行
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "competitor_research"}, expect=409)
    # human_review / idea_generation はアイデア単位で AI 実行できない
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "human_review"}, expect=422)
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "idea_generation"}, expect=422)
    # 人間以外は起動できない
    system_api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"}, expect=403)
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    # 担当 AI社員がいないステージ
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "competitor_research"}, expect=409)
    # 再実行には rerun_of_id が必要
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"}, expect=409)


def test_archived_exploration_cannot_run(api: Api) -> None:
    exp = api.exploration()
    api.patch(f"/explorations/{exp['id']}", {"status": "archived"})
    api.post(f"/explorations/{exp['id']}/stage-runs", {}, expect=409)


def test_unavailable_llm_provider_records_failed_execution(api: Api) -> None:
    exp = api.exploration()
    ig = next(e for e in api.items("/ai-employees") if e["key"] == "idea_generator")
    api.patch(f"/ai-employees/{ig['id']}", {"llm_config": {"provider": "openai", "model": "x"}})
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    assert run["status"] == "failed"
    ex = run["executions"][0]
    assert ex["status"] == "failed"
    assert ex["error_type"] == "llm_error"
    assert "not available" in ex["error_message"]
    assert ex["output"] is None
    assert api.items(f"/explorations/{exp['id']}/ideas") == []
    assert api.items(f"/explorations/{exp['id']}/analyses") == []
    # 失敗した試行は rerun_of_id で再実行できる
    api.patch(f"/ai-employees/{ig['id']}", {"llm_config": {"provider": "fake"}})
    rerun = api.post(f"/explorations/{exp['id']}/stage-runs", {"rerun_of_id": run["id"]})
    assert rerun["status"] == "succeeded"
    assert rerun["executions"][0]["ai_employee_version"] == 3


def test_llm_error_is_recorded_and_partial_output_rolled_back(
    session: Session, settings: Settings, human: Actor, api: Api
) -> None:
    exp = api.exploration()
    service = StageRunService(
        session,
        settings,
        build_default_registry(),
        ToolRegistry(),
        llm_client_factory=lambda _: FakeLLMClient(fail_with=LLMError("upstream 500")),
    )
    from uuid import UUID

    run = service.run_exploration_stage(human, UUID(exp["id"]), ExplorationStageRunCommand())
    assert run.status == "failed"
    [execution] = service.executions_for(run.id)
    assert execution.error_type == "llm_error"
    assert execution.error_message == "upstream 500"
    assert execution.finished_at is not None
    assert session.scalars(select(Idea)).all() == []
    assert session.scalars(select(Analysis)).all() == []
    actions = {a.action for a in session.scalars(select(AuditEvent)).all()}
    assert {"started", "failed"} <= actions


def test_invalid_agent_output_is_validation_error(
    session: Session, settings: Settings, human: Actor, api: Api
) -> None:
    from uuid import UUID

    exp = api.exploration()
    bad = FakeLLMClient(responders={"idea_generator": lambda _: {"summary": "x", "ideas": []}})
    service = StageRunService(
        session,
        settings,
        build_default_registry(),
        ToolRegistry(),
        llm_client_factory=lambda _: bad,
    )
    run = service.run_exploration_stage(human, UUID(exp["id"]), ExplorationStageRunCommand())
    [execution] = service.executions_for(run.id)
    assert execution.error_type == "validation_error"


def test_execution_lookup(api: Api) -> None:
    exp = api.exploration()
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    ex_id = run["executions"][0]["id"]
    assert api.get(f"/executions/{ex_id}")["stage_run_id"] == run["id"]
    assert api.get(f"/stage-runs/{run['id']}")["id"] == run["id"]
    assert len(api.items(f"/explorations/{exp['id']}/stage-runs")) == 1
    assert len(api.items("/stages")) == 9
