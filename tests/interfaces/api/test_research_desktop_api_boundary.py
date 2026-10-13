from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from interfaces.api import create_app
from interfaces.services.research_desktop_control import ResearchDesktopControlError


_DESKTOP_ENV = {
    "NEWSROOM_DEPLOYMENT_PROFILE": "local-desktop",
    "NEWSROOM_DESKTOP_CANONICAL_ORIGIN": "http://127.0.0.1:3000",
    "NEWSROOM_DESKTOP_INSTANCE_KEY": "local-desktop:http://127.0.0.1:3000",
    "NEWSROOM_DESKTOP_RELEASE_ID": "release-test",
    "NEWSROOM_DESKTOP_CONFIG_DIGEST": "a" * 64,
    "NEWSROOM_DESKTOP_BOOT_ID": "b" * 32,
}


def _desktop_env(monkeypatch) -> None:
    for key, value in _DESKTOP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NEWSROOM_CANONICAL_ORIGIN", raising=False)


def test_local_desktop_direct_operator_and_private_routes_fail_closed(monkeypatch) -> None:
    _desktop_env(monkeypatch)
    client = TestClient(create_app(audit_emitter_factory=None))

    requests = (
        ("get", "/api/v2/graph-runs"),
        ("get", "/api/v2/graph-runs/run-1/artifacts/output"),
        ("get", "/api/v1/reports"),
        ("get", "/api/v1/storage/metrics"),
        ("get", "/api/v1/memory/private-document"),
        ("get", "/api/v1/events/projections/runs/run-1/status"),
        ("post", "/api/v1/mcp/resources/read"),
        ("post", "/api/v1/mcp/tools/news.research.analyze_paper/call"),
    )
    for method, path in requests:
        if method == "post":
            response = client.post(
                path, headers={"Authorization": "Bearer shared-operator-token"}, json={},
            )
        else:
            response = client.get(
                path, headers={"Authorization": "Bearer shared-operator-token"},
            )
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "desktop_operator_api_disabled"

    private_research = client.get("/api/v1/research/runs/run-1/trace")
    assert private_research.status_code == 401
    assert private_research.json()["error"]["code"] == "auth_required"


def test_local_desktop_health_and_session_expose_exact_runtime_identity(monkeypatch) -> None:
    _desktop_env(monkeypatch)
    client = TestClient(
        create_app(
            auth_service_factory=lambda: _ReadOnlyAuthService(),
            audit_emitter_factory=None,
        )
    )
    expected = {
        "deployment_profile": "local-desktop",
        "instance_key": "local-desktop:http://127.0.0.1:3000",
        "release_id": "release-test",
        "config_digest": "a" * 64,
        "boot_id": "b" * 32,
    }

    assert client.get("/health").json()["data"]["local_desktop"] == expected
    assert client.get("/health/ready").json()["data"]["local_desktop"] == expected
    assert client.get("/api/v1/auth/session").json()["data"]["local_desktop"] == expected


@pytest.mark.parametrize(
    ("key", "value", "error_code"),
    (
        ("NEWSROOM_DESKTOP_BOOT_ID", None, "desktop_boot_id_invalid"),
        ("NEWSROOM_DESKTOP_BOOT_ID", "not-hex", "desktop_boot_id_invalid"),
        ("NEWSROOM_DESKTOP_CONFIG_DIGEST", "abc", "desktop_config_digest_invalid"),
        ("NEWSROOM_DESKTOP_INSTANCE_KEY", "desktop-test", "desktop_instance_key_invalid"),
        (
            "NEWSROOM_DESKTOP_INSTANCE_KEY",
            "local-desktop:http://127.0.0.1:3999",
            "desktop_instance_key_invalid",
        ),
    ),
)
def test_local_desktop_invalid_runtime_identity_is_not_ready(
    monkeypatch, key: str, value: str | None, error_code: str,
) -> None:
    _desktop_env(monkeypatch)
    if value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, value)
    client = TestClient(create_app(audit_emitter_factory=None))

    health = client.get("/health")
    ready = client.get("/health/ready")
    assert health.status_code == 503
    assert ready.status_code == 503
    assert health.json()["error"]["code"] == error_code


def test_desktop_drain_rejects_anonymous_auth_writes_but_keeps_status_read(monkeypatch) -> None:
    _desktop_env(monkeypatch)
    desktop = _DrainingDesktopService()
    client = TestClient(
        create_app(
            auth_service_factory=lambda: _ReadOnlyAuthService(),
            research_desktop_service_factory=lambda: desktop,
            audit_emitter_factory=None,
        )
    )

    rejected = client.post(
        "/api/v1/auth/bootstrap",
        headers={"Origin": "http://127.0.0.1:3000"},
        json={"username": "owner", "password": "correct-horse"},
    )
    status = client.get("/api/v1/auth/session")

    assert rejected.status_code == 503
    assert rejected.json()["error"]["code"] == "desktop_admission_draining"
    assert status.status_code == 200
    assert desktop.begin_calls == ["POST /api/v1/auth/bootstrap"]


def test_default_profile_health_and_session_keep_original_shape(monkeypatch) -> None:
    monkeypatch.setenv("NEWSROOM_DEPLOYMENT_PROFILE", "default")
    client = TestClient(
        create_app(
            auth_service_factory=lambda: _ReadOnlyAuthService(),
            audit_emitter_factory=None,
        )
    )

    assert "local_desktop" not in client.get("/health").json()["data"]
    assert "local_desktop" not in client.get("/health/ready").json()["data"]
    assert "local_desktop" not in client.get("/api/v1/auth/session").json()["data"]


class _ReadOnlyAuthService:
    def is_initialized(self) -> bool:
        return False

    def get_session(self, token):
        raise AssertionError("no session lookup expected")


class _DrainingDesktopService:
    def __init__(self) -> None:
        self.begin_calls = []

    def begin_business_operation(self, *, operation: str) -> str:
        self.begin_calls.append(operation)
        raise ResearchDesktopControlError(
            "desktop_admission_draining",
            "Desktop is draining",
            status_code=503,
            retryable=True,
        )

    def end_business_operation(self, lease_id: str) -> None:
        raise AssertionError("no lease was admitted")
