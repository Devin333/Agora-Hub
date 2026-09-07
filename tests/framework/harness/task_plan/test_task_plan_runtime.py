from __future__ import annotations

from dataclasses import replace

import pytest

from framework.agent.models.orchestration import ParentObservationLimits
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan import (
    FakePlanCandidateBuilder,
    InMemoryTaskPlanStore,
    PlanBuildBudget,
    PlanCandidate,
    PlanPatch,
    PlanPatchOperation,
    PlanPatchOperationType,
    TaskAcceptanceCriteria,
    TaskBudget,
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
    TaskOutputContract,
    TaskPlanEvent,
    TaskPlanPolicy,
    TaskPlanScheduler,
    TaskPlanStageRequest,
    TaskPlanStageIdentity,
    TaskPlanStageRunner,
    TaskPlanValidator,
    TaskResultRecord,
    TaskSpec,
    TaskLifecycle,
)
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.inspection import (
    TaskPlanInspectionDecision,
    TaskPlanInspectionRequest,
    TaskPlanInspectionService,
    TaskPlanReplayVerdict,
)
from framework.harness.task_plan.observability import (
    TaskPlanMetricSample,
    task_plan_metric_samples,
    task_plan_trace_events,
)
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.workers.result import HarnessWorkerResult
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
from framework.shared.graph_identity import GraphExecutionIdentity
from tests.fixtures.task_plan import build_task_plan_stage_binding


class _Worker:
    worker_id = "worker"
    worker_version = "1"
    worker_type = HarnessWorkerType.LLM

    def execute(self, task):
        return HarnessWorkerResult(status="succeeded", output={"value": task["task_id"] if "task_id" in task else "ok"})


class _AcceptingResultVerifier:
    registered_gate_refs = ("gate@1",)

    def verify(self, result, *, task, request):
        instance = request.instance
        return TaskResultRecord.for_plan(
            request.plan,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            status=TaskLifecycle.SUCCEEDED,
            result_ref=f"result://{instance.task_id}",
            output_roles=(task.output_role,),
            output_schema_ref=task.task.output_contract.schema_ref,
            verified_gate_refs=task.gate_refs,
            gate_evidence_refs=tuple(
                f"evidence://gate/{index}"
                for index, _gate_ref in enumerate(task.gate_refs, start=1)
            ),
        )


class _FailOnceResultVerifier(_AcceptingResultVerifier):
    def __init__(self, *, failure_code: str = "task_worker_failed") -> None:
        self.calls = 0
        self.failure_code = failure_code

    def verify(self, result, *, task, request):
        self.calls += 1
        if self.calls == 1:
            instance = request.instance
            return TaskResultRecord.for_plan(
                request.plan,
                task_id=instance.task_id,
                task_instance_id=instance.task_instance_id,
                attempt=instance.attempt,
                status=TaskLifecycle.FAILED,
                error_code=self.failure_code,
            )
        return super().verify(result, task=task, request=request)


def _setup(*, roles=("role",), capabilities=("cap",)):
    policy = TaskPlanPolicy(
        policy_id="test.task-plan",
        version="1",
        stage_id="dynamic_stage",
        allowed_worker_capabilities=capabilities,
        allowed_subagent_ids=(),
        allowed_tool_ids=(),
        allowed_memory_namespaces=(),
        allowed_input_refs=("document",),
        allowed_output_roles=roles,
        required_output_roles=roles,
        allowed_output_schema_refs=("schema://result@1",),
        allowed_gate_refs=("gate@1",),
        deterministic_aggregator_refs={},
        pinned_capability_bindings={capability: f"{capability}-worker@1" for capability in capabilities},
        required_worker_contract_refs={capability: f"{capability}-contract@1" for capability in capabilities},
        max_tasks=8,
        max_depth=8,
        max_parallelism=2,
        max_replans=1,
        max_task_attempts=2,
        max_plan_build_calls=1,
        max_plan_build_turns=2,
        max_plan_build_tool_calls=0,
        per_task_budget=TaskBudget(max_turns=2),
        aggregate_task_budget=TaskBudget(max_turns=8),
    )
    stage_binding = build_task_plan_stage_binding(
        graph_id="graph",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
    )
    registrations = []
    for capability in capabilities:
        worker = _Worker()
        worker.worker_id = f"{capability}-worker"
        binding = HarnessWorkerBinding(
            HarnessContractReference(HarnessContractKind.WORKER, worker.worker_id, "1"),
            HarnessWorkerType.LLM,
            worker,
        )
        registrations.append(TaskCapabilityRegistration(
            capability,
            binding,
            f"{capability}-contract@1",
            "schema://input@1",
            "schema://result@1",
        ))
    return stage_binding, policy, TaskCapabilityRegistry(registrations)


def test_task_plan_policy_observation_defaults_and_checksum_roundtrip() -> None:
    _, policy, _ = _setup()
    expected = ParentObservationLimits().to_dict()
    assert dict(policy.parent_observation_limits) == expected
    restored = TaskPlanPolicy.from_dict(policy.to_dict())
    assert dict(restored.parent_observation_limits) == expected
    assert restored.policy_checksum == policy.policy_checksum

    changed = replace(policy, parent_observation_limits={**expected, "max_summary_bytes": 1024})
    assert changed.policy_checksum != policy.policy_checksum
    assert TaskPlanPolicy.from_dict(changed.to_dict()).policy_checksum == changed.policy_checksum


@pytest.mark.parametrize("field", ["max_total_bytes", "max_observaton_bytes"])
def test_task_plan_policy_rejects_noncanonical_observation_fields(field: str) -> None:
    _, policy, _ = _setup()
    with pytest.raises(HarnessValidationError) as exc_info:
        replace(policy, parent_observation_limits={**policy.parent_observation_limits, field: 512})
    assert exc_info.value.code == "invalid_task_plan_policy"


def _task(task_id: str, capability: str = "cap", role: str = "role", depends_on=()):
    return TaskSpec(
        task_id=task_id,
        objective=f"objective-{task_id}",
        worker_capability=capability,
        input_refs=("document",),
        output_contract=TaskOutputContract("schema://result@1", role),
        acceptance_criteria=TaskAcceptanceCriteria(("gate@1",)),
        depends_on=depends_on,
        budget_request=TaskBudget(max_turns=1),
        retry_policy={"max_attempts": 1, "retryable_reason_codes": []},
    )


def _candidate(stage_binding, tasks, roles=("role",)):
    return PlanCandidate.for_stage(
        stage_identity=TaskPlanStageIdentity("run", stage_binding),
        candidate_id="candidate",
        input_context_refs=("document",),
        tasks=tuple(tasks),
        required_output_roles=roles,
        generated_by="planner@1",
        requested_plan_budget=PlanBuildBudget(),
    )


def _execution_identity(candidate):
    return GraphExecutionIdentity(
        run_id=candidate.run_id, graph_id=candidate.graph_id, graph_version=candidate.graph_version,
        graph_ref=candidate.graph_ref, graph_checksum=candidate.graph_checksum,
        node_id=candidate.stage_id, node_instance_id=f"{candidate.stage_id}-node-1",
        activity_id="test-parent-activity", attempt=1,
    )


def test_validation_rejects_cycle_without_binding_or_worker_activity():
    graph, policy, registry = _setup()
    candidate = _candidate(graph, (_task("a", depends_on=("b",)), _task("b", depends_on=("a",))))
    result = TaskPlanValidator().validate(
        candidate,
        policy=policy,
        capabilities=registry,
        context=validator_context(graph),
    )
    assert not result.accepted
    assert "dependency_cycle" in {item.code for item in result.diagnostics}


def test_scheduler_orders_ready_tasks_and_honors_parallelism():
    graph, policy, registry = _setup(roles=("role_a", "role_b"))
    candidate = _candidate(graph, (_task("b", role="role_b"), _task("a", role="role_a")), roles=("role_a", "role_b"))
    validator = TaskPlanValidator()
    context = validator_context(graph)
    plan = validator.accept(
        candidate,
        policy,
        registry,
        context=context,
        accepted_at="2026-08-01T00:00:00Z",
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    decision = TaskPlanScheduler().next_ready_tasks(
        store.load_projection("run", "dynamic_stage"),
        1,
        plan=plan,
        policy=policy,
        available_input_refs=("document",),
    )
    assert [item.task_id for item in decision.task_instances] == ["a"]


def test_ready_task_is_durably_committed_before_worker_invocation() -> None:
    graph, policy, registry = _setup()
    candidate = _candidate(graph, (_task("a"),))
    store = InMemoryTaskPlanStore()
    event_types_at_worker_call: list[str] = []

    def execute(_binding, _request):
        event_types_at_worker_call.extend(
            event.event_type
            for event in store.read_events("run", "dynamic_stage")
        )
        return HarnessWorkerResult(status="succeeded", output={"value": "accepted"})

    result = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
        result_verifier=_AcceptingResultVerifier(),
        worker_executor=execute,
    ).run(
        TaskPlanStageRequest(
            run_id="run",
            stage_binding=graph,
            context_refs={"document": "document"},
            policy=policy,
            policy_ref=policy.exact_ref,
            candidate=candidate,
            accepted_at="2026-08-01T00:00:00Z",
        )
    )

    assert result.status.value == "succeeded"
    assert event_types_at_worker_call[-3:] == [
        "TASK_READY",
        "TASK_DISPATCHED",
        "TASK_STARTED",
    ]


def test_runner_fails_closed_when_verifier_has_no_gate_registry() -> None:
    graph, policy, registry = _setup()
    candidate = _candidate(graph, (_task("a"),))
    calls: list[str] = []

    class _VerifierWithoutRegistry:
        def verify(self, result, *, task, request):
            return _AcceptingResultVerifier().verify(
                result,
                task=task,
                request=request,
            )

    def execute(_binding, request):
        calls.append(request.task_id)
        return HarnessWorkerResult(status="succeeded", output={"value": "unexpected"})

    result = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=InMemoryTaskPlanStore(),
        result_verifier=_VerifierWithoutRegistry(),
        worker_executor=execute,
    ).run(
        TaskPlanStageRequest(
            run_id="run",
            stage_binding=graph,
            context_refs={"document": "document"},
            policy=policy,
            policy_ref=policy.exact_ref,
            candidate=candidate,
            accepted_at="2026-08-01T00:00:00Z",
        )
    )

    assert result.status.value == "blocked"
    assert result.diagnostics["reason_code"] == "task_plan_candidate_rejected"
    assert calls == []


def test_replacement_failure_is_skipped_and_runner_reaches_verified() -> None:
    graph, policy, registry = _setup()
    candidate = _candidate(graph, (_task("helper"),))
    store = InMemoryTaskPlanStore()
    verifier = _FailOnceResultVerifier()
    calls: list[str] = []

    def execute(_binding, request):
        calls.append(request.task_id)
        return HarnessWorkerResult(status="succeeded", output={"value": request.task_id})

    request = TaskPlanStageRequest(
        run_id="run",
        stage_binding=graph,
        context_refs={"document": "document"},
        policy=policy,
        policy_ref=policy.exact_ref,
        candidate=candidate,
        accepted_at="2026-08-01T00:00:00Z",
    )
    first = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
        result_verifier=verifier,
        worker_executor=execute,
    ).run(request)
    assert first.status.value == "blocked"
    assert first.diagnostics["reason_code"] == "task_plan_retry_not_allowed"
    assert calls == ["helper"]

    plan = store.plan("run", "dynamic_stage")
    assert plan is not None
    replacement = PlanPatch.for_plan(
        plan,
        patch_id="replacement-patch",
        reason_code="repair",
        source_candidate_ref="candidate://replacement",
        operations=(
            PlanPatchOperation(
                PlanPatchOperationType.ADD_REPLACEMENT_TASK,
                target_task_id="helper",
                replacement_task=_task("helper-replacement"),
            ),
        ),
    )
    next_plan = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
        result_verifier=verifier,
        worker_executor=execute,
    ).apply_patch(request, replacement)
    assert next_plan.version == 2
    assert next(item for item in store.load_projection("run", "dynamic_stage").tasks if item.task_id == "helper").status is TaskLifecycle.SKIPPED

    final = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
        result_verifier=verifier,
        worker_executor=execute,
    ).run(request)
    assert final.status.value == "succeeded"
    assert calls == ["helper", "helper-replacement"]
    assert [event.event_type for event in store.read_events("run", "dynamic_stage")][-2:] == [
        "STAGE_OUTPUT_AGGREGATED",
        "TASK_PLAN_VERIFIED",
    ]


def test_terminal_failure_closes_unadmitted_dependency_chain_without_child_calls() -> None:
    graph, policy, registry = _setup(
        roles=("role_a", "role_b", "role_c", "role_d"),
        capabilities=("cap_a", "cap_b", "cap_c", "cap_d"),
    )
    candidate = _candidate(
        graph,
        (
            _task("a", "cap_a", "role_a"),
            _task("b", "cap_b", "role_b", depends_on=("a",)),
            _task("c", "cap_c", "role_c", depends_on=("b",)),
            _task("d", "cap_d", "role_d"),
        ),
        roles=("role_a", "role_b", "role_c", "role_d"),
    )
    store = InMemoryTaskPlanStore()
    calls: list[str] = []

    def execute(_binding, instance, identity):
        assert identity == _execution_identity(candidate)
        calls.append(instance.task_id)
        return HarnessWorkerResult(status="succeeded", output={"value": instance.task_id})

    request = TaskPlanStageRequest(
        run_id="run",
        stage_binding=graph,
        context_refs={"document": "document"},
        policy=policy,
        policy_ref=policy.exact_ref,
        candidate=candidate,
        accepted_at="2026-08-01T00:00:00Z",
    )
    result = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
        result_verifier=_FailOnceResultVerifier(failure_code="fatal"),
        worker_executor=execute,
        parallel_coordinator=ParallelAgentCoordinator(
            max_workers=2,
            allow_test_executor=True,
        ),
    ).run(replace(request, execution_identity=_execution_identity(candidate)))

    projection = store.load_projection("run", "dynamic_stage")
    statuses = {item.task_id: item.status for item in projection.tasks}
    assert result.status.value == "blocked"
    assert sorted(calls) == ["a", "d"]
    assert statuses["b"] is TaskLifecycle.BLOCKED_DEPENDENCY
    assert statuses["c"] is TaskLifecycle.BLOCKED_DEPENDENCY
    assert statuses["d"] is TaskLifecycle.SUCCEEDED
    assert [event.event_type for event in store.read_events("run", "dynamic_stage")].count(
        "TASK_BLOCKED_UPSTREAM_FAILURE"
    ) == 2


def test_store_accepts_duplicate_identical_result_once():
    graph, policy, registry = _setup()
    candidate = _candidate(graph, (_task("a"),))
    validator = TaskPlanValidator()
    plan = validator.accept(
        candidate,
        policy,
        registry,
        context=validator_context(graph),
        accepted_at="2026-08-01T00:00:00Z",
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    scheduler = TaskPlanScheduler()
    decision = scheduler.next_ready_tasks(store.load_projection("run", "dynamic_stage"), 1, plan=plan, policy=policy, available_input_refs=("document",))
    store.update_projection(scheduler.mark_dispatched(scheduler.reserve_ready_tasks(store.load_projection("run", "dynamic_stage"), decision), decision.task_instances[0]))
    instance = decision.task_instances[0]
    result = TaskResultRecord.for_plan(
        plan,
        task_id="a",
        task_instance_id=instance.task_instance_id,
        attempt=1,
        status=TaskLifecycle.SUCCEEDED,
        result_ref="result://a",
        output_refs=("artifact://a",),
        output_roles=("role",),
        output_schema_ref="schema://result@1",
    )
    assert store.append_result(result) == store.append_result(result)
    assert len(store.results_for("run", "dynamic_stage", plan.plan_id, 1)) == 1
    assert store.results_for("run", "dynamic_stage", "sha256:" + "0" * 64, 1) == ()
    assert store.results_for("run", "dynamic_stage", plan.plan_id, 999) == ()


def validator_context(stage_binding):
    from framework.harness.task_plan import TaskPlanValidationContext

    return TaskPlanValidationContext(
        run_id="run",
        stage_binding=stage_binding,
        available_input_refs=("document",),
        registered_gate_refs=("gate@1",),
    )


class _InspectionAuthorizer:
    def __init__(self, authorized: bool) -> None:
        self.authorized = authorized
        self.calls = 0

    def authorize(self, request: TaskPlanInspectionRequest) -> TaskPlanInspectionDecision:
        self.calls += 1
        if self.authorized:
            return TaskPlanInspectionDecision(True, "authz://task-plan/read")
        return TaskPlanInspectionDecision(False, denial_reason_code="tenant_scope_denied")


class _CountingStore(InMemoryTaskPlanStore):
    def __init__(self) -> None:
        super().__init__()
        self.load_calls = 0

    def load_projection(self, run_id: str, stage_id: str):
        self.load_calls += 1
        return super().load_projection(run_id, stage_id)


class _HaltFailingStore(InMemoryTaskPlanStore):
    def __init__(self) -> None:
        super().__init__()
        self.halt_attempts = 0

    def append_event(self, event: TaskPlanEvent) -> str:
        if event.event_type == "TASK_PLAN_HALTED":
            self.halt_attempts += 1
            raise OSError("event store unavailable")
        return super().append_event(event)


class _SimulatedProcessCrash(BaseException):
    pass


class _RetryEventCrashStore(InMemoryTaskPlanStore):
    def __init__(self) -> None:
        super().__init__()
        self.crash_on_retry_event = True

    def commit_event(self, event, projection):
        if self.crash_on_retry_event and event.event_type == "TASK_RETRY_SCHEDULED":
            self.crash_on_retry_event = False
            raise _SimulatedProcessCrash()
        return super().commit_event(event, projection)


def test_runner_recovers_failed_result_before_retry_event_without_redispatching_attempt_one():
    graph, policy, registry = _setup()
    candidate = _candidate(
        graph,
        (
            replace(
                _task("a"),
                retry_policy={
                    "max_attempts": 2,
                    "retryable_reason_codes": ("transport",),
                },
            ),
        ),
    )
    store = _RetryEventCrashStore()
    verifier = _FailOnceResultVerifier(failure_code="transport")
    calls: list[str] = []

    def execute(_binding, request):
        calls.append(request.task_id)
        return HarnessWorkerResult(status="succeeded", output={"value": request.task_id})

    request = TaskPlanStageRequest(
        run_id="run",
        stage_binding=graph,
        context_refs={"document": "document"},
        policy=policy,
        policy_ref=policy.exact_ref,
        candidate=candidate,
        accepted_at="2026-08-01T00:00:00Z",
    )
    runner = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
        result_verifier=verifier,
        worker_executor=execute,
    )

    with pytest.raises(_SimulatedProcessCrash):
        runner.run(request)

    failed_projection = store.load_projection("run", "dynamic_stage")
    assert failed_projection.tasks[0].status is TaskLifecycle.FAILED
    assert failed_projection.tasks[0].failure_reason_code == "transport"
    assert "TASK_RETRY_SCHEDULED" not in {
        event.event_type for event in store.read_events("run", "dynamic_stage")
    }
    assert calls == ["a"]

    recovered = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
        result_verifier=verifier,
        worker_executor=execute,
    ).run(request)

    assert recovered.status.value == "succeeded"
    assert calls == ["a", "a"]
    assert any(
        event.event_type == "TASK_RETRY_SCHEDULED"
        for event in store.read_events("run", "dynamic_stage")
    )


def test_parallel_runner_retries_with_a_new_attempt_in_the_same_dispatch_group():
    graph, policy, registry = _setup()
    policy = replace(
        policy,
        capability_capacity=2,
        available_concurrency_reservations=2,
    )
    candidate = _candidate(
        graph,
        (
            replace(
                _task("a"),
                retry_policy={
                    "max_attempts": 2,
                    "retryable_reason_codes": ("transport",),
                },
            ),
        ),
    )
    store = InMemoryTaskPlanStore()
    verifier = _FailOnceResultVerifier(failure_code="transport")
    attempts: list[tuple[str, int, str]] = []

    def execute(_binding, instance, identity):
        assert identity == _execution_identity(candidate)
        attempts.append(
            (
                instance.task_id,
                instance.attempt,
                instance.task_instance_id,
            )
        )
        return HarnessWorkerResult(
            status="succeeded",
            output={"value": instance.task_id},
        )

    request = TaskPlanStageRequest(
        run_id="run",
        stage_binding=graph,
        context_refs={"document": "document"},
        policy=policy,
        policy_ref=policy.exact_ref,
        candidate=candidate,
        accepted_at="2026-08-01T00:00:00Z",
    )
    runner = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
        result_verifier=verifier,
        worker_executor=execute,
        parallel_coordinator=ParallelAgentCoordinator(
            max_workers=2,
            allow_test_executor=True,
        ),
    )

    result = runner.run(replace(request, execution_identity=_execution_identity(candidate)))

    assert result.status.value == "succeeded"
    assert [attempt for _task_id, attempt, _instance_id in attempts] == [1, 2]
    assert attempts[0][2] != attempts[1][2]
    events = store.read_events("run", "dynamic_stage")
    assert [event.event_type for event in events].count("TASK_RETRY_SCHEDULED") == 1
    wave_admissions = [
        event
        for event in events
        if event.event_type == "TASK_WAVE_ADMITTED"
    ]
    assert len(wave_admissions) == 2
    assert {event.payload["wave"]["group_id"] for event in wave_admissions} == {
        event.payload["group"]["group_id"]
        for event in events
        if event.event_type == "TASK_GROUP_ADMITTED"
    }
    assert any(event.event_type == "TASK_GROUP_JOINED" for event in events)


def test_parallel_policy_without_coordinator_fails_closed_even_when_serial_fallback_is_enabled():
    graph, policy, registry = _setup()
    policy = replace(
        policy,
        capability_capacity=2,
        available_concurrency_reservations=2,
        serial_fallback=True,
    )
    candidate = _candidate(graph, (_task("a"),))
    calls: list[str] = []

    def execute(_binding, request):
        calls.append(request.task_id)
        return HarnessWorkerResult(status="succeeded", output={"value": request.task_id})

    result = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=InMemoryTaskPlanStore(),
        result_verifier=_AcceptingResultVerifier(),
        worker_executor=execute,
    ).run(
        TaskPlanStageRequest(
            run_id="run",
            stage_binding=graph,
            context_refs={"document": "document"},
            policy=policy,
            policy_ref=policy.exact_ref,
            candidate=candidate,
            accepted_at="2026-08-01T00:00:00Z",
        )
    )

    assert result.status.value == "blocked"
    assert result.diagnostics["reason_code"] == "TASK_GROUP_WAVE_ADAPTER_REQUIRED"
    assert calls == []


def test_authorized_inspection_exposes_only_task_plan_control_projection() -> None:
    graph, policy, registry = _setup()
    candidate = _candidate(graph, (_task("a"),))
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        registry,
        context=validator_context(graph),
        accepted_at="2026-08-01T00:00:00Z",
    )
    store = _CountingStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    authorizer = _InspectionAuthorizer(True)

    inspection = TaskPlanInspectionService(
        store=store,
        authorizer=authorizer,
    ).inspect(
        TaskPlanInspectionRequest(
            run_id="run",
            stage_id="dynamic_stage",
            principal_id="operator",
            tenant_id="tenant",
            authentication_evidence_ref="authn://operator/session",
        ),
        replay=TaskPlanReplayVerdict(True, canonical_payload_checksum({"replay": "ok"})),
    ).to_dict()

    assert authorizer.calls == 1
    assert store.load_calls == 1
    assert inspection["current_plan"]["plan_checksum"] == plan.plan_checksum
    assert inspection["candidate_refs"] == [candidate.candidate_checksum]
    assert inspection["tasks"] == [
        {
            "task_id": "a",
            "status": "pending",
            "depends_on": [],
            "attempts": 0,
            "active_instance_id": None,
            "worker_ref": "cap-worker@1",
            "worker_capability": "cap",
            "output_role": "role",
            "output_schema_ref": "schema://result@1",
            "result_ref": None,
            "result_checksum": None,
            "failure_reason_code": None,
        }
    ]
    assert "payload" not in str(inspection)


def test_unauthorized_inspection_fails_before_reading_task_plan_store() -> None:
    store = _CountingStore()
    authorizer = _InspectionAuthorizer(False)
    service = TaskPlanInspectionService(store=store, authorizer=authorizer)

    with pytest.raises(HarnessValidationError) as captured:
        service.inspect(
            TaskPlanInspectionRequest(
                run_id="run",
                stage_id="dynamic_stage",
                principal_id="operator",
                tenant_id="tenant",
                authentication_evidence_ref="authn://operator/session",
            )
        )

    assert captured.value.code == "task_plan_inspection_unauthorized"
    assert authorizer.calls == 1
    assert store.load_calls == 0


def test_task_plan_metrics_and_trace_are_low_cardinality_and_payload_free() -> None:
    graph, policy, registry = _setup()
    candidate = _candidate(graph, (_task("a"),))
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        registry,
        context=validator_context(graph),
        accepted_at="2026-08-01T00:00:00Z",
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    events = store.read_events("run", "dynamic_stage")

    samples = task_plan_metric_samples(
        store.load_projection("run", "dynamic_stage"),
        plan,
        events,
        replay_verified=True,
    )
    trace = task_plan_trace_events(events)

    assert all("run_id" not in sample.labels for sample in samples)
    assert any(sample.name == "harness_task_plan_replay_verification" for sample in samples)
    assert all("payload" not in item.to_dict() for item in trace)
    with pytest.raises(HarnessValidationError) as captured:
        TaskPlanMetricSample("bad", 1, {"run_id": "run"})
    assert captured.value.code == "task_plan_metric_label_rejected"


def test_stage_runner_rejects_policy_drift_before_recording_patch() -> None:
    graph, policy, registry = _setup()
    candidate = _candidate(graph, (_task("a"),))
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        registry,
        context=validator_context(graph),
        accepted_at="2026-08-01T00:00:00Z",
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    events_before = store.read_events("run", "dynamic_stage")
    projection_before = store.load_projection("run", "dynamic_stage")
    drifted_policy = replace(policy, max_depth=policy.max_depth + 1)
    patch = PlanPatch.for_plan(
        plan,
        patch_id="policy-drift",
        reason_code="repair",
        source_candidate_ref="candidate://policy-drift",
        operations=(
            PlanPatchOperation(
                PlanPatchOperationType.SKIP_PENDING_TASK,
                target_task_id="a",
            ),
        ),
    )
    runner = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
    )
    request = TaskPlanStageRequest(
        run_id="run",
        stage_binding=graph,
        context_refs={"document": "document"},
        policy=drifted_policy,
        policy_ref=drifted_policy.exact_ref,
        accepted_at="2026-08-01T00:01:00Z",
    )

    with pytest.raises(HarnessValidationError) as error:
        runner.apply_patch(request, patch)

    assert error.value.code == "task_plan_policy_mismatch"
    assert store.read_events("run", "dynamic_stage") == events_before
    assert store.load_projection("run", "dynamic_stage") == projection_before


def test_stage_request_rejects_required_role_drift_from_frozen_graph() -> None:
    stage_binding, policy, _ = _setup()
    drifted_policy = replace(
        policy,
        allowed_output_roles=("other-role",),
        required_output_roles=("other-role",),
    )

    with pytest.raises(HarnessValidationError) as error:
        TaskPlanStageRequest(
            run_id="run",
            stage_binding=stage_binding,
            context_refs={"document": "document"},
            policy=drifted_policy,
            accepted_at="2026-08-01T00:01:00Z",
        )

    assert error.value.code == "task_plan_policy_mismatch"


def test_stage_runner_rejects_existing_plan_from_another_frozen_graph() -> None:
    stage_binding, policy, registry = _setup()
    candidate = _candidate(stage_binding, (_task("a"),))
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        registry,
        context=validator_context(stage_binding),
        accepted_at="2026-08-01T00:00:00Z",
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    events_before = store.read_events("run", "dynamic_stage")
    alternate_binding = build_task_plan_stage_binding(
        graph_id="graph",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
        metadata_overrides={"graph_revision": "2"},
    )
    request = TaskPlanStageRequest(
        run_id="run",
        stage_binding=alternate_binding,
        context_refs={"document": "document"},
        policy=policy,
        accepted_at="2026-08-01T00:01:00Z",
    )
    patch = PlanPatch.for_plan(
        plan,
        patch_id="wrong-frozen-graph",
        reason_code="repair",
        source_candidate_ref="candidate://wrong-frozen-graph",
        operations=(
            PlanPatchOperation(
                PlanPatchOperationType.SKIP_PENDING_TASK,
                target_task_id="a",
            ),
        ),
    )

    with pytest.raises(HarnessValidationError) as error:
        TaskPlanStageRunner(
            candidate_builder=FakePlanCandidateBuilder(candidate),
            capability_registry=registry,
            store=store,
        ).apply_patch(request, patch)

    assert error.value.code == "task_plan_pinned_version_mismatch"
    assert store.read_events("run", "dynamic_stage") == events_before


def test_stage_runner_fails_closed_when_halt_event_cannot_be_persisted() -> None:
    graph, policy, registry = _setup()
    candidate = _candidate(
        graph,
        (_task("a", depends_on=("b",)), _task("b", depends_on=("a",))),
    )
    store = _HaltFailingStore()
    runner = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
    )
    request = TaskPlanStageRequest(
        run_id="run",
        stage_binding=graph,
        context_refs={"document": "document"},
        policy=policy,
        policy_ref=policy.exact_ref,
        candidate=candidate,
        accepted_at="2026-08-01T00:01:00Z",
    )

    with pytest.raises(HarnessValidationError) as error:
        runner.run(request)

    assert error.value.code == "task_plan_halt_persistence_failed"
    assert error.value.details["run_id"] == "run"
    assert error.value.details["stage_id"] == "dynamic_stage"
    assert error.value.details["reason_code"] == "task_plan_candidate_rejected"
    assert store.halt_attempts == 1
    assert not {
        "TASK_PLAN_HALTED",
        "STAGE_OUTPUT_AGGREGATED",
        "TASK_PLAN_VERIFIED",
    } & {event.event_type for event in store.read_events("run", "dynamic_stage")}
