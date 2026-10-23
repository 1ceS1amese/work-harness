"""Keep persistence locks held until offloaded work has actually finished."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

_T = TypeVar("_T")


async def settle_before_cancel(operation: Awaitable[_T]) -> _T:
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling to_thread only cancels its waiter, never the underlying thread.
        # Drain even repeated cancellation before the caller releases its write lock.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def write_in_thread(operation: Callable[..., _T], *args: object) -> _T:
    return await settle_before_cancel(asyncio.to_thread(operation, *args))
