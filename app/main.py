# FastAPI app: lifespan, request_id + endpoint binding middleware, router
# wiring, /health. Lifespan runs Mongo index setup and Weaviate auto-indexer
# (idempotent: only missing materials get embedded + upserted).
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request

from app.config import get_settings
from app.exceptions import register_exception_handlers
from app.src.agent.routes import router as chat_router
from app.src.auth.routes import router as auth_router
from app.src.cache import Cache
from app.src.cache.client import close_redis, get_redis
from app.src.database import Data
from app.src.database.client import close_mongo, ensure_indexes
from app.src.database.routes import router as data_router
from app.src.eval.routes import router as eval_router
from app.src.logs import (
    bind_request,
    configure_logging,
    endpoint_var,
    log,
)
from app.src.metrics.routes import router as metrics_router
from app.src.vector.client import close_weaviate, ensure_collection


# /health stays inline — it's small and only checks Redis liveness.
health_router = APIRouter(tags=["health"])


@health_router.get("/health")
async def health() -> dict:
    try:
        pong = await get_redis().ping()
    except Exception as e:
        return {"status": "degraded", "redis_error": str(e)}
    return {"status": "ok", "redis": pong}


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    configure_logging(s.log_level)
    log.info("app_starting", env=s.env)

    await ensure_indexes()
    try:
        await ensure_collection()
    except Exception as e:
        log.warning("weaviate_unavailable_at_startup", err=str(e))

    # auto-index any new Mongo materials into Weaviate (idempotent)
    try:
        data = Data(Cache())
        job_id, queued = await data.reindex_all_missing()
        log.info("startup_index_queued", job_id=job_id, queued=queued)
    except Exception as e:
        log.warning("startup_index_failed", err=str(e))

    yield

    await close_mongo()
    await close_redis()
    await close_weaviate()


def create_app() -> FastAPI:
    app = FastAPI(title="student-agent", version="1.0.0", lifespan=lifespan)
    register_exception_handlers(app)

    @app.middleware("http")
    async def _request_context_mw(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid4().hex
        # bind request_id + endpoint into log ContextVars
        endpoint = f"{request.method} {request.url.path}"
        bind_request(rid)
        endpoint_var.set(endpoint)
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(auth_router)
    app.include_router(data_router)
    app.include_router(chat_router)
    app.include_router(eval_router)
    return app


app = create_app()
