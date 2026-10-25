"""GpuScheduler: priorities, FIFO, leases, queue limits, timeouts, supersession, cancellation."""

from __future__ import annotations

import asyncio
import contextlib

import fakeredis
import pytest

from app.scheduler import GpuScheduler, Priority, QueueFull, QueueTimeout, Superseded


def make_scheduler(redis, **overrides) -> GpuScheduler:
    options = {"max_inflight": 1, "max_queue": 8, "queue_timeout_s": 5.0, "poll_s": 0.05}
    options.update(overrides)
    return GpuScheduler(redis, **options)


@contextlib.asynccontextmanager
async def started(*schedulers: GpuScheduler):
    for s in schedulers:
        await s.start()
    try:
        yield
    finally:
        for s in schedulers:
            await s.stop()


async def wait_until(predicate, timeout: float = 3.0) -> None:
    async def poll():
        while not await predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


async def queued(scheduler: GpuScheduler, n: int) -> None:
    async def check():
        return await scheduler.queue_depth() >= n

    await wait_until(check)


async def test_lower_priority_value_runs_first_and_fifo_within_priority(redis):
    scheduler = make_scheduler(redis)
    order: list[str] = []

    async def job(name: str, priority: Priority, principal: int):
        async with scheduler.slot(priority, principal, name):
            order.append(name)

    async with started(scheduler):
        async with scheduler.slot(Priority.CHAT, 1, "holder"):
            # Enqueue in an order that differs from the expected run order.
            tasks = []
            for name, priority, principal in [
                ("batch-1", Priority.BATCH, 2),
                ("chat-1", Priority.CHAT, 3),
                ("chat-2", Priority.CHAT, 4),
                ("auto-1", Priority.AUTOCOMPLETE, 5),
            ]:
                tasks.append(asyncio.create_task(job(name, priority, principal)))
                await queued(scheduler, len(tasks))
            assert (await scheduler.depth_by_priority()) == {
                Priority.AUTOCOMPLETE: 1,
                Priority.CHAT: 2,
                Priority.BATCH: 1,
            }
            assert await scheduler.inflight() == 1
        await asyncio.gather(*tasks)

    assert order == ["auto-1", "chat-1", "chat-2", "batch-1"]
    assert await scheduler.inflight() == 0
    assert await scheduler.queue_depth() == 0


async def test_max_inflight_is_shared_by_replicas(redis, fake_server):
    # Two scheduler instances on one Redis behave like two gateway replicas.
    a = make_scheduler(redis, max_inflight=2)
    b = make_scheduler(fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True), max_inflight=2)
    running = 0
    peak = 0

    async def job(s: GpuScheduler, i: int):
        nonlocal running, peak
        async with s.slot(Priority.CHAT, i, "chat"):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1

    async with started(a, b):
        await asyncio.gather(*(job(a if i % 2 else b, i) for i in range(8)))
    assert peak == 2


async def test_pubsub_wakes_waiter_on_another_replica_quickly(redis, fake_server):
    # Polling is slow here (2 s), so a fast hand-over proves the pub/sub notification works.
    a = make_scheduler(redis, poll_s=2.0)
    b = make_scheduler(fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True), poll_s=2.0)
    async with started(a, b):
        await asyncio.sleep(0.1)  # let both subscribe
        holder = a.slot(Priority.CHAT, 1, "chat")
        await holder.__aenter__()

        async def waiter():
            async with b.slot(Priority.CHAT, 2, "chat") as slot:
                return slot

        task = asyncio.create_task(waiter())
        await queued(a, 1)
        loop = asyncio.get_running_loop()
        released_at = loop.time()
        await holder.__aexit__(None, None, None)
        await asyncio.wait_for(task, 1.0)
        assert loop.time() - released_at < 1.0


async def test_queue_full_rejects_new_tickets(redis):
    scheduler = make_scheduler(redis, max_queue=2)
    async with started(scheduler):
        async with scheduler.slot(Priority.CHAT, 1, "holder"):
            waiters = [asyncio.create_task(_hold(scheduler, Priority.CHAT, p)) for p in (2, 3)]
            await queued(scheduler, 2)
            with pytest.raises(QueueFull) as info:
                async with scheduler.slot(Priority.CHAT, 4, "chat"):
                    pass
            assert info.value.status == 429
            assert info.value.type == "queue_full"
            assert info.value.headers()["Retry-After"]
        await asyncio.gather(*waiters)


async def _hold(scheduler: GpuScheduler, priority: Priority, principal: int, kind: str = "chat"):
    async with scheduler.slot(priority, principal, kind):
        await asyncio.sleep(0)


async def test_waiting_longer_than_the_timeout_raises_504(redis):
    scheduler = make_scheduler(redis, queue_timeout_s=0.3)
    async with started(scheduler), scheduler.slot(Priority.CHAT, 1, "holder"):
        with pytest.raises(QueueTimeout) as info:
            async with scheduler.slot(Priority.CHAT, 2, "chat"):
                pass
        assert info.value.status == 504
        assert info.value.type == "queue_timeout"
        # The timed-out ticket must not linger in the queue.
        assert await scheduler.queue_depth() == 0


async def test_lease_of_a_crashed_holder_expires(redis):
    scheduler = make_scheduler(redis, lease_ttl_s=0.3, heartbeat_s=0.1)
    # What a replica that died mid-request leaves behind: a lease nobody refreshes any more.
    await redis.set(f"{scheduler.prefix}lease:crashed", "1", px=300)
    await redis.sadd(scheduler.leases_key, "crashed")
    async with started(scheduler):
        assert await scheduler.inflight() == 1
        loop = asyncio.get_running_loop()
        start = loop.time()
        async with scheduler.slot(Priority.CHAT, 2, "chat"):
            waited = loop.time() - start
            assert await scheduler.inflight() == 1  # only ours; the stale one was pruned
        assert 0.2 <= waited < 2.0


async def test_heartbeat_keeps_a_long_lease_alive(redis):
    scheduler = make_scheduler(redis, lease_ttl_s=0.3, heartbeat_s=0.05)
    async with started(scheduler):
        async with scheduler.slot(Priority.CHAT, 1, "chat"):
            await asyncio.sleep(0.8)  # well past the TTL
            assert await scheduler.inflight() == 1
        assert await scheduler.inflight() == 0


async def test_newer_autocomplete_supersedes_queued_one_from_same_principal(redis):
    scheduler = make_scheduler(redis)
    async with started(scheduler):
        async with scheduler.slot(Priority.CHAT, 99, "holder"):
            older = asyncio.create_task(_hold(scheduler, Priority.AUTOCOMPLETE, 7, "completions"))
            await queued(scheduler, 1)
            other_user = asyncio.create_task(_hold(scheduler, Priority.AUTOCOMPLETE, 8, "completions"))
            await queued(scheduler, 2)
            newer = asyncio.create_task(_hold(scheduler, Priority.AUTOCOMPLETE, 7, "completions"))

            with pytest.raises(Superseded) as info:
                await asyncio.wait_for(older, 2.0)
            assert info.value.status == 499
            assert info.value.type == "superseded"
            await asyncio.sleep(0.1)
            assert not newer.done() and not other_user.done()
            assert await scheduler.queue_depth() == 2
        await asyncio.wait_for(asyncio.gather(newer, other_user), 2.0)


async def test_chat_requests_are_never_superseded(redis):
    scheduler = make_scheduler(redis)
    async with started(scheduler):
        async with scheduler.slot(Priority.CHAT, 99, "holder"):
            first = asyncio.create_task(_hold(scheduler, Priority.CHAT, 7))
            await queued(scheduler, 1)
            second = asyncio.create_task(_hold(scheduler, Priority.CHAT, 7))
            await queued(scheduler, 2)
        await asyncio.wait_for(asyncio.gather(first, second), 2.0)


async def test_cancelling_a_waiter_removes_its_ticket(redis):
    scheduler = make_scheduler(redis)
    async with started(scheduler), scheduler.slot(Priority.CHAT, 1, "holder"):
        task = asyncio.create_task(_hold(scheduler, Priority.CHAT, 2))
        await queued(scheduler, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await scheduler.queue_depth() == 0


async def test_dead_waiters_ticket_is_pruned(redis):
    scheduler = make_scheduler(redis, lease_ttl_s=0.2)
    async with started(scheduler):
        # A ticket whose waiter vanished: in the queue but its liveness key never refreshed.
        await redis.zadd(scheduler.queue_key, {"ghost": 0})
        async with scheduler.slot(Priority.BATCH, 1, "batch") as slot:
            assert slot.priority == Priority.BATCH
        assert await scheduler.queue_depth() == 0
