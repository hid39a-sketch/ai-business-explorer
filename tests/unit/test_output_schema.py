"""主張と Evidence の relation と、構造化出力に使う実行ごとのスキーマ（第2回仕様 C-09・D-15）。

relation は主張（claim）の内容と Evidence の関係で、必須。evidence_based は「Evidence によって真偽が
評価された主張」で、supports か contradicts が1つ以上あれば条件を満たす（context だけでは不可）。
スキーマは API に送る形（anthropic.transform_schema の後）で jsonschema により検証する。
"""

from typing import Any
from uuid import UUID, uuid4

import anthropic
import jsonschema
import pytest
from pydantic import ValidationError

from ai_business_explorer.agents.base import Claim, EvidenceRef, output_schema_for
from ai_business_explorer.agents.employees.idea_generator import IdeaGeneratorOutput
from ai_business_explorer.agents.employees.market_researcher import MarketResearcherOutput

EV1 = UUID("01a0f8b7-1a14-77c7-9ab2-325dc6db173c")
EV2 = UUID("01a0f8b7-1a22-7944-b790-96790053e979")


def _claim(kind: str, *refs: dict[str, Any]) -> dict[str, Any]:
    return {"id": "c1", "text": "主張", "kind": kind, "evidence_refs": list(refs)}


def _ref(evidence_id: UUID, relation: str) -> dict[str, str]:
    return {"evidence_id": str(evidence_id), "relation": relation}


# ---------------------------------------------------------------------- relation（Pydantic）


def test_relation_is_required() -> None:
    """relation を省いた出力は supports とみなさず、検証エラーにする。"""
    with pytest.raises(ValidationError, match="relation"):
        EvidenceRef.model_validate({"evidence_id": str(EV1)})
    with pytest.raises(ValidationError, match="relation"):
        Claim.model_validate(_claim("evidence_based", {"evidence_id": str(EV1)}))


@pytest.mark.parametrize("relation", ["supports", "contradicts", "context"])
def test_each_relation_value_is_accepted(relation: str) -> None:
    claim = Claim.model_validate(_claim("inference", _ref(EV1, relation)))
    assert claim.evidence_refs[0].relation.value == relation


def test_unknown_relation_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Claim.model_validate(_claim("inference", _ref(EV1, "neutral")))


@pytest.mark.parametrize("relation", ["supports", "contradicts"])
def test_evidence_based_is_satisfied_by_supports_or_contradicts(relation: str) -> None:
    """evidence_based は Evidence によって真偽が評価された主張（否定された主張も含む）。"""
    claim = Claim.model_validate(_claim("evidence_based", _ref(EV1, relation)))
    assert claim.kind.value == "evidence_based"


def test_evidence_based_with_only_context_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no supports/contradicts"):
        Claim.model_validate(_claim("evidence_based", _ref(EV1, "context")))


def test_context_may_accompany_supports() -> None:
    claim = Claim.model_validate(
        _claim("evidence_based", _ref(EV1, "supports"), _ref(EV1, "context"))
    )
    assert len(claim.evidence_refs) == 2


def test_supports_and_contradicts_on_the_same_pair_is_rejected() -> None:
    with pytest.raises(ValidationError, match="both supports and contradicts"):
        Claim.model_validate(
            _claim("evidence_based", _ref(EV1, "supports"), _ref(EV1, "contradicts"))
        )


def test_supports_and_contradicts_on_different_evidence_is_allowed() -> None:
    claim = Claim.model_validate(
        _claim("evidence_based", _ref(EV1, "supports"), _ref(EV2, "contradicts"))
    )
    assert len(claim.evidence_refs) == 2


# ---------------------------------------------------------------------- 実行ごとのスキーマ


def _api_schema(evidence_ids: list[UUID]) -> dict[str, Any]:
    """API に送る形のスキーマ（Claude のクライアントと同じ変換）。"""
    return anthropic.transform_schema(output_schema_for(MarketResearcherOutput, evidence_ids))


def _output(*claims: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "s", "market_overview": "m", "claims": list(claims)}


def _schema_errors(schema: dict[str, Any], value: dict[str, Any]) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    return [e.message for e in validator.iter_errors(value)]


def test_relation_is_required_in_the_schema_too() -> None:
    """Pydantic と構造化出力のスキーマは同じ出力モデルから作る（relation はどちらでも必須）。"""
    ref = _api_schema([EV1])["$defs"]["EvidenceRef"]
    assert ref["required"] == ["evidence_id", "relation"]
    assert ref["additionalProperties"] is False
    assert _schema_errors(
        _api_schema([EV1]), _output(_claim("evidence_based", {"evidence_id": str(EV1)}))
    )


def test_evidence_ids_are_limited_to_the_run_input() -> None:
    schema = _api_schema([EV1, EV2])
    assert schema["$defs"]["EvidenceRef"]["properties"]["evidence_id"]["enum"] == [
        str(EV1),
        str(EV2),
    ]
    for relation in ("supports", "contradicts", "context"):
        assert not _schema_errors(schema, _output(_claim("inference", _ref(EV1, relation))))
    assert not _schema_errors(schema, _output(_claim("evidence_based", _ref(EV2, "contradicts"))))
    # 入力にない ID はスキーマの段階で出力できない
    errors = _schema_errors(schema, _output(_claim("evidence_based", _ref(uuid4(), "supports"))))
    assert any("is not one of" in e for e in errors)


def test_without_evidence_only_inference_and_speculation_are_possible() -> None:
    """Evidence が0件のとき、evidence_refs も evidence_based もスキーマで出力できない。"""
    schema = _api_schema([])
    claim_schema = schema["$defs"]["Claim"]
    assert "evidence_refs" not in claim_schema["properties"]
    assert claim_schema["additionalProperties"] is False
    assert claim_schema["properties"]["kind"]["enum"] == ["inference", "speculation"]
    assert {"EvidenceRef", "EvidenceRelation", "ClaimKind"}.isdisjoint(schema["$defs"])

    for kind in ("inference", "speculation"):
        plain = {"id": "c1", "text": "主張", "kind": kind}
        assert not _schema_errors(schema, _output(plain))
        # スキーマに合う出力は、出力モデルでもそのまま受け付けられる（evidence_refs は空）
        assert MarketResearcherOutput.model_validate(_output(plain)).claims[0].evidence_refs == []
    based = {"id": "c1", "text": "主張", "kind": "evidence_based"}
    assert _schema_errors(schema, _output(based))
    with_refs = {"id": "c1", "text": "主張", "kind": "inference", "evidence_refs": []}
    assert _schema_errors(schema, _output(with_refs))


def test_idea_generator_schema_has_no_evidence() -> None:
    schema = anthropic.transform_schema(output_schema_for(IdeaGeneratorOutput, []))
    claim_schema = schema["$defs"]["Claim"]
    assert "evidence_refs" not in claim_schema["properties"]
    assert claim_schema["properties"]["kind"]["enum"] == ["inference", "speculation"]
    assert "IdeaCandidate" in schema["$defs"]


def test_schema_is_built_fresh_for_each_run() -> None:
    """実行ごとのスキーマは、出力モデルの JSON Schema を書き換えない。"""
    output_schema_for(MarketResearcherOutput, [])
    output_schema_for(MarketResearcherOutput, [EV1])
    original = MarketResearcherOutput.model_json_schema()
    assert "evidence_refs" in original["$defs"]["Claim"]["properties"]
    assert "enum" not in original["$defs"]["EvidenceRef"]["properties"]["evidence_id"]
