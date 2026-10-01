"""Tool 抽象化のテスト。本番用ツールはないため、テスト専用の Fake ツールを使う。"""

from typing import ClassVar
from uuid import uuid4

import pytest
from pydantic import BaseModel

from ai_business_explorer.tools.base import (
    Tool,
    ToolBox,
    ToolContext,
    ToolNotAllowedError,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
    default_tool_registry,
)


class EchoIn(BaseModel):
    text: str


class EchoOut(BaseModel):
    text: str


class EchoTool(Tool):
    name: ClassVar[str] = "echo"
    version: ClassVar[str] = "1"
    description: ClassVar[str] = "test only"
    side_effect: ClassVar[ToolSideEffect] = ToolSideEffect.READ_ONLY
    input_model: ClassVar[type[BaseModel]] = EchoIn
    output_model: ClassVar[type[BaseModel]] = EchoOut

    def execute(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        assert isinstance(tool_input, EchoIn)
        return ToolResult(output={"text": tool_input.text})


class ExternalTool(EchoTool):
    name: ClassVar[str] = "external"
    side_effect: ClassVar[ToolSideEffect] = ToolSideEffect.EXTERNAL_READ


def _toolbox(allowed: list[str]) -> ToolBox:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(ExternalTool())
    ctx = ToolContext(execution_id=uuid4(), exploration_id=uuid4())
    return ToolBox(registry, allowed, [ToolSideEffect.READ_ONLY], ctx)


def test_allowed_read_only_tool_runs_and_is_recorded() -> None:
    box = _toolbox(["echo"])
    assert box.call("echo", {"text": "hi"}).output == {"text": "hi"}
    assert [c.tool_name for c in box.calls] == ["echo"]


def test_tool_not_in_allowlist_is_rejected() -> None:
    with pytest.raises(ToolNotAllowedError):
        _toolbox([]).call("echo", {"text": "hi"})


def test_side_effect_policy_blocks_external_tools() -> None:
    with pytest.raises(ToolNotAllowedError):
        _toolbox(["external"]).call("external", {"text": "hi"})


def test_unregistered_tool_is_rejected() -> None:
    with pytest.raises(ToolNotAllowedError):
        _toolbox(["missing"]).call("missing", {})


def test_no_production_tools_are_registered_in_phase_1() -> None:
    assert default_tool_registry.names() == []
