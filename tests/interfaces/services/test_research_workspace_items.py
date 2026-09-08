from __future__ import annotations

import json
from pathlib import Path

import pytest

from interfaces.services.research_history_service import ResearchHistoryError, ResearchHistoryService, ResearchHistoryConflictError


def items() -> dict:
    return {
        "materials": [{"id": "source-a", "groupId": "group-a", "kind": "paper", "title": "A paper", "url": "https://arxiv.org/abs/2605.22343", "notes": "First line\nSecond line", "createdAt": 1, "updatedAt": 2}],
        "reportDrafts": [{"id": "draft-a", "groupId": "group-a", "question": "Agent", "title": "Agent report", "scope": "Evaluation", "notes": "Working notes", "materials": [{"id": "source-a", "title": "A paper", "url": "https://arxiv.org/abs/2605.22343", "notes": "Read methods"}], "updatedAt": 2}],
        "prompts": [{"id": "prompt-a", "name": "Agent papers", "question": "Find Agent papers", "mode": "papers", "constraints": {"recentYear": True, "hasCode": True}, "updatedAt": 2}],
        "composerDraft": {"question": "Not submitted", "mode": "papers", "constraints": {}, "groupId": "group-a", "updatedAt": 2},
    }


GROUPS = [{"id": "group-a", "name": "Agent", "createdAt": 1, "updatedAt": 2}]


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
