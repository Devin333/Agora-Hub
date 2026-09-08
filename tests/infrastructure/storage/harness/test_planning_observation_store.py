from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from framework.events.canonical import checksum_for
from framework.harness.ref_authority import RefAccessPolicy, RefDescriptor
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from framework.harness.task_plan.planning_metadata import (
    PlanningObservationConflictError,
    PlanningObservationCorruptError,
    PlanningObservationDescriptor,
    PlanningObservationDescriptorPort,
    PlanningObservationIncompleteError,
    PlanningObservationStorageError,
)
from framework.harness.task_plan.planning_observation import (
    PlanningObservationReceipt,
    PlanningObservationRequest,
    PlanningObservationStorePort,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.storage.harness import planning_observation as storage_module
from infrastructure.storage.harness.planning_observation import (
    FilesystemPlanningObservationStore,
)


def _snapshot() -> RefAuthoritySnapshot:
    execution = GraphExecutionIdentity(
        run_id="run-1",
        graph_id="research.graph",
        graph_version="1",
        graph_ref="research.graph@1",
        graph_checksum=checksum_for("research graph"),
        node_id="analysis",
        node_instance_id="analysis-node-1",
        activity_id="analysis-activity-1",
        attempt=1,
    )
    descriptor = RefDescriptor(
        ref="document",
        run_id=execution.run_id,
        stage_id="analysis",
        tenant_id="tenant-1",
        owner_id="research-owner",
        access_mode="READ_ONLY",
        artifact_type="graph_input",
        source_checksum=checksum_for("document payload"),
        ref_kind="input",
        scope="SHARED_READ_ONLY",
    )
    return RefAuthoritySnapshot(
        execution_identity=execution,
        stage_id="analysis",
        stage_binding_checksum=checksum_for("analysis binding"),
        task_policy_checksum=checksum_for("task policy"),
        source_checksum=checksum_for("input set"),
        policy=RefAccessPolicy(
            policy_id="research-input-refs",
            version="1",
            run_id=execution.run_id,
            stage_id="analysis",
            tenant_id="tenant-1",
            owner_id="research-owner",
            allowed_refs=(descriptor.ref,),
            allowed_artifact_types=(descriptor.artifact_type,),
            allowed_ref_kinds=(descriptor.ref_kind,),
            shared_read_only_refs=(descriptor.ref,),
            pinned_checksums={descriptor.ref: descriptor.source_checksum},
        ),
        descriptors=(descriptor,),
    )


def _request(
    snapshot: RefAuthoritySnapshot,
    *,
    request_id: str = "planning-request-1",
    planner_turn_id: str = "planner-turn-1",
    attempt: int = 1,
) -> PlanningObservationRequest:
    return PlanningObservationRequest(
        request_id=request_id,
        run_id=snapshot.run_id,
        stage_id=snapshot.stage_id,
        planner_turn_id=planner_turn_id,
        policy_checksum=snapshot.task_policy_checksum,
        correlation_id=f"{request_id}-correlation",
        tool_name="research.lookup",
        purpose="look up immutable evidence",
        arguments={"query": "private query must remain outside metadata"},
        attempt=attempt,
    )


def _receipt(
    snapshot: RefAuthoritySnapshot,
    *,
    request_id: str = "planning-request-1",
    planner_turn_id: str = "planner-turn-1",
    attempt: int = 1,
    summary: str = "private observation must remain outside metadata",
) -> PlanningObservationReceipt:
    return PlanningObservationReceipt(
        request=_request(
            snapshot,
            request_id=request_id,
            planner_turn_id=planner_turn_id,
            attempt=attempt,
        ),
        status="SUCCEEDED",
        tool_call_id=f"{request_id}-call",
        observation_summary=summary,
        artifact_refs=(f"artifact://{request_id}",),
        result_checksum=checksum_for({"request_id": request_id, "summary": summary}),
        elapsed_ms=7,
    )


def _paths(root: Path) -> tuple[Path, Path]:
    return next(root.rglob("po_*.json")), next(root.rglob("po_*.meta"))


def test_descriptor_is_exact_metadata_only_and_checksum_bound() -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    payload = FilesystemPlanningObservationStore._payload_bytes(receipt)
    descriptor = PlanningObservationDescriptor.for_receipt(
        receipt,
        input_snapshot_ref=snapshot.snapshot_ref,
        payload_checksum=f"sha256:{sha256(payload).hexdigest()}",
        payload_size_bytes=len(payload),
    )

    assert set(descriptor.to_dict()) == {
        "schema_version",
        "input_snapshot_ref",
        "request_checksum",
        "run_id",
        "stage_id",
        "planner_turn_id",
        "policy_checksum",
        "request_id",
        "attempt",
        "source_ref",
        "receipt_checksum",
        "status",
        "tool_call_id",
        "artifact_refs",
        "payload_checksum",
        "payload_size_bytes",
        "receipt_schema",
        "descriptor_checksum",
    }
    assert "arguments" not in descriptor.to_dict()
    assert "observation_summary" not in descriptor.to_dict()
    assert PlanningObservationDescriptor.from_dict(descriptor.to_dict()) == descriptor

    tampered = descriptor.to_dict()
    tampered["status"] = "FAILED"
    with pytest.raises(PlanningObservationCorruptError):
        PlanningObservationDescriptor.from_dict(tampered)


def test_store_roundtrip_reopen_and_both_ports(tmp_path: Path) -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    store = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)

    assert isinstance(store, PlanningObservationStorePort)
    assert isinstance(store, PlanningObservationDescriptorPort)
    assert store.save(receipt) == receipt.receipt_checksum
    assert store.by_request(receipt.request.request_checksum) == receipt
    assert store.by_source_ref(receipt.source_ref) == receipt

    reopened = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    descriptor = reopened.describe_request(receipt.request.request_checksum)
    assert descriptor is not None
    assert descriptor.input_snapshot_ref == snapshot.snapshot_ref
    assert descriptor.request_checksum == receipt.request.request_checksum
    assert descriptor.receipt_checksum == receipt.receipt_checksum
    assert reopened.describe_ref(receipt.source_ref) == descriptor
    assert reopened.descriptors_for_scope(
        snapshot.run_id,
        snapshot.stage_id,
        receipt.request.planner_turn_id,
    ) == (descriptor,)


def test_shared_base_root_keeps_input_snapshots_physically_isolated(
    tmp_path: Path,
) -> None:
    first_snapshot = _snapshot()
    second_snapshot = replace(
        first_snapshot,
        source_checksum=checksum_for("different admitted input set"),
    )
    receipt = _receipt(first_snapshot)
    first = FilesystemPlanningObservationStore(
        tmp_path,
        input_snapshot=first_snapshot,
    )
    second = FilesystemPlanningObservationStore(
        tmp_path,
        input_snapshot=second_snapshot,
    )

    first.save(receipt)
    second.save(receipt)

    first_descriptor = first.describe_ref(receipt.source_ref)
    second_descriptor = second.describe_ref(receipt.source_ref)
    assert first_descriptor is not None
    assert second_descriptor is not None
    assert first_descriptor.input_snapshot_ref == first_snapshot.snapshot_ref
    assert second_descriptor.input_snapshot_ref == second_snapshot.snapshot_ref
    assert first_descriptor != second_descriptor
    assert len(tuple(tmp_path.rglob("po_*.meta"))) == 2


def test_descriptor_queries_never_open_payload_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    store = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    store.save(receipt)
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path.suffix == ".json":
            raise AssertionError("descriptor lookup must not open receipt payload")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    descriptor = store.describe_request(receipt.request.request_checksum)
    assert descriptor is not None
    assert store.describe_ref(receipt.source_ref) == descriptor
    assert store.descriptors_for_scope(
        snapshot.run_id,
        snapshot.stage_id,
        receipt.request.planner_turn_id,
    ) == (descriptor,)


def test_payload_corruption_is_still_detected_by_receipt_read(tmp_path: Path) -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    store = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    store.save(receipt)
    payload_path, _ = _paths(tmp_path)
    content = payload_path.read_bytes()
    marker = b"private observation"
    assert marker in content
    payload_path.write_bytes(content.replace(marker, b"tampered observatio", 1))

    assert store.describe_request(receipt.request.request_checksum) is not None
    with pytest.raises(PlanningObservationCorruptError, match="checksum"):
        store.by_request(receipt.request.request_checksum)


def test_descriptor_tampering_is_rejected_before_payload_read(tmp_path: Path) -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    store = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    store.save(receipt)
    _, metadata_path = _paths(tmp_path)
    content = metadata_path.read_text(encoding="utf-8")
    assert '"status":"SUCCEEDED"' in content
    metadata_path.write_text(
        content.replace('"status":"SUCCEEDED"', '"status":"REJECTED"'),
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(PlanningObservationCorruptError):
        store.describe_request(receipt.request.request_checksum)


@pytest.mark.parametrize("missing_suffix", (".meta", ".json"))
def test_one_sided_commit_fails_closed_across_reopen(
    tmp_path: Path,
    missing_suffix: str,
) -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    store = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    store.save(receipt)
    payload_path, metadata_path = _paths(tmp_path)
    {".json": payload_path, ".meta": metadata_path}[missing_suffix].unlink()

    reopened = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    with pytest.raises(PlanningObservationIncompleteError):
        reopened.describe_request(receipt.request.request_checksum)
    with pytest.raises(PlanningObservationIncompleteError):
        reopened.descriptors_for_scope(
            snapshot.run_id,
            snapshot.stage_id,
            receipt.request.planner_turn_id,
        )


def test_crash_after_metadata_commit_is_not_treated_as_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    store = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    atomic_create = storage_module.verified_atomic_create
    calls = 0

    def fail_before_payload(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected crash before payload commit")
        return atomic_create(*args, **kwargs)

    monkeypatch.setattr(storage_module, "verified_atomic_create", fail_before_payload)
    with pytest.raises(PlanningObservationStorageError, match="commit failed"):
        store.save(receipt)
    assert len(tuple(tmp_path.rglob("po_*.meta"))) == 1
    assert not tuple(tmp_path.rglob("po_*.json"))

    reopened = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    with pytest.raises(PlanningObservationIncompleteError):
        reopened.describe_request(receipt.request.request_checksum)
    with pytest.raises(PlanningObservationIncompleteError):
        reopened.save(receipt)


def test_identical_concurrent_saves_reuse_one_immutable_pair(tmp_path: Path) -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    stores = tuple(
        FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
        for _ in range(8)
    )
    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = tuple(pool.map(lambda item: item.save(receipt), stores))

    assert results == (receipt.receipt_checksum,) * len(stores)
    assert len(tuple(tmp_path.rglob("po_*.meta"))) == 1
    assert len(tuple(tmp_path.rglob("po_*.json"))) == 1


def test_identical_cross_process_saves_reuse_one_immutable_pair(
    tmp_path: Path,
) -> None:
    script = """
import sys
from infrastructure.storage.harness.planning_observation import FilesystemPlanningObservationStore
from tests.infrastructure.storage.harness.test_planning_observation_store import _receipt, _snapshot

snapshot = _snapshot()
receipt = _receipt(snapshot)
store = FilesystemPlanningObservationStore(sys.argv[1], input_snapshot=snapshot)
print(store.save(receipt))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[4])
    processes = tuple(
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=Path(__file__).parents[4],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    )
    outputs = tuple(process.communicate(timeout=20) for process in processes)
    receipt = _receipt(_snapshot())

    for process, (stdout, stderr) in zip(processes, outputs, strict=True):
        assert process.returncode == 0, stderr
        assert stdout.strip() == receipt.receipt_checksum
    assert len(tuple(tmp_path.rglob("po_*.meta"))) == 1
    assert len(tuple(tmp_path.rglob("po_*.json"))) == 1


def test_same_request_with_different_receipt_is_an_idempotency_conflict(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    first = _receipt(snapshot, summary="first immutable observation")
    changed = _receipt(snapshot, summary="other immutable observation")
    assert first.request == changed.request
    store = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    store.save(first)

    with pytest.raises(PlanningObservationConflictError) as error:
        store.save(changed)
    assert error.value.code == "planning_observation_idempotency_conflict"
    assert store.by_request(first.request.request_checksum) == first


@pytest.mark.parametrize("changed_field", ("run_id", "stage_id", "policy_checksum"))
def test_receipt_must_match_the_admitted_input_snapshot(
    tmp_path: Path,
    changed_field: str,
) -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    changed_request = replace(
        receipt.request,
        **{
            changed_field: (
                checksum_for("other policy")
                if changed_field == "policy_checksum"
                else f"other-{changed_field}"
            )
        },
    )
    changed_receipt = replace(receipt, request=changed_request)
    store = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)

    with pytest.raises(PlanningObservationConflictError) as error:
        store.save(changed_receipt)
    assert error.value.code == "planning_observation_scope_mismatch"
    assert not tuple(tmp_path.rglob("po_*.meta"))


def test_store_requires_an_input_admission_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot()
    planning_snapshot = replace(
        snapshot,
        phase=RefSnapshotPhase.PLANNING_OBSERVATION,
        parent_snapshot_ref=snapshot.snapshot_ref,
    )
    with pytest.raises(ValueError, match="input admission"):
        FilesystemPlanningObservationStore(
            tmp_path,
            input_snapshot=planning_snapshot,
        )


def test_receipt_and_metadata_size_are_checked_before_commit(tmp_path: Path) -> None:
    snapshot = _snapshot()
    receipt = _receipt(snapshot)
    store = FilesystemPlanningObservationStore(
        tmp_path,
        input_snapshot=snapshot,
        max_receipt_bytes=128,
    )

    with pytest.raises(PlanningObservationStorageError) as error:
        store.save(receipt)
    assert error.value.code == "planning_observation_receipt_size_exceeded"
    assert not tuple(tmp_path.rglob("po_*.meta"))
    assert not tuple(tmp_path.rglob("po_*.json"))


def test_scope_query_is_stable_and_bounded(tmp_path: Path) -> None:
    snapshot = _snapshot()
    second = _receipt(snapshot, request_id="planning-request-2", attempt=2)
    first = _receipt(snapshot)
    store = FilesystemPlanningObservationStore(tmp_path, input_snapshot=snapshot)
    store.save(second)
    store.save(first)

    assert tuple(
        item.request_id
        for item in store.descriptors_for_scope(
            snapshot.run_id,
            snapshot.stage_id,
            "planner-turn-1",
        )
    ) == ("planning-request-1", "planning-request-2")
    bounded = FilesystemPlanningObservationStore(
        tmp_path,
        input_snapshot=snapshot,
        max_descriptors=1,
    )
    with pytest.raises(PlanningObservationStorageError) as error:
        bounded.descriptors_for_scope(snapshot.run_id, snapshot.stage_id, "planner-turn-1")
    assert error.value.code == "planning_observation_query_limit_exceeded"
