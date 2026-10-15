from __future__ import annotations

import json
from pathlib import Path

import pytest

from interfaces.services.research_history_service import ResearchHistoryError, ResearchHistoryService, ResearchHistoryConflictError


def items() -> dict:
    return {
        "materials": [{"id": "source-a", "groupId": "group-a", "kind": "paper", "title": "A paper", "url": "https://arxiv.org/abs/2605.22343", "notes": "First line\nSecond line", "favorite": False, "readingStatus": "unread", "tags": [], "createdAt": 1, "updatedAt": 2}],
        "reportDrafts": [{"id": "draft-a", "groupId": "group-a", "question": "Agent", "title": "Agent report", "scope": "Evaluation", "notes": "Working notes", "materials": [{"id": "source-a", "title": "A paper", "url": "https://arxiv.org/abs/2605.22343", "notes": "Read methods"}], "updatedAt": 2}],
        "prompts": [{"id": "prompt-a", "name": "Agent papers", "question": "Find Agent papers", "mode": "papers", "constraints": {"recentYear": True, "hasCode": True}, "updatedAt": 2}],
        "composerDraft": {"question": "Not submitted", "mode": "papers", "constraints": {}, "groupId": "group-a", "updatedAt": 2},
    }


GROUPS = [{"id": "group-a", "name": "Agent", "createdAt": 1, "updatedAt": 2}]


def test_private_pdf_source_identity_survives_history_without_a_reader_link(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    pdf = {**items()["materials"][0], "kind": "pdf", "url": "", "referenceId": "private-paper"}
    saved = service.replace(user_id="a", revision=0, visits=[], groups=GROUPS, workspace_items={"materials": [pdf]})
    assert saved.workspace_items["materials"] == [pdf]
    assert ResearchHistoryService(tmp_path / "history.json").read(user_id="a").workspace_items["materials"] == [pdf]
    assert service.read(user_id="b").workspace_items == {}
    del pdf["referenceId"]
    with pytest.raises(ResearchHistoryError):
        service.replace(user_id="a", revision=1, visits=[], groups=GROUPS, workspace_items={"materials": [pdf]})


def visit(module: str = "reports") -> dict:
    return {"id": "visit-a", "module": module, "question": "Agent", "href": f"/{module}?question=Agent&researchSession=visit-a", "scrollY": 20, "title": "Agent", "groupId": "group-a", "isFavorite": False, "createdAt": 1, "updatedAt": 2, "deletedAt": None, "archivedAt": 2,
            "activity": {"kind": "report", "draftId": "draft-a", "title": "Agent report", "updatedAt": 2}}


def test_owned_workspace_round_trip_and_old_client_preservation(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    service = ResearchHistoryService(path)
    original = items()
    saved = service.replace(user_id="a", revision=0, visits=[visit()], groups=GROUPS, workspace_items=original)
    assert saved.workspace_items == original
    reopened = ResearchHistoryService(path)
    assert reopened.read(user_id="a").visits[0]["activity"]["draftId"] == "draft-a"
    assert reopened.read(user_id="b").workspace_items == {}
    next_snapshot = reopened.replace(user_id="a", revision=1, visits=[visit()], groups=GROUPS)
    assert next_snapshot.workspace_items == original
    with pytest.raises(ResearchHistoryConflictError):
        reopened.replace(user_id="a", revision=1, visits=[], groups=[], workspace_items={"materials": []})
    assert reopened.read(user_id="a").workspace_items == original


def test_legacy_snapshot_remains_readable_without_inventing_content(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"schemaVersion": "newsroom_research_history.v1", "users": {"a": {"revision": 3, "visits": [], "groups": []}}}), encoding="utf-8")
    snapshot = ResearchHistoryService(path).read(user_id="a")
    assert snapshot.to_dict() == {"revision": 3, "visits": [], "groups": []}


@pytest.mark.parametrize("damage", ["group", "draft", "reader", "owner", "duplicate", "boolean-time", "null-condition", "missing-source", "composer-source", "duplicate-composer-source"])
def test_invalid_owned_references_and_payloads_leave_storage_untouched(tmp_path: Path, damage: str) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    content, records = items(), [visit()]
    if damage == "group": content["materials"][0]["groupId"] = "other-account-group"
    elif damage == "draft": records[0]["activity"]["draftId"] = "other-account-draft"
    elif damage == "reader":
        records = [visit("papers")]
        records[0]["activity"] = {"kind": "reader", "paperId": "paper-a", "title": "A", "href": "//attacker.example/papers/a/read", "updatedAt": 2}
    elif damage == "owner": content["materials"][0]["owner"] = "other-account"
    elif damage == "duplicate": content["materials"] *= 2
    elif damage == "boolean-time": content["materials"][0]["updatedAt"] = True
    elif damage == "null-condition": content["prompts"][0]["constraints"]["paperType"] = None
    elif damage == "missing-source": content["materials"][0]["url"] = ""
    elif damage == "composer-source": content["composerDraft"]["materialIds"] = ["other-owner-source"]
    elif damage == "duplicate-composer-source": content["composerDraft"]["materialIds"] = ["source-a", "source-a"]
    with pytest.raises(ResearchHistoryError):
        service.replace(user_id="a", revision=0, visits=records, groups=GROUPS, workspace_items=content)
    assert service.read(user_id="a").revision == 0


def test_document_byte_limit_is_enforced_before_durable_write(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    material = items()["materials"][0]
    materials = [{**material, "id": f"source-{i}", "notes": "研" * 10000} for i in range(150)]
    with pytest.raises(ResearchHistoryError, match="4 MiB"):
        service.replace(user_id="a", revision=0, visits=[], groups=GROUPS, workspace_items={"materials": materials})
    assert service.read(user_id="a").revision == 0


def test_group_and_draft_deletion_cannot_leave_hidden_dangling_data(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    service.replace(user_id="a", revision=0, visits=[visit()], groups=GROUPS, workspace_items=items())
    with pytest.raises(ResearchHistoryError):
        service.replace(user_id="a", revision=1, visits=[], groups=[])
    with pytest.raises(ResearchHistoryError):
        service.replace(user_id="a", revision=1, visits=[visit()], groups=GROUPS, workspace_items={"reportDrafts": []})
    assert service.read(user_id="a").revision == 1


def test_private_analysis_and_reader_selection_state_are_revision_bound(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    fingerprint = "sha256:" + "a" * 64
    workspace = {
        "pendingOperations": [{
            "requestId": "request-0001", "generation": "gen_" + "b" * 32,
            "kind": "analyze", "fingerprint": fingerprint,
            "semantic": {
                "kind": "analyze", "paperId": "2401.01234",
                "sourceRevision": fingerprint, "sourceFingerprint": fingerprint,
                "sourceUrl": None, "pdfUrl": None,
                "objective": "Analyze", "constraints": [],
            },
            "status": "prepared", "paperId": "2401.01234",
            "sourceRevision": fingerprint, "createdAt": 1, "updatedAt": 1,
        }],
        "readerSelections": [{
            "id": "selection-1", "paperId": "2401.01234", "sourceRevision": fingerprint,
            "sectionId": "methods", "paragraphId": "paragraph-1",
            "selectedText": "bounded retrieval", "surroundingText": "uses bounded retrieval",
            "startOffset": 5, "endOffset": 22, "createdAt": 1, "updatedAt": 2,
        }],
        "readerEvents": [{
            "id": "event-1", "type": "selection_created",
            "paperId": "2401.01234", "sourceRevision": fingerprint,
            "selectionId": "selection-1", "createdAt": 2,
        }],
    }

    saved = service.replace(user_id="alice", revision=0, visits=[], groups=[], workspace_items=workspace)
    assert saved.workspace_items == workspace

    workspace["readerEvents"][0]["sourceRevision"] = "other-revision"
    with pytest.raises(ResearchHistoryError):
        service.replace(user_id="alice", revision=1, visits=[], groups=[], workspace_items=workspace)


def test_prepare_operation_round_trips_without_mutating_fixed_semantic(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    workspace = {
        "pendingOperations": [{
            "requestId": "prepare-0001",
            "generation": "gen_" + "b" * 32,
            "kind": "prepare",
            "fingerprint": "sha256:" + "c" * 64,
            "semantic": {
                "kind": "prepare",
                "paperId": "2401.01234",
                "sourceUrl": "https://arxiv.org/abs/2401.01234",
                "sourceType": None,
            },
            "status": "prepared",
            "paperId": "2401.01234",
            "createdAt": 1,
            "updatedAt": 1,
        }],
    }

    saved = service.replace(
        user_id="alice", revision=0, visits=[], groups=[], workspace_items=workspace
    )
    reopened = ResearchHistoryService(tmp_path / "history.json").read(user_id="alice")

    assert saved.workspace_items == workspace
    assert reopened.workspace_items["pendingOperations"][0]["semantic"] == workspace["pendingOperations"][0]["semantic"]
    assert "sourceRevision" not in reopened.workspace_items["pendingOperations"][0]["semantic"]


def test_shared_request_semantics_all_round_trip_as_fixed_pending_operations(
    tmp_path: Path,
) -> None:
    fixture_path = Path(__file__).resolve().parents[2] / "fixtures" / "research_desktop_request_contracts.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]
    operations = []
    for index, case in enumerate(fixtures, start=1):
        semantic = dict(case["semantic"])
        operation = {
            "requestId": f"fixture-request-{index}",
            "generation": "gen_" + "b" * 32,
            "kind": semantic["kind"],
            "fingerprint": case["fingerprint"],
            "semantic": semantic,
            "status": "prepared",
            "createdAt": index,
            "updatedAt": index,
        }
        if isinstance(semantic.get("paperId"), str):
            operation["paperId"] = semantic["paperId"]
        if isinstance(semantic.get("sourceRevision"), str):
            operation["sourceRevision"] = semantic["sourceRevision"]
        if isinstance(semantic.get("question"), str):
            operation["question"] = semantic["question"]
        operations.append(operation)

    workspace = {"pendingOperations": operations}
    service = ResearchHistoryService(tmp_path / "history.json")
    saved = service.replace(
        user_id="alice", revision=0, visits=[], groups=[], workspace_items=workspace,
    )
    reopened = ResearchHistoryService(tmp_path / "history.json").read(user_id="alice")

    assert saved.workspace_items == workspace
    assert [item["semantic"] for item in reopened.workspace_items["pendingOperations"]] == [
        case["semantic"] for case in fixtures
    ]

def test_reader_notebook_allows_20000_chars_but_selection_note_stays_bounded(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    revision = "sha256:" + "a" * 64
    notebook = {
        "paperId": "2601.01234", "sourceRevision": revision, "note": "研" * 20000,
        "question": "", "fontSize": 18, "sectionId": "", "pdfPage": 1, "updatedAt": 2,
    }
    selection = {
        "id": "selection-1", "paperId": "2601.01234", "sourceRevision": revision,
        "sectionId": "methods", "paragraphId": "paragraph-1",
        "selectedText": "bounded retrieval", "surroundingText": "uses bounded retrieval",
        "startOffset": 5, "endOffset": 22, "noteText": "注" * 10000,
        "createdAt": 1, "updatedAt": 2,
    }

    saved = service.replace(
        user_id="alice", revision=0, visits=[], groups=[],
        workspace_items={"readerNotebooks": [notebook], "readerSelections": [selection]},
    )
    assert saved.workspace_items["readerNotebooks"][0]["note"] == "研" * 20000
    assert saved.workspace_items["readerSelections"][0]["noteText"] == "注" * 10000

    notebook["note"] += "超"
    with pytest.raises(ResearchHistoryError):
        service.replace(
            user_id="alice", revision=1, visits=[], groups=[],
            workspace_items={"readerNotebooks": [notebook], "readerSelections": [selection]},
        )
    notebook["note"] = notebook["note"][:-1]

    selection["noteText"] += "超"
    with pytest.raises(ResearchHistoryError):
        service.replace(
            user_id="alice", revision=1, visits=[], groups=[],
            workspace_items={"readerNotebooks": [notebook], "readerSelections": [selection]},
        )


def test_reader_notebook_view_defaults_to_text_and_rejects_invalid_fields(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    revision = "sha256:" + "b" * 64
    notebook = {
        "paperId": "2601.01234", "sourceRevision": revision, "note": "",
        "question": "", "fontSize": 18, "sectionId": "", "pdfPage": 1, "updatedAt": 2,
    }

    legacy = service.replace(
        user_id="alice", revision=0, visits=[], groups=[],
        workspace_items={"readerNotebooks": [notebook]},
    )
    assert legacy.workspace_items["readerNotebooks"][0].get("readerView", "text") == "text"

    notebook["readerView"] = "pdf"
    saved = service.replace(
        user_id="alice", revision=1, visits=[], groups=[],
        workspace_items={"readerNotebooks": [notebook]},
    )
    assert saved.workspace_items["readerNotebooks"][0]["readerView"] == "pdf"

    notebook["readerView"] = "slides"
    with pytest.raises(ResearchHistoryError):
        service.replace(
            user_id="alice", revision=2, visits=[], groups=[],
            workspace_items={"readerNotebooks": [notebook]},
        )

    notebook["readerView"] = "text"
    notebook["unexpected"] = True
    with pytest.raises(ResearchHistoryError):
        service.replace(
            user_id="alice", revision=2, visits=[], groups=[],
            workspace_items={"readerNotebooks": [notebook]},
        )


def test_legacy_material_defaults_are_migrated_and_new_state_survives_cas(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    service = ResearchHistoryService(path)
    legacy = {
        "id": "source-legacy",
        "groupId": "group-a",
        "kind": "paper",
        "title": "Legacy paper",
        "url": "https://example.test/legacy",
        "notes": "",
        "createdAt": 1,
        "updatedAt": 1,
    }

    migrated = service.replace(
        user_id="a",
        revision=0,
        visits=[],
        groups=GROUPS,
        workspace_items={"materials": [legacy]},
    )
    assert migrated.workspace_items["materials"][0] == {
        **legacy,
        "favorite": False,
        "readingStatus": "unread",
        "tags": [],
    }

    updated = {
        **migrated.workspace_items["materials"][0],
        "favorite": True,
        "readingStatus": "read",
        "tags": ["RAG", "Methods"],
        "updatedAt": 2,
    }
    saved = service.replace(
        user_id="a",
        revision=1,
        visits=[],
        groups=GROUPS,
        workspace_items={"materials": [updated]},
    )
    assert saved.workspace_items["materials"] == [updated]
    assert ResearchHistoryService(path).read(user_id="a").workspace_items["materials"] == [updated]


@pytest.mark.parametrize(
    "tags",
    [
        [""],
        [" leading"],
        ["duplicate", "DUPLICATE"],
        ["x" * 65],
        [f"tag-{index}" for index in range(33)],
    ],
)
def test_material_tags_are_bounded_trimmed_and_unique(tmp_path: Path, tags: list[str]) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    material = {**items()["materials"][0], "tags": tags}

    with pytest.raises(ResearchHistoryError):
        service.replace(
            user_id="a",
            revision=0,
            visits=[],
            groups=GROUPS,
            workspace_items={"materials": [material]},
        )
