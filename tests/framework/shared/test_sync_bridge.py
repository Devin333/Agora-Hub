from __future__ import annotations

import asyncio

import pytest

from framework.shared.sync_bridge import resolve_maybe_awaitable, run_coroutine_sync


async def _double(value: int) -> int:
    await asyncio.sleep(0)
    return value * 2


async def _fail() -> None:
    raise LookupError("missing")


def test_runs_a_coroutine_without_an_event_loop() -> None:
    assert run_coroutine_sync(_double(3)) == 6


def test_runs_a_coroutine_inside_a_running_loop_without_reentering_it() -> None:
    async def caller() -> int:
        return run_coroutine_sync(_double(5))

    assert asyncio.run(caller()) == 10


def test_reraises_the_coroutine_error_in_the_caller() -> None:
    with pytest.raises(LookupError):
        run_coroutine_sync(_fail())

    async def caller() -> None:
        run_coroutine_sync(_fail())

    with pytest.raises(LookupError):
        asyncio.run(caller())


def test_accepts_non_coroutine_awaitables() -> None:
    class _Awaitable:
        def __await__(self):
            return _double(4).__await__()

    assert run_coroutine_sync(_Awaitable()) == 8


def test_resolve_maybe_awaitable_passes_plain_values_through() -> None:
    marker = object()

    assert resolve_maybe_awaitable(marker) is marker
    assert resolve_maybe_awaitable(_double(1)) == 2
