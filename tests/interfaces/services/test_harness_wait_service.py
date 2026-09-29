from __future__ import annotations

from datetime import UTC, datetime

import pytest

from framework.events.canonical import checksum_for
from framework.events.runtime.projection import RuntimeEventProjection
from framework.harness.control_plane.harness import (
    HarnessControlPlane,
    InMemoryHarnessEventPort,
)
from framework.harness.control_plane.policy import HarnessBudget
from framework.harness.control_plane.state import HarnessRunSpec
from framework.harness.runtime.graph_dispatcher import HarnessGraphPhysicalActivityDispatcher
from framework.harness.runtime.activity_executor import HarnessGraphPhysicalActivityExecutor
from framework.harness import InMemoryHarnessNodeOutputResource
from framework.shared.attempts import AttemptSupervisor
from framework.harness.graph.dsl import (
    HarnessGraphSpec,
    Sequence,
    StepRef,
    Wait,
    WaitTimeoutPolicy,
)
from framework.harness.graph.activity import HarnessLeafActivityKind, HarnessStepSpec, HarnessWorkerType
from framework.harness.graph.bindings import (
    HarnessActivityCapabilities,
    HarnessActivityContractBinding,
    HarnessLeafActivityBinding as HarnessRuntimeLeafActivityBinding,
    HarnessRuntimeBindingAuthority,
    HarnessWorkerBinding,
)
from framework.harness.graph.definition import HarnessGraphDefinition, HarnessGraphLeafBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.side_effects.fake import CountingHarnessSideEffectHandler, InMemoryHarnessSideEffectStore
from framework.harness.side_effects.models import HarnessTerminalSideEffectPolicy
from framework.harness.side_effects.registry import HarnessSideEffectHandlerBinding, HarnessSideEffectRegistry
from framework.harness.workers.result import HarnessWorkerResult
from framework.harness.waits.models import (
    HarnessWaitScope,
    HarnessWaitTimeoutRecord,
    HarnessWaitTimerWakeRecord,
    approval_event_ref_for,
)
from interfaces.models.actor import ActorContext
from interfaces.services.harness_wait_service import (
    HarnessWaitActorScope,
    HarnessWaitApplicationService,
    HarnessWaitApprovalDecision,
    HarnessWaitAuthorizationError,
    HarnessWaitNotFoundError,
    HarnessWaitRequestError,
    HarnessWaitRuntimeBinding,
)


_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


class _RuntimeResolver:
    def __init__(self, binding: HarnessWaitRuntimeBinding) -> None:
        self.binding = binding
        self.actors: list[str] = []

    def resolve(
        self,
        run_id: str,
        *,
        actor: ActorContext,
    ) -> HarnessWaitRuntimeBinding:
        assert run_id == self.binding.run_spec.run_id
        self.actors.append(actor.actor_id)
        return self.binding


class _ActorScopeResolver:
    def __init__(self, scope: HarnessWaitActorScope) -> None:
        self.scope = scope

    def resolve(self, actor: ActorContext) -> HarnessWaitActorScope:
        assert actor.actor_id
        return self.scope


class _ApprovalResolver:
    def __init__(
        self,
        actor_identity_scope_ref: str,
        *,
        scope: HarnessWaitScope | None = None,
        graph_id: str | None = None,
        graph_version: str | None = None,
        graph_ref: str | None = None,
        graph_checksum: str | None = None,
        evidence_overrides: dict[str, object] | None = None,
    ) -> None:
        self.actor_identity_scope_ref = actor_identity_scope_ref
        self.scope = scope
        self.graph_id = graph_id
        self.graph_version = graph_version
        self.graph_ref = graph_ref
        self.graph_checksum = graph_checksum
        self.evidence_overrides = evidence_overrides or {}
        self.calls: list[tuple[str, str, str, bool]] = []
        self.decisions: dict[str, HarnessWaitApprovalDecision] = {}

    def resolve(
        self,
        approval_id: str,
        *,
        run_id: str,
        node_instance_id: str,
        actor: ActorContext,
        requested_approved: bool,
    ) -> HarnessWaitApprovalDecision:
        self.calls.append((approval_id, run_id, node_instance_id, requested_approved))
        existing = self.decisions.get(approval_id)
        if existing is not None:
            if existing.approved is not requested_approved:
                raise HarnessWaitRequestError(
                    "approval id already has another durable decision",
                    code="wait_approval_decision_conflict",
                )
            return existing
        decision = HarnessWaitApprovalDecision(
            approval_id=str(self.evidence_overrides.get("approval_id", approval_id)),
            run_id=str(self.evidence_overrides.get("run_id", run_id)),
            node_instance_id=str(
                self.evidence_overrides.get("node_instance_id", node_instance_id)
            ),
            graph_id=str(self.evidence_overrides.get("graph_id", self.graph_id))
            if self.graph_id is not None
            else None,
            graph_version=str(
                self.evidence_overrides.get("graph_version", self.graph_version)
            )
            if self.graph_version is not None
            else None,
            graph_ref=str(self.evidence_overrides.get("graph_ref", self.graph_ref))
            if self.graph_ref is not None
            else None,
            graph_checksum=str(
                self.evidence_overrides.get("graph_checksum", self.graph_checksum)
            )
            if self.graph_checksum is not None
            else None,
            approval_event_ref=(
                str(self.evidence_overrides["approval_event_ref"])
                if "approval_event_ref" in self.evidence_overrides
                else
                approval_event_ref_for(
                    approval_id=approval_id,
                    scope=self.scope,
                    actor_identity_scope_ref=self.actor_identity_scope_ref,
                    approved=requested_approved,
                    graph_id=self.graph_id,
                    graph_version=self.graph_version,
                    graph_ref=self.graph_ref,
                    graph_checksum=self.graph_checksum,
                )
                if self.scope is not None
                else checksum_for(
                    {
                        "approval_id": approval_id,
                        "actor_id": actor.actor_id,
                        "approved": requested_approved,
                    }
                )
            ),
            actor_identity_scope_ref=self.actor_identity_scope_ref,
            approved=requested_approved,
        )
        self.decisions[approval_id] = decision
        return decision


def test_inspection_is_actor_scoped_and_returns_only_safe_wait_fields() -> None:
    service, registration, resolver = _waiting_service("inspect")

    result = service.inspect_wait("run-service-inspect", registration.node_instance_id)

    payload = result.to_dict()
    assert result.status == "registered"
    assert result.lifecycle == "waiting"
    assert resolver.actors == ["actor-1"]
    assert "tenant_scope_ref" not in payload
    assert "identity_scope_ref" not in payload
    assert "signal_inbox" not in payload
    assert result.graph_id == "graph-run-service-inspect"
    assert result.graph_version == "1"
    assert result.graph_ref == "graph-run-service-inspect@1"
    assert result.graph_checksum.startswith("sha256:")


def test_signal_operation_validates_schema_and_correlation_then_drives_run() -> None:
    service, registration, _ = _waiting_service("signal")

    result = service.deliver_signal(
        "run-service-signal",
        registration.node_instance_id,
        signal_id="signal-1",
        signal_schema_ref=registration.signal_schema_ref,
        correlation={"request_id": "request-run-service-signal"},
        payload_ref=checksum_for({"payload": "accepted"}),
    )

    assert result.operation == "signal"
    assert result.wait.status == "resumed"
    assert result.wait.lifecycle == "completed"
    assert result.wait.outcome == "succeeded"


@pytest.mark.parametrize(
    ("schema_ref", "correlation", "code"),
    (
        (
            "wrong.signal@1",
            {"request_id": "request-run-service-invalid"},
            "wait_signal_schema_mismatch",
        ),
        ("newsroom.wait@1", {"request_id": "wrong"}, "wait_correlation_mismatch"),
    ),
)
def test_signal_rejects_untrusted_schema_or_correlation(
    schema_ref: str,
    correlation: dict[str, object],
    code: str,
) -> None:
    service, registration, _ = _waiting_service("invalid")

    with pytest.raises(HarnessWaitRequestError) as captured:
        service.deliver_signal(
            "run-service-invalid",
            registration.node_instance_id,
            signal_id="signal-invalid",
            signal_schema_ref=schema_ref,
            correlation=correlation,
            payload_ref=checksum_for({"payload": "invalid"}),
        )

    assert captured.value.code == code


def test_wrong_actor_scope_is_hidden_as_not_found() -> None:
    service, registration, _ = _waiting_service(
        "wrong-scope",
        actor_scope_overrides={
            "tenant_scope_ref": checksum_for({"tenant": "other"}),
        },
    )

    with pytest.raises(HarnessWaitNotFoundError) as captured:
        service.inspect_wait(
            "run-service-wrong-scope",
            registration.node_instance_id,
        )

    assert captured.value.code == "wait_not_found"


def test_missing_wait_node_is_hidden_as_not_found() -> None:
    service, _, _ = _waiting_service("missing-node")

    with pytest.raises(HarnessWaitNotFoundError) as captured:
        service.inspect_wait("run-service-missing-node", "unknown-node")

    assert captured.value.code == "wait_not_found"


def test_missing_permission_fails_before_runtime_resolution() -> None:
    service, registration, resolver = _waiting_service(
        "permission",
        actor=ActorContext(
            actor_id="viewer",
            actor_type="user",
            roles=["viewer"],
            request_id="request-permission",
        ),
    )

    with pytest.raises(HarnessWaitAuthorizationError) as captured:
        service.cancel_wait(
            "run-service-permission",
            registration.node_instance_id,
            cancellation_id="cancel-1",
            reason_code="operator_cancelled",
        )

    assert captured.value.code == "wait_permission_denied"
    assert resolver.actors == []


def test_approval_uses_resolved_durable_evidence_and_actor_identity() -> None:
    service, registration, _ = _waiting_service("approval", wait_kind="approval")

    result = service.decide_approval(
        "run-service-approval",
        registration.node_instance_id,
        approval_id="approval-1",
        approved=True,
    )

    assert result.operation == "approval"
    assert result.wait.status == "resumed"
    assert result.wait.outcome == "succeeded"


def test_approval_identical_retry_reuses_durable_evidence_without_new_event() -> None:
    service, registration, _ = _waiting_service(
        "approval-retry",
        wait_kind="approval",
    )

    first = service.decide_approval(
        "run-service-approval-retry",
        registration.node_instance_id,
        approval_id="approval-1",
        approved=True,
    )
    retried = service.decide_approval(
        "run-service-approval-retry",
        registration.node_instance_id,
        approval_id="approval-1",
        approved=True,
    )

    assert retried.wait.last_event_sequence == first.wait.last_event_sequence
    assert retried.wait.outcome == "succeeded"


def test_approval_retry_rejects_a_conflicting_durable_decision() -> None:
    service, registration, _ = _waiting_service(
        "approval-conflict",
        wait_kind="approval",
    )
    service.decide_approval(
        "run-service-approval-conflict",
        registration.node_instance_id,
        approval_id="approval-1",
        approved=True,
    )

    with pytest.raises(HarnessWaitRequestError) as captured:
        service.decide_approval(
            "run-service-approval-conflict",
            registration.node_instance_id,
            approval_id="approval-1",
            approved=False,
        )

    assert captured.value.code == "wait_approval_decision_conflict"


@pytest.mark.parametrize(
    "evidence_overrides",
    (
        {"approval_id": "other-approval"},
        {"run_id": "other-run"},
        {"node_instance_id": "other-node"},
    ),
)
def test_approval_rejects_evidence_bound_to_another_resource(
    evidence_overrides: dict[str, object],
) -> None:
    service, registration, _ = _waiting_service(
        "approval-evidence-scope",
        wait_kind="approval",
        approval_evidence_overrides=evidence_overrides,
    )

    with pytest.raises(HarnessWaitAuthorizationError) as captured:
        service.decide_approval(
            "run-service-approval-evidence-scope",
            registration.node_instance_id,
            approval_id="approval-1",
            approved=True,
        )

    assert captured.value.code == "wait_approval_evidence_unauthorized"


def test_approval_rejects_forged_event_reference() -> None:
    service, registration, _ = _waiting_service(
        "approval-forged-event",
        wait_kind="approval",
        approval_evidence_overrides={
            "approval_event_ref": checksum_for({"forged": True}),
        },
    )

    with pytest.raises(HarnessWaitAuthorizationError) as captured:
        service.decide_approval(
            "run-service-approval-forged-event",
            registration.node_instance_id,
            approval_id="approval-1",
            approved=True,
        )

    assert captured.value.code == "wait_approval_event_ref_invalid"


def test_approval_rejects_graph_checksum_tampering() -> None:
    service, registration, _ = _waiting_service(
        "approval-forged-graph",
        wait_kind="approval",
        approval_evidence_overrides={
            "graph_checksum": checksum_for({"forged": "graph"}),
        },
    )

    with pytest.raises(HarnessWaitAuthorizationError) as captured:
        service.decide_approval(
            "run-service-approval-forged-graph",
            registration.node_instance_id,
            approval_id="approval-1",
            approved=True,
        )

    assert captured.value.code == "wait_approval_graph_mismatch"


def test_cancellation_uses_authoritative_scope_and_completes_cancelled() -> None:
    service, registration, _ = _waiting_service("cancel")

    result = service.cancel_wait(
        "run-service-cancel",
        registration.node_instance_id,
        cancellation_id="cancel-1",
        reason_code="operator_cancelled",
    )

    assert result.operation == "cancellation"
    assert result.wait.status == "cancelled"
    assert result.wait.lifecycle == "completed"
    assert result.wait.outcome == "cancelled"


def test_cancellation_identical_retry_does_not_append_another_event() -> None:
    service, registration, _ = _waiting_service("cancel-retry")

    first = service.cancel_wait(
        "run-service-cancel-retry",
        registration.node_instance_id,
        cancellation_id="cancel-1",
        reason_code="operator_cancelled",
    )
    retried = service.cancel_wait(
        "run-service-cancel-retry",
        registration.node_instance_id,
        cancellation_id="cancel-1",
        reason_code="operator_cancelled",
    )

    assert retried.wait.last_event_sequence == first.wait.last_event_sequence
    assert retried.wait.outcome == "cancelled"


def test_cancellation_id_reuse_with_another_reason_is_a_conflict() -> None:
    service, registration, _ = _waiting_service("cancel-conflict")
    service.cancel_wait(
        "run-service-cancel-conflict",
        registration.node_instance_id,
        cancellation_id="cancel-1",
        reason_code="operator_cancelled",
    )

    with pytest.raises(HarnessWaitRequestError) as captured:
        service.cancel_wait(
            "run-service-cancel-conflict",
            registration.node_instance_id,
            cancellation_id="cancel-1",
            reason_code="policy_cancelled",
        )

    assert captured.value.code == "graph_wait_cause_identity_conflict"


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    (
        (
            {"signal_schema_ref": "newsroom.wait@latest"},
            "wait_signal_schema_invalid",
        ),
        ({"payload_ref": "inline-payload"}, "wait_payload_ref_invalid"),
        ({"signal_id": "   "}, "wait_signal_id_invalid"),
    ),
)
def test_signal_rejects_invalid_external_references(
    overrides: dict[str, object],
    expected_code: str,
) -> None:
    service, registration, _ = _waiting_service("invalid-reference")
    arguments: dict[str, object] = {
        "signal_id": "signal-1",
        "signal_schema_ref": registration.signal_schema_ref,
        "correlation": {"request_id": "request-run-service-invalid-reference"},
        "payload_ref": checksum_for({"payload": "accepted"}),
        **overrides,
    }

    with pytest.raises(HarnessWaitRequestError) as captured:
        service.deliver_signal(
            "run-service-invalid-reference",
            registration.node_instance_id,
            **arguments,
        )

    assert captured.value.code == expected_code


def test_signal_rejects_non_canonical_correlation_as_request_error() -> None:
    service, registration, _ = _waiting_service("invalid-correlation")

    with pytest.raises(HarnessWaitRequestError) as captured:
        service.deliver_signal(
            "run-service-invalid-correlation",
            registration.node_instance_id,
            signal_id="signal-1",
            signal_schema_ref=registration.signal_schema_ref,
            correlation={"unsupported": object()},
            payload_ref=checksum_for({"payload": "accepted"}),
        )

    assert captured.value.code == "wait_correlation_invalid"


def test_timer_adapter_sink_records_wake_through_application_service() -> None:
    service, registration, _ = _waiting_service("timer", wait_kind="timer")
    scope = service.inspect_wait(
        "run-service-timer",
        registration.node_instance_id,
    )

    service.record_timer_wake(
        HarnessWaitTimerWakeRecord(
            _wait_scope("run-service-timer", registration),
            registration.deadline_ref,
            checksum_for({"timer": "service-wake"}),
            0,
        )
    )

    completed = service.inspect_wait(
        "run-service-timer",
        registration.node_instance_id,
    )
    assert scope.status == "registered"
    assert completed.status == "resumed"
    assert completed.lifecycle == "completed"


def test_wait_mutations_emit_redacted_runtime_facts() -> None:
    projection = RuntimeEventProjection()
    service, registration, _ = _waiting_service(
        "runtime-events",
        wait_kind="timer",
        runtime_event_sink=projection,
    )
    service.record_timer_wake(
        HarnessWaitTimerWakeRecord(
            _wait_scope("run-service-runtime-events", registration),
            registration.deadline_ref,
            checksum_for({"timer": "runtime-event"}),
            0,
        )
    )
    events = projection.timeline(stream_id="run-service-runtime-events").events
    assert len(events) == 1
    assert events[0].event_type.value == "worker_status"
    assert events[0].metadata["operation"] == "timer"


def test_wait_timeout_emits_canonical_timeout_with_bounded_evidence_refs() -> None:
    projection = RuntimeEventProjection()
    service, registration, _ = _waiting_service(
        "runtime-timeout",
        wait_kind="timer",
        runtime_event_sink=projection,
    )
    timeout_event_ref = checksum_for({"timeout": "runtime-timeout"})
    service.record_wait_timeout(
        HarnessWaitTimeoutRecord(
            _wait_scope("run-service-runtime-timeout", registration),
            registration.deadline_ref,
            timeout_event_ref,
            0,
        )
    )

    events = projection.timeline(stream_id="run-service-runtime-timeout").events
    assert len(events) == 1
    event = events[0]
    assert event.event_type.value == "timeout"
    assert event.status == "timed_out"
    assert event.reason_code == "wait_timeout"
    assert registration.deadline_ref in event.refs
    assert timeout_event_ref in event.refs


def _waiting_service(
    suffix: str,
    *,
    wait_kind: str = "signal",
    actor: ActorContext | None = None,
    actor_scope_overrides: dict[str, str] | None = None,
    approval_evidence_overrides: dict[str, object] | None = None,
    runtime_event_sink: object | None = None,
) -> tuple[HarnessWaitApplicationService, object, _RuntimeResolver]:
    run_id = f"run-service-{suffix}"
    tenant_scope_ref = checksum_for({"tenant": run_id})
    identity_scope_ref = checksum_for({"identity": run_id})
    actor_identity_scope_ref = checksum_for({"actor": "actor-1"})
    inputs = {
        "request_id": f"request-{run_id}",
        **({"approval_id": "approval-1"} if wait_kind == "approval" else {}),
        "tenant_scope_ref": tenant_scope_ref,
        "identity_scope_ref": identity_scope_ref,
        "deadline_ref": checksum_for({"deadline": run_id}),
    }
    after = HarnessStepSpec(
        "after",
        HarnessWorkerType.FUNCTION,
        output_key="after_output",
        metadata={"step_version": "1", "worker_version": "1"},
    )
    graph = HarnessGraphSpec(
        graph_id=f"graph-{run_id}",
        root=Sequence(
            (
                Wait(
                    "approval-wait" if wait_kind == "approval" else "signal-wait",
                    wait_kind,
                    {
                        "request_id": "graph.inputs.request_id",
                        **(
                            {"approval_id": "graph.inputs.approval_id"}
                            if wait_kind == "approval"
                            else {}
                        ),
                    },
                    "newsroom.wait",
                    "1",
                    "graph.inputs.tenant_scope_ref",
                    "graph.inputs.identity_scope_ref",
                    deadline_input_path=(
                        "graph.inputs.deadline_ref"
                        if wait_kind == "timer"
                        else None
                    ),
                    timeout_policy=(
                        WaitTimeoutPolicy("halt") if wait_kind == "timer" else None
                    ),
                ),
                StepRef("after"),
            )
        ),
        input_keys=tuple(sorted(inputs)),
    )
    definition = HarnessGraphDefinition(
        graph_id=graph.graph_id,
        graph_version="1",
        root=graph,
        activities=(after,),
        leaf_activity_bindings=(
            HarnessGraphLeafBinding(
                "after",
                HarnessLeafActivityKind.FUNCTION,
                HarnessContractReference(HarnessContractKind.WORKER, "after", "1"),
                HarnessContractReference(HarnessContractKind.ACTIVITY, "test.function.activity", "1"),
            ),
        ),
        task_plan_stage_bindings=(),
        committed_output_bindings=(),
        repair_bindings=(),
        terminal_side_effect_policy=HarnessTerminalSideEffectPolicy(
            policy_id="test.terminal",
            version="1",
            handler="test.terminal@1",
            kind="artifact",
            requires_approval=False,
            retry_limit=1,
            not_required_evidence_ref=checksum_for("test.terminal"),
        ),
    )
    run_spec = HarnessRunSpec(
        run_id,
        definition,
        inputs=inputs,
        metadata={
            "tenant_scope_ref": tenant_scope_ref,
            "identity_scope_ref": identity_scope_ref,
            "subject_scope_ref": checksum_for({"subject": run_id}),
        },
        budget=HarnessBudget.safe_default(),
        created_at=_NOW,
    )
    worker = _FunctionWorker("after")
    activity = _FunctionActivity()
    side_effect_store = InMemoryHarnessSideEffectStore()
    side_effect_registry = HarnessSideEffectRegistry(
        (
            HarnessSideEffectHandlerBinding(
                "test.terminal@1",
                "artifact",
                CountingHarnessSideEffectHandler(side_effect_store, disposition="accepted"),
            ),
        )
    )
    control_plane = HarnessControlPlane(
        event_port=InMemoryHarnessEventPort(),
        side_effect_store=side_effect_store,
        runtime_binding_authority=HarnessRuntimeBindingAuthority(
            workers=(HarnessWorkerBinding("after@1", HarnessWorkerType.FUNCTION, worker),),
            activities=(HarnessActivityContractBinding("test.function.activity@1", activity),),
            leaf_activities=(
                HarnessRuntimeLeafActivityBinding(
                    HarnessLeafActivityKind.FUNCTION,
                    "after@1",
                    "test.function.activity@1",
                ),
            ),
            side_effect_registry=side_effect_registry,
        ),
    )
    executor = HarnessGraphPhysicalActivityExecutor(
        binding_authority=control_plane.runtime_binding_authority,
        input_resolver=control_plane,
        node_output_resource=InMemoryHarnessNodeOutputResource(),
        result_committer=None,
        supervisor=AttemptSupervisor(),
    )
    control_plane.install_graph_activity_dispatcher(
        HarnessGraphPhysicalActivityDispatcher(
            executor=executor,
            graph_resolver=control_plane.graph_for_activity,
            input_resolver=control_plane,
            accept=control_plane.accept_graph_activity_for_execution,
            record_call_marker=control_plane.record_graph_activity_call_marker,
            record_result=control_plane.record_graph_activity_result_event,
            apply_result=control_plane.commit_physical_graph_result,
        )
    )
    waiting = control_plane.run(run_spec).state
    assert waiting is not None
    registration = waiting.wait_registrations[0]
    binding = HarnessWaitRuntimeBinding(run_spec, control_plane)
    runtime_resolver = _RuntimeResolver(binding)
    actor_scope_values = {
        "tenant_scope_ref": tenant_scope_ref,
        "identity_scope_ref": identity_scope_ref,
        "actor_identity_scope_ref": actor_identity_scope_ref,
        **(actor_scope_overrides or {}),
    }
    actor_scope_resolver = _ActorScopeResolver(
        HarnessWaitActorScope(**actor_scope_values)
    )
    approval_resolver = _ApprovalResolver(
        actor_identity_scope_ref,
        scope=_wait_scope(run_id, registration),
        graph_id=waiting.graph_ref.graph_id,
        graph_version=waiting.graph_ref.identity_version,
        graph_ref=waiting.graph_ref.identity_ref.exact_ref,
        graph_checksum=waiting.graph_ref.checksum,
        evidence_overrides=approval_evidence_overrides,
    )
    service = HarnessWaitApplicationService(
        actor=actor
        or ActorContext(
            actor_id="actor-1",
            actor_type="user",
            roles=["admin"],
            request_id=f"request-{suffix}",
        ),
        runtime_resolver=runtime_resolver,
        actor_scope_resolver=actor_scope_resolver,
        approval_resolver=approval_resolver,
        clock=lambda: _NOW,
        runtime_event_sink=runtime_event_sink,
    )
    return service, registration, runtime_resolver


class _FunctionWorker:
    worker_version = "1"
    worker_type = HarnessWorkerType.FUNCTION

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def execute(self, task: dict) -> HarnessWorkerResult:
        return HarnessWorkerResult("succeeded", output=task)


class _FunctionActivity:
    activity_contract_id = "test.function.activity"
    activity_contract_version = "1"
    capabilities = HarnessActivityCapabilities()

    def dispatch(self, _request: dict) -> None:
        return None


def _wait_scope(run_id: str, registration) -> HarnessWaitScope:
    return HarnessWaitScope(
        registration.wait_id,
        run_id,
        registration.node_instance_id,
        registration.tenant_scope_ref,
        registration.identity_scope_ref,
        registration.signal_schema_ref,
        registration.correlation_ref,
    )
