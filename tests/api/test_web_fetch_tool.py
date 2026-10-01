"""Web 取得 Tool をステージ実行から使う（第2回仕様 2章・13章・R-20）。外部には接続しない。

- 外部 Tool は組織で有効にしたときだけ使える。ドメインは組織の許可リストに従う（設定ファイル）。
- 取得結果は Evidence 候補（原情報）になり、AI生成の補助情報とは混ざらない。
- 1実行あたりの取得は 20件まで。秘密情報は Tool にも AI社員にもログにも渡らない。
"""

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ai_business_explorer.agents.base import Agent, AgentContext, AnalysisDraft, CandidateNote
from ai_business_explorer.config import Settings
from ai_business_explorer.seed import DEFAULT_ORGANIZATION_ID
from ai_business_explorer.tools.defaults import build_tool_registry
from tests.conftest import Api
from tests.fake_web import FakeWeb

SECRET = "sk-test-secret-never-sent"
AI_NOTE = "AIの要約：このページは市場が拡大していると述べている（AI生成）"


class _Empty(BaseModel):
    pass


class _WebCollector(Agent):
    """research_question に並べた URL を web_fetch で取得するテスト専用の AI社員。"""

    implementation_key = "test_web_collector"
    stage_key = "idea_generation"
    output_schema_version = "test.v1"
    input_model = _Empty
    output_model = _Empty
    seen_outputs: ClassVar[list[dict[str, Any]]] = []

    def run(self, ctx: AgentContext) -> AnalysisDraft:
        notes = []
        for url in (ctx.research_question or "").split():
            result = ctx.tools.call("web_fetch", {"url": url})
            _WebCollector.seen_outputs.append(result.model_dump(mode="json"))
            notes += [
                CandidateNote(candidate_id=c.candidate_id, note=AI_NOTE)
                for c in result.evidence_candidates
                if c.candidate_id
            ]
        return AnalysisDraft(summary="collected", candidate_notes=notes)


@pytest.fixture
def web(client: TestClient) -> FakeWeb:
    web = FakeWeb()
    client.app.state.tool_registry = build_tool_registry(fetcher=web.fetcher())  # type: ignore[attr-defined]
    return web


@pytest.fixture
def collector(client: TestClient, api: Api, web: FakeWeb) -> dict[str, Any]:
    _WebCollector.seen_outputs = []
    client.app.state.agent_registry.register(_WebCollector())  # type: ignore[attr-defined]
    return api.post(
        "/ai-employees",
        {
            "key": "web_collector",
            "name": "WebCollector",
            "role": "収集",
            "stage_key": "idea_generation",
            "implementation_key": "test_web_collector",
            "prompt_key": "idea_generator",
            "prompt_version": "v1",
            "allowed_tools": ["web_fetch"],
            "status": "active",
            "llm_config": {"provider": "fake", "max_tool_calls": 50},
        },
    )


def _configure(
    client: TestClient,
    settings: Settings,
    tmp_path: Path,
    *,
    enabled: list[str] | None = None,
    allowed: list[str] | None = None,
    blocked: list[str] | None = None,
    **extra: Any,
) -> None:
    path = tmp_path / "tools.json"
    path.write_text(
        json.dumps(
            {
                "organizations": {
                    str(DEFAULT_ORGANIZATION_ID): {
                        "enabled_tools": ["web_fetch"] if enabled is None else enabled,
                        "web_fetch": {
                            "allowed_domains": ["example.com"] if allowed is None else allowed,
                            "blocked_domains": blocked or [],
                        },
                    }
                }
            }
        )
    )
    client.app.state.settings = settings.model_copy(  # type: ignore[attr-defined]
        update={"tool_config_path": str(path), **extra}
    )


def _collect(api: Api, exp_id: str, employee: dict[str, Any], urls: list[str]) -> Any:
    return api.post(
        f"/explorations/{exp_id}/stage-runs",
        {
            "mode": "collect_only",
            "ai_employee_id": employee["id"],
            "research_question": " ".join(urls),
        },
    )


def test_fetched_page_becomes_a_pending_candidate(
    client: TestClient,
    api: Api,
    settings: Settings,
    tmp_path: Path,
    web: FakeWeb,
    collector: dict[str, Any],
) -> None:
    _configure(client, settings, tmp_path)
    web.html(
        "www.example.com",
        "/report",
        "<title>市場レポート</title><p>市場は拡大している。</p><script>steal()</script>",
    )
    exp = api.exploration()
    run = _collect(api, exp["id"], collector, ["https://www.example.com/report"])
    assert run["status"] == "succeeded"
    [candidate] = api.items(f"/explorations/{exp['id']}/evidence-candidates")
    assert candidate["status"] == "pending"
    assert (candidate["source_type"], candidate["title"]) == ("web", "市場レポート")
    assert candidate["snapshot"] == "市場は拡大している。"
    assert "steal" not in str(candidate)
    # AI生成の補助情報は候補の原情報に混ざらない（B-21）
    assert AI_NOTE not in str(candidate)
    [note] = api.items(f"/evidence-candidates/{candidate['id']}/ai-notes")
    assert note["note"] == AI_NOTE

    accepted = api.post(f"/evidence-candidates/{candidate['id']}/accept", {"summary": "人の要約"})
    provenance = accepted["evidence"]["provenance"]
    assert provenance["tool_name"] == "web_fetch"
    assert provenance["tool_input"] == {"url": "https://www.example.com/report"}
    assert AI_NOTE not in str(accepted["evidence"])


def test_external_tool_must_be_enabled_for_the_organization(
    client: TestClient,
    api: Api,
    settings: Settings,
    tmp_path: Path,
    web: FakeWeb,
    collector: dict[str, Any],
) -> None:
    _configure(client, settings, tmp_path, enabled=[])
    exp = api.exploration()
    run = _collect(api, exp["id"], collector, ["https://example.com/"])
    [ex] = run["executions"]
    assert (run["status"], ex["error_type"]) == ("failed", "tool_error")
    assert "not enabled for this organization" in ex["error_message"]
    assert web.sent == []
    # 設定ファイルがなければ外部 Tool は使えない（安全側の既定）
    client.app.state.settings = settings  # type: ignore[attr-defined]
    run = _collect(api, exp["id"], collector, ["https://example.com/"])
    assert run["executions"][0]["error_type"] == "tool_error"
    assert web.sent == []


def test_domains_outside_the_allowlist_and_private_addresses_are_not_fetched(
    client: TestClient,
    api: Api,
    settings: Settings,
    tmp_path: Path,
    web: FakeWeb,
    collector: dict[str, Any],
) -> None:
    _configure(client, settings, tmp_path, allowed=["*"], blocked=["blocked.example.com"])
    web.dns["metadata.example.org"] = ["169.254.169.254"]
    exp = api.exploration()
    for url, message in [
        ("https://blocked.example.com/", "not allowed"),
        ("https://metadata.example.org/latest", "not a public address"),
        ("http://example.com/", "only https"),
    ]:
        run = _collect(api, exp["id"], collector, [url])
        [ex] = run["executions"]
        assert ex["error_type"] == "tool_error"
        assert message in ex["error_message"]
        [call] = api.items(f"/executions/{ex['id']}/tool-calls")
        assert (call["tool_name"], call["status"]) == ("web_fetch", "failed")
    assert api.items(f"/explorations/{exp['id']}/evidence-candidates") == []
    assert all(s.host != "metadata.example.org" for s in web.sent)


def test_at_most_twenty_fetches_per_execution(
    client: TestClient,
    api: Api,
    settings: Settings,
    tmp_path: Path,
    web: FakeWeb,
    collector: dict[str, Any],
) -> None:
    _configure(client, settings, tmp_path)
    for i in range(21):
        web.html("example.com", f"/p{i}", f"<p>page {i}</p>")
    exp = api.exploration()
    urls = [f"https://example.com/p{i}" for i in range(21)]
    run = _collect(api, exp["id"], collector, urls)
    [ex] = run["executions"]
    assert ex["error_type"] == "tool_error"
    assert "call limit 20" in ex["error_message"]
    assert len(api.items(f"/executions/{ex['id']}/tool-calls")) == 20
    # 上限までに取得した候補（原情報）は残る
    assert len(api.items(f"/explorations/{exp['id']}/evidence-candidates")) == 20


def test_secrets_are_never_sent_or_logged(
    client: TestClient,
    api: Api,
    session: Session,
    settings: Settings,
    tmp_path: Path,
    web: FakeWeb,
    collector: dict[str, Any],
) -> None:
    _configure(client, settings, tmp_path, llm_api_key=SECRET)
    web.html("example.com", "/", "<p>ok</p>", **{"set-cookie": f"token={SECRET}"})
    exp = api.exploration()
    _collect(api, exp["id"], collector, ["https://example.com/"])
    assert web.sent
    assert all(SECRET not in json.dumps(s.headers) for s in web.sent)
    assert all(not {"Authorization", "Cookie"} & set(s.headers) for s in web.sent)
    # AI社員に渡した結果に応答ヘッダー（Cookie）は含まれない
    assert SECRET not in json.dumps(_WebCollector.seen_outputs)
    for table in ("tool_calls", "tool_call_outputs", "evidence_candidates", "llm_calls"):
        rows = session.execute(text(f"SELECT row_to_json(t)::text FROM {table} t")).scalars()  # noqa: S608  固定のテーブル名
        assert all(SECRET not in row for row in rows), table


def test_write_tools_are_never_allowed() -> None:
    with pytest.raises(ValueError, match="write externally"):
        Settings(tool_allowed_side_effects=["read_only", "write"])
