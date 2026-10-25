"""Cluster-internal endpoints: KEDA demand, probes and Prometheus metrics. Not exposed by the ingress."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.backend import BackendManager
from app.db import async_session
from app.metering import DEMAND

log = logging.getLogger(__name__)

router = APIRouter(tags=["internal"])


@router.get("/internal/demand")
async def demand(request: Request) -> dict[str, Any]:
    """Polled by the KEDA metrics-api trigger; any gateway answers for the whole (shared) GPU."""
    backend: BackendManager = request.app.state.backend
    current = await backend.demand()
    DEMAND.set(current.demand)
    return current.as_dict()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    # Ollama is deliberately not checked: it sleeps at zero replicas and the gateway must still
    # serve (and wake it).
    checks: dict[str, str] = {}
    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        log.warning("readiness: database check failed: %s", exc)
        checks["database"] = "failed"
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        log.warning("readiness: redis check failed: %s", exc)
        checks["redis"] = "failed"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "unavailable", "checks": checks}, status_code=200 if ok else 503)


@router.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
