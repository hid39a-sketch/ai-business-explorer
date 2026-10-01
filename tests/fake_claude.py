"""Claude API の SDK クライアントの Fake（実際の API には接続しない）。"""

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

FAKE_KEY = "sk-ant-test-fake-key-never-sent"


@dataclass
class FakeReply:
    text: str = '{"ok": true}'
    input_tokens: int = 100
    output_tokens: int = 50
    stop_reason: str = "end_turn"
    model: str = "claude-opus-5-5"
    request_id: str = "req_fake_123"
    error: Exception | None = None


@dataclass
class FakeClaudeSDK:
    """messages.create と with_options だけを持つ。受け取った引数と時間制限を記録する。"""

    replies: list[FakeReply] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    api_keys: list[str] = field(default_factory=list)

    @property
    def messages(self) -> Any:
        return SimpleNamespace(create=self._create)

    def with_options(self, **kwargs: Any) -> "FakeClaudeSDK":
        self.timeouts.append(kwargs["timeout"])
        return self

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        reply = self.replies.pop(0) if self.replies else FakeReply()
        if reply.error is not None:
            raise reply.error
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=reply.text),
            ],
            usage=SimpleNamespace(
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=None,
            ),
            model=reply.model,
            stop_reason=reply.stop_reason,
            stop_details=(
                SimpleNamespace(category="cyber") if reply.stop_reason == "refusal" else None
            ),
            _request_id=reply.request_id,
        )

    def factory(self, api_key: str) -> "FakeClaudeSDK":
        self.api_keys.append(api_key)
        return self


def idea_reply(**kwargs: Any) -> FakeReply:
    """IdeaGenerator の出力形式の応答。"""
    body = {
        "summary": "候補を作成しました",
        "ideas": [{"title": "AI議事録", "summary": "会議の記録", "problem": "記録の手間"}],
        "claims": [{"id": "s1", "text": "需要があるかもしれない", "kind": "speculation"}],
    }
    return FakeReply(text=json.dumps(body, ensure_ascii=False), **kwargs)
