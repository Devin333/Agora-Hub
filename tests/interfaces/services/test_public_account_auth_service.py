from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from interfaces.services.account_auth.config import PublicAuthSettings
from interfaces.services.account_auth.delivery import SmtpOtpDelivery
from interfaces.services.account_auth.errors import (
    AuthChallengeInvalidError,
    AuthChallengeRateLimitedError,
    AuthDeliveryFailedError,
    AuthMethodUnavailableError,
    AuthProviderStateInvalidError,
)
from interfaces.services.account_auth.oauth import VerifiedProviderIdentity
from interfaces.services.auth_service import AuthApplicationService, AuthInvalidCredentialsError

UTC = timezone.utc
BINDING = "binding_abcdefghijklmnopqrstuvwxyz012345"
OTHER_BINDING = "binding_other_abcdefghijklmnopqrstuvwxyz"


class RecordingDelivery:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[tuple[str, str]] = []

    def send(self, *, destination: str, code: str) -> None:
        self.messages.append((destination, code))
        if self.fail:
            raise AuthDeliveryFailedError("delivery failed")


class FakeProvider:
    def __init__(self, subject: str = "subject-1", display_name: str = "Ada Researcher") -> None:
        self.subject = subject
        self.display_name = display_name
        self.exchanges: list[tuple[str, str, str]] = []

    def authorization_url(self, *, state: str, nonce: str, redirect_uri: str) -> str:
        return "https://provider.example/authorize?" + f"state={state}&nonce={nonce}&redirect_uri={redirect_uri}"

    def exchange(self, *, code: str, redirect_uri: str, nonce: str) -> VerifiedProviderIdentity:
        self.exchanges.append((code, redirect_uri, nonce))
        return VerifiedProviderIdentity(subject=self.subject, display_name=self.display_name)


@dataclass
class MutableClock:
    value: datetime = datetime(2026, 9, 8, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def test_unconfigured_public_methods_fail_closed(tmp_path: Path) -> None:
    service = _service(tmp_path, configured=False)

    methods = service.public_methods()

    assert all(not item["available"] for item in methods["methods"].values())
    assert methods["termsUrl"] is None
    assert methods["privacyUrl"] is None


def test_verify_otp_fails_closed_when_challenge_secret_is_unconfigured(tmp_path: Path) -> None:
    service = _service(tmp_path, configured=False)

    with pytest.raises(AuthMethodUnavailableError):
        service.verify_otp(
            challenge_id="otp_0123456789abcdef01234567",
            code="123456",
            browser_binding=BINDING,
        )


def test_email_otp_creates_and_reuses_unprivileged_identity_without_storing_plain_code(tmp_path: Path) -> None:
    delivery = RecordingDelivery()
    service = _service(tmp_path, email_delivery=delivery)

    challenge = service.request_otp(
        channel="email",
        destination="Ada@Example.com",
        consent=True,
        browser_binding=BINDING,
    )
    code = delivery.messages[-1][1]
    first = service.verify_otp(challenge_id=challenge["challengeId"], code=code, browser_binding=BINDING)

    assert challenge["destinationHint"] == "a***@example.com"
    assert first.user.role == "user"
    assert code not in (tmp_path / "otp.json").read_text(encoding="utf-8")
    with pytest.raises(AuthChallengeInvalidError):
        service.verify_otp(challenge_id=challenge["challengeId"], code=code, browser_binding=BINDING)

    _clock(service).advance(seconds=61)
    second_challenge = service.request_otp(channel="email", destination="ada@example.com", consent=True, browser_binding=BINDING)
    second = service.verify_otp(challenge_id=second_challenge["challengeId"], code=delivery.messages[-1][1], browser_binding=BINDING)
    assert second.user.userId == first.user.userId


def test_otp_is_browser_bound_and_failed_attempts_are_persistently_bounded(tmp_path: Path) -> None:
    delivery = RecordingDelivery()
    service = _service(tmp_path, email_delivery=delivery)
    challenge = service.request_otp(channel="email", destination="ada@example.com", consent=True, browser_binding=BINDING)

    with pytest.raises(AuthChallengeInvalidError):
        service.verify_otp(challenge_id=challenge["challengeId"], code=delivery.messages[-1][1], browser_binding=OTHER_BINDING)
    for _ in range(4):
        with pytest.raises(AuthChallengeInvalidError):
            service.verify_otp(challenge_id=challenge["challengeId"], code="000000", browser_binding=BINDING)
    with pytest.raises(AuthChallengeRateLimitedError):
        service.verify_otp(challenge_id=challenge["challengeId"], code="000000", browser_binding=BINDING)

    record = json.loads((tmp_path / "otp.json").read_text(encoding="utf-8"))["challenges"][0]
    assert record["attempts"] == 5
    assert record["status"] == "attempts_exhausted"


def test_phone_otp_uses_e164_identity_and_masked_response(tmp_path: Path) -> None:
    delivery = RecordingDelivery()
    service = _service(tmp_path)
    service._phone_delivery = delivery
    challenge = service.request_otp(
        channel="phone",
        destination="+86 138 0013 8000",
        consent=True,
        browser_binding=BINDING,
    )

    session = service.verify_otp(
        challenge_id=challenge["challengeId"],
        code=delivery.messages[-1][1],
        browser_binding=BINDING,
    )

    assert delivery.messages[0][0] == "+8613800138000"
    assert challenge["destinationHint"] == "***8000"
    assert session.user.role == "user"


def test_failed_delivery_never_produces_verifiable_challenge(tmp_path: Path) -> None:
    service = _service(tmp_path, email_delivery=RecordingDelivery(fail=True))

    with pytest.raises(AuthDeliveryFailedError):
        service.request_otp(channel="email", destination="ada@example.com", consent=True, browser_binding=BINDING)
    record = json.loads((tmp_path / "otp.json").read_text(encoding="utf-8"))["challenges"][0]
    assert record["status"] == "delivery_failed"
    with pytest.raises(AuthChallengeInvalidError):
        service.verify_otp(challenge_id=record["challengeId"], code="123456", browser_binding=BINDING)


def test_send_limits_apply_to_browser_and_destination(tmp_path: Path) -> None:
    clock = MutableClock()
    service = _service(tmp_path, email_delivery=RecordingDelivery(), clock=clock)
    service.request_otp(channel="email", destination="ada@example.com", consent=True, browser_binding=BINDING)

    with pytest.raises(AuthChallengeRateLimitedError) as cooldown:
        service.request_otp(channel="email", destination="other@example.com", consent=True, browser_binding=BINDING)
    assert cooldown.value.retry_after <= 61

    for index in range(2):
        clock.advance(seconds=61)
        service.request_otp(
            channel="email",
            destination="ada@example.com",
            consent=True,
            browser_binding=f"binding_{index}_abcdefghijklmnopqrstuvwxyz012345",
        )
    clock.advance(seconds=61)
    with pytest.raises(AuthChallengeRateLimitedError):
        service.request_otp(
            channel="email",
            destination="ada@example.com",
            consent=True,
            browser_binding="binding_3_abcdefghijklmnopqrstuvwxyz012345",
        )


def test_public_user_does_not_block_admin_bootstrap_or_password_login(tmp_path: Path) -> None:
    service = _service(tmp_path, email_delivery=RecordingDelivery())
    public_session = service.create_identity_session(provider="email", subject="ada@example.com", username="ada")

    assert service.is_initialized() is False
    admin_session = service.bootstrap(username="admin", password="strong-password")
    assert admin_session.user.role == "admin"
    with pytest.raises(AuthInvalidCredentialsError):
        service.login(username="ada", password="not-the-password")


def test_bootstrap_rejects_username_already_owned_by_public_user(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.create_identity_session(provider="wechat", subject="微信用户一", username="researcher")

    with pytest.raises(ValueError, match="username is already in use"):
        service.bootstrap(username="researcher", password="strong-password")


def test_non_ascii_provider_subject_is_resolved_without_type_error(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = service.create_identity_session(provider="wechat", subject="微信用户一", username="researcher")
    second = service.create_identity_session(provider="wechat", subject="微信用户一", username="researcher")

    assert first.user.userId == second.user.userId


def test_invalid_smtp_security_mode_fails_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings = PublicAuthSettings(**{**settings.__dict__, "smtp_security": "plain"})

    assert settings.method_available("email") == (False, "not_configured")
    with pytest.raises(AuthMethodUnavailableError):
        SmtpOtpDelivery(settings).send(destination="user@example.com", code="123456")


def test_old_otp_and_oauth_transactions_are_pruned(tmp_path: Path) -> None:
    clock = MutableClock()
    provider = FakeProvider()
    service = _service(tmp_path, clock=clock, oauth_providers={"google": provider})
    service.request_otp(channel="email", destination="old@example.com", consent=True, browser_binding=BINDING)
    service.start_oauth(provider="google", consent=True, browser_binding=BINDING)

    clock.advance(hours=25)
    service.request_otp(channel="email", destination="new@example.com", consent=True, browser_binding=BINDING)
    service.start_oauth(provider="google", consent=True, browser_binding=BINDING)

    assert len(service._public_auth().otp.read()) == 1
    assert len(service._public_auth().oauth.read()) == 1


def test_oauth_state_is_browser_bound_single_use_and_provider_qualified(tmp_path: Path) -> None:
    provider = FakeProvider(subject="google-subject")
    service = _service(tmp_path, oauth_providers={"google": provider})
    authorization = service.start_oauth(provider="google", consent=True, browser_binding=BINDING)
    state = parse_qs(urlparse(authorization["authorizationUrl"]).query)["state"][0]

    with pytest.raises(AuthProviderStateInvalidError):
        service.complete_oauth(provider="google", code="code", state=state, browser_binding=OTHER_BINDING)
    first = service.complete_oauth(provider="google", code="code", state=state, browser_binding=BINDING)
    assert first.user.role == "user"
    assert provider.exchanges[0][1] == "https://agora.example/api/auth/oauth/google/callback"
    with pytest.raises(AuthProviderStateInvalidError):
        service.complete_oauth(provider="google", code="code", state=state, browser_binding=BINDING)

    qq = service.create_identity_session(provider="qq", subject="google-subject", username="Ada Researcher")
    assert qq.user.userId != first.user.userId


def test_concurrent_resolution_of_same_identity_creates_one_user(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: service.create_identity_session(provider="google", subject="same-subject", username="Same User"),
                range(20),
            )
        )

    assert len({result.user.userId for result in results}) == 1
    payload = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))
    assert len(payload["users"]) == 1
    assert len(payload["identities"]) == 1


def test_concurrent_otp_verification_issues_exactly_one_session(tmp_path: Path) -> None:
    delivery = RecordingDelivery()
    service = _service(tmp_path, email_delivery=delivery)
    challenge = service.request_otp(
        channel="email",
        destination="concurrent@example.com",
        consent=True,
        browser_binding=BINDING,
    )

    def verify(_: int) -> str:
        try:
            return service.verify_otp(
                challenge_id=challenge["challengeId"],
                code=delivery.messages[-1][1],
                browser_binding=BINDING,
            ).sessionId
        except AuthChallengeInvalidError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(verify, range(12)))

    assert len([result for result in results if result != "rejected"]) == 1


def test_legacy_v1_user_file_remains_readable_and_upgrades_on_identity_write(tmp_path: Path) -> None:
    users_path = tmp_path / "users.json"
    users_path.write_text(
        json.dumps(
            {
                "schemaVersion": "newsroom_auth_users.v1",
                "users": [
                    {
                        "userId": "user_admin",
                        "username": "admin",
                        "role": "admin",
                        "createdAt": "2026-09-08T00:00:00Z",
                        "updatedAt": "2026-09-08T00:00:00Z",
                        "passwordHash": "hash",
                        "passwordSalt": "00" * 16,
                        "passwordIterations": 210000,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = _service(tmp_path)

    assert service.is_initialized() is True
    service.create_identity_session(provider="google", subject="new-subject", username="researcher")

    payload = json.loads(users_path.read_text(encoding="utf-8"))
    assert payload["schemaVersion"] == "newsroom_auth_users.v2"
    assert {user["userId"] for user in payload["users"]} == {"user_admin", payload["identities"][0]["userId"]}


def _service(
    tmp_path: Path,
    *,
    configured: bool = True,
    email_delivery: RecordingDelivery | None = None,
    oauth_providers: dict[str, FakeProvider] | None = None,
    clock: MutableClock | None = None,
) -> AuthApplicationService:
    settings = _settings(tmp_path, configured=configured)
    actual_clock = clock or MutableClock()
    return AuthApplicationService(
        user_store_path=tmp_path / "users.json",
        session_store_path=tmp_path / "sessions.json",
        public_auth_settings=settings,
        email_delivery=email_delivery or RecordingDelivery(),
        phone_delivery=RecordingDelivery(),
        oauth_providers=oauth_providers or {},
        clock=actual_clock,
    )


def _settings(tmp_path: Path, *, configured: bool = True) -> PublicAuthSettings:
    return PublicAuthSettings(
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


def _clock(service: AuthApplicationService) -> MutableClock:
    return service._public_clock
