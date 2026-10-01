"""アプリケーション設定。値は環境変数（または .env）から読み込む。秘密情報をコードに書かない。"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_business_explorer.domain.enums import DataClassification
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

    # ステージ実行の方式（第2回仕様 9章）。async：受付後にワーカーが実行する。
    # sync：テストと Fake LLM 用。同じ API（202）のまま、応答の前に実行を終える。
    execution_mode: Literal["async", "sync"] = "async"
    # タイムアウト（R-20 の暫定値）。超えたら failed（timeout）。
    stage_run_timeout_seconds: float = 600
    llm_call_timeout_seconds: float = 120
    # ワーカーの生存確認。heartbeat がこの時間より古い running の実行は failed にする。
    worker_heartbeat_interval_seconds: float = 10
    worker_heartbeat_timeout_seconds: float = 60
    worker_poll_interval_seconds: float = 2

    # LLM プロバイダーごとの送信可能な分類の上限（第2回仕様 11章・R-03）。JSON で指定する
    # （例：{"fake": "internal"}）。指定のないプロバイダーは internal。契約条件を確認するまでは
    # internal のままにする。restricted はどの LLM にも送らない（設定しても拒否する）。
    llm_max_classification: dict[str, DataClassification] = {}

    @field_validator("llm_max_classification")
    @classmethod
    def _restricted_is_never_sent(
        cls, value: dict[str, DataClassification]
    ) -> dict[str, DataClassification]:
        for provider, limit in value.items():
            if limit is DataClassification.RESTRICTED:
                raise ValueError(f"restricted data must not be sent to any LLM ({provider})")
        return value

    def llm_send_limit(self, provider: str) -> DataClassification:
        limit = DataClassification(
            self.llm_max_classification.get(provider, DataClassification.INTERNAL)
        )
        # restricted はどの LLM にも送らない（検証を経ずに作った設定でも上限を confidential にする）
        if limit is DataClassification.RESTRICTED:
            return DataClassification.CONFIDENTIAL
        return limit


@lru_cache
def get_settings() -> Settings:
    return Settings()
