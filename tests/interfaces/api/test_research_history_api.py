from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from interfaces.api import create_app
from interfaces.services.auth_service import AuthApplicationService
from interfaces.services.research_history_service import ResearchHistoryService


def _client(tmp_path: Path) -> tuple[TestClient, AuthApplicationService]:
    auth = AuthApplicationService(
        user_store_path=tmp_path / "users.json",
        session_store_path=tmp_path / "sessions.json",
    )
    history = ResearchHistoryService(tmp_path / "history.json")
    client = TestClient(
        create_app(
            auth_service_factory=lambda: auth,
            research_history_service_factory=lambda: history,
            audit_emitter_factory=None,
        )
    )
    return client, auth


def _session(client: TestClient, username: str = "researcher") -> str:
    response = client.post(
        "/api/v1/auth/bootstrap",
        json={"username": username, "password": "long-enough-password"},
    )
    return response.json()["data"]["session"]["sessionToken"]


def _visit() -> dict:
    return {
        "id": "papers:one",
        "module": "papers",
        "question": "Agent",
        "href": "/papers?question=Agent",
        "scrollY": 12,
        "createdAt": 1,
        "updatedAt": 1,
        "title": "Agent",
        "groupId": None,
        "isFavorite": False,
        "deletedAt": None,
    }


def test_history_api_requires_auth_and_keeps_users_isolated(tmp_path: Path) -> None:
    client, auth = _client(tmp_path)
    assert client.get("/api/v1/research/history").status_code == 401
    first_token = _session(client)
    second_token = auth.create_identity_session(
        provider="google",
        subject="second-user-subject",
        username="second-researcher",
    ).sessionToken
    assert second_token

    saved = client.put(
        "/api/v1/research/history",
        headers={"X-Newsroom-Session": first_token},
        json={"revision": 0, "visits": [_visit()], "groups": []},
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["revision"] == 1
    loaded = client.get("/api/v1/research/history", headers={"X-Newsroom-Session": first_token})
    assert loaded.json()["data"]["visits"][0]["question"] == "Agent"
    isolated = client.get("/api/v1/research/history", headers={"X-Newsroom-Session": second_token})
    assert isolated.json()["data"] == {"revision": 0, "visits": [], "groups": []}


def test_history_api_returns_conflict_without_overwriting_newer_data(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    token = _session(client)
    headers = {"X-Newsroom-Session": token}
    first = client.put("/api/v1/research/history", headers=headers, json={"revision": 0, "visits": [_visit()], "groups": []})
    assert first.status_code == 200

    conflict = client.put("/api/v1/research/history", headers=headers, json={"revision": 0, "visits": [], "groups": []})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "research_history_conflict"
    assert client.get("/api/v1/research/history", headers=headers).json()["data"]["visits"]


def test_history_api_rejects_external_href(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    token = _session(client)
    visit = _visit()
    visit["href"] = "https://evil.example/?question=Agent"
    response = client.put(
        "/api/v1/research/history",
        headers={"X-Newsroom-Session": token},
        json={"revision": 0, "visits": [visit], "groups": []},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "research_history_invalid"


def test_history_api_rejects_user_supplied_owner_and_missing_title(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    token = _session(client)
    visit = _visit()
    visit.pop("title")
    response = client.put(
        "/api/v1/research/history",
        headers={"X-Newsroom-Session": token},
        json={"revision": 0, "visits": [visit], "groups": [], "owner": "attacker"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def _history_pending_operation(kind: str, *, passed: bool = False) -> dict:
    fixture_path = Path(__file__).resolve().parents[2] / "fixtures" / "research_desktop_request_contracts.json"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]
    case = next(case for case in cases if case["semantic"]["kind"] == kind)
    semantic = case["semantic"]
    operation = {
        "requestId": f"history-{kind}-0001",
        "generation": "gen_" + "b" * 32,
        "kind": kind,
        "fingerprint": case["fingerprint"],
        "semantic": semantic,
        "status": "passed" if passed else "prepared",
        "createdAt": 1,
        "updatedAt": 2 if passed else 1,
    }
    for field in ("paperId", "sourceRevision", "sourceFingerprint", "question"):
        if field in semantic:
            operation[field] = semantic[field]
    if passed and kind == "import":
        operation.update(paperId="imported-paper-001", importId="import-0001")
    elif passed and kind == "prepare":
        operation.update(
            sourceRevision="prepared-source-revision-1",
            sourceFingerprint="sha256:" + "e" * 64,
        )
    return operation


@pytest.mark.parametrize("kind", ("import", "prepare", "analyze", "ask"))
def test_history_api_preserves_operation_receipt_reference_after_reopen(
    tmp_path: Path, kind: str,
) -> None:
    client, auth = _client(tmp_path)
    headers = {"X-Newsroom-Session": _session(client)}
    pending = _history_pending_operation(kind)
    first = client.put(
        "/api/v1/research/history", headers=headers,
        json={"revision": 0, "visits": [], "groups": [], "pendingOperations": [pending]},
    )
    assert first.status_code == 200
    assert first.json()["data"]["pendingOperations"] == [pending]

    # Re-create both service and API composition against the same durable files.
    reopened_client, _ = _client(tmp_path)
    restored_pending = reopened_client.get("/api/v1/research/history", headers=headers)
    assert restored_pending.status_code == 200
    assert restored_pending.json()["data"] == first.json()["data"]
    completed = _history_pending_operation(kind, passed=True)
    saved = reopened_client.put(
        "/api/v1/research/history", headers=headers,
        json={
            "revision": first.json()["data"]["revision"],
            "visits": [], "groups": [], "pendingOperations": [completed],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["revision"] == 2
    assert saved.json()["data"]["pendingOperations"] == [completed]
    final_client, _ = _client(tmp_path)
    restored = final_client.get("/api/v1/research/history", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["data"] == saved.json()["data"]
    stored = restored.json()["data"]["pendingOperations"][0]
    assert stored["semantic"] == pending["semantic"]
    assert (stored["requestId"], stored["generation"], stored["kind"], stored["fingerprint"]) == (
        pending["requestId"], pending["generation"], pending["kind"], pending["fingerprint"],
    )
    assert "verified" not in stored
    assert "qualityGate" not in stored
    assert "result" not in stored
    other_token = auth.create_identity_session(
        provider="google", subject="other-history-owner", username="other-researcher",
    ).sessionToken
    assert other_token
    isolated = final_client.get(
        "/api/v1/research/history", headers={"X-Newsroom-Session": other_token},
    )
    assert isolated.status_code == 200
    assert isolated.json()["data"] == {"revision": 0, "visits": [], "groups": []}


@pytest.mark.parametrize("kind", ("import", "prepare"))
@pytest.mark.parametrize("status", (
    "prepared", "waiting", "running", "needs_action", "rejected", "failed", "interrupted", "unknown",
))
def test_history_api_rejects_output_identity_for_non_passed_operations(
    tmp_path: Path, kind: str, status: str,
) -> None:
    client, _ = _client(tmp_path)
    headers = {"X-Newsroom-Session": _session(client)}
    operation = _history_pending_operation(kind, passed=True)
    operation["status"] = status
    response = client.put(
        "/api/v1/research/history", headers=headers,
        json={"revision": 0, "visits": [], "groups": [], "pendingOperations": [operation]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    loaded = client.get("/api/v1/research/history", headers=headers)
    assert loaded.status_code == 200
    assert loaded.json()["data"] == {"revision": 0, "visits": [], "groups": []}


@pytest.mark.parametrize("kind,field,value", (
    ("prepare", "paperId", "other-paper"),
    ("analyze", "paperId", "other-paper"),
    ("ask", "paperId", "other-paper"),
    ("import", "sourceRevision", "other-revision"),
    ("analyze", "sourceRevision", "other-revision"),
    ("ask", "sourceRevision", "other-revision"),
    ("import", "sourceFingerprint", "sha256:" + "f" * 64),
    ("analyze", "sourceFingerprint", "sha256:" + "f" * 64),
    ("ask", "sourceFingerprint", "sha256:" + "f" * 64),
    ("prepare", "sourceFingerprint", "sha256:" + "e" * 64 + "\n"),
    ("prepare", "sourceFingerprint", "sha256:" + "E" * 64),
    ("prepare", "sourceFingerprint", None),
    ("ask", "question", "A different question"),
    ("import", "verified", True),
    ("prepare", "verified", True),
))
def test_history_api_rejects_invalid_receipt_identity_without_losing_saved_history(
    tmp_path: Path, kind: str, field: str, value: object,
) -> None:
    client, _ = _client(tmp_path)
    headers = {"X-Newsroom-Session": _session(client)}
    original = _history_pending_operation(kind)
    saved = client.put(
        "/api/v1/research/history", headers=headers,
        json={"revision": 0, "visits": [], "groups": [], "pendingOperations": [original]},
    )
    assert saved.status_code == 200
    operation = _history_pending_operation(kind, passed=True)
    operation[field] = value
    rejected = client.put(
        "/api/v1/research/history", headers=headers,
        json={"revision": 1, "visits": [], "groups": [], "pendingOperations": [operation]},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_request"
    reopened_client, _ = _client(tmp_path)
    loaded = reopened_client.get("/api/v1/research/history", headers=headers)
    assert loaded.status_code == 200
    assert loaded.json()["data"] == saved.json()["data"]
