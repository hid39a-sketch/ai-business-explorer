"""初期データ投入（冪等）。

既定組織、人間 actor 1人（admin）、system actor 1人（ロールなし）、
Fake AI社員2体（それぞれのステージの primary）、LLM の単価（Fake 0 USD、Claude Opus 5.5）。

実行: uv run python -m ai_business_explorer.seed
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import AgentRegistry, build_default_registry
from ai_business_explorer.config import get_settings
from ai_business_explorer.domain.enums import (
    ActorType,
    AIEmployeeStatus,
    OrganizationRole,
    PricingKind,
    StageAssignmentRole,
)
from ai_business_explorer.domain.stages import IDEA_GENERATION
from ai_business_explorer.infrastructure.db.models import (
    Actor,
    AIEmployee,
    Organization,
    OrganizationMembership,
    Pricing,
    StageAssignment,
)
from ai_business_explorer.infrastructure.db.repositories import (
    AIEmployeeRepository,
    OrganizationMembershipRepository,
    StageAssignmentRepository,
)
from ai_business_explorer.infrastructure.db.session import build_engine, build_session_factory
from ai_business_explorer.llm.claude import CLAUDE_DEFAULT_MODEL, CLAUDE_PROVIDER
from ai_business_explorer.llm.fake import FAKE_MODEL, FAKE_PROVIDER

# Swagger から操作しやすいよう固定 ID を使う。
# 既定組織の ID は migration 0002 と同じ値（第2回仕様 E-05）。
DEFAULT_ORGANIZATION_ID = UUID("00000000-0000-7000-8000-000000000100")
DEFAULT_ORGANIZATION_NAME = "Default Organization"
DEFAULT_HUMAN_ACTOR_ID = UUID("00000000-0000-7000-8000-000000000001")
SYSTEM_ACTOR_ID = UUID("00000000-0000-7000-8000-000000000002")

# 単価の適用開始日時。Fake LLM は費用が発生しないが、単価の行があることで予算の確認と費用の記録が
# 実際の LLM と同じ経路を通る
FAKE_PRICING_EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=UTC)

SEED_EMPLOYEES = [
    {
        "key": "idea_generator",
        "name": "IdeaGenerator",
        "role": "アイデア発掘",
        "description": "探索案件のテーマから事業アイデア候補を生成する（第1回は Fake LLM）。",
        "purpose": "人間が採否を判断するための Idea 候補を用意する。",
        "stage_key": IDEA_GENERATION,
        "implementation_key": "idea_generator",
        "prompt_key": "idea_generator",
        # v2：出力契約 v2（第2回仕様 17章）。v1 は変更しない。
        # seed は AI社員がないときだけ作るので、既存の DB の AI社員は書き換えない
        "prompt_version": "v2",
    },
    {
        "key": "market_researcher",
        "name": "MarketResearcher",
        "role": "市場調査",
        "description": (
            "採用済み Idea と Evidence から市場調査の分析を作成する（第1回は Fake LLM）。"
        ),
        "purpose": "Evidence に基づく主張と推論を区別した市場分析を人間に提示する。",
        "stage_key": "market_research",
        "implementation_key": "market_researcher",
        "prompt_key": "market_researcher",
        # v4：v3（relation の意味・レビュー情報の扱い）に出力契約 v2（17章）を加えた版。
        # v1〜v3 は変更しない（出力契約 v1 のまま）。
        # seed は AI社員がないときだけ作るので、既存の DB の AI社員は書き換えない
        "prompt_version": "v4",
    },
]


def seed(session: Session) -> None:
    if session.get(Organization, DEFAULT_ORGANIZATION_ID) is None:
        session.add(Organization(id=DEFAULT_ORGANIZATION_ID, name=DEFAULT_ORGANIZATION_NAME))
        session.flush()
    for actor_id, actor_type, name in (
        (DEFAULT_HUMAN_ACTOR_ID, ActorType.HUMAN, "Default Human Reviewer"),
        (SYSTEM_ACTOR_ID, ActorType.SYSTEM, "System"),
    ):
        if session.get(Actor, actor_id) is None:
            session.add(Actor(id=actor_id, actor_type=actor_type.value, display_name=name))
    session.flush()
    # ロールは人間だけに付ける。system actor には付けない。
    if OrganizationMembershipRepository(session).for_actor(DEFAULT_HUMAN_ACTOR_ID) is None:
        session.add(
            OrganizationMembership(
                organization_id=DEFAULT_ORGANIZATION_ID,
                actor_id=DEFAULT_HUMAN_ACTOR_ID,
                actor_type=ActorType.HUMAN.value,
                role=OrganizationRole.ADMIN.value,
            )
        )

    registry = build_default_registry()
    employees = AIEmployeeRepository(session)
    assignments = StageAssignmentRepository(session)
    for spec in SEED_EMPLOYEES:
        employee = employees.get_by_key(DEFAULT_ORGANIZATION_ID, spec["key"])
        if employee is None:
            employee = _create_employee(session, registry, spec)
        has_primary = assignments.list_where(
            StageAssignment.organization_id == DEFAULT_ORGANIZATION_ID,
            StageAssignment.stage_key == employee.stage_key,
            StageAssignment.role == StageAssignmentRole.PRIMARY.value,
        )
        if not has_primary:
            assignments.add(
                StageAssignment(
                    organization_id=DEFAULT_ORGANIZATION_ID,
                    stage_key=employee.stage_key,
                    ai_employee_id=employee.id,
                    role=StageAssignmentRole.PRIMARY.value,
                )
            )
    _seed_pricing(session)
    session.commit()


# LLM の単価（USD / 100万トークン）。単価が変わったら新しい行（適用開始日時）を足す
SEED_LLM_PRICING = [
    (FAKE_PROVIDER, FAKE_MODEL, Decimal(0), Decimal(0)),
    # Anthropic の公開価格（2026-09 時点）。thinking のトークンは出力として課金される。
    # 公式にある別名と日付付き ID を登録し、架空の ID は作らない（第2回仕様 10章 SC候補-12）
    (CLAUDE_PROVIDER, CLAUDE_DEFAULT_MODEL, Decimal(4), Decimal(20)),  # claude-opus-5-5
    (CLAUDE_PROVIDER, "claude-sonnet-5-5", Decimal(2), Decimal(10)),
    (CLAUDE_PROVIDER, "claude-haiku-4-5", Decimal(1), Decimal(5)),
    (CLAUDE_PROVIDER, "claude-haiku-4-5-20251001", Decimal(1), Decimal(5)),
]


def _seed_pricing(session: Session) -> None:
    for provider, model, input_price, output_price in SEED_LLM_PRICING:
        exists = session.scalars(
            select(Pricing).where(
                Pricing.kind == PricingKind.LLM.value,
                Pricing.provider == provider,
                Pricing.model == model,
            )
        ).first()
        if exists is None:
            session.add(
                Pricing(
                    kind=PricingKind.LLM.value,
                    provider=provider,
                    model=model,
                    input_per_million_tokens=input_price,
                    output_per_million_tokens=output_price,
                    per_call=Decimal(0),
                    currency="USD",
                    effective_from=FAKE_PRICING_EFFECTIVE_FROM,
                )
            )


def _create_employee(session: Session, registry: AgentRegistry, spec: dict[str, str]) -> AIEmployee:
    agent = registry.get(spec["implementation_key"])
    if agent is None:
        raise RuntimeError(f"implementation not registered: {spec['implementation_key']}")
    employee = AIEmployee(
        **spec,
        organization_id=DEFAULT_ORGANIZATION_ID,
        llm_config={"provider": FAKE_PROVIDER, "model": FAKE_MODEL},
        allowed_tools=[],
        input_format=agent.input_model.model_json_schema(),
        output_format=agent.contract_for(spec["prompt_version"]).output_model.model_json_schema(),
        status=AIEmployeeStatus.ACTIVE.value,
        version=1,
    )
    session.add(employee)
    session.flush()
    return employee


def main() -> None:
    engine = build_engine(get_settings().database_url)
    with build_session_factory(engine)() as session:
        seed(session)
    print("seed completed")


if __name__ == "__main__":
    main()
