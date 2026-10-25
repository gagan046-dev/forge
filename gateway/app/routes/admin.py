"""Admin API for the web console (SPEC 2.7). Everything except login/me needs the admin session."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    check_login_rate,
    create_session_token,
    generate_api_key,
    hash_api_key,
    key_display_prefix,
    password_matches,
    require_admin,
    require_csrf,
    session_is_valid,
)
from app.backend import BackendManager
from app.config import Settings
from app.db import get_session
from app.errors import ForgeError
from app.metering import Metering
from app.models import ApiKey, Conversation
from app.scheduler import GpuScheduler, Priority

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["admin"])
admin = [Depends(require_admin)]


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _not_found(what: str) -> ForgeError:
    return ForgeError(404, "not_found", f"{what} not found")


# ---- session ---------------------------------------------------------------------------------


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


@router.post("/auth/login")
async def login(request: Request, body: LoginBody) -> JSONResponse:
    require_csrf(request)  # also blocks login CSRF (a site logging the browser into our console)
    settings = _settings(request)
    client_ip = request.client.host if request.client else "unknown"
    await check_login_rate(request.app.state.redis, settings, client_ip)
    if not password_matches(settings, body.password):
        log.info("failed admin login from %s", client_ip)
        raise ForgeError(401, "invalid_credentials", "Wrong password")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(settings),
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.post("/auth/logout")
async def logout(request: Request) -> JSONResponse:
    require_csrf(request)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")
    return response


@router.get("/auth/me")
async def me(request: Request) -> dict[str, bool]:
    return {"authenticated": session_is_valid(_settings(request), request.cookies.get(SESSION_COOKIE))}


# ---- API keys --------------------------------------------------------------------------------


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    rpm_limit: int | None = Field(default=None, ge=1, le=100_000)
    daily_token_quota: int | None = Field(default=None, ge=1)


class KeyUpdate(BaseModel):
    rpm_limit: int | None = Field(default=None, ge=1, le=100_000)
    daily_token_quota: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _something_to_change(self) -> KeyUpdate:
        if self.rpm_limit is None and self.daily_token_quota is None:
            raise ValueError("set rpm_limit and/or daily_token_quota")
        return self


def _key_out(key: ApiKey) -> dict[str, Any]:
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "rpm_limit": key.rpm_limit,
        "daily_token_quota": key.daily_token_quota,
        "created_at": key.created_at,
        "revoked_at": key.revoked_at,
        "last_used_at": key.last_used_at,
        "builtin": key.builtin,
    }


@router.get("/keys", dependencies=admin)
async def list_keys(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    keys = await session.scalars(select(ApiKey).order_by(ApiKey.id))
    return [_key_out(k) for k in keys]


@router.post("/keys", dependencies=admin, status_code=201)
async def create_key(body: KeyCreate, request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    settings = _settings(request)
    plain = generate_api_key()
    key = ApiKey(
        name=body.name.strip(),
        prefix=key_display_prefix(plain),
        key_hash=hash_api_key(plain),
        rpm_limit=body.rpm_limit or settings.default_rpm,
        daily_token_quota=body.daily_token_quota or settings.default_daily_tokens,
        builtin=False,
    )
    session.add(key)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ForgeError(409, "conflict", f"A key named '{body.name}' already exists") from None
    await session.refresh(key)
    # The only time the plain key leaves the server; only its hash is stored.
    return _key_out(key) | {"key": plain}


async def _get_key(session: AsyncSession, key_id: int) -> ApiKey:
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise _not_found("API key")
    return key


@router.patch("/keys/{key_id}", dependencies=admin)
async def update_key(key_id: int, body: KeyUpdate, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    key = await _get_key(session, key_id)
    if body.rpm_limit is not None:
        key.rpm_limit = body.rpm_limit
    if body.daily_token_quota is not None:
        key.daily_token_quota = body.daily_token_quota
    await session.commit()
    return _key_out(key)


@router.delete("/keys/{key_id}", dependencies=admin)
async def revoke_key(key_id: int, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    key = await _get_key(session, key_id)
    if key.builtin:
        raise ForgeError(400, "invalid_request_error", "The built-in web-console key can't be revoked")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        await session.commit()
    return _key_out(key)


# ---- usage -----------------------------------------------------------------------------------

_RANGES: dict[str, tuple[timedelta, str]] = {
    "24h": (timedelta(hours=24), "hour"),
    "7d": (timedelta(days=7), "day"),
    "30d": (timedelta(days=30), "day"),
}

_ERRORS = "count(*) FILTER (WHERE e.status >= 400)"
_SUMS = (
    "count(*) AS requests, coalesce(sum(e.prompt_tokens), 0) AS prompt_tokens, "
    f"coalesce(sum(e.completion_tokens), 0) AS completion_tokens, {_ERRORS} AS errors"
)


def _ms(value: float | None) -> int | None:
    return None if value is None else round(value)


@router.get("/usage", dependencies=admin)
async def usage(
    request: Request,
    range_: Literal["24h", "7d", "30d"] = Query(default="24h", alias="range"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    span, unit = _RANGES[range_]
    since = datetime.now(UTC) - span
    params = {"since": since}

    totals = (await session.execute(text(f"SELECT {_SUMS} FROM usage_events e WHERE e.ts >= :since"), params)).one()

    by_key = await session.execute(
        text(
            f"SELECT k.id AS key_id, k.name, {_SUMS} FROM usage_events e JOIN api_keys k ON k.id = e.key_id "
            "WHERE e.ts >= :since GROUP BY k.id, k.name ORDER BY requests DESC"
        ),
        params,
    )
    by_model = await session.execute(
        text(
            f"SELECT e.model, {_SUMS}, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY e.duration_ms) FILTER (WHERE e.status < 400) AS p50_ms, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY e.duration_ms) FILTER (WHERE e.status < 400) AS p95_ms, "
            "avg(e.tokens_per_s) AS tokens_per_s_avg "
            "FROM usage_events e WHERE e.ts >= :since GROUP BY e.model ORDER BY requests DESC"
        ),
        params,
    )
    # `unit` comes from the fixed _RANGES table above, never from the client, so inlining is safe.
    timeseries = await session.execute(
        text(
            f"""
            WITH buckets AS (
                SELECT generate_series(date_trunc('{unit}', CAST(:since AS timestamptz)),
                                       date_trunc('{unit}', now()), interval '1 {unit}') AS bucket
            ), agg AS (
                SELECT date_trunc('{unit}', e.ts) AS bucket, count(*) AS requests,
                       coalesce(sum(e.prompt_tokens + e.completion_tokens), 0) AS tokens
                FROM usage_events e WHERE e.ts >= :since GROUP BY 1
            )
            SELECT b.bucket, coalesce(a.requests, 0) AS requests, coalesce(a.tokens, 0) AS tokens
            FROM buckets b LEFT JOIN agg a ON a.bucket = b.bucket ORDER BY b.bucket
            """
        ),
        params,
    )
    latency = (
        await session.execute(
            text(
                "SELECT "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY e.duration_ms) AS p50, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY e.duration_ms) AS p95, "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY e.ttft_ms) AS ttft_p50, "
                "percentile_cont(0.95) WITHIN GROUP (ORDER BY e.ttft_ms) AS ttft_p95, "
                "avg(e.tokens_per_s) AS tps "
                "FROM usage_events e WHERE e.ts >= :since AND e.status < 400"
            ),
            params,
        )
    ).one()

    active_keys = (
        await session.execute(
            select(ApiKey.id, ApiKey.name, ApiKey.daily_token_quota)
            .where(ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.id)
        )
    ).all()
    metering: Metering = request.app.state.metering
    used = await metering.tokens_today([k.id for k in active_keys])

    def counts(row: Any) -> dict[str, int]:
        return {
            "requests": int(row.requests),
            "prompt_tokens": int(row.prompt_tokens),
            "completion_tokens": int(row.completion_tokens),
            "errors": int(row.errors),
        }

    return {
        "range": range_,
        "since": since,
        "totals": counts(totals),
        "by_key": [{"key_id": r.key_id, "name": r.name} | counts(r) for r in by_key],
        "by_model": [
            {"model": r.model}
            | counts(r)
            | {
                "p50_ms": _ms(r.p50_ms),
                "p95_ms": _ms(r.p95_ms),
                "tokens_per_s_avg": None if r.tokens_per_s_avg is None else round(r.tokens_per_s_avg, 2),
            }
            for r in by_model
        ],
        "timeseries": [{"t": r.bucket, "requests": int(r.requests), "tokens": int(r.tokens)} for r in timeseries],
        "latency": {
            "p50_ms": _ms(latency.p50),
            "p95_ms": _ms(latency.p95),
            "ttft_p50_ms": _ms(latency.ttft_p50),
            "ttft_p95_ms": _ms(latency.ttft_p95),
            "tokens_per_s_avg": None if latency.tps is None else round(latency.tps, 2),
        },
        "today_by_key": [
            {"key_id": k.id, "name": k.name, "tokens_used": used.get(k.id, 0), "daily_token_quota": k.daily_token_quota}
            for k in active_keys
        ],
    }


# ---- models and system status ----------------------------------------------------------------


def _ollama_tag(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


async def _ollama_json(request: Request, path: str) -> dict[str, Any] | None:
    settings = _settings(request)
    try:
        response = await request.app.state.http.get(f"{settings.ollama_base}{path}", timeout=3.0)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


@router.get("/models", dependencies=admin)
async def models(request: Request) -> dict[str, Any]:
    backend: BackendManager = request.app.state.backend
    state = await backend.state()
    # Only ask Ollama when it is up: this page must never be the reason the GPU wakes.
    tags = await _ollama_json(request, "/api/tags") if state == "ready" else None
    ps = await _ollama_json(request, "/api/ps") if state == "ready" else None
    pulled = {_ollama_tag(m.get("name") or m.get("model", "")) for m in (tags or {}).get("models", [])}
    loaded = {_ollama_tag(m.get("name") or m.get("model", "")): m for m in (ps or {}).get("models", [])}

    out = []
    for spec in _settings(request).models:
        tag = _ollama_tag(spec.name)
        running = loaded.get(tag)
        out.append(
            {
                "alias": spec.alias,
                "name": spec.name,
                "role": spec.role,
                "context_length": spec.context_length,
                "dimensions": spec.dimensions,
                "pulled": tag in pulled if tags is not None else None,
                "loaded": running is not None,
                "size_vram": running.get("size_vram") if running else None,
            }
        )
    return {"backend": state, "models": out}


@router.get("/status", dependencies=admin)
async def status(request: Request) -> dict[str, Any]:
    backend: BackendManager = request.app.state.backend
    scheduler: GpuScheduler = request.app.state.scheduler
    settings = _settings(request)
    demand = await backend.demand()
    stats = await scheduler.stats()
    gpu = None
    try:
        response = await request.app.state.http.get(f"{settings.gpu_exporter_url.rstrip('/')}/gpu", timeout=2.0)
        if response.status_code == 200:
            gpu = response.json()
    except (httpx.HTTPError, ValueError):
        gpu = None
    return {
        "backend": await backend.state(demand),
        "queue": {p.name.lower(): stats.by_priority[p] for p in Priority} | {"total": stats.queued},
        "inflight": stats.inflight,
        "max_inflight": settings.max_inflight,
        "demand": demand.demand,
        "waking": demand.waking,
        "idle_for_s": demand.idle_for_s,
        "idle_seconds": settings.idle_seconds,
        "gpu": gpu,
    }


# ---- conversations ---------------------------------------------------------------------------


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")  # the UI may keep per-answer stats alongside
    role: Literal["system", "user", "assistant"]
    content: str


class ConversationIn(BaseModel):
    title: str = Field(default="", max_length=300)
    model: str = Field(default="", max_length=200)
    messages: list[Message] = Field(default_factory=list, max_length=2000)


def _conversation_out(c: Conversation) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "model": c.model,
        "messages": c.messages,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


@router.get("/conversations", dependencies=admin)
async def list_conversations(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Conversation.id, Conversation.title, Conversation.updated_at)
        .order_by(Conversation.updated_at.desc())
        .limit(500)
    )
    return [{"id": r.id, "title": r.title, "updated_at": r.updated_at} for r in rows]


@router.get("/conversations/{conversation_id}", dependencies=admin)
async def get_conversation(conversation_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise _not_found("Conversation")
    return _conversation_out(conversation)


@router.put("/conversations/{conversation_id}", dependencies=admin)
async def put_conversation(
    conversation_id: uuid.UUID, body: ConversationIn, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    values = {
        "title": body.title,
        "model": body.model,
        "messages": [m.model_dump() for m in body.messages],
    }
    statement = (
        insert(Conversation)
        .values(id=conversation_id, **values)
        .on_conflict_do_update(index_elements=["id"], set_=values | {"updated_at": func.now()})
        .returning(Conversation)
    )
    conversation = (await session.scalars(statement)).one()
    await session.commit()
    return _conversation_out(conversation)


@router.delete("/conversations/{conversation_id}", dependencies=admin, status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> Response:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        raise _not_found("Conversation")
    await session.delete(conversation)
    await session.commit()
    return Response(status_code=204)
