"""決定的な応答を返す Fake LLM。第1回で唯一の LLMClient 実装。

応答は prompt_key ごとの responder で決まる。responder は最後の user メッセージ（JSON）を
入力として受け取り、構造化応答を返す。テストでは responder や例外を差し替えられる。
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from ai_business_explorer.llm.base import LLMError, LLMRequest, LLMResponse, LLMUsage

Responder = Callable[[dict[str, Any]], dict[str, Any]]

FAKE_PROVIDER = "fake"
FAKE_MODEL = "fake-model-v1"


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _idea_generator(payload: dict[str, Any]) -> dict[str, Any]:
    theme = str(payload.get("exploration", {}).get("theme") or "テーマ未設定")
    tag = _digest(payload)[:6]
    ideas = [
        {
            "title": f"{theme}に関する事業アイデア案{i}（Fake-{tag}）",
            "summary": f"Fake LLM が生成した候補{i}。人間による採用判断が必要。",
            "problem": f"{theme}領域の課題仮説{i}（未検証）",
        }
        for i in range(1, 4)
    ]
    return {
        "summary": f"「{theme}」から事業アイデア候補を{len(ideas)}件生成した（Fake 出力）。",
        "ideas": ideas,
        "claims": [
            {
                "id": "c1",
                "text": "生成した候補はいずれも根拠未確認の仮説である。",
                "kind": "speculation",
                "evidence_refs": [],
            }
        ],
    }


def _market_researcher(payload: dict[str, Any]) -> dict[str, Any]:
    evidence = payload.get("evidence", [])
    claims: list[dict[str, Any]] = [
        {
            "id": f"e{i}",
            "text": f"根拠「{ev['title']}」は対象市場に関する情報を含む（Fake 出力）。",
            "kind": "evidence_based",
            "evidence_refs": [{"evidence_id": ev["id"], "relation": "supports"}],
        }
        for i, ev in enumerate(evidence, start=1)
    ]
    claims.append(
        {
            "id": "i1",
            "text": "市場規模は未検証であり、追加の根拠が必要である。",
            "kind": "inference",
            "evidence_refs": [],
        }
    )
    return {
        "summary": f"{len(evidence)}件の根拠をもとに市場調査の下書きを作成した（Fake 出力）。",
        "market_overview": "Fake LLM による市場概要。実データに基づかない。",
        "claims": claims,
    }


DEFAULT_RESPONDERS: Mapping[str, Responder] = {
    "idea_generator": _idea_generator,
    "market_researcher": _market_researcher,
}


class FakeLLMClient:
    def __init__(
        self,
        responders: Mapping[str, Responder] | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._responders = dict(DEFAULT_RESPONDERS if responders is None else responders)
        self._fail_with = fail_with

    @property
    def provider(self) -> str:
        return FAKE_PROVIDER

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._fail_with is not None:
            raise self._fail_with
        if not request.messages:
            raise LLMError("request has no messages")
        try:
            payload = json.loads(request.messages[-1].content)
        except json.JSONDecodeError as exc:
            raise LLMError("fake LLM expects JSON user message") from exc

        responder = self._responders.get(request.prompt_key)
        structured = responder(payload) if responder else None
        text = (
            json.dumps(structured, ensure_ascii=False)
            if structured
            else f"fake:{_digest(payload)[:12]}"
        )
        return LLMResponse(
            provider=FAKE_PROVIDER,
            model=request.model,
            text=text,
            structured=structured,
            usage=LLMUsage(
                input_tokens=len(request.system) + len(request.messages[-1].content),
                output_tokens=len(text),
            ),
        )
