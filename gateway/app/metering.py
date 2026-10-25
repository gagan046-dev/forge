"""Token counting, rate limits, daily quotas, usage events and Prometheus metrics (SPEC 2.6)."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Histogram
from redis.asyncio import Redis

from app.db import async_session
from app.errors import ForgeError
from app.models import UsageEvent

if TYPE_CHECKING:
    from app.auth import Principal
    from app.config import Settings

log = logging.getLogger(__name__)

# ---- Prometheus metrics ----------------------------------------------------------------------

REQUESTS = Counter("forge_requests_total", "Requests handled", ["endpoint", "model", "status"])
TOKENS = Counter("forge_tokens_total", "Tokens processed", ["model", "kind"])
REQUEST_DURATION = Histogram(
    "forge_request_duration_seconds",
    "End-to-end request duration, including queueing and wake-up",
    ["endpoint", "model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 240),
)
TTFT = Histogram(
    "forge_ttft_seconds",
    "Time from request arrival to the first streamed token",
    ["model"],
    buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 2, 5, 10, 30, 60, 120),
)
QUEUE_WAIT = Histogram(
    "forge_queue_wait_seconds",
    "Time spent waiting for a GPU slot",
    ["priority"],
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
QUEUE_DEPTH = Gauge("forge_queue_depth", "Tickets waiting for a GPU slot", ["priority"])
INFLIGHT = Gauge("forge_inflight", "GPU slots in use across all gateways")
BACKEND_UP = Gauge("forge_backend_up", "1 when Ollama answers /api/version")
DEMAND = Gauge("forge_demand", "Demand value reported to KEDA")


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token for code and English).

    Used only when Ollama doesn't report usage, e.g. a stream the client aborted early.
    """
    return math.ceil(len(text) / 4) if text else 0


# ---- per-call measurements -------------------------------------------------------------------


@dataclass
class CallRecord:
    """Measurements for one request, filled in as it progresses and written once at the end.

    Times come from perf_counter: on Windows, monotonic() ticks in ~16 ms steps, too coarse for TTFT.
    """

    endpoint: str  # chat | completions | embeddings | rag
    model: str  # alias
    started: float = field(default_factory=time.perf_counter)
    queue_ms: int = 0
    upstream_started: float | None = None
    first_token_at: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    status: int = 200
    error_type: str | None = None

    def mark_upstream_start(self) -> None:
        self.upstream_started = time.perf_counter()

    def mark_first_token(self) -> None:
        if self.first_token_at is None:
            self.first_token_at = time.perf_counter()

    def fail(self, status: int, error_type: str) -> None:
        self.status = status
        self.error_type = error_type

    @property
    def ttft_ms(self) -> int | None:
        if self.first_token_at is None:
            return None
        return int((self.first_token_at - self.started) * 1000)

    def finish(self) -> tuple[int, float | None]:
        """Return (duration_ms, tokens_per_s)."""
        end = time.perf_counter()
        duration_ms = int((end - self.started) * 1000)
        # Generation speed excludes queueing and prompt processing when we know the first token
        # time; for non-streamed calls the best we have is the upstream call duration.
        gen_start = self.first_token_at or self.upstream_started
        tokens_per_s = None
        if self.completion_tokens > 0 and gen_start is not None and end - gen_start > 0:
            tokens_per_s = round(self.completion_tokens / (end - gen_start), 2)
        return duration_ms, tokens_per_s


# ---- limits ----------------------------------------------------------------------------------


class RateLimited(ForgeError):
    def __init__(self, limit: int, retry_after: int) -> None:
        super().__init__(
            429,
            "rate_limit_exceeded",
            f"Rate limit of {limit} requests per minute exceeded",
            retry_after=retry_after,
        )


class QuotaExceeded(ForgeError):
    def __init__(self, quota: int, retry_after: int) -> None:
        super().__init__(429, "quota_exceeded", f"Daily token quota of {quota} exhausted", retry_after=retry_after)


def _utc_day(now: datetime) -> str:
    return now.strftime("%Y%m%d")


def _seconds_to_midnight(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


class Metering:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.prefix = f"forge:{settings.env}:"

    def _limits(self, principal: Principal) -> tuple[int, int]:
        rpm = principal.rpm_limit if principal.rpm_limit is not None else self.settings.default_rpm
        quota = (
            principal.daily_token_quota
            if principal.daily_token_quota is not None
            else self.settings.default_daily_tokens
        )
        return rpm, quota

    def _token_key(self, key_id: int, now: datetime | None = None) -> str:
        return f"{self.prefix}tok:{key_id}:{_utc_day(now or datetime.now(UTC))}"

    async def check_limits(self, principal: Principal) -> None:
        """Count this request against the key's per-minute window and check its daily quota.

        The admin session (web console, RAG indexing) is metered but not limited: it is the
        operator's own console, and a large repo index needs more than a day's API-key quota.
        """
        if principal.is_admin:
            return
        rpm, quota = self._limits(principal)
        now_s = time.time()
        minute = int(now_s // 60)
        rpm_key = f"{self.prefix}rpm:{principal.key_id}:{minute}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(rpm_key)
            pipe.expire(rpm_key, 120)
            pipe.get(self._token_key(principal.key_id))
            count, _, used = await pipe.execute()
        if int(count) > rpm:
            raise RateLimited(rpm, retry_after=60 - int(now_s % 60))
        if int(used or 0) >= quota:
            raise QuotaExceeded(quota, retry_after=_seconds_to_midnight(datetime.now(UTC)))

    async def tokens_today(self, key_ids: list[int]) -> dict[int, int]:
        if not key_ids:
            return {}
        values = await self.redis.mget([self._token_key(k) for k in key_ids])
        return {k: int(v or 0) for k, v in zip(key_ids, values, strict=True)}

    async def record(self, principal: Principal, call: CallRecord) -> None:
        """Write metrics, the daily token counter and one usage_events row. Never raises."""
        duration_ms, tokens_per_s = call.finish()
        status = str(call.status)
        REQUESTS.labels(endpoint=call.endpoint, model=call.model, status=status).inc()
        REQUEST_DURATION.labels(endpoint=call.endpoint, model=call.model).observe(duration_ms / 1000)
        if call.prompt_tokens:
            TOKENS.labels(model=call.model, kind="prompt").inc(call.prompt_tokens)
        if call.completion_tokens:
            TOKENS.labels(model=call.model, kind="completion").inc(call.completion_tokens)
        if call.ttft_ms is not None:
            TTFT.labels(model=call.model).observe(call.ttft_ms / 1000)

        total = call.prompt_tokens + call.completion_tokens
        if total:
            try:
                key = self._token_key(principal.key_id)
                async with self.redis.pipeline(transaction=True) as pipe:
                    pipe.incrby(key, total)
                    pipe.expire(key, 2 * 86400)
                    await pipe.execute()
            except Exception:
                log.exception("could not update the token counter for key %s", principal.key_id)

        try:
            async with async_session() as session:
                session.add(
                    UsageEvent(
                        key_id=principal.key_id,
                        endpoint=call.endpoint,
                        model=call.model,
                        status=call.status,
                        prompt_tokens=call.prompt_tokens,
                        completion_tokens=call.completion_tokens,
                        queue_ms=call.queue_ms,
                        ttft_ms=call.ttft_ms,
                        duration_ms=duration_ms,
                        tokens_per_s=tokens_per_s,
                        error_type=call.error_type,
                    )
                )
                await session.commit()
        except Exception:
            log.exception("could not write usage event")
