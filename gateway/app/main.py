"""FastAPI application factory. Run with: uvicorn --factory app.main:create_app"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.asyncutil import drain_cleanup_tasks
from app.auth import ensure_web_console_key
from app.backend import BackendManager
from app.config import Settings, get_settings
from app.db import async_session, dispose_db, init_db
from app.errors import install_error_handlers
from app.llm import LLM
from app.metering import DEMAND, INFLIGHT, QUEUE_DEPTH, Metering
from app.rag.router import router as rag_router
from app.routes import admin, internal, openai
from app.scheduler import GpuScheduler, Priority

log = logging.getLogger("app")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestIdMiddleware:
    """Give every request an id (reusing a sane incoming X-Request-Id) and echo it back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = next((v.decode() for k, v in scope["headers"] if k == b"x-request-id"), "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_id)


async def _ensure_builtin_key(settings: Settings, attempts: int = 30, delay_s: float = 2.0) -> int:
    """Create the web-console key, waiting for Postgres (and the migration Job) if needed."""
    for attempt in range(1, attempts + 1):
        try:
            async with async_session() as session:
                return await ensure_web_console_key(session, settings)
        except Exception as exc:
            if attempt == attempts:
                raise
            log.warning("database not ready (%s); retrying in %.0fs", exc.__class__.__name__, delay_s)
            await asyncio.sleep(delay_s)
    raise AssertionError("unreachable")


async def _refresh_gauges(scheduler: GpuScheduler, backend: BackendManager, every_s: float = 2.0) -> None:
    """Keep the queue/backend gauges current between scrapes (they live in Redis, not here)."""
    while True:
        try:
            stats = await scheduler.stats()
            for priority in Priority:
                QUEUE_DEPTH.labels(priority=priority.name.lower()).set(stats.by_priority[priority])
            INFLIGHT.set(stats.inflight)
            DEMAND.set((await backend.demand()).demand)
            await backend.is_ready()  # updates forge_backend_up
        except Exception:
            log.debug("gauge refresh failed", exc_info=True)
        await asyncio.sleep(every_s)


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. `redis` and `http_transport` let tests inject fakes."""
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db(settings.database_url)
        redis_client = redis or Redis.from_url(settings.redis_url, decode_responses=True, health_check_interval=30)
        http = httpx.AsyncClient(
            transport=http_transport,
            # Generous read timeout: a long answer or a cold model load can pause the stream.
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=30.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=20),
        )
        scheduler = GpuScheduler(
            redis_client,
            max_inflight=settings.max_inflight,
            max_queue=settings.max_queue,
            queue_timeout_s=settings.queue_timeout_s,
        )
        backend = BackendManager(settings, redis_client, http, scheduler)
        metering = Metering(redis_client, settings)

        app.state.settings = settings
        app.state.redis = redis_client
        app.state.http = http
        app.state.scheduler = scheduler
        app.state.backend = backend
        app.state.metering = metering
        app.state.llm = LLM(settings, http, scheduler, backend, metering)

        await scheduler.start()
        gauges = None
        try:
            app.state.web_console_key_id = await _ensure_builtin_key(settings)
            gauges = asyncio.create_task(_refresh_gauges(scheduler, backend), name="gauges")
            log.info("forge gateway ready (env=%s, models=%s)", settings.env, [m.alias for m in settings.models])
            yield
        finally:
            if gauges is not None:
                gauges.cancel()
                with suppress(asyncio.CancelledError):
                    await gauges
            # Stop a running RAG index job while the DB, Redis and LLM client are still open.
            rag_indexer = getattr(app.state, "rag_indexer", None)
            if rag_indexer is not None:
                await rag_indexer.aclose()
            await scheduler.stop()
            await drain_cleanup_tasks()
            await http.aclose()
            if redis is None:
                await redis_client.aclose()
            await dispose_db()

    app = FastAPI(
        title="Forge gateway",
        lifespan=lifespan,
        docs_url="/internal/docs",
        redoc_url=None,
        openapi_url="/internal/openapi.json",
    )
    app.state.settings = settings
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(openai.router)
    app.include_router(admin.router)
    app.include_router(rag_router)  # declares its own /api/repos prefix
    app.include_router(internal.router)
    return app
