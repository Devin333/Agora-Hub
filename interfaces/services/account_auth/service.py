from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

import phonenumbers
from email_validator import EmailNotValidError, validate_email

from interfaces.services.account_auth.config import PublicAuthSettings
from interfaces.services.account_auth.delivery import OtpDelivery, SmtpOtpDelivery, TwilioSmsOtpDelivery
from interfaces.services.account_auth.errors import (
    AuthChallengeExpiredError,
    AuthChallengeInvalidError,
    AuthChallengeRateLimitedError,
    AuthConsentRequiredError,
    AuthInvalidBindingError,
    AuthIdentityConflictError,
    AuthMethodUnavailableError,
    AuthProviderStateExpiredError,
    AuthProviderStateInvalidError,
    AuthProviderUnavailableError,
)
from interfaces.services.account_auth.oauth import OAuthProvider, provider_for
from interfaces.services.account_auth.storage import (
    JsonTransactionStore,
    constant_equal,
    format_datetime,
    keyed_digest,
    parse_datetime,
)

UTC = timezone.utc
OTP_TTL = timedelta(minutes=10)
OAUTH_TTL = timedelta(minutes=5)
TRANSACTION_RETENTION = timedelta(hours=24)
SEND_WINDOW = timedelta(hours=1)
SEND_COOLDOWN = timedelta(seconds=60)
MAX_BROWSER_SENDS = 5
MAX_DESTINATION_SENDS = 3
MAX_GLOBAL_SENDS = 100
MAX_VERIFY_ATTEMPTS = 5


class PublicAccountAuthService:
    def __init__(
        self,
        auth_service: Any,
        *,
        settings: PublicAuthSettings | None = None,
        email_delivery: OtpDelivery | None = None,
        phone_delivery: OtpDelivery | None = None,
        oauth_providers: Mapping[str, OAuthProvider] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.auth_service = auth_service
        self.settings = settings or PublicAuthSettings.from_env()
        self.email_delivery = email_delivery or SmtpOtpDelivery(self.settings)
        self.phone_delivery = phone_delivery or TwilioSmsOtpDelivery(self.settings)
        self.oauth_providers = dict(oauth_providers or {})
        self.clock = clock or (lambda: datetime.now(UTC))
        self.otp = JsonTransactionStore(
            self.settings.otp_store_path,
            collection="challenges",
            schema_version="newsroom_auth_otp_challenges.v1",
        )
        self.oauth = JsonTransactionStore(
            self.settings.oauth_store_path,
            collection="states",
            schema_version="newsroom_auth_oauth_states.v1",
        )

    def methods(self) -> dict[str, Any]:
        methods: dict[str, dict[str, Any]] = {}
        for method in ("phone", "email", "google", "wechat", "qq"):
            available, reason = self.settings.method_available(method)
            methods[method] = {"available": available}
            if reason:
                methods[method]["reason"] = reason
        return {
            "methods": methods,
            "termsUrl": self.settings.terms_url,
            "privacyUrl": self.settings.privacy_url,
        }

    def request_otp(self, *, channel: str, destination: str, consent: bool, browser_binding: str) -> dict[str, Any]:
        if consent is not True:
            raise AuthConsentRequiredError("terms and privacy consent is required")
        normalized_channel = channel.strip().lower()
        normalized_destination = _normalize_destination(normalized_channel, destination)
        self._require_available(normalized_channel)
        binding = _validate_binding(browser_binding)
        now = self.clock()
        challenge_id = f"otp_{secrets.token_hex(12)}"
        code = f"{secrets.randbelow(1_000_000):06d}"
        assert self.settings.secret is not None
        binding_digest = keyed_digest(self.settings.secret, "browser", binding)
        destination_digest = keyed_digest(self.settings.secret, normalized_channel, normalized_destination)
        code_digest = keyed_digest(
            self.settings.secret,
            "otp",
            f"{challenge_id}\0{binding}\0{normalized_channel}\0{normalized_destination}\0{code}",
        )

        def reserve(records: list[dict[str, Any]]) -> None:
            records[:] = [
                record for record in records
                if parse_datetime(record["createdAt"]) > now - TRANSACTION_RETENTION
            ]
            recent = [record for record in records if parse_datetime(record["createdAt"]) > now - SEND_WINDOW]
            cooldown = [
                record for record in recent
                if constant_equal(str(record["bindingDigest"]), binding_digest)
                and parse_datetime(record["createdAt"]) > now - SEND_COOLDOWN
            ]
            browser_count = sum(constant_equal(str(record["bindingDigest"]), binding_digest) for record in recent)
            destination_count = sum(constant_equal(str(record["destinationDigest"]), destination_digest) for record in recent)
            if cooldown:
                retry_after = int((parse_datetime(cooldown[-1]["createdAt"]) + SEND_COOLDOWN - now).total_seconds()) + 1
                raise AuthChallengeRateLimitedError("wait before requesting another code", retry_after=retry_after)
            if browser_count >= MAX_BROWSER_SENDS or destination_count >= MAX_DESTINATION_SENDS or len(recent) >= MAX_GLOBAL_SENDS:
                raise AuthChallengeRateLimitedError("verification request limit reached", retry_after=3600)
            for record in records:
                if constant_equal(str(record["bindingDigest"]), binding_digest) and record.get("status") == "delivered":
                    record["status"] = "superseded"
            records.append(
                {
                    "challengeId": challenge_id,
                    "channel": normalized_channel,
                    "destination": normalized_destination,
                    "destinationDigest": destination_digest,
                    "bindingDigest": binding_digest,
                    "codeDigest": code_digest,
                    "createdAt": format_datetime(now),
                    "expiresAt": format_datetime(now + OTP_TTL),
                    "attempts": 0,
                    "status": "pending",
                }
            )

        self.otp.mutate(reserve)
        delivery = self.email_delivery if normalized_channel == "email" else self.phone_delivery
        try:
            delivery.send(destination=normalized_destination, code=code)
        except Exception:
            self._set_otp_status(challenge_id, "delivery_failed")
            raise
        self._set_otp_status(challenge_id, "delivered")
        return {
            "challengeId": challenge_id,
            "channel": normalized_channel,
            "destinationHint": _destination_hint(normalized_channel, normalized_destination),
            "expiresAt": format_datetime(now + OTP_TTL),
            "resendAt": format_datetime(now + SEND_COOLDOWN),
        }

    def verify_otp(self, *, challenge_id: str, code: str, browser_binding: str) -> Any:
        binding = _validate_binding(browser_binding)
        if not self.settings.secret or len(self.settings.secret) < 32 or not self.settings.legal_ready:
            raise AuthMethodUnavailableError("verification sign-in is unavailable")
        if not re.fullmatch(r"otp_[A-Fa-f0-9]{24}", challenge_id):
            raise AuthChallengeInvalidError("verification challenge is invalid")
        if not re.fullmatch(r"\d{6}", code):
            raise AuthChallengeInvalidError("verification code is invalid")
        now = self.clock()
        binding_digest = keyed_digest(self.settings.secret, "browser", binding)

        def consume(records: list[dict[str, Any]]) -> tuple[str, str | None, str | None]:
            candidates = [
                record for record in records
                if record.get("challengeId") == challenge_id
                and constant_equal(str(record["bindingDigest"]), binding_digest)
                and record.get("status") == "delivered"
            ]
            if not candidates:
                return "invalid", None, None
            record = max(candidates, key=lambda item: parse_datetime(item["createdAt"]))
            if parse_datetime(record["expiresAt"]) <= now:
                record["status"] = "expired"
                return "expired", None, None
            attempts = int(record.get("attempts") or 0)
            if attempts >= MAX_VERIFY_ATTEMPTS:
                record["status"] = "attempts_exhausted"
                return "exhausted", None, None
            expected = keyed_digest(
                self.settings.secret or "",
                "otp",
                f"{record['challengeId']}\0{binding}\0{record['channel']}\0{record['destination']}\0{code}",
            )
            if not constant_equal(str(record["codeDigest"]), expected):
                record["attempts"] = attempts + 1
                if attempts + 1 >= MAX_VERIFY_ATTEMPTS:
                    record["status"] = "attempts_exhausted"
                    return "exhausted", None, None
                return "wrong", None, None
            record["status"] = "consumed"
            record["consumedAt"] = format_datetime(now)
            return "ok", str(record["channel"]), str(record["destination"])

        outcome, channel, destination = self.otp.mutate(consume)
        if outcome == "expired":
            raise AuthChallengeExpiredError("verification code has expired")
        if outcome == "exhausted":
            raise AuthChallengeRateLimitedError("verification attempt limit reached", retry_after=600)
        if outcome != "ok" or channel is None or destination is None:
            raise AuthChallengeInvalidError("verification code is invalid")
        username = destination.split("@", 1)[0] if channel == "email" else f"user_{destination[-4:]}"
        return self._identity_session(provider=channel, subject=destination, username=username)

    def start_oauth(self, *, provider: str, consent: bool, browser_binding: str) -> dict[str, Any]:
        if consent is not True:
            raise AuthConsentRequiredError("terms and privacy consent is required")
        normalized_provider = provider.strip().lower()
        if normalized_provider not in {"google", "wechat", "qq"}:
            raise AuthMethodUnavailableError("sign-in provider is not supported")
        self._require_provider_available(normalized_provider)
        binding = _validate_binding(browser_binding)
        redirect_uri = self.settings.callback_url(normalized_provider)
        if redirect_uri is None:
            raise AuthMethodUnavailableError("sign-in provider callback is not configured")
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        now = self.clock()
        assert self.settings.secret is not None
        record = {
            "provider": normalized_provider,
            "stateDigest": keyed_digest(self.settings.secret, "oauth-state", state),
            "bindingDigest": keyed_digest(self.settings.secret, "browser", binding),
            "nonce": nonce,
            "createdAt": format_datetime(now),
            "expiresAt": format_datetime(now + OAUTH_TTL),
            "status": "active",
        }
        def reserve_state(records: list[dict[str, Any]]) -> None:
            records[:] = [
                item for item in records
                if parse_datetime(item["createdAt"]) > now - TRANSACTION_RETENTION
            ]
            recent = [item for item in records if parse_datetime(item["createdAt"]) > now - SEND_WINDOW]
            binding_digest = str(record["bindingDigest"])
            if len(recent) >= 500 or sum(
                constant_equal(str(item.get("bindingDigest") or ""), binding_digest) for item in recent
            ) >= 30:
                raise AuthChallengeRateLimitedError("authorization request limit reached", retry_after=3600)
            records.append(record)

        self.oauth.mutate(reserve_state)
        adapter = self._provider(normalized_provider)
        return {
            "authorizationUrl": adapter.authorization_url(state=state, nonce=nonce, redirect_uri=redirect_uri),
            "expiresAt": format_datetime(now + OAUTH_TTL),
        }

    def complete_oauth(self, *, provider: str, code: str, state: str, browser_binding: str) -> Any:
        normalized_provider = provider.strip().lower()
        self._require_provider_available(normalized_provider)
        binding = _validate_binding(browser_binding)
        if not code or len(code) > 4096 or not state or len(state) > 512:
            raise AuthProviderStateInvalidError("authorization response is invalid")
        now = self.clock()
        assert self.settings.secret is not None
        state_digest = keyed_digest(self.settings.secret, "oauth-state", state)
        binding_digest = keyed_digest(self.settings.secret, "browser", binding)

        def consume(records: list[dict[str, Any]]) -> tuple[str, str | None]:
            record = next(
                (
                    item for item in records
                    if item.get("provider") == normalized_provider
                    and constant_equal(str(item.get("stateDigest") or ""), state_digest)
                ),
                None,
            )
            if record is None or record.get("status") != "active":
                return "invalid", None
            if not constant_equal(str(record.get("bindingDigest") or ""), binding_digest):
                return "invalid", None
            if parse_datetime(record["expiresAt"]) <= now:
                record["status"] = "expired"
                return "expired", None
            record["status"] = "consumed"
            record["consumedAt"] = format_datetime(now)
            return "ok", str(record["nonce"])

        outcome, nonce = self.oauth.mutate(consume)
        if outcome == "expired":
            raise AuthProviderStateExpiredError("authorization state has expired")
        if outcome != "ok" or nonce is None:
            raise AuthProviderStateInvalidError("authorization state is invalid")
        redirect_uri = self.settings.callback_url(normalized_provider)
        if redirect_uri is None:
            raise AuthMethodUnavailableError("sign-in provider callback is not configured")
        identity = self._provider(normalized_provider).exchange(code=code, redirect_uri=redirect_uri, nonce=nonce)
        return self._identity_session(
            provider=normalized_provider,
            subject=identity.subject,
            username=identity.display_name,
        )

    def _provider(self, provider: str) -> OAuthProvider:
        return self.oauth_providers.get(provider) or provider_for(self.settings, provider)

    def _identity_session(self, *, provider: str, subject: str, username: str) -> Any:
        try:
            return self.auth_service.create_identity_session(
                provider=provider,
                subject=subject,
                username=username,
            )
        except (ValueError, RuntimeError) as exc:
            raise AuthIdentityConflictError("verified identity could not be resolved") from exc

    def _require_available(self, method: str) -> None:
        available, _ = self.settings.method_available(method)
        if not available:
            raise AuthMethodUnavailableError(f"{method} sign-in is unavailable")

    def _require_provider_available(self, provider: str) -> None:
        available, _ = self.settings.method_available(provider)
        if not available:
            raise AuthProviderUnavailableError(f"{provider} sign-in is unavailable")

    def _set_otp_status(self, challenge_id: str, status: str) -> None:
        def update(records: list[dict[str, Any]]) -> None:
            for record in records:
                if record.get("challengeId") == challenge_id:
                    record["status"] = status
                    return
            raise AuthChallengeInvalidError("verification challenge was not found")

        self.otp.mutate(update)


def _validate_binding(value: str) -> str:
    binding = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", binding):
        raise AuthInvalidBindingError("valid browser binding is required")
    return binding


def _normalize_destination(channel: str, value: str) -> str:
    destination = value.strip()
    if channel == "email":
        try:
            return validate_email(destination, check_deliverability=False).normalized.casefold()
        except EmailNotValidError as exc:
            raise AuthChallengeInvalidError("valid email address is required") from exc

    if channel == "phone":
        try:
            parsed = phonenumbers.parse(destination, None)
        except phonenumbers.NumberParseException as exc:
            raise AuthChallengeInvalidError("phone number must use E.164 format") from exc
        if not phonenumbers.is_valid_number(parsed):
            raise AuthChallengeInvalidError("phone number must use E.164 format")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    raise AuthMethodUnavailableError("verification channel is not supported")


def _destination_hint(channel: str, destination: str) -> str:
    if channel == "email":
        local, domain = destination.split("@", 1)
        return f"{local[:1]}***@{domain}"
    return f"***{destination[-4:]}"
