from fastapi import APIRouter
from sqlalchemy import text

from ai_business_explorer.api.v1.deps import DbSessionDep
from ai_business_explorer.api.v1.routers import (
    actors,
    ai_employees,
    analyses,
    candidates,
    costs,
    evidence,
    explorations,
    ideas,
    stage_assignments,
    stage_runs,
)

api_router = APIRouter(prefix="/api/v1")


@api_router.get("/health", tags=["health"])
def health(session: DbSessionDep) -> dict[str, str]:
    """死活確認。組織のデータを返さないので、操作者なしで呼べる。"""
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


for module in (
    actors,
    ai_employees,
    stage_assignments,
    explorations,
    ideas,
    stage_runs,
    evidence,
    analyses,
    costs,
    candidates,
):
    api_router.include_router(module.router)
