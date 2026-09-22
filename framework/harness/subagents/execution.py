"""Harness-owned execution boundary for one durably admitted child."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from typing import Protocol, runtime_checkable

from framework.agent.models import AgentLoopResult
from framework.events import TransactionalStateReaderPort, TransactionalStateRuntimePort
from framework.events.canonical import checksum_for
from framework.governance.budget import BudgetScopeType
from framework.harness.control_plane.activity_execution import HarnessGraphActivityTaskContext
from framework.harness.control_plane.budget_reservation import BudgetReservation
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from framework.harness.subagents.agent_runner import (
    AuthorizedChildInputReaderPort,
    ChildAgentRunnerAdapter,
)
from framework.harness.subagents.models import SubAgentInvocation
from framework.harness.subagents.transcript import SubAgentAttemptIdentity
from framework.harness.subagents.tool_evidence import (
    CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
    ChildToolEvidenceCompleteness,
    ChildToolEvidenceIndex,
    ChildToolEvidenceLimits,
    ChildToolEvidenceScope,
    DurableChildToolEvidenceOwner,
    MetadataFirstChildToolEvidenceAuthorization,
)
from framework.harness.task_plan.capability import ResolvedCapabilityBinding
from framework.harness.task_plan.durable_store import DurableTaskPlanStore, TaskPlanArtifactStorePort
from framework.harness.task_plan.models import TaskInstance, ValidatedTaskPlan
from framework.harness.task_plan.parallel import (
    DispatchGroup,
    DispatchWave,
    TaskReservation,
    spawn_operation_key,
)
from framework.harness.task_plan.store import TaskPlanEvent
from framework.llm.budget import GlobalBudgetTracker
from framework.shared.graph_identity import GraphExecutionIdentity


TRUSTED_CHILD_EXECUTION_SCHEMA = "newsroom.trusted-child-execution/v1"
TRUSTED_CHILD_EXECUTION_RETAINED_SCHEMA = (
    "newsroom.trusted-child-execution-retained/v1"
)


@dataclass(frozen=True, slots=True)
class AdmittedChildExecution:
    """Typed, cross-checked facts which authorize a physical child attempt."""

    plan: ValidatedTaskPlan
    instance: TaskInstance
    binding: ResolvedCapabilityBinding
    execution_identity: GraphExecutionIdentity
    group: DispatchGroup
    wave: DispatchWave
    reservation: TaskReservation
    spawn_intent: TaskPlanEvent
    budget_reservation: BudgetReservation

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        if not isinstance(self.instance, TaskInstance):
            raise TypeError("instance must be TaskInstance")
        if not isinstance(self.binding, ResolvedCapabilityBinding):
            raise TypeError("binding must be ResolvedCapabilityBinding")
        if not isinstance(self.execution_identity, GraphExecutionIdentity):
            raise TypeError("execution_identity must be GraphExecutionIdentity")
        if not isinstance(self.group, DispatchGroup) or not isinstance(self.wave, DispatchWave):
            raise TypeError("group and wave must be canonical dispatch values")
        if not isinstance(self.reservation, TaskReservation):
            raise TypeError("reservation must be TaskReservation")
        if not isinstance(self.spawn_intent, TaskPlanEvent):
            raise TypeError("spawn_intent must be TaskPlanEvent")
        if not isinstance(self.budget_reservation, BudgetReservation):
            raise TypeError("budget_reservation must be BudgetReservation")
        self._validate()

    @property
    def operation_key(self) -> str:
        return self.budget_reservation.reservation_key

    def _validate(self) -> None:
        plan, instance = self.plan, self.instance
        task = next((item for item in plan.tasks if item.task_id == instance.task_id), None)
        expected_operation = spawn_operation_key(
            self.group.group_id,
            self.wave.wave_id,
            instance.task_instance_id,
            instance.attempt,
        )
        identity_fields = (
            self.execution_identity.run_id,
            self.execution_identity.graph_id,
            self.execution_identity.graph_version,
            self.execution_identity.graph_ref,
            self.execution_identity.graph_checksum,
        )
        if (
            not instance.matches_plan_identity(plan)
            or task is None
            or task.task_definition_checksum != instance.task_definition_checksum
            or self.group.parent_graph_identity != self.execution_identity
            or (self.group.run_id, self.group.stage_id, self.group.plan_id, self.group.plan_version, self.group.plan_checksum)
            != (plan.run_id, plan.stage_id, plan.plan_id, plan.version, plan.plan_checksum)
            or self.group.policy_ref != plan.policy_ref
            or self.group.policy_checksum != plan.policy_checksum
            or self.wave.group_id != self.group.group_id
            or self.wave.execution_mode != "SUPERVISED"
            or instance.task_id not in self.wave.task_ids
            or self.reservation not in self.wave.reservations
            or self.reservation.task_id != instance.task_id
            or self.reservation.idempotency_key != instance.idempotency_key
            or dict(self.reservation.budget) != instance.budget_snapshot.to_dict()
            or self.binding.capability != task.task.worker_capability
            or self.binding.worker_ref != task.worker_ref
            or self.binding.worker_contract_ref != task.worker_contract_ref
            or self.binding.allowed_tools != task.allowed_tools
            or self.binding.allowed_memory_namespaces != task.allowed_memory_namespaces
            or self.spawn_intent.event_type != "TASK_ATTEMPT_SPAWN_INTENT"
            or (self.spawn_intent.run_id, self.spawn_intent.stage_id, self.spawn_intent.plan_id, self.spawn_intent.plan_version)
            != (plan.run_id, plan.stage_id, plan.plan_id, plan.version)
            or (self.spawn_intent.task_id, self.spawn_intent.task_instance_id, self.spawn_intent.attempt)
            != (instance.task_id, instance.task_instance_id, instance.attempt)
            or tuple(getattr(self.spawn_intent, name) for name in ("run_id", "graph_id", "graph_version", "graph_ref", "graph_checksum"))
            != identity_fields
            or self.spawn_intent.payload.get("group_id") != self.group.group_id
            or self.spawn_intent.payload.get("wave_id") != self.wave.wave_id
            or self.spawn_intent.payload.get("operation_key") != expected_operation
            or self.spawn_intent.payload.get("idempotency_key") != expected_operation
            or self.budget_reservation.reservation_key != expected_operation
            or self.budget_reservation.owner_scope
            != f"{plan.run_id}:{plan.stage_id}:{self.group.group_id}"
            or dict(self.budget_reservation.attempt_allocation)
            != instance.budget_snapshot.to_dict()
        ):
            raise HarnessValidationError(
                "child execution admission facts do not identify one accepted attempt",
                code="task_plan_child_execution_admission_mismatch",
            )


@runtime_checkable
class ChildBudgetTrackerProviderPort(Protocol):
    """Issue the canonical-ledger tracker for exactly one admitted child."""

    def tracker_for(self, admission: AdmittedChildExecution) -> GlobalBudgetTracker: ...


@runtime_checkable
class ChildExecutionLeasePort(Protocol):
    """Fail closed unless this admitted operation still owns live execution."""

    def require_active(self, admission: AdmittedChildExecution) -> None: ...


@runtime_checkable
class ChildExecutionControlPort(Protocol):
    """Invocation-local live control, bound by the supervised worker callback."""

    def raise_if_active(self) -> None: ...

    @property
    def attempt_context(self) -> object: ...


@runtime_checkable
class ChildToolEvidenceLimitsProviderPort(Protocol):
    """Project the pinned ToolRuntime retry policy into bounded evidence limits."""

    def limits_for(self, admission: AdmittedChildExecution) -> ChildToolEvidenceLimits: ...


@runtime_checkable
class ChildExecutionUsageMeterPort(Protocol):
    """Read the canonical child ledger after every external operation settles."""

    def measure(
        self,
        admission: AdmittedChildExecution,
        *,
        runner_result: AgentLoopResult,
        evidence_index: ChildToolEvidenceIndex,
    ) -> "TrustedChildUsage": ...

    def memory_namespaces_for(
        self,
        admission: AdmittedChildExecution,
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class TrustedChildUsage:
    turns: int
    llm_calls: int
    tool_calls: int
    logical_tool_calls: int
    memory_ops: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cached_input_tokens: int
    tokens: int
    time_ms: int
    cost_microusd: int | None
    schema_version: str = "newsroom.trusted-child-usage/v1"
    usage_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "turns", "llm_calls", "tool_calls", "logical_tool_calls",
            "memory_ops", "input_tokens", "output_tokens", "reasoning_tokens",
            "cached_input_tokens", "tokens", "time_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HarnessValidationError(
                    "trusted child usage must contain non-negative integers",
                    code="subagent_trusted_usage_invalid",
                )
        if self.cost_microusd is not None and (
            isinstance(self.cost_microusd, bool)
            or not isinstance(self.cost_microusd, int)
            or self.cost_microusd < 0
        ):
            raise HarnessValidationError(
                "trusted child cost must be a non-negative integer or absent",
                code="subagent_trusted_usage_invalid",
            )
        if (
            self.tokens
            != self.input_tokens + self.output_tokens + self.reasoning_tokens
            or self.cached_input_tokens > self.input_tokens
        ):
            raise HarnessValidationError(
                "trusted child usage dimensions are internally inconsistent",
                code="subagent_trusted_usage_invalid",
            )
        if self.schema_version != "newsroom.trusted-child-usage/v1":
            raise HarnessValidationError(
                "trusted child usage schema is unsupported",
                code="subagent_trusted_usage_invalid",
            )
        object.__setattr__(self, "usage_checksum", checksum_for(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, int | str | None]:
        return {
            "schema_version": self.schema_version,
            "turns": self.turns,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "logical_tool_calls": self.logical_tool_calls,
            "memory_ops": self.memory_ops,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "tokens": self.tokens,
            "time_ms": self.time_ms,
            "cost_microusd": self.cost_microusd,
        }

    def to_dict(self) -> dict[str, int | str | None]:
        return {**self.checksum_projection(), "usage_checksum": self.usage_checksum}

    def budget_usage(self) -> dict[str, int]:
        usage = {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "memory_ops": self.memory_ops,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "time_ms": self.time_ms,
        }
        if self.cost_microusd is not None:
            usage["cost_microusd"] = self.cost_microusd
        return usage

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "TrustedChildUsage":
        expected = {
            "schema_version", "turns", "llm_calls", "tool_calls",
            "logical_tool_calls", "memory_ops", "input_tokens", "output_tokens",
            "reasoning_tokens", "cached_input_tokens", "tokens", "time_ms",
            "cost_microusd", "usage_checksum",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise HarnessValidationError("trusted usage fields are invalid", code="subagent_trusted_usage_invalid")
        supplied = value["usage_checksum"]
        result = cls(
            schema_version=value["schema_version"],  # type: ignore[arg-type]
            turns=value["turns"],  # type: ignore[arg-type]
            llm_calls=value["llm_calls"],  # type: ignore[arg-type]
            tool_calls=value["tool_calls"],  # type: ignore[arg-type]
            logical_tool_calls=value["logical_tool_calls"],  # type: ignore[arg-type]
            memory_ops=value["memory_ops"],  # type: ignore[arg-type]
            input_tokens=value["input_tokens"],  # type: ignore[arg-type]
            output_tokens=value["output_tokens"],  # type: ignore[arg-type]
            reasoning_tokens=value["reasoning_tokens"],  # type: ignore[arg-type]
            cached_input_tokens=value["cached_input_tokens"],  # type: ignore[arg-type]
            tokens=value["tokens"],  # type: ignore[arg-type]
            time_ms=value["time_ms"],  # type: ignore[arg-type]
            cost_microusd=value["cost_microusd"],  # type: ignore[arg-type]
        )
        if supplied != result.usage_checksum:
            raise HarnessValidationError("trusted usage checksum mismatch", code="subagent_trusted_usage_invalid")
        return result


@dataclass(frozen=True, slots=True)
class TrustedChildExecutionOutcome:
    invocation: SubAgentInvocation
    runner_result: AgentLoopResult
    evidence_index: ChildToolEvidenceIndex
    usage: TrustedChildUsage
    requested_tools: tuple[str, ...]
    memory_namespaces: tuple[str, ...]
    schema_version: str = TRUSTED_CHILD_EXECUTION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.invocation, SubAgentInvocation):
            raise TypeError("invocation must be SubAgentInvocation")
        if not isinstance(self.runner_result, AgentLoopResult):
            raise TypeError("runner_result must be AgentLoopResult")
        if not isinstance(self.evidence_index, ChildToolEvidenceIndex):
            raise TypeError("evidence_index must be ChildToolEvidenceIndex")
        if not isinstance(self.usage, TrustedChildUsage):
            raise TypeError("usage must be TrustedChildUsage")
        if self.schema_version != TRUSTED_CHILD_EXECUTION_SCHEMA:
            raise HarnessValidationError("trusted child execution schema is unsupported", code="subagent_trusted_execution_invalid")
        tools = self.requested_tools
        namespaces = self.memory_namespaces
        if (
            not isinstance(tools, tuple)
            or not isinstance(namespaces, tuple)
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                or len(item) > 256
                for item in (*tools, *namespaces)
            )
        ):
            raise HarnessValidationError(
                "trusted child tool or memory identities are invalid",
                code="subagent_trusted_execution_invalid",
            )
        if len(tools) != self.evidence_index.registered_logical_calls:
            raise HarnessValidationError("runner tool records differ from durable evidence", code="subagent_tool_evidence_mismatch")
        if self.usage.logical_tool_calls != len(tools) or self.usage.tool_calls != self.evidence_index.admitted_physical_attempts:
            raise HarnessValidationError("trusted usage differs from durable evidence", code="subagent_tool_evidence_mismatch")
        if self.usage.memory_ops != len(namespaces):
            raise HarnessValidationError(
                "trusted memory usage differs from its measured operations",
                code="subagent_trusted_usage_invalid",
            )

    def transcript_event(self) -> dict[str, object]:
        projection: dict[str, object] = {
            "schema_version": self.schema_version,
            "attempt_identity_checksum": self.invocation.attempt_identity.identity_checksum,
            "usage": self.usage.to_dict(),
            "tool_evidence": _index_projection(self.evidence_index),
            "used_tools": list(self.requested_tools),
            "used_memory_namespaces": list(self.memory_namespaces),
        }
        return {**projection, "execution_checksum": checksum_for(projection)}


class ChildExecutionEvidenceRetainedError(HarnessValidationError):
    """A live runner failed after durable evidence opened; never retry blindly."""

    def __init__(self, evidence_index: ChildToolEvidenceIndex, cause: BaseException) -> None:
        super().__init__(
            "child execution failed after durable tool evidence opened",
            code="subagent_child_execution_evidence_retained",
            details={
                "evidence_ref": evidence_index.ref,
                "evidence_checksum": evidence_index.content_checksum,
                "cause_type": type(cause).__name__,
            },
        )
        self.evidence_index = evidence_index

    def transcript_event(self, identity: SubAgentInvocation) -> dict[str, object]:
        projection: dict[str, object] = {
            "schema_version": TRUSTED_CHILD_EXECUTION_RETAINED_SCHEMA,
            "attempt_identity_checksum": identity.attempt_identity.identity_checksum,
            "tool_evidence": _index_projection(self.evidence_index),
            "reason_code": self.code or "subagent_child_execution_evidence_retained",
        }
        return {**projection, "execution_checksum": checksum_for(projection)}


class HarnessChildExecutionService:
    """Run only a child whose plan, grant, reservation, and lease are authoritative."""

    def __init__(
        self,
        *,
        store: DurableTaskPlanStore,
        ref_admission_service: HarnessRefAdmissionService,
        state_runtime: TransactionalStateRuntimePort,
        state_reader: TransactionalStateReaderPort,
        artifact_store: TaskPlanArtifactStorePort,
        budget_trackers: ChildBudgetTrackerProviderPort,
        leases: ChildExecutionLeasePort | None,
        evidence_limits: ChildToolEvidenceLimitsProviderPort,
        usage_meter: ChildExecutionUsageMeterPort,
        input_reader: AuthorizedChildInputReaderPort | None = None,
    ) -> None:
        if not isinstance(store, DurableTaskPlanStore):
            raise TypeError("store must be DurableTaskPlanStore")
        if not isinstance(ref_admission_service, HarnessRefAdmissionService):
            raise TypeError("ref_admission_service must be HarnessRefAdmissionService")
        if not isinstance(state_runtime, TransactionalStateRuntimePort):
            raise TypeError("state_runtime must implement TransactionalStateRuntimePort")
        if not isinstance(state_reader, TransactionalStateReaderPort):
            raise TypeError("state_reader must implement TransactionalStateReaderPort")
        if not isinstance(artifact_store, TaskPlanArtifactStorePort):
            raise TypeError("artifact_store must implement TaskPlanArtifactStorePort")
        if not isinstance(budget_trackers, ChildBudgetTrackerProviderPort):
            raise TypeError("budget_trackers must implement ChildBudgetTrackerProviderPort")
        if leases is not None and not isinstance(leases, ChildExecutionLeasePort):
            raise TypeError("leases must implement ChildExecutionLeasePort")
        if not isinstance(evidence_limits, ChildToolEvidenceLimitsProviderPort):
            raise TypeError("evidence_limits must implement ChildToolEvidenceLimitsProviderPort")
        if not isinstance(usage_meter, ChildExecutionUsageMeterPort):
            raise TypeError("usage_meter must implement ChildExecutionUsageMeterPort")
        if input_reader is not None and not isinstance(input_reader, AuthorizedChildInputReaderPort):
            raise TypeError("input_reader must implement AuthorizedChildInputReaderPort")
        self._store = store
        self._refs = ref_admission_service
        self._state_runtime = state_runtime
        self._state_reader = state_reader
        self._artifact_store = artifact_store
        self._budget_trackers = budget_trackers
        self._leases = leases
        self._evidence_limits = evidence_limits
        self._usage_meter = usage_meter
        self._input_reader = input_reader

    @property
    def store(self) -> DurableTaskPlanStore:
        return self._store

    @property
    def ref_admission_service(self) -> HarnessRefAdmissionService:
        return self._refs

    def execute(
        self,
        admission: AdmittedChildExecution,
        invocation: SubAgentInvocation,
    ) -> TrustedChildExecutionOutcome:
        if not isinstance(admission, AdmittedChildExecution):
            raise TypeError("admission must be AdmittedChildExecution")
        if not isinstance(invocation, SubAgentInvocation):
            raise TypeError("invocation must be SubAgentInvocation")
        if RefAuthoritySnapshot.execution_for_attempt(invocation.attempt_identity) != admission.execution_identity or (
            invocation.task_instance_id,
            invocation.task_id,
            invocation.attempt,
        ) != (
            admission.instance.task_instance_id,
            admission.instance.task_id,
            admission.instance.attempt,
        ):
            raise HarnessValidationError("invocation differs from child admission", code="task_plan_child_execution_admission_mismatch")

        parent_context = self._store.load_parent_execution_context(
            admission.execution_identity,
            stage_id=admission.plan.stage_id,
            stage_binding_checksum=admission.plan.stage_binding_checksum,
        )
        grant = self._child_grant(invocation, admission)
        runner = admission.binding.registration.worker_binding.implementation
        if not isinstance(runner, ChildAgentRunnerAdapter):
            raise HarnessValidationError(
                "production generic child requires ChildAgentRunnerAdapter",
                code="task_plan_subagent_runner_adapter_required",
            )
        control = self._require_live_control(admission)
        control.raise_if_active()
        tracker = self._budget_trackers.tracker_for(admission)
        if not isinstance(tracker, GlobalBudgetTracker):
            raise HarnessValidationError("child budget tracker provider returned an invalid tracker", code="task_plan_budget_authority_missing")
        self._require_budget_tracker(admission, invocation, tracker)

        limits = self._evidence_limits.limits_for(admission)
        if not isinstance(limits, ChildToolEvidenceLimits):
            raise HarnessValidationError("child evidence limits provider returned an invalid policy", code="subagent_tool_evidence_policy_invalid")
        physical_limit = int(
            admission.budget_reservation.attempt_allocation.get(
                "max_tool_calls",
                0,
            )
        )
        if (
            limits.max_physical_attempts != physical_limit
            or limits.max_logical_calls < max(1, physical_limit)
        ):
            raise HarnessValidationError("child evidence limits differ from the admitted call boundary", code="subagent_tool_evidence_policy_invalid")
        scope = self._scope(admission, grant, limits)
        existing = self._state_reader.load_transactional_state(
            CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
            scope.child_scope_key,
        )
        if existing is not None:
            # A previous process may have opened, closed, or lost the outcome
            # after an external call.  In all three cases the same attempt
            # cannot reacquire execution ownership.
            raise HarnessValidationError(
                "child tool evidence already exists; recovery is required before execution",
                code="subagent_tool_evidence_recovery_required",
            )
        evidence = DurableChildToolEvidenceOwner(
            scope,
            state_runtime=self._state_runtime,
            state_reader=self._state_reader,
            artifact_store=self._artifact_store,
            authorization_port=MetadataFirstChildToolEvidenceAuthorization(),
        )
        evidence.open_scope()
        try:
            control.raise_if_active()
            from framework.llm.budget import bind_llm_budget_invocation

            with bind_llm_budget_invocation(
                tracker,
                execution_identity=admission.execution_identity,
                execution_guard=control.raise_if_active,
            ):
                result = runner.invoke(
                    invocation,
                    parent_task_context=parent_context,
                    ref_admission_service=self._refs,
                    global_budget_tracker=tracker,
                    tool_execution_evidence=evidence,
                    input_reader=self._input_reader,
                )
            if not isinstance(result, AgentLoopResult):
                raise HarnessValidationError("child runner returned an invalid result", code="subagent_runner_result_invalid")
            control.raise_if_active()
            index = evidence.close_scope()
        except BaseException as exc:  # durable close decides whether a retry is legal
            try:
                retained = evidence.close_scope()
            except BaseException:
                raise HarnessValidationError(
                    "child execution evidence is unresolved; live retry is prohibited",
                    code="subagent_tool_evidence_recovery_required",
                ) from exc
            raise ChildExecutionEvidenceRetainedError(retained, exc) from exc
        try:
            # close_scope performs a metadata-first read and full canonical rebuild.
            if (
                index.completeness is not ChildToolEvidenceCompleteness.COMPLETE
                or index.unresolved_attempts != 0
            ):
                raise HarnessValidationError(
                    "child tool evidence is incomplete",
                    code="subagent_tool_evidence_incomplete",
                )
            requested_tools = tuple(
                _runner_tool_name(item) for item in result.tool_calls
            )
            usage = self._usage_meter.measure(
                admission,
                runner_result=result,
                evidence_index=index,
            )
            if not isinstance(usage, TrustedChildUsage):
                raise HarnessValidationError(
                    "child usage meter returned an invalid measurement",
                    code="subagent_trusted_usage_invalid",
                )
            self._validate_usage(admission, result, index, usage)
            return TrustedChildExecutionOutcome(
                invocation,
                result,
                index,
                usage,
                requested_tools,
                self._usage_meter.memory_namespaces_for(admission),
            )
        except ChildExecutionEvidenceRetainedError:
            raise
        except BaseException as exc:
            raise ChildExecutionEvidenceRetainedError(index, exc) from exc

    def recover_evidence(
        self,
        admission: AdmittedChildExecution,
        invocation: SubAgentInvocation,
    ) -> ChildToolEvidenceIndex:
        """Offline-only evidence validation for a previously persisted transcript."""

        grant = self._child_grant(invocation, admission)
        lookup_scope = self._scope(
            admission,
            grant,
            ChildToolEvidenceLimits(
                max_logical_calls=0,
                max_physical_attempts=0,
            ),
        )
        snapshot = self._state_reader.load_transactional_state(
            CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
            lookup_scope.child_scope_key,
        )
        if snapshot is None or snapshot.payload.get("status") != "CLOSED":
            raise HarnessValidationError(
                "child tool evidence has no closed durable index",
                code="subagent_tool_evidence_recovery_required",
            )
        scope = self._scope_from_state(snapshot.payload.get("scope"))
        if scope != self._scope(admission, grant, scope.limits):
            raise HarnessValidationError(
                "recovered child evidence scope differs from admission",
                code="subagent_tool_evidence_recovery_required",
            )
        evidence = DurableChildToolEvidenceOwner(
            scope,
            state_runtime=self._state_runtime,
            state_reader=self._state_reader,
            artifact_store=self._artifact_store,
            authorization_port=MetadataFirstChildToolEvidenceAuthorization(),
        )
        index = evidence.close_scope()
        return index

    @staticmethod
    def _scope_from_state(value: object) -> ChildToolEvidenceScope:
        if not isinstance(value, Mapping):
            raise HarnessValidationError(
                "recovered child evidence has no canonical scope",
                code="subagent_tool_evidence_recovery_required",
            )
        fields = dict(value)
        try:
            supplied_scope = fields.pop("scope_checksum")
            supplied_key = fields.pop("child_scope_key")
            supplied_run = fields.pop("run_id")
            identity = GraphExecutionIdentity.from_dict(
                fields.pop("parent_graph_identity")
            )
            limits = ChildToolEvidenceLimits(**dict(fields.pop("limits")))
            scope = ChildToolEvidenceScope(
                parent_graph_identity=identity,
                limits=limits,
                **fields,
            )
        except (KeyError, TypeError, ValueError, HarnessValidationError) as exc:
            raise HarnessValidationError(
                "recovered child evidence scope is invalid",
                code="subagent_tool_evidence_recovery_required",
            ) from exc
        if (
            supplied_scope != scope.scope_checksum
            or supplied_key != scope.child_scope_key
            or supplied_run != scope.run_id
            or dict(value) != scope.to_dict()
        ):
            raise HarnessValidationError(
                "recovered child evidence scope checksum is invalid",
                code="subagent_tool_evidence_recovery_required",
            )
        return scope

    @staticmethod
    def _require_budget_tracker(
        admission: AdmittedChildExecution,
        invocation: SubAgentInvocation,
        tracker: GlobalBudgetTracker,
    ) -> None:
        allocation = admission.budget_reservation.attempt_allocation
        limits = tracker.budget_policy.limits
        expected_scope_id = "subagent:" + sha256(
            invocation.attempt_identity.identity_checksum.encode("utf-8")
        ).hexdigest()
        expected_cost = (
            Decimal(allocation["cost_limit"]) / Decimal(1_000_000)
            if "cost_limit" in allocation
            else None
        )
        if (
            tracker.execution_identity != admission.execution_identity
            or tracker.scope.execution_identity != admission.execution_identity
            or tracker.scope.run_id != admission.execution_identity.run_id
            or tracker.scope.scope_type is not BudgetScopeType.SUBAGENT
            or tracker.scope.scope_id != expected_scope_id
            or limits.llm_calls != allocation["max_turns"]
            or limits.total_tokens != allocation["token_limit"]
            or limits.output_tokens != allocation["max_output_tokens"]
            or limits.estimated_cost_usd != expected_cost
        ):
            raise HarnessValidationError(
                "child budget tracker differs from the admitted allocation",
                code="subagent_runner_budget_scope_mismatch",
            )

    def _require_live_control(
        self,
        admission: AdmittedChildExecution,
    ) -> ChildExecutionControlPort:
        """Require the supervisor-bound control instead of fabricating an attempt."""

        try:
            from framework.harness.subagents.execution_control import (
                ChildExecutionControl,
                require_current_child_execution_control,
            )
            from framework.shared.attempts import current_attempt_context
        except ImportError as exc:
            raise HarnessValidationError(
                "live child execution control is unavailable",
                code="task_plan_child_execution_control_missing",
            ) from exc
        control = require_current_child_execution_control(
            task_instance=admission.instance,
            parent_graph_identity=admission.execution_identity,
        )
        if not isinstance(control, ChildExecutionControl) or current_attempt_context() is not control.attempt_context:
            raise HarnessValidationError(
                "child execution control is not bound to the active attempt",
                code="task_plan_child_execution_control_missing",
            )
        if self._leases is not None:
            self._leases.require_active(admission)
        return control

    @staticmethod
    def _validate_usage(
        admission: AdmittedChildExecution,
        result: AgentLoopResult,
        index: ChildToolEvidenceIndex,
        usage: TrustedChildUsage,
    ) -> None:
        allocation = admission.budget_reservation.attempt_allocation
        measured = usage.budget_usage()
        required_dimensions = {
            "turns", "tool_calls", "memory_ops", "output_tokens", "tokens", "time_ms",
            *({"cost_microusd"} if "cost_limit" in allocation else set()),
        }
        if (
            set(measured) != required_dimensions
            or usage.turns != result.iterations
            or usage.llm_calls < result.metrics.llm_calls
            or usage.tool_calls != index.admitted_physical_attempts
            or usage.logical_tool_calls != index.registered_logical_calls
            or ("cost_limit" in allocation) != (usage.cost_microusd is not None)
        ):
            raise HarnessValidationError("child usage does not match authoritative execution facts", code="subagent_trusted_usage_invalid")
        if usage.tokens != usage.input_tokens + usage.output_tokens + usage.reasoning_tokens:
            raise HarnessValidationError("child token measurement is internally inconsistent", code="subagent_trusted_usage_invalid")
        limits = {
            "turns": allocation["max_turns"],
            "tool_calls": allocation["max_tool_calls"],
            "memory_ops": allocation["max_memory_ops"],
            "output_tokens": allocation["max_output_tokens"],
            "tokens": allocation["token_limit"],
            "time_ms": allocation["time_limit_ms"],
            **({"cost_microusd": allocation["cost_limit"]} if "cost_limit" in allocation else {}),
        }
        if any(measured[name] > limit for name, limit in limits.items()):
            raise HarnessValidationError("child usage exceeds its admitted allocation", code="task_plan_budget_exceeded")

    def _child_grant(
        self,
        invocation: SubAgentInvocation,
        admission: AdmittedChildExecution,
    ) -> RefAuthoritySnapshot:
        key = RefAuthoritySnapshot.attempt_binding_key(
            invocation.attempt_identity,
            RefSnapshotPhase.CHILD_INPUT,
        )
        grant = self._refs.store.find(run_id=admission.plan.run_id, binding_key=key)
        if (
            not isinstance(grant, RefAuthoritySnapshot)
            or grant.phase is not RefSnapshotPhase.CHILD_INPUT
            or grant.attempt_identity != invocation.attempt_identity
            or grant.execution_identity != admission.execution_identity
            or grant.stage_binding_checksum != admission.plan.stage_binding_checksum
            or grant.task_policy_checksum != admission.plan.policy_checksum
            or grant.policy.owner_id != invocation.child_run_id
        ):
            raise HarnessValidationError("child input grant differs from admitted execution", code="REF_SNAPSHOT_BINDING_MISMATCH")
        return grant

    @staticmethod
    def _scope(
        admission: AdmittedChildExecution,
        grant: RefAuthoritySnapshot,
        limits: ChildToolEvidenceLimits,
    ) -> ChildToolEvidenceScope:
        return ChildToolEvidenceScope(
            parent_graph_identity=admission.execution_identity,
            stage_id=admission.plan.stage_id,
            plan_id=admission.plan.plan_id,
            plan_version=admission.plan.version,
            group_id=admission.group.group_id,
            wave_id=admission.wave.wave_id,
            task_id=admission.instance.task_id,
            task_instance_id=admission.instance.task_instance_id,
            task_attempt=admission.instance.attempt,
            task_instance_checksum=admission.instance.instance_checksum,
            binding_ref=admission.binding.worker_ref,
            binding_checksum=admission.binding.binding_checksum,
            spawn_operation_key=admission.operation_key,
            owner_scope=admission.budget_reservation.owner_scope,
            tenant_id=grant.policy.tenant_id,
            input_grant_ref=grant.snapshot_ref,
            input_grant_checksum=grant.snapshot_checksum,
            policy_checksum=admission.plan.policy_checksum,
            reservation_key=admission.budget_reservation.reservation_key,
            reservation_revision=admission.budget_reservation.ledger_version,
            allocation_checksum=checksum_for(dict(admission.budget_reservation.attempt_allocation)),
            limits=limits,
        )


def _runner_tool_name(value: Mapping[str, object]) -> str:
    name = value.get("tool_name")
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > 256
    ):
        raise HarnessValidationError("runner tool record has no tool identity", code="subagent_tool_evidence_mismatch")
    return name


def _index_projection(index: ChildToolEvidenceIndex) -> dict[str, object]:
    return {
        "ref": index.ref,
        "checksum": index.checksum,
        "content_checksum": index.content_checksum,
        "byte_size": index.byte_size,
        "revision": index.revision,
        "completeness": index.completeness.value,
        "disposition": index.disposition,
        "registered_logical_calls": index.registered_logical_calls,
        "rejected_logical_calls": index.rejected_logical_calls,
        "admitted_physical_attempts": index.admitted_physical_attempts,
        "known_terminal_attempts": index.known_terminal_attempts,
        "unresolved_attempts": index.unresolved_attempts,
    }


def validate_trusted_execution_event(
    value: Mapping[str, object],
    *,
    identity: SubAgentAttemptIdentity,
    tool_call_refs: tuple[str, ...],
) -> dict[str, object]:
    """Validate the bounded transcript projection before recovery trusts it."""

    expected = {
        "schema_version",
        "attempt_identity_checksum",
        "usage",
        "tool_evidence",
        "used_tools",
        "used_memory_namespaces",
        "execution_checksum",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise HarnessValidationError("trusted execution event fields are invalid", code="subagent_trusted_execution_invalid")
    projection = {name: value[name] for name in expected - {"execution_checksum"}}
    if (
        projection["schema_version"] != TRUSTED_CHILD_EXECUTION_SCHEMA
        or projection["attempt_identity_checksum"] != identity.identity_checksum
        or value["execution_checksum"] != checksum_for(projection)
    ):
        raise HarnessValidationError("trusted execution event identity is invalid", code="subagent_trusted_execution_invalid")
    usage_value = projection["usage"]
    evidence_value = projection["tool_evidence"]
    if not isinstance(usage_value, Mapping) or not isinstance(evidence_value, Mapping):
        raise HarnessValidationError("trusted execution event payload is invalid", code="subagent_trusted_execution_invalid")
    usage = TrustedChildUsage.from_dict(usage_value)
    used_tools = _identity_sequence(projection["used_tools"], "used_tools")
    used_memory = _identity_sequence(
        projection["used_memory_namespaces"],
        "used_memory_namespaces",
    )
    if (
        len(used_tools) != usage.logical_tool_calls
        or len(used_memory) != usage.memory_ops
    ):
        raise HarnessValidationError(
            "trusted execution operation identities differ from usage",
            code="subagent_trusted_execution_invalid",
        )
    _validate_evidence_projection(
        evidence_value,
        tool_call_refs=tool_call_refs,
        expected_logical_calls=usage.logical_tool_calls,
        expected_physical_attempts=usage.tool_calls,
    )
    return dict(value)


def validate_retained_execution_event(
    value: Mapping[str, object],
    *,
    identity: SubAgentAttemptIdentity,
    tool_call_refs: tuple[str, ...],
) -> dict[str, object]:
    """Validate a halted attempt's durable evidence retention projection."""

    expected = {
        "schema_version",
        "attempt_identity_checksum",
        "tool_evidence",
        "reason_code",
        "execution_checksum",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise HarnessValidationError(
            "retained execution event fields are invalid",
            code="subagent_trusted_execution_invalid",
        )
    projection = {name: value[name] for name in expected - {"execution_checksum"}}
    if (
        projection["schema_version"] != TRUSTED_CHILD_EXECUTION_RETAINED_SCHEMA
        or projection["attempt_identity_checksum"] != identity.identity_checksum
        or projection["reason_code"]
        != "subagent_child_execution_evidence_retained"
        or value["execution_checksum"] != checksum_for(projection)
    ):
        raise HarnessValidationError(
            "retained execution event identity is invalid",
            code="subagent_trusted_execution_invalid",
        )
    evidence = projection["tool_evidence"]
    if not isinstance(evidence, Mapping):
        raise HarnessValidationError(
            "retained execution evidence is invalid",
            code="subagent_trusted_execution_invalid",
        )
    _validate_evidence_projection(
        evidence,
        tool_call_refs=tool_call_refs,
        require_complete=False,
    )
    return dict(value)


def _validate_evidence_projection(
    evidence_value: Mapping[str, object],
    *,
    tool_call_refs: tuple[str, ...],
    expected_logical_calls: int | None = None,
    expected_physical_attempts: int | None = None,
    require_complete: bool = True,
) -> None:
    evidence_fields = {
        "ref", "checksum", "content_checksum", "byte_size", "revision",
        "completeness", "disposition", "registered_logical_calls",
        "rejected_logical_calls", "admitted_physical_attempts",
        "known_terminal_attempts", "unresolved_attempts",
    }
    if set(evidence_value) != evidence_fields:
        raise HarnessValidationError("trusted tool evidence fields are invalid", code="subagent_trusted_execution_invalid")
    for name in (
        "byte_size", "revision", "registered_logical_calls", "rejected_logical_calls",
        "admitted_physical_attempts", "known_terminal_attempts", "unresolved_attempts",
    ):
        number = evidence_value[name]
        minimum = 1 if name in {"byte_size", "revision"} else 0
        if isinstance(number, bool) or not isinstance(number, int) or number < minimum:
            raise HarnessValidationError("trusted tool evidence count is invalid", code="subagent_trusted_execution_invalid")
    ref = evidence_value["ref"]
    disposition = evidence_value["disposition"]
    registered = evidence_value["registered_logical_calls"]
    rejected = evidence_value["rejected_logical_calls"]
    physical = evidence_value["admitted_physical_attempts"]
    terminal = evidence_value["known_terminal_attempts"]
    unresolved = evidence_value["unresolved_attempts"]
    if (
        not isinstance(ref, str)
        or not ref
        or ref != ref.strip()
        or len(ref) > 2048
        or tool_call_refs != (ref,)
        or disposition
        not in {
            "NO_TOOL_REQUESTS",
            "NO_TOOL_INVOCATIONS",
            "UNKNOWN",
            "TOOL_INVOCATIONS_RECORDED",
        }
        or rejected > registered
        or terminal > physical
        or unresolved > registered + physical
        or (
            disposition == "NO_TOOL_REQUESTS"
            and any((registered, rejected, physical, terminal, unresolved))
        )
        or (
            disposition == "NO_TOOL_INVOCATIONS"
            and not (
                registered > 0
                and rejected == registered
                and physical == 0
                and terminal == 0
                and unresolved == 0
            )
        )
        or evidence_value["completeness"]
        not in {
            ChildToolEvidenceCompleteness.COMPLETE.value,
            ChildToolEvidenceCompleteness.INCOMPLETE.value,
        }
        or (
            require_complete
            and (
                evidence_value["completeness"]
                != ChildToolEvidenceCompleteness.COMPLETE.value
                or evidence_value["unresolved_attempts"] != 0
            )
        )
        or (
            not require_complete
            and (
                evidence_value["completeness"]
                == ChildToolEvidenceCompleteness.COMPLETE.value
            )
            != (evidence_value["unresolved_attempts"] == 0)
        )
        or (
            expected_logical_calls is not None
            and expected_logical_calls
            != evidence_value["registered_logical_calls"]
        )
        or (
            expected_physical_attempts is not None
            and expected_physical_attempts
            != evidence_value["admitted_physical_attempts"]
        )
        or not _is_checksum(evidence_value["content_checksum"])
        or not _is_artifact_checksum(evidence_value["checksum"])
    ):
        raise HarnessValidationError("trusted tool evidence projection is invalid", code="subagent_trusted_execution_invalid")


def _is_checksum(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _identity_sequence(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise HarnessValidationError(
            f"trusted execution {field_name} must be an array",
            code="subagent_trusted_execution_invalid",
        )
    result = tuple(value)
    if any(
        not isinstance(item, str)
        or not item
        or item != item.strip()
        or len(item) > 256
        for item in result
    ):
        raise HarnessValidationError(
            f"trusted execution {field_name} is invalid",
            code="subagent_trusted_execution_invalid",
        )
    return result


def _is_artifact_checksum(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "AdmittedChildExecution",
    "ChildExecutionEvidenceRetainedError",
    "ChildBudgetTrackerProviderPort",
    "ChildExecutionLeasePort",
    "ChildExecutionControlPort",
    "ChildToolEvidenceLimitsProviderPort",
    "ChildExecutionUsageMeterPort",
    "HarnessChildExecutionService",
    "TRUSTED_CHILD_EXECUTION_RETAINED_SCHEMA",
    "TRUSTED_CHILD_EXECUTION_SCHEMA",
    "TrustedChildExecutionOutcome",
    "TrustedChildUsage",
    "validate_retained_execution_event",
    "validate_trusted_execution_event",
]
