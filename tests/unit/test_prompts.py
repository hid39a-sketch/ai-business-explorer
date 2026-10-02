import hashlib

import pytest

from ai_business_explorer.domain.errors import DomainValidationError
from ai_business_explorer.prompts.loader import PROMPTS_DIR, load_prompt


def test_load_prompt_records_version_and_hash() -> None:
    prompt = load_prompt("idea_generator", "v1")
    raw = (PROMPTS_DIR / "idea_generator" / "v1.md").read_bytes()
    assert prompt.version == "v1"
    assert prompt.sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize(
    ("key", "version"), [("../etc", "v1"), ("idea_generator", "../v1"), ("Idea", "v1")]
)
def test_invalid_prompt_reference_is_rejected(key: str, version: str) -> None:
    with pytest.raises(DomainValidationError):
        load_prompt(key, version)


def test_missing_prompt() -> None:
    with pytest.raises(DomainValidationError):
        load_prompt("idea_generator", "v99")


# market_researcher の v1 の内容（変更しない。実行記録のハッシュと照合できなくなるため）
MARKET_RESEARCHER_V1_SHA256 = "e4f61bf4df1145c6f2253f228ac4bf8eeb64446637e3d929f601a91e1e7c83a2"


def test_market_researcher_v1_is_unchanged() -> None:
    assert load_prompt("market_researcher", "v1").sha256 == MARKET_RESEARCHER_V1_SHA256


def test_market_researcher_v2_defines_relation_against_the_claim() -> None:
    """v2 は relation を「主張の内容と Evidence の関係」として定義する（Idea の前提ではない）。"""
    prompt = load_prompt("market_researcher", "v2")
    assert prompt.version == "v2"
    assert prompt.sha256 != MARKET_RESEARCHER_V1_SHA256
    for phrase in (
        "Idea の前提・仮説との関係ではありません",
        '"supports"：Evidence がその主張の内容を支持する',
        '"contradicts"：Evidence がその主張の内容を否定する',
        '"context"：Evidence は主張の真偽を直接支持・否定せず',
        "Evidence によって真偽が評価された主張",
        '"context" だけでは evidence_based になりません',
        "主張の本文と照らして判定",
    ):
        assert phrase in prompt.text, phrase


# market_researcher の v2 の内容（v3 の追加後も変更しない）
MARKET_RESEARCHER_V2_SHA256 = "77a776cfd2c694dbdd1c2538cb566f00a7cb7798ed72e14a8403afb81664c04b"


def test_market_researcher_v2_is_unchanged() -> None:
    assert load_prompt("market_researcher", "v2").sha256 == MARKET_RESEARCHER_V2_SHA256


def test_market_researcher_v3_keeps_v2_and_adds_review_handling() -> None:
    """v3 は v2 の制約（relation の意味）を含み、前段の分析のレビュー情報の扱いを足す（V-07）。"""
    v2 = load_prompt("market_researcher", "v2").text
    v3 = load_prompt("market_researcher", "v3").text
    for line in v2.splitlines():
        if line.startswith("入力は JSON"):
            continue
        assert line in v3, line
    for phrase in (
        "review_status",
        "latest_review",
        "decision・comment・corrections",
        "過去のレビューではありません",
        "レビューは人間の判断であり、Evidence ではありません",
        '"rejected" の分析の内容を、事実や前提として扱わないでください',
        "comment の指摘を考慮し",
        "corrections がある場合は、必要に応じてその訂正を反映してください",
    ):
        assert phrase in v3, phrase


def test_seed_uses_market_researcher_v3() -> None:
    from ai_business_explorer.seed import SEED_EMPLOYEES

    versions = {e["key"]: e["prompt_version"] for e in SEED_EMPLOYEES}
    assert versions == {"idea_generator": "v1", "market_researcher": "v3"}
