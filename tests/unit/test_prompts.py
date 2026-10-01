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
