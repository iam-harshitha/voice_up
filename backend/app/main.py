"""Application factory. Run with: uvicorn app.main:create_app --factory

Startup never creates or migrates tables; apply migrations with `alembic upgrade head`.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import REQUEST_ID_HEADER, RequestContextMiddleware, configure_logging
from app.db.session import build_engine, build_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    engine = build_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    is_production = settings.app_env == "production"
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if is_production else "/docs",
        openapi_url=None if is_production else "/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)

    register_error_handlers(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH"],
            allow_headers=["Content-Type", "X-API-Key", "X-CSRF-Token", REQUEST_ID_HEADER],
            expose_headers=[REQUEST_ID_HEADER],
        )
    # Added last so it is the outermost user middleware and sees every response.
    app.add_middleware(RequestContextMiddleware)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app
