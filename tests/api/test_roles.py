"""ロールごとの操作の可否（第2回仕様 1章の表、A-07・R-11）と、組織による分離（R-01）。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ai_business_explorer.api.v1.deps import get_principal
from ai_business_explorer.api.v1.router import api_router, health
from ai_business_explorer.api.v1.routers.stage_runs import list_stages
from ai_business_explorer.domain.ids import uuid7
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    Organization,
    OrganizationMembership,
)
from ai_business_explorer.seed import DEFAULT_ORGANIZATION_ID
from tests.api.test_rerun_send_back import _TestCompetitorResearcher
from tests.conftest import Api

ROLES = ["viewer", "member", "reviewer", "admin"]


def _human(
    session: Session, role: str | None, organization_id: UUID = DEFAULT_ORGANIZATION_ID
) -> UUID:
    actor = Actor(id=uuid7(), actor_type="human", display_name=f"{role or 'no role'} user")
    session.add(actor)
    session.flush()
    if role is not None:
        session.add(
            OrganizationMembership(
                organization_id=organization_id,
                actor_id=actor.id,
                actor_type="human",
                role=role,
            )
        )
    session.commit()
    return actor.id


@pytest.fixture
def as_role(client: TestClient, session: Session) -> Callable[[str], Api]:
    return lambda role: Api(client, _human(session, role))


@pytest.fixture
def setup(api: Api) -> dict[str, Any]:
    """admin（seed の人間 actor）で、各操作の対象を用意する。"""
    exp = api.exploration()
    candidate = api.post(f"/explorations/{exp['id']}/ideas", {"title": "候補"})
    idea = api.adopted_idea(exp["id"])
    ev = api.evidence(exp["id"], idea["id"])
    run = api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "market_research"})
    analysis_id = run["executions"][0]["output"]["analysis_id"]
    return {
        "exp": exp,
        "candidate": candidate,
        "idea": idea,
        "evidence": ev,
        "run": run,
        "analysis_id": analysis_id,
    }


def _employee_payload(key: str = "extra_researcher") -> dict[str, Any]:
    return {"key": key, "name": "Extra", "role": "調査", "stage_key": "market_research"}


# (操作名, 必要なロール, 実行する関数, 成功時のステータス)
Operation = tuple[str, str, Callable[[Api, dict[str, Any]], Any], int]


def _call(method: str, path: Callable[[dict[str, Any]], str], body: Any = None) -> Any:
    def run(a: Api, s: dict[str, Any]) -> Any:
        kwargs: dict[str, Any] = {"headers": a.h}
        if method != "get":
            kwargs["json"] = body(s) if callable(body) else (body or {})
        return getattr(a.client, method)(f"/api/v1{path(s)}", **kwargs)

    return run


def _ops() -> list[Operation]:
    exp = lambda s: f"/explorations/{s['exp']['id']}"  # noqa: E731
    idea = lambda s: f"/ideas/{s['idea']['id']}"  # noqa: E731
    candidate = lambda s: f"/ideas/{s['candidate']['id']}"  # noqa: E731
    employee = lambda s: f"/ai-employees/{s['run']['executions'][0]['ai_employee_id']}"  # noqa: E731
    evidence_body = lambda s: {  # noqa: E731
        "exploration_id": s["exp"]["id"],
        "source_type": "human_input",
        "title": "t",
    }
    rerun_body = lambda s: {"stage_key": "market_research", "rerun_of_id": s["run"]["id"]}  # noqa: E731
    return [
        ("read exploration", "viewer", _call("get", exp), 200),
        ("list actors", "viewer", _call("get", lambda s: "/actors"), 200),
        (
            "create exploration",
            "member",
            _call("post", lambda s: "/explorations", {"title": "B", "theme": "t"}),
            201,
        ),
        ("update exploration", "member", _call("patch", exp, {"description": "d"}), 200),
        ("create idea", "member", _call("post", lambda s: f"{exp(s)}/ideas", {"title": "C"}), 201),
        ("update idea", "member", _call("patch", idea, {"summary": "x"}), 200),
        ("register evidence", "member", _call("post", lambda s: "/evidence", evidence_body), 201),
        (
            "retract evidence",
            "member",
            _call("post", lambda s: f"/evidence/{s['evidence']['id']}/retract", {"reason": "r"}),
            200,
        ),
        ("run stage", "member", _call("post", lambda s: f"{exp(s)}/stage-runs"), 201),
        (
            "rerun stage",
            "member",
            _call("post", lambda s: f"{idea(s)}/stage-runs", rerun_body),
            201,
        ),
        ("adopt idea", "reviewer", _call("post", lambda s: f"{candidate(s)}/adopt"), 200),
        ("reject idea", "reviewer", _call("post", lambda s: f"{candidate(s)}/reject"), 200),
        (
            "human review",
            "reviewer",
            _call(
                "post",
                lambda s: f"/analyses/{s['analysis_id']}/human-reviews",
                {"decision": "approve"},
            ),
            201,
        ),
        (
            "human decision",
            "reviewer",
            _call(
                "post",
                lambda s: f"{idea(s)}/human-decisions",
                {"decision": "go", "rationale": "r"},
            ),
            201,
        ),
        (
            "register ai employee",
            "admin",
            _call("post", lambda s: "/ai-employees", _employee_payload()),
            201,
        ),
        ("update ai employee", "admin", _call("patch", employee, {"description": "d"}), 200),
        (
            "purge evidence",
            "admin",
            _call("post", lambda s: f"/evidence/{s['evidence']['id']}/purge", {"reason": "r"}),
            200,
        ),
    ]


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("op", _ops(), ids=lambda op: op[0])
def test_role_matrix(
    role: str, op: Operation, as_role: Callable[[str], Api], setup: dict[str, Any]
) -> None:
    _, required, call, ok_status = op
    res = call(as_role(role), setup)
    if ROLES.index(role) >= ROLES.index(required):
        assert res.status_code == ok_status, res.text
    else:
        assert res.status_code == 403, res.text
        assert res.json()["error"] == "PermissionDeniedError"


def test_send_back_requires_reviewer(
    client: TestClient, api: Api, as_role: Callable[[str], Api], setup: dict[str, Any]
) -> None:
    """差し戻しは reviewer 以上（R-11）。member は実行・再実行はできるが差し戻しはできない。"""
    client.app.state.agent_registry.register(_TestCompetitorResearcher())  # type: ignore[attr-defined]
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
    idea = setup["idea"]
    api.post(f"/ideas/{idea['id']}/stage-runs", {"stage_key": "competitor_research"})
    body = {"to_stage_key": "market_research", "reason": "根拠不足"}
    as_role("member").post(f"/ideas/{idea['id']}/send-back", body, expect=403)
    as_role("viewer").post(f"/ideas/{idea['id']}/send-back", body, expect=403)
    as_role("reviewer").post(f"/ideas/{idea['id']}/send-back", body)


def test_system_actor_cannot_use_the_api(system_api: Api, setup: dict[str, Any]) -> None:
    """system actor はロールを持てないため、閲覧も含めてどの操作もできない。"""
    exp_id = setup["exp"]["id"]
    for res in (
        system_api.client.get(f"/api/v1/explorations/{exp_id}", headers=system_api.h),
        system_api.client.get("/api/v1/actors", headers=system_api.h),
        system_api.client.post(
            f"/api/v1/analyses/{setup['analysis_id']}/human-reviews",
            json={"decision": "approve"},
            headers=system_api.h,
        ),
    ):
        assert res.status_code == 403, res.text


def test_human_without_membership_is_rejected(
    client: TestClient, session: Session, setup: dict[str, Any]
) -> None:
    api = Api(client, _human(session, None))
    api.get(f"/explorations/{setup['exp']['id']}", expect=403)
    api.post("/explorations", {"title": "x", "theme": "t"}, expect=403)


def test_missing_or_unknown_actor_is_401(client: TestClient) -> None:
    assert client.get("/api/v1/explorations").status_code == 401
    res = client.get("/api/v1/explorations", headers={"X-Actor-Id": str(uuid7())})
    assert res.status_code == 401


def test_stage_catalog_and_health_are_public(client: TestClient) -> None:
    """ステージ定義と死活確認は組織のデータを含まないので、操作者なしで呼べる。"""
    assert client.get("/api/v1/stages").status_code == 200
    assert client.get("/api/v1/health").status_code == 200


def test_every_data_endpoint_resolves_the_principal() -> None:
    """組織の絞り込み漏れを防ぐため、/stages と /health 以外のすべての API は操作者を解決する。"""

    def routes(router: Any) -> list[APIRoute]:
        found: list[APIRoute] = []
        for route in router.routes:
            if isinstance(route, APIRoute):
                found.append(route)
            elif hasattr(route, "original_router"):
                found.extend(routes(route.original_router))
        return found

    def calls(dependant: Any) -> set[Any]:
        found = {dependant.call}
        for sub in dependant.dependencies:
            found |= calls(sub)
        return found

    all_routes = routes(api_router)
    assert len(all_routes) > 20
    public = {list_stages, health}
    unprotected = [
        r.path
        for r in all_routes
        if r.endpoint not in public and get_principal not in calls(r.dependant)
    ]
    assert unprotected == []


def test_responses_include_organization_id(api: Api, setup: dict[str, Any]) -> None:
    org = str(DEFAULT_ORGANIZATION_ID)
    assert setup["exp"]["organization_id"] == org
    assert setup["idea"]["organization_id"] == org
    assert setup["evidence"]["organization_id"] == org
    assert setup["run"]["organization_id"] == org
    assert setup["run"]["executions"][0]["organization_id"] == org
    assert api.get(f"/analyses/{setup['analysis_id']}")["organization_id"] == org


# ---------------------------------------------------------------- 組織による分離


@pytest.fixture
def other_org(session: Session) -> UUID:
    org = Organization(id=uuid7(), name="Other Organization")
    session.add(org)
    session.commit()
    return org.id


@pytest.fixture
def other_admin(client: TestClient, session: Session, other_org: UUID) -> Api:
    return Api(client, _human(session, "admin", other_org))


def test_other_organization_data_is_not_found(other_admin: Api, setup: dict[str, Any]) -> None:
    exp_id, idea_id = setup["exp"]["id"], setup["idea"]["id"]
    for path in (
        f"/explorations/{exp_id}",
        f"/explorations/{exp_id}/ideas",
        f"/explorations/{exp_id}/evidence",
        f"/explorations/{exp_id}/stage-runs",
        f"/ideas/{idea_id}",
        f"/ideas/{idea_id}/analyses",
        f"/evidence/{setup['evidence']['id']}",
        f"/stage-runs/{setup['run']['id']}",
        f"/executions/{setup['run']['executions'][0]['id']}",
        f"/analyses/{setup['analysis_id']}",
        f"/ai-employees/{setup['run']['executions'][0]['ai_employee_id']}",
    ):
        other_admin.get(path, expect=404)
    assert other_admin.items("/explorations") == []
    assert other_admin.items("/ai-employees") == []


def test_cannot_write_to_other_organization_data(other_admin: Api, setup: dict[str, Any]) -> None:
    exp_id, idea_id = setup["exp"]["id"], setup["idea"]["id"]
    other_admin.post(f"/explorations/{exp_id}/ideas", {"title": "x"}, expect=404)
    other_admin.post(
        "/evidence",
        {"exploration_id": exp_id, "source_type": "human_input", "title": "t"},
        expect=404,
    )
    other_admin.post(f"/ideas/{idea_id}/stage-runs", {"stage_key": "market_research"}, expect=404)
    other_admin.post(
        f"/analyses/{setup['analysis_id']}/human-reviews", {"decision": "approve"}, expect=404
    )
    other_admin.post(
        f"/ideas/{idea_id}/human-decisions", {"decision": "go", "rationale": "r"}, expect=404
    )


def test_new_data_belongs_to_the_actor_organization(other_admin: Api, other_org: UUID) -> None:
    exp = other_admin.exploration()
    assert exp["organization_id"] == str(other_org)
    idea = other_admin.post(f"/explorations/{exp['id']}/ideas", {"title": "A"})
    assert idea["organization_id"] == str(other_org)


def test_ai_employee_key_is_unique_per_organization(
    api: Api, other_admin: Api, other_org: UUID
) -> None:
    """B-03：同じ key を別の組織で使える。同じ組織では重複できない。"""
    first = api.post("/ai-employees", _employee_payload("shared_key"))
    api.post("/ai-employees", _employee_payload("shared_key"), expect=409)
    second = other_admin.post("/ai-employees", _employee_payload("shared_key"))
    assert first["organization_id"] != second["organization_id"]
    assert second["organization_id"] == str(other_org)


def test_actors_are_listed_within_the_organization(
    api: Api, other_admin: Api, system_id: UUID, human_id: UUID
) -> None:
    ids = {a["id"] for a in api.items("/actors")}
    assert str(human_id) in ids
    assert str(system_id) not in ids  # system actor はどの組織にも所属しない
    assert str(other_admin.h["X-Actor-Id"]) not in ids
    api.get(f"/actors/{other_admin.h['X-Actor-Id']}", expect=404)
    api.get(f"/actors/{system_id}", expect=404)
    api.get(f"/actors/{human_id}")
