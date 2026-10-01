"""本番で使う Tool のレジストリ（API とワーカーで共通）。"""

from ai_business_explorer import __version__
from ai_business_explorer.tools.base import ToolRegistry
from ai_business_explorer.tools.web_fetch import WebFetcher, WebFetchLimits, WebFetchTool


def build_tool_registry(
    user_agent: str | None = None, fetcher: WebFetcher | None = None
) -> ToolRegistry:
    """組織ごとの有効化とドメインの許可リストは、実行ごとに設定ファイルから渡す（ToolContext）。"""
    registry = ToolRegistry()
    registry.register(
        WebFetchTool(fetcher or WebFetcher(user_agent or default_user_agent(), WebFetchLimits()))
    )
    return registry


def default_user_agent() -> str:
    return f"AIBusinessExplorer/{__version__} (+research; read-only)"
