"""テスト共通設定。本物の PostgreSQL（TEST_DATABASE_URL）を使う。"""

import os
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ai_business_explorer.config import Settings
from ai_business_explorer.infrastructure.db.base import Base
from ai_business_explorer.infrastructure.db.models import Actor
from ai_business_explorer.infrastructure.db.session import build_engine, build_session_factory
from ai_business_explorer.main import create_app
from ai_business_explorer.seed import DEFAULT_HUMAN_ACTOR_ID, SYSTEM_ACTOR_ID, seed

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://abe:abe@localhost:5432/ai_business_explorer_test",
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(app_env="test", database_url=TEST_DATABASE_URL, code_version="test-sha")


@pytest.fixture(scope="session")
def engine(settings: Settings) -> Iterator[Engine]:
    engine = build_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    factory = build_session_factory(engine)
    with factory() as s:
        seed(s)
    yield factory
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def client(settings: Settings, session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app = create_app(settings)
    app.state.session_factory = session_factory
    with TestClient(app) as c:
        yield c


@pytest.fixture
def human_id() -> UUID:
    return DEFAULT_HUMAN_ACTOR_ID


@pytest.fixture
def system_id() -> UUID:
    return SYSTEM_ACTOR_ID


@pytest.fixture
def human(session: Session) -> Actor:
    actor = session.get(Actor, DEFAULT_HUMAN_ACTOR_ID)
    assert actor is not None
    return actor


@pytest.fixture
def system_actor(session: Session) -> Actor:
    actor = session.get(Actor, SYSTEM_ACTOR_ID)
    assert actor is not None
    return actor


def headers(actor_id: UUID) -> dict[str, str]:
    return {"X-Actor-Id": str(actor_id)}


class Api:
    """API テスト用の小さなヘルパー。"""

    def __init__(self, client: TestClient, actor_id: UUID) -> None:
        self.client = client
        self.h = headers(actor_id)

    def post(self, path: str, body: dict[str, Any] | None = None, expect: int = 201) -> Any:
        res = self.client.post(f"/api/v1{path}", json=body or {}, headers=self.h)
        assert res.status_code == expect, res.text
        return res.json()

    def patch(self, path: str, body: dict[str, Any], expect: int = 200) -> Any:
        res = self.client.patch(f"/api/v1{path}", json=body, headers=self.h)
        assert res.status_code == expect, res.text
        return res.json()

    def get(self, path: str, expect: int = 200) -> Any:
        res = self.client.get(f"/api/v1{path}", headers=self.h)
        assert res.status_code == expect, res.text
        return res.json()

    def exploration(self, theme: str = "AIを活用した新規事業を探索する") -> Any:
        return self.post("/explorations", {"title": "探索案件A", "theme": theme})

    def adopted_idea(self, exploration_id: str, title: str = "AI議事録サービス") -> Any:
        idea = self.post(f"/explorations/{exploration_id}/ideas", {"title": title})
        return self.post(f"/ideas/{idea['id']}/adopt", {"reason": "検証する"}, expect=200)

    def evidence(self, exploration_id: str, idea_id: str | None = None, title: str = "統計") -> Any:
        return self.post(
            "/evidence",
            {
                "exploration_id": exploration_id,
                "idea_id": idea_id,
                "source_type": "human_input",
                "title": title,
                "url": "https://example.com/report",
                "quote": "市場は拡大している。",
            },
        )


@pytest.fixture
def api(client: TestClient, human_id: UUID) -> Api:
    return Api(client, human_id)


@pytest.fixture
def system_api(client: TestClient, system_id: UUID) -> Api:
    return Api(client, system_id)
