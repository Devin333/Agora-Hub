from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from framework.events import EventRuntime, EventSchemaCatalog
from framework.harness.subagents import (
    ChildAgentSpawnRequest,
    ChildAgentState,
    DurableChildAgentEventLog,
    HarnessOwnedChildAgentRuntime,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import (
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolSideEffect,
    ToolStatus,
)
from infrastructure.storage.events.sqlite import SQLiteEventStore


STATE_KEY = "integration.execution-recovery.children"
RUN_ID = "execution-recovery-run"
TENANT_ID = "tenant-a"


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id=RUN_ID,
        graph_id="execution-recovery",
        graph_version="1",
        graph_ref="execution-recovery@1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="controller",
        node_instance_id="controller:1",
        activity_id="controller-activity",
        attempt=1,
    )


def _request() -> ChildAgentSpawnRequest:
    return ChildAgentSpawnRequest(
        parent_graph_identity=_identity(),
        stage_id="integration",
        task_id="execute-tool",
        task_instance_id="execute-tool:1",
        attempt=1,
        allowed_tools=("sample.publish",),
        allowed_memory_namespaces=("integration",),
        budget={"turns": 2},
        operation_id="external-effect-operation",
        child_id="child-external-effect-operation",
        lease_seconds=60,
    )


def _write(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _wait(path: Path, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"barrier timeout: {path}")
        time.sleep(0.01)


def _runtime(database: Path, owner_id: str) -> HarnessOwnedChildAgentRuntime:
    clock_offset = 60 if owner_id.endswith("recover") else 0
    clock = lambda: datetime(2026, 9, 23, tzinfo=UTC) + timedelta(
        seconds=clock_offset
    )
    store = SQLiteEventStore(database)
    event_log = DurableChildAgentEventLog(
        state_runtime=EventRuntime(
            store=store,
            schema_catalog=EventSchemaCatalog(),
            backend="sqlite",
        ),
        state_reader=store,
        state_key=STATE_KEY,
        clock=clock,
    )
    runtime = HarnessOwnedChildAgentRuntime(
        event_log=event_log,
        max_children=1,
        owner_id=owner_id,
        clock=clock,
        renewal_interval_seconds=None,
    )
    runtime.start()
    runtime.register_run_scope(RUN_ID, TENANT_ID)
    return runtime


def _execute_tool(effect_log: Path) -> dict[str, object]:
    def publish(arguments: dict[str, object]) -> dict[str, object]:
        with effect_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(arguments, sort_keys=True) + "\n")
        return {"published": arguments["document_id"]}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.publish",
            side_effect=ToolSideEffect.WRITES_EXTERNAL_STATE,
        ),
        publish,
    )
    observation = ToolExecutor(registry, graph_identity=_identity()).execute(
        ToolCall(
            tool_name="sample.publish",
            arguments={"document_id": "paper-1"},
            call_id="publish-paper-1",
            graph_identity=_identity(),
        ),
        ToolPolicy(
            require_explicit_allowlist=False,
            require_approval_for_side_effects=False,
        ),
    )
    if observation.status is not ToolStatus.SUCCEEDED:
        raise AssertionError(f"external write tool failed: {observation.status}")
    return {
        "candidate": observation.result.output,
        "logical_tool_key": "tool:publish-paper-1",
    }


def main() -> int:
    mode = sys.argv[1]
    database = Path(sys.argv[2])
    signal = Path(sys.argv[3])
    release = Path(sys.argv[4])
    effect_log = Path(sys.argv[5])
    runtime = _runtime(database, f"controller-{mode}")
    try:
        if mode == "commit":
            worker = lambda _handle: _execute_tool(effect_log)
        elif mode == "recover":
            worker = lambda _handle: (_ for _ in ()).throw(
                AssertionError("recovered child worker was reinvoked")
            )
        else:
            return 2
        handle = runtime.supervisor.spawn(_request(), worker=worker)
        result = runtime.supervisor.wait(
            handle.child_id,
            operation_id=handle.operation_id,
            timeout_seconds=5,
        )
        if result.receipt is None or result.receipt.status is not ChildAgentState.SUCCEEDED:
            raise AssertionError("child result was not durably committed")
        _write(
            signal,
            {
                "receipt": result.receipt.to_dict(),
                "result": result.result,
            },
        )
        _wait(release)
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
