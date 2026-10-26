"""Run a coroutine to completion from synchronous code.

Synchronous call sites (retrieval stages, worker adapters) sometimes need an async
port. Without a running event loop the coroutine runs on a fresh loop here; inside
a running loop (an async caller further up the stack) it runs on a fresh loop in a
helper thread, so the caller's loop is never re-entered.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from threading import Thread
from typing import Any, TypeVar

T = TypeVar("T")


def run_coroutine_sync(awaitable: Awaitable[T]) -> T:
    """Block until ``awaitable`` completes and return its result (or raise its error)."""
    coroutine = _as_coroutine(awaitable)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            result["value"] = asyncio.run(coroutine)
        except BaseException as exc:  # re-raised in the caller thread
            result["error"] = exc

    thread = Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


def resolve_maybe_awaitable(value: Any) -> Any:
    """Return ``value`` itself, or its awaited result when it is awaitable."""
    if not inspect.isawaitable(value):
        return value
    return run_coroutine_sync(value)


def _as_coroutine(awaitable: Awaitable[T]) -> Any:
    if inspect.iscoroutine(awaitable):
        return awaitable

    async def _await() -> T:
        return await awaitable

    return _await()


__all__ = ["resolve_maybe_awaitable", "run_coroutine_sync"]
