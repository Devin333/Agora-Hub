"""Execute and recover generic delegated tasks through one authorized owner."""

from __future__ import annotations

from framework.harness.context.models import ContextEnvelope
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.policy import HarnessBudget, HarnessBudgetSnapshot
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_results import HarnessResultRefAuthority
from framework.harness.subagents.gates import FakeSubAgentGateSuite
from framework.harness.subagents.models import SubAgentInvocation, SubAgentResult
from framework.harness.subagents.runtime import SubAgentRuntime
from framework.harness.subagents.execution import (
    AdmittedChildExecution,
    HarnessChildExecutionService,
)
from framework.harness.subagents.agent_runner import ChildAgentRunnerAdapter
from framework.harness.task_plan.capability import (
    ResolvedCapabilityBinding, ResolvedSubAgentTaskAdapter, task_plan_context_identities,
)
from framework.harness.task_plan.durable_store import DurableTaskPlanStore
from framework.harness.task_plan.dependency_refs import AcceptedDependencyResultResolver
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.models import TaskInstance
from framework.harness.task_plan.parallel import DispatchGroup, DispatchWave
from framework.harness.control_plane.budget_reservation import BudgetReservation
from framework.harness.task_plan.scheduler import task_instance_for_attempt
from framework.harness.task_plan.verification import (
    TaskPlanGateRegistry, TaskPlanResultVerifier, subagent_attempt_evidence,
)
from framework.harness.workers.result import HarnessWorkerResult
from framework.shared.graph_identity import GraphExecutionIdentity


class HarnessSubAgentTaskExecutor:
    """Own the same input admission, worker and result authority on both paths.

    Context is rebuilt only from the accepted task and physical Graph attempt.
    Business workers resolve the declared refs through their scoped data ports;
    the executor never copies a parent conversation or invents input payloads.
    """

    def __init__(
        self, *, store: DurableTaskPlanStore, runtime: SubAgentRuntime,
        ref_admission_service: HarnessRefAdmissionService,
        task_policy: TaskPlanPolicy,
        execution_service: HarnessChildExecutionService | None = None,
    ) -> None:
        if not isinstance(store, DurableTaskPlanStore):
            raise TypeError("store must be DurableTaskPlanStore")
        if not isinstance(runtime, SubAgentRuntime):
            raise TypeError("runtime must be SubAgentRuntime")
        if not isinstance(ref_admission_service, HarnessRefAdmissionService):
            raise TypeError("ref_admission_service must be HarnessRefAdmissionService")
        self._store = store
        self._runtime = runtime
        self._ref_admission_service = ref_admission_service
        if execution_service is not None and not isinstance(execution_service, HarnessChildExecutionService):
            raise TypeError("execution_service must be HarnessChildExecutionService")
        if execution_service is not None and (
            execution_service.store is not store
            or execution_service.ref_admission_service is not ref_admission_service
        ):
            raise ValueError("trusted child execution must share canonical store and admission owners")
        self._execution_service = execution_service
        authority = self._require_authority()
        self._dependency_resolver = AcceptedDependencyResultResolver(store=store, authority=authority, policy=task_policy)
        self._adapter = ResolvedSubAgentTaskAdapter(
            runtime, ref_admission_service=ref_admission_service,
            dependency_result_resolver=self._dependency_resolver,
        )

    @property
    def store(self) -> DurableTaskPlanStore:
        return self._store

    @property
    def runtime(self) -> SubAgentRuntime:
        return self._runtime

    @property
    def ref_admission_service(self) -> HarnessRefAdmissionService:
        return self._ref_admission_service

    @property
    def result_ref_authority(self) -> HarnessResultRefAuthority:
        return self._require_authority()

    def _require_authority(self) -> HarnessResultRefAuthority:
        authority = self.runtime.result_ref_authority
        if not isinstance(authority, HarnessResultRefAuthority) or not authority.is_durable:
            raise ValueError("child executor requires durable result reference authority")
        if authority.store is not self.ref_admission_service.store:
            raise ValueError("child input and result authority must share the canonical snapshot store")
        if authority.transcript_store is not self.runtime.transcript_store:
            raise ValueError("child runtime and result authority must share the transcript store")
        # The existing suite owns concrete deterministic boundary gates. A
        # caller cannot replace one of these with a permissive protocol shim.
        defaults = FakeSubAgentGateSuite()
        if type(self.runtime.gates) is not FakeSubAgentGateSuite or any(
            type(getattr(self.runtime.gates, name, None)) is not type(gate)
            for name, gate in vars(defaults).items()
        ):
            raise ValueError("child runtime requires the deterministic SubAgent gate suite")
        return authority

    def require_production_bindings(
        self, *, store: DurableTaskPlanStore, admission: HarnessRefAdmissionService,
        verifier: TaskPlanResultVerifier, bindings: tuple[ResolvedCapabilityBinding, ...],
        gate_refs: tuple[str, ...],
        task_policy: TaskPlanPolicy,
    ) -> None:
        authority = self._require_authority()
        if self._dependency_resolver.policy != task_policy:
            raise ValueError("child executor must use the pinned TaskPlan policy")
        if self.store is not store:
            raise ValueError("child executor must use the configured TaskPlan store")
        if self.ref_admission_service is not admission:
            raise ValueError("child executor must use the configured input admission service")
        if not isinstance(verifier, TaskPlanResultVerifier):
            raise TypeError("result_verifier must be TaskPlanResultVerifier")
        if verifier.result_ref_authority is not authority or verifier.transcript_store is not self.runtime.transcript_store:
            raise ValueError("child executor and result_verifier must share result authority and transcript store")
        if (
            authority.artifact_descriptors is None
            or verifier.artifact_reference_verifier is not authority.artifact_descriptors
        ):
            raise ValueError("result authority and verifier must share the canonical artifact owner")
        if not isinstance(verifier.gate_registry, TaskPlanGateRegistry) or set(gate_refs) - set(verifier.registered_gate_refs):
            raise ValueError("result_verifier requires every profile gate in its deterministic gate registry")
        for binding in bindings:
            self._require_worker(binding)

    def _require_worker(self, binding: ResolvedCapabilityBinding) -> None:
        spec = binding.subagent_spec
        if binding.registration.worker_binding.worker_type is not HarnessWorkerType.SUBAGENT or spec is None:
            raise ValueError("generic child capability must bind a SUBAGENT worker")
        if self._execution_service is not None:
            if not isinstance(binding.registration.worker_binding.implementation, ChildAgentRunnerAdapter):
                raise ValueError("production generic child requires ChildAgentRunnerAdapter")
            return
        if self.runtime.workers.get(spec.subagent_id) is not binding.registration.worker_binding.implementation:
            raise ValueError("child runtime worker differs from its pinned capability binding")

    def _invocation(
        self, binding: ResolvedCapabilityBinding, instance: TaskInstance,
        execution_identity: GraphExecutionIdentity,
    ) -> SubAgentInvocation:
        self._require_authority()
        self._require_worker(binding)
        if not isinstance(execution_identity, GraphExecutionIdentity):
            raise HarnessValidationError("child execution requires physical Graph identity", code="task_plan_execution_identity_required")
        if not isinstance(instance, TaskInstance):
            raise TypeError("instance must be TaskInstance")
        plan = self.store.plan(instance.run_id, instance.stage_id, instance.plan_version)
        if plan is None:
            raise HarnessValidationError("accepted child plan is unavailable", code="task_plan_result_identity_mismatch")
        graph_identity, task_identity = task_plan_context_identities(plan, instance, execution_identity=execution_identity)
        resolved = next(item for item in plan.tasks if item.task_id == instance.task_id)
        if instance != task_instance_for_attempt(plan, resolved.task_id, attempt=instance.attempt):
            raise HarnessValidationError("child instance differs from its accepted task", code="task_plan_result_identity_mismatch")
        if (
            binding.capability != resolved.task.worker_capability
            or binding.worker_ref != resolved.worker_ref
            or binding.worker_contract_ref != resolved.worker_contract_ref
            or binding.subagent_spec.subagent_id != resolved.subagent_id
            or binding.allowed_tools != resolved.allowed_tools
            or binding.allowed_memory_namespaces != resolved.allowed_memory_namespaces
        ):
            raise HarnessValidationError("child capability differs from its accepted boundary", code="stale_task_capability_binding")
        history = self.store.read_events(plan.run_id, plan.stage_id)
        groups = {
            event.payload["group"]["group_id"]: event.payload["group"]
            for event in history if event.event_type == "TASK_GROUP_ADMITTED"
            and (event.plan_id, event.plan_version) == (plan.plan_id, plan.version)
        }
        if not any(
            event.event_type == "TASK_ATTEMPT_SPAWN_INTENT"
            and (event.plan_id, event.plan_version) == (plan.plan_id, plan.version)
            and event.payload.get("task_instance_id") == instance.task_instance_id
            and event.payload.get("attempt") == instance.attempt
            and groups.get(event.payload.get("group_id"), {}).get("parent_graph_identity") == execution_identity.to_dict()
            for event in history
        ):
            raise HarnessValidationError("child has no admitted attempt in this Graph execution", code="task_plan_result_identity_mismatch")
        context = ContextEnvelope.for_graph(
            envelope_id=f"agent-task-plan-context:{instance.task_instance_id}:{execution_identity.activity_id}",
            graph_identity=graph_identity, task_execution_identity=task_identity,
            phase="EXECUTE", worker_id=binding.worker_ref, worker_type=HarnessWorkerType.TASK_PLAN.value,
            dynamic_tail={
                "objective": resolved.task.objective,
                "input_refs": list(resolved.task.input_refs),
                "raw_parent_messages_included": False,
            },
        )
        budget = HarnessBudgetSnapshot.from_budget(HarnessBudget(
            max_turns=instance.budget_snapshot.max_turns,
            max_replans=0, max_retries_per_step=0, max_worker_calls=1,
        ))
        return self._adapter.build_invocation(
            plan=plan, resolved_task=resolved, binding=binding, instance=instance,
            context_pack=context, budget_snapshot=budget, execution_identity=execution_identity,
        )

    def __call__(
        self, binding: ResolvedCapabilityBinding, instance: TaskInstance,
        execution_identity: GraphExecutionIdentity,
    ) -> HarnessWorkerResult:
        invocation = self._invocation(binding, instance, execution_identity)
        if self._execution_service is not None:
            child = self.runtime.invoke_trusted(
                invocation,
                admission=self._admission(binding, instance, execution_identity),
                execution_service=self._execution_service,
            )
            return self._result(child)
        return self._result(self.runtime.invoke(invocation))

    def recover(
        self, binding: ResolvedCapabilityBinding, instance: TaskInstance,
        execution_identity: GraphExecutionIdentity,
    ) -> HarnessWorkerResult | None:
        invocation = self._invocation(binding, instance, execution_identity)
        child = self.runtime.recover(invocation)
        if child is not None and self._execution_service is not None:
            index = self._execution_service.recover_evidence(
                self._admission(binding, instance, execution_identity),
                invocation,
            )
            self.runtime.verify_recovered_evidence(child, index)
        return self._result(child) if child is not None else None

    def _admission(
        self,
        binding: ResolvedCapabilityBinding,
        instance: TaskInstance,
        execution_identity: GraphExecutionIdentity,
    ) -> AdmittedChildExecution:
        plan = self.store.plan(instance.run_id, instance.stage_id, instance.plan_version)
        if plan is None:
            raise HarnessValidationError("accepted child plan is unavailable", code="task_plan_result_identity_mismatch")
        history = self.store.read_events(plan.run_id, plan.stage_id)
        admissions = [
            event for event in history
            if event.event_type == "TASK_WAVE_ADMITTED"
            and (event.plan_id, event.plan_version) == (plan.plan_id, plan.version)
            and event.payload.get("group", {}).get("parent_graph_identity") == execution_identity.to_dict()
            and any(
                item.get("idempotency_key") == instance.idempotency_key
                for item in event.payload.get("wave", {}).get("reservations", ())
            )
        ]
        intents = [
            event for event in history
            if event.event_type == "TASK_ATTEMPT_SPAWN_INTENT"
            and (event.plan_id, event.plan_version, event.task_id, event.task_instance_id, event.attempt)
            == (plan.plan_id, plan.version, instance.task_id, instance.task_instance_id, instance.attempt)
            and tuple(
                getattr(event, name)
                for name in (
                    "run_id",
                    "graph_id",
                    "graph_version",
                    "graph_ref",
                    "graph_checksum",
                )
            )
            == (
                execution_identity.run_id,
                execution_identity.graph_id,
                execution_identity.graph_version,
                execution_identity.graph_ref,
                execution_identity.graph_checksum,
            )
        ]
        if len(admissions) != 1 or len(intents) != 1:
            raise HarnessValidationError(
                "child execution requires one durable wave admission and spawn intent",
                code="task_plan_child_execution_admission_mismatch",
            )
        try:
            event, intent = admissions[0], intents[0]
            group = DispatchGroup.from_dict(event.payload["group"])
            wave = DispatchWave.from_dict(event.payload["wave"])
            reservation = next(item for item in wave.reservations if item.task_id == instance.task_id)
            budget = BudgetReservation.from_dict(intent.payload["budget_reservation"])
        except (KeyError, StopIteration, TypeError, ValueError, HarnessValidationError) as exc:
            raise HarnessValidationError(
                "child execution admission payload is invalid",
                code="task_plan_child_execution_admission_mismatch",
            ) from exc
        return AdmittedChildExecution(
            plan=plan,
            instance=instance,
            binding=binding,
            execution_identity=execution_identity,
            group=group,
            wave=wave,
            reservation=reservation,
            spawn_intent=intents[0],
            budget_reservation=budget,
        )

    @staticmethod
    def _result(child: SubAgentResult) -> HarnessWorkerResult:
        succeeded = child.status.value == "succeeded"
        used_tools = child.metadata.get("used_tools", ())
        used_memory = child.metadata.get("used_memory_namespaces", ())
        return HarnessWorkerResult(
            status="succeeded" if succeeded else "failed", output=child.output,
            artifacts=child.artifact_refs,
            diagnostics={
                "subagent_id": child.subagent_id,
                "used_tools": list(used_tools),
                "used_memory_namespaces": list(used_memory),
            },
            metrics=child.metadata.get("worker_metrics", {}),
            evidence=(subagent_attempt_evidence(child.transcript_receipt),) if child.transcript_receipt is not None else (),
            error=None if succeeded else "delegated SubAgent failed verification",
        )
