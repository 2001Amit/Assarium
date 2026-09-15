from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.guard import assert_routes_are_guarded
from app.api.middleware import install as install_middleware
from app.api.routers import (
    ai,
    audit,
    connections,
    dashboards,
    datasets,
    lineage,
    pipeline,
    schedules,
    semantic,
    sources,
)
from app.core.config import get_settings
from app.core.errors import AssariumError
from app.db.session import init_db

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s"
)
logger = logging.getLogger("assarium")
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()

    # Local development would otherwise start with no tenant and answer 403 to
    # everything. Gated on local mode, which production configuration refuses.
    from app.db.session import session_scope
    from app.tenancy.bootstrap import ensure_local_tenant

    with session_scope() as db:
        ensure_local_tenant(db)
    logger.info("Assarium API ready — engine=%s, data_dir=%s", settings.engine, settings.data_dir)

    runner_task = None
    if settings.scheduler_enabled:
        from app.orchestration.runner import run_loop

        runner_task = asyncio.create_task(
            run_loop(poll_seconds=settings.scheduler_poll_seconds)
        )
        logger.info(
            "Orchestration runner launched (poll every %ds)",
            settings.scheduler_poll_seconds,
        )
    else:
        logger.info("Orchestration runner disabled by configuration")

    yield

    if runner_task is not None:
        runner_task.cancel()
        try:
            await runner_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Assarium API",
    version="0.1.0",
    description="Self-serve data platform: connect, refine, model, analyse.",
    lifespan=lifespan,
)

install_middleware(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AssariumError)
async def assarium_error_handler(_request: Request, exc: AssariumError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """
    Turn anything unexpected into a plain 500.

    Two reasons this is not left to Starlette's default. It returns the response from
    outside our middleware, so the security headers never reach it; and with debug on it
    will happily send a traceback, which names internal paths and sometimes the values
    that were being handled.
    """
    logger.exception("Unhandled error on %s", _request.url.path, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"error": {
            "code": "internal_error",
            "message": "Something went wrong on our side. The failure has been logged.",
            "details": {},
        }},
    )


@app.get("/api/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "engine": settings.engine, "environment": settings.environment}


app.include_router(sources.router)
app.include_router(connections.router)
app.include_router(datasets.router)
app.include_router(pipeline.router)
app.include_router(semantic.router)
app.include_router(dashboards.router)
app.include_router(ai.router)
app.include_router(schedules.router)
app.include_router(audit.router)
app.include_router(lineage.router)

# Runs at import, before a single request is served. A route that can be reached without
# resolving a tenant is a data leak, not a bug to find later, so the process refuses to
# start rather than serving it.
assert_routes_are_guarded(app)
