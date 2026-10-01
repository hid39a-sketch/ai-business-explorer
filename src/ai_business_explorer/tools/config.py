"""組織ごとの Tool 設定（第2回仕様 13章。第2回は設定ファイルで持つ）。

設定ファイル（JSON。パスは環境変数 TOOL_CONFIG_PATH）の例：

    {
      "organizations": {
        "00000000-0000-7000-8000-000000000100": {
          "enabled_tools": ["web_fetch"],
          "web_fetch": {
            "allowed_domains": ["example.com", "go.jp"],
            "blocked_domains": ["internal.example.com"]
          }
        }
      }
    }

- 外部と通信する Tool（external_read）は、enabled_tools にある組織だけが使える。
- Web 取得は allowed_domains にあるドメイン（とそのサブドメイン）だけ。"*" なら禁止リスト以外の
  すべて。
  利用規約・robots.txt を確認したドメインだけを入れる（運用手順）。
- 設定ファイルがない・組織の設定がない場合は、外部 Tool を使えない（安全側の既定）。
"""

import json
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WebFetchConfig(_Strict):
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)


class OrganizationToolConfig(_Strict):
    enabled_tools: list[str] = Field(default_factory=list)
    web_fetch: WebFetchConfig = Field(default_factory=WebFetchConfig)


class ToolConfig(_Strict):
    organizations: dict[UUID, OrganizationToolConfig] = Field(default_factory=dict)

    def for_organization(self, organization_id: UUID | None) -> OrganizationToolConfig:
        if organization_id is None:
            return OrganizationToolConfig()
        return self.organizations.get(organization_id, OrganizationToolConfig())

    def enabled_tools(self, organization_id: UUID | None) -> frozenset[str]:
        return frozenset(self.for_organization(organization_id).enabled_tools)

    def web_fetch_domains(
        self, organization_id: UUID | None
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        web = self.for_organization(organization_id).web_fetch
        return tuple(web.allowed_domains), tuple(web.blocked_domains)


def load_tool_config(path: str | None) -> ToolConfig:
    if not path:
        return ToolConfig()
    file = Path(path)
    if not file.exists():
        raise RuntimeError(f"tool config file not found: {path}")
    return ToolConfig.model_validate(json.loads(file.read_text(encoding="utf-8")))
