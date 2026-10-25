"""API keys, the admin session cookie and CSRF protection (SPEC 2.2)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import async_session
from app.errors import ForgeError
from app.models import WEB_CONSOLE_KEY_NAME, ApiKey

SESSION_COOKIE = "forge_session"
SESSION_MAX_AGE_S = 12 * 3600
CSRF_HEADER = "X-Forge-Csrf"
LOGIN_ATTEMPTS_PER_MINUTE = 5
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LAST_USED_GRANULARITY = timedelta(seconds=60)


@dataclass(frozen=True)
class Principal:
    """Who is making a request. The limits are optional so callers can build a bare Principal;
    None means "use the configured defaults"."""

    key_id: int
    name: str
    is_admin: bool
    rpm_limit: int | None = None
    daily_token_quota: int | None = None


# ---- API keys --------------------------------------------------------------------------------


def generate_api_key() -> str:
    return "fk_" + secrets.token_urlsafe(30)  # 30 random bytes -> 40 URL-safe characters


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def key_display_prefix(key: str) -> str:
    return key[:10]


def _unauthorized(message: str) -> ForgeError:
    return ForgeError(401, "invalid_api_key", message)


async def ensure_web_console_key(session: AsyncSession, settings: Settings) -> int:
    """Create the built-in key row the admin session is attributed to; return its id.

    ON CONFLICT makes this safe when several replicas start at once.
    """
    await session.execute(
        insert(ApiKey)
        .values(
            name=WEB_CONSOLE_KEY_NAME,
            prefix=None,
            key_hash=None,
            rpm_limit=settings.default_rpm,
            daily_token_quota=settings.default_daily_tokens,
            builtin=True,
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )
    await session.commit()
    key_id = await session.scalar(select(ApiKey.id).where(ApiKey.name == WEB_CONSOLE_KEY_NAME))
    assert key_id is not None
    return key_id


async def _principal_from_key(session: AsyncSession, token: str) -> Principal:
    key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(token)))
    if key is None:
        raise _unauthorized("Invalid API key")
    if key.revoked_at is not None:
        raise _unauthorized("This API key has been revoked")
    now = datetime.now(UTC)
    # Throttled so a busy key doesn't turn every request into a row update.
    if key.last_used_at is None or now - key.last_used_at > _LAST_USED_GRANULARITY:
        await session.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now))
        await session.commit()
    return Principal(
        key_id=key.id,
        name=key.name,
        is_admin=False,
        rpm_limit=key.rpm_limit,
        daily_token_quota=key.daily_token_quota,
    )


# ---- admin session ---------------------------------------------------------------------------


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret.get_secret_value(), salt="forge-session")


def password_matches(settings: Settings, candidate: str) -> bool:
    expected = settings.admin_password.get_secret_value().encode()
    return hmac.compare_digest(expected, candidate.encode())


def create_session_token(settings: Settings) -> str:
    # The random session id lets logout revoke this one token server-side (see revoke_session).
    return _serializer(settings).dumps({"sub": "admin", "sid": secrets.token_urlsafe(16)})


def _session_id(settings: Settings, token: str | None) -> str | None:
    """The session id of a correctly signed, unexpired admin token, else None."""
    if not token:
        return None
    try:
        data = _serializer(settings).loads(token, max_age=SESSION_MAX_AGE_S)
    except BadSignature:  # also covers SignatureExpired
        return None
    if not isinstance(data, dict) or data.get("sub") != "admin" or not isinstance(data.get("sid"), str):
        return None
    return data["sid"]


def _revoked_key(settings: Settings, sid: str) -> str:
    return f"forge:{settings.env}:session:revoked:{sid}"


async def session_is_valid(redis: Redis, settings: Settings, token: str | None) -> bool:
    sid = _session_id(settings, token)
    return sid is not None and not await redis.exists(_revoked_key(settings, sid))


async def revoke_session(redis: Redis, settings: Settings, token: str | None) -> None:
    """Make a session token unusable before it expires (logout), on every gateway replica."""
    sid = _session_id(settings, token)
    if sid is not None:
        # Kept as long as the token could still be valid; after that the signature check fails anyway.
        await redis.set(_revoked_key(settings, sid), "1", ex=SESSION_MAX_AGE_S)


def _login_key(settings: Settings, client_ip: str) -> str:
    return f"forge:{settings.env}:login:{client_ip}:{int(time.time() // 60)}"


def _login_retry_after() -> int:
    return 60 - int(time.time() % 60)


async def check_login_rate(redis: Redis, settings: Settings, client_ip: str) -> None:
    """Refuse a login attempt once this client had too many failures in the current minute.

    Only failures count (see record_login_failure): behind a NAT every client can share one
    address, so counting successful logins too would lock the admin out after a few page loads.
    """
    failures = await redis.get(_login_key(settings, client_ip))
    if int(failures or 0) >= LOGIN_ATTEMPTS_PER_MINUTE:
        raise ForgeError(429, "rate_limit_exceeded", "Too many failed login attempts", retry_after=_login_retry_after())


async def record_login_failure(redis: Redis, settings: Settings, client_ip: str) -> None:
    key = _login_key(settings, client_ip)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 120)
        await pipe.execute()


def require_csrf(request: Request) -> None:
    """Cookie-authenticated writes must carry X-Forge-Csrf: 1.

    Browsers only attach custom headers to same-origin requests (or after a CORS preflight we
    never approve), so a cross-site form or image tag can't produce one.
    """
    if request.method not in _SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
        raise ForgeError(403, "csrf_failed", f"Missing {CSRF_HEADER} header")


async def _admin_principal(request: Request, session: AsyncSession) -> Principal | None:
    settings: Settings = request.app.state.settings
    if not await session_is_valid(request.app.state.redis, settings, request.cookies.get(SESSION_COOKIE)):
        return None
    key = await session.scalar(select(ApiKey).where(ApiKey.name == WEB_CONSOLE_KEY_NAME))
    if key is None:  # only possible if someone deleted the row by hand
        key_id = await ensure_web_console_key(session, settings)
        key = await session.get(ApiKey, key_id)
        assert key is not None
    return Principal(
        key_id=key.id,
        name=key.name,
        is_admin=True,
        rpm_limit=key.rpm_limit,
        daily_token_quota=key.daily_token_quota,
    )


# ---- FastAPI dependencies --------------------------------------------------------------------


# Both dependencies use their own short-lived session rather than the request-scoped one: a
# streamed answer can run for minutes and must not pin a pooled connection the whole time.


async def require_principal(request: Request) -> Principal:
    """A Bearer API key, or else a valid admin session cookie (with CSRF header on writes)."""
    authorization = request.headers.get("authorization")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _unauthorized("Expected 'Authorization: Bearer <key>'")
        async with async_session() as session:
            return await _principal_from_key(session, token.strip())

    async with async_session() as session:
        principal = await _admin_principal(request, session)
    if principal is None:
        raise _unauthorized("Missing API key")
    require_csrf(request)
    return principal


async def require_admin(request: Request) -> Principal:
    """The admin session cookie only; API keys can't manage the platform."""
    async with async_session() as session:
        principal = await _admin_principal(request, session)
    if principal is None:
        raise ForgeError(401, "unauthorized", "Admin login required")
    require_csrf(request)
    return principal
