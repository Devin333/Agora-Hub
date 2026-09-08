from __future__ import annotations

import json
import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from framework.events.canonical import checksum_for
from framework.shared.json import stable_json_dumps
from framework.harness.subagents.transcript import SubAgentTranscriptStoreError
from framework.harness import (
    HarnessValidationError,
    SubAgentAttemptIdentity,
    SubAgentAttemptDescriptor,
    SubAgentAttemptMetadataIncompleteError,
    SubAgentContextEvidence,
    SubAgentOutputDocument,
    SubAgentTranscript,
    SubAgentTranscriptCorruptError,
    SubAgentTranscriptConflictError,
    SubAgentTranscriptDescriptorPort,
    FakeSubAgentTranscriptStore,
)
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_CONDITION_POLICY_VERSION,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
)
from framework.harness.subagents.transcript import (
    SUBAGENT_BUNDLE_SCHEMA_V3,
    SUBAGENT_RECEIPT_SCHEMA_V3,
)
from infrastructure.storage.harness import FilesystemSubAgentTranscriptStore


FIXED_TIME = datetime(2026, 8, 19, 1, 2, 3, tzinfo=UTC)


def _identity(*, parent_run_id: str = "run-1", attempt: int = 1) -> SubAgentAttemptIdentity:
    return SubAgentAttemptIdentity(
        invocation_id=f"invocation://{parent_run_id}/task-1/{attempt}",
        parent_run_id=parent_run_id,
        child_run_id=f"{parent_run_id}:stage-1:task-instance-{attempt}",
        graph_id="research.paper_analysis.dynamic",
        graph_version="3",
        graph_ref="research.paper_analysis.dynamic@3",
        graph_schema_version=GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
        compiler_version=HARNESS_GRAPH_ONLY_COMPILER_VERSION,
        condition_policy_version=HARNESS_CONDITION_POLICY_VERSION,
        graph_checksum=checksum_for({"graph": "research.paper_analysis.dynamic@3"}),
        stage_id="dynamic_analysis_stage",
        stage_binding_checksum=checksum_for({"stage": "dynamic_analysis_stage"}),
        stage_identity_schema="newsroom.harness-task-plan-stage-identity/v2",
        stage_identity_checksum=checksum_for({"stage_identity": "dynamic_analysis_stage"}),
        plan_id="plan-1",
        plan_version=1,
        plan_checksum=checksum_for({"plan": "plan-1"}),
        task_id="structure",
        task_definition_checksum=checksum_for({"task": "structure"}),
        context_envelope_id="context://run-1/task-1",
        context_envelope_checksum=checksum_for({"context": "run-1/task-1"}),
        node_id="dynamic-analysis-node",
        node_instance_id=f"dynamic-analysis-node-instance-{attempt}",
        activity_id=f"dynamic-analysis-activity-{attempt}",
        activity_attempt=1,
        task_instance_id=f"task-instance-{attempt}",
        attempt=attempt,
        subagent_id="research-structure-analyst",
    )


def _documents(
    identity: SubAgentAttemptIdentity,
    *,
    result: str = "ok",
) -> tuple[SubAgentContextEvidence, SubAgentOutputDocument, SubAgentTranscript]:
    context = SubAgentContextEvidence(
        identity=identity,
        context_envelope_ref=identity.context_envelope_id,
        input_refs=("artifact://input/source",),
        memory_context_refs=(),
        redaction_report={"raw_parent_messages_included": False},
    )
    output = SubAgentOutputDocument(
        identity=identity,
        status="succeeded",
        output={"result": result},
        artifact_refs=("artifact://analysis/structure",),
    )
    gate = {
        "gate_id": "subagent_output_schema",
        "gate_version": "1",
        "input_checksum": checksum_for({"output": result}),
        "passed": True,
        "reason_code": "subagent_output_schema_passed",
    }
    transcript = SubAgentTranscript(
        identity=identity,
        context_envelope_ref=context.context_envelope_ref,
        input_refs=context.input_refs,
        output_ref=output.ref,
        output_checksum=output.output_checksum,
        artifact_refs=output.artifact_refs,
        gate_results=({**gate, "evidence_checksum": checksum_for(gate)},),
        budget_snapshot={"max_turns": 3},
        redaction_report=context.redaction_report,
        events=({"event_type": "subagent_completed"},),
        observed_at=FIXED_TIME,
    )
    return context, output, transcript


def _bundle_path(root: Path) -> Path:
    return next(root.rglob("sat_*.json"))


def _metadata_path(root: Path) -> Path:
    return next(root.rglob("sat_*.meta"))


def test_filesystem_store_roundtrips_only_v3_bundle(tmp_path: Path) -> None:
    documents = _documents(_identity())
    store = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)

    receipt = store.write(*documents)
    payload = json.loads(_bundle_path(tmp_path).read_text(encoding="utf-8"))

    assert payload["schema_version"] == SUBAGENT_BUNDLE_SCHEMA_V3
    assert receipt.schema_version == SUBAGENT_RECEIPT_SCHEMA_V3
    assert receipt.transcript_ref.startswith("subagent-transcript://v3/")
    assert store.verify(receipt) == receipt
    assert store.read(receipt.transcript_ref) == documents[2]
    assert store.read_context(receipt.context_ref) == documents[0]
    assert store.read_output(receipt.output_ref) == documents[1]


def test_filesystem_store_rejects_tampered_v3_bundle(tmp_path: Path) -> None:
    documents = _documents(_identity())
    store = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)
    receipt = store.write(*documents)
    path = _bundle_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["output"]["output"]["result"] = "forged"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SubAgentTranscriptCorruptError):
        store.verify(receipt)


def test_filesystem_store_rejects_pre_cutover_refs(tmp_path: Path) -> None:
    store = FilesystemSubAgentTranscriptStore(tmp_path)

    with pytest.raises(HarnessValidationError):
        store.read("subagent-transcript://v2/run-1/sat_" + "0" * 64)


def test_attempt_descriptor_roundtrips_exact_trusted_metadata(tmp_path: Path) -> None:
    identity = _identity()
    documents = _documents(identity)
    store = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)

    receipt = store.write(*documents)
    descriptor = store.describe_attempt(identity)

    assert isinstance(store, SubAgentTranscriptDescriptorPort)
    assert descriptor is not None
    assert descriptor.identity == identity
    assert descriptor.receipt == receipt
    assert descriptor.artifact_refs == documents[1].artifact_refs
    assert descriptor.bundle_size_bytes == _bundle_path(tmp_path).stat().st_size
    assert SubAgentAttemptDescriptor.from_dict(descriptor.to_dict()) == descriptor
    assert store.describe_ref(receipt.transcript_ref) == descriptor
    assert store.describe_ref(receipt.context_ref) == descriptor
    assert store.describe_ref(receipt.output_ref) == descriptor
    assert identity.result_attempt_id == (
        "subagent_" + identity.identity_checksum.removeprefix("sha256:")
    )


def test_descriptor_size_limit_rejects_before_either_file_is_committed(tmp_path: Path) -> None:
    store = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME, max_transcript_bytes=3000)
    documents = _documents(_identity())
    assert len(stable_json_dumps(documents[2].to_dict()).encode("utf-8")) < 3000
    with pytest.raises(SubAgentTranscriptStoreError) as error:
        store.write(*documents)
    assert error.value.code == "subagent_transcript_size_exceeded"
    assert not tuple(tmp_path.rglob("sat_*.meta"))
    assert not tuple(tmp_path.rglob("sat_*.json"))
    assert store.describe_attempt(documents[0].identity) is None
    assert FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME).write(*documents)


def test_fake_store_implements_descriptor_boundary() -> None:
    identity = _identity()
    documents = _documents(identity)
    store = FakeSubAgentTranscriptStore()
    receipt = store.write(*documents)

    assert isinstance(store, SubAgentTranscriptDescriptorPort)
    descriptor = store.describe_attempt(identity)
    assert descriptor is not None
    assert descriptor.receipt == receipt
    assert descriptor.artifact_refs == documents[1].artifact_refs
    assert store.describe_ref(receipt.output_ref) == descriptor

    del store.outputs[receipt.output_ref]
    with pytest.raises(SubAgentAttemptMetadataIncompleteError):
        store.describe_attempt(identity)


def test_descriptor_query_does_not_open_bundle_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    store = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)
    receipt = store.write(*_documents(identity))
    bundle_path = _bundle_path(tmp_path)
    original_open = Path.open

    def fail_bundle_open(path: Path, *args: object, **kwargs: object):
        if path == bundle_path:
            raise AssertionError("descriptor query read bundle payload")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_bundle_open)

    assert store.describe_attempt(identity) is not None
    assert store.describe_ref(receipt.output_ref) is not None


def test_descriptor_tamper_and_cross_identity_are_rejected(tmp_path: Path) -> None:
    identity = _identity()
    store = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)
    receipt = store.write(*_documents(identity))
    metadata_path = _metadata_path(tmp_path)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["artifact_refs"] = ["artifact://forged"]
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SubAgentTranscriptCorruptError):
        store.describe_attempt(identity)

    other = _identity(parent_run_id="run-2", attempt=2)
    assert store.describe_attempt(other) is None
    assert store.describe_ref(
        receipt.output_ref.replace("/run-1/", "/run-2/")
    ) is None


def test_bundle_tamper_is_metadata_only_visible_but_verify_rejects(
    tmp_path: Path,
) -> None:
    identity = _identity()
    store = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)
    receipt = store.write(*_documents(identity))
    path = _bundle_path(tmp_path)
    content = path.read_bytes()
    marker = b'"result":"ok"'
    assert marker in content
    path.write_bytes(content.replace(marker, b'"result":"no"', 1))

    assert store.describe_attempt(identity) is not None
    with pytest.raises(SubAgentTranscriptCorruptError):
        store.verify(receipt)


@pytest.mark.parametrize("missing", ["metadata", "bundle"])
def test_one_sided_attempt_commit_fails_closed(
    tmp_path: Path,
    missing: str,
) -> None:
    identity = _identity()
    documents = _documents(identity)
    store = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)
    store.write(*documents)
    target = _metadata_path(tmp_path) if missing == "metadata" else _bundle_path(tmp_path)
    target.unlink()
    reopened = FilesystemSubAgentTranscriptStore(tmp_path)

    with pytest.raises(SubAgentAttemptMetadataIncompleteError) as captured:
        reopened.describe_attempt(identity)
    assert captured.value.code == "subagent_attempt_metadata_incomplete"
    with pytest.raises(SubAgentAttemptMetadataIncompleteError):
        reopened.find_by_identity(identity)
    with pytest.raises(SubAgentAttemptMetadataIncompleteError):
        reopened.write(*documents)


def test_crash_after_metadata_before_bundle_remains_typed_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    documents = _documents(identity)
    store = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)
    storage_module = importlib.import_module(
        "infrastructure.storage.harness.subagent_transcript"
    )
    real_create = storage_module.verified_atomic_create

    def crash_before_bundle(path: Path, content: bytes, **kwargs: object) -> bool:
        if path.name.endswith(".json"):
            raise OSError("simulated crash before bundle publish")
        return real_create(path, content, **kwargs)

    monkeypatch.setattr(storage_module, "verified_atomic_create", crash_before_bundle)
    with pytest.raises(HarnessValidationError) as captured:
        store.write(*documents)
    assert captured.value.code == "subagent_transcript_store_unavailable"
    assert _metadata_path(tmp_path).is_file()
    assert not any(tmp_path.rglob("sat_*.json"))

    monkeypatch.setattr(storage_module, "verified_atomic_create", real_create)
    reopened = FilesystemSubAgentTranscriptStore(tmp_path)
    with pytest.raises(SubAgentAttemptMetadataIncompleteError):
        reopened.describe_attempt(identity)
    with pytest.raises(SubAgentAttemptMetadataIncompleteError):
        reopened.write(*documents)


def test_concurrent_identical_writes_reuse_first_receipt(tmp_path: Path) -> None:
    identity = _identity()
    documents = _documents(identity)
    first_time = FIXED_TIME
    second_time = FIXED_TIME.replace(second=4)
    stores = (
        FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: first_time),
        FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: second_time),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(
            executor.map(lambda store: store.write(*documents), stores)
        )

    assert receipts[0] == receipts[1]
    assert len(tuple(tmp_path.rglob("sat_*.meta"))) == 1
    assert stores[0].describe_attempt(identity).receipt == receipts[0]  # type: ignore[union-attr]


def test_concurrent_conflicting_writes_reject_one_candidate(tmp_path: Path) -> None:
    identity = _identity()
    store_a = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)
    store_b = FilesystemSubAgentTranscriptStore(tmp_path, clock=lambda: FIXED_TIME)

    def write_candidate(
        store: FilesystemSubAgentTranscriptStore,
        result: str,
    ) -> object:
        try:
            return store.write(*_documents(identity, result=result))
        except Exception as exc:  # asserted below as the competing terminal result
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(write_candidate, store_a, "first"),
            executor.submit(write_candidate, store_b, "other"),
        )
        results = tuple(future.result() for future in futures)

    assert sum(isinstance(item, SubAgentTranscriptConflictError) for item in results) == 1
    assert sum(not isinstance(item, Exception) for item in results) == 1
