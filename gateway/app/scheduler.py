"""Distributed priority queue for the single GPU (SPEC 2.4).

Every gateway replica in both environments shares one Redis, so the GPU's concurrency limit
(MAX_INFLIGHT) holds globally. The pieces, all under the `forge:gpu:` prefix:

- `queue`             sorted set of waiting tickets, score = priority * 10**13 + enqueue_ms, so lower
                      priorities run first and each priority is FIFO;
- `ticket:{id}`       short-TTL key the waiter refreshes; a ticket whose waiter died is pruned;
- `lease:{id}`        short-TTL key the slot holder refreshes; a crashed replica's slot frees itself;
- `leases`            set of lease ids, so the Lua scripts can count live leases without SCAN;
- `ac:{principal}`    the principal's newest queued autocomplete ticket (for supersession);
- `superseded:{id}`   marker telling an older autocomplete waiter why its ticket disappeared;
- `events`            pub/sub channel announcing that a slot was freed or the queue changed.

All state transitions run as Lua scripts, so admission and acquisition are atomic across replicas.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import IntEnum

from redis.asyncio import Redis

from app.asyncutil import run_shielded
from app.errors import ForgeError
from app.metering import QUEUE_WAIT

log = logging.getLogger(__name__)

PRIORITY_SPAN = 10**13  # larger than any epoch-ms value for the next few centuries


class Priority(IntEnum):
    AUTOCOMPLETE = 0
    CHAT = 1
    BATCH = 2


class QueueFull(ForgeError):
    def __init__(self) -> None:
        super().__init__(429, "queue_full", "The GPU queue is full, retry shortly", retry_after=2)


class QueueTimeout(ForgeError):
    def __init__(self, waited_s: float) -> None:
        super().__init__(504, "queue_timeout", f"Waited {waited_s:.0f}s for a GPU slot")


class Superseded(ForgeError):
    def __init__(self) -> None:
        # 499 is nginx's "client closed request": the client already moved on to a newer request.
        super().__init__(499, "superseded", "Superseded by a newer autocomplete request")


@dataclass(frozen=True)
class Slot:
    ticket: str
    priority: Priority
    kind: str
    queue_ms: int


@dataclass(frozen=True)
class QueueStats:
    inflight: int
    by_priority: dict[Priority, int]

    @property
    def queued(self) -> int:
        return sum(self.by_priority.values())


# Shared Lua helpers, prepended to each script. ARGV[1] is always the key prefix.
_LUA_HELPERS = """
local prefix = ARGV[1]
local function live_leases(leases_key)
  local n = 0
  for _, id in ipairs(redis.call('SMEMBERS', leases_key)) do
    if redis.call('EXISTS', prefix .. 'lease:' .. id) == 1 then
      n = n + 1
    else
      redis.call('SREM', leases_key, id)
    end
  end
  return n
end
local function prune_queue(queue_key)
  for _, id in ipairs(redis.call('ZRANGE', queue_key, 0, -1)) do
    if redis.call('EXISTS', prefix .. 'ticket:' .. id) == 0 then
      redis.call('ZREM', queue_key, id)
    end
  end
end
"""

# KEYS: queue, ac_key   ARGV: prefix, ticket, score, max_queue, ticket_ttl_ms, supersede(0|1), meta
# Returns {admitted, superseded_old}.
_ENQUEUE = (
    _LUA_HELPERS
    + """
local queue, ac_key = KEYS[1], KEYS[2]
local ticket, score = ARGV[2], tonumber(ARGV[3])
local max_queue, ttl, supersede = tonumber(ARGV[4]), tonumber(ARGV[5]), ARGV[6] == '1'
prune_queue(queue)
local superseded = 0
if supersede then
  local old = redis.call('GET', ac_key)
  if old and redis.call('ZREM', queue, old) == 1 then
    redis.call('DEL', prefix .. 'ticket:' .. old)
    redis.call('SET', prefix .. 'superseded:' .. old, '1', 'PX', 60000)
    superseded = 1
  end
end
if redis.call('ZCARD', queue) >= max_queue then
  return {0, superseded}
end
redis.call('ZADD', queue, score, ticket)
redis.call('SET', prefix .. 'ticket:' .. ticket, ARGV[7], 'PX', ttl)
if supersede then
  redis.call('SET', ac_key, ticket, 'PX', 600000)
end
return {1, superseded}
"""
)

# KEYS: queue, leases   ARGV: prefix, ticket, max_inflight, lease_ttl_ms
# Returns 1 acquired, 0 keep waiting, -1 ticket vanished (expired), -2 superseded.
_ACQUIRE = (
    _LUA_HELPERS
    + """
local queue, leases = KEYS[1], KEYS[2]
local ticket = ARGV[2]
if not redis.call('ZSCORE', queue, ticket) then
  if redis.call('EXISTS', prefix .. 'superseded:' .. ticket) == 1 then return -2 end
  return -1
end
local free = tonumber(ARGV[3]) - live_leases(leases)
if free <= 0 then return 0 end
local position = 0
for _, id in ipairs(redis.call('ZRANGE', queue, 0, -1)) do
  if redis.call('EXISTS', prefix .. 'ticket:' .. id) == 0 then
    redis.call('ZREM', queue, id)
  else
    if id == ticket then
      redis.call('ZREM', queue, ticket)
      redis.call('DEL', prefix .. 'ticket:' .. ticket)
      redis.call('SET', prefix .. 'lease:' .. ticket, '1', 'PX', tonumber(ARGV[4]))
      redis.call('SADD', leases, ticket)
      return 1
    end
    position = position + 1
    if position >= free then return 0 end
  end
end
return 0
"""
)

# KEYS: queue, leases   ARGV: prefix, then (min, max) score bounds per priority.
# Returns {inflight, depth_p0, depth_p1, ...}.
_STATS = (
    _LUA_HELPERS
    + """
local queue, leases = KEYS[1], KEYS[2]
prune_queue(queue)
local result = {live_leases(leases)}
for i = 2, #ARGV, 2 do
  table.insert(result, redis.call('ZCOUNT', queue, ARGV[i], ARGV[i + 1]))
end
return result
"""
)


class GpuScheduler:
    def __init__(
        self,
        redis: Redis,
        *,
        max_inflight: int,
        max_queue: int,
        queue_timeout_s: float,
        prefix: str = "forge:gpu:",
        lease_ttl_s: float = 15.0,
        heartbeat_s: float = 5.0,
        poll_s: float = 0.5,
    ) -> None:
        self.redis = redis
        self.max_inflight = max_inflight
        self.max_queue = max_queue
        self.queue_timeout_s = queue_timeout_s
        self.prefix = prefix
        self.lease_ttl_ms = int(lease_ttl_s * 1000)
        self.heartbeat_s = heartbeat_s
        self.poll_s = poll_s
        self.queue_key = prefix + "queue"
        self.leases_key = prefix + "leases"
        self.channel = prefix + "events"
        self._enqueue = redis.register_script(_ENQUEUE)
        self._acquire = redis.register_script(_ACQUIRE)
        self._stats = redis.register_script(_STATS)
        # Replaced (and the old one set) on every notification; waiters grab the current one
        # before checking the queue, so a notification between check and wait is never lost.
        self._changed = asyncio.Event()
        self._listener: asyncio.Task[None] | None = None

    # ---- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        self._listener = asyncio.create_task(self._listen(), name="gpu-scheduler-pubsub")

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            with suppress(asyncio.CancelledError):
                await self._listener
            self._listener = None

    async def _listen(self) -> None:
        """Turn pub/sub messages from any replica into local wake-ups. Polling covers any gap."""
        while True:
            pubsub = self.redis.pubsub()
            try:
                await pubsub.subscribe(self.channel)
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message is not None:
                        self._notify_local()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("scheduler pub/sub connection lost; falling back to polling", exc_info=True)
                await asyncio.sleep(1.0)
            finally:
                with suppress(Exception):
                    await pubsub.aclose()

    def _notify_local(self) -> None:
        event, self._changed = self._changed, asyncio.Event()
        event.set()

    # ---- public API ------------------------------------------------------------------------

    @asynccontextmanager
    async def slot(self, priority: Priority, principal_id: int, kind: str) -> AsyncIterator[Slot]:
        """Wait for a GPU slot and hold it for the duration of the block.

        Raises QueueFull, QueueTimeout or Superseded. Cancelling the waiting task (for example on
        client disconnect) removes the ticket from the queue.
        """
        priority = Priority(priority)
        ticket = uuid.uuid4().hex
        started = time.monotonic()
        score = int(priority) * PRIORITY_SPAN + int(time.time() * 1000)
        supersede = priority == Priority.AUTOCOMPLETE

        admitted, superseded_old = await self._enqueue(
            keys=[self.queue_key, f"{self.prefix}ac:{principal_id}"],
            args=[
                self.prefix,
                ticket,
                score,
                self.max_queue,
                self.lease_ttl_ms,
                "1" if supersede else "0",
                f"{kind}:{principal_id}",
            ],
        )
        if superseded_old:
            await self._publish()  # wake the superseded waiter, wherever it runs
        if not admitted:
            raise QueueFull()

        acquired = False
        try:
            await self._wait_for_turn(ticket, score, started)
            acquired = True
        finally:
            if not acquired:
                await run_shielded(self._remove_ticket(ticket), what="removing GPU ticket")

        queue_ms = int((time.monotonic() - started) * 1000)
        QUEUE_WAIT.labels(priority=priority.name.lower()).observe(queue_ms / 1000)
        heartbeat = asyncio.create_task(self._keep_lease(ticket), name=f"gpu-lease-{ticket[:8]}")
        try:
            yield Slot(ticket=ticket, priority=priority, kind=kind, queue_ms=queue_ms)
        finally:
            heartbeat.cancel()
            await run_shielded(self._release(ticket, heartbeat), what="releasing GPU lease")

    async def stats(self) -> QueueStats:
        bounds: list[int | str] = []
        for p in Priority:
            bounds += [int(p) * PRIORITY_SPAN, f"({(int(p) + 1) * PRIORITY_SPAN}"]
        raw = await self._stats(keys=[self.queue_key, self.leases_key], args=[self.prefix, *bounds])
        values = [int(v) for v in raw]
        return QueueStats(inflight=values[0], by_priority=dict(zip(Priority, values[1:], strict=True)))

    async def queue_depth(self) -> int:
        return (await self.stats()).queued

    async def inflight(self) -> int:
        return (await self.stats()).inflight

    async def depth_by_priority(self) -> dict[Priority, int]:
        return (await self.stats()).by_priority

    # ---- internals -------------------------------------------------------------------------

    async def _wait_for_turn(self, ticket: str, score: int, started: float) -> None:
        deadline = started + self.queue_timeout_s
        last_heartbeat = time.monotonic()
        while True:
            changed = self._changed
            result = int(
                await self._acquire(
                    keys=[self.queue_key, self.leases_key],
                    args=[self.prefix, ticket, self.max_inflight, self.lease_ttl_ms],
                )
            )
            if result == 1:
                return
            if result == -2:
                raise Superseded()
            if result == -1:
                # Our ticket expired (e.g. the event loop stalled past its TTL). Put it back at
                # its original position rather than failing the request.
                await self._readd_ticket(ticket, score)

            now = time.monotonic()
            if now >= deadline:
                raise QueueTimeout(now - started)
            if now - last_heartbeat >= self.heartbeat_s:
                await self.redis.pexpire(f"{self.prefix}ticket:{ticket}", self.lease_ttl_ms)
                last_heartbeat = now
            with suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), timeout=min(self.poll_s, deadline - now))

    async def _readd_ticket(self, ticket: str, score: int) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zadd(self.queue_key, {ticket: score})
            pipe.set(f"{self.prefix}ticket:{ticket}", "1", px=self.lease_ttl_ms)
            await pipe.execute()

    async def _keep_lease(self, ticket: str) -> None:
        key = f"{self.prefix}lease:{ticket}"
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                if not await self.redis.pexpire(key, self.lease_ttl_ms):
                    log.warning("GPU lease %s expired while held; the slot count may briefly overshoot", ticket)
            except Exception:
                log.warning("could not refresh GPU lease %s", ticket, exc_info=True)

    async def _remove_ticket(self, ticket: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrem(self.queue_key, ticket)
            pipe.delete(f"{self.prefix}ticket:{ticket}")
            await pipe.execute()
        await self._publish()

    async def _release(self, ticket: str, heartbeat: asyncio.Task[None]) -> None:
        with suppress(asyncio.CancelledError):
            await heartbeat
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(f"{self.prefix}lease:{ticket}")
            pipe.srem(self.leases_key, ticket)
            await pipe.execute()
        await self._publish()

    async def _publish(self) -> None:
        self._notify_local()
        try:
            await self.redis.publish(self.channel, "changed")
        except Exception:
            log.warning("could not publish scheduler event; other replicas will poll", exc_info=True)
