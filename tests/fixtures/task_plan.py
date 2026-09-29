from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from framework.events.canonical import checksum_for
from framework.harness.graph import (
    HarnessGraphSpec,
    HarnessStepSpec,
    HarnessWorkerType,
    StepRef,
)
from framework.harness.graph.compiler import HarnessGraphCompiler
from framework.harness.graph.definition import (
    HarnessGraphDefinition,
    HarnessGraphLeafBinding,
    HarnessGraphTaskPlanStageBinding,
)
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.side_effects.models import HarnessTerminalSideEffectPolicy
from framework.harness.task_plan import TaskPlanStageBinding
from framework.harness.task_plan.gate_evidence import (
    TaskPlanGateArtifactRefs,
    TaskPlanGateEvidence,
    TaskPlanGateVerificationArtifacts,
    TaskPlanWorkerResultInputArtifacts,
    normalize_gate_evidence_refs,
)
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.models import TaskInstance, ValidatedTaskPlan
from framework.harness.workers.result import HarnessWorkerResult, HarnessWorkerStatus
from framework.harness.task_plan.schema import (
    GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
)


_DEFAULT_SUPPORT_REFS = {
    "candidate_builder_ref": "test.task-plan-builder@1",
    "capability_registry_ref": "test.task-capability-registry@1",
    "gate_registry_ref": "test.task-gate-registry@1",
    "aggregator_ref": "test.task-plan-aggregator@1",
    "event_schema": "newsroom.harness-task-plan-event/v3",
    "checkpoint_ref": "test.task-plan-checkpoint@1",
    "result_store_ref": "test.task-plan-result-store@1",
}


@dataclass
class InMemoryTaskPlanGateArtifactWriter:
    """Typed test owner with exact scoped reads; production never uses it."""

    calls: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)
    _artifacts: dict[
        str,
        tuple[
            str,
            str,
            TaskPlanGateArtifactRefs,
            TaskInstance,
            HarnessWorkerResult,
            tuple[TaskPlanGateEvidence, ...],
        ],
    ] = field(default_factory=dict)
    _inputs: dict[
        str,
        tuple[str, str, TaskInstance, HarnessWorkerResult],
    ] = field(default_factory=dict)

    def persist_worker_result_input(
        self,
        plan: ValidatedTaskPlan,
        instance: TaskInstance,
        worker_result: HarnessWorkerResult,
    ) -> str:
        if worker_result.status is HarnessWorkerStatus.SUCCEEDED:
            raise AssertionError("worker failure proof requires a non-succeeded result")
        return self._persist_input(plan, instance, worker_result)

    def persist_gate_verification(
        self,
        plan: ValidatedTaskPlan,
        instance: TaskInstance,
        worker_result: HarnessWorkerResult,
        evidences: Sequence[TaskPlanGateEvidence],
    ) -> TaskPlanGateArtifactRefs:
        evidence_items = tuple(evidences)
        input_checksum = canonical_payload_checksum(
            {
                "instance": instance.checksum_projection(),
                "worker_result": worker_result.candidate_payload(),
            }
        )
        self._persist_input(plan, instance, worker_result)
        refs = TaskPlanGateArtifactRefs(
            input_checksum=input_checksum,
            evidence_checksums=tuple(
                item.evidence_checksum for item in evidence_items
            ),
        )
        self.calls.append(
            (
                plan.run_id,
                instance.task_instance_id,
                tuple(item.evidence_checksum for item in evidence_items),
            )
        )
        record = (
            plan.run_id,
            plan.stage_id,
            refs,
            instance,
            worker_result,
            evidence_items,
        )
        for evidence_ref in refs.evidence_checksums:
            existing = self._artifacts.get(evidence_ref)
            if existing is not None and existing != record:
                raise AssertionError("conflicting in-memory gate evidence ref")
            self._artifacts[evidence_ref] = record
        return refs

    def _persist_input(
        self,
        plan: ValidatedTaskPlan,
        instance: TaskInstance,
        worker_result: HarnessWorkerResult,
    ) -> str:
        if not instance.matches_plan_identity(plan) or worker_result.effect_intent is not None:
            raise AssertionError("invalid in-memory worker result input")
        input_checksum = canonical_payload_checksum(
            {
                "instance": instance.checksum_projection(),
                "worker_result": worker_result.candidate_payload(),
            }
        )
        record = (plan.run_id, plan.stage_id, instance, worker_result)
        existing = self._inputs.get(input_checksum)
        if existing is not None and existing != record:
            raise AssertionError("conflicting in-memory worker input ref")
        self._inputs[input_checksum] = record
        return input_checksum

    def read_worker_result_input(
        self,
        run_id: str,
        stage_id: str,
        input_checksum: str,
    ) -> TaskPlanWorkerResultInputArtifacts:
        record = self._inputs.get(input_checksum)
        if record is None:
            raise AssertionError("missing in-memory worker result input")
        stored_run, stored_stage, instance, worker_result = record
        if (stored_run, stored_stage) != (run_id, stage_id):
            raise AssertionError("in-memory worker result input scope mismatch")
        return TaskPlanWorkerResultInputArtifacts(
            input_checksum=input_checksum,
            instance=instance,
            worker_result=worker_result,
        )

    def read_gate_evidence(
        self,
        run_id: str,
        stage_id: str,
        gate_evidence_refs: Sequence[str],
    ) -> TaskPlanGateVerificationArtifacts:
        refs = normalize_gate_evidence_refs(gate_evidence_refs)
        records = tuple(self._artifacts.get(ref) for ref in refs)
        if any(record is None for record in records) or len(set(map(id, records))) != 1:
            raise AssertionError("missing or conflicting in-memory gate evidence")
        record = records[0]
        assert record is not None
        stored_run, stored_stage, stored_refs, instance, worker_result, evidences = record
        if (
            (stored_run, stored_stage) != (run_id, stage_id)
            or stored_refs.evidence_checksums != refs
        ):
            raise AssertionError("in-memory gate evidence scope mismatch")
        return TaskPlanGateVerificationArtifacts(
            refs=stored_refs,
            instance=instance,
            worker_result=worker_result,
            evidences=evidences,
        )


def build_task_plan_stage_binding(
    *,
    graph_id: str,
    stage_id: str,
    policy_ref: str,
    required_output_roles: Sequence[str],
    input_keys: Sequence[str] = ("document", "evidence_pack"),
    metadata_overrides: Mapping[str, Any] | None = None,
    worker_type: HarnessWorkerType | str = HarnessWorkerType.TASK_PLAN,
) -> TaskPlanStageBinding:
    metadata: dict[str, Any] = {
        "test_fixture": "graph-task-plan-stage",
    }
    metadata.update(metadata_overrides or {})
    step = HarnessStepSpec(
        step_id=stage_id,
        worker_type=worker_type,
        input_keys=tuple(input_keys),
        output_key="task_plan_output",
        metadata=metadata,
    )
    root = HarnessGraphSpec(
        graph_id=f"{graph_id}.graph",
        root=StepRef(stage_id),
        input_keys=tuple(input_keys),
        terminal_output_keys=("task_plan_output",),
    )
    definition = HarnessGraphDefinition(
        graph_id=root.graph_id,
        graph_version="1",
        root=root,
        activities=(step,),
        leaf_activity_bindings=(HarnessGraphLeafBinding(
            activity_id=stage_id, leaf_activity_kind="agent_loop",
            worker_ref=HarnessContractReference(HarnessContractKind.WORKER, f"{stage_id}.worker", "1"),
            activity_ref=HarnessContractReference(HarnessContractKind.ACTIVITY, f"{stage_id}.activity", "1"),
        ),) if worker_type == HarnessWorkerType.AGENT_LOOP else (),
        task_plan_stage_bindings=(
            HarnessGraphTaskPlanStageBinding(
                activity_id=stage_id,
                worker_ref=HarnessContractReference(
                    HarnessContractKind.WORKER,
                    f"{stage_id}.worker",
                    "1",
                ),
                activity_ref=HarnessContractReference(
                    HarnessContractKind.ACTIVITY,
                    f"{stage_id}.activity",
                    "1",
                ),
                policy_ref=policy_ref,
                task_plan_schema=GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
                required_output_roles=tuple(required_output_roles),
                support_refs=_DEFAULT_SUPPORT_REFS,
            ),
        ),
        committed_output_bindings=(),
        repair_bindings=(),
        terminal_side_effect_policy=HarnessTerminalSideEffectPolicy(
            policy_id="test.task-plan-terminal",
            version="1",
            handler="test.task-plan-terminal@1",
            kind="task_plan_test_terminal",
            requires_approval=False,
            retry_limit=1,
            not_required_evidence_ref=checksum_for(
                {"terminal_side_effect": "not_required"}
            ),
        ),
    )
    graph = HarnessGraphCompiler().compile(definition).graph
    return TaskPlanStageBinding(graph, stage_id)


__all__ = [
    "InMemoryTaskPlanGateArtifactWriter",
    "build_task_plan_stage_binding",
]
