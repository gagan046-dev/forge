"""Ollama readiness, scale-from-zero wake-up and the demand signal KEDA scales on (SPEC 2.4)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx
from redis.asyncio import Redis

from app.asyncutil import run_shielded
from app.config import Settings
from app.errors import ForgeError
from app.metering import BACKEND_UP
from app.scheduler import GpuScheduler

log = logging.getLogger(__name__)

BackendState = Literal["ready", "waking", "sleeping"]

LAST_ACTIVITY_KEY = "forge:gpu:last_activity"
# Sorted set of gateways' wake-up waiters, scored by an expiry timestamp so a crashed replica's
# waiters stop counting on their own.
WAKING_KEY = "forge:gpu:waking"
_WAITER_TTL_MS = 10_000


class BackendUnavailable(ForgeError):
    def __init__(self, waited_s: float) -> None:
        super().__init__(
            503,
            "backend_unavailable",
            f"The model server did not become ready within {waited_s:.0f}s",
            retry_after=30,
        )


@dataclass(frozen=True)
class Demand:
    demand: int
    queued: int
    inflight: int
    waking: int
    idle_for_s: float | None

    def as_dict(self) -> dict:
        return {
            "demand": self.demand,
            "queued": self.queued,
            "inflight": self.inflight,
            "waking": self.waking,
            "idle_for_s": self.idle_for_s,
        }


class BackendManager:
    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        http: httpx.AsyncClient,
        scheduler: GpuScheduler,
        *,
        readiness_cache_s: float = 2.0,
        wake_poll_s: float = 1.0,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.http = http
        self.scheduler = scheduler
        self.readiness_cache_s = readiness_cache_s
        self.wake_poll_s = wake_poll_s
        self._ready: bool = False
        self._checked_at: float = float("-inf")
        self._check_lock = asyncio.Lock()

    async def is_ready(self, *, fresh: bool = False) -> bool:
        """Whether Ollama answers GET /api/version; cached briefly so busy traffic doesn't probe it."""
        if not fresh and time.monotonic() - self._checked_at < self.readiness_cache_s:
            return self._ready
        async with self._check_lock:
            if not fresh and time.monotonic() - self._checked_at < self.readiness_cache_s:
                return self._ready
            try:
                response = await self.http.get(f"{self.settings.ollama_base}/api/version", timeout=2.0)
                ready = response.status_code == 200
            except httpx.HTTPError:
                ready = False
            self._ready, self._checked_at = ready, time.monotonic()
            BACKEND_UP.set(1 if ready else 0)
            return ready

    def mark_down(self) -> None:
        """Forget the cached readiness after a connection failure, so the next call re-probes."""
        self._ready, self._checked_at = False, float("-inf")
        BACKEND_UP.set(0)

    async def touch(self) -> None:
        await self.redis.set(LAST_ACTIVITY_KEY, f"{time.time():.3f}")

    async def ensure_ready(self) -> None:
        """Return once Ollama is ready, waking it from zero replicas if needed.

        Recording activity first raises the demand KEDA polls, which scales Ollama up; this
        request then waits (counted as a waking waiter) until readiness or WAKE_TIMEOUT_S.
        """
        await self.touch()
        if await self.is_ready():
            return

        waiter = uuid.uuid4().hex
        started = time.monotonic()
        deadline = started + self.settings.wake_timeout_s
        log.info("model server not ready; waiting for it to wake up")
        try:
            while True:
                await self.redis.zadd(WAKING_KEY, {waiter: int(time.time() * 1000) + _WAITER_TTL_MS})
                if await self.is_ready(fresh=True):
                    log.info("model server ready after %.1fs", time.monotonic() - started)
                    return
                if time.monotonic() >= deadline:
                    raise BackendUnavailable(time.monotonic() - started)
                await self.touch()
                await asyncio.sleep(self.wake_poll_s)
        finally:
            await run_shielded(self.redis.zrem(WAKING_KEY, waiter), what="removing wake-up waiter")

    async def waking_count(self) -> int:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(WAKING_KEY, "-inf", int(time.time() * 1000))
            pipe.zcard(WAKING_KEY)
            _, count = await pipe.execute()
        return int(count)

    async def idle_for_s(self) -> float | None:
        raw = await self.redis.get(LAST_ACTIVITY_KEY)
        if raw is None:
            return None
        return max(0.0, round(time.time() - float(raw), 1))

    async def demand(self) -> Demand:
        """queued + inflight + waking waiters + 1 while there was activity within IDLE_SECONDS."""
        stats = await self.scheduler.stats()
        waking = await self.waking_count()
        idle_for = await self.idle_for_s()
        recently_active = 1 if idle_for is not None and idle_for < self.settings.idle_seconds else 0
        return Demand(
            demand=stats.queued + stats.inflight + waking + recently_active,
            queued=stats.queued,
            inflight=stats.inflight,
            waking=waking,
            idle_for_s=idle_for,
        )

    async def state(self, demand: Demand | None = None) -> BackendState:
        if await self.is_ready():
            return "ready"
        demand = demand or await self.demand()
        # Not ready but wanted: KEDA is (or is about to be) scaling Ollama up.
        return "waking" if demand.demand > 0 else "sleeping"
