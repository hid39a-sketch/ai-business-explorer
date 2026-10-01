"""migration のテスト共通。

テストモジュールごとに専用のデータベースを作る（他のテストのスキーマに触れない）。
"""

import os
from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text

from tests.conftest import ROOT, TEST_DATABASE_URL


@pytest.fixture(scope="module")
def migration_url(request: pytest.FixtureRequest) -> Iterator[str]:
    base = make_url(TEST_DATABASE_URL)
    module = request.module.__name__.rsplit(".", 1)[-1]
    name = f"{base.database}_{module}"[:63]
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    yield base.set(database=name).render_as_string(hide_password=False)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture(scope="module")
def engine(migration_url: str) -> Iterator[Engine]:
    engine = create_engine(migration_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def alembic_cfg(migration_url: str) -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", migration_url)
    return cfg
