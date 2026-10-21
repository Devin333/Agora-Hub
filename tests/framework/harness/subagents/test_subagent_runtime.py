from __future__ import annotations

from framework.harness import FakeSubAgentRuntime, FakeSubAgentWorker, HarnessWorkerResult, SubAgentStatus, fake_subagent_spec


def test_subagent_runtime_invokes_worker_with_isolated_context() -> None:
    runtime = FakeSubAgentRuntime(fake_subagent_spec())
    invocation = runtime.build_invocation()

    result = runtime.invoke(invocation)

    assert result.status == SubAgentStatus.SUCCEEDED
    assert result.transcript_ref
    assert invocation.child_run_id != invocation.parent_run_id


def test_subagent_runtime_rejects_unauthorized_tool_call() -> None:
    worker = FakeSubAgentWorker(
        (HarnessWorkerResult(status="succeeded", output={"result": "ok", "requested_tools": ["artifact.write"]}),)
    )
    runtime = FakeSubAgentRuntime(fake_subagent_spec(allowed_tools=("search.read",)), worker)
    result = runtime.invoke(runtime.build_invocation())

    assert result.status == SubAgentStatus.HALTED
    gate_results = result.metadata["gate_results"]
    assert any(gate["gate"] == "subagent_tool_allowlist" and gate["passed"] is False for gate in gate_results)


def test_subagent_runtime_rejects_unauthorized_memory_namespace() -> None:
    worker = FakeSubAgentWorker(
        (HarnessWorkerResult(status="succeeded", output={"result": "ok", "memory_namespaces": ["research.private"]}),)
    )
    runtime = FakeSubAgentRuntime(fake_subagent_spec(allowed_memory_namespaces=("research.public",)), worker)
    result = runtime.invoke(runtime.build_invocation())

    assert result.status == SubAgentStatus.HALTED
    assert any(
        gate["gate"] == "subagent_memory_namespace" and gate["passed"] is False
        for gate in result.metadata["gate_results"]
    )


def test_subagent_runtime_halts_when_child_budget_exceeded() -> None:
    worker = FakeSubAgentWorker((HarnessWorkerResult(status="succeeded", output={"result": "ok", "turns_used": 3}),))
    runtime = FakeSubAgentRuntime(fake_subagent_spec(budget={"max_turns": 1, "max_tool_calls": 1, "max_memory_ops": 1}), worker)
    result = runtime.invoke(runtime.build_invocation())

    assert result.status == SubAgentStatus.HALTED
    assert any(gate["gate"] == "subagent_budget" and gate["passed"] is False for gate in result.metadata["gate_results"])


def test_subagent_runtime_rechecks_cancellation_at_worker_dispatch(monkeypatch) -> None:
    from threading import Event, Thread

    from framework.harness.subagents import runtime as runtime_module
    from framework.harness.subagents.execution_control import bind_child_execution_control
    from tests.framework.harness.subagents.test_execution_control import _control

    control, _, _ = _control()
    worker = FakeSubAgentWorker()
    runtime = FakeSubAgentRuntime(fake_subagent_spec(), worker)
    invocation = runtime.build_invocation()
    dispatch_ready = Event()
    release_dispatch = Event()
    results = []
    errors = []
    inspect_identity = runtime_module._accepts_execution_identity

    def pause_admission(callback):
        dispatch_ready.set()
        assert release_dispatch.wait(timeout=5)
        return inspect_identity(callback)

    monkeypatch.setattr(runtime_module, "_accepts_execution_identity", pause_admission)

    def invoke():
        try:
            with bind_child_execution_control(control):
                results.append(runtime.invoke(invocation))
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=invoke)
    try:
        thread.start()
        assert dispatch_ready.wait(timeout=5)
        control.request_cancel()
        release_dispatch.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors == []
        assert worker.calls == []
        assert len(results) == 1
        assert results[0].status is SubAgentStatus.HALTED
    finally:
        release_dispatch.set()
        thread.join(timeout=5)
