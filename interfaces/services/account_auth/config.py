from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urljoin, urlparse


DEFAULT_OTP_CHALLENGES_PATH = ".newsroom/auth/otp_challenges.json"
DEFAULT_OAUTH_STATES_PATH = ".newsroom/auth/oauth_states.json"


@dataclass(frozen=True)
class PublicAuthSettings:
    secret: str | None
    terms_url: str | None
    privacy_url: str | None
    public_origin: str | None
    otp_store_path: Path
    oauth_store_path: Path
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_security: str = "starttls"
    smtp_allow_unauthenticated: bool = False
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    wechat_client_id: str | None = None
    wechat_client_secret: str | None = None
    qq_client_id: str | None = None
    qq_client_secret: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PublicAuthSettings":
        values = os.environ if env is None else env
        return cls(
            secret=_optional(values.get("NEWSROOM_AUTH_CHALLENGE_SECRET")),
            terms_url=_public_url(values.get("NEWSROOM_AUTH_TERMS_URL")),
            privacy_url=_public_url(values.get("NEWSROOM_AUTH_PRIVACY_URL")),
            public_origin=_origin(values.get("NEWSROOM_AUTH_PUBLIC_ORIGIN")),
            otp_store_path=Path(values.get("NEWSROOM_AUTH_OTP_PATH") or DEFAULT_OTP_CHALLENGES_PATH),
            oauth_store_path=Path(values.get("NEWSROOM_AUTH_OAUTH_PATH") or DEFAULT_OAUTH_STATES_PATH),
            smtp_host=_optional(values.get("NEWSROOM_AUTH_SMTP_HOST")),
            smtp_port=_positive_int(values.get("NEWSROOM_AUTH_SMTP_PORT"), 587),
            smtp_username=_optional(values.get("NEWSROOM_AUTH_SMTP_USERNAME")),
            smtp_password=_optional(values.get("NEWSROOM_AUTH_SMTP_PASSWORD")),
            smtp_from=_optional(values.get("NEWSROOM_AUTH_SMTP_FROM")),
            smtp_security=(values.get("NEWSROOM_AUTH_SMTP_SECURITY") or "starttls").strip().lower(),
            smtp_allow_unauthenticated=_truthy(values.get("NEWSROOM_AUTH_SMTP_ALLOW_UNAUTHENTICATED")),
            twilio_account_sid=_optional(values.get("NEWSROOM_AUTH_TWILIO_ACCOUNT_SID")),
            twilio_auth_token=_optional(values.get("NEWSROOM_AUTH_TWILIO_AUTH_TOKEN")),
            twilio_from=_optional(values.get("NEWSROOM_AUTH_TWILIO_FROM")),
            google_client_id=_optional(values.get("NEWSROOM_AUTH_GOOGLE_CLIENT_ID")),
            google_client_secret=_optional(values.get("NEWSROOM_AUTH_GOOGLE_CLIENT_SECRET")),
            wechat_client_id=_optional(values.get("NEWSROOM_AUTH_WECHAT_CLIENT_ID")),
            wechat_client_secret=_optional(values.get("NEWSROOM_AUTH_WECHAT_CLIENT_SECRET")),
            qq_client_id=_optional(values.get("NEWSROOM_AUTH_QQ_CLIENT_ID")),
            qq_client_secret=_optional(values.get("NEWSROOM_AUTH_QQ_CLIENT_SECRET")),
        )

    @property
    def legal_ready(self) -> bool:
        return self.terms_url is not None and self.privacy_url is not None

    def callback_url(self, provider: str) -> str | None:
        if self.public_origin is None:
            return None
        return urljoin(f"{self.public_origin}/", f"api/auth/oauth/{provider}/callback")

    def method_available(self, method: str) -> tuple[bool, str | None]:
        if not self.legal_ready:
            return False, "legal_urls_required"
        if not self.secret or len(self.secret) < 32:
            return False, "challenge_secret_required"
        if method == "email":
            credentials_valid = (
                bool(self.smtp_username and self.smtp_password)
                or (self.smtp_allow_unauthenticated and not self.smtp_username and not self.smtp_password)
            )
            ready = bool(
                self.smtp_host
                and self.smtp_from
                and self.smtp_security in {"starttls", "ssl"}
                and credentials_valid
            )
        elif method == "phone":
            ready = bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from)
        elif method == "google":
            ready = bool(self.public_origin and self.google_client_id and self.google_client_secret)
        elif method == "wechat":
            ready = bool(self.public_origin and self.wechat_client_id and self.wechat_client_secret)
        elif method == "qq":
            ready = bool(self.public_origin and self.qq_client_id and self.qq_client_secret)
        else:
            return False, "unsupported_method"
        return (True, None) if ready else (False, "not_configured")


def _optional(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _public_url(value: str | None) -> str | None:
    normalized = _optional(value)
    if normalized is None:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return None
    return normalized


def _origin(value: str | None) -> str | None:
    normalized = _public_url(value)
    if normalized is None:
        return None
    parsed = urlparse(normalized)
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return None
    return normalized.rstrip("/")
