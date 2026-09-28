"""Run one admitted Harness child through the real AgentRunner."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from typing import Any, Protocol, runtime_checkable

from framework.agent.loop import AgentRunner
from framework.agent.models import AgentLoopResult, AgentSpec
from framework.governance.budget import BudgetScopeType
from framework.harness.control_plane.activity_execution import (
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_authority import REF_KIND_MEMORY
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from framework.harness.subagents.agent_policy import project_child_agent_spec
from framework.harness.subagents.models import SubAgentInvocation
from framework.harness.subagents.tool_evidence import ChildToolEvidenceScope
from framework.llm.budget import GlobalBudgetTracker
from framework.llm.models import LLMClient
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import ToolRegistry
from framework.tool.runtime.evidence import ToolExecutionEvidencePort


@runtime_checkable
class AuthorizedChildInputReaderPort(Protocol):
    """Materialize values only through one already-verified child grant."""

    def read(self, grant: RefAuthoritySnapshot) -> Mapping[str, Any]: ...


class ChildAgentRunnerAdapter:
    """Project and execute an admitted child without inheriting parent state."""

    def __init__(
        self,
        *,
        registered_agent: AgentSpec,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        conversation_store: Any,
        execution_environment: Any,
        require_explicit_execution_profile: bool = True,
        worker_id: str | None = None,
        worker_version: str = "1",
    ) -> None:
        if not isinstance(registered_agent, AgentSpec):
            raise TypeError("registered_agent must be AgentSpec")
        if not isinstance(tool_registry, ToolRegistry):
            raise TypeError("tool_registry must be ToolRegistry")
        if not callable(getattr(llm_client, "complete", None)):
            raise TypeError("llm_client must implement LLMClient")
        if conversation_store is None:
            raise TypeError("conversation_store is required")
        if execution_environment is None:
            raise TypeError("execution_environment is required")
        if not isinstance(require_explicit_execution_profile, bool):
            raise TypeError("require_explicit_execution_profile must be boolean")
        resolved_worker_id = worker_id or registered_agent.agent_id
        if not isinstance(resolved_worker_id, str) or not resolved_worker_id.strip():
            raise ValueError("worker_id must be non-empty text")
        if not isinstance(worker_version, str) or not worker_version.strip():
            raise ValueError("worker_version must be non-empty text")
        self.worker_id = resolved_worker_id.strip()
        self.worker_version = worker_version.strip()
        # HarnessWorkerBinding validates this identity before a capability can
        # enter the production registry.  The trusted path calls ``invoke``;
        # ``execute`` exists only to make accidental legacy dispatch fail
        # closed instead of bypassing the execution service.
        self.worker_type = "subagent"
        self._registered_agent = deepcopy(registered_agent)
        self._llm_client = llm_client
        self._tool_registry = tool_registry
        self._conversation_store = conversation_store
        self._execution_environment = execution_environment
        self._require_explicit_execution_profile = require_explicit_execution_profile

    def execute(self, _task: Mapping[str, Any], **_kwargs: Any) -> Any:
        """Reject legacy worker dispatch outside the trusted execution service."""

        raise HarnessValidationError(
            "ChildAgentRunnerAdapter must be invoked through HarnessChildExecutionService",
            code="task_plan_child_execution_service_required",
        )

    def invoke(
        self,
        invocation: SubAgentInvocation,
        *,
        parent_task_context: HarnessGraphActivityTaskContext,
        ref_admission_service: HarnessRefAdmissionService,
        global_budget_tracker: GlobalBudgetTracker,
        tool_execution_evidence: ToolExecutionEvidencePort,
        input_reader: AuthorizedChildInputReaderPort | None = None,
    ) -> AgentLoopResult:
        """Execute a child after all Harness-owned scope checks have passed.

        The supplied tracker and evidence capability remain the authoritative
        runtime owners. This adapter does not claim to settle time, cost, tool,
        or TaskPlan reservation accounting.
        """

        if not isinstance(invocation, SubAgentInvocation):
            raise TypeError("invocation must be SubAgentInvocation")
        if not isinstance(parent_task_context, HarnessGraphActivityTaskContext):
            raise TypeError("parent_task_context must be HarnessGraphActivityTaskContext")
        if not isinstance(ref_admission_service, HarnessRefAdmissionService):
            raise TypeError("ref_admission_service must be HarnessRefAdmissionService")
        if not isinstance(global_budget_tracker, GlobalBudgetTracker):
            raise TypeError("global_budget_tracker must be GlobalBudgetTracker")
        if not isinstance(tool_execution_evidence, ToolExecutionEvidencePort):
            raise TypeError("tool_execution_evidence must implement ToolExecutionEvidencePort")
        if input_reader is not None and not isinstance(
            input_reader, AuthorizedChildInputReaderPort
        ):
            raise TypeError("input_reader must implement AuthorizedChildInputReaderPort")

        execution_identity = RefAuthoritySnapshot.execution_for_attempt(
            invocation.attempt_identity
        )
        self._require_parent_context(parent_task_context, execution_identity)
        grant = self._require_child_grant(
            invocation,
            ref_admission_service=ref_admission_service,
            execution_identity=execution_identity,
        )
        self._require_budget_tracker(
            global_budget_tracker,
            execution_identity,
            invocation.attempt_identity.identity_checksum,
        )
        self._require_evidence_scope(
            tool_execution_evidence,
            invocation=invocation,
            execution_identity=execution_identity,
            grant=grant,
        )
        objective = self._accepted_objective(invocation, grant)
        resolved_inputs = self._read_inputs(input_reader, grant)
        child_agent = project_child_agent_spec(self._registered_agent, invocation)
        # The generic child protocol is an action protocol (tool calls and a
        # final action). The admitted SubAgent output schema remains enforced
        # by the deterministic output gate after execution; it must not become
        # a provider-managed terminal contract here.
        required_outputs = invocation.subagent_spec.output_schema.get("required", ())
        output_key = (
            required_outputs[0]
            if isinstance(required_outputs, (list, tuple)) and required_outputs
            else child_agent.output_key
        )
        child_agent = replace(child_agent, output_schema=None, output_key=output_key)
        memory_recall = ref_admission_service.memory_recall(
            grant,
            execution_identity=execution_identity,
            attempt_identity=invocation.attempt_identity,
        )

        # Runner owns mutable loop state, so every physical child attempt gets a
        # fresh instance. Shared registries and stores remain injected resources.
        runner = AgentRunner(
            llm_client=self._llm_client,
            tool_registry=self._tool_registry,
            conversation_store=self._conversation_store,
            global_budget_tracker=global_budget_tracker,
            orchestration_port=None,
            orchestration_enabled=False,
            execution_environment=self._execution_environment,
            require_explicit_execution_profile=self._require_explicit_execution_profile,
        )
        return runner.run(
            child_agent,
            {
                "objective": objective,
                "input_refs": list(invocation.input_refs),
                **(
                    {"resolved_inputs": resolved_inputs}
                    if resolved_inputs is not None
                    else {}
                ),
            },
            conversation_id=self._conversation_id(invocation),
            run_id=execution_identity.run_id,
            graph_id=execution_identity.graph_id,
            graph_version=execution_identity.graph_version,
            graph_ref=execution_identity.graph_ref,
            graph_checksum=execution_identity.graph_checksum,
            node_id=execution_identity.node_id,
            node_instance_id=execution_identity.node_instance_id,
            graph_checkpoint_ref=parent_task_context.graph_checkpoint_ref,
            activity_id=execution_identity.activity_id,
            attempt=execution_identity.attempt,
            global_budget_tracker=global_budget_tracker,
            standalone=False,
            memory_recall=memory_recall,
            tool_execution_evidence=tool_execution_evidence,
        )

    @staticmethod
    def _require_parent_context(
        context: HarnessGraphActivityTaskContext,
        execution: GraphExecutionIdentity,
    ) -> None:
        activity = context.activity
        actual = {
            "run_id": activity.run_id,
            "graph_id": activity.graph_ref.graph_id,
            "graph_version": activity.graph_ref.identity_version,
            "graph_ref": activity.graph_ref.identity_ref.exact_ref,
            "graph_checksum": activity.graph_ref.checksum,
            "node_id": activity.node_id,
            "node_instance_id": activity.node_instance_id,
            "activity_id": activity.activity_id,
            "attempt": activity.attempt,
        }
        mismatches = tuple(
            name for name, expected in execution.to_dict().items()
            if actual[name] != expected
        )
        if mismatches:
            raise HarnessValidationError(
                "parent Graph checkpoint differs from the admitted child attempt",
                code="subagent_runner_checkpoint_mismatch",
                details={"mismatches": list(mismatches)},
            )

    @staticmethod
    def _require_child_grant(
        invocation: SubAgentInvocation,
        *,
        ref_admission_service: HarnessRefAdmissionService,
        execution_identity: GraphExecutionIdentity,
    ) -> RefAuthoritySnapshot:
        store = ref_admission_service.store
        if getattr(store, "is_durable", False) is not True:
            raise HarnessValidationError(
                "child Runner requires the canonical durable reference store",
                code="REF_SNAPSHOT_MISSING",
            )
        binding_key = RefAuthoritySnapshot.attempt_binding_key(
            invocation.attempt_identity,
            RefSnapshotPhase.CHILD_INPUT,
        )
        grant = store.find(run_id=invocation.parent_run_id, binding_key=binding_key)
        if grant is None:
            raise HarnessValidationError(
                "child input grant is not durably admitted",
                code="REF_SNAPSHOT_MISSING",
            )
        if not isinstance(grant, RefAuthoritySnapshot):
            raise HarnessValidationError(
                "child input grant is invalid",
                code="REF_SNAPSHOT_CONFLICT",
            )
        recorded = store.get(
            run_id=invocation.parent_run_id,
            snapshot_ref=grant.snapshot_ref,
        )
        if recorded != grant:
            raise HarnessValidationError(
                "child input grant differs from canonical storage",
                code="REF_SNAPSHOT_CONFLICT",
            )
        if grant.parent_snapshot_ref is None:
            raise HarnessValidationError(
                "child input grant has no admitted parent",
                code="REF_SNAPSHOT_BINDING_MISMATCH",
            )
        parent = store.get(
            run_id=invocation.parent_run_id,
            snapshot_ref=grant.parent_snapshot_ref,
        )
        grant.validate_parent(parent)
        grant.validate_dependency_sources({
            ref: store.get(run_id=invocation.parent_run_id, snapshot_ref=ref)
            for ref in {item.source_snapshot_ref for item in grant.dependency_bindings}
        })

        input_grant_refs = tuple(
            item.ref for item in grant.descriptors if item.ref_kind != REF_KIND_MEMORY
        )
        memory_grant_refs = tuple(
            item.ref for item in grant.descriptors if item.ref_kind == REF_KIND_MEMORY
        )
        envelope = invocation.context_envelope
        if (
            grant.phase is not RefSnapshotPhase.CHILD_INPUT
            or grant.execution_identity != execution_identity
            or grant.attempt_identity != invocation.attempt_identity
            or grant.stage_id != invocation.stage_id
            or grant.policy.owner_id != invocation.child_run_id
            or envelope.child_run_id != invocation.child_run_id
            or envelope.parent_run_id != invocation.parent_run_id
            or envelope.subagent_id != invocation.subagent_spec.subagent_id
            or envelope.allowed_input_refs != invocation.input_refs
            or len(input_grant_refs) != len(invocation.input_refs)
            or set(input_grant_refs) != set(invocation.input_refs)
            or len(memory_grant_refs) != len(envelope.memory_context_refs)
            or set(memory_grant_refs) != set(envelope.memory_context_refs)
        ):
            raise HarnessValidationError(
                "child input grant differs from the admitted invocation",
                code="REF_SNAPSHOT_BINDING_MISMATCH",
            )
        return grant

    @staticmethod
    def _require_budget_tracker(
        tracker: GlobalBudgetTracker,
        execution: GraphExecutionIdentity,
        attempt_checksum: str,
    ) -> None:
        expected_scope_id = "subagent:" + sha256(
            attempt_checksum.encode("utf-8")
        ).hexdigest()
        if (
            tracker.execution_identity != execution
            or tracker.scope.execution_identity != execution
            or tracker.scope.run_id != execution.run_id
            or tracker.scope.scope_type is not BudgetScopeType.SUBAGENT
            or tracker.scope.scope_id != expected_scope_id
        ):
            raise HarnessValidationError(
                "child budget tracker differs from the admitted Graph execution",
                code="subagent_runner_budget_scope_mismatch",
            )

    @staticmethod
    def _require_evidence_scope(
        evidence: ToolExecutionEvidencePort,
        *,
        invocation: SubAgentInvocation,
        execution_identity: GraphExecutionIdentity,
        grant: RefAuthoritySnapshot,
    ) -> None:
        scope = getattr(evidence, "scope", None)
        if (
            getattr(evidence, "is_durable", False) is not True
            or not isinstance(scope, ChildToolEvidenceScope)
            or scope.parent_graph_identity != execution_identity
            or scope.stage_id != invocation.stage_id
            or scope.plan_id != invocation.attempt_identity.plan_id
            or scope.plan_version != invocation.attempt_identity.plan_version
            or scope.task_id != invocation.task_id
            or scope.task_instance_id != invocation.task_instance_id
            or scope.task_attempt != invocation.attempt
            or scope.input_grant_ref != grant.snapshot_ref
            or scope.input_grant_checksum != grant.snapshot_checksum
            or scope.policy_checksum != grant.task_policy_checksum
        ):
            raise HarnessValidationError(
                "tool evidence capability differs from the admitted child attempt",
                code="subagent_runner_evidence_scope_mismatch",
            )

    @staticmethod
    def _accepted_objective(
        invocation: SubAgentInvocation,
        grant: RefAuthoritySnapshot,
    ) -> str:
        context = invocation.context_envelope.context_pack
        objective = context.dynamic_tail.get("objective")
        declared_refs = context.dynamic_tail.get("input_refs")
        if (
            not isinstance(objective, str)
            or not objective.strip()
            or objective != objective.strip()
            or not isinstance(declared_refs, list | tuple)
            or any(
                not isinstance(ref, str)
                or not ref.strip()
                or ref != ref.strip()
                for ref in declared_refs
            )
            or len(set(declared_refs)) != len(declared_refs)
        ):
            raise HarnessValidationError(
                "child objective or input refs differ from the accepted context",
                code="subagent_runner_input_scope_mismatch",
            )
        dependency_replacements = {
            binding.logical_ref: binding.descriptor.ref
            for binding in grant.dependency_bindings
        }
        replaced_refs = tuple(
            dependency_replacements.get(ref, ref)
            for ref in declared_refs
        )
        expected_refs = (
            tuple(sorted(set(replaced_refs)))
            if grant.dependency_bindings
            else replaced_refs
        )
        if (
            expected_refs != invocation.input_refs
            or any(
                binding.logical_ref not in declared_refs
                or binding.descriptor.ref not in invocation.input_refs
                or binding.shared_descriptor
                not in grant.descriptors
                for binding in grant.dependency_bindings
            )
        ):
            raise HarnessValidationError(
                "child objective or input refs differ from the accepted context",
                code="subagent_runner_input_scope_mismatch",
            )
        return objective

    @staticmethod
    def _read_inputs(
        reader: AuthorizedChildInputReaderPort | None,
        grant: RefAuthoritySnapshot,
    ) -> dict[str, Any] | None:
        if reader is None:
            return None
        values = reader.read(grant)
        if not isinstance(values, Mapping):
            raise HarnessValidationError(
                "authorized child input reader returned an invalid payload",
                code="subagent_runner_input_resolution_invalid",
            )
        admitted_refs = {
            item.ref for item in grant.descriptors if item.ref_kind != REF_KIND_MEMORY
        }
        if set(values) != admitted_refs or any(
            not isinstance(ref, str) for ref in values
        ):
            raise HarnessValidationError(
                "authorized child input reader returned values outside its grant",
                code="subagent_runner_input_resolution_invalid",
            )
        return deepcopy(dict(values))

    @staticmethod
    def _conversation_id(invocation: SubAgentInvocation) -> str:
        digest = invocation.attempt_identity.identity_checksum.removeprefix("sha256:")
        return f"child-agent-{digest}"


__all__ = ["AuthorizedChildInputReaderPort", "ChildAgentRunnerAdapter"]
