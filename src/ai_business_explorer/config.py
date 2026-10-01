"""アプリケーション設定。値は環境変数（または .env）から読み込む。秘密情報をコードに書かない。"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_business_explorer.domain.enums import BudgetMode, DataClassification, PayloadMode
from ai_business_explorer.tools.base import ToolSideEffect


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test"] = "development"
    database_url: str = "postgresql+psycopg://abe:abe@localhost:5432/ai_business_explorer"

    # llm_config.provider を指定しないときのプロバイダー。fake（既定）/ anthropic（Claude API）
    llm_provider: Literal["fake", "anthropic"] = "fake"
    # Claude API の API キー（第2回仕様 R-03）。環境変数・シークレットからだけ渡す。ログに残さない。
    # 未設定なら anthropic の AI社員は実行できない（llm_error）。APP_ENV=test では使わない
    llm_api_key: SecretStr | None = None

    # AI社員が使えるツールの副作用区分（第2回仕様 13章）。外部への読み取り（external_read）まで。
    # 外部に書き込む write は設定しても拒否する。外部 Tool は組織ごとの有効化も必要
    # （TOOL_CONFIG_PATH）
    tool_allowed_side_effects: list[ToolSideEffect] = [
        ToolSideEffect.READ_ONLY,
        ToolSideEffect.EXTERNAL_READ,
    ]
    # 組織ごとの Tool 設定ファイル（JSON）。なければ外部 Tool は使えない
    tool_config_path: str | None = None
    # Web 取得で送る User-Agent。未設定なら既定値（製品名とバージョン）
    web_fetch_user_agent: str | None = None

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

    @field_validator("tool_allowed_side_effects")
    @classmethod
    def _write_is_never_allowed(cls, value: list[ToolSideEffect]) -> list[ToolSideEffect]:
        if ToolSideEffect.WRITE in value:
            raise ValueError("tools that write externally are not allowed")
        return value

    @field_validator("llm_max_classification")
    @classmethod
    def _restricted_is_never_sent(
        cls, value: dict[str, DataClassification]
    ) -> dict[str, DataClassification]:
        for provider, limit in value.items():
            if limit is DataClassification.RESTRICTED:
                raise ValueError(f"restricted data must not be sent to any LLM ({provider})")
        return value

    # 費用管理（第2回仕様 10章・R-21 の暫定値）。費用はプロバイダーの請求通貨のまま記録する。
    # 予算の行（budgets）がない組織は、この月額上限と方式を使う。
    budget_currency: str = "USD"
    organization_monthly_budget: Decimal = Decimal("100")
    organization_budget_mode: BudgetMode = BudgetMode.HARD
    # 1回の実行（execution）ごとの上限。AI社員の llm_config で個別に指定できる
    execution_max_cost: Decimal = Decimal("1")
    execution_max_llm_calls: int = 20
    execution_max_tool_calls: int = 20

    # LLM ログの本文（第2回仕様 12章・R-16）。public・internal は full、confidential 以上は
    # 保存しない。none にすると、すべての本文を保存しない（既定より厳しくすることだけを許す）。
    llm_payload_mode: PayloadMode = PayloadMode.FULL
    # 保存期間（R-20）。期限を過ぎた本文は retention コマンドで消す
    llm_payload_retention_days: int = 90
    # Tool の生の出力は 90日、Tool で取得した全文（候補のスナップショット）は 180日（R-20）
    tool_output_retention_days: int = 90
    tool_snapshot_retention_days: int = 180

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
