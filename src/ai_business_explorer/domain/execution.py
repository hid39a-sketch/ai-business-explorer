"""実行中の LLM・Tool 呼び出しの確認（第2回仕様 9章）。"""

from typing import Protocol


class CallGuard(Protocol):
    """LLM・Tool を呼ぶ前の確認。取り消し・タイムアウトなら例外で以降の呼び出しを止める。"""

    def check(self) -> None: ...


class ExecutionCancelledError(Exception):
    """人間がステージ実行を取り消した（以降の LLM・Tool 呼び出しを止める）。"""
