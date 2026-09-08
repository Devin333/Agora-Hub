from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from framework.agent.artifacts.stores.filesystem import FilesystemArtifactStore
from framework.events.runtime.publisher import EventRuntime
from framework.events.schema import default_event_schema_catalog
from framework.harness.agent_loop.child_executor import HarnessSubAgentTaskExecutor
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_results import HarnessResultRefAuthority
from framework.harness.ref_snapshot_store import DurableRefAuthoritySnapshotStore
from framework.harness.subagents.models import SubAgentSpec
from framework.harness.subagents.runtime import SubAgentRuntime
from framework.harness.task_plan.capability import (
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
)
from framework.harness.task_plan.durable_store import DurableTaskPlanStore
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.verification import TaskPlanGateRegistry, TaskPlanResultVerifier
from framework.harness.workers.result import HarnessWorkerResult
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from infrastructure.storage.events.sqlite import SQLiteEventStore
from infrastructure.storage.harness import FilesystemSubAgentTranscriptStore


class _DelegatedSummaryWorker:
    worker_type = HarnessWorkerType.SUBAGENT

    def __init__(
        self,
        *,
        worker_id: str,
        worker_version: str,
        on_worker: Callable[[dict[str, Any], GraphExecutionIdentity], None] | None,
    ) -> None:
        self.worker_id = worker_id
        self.worker_version = worker_version
        self._on_worker = on_worker

    def execute(
        self,
        task: dict[str, Any],
        *,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> HarnessWorkerResult:
        if self._on_worker is not None:
            if not isinstance(execution_identity, GraphExecutionIdentity):
                raise AssertionError("delegated fixture worker requires Graph execution identity")
            self._on_worker(task, execution_identity)
        return HarnessWorkerResult(
            status="succeeded",
            output={"summary": f"verified summary from {self.worker_id}"},
        )


def build_ref_admission(root: str | Path) -> HarnessRefAdmissionService:
    root_path = Path(root)
    events = SQLiteEventStore(root_path / "ref-events.sqlite3")
    runtime = EventRuntime(store=events, schema_catalog=default_event_schema_catalog())
    store = DurableRefAuthoritySnapshotStore(
        runtime,
        events,
        artifact_store=FilesystemArtifactStore(root_path / "ref-artifacts"),
        tenant_id="production",
    )
    return HarnessRefAdmissionService(store)


def build_task_plan_store(root: str | Path) -> DurableTaskPlanStore:
    root_path = Path(root)
    events = SQLiteEventStore(root_path / "task-plan-events.sqlite3")
    runtime = EventRuntime(store=events, schema_catalog=default_event_schema_catalog())
    return DurableTaskPlanStore(
        runtime,
        events,
        artifact_store=FilesystemArtifactStore(root_path / "task-plan-artifacts"),
        tenant_id="production",
    )


def build_child_dependencies(
    root: str | Path,
    *,
    policy: TaskPlanPolicy,
    store: DurableTaskPlanStore,
    admission: HarnessRefAdmissionService,
    on_worker: Callable[[dict[str, Any], GraphExecutionIdentity], None] | None = None,
) -> SimpleNamespace:
    """Build one inspectable, production-shaped generic child dependency graph."""

    root_path = Path(root)
    workers: dict[str, _DelegatedSummaryWorker] = {}
    registrations: list[TaskCapabilityRegistration] = []
    subagent_ids: list[str] = []
    for capability in policy.allowed_worker_capabilities:
        worker_ref = policy.pinned_capability_bindings[capability]
        worker_id, worker_version = worker_ref.rsplit("@", 1)
        subagent_id = f"{capability}.subagent"
        subagent_ids.append(subagent_id)
        worker = _DelegatedSummaryWorker(
            worker_id=worker_id,
            worker_version=worker_version,
            on_worker=on_worker,
        )
        workers[subagent_id] = worker
        registrations.append(
            TaskCapabilityRegistration(
                capability=capability,
                worker_binding=HarnessWorkerBinding(
                    HarnessContractReference(
                        HarnessContractKind.WORKER,
                        worker_id,
                        worker_version,
                    ),
                    HarnessWorkerType.SUBAGENT,
                    worker,
                ),
                worker_contract_ref=policy.required_worker_contract_refs[capability],
                input_schema_ref="schema://generic-child-input@1",
                output_schema_ref=policy.allowed_output_schema_refs[0],
                subagent_spec=SubAgentSpec(
                    subagent_id=subagent_id,
                    role=capability,
                    purpose=f"Execute admitted capability {capability}.",
                    input_schema={
                        "required": ["input_refs", "task_id", "task_definition_checksum"],
                    },
                    output_schema={
                        "required": ["summary"],
                        "properties": {"summary": {"type": "string"}},
                    },
                    allowed_tools=policy.allowed_tool_ids,
                    allowed_memory_namespaces=policy.allowed_memory_namespaces,
                    context_policy={"allow_sibling_history": False},
                    budget={
                        "max_turns": policy.per_task_budget.max_turns,
                        "max_tool_calls": policy.per_task_budget.max_tool_calls,
                        "max_memory_ops": policy.per_task_budget.max_memory_ops,
                    },
                ),
            )
        )

    bound_policy = replace(policy, allowed_subagent_ids=tuple(subagent_ids))
    transcripts = FilesystemSubAgentTranscriptStore(root_path / "subagent-transcripts")
    artifacts = FilesystemHarnessArtifactPort(root_path / "artifacts")
    result_ref_authority = HarnessResultRefAuthority(
        admission.store,
        transcript_store=transcripts,
        artifact_descriptors=artifacts,
        tenant_id="production",
    )
    runtime = SubAgentRuntime(
        workers=workers,
        transcript_store=transcripts,
        result_ref_authority=result_ref_authority,
    )
    gates = TaskPlanGateRegistry()

    def verify_summary(request: Any) -> bool:
        summary = request.worker_result.output.get("summary")
        return isinstance(summary, str) and bool(summary.strip())

    for gate_ref in bound_policy.allowed_gate_refs:
        gates.register(gate_ref, verify_summary, deterministic=True)
    verifier = TaskPlanResultVerifier(
        gates,
        transcript_store=transcripts,
        artifact_reference_verifier=artifacts,
        result_ref_authority=result_ref_authority,
    )
    executor = HarnessSubAgentTaskExecutor(
        store=store,
        runtime=runtime,
        ref_admission_service=admission,
    )
    return SimpleNamespace(
        policy=bound_policy,
        capability_registry=TaskCapabilityRegistry(registrations),
        executor=executor,
        verifier=verifier,
        runtime=runtime,
        transcripts=transcripts,
        artifacts=artifacts,
    )


__all__ = [
    "build_child_dependencies",
    "build_ref_admission",
    "build_task_plan_store",
]
