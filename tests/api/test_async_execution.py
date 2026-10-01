"""非同期のステージ実行・取り消し・タイムアウト・heartbeat・primary / secondary。

第2回仕様 7章・9章。
"""

import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.application.commands import (
    ExplorationStageRunCommand,
    IdeaStageRunCommand,
)
from ai_business_explorer.application.common import utcnow
from ai_business_explorer.application.stage_runs import (
    HEARTBEAT_LOST_MESSAGE,
    StageRunService,
    fail_stale_runs,
)
from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    Analysis,
    Execution,
    Idea,
    StageRun,
)
from ai_business_explorer.infrastructure.db.repositories import scope_to_organization
from ai_business_explorer.llm.base import LLMError
from ai_business_explorer.llm.fake import DEFAULT_RESPONDERS, FakeLLMClient
from ai_business_explorer.seed import DEFAULT_HUMAN_ACTOR_ID, DEFAULT_ORGANIZATION_ID
from ai_business_explorer.tools.base import ToolRegistry, default_tool_registry
from ai_business_explorer.worker import _Heartbeat, run_once
from tests.api.test_rerun_send_back import _TestCompetitorResearcher
from tests.conftest import Api


@pytest.fixture
def async_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"execution_mode": "async"})


@pytest.fixture
def async_api(client: TestClient, api: Api, async_settings: Settings) -> Api:
    """受付だけをして、実行はワーカー（run_once）に任せる API。"""
    client.app.state.settings = async_settings  # type: ignore[attr-defined]
    return api


def _work(session_factory: sessionmaker[Session], settings: Settings) -> bool:
    return run_once(
        session_factory, settings, build_default_registry(), default_tool_registry, "test-worker"
    )


def _secondary(api: Api, stage_key: str, key: str, implementation: str, prompt: str) -> str:
    """同じステージに secondary の AI社員を登録して割り当てる（admin）。"""
    employee = api.post(
        "/ai-employees",
        {
            "key": key,
            "name": key,
            "role": "副担当",
            "stage_key": stage_key,
            "implementation_key": implementation,
            "prompt_key": prompt,
            "prompt_version": "v1",
            "status": "active",
        },
    )
    api.post(
        "/stage-assignments",
        {"stage_key": stage_key, "ai_employee_id": employee["id"], "role": "secondary"},
    )
    return str(employee["id"])


def _service(
    session: Session, settings: Settings, llm: Callable[[str], Any] | None = None
) -> StageRunService:
    scope_to_organization(session, DEFAULT_ORGANIZATION_ID)
    if llm is None:
        return StageRunService(session, settings, build_default_registry(), ToolRegistry())
    return StageRunService(
        session, settings, build_default_registry(), ToolRegistry(), llm_client_factory=llm
    )


# ---------------------------------------------------------------------- 受付とワーカー


def test_stage_run_is_accepted_as_queued_and_processed_by_the_worker(
    async_api: Api, session_factory: sessionmaker[Session], async_settings: Settings
) -> None:
    exp = async_api.exploration()
    run = async_api.post(f"/explorations/{exp['id']}/stage-runs", {})
    assert run["status"] == "queued"
    assert run["claimed_at"] is None
    assert run["worker_id"] is None
    [ex] = run["executions"]
    assert ex["status"] == "queued"
    assert ex["assignment_role"] == "primary"
    assert ex["started_at"] is None
    assert async_api.items(f"/explorations/{exp['id']}/ideas") == []

    assert _work(session_factory, async_settings) is True
    done = async_api.get(f"/stage-runs/{run['id']}")
    assert done["status"] == "succeeded"
    assert done["worker_id"] == "test-worker"
    assert done["claimed_at"] is not None
    assert done["heartbeat_at"] is not None
    assert done["executions"][0]["status"] == "succeeded"
    assert done["executions"][0]["started_at"] is not None
    assert len(async_api.items(f"/explorations/{exp['id']}/ideas")) == 3
    # 次のステージへの連鎖も自動の再実行もしない
    assert _work(session_factory, async_settings) is False
    assert len(async_api.items(f"/explorations/{exp['id']}/stage-runs")) == 1


def test_a_stage_cannot_be_started_twice_while_queued(async_api: Api) -> None:
    exp = async_api.exploration()
    run = async_api.post(f"/explorations/{exp['id']}/stage-runs", {})
    async_api.post(f"/explorations/{exp['id']}/stage-runs", {}, expect=409)
    # 終わっていない試行の再実行も受け付けない
    res = async_api.client.post(
        f"/api/v1/explorations/{exp['id']}/stage-runs",
        json={"rerun_of_id": run["id"]},
        headers=async_api.h,
    )
    assert res.status_code == 409
    assert "cancel it first" in res.json()["detail"]


def test_execute_is_a_no_op_unless_queued(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    service = _service(session, settings)
    service.execute(UUID(run["id"]), worker_id="other")
    again = api.get(f"/stage-runs/{run['id']}")
    assert again["worker_id"] == "sync"
    assert again["status"] == "succeeded"
    assert len(session.scalars(select(Analysis)).all()) == 1


# ---------------------------------------------------------------------- 取り消し


def test_human_can_cancel_a_queued_run(
    async_api: Api, session_factory: sessionmaker[Session], async_settings: Settings
) -> None:
    exp = async_api.exploration()
    run = async_api.post(f"/explorations/{exp['id']}/stage-runs", {})
    cancelled = async_api.post(f"/stage-runs/{run['id']}/cancel", expect=200)
    assert cancelled["status"] == "cancelled"
    assert cancelled["finished_at"] is not None
    assert [e["status"] for e in cancelled["executions"]] == ["cancelled"]
    # 取り消した実行はワーカーが拾わない・状態も上書きされない
    assert _work(session_factory, async_settings) is False
    assert async_api.get(f"/stage-runs/{run['id']}")["status"] == "cancelled"
    async_api.post(f"/stage-runs/{run['id']}/cancel", expect=409)
    # 人間が改めて再実行する（自動の再実行はしない）
    rerun = async_api.post(f"/explorations/{exp['id']}/stage-runs", {"rerun_of_id": run["id"]})
    assert rerun["trigger"] == "rerun"
    assert rerun["status"] == "queued"


def test_finished_runs_cannot_be_cancelled(api: Api) -> None:
    exp = api.exploration()
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    res = api.client.post(f"/api/v1/stage-runs/{run['id']}/cancel", headers=api.h)
    assert res.status_code == 409


def test_cancel_during_running_discards_output_and_skips_remaining_executions(
    async_api: Api,
    session: Session,
    session_factory: sessionmaker[Session],
    async_settings: Settings,
) -> None:
    exp = async_api.exploration()
    _secondary(async_api, "idea_generation", "ig_b", "idea_generator", "idea_generator")
    ig_b = next(e for e in async_api.items("/ai-employees") if e["key"] == "ig_b")
    run = async_api.post(
        f"/explorations/{exp['id']}/stage-runs", {"secondary_ai_employee_ids": [ig_b["id"]]}
    )

    def cancel_then_answer(payload: dict[str, Any]) -> Any:
        # LLM の応答待ちの間に、人間が別のリクエストで取り消す
        with session_factory() as other:
            scope_to_organization(other, DEFAULT_ORGANIZATION_ID)
            actor = other.get(Actor, DEFAULT_HUMAN_ACTOR_ID)
            assert actor is not None
            _service(other, async_settings).cancel(actor, UUID(run["id"]))
        return DEFAULT_RESPONDERS["idea_generator"](payload)

    llm = FakeLLMClient(responders={"idea_generator": cancel_then_answer})
    _service(session, async_settings, lambda _: llm).execute(UUID(run["id"]), "w")

    after = async_api.get(f"/stage-runs/{run['id']}")
    assert after["status"] == "cancelled"
    assert [e["status"] for e in after["executions"]] == ["cancelled", "cancelled"]
    assert after["executions"][0]["error_type"] is None
    # 取り消し後に返ってきた出力は保存しない
    assert session.scalars(select(Analysis)).all() == []
    assert session.scalars(select(Idea)).all() == []


def test_cancel_requires_member(
    async_api: Api, as_role: Callable[[str], Api], system_api: Api
) -> None:
    exp = async_api.exploration()
    run = async_api.post(f"/explorations/{exp['id']}/stage-runs", {})
    as_role("viewer").post(f"/stage-runs/{run['id']}/cancel", expect=403)
    system_api.post(f"/stage-runs/{run['id']}/cancel", expect=403)
    assert as_role("member").post(f"/stage-runs/{run['id']}/cancel", expect=200)["status"] == (
        "cancelled"
    )


# ---------------------------------------------------------------------- タイムアウト・heartbeat


def test_timeout_fails_the_run_before_calling_the_llm(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    calls: list[dict[str, Any]] = []
    llm = FakeLLMClient(responders={"idea_generator": lambda p: calls.append(p)})
    service = _service(
        session, settings.model_copy(update={"stage_run_timeout_seconds": 0}), lambda _: llm
    )
    run = service.run_exploration_stage(human, UUID(exp["id"]), ExplorationStageRunCommand())
    assert run.status == "failed"
    [execution] = service.executions_for(run.id)
    assert execution.error_type == "timeout"
    assert calls == []


def test_llm_calls_carry_the_per_call_timeout(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    seen: list[float | None] = []

    class _Recording(FakeLLMClient):
        def complete(self, request: Any) -> Any:
            seen.append(request.timeout_seconds)
            return super().complete(request)

    service = _service(
        session,
        settings.model_copy(update={"llm_call_timeout_seconds": 7.5}),
        lambda _: _Recording(),
    )
    service.run_exploration_stage(human, UUID(exp["id"]), ExplorationStageRunCommand())
    assert seen == [7.5]


def test_heartbeat_loss_fails_the_run_without_rerunning(
    async_api: Api,
    session: Session,
    session_factory: sessionmaker[Session],
    async_settings: Settings,
) -> None:
    exp = async_api.exploration()
    lost = async_api.post(f"/explorations/{exp['id']}/stage-runs", {})
    # ワーカーが取り出した後に止まった状態を作る
    stage_run = session.get(StageRun, UUID(lost["id"]))
    assert stage_run is not None
    stage_run.status = "running"
    stage_run.heartbeat_at = utcnow() - timedelta(minutes=10)
    execution = session.scalars(
        select(Execution).where(Execution.stage_run_id == stage_run.id)
    ).one()
    execution.status = "running"
    session.commit()

    assert fail_stale_runs(session, heartbeat_timeout_seconds=60) == [stage_run.id]
    after = async_api.get(f"/stage-runs/{lost['id']}")
    assert after["status"] == "failed"
    [ex] = after["executions"]
    assert (ex["status"], ex["error_type"], ex["error_message"]) == (
        "failed",
        "unexpected",
        HEARTBEAT_LOST_MESSAGE,
    )
    # 自動では再実行しない
    assert _work(session_factory, async_settings) is False
    assert async_api.get(f"/stage-runs/{lost['id']}")["status"] == "failed"


def test_worker_heartbeat_thread_refreshes_running_runs(
    async_api: Api, session: Session, session_factory: sessionmaker[Session]
) -> None:
    exp = async_api.exploration()
    run = async_api.post(f"/explorations/{exp['id']}/stage-runs", {})
    stage_run = session.get(StageRun, UUID(run["id"]))
    assert stage_run is not None
    old = utcnow() - timedelta(minutes=10)
    stage_run.status = "running"
    stage_run.heartbeat_at = old
    session.commit()
    with _Heartbeat(session_factory, stage_run.id, interval=0.01):
        time.sleep(0.2)
    session.refresh(stage_run)
    assert stage_run.heartbeat_at is not None
    assert stage_run.heartbeat_at > old
    assert fail_stale_runs(session, heartbeat_timeout_seconds=60) == []


def test_live_heartbeat_is_not_failed(async_api: Api, session: Session) -> None:
    exp = async_api.exploration()
    run = async_api.post(f"/explorations/{exp['id']}/stage-runs", {})
    stage_run = session.get(StageRun, UUID(run["id"]))
    assert stage_run is not None
    stage_run.status = "running"
    stage_run.heartbeat_at = utcnow()
    session.commit()
    assert fail_stale_runs(session, heartbeat_timeout_seconds=60) == []


# ---------------------------------------------------------------------- primary / secondary


def test_primary_and_secondary_run_with_independent_version_chains(api: Api) -> None:
    exp = api.exploration()
    secondary_id = _secondary(api, "idea_generation", "ig_b", "idea_generator", "idea_generator")
    body = {"secondary_ai_employee_ids": [secondary_id]}
    run = api.post(f"/explorations/{exp['id']}/stage-runs", body)
    assert run["status"] == "succeeded"
    primary, secondary = run["executions"]
    assert (primary["assignment_role"], secondary["assignment_role"]) == ("primary", "secondary")
    assert secondary["ai_employee_id"] == secondary_id
    assert {primary["status"], secondary["status"]} == {"succeeded"}
    # Idea 候補は primary の出力からだけ作る
    ideas = api.items(f"/explorations/{exp['id']}/ideas")
    assert len(ideas) == 3
    assert {i["origin_analysis_id"] for i in ideas} == {primary["output"]["analysis_id"]}
    assert secondary["output"]["idea_ids"] == []

    rerun = api.post(f"/explorations/{exp['id']}/stage-runs", {**body, "rerun_of_id": run["id"]})
    analyses = api.items(f"/explorations/{exp['id']}/analyses")
    chains: dict[str, list[tuple[int, str | None]]] = {}
    for a in analyses:
        chains.setdefault(a["ai_employee_id"], []).append((a["version_no"], a["supersedes_id"]))
    assert sorted(chains[secondary_id]) == [
        (1, None),
        (2, secondary["output"]["analysis_id"]),
    ]
    assert sorted(chains[primary["ai_employee_id"]]) == [
        (1, None),
        (2, primary["output"]["analysis_id"]),
    ]
    assert rerun["executions"][1]["assignment_role"] == "secondary"


def test_secondary_failure_does_not_fail_the_stage(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    secondary_id = _secondary(api, "idea_generation", "ig_b", "idea_generator", "idea_generator")
    calls = 0

    def second_call_fails(payload: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise LLMError("secondary upstream 500")
        return DEFAULT_RESPONDERS["idea_generator"](payload)

    llm = FakeLLMClient(responders={"idea_generator": second_call_fails})
    service = _service(session, settings, lambda _: llm)
    cmd = ExplorationStageRunCommand(secondary_ai_employee_ids=[UUID(secondary_id)])
    run = service.run_exploration_stage(human, UUID(exp["id"]), cmd)
    assert run.status == "succeeded"
    primary, secondary = service.executions_for(run.id)
    assert primary.status == "succeeded"
    assert (secondary.status, secondary.error_type) == ("failed", "llm_error")


def test_primary_failure_fails_the_stage_even_if_secondary_succeeds(
    api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    secondary_id = _secondary(api, "idea_generation", "ig_b", "idea_generator", "idea_generator")
    calls = 0

    def first_call_fails(payload: dict[str, Any]) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMError("primary upstream 500")
        return DEFAULT_RESPONDERS["idea_generator"](payload)

    llm = FakeLLMClient(responders={"idea_generator": first_call_fails})
    service = _service(session, settings, lambda _: llm)
    cmd = ExplorationStageRunCommand(secondary_ai_employee_ids=[UUID(secondary_id)])
    run = service.run_exploration_stage(human, UUID(exp["id"]), cmd)
    assert run.status == "failed"
    primary, secondary = service.executions_for(run.id)
    assert (primary.status, secondary.status) == ("failed", "succeeded")
    # secondary の出力から Idea 候補は作らない
    assert session.scalars(select(Idea)).all() == []


def test_secondary_analyses_are_not_inputs_to_later_stages(
    client: TestClient, api: Api, session: Session, settings: Settings, human: Actor
) -> None:
    exp = api.exploration()
    idea = api.adopted_idea(exp["id"])
    api.evidence(exp["id"], idea["id"])
    secondary_id = _secondary(
        api, "market_research", "mr_b", "market_researcher", "market_researcher"
    )
    mr = api.post(
        f"/ideas/{idea['id']}/stage-runs",
        {"stage_key": "market_research", "secondary_ai_employee_ids": [secondary_id]},
    )
    primary, secondary = mr["executions"]
    assert {primary["status"], secondary["status"]} == {"succeeded"}

    registry = client.app.state.agent_registry  # type: ignore[attr-defined]
    registry.register(_TestCompetitorResearcher())
    api.post(
        "/ai-employees",
        {
            "key": "test_competitor",
            "name": "TestCompetitor",
            "role": "test",
            "stage_key": "competitor_research",
            "implementation_key": "test_competitor_researcher",
            "prompt_key": "market_researcher",
            "prompt_version": "v1",
            "status": "active",
        },
    )
    scope_to_organization(session, DEFAULT_ORGANIZATION_ID)
    service = StageRunService(session, settings, registry, ToolRegistry())
    cr = service.run_idea_stage(
        human, UUID(idea["id"]), IdeaStageRunCommand(stage_key="competitor_research")
    )
    assert cr.status == "succeeded"
    inputs = cr.input_snapshot["analysis_ids"]
    assert primary["output"]["analysis_id"] in inputs
    assert secondary["output"]["analysis_id"] not in inputs


def test_secondary_selection_is_validated(api: Api) -> None:
    exp = api.exploration()
    ig = next(e for e in api.items("/ai-employees") if e["key"] == "idea_generator")
    unassigned = api.post(
        "/ai-employees",
        {
            "key": "ig_c",
            "name": "ig_c",
            "role": "r",
            "stage_key": "idea_generation",
            "implementation_key": "idea_generator",
            "prompt_key": "idea_generator",
            "prompt_version": "v1",
            "status": "active",
        },
    )
    secondary_id = _secondary(api, "idea_generation", "ig_b", "idea_generator", "idea_generator")
    path = f"/explorations/{exp['id']}/stage-runs"
    for ids in ([unassigned["id"]], [ig["id"]], [secondary_id, secondary_id]):
        api.post(path, {"secondary_ai_employee_ids": ids}, expect=422)
    assert api.items(f"/explorations/{exp['id']}/stage-runs") == []


# ---------------------------------------------------------------------- 担当の設定（admin）


def test_stage_assignments_are_managed_by_admin(
    api: Api, as_role: Callable[[str], Api], system_api: Api
) -> None:
    employee = api.post(
        "/ai-employees",
        {"key": "mr_x", "name": "x", "role": "r", "stage_key": "market_research"},
    )
    body = {"stage_key": "market_research", "ai_employee_id": employee["id"], "role": "secondary"}
    for role in ("viewer", "member", "reviewer"):
        as_role(role).post("/stage-assignments", body, expect=403)
    system_api.post("/stage-assignments", body, expect=403)
    created = as_role("admin").post("/stage-assignments", body)
    assert created["role"] == "secondary"
    # 閲覧は viewer から
    listed = as_role("viewer").items("/stage-assignments?stage_key=market_research")
    assert {a["id"] for a in listed} >= {created["id"]}
    # 同じ社員の重複・primary の重複・担当外のステージは拒否
    api.post("/stage-assignments", body, expect=409)
    api.post("/stage-assignments", {**body, "role": "primary"}, expect=409)
    api.post("/stage-assignments", {**body, "stage_key": "idea_generation"}, expect=422)
    api.post("/stage-assignments", {**body, "stage_key": "human_review"}, expect=422)

    viewer = as_role("member")
    res = viewer.client.delete(f"/api/v1/stage-assignments/{created['id']}", headers=viewer.h)
    assert res.status_code == 403
    res = api.client.delete(f"/api/v1/stage-assignments/{created['id']}", headers=api.h)
    assert res.status_code == 204
    res = api.client.delete(f"/api/v1/stage-assignments/{created['id']}", headers=api.h)
    assert res.status_code == 404
