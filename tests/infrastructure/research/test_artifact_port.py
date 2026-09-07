from __future__ import annotations

import json
import logging
import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from framework.agent.artifacts import (
    ArtifactChecksumMismatchError,
    ArtifactNotFoundError,
    ArtifactStoreMetadataError,
    compute_checksum,
)
from framework.events.canonical import checksum_for
from framework.harness import ArtifactWriteRequest
from framework.harness.artifacts import (
    ArtifactReferenceDescriptorPort,
    GraphTerminalArtifact,
    GraphTerminalManifest,
    GraphTerminalManifestV2,
    GraphTerminalManifestCommitRequest,
    GraphTerminalManifestContext,
    GraphTerminalManifestError,
    GraphTerminalManifestErrorCode,
)
from framework.harness.graph import HarnessGraphCompiler
from framework.harness.graph.execution_versions import GraphExecutionVersionManifest
from backend.research.graphs import build_paper_analysis_graph_definition
from framework.shared.json import stable_json_dumps
from infrastructure.research.artifact_port import (
    ArtifactPublicationVisibilityError,
    ArtifactRunBindingError,
    ArtifactWriteConflictError,
    FilesystemHarnessArtifactPort,
    is_verified_internal_staged_artifact,
)
from infrastructure.research.diagnostics import (
    MISSING_IDENTITY_REF,
    RESEARCH_PERSISTENCE_LOGGER,
    RESEARCH_PERSISTENCE_OPERATION_EVENT,
)


_NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)


def test_run_binding_is_required_nested_and_reset(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    request = ArtifactWriteRequest("research-analysis", {"status": "ok"})

    with pytest.raises(ArtifactRunBindingError, match="bound run"):
        port.write_artifact(request)

    with port.bind_run("run-outer"):
        assert port.current_run_id == "run-outer"
        with port.bind_run("run-inner"):
            assert port.current_run_id == "run-inner"
        assert port.current_run_id == "run-outer"
    assert port.current_run_id is None


def test_staged_context_round_trip_is_restart_safe_and_checksum_bound(tmp_path) -> None:
    request = _context_request({"status": "ok", "score": 0.9})
    port = FilesystemHarnessArtifactPort(tmp_path)

    with port.bind_run("run-1"):
        ref = port.write_artifact(request)

    content = stable_json_dumps(request.to_dict()).encode("utf-8")
    descriptor = port.list_staged_artifacts("run-1")[0]
    assert ref.ref == f"artifact://run-1/{request.artifact_type}"
    assert ref.checksum == f"sha256:{compute_checksum(content)}"
    assert descriptor.content_checksum == ref.checksum
    assert descriptor.byte_size == len(content)
    assert (tmp_path / "run-1" / descriptor.relative_path).read_bytes() == content
    assert is_verified_internal_staged_artifact(descriptor)

    restarted = FilesystemHarnessArtifactPort(tmp_path)
    assert restarted.read_artifact(ref.ref) == request.to_dict()


def test_unpublished_member_is_hidden_but_reference_verification_is_available(
    tmp_path,
) -> None:
    publication_calls: list[object] = []
    port = FilesystemHarnessArtifactPort(
        tmp_path,
        accepted_run_resolver=lambda claim: (publication_calls.append(claim) or False),
    )
    with port.bind_run("run-1"):
        ref = port.write_artifact(
            ArtifactWriteRequest("research-analysis", {"status": "candidate"})
        )

    assert port.verify_artifact_ref(ref.ref, expected_run_id="run-1") is None
    with pytest.raises(ArtifactPublicationVisibilityError) as hidden:
        port.read_artifact(ref.ref)

    assert hidden.value.disposition == "staging_only"
    assert publication_calls == []


def test_staged_artifact_descriptor_is_exact_and_payload_free(
    tmp_path,
    monkeypatch,
) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    request = _context_request({"status": "candidate"})
    with port.bind_run("run-1"):
        ref = port.write_artifact(request)
    monkeypatch.setattr(
        port.store,
        "read",
        lambda _ref: pytest.fail("descriptor attempted to read artifact payload bytes"),
    )

    descriptor = port.describe_artifact_ref(
        ref.ref,
        expected_run_id="run-1",
    )

    assert isinstance(port, ArtifactReferenceDescriptorPort)
    assert descriptor.ref == ref.ref
    assert descriptor.run_id == "run-1"
    assert descriptor.artifact_type == request.artifact_type
    assert descriptor.checksum == ref.checksum
    assert descriptor.byte_size == len(
        stable_json_dumps(request.to_dict()).encode("utf-8")
    )
    assert descriptor.media_type == "application/json"
    assert descriptor.tenant_id is None
    assert descriptor.graph_id is None
    assert descriptor.node_id == "context"
    assert descriptor.attempt_id == "context-1"
    assert descriptor.identity_scope_ref is None
    assert descriptor.subject_scope_ref is None
    assert not hasattr(descriptor, "payload")


def test_terminal_artifact_descriptor_binds_trusted_tenant_and_graph(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        ref = port.write_artifact(_context_request({"status": "accepted"}))
    terminal = _commit_terminal_manifest(port, "run-1")

    descriptor = port.describe_artifact_ref(
        ref.ref,
        expected_run_id="run-1",
        expected_tenant_id="tenant-1",
    )

    assert descriptor.tenant_id == "tenant-1"
    assert descriptor.graph_id == terminal.graph_id
    artifact_path = tmp_path / "run-1" / terminal.artifacts[0].relative_path
    artifact_path.write_bytes(b"tampered-after-authority-check")
    with pytest.raises(ArtifactRunBindingError, match="expected tenant"):
        port.describe_artifact_ref(
            ref.ref,
            expected_run_id="run-1",
            expected_tenant_id="tenant-2",
        )


def test_staged_descriptor_rejects_unverifiable_tenant_ownership(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        ref = port.write_artifact(_context_request({"status": "candidate"}))

    with pytest.raises(ArtifactRunBindingError, match="ownership is unavailable"):
        port.describe_artifact_ref(
            ref.ref,
            expected_run_id="run-1",
            expected_tenant_id="tenant-1",
        )


def test_artifact_ref_verifier_rejects_missing_and_cross_run_refs(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        ref = port.write_artifact(_context_request({"status": "candidate"}))

    with pytest.raises(ArtifactRunBindingError, match="expected parent run"):
        port.verify_artifact_ref(ref.ref, expected_run_id="run-2")
    with pytest.raises(ArtifactNotFoundError):
        port.verify_artifact_ref(
            "artifact://missing-run/research-analysis",
            expected_run_id="missing-run",
        )


def test_internal_graph_reader_allows_only_verified_graph_result_members(
    tmp_path,
) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        graph_ref = port.write_artifact(_graph_request({"candidate": "verified"}))
        public_ref = port.write_artifact(
            ArtifactWriteRequest("research-analysis", {"status": "candidate"})
        )

    assert port.read_graph_result_artifact(
        graph_ref.ref,
        expected_run_id="run-1",
    )["payload"] == {"candidate": "verified"}
    with pytest.raises(ArtifactStoreMetadataError, match="ref-only"):
        port.read_graph_result_artifact(public_ref.ref, expected_run_id="run-1")
    with pytest.raises(ArtifactRunBindingError, match="expected run"):
        port.read_graph_result_artifact(graph_ref.ref, expected_run_id="run-2")


def test_internal_staging_membership_rejects_spoofed_context_type(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    identity = "a" * 64
    with port.bind_run("run-1"):
        ref = port.write_artifact(
            ArtifactWriteRequest(
                f"context-unregistered-{identity}",
                {"snapshot": "spoofed"},
                metadata={
                    "context_ref_only": True,
                    "identity_checksum": f"sha256:{identity}",
                },
            )
        )

    descriptor = port.list_staged_artifacts("run-1")[0]
    assert not is_verified_internal_staged_artifact(descriptor)
    with pytest.raises(ArtifactPublicationVisibilityError):
        port.read_artifact(ref.ref)


def test_long_artifact_type_uses_bounded_hashed_path_without_changing_ref(
    tmp_path,
) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    request = _context_request({"snapshot": "verified"})

    with port.bind_run("run-1"):
        stored = port.write_artifact(request)

    descriptor = port.list_staged_artifacts("run-1")[0]
    assert stored.ref == f"artifact://run-1/{request.artifact_type}"
    assert descriptor.relative_path.startswith("artifacts/a-")
    assert len(Path(descriptor.relative_path).name) == 71
    assert port.read_artifact(stored.ref)["payload"] == {"snapshot": "verified"}


def test_same_type_identical_write_is_idempotent(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    request = _context_request({"version": 1})

    with port.bind_run("run-1"):
        ref = port.write_artifact(request)
        duplicate = port.write_artifact(request)

    assert duplicate == ref
    assert len(port.list_staged_artifacts("run-1")) == 1
    assert port.read_artifact(ref.ref)["payload"] == {"version": 1}


def test_same_type_different_write_fails_without_replacing_bytes(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    first = _context_request({"version": 1})
    second = _context_request({"version": 2})
    with port.bind_run("run-1"):
        ref = port.write_artifact(first)
        with pytest.raises(ArtifactWriteConflictError, match="immutable"):
            port.write_artifact(second)

    assert port.read_artifact(ref.ref)["payload"] == {"version": 1}


def test_context_local_binding_isolates_concurrent_runs(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    barrier = Barrier(2)

    def publish(run_id: str) -> tuple[str, str | None]:
        with port.bind_run(run_id):
            barrier.wait()
            ref = port.write_artifact(_context_request({"run_id": run_id}))
            return ref.ref, port.current_run_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, ("run-a", "run-b")))

    artifact_type = _context_request({}).artifact_type
    assert set(results) == {
        (f"artifact://run-a/{artifact_type}", "run-a"),
        (f"artifact://run-b/{artifact_type}", "run-b"),
    }
    assert port.current_run_id is None
    assert port.read_artifact(results[0][0])["payload"]["run_id"] in {
        "run-a",
        "run-b",
    }
    assert port.read_artifact(results[1][0])["payload"]["run_id"] in {
        "run-a",
        "run-b",
    }


def test_conflicting_metadata_run_id_fails_closed(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"), pytest.raises(ArtifactRunBindingError, match="conflicts"):
        port.write_artifact(
            ArtifactWriteRequest(
                "research-analysis",
                {"status": "ok"},
                metadata={"run_id": "run-2"},
            )
        )
    assert not (tmp_path / "run-1").exists()


@pytest.mark.parametrize(
    "ref",
    [
        "artifact://run-1/../research-analysis",
        "artifact://run-1/research-analysis?raw=1",
        "artifact://run-1/research-analysis#fragment",
        "file://run-1/research-analysis",
    ],
)
def test_noncanonical_references_are_rejected(tmp_path, ref: str) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with pytest.raises((ArtifactStoreMetadataError, ValueError)):
        port.read_artifact(ref)
    with pytest.raises((ArtifactStoreMetadataError, ValueError)):
        port.verify_artifact_ref(ref, expected_run_id="run-1")


def test_artifact_byte_tamper_is_detected(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        ref = port.write_artifact(_context_request({"status": "ok"}))
    descriptor = port.list_staged_artifacts("run-1")[0]
    (tmp_path / "run-1" / descriptor.relative_path).write_bytes(b"tampered")

    with pytest.raises(ArtifactChecksumMismatchError):
        port.read_artifact(ref.ref)
    with pytest.raises(ArtifactChecksumMismatchError):
        port.verify_artifact_ref(ref.ref, expected_run_id="run-1")
    assert (
        port.describe_artifact_ref(ref.ref, expected_run_id="run-1").checksum
        == ref.checksum
    )


def test_artifact_descriptor_rejects_missing_authority_record(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        ref = port.write_artifact(_context_request({"status": "candidate"}))
    staging_record = port.terminal_store._staging_record_path(
        "run-1",
        _context_request({}).artifact_type,
    )
    staging_record.unlink()

    with pytest.raises(ArtifactNotFoundError):
        port.describe_artifact_ref(ref.ref, expected_run_id="run-1")


def test_graph_terminal_manifest_hash_is_required(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        port.write_artifact(_context_request({"status": "ok"}))
    _commit_terminal_manifest(port, "run-1")
    path = tmp_path / "run-1" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.pop("manifest_hash")
    port.manager.write_json("run-1", "manifest.json", manifest)

    with pytest.raises(GraphTerminalManifestError) as raised:
        port.read_terminal_manifest("run-1")
    assert raised.value.code is GraphTerminalManifestErrorCode.SCHEMA_INVALID


def test_graph_terminal_manifest_tamper_is_detected(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        port.write_artifact(_context_request({"status": "ok"}))
    _commit_terminal_manifest(port, "run-1")
    path = tmp_path / "run-1" / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["relative_path"] = "artifacts/forged.json"
    port.manager.write_json("run-1", "manifest.json", manifest)

    with pytest.raises(GraphTerminalManifestError) as raised:
        port.read_terminal_manifest("run-1")
    assert raised.value.code is GraphTerminalManifestErrorCode.HASH_MISMATCH


def test_unpublished_terminal_commit_is_failure_only_and_restart_safe(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    execution_versions = _execution_versions()
    with port.bind_run("run-1"):
        port.write_artifact(_context_request({"status": "gate-failed"}))

    committed = port.commit_unpublished_terminal_manifest(
        GraphTerminalManifestCommitRequest(
            context=GraphTerminalManifestContext(
                tenant_id="tenant-1",
                graph_id=execution_versions.graph_id,
                graph_version=execution_versions.graph_version,
                graph_schema_version=(
                    execution_versions.normalized_graph_schema_version
                ),
                compiler_version=execution_versions.compiler_version,
                normalized_graph_checksum=(
                    execution_versions.normalized_graph_checksum
                ),
                execution_versions=execution_versions,
                started_at=_NOW,
                terminal_node_ids=execution_versions.terminal_node_ids,
            ),
            run_id="run-1",
            status="failed",
            completed_at=_NOW + timedelta(seconds=1),
            terminal_state_ref=checksum_for({"state": "run-1"}),
            gate_evidence_refs=(checksum_for({"gate": "failed"}),),
        )
    )

    assert committed.status.value == "failed"
    assert committed.publication is None
    assert tuple(item.artifact_key for item in committed.artifacts) == (
        _context_request({}).artifact_type,
    )
    assert FilesystemHarnessArtifactPort(tmp_path).read_terminal_manifest(
        "run-1"
    ) == committed
    ref = f"artifact://run-1/{_context_request({}).artifact_type}"
    with pytest.raises(ArtifactPublicationVisibilityError) as hidden:
        port.read_artifact(ref)
    assert hidden.value.disposition == "quarantine"


def test_non_regular_artifact_is_rejected(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        ref = port.write_artifact(_context_request({"status": "ok"}))
    descriptor = port.list_staged_artifacts("run-1")[0]
    path = tmp_path / "run-1" / descriptor.relative_path
    path.unlink()
    path.mkdir()

    with pytest.raises(ArtifactStoreMetadataError, match="regular file"):
        port.read_artifact(ref.ref)


def test_symlinked_terminal_manifest_is_rejected(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"):
        port.write_artifact(_context_request({"status": "ok"}))
    _commit_terminal_manifest(port, "run-1")
    manifest_path = tmp_path / "run-1" / "manifest.json"
    target = manifest_path.with_name("manifest-target.json")
    target.write_bytes(manifest_path.read_bytes())
    manifest_path.unlink()
    try:
        manifest_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ArtifactStoreMetadataError, match="symlink"):
        port.read_terminal_manifest("run-1")


def test_symlinked_artifact_write_target_cannot_replace_existing_bytes(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    first = _context_request({"version": 1}, prefix="context-source-snapshot")
    second = _context_request({"version": 2}, prefix="context-result-snapshot")
    with port.bind_run("run-1"):
        committed = port.write_artifact(first)
    first_descriptor = port.list_staged_artifacts("run-1")[0]
    first_path = tmp_path / "run-1" / first_descriptor.relative_path
    malicious_target = tmp_path / "run-1" / port._canonical_path(second.artifact_type)
    try:
        malicious_target.symlink_to(first_path)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with port.bind_run("run-1"), pytest.raises(ArtifactStoreMetadataError):
        port.write_artifact(second)

    assert port.read_artifact(committed.ref)["payload"] == {"version": 1}


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_junctioned_artifact_directory_cannot_redirect_read_or_write(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    first = _context_request({"version": 1}, prefix="context-source-snapshot")
    second = _context_request({"version": 2}, prefix="context-result-snapshot")
    with port.bind_run("run-junction"):
        committed = port.write_artifact(first)

    run_dir = tmp_path / "run-junction"
    artifacts_dir = run_dir / "artifacts"
    committed_dir = run_dir / "committed-artifacts"
    victim_dir = run_dir / "victim"
    victim_dir.mkdir()
    victim_path = victim_dir / Path(port._canonical_path(second.artifact_type)).name
    victim_before = b'{"owner":"victim"}'
    victim_path.write_bytes(victim_before)
    artifacts_dir.rename(committed_dir)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(artifacts_dir), str(victim_dir)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        committed_dir.rename(artifacts_dir)
        pytest.skip("junction creation unavailable")

    try:
        with pytest.raises(ArtifactStoreMetadataError, match="reparse point"):
            port.read_artifact(committed.ref)
        with port.bind_run("run-junction"), pytest.raises(
            ArtifactStoreMetadataError,
            match="reparse point",
        ):
            port.write_artifact(second)
        assert victim_path.read_bytes() == victim_before
    finally:
        artifacts_dir.rmdir()
        committed_dir.rename(artifacts_dir)

    assert port.read_artifact(committed.ref)["payload"] == {"version": 1}


@pytest.mark.parametrize(
    ("content", "error_pattern"),
    [
        (b"not-json", "invalid artifact JSON"),
        (
            b'{"artifact_type":"graph-result-invalid","media_type":"application/json",'
            b'"metadata":{},"payload":{"score":NaN}}',
            "non-finite",
        ),
    ],
)
def test_noncanonical_json_is_rejected_after_integrity_verification(
    tmp_path,
    content: bytes,
    error_pattern: str,
) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    identity = "d" * 64
    artifact_type = f"graph-result-{identity}"
    relative_path = port._canonical_path(artifact_type)
    port.manager.write_bytes("run-1", relative_path, content)
    port.terminal_store.stage_artifact(
        run_id="run-1",
        artifact=GraphTerminalArtifact(
            artifact_key=artifact_type,
            artifact_id=artifact_type,
            ref=f"artifact://run-1/{artifact_type}",
            relative_path=relative_path,
            content_checksum=f"sha256:{compute_checksum(content)}",
            byte_size=len(content),
            media_type="application/json",
            node_id="analyze",
            attempt_id="attempt-1",
            required_for_replay=False,
            required_for_publication=False,
            metadata={
                "graph_result_ref_only": True,
                "identity_checksum": f"sha256:{identity}",
            },
        ),
    )

    with pytest.raises(ArtifactStoreMetadataError, match=error_pattern):
        port.read_graph_result_artifact(
            f"artifact://run-1/{artifact_type}",
            expected_run_id="run-1",
        )


def test_staging_record_failure_preserves_previous_member(tmp_path, monkeypatch) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    first = _context_request({"version": 1}, prefix="context-source-snapshot")
    second = _context_request({"version": 2}, prefix="context-result-snapshot")
    with port.bind_run("run-1"):
        committed = port.write_artifact(first)

    def fail_stage(*args, **kwargs):
        raise OSError("injected staging record failure")

    monkeypatch.setattr(port.terminal_store, "stage_artifact", fail_stage)
    with port.bind_run("run-1"), pytest.raises(OSError, match="staging record"):
        port.write_artifact(second)

    assert port.read_artifact(committed.ref)["payload"] == {"version": 1}
    assert tuple(
        artifact.artifact_key for artifact in port.list_staged_artifacts("run-1")
    ) == (first.artifact_type,)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_rejected(value: float, tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    with port.bind_run("run-1"), pytest.raises(
        ArtifactStoreMetadataError,
        match="non-finite",
    ):
        port.write_artifact(ArtifactWriteRequest("research-analysis", {"score": value}))


def test_max_write_bytes_is_enforced_before_filesystem_side_effect(tmp_path) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path, max_write_bytes=16)
    with port.bind_run("run-1"), pytest.raises(ValueError, match="max_write_bytes"):
        port.write_artifact(
            ArtifactWriteRequest("research-analysis", {"content": "large"})
        )
    assert not (tmp_path / "run-1").exists()


def test_artifact_diagnostics_are_allow_listed_and_exclude_payloads(
    tmp_path,
    caplog,
) -> None:
    run_id = "run-sensitive-identity"
    secrets = (
        "paper content must stay private",
        "prompt=hidden-system-instruction",
        "credential=provider-top-secret",
        str(tmp_path / "private" / "artifact.json"),
    )
    port = FilesystemHarnessArtifactPort(tmp_path)
    caplog.set_level(logging.INFO, logger=RESEARCH_PERSISTENCE_LOGGER)

    with port.bind_run(run_id):
        ref = port.write_artifact(
            _context_request(
                {
                    "paper_content": secrets[0],
                    "prompt": secrets[1],
                    "credential": secrets[2],
                    "path": secrets[3],
                }
            )
        )
    port.read_artifact(ref.ref)

    records = _research_persistence_records(caplog)
    assert [record.research_event_dimensions["operation"] for record in records] == [
        "artifact_write",
        "artifact_read",
    ]
    assert all(
        set(record.research_event_dimensions)
        == {
            "component",
            "operation",
            "outcome",
            "reason",
            "run_identity",
            "paper_identity",
        }
        for record in records
    )
    assert all(
        record.research_event_dimensions["outcome"] == "succeeded"
        and record.research_event_dimensions["reason"] == "completed"
        and record.research_event_dimensions["paper_identity"] == MISSING_IDENTITY_REF
        for record in records
    )
    assert records[0].research_event_dimensions["run_identity"] == (
        records[1].research_event_dimensions["run_identity"]
    )
    assert records[0].research_event_dimensions["run_identity"].startswith(
        "redacted:sha256:"
    )
    for secret in (*secrets, run_id):
        assert secret not in caplog.text
        assert all(secret not in repr(record.research_event_dimensions) for record in records)


def test_artifact_failure_diagnostic_excludes_raw_exception_and_path(
    tmp_path,
    caplog,
    monkeypatch,
) -> None:
    raw_exception = f"credential=secret path={tmp_path / 'private' / 'manifest.json'}"
    port = FilesystemHarnessArtifactPort(tmp_path)
    caplog.set_level(logging.INFO, logger=RESEARCH_PERSISTENCE_LOGGER)

    def fail_write(*_args, **_kwargs):
        raise OSError(raw_exception)

    monkeypatch.setattr(port.manager, "write_bytes", fail_write)
    with port.bind_run("run-failure"), pytest.raises(OSError, match="credential"):
        port.write_artifact(
            ArtifactWriteRequest(
                "research-analysis",
                {"paper_content": "must-not-be-logged"},
            )
        )

    records = _research_persistence_records(caplog)
    assert len(records) == 1
    assert records[0].research_event_dimensions["outcome"] == "failed"
    assert records[0].research_event_dimensions["reason"] == "filesystem_unavailable"
    assert records[0].exc_info is None
    assert records[0].stack_info is None
    assert raw_exception not in caplog.text
    assert "must-not-be-logged" not in caplog.text


def _context_request(
    payload,
    *,
    identity: str = "a" * 64,
    prefix: str = "context-source-snapshot",
) -> ArtifactWriteRequest:
    return ArtifactWriteRequest(
        f"{prefix}-{identity}",
        payload,
        metadata={
            "context_ref_only": True,
            "identity_checksum": f"sha256:{identity}",
        },
    )


def _graph_request(payload, *, identity: str = "b" * 64) -> ArtifactWriteRequest:
    return ArtifactWriteRequest(
        f"graph-result-{identity}",
        payload,
        metadata={
            "run_id": "run-1",
            "graph_result_ref_only": True,
            "identity_checksum": f"sha256:{identity}",
        },
    )


def _commit_terminal_manifest(
    port: FilesystemHarnessArtifactPort,
    run_id: str,
) -> GraphTerminalManifestV2:
    execution_versions = _execution_versions()
    return port.write_terminal_manifest(
        GraphTerminalManifestV2(
            execution_versions=execution_versions,
            terminal=GraphTerminalManifest(
                tenant_id="tenant-1",
                run_id=run_id,
                graph_id=execution_versions.graph_id,
                graph_version=execution_versions.graph_version,
                graph_schema_version=(
                    execution_versions.normalized_graph_schema_version
                ),
                compiler_version=execution_versions.compiler_version,
                normalized_graph_checksum=(
                    execution_versions.normalized_graph_checksum
                ),
                status="succeeded",
                started_at=_NOW,
                completed_at=_NOW + timedelta(seconds=1),
                terminal_state_ref=checksum_for({"state": run_id}),
                checkpoint_ref=f"graph-state://{run_id}/terminal",
                terminal_node_ids=execution_versions.terminal_node_ids,
                gate_evidence_refs=(checksum_for({"gate": run_id}),),
                artifacts=port.list_staged_artifacts(run_id),
            ),
        )
    )


def _execution_versions() -> GraphExecutionVersionManifest:
    graph = HarnessGraphCompiler().compile(
        build_paper_analysis_graph_definition()
    ).graph
    return GraphExecutionVersionManifest.from_normalized_graph(graph)


def _research_persistence_records(caplog):
    return [
        record
        for record in caplog.records
        if record.msg == RESEARCH_PERSISTENCE_OPERATION_EVENT
    ]
