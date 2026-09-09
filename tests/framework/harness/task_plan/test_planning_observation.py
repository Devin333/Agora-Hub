from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

import framework.harness.task_plan.planning_observation as planning_module
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.planning_observation import (
    HarnessPlanningObservationService,
    InMemoryPlanningObservationStore,
    JsonlPlanningObservationStore,
    PlanningObservationPolicy,
    PlanningObservationReceipt,
    PlanningObservationRequest,
)
from framework.tool import ToolDefinition, ToolExecutor, ToolRegistry, ToolSideEffect, ToolStatus


def _service(*, tool_name: str = "research.lookup", side_effect: str = "read_only"):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=tool_name,
            version="1.0.0",
            side_effect=side_effect,
            concurrency_safe=True,
            input_schema={"type": "object"},
        ),
        lambda arguments: {"answer": arguments["query"]},
    )
    store = InMemoryPlanningObservationStore()
    policy = PlanningObservationPolicy(
        policy_checksum="sha256:" + "1" * 64,
        allowed_tool_ids=(f"{tool_name}@1.0.0",),
        max_tool_calls=1,
        timeout_seconds=2,
    )
    return (
        HarnessPlanningObservationService(
            executor=ToolExecutor(registry),
            registry=registry,
            store=store,
            policy=policy,
        ),
        store,
        policy,
    )


def _request(policy_checksum: str, *, tool_name: str = "research.lookup") -> PlanningObservationRequest:
    return PlanningObservationRequest(
        request_id="planning-request-1",
        run_id="run-1",
        stage_id="stage-1",
        planner_turn_id="turn-1",
        policy_checksum=policy_checksum,
        correlation_id="corr-1",
        tool_name=tool_name,
        purpose="look up a read-only fact",
        arguments={"query": "paper"},
    )


def test_planning_observation_persists_receipt_and_replay_never_executes() -> None:
    service, store, policy = _service()
    request = _request(policy.policy_checksum)
    receipt = service.observe(request)
    assert receipt.status == "SUCCEEDED"
    assert receipt.source_ref.startswith("planning-observation://")
    assert store.by_request(request.request_checksum) == receipt

    class _ExplodingExecutor:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("replay must not invoke a live tool")

    service._executor = _ExplodingExecutor()  # type: ignore[attr-defined]
    assert service.replay(request) == receipt


def test_planning_observation_denies_unallowlisted_and_side_effect_tools_before_execution() -> None:
    service, _store, policy = _service(tool_name="research.write", side_effect="writes_local_state")
    receipt = service.observe(_request(policy.policy_checksum, tool_name="research.write"))
    assert receipt.status == "REJECTED"
    assert receipt.reason_code == "planning_tool_not_read_only"

    service, _store, policy = _service()
    request = _request(policy.policy_checksum, tool_name="research.unknown")
    receipt = service.observe(request)
    assert receipt.status == "REJECTED"
    assert receipt.reason_code == "planning_tool_unavailable"


def test_planning_observation_enforces_policy_scope_and_budget() -> None:
    service, store, policy = _service()
    request = _request(policy.policy_checksum)
    receipt = service.observe(request)
    assert service.observe(request) == receipt

    second = PlanningObservationRequest(
        request_id="planning-request-2",
        run_id=request.run_id,
        stage_id=request.stage_id,
        planner_turn_id=request.planner_turn_id,
        policy_checksum=request.policy_checksum,
        correlation_id="corr-2",
        tool_name=request.tool_name,
        purpose=request.purpose,
        arguments=request.arguments,
    )
    denied = service.observe(second)
    assert denied.status == "REJECTED"
    assert denied.reason_code == "planning_tool_budget_exhausted"

    with pytest.raises(HarnessValidationError, match="outside candidate scope"):
        service.validate_source_refs(
            (receipt.source_ref,),
            run_id="other-run",
            stage_id=request.stage_id,
            planner_turn_id=request.planner_turn_id,
            policy_checksum=policy.policy_checksum,
        )


def test_planning_observation_replay_requires_durable_receipt() -> None:
    service, _store, policy = _service()
    with pytest.raises(HarnessValidationError) as exc_info:
        service.replay(_request(policy.policy_checksum))
    assert exc_info.value.code == "planning_observation_receipt_missing"


def test_planning_observation_policy_defaults_to_denied() -> None:
    service, _store, _policy = _service()
    denied_policy = PlanningObservationPolicy(
        policy_checksum="sha256:" + "2" * 64,
    )
    service = HarnessPlanningObservationService(
        executor=service._executor,  # type: ignore[attr-defined]
        registry=service._registry,  # type: ignore[attr-defined]
        store=InMemoryPlanningObservationStore(),
        policy=denied_policy,
    )
    receipt = service.observe(_request(denied_policy.policy_checksum))
    assert receipt.status == "REJECTED"
    assert receipt.reason_code == "planning_tool_not_allowlisted"


def test_planning_observation_jsonl_store_restores_integrity_checked_receipt(tmp_path) -> None:
    service, _store, policy = _service()
    receipt = service.observe(_request(policy.policy_checksum))
    path = tmp_path / "planning-receipts.jsonl"
    durable = JsonlPlanningObservationStore(path)
    durable.save(receipt)

    restored = JsonlPlanningObservationStore(path)
    assert restored.by_source_ref(receipt.source_ref) == receipt


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("change", ["unknown", "missing", "checksum"])
def test_receipt_parser_rejects_noncanonical_envelopes(nested, change) -> None:
    service, _, policy = _service()
    receipt = service.observe(_request(policy.policy_checksum))
    payload = receipt.to_dict()
    target = payload["request"] if nested else payload
    if change == "unknown":
        target["unexpected"] = True
    elif change == "missing":
        del target["schema_version"]
    else:
        target["request_checksum"] = "sha256:" + "0" * 64
    with pytest.raises(HarnessValidationError) as error:
        PlanningObservationReceipt.from_dict(payload)
    assert error.value.code == "planning_observation_receipt_corrupt"


def test_failed_executor_attempt_is_durable_and_consumes_budget(monkeypatch, tmp_path) -> None:
    service, _, policy = _service()
    request = _request(policy.policy_checksum)
    path = tmp_path / "failed-planning.jsonl"
    service._store = JsonlPlanningObservationStore(path)
    calls = []

    def fail(call, _policy):
        calls.append(call)
        raise RuntimeError("executor failed after receiving the call")

    monkeypatch.setattr(service._executor, "execute", fail)
    receipt = service.observe(request)
    assert receipt.status == "FAILED"
    assert receipt.tool_call_id == calls[0].call_id
    service._store = JsonlPlanningObservationStore(path)
    assert service.observe(request) == receipt
    assert service.replay(request) == receipt
    denied = service.observe(replace(request, request_id="retry-2", attempt=2))
    assert denied.reason_code == "planning_tool_budget_exhausted"
    assert len(calls) == 1


def test_late_success_is_timeout_and_cannot_be_candidate_evidence(monkeypatch) -> None:
    service, _, policy = _service()
    request = _request(policy.policy_checksum)
    execute = service._executor.execute

    def late_success(call, tool_policy):
        observation = execute(call, tool_policy)
        assert observation.status is ToolStatus.SUCCEEDED
        return replace(observation, elapsed_ms=policy.timeout_seconds * 1000 + 1)

    monkeypatch.setattr(service._executor, "execute", late_success)
    receipt = service.observe(request)
    assert receipt.status == "TIMED_OUT"
    assert receipt.reason_code == "planning_tool_timeout"
    assert receipt.result_checksum is None
    assert service.replay(request) == receipt
    with pytest.raises(HarnessValidationError):
        service.validate_source_refs(
            (receipt.source_ref,), run_id=request.run_id, stage_id=request.stage_id,
            planner_turn_id=request.planner_turn_id, policy_checksum=policy.policy_checksum,
        )


def test_harness_measures_deadline_independently_of_tool_report(monkeypatch) -> None:
    service, _, policy = _service()
    ticks = iter((100.0, 103.0))
    monkeypatch.setattr(planning_module, "perf_counter", lambda: next(ticks))
    receipt = service.observe(_request(policy.policy_checksum))
    assert receipt.status == "TIMED_OUT"
    assert receipt.elapsed_ms == 3000
    assert receipt.result_checksum is None


def test_wrong_call_observation_is_recorded_as_failed_attempt(monkeypatch) -> None:
    service, _, policy = _service()
    execute = service._executor.execute
    calls = []

    def wrong_call(call, tool_policy):
        calls.append(call)
        observation = execute(call, tool_policy)
        return replace(observation, call=replace(call, call_id="unrelated-call"))

    monkeypatch.setattr(service._executor, "execute", wrong_call)
    request = _request(policy.policy_checksum)
    receipt = service.observe(request)
    assert receipt.status == "FAILED"
    assert receipt.reason_code == "planning_tool_observation_mismatch"
    assert receipt.tool_call_id == calls[0].call_id
    denied = service.observe(replace(request, request_id="request-2"))
    assert denied.reason_code == "planning_tool_budget_exhausted"
    assert len(calls) == 1


def test_sources_must_match_service_pinned_policy() -> None:
    service, store, policy = _service()
    request = _request(policy.policy_checksum)
    receipt = service.observe(request)
    changed_policy = replace(policy, policy_checksum="sha256:" + "2" * 64)
    changed_service = HarnessPlanningObservationService(
        executor=service._executor, registry=service._registry,
        store=store, policy=changed_policy,
    )
    with pytest.raises(HarnessValidationError) as error:
        changed_service.validate_source_refs(
            (receipt.source_ref,), run_id=request.run_id, stage_id=request.stage_id,
            planner_turn_id=request.planner_turn_id, policy_checksum=policy.policy_checksum,
        )
    assert error.value.code == "planning_policy_checksum_mismatch"


def test_unreadable_commit_is_not_exposed_as_success(monkeypatch) -> None:
    service, store, policy = _service()
    monkeypatch.setattr(store, "save", lambda receipt: receipt.receipt_checksum)
    with pytest.raises(HarnessValidationError) as error:
        service.observe(_request(policy.policy_checksum))
    assert error.value.code == "planning_observation_receipt_corrupt"


@pytest.mark.parametrize("read_path", ["observe", "replay", "source"])
def test_read_paths_reject_receipt_with_mutated_checksum(read_path) -> None:
    service, store, policy = _service()
    request = _request(policy.policy_checksum)
    receipt = service.observe(request)
    original_ref = receipt.source_ref
    stored = store.by_request(request.request_checksum)
    assert stored is not None
    object.__setattr__(stored, "receipt_checksum", "sha256:" + "0" * 64)
    with pytest.raises(HarnessValidationError) as error:
        if read_path == "source":
            service.validate_source_refs(
                (original_ref,), run_id=request.run_id, stage_id=request.stage_id,
                planner_turn_id=request.planner_turn_id, policy_checksum=policy.policy_checksum,
            )
        else:
            getattr(service, read_path)(request)
    assert error.value.code == "planning_observation_receipt_corrupt"


def test_crash_after_intent_preserves_budget_and_never_reexecutes(monkeypatch, tmp_path) -> None:
    service, _, policy = _service()
    request = _request(policy.policy_checksum)
    path = tmp_path / "planning-intents.jsonl"
    service._store = JsonlPlanningObservationStore(path)
    calls = []

    def crash(call, _policy):
        calls.append(call)
        raise SystemExit("simulated process exit before receipt")

    monkeypatch.setattr(service._executor, "execute", crash)
    with pytest.raises(SystemExit):
        service.observe(request)
    service._store = JsonlPlanningObservationStore(path)
    with pytest.raises(HarnessValidationError) as error:
        service.observe(request)
    assert error.value.code == "planning_call_outcome_unconfirmed"
    denied = service.observe(replace(request, request_id="next-request", attempt=2))
    assert denied.reason_code == "planning_tool_budget_exhausted"
    assert len(calls) == 1


def test_jsonl_instances_atomically_reserve_shared_budget(tmp_path) -> None:
    path = tmp_path / "shared-intents.jsonl"
    stores = [JsonlPlanningObservationStore(path) for _ in range(2)]
    request = _request("sha256:" + "1" * 64)

    def reserve(index):
        return bool(stores[index].reserve_call(replace(request, request_id=f"request-{index}"), f"call-{index}", 1))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert sorted(results) == [False, True]


def test_same_planner_turn_shares_deadline_across_calls_and_restart(monkeypatch, tmp_path):
    service, _, policy = _service()
    service._policy = replace(policy, max_tool_calls=3)
    request = _request(policy.policy_checksum)
    clock = {"ms": 100000}
    monkeypatch.setattr(planning_module, "time_ns", lambda: clock["ms"] * 1_000_000)
    path = tmp_path / "bounded-turn.jsonl"
    service._store = JsonlPlanningObservationStore(path)
    execute = service._executor.execute
    timeouts = []

    def record(call, tool_policy):
        timeouts.append(tool_policy.timeout_seconds_default)
        return execute(call, tool_policy)

    monkeypatch.setattr(service._executor, "execute", record)
    first = service.observe(request)
    assert first.status == "SUCCEEDED"
    clock["ms"] += 1000
    service._store = JsonlPlanningObservationStore(path)
    second = service.observe(replace(request, request_id="second"))
    assert second.status == "SUCCEEDED"
    assert timeouts == [2.0, 1.0]
    clock["ms"] += 1001
    with pytest.raises(HarnessValidationError) as error:
        service.observe(replace(request, request_id="third"))
    assert error.value.code == "planning_turn_timeout"
    assert len(timeouts) == 2
    assert service.replay(request) == first
