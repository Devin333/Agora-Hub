"""Request-scoped context must follow work onto harness worker threads."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

from backend.research.rag.adapters.answer_worker import _run_async
from backend.research.rag.adapters.llm_plan_worker import _run_maybe_async
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
from tests.framework.harness.subagents.test_child_agent_supervisor import (
    _request as _child_request,
)
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _accepted_parallel_plan,
    _request,
    _result,
)

_REQUEST_OWNER: ContextVar[str | None] = ContextVar("test_request_owner", default=None)


async def _owner_from_coroutine() -> str | None:
    return _REQUEST_OWNER.get()


def _run_inside_running_loop(fn):
    async def outer():
        return fn()

    return asyncio.run(outer())


def test_answer_worker_async_bridge_keeps_caller_context() -> None:
    token = _REQUEST_OWNER.set("alice")
    try:
        assert _run_inside_running_loop(lambda: _run_async(_owner_from_coroutine())) == "alice"
    finally:
        _REQUEST_OWNER.reset(token)


def test_plan_worker_async_bridge_keeps_caller_context() -> None:
    token = _REQUEST_OWNER.set("bob")
    try:
        assert (
            _run_inside_running_loop(lambda: _run_maybe_async(_owner_from_coroutine()))
            == "bob"
        )
    finally:
        _REQUEST_OWNER.reset(token)


def test_child_supervisor_runs_children_in_the_spawning_context() -> None:
    supervisor = ChildAgentSupervisor(
        worker_factory=lambda _: {"owner": _REQUEST_OWNER.get()}
    )
    try:
        token = _REQUEST_OWNER.set("alice")
        try:
            handle = supervisor.spawn(_child_request())
        finally:
            _REQUEST_OWNER.reset(token)
        result = supervisor.wait(handle.child_id, operation_id="op-1", timeout_seconds=5)
    finally:
        supervisor.shutdown()
    assert result.result == {"owner": "alice"}


def test_parallel_dispatch_runs_tasks_in_the_dispatching_context() -> None:
    plan = _accepted_parallel_plan()
    request = _request(plan)
    seen: list[str | None] = []
    coordinator = ParallelAgentCoordinator(max_workers=2, allow_test_executor=True)

    def invoke(instance):
        seen.append(_REQUEST_OWNER.get())
        return _result(plan, instance)

    token = _REQUEST_OWNER.set("carol")
    try:
        dispatched = coordinator.dispatch(request, invoke)
    finally:
        _REQUEST_OWNER.reset(token)

    assert dispatched.succeeded is True
    assert seen and set(seen) == {"carol"}
