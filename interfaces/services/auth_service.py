from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as _tz
UTC = _tz.utc
from pathlib import Path
from typing import Any, Protocol

from interfaces.services.json_file_store import locked_json_file, read_json_object_unlocked, write_json_object_unlocked


DEFAULT_AUTH_USERS_PATH = ".newsroom/auth/users.json"
DEFAULT_AUTH_SESSIONS_PATH = ".newsroom/auth/sessions.json"
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
PASSWORD_HASH_ITERATIONS = 210_000


class AuthError(ValueError):
    code = "auth_error"


class AuthAlreadyInitializedError(AuthError):
    code = "auth_already_initialized"


class AuthInvalidCredentialsError(AuthError):
    code = "auth_invalid_credentials"


class AuthSessionInvalidError(AuthError):
    code = "auth_session_invalid"


@dataclass(frozen=True)
class AuthIdentity:
    provider: str
    subject: str
    userId: str
    createdAt: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "subject": self.subject,
            "userId": self.userId,
            "createdAt": _format_datetime(self.createdAt),
        }

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> "AuthIdentity":
        return cls(
            provider=str(payload["provider"]),
            subject=str(payload["subject"]),
            userId=str(payload["userId"]),
            createdAt=_parse_datetime(payload.get("createdAt")),
        )


@dataclass(frozen=True)
class AuthUser:
    userId: str
    username: str
    role: str = "admin"
    createdAt: datetime = field(default_factory=lambda: datetime.now(UTC))
    updatedAt: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "userId": self.userId,
            "username": self.username,
            "role": self.role,
            "createdAt": _format_datetime(self.createdAt),
            "updatedAt": _format_datetime(self.updatedAt),
        }


@dataclass(frozen=True)
class StoredAuthUser(AuthUser):
    passwordHash: str = ""
    passwordSalt: str = ""
    passwordIterations: int = PASSWORD_HASH_ITERATIONS

    def to_record(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.update(
            {
                "passwordHash": self.passwordHash,
                "passwordSalt": self.passwordSalt,
                "passwordIterations": self.passwordIterations,
            }
        )
        return payload

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> "StoredAuthUser":
        return cls(
            userId=str(payload["userId"]),
            username=str(payload["username"]),
            role=str(payload.get("role") or "admin"),
            createdAt=_parse_datetime(payload.get("createdAt")),
            updatedAt=_parse_datetime(payload.get("updatedAt")),
            passwordHash=str(payload.get("passwordHash") or ""),
            passwordSalt=str(payload.get("passwordSalt") or ""),
            passwordIterations=int(payload.get("passwordIterations") or PASSWORD_HASH_ITERATIONS),
        )


@dataclass(frozen=True)
class AuthSession:
    sessionId: str
    userId: str
    tokenDigest: str
    createdAt: datetime
    expiresAt: datetime
    revokedAt: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revokedAt is None and self.expiresAt > datetime.now(UTC)

    def to_record(self) -> dict[str, Any]:
        payload = {
            "sessionId": self.sessionId,
            "userId": self.userId,
            "tokenDigest": self.tokenDigest,
            "createdAt": _format_datetime(self.createdAt),
            "expiresAt": _format_datetime(self.expiresAt),
        }
        if self.revokedAt is not None:
            payload["revokedAt"] = _format_datetime(self.revokedAt)
        return payload

    @classmethod
    def from_record(cls, payload: dict[str, Any]) -> "AuthSession":
        revoked = payload.get("revokedAt")
        return cls(
            sessionId=str(payload["sessionId"]),
            userId=str(payload["userId"]),
            tokenDigest=str(payload["tokenDigest"]),
            createdAt=_parse_datetime(payload.get("createdAt")),
            expiresAt=_parse_datetime(payload.get("expiresAt")),
            revokedAt=_parse_datetime(revoked) if revoked else None,
        )


@dataclass(frozen=True)
class AuthSessionResult:
    user: AuthUser
    sessionId: str
    expiresAt: datetime
    sessionToken: str | None = None
    initialized: bool = True

    def to_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        payload = {
            "user": self.user.to_dict(),
            "sessionId": self.sessionId,
            "expiresAt": _format_datetime(self.expiresAt),
            "initialized": self.initialized,
        }
        if include_token and self.sessionToken:
            payload["sessionToken"] = self.sessionToken
        return payload


class AuthUserRepository(Protocol):
    def list_users(self) -> list[StoredAuthUser]: ...
    def get_user_by_username(self, username: str) -> StoredAuthUser | None: ...
    def get_user(self, user_id: str) -> StoredAuthUser | None: ...
    def add_user(self, user: StoredAuthUser) -> StoredAuthUser: ...
    def bootstrap_first_user(self, user: StoredAuthUser) -> StoredAuthUser: ...
    def resolve_identity(self, *, provider: str, subject: str, username: str) -> StoredAuthUser: ...


class AuthSessionRepository(Protocol):
    def create_session(self, session: AuthSession) -> AuthSession: ...
    def get_session_by_digest(self, token_digest: str) -> AuthSession | None: ...
    def revoke_session(self, token_digest: str) -> bool: ...


class LocalJsonAuthUserRepository:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("NEWSROOM_AUTH_USERS_PATH") or DEFAULT_AUTH_USERS_PATH)

    def list_users(self) -> list[StoredAuthUser]:
        return sorted(self._read_records().values(), key=lambda item: item.username)

    def get_user_by_username(self, username: str) -> StoredAuthUser | None:
        normalized = _normalize_username(username)
        for user in self._read_records().values():
            if user.username.lower() == normalized.lower():
                return user
        return None

    def get_user(self, user_id: str) -> StoredAuthUser | None:
        return self._read_records().get(user_id)

    def add_user(self, user: StoredAuthUser) -> StoredAuthUser:
        with locked_json_file(self.path) as path:
            records, identities = self._read_payload_unlocked(path)
            if user.userId in records or any(item.username.lower() == user.username.lower() for item in records.values()):
                raise ValueError("user already exists")
            records[user.userId] = user
            self._write_payload_unlocked(path, records, identities)
        return user

    def bootstrap_first_user(self, user: StoredAuthUser) -> StoredAuthUser:
        with locked_json_file(self.path) as path:
            records, identities = self._read_payload_unlocked(path)
            if any(item.role == "admin" for item in records.values()):
                raise AuthAlreadyInitializedError("account bootstrap is already complete")
            if any(item.username.lower() == user.username.lower() for item in records.values()):
                raise ValueError("username is already in use")
            records[user.userId] = user
            self._write_payload_unlocked(path, records, identities)
        return user

    def resolve_identity(self, *, provider: str, subject: str, username: str) -> StoredAuthUser:
        normalized_provider = provider.strip().lower()
        normalized_subject = subject.strip()
        if not normalized_provider or not normalized_subject:
            raise ValueError("provider identity is required")
        with locked_json_file(self.path) as path:
            records, identities = self._read_payload_unlocked(path)
            for identity in identities:
                if identity.provider == normalized_provider and identity.subject == normalized_subject:
                    user = records.get(identity.userId)
                    if user is None:
                        raise AuthSessionInvalidError("identity user was not found")
                    return user
            actual_username = _unique_public_username(username, records.values())
            now = datetime.now(UTC)
            user = StoredAuthUser(
                userId=f"user_{secrets.token_hex(8)}",
                username=actual_username,
                role="user",
                createdAt=now,
                updatedAt=now,
            )
            records[user.userId] = user
            identities.append(
                AuthIdentity(
                    provider=normalized_provider,
                    subject=normalized_subject,
                    userId=user.userId,
                    createdAt=now,
                )
            )
            self._write_payload_unlocked(path, records, identities)
            return user

    def _read_records(self) -> dict[str, StoredAuthUser]:
        with locked_json_file(self.path) as path:
            return self._read_records_unlocked(path)

    def _read_records_unlocked(self, path: Path) -> dict[str, StoredAuthUser]:
        records, _ = self._read_payload_unlocked(path)
        return records

    def _read_payload_unlocked(self, path: Path) -> tuple[dict[str, StoredAuthUser], list[AuthIdentity]]:
        payload = read_json_object_unlocked(path, default={"users": []}, strict=True)
        records = {user.userId: user for user in (StoredAuthUser.from_record(item) for item in payload.get("users", []))}
        identities = [AuthIdentity.from_record(item) for item in payload.get("identities", [])]
        return records, identities

    def _write_records_unlocked(self, path: Path, records: dict[str, StoredAuthUser]) -> None:
        _, identities = self._read_payload_unlocked(path)
        self._write_payload_unlocked(path, records, identities)

    def _write_payload_unlocked(
        self,
        path: Path,
        records: dict[str, StoredAuthUser],
        identities: list[AuthIdentity],
    ) -> None:
        payload = {
            "schemaVersion": "newsroom_auth_users.v2",
            "users": [record.to_record() for record in sorted(records.values(), key=lambda item: item.username)],
            "identities": [
                identity.to_record()
                for identity in sorted(identities, key=lambda item: (item.provider, item.subject))
            ],
        }
        write_json_object_unlocked(path, payload)


class LocalJsonAuthSessionRepository:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("NEWSROOM_AUTH_SESSIONS_PATH") or DEFAULT_AUTH_SESSIONS_PATH)

    def create_session(self, session: AuthSession) -> AuthSession:
        with locked_json_file(self.path) as path:
            records = self._read_records_unlocked(path)
            records[session.sessionId] = session
            self._write_records_unlocked(path, records)
        return session

    def get_session_by_digest(self, token_digest: str) -> AuthSession | None:
        for session in self._read_records().values():
            if hmac.compare_digest(session.tokenDigest, token_digest):
                return session
        return None

    def revoke_session(self, token_digest: str) -> bool:
        with locked_json_file(self.path) as path:
            records = self._read_records_unlocked(path)
            changed = False
            for session_id, session in list(records.items()):
                if hmac.compare_digest(session.tokenDigest, token_digest):
                    records[session_id] = AuthSession(
                        sessionId=session.sessionId,
                        userId=session.userId,
                        tokenDigest=session.tokenDigest,
                        createdAt=session.createdAt,
                        expiresAt=session.expiresAt,
                        revokedAt=datetime.now(UTC),
                    )
                    changed = True
            if changed:
                self._write_records_unlocked(path, records)
            return changed

    def _read_records(self) -> dict[str, AuthSession]:
        with locked_json_file(self.path) as path:
            return self._read_records_unlocked(path)

    def _read_records_unlocked(self, path: Path) -> dict[str, AuthSession]:
        payload = read_json_object_unlocked(path, default={"sessions": []}, strict=True)
        return {item.sessionId: item for item in (AuthSession.from_record(record) for record in payload.get("sessions", []))}

    def _write_records_unlocked(self, path: Path, records: dict[str, AuthSession]) -> None:
        payload = {
            "schemaVersion": "newsroom_auth_sessions.v1",
            "sessions": [record.to_record() for record in sorted(records.values(), key=lambda item: item.createdAt)],
        }
        write_json_object_unlocked(path, payload)


class AuthApplicationService:
    def __init__(
        self,
        user_repository: AuthUserRepository | None = None,
        session_repository: AuthSessionRepository | None = None,
        *,
        user_store_path: str | Path | None = None,
        session_store_path: str | Path | None = None,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        public_auth_settings: Any | None = None,
        email_delivery: Any | None = None,
        phone_delivery: Any | None = None,
        oauth_providers: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self.users = user_repository or LocalJsonAuthUserRepository(user_store_path)
        self.sessions = session_repository or LocalJsonAuthSessionRepository(session_store_path)
        self.session_ttl_seconds = session_ttl_seconds
        self._public_auth_settings = public_auth_settings
        self._email_delivery = email_delivery
        self._phone_delivery = phone_delivery
        self._oauth_providers = oauth_providers
        self._public_clock = clock
        self._public_auth_service: Any | None = None

    def is_initialized(self) -> bool:
        return any(user.role == "admin" for user in self.users.list_users())

    def bootstrap(self, *, username: str, password: str) -> AuthSessionResult:
        user = self._build_user(username=username, password=password, role="admin")
        bootstrap_first = getattr(self.users, "bootstrap_first_user", None)
        if callable(bootstrap_first):
            user = bootstrap_first(user)
        else:
            if self.is_initialized():
                raise AuthAlreadyInitializedError("account bootstrap is already complete")
            user = self.users.add_user(user)
        return self._create_session_result(user)

    def login(self, *, username: str, password: str) -> AuthSessionResult:
        user = self.users.get_user_by_username(username)
        if user is None or not _verify_password(password, user):
            raise AuthInvalidCredentialsError("invalid username or password")
        return self._create_session_result(user)

    def logout(self, session_token: str | None) -> bool:
        if not session_token:
            return False
        return self.sessions.revoke_session(_token_digest(session_token))

    def get_session(self, session_token: str | None) -> AuthSessionResult:
        if not session_token:
            raise AuthSessionInvalidError("valid session required")
        session = self.sessions.get_session_by_digest(_token_digest(session_token))
        if session is None or not session.active:
            raise AuthSessionInvalidError("valid session required")
        user = self.users.get_user(session.userId)
        if user is None:
            raise AuthSessionInvalidError("session user was not found")
        return AuthSessionResult(user=_public_user(user), sessionId=session.sessionId, expiresAt=session.expiresAt)

    def _create_user(self, *, username: str, password: str, role: str) -> StoredAuthUser:
        return self.users.add_user(self._build_user(username=username, password=password, role=role))

    def _build_user(self, *, username: str, password: str, role: str) -> StoredAuthUser:
        normalized_username = _normalize_username(username)
        _validate_password(password)
        salt = secrets.token_hex(16)
        now = datetime.now(UTC)
        return StoredAuthUser(
            userId=f"user_{secrets.token_hex(8)}",
            username=normalized_username,
            role=role,
            createdAt=now,
            updatedAt=now,
            passwordHash=_password_hash(password, salt, PASSWORD_HASH_ITERATIONS),
            passwordSalt=salt,
            passwordIterations=PASSWORD_HASH_ITERATIONS,
        )

    def _create_session_result(self, user: StoredAuthUser) -> AuthSessionResult:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self.session_ttl_seconds)
        session = AuthSession(
            sessionId=f"sess_{secrets.token_hex(12)}",
            userId=user.userId,
            tokenDigest=_token_digest(token),
            createdAt=now,
            expiresAt=expires_at,
        )
        self.sessions.create_session(session)
        return AuthSessionResult(
            user=_public_user(user),
            sessionId=session.sessionId,
            expiresAt=expires_at,
            sessionToken=token,
        )

    def create_identity_session(self, *, provider: str, subject: str, username: str) -> AuthSessionResult:
        resolver = getattr(self.users, "resolve_identity", None)
        if not callable(resolver):
            raise RuntimeError("auth user repository does not support public identities")
        user = resolver(provider=provider, subject=subject, username=username)
        if user.role != "user":
            raise AuthSessionInvalidError("public identity resolved to a privileged account")
        return self._create_session_result(user)

    def public_methods(self) -> dict[str, Any]:
        return self._public_auth().methods()

    def request_otp(
        self,
        *,
        channel: str,
        destination: str,
        consent: bool,
        browser_binding: str,
    ) -> dict[str, Any]:
        return self._public_auth().request_otp(
            channel=channel,
            destination=destination,
            consent=consent,
            browser_binding=browser_binding,
        )

    def verify_otp(self, *, challenge_id: str, code: str, browser_binding: str) -> AuthSessionResult:
        return self._public_auth().verify_otp(
            challenge_id=challenge_id,
            code=code,
            browser_binding=browser_binding,
        )

    def start_oauth(
        self,
        *,
        provider: str,
        consent: bool,
        browser_binding: str,
    ) -> dict[str, Any]:
        return self._public_auth().start_oauth(
            provider=provider,
            consent=consent,
            browser_binding=browser_binding,
        )

    def complete_oauth(
        self,
        *,
        provider: str,
        code: str,
        state: str,
        browser_binding: str,
    ) -> AuthSessionResult:
        return self._public_auth().complete_oauth(
            provider=provider,
            code=code,
            state=state,
            browser_binding=browser_binding,
        )

    def _public_auth(self) -> Any:
        if self._public_auth_service is None:
            from interfaces.services.account_auth.service import PublicAccountAuthService

            self._public_auth_service = PublicAccountAuthService(
                self,
                settings=self._public_auth_settings,
                email_delivery=self._email_delivery,
                phone_delivery=self._phone_delivery,
                oauth_providers=self._oauth_providers,
                clock=self._public_clock,
            )
        return self._public_auth_service


def _public_user(user: StoredAuthUser) -> AuthUser:
    return AuthUser(
        userId=user.userId,
        username=user.username,
        role=user.role,
        createdAt=user.createdAt,
        updatedAt=user.updatedAt,
    )


def _normalize_username(value: str) -> str:
    username = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", username):
        raise ValueError("username must be 3-64 letters, numbers, dots, underscores, or hyphens")
    return username


def _validate_password(value: str) -> None:
    if len(value) < 8:
        raise ValueError("password must be at least 8 characters")
    if len(value) > 256:
        raise ValueError("password is too long")


def _password_hash(password: str, salt: str, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return digest.hex()


def _verify_password(password: str, user: StoredAuthUser) -> bool:
    if not user.passwordHash or not user.passwordSalt or user.passwordIterations <= 0:
        return False
    expected = _password_hash(password, user.passwordSalt, user.passwordIterations)
    return hmac.compare_digest(expected, user.passwordHash)


def _unique_public_username(preferred: str, users: Any) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", preferred.strip()).strip("._-")[:48]
    if len(base) < 3:
        base = "Agora_user"
    existing = {item.username.lower() for item in users}
    if base.lower() not in existing:
        return base
    while True:
        candidate = f"{base[:48]}_{secrets.token_hex(3)}"
        if candidate.lower() not in existing:
            return candidate


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _format_datetime(value: datetime) -> str:
    actual = value if value.tzinfo else value.replace(tzinfo=UTC)
    return actual.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
