"""Issue reference admission grants from Harness-owned Graph input documents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from framework.harness.control_plane.activity_execution import (
    HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.activity import graph_activity_input_checksum
from framework.harness.graph.canonical import freeze_json, mapping_to_dict
from framework.harness.ref_authority import (
    REF_KIND_INPUT,
    REF_KIND_MEMORY,
    RefAccessMode,
    RefAccessPolicy,
    RefAuthority,
    RefDescriptor,
    RefScope,
)
from framework.harness.ref_snapshot import (
    RefAuthoritySnapshot,
    RefAuthoritySnapshotStorePort,
    RefSnapshotPhase,
    SnapshotRefResolutionPort,
)
from framework.harness.subagents.transcript import SubAgentAttemptIdentity
from framework.harness.task_plan.canonical import canonical_payload_checksum, stable_text_tuple
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.stage_binding import TaskPlanStageBinding
from framework.memory.namespace import MemoryNamespaceDescriptor, MemoryNamespaceDescriptorPort, namespace_revision
from framework.shared.graph_identity import GraphExecutionIdentity

if TYPE_CHECKING:
    from framework.harness.ref_memory import HarnessMemoryNamespaceReader, HarnessMemoryRecallRuntime


class HarnessRefAdmissionService:
    def __init__(
        self,
        store: RefAuthoritySnapshotStorePort,
        *,
        memory_namespaces: MemoryNamespaceDescriptorPort | None = None,
        memory_namespace_refs: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(store, RefAuthoritySnapshotStorePort):
            raise TypeError("store must implement RefAuthoritySnapshotStorePort")
        self.store = store
        self.memory_namespace_refs = stable_text_tuple(
            memory_namespace_refs,
            "memory_namespace_refs",
            item_kind="reference",
        )
        for ref in self.memory_namespace_refs:
            namespace_revision(ref)
        if self.memory_namespace_refs and not isinstance(memory_namespaces, MemoryNamespaceDescriptorPort):
            raise TypeError("memory namespace refs require a metadata-only namespace port")
        if memory_namespaces is not None and (
            not isinstance(memory_namespaces, MemoryNamespaceDescriptorPort)
            or getattr(memory_namespaces, "is_durable", False) is not True
        ):
            raise TypeError("memory namespace authority requires durable metadata")
        self.memory_namespaces = memory_namespaces

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
        if stage_binding.is_agent_delegation:
            if not isinstance(inputs, Mapping) or set(inputs) != {
                "inputs", "conversation_id", "resume_from_cursor",
            }:
                raise HarnessValidationError("AgentLoop Graph input envelope is invalid", code="REF_INPUT_INVALID")
            inputs = inputs.get("inputs")
            if not isinstance(inputs, Mapping):
                raise HarnessValidationError("AgentLoop inputs must be an object", code="REF_INPUT_INVALID")
            # The parent may see private prompt inputs. Only the explicitly
            # configured input names enter the delegation grant.
            inputs = {
                name: value for name, value in inputs.items()
                if name in task_policy.allowed_input_refs
            }
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
        recorded = self.store.find(
            run_id=activity.run_id,
            binding_key=RefAuthoritySnapshot.admission_binding_key(
                execution,
                stage_binding.stage_id,
                stage_binding.binding_checksum,
            ),
        )
        if recorded is not None:
            memory_descriptors = tuple(
                item for item in recorded.descriptors if item.ref_kind == REF_KIND_MEMORY
            )
            if tuple(sorted(item.ref for item in memory_descriptors)) != self.memory_namespace_refs:
                raise HarnessValidationError("memory bindings differ from the admitted grant", code="REF_SNAPSHOT_CONFLICT")
        else:
            memory_descriptors = self._resolve_memory_descriptors(
                run_id=activity.run_id, stage_id=stage_binding.stage_id,
            )
        if any(item.namespace not in task_policy.allowed_memory_namespaces for item in memory_descriptors):
            raise HarnessValidationError("memory namespace exceeds TaskPlan policy", code="REF_UNAUTHORIZED")
        descriptors += memory_descriptors
        policy = RefAccessPolicy(
            policy_id="graph-input:" + stage_binding.stage_id, version="1",
            run_id=activity.run_id, stage_id=stage_binding.stage_id,
            tenant_id=activity.tenant_scope_ref, owner_id=activity.identity_scope_ref,
            allowed_refs=tuple(item.ref for item in descriptors),
            allowed_artifact_types=tuple(sorted({item.artifact_type for item in descriptors})),
            allowed_ref_kinds=tuple(sorted({item.ref_kind for item in descriptors})),
            allowed_memory_namespaces=tuple(item.namespace for item in memory_descriptors),
            pinned_checksums={item.ref: item.source_checksum for item in descriptors},
        )
        snapshot = RefAuthoritySnapshot(
            execution_identity=execution, stage_id=stage_binding.stage_id,
            stage_binding_checksum=stage_binding.binding_checksum,
            task_policy_checksum=task_policy.policy_checksum,
            source_checksum=activity.input_ref, policy=policy, descriptors=descriptors,
        )
        return self._commit(snapshot)

    def _resolve_memory_descriptors(
        self,
        *,
        run_id: str,
        stage_id: str,
    ) -> tuple[RefDescriptor, ...]:
        result = []
        for ref in self.memory_namespace_refs:
            metadata = self.memory_namespaces.describe(ref)
            if (
                not isinstance(metadata, MemoryNamespaceDescriptor)
                or metadata.exact_ref != ref
            ):
                raise HarnessValidationError(
                    "trusted memory namespace metadata is unavailable",
                    code="REF_UNRESOLVED",
                )
            result.append(RefDescriptor.memory(
                namespace=metadata.namespace, ref=ref, run_id=run_id, stage_id=stage_id,
                tenant_id=metadata.tenant_id, owner_id=metadata.owner_id,
                source_checksum=metadata.source_checksum,
                scope=RefScope.SHARED_READ_ONLY if metadata.shared_read_only else RefScope.PRIVATE,
            ))
        return tuple(result)

    def memory_reader(
        self,
        snapshot: RefAuthoritySnapshot,
        *,
        execution_identity: GraphExecutionIdentity,
        attempt_identity: SubAgentAttemptIdentity | None = None,
    ) -> HarnessMemoryNamespaceReader:
        from framework.harness.ref_memory import HarnessMemoryNamespaceReader

        return HarnessMemoryNamespaceReader(
            snapshot=snapshot,
            snapshot_store=self.store,
            namespace_store=self.memory_namespaces,
            execution_identity=execution_identity,
            attempt_identity=attempt_identity,
        )

    def memory_recall(
        self,
        snapshot: RefAuthoritySnapshot,
        *,
        execution_identity: GraphExecutionIdentity,
        attempt_identity: SubAgentAttemptIdentity | None = None,
    ) -> HarnessMemoryRecallRuntime:
        from framework.harness.ref_memory import HarnessMemoryRecallRuntime

        return HarnessMemoryRecallRuntime(self.memory_reader(
            snapshot, execution_identity=execution_identity, attempt_identity=attempt_identity,
        ))

    def admit_child_inputs(
        self,
        parent: RefAuthoritySnapshot,
        *,
        attempt_identity: SubAgentAttemptIdentity,
        input_refs: tuple[str, ...],
        memory_namespaces: tuple[str, ...] = (),
    ) -> RefAuthoritySnapshot:
        refs = stable_text_tuple(input_refs, "input_refs", item_kind="reference")
        available = {item.ref: item for item in parent.descriptors}
        if parent.phase is not RefSnapshotPhase.INPUT_ADMISSION or not set(refs).issubset(available):
            raise HarnessValidationError("child inputs are outside the admitted reference grant", code="REF_UNAUTHORIZED")
        if any(available[ref].ref_kind != REF_KIND_INPUT for ref in refs):
            raise HarnessValidationError("child inputs must be admitted input references", code="REF_UNAUTHORIZED")
        namespaces = stable_text_tuple(
            memory_namespaces,
            "memory_namespaces",
            item_kind="reference",
        )
        memory_descriptors = tuple(RefAuthority().authorize_memory_namespace_ref(
            namespace, parent.policy, resolver=SnapshotRefResolutionPort(parent),
        ) for namespace in namespaces)
        descriptors = tuple(available[ref] for ref in refs) + memory_descriptors
        refs = tuple(item.ref for item in descriptors)
        policy = replace(
            parent.policy, policy_id="child-input:" + attempt_identity.task_instance_id,
            owner_id=attempt_identity.child_run_id, allowed_refs=refs,
            allowed_artifact_types=tuple(sorted({item.artifact_type for item in descriptors})),
            allowed_ref_kinds=tuple(sorted({item.ref_kind for item in descriptors})),
            allowed_memory_namespaces=namespaces, writable_refs=(),
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
