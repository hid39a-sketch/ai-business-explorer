from fastapi import APIRouter
from sqlalchemy import text

from ai_business_explorer.api.v1.deps import SessionDep
from ai_business_explorer.api.v1.routers import (
    actors,
    ai_employees,
    analyses,
    evidence,
    explorations,
    ideas,
    stage_runs,
)

api_router = APIRouter(prefix="/api/v1")


@api_router.get("/health", tags=["health"])
def health(session: SessionDep) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


for module in (actors, ai_employees, explorations, ideas, stage_runs, evidence, analyses):
    api_router.include_router(module.router)
