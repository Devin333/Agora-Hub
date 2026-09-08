"""Per-activity admission of immutable Research RAG memory."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from backend.research.application.ask_paper import ResearchActorScope
from backend.research.domain import research_identity_scope_ref
from backend.research.graphs import (
    build_dynamic_paper_analysis_graph_definition,
    build_paper_analysis_context_graph_identity,
    build_paper_analysis_graph_definition,
)
from framework.harness.context.models import ContextGraphIdentity
from framework.harness.control_plane.activity_execution import (
    HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph import HarnessGraphCompiler
from framework.harness.ref_admission import (
    HarnessRefAdmissionService,
    PhysicalGraphRefAdmissionBinding,
)
from framework.harness.task_plan.canonical import stable_text_tuple
from framework.memory.namespace import MemoryNamespaceStorePort, namespace_revision
from framework.memory.recall_port import ExecutionMemoryRecallPort


@runtime_checkable
class ResearchRAGMemoryAdmissionPort(Protocol):
    def admit(
        self,
        task: Mapping[str, Any],
        *,
        actor_scope: ResearchActorScope,
        graph_identity: ContextGraphIdentity,
        dynamic_task_plan: bool,
    ) -> ExecutionMemoryRecallPort:
        """Issue one read-only capability for the current physical RAG activity."""


class HarnessResearchRAGMemoryAdmission:
    """Bind trusted composition revisions to a physical Research Graph call."""

    def __init__(
        self,
        ref_admission: HarnessRefAdmissionService,
        *,
        namespace_refs: tuple[str, ...],
    ) -> None:
        if not isinstance(ref_admission, HarnessRefAdmissionService):
            raise TypeError("ref_admission must be HarnessRefAdmissionService")
        if getattr(ref_admission.store, "is_durable", False) is not True:
            raise TypeError(
                "Research RAG memory admission requires durable reference snapshots"
            )
        if (
            not isinstance(ref_admission.memory_namespaces, MemoryNamespaceStorePort)
            or getattr(ref_admission.memory_namespaces, "is_durable", False) is not True
        ):
            raise TypeError(
                "Research RAG memory admission requires durable namespace storage"
            )
        refs = stable_text_tuple(
            namespace_refs,
            "namespace_refs",
            item_kind="reference",
            allow_empty=True,
        )
        if not refs:
            raise HarnessValidationError(
                "Research RAG memory requires exact namespace revisions",
                code="REF_AUTHORITY_REQUIRED",
            )
        for ref in refs:
            namespace_revision(ref)
        self._ref_admission = ref_admission
        self._namespace_refs = refs
        self._bindings = {
            False: PhysicalGraphRefAdmissionBinding(
                HarnessGraphCompiler().compile(
                    build_paper_analysis_graph_definition()
                ).graph,
                "run_research_rag",
            ),
            True: PhysicalGraphRefAdmissionBinding(
                HarnessGraphCompiler().compile(
                    build_dynamic_paper_analysis_graph_definition()
                ).graph,
                "run_research_rag",
            ),
        }

    def admit(
        self,
        task: Mapping[str, Any],
        *,
        actor_scope: ResearchActorScope,
        graph_identity: ContextGraphIdentity,
        dynamic_task_plan: bool,
    ) -> ExecutionMemoryRecallPort:
        if not isinstance(actor_scope, ResearchActorScope):
            raise TypeError("actor_scope must be ResearchActorScope")
        if not isinstance(graph_identity, ContextGraphIdentity):
            raise TypeError("graph_identity must be ContextGraphIdentity")
        if not graph_identity.has_physical_activity:
            raise HarnessValidationError(
                "Research RAG memory requires a physical Graph execution",
                code="REF_AUTHORITY_REQUIRED",
            )
        if not actor_scope.tenant_id or not actor_scope.user_id:
            raise HarnessValidationError(
                "Research RAG memory requires authenticated tenant and owner scope",
                code="REF_UNAUTHORIZED",
            )
        raw_context = task.get(HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY)
        if not isinstance(raw_context, Mapping):
            raise HarnessValidationError(
                "Research RAG memory requires Harness activity context",
                code="REF_AUTHORITY_REQUIRED",
            )
        activity = HarnessGraphActivityTaskContext.from_dict(raw_context).activity
        execution_identity = graph_identity.to_graph_execution_identity()
        if (
            execution_identity.run_id != activity.run_id
            or execution_identity.graph_id != activity.graph_ref.graph_id
            or execution_identity.graph_version != activity.graph_ref.identity_version
            or execution_identity.graph_ref != activity.graph_ref.identity_ref.exact_ref
            or execution_identity.graph_checksum != activity.graph_ref.checksum
            or execution_identity.node_id != activity.node_id
            or execution_identity.node_instance_id != activity.node_instance_id
            or execution_identity.activity_id != activity.activity_id
            or execution_identity.attempt != activity.attempt
        ):
            raise HarnessValidationError(
                "Research RAG memory caller differs from its physical activity",
                code="REF_SNAPSHOT_BINDING_MISMATCH",
            )
        expected = build_paper_analysis_context_graph_identity(
            run_id=graph_identity.run_id,
            stage_id="run_research_rag",
            dynamic_task_plan=dynamic_task_plan,
        ).with_physical_activity(
            node_id=graph_identity.node_id or "",
            node_instance_id=graph_identity.node_instance_id or "",
            activity_id=graph_identity.activity_id or "",
            activity_attempt=graph_identity.activity_attempt or 0,
        )
        if expected != graph_identity:
            raise HarnessValidationError(
                "Research RAG memory identity conflicts with the frozen Graph",
                code="REF_POLICY_SCOPE_MISMATCH",
            )
        identity_scope_ref = research_identity_scope_ref(actor_scope.to_metadata())
        snapshot = self._ref_admission.admit_physical_graph_inputs(
            task,
            binding=self._bindings[bool(dynamic_task_plan)],
            tenant_scope_ref=identity_scope_ref,
            identity_scope_ref=identity_scope_ref,
            tenant_id=actor_scope.tenant_id,
            owner_id=actor_scope.user_id,
            memory_namespace=actor_scope.memory_namespace,
            memory_namespace_refs=self._namespace_refs,
        )
        if snapshot.execution_identity != execution_identity:
            raise HarnessValidationError(
                "Research RAG memory grant differs from its physical execution",
                code="REF_SNAPSHOT_BINDING_MISMATCH",
            )
        return self._ref_admission.memory_recall(
            snapshot,
            execution_identity=execution_identity,
        )


__all__ = [
    "HarnessResearchRAGMemoryAdmission",
    "ResearchRAGMemoryAdmissionPort",
]
