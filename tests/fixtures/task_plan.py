from __future__ import annotations

from collections.abc import Mapping, Sequence
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


__all__ = ["build_task_plan_stage_binding"]
