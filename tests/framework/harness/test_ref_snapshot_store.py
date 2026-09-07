from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from framework.agent.artifacts.stores.filesystem import FilesystemArtifactStore
from framework.events.canonical import ProducerIdentity, checksum_for
from framework.events.runtime.publisher import EventRuntime
from framework.events.schema import default_event_schema_catalog
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import RefAccessPolicy, RefDescriptor
from framework.harness.ref_snapshot import RefAuthoritySnapshot, SnapshotRefResolutionPort
from framework.harness.ref_snapshot_store import DurableRefAuthoritySnapshotStore
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.storage.events.sqlite import SQLiteEventStore
from tests.framework.harness.test_ref_snapshot import (
    _child_snapshot,
    _result_snapshot,
    _root_snapshot,
)


def _snapshot() -> RefAuthoritySnapshot:
    execution = GraphExecutionIdentity(
        run_id="ref-run", graph_id="ref.graph", graph_version="1",
        graph_ref="ref.graph@1", graph_checksum=checksum_for("graph"),
        node_id="analysis", node_instance_id="node-1", activity_id="activity-1", attempt=1,
    )
    descriptor = RefDescriptor(
        ref="document", run_id=execution.run_id, stage_id="analysis",
        tenant_id="tenant-1", owner_id="owner-1", access_mode="READ_ONLY",
        artifact_type="graph_input", source_checksum=checksum_for("source"),
        ref_kind="input", scope="SHARED_READ_ONLY",
    )
    return RefAuthoritySnapshot(
        execution_identity=execution, stage_id="analysis",
        stage_binding_checksum=checksum_for("binding"),
        task_policy_checksum=checksum_for("task-policy"), source_checksum=checksum_for("inputs"),
        policy=RefAccessPolicy(
            policy_id="refs", version="1", run_id=execution.run_id,
            stage_id="analysis", tenant_id="tenant-1", owner_id="owner-1",
            allowed_refs=(descriptor.ref,), allowed_artifact_types=("graph_input",),
            allowed_ref_kinds=("input",), pinned_checksums={descriptor.ref: descriptor.source_checksum},
        ),
        descriptors=(descriptor,),
    )


def _store(root: Path, *, tenant_id: str = "control", runtime_factory=None):
    events = SQLiteEventStore(root / "events.sqlite3")
    runtime = EventRuntime(store=events, schema_catalog=default_event_schema_catalog())
    if runtime_factory is not None:
        runtime = runtime_factory(runtime)
    return DurableRefAuthoritySnapshotStore(
        runtime, events, artifact_store=FilesystemArtifactStore(root / "artifacts"), tenant_id=tenant_id,
    ), events


def test_reference_snapshot_roundtrip_is_exact_and_immutable():
    snapshot = _snapshot()
    assert RefAuthoritySnapshot.from_dict(snapshot.to_dict()) == snapshot
    assert snapshot.binding_key == RefAuthoritySnapshot.admission_binding_key(
        snapshot.execution_identity, snapshot.stage_id, snapshot.stage_binding_checksum,
    )
    resolver = SnapshotRefResolutionPort(snapshot)
    assert resolver.resolve("document") == snapshot.descriptors[0]
    assert resolver.resolve("sibling") is None
    assert resolver.resolve_namespace("document") is None
    changed = replace(snapshot, source_checksum=checksum_for("different inputs"))
    assert changed.binding_key == snapshot.binding_key
    assert changed.snapshot_ref != snapshot.snapshot_ref
    with pytest.raises(TypeError):
        snapshot.policy.pinned_checksums["document"] = checksum_for("changed")
    payload = snapshot.to_dict()
    payload["source_checksum"] = checksum_for("changed")
    with pytest.raises(HarnessValidationError, match="checksum"):
        RefAuthoritySnapshot.from_dict(payload)
    with pytest.raises(HarnessValidationError, match="exactly"):
        replace(snapshot, descriptors=())


def test_committed_grant_survives_reopen_and_reuses_one_event(tmp_path: Path):
    snapshot = _snapshot()
    first, events = _store(tmp_path)
    assert first.commit(snapshot) == snapshot.snapshot_ref
    reopened, _ = _store(tmp_path)
    assert reopened.get(run_id=snapshot.run_id, snapshot_ref=snapshot.snapshot_ref) == snapshot
    assert reopened.find(run_id=snapshot.run_id, binding_key=snapshot.binding_key) == snapshot
    assert reopened.commit(snapshot) == snapshot.snapshot_ref
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1
    assert len(tuple((tmp_path / "artifacts").rglob("*.json"))) == 1


def test_changed_input_policy_or_descriptors_cannot_rewrite_admission(tmp_path: Path):
    snapshot = _snapshot()
    store, events = _store(tmp_path)
    store.commit(snapshot)
    changed_descriptor = replace(
        snapshot.descriptors[0],
        source_checksum=checksum_for("other descriptor source"),
    )
    changed_authority = replace(
        snapshot,
        descriptors=(changed_descriptor,),
        policy=replace(
            snapshot.policy,
            pinned_checksums={
                changed_descriptor.ref: changed_descriptor.source_checksum,
            },
        ),
    )
    for changed in (
        replace(snapshot, source_checksum=checksum_for("other inputs")),
        replace(snapshot, task_policy_checksum=checksum_for("other policy")),
        changed_authority,
    ):
        with pytest.raises(HarnessValidationError) as error:
            store.commit(changed)
        assert error.value.code == "REF_SNAPSHOT_CONFLICT"
    assert store.get(run_id=snapshot.run_id, snapshot_ref=snapshot.snapshot_ref) == snapshot
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1
    assert len(tuple((tmp_path / "artifacts").rglob("*.json"))) == 1
    reopened, _ = _store(tmp_path)
    assert reopened.find(
        run_id=snapshot.run_id,
        binding_key=snapshot.binding_key,
    ) == snapshot


def test_reopen_recovers_only_the_original_committed_parent_chain(tmp_path: Path):
    root = _root_snapshot()
    child = _child_snapshot(root)
    result = _result_snapshot(child)
    store, events = _store(tmp_path)

    assert store.commit(root) == root.snapshot_ref
    assert store.commit(child) == child.snapshot_ref
    assert store.commit(result) == result.snapshot_ref

    reopened, _ = _store(tmp_path)
    assert reopened.get(run_id=root.run_id, snapshot_ref=root.snapshot_ref) == root
    assert reopened.get(run_id=child.run_id, snapshot_ref=child.snapshot_ref) == child
    assert reopened.get(run_id=result.run_id, snapshot_ref=result.snapshot_ref) == result
    assert reopened.find(run_id=child.run_id, binding_key=child.binding_key) == child
    assert events.get_stream_high_watermark("run:run-1", tenant_id="control") == 3


class _FailOnceRuntime:
    def __init__(self, delegate):
        self.delegate = delegate
        self.fail = True

    def publish(self, request, **kwargs):
        if self.fail:
            self.fail = False
            raise RuntimeError("injected pre-commit crash")
        return self.delegate.publish(request, **kwargs)

    def publish_batch(self, requests, **kwargs):
        return self.delegate.publish_batch(requests, **kwargs)


class _EnvelopeMutationRuntime:
    def __init__(self, delegate, field: str):
        self.delegate = delegate
        self.field = field

    def publish(self, request, **kwargs):
        mutations = {
            "event_id": {"event_id": "refgrant_forged"},
            "subject": {"subject": "forged-stage"},
            "correlation_id": {"correlation_id": "forged-run"},
            "producer": {
                "producer": ProducerIdentity(component="forged-producer", version="1")
            },
        }
        return self.delegate.publish(replace(request, **mutations[self.field]), **kwargs)

    def publish_batch(self, requests, **kwargs):
        return self.delegate.publish_batch(requests, **kwargs)


@pytest.mark.parametrize(
    "field",
    ("event_id", "subject", "correlation_id", "producer"),
)
def test_noncanonical_event_envelope_never_grants_access(
    tmp_path: Path,
    field: str,
):
    snapshot = _snapshot()
    store, events = _store(
        tmp_path,
        runtime_factory=lambda runtime: _EnvelopeMutationRuntime(runtime, field),
    )

    with pytest.raises(HarnessValidationError) as committed:
        store.commit(snapshot)
    assert committed.value.code == "REF_SNAPSHOT_CORRUPT"
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1
    with pytest.raises(HarnessValidationError) as recovered:
        store.get(run_id=snapshot.run_id, snapshot_ref=snapshot.snapshot_ref)
    assert recovered.value.code == "REF_SNAPSHOT_CORRUPT"


def test_orphan_artifact_never_grants_access_before_event_commit(tmp_path: Path):
    snapshot = _snapshot()
    store, events = _store(tmp_path, runtime_factory=_FailOnceRuntime)
    with pytest.raises(RuntimeError, match="pre-commit"):
        store.commit(snapshot)
    assert len(tuple((tmp_path / "artifacts").rglob("*.json"))) == 1
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") is None
    with pytest.raises(HarnessValidationError) as error:
        store.get(run_id=snapshot.run_id, snapshot_ref=snapshot.snapshot_ref)
    assert error.value.code == "REF_SNAPSHOT_MISSING"
    assert store.find(run_id=snapshot.run_id, binding_key=snapshot.binding_key) is None
    assert store.commit(snapshot) == snapshot.snapshot_ref
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1


@pytest.mark.parametrize("remove", [False, True])
def test_corrupt_or_missing_committed_artifact_is_not_repaired(tmp_path: Path, remove: bool):
    snapshot = _snapshot()
    store, events = _store(tmp_path)
    store.commit(snapshot)
    artifact = next((tmp_path / "artifacts").rglob("*.json"))
    if remove:
        artifact.unlink()
    else:
        artifact.write_bytes(b'{"corrupt":true}')
    reopened, _ = _store(tmp_path)
    with pytest.raises(HarnessValidationError):
        reopened.get(run_id=snapshot.run_id, snapshot_ref=snapshot.snapshot_ref)
    with pytest.raises(HarnessValidationError):
        reopened.commit(snapshot)
    assert artifact.exists() is not remove
    if not remove:
        assert artifact.read_bytes() == b'{"corrupt":true}'
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1


def test_lookup_cannot_cross_run_or_event_tenant(tmp_path: Path):
    snapshot = _snapshot()
    store, _ = _store(tmp_path)
    store.commit(snapshot)
    other_tenant, _ = _store(tmp_path, tenant_id="other")
    for reader, run_id in ((other_tenant, snapshot.run_id), (store, "other-run")):
        with pytest.raises(HarnessValidationError) as error:
            reader.get(run_id=run_id, snapshot_ref=snapshot.snapshot_ref)
        assert error.value.code == "REF_SNAPSHOT_MISSING"


def test_concurrent_equal_admissions_commit_one_authoritative_grant(tmp_path: Path):
    snapshot = _snapshot()
    stores = [_store(tmp_path)[0] for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        refs = tuple(executor.map(lambda store: store.commit(snapshot), stores))
    assert refs == (snapshot.snapshot_ref,) * 4
    store, events = _store(tmp_path)
    assert store.find(run_id=snapshot.run_id, binding_key=snapshot.binding_key) == snapshot
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1


def test_concurrent_conflicting_admissions_never_create_two_grants(tmp_path: Path):
    original = _snapshot()
    conflicting = replace(original, source_checksum=checksum_for("other source"))
    stores = [_store(tmp_path)[0] for _ in range(2)]

    def commit(index):
        try:
            return stores[index].commit((original, conflicting)[index])
        except HarnessValidationError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(commit, range(2)))
    assert outcomes.count("REF_SNAPSHOT_CONFLICT") == 1
    store, events = _store(tmp_path)
    accepted = store.find(run_id=original.run_id, binding_key=original.binding_key)
    assert accepted in (original, conflicting)
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1
