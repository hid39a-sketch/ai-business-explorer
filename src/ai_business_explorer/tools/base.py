"""Tool 抽象化。第1回はインターフェースとレジストリのみで、実ツールは実装しない。

- ツールは副作用区分（side_effect）を宣言する。
- AI社員は許可リスト（allowed_tools）にあるツールだけを、許可された副作用区分の範囲で使える。
- 外部から取得した情報は ToolResult.evidence_candidates として返し、Evidence 登録経路に渡す
  （AI 生成テキストを Evidence にしないため、候補はツールが取得した外部データに限る）。
"""

import time
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, ClassVar, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from ai_business_explorer.domain.execution import CallGuard


class ToolSideEffect(StrEnum):
    READ_ONLY = "read_only"
    EXTERNAL_READ = "external_read"
    WRITE = "write"


class ToolError(Exception):
    pass


class ToolNotAllowedError(ToolError):
    pass


class EvidenceCandidate(BaseModel):
    source_type: str
    title: str
    url: str | None = None
    quote: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_candidates: list[EvidenceCandidate] = Field(default_factory=list)


class ToolContext(BaseModel):
    execution_id: UUID
    exploration_id: UUID
    idea_id: UUID | None = None


class Tool(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    description: ClassVar[str]
    side_effect: ClassVar[ToolSideEffect]
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

    @abstractmethod
    def execute(self, tool_input: BaseModel, context: ToolContext) -> ToolResult: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)


class ToolCallRecord(BaseModel):
    tool_name: str
    tool_version: str
    side_effect: ToolSideEffect


class ToolCallRecorder(Protocol):
    """Tool 呼び出しの記録と上限の確認（第2回仕様 10章・12章）。実装はアプリケーション層。"""

    def before_tool_call(self, tool: "Tool") -> None:
        """呼び出しの前。上限を超えていれば例外で止める。"""

    def after_tool_call(
        self,
        tool: "Tool",
        tool_input: dict[str, Any],
        result: "ToolResult | None",
        error: BaseException | None,
        latency_ms: int,
    ) -> None:
        """呼び出しの後（成功・失敗とも）。費用とメタデータを記録する。"""


class ToolBox:
    """1回の実行で AI社員に渡すツール窓口。許可リストと副作用ポリシーを強制する。"""

    def __init__(
        self,
        registry: ToolRegistry,
        allowed_tools: list[str],
        allowed_side_effects: list[ToolSideEffect],
        context: ToolContext,
        guard: CallGuard | None = None,
        recorder: ToolCallRecorder | None = None,
    ) -> None:
        self._guard = guard
        self._recorder = recorder
        self._registry = registry
        self._allowed = frozenset(allowed_tools)
        self._allowed_side_effects = frozenset(allowed_side_effects)
        self._context = context
        self.calls: list[ToolCallRecord] = []

    def call(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        if self._guard is not None:
            self._guard.check()  # 取り消し・タイムアウトなら以降の呼び出しを止める
        if name not in self._allowed:
            raise ToolNotAllowedError(f"tool '{name}' is not in allowed_tools")
        tool = self._registry.get(name)
        if tool is None:
            raise ToolNotAllowedError(f"tool '{name}' is not registered")
        if tool.side_effect not in self._allowed_side_effects:
            raise ToolNotAllowedError(
                f"tool '{name}' side_effect '{tool.side_effect}' is not allowed"
            )
        parsed = tool.input_model.model_validate(tool_input)
        if self._recorder is not None:
            self._recorder.before_tool_call(tool)
        self.calls.append(
            ToolCallRecord(
                tool_name=tool.name, tool_version=tool.version, side_effect=tool.side_effect
            )
        )
        started = time.monotonic()
        try:
            result = tool.execute(parsed, self._context)
            tool.output_model.model_validate(result.output)
        except Exception as exc:
            if self._recorder is not None:
                elapsed = int((time.monotonic() - started) * 1000)
                self._recorder.after_tool_call(tool, tool_input, None, exc, elapsed)
            raise
        if self._recorder is not None:
            elapsed = int((time.monotonic() - started) * 1000)
            self._recorder.after_tool_call(tool, tool_input, result, None, elapsed)
        return result


# 第1回は本番用ツールを登録しない。
default_tool_registry = ToolRegistry()
