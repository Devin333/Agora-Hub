from copy import deepcopy
from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan import TaskBudget, TaskPlanCheckpoint, TaskPlanReplayReducer, TaskPlanValidator
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger, result_budget_usage
from framework.harness.control_plane.budget_reservation import BudgetReservation
from framework.harness.task_plan.budget_settlement import BudgetSettlementReceipt
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.parallel import child_budget_reservation
from framework.harness.task_plan.queue import TaskPlanQueueProjection
from framework.harness.task_plan.scheduler import task_instance_for_attempt
from tests.framework.harness.task_plan.test_budget_ledger import _failed
from tests.framework.harness.task_plan.test_durable_task_plan_store import _ArtifactStore, _EventStore, _start, _store
from tests.framework.harness.task_plan.test_task_plan_runtime import _candidate, _setup, _task, validator_context


def _allocation(**overrides):
    fields = dict(max_turns=2, max_tool_calls=3, max_memory_ops=2, max_output_tokens=10, token_limit=100, time_limit_ms=1000, cost_limit=500)
    fields.update(overrides)
    return TaskBudget(**fields)


def _plan(task_ids=("a", "b"), *, budget=None, parent=None):
    roles = tuple(f"role_{task_id}" for task_id in task_ids)
    capabilities = tuple(f"cap_{task_id}" for task_id in task_ids)
    graph, policy, registry = _setup(roles=roles, capabilities=capabilities)
    budget = budget or _allocation()
    parent = parent or budget.plus(budget).plus(budget).plus(budget)
    policy = replace(policy, per_task_budget=budget, aggregate_task_budget=parent)
    tasks = tuple(
        replace(_task(task_id, capability, role), budget_request=budget,
                retry_policy={"max_attempts": 2, "retryable_reason_codes": ["worker_failed"]})
        for task_id, capability, role in zip(task_ids, capabilities, roles, strict=True)
    )
    candidate = _candidate(graph, tasks, roles=roles)
    plan = TaskPlanValidator().accept(candidate, policy, registry, context=validator_context(graph), accepted_at="2026-09-09T00:00:00Z")
    return candidate, plan


def _usage(**overrides):
    value = dict(turns=1, tool_calls=2, memory_ops=0, output_tokens=4, tokens=20, time_ms=400, cost_microusd=150)
    value.update(overrides)
    return value


def _receipt(instance, **overrides):
    fields = dict(
        reservation_key=instance.idempotency_key, instance_checksum=instance.instance_checksum,
        source_receipt_checksum="sha256:" + "a" * 64, reason_code="CANCELLED",
        usage=_usage(), termination_confirmed=True,
    )
    fields.update(overrides)
    return BudgetSettlementReceipt(**fields)


class _ReceiptAuthority:
    def __init__(self, receipt):
        self.receipt = receipt

    def read_budget_settlement(self, *, source_receipt_checksum, reservation_key):
        if (source_receipt_checksum, reservation_key) == (self.receipt.source_receipt_checksum, self.receipt.reservation_key):
            return self.receipt
        return None


def _settle_terminated(ledger, instance, receipt):
    return ledger.settle_terminated(instance, receipt=receipt, authority=_ReceiptAuthority(receipt))


def _assert_invariants(ledger):
    counters = ledger.counters()
    for name, limit in ledger.parent_allocation.items():
        assert sum(counters[f"{state}_{name}"] for state in ("consumed", "released", "reserved")) <= limit
    assert TaskPlanBudgetLedger.from_snapshot(ledger.snapshot()) == ledger


def test_full_allocation_is_explicit_and_cost_is_optional_not_zero_by_default():
    budget = _allocation()
    assert TaskBudget.from_dict(budget.to_dict()) == budget
    unpriced = _allocation(cost_limit=None)
    assert "cost_limit" not in unpriced.to_dict()
    assert _allocation(cost_limit=0).to_dict()["cost_limit"] == 0
    assert "cost_limit" in unpriced.exceeds(budget)
    assert "time_limit_ms" in TaskBudget().exceeds(budget)
    with pytest.raises(HarnessValidationError, match="dimensions"):
        budget.plus(unpriced)


@pytest.mark.parametrize("changes", [
    {"token_limit": None}, {"time_limit_ms": None}, {"time_limit_ms": 0},
    {"cost_limit": -1}, {"cost_limit": 0.1}, {"token_limit": True},
    {"token_limit": 9}, {"time_limit_ms": float("inf")},
])
def test_invalid_partial_or_float_budget_is_rejected(changes):
    with pytest.raises(HarnessValidationError):
        _allocation(**changes)


def test_batch_reservation_and_settlement_use_all_dimensions_once():
    _, plan = _plan()
    instances = tuple(task_instance_for_attempt(plan, task.task_id, 1) for task in plan.tasks)
    initial = TaskPlanBudgetLedger.for_plan(plan)
    admitted = initial.reserve(instances)
    assert initial.ledger_version == 0
    assert admitted.reserve(instances) is admitted
    assert admitted.counters()["reserved_time_limit_ms"] == 2000
    result = _failed(plan, instances[0], usage=_usage())
    settled = admitted.settle(result)
    assert settled.settle(result) is settled
    record = settled.records[instances[0].idempotency_key]
    assert record["consumed"]["token_limit"] == 20
    assert record["consumed"]["time_limit_ms"] == 400
    assert record["consumed"]["cost_limit"] == 150
    assert record["released"]["token_limit"] == 80
    assert record["released"]["time_limit_ms"] == 600
    assert record["released"]["cost_limit"] == 350
    _assert_invariants(settled)


@pytest.mark.parametrize("dimension", ["token_limit", "time_limit_ms", "cost_limit"])
def test_batch_exhaustion_leaves_no_partial_reservation(dimension):
    _, plan = _plan()
    instances = tuple(task_instance_for_attempt(plan, task.task_id, 1) for task in plan.tasks)
    initial = TaskPlanBudgetLedger.for_plan(plan)
    limits = dict(initial.parent_allocation)
    limits[dimension] = instances[0].budget_snapshot.to_dict()[dimension]
    initial = replace(initial, parent_allocation=limits)
    before = initial.snapshot()
    with pytest.raises(HarnessValidationError, match="exceed"):
        initial.reserve(instances)
    assert initial.snapshot() == before
    _assert_invariants(initial)


@pytest.mark.parametrize("reason", ["CANCELLED", "RECLAIMED", "RECOVERED", "BUDGET_EXCEEDED"])
def test_confirmed_lifecycle_settlement_keeps_receipt_and_new_retry_identity(reason):
    _, plan = _plan(("a",))
    first = task_instance_for_attempt(plan, "a", 1)
    retry = task_instance_for_attempt(plan, "a", 2)
    initial = TaskPlanBudgetLedger.for_plan(plan).reserve((first,))
    receipt = _receipt(first, reason_code=reason)
    settled = _settle_terminated(initial, first, receipt)
    assert _settle_terminated(settled, first, receipt) is settled
    assert settled.records[first.idempotency_key]["status"] == "TERMINATED"
    assert settled.records[first.idempotency_key]["settlement_receipt"] == receipt.to_dict()
    with pytest.raises(HarnessValidationError):
        _settle_terminated(settled, first, replace(receipt, source_receipt_checksum="sha256:" + "b" * 64))
    with pytest.raises(HarnessValidationError):
        _settle_terminated(settled, first, replace(receipt, usage=_usage(time_ms=401)))
    with pytest.raises(HarnessValidationError):
        initial.reserve((retry,))
    retried = settled.reserve((retry,))
    assert retried.records[first.idempotency_key] == settled.records[first.idempotency_key]
    assert retried.counters()["reserved_time_limit_ms"] == 1000
    _assert_invariants(retried)


def test_unconfirmed_termination_or_wrong_attempt_retains_outstanding_charge():
    _, plan = _plan(("a",))
    instance = task_instance_for_attempt(plan, "a", 1)
    initial = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    before = initial.snapshot()
    receipt = _receipt(instance, termination_confirmed=False)
    with pytest.raises(HarnessValidationError, match="confirmed"):
        _settle_terminated(initial, instance, receipt)
    with pytest.raises(HarnessValidationError, match="differs"):
        _settle_terminated(initial, replace(instance, budget_snapshot=_allocation(cost_limit=499)), replace(receipt, termination_confirmed=True))
    assert initial.snapshot() == before


@pytest.mark.parametrize("usage", [
    _usage(tokens=101), _usage(time_ms=1001), _usage(cost_microusd=501),
    _usage(cost_microusd=0.1), _usage(time_ms=True),
    _usage(tokens=3), _usage(time_limit_ms=401),
])
def test_usage_overrun_or_conflicting_alias_does_not_release_budget(usage):
    with pytest.raises(HarnessValidationError):
        result_budget_usage(usage, _allocation().to_dict())


def test_missing_usage_charges_full_allocation_and_unstarted_release_is_idempotent():
    _, plan = _plan(("a",))
    instance = task_instance_for_attempt(plan, "a", 1)
    initial = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    assert result_budget_usage({}, instance.budget_snapshot.to_dict()) == instance.budget_snapshot.to_dict()
    released = initial.release_unstarted(instance.task_instance_id, "a", 1, reason_code="cancel-before-spawn")
    assert released.release_unstarted(instance.task_instance_id, "a", 1, reason_code="cancel-before-spawn") is released
    for name, amount in instance.budget_snapshot.to_dict().items():
        assert released.counters()[f"released_{name}"] == amount
        assert released.counters()[f"consumed_{name}"] == 0
        assert released.counters()[f"reserved_{name}"] == 0
    _assert_invariants(released)


def test_child_receipt_is_a_projection_of_the_exact_ledger_record():
    _, plan = _plan(("a",))
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    payload = child_budget_reservation(ledger, instance, group_id="group", wave_id="wave")
    reservation = BudgetReservation.from_dict(payload)
    assert reservation.attempt_allocation == instance.budget_snapshot.to_dict()
    assert reservation.parent_allocation == ledger.parent_allocation
    assert payload["token_limit"] == 100
    assert payload["time_limit_ms"] == 1000
    assert payload["tool_call_limit"] == 3
    assert payload["cost_limit"] == 500
    assert payload["cost_unit"] == "USD_MICRO"
    for field in ("token_limit", "time_limit_ms", "tool_call_limit", "remaining_tokens"):
        corrupt = deepcopy(payload)
        corrupt[field] += 1
        corrupt["reservation_checksum"] = canonical_payload_checksum({k: v for k, v in corrupt.items() if k != "reservation_checksum"})
        with pytest.raises(HarnessValidationError):
            BudgetReservation.from_dict(corrupt)


def test_resigned_numeric_projection_cannot_substitute_float_or_boolean():
    budget = _allocation(max_tool_calls=1)
    reservation = BudgetReservation("run:stage:group", "op", budget.to_dict(), budget.to_dict(), 1)
    for wrong in (True, 1.0):
        payload = reservation.to_dict()
        payload["tool_call_limit"] = wrong
        payload["reservation_checksum"] = canonical_payload_checksum({key: value for key, value in payload.items() if key != "reservation_checksum"})
        with pytest.raises(HarnessValidationError):
            BudgetReservation.from_dict(payload)


@pytest.mark.parametrize("tamper", ["partition", "revision", "receipt", "schema", "dimensions"])
def test_resigned_lifecycle_history_still_rejects_invalid_facts(tamper):
    _, plan = _plan(("a",))
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = _settle_terminated(TaskPlanBudgetLedger.for_plan(plan).reserve((instance,)), instance, _receipt(instance, reason_code="RECOVERED"))
    snapshot = ledger.snapshot()
    raw = snapshot["ledger"]
    record = raw["records"][instance.idempotency_key]
    if tamper == "partition":
        record["released"]["time_limit_ms"] = 599
    elif tamper == "revision":
        record["settled_revision"] = 1
    elif tamper == "receipt":
        record["settlement_receipt"] = None
    elif tamper == "schema":
        raw["schema_version"] = "agora.task-plan-budget-ledger/v1"
    else:
        record["consumed"].pop("cost_limit")
    raw["ledger_checksum"] = canonical_payload_checksum({key: value for key, value in raw.items() if key != "ledger_checksum"})
    with pytest.raises(HarnessValidationError):
        TaskPlanBudgetLedger.from_snapshot(snapshot)


@pytest.mark.parametrize("priced", [False, True])
def test_durable_settlement_is_atomic_and_replays_all_dimensions_without_live_calls(priced, monkeypatch):
    from framework.harness.task_plan.stage import TaskPlanStageRunner

    def no_live_calls(*args, **kwargs):
        pytest.fail("offline budget replay invoked a worker")

    monkeypatch.setattr(TaskPlanStageRunner, "_invoke", no_live_calls)
    candidate, plan = _plan(("a",), budget=_allocation(cost_limit=500 if priced else None))
    events, artifacts = _EventStore(fail_on_event_type="TASK_FAILED"), _ArtifactStore()
    store = _store(events, artifacts)
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, "a")
    result = _failed(plan, instance, usage=_usage())
    before = store.load_projection(plan.run_id, plan.stage_id)
    with pytest.raises(RuntimeError, match="injected batch failure"):
        store.append_result(result)
    assert _store(events, artifacts).load_projection(plan.run_id, plan.stage_id) == before
    events.fail_on_event_type = None
    store.append_result(result)
    reopened = _store(events, artifacts)
    projection = reopened.load_projection(plan.run_id, plan.stage_id)
    history = reopened.read_events(plan.run_id, plan.stage_id)
    reopened.append_result(result)
    assert reopened.read_events(plan.run_id, plan.stage_id) == history
    report = TaskPlanReplayReducer().replay(
        (plan,), history,
        results=reopened.result_history_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version),
    )
    assert report.projection == projection
    ledger = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
    assert ledger.records[instance.idempotency_key]["consumed"] == result_budget_usage(_usage(), instance.budget_snapshot.to_dict())
    assert ledger.counters()["released_time_limit_ms"] == 600
    assert ("consumed_cost_limit" in ledger.counters()) is priced
    checkpoint = TaskPlanCheckpoint.from_replay("execution-budget", plan, report, created_at="2026-09-09T00:00:00Z")
    assert TaskPlanCheckpoint.from_dict(checkpoint.to_dict()).budget_snapshot == projection.consumed_budget
    _assert_invariants(ledger)


def test_queue_roundtrip_retains_exact_versioned_budget_identity():
    _, plan = _plan(("a",))
    instance = task_instance_for_attempt(plan, "a", 1)
    queue = TaskPlanQueueProjection.for_instance(instance, queue_name="research")
    assert TaskPlanQueueProjection.from_dict(queue.to_dict()) == queue
    assert TaskPlanQueueProjection.from_task(queue.to_task()).task_instance == instance


def test_configured_execution_policy_rejects_missing_budget_dimensions():
    graph, policy, registry = _setup()
    policy = replace(policy, per_task_budget=_allocation(), aggregate_task_budget=_allocation().plus(_allocation()))
    candidate = _candidate(graph, (_task("a"),))
    outcome = TaskPlanValidator().validate(candidate, policy=policy, capabilities=registry, context=validator_context(graph))
    assert not outcome.accepted
    assert "task_budget_exceeded" in {item.code for item in outcome.diagnostics}
    with pytest.raises(HarnessValidationError) as error:
        replace(policy, aggregate_task_budget=TaskBudget(8, 12, 8, 40))
    assert error.value.code == "invalid_task_plan_budget_policy"


@pytest.mark.parametrize("serial_fallback", [False, True])
def test_missing_execution_policy_fails_before_group_event_or_child_spawn(serial_fallback):
    from framework.harness.subagents.supervisor import ChildAgentSupervisor
    from framework.harness.task_plan.parallel import ParallelAgentCoordinator
    from tests.framework.harness.task_plan.test_parallel_orchestration import _request

    graph, policy, registry = _setup()
    plan = TaskPlanValidator().accept(
        _candidate(graph, (_task("a"),)), policy, registry,
        context=validator_context(graph), accepted_at="2026-09-09T00:00:00Z",
    )
    events = []
    def no_worker(_):
        pytest.fail("missing budget policy reached a child worker")
    supervisor = ChildAgentSupervisor(max_children=2, worker_factory=no_worker)
    coordinator = ParallelAgentCoordinator(max_workers=2, child_supervisor=supervisor, event_sink=events.append)
    with pytest.raises(HarnessValidationError) as error:
        coordinator.create_group(replace(_request(plan), serial_fallback=serial_fallback))
    assert error.value.code == "task_plan_budget_policy_missing"
    assert events == []
    assert supervisor.events.events == []


def test_released_attempt_does_not_refund_group_envelope_for_retry():
    _, plan = _plan(("a",), parent=_allocation())
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = _settle_terminated(
        TaskPlanBudgetLedger.for_plan(plan).reserve((instance,)), instance, _receipt(instance),
    )
    with pytest.raises(HarnessValidationError, match="exceed"):
        ledger.reserve((task_instance_for_attempt(plan, "a", 2),))
    assert ledger.ledger_version == 2
    _assert_invariants(ledger)


def test_supervisor_rejects_resigned_invalid_receipt_with_its_public_error_type():
    from tests.framework.harness.subagents.test_child_agent_supervisor import _request

    _, plan = _plan(("a",))
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    payload = child_budget_reservation(ledger, instance, group_id="group", wave_id="wave")
    assert _request(budget=payload).budget == payload
    payload["token_limit"] += 1
    payload["reservation_checksum"] = canonical_payload_checksum({k: v for k, v in payload.items() if k != "reservation_checksum"})
    with pytest.raises(ValueError, match="invalid versioned child budget"):
        _request(budget=payload)


def test_supervisor_cost_capacity_and_recovery_do_not_round_large_integers():
    from framework.harness.subagents.supervisor import ChildAgentAdmissionError, ChildAgentSupervisor
    from tests.framework.harness.subagents.test_child_agent_supervisor import _request

    amount = 2**53 + 1
    attempt = _allocation(cost_limit=amount)
    parent = attempt.plus(attempt).plus(attempt).plus(attempt)
    parent = replace(parent, cost_limit=2 * amount - 1)

    def receipt(key):
        return BudgetReservation("run:stage:group", key, parent.to_dict(), attempt.to_dict(), 1).to_dict()

    events = []
    supervisor = ChildAgentSupervisor(max_children=3, event_sink=events.append)
    first = supervisor.spawn(_request(child_id="budget-a", operation_id="op-a", budget=receipt("op-a")))
    supervisor.complete(first.child_id, operation_id="op-a", output={"candidate": "ok"})
    recovered = ChildAgentSupervisor(events=type(supervisor.events)(events), max_children=3)
    recovered.recover()
    for runtime in (supervisor, recovered):
        with pytest.raises(ChildAgentAdmissionError) as error:
            runtime.spawn(_request(child_id="budget-b", operation_id="op-b", budget=receipt("op-b")))
        assert error.value.code == "child_budget_exhausted"


def test_supervisor_pins_zero_cost_and_failed_admission_does_not_change_limits():
    from framework.harness.subagents.supervisor import ChildAgentAdmissionError, ChildAgentSupervisor
    from tests.framework.harness.subagents.test_child_agent_supervisor import _request

    zero = _allocation(cost_limit=0)
    parent = zero.plus(zero)
    supervisor = ChildAgentSupervisor(max_children=3)

    def request(key, attempt, envelope):
        budget = BudgetReservation("run:stage:group", key, envelope.to_dict(), attempt.to_dict(), 1).to_dict()
        return _request(child_id=key, operation_id=key, budget=budget)

    supervisor.spawn(request("first", zero, parent))
    # A conflicting new cost allocation cannot silently replace the zero cap.
    with pytest.raises(ChildAgentAdmissionError) as error:
        supervisor.spawn(request("conflict", _allocation(cost_limit=1), replace(parent, cost_limit=1)))
    assert error.value.code == "child_budget_conflict"
    supervisor.spawn(request("second", zero, parent))


@pytest.mark.parametrize("cost", [None, 0, 500])
def test_spawn_event_schema_accepts_typed_budget_and_rejects_missing_time(cost):
    from jsonschema import Draft202012Validator
    from framework.events.schema.catalog import default_event_schema_catalog

    _, plan = _plan(("a",), budget=_allocation(cost_limit=cost))
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    payload = child_budget_reservation(ledger, instance, group_id="group", wave_id="wave")
    catalog = default_event_schema_catalog()
    registration = catalog.get("TASK_ATTEMPT_SPAWN_INTENT", catalog.current_schema("TASK_ATTEMPT_SPAWN_INTENT"))
    schema = registration.json_schema["properties"]["details"]["properties"]["budget_reservation"]
    validator = Draft202012Validator(schema)
    validator.validate(payload)
    legacy = {**payload, "schema_version": "agora.harness-budget-reservation/v1"}
    assert list(validator.iter_errors(legacy))
    corrupt = deepcopy(payload)
    corrupt["attempt_allocation"]["unknown_budget"] = 1
    assert list(validator.iter_errors(corrupt))
    payload.pop("time_limit_ms")
    assert list(validator.iter_errors(payload))


def test_settlement_receipt_binds_first_settlement_to_exact_attempt_and_usage():
    _, plan = _plan(("a",))
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    receipt = _receipt(instance)
    assert BudgetSettlementReceipt.from_dict(receipt.to_dict()) == receipt
    corrupt = receipt.to_dict()
    corrupt["usage"]["time_ms"] = 0
    with pytest.raises(HarnessValidationError, match="checksum"):
        BudgetSettlementReceipt.from_dict(corrupt)
    for changed in (
        replace(receipt, instance_checksum="sha256:" + "b" * 64),
        replace(receipt, reservation_key="another-attempt"),
    ):
        with pytest.raises(HarnessValidationError, match="differs"):
            _settle_terminated(ledger, instance, changed)
    assert ledger.ledger_version == 1


def test_self_reported_usage_cannot_override_authenticated_lifecycle_evidence():
    _, plan = _plan(("a",))
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    receipt = _receipt(instance)
    authority = _ReceiptAuthority(receipt)
    for forged in (
        replace(receipt, usage=_usage(tokens=5, time_ms=0, cost_microusd=0)),
        replace(receipt, reason_code="RECOVERED"),
        replace(receipt, source_receipt_checksum="sha256:" + "b" * 64),
    ):
        with pytest.raises(HarnessValidationError) as error:
            ledger.settle_terminated(instance, receipt=forged, authority=authority)
        assert error.value.code == "task_plan_budget_receipt_unverified"
    with pytest.raises(HarnessValidationError) as error:
        ledger.settle_terminated(instance, receipt=receipt, authority=None)
    assert error.value.code == "task_plan_budget_authority_missing"
    settled = ledger.settle_terminated(instance, receipt=receipt, authority=authority)
    assert settled.records[instance.idempotency_key]["consumed"]["cost_limit"] == 150
    assert ledger.ledger_version == 1


def test_total_token_measurement_bounds_missing_output_measurement():
    assert result_budget_usage({"tokens": 3}, _allocation().to_dict())["max_output_tokens"] == 3


@pytest.mark.parametrize("parent", [None, [], "budget", {"max_turns": True}])
def test_malformed_snapshot_parent_is_a_typed_validation_error(parent):
    with pytest.raises(HarnessValidationError):
        TaskPlanBudgetLedger.from_snapshot({"ledger": {"parent_allocation": parent}})


def test_zero_token_and_cost_envelope_cannot_consume_either_dimension():
    budget = _allocation(max_output_tokens=0, token_limit=0, cost_limit=0)
    usage = _usage(output_tokens=0, tokens=0, cost_microusd=0)
    assert result_budget_usage(usage, budget.to_dict())["token_limit"] == 0
    for dimension in ("tokens", "cost_microusd"):
        with pytest.raises(HarnessValidationError) as error:
            result_budget_usage({**usage, dimension: 1}, budget.to_dict())
        assert error.value.code == "BUDGET_EXCEEDED"
