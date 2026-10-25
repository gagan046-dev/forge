"""Small asyncio helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Coroutine
from contextlib import suppress
from typing import Any

import anyio
from starlette.types import Receive

from app.errors import ForgeError

log = logging.getLogger(__name__)

_cleanup_tasks: set[asyncio.Task[Any]] = set()


async def run_shielded(coro: Coroutine[Any, Any, Any], *, what: str) -> None:
    """Run cleanup to completion even when the caller is being cancelled.

    Starlette cancels a disconnected client's request through an anyio cancel scope, which would
    re-cancel every await in a plain `finally`. The shielded scope protects against that, and
    asyncio.shield keeps the cleanup task running if a native task.cancel() arrives anyway.
    Failures are logged, not raised: cleanup must never mask the original outcome.
    """
    task = asyncio.ensure_future(coro)
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)
    try:
        with anyio.CancelScope(shield=True):
            await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("%s failed", what, exc_info=True)


async def drain_cleanup_tasks() -> None:
    """Wait for in-flight cleanups (called on shutdown)."""
    if _cleanup_tasks:
        await asyncio.gather(*list(_cleanup_tasks), return_exceptions=True)


class ClientDisconnected(ForgeError):
    def __init__(self) -> None:
        # 499 is nginx's "client closed request"; nobody reads the body, it is for logs and metrics.
        super().__init__(499, "client_disconnected", "The client closed the connection")


async def _until_disconnected(receive: Receive) -> None:
    # The handler has already read the body, so the next ASGI message is the disconnect (the
    # server answers it immediately after the client goes away, or once the response is sent).
    while (await receive())["type"] != "http.disconnect":
        pass


async def cancel_on_disconnect[T](receive: Receive, work: Awaitable[T]) -> T:
    """Await `work`, cancelling it if the HTTP client disconnects first.

    Uvicorn does not cancel a request handler when the client goes away before the response has
    started; only a streaming response notices, and only once it is running. A request waiting for
    a GPU slot (or for a non-streamed answer) would otherwise keep its queue ticket and generate
    for nobody. Cancelling the work runs its normal cleanup: the ticket is removed and the usage
    event is recorded as 499 client_disconnected. Raises ClientDisconnected in that case.
    """
    task = asyncio.ensure_future(work)
    watcher = asyncio.ensure_future(_until_disconnected(receive))
    try:
        await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        watcher.cancel()
        await asyncio.gather(task, watcher, return_exceptions=True)
        raise

    if not task.done() and watcher.done() and watcher.exception() is None:
        task.cancel()
    else:
        watcher.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await watcher
    try:
        return await task  # also when the work finished just as the client left
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise  # this handler itself is being cancelled, not just the work
        raise ClientDisconnected() from None
