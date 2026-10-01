"""アプリケーション設定。値は環境変数（または .env）から読み込む。秘密情報をコードに書かない。"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_business_explorer.tools.base import ToolSideEffect


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test"] = "development"
    database_url: str = "postgresql+psycopg://abe:abe@localhost:5432/ai_business_explorer"

    # 第1回は Fake 以外の LLM プロバイダーを受け付けない（起動時に検証される）。
    llm_provider: Literal["fake"] = "fake"
    # 将来の LLM 接続用。第1回では使用しない。
    llm_api_key: SecretStr | None = None

    # AI社員が使えるツールの副作用区分。第1回は読み取り専用のみ。
    tool_allowed_side_effects: list[ToolSideEffect] = [ToolSideEffect.READ_ONLY]

    # 実行記録に残すコードバージョン。未設定なら git から取得を試みる。
    code_version: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
