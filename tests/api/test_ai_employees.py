from typing import Any

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
    keys = {e["key"] for e in api.items("/ai-employees")}
    assert {"idea_generator", "market_researcher", "competitor_researcher"} <= keys


def test_update_ai_employee_increments_version_and_is_audited(api: Api) -> None:
    created = api.post("/ai-employees", _payload())
    updated = api.patch(f"/ai-employees/{created['id']}", {"role": "競合・代替手段の調査"})
    assert updated["version"] == 2
    assert updated["role"] == "競合・代替手段の調査"
    again = api.patch(f"/ai-employees/{created['id']}", {"status": "inactive"})
    assert again["version"] == 3


def test_seeded_fake_employees_are_active_with_formats(api: Api) -> None:
    employees = {e["key"]: e for e in api.items("/ai-employees")}
    ig = employees["idea_generator"]
    assert ig["status"] == "active"
    assert ig["prompt_key"] == "idea_generator"
    # seed は出力契約 v2（Prompt v2）。output_format はその出力モデルのスキーマ（第2回仕様 17章）
    assert ig["prompt_version"] == "v2"
    assert ig["output_format"]["title"] == "IdeaGeneratorOutputV2"


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
    ig = next(e for e in api.items("/ai-employees") if e["key"] == "idea_generator")
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
    # primary の割り当てが無効な社員のままなら、別の社員に勝手に切り替えずに止める
    api.post(f"/explorations/{exp['id']}/stage-runs", {}, expect=409)
    # 担当の付け替え（admin）
    current = api.items("/stage-assignments?stage_key=idea_generation")
    assert [a["ai_employee_id"] for a in current] == [ig["id"]]
    res = api.client.delete(f"/api/v1/stage-assignments/{current[0]['id']}", headers=api.h)
    assert res.status_code == 204
    api.post(
        "/stage-assignments",
        {"stage_key": "idea_generation", "ai_employee_id": second["id"], "role": "primary"},
    )
    run = api.post(f"/explorations/{exp['id']}/stage-runs", {})
    assert run["status"] == "succeeded"
    assert run["executions"][0]["ai_employee_id"] == second["id"]


def _claims_schema(employee: dict[str, Any]) -> dict[str, Any]:
    claims: dict[str, Any] = employee["output_format"]["properties"]["claims"]
    return claims


def _ig(key: str) -> dict[str, object]:
    return _payload(
        key=key,
        stage_key="idea_generation",
        implementation_key="idea_generator",
        prompt_key="idea_generator",
        prompt_version="v1",
        output_format=None,
        input_format=None,
    )


def test_prompt_change_without_output_format_sets_the_new_contract_schema(api: Api) -> None:
    """Prompt の版を変え、output_format を指定しなければ、新しい契約のスキーマになる（17章）。"""
    employee = api.post("/ai-employees", _ig("ig_switch"))
    path = f"/ai-employees/{employee['id']}"
    assert employee["output_format"]["title"] == "IdeaGeneratorOutput"
    v2 = api.patch(path, {"prompt_version": "v2"})
    assert v2["output_format"]["title"] == "IdeaGeneratorOutputV2"
    assert _claims_schema(v2)["maxItems"] == 10
    # 戻すと v1 のスキーマに戻る
    v1 = api.patch(path, {"prompt_version": "v1"})
    assert v1["output_format"]["title"] == "IdeaGeneratorOutput"
    assert "maxItems" not in _claims_schema(v1)


def test_prompt_change_keeps_an_explicit_output_format(api: Api) -> None:
    employee = api.post("/ai-employees", _ig("ig_explicit"))
    path = f"/ai-employees/{employee['id']}"
    custom = {"type": "object", "title": "Custom"}
    updated = api.patch(path, {"prompt_version": "v2", "output_format": custom})
    assert updated["output_format"] == custom
    # Prompt を変えない更新では、output_format はそのまま
    assert api.patch(path, {"name": "renamed"})["output_format"] == custom
