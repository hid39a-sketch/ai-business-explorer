from tests.conftest import Api


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "key": "competitor_researcher",
        "name": "CompetitorResearcher",
        "role": "競合調査",
        "description": "競合を調査する",
        "purpose": "競合状況の把握",
        "stage_key": "competitor_research",
        "llm_config": {"provider": "fake", "model": "fake-model-v1"},
        "allowed_tools": ["web_search"],
        "input_format": {"type": "object"},
        "output_format": {"type": "object"},
    }
    body.update(overrides)
    return body


def test_register_get_and_list_ai_employee(api: Api) -> None:
    created = api.post("/ai-employees", _payload())
    assert created["version"] == 1
    assert created["status"] == "draft"
    assert created["implementation_key"] is None
    fetched = api.get(f"/ai-employees/{created['id']}")
    assert fetched["name"] == "CompetitorResearcher"
    keys = {e["key"] for e in api.get("/ai-employees")}
    assert {"idea_generator", "market_researcher", "competitor_researcher"} <= keys


def test_update_ai_employee_increments_version_and_is_audited(api: Api) -> None:
    created = api.post("/ai-employees", _payload())
    updated = api.patch(f"/ai-employees/{created['id']}", {"role": "競合・代替手段の調査"})
    assert updated["version"] == 2
    assert updated["role"] == "競合・代替手段の調査"
    again = api.patch(f"/ai-employees/{created['id']}", {"status": "inactive"})
    assert again["version"] == 3


def test_seeded_fake_employees_are_active_with_formats(api: Api) -> None:
    employees = {e["key"]: e for e in api.get("/ai-employees")}
    ig = employees["idea_generator"]
    assert ig["status"] == "active"
    assert ig["prompt_key"] == "idea_generator"
    assert ig["output_format"]["title"] == "IdeaGeneratorOutput"


def test_validation_rules(api: Api) -> None:
    api.post("/ai-employees", _payload(stage_key="human_review"), expect=422)
    api.post("/ai-employees", _payload(stage_key="nope"), expect=422)
    api.post("/ai-employees", _payload(implementation_key="unknown"), expect=422)
    # 実装の担当ステージと一致しない
    api.post(
        "/ai-employees",
        _payload(
            implementation_key="idea_generator", prompt_key="idea_generator", prompt_version="v1"
        ),
        expect=422,
    )
    # 実装ありなら Prompt 必須
    api.post(
        "/ai-employees",
        _payload(key="ig2", stage_key="idea_generation", implementation_key="idea_generator"),
        expect=422,
    )
    api.post("/ai-employees", _payload(prompt_key="missing", prompt_version="v1"), expect=422)
    api.post("/ai-employees", _payload(key="Bad-Key"), expect=422)
    api.post("/ai-employees", _payload(key="idea_generator"), expect=409)
    created = api.post("/ai-employees", _payload())
    api.patch(f"/ai-employees/{created['id']}", {"name": None}, expect=422)


def test_writes_require_human_actor(api: Api, system_api: Api) -> None:
    system_api.post("/ai-employees", _payload(), expect=403)
    res = api.client.post("/api/v1/ai-employees", json=_payload())
    assert res.status_code == 401
    res = api.client.post(
        "/api/v1/ai-employees",
        json=_payload(),
        headers={"X-Actor-Id": "00000000-0000-7000-8000-00000000ffff"},
    )
    assert res.status_code == 401


def test_new_employee_can_be_added_and_executed_for_a_new_stage_definition(api: Api) -> None:
    """将来の追加手順の確認: 実装を持つ AI社員を API から追加して実行できる。"""
    exp = api.exploration()
    ig = next(e for e in api.get("/ai-employees") if e["key"] == "idea_generator")
    api.patch(f"/ai-employees/{ig['id']}", {"status": "inactive"})
    second = api.post(
        "/ai-employees",
        _payload(
            key="idea_generator_b",
            name="IdeaGeneratorB",
            stage_key="idea_generation",
            implementation_key="idea_generator",
            prompt_key="idea_generator",
            prompt_version="v1",
            status="active",
            allowed_tools=[],
        ),
    )
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    assert run["status"] == "succeeded"
    assert run["executions"][0]["ai_employee_id"] == second["id"]
