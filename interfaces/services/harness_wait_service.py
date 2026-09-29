from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol, runtime_checkable

from framework.harness.control_plane.graph_state import HarnessGraphState
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.harness import HarnessRunResult
from framework.harness.control_plane.state import HarnessRunSpec
from framework.harness.waits.models import (
    HarnessWaitApprovalEvidenceRecord,
    HarnessWaitCancellationRecord,
    HarnessWaitScope,
    HarnessWaitSignal,
    HarnessWaitTimeoutRecord,
    HarnessWaitTimerWakeRecord,
    approval_event_ref_for,
)
from framework.harness.graph.canonical import (
    canonical_checksum,
    exact_reference,
    freeze_json,
    required_text,
)
from interfaces.models.actor import (
    MANAGE_APPROVALS_PERMISSION,
    READ_REPORTS_PERMISSION,
    WRITE_RUNS_PERMISSION,
    ActorContext,
)


_CHECKSUM_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class HarnessWaitApplicationError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        self.code = required_text(code, "code")
        super().__init__(message)


class HarnessWaitAuthorizationError(HarnessWaitApplicationError):
    pass


class HarnessWaitNotFoundError(HarnessWaitApplicationError):
    pass


class HarnessWaitRequestError(HarnessWaitApplicationError):
    pass


HarnessWaitCause = (
    HarnessWaitSignal
    | HarnessWaitApprovalEvidenceRecord
    | HarnessWaitCancellationRecord
    | HarnessWaitTimerWakeRecord
    | HarnessWaitTimeoutRecord
)


@runtime_checkable
class HarnessWaitControlPlanePort(Protocol):
    """Narrow public runtime surface used by Wait application operations."""

    def inspect_graph_wait_scope(
        self,
        run_spec: HarnessRunSpec,
        node_instance_id: str,
    ) -> HarnessWaitScope: ...

    def accept_graph_wait_cause(
        self,
        run_spec: HarnessRunSpec,
        cause: HarnessWaitCause,
        *,
        occurred_at: datetime,
    ) -> HarnessGraphState: ...

    def recover_and_run(self, run_spec: HarnessRunSpec) -> HarnessRunResult: ...

    def recover_graph(self, run_spec: HarnessRunSpec) -> HarnessGraphState: ...


@dataclass(frozen=True, slots=True)
class HarnessWaitRuntimeBinding:
    run_spec: HarnessRunSpec
    control_plane: HarnessWaitControlPlanePort

    def __post_init__(self) -> None:
        if not isinstance(self.run_spec, HarnessRunSpec):
            raise TypeError("run_spec must be HarnessRunSpec")
        if not isinstance(self.control_plane, HarnessWaitControlPlanePort):
            raise TypeError("control_plane must implement HarnessWaitControlPlanePort")


@runtime_checkable
class HarnessWaitRuntimeResolverPort(Protocol):
    def resolve(
        self,
        run_id: str,
        *,
        actor: ActorContext,
    ) -> HarnessWaitRuntimeBinding: ...


@dataclass(frozen=True, slots=True)
class HarnessWaitActorScope:
    tenant_scope_ref: str
    identity_scope_ref: str
    actor_identity_scope_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "tenant_scope_ref",
            "identity_scope_ref",
            "actor_identity_scope_ref",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _CHECKSUM_PATTERN.fullmatch(value):
                raise ValueError(f"{field_name} must be a sha256 reference")


@runtime_checkable
class HarnessWaitActorScopeResolverPort(Protocol):
    def resolve(self, actor: ActorContext) -> HarnessWaitActorScope: ...


@dataclass(frozen=True, slots=True)
class HarnessWaitApprovalDecision:
    approval_id: str
    run_id: str
    node_instance_id: str
    approval_event_ref: str
    actor_identity_scope_ref: str
    approved: bool
    graph_id: str | None = None
    graph_version: str | None = None
    graph_ref: str | None = None
    graph_checksum: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("approval_id", "run_id", "node_instance_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            object.__setattr__(
                self,
                field_name,
                value.strip(),
            )
        for field_name in ("approval_event_ref", "actor_identity_scope_ref"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _CHECKSUM_PATTERN.fullmatch(value):
                raise ValueError(f"{field_name} must be a sha256 reference")
        if not isinstance(self.approved, bool):
            raise TypeError("approved must be a boolean")
        graph_values = (
            self.graph_id,
            self.graph_version,
            self.graph_ref,
            self.graph_checksum,
        )
        if any(value is not None for value in graph_values):
            if any(value is None for value in graph_values):
                raise ValueError("approval decision must bind all Graph identity fields")
            object.__setattr__(
                self, "graph_id", required_text(self.graph_id, "graph_id")
            )
            object.__setattr__(
                self,
                "graph_version",
                required_text(self.graph_version, "graph_version"),
            )
            object.__setattr__(
                self, "graph_ref", exact_reference(self.graph_ref, "graph_ref")
            )
            object.__setattr__(
                self,
                "graph_checksum",
                _request_checksum(
                    self.graph_checksum,
                    "graph_checksum",
                    code="approval_graph_checksum_invalid",
                ),
            )


@runtime_checkable
class HarnessWaitApprovalResolverPort(Protocol):
    """Resolve durable approval evidence with retry-safe decision semantics.

    An identical retry must return the existing durable decision. Reusing an
    approval id with a conflicting decision must fail closed.
    """

    def resolve(
        self,
        approval_id: str,
        *,
        run_id: str,
        node_instance_id: str,
        actor: ActorContext,
        requested_approved: bool,
    ) -> HarnessWaitApprovalDecision: ...


@dataclass(frozen=True, slots=True)
class HarnessWaitInspectionResult:
    run_id: str
    node_instance_id: str
    wait_id: str
    kind: str
    status: str
    signal_schema_ref: str
    lifecycle: str
    outcome: str
    registered_sequence: int | None
    last_event_sequence: int
    graph_id: str | None = None
    graph_version: str | None = None
    graph_ref: str | None = None
    graph_checksum: str | None = None
    approval_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "node_instance_id": self.node_instance_id,
            "wait_id": self.wait_id,
            "kind": self.kind,
            "status": self.status,
            "signal_schema_ref": self.signal_schema_ref,
            "lifecycle": self.lifecycle,
            "outcome": self.outcome,
            "registered_sequence": self.registered_sequence,
            "last_event_sequence": self.last_event_sequence,
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "graph_ref": self.graph_ref,
            "graph_checksum": self.graph_checksum,
            "approval_id": self.approval_id,
        }


@dataclass(frozen=True, slots=True)
class HarnessWaitOperationResult:
    operation: str
    wait: HarnessWaitInspectionResult

    def to_dict(self) -> dict[str, object]:
        return {"operation": self.operation, "wait": self.wait.to_dict()}


@dataclass(frozen=True, slots=True)
class HarnessWaitInspectionListResult:
    run_id: str
    waits: tuple[HarnessWaitInspectionResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "waits": [item.to_dict() for item in self.waits],
        }


class HarnessWaitApplicationService:
    """Actor-bound application boundary for durable Harness Wait operations."""

    def __init__(
        self,
        *,
        actor: ActorContext,
        runtime_resolver: HarnessWaitRuntimeResolverPort,
        actor_scope_resolver: HarnessWaitActorScopeResolverPort,
        approval_resolver: HarnessWaitApprovalResolverPort | None = None,
        clock: Callable[[], datetime] | None = None,
        runtime_event_sink: Any | None = None,
        runtime_event_identity: Any | None = None,
    ) -> None:
        if not isinstance(actor, ActorContext):
            raise TypeError("actor must be ActorContext")
        if not isinstance(runtime_resolver, HarnessWaitRuntimeResolverPort):
            raise TypeError(
                "runtime_resolver must implement HarnessWaitRuntimeResolverPort"
            )
        if not isinstance(actor_scope_resolver, HarnessWaitActorScopeResolverPort):
            raise TypeError(
                "actor_scope_resolver must implement HarnessWaitActorScopeResolverPort"
            )
        if approval_resolver is not None and not isinstance(
            approval_resolver,
            HarnessWaitApprovalResolverPort,
        ):
            raise TypeError(
                "approval_resolver must implement HarnessWaitApprovalResolverPort"
            )
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._actor = actor
        self._runtime_resolver = runtime_resolver
        self._actor_scope_resolver = actor_scope_resolver
        self._approval_resolver = approval_resolver
        self._clock = clock or _utc_now
        self._runtime_event_sink = runtime_event_sink
        self._runtime_event_identity = runtime_event_identity

    def inspect_wait(
        self,
        run_id: str,
        node_instance_id: str,
    ) -> HarnessWaitInspectionResult:
        self._require_permission(READ_REPORTS_PERMISSION)
        binding = self._binding(run_id)
        scope, _ = self._authorized_scope(binding, node_instance_id)
        return self._inspection(binding, scope)

    def list_waits(self, run_id: str) -> HarnessWaitInspectionListResult:
        """Project unresolved Waits visible to the authenticated actor."""

        self._require_permission(READ_REPORTS_PERMISSION)
        binding = self._binding(run_id)
        state = binding.control_plane.recover_graph(binding.run_spec)
        actor_scope = self._actor_scope_resolver.resolve(self._actor)
        if not isinstance(actor_scope, HarnessWaitActorScope):
            raise HarnessWaitApplicationError(
                "actor scope resolver returned an invalid scope",
                code="wait_actor_scope_resolver_invalid",
            )
        inspections: list[HarnessWaitInspectionResult] = []
        for registration in state.wait_registrations:
            if not registration.unresolved:
                continue
            if (
                registration.tenant_scope_ref != actor_scope.tenant_scope_ref
                or registration.identity_scope_ref != actor_scope.identity_scope_ref
            ):
                continue
            scope = HarnessWaitScope(
                wait_id=registration.wait_id,
                run_id=state.run_id,
                node_instance_id=registration.node_instance_id,
                tenant_scope_ref=registration.tenant_scope_ref,
                identity_scope_ref=registration.identity_scope_ref,
                signal_schema_ref=registration.signal_schema_ref,
                correlation_ref=registration.correlation_ref,
            )
            inspections.append(self._inspection_from_state(state, scope))
        inspections.sort(key=lambda item: (item.registered_sequence or 0, item.node_instance_id))
        return HarnessWaitInspectionListResult(
            run_id=state.run_id,
            waits=tuple(inspections),
        )

    def deliver_signal(
        self,
        run_id: str,
        node_instance_id: str,
        *,
        signal_id: str,
        signal_schema_ref: str,
        correlation: Mapping[str, object],
        payload_ref: str,
    ) -> HarnessWaitOperationResult:
        self._require_permission(WRITE_RUNS_PERMISSION)
        binding = self._binding(run_id)
        scope, _ = self._authorized_scope(binding, node_instance_id)
        actual_schema_ref = _request_exact_reference(
            signal_schema_ref,
            "signal_schema_ref",
            code="wait_signal_schema_invalid",
        )
        if actual_schema_ref != scope.signal_schema_ref:
            raise HarnessWaitRequestError(
                "signal schema does not match the durable Wait",
                code="wait_signal_schema_mismatch",
            )
        if not isinstance(correlation, Mapping):
            raise HarnessWaitRequestError(
                "correlation must be an object",
                code="wait_correlation_invalid",
            )
        try:
            canonical_correlation = freeze_json(dict(correlation), "correlation")
            correlation_ref = canonical_checksum(canonical_correlation)
        except (HarnessValidationError, TypeError, ValueError) as exc:
            raise HarnessWaitRequestError(
                "correlation must contain canonical JSON values",
                code="wait_correlation_invalid",
            ) from exc
        if correlation_ref != scope.correlation_ref:
            raise HarnessWaitRequestError(
                "signal correlation does not match the durable Wait",
                code="wait_correlation_mismatch",
            )
        cause = HarnessWaitSignal(
            signal_id=_request_text(
                signal_id,
                "signal_id",
                code="wait_signal_id_invalid",
            ),
            scope=scope,
            payload_ref=_request_checksum(
                payload_ref,
                "payload_ref",
                code="wait_payload_ref_invalid",
            ),
            received_sequence=0,
        )
        return self._submit(binding, cause, operation="signal")

    def decide_approval(
        self,
        run_id: str,
        node_instance_id: str,
        *,
        approval_id: str,
        approved: bool,
    ) -> HarnessWaitOperationResult:
        self._require_permission(MANAGE_APPROVALS_PERMISSION)
        binding = self._binding(run_id)
        scope, actor_scope = self._authorized_scope(binding, node_instance_id)
        state = binding.control_plane.recover_graph(binding.run_spec)
        graph_ref = state.graph_ref
        authoritative_approval_id = _approval_id_from_node(
            state,
            scope.node_instance_id,
            run_spec=binding.run_spec,
        )
        if self._approval_resolver is None:
            raise HarnessWaitApplicationError(
                "approval evidence resolver is unavailable",
                code="wait_approval_resolver_missing",
            )
        if not isinstance(approved, bool):
            raise HarnessWaitRequestError(
                "approved must be a boolean",
                code="wait_approval_decision_invalid",
            )
        approval_id = _request_text(
            approval_id,
            "approval_id",
            code="wait_approval_id_invalid",
        )
        decision = self._approval_resolver.resolve(
            approval_id,
            run_id=binding.run_spec.run_id,
            node_instance_id=node_instance_id,
            actor=self._actor,
            requested_approved=approved,
        )
        if not isinstance(decision, HarnessWaitApprovalDecision):
            raise HarnessWaitApplicationError(
                "approval resolver returned invalid evidence",
                code="wait_approval_resolver_invalid",
            )
        if (
            decision.approval_id != approval_id
            or decision.run_id != binding.run_spec.run_id
            or decision.node_instance_id != node_instance_id
            or decision.approved is not approved
            or decision.actor_identity_scope_ref != actor_scope.actor_identity_scope_ref
        ):
            raise HarnessWaitAuthorizationError(
                "approval evidence is outside the authorized actor scope",
                code="wait_approval_evidence_unauthorized",
            )
        if (
            authoritative_approval_id is not None
            and approval_id != authoritative_approval_id
        ):
            raise HarnessWaitAuthorizationError(
                "approval id does not match the authoritative Graph Wait",
                code="wait_approval_id_mismatch",
            )
        if (
            decision.graph_id != graph_ref.graph_id
            or decision.graph_version != graph_ref.identity_version
            or decision.graph_ref != graph_ref.identity_ref.exact_ref
            or decision.graph_checksum != graph_ref.checksum
        ):
            raise HarnessWaitAuthorizationError(
                "approval evidence is bound to another Graph",
                code="wait_approval_graph_mismatch",
            )
        expected_event_ref = approval_event_ref_for(
            approval_id=approval_id,
            scope=scope,
            actor_identity_scope_ref=actor_scope.actor_identity_scope_ref,
            approved=approved,
            graph_id=graph_ref.graph_id,
            graph_version=graph_ref.identity_version,
            graph_ref=graph_ref.identity_ref.exact_ref,
            graph_checksum=graph_ref.checksum,
        )
        if decision.approval_event_ref != expected_event_ref:
            raise HarnessWaitAuthorizationError(
                "approval event evidence does not match canonical Graph content",
                code="wait_approval_event_ref_invalid",
            )
        cause = HarnessWaitApprovalEvidenceRecord(
            scope=scope,
            approval_event_ref=decision.approval_event_ref,
            actor_identity_scope_ref=decision.actor_identity_scope_ref,
            approved=decision.approved,
            recorded_sequence=0,
            approval_id=decision.approval_id,
            graph_id=decision.graph_id,
            graph_version=decision.graph_version,
            graph_ref=decision.graph_ref,
            graph_checksum=decision.graph_checksum,
        )
        return self._submit(binding, cause, operation="approval")

    def cancel_wait(
        self,
        run_id: str,
        node_instance_id: str,
        *,
        cancellation_id: str,
        reason_code: str,
    ) -> HarnessWaitOperationResult:
        self._require_permission(WRITE_RUNS_PERMISSION)
        binding = self._binding(run_id)
        scope, actor_scope = self._authorized_scope(binding, node_instance_id)
        cancellation_id = _request_text(
            cancellation_id,
            "cancellation_id",
            code="wait_cancellation_id_invalid",
        )
        reason_code = _request_text(
            reason_code,
            "reason_code",
            code="wait_cancellation_reason_invalid",
        )
        cancellation_event_ref = canonical_checksum(
            {
                "cancellation_id": cancellation_id,
                "run_id": binding.run_spec.run_id,
                "node_instance_id": node_instance_id,
            }
        )
        cause = HarnessWaitCancellationRecord(
            scope=scope,
            cancellation_event_ref=cancellation_event_ref,
            actor_identity_scope_ref=actor_scope.actor_identity_scope_ref,
            reason_code=reason_code,
            cancelled_sequence=0,
        )
        return self._submit(binding, cause, operation="cancellation")

    def record_timer_wake(self, wake: HarnessWaitTimerWakeRecord) -> None:
        self._require_permission(WRITE_RUNS_PERMISSION)
        if not isinstance(wake, HarnessWaitTimerWakeRecord):
            raise TypeError("wake must be HarnessWaitTimerWakeRecord")
        binding = self._binding(wake.scope.run_id)
        scope, _ = self._authorized_scope(binding, wake.scope.node_instance_id)
        if wake.scope != scope:
            raise HarnessWaitRequestError(
                "timer wake does not match the durable Wait scope",
                code="graph_wait_cause_scope_mismatch",
            )
        self._submit(binding, wake, operation="timer")

    def record_wait_timeout(self, timeout: HarnessWaitTimeoutRecord) -> None:
        self._require_permission(WRITE_RUNS_PERMISSION)
        if not isinstance(timeout, HarnessWaitTimeoutRecord):
            raise TypeError("timeout must be HarnessWaitTimeoutRecord")
        binding = self._binding(timeout.scope.run_id)
        scope, _ = self._authorized_scope(binding, timeout.scope.node_instance_id)
        if timeout.scope != scope:
            raise HarnessWaitRequestError(
                "timeout does not match the durable Wait scope",
                code="graph_wait_cause_scope_mismatch",
            )
        self._submit(binding, timeout, operation="timeout")

    def _binding(self, run_id: str) -> HarnessWaitRuntimeBinding:
        run_id = _request_text(run_id, "run_id", code="wait_run_id_invalid")
        try:
            binding = self._runtime_resolver.resolve(run_id, actor=self._actor)
        except HarnessWaitApplicationError:
            raise
        except (RuntimeError, ValueError, TypeError) as exc:
            raise HarnessWaitApplicationError(
                "runtime resolver is unavailable",
                code="wait_runtime_resolver_unavailable",
            ) from exc
        if not isinstance(binding, HarnessWaitRuntimeBinding):
            raise HarnessWaitApplicationError(
                "runtime resolver returned an invalid binding",
                code="wait_runtime_resolver_invalid",
            )
        if binding.run_spec.run_id != run_id:
            raise HarnessWaitApplicationError(
                "runtime resolver returned another run",
                code="wait_runtime_binding_mismatch",
            )
        return binding

    def _authorized_scope(
        self,
        binding: HarnessWaitRuntimeBinding,
        node_instance_id: str,
    ) -> tuple[HarnessWaitScope, HarnessWaitActorScope]:
        node_instance_id = _request_text(
            node_instance_id,
            "node_instance_id",
            code="wait_node_instance_id_invalid",
        )
        try:
            scope = binding.control_plane.inspect_graph_wait_scope(
                binding.run_spec,
                node_instance_id,
            )
        except HarnessValidationError as exc:
            if exc.code != "graph_wait_cause_node_mismatch":
                raise
            raise HarnessWaitNotFoundError(
                "Wait was not found in the authorized actor scope",
                code="wait_not_found",
            ) from exc
        actor_scope = self._actor_scope_resolver.resolve(self._actor)
        if not isinstance(actor_scope, HarnessWaitActorScope):
            raise HarnessWaitApplicationError(
                "actor scope resolver returned an invalid scope",
                code="wait_actor_scope_resolver_invalid",
            )
        if (
            scope.tenant_scope_ref != actor_scope.tenant_scope_ref
            or scope.identity_scope_ref != actor_scope.identity_scope_ref
        ):
            raise HarnessWaitNotFoundError(
                "Wait was not found in the authorized actor scope",
                code="wait_not_found",
            )
        return scope, actor_scope

    def _submit(
        self,
        binding: HarnessWaitRuntimeBinding,
        cause: HarnessWaitCause,
        *,
        operation: str,
    ) -> HarnessWaitOperationResult:
        try:
            accepted = binding.control_plane.accept_graph_wait_cause(
                binding.run_spec,
                cause,
                occurred_at=self._clock(),
            )
        except HarnessValidationError as exc:
            if exc.code not in {
                "graph_wait_cause_identity_conflict",
                "wait_signal_identity_conflict",
            }:
                raise
            raise HarnessWaitRequestError(str(exc), code=exc.code) from exc
        driven = binding.control_plane.recover_and_run(binding.run_spec)
        state = driven.state
        scope = cause.scope
        self._emit_runtime_event(
            binding,
            cause,
            operation=operation,
        )
        return HarnessWaitOperationResult(
            operation=operation,
            wait=self._inspection_from_state(state, scope),
        )

    def _emit_runtime_event(
        self,
        binding: HarnessWaitRuntimeBinding,
        cause: HarnessWaitCause,
        *,
        operation: str,
    ) -> None:
        if self._runtime_event_sink is None:
            return
        from framework.events.runtime.projection import RuntimeEventEmitter, RuntimeEventIdentity

        identity = self._runtime_event_identity
        if identity is None:
            identity = RuntimeEventIdentity(
                node_instance_id=cause.scope.node_instance_id,
                attempt_id=cause.scope.wait_id,
            )
        elif not isinstance(identity, RuntimeEventIdentity):
            identity = RuntimeEventIdentity(**dict(identity))
        if identity.run_id is not None and identity.run_id != binding.run_spec.run_id:
            raise HarnessWaitApplicationError(
                "runtime event identity does not match the Wait run",
                code="wait_runtime_event_identity_mismatch",
            )

        event_type = {
            "approval": "approval_decided",
            "cancellation": "cancellation_confirmed",
            "timeout": "timeout",
            "signal": "worker_status",
            "timer": "worker_status",
        }.get(operation, "runtime_error")
        status = "completed"
        reason_code = getattr(cause, "reason_code", None)
        if operation == "approval":
            reason_code = "approval_granted" if getattr(cause, "approved", False) else "approval_denied"
            status = "approved" if getattr(cause, "approved", False) else "denied"
        elif operation == "cancellation":
            reason_code = reason_code or "wait_cancelled"
            status = "cancelled"
        elif operation == "timeout":
            reason_code = reason_code or "wait_timeout"
            status = "timed_out"
        elif operation == "signal":
            reason_code = "wait_signal_delivered"
            status = "signalled"
        elif operation == "timer":
            reason_code = "wait_timer_woke"
            status = "woken"
        refs = tuple(
            str(value)
            for name in (
                "approval_event_ref",
                "actor_identity_scope_ref",
                "payload_ref",
                "cancellation_event_ref",
                "deadline_ref",
                "timeout_event_ref",
            )
            if isinstance(value := getattr(cause, name, None), str)
        )
        event_id = "wait-runtime:" + canonical_checksum(
            {
                "operation": operation,
                "wait_id": cause.scope.wait_id,
                "run_id": binding.run_spec.run_id,
                "refs": refs,
                "status": status,
            }
        )
        RuntimeEventEmitter(
            self._runtime_event_sink,
            identity=identity,
            source="harness-wait-service",
            stream_id=binding.run_spec.run_id,
        ).emit(
            event_type,
            event_id=event_id,
            status=status,
            reason_code=reason_code,
            refs=refs + (cause.scope.wait_id,),
            metadata={
                "operation": operation,
                "wait_id": cause.scope.wait_id,
                "node_instance_id": cause.scope.node_instance_id,
                "status": status,
            },
        )

    def _inspection(
        self,
        binding: HarnessWaitRuntimeBinding,
        scope: HarnessWaitScope,
    ) -> HarnessWaitInspectionResult:
        state = binding.control_plane.recover_graph(binding.run_spec)
        return self._inspection_from_state(state, scope)

    @staticmethod
    def _inspection_from_state(
        state: HarnessGraphState,
        scope: HarnessWaitScope,
    ) -> HarnessWaitInspectionResult:
        registrations = tuple(
            item
            for item in state.wait_registrations
            if item.node_instance_id == scope.node_instance_id
            and item.wait_id == scope.wait_id
        )
        registration = (
            None
            if not registrations
            else max(
                registrations,
                key=lambda item: (item.registered_sequence, item.wait_id),
            )
        )
        return HarnessWaitInspectionResult(
            run_id=state.run_id,
            node_instance_id=scope.node_instance_id,
            wait_id=scope.wait_id,
            kind=("unregistered" if registration is None else registration.kind.value),
            status=("ready" if registration is None else registration.status.value),
            signal_schema_ref=scope.signal_schema_ref,
            lifecycle=state.lifecycle.value,
            outcome=state.outcome.value,
            registered_sequence=(
                None if registration is None else registration.registered_sequence
            ),
            last_event_sequence=state.last_event_sequence,
            graph_id=state.graph_ref.graph_id,
            graph_version=state.graph_ref.identity_version,
            graph_ref=state.graph_ref.identity_ref.exact_ref,
            graph_checksum=state.graph_ref.checksum,
            approval_id=_approval_id_from_node(state, scope.node_instance_id),
        )

    def _require_permission(self, permission: str) -> None:
        if self._actor.has_permission(permission):
            return
        raise HarnessWaitAuthorizationError(
            "actor lacks the required Harness Wait permission",
            code="wait_permission_denied",
        )


def _approval_id_from_node(
    state: HarnessGraphState,
    node_instance_id: str,
    *,
    run_spec: HarnessRunSpec | None = None,
) -> str | None:
    node = next(
        (item for item in state.node_instances if item.instance_id == node_instance_id),
        None,
    )
    if node is None:
        return None

    def find(value: object) -> str | None:
        if isinstance(value, Mapping):
            candidate = value.get("approval_id")
            if isinstance(candidate, str) and candidate.strip():
                candidate = candidate.strip()
                if candidate.startswith("graph.inputs.") and run_spec is not None:
                    input_key = candidate.removeprefix("graph.inputs.")
                    resolved = run_spec.inputs.get(input_key)
                    return resolved.strip() if isinstance(resolved, str) and resolved.strip() else None
                return candidate
            for nested in value.values():
                found = find(nested)
                if found is not None:
                    return found
        elif isinstance(value, (tuple, list)):
            for nested in value:
                found = find(nested)
                if found is not None:
                    return found
        return None

    return find(node.metadata) or find(node.output_refs)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _request_text(value: object, field_name: str, *, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessWaitRequestError(
            f"{field_name} is invalid",
            code=code,
        )
    try:
        return required_text(value, field_name)
    except HarnessValidationError as exc:
        raise HarnessWaitRequestError(
            f"{field_name} is invalid",
            code=code,
        ) from exc


def _request_exact_reference(value: object, field_name: str, *, code: str) -> str:
    if not isinstance(value, str):
        raise HarnessWaitRequestError(
            f"{field_name} must be an exact version reference",
            code=code,
        )
    try:
        return exact_reference(value, field_name)
    except HarnessValidationError as exc:
        raise HarnessWaitRequestError(
            f"{field_name} must be an exact version reference",
            code=code,
        ) from exc


def _request_checksum(value: object, field_name: str, *, code: str) -> str:
    if isinstance(value, str) and _CHECKSUM_PATTERN.fullmatch(value):
        return value
    raise HarnessWaitRequestError(
        f"{field_name} must be a sha256 reference",
        code=code,
    )


__all__ = [
    "HarnessWaitActorScope",
    "HarnessWaitActorScopeResolverPort",
    "HarnessWaitApplicationError",
    "HarnessWaitApplicationService",
    "HarnessWaitApprovalDecision",
    "HarnessWaitApprovalResolverPort",
    "HarnessWaitAuthorizationError",
    "HarnessWaitControlPlanePort",
    "HarnessWaitInspectionResult",
    "HarnessWaitInspectionListResult",
    "HarnessWaitNotFoundError",
    "HarnessWaitOperationResult",
    "HarnessWaitRequestError",
    "HarnessWaitRuntimeBinding",
    "HarnessWaitRuntimeResolverPort",
]
