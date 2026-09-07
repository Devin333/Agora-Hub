"""Issue reference admission grants from Harness-owned Graph input documents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from framework.harness.control_plane.activity_execution import (
    HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.activity import graph_activity_input_checksum
from framework.harness.graph.canonical import freeze_json, mapping_to_dict
from framework.harness.ref_authority import (
    REF_KIND_INPUT, RefAccessMode, RefAccessPolicy, RefDescriptor, RefScope,
)
from framework.harness.ref_snapshot import (
    RefAuthoritySnapshot, RefAuthoritySnapshotStorePort, RefSnapshotPhase,
)
from framework.harness.subagents.transcript import SubAgentAttemptIdentity
from framework.harness.task_plan.canonical import canonical_payload_checksum, stable_text_tuple
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.stage_binding import TaskPlanStageBinding
from framework.shared.graph_identity import GraphExecutionIdentity


class HarnessRefAdmissionService:
    def __init__(self, store: RefAuthoritySnapshotStorePort) -> None:
        if not isinstance(store, RefAuthoritySnapshotStorePort):
            raise TypeError("store must implement RefAuthoritySnapshotStorePort")
        self.store = store

    def admit_graph_inputs(
        self,
        task: Mapping[str, Any],
        *,
        stage_binding: TaskPlanStageBinding,
        task_policy: TaskPlanPolicy,
    ) -> RefAuthoritySnapshot:
        if not isinstance(stage_binding, TaskPlanStageBinding) or not isinstance(task_policy, TaskPlanPolicy):
            raise TypeError("reference admission requires the pinned stage binding and TaskPlan policy")
        frozen = freeze_json(task, "$.ref_admission.task")
        if not isinstance(frozen, Mapping):
            raise HarnessValidationError("Graph reference admission task must be an object", code="REF_INPUT_INVALID")
        task = mapping_to_dict(frozen)
        raw_context = task.get(HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY)
        if not isinstance(raw_context, Mapping):
            raise HarnessValidationError("Graph input admission requires its activity context", code="REF_INPUT_INVALID")
        context = HarnessGraphActivityTaskContext.from_dict(raw_context)
        activity = context.activity
        graph = activity.graph_ref
        source_task = {key: value for key, value in task.items() if key != HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY}
        if graph_activity_input_checksum(source_task) != activity.input_ref:
            raise HarnessValidationError("Graph inputs differ from the admitted input document", code="REF_INPUT_CHECKSUM_MISMATCH")
        if (
            task_policy.exact_ref != stage_binding.policy_ref
            or graph.identity_ref.exact_ref != stage_binding.graph.graph_ref.exact_ref
            or graph.checksum != stage_binding.graph_checksum
            or activity.node_id != stage_binding.node_id
            or activity.worker_ref.exact_ref != stage_binding.worker_ref
            or activity.activity_ref.exact_ref != stage_binding.activity_ref
            or activity.step_ref.exact_ref != stage_binding.step_ref
            or source_task.get("run_id") != activity.run_id
            or source_task.get("step_id") != stage_binding.stage_id
        ):
            raise HarnessValidationError("Graph input authority is outside its pinned stage", code="REF_POLICY_SCOPE_MISMATCH")
        inputs = source_task.get("inputs")
        if not isinstance(inputs, Mapping) or any(value is None for value in inputs.values()):
            raise HarnessValidationError("Graph input references are unavailable", code="REF_INPUT_INVALID")
        if not set(inputs).issubset(task_policy.allowed_input_refs):
            raise HarnessValidationError("Graph input references exceed the pinned policy", code="REF_UNAUTHORIZED")
        execution = GraphExecutionIdentity(
            run_id=activity.run_id, graph_id=graph.graph_id,
            graph_version=graph.identity_version, graph_ref=graph.identity_ref.exact_ref,
            graph_checksum=graph.checksum, node_id=activity.node_id,
            node_instance_id=activity.node_instance_id,
            activity_id=activity.activity_id, attempt=activity.attempt,
        )
        descriptors = tuple(
            RefDescriptor(
                ref=name, run_id=activity.run_id, stage_id=stage_binding.stage_id,
                tenant_id=activity.tenant_scope_ref, owner_id=activity.identity_scope_ref,
                access_mode=RefAccessMode.READ_ONLY, artifact_type="graph_input",
                source_checksum=canonical_payload_checksum({"name": name, "value": inputs[name]}),
                ref_kind=REF_KIND_INPUT, scope=RefScope.SHARED_READ_ONLY,
            )
            for name in sorted(inputs)
        )
        policy = RefAccessPolicy(
            policy_id="graph-input:" + stage_binding.stage_id, version="1",
            run_id=activity.run_id, stage_id=stage_binding.stage_id,
            tenant_id=activity.tenant_scope_ref, owner_id=activity.identity_scope_ref,
            allowed_refs=tuple(item.ref for item in descriptors),
            allowed_artifact_types=("graph_input",), allowed_ref_kinds=(REF_KIND_INPUT,),
            pinned_checksums={item.ref: item.source_checksum for item in descriptors},
        )
        snapshot = RefAuthoritySnapshot(
            execution_identity=execution, stage_id=stage_binding.stage_id,
            stage_binding_checksum=stage_binding.binding_checksum,
            task_policy_checksum=task_policy.policy_checksum,
            source_checksum=activity.input_ref, policy=policy, descriptors=descriptors,
        )
        return self._commit(snapshot)

    def admit_child_inputs(
        self,
        parent: RefAuthoritySnapshot,
        *,
        attempt_identity: SubAgentAttemptIdentity,
        input_refs: tuple[str, ...],
    ) -> RefAuthoritySnapshot:
        refs = stable_text_tuple(input_refs, "input_refs", item_kind="reference")
        available = {item.ref: item for item in parent.descriptors}
        if parent.phase is not RefSnapshotPhase.INPUT_ADMISSION or not set(refs).issubset(available):
            raise HarnessValidationError("child inputs are outside the admitted reference grant", code="REF_UNAUTHORIZED")
        descriptors = tuple(available[ref] for ref in refs)
        policy = replace(
            parent.policy, policy_id="child-input:" + attempt_identity.task_instance_id,
            owner_id=attempt_identity.child_run_id, allowed_refs=refs,
            allowed_artifact_types=tuple(sorted({item.artifact_type for item in descriptors})),
            allowed_ref_kinds=tuple(sorted({item.ref_kind for item in descriptors})),
            allowed_memory_namespaces=(), writable_refs=(),
            shared_read_only_refs=refs,
            pinned_checksums={item.ref: item.source_checksum for item in descriptors},
        )
        snapshot = RefAuthoritySnapshot(
            execution_identity=parent.execution_identity, stage_id=parent.stage_id,
            stage_binding_checksum=parent.stage_binding_checksum,
            task_policy_checksum=parent.task_policy_checksum,
            source_checksum=parent.source_checksum, policy=policy, descriptors=descriptors,
            phase=RefSnapshotPhase.CHILD_INPUT,
            parent_snapshot_ref=parent.snapshot_ref, attempt_identity=attempt_identity,
        )
        snapshot.validate_parent(parent)
        return self._commit(snapshot)

    def _commit(self, snapshot: RefAuthoritySnapshot) -> RefAuthoritySnapshot:
        ref = self.store.commit(snapshot)
        if ref != snapshot.snapshot_ref:
            raise HarnessValidationError("reference store returned a different grant", code="REF_SNAPSHOT_CONFLICT")
        recorded = self.store.get(run_id=snapshot.run_id, snapshot_ref=ref)
        if recorded != snapshot:
            raise HarnessValidationError("reference store returned different grant contents", code="REF_SNAPSHOT_CONFLICT")
        return recorded
