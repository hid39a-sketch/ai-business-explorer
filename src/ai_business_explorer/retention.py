"""保存期間を過ぎたログの本文を消す（第2回仕様 14章・R-20）。手動または cron から起動する。

起動: uv run python -m ai_business_explorer.retention

- LLM ログの本文（llm_call_payloads）：LLM_PAYLOAD_RETENTION_DAYS（既定 90日）を過ぎたもの。
  メタデータ（llm_calls）は永続で残し、payload_deleted_at に消した日時を記録する。
"""

from ai_business_explorer.application.costs import delete_expired_payloads
from ai_business_explorer.config import get_settings
from ai_business_explorer.infrastructure.db.session import build_engine, build_session_factory


def main() -> None:
    settings = get_settings()
    with build_session_factory(build_engine(settings.database_url))() as session:
        deleted = delete_expired_payloads(session, settings.llm_payload_retention_days)
    days = settings.llm_payload_retention_days
    print(f"deleted {deleted} LLM call payloads older than {days} days")


if __name__ == "__main__":
    main()
