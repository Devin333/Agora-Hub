from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient

from interfaces.api import create_app
from interfaces.cli.news import main as news_main
from tests.interfaces.api.hosted_support import (
    build_test_hosted_runtime,
    configure_hosted_env,
    mutation_headers,
    sign_in,
)

ADMIN_PASSWORD = "admin-password-0001"
USER_PASSWORD = "user-password-0002"


def _client(monkeypatch, tmp_path):
    configure_hosted_env(monkeypatch, tmp_path)
    runtime = build_test_hosted_runtime(tmp_path)
    app = create_app(hosted_runtime=runtime, audit_emitter_factory=None)
    return TestClient(app), runtime


def _data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_admin_creates_account_with_one_time_password_and_user_signs_in(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    admin = mutation_headers(sign_in(runtime, "root", ADMIN_PASSWORD, role="admin"))

    created = _data(client.post("/api/v1/admin/accounts", headers=admin, json={"username": "alice"}))
    initial = created["initialPassword"]
    listed = _data(client.get("/api/v1/admin/accounts", headers=admin))

    assert created["account"]["role"] == "user"
    assert all("initialPassword" not in item and "passwordHash" not in item for item in listed["accounts"])
    signed = client.post(
        "/api/v1/auth/login",
        headers={"Origin": admin["Origin"]},
        json={"username": "alice", "password": initial},
    )
    assert signed.status_code == 200


def test_duplicate_username_is_a_conflict(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    admin = mutation_headers(sign_in(runtime, "root", ADMIN_PASSWORD, role="admin"))
    payload = {"username": "alice", "password": USER_PASSWORD}

    assert client.post("/api/v1/admin/accounts", headers=admin, json=payload).status_code == 200
    duplicate = client.post("/api/v1/admin/accounts", headers=admin, json=payload)

    assert duplicate.status_code == 409


def test_disabling_an_account_ends_its_sessions_and_blocks_sign_in(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    admin = mutation_headers(sign_in(runtime, "root", ADMIN_PASSWORD, role="admin"))
    alice = sign_in(runtime, "alice", USER_PASSWORD)
    alice_id = _data(client.get("/api/v1/account", headers=alice))["account"]["userId"]

    _data(client.post(f"/api/v1/admin/accounts/{alice_id}/disable", headers=admin))

    assert client.get("/api/v1/account", headers=alice).status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        headers={"Origin": admin["Origin"]},
        json={"username": "alice", "password": USER_PASSWORD},
    )
    assert login.status_code == 403
    assert login.json()["error"]["code"] == "account_disabled"

    _data(client.post(f"/api/v1/admin/accounts/{alice_id}/enable", headers=admin))
    assert client.post(
        "/api/v1/auth/login",
        headers={"Origin": admin["Origin"]},
        json={"username": "alice", "password": USER_PASSWORD},
    ).status_code == 200


def test_last_enabled_administrator_is_protected(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    admin = mutation_headers(sign_in(runtime, "root", ADMIN_PASSWORD, role="admin"))
    root_id = _data(client.get("/api/v1/account", headers=admin))["account"]["userId"]

    self_disable = client.post(f"/api/v1/admin/accounts/{root_id}/disable", headers=admin)
    demote = client.put(f"/api/v1/admin/accounts/{root_id}/role", headers=admin, json={"role": "user"})

    assert self_disable.status_code == 409
    assert self_disable.json()["error"]["code"] == "cannot_disable_self"
    assert demote.status_code == 409
    assert demote.json()["error"]["code"] == "last_admin_required"


def test_role_change_and_password_reset_end_existing_sessions(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    admin = mutation_headers(sign_in(runtime, "root", ADMIN_PASSWORD, role="admin"))
    alice = sign_in(runtime, "alice", USER_PASSWORD)
    alice_id = _data(client.get("/api/v1/account", headers=alice))["account"]["userId"]

    _data(client.put(f"/api/v1/admin/accounts/{alice_id}/role", headers=admin, json={"role": "admin"}))
    assert client.get("/api/v1/account", headers=alice).status_code == 401

    alice = {"X-Newsroom-Session": runtime.auth.login(username="alice", password=USER_PASSWORD).sessionToken}
    reset = _data(client.post(f"/api/v1/admin/accounts/{alice_id}/password", headers=admin, json={}))
    assert client.get("/api/v1/account", headers=alice).status_code == 401
    assert runtime.auth.login(username="alice", password=reset["initialPassword"]).sessionToken


def test_own_password_change_returns_a_new_session_and_voids_the_rest(monkeypatch, tmp_path) -> None:
    client, runtime = _client(monkeypatch, tmp_path)
    first = sign_in(runtime, "alice", USER_PASSWORD)
    second = {"X-Newsroom-Session": runtime.auth.login(username="alice", password=USER_PASSWORD).sessionToken}

    wrong = client.post(
        "/api/v1/account/password",
        headers=mutation_headers(first),
        json={"currentPassword": "not-the-password", "newPassword": "new-password-0003"},
    )
    changed = _data(
        client.post(
            "/api/v1/account/password",
            headers=mutation_headers(first),
            json={"currentPassword": USER_PASSWORD, "newPassword": "new-password-0003"},
        )
    )

    assert wrong.status_code == 400
    refreshed = {"X-Newsroom-Session": changed["session"]["sessionToken"]}
    assert client.get("/api/v1/account", headers=first).status_code == 401
    assert client.get("/api/v1/account", headers=second).status_code == 401
    assert client.get("/api/v1/account", headers=refreshed).status_code == 200


def test_server_cli_creates_the_first_administrator_from_stdin(monkeypatch, tmp_path, capsys) -> None:
    configure_hosted_env(monkeypatch, tmp_path)
    # The CLI never needs the credential master key.
    monkeypatch.delenv("NEWSROOM_CREDENTIAL_ENCRYPTION_KEY")
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{ADMIN_PASSWORD}\n"))

    assert news_main(["accounts", "create-admin", "root", "--password-stdin"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["account"]["role"] == "admin"
    assert "initialPassword" not in created

    assert news_main(["accounts", "create-user", "alice", "--generate-password"]) == 0
    generated = json.loads(capsys.readouterr().out)["initialPassword"]

    assert news_main(["accounts", "disable", "root"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "last_admin_required"
    assert news_main(["accounts", "disable", "alice"]) == 0
    capsys.readouterr()
    assert news_main(["accounts", "list"]) == 0
    accounts = {item["username"]: item for item in json.loads(capsys.readouterr().out)["accounts"]}
    assert accounts["alice"]["enabled"] is False

    runtime = build_test_hosted_runtime(tmp_path)
    assert runtime.auth.login(username="root", password=ADMIN_PASSWORD).sessionToken
    assert news_main(["accounts", "enable", "alice"]) == 0
    assert runtime.auth.login(username="alice", password=generated).sessionToken


def test_server_cli_rejects_an_empty_stdin_password(monkeypatch, tmp_path, capsys) -> None:
    configure_hosted_env(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    assert news_main(["accounts", "create-admin", "root", "--password-stdin"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "account_invalid_request"
