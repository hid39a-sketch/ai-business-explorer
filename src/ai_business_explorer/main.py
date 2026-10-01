"""FastAPI アプリケーション。起動: uv run uvicorn ai_business_explorer.main:app --reload"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from ai_business_explorer import __version__
from ai_business_explorer.agents.registry import build_default_registry
from ai_business_explorer.api.v1.router import api_router
from ai_business_explorer.config import Settings, get_settings
from ai_business_explorer.domain.errors import (
    AuthenticationRequiredError,
    DomainError,
    DomainValidationError,
    InvalidStateError,
    NotFoundError,
    PermissionDeniedError,
)
from ai_business_explorer.infrastructure.db.session import build_engine, build_session_factory
from ai_business_explorer.tools.base import default_tool_registry

_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    AuthenticationRequiredError: status.HTTP_401_UNAUTHORIZED,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
    InvalidStateError: status.HTTP_409_CONFLICT,
    DomainValidationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    code = next(
        (c for t, c in _STATUS_BY_ERROR.items() if isinstance(exc, t)),
        status.HTTP_400_BAD_REQUEST,
    )
    return JSONResponse(status_code=code, content={"detail": str(exc), "error": type(exc).__name__})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="AI Business Explorer",
        version=__version__,
        description=(
            "AI社員事業探索システムの基盤 API。Evidence / AI Analysis / Human Review / "
            "Human Decision を分離する。書き込み操作には X-Actor-Id ヘッダ（human actor）が必要。"
        ),
    )
    engine = build_engine(settings.database_url)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.state.agent_registry = build_default_registry()
    app.state.tool_registry = default_tool_registry
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.include_router(api_router)
    return app


app = create_app()
