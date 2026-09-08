from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from interfaces.api import create_app
from interfaces.services.account_auth.config import PublicAuthSettings
from interfaces.services.account_auth.oauth import VerifiedProviderIdentity
from interfaces.services.auth_service import AuthApplicationService

BINDING = "binding_abcdefghijklmnopqrstuvwxyz012345"


class RecordingDelivery:
    def __init__(self) -> None:
        self.code = ""

    def send(self, *, destination: str, code: str) -> None:
        self.code = code


class FakeGoogle:
    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        return f"https://accounts.example/authorize?state={state}"

    def exchange(self, *, code: str, redirect_uri: str, nonce: str) -> VerifiedProviderIdentity:
        return VerifiedProviderIdentity(subject="verified-google-subject", display_name="Research User")


def test_auth_methods_are_discoverable_when_unconfigured(tmp_path: Path) -> None:
    service = _auth_service(tmp_path, configured=False)
    client = TestClient(create_app(auth_service_factory=lambda: service, audit_emitter_factory=None))

    response = client.get("/api/v1/auth/methods")

    assert response.status_code == 200
    assert response.json()["data"]["methods"]["google"] == {
        "available": False,
        "reason": "legal_urls_required",
    }


def test_otp_api_issues_existing_session_contract_and_rejects_replay(tmp_path: Path) -> None:
    delivery = RecordingDelivery()
    service = _auth_service(tmp_path, delivery=delivery)
    client = TestClient(create_app(auth_service_factory=lambda: service, audit_emitter_factory=None))

    created = client.post(
        "/api/v1/auth/otp/challenges",
        headers={"x-newsroom-auth-binding": BINDING},
        json={"channel": "email", "destination": "user@example.com", "consent": True},
    )
    challenge_id = created.json()["data"]["challengeId"]
    verified = client.post(
        f"/api/v1/auth/otp/challenges/{challenge_id}/verify",
        headers={"x-newsroom-auth-binding": BINDING},
        json={"code": delivery.code},
    )
    replay = client.post(
        f"/api/v1/auth/otp/challenges/{challenge_id}/verify",
        headers={"x-newsroom-auth-binding": BINDING},
        json={"code": delivery.code},
    )

    assert created.status_code == 200
    session = verified.json()["data"]["session"]
    assert verified.status_code == 200
    assert session["sessionToken"]
    assert session["user"]["role"] == "user"
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "auth_challenge_invalid"


def test_public_auth_errors_have_stable_codes_and_retry_headers(tmp_path: Path) -> None:
    service = _auth_service(tmp_path)
    client = TestClient(create_app(auth_service_factory=lambda: service, audit_emitter_factory=None))
    payload = {"channel": "email", "destination": "user@example.com", "consent": True}

    missing_binding = client.post("/api/v1/auth/otp/challenges", json=payload)
    client.post("/api/v1/auth/otp/challenges", headers={"x-newsroom-auth-binding": BINDING}, json=payload)
    rate_limited = client.post(
        "/api/v1/auth/otp/challenges",
        headers={"x-newsroom-auth-binding": BINDING},
        json={**payload, "destination": "second@example.com"},
    )

    assert missing_binding.status_code == 400
    assert missing_binding.json()["error"]["code"] == "auth_invalid_browser_binding"
    assert rate_limited.status_code == 429
    assert int(rate_limited.headers["retry-after"]) > 0


def test_unconfigured_provider_uses_provider_unavailable_contract(tmp_path: Path) -> None:
    service = _auth_service(tmp_path, configured=False)
    client = TestClient(create_app(auth_service_factory=lambda: service, audit_emitter_factory=None))

    response = client.post(
        "/api/v1/auth/oauth/google/authorize",
        headers={"x-newsroom-auth-binding": BINDING},
        json={"consent": True},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth_provider_unavailable"


def test_oauth_api_requires_matching_single_use_state(tmp_path: Path) -> None:
    service = _auth_service(tmp_path, oauth_providers={"google": FakeGoogle()})
    client = TestClient(create_app(auth_service_factory=lambda: service, audit_emitter_factory=None))

    started = client.post(
        "/api/v1/auth/oauth/google/authorize",
        headers={"x-newsroom-auth-binding": BINDING},
        json={"consent": True},
    )
    state = parse_qs(urlparse(started.json()["data"]["authorizationUrl"]).query)["state"][0]
    callback = client.post(
        "/api/v1/auth/oauth/google/callback",
        headers={"x-newsroom-auth-binding": BINDING},
        json={"code": "provider-code", "state": state},
    )
    replay = client.post(
        "/api/v1/auth/oauth/google/callback",
        headers={"x-newsroom-auth-binding": BINDING},
        json={"code": "provider-code", "state": state},
    )

    assert callback.status_code == 200
    assert callback.json()["data"]["session"]["user"]["role"] == "user"
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "auth_provider_rejected"


def _auth_service(
    tmp_path: Path,
    *,
    configured: bool = True,
    delivery: RecordingDelivery | None = None,
    oauth_providers: dict[str, FakeGoogle] | None = None,
) -> AuthApplicationService:
    settings = PublicAuthSettings(
        secret="s" * 64 if configured else None,
        terms_url="https://agora.example/terms" if configured else None,
        privacy_url="https://agora.example/privacy" if configured else None,
        public_origin="https://agora.example" if configured else None,
        otp_store_path=tmp_path / "otp.json",
        oauth_store_path=tmp_path / "oauth.json",
        smtp_host="smtp.example" if configured else None,
        smtp_username="smtp-user" if configured else None,
        smtp_password="smtp-password" if configured else None,
        smtp_from="accounts@agora.example" if configured else None,
        twilio_account_sid="AC123" if configured else None,
        twilio_auth_token="twilio-secret" if configured else None,
        twilio_from="+14155550100" if configured else None,
        google_client_id="google-id" if configured else None,
        google_client_secret="google-secret" if configured else None,
        wechat_client_id="wechat-id" if configured else None,
        wechat_client_secret="wechat-secret" if configured else None,
        qq_client_id="qq-id" if configured else None,
        qq_client_secret="qq-secret" if configured else None,
    )
    actual_delivery = delivery or RecordingDelivery()
    return AuthApplicationService(
        user_store_path=tmp_path / "users.json",
        session_store_path=tmp_path / "sessions.json",
        public_auth_settings=settings,
        email_delivery=actual_delivery,
        phone_delivery=actual_delivery,
        oauth_providers=oauth_providers or {},
    )
