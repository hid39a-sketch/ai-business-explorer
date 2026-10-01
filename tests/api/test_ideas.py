from tests.conftest import Api

PROFILE = {
    "title": "AI議事録サービス",
    "summary": "会議を自動で要約する",
    "problem": "議事録作成に時間がかかる",
    "target_customer": "中小企業",
    "target_market": "国内SaaS",
    "revenue_model": "月額課金",
    "required_technology": "音声認識",
    "required_data": "会議音声",
    "competitor_info": "既存ツール多数",
    "ip_info": "未調査",
    "legal_regulatory_info": "個人情報保護法",
    "initial_cost": "約500万円（仮）",
    "running_cost": "月20万円（仮）",
    "time_to_revenue": "12か月程度",
    "scalability": "高い可能性",
    "imitability": "模倣は容易",
    "ai_advantage": "要約精度",
}


def test_create_get_update_idea_with_japanese_profile(api: Api) -> None:
    exp = api.exploration()
    idea = api.post(f"/explorations/{exp['id']}/ideas", PROFILE)
    assert idea["adoption_status"] == "candidate"
    assert idea["origin_type"] == "human"
    assert idea["research_status"] == {
        "current_stage_key": None,
        "latest_stage_key": None,
        "latest_stage_run_status": None,
    }
    fetched = api.get(f"/ideas/{idea['id']}")
    assert fetched["legal_regulatory_info"] == "個人情報保護法"
    updated = api.patch(f"/ideas/{idea['id']}", {"target_customer": "士業事務所"})
    assert updated["target_customer"] == "士業事務所"
    assert updated["title"] == PROFILE["title"]
    listed = api.items(f"/explorations/{exp['id']}/ideas")
    assert [i["id"] for i in listed] == [idea["id"]]


def test_one_exploration_has_many_ideas(api: Api) -> None:
    exp = api.exploration()
    for n in range(3):
        api.post(f"/explorations/{exp['id']}/ideas", {"title": f"案{n}"})
    assert len(api.items(f"/explorations/{exp['id']}/ideas")) == 3


def test_adopt_and_reject_are_one_way_from_candidate(api: Api) -> None:
    exp = api.exploration()
    a = api.post(f"/explorations/{exp['id']}/ideas", {"title": "A"})
    b = api.post(f"/explorations/{exp['id']}/ideas", {"title": "B"})
    assert api.post(f"/ideas/{a['id']}/adopt", {}, expect=200)["adoption_status"] == "adopted"
    assert api.post(f"/ideas/{b['id']}/reject", {}, expect=200)["adoption_status"] == "rejected"
    api.post(f"/ideas/{a['id']}/reject", {}, expect=409)
    api.post(f"/ideas/{b['id']}/adopt", {}, expect=409)


def test_only_humans_can_adopt_or_update_ideas(api: Api, system_api: Api) -> None:
    exp = api.exploration()
    idea = api.post(f"/explorations/{exp['id']}/ideas", {"title": "A"})
    system_api.post(f"/ideas/{idea['id']}/adopt", {}, expect=403)
    system_api.patch(f"/ideas/{idea['id']}", {"summary": "x"}, expect=403)
    system_api.post(f"/explorations/{exp['id']}/ideas", {"title": "B"}, expect=403)


def test_idea_validation(api: Api) -> None:
    exp = api.exploration()
    idea = api.post(f"/explorations/{exp['id']}/ideas", {"title": "A"})
    api.patch(f"/ideas/{idea['id']}", {"title": None}, expect=422)
    api.patch(f"/ideas/{idea['id']}", {"adoption_status": "adopted"}, expect=422)
    api.post(f"/explorations/{exp['id']}/ideas", {"title": ""}, expect=422)
    api.get("/ideas/00000000-0000-7000-8000-00000000ffff", expect=404)
