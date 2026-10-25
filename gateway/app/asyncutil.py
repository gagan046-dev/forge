"""Small asyncio helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import anyio

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
