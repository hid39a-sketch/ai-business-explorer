"""ステージ実行のワーカー。起動: uv run python -m ai_business_explorer.worker

queued の stage_run を古い順に1つずつ取り出して実行する（第2回仕様 9章）。
- 複数のワーカーを並べてよい（FOR UPDATE SKIP LOCKED で取り出しが重ならない）。
- 実行中は別のセッションで heartbeat を更新する。heartbeat が途絶えた running の実行は、
  いずれかのワーカーが failed にする。自動の再実行・次のステージへの連鎖はしない。
"""

import logging
import os
import signal
import socket
import threading
from types import FrameType
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ai_business_explorer.agents.registry import AgentRegistry, build_default_registry
from ai_business_explorer.application.stage_runs import (
    StageRunService,
    claim_next_queued,
    fail_stale_runs,
    touch_heartbeat,
)
from ai_business_explorer.config import Settings, get_settings
from ai_business_explorer.infrastructure.db.models import StageRun
from ai_business_explorer.infrastructure.db.repositories import scope_to_organization
from ai_business_explorer.infrastructure.db.session import build_engine, build_session_factory
from ai_business_explorer.tools.base import ToolRegistry
from ai_business_explorer.tools.defaults import build_tool_registry

logger = logging.getLogger(__name__)


class _Heartbeat:
    """実行中の stage_run の heartbeat_at を一定間隔で更新する。"""

    def __init__(
        self, session_factory: sessionmaker[Session], stage_run_id: UUID, interval: float
    ) -> None:
        self._session_factory = session_factory
        self._stage_run_id = stage_run_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with self._session_factory() as session:
                    touch_heartbeat(session, self._stage_run_id)
            except Exception:
                logger.exception("heartbeat update failed for %s", self._stage_run_id)


def run_once(
    session_factory: sessionmaker[Session],
    settings: Settings,
    agents: AgentRegistry,
    tools: ToolRegistry,
    worker_id: str,
) -> bool:
    """途絶した実行を片付け、queued の実行を1つ処理する。処理したら True。"""
    with session_factory() as session:
        for stale_id in fail_stale_runs(session, settings.worker_heartbeat_timeout_seconds):
            logger.warning("stage run %s failed: worker heartbeat lost", stale_id)
        stage_run_id = claim_next_queued(session)
    if stage_run_id is None:
        return False
    with session_factory() as session:
        organization_id = session.execute(
            select(StageRun.organization_id).where(StageRun.id == stage_run_id)
        ).scalar_one()
        session.rollback()
        # 実行中の取得・一覧を、その stage_run の組織に限る
        scope_to_organization(session, organization_id)
        with _Heartbeat(session_factory, stage_run_id, settings.worker_heartbeat_interval_seconds):
            StageRunService(session, settings, agents, tools).execute(stage_run_id, worker_id)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    session_factory = build_session_factory(build_engine(settings.database_url))
    agents = build_default_registry()
    tools = build_tool_registry(settings.web_fetch_user_agent)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stopping = threading.Event()

    def _stop(signum: int, _: FrameType | None) -> None:
        logger.info("signal %s received; stopping after the current run", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("worker %s started", worker_id)
    while not stopping.is_set():
        try:
            processed = run_once(session_factory, settings, agents, tools, worker_id)
        except Exception:
            logger.exception("worker iteration failed")
            processed = False
        if not processed:
            stopping.wait(settings.worker_poll_interval_seconds)
    logger.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
