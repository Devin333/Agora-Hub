from __future__ import annotations

import json
import sqlite3
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
from infrastructure.storage.events.sqlite import SQLiteEventStore


SCOPE = "research.dynamic-analysis.children"
RUN_ID = "independent-process-run"
TENANT_ID = "independent-process-tenant"


def _signal(path: Path, payload: dict[str, object] | None = None) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload or {"ready": True}), encoding="utf-8")
    temporary.replace(path)


def _wait(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"barrier timeout: {path}")
        time.sleep(0.01)


def _clock(offset: int) -> datetime:
    return datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset)


def _open_runtime(
    database: Path, *, owner_id: str, clock_offset: int = 0, lease_seconds: float = 30.0
):
    store = SQLiteEventStore(database, initialize=False)
    event_runtime = EventRuntime(
        store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite"
    )
    event_log = DurableChildAgentEventLog(
        state_runtime=event_runtime,
        state_reader=store,
        state_key=SCOPE,
        owner_lease_seconds=lease_seconds,
        clock=lambda: _clock(clock_offset),
    )
    owned = HarnessOwnedChildAgentRuntime(
        event_log=event_log,
        max_children=1,
        owner_id=owner_id,
        clock=lambda: _clock(clock_offset),
        renewal_interval_seconds=None,
    )
    return store, owned


def _request(*, operation_id: str = "independent-operation") -> ChildAgentSpawnRequest:
    identity = GraphExecutionIdentity(
        run_id=RUN_ID,
        graph_id="research.dynamic",
        graph_version="1",
        graph_ref="research.dynamic@1",
        graph_checksum="sha256:" + "1" * 64,
        node_id="dynamic-child",
        node_instance_id="dynamic-child-instance",
        activity_id="dynamic-child-activity",
        attempt=1,
    )
    return ChildAgentSpawnRequest(
        parent_graph_identity=identity,
        stage_id="dynamic_analysis",
        task_id="structure",
        task_instance_id="structure-instance",
        attempt=1,
        allowed_tools=("research.read",),
        allowed_memory_namespaces=("research",),
        budget={"turns": 2},
        operation_id=operation_id,
        child_id=(
            "independent-child"
            if operation_id == "independent-operation"
            else f"child-{operation_id}"
        ),
        lease_seconds=60.0,
    )


def _increment_external(counter_db: Path) -> None:
    with sqlite3.connect(counter_db, timeout=10.0) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS effects (id INTEGER PRIMARY KEY, count INTEGER NOT NULL)"
        )
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT count FROM effects WHERE id = 1").fetchone()
        count = 0 if row is None else int(row[0])
        if row is None:
            connection.execute(
                "INSERT INTO effects(id, count) VALUES(1, ?)", (count + 1,)
            )
        else:
            connection.execute(
                "UPDATE effects SET count = ? WHERE id = 1", (count + 1,)
            )
        connection.commit()


class _SuccessWorker:
    def __init__(self, counter_db: Path):
        self.counter_db = counter_db

    def run(self, _handle):
        _increment_external(self.counter_db)
        return {"candidate": "durably-committed"}


class _EffectThenBlockWorker:
    def __init__(self, counter_db: Path, effect_barrier: Path, release_barrier: Path):
        self.counter_db = counter_db
        self.effect_barrier = effect_barrier
        self.release_barrier = release_barrier

    def run(self, _handle):
        _increment_external(self.counter_db)
        _signal(self.effect_barrier, {"effect_committed": True})
        _wait(self.release_barrier, timeout=120.0)
        return {"candidate": "late"}


class _HoldWorker:
    def __init__(self, release_barrier: Path):
        self.release_barrier = release_barrier

    def run(self, _handle):
        _wait(self.release_barrier, timeout=120.0)
        return {"candidate": "held"}


class _DockerEffectWorker:
    """Exercise the actual provider with a side effect outside Python."""

    def __init__(self, options):
        self.options = options

    def run(self, handle):
        from framework.execution_environment import (
            ExecutionEnvironmentRegistry,
            ExecutionProfile,
            ExecutionRequest,
            ResourceLimits,
        )
        from infrastructure.execution_environment.docker import (
            DockerExecutionEnvironment,
        )

        registry = ExecutionEnvironmentRegistry()
        registry.register(DockerExecutionEnvironment())
        request = ExecutionRequest(
            execution_id=self.options["execution_id"],
            tool_id="qualification.external-effect@1",
            graph_identity=handle.child_graph_identity,
            operation_id=handle.operation_id,
            attempt_id=handle.task_instance_id,
            profile=ExecutionProfile.sandboxed_process(
                provider_id="docker",
                allowed_argv_prefixes=(("sh",),),
            ),
            image=self.options["image"],
            argv=(
                "sh",
                "-c",
                'printf "effect\n" >> "$1/effects"; '
                'exec sleep 120',
                "qualification",
                self.options["effect_root"],
            ),
            write_roots=(self.options["effect_root"],),
            resource_limits=ResourceLimits(
                max_memory_bytes=64 * 1024 * 1024, max_processes=1
            ),
            timeout_seconds=120,
            cancellation_grace_seconds=1,
        )
        outcome = registry.execute(request)
        return {"candidate": outcome.receipt.status.value}


def _write_outcome(path: Path, result) -> None:
    receipt = None if result.receipt is None else result.receipt.to_dict()
    path.write_text(
        json.dumps(
            {
                "state": result.handle.state.value,
                "receipt": receipt,
                "result": result.result,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def main() -> int:
    if len(sys.argv) < 4:
        return 2
    mode = sys.argv[1]
    database = Path(sys.argv[2])
    signal_path = Path(sys.argv[3])
    owner_id = sys.argv[4] if len(sys.argv) > 4 else f"owner-{mode}"
    options = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}
    store = None
    owned = None
    try:
        store, owned = _open_runtime(
            database,
            owner_id=owner_id,
            clock_offset=int(options.get("clock_offset", 0)),
            lease_seconds=float(options.get("lease_seconds", 30.0)),
        )
        try:
            owned.start()
            owned.register_run_scope(RUN_ID, TENANT_ID)
        except Exception as exc:
            _signal(
                signal_path,
                {
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "code": getattr(exc, "code", None),
                },
            )
            return 0
        supervisor = owned.supervisor
        if mode in {"docker_effect", "recover_docker"}:
            handle = supervisor.spawn(_request(), worker=_DockerEffectWorker(options))
            if mode == "docker_effect":
                _signal(signal_path, {"spawned": handle.child_id})
                _wait(Path(options["release_path"]), timeout=120.0)
            else:
                result = supervisor.wait(
                    handle.child_id, operation_id=handle.operation_id
                )
                _write_outcome(Path(options["outcome_path"]), result)
                _signal(signal_path, {"recovered": result.handle.state.value})
            return 0
        if mode == "hold":
            if options.get("spawn_child"):
                handle = supervisor.spawn(
                    _request(operation_id="held-operation"),
                    worker=_HoldWorker(Path(options["release_path"])),
                )
                _signal(signal_path, {"owner": owner_id, "child_id": handle.child_id})
            else:
                _signal(signal_path, {"owner": owner_id})
            if options.get("submit_path"):
                _wait(Path(options["submit_path"]), timeout=120.0)
                try:
                    supervisor.spawn(_request(operation_id="old-owner-operation"))
                except Exception as exc:
                    _signal(
                        Path(options["submit_outcome"]),
                        {
                            "error": type(exc).__name__,
                            "message": str(exc),
                            "code": getattr(exc, "code", None),
                        },
                    )
                else:
                    _signal(Path(options["submit_outcome"]), {"admitted": True})
            _wait(Path(options["release_path"]), timeout=120.0)
            return 0
        if mode == "compete":
            try:
                supervisor.spawn(_request(operation_id="competing-operation"))
            except (
                Exception
            ) as exc:  # typed owner/capacity denial is asserted by parent
                _signal(
                    signal_path,
                    {
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "code": getattr(exc, "code", None),
                    },
                )
                return 0
            _signal(signal_path, {"admitted": True})
            return 0
        if mode == "terminal":
            handle = supervisor.spawn(
                _request(), worker=_SuccessWorker(Path(options["counter_db"]))
            )
            result = supervisor.wait(
                handle.child_id, operation_id=handle.operation_id, timeout_seconds=10
            )
            if (
                result.receipt is None
                or result.receipt.status is not ChildAgentState.SUCCEEDED
            ):
                raise AssertionError("terminal worker did not commit succeeded receipt")
            _signal(signal_path, {"receipt_checksum": result.receipt.receipt_checksum})
            _wait(Path(options["release_path"]), timeout=120.0)
            return 0
        if mode == "effect":
            worker = _EffectThenBlockWorker(
                Path(options["counter_db"]),
                Path(options["effect_barrier"]),
                Path(options["release_path"]),
            )
            handle = supervisor.spawn(_request(), worker=worker)
            _signal(signal_path, {"spawned": handle.child_id})
            _wait(Path(options["release_path"]), timeout=120.0)
            return 0
        if mode == "recover":
            handle = supervisor.spawn(
                _request(), worker=_SuccessWorker(Path(options["counter_db"]))
            )
            result = supervisor.wait(handle.child_id, operation_id=handle.operation_id)
            _write_outcome(Path(options["outcome_path"]), result)
            _signal(signal_path, {"recovered": result.handle.state.value})
            return 0
        if mode == "takeover":
            try:
                supervisor.spawn(_request(operation_id="takeover-operation"))
            except Exception as exc:
                _signal(
                    signal_path,
                    {
                        "error": type(exc).__name__,
                        "message": str(exc),
                        "code": getattr(exc, "code", None),
                    },
                )
                return 0
            _signal(signal_path, {"admitted": True})
            return 0
        return 2
    finally:
        try:
            if owned is not None:
                owned.close()
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    raise SystemExit(main())
