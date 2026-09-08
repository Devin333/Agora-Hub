from __future__ import annotations

from pathlib import Path

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
