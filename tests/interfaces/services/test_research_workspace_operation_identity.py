from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from interfaces.services.research_history_service import (
    ResearchHistoryError,
    ResearchHistoryService,
)


KINDS = ("prepare", "import", "analyze", "ask")
NON_PASSED_STATUSES = (
    "prepared", "waiting", "running", "needs_action", "rejected", "failed",
    "interrupted", "unknown",
)
STATUSES = (*NON_PASSED_STATUSES, "passed")


def _pending(kind: str, *, status: str = "prepared") -> dict:
    fixture_path = Path(__file__).resolve().parents[2] / "fixtures" / "research_desktop_request_contracts.json"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]
    case = next(case for case in cases if case["semantic"]["kind"] == kind)
    semantic = case["semantic"]
    operation = {
        "requestId": f"request-{kind}-0001",
        "generation": "gen_" + "b" * 32,
        "kind": kind,
        "fingerprint": case["fingerprint"],
        "semantic": semantic,
        "status": status,
        "createdAt": 1,
        "updatedAt": 2,
    }
    for field in ("paperId", "sourceRevision", "sourceFingerprint", "question"):
        if field in semantic:
            operation[field] = semantic[field]
    return operation


def _completed(kind: str) -> dict:
    operation = _pending(kind, status="passed")
    if kind == "import":
        operation.update(paperId="imported-paper-001", importId="import-0001")
    elif kind == "prepare":
        operation.update(
            sourceRevision="prepared-source-revision-1",
            sourceFingerprint="sha256:" + "e" * 64,
        )
    return operation


def _replace(service: ResearchHistoryService, operation: dict, *, revision: int = 0):
    return service.replace(
        user_id="alice", revision=revision, visits=[], groups=[],
        workspace_items={"pendingOperations": [operation]},
    )


def _assert_rejected_without_overwrite(tmp_path: Path, operation: dict) -> None:
    path = tmp_path / "history.json"
    service = ResearchHistoryService(path)
    saved = _replace(service, _pending(operation["kind"]))
    before = path.read_bytes()
    with pytest.raises(ResearchHistoryError) as error:
        _replace(service, operation, revision=saved.revision)
    assert error.value.code == "research_history_invalid"
    assert path.read_bytes() == before
    assert ResearchHistoryService(path).read(user_id="alice").to_dict() == saved.to_dict()


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", STATUSES)
def test_fixed_input_identity_survives_every_operation_status(
    tmp_path: Path, kind: str, status: str,
) -> None:
    operation = _pending(kind, status=status)
    path = tmp_path / "history.json"
    saved = _replace(ResearchHistoryService(path), operation)
    reopened = ResearchHistoryService(path).read(user_id="alice")
    assert saved.workspace_items == {"pendingOperations": [operation]}
    assert reopened.to_dict() == saved.to_dict()


@pytest.mark.parametrize("kind", ("import", "prepare"))
def test_completed_output_identity_round_trips_after_pending_history_reopen(
    tmp_path: Path, kind: str,
) -> None:
    path = tmp_path / "history.json"
    pending = _pending(kind)
    fixed_semantic = deepcopy(pending["semantic"])
    first = _replace(ResearchHistoryService(path), pending)
    completed = _completed(kind)
    saved = _replace(ResearchHistoryService(path), completed, revision=first.revision)
    reopened = ResearchHistoryService(path).read(user_id="alice")
    assert saved.revision == first.revision + 1
    assert reopened.to_dict() == saved.to_dict()
    assert reopened.workspace_items == {"pendingOperations": [completed]}
    assert reopened.workspace_items["pendingOperations"][0]["semantic"] == fixed_semantic
    if kind == "import":
        assert "paperId" not in fixed_semantic
    else:
        assert "sourceRevision" not in fixed_semantic
        assert "sourceFingerprint" not in fixed_semantic
    assert ResearchHistoryService(path).read(user_id="bob").workspace_items == {}


@pytest.mark.parametrize("kind", ("import", "prepare"))
@pytest.mark.parametrize("status", NON_PASSED_STATUSES)
def test_output_identity_cannot_be_claimed_before_passed(
    tmp_path: Path, kind: str, status: str,
) -> None:
    operation = _completed(kind)
    operation["status"] = status
    _assert_rejected_without_overwrite(tmp_path, operation)


@pytest.mark.parametrize("missing", ("importId", "sourceRevision", "sourceFingerprint"))
def test_import_canonical_paper_requires_complete_output_reference(
    tmp_path: Path, missing: str,
) -> None:
    operation = _completed("import")
    del operation[missing]
    _assert_rejected_without_overwrite(tmp_path, operation)


@pytest.mark.parametrize("missing", ("sourceRevision", "sourceFingerprint"))
def test_prepare_output_source_identity_must_be_a_pair(tmp_path: Path, missing: str) -> None:
    operation = _completed("prepare")
    del operation[missing]
    _assert_rejected_without_overwrite(tmp_path, operation)


@pytest.mark.parametrize("paper_id", ("", " ", "../paper", "paper/id", "paper?token=secret", "paper\n", "p" * 201))
def test_import_canonical_paper_identity_is_bounded_and_safe(
    tmp_path: Path, paper_id: str,
) -> None:
    operation = _completed("import")
    operation["paperId"] = paper_id
    _assert_rejected_without_overwrite(tmp_path, operation)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fingerprint", (
    "", "a" * 64, "sha256:" + "a" * 63, "sha256:" + "a" * 65,
    "sha256:" + "A" * 64, "sha256:" + "g" * 64,
    "SHA256:" + "a" * 64, " sha256:" + "a" * 64,
    "sha256:" + "a" * 64 + "\n", None, 123,
))
def test_output_source_fingerprint_requires_exact_sha256(
    tmp_path: Path, kind: str, fingerprint: object,
) -> None:
    operation = _completed(kind)
    operation["sourceFingerprint"] = fingerprint
    _assert_rejected_without_overwrite(tmp_path, operation)


@pytest.mark.parametrize("kind,field,replacement", (
    ("prepare", "paperId", "other-paper"),
    ("analyze", "paperId", "other-paper"),
    ("ask", "paperId", "other-paper"),
    ("import", "sourceRevision", "other-revision"),
    ("analyze", "sourceRevision", "other-revision"),
    ("ask", "sourceRevision", "other-revision"),
    ("import", "sourceFingerprint", "sha256:" + "f" * 64),
    ("analyze", "sourceFingerprint", "sha256:" + "f" * 64),
    ("ask", "sourceFingerprint", "sha256:" + "f" * 64),
    ("ask", "question", "A different question"),
    ("prepare", "question", "An unsupported question"),
    ("import", "question", "An unsupported question"),
    ("analyze", "question", "An unsupported question"),
))
@pytest.mark.parametrize("status", STATUSES)
def test_fixed_semantic_identity_cannot_be_replaced_in_any_status(
    tmp_path: Path, kind: str, field: str, replacement: str, status: str,
) -> None:
    operation = _completed(kind) if status == "passed" else _pending(kind, status=status)
    operation[field] = replacement
    _assert_rejected_without_overwrite(tmp_path, operation)


@pytest.mark.parametrize("kind", KINDS)
def test_legacy_pending_operations_keep_omitted_identity_fields_omitted(
    tmp_path: Path, kind: str,
) -> None:
    operation = _pending(kind)
    for field in ("paperId", "sourceRevision", "sourceFingerprint", "question"):
        operation.pop(field, None)
    path = tmp_path / "history.json"
    saved = _replace(ResearchHistoryService(path), operation)
    assert saved.workspace_items == {"pendingOperations": [operation]}
    assert ResearchHistoryService(path).read(user_id="alice").workspace_items == saved.workspace_items


@pytest.mark.parametrize("kind", KINDS)
def test_history_stores_only_receipt_references_without_inventing_authority(
    tmp_path: Path, kind: str,
) -> None:
    # No desktop admission/gate exists in this fixture. History is not that gate:
    # it preserves a well-shaped client reference without claiming verification.
    operation = _completed(kind)
    snapshot = _replace(ResearchHistoryService(tmp_path / "history.json"), operation)
    stored = snapshot.workspace_items["pendingOperations"][0]
    assert stored == operation
    assert "verified" not in stored
    assert "result" not in stored
    assert "qualityGate" not in stored
    assert "recoverable" not in stored


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("field,value", (
    ("verified", True), ("result", {"status": "ready"}), ("qualityGate", {"passed": True}),
))
def test_history_rejects_embedded_authority_flags_and_receipts(
    tmp_path: Path, kind: str, field: str, value: object,
) -> None:
    operation = _completed(kind)
    operation[field] = value
    _assert_rejected_without_overwrite(tmp_path, operation)


@pytest.mark.parametrize("kind,field,value", (
    ("import", "paperId", "injected-semantic-paper"),
    ("prepare", "sourceRevision", "injected-semantic-revision"),
    ("prepare", "sourceFingerprint", "sha256:" + "e" * 64),
))
def test_outputs_are_not_added_to_fixed_input_semantic(
    tmp_path: Path, kind: str, field: str, value: str,
) -> None:
    operation = _completed(kind)
    operation["semantic"][field] = value
    _assert_rejected_without_overwrite(tmp_path, operation)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("field,value", (
    ("generation", "gen_invalid"),
    ("fingerprint", "sha256:invalid"),
    ("updatedAt", 0),
    ("updatedAt", 1),
))
def test_existing_request_identity_and_time_constraints_are_preserved(
    tmp_path: Path, kind: str, field: str, value: object,
) -> None:
    operation = _completed(kind)
    operation["createdAt"] = 2
    operation["updatedAt"] = 3
    operation[field] = value
    _assert_rejected_without_overwrite(tmp_path, operation)


@pytest.mark.parametrize("kind", KINDS)
def test_operation_kind_must_still_match_fixed_semantic(tmp_path: Path, kind: str) -> None:
    operation = _completed(kind)
    operation["kind"] = "ask" if kind != "ask" else "prepare"
    _assert_rejected_without_overwrite(tmp_path, operation)
