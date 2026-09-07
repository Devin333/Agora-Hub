from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from framework.events.canonical import checksum_for
from framework.harness.artifacts import (
    ArtifactCatalogPort,
    ArtifactReferenceDescriptorPort,
)
from framework.harness.artifacts.catalog import (
    ArtifactCatalogGcAction,
    ArtifactCatalogGcDetachRequest,
    ArtifactCatalogGcReason,
    ArtifactCatalogIdentity,
    ArtifactCatalogRegistrationRequest,
    ArtifactLifecycleAuthorization,
    ArtifactLifecycleAuthorityKind,
    ArtifactLogicalReference,
    ArtifactReferenceKind,
    ArtifactReferenceRetirementReason,
    ArtifactReferenceRetirementRequest,
    ArtifactVerificationReceipt,
)
from framework.harness.runtime import (
    ArtifactClass,
    ArtifactRecord,
    GraphArtifactResultError,
    GraphArtifactResultErrorCode,
    ResultSensitivity,
    RetentionClass,
)
from infrastructure.storage.artifacts import (
    CATALOG_SCHEMA_VERSION,
    LocalJsonArtifactCatalog,
)


NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
CHECKSUM = "sha256:" + "a" * 64


def _record(
    *,
    tenant_id: str = "tenant-1",
    run_id: str = "run-1",
    artifact_id: str = "artifact-1",
    content_checksum: str = CHECKSUM,
    expires_at: datetime | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        ref=f"artifact://{run_id}/{artifact_id}",
        artifact_id=artifact_id,
        artifact_type="node_result",
        content_checksum=content_checksum,
        byte_size=17,
        media_type="application/json",
        artifact_class=ArtifactClass.EVIDENCE,
        tenant_id=tenant_id,
        run_id=run_id,
        graph_id="research-graph",
        node_id="collect-evidence",
        attempt_id="attempt-1",
        producer_revision="research-worker@abc123",
        sensitivity=ResultSensitivity.INTERNAL,
        reusable=False,
        dependency_digest=None,
        retention_class=RetentionClass.EVIDENCE,
        expires_at=expires_at or NOW + timedelta(days=30),
        required_for_replay=False,
        required_for_publication=False,
        created_at=NOW,
    )


def _request(record: ArtifactRecord) -> ArtifactCatalogRegistrationRequest:
    return ArtifactCatalogRegistrationRequest.from_verified_record(
        record,
        verified_at=NOW + timedelta(seconds=1),
    )


def _state_path(root: Path) -> Path:
    return root / "catalog.json"


def _retirement_request(
    logical_reference: ArtifactLogicalReference,
    *,
    requested_at: datetime,
) -> ArtifactReferenceRetirementRequest:
    authorization = ArtifactLifecycleAuthorization.create(
        kind=ArtifactLifecycleAuthorityKind.TERMINAL_RUN,
        tenant_id=logical_reference.tenant_id,
        owner_run_id=logical_reference.owner_run_id,
        owner_id=logical_reference.owner_id,
        lifecycle_ref=f"run-lifecycle://{logical_reference.owner_run_id}/terminal",
        observed_at=requested_at - timedelta(seconds=1),
        policy_version="graph-artifact-policy@1",
    )
    return ArtifactReferenceRetirementRequest.create(
        reference=logical_reference,
        authorization=authorization,
        reason=ArtifactReferenceRetirementReason.RETENTION_EXPIRED,
        requested_at=requested_at,
    )


def _read_state(root: Path) -> dict:
    return json.loads(_state_path(root).read_text(encoding="utf-8"))


def _write_state(root: Path, state: dict) -> None:
    unsigned = {key: value for key, value in state.items() if key != "state_checksum"}
    state["state_checksum"] = checksum_for(unsigned)
    _state_path(root).write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def test_adapter_satisfies_port_and_deduplicates_across_runs_after_restart(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    first = catalog.register(_request(_record(run_id="run-1", artifact_id="artifact-a")))
    second = catalog.register(_request(_record(run_id="run-2", artifact_id="artifact-b")))

    assert isinstance(catalog, ArtifactCatalogPort)
    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.entry == first.entry
    assert second.entry.record.ref == "artifact://run-1/artifact-a"
    assert catalog.get_by_ref(
        tenant_id="tenant-1",
        ref="artifact://run-1/artifact-a",
    ) == first.entry
    assert catalog.find_by_checksum(tenant_id="tenant-1", content_checksum=CHECKSUM) == (first.entry,)
    assert catalog.list_by_run(tenant_id="tenant-1", run_id="run-1") == (first.entry,)
    assert catalog.list_by_run(tenant_id="tenant-1", run_id="run-2") == (first.entry,)

    restarted = LocalJsonArtifactCatalog(tmp_path)
    assert restarted.get(first.entry.entry_id) == first.entry
    assert len(restarted.list_references(first.entry.entry_id)) == 2
    state = _read_state(tmp_path)
    assert state["schema_version"] == CATALOG_SCHEMA_VERSION
    assert len(state["entries"]) == 1
    assert len(state["claims"]) == 2
    assert len(state["references"]) == 2
    with pytest.raises(GraphArtifactResultError) as missing_ref:
        catalog.get_by_ref(
            tenant_id="tenant-1",
            ref="artifact://run-2/artifact-b",
        )
    assert missing_ref.value.error_code is (
        GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND
    )
    with pytest.raises(GraphArtifactResultError) as wrong_tenant:
        catalog.get_by_ref(
            tenant_id="tenant-2",
            ref="artifact://run-1/artifact-a",
        )
    assert wrong_tenant.value.error_code is (
        GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND
    )


def test_catalog_describes_exact_trusted_artifact_metadata(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    record = _record()
    catalog.register(_request(record))

    descriptor = catalog.describe_artifact_ref(
        record.ref,
        expected_run_id="run-1",
        expected_tenant_id="tenant-1",
    )

    assert isinstance(catalog, ArtifactReferenceDescriptorPort)
    assert descriptor.ref == record.ref
    assert descriptor.run_id == record.run_id
    assert descriptor.artifact_type == record.artifact_type
    assert descriptor.checksum == record.content_checksum
    assert descriptor.byte_size == record.byte_size
    assert descriptor.media_type == record.media_type
    assert descriptor.tenant_id == record.tenant_id
    assert descriptor.graph_id == record.graph_id
    assert descriptor.node_id == record.node_id
    assert descriptor.attempt_id == record.attempt_id
    assert descriptor.identity_scope_ref is None
    assert descriptor.subject_scope_ref is None
    assert not hasattr(descriptor, "payload")


def test_catalog_descriptor_rejects_wrong_ownership(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    record = _record()
    catalog.register(_request(record))

    with pytest.raises(GraphArtifactResultError) as wrong_run:
        catalog.describe_artifact_ref(
            record.ref,
            expected_run_id="run-2",
            expected_tenant_id="tenant-1",
        )
    assert wrong_run.value.error_code is GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH

    with pytest.raises(GraphArtifactResultError) as wrong_tenant:
        catalog.describe_artifact_ref(
            record.ref,
            expected_run_id="run-1",
            expected_tenant_id="tenant-2",
        )
    assert wrong_tenant.value.error_code is (
        GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND
    )

    with pytest.raises(GraphArtifactResultError) as missing_tenant:
        catalog.describe_artifact_ref(
            record.ref,
            expected_run_id="run-1",
        )
    assert missing_tenant.value.error_code is (
        GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH
    )


def test_catalog_descriptor_rejects_corrupt_state_checksum(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    record = _record()
    catalog.register(_request(record))
    state = _read_state(tmp_path)
    state["state_checksum"] = checksum_for({"tampered": True})
    _state_path(tmp_path).write_text(
        json.dumps(state, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(GraphArtifactResultError) as raised:
        catalog.describe_artifact_ref(
            record.ref,
            expected_run_id="run-1",
            expected_tenant_id="tenant-1",
        )
    assert raised.value.error_code is GraphArtifactResultErrorCode.ARTIFACT_CATALOG_CORRUPT


def test_catalog_descriptor_rejects_missing_catalog_record(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)

    with pytest.raises(GraphArtifactResultError) as raised:
        catalog.describe_artifact_ref(
            "artifact://run-1/artifact-1",
            expected_run_id="run-1",
            expected_tenant_id="tenant-1",
        )
    assert raised.value.error_code is (
        GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND
    )


def test_same_logical_identity_different_checksum_fails_without_mutation(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    original = catalog.register(_request(_record()))
    before = _state_path(tmp_path).read_bytes()

    with pytest.raises(GraphArtifactResultError) as exc_info:
        catalog.register(
            _request(_record(content_checksum="sha256:" + "b" * 64))
        )

    assert exc_info.value.error_code is GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT
    assert _state_path(tmp_path).read_bytes() == before
    assert catalog.get(original.entry.entry_id) == original.entry


def test_same_logical_identity_is_idempotent_across_new_verification_time(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    record = _record()
    first = catalog.register(_request(record))
    later = catalog.register(
        ArtifactCatalogRegistrationRequest.from_verified_record(
            record,
            verified_at=NOW + timedelta(hours=1),
        )
    )

    assert later.entry == first.entry
    assert later.claim == first.claim
    assert later.reference == first.reference
    assert later.deduplicated is True
    assert len(catalog.list_references(first.entry.entry_id)) == 1


def test_same_logical_identity_cannot_change_metadata_after_commit(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    record = _record()
    catalog.register(_request(record))
    changed = replace(
        record,
        artifact_class=ArtifactClass.REPORT,
        retention_class=RetentionClass.REPORT,
        expires_at=None,
        required_for_publication=True,
    )
    before = _state_path(tmp_path).read_bytes()

    with pytest.raises(GraphArtifactResultError) as exc_info:
        catalog.register(_request(changed))

    assert exc_info.value.error_code is GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT
    assert _state_path(tmp_path).read_bytes() == before


def test_reconciliation_filters_physical_drift_to_explicit_tenant(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    tenant_one = catalog.register(
        _request(
            _record(
                tenant_id="tenant-1",
                run_id="run-tenant-1",
                artifact_id="artifact-tenant-1",
                content_checksum="sha256:" + "1" * 64,
            )
        )
    )
    tenant_two = catalog.register(
        _request(
            _record(
                tenant_id="tenant-2",
                run_id="run-tenant-2",
                artifact_id="artifact-tenant-2",
                content_checksum="sha256:" + "2" * 64,
            )
        )
    )

    reconciled = catalog.reconcile(
        now=NOW,
        tenant_id="tenant-1",
        physical_inventory=(),
    )

    assert reconciled.is_clean is False
    assert {issue.entry_id for issue in reconciled.issues} == {
        tenant_one.entry.entry_id
    }
    assert tenant_two.entry.entry_id not in json.dumps(reconciled.to_dict())


def test_reference_scope_and_protected_reference_removal_guard(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    registered = catalog.register(_request(_record()))
    evidence_ref = ArtifactLogicalReference.create(
        entry_id=registered.entry.entry_id,
        tenant_id="tenant-1",
        owner_run_id="run-2",
        owner_id="evidence-pack-1",
        kind=ArtifactReferenceKind.EVIDENCE,
        created_at=NOW + timedelta(seconds=2),
    )

    assert catalog.add_reference(evidence_ref) == evidence_ref
    assert catalog.add_reference(evidence_ref) == evidence_ref
    assert catalog.list_by_run(tenant_id="tenant-1", run_id="run-2") == (registered.entry,)
    with pytest.raises(GraphArtifactResultError) as exc_info:
        catalog.add_reference(replace(evidence_ref, tenant_id="tenant-2"))
    assert exc_info.value.error_code is GraphArtifactResultErrorCode.ARTIFACT_REFERENCE_CONFLICT
    with pytest.raises(GraphArtifactResultError) as remove_scope:
        catalog.remove_reference(
            tenant_id="tenant-2",
            reference_id=evidence_ref.reference_id,
        )
    assert remove_scope.value.error_code is GraphArtifactResultErrorCode.ARTIFACT_SCOPE_MISMATCH
    with pytest.raises(GraphArtifactResultError) as lifecycle:
        catalog.remove_reference(
            tenant_id="tenant-1",
            reference_id=evidence_ref.reference_id,
        )
    assert lifecycle.value.error_code is (
        GraphArtifactResultErrorCode.LIFECYCLE_AUTHORIZATION_INVALID
    )


def test_expiring_reference_can_be_removed_idempotently(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    registered = catalog.register(_request(_record()))
    ephemeral = ArtifactLogicalReference.create(
        entry_id=registered.entry.entry_id,
        tenant_id="tenant-1",
        owner_run_id="run-1",
        owner_id="temporary-1",
        kind=ArtifactReferenceKind.EPHEMERAL,
        created_at=NOW + timedelta(seconds=2),
        expires_at=NOW + timedelta(minutes=1),
    )
    catalog.add_reference(ephemeral)

    assert catalog.remove_reference(
        tenant_id="tenant-1",
        reference_id=ephemeral.reference_id,
    ) is True
    assert catalog.remove_reference(
        tenant_id="tenant-1",
        reference_id=ephemeral.reference_id,
    ) is False


def test_gc_plan_protects_replay_then_marks_expired_unreferenced_entry(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    registered = catalog.register(
        _request(_record(expires_at=NOW + timedelta(days=1)))
    )
    replay_ref = ArtifactLogicalReference.create(
        entry_id=registered.entry.entry_id,
        tenant_id="tenant-1",
        owner_run_id="run-1",
        owner_id="artifact-1",
        kind=ArtifactReferenceKind.REPLAY,
        created_at=NOW + timedelta(seconds=2),
    )
    catalog.add_reference(replay_ref)

    protected = catalog.plan_gc(now=NOW + timedelta(days=2))
    assert protected.decisions[0].action.value == "keep"
    assert protected.decisions[0].reason.value == "replay_required"
    assert catalog.get(registered.entry.entry_id) == registered.entry

    retirement_time = NOW + timedelta(days=2)
    first = catalog.retire_reference(
        _retirement_request(
            registered.reference,
            requested_at=retirement_time,
        )
    )
    assert catalog.retire_reference(
        _retirement_request(
            registered.reference,
            requested_at=retirement_time,
        )
    ) == first
    catalog.retire_reference(
        _retirement_request(replay_ref, requested_at=retirement_time)
    )
    deletable = catalog.plan_gc(now=NOW + timedelta(days=2))
    repeated = catalog.plan_gc(now=NOW + timedelta(days=2))
    assert deletable == repeated
    assert deletable.decisions[0].action.value == "delete_candidate"
    assert deletable.decisions[0].reason.value == "expired_unreferenced"
    assert catalog.get(registered.entry.entry_id) == registered.entry


def test_gc_detach_rejects_a_reference_added_after_planning(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    registered = catalog.register(
        _request(_record(expires_at=NOW + timedelta(days=1)))
    )
    retirement_time = NOW + timedelta(days=2)
    catalog.retire_reference(
        _retirement_request(registered.reference, requested_at=retirement_time)
    )
    plan = catalog.plan_gc(now=retirement_time)
    decision = plan.decisions[0]
    late_reference = ArtifactLogicalReference.create(
        entry_id=registered.entry.entry_id,
        tenant_id="tenant-1",
        owner_run_id="run-1",
        owner_id="late-replay",
        kind=ArtifactReferenceKind.REPLAY,
        created_at=retirement_time,
    )
    catalog.add_reference(late_reference)
    request = ArtifactCatalogGcDetachRequest.create(
        plan_checksum=plan.plan_checksum,
        catalog_snapshot_checksum=plan.catalog_snapshot_checksum,
        decision=decision,
        requested_at=retirement_time,
    )

    with pytest.raises(GraphArtifactResultError) as stale:
        catalog.detach_gc_candidate(request)

    assert stale.value.error_code is GraphArtifactResultErrorCode.GC_PLAN_STALE
    assert catalog.get(registered.entry.entry_id) == registered.entry
    assert catalog.list_references(registered.entry.entry_id) == (late_reference,)


def test_tenant_scoped_snapshot_and_gc_plan_exclude_other_tenants(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    tenant_one = catalog.register(_request(_record()))
    tenant_two = catalog.register(
        _request(
            _record(
                tenant_id="tenant-2",
                run_id="run-2",
                artifact_id="artifact-2",
            )
        )
    )

    aggregate = catalog.snapshot(captured_at=NOW)
    scoped = catalog.snapshot(captured_at=NOW, tenant_id="tenant-1")
    plan = catalog.plan_gc(now=NOW, tenant_id="tenant-1")

    assert {entry.entry_id for entry in aggregate.entries} == {
        tenant_one.entry.entry_id,
        tenant_two.entry.entry_id,
    }
    assert scoped.entries == (tenant_one.entry,)
    assert scoped.claims == (tenant_one.claim,)
    assert scoped.references == (tenant_one.reference,)
    assert plan.catalog_snapshot_checksum == scoped.snapshot_checksum
    assert tuple(decision.tenant_id for decision in plan.decisions) == ("tenant-1",)


def test_gc_detach_ignores_unrelated_tenant_catalog_changes(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    registered = catalog.register(
        _request(_record(expires_at=NOW + timedelta(days=1)))
    )
    retirement_time = NOW + timedelta(days=2)
    catalog.retire_reference(
        _retirement_request(registered.reference, requested_at=retirement_time)
    )
    plan = catalog.plan_gc(now=retirement_time, tenant_id="tenant-1")
    decision = plan.decisions[0]

    unrelated = catalog.register(
        _request(
            _record(
                tenant_id="tenant-2",
                run_id="run-2",
                artifact_id="artifact-2",
            )
        )
    )
    request = ArtifactCatalogGcDetachRequest.create(
        plan_checksum=plan.plan_checksum,
        catalog_snapshot_checksum=plan.catalog_snapshot_checksum,
        decision=decision,
        requested_at=retirement_time,
    )

    receipt = catalog.detach_gc_candidate(request)

    assert receipt.entry == registered.entry
    assert receipt.claims == (registered.claim,)
    assert receipt.references == ()
    assert catalog.get(unrelated.entry.entry_id) == unrelated.entry
    with pytest.raises(GraphArtifactResultError) as missing:
        catalog.get(registered.entry.entry_id)
    assert missing.value.error_code is (
        GraphArtifactResultErrorCode.ARTIFACT_CATALOG_NOT_FOUND
    )


def test_retention_and_expiring_reference_ttl_boundaries_are_exact(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    expiry = NOW + timedelta(days=1)
    registered = catalog.register(_request(_record(expires_at=expiry)))

    with pytest.raises(GraphArtifactResultError) as early_retirement:
        catalog.retire_reference(
            _retirement_request(
                registered.reference,
                requested_at=expiry - timedelta(microseconds=1),
            )
        )
    assert early_retirement.value.error_code is (
        GraphArtifactResultErrorCode.LIFECYCLE_AUTHORIZATION_INVALID
    )

    catalog.retire_reference(
        _retirement_request(registered.reference, requested_at=expiry)
    )
    ephemeral = ArtifactLogicalReference.create(
        entry_id=registered.entry.entry_id,
        tenant_id="tenant-1",
        owner_run_id="run-1",
        owner_id="ttl-boundary",
        kind=ArtifactReferenceKind.EPHEMERAL,
        created_at=NOW + timedelta(seconds=2),
        expires_at=expiry,
    )
    catalog.add_reference(ephemeral)

    before = catalog.plan_gc(now=expiry - timedelta(microseconds=1))
    at_boundary = catalog.plan_gc(now=expiry)

    assert before.decisions[0].action is ArtifactCatalogGcAction.KEEP
    assert before.decisions[0].reason is ArtifactCatalogGcReason.REFERENCE_PROTECTED
    assert at_boundary.decisions[0].action is ArtifactCatalogGcAction.DELETE_CANDIDATE
    assert at_boundary.decisions[0].reason is (
        ArtifactCatalogGcReason.EXPIRED_UNREFERENCED
    )


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        (ArtifactReferenceKind.RUN, ArtifactCatalogGcReason.REFERENCE_PROTECTED),
        (ArtifactReferenceKind.REPORT, ArtifactCatalogGcReason.REFERENCE_PROTECTED),
        (ArtifactReferenceKind.EVIDENCE, ArtifactCatalogGcReason.REFERENCE_PROTECTED),
        (ArtifactReferenceKind.REPLAY, ArtifactCatalogGcReason.REPLAY_REQUIRED),
        (
            ArtifactReferenceKind.PUBLICATION,
            ArtifactCatalogGcReason.PUBLICATION_REQUIRED,
        ),
    ],
)
def test_gc_plan_preserves_each_active_protected_reference_kind(
    tmp_path,
    kind: ArtifactReferenceKind,
    reason: ArtifactCatalogGcReason,
) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    expiry = NOW + timedelta(days=1)
    registered = catalog.register(_request(_record(expires_at=expiry)))
    observation_time = expiry + timedelta(days=1)
    catalog.retire_reference(
        _retirement_request(
            registered.reference,
            requested_at=observation_time,
        )
    )
    protected = ArtifactLogicalReference.create(
        entry_id=registered.entry.entry_id,
        tenant_id="tenant-1",
        owner_run_id="run-1",
        owner_id=f"protected-{kind.value}",
        kind=kind,
        created_at=NOW + timedelta(seconds=2),
    )
    catalog.add_reference(protected)

    decision = catalog.plan_gc(now=observation_time).decisions[0]

    assert decision.action is ArtifactCatalogGcAction.KEEP
    assert decision.reason is reason
    assert decision.active_reference_ids == (protected.reference_id,)


def test_concurrent_gc_detach_has_one_receipt_and_one_stale_result(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    registered = catalog.register(
        _request(_record(expires_at=NOW + timedelta(days=1)))
    )
    retirement_time = NOW + timedelta(days=2)
    catalog.retire_reference(
        _retirement_request(registered.reference, requested_at=retirement_time)
    )
    plan = catalog.plan_gc(now=retirement_time, tenant_id="tenant-1")
    request = ArtifactCatalogGcDetachRequest.create(
        plan_checksum=plan.plan_checksum,
        catalog_snapshot_checksum=plan.catalog_snapshot_checksum,
        decision=plan.decisions[0],
        requested_at=retirement_time,
    )

    def detach(_index: int):
        try:
            return catalog.detach_gc_candidate(request)
        except GraphArtifactResultError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(detach, range(2)))

    receipts = tuple(
        outcome
        for outcome in outcomes
        if not isinstance(outcome, GraphArtifactResultError)
    )
    failures = tuple(
        outcome
        for outcome in outcomes
        if isinstance(outcome, GraphArtifactResultError)
    )
    assert len(receipts) == 1
    assert receipts[0].entry == registered.entry
    assert len(failures) == 1
    assert failures[0].error_code is GraphArtifactResultErrorCode.GC_PLAN_STALE
    state = _read_state(tmp_path)
    assert state["entries"] == []
    assert state["claims"] == []
    assert state["references"] == []


def test_gc_detach_applies_multiple_candidates_from_one_snapshot(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    first = catalog.register(
        _request(_record(expires_at=NOW + timedelta(days=1)))
    )
    second = catalog.register(
        _request(
            _record(
                run_id="run-2",
                artifact_id="artifact-2",
                expires_at=NOW + timedelta(days=1),
            )
        )
    )
    retirement_time = NOW + timedelta(days=2)
    for registered in (first, second):
        catalog.retire_reference(
            _retirement_request(
                registered.reference,
                requested_at=retirement_time,
            )
        )
    plan = catalog.plan_gc(now=retirement_time, tenant_id="tenant-1")

    receipts = tuple(
        catalog.detach_gc_candidate(
            ArtifactCatalogGcDetachRequest.create(
                plan_checksum=plan.plan_checksum,
                catalog_snapshot_checksum=plan.catalog_snapshot_checksum,
                decision=decision,
                requested_at=retirement_time,
            )
        )
        for decision in plan.decisions
    )

    assert {receipt.entry.entry_id for receipt in receipts} == {
        first.entry.entry_id,
        second.entry.entry_id,
    }
    state = _read_state(tmp_path)
    assert state["entries"] == []
    assert state["claims"] == []
    assert state["references"] == []


def test_parallel_registration_has_one_entry_and_no_lost_references(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)

    def register(index: int):
        return catalog.register(
            _request(
                _record(
                    run_id=f"run-{index}",
                    artifact_id=f"artifact-{index}",
                )
            )
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = tuple(executor.map(register, range(40)))

    assert len({item.entry.entry_id for item in results}) == 1
    assert sum(not item.deduplicated for item in results) == 1
    restarted = LocalJsonArtifactCatalog(tmp_path)
    entry_id = results[0].entry.entry_id
    assert len(restarted.list_references(entry_id)) == 40
    state = _read_state(tmp_path)
    assert len(state["entries"]) == 1
    assert len(state["claims"]) == 40


def test_two_processes_serialize_dedup_registration(tmp_path) -> None:
    store_root = tmp_path / "catalog"
    start_path = tmp_path / "start.signal"
    helper = Path(__file__).with_name("graph_artifact_catalog_process_worker.py")
    records = (
        _record(run_id="run-process-1", artifact_id="artifact-process-1"),
        _record(run_id="run-process-2", artifact_id="artifact-process-2"),
    )
    record_paths = (tmp_path / "record-1.json", tmp_path / "record-2.json")
    result_paths = (tmp_path / "result-1.json", tmp_path / "result-2.json")
    for path, record in zip(record_paths, records, strict=True):
        path.write_text(json.dumps(record.to_dict()), encoding="utf-8")
    processes = [
        subprocess.Popen(
            (
                sys.executable,
                str(helper),
                str(store_root),
                str(record_path),
                str(start_path),
                str(result_path),
                "2026-08-14T08:00:01Z",
            ),
            cwd=Path(__file__).parents[3],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for record_path, result_path in zip(record_paths, result_paths, strict=True)
    ]
    start_path.write_text("start", encoding="utf-8")
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, (stdout, stderr)

    reopened = LocalJsonArtifactCatalog(store_root)
    entry_id = ArtifactCatalogIdentity.from_record(records[0]).entry_id
    assert len(reopened.list_references(entry_id)) == 2
    assert len(reopened.list_claims_by_run(tenant_id="tenant-1", run_id="run-process-1")) == 1
    assert len(reopened.list_claims_by_run(tenant_id="tenant-1", run_id="run-process-2")) == 1
    state = _read_state(store_root)
    assert len(state["entries"]) == 1
    assert len(state["claims"]) == 2


@pytest.mark.parametrize("tamper", ["checksum", "version", "unknown_field"])
def test_state_integrity_and_exact_version_fail_closed(tmp_path, tamper: str) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    registered = catalog.register(_request(_record()))
    state = _read_state(tmp_path)
    if tamper == "checksum":
        state["state_checksum"] = "sha256:" + "f" * 64
        _state_path(tmp_path).write_text(json.dumps(state), encoding="utf-8")
    elif tamper == "version":
        state["schema_version"] = "newsroom.graph-artifact-catalog/v999"
        _write_state(tmp_path, state)
    else:
        state["unexpected"] = True
        _write_state(tmp_path, state)

    with pytest.raises(GraphArtifactResultError) as exc_info:
        catalog.get(registered.entry.entry_id)

    assert exc_info.value.error_code is GraphArtifactResultErrorCode.ARTIFACT_CATALOG_CORRUPT


def test_reconcile_reports_dangling_metadata_without_mutation(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    catalog.register(_request(_record()))
    state = _read_state(tmp_path)
    state["entries"] = []
    _write_state(tmp_path, state)
    before = _state_path(tmp_path).read_bytes()

    plan = catalog.reconcile(now=NOW + timedelta(days=1))

    assert plan.is_clean is False
    assert {item.kind.value for item in plan.issues} == {
        "dangling_logical_identity",
        "dangling_reference",
    }
    assert _state_path(tmp_path).read_bytes() == before
    with pytest.raises(GraphArtifactResultError) as exc_info:
        catalog.find_by_checksum(tenant_id="tenant-1", content_checksum=CHECKSUM)
    assert exc_info.value.error_code is GraphArtifactResultErrorCode.ARTIFACT_CATALOG_CORRUPT


def test_reconcile_compares_verified_physical_inventory_without_mutation(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    registered = catalog.register(_request(_record()))
    matching = ArtifactVerificationReceipt.for_record(
        registered.entry.record,
        verified_at=NOW + timedelta(seconds=2),
    )
    unregistered_record = _record(
        run_id="run-orphan-bytes",
        artifact_id="artifact-orphan-bytes",
        content_checksum="sha256:" + "b" * 64,
    )
    unregistered = ArtifactVerificationReceipt.for_record(
        unregistered_record,
        verified_at=NOW + timedelta(seconds=2),
    )
    before = _state_path(tmp_path).read_bytes()

    clean = catalog.reconcile(now=NOW + timedelta(days=1), physical_inventory=(matching,))
    missing = catalog.reconcile(now=NOW + timedelta(days=1), physical_inventory=())
    extra = catalog.reconcile(
        now=NOW + timedelta(days=1),
        physical_inventory=(matching, unregistered),
    )
    mismatched = catalog.reconcile(
        now=NOW + timedelta(days=1),
        physical_inventory=(replace(matching, byte_size=18),),
    )

    assert clean.is_clean is True
    assert {item.kind.value for item in missing.issues} == {"missing_physical_object"}
    assert {item.kind.value for item in extra.issues} == {"unregistered_physical_object"}
    assert {item.kind.value for item in mismatched.issues} == {"physical_identity_mismatch"}
    assert _state_path(tmp_path).read_bytes() == before


def test_dedup_claim_preserves_logical_record_with_canonical_ref(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    first = catalog.register(_request(_record(run_id="run-1", artifact_id="artifact-1")))
    second_record = replace(
        _record(run_id="run-2", artifact_id="artifact-2"),
        artifact_class=ArtifactClass.REPORT,
        retention_class=RetentionClass.REPORT,
        expires_at=None,
        required_for_publication=True,
    )
    second = catalog.register(_request(second_record))

    claims = catalog.list_claims_by_run(tenant_id="tenant-1", run_id="run-2")
    assert len(claims) == 1
    assert catalog.get_claim(
        tenant_id="tenant-1",
        run_id="run-2",
        artifact_id="artifact-2",
    ) == claims[0]
    assert claims[0].record.run_id == "run-2"
    assert claims[0].record.artifact_class is ArtifactClass.REPORT
    assert claims[0].record.required_for_publication is True
    assert claims[0].record.ref == first.entry.record.ref
    assert second.reference.kind is ArtifactReferenceKind.PUBLICATION


def test_catalog_state_symlink_is_rejected(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(tmp_path)
    registered = catalog.register(_request(_record()))
    state_path = _state_path(tmp_path)
    real_path = tmp_path / "catalog-real.json"
    state_path.replace(real_path)
    try:
        os.symlink(real_path, state_path)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(GraphArtifactResultError) as exc_info:
        catalog.get(registered.entry.entry_id)

    assert exc_info.value.error_code is GraphArtifactResultErrorCode.ARTIFACT_CATALOG_CORRUPT


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_junctioned_catalog_root_is_rejected(tmp_path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    junction = tmp_path / "catalog-junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(victim)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("junction creation unavailable")

    try:
        catalog = LocalJsonArtifactCatalog(junction)
        with pytest.raises(GraphArtifactResultError) as exc_info:
            catalog.register(_request(_record()))
        assert exc_info.value.error_code is GraphArtifactResultErrorCode.ARTIFACT_CATALOG_CORRUPT
        assert list(victim.iterdir()) == []
    finally:
        junction.rmdir()


def test_catalog_bounds_fail_without_partial_state(tmp_path) -> None:
    catalog = LocalJsonArtifactCatalog(
        tmp_path,
        max_entries=1,
        max_claims=1,
        max_references=1,
    )
    catalog.register(_request(_record()))
    before = _state_path(tmp_path).read_bytes()

    with pytest.raises(GraphArtifactResultError) as exc_info:
        catalog.register(
            _request(
                _record(
                    run_id="run-2",
                    artifact_id="artifact-2",
                    content_checksum="sha256:" + "b" * 64,
                )
            )
        )

    assert exc_info.value.error_code is GraphArtifactResultErrorCode.ARTIFACT_QUOTA_EXCEEDED
    assert _state_path(tmp_path).read_bytes() == before
