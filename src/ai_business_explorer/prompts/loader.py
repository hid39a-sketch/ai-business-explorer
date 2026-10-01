"""Prompt はリポジトリ内のファイルで管理する: prompts/<prompt_key>/<prompt_version>.md

実行記録には prompt_key / prompt_version / SHA-256 を保存し、どの Prompt で生成された
Analysis なのかを後から追跡できるようにする。
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ai_business_explorer.domain.errors import DomainValidationError

PROMPTS_DIR = Path(__file__).resolve().parent

_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_VERSION_RE = re.compile(r"^v[0-9]{1,4}$")


@dataclass(frozen=True)
class PromptTemplate:
    key: str
    version: str
    text: str
    sha256: str


def prompt_path(key: str, version: str) -> Path:
    # パストラバーサル防止のため、キーとバージョンの書式を厳格に検証する。
    if not _KEY_RE.match(key) or not _VERSION_RE.match(version):
        raise DomainValidationError(f"invalid prompt reference: {key}/{version}")
    return PROMPTS_DIR / key / f"{version}.md"


def prompt_exists(key: str, version: str) -> bool:
    return prompt_path(key, version).is_file()


def load_prompt(key: str, version: str) -> PromptTemplate:
    path = prompt_path(key, version)
    if not path.is_file():
        raise DomainValidationError(f"prompt not found: {key}/{version}")
    raw = path.read_bytes()
    return PromptTemplate(
        key=key, version=version, text=raw.decode("utf-8"), sha256=hashlib.sha256(raw).hexdigest()
    )
