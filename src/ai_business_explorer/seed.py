"""初期データ投入（冪等）。人間 actor 1人、system actor 1人、Fake AI社員2体。

実行: uv run python -m ai_business_explorer.seed
"""

from uuid import UUID

from sqlalchemy.orm import Session

from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.config import get_settings
from ai_business_explorer.domain.enums import ActorType, AIEmployeeStatus
from ai_business_explorer.domain.stages import IDEA_GENERATION
from ai_business_explorer.infrastructure.db.models import Actor, AIEmployee
from ai_business_explorer.infrastructure.db.repositories import AIEmployeeRepository
from ai_business_explorer.infrastructure.db.session import build_engine, build_session_factory
from ai_business_explorer.llm.fake import FAKE_MODEL, FAKE_PROVIDER

# Swagger から操作しやすいよう固定 ID を使う。
DEFAULT_HUMAN_ACTOR_ID = UUID("00000000-0000-7000-8000-000000000001")
SYSTEM_ACTOR_ID = UUID("00000000-0000-7000-8000-000000000002")

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
    },
]


def seed(session: Session) -> None:
    for actor_id, actor_type, name in (
        (DEFAULT_HUMAN_ACTOR_ID, ActorType.HUMAN, "Default Human Reviewer"),
        (SYSTEM_ACTOR_ID, ActorType.SYSTEM, "System"),
    ):
        if session.get(Actor, actor_id) is None:
            session.add(Actor(id=actor_id, actor_type=actor_type.value, display_name=name))

    registry = build_default_registry()
    employees = AIEmployeeRepository(session)
    for spec in SEED_EMPLOYEES:
        if employees.get_by_key(spec["key"]) is not None:
            continue
        agent = registry.get(spec["implementation_key"])
        if agent is None:
            raise RuntimeError(f"implementation not registered: {spec['implementation_key']}")
        session.add(
            AIEmployee(
                **spec,
                prompt_version="v1",
                llm_config={"provider": FAKE_PROVIDER, "model": FAKE_MODEL},
                allowed_tools=[],
                input_format=agent.input_model.model_json_schema(),
                output_format=agent.output_model.model_json_schema(),
                status=AIEmployeeStatus.ACTIVE.value,
                version=1,
            )
        )
    session.commit()


def main() -> None:
    engine = build_engine(get_settings().database_url)
    with build_session_factory(engine)() as session:
        seed(session)
    print("seed completed")


if __name__ == "__main__":
    main()
