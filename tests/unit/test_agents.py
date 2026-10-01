import ast
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

import ai_business_explorer.agents as agents_pkg
from ai_business_explorer.agents.base import AgentContext, Claim, ExplorationView
from ai_business_explorer.agents.employees.idea_generator import IdeaGenerator
from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.llm.fake import FakeLLMClient
from ai_business_explorer.prompts.loader import load_prompt
from ai_business_explorer.tools.base import ToolBox, ToolContext, ToolRegistry


def test_idea_generator_produces_candidates_with_fake_llm() -> None:
    exploration = ExplorationView(id=uuid4(), title="探索", theme="物流", description=None)
    ctx = AgentContext(
        exploration=exploration,
        idea=None,
        evidence=[],
        prior_analyses=[],
        research_question=None,
        llm=FakeLLMClient(),
        llm_model="fake-model-v1",
        tools=ToolBox(
            ToolRegistry(), [], [], ToolContext(execution_id=uuid4(), exploration_id=exploration.id)
        ),
        prompt=load_prompt("idea_generator", "v1"),
    )
    draft = IdeaGenerator().run(ctx)
    assert len(draft.idea_candidates) == 3
    assert all(c.kind != "evidence_based" for c in draft.claims)


def test_evidence_based_claim_requires_evidence_refs() -> None:
    with pytest.raises(ValidationError):
        Claim(id="c1", text="事実", kind="evidence_based", evidence_refs=[])


def test_registry_contains_phase_1_employees() -> None:
    assert build_default_registry().keys() == ["idea_generator", "market_researcher"]


def test_agents_package_cannot_reach_db_or_services() -> None:
    """AI社員から DB・サービス（Review/Decision/再実行/差し戻し）へ到達できないことを保証する。"""
    forbidden = (
        "ai_business_explorer.infrastructure",
        "ai_business_explorer.application",
        "sqlalchemy",
    )
    root = Path(agents_pkg.__file__).parent
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(forbidden), f"{path.name} imports {name}"
