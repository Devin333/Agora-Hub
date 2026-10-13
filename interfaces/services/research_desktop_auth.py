from __future__ import annotations

import os
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from fastapi import Request

from interfaces.models import ActorContext
from interfaces.services.auth_service import AuthApplicationService, AuthSessionInvalidError

DEFAULT_DESKTOP_CANONICAL_ORIGIN = "http://127.0.0.1:3000"
DESKTOP_PROFILE = "local-desktop"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ANONYMOUS_AUTH_PATHS = frozenset(
    {
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/login",
        "/api/v1/auth/methods",
    }
)


class ResearchDesktopAuthError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def local_desktop_profile_enabled(env: Mapping[str, str] | None = None) -> bool:
    values = env if env is not None else os.environ
    profile = str(values.get("NEWSROOM_DEPLOYMENT_PROFILE") or "default").strip() or "default"
    if profile not in {"default", DESKTOP_PROFILE}:
        raise ResearchDesktopAuthError(
            "deployment_profile_invalid",
            "NEWSROOM_DEPLOYMENT_PROFILE must be default or local-desktop",
            status_code=500,
        )
    return profile == DESKTOP_PROFILE



def local_desktop_runtime_identity(env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    values = env if env is not None else os.environ
    if not local_desktop_profile_enabled(values):
        return None
    instance_key = _desktop_identity_text(values, "NEWSROOM_DESKTOP_INSTANCE_KEY")
    expected_instance_key = f"{DESKTOP_PROFILE}:{desktop_canonical_origin(values)}"
    if instance_key != expected_instance_key:
        raise ResearchDesktopAuthError(
            "desktop_instance_key_invalid",
            "NEWSROOM_DESKTOP_INSTANCE_KEY must identify the configured local-desktop origin",
            status_code=503,
        )
    release_id = _desktop_identity_text(values, "NEWSROOM_DESKTOP_RELEASE_ID")
    config_digest = str(values.get("NEWSROOM_DESKTOP_CONFIG_DIGEST") or "").strip().lower()
    boot_id = str(values.get("NEWSROOM_DESKTOP_BOOT_ID") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", config_digest):
        raise ResearchDesktopAuthError(
            "desktop_config_digest_invalid",
            "NEWSROOM_DESKTOP_CONFIG_DIGEST must be 64 lowercase hexadecimal characters",
            status_code=503,
        )
    if not re.fullmatch(r"[0-9a-f]{32}", boot_id):
        raise ResearchDesktopAuthError(
            "desktop_boot_id_invalid",
            "NEWSROOM_DESKTOP_BOOT_ID must be 32 lowercase hexadecimal characters",
            status_code=503,
        )
    return {
        "deployment_profile": DESKTOP_PROFILE,
        "instance_key": instance_key,
        "release_id": release_id,
        "config_digest": config_digest,
        "boot_id": boot_id,
    }


def _desktop_identity_text(values: Mapping[str, str], name: str) -> str:
    value = str(values.get(name) or "").strip()
    if not value or len(value) > 256 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        raise ResearchDesktopAuthError(
            "desktop_runtime_identity_invalid",
            f"{name} is required and must contain visible ASCII characters",
            status_code=503,
        )
    return value


def desktop_canonical_origin(env: Mapping[str, str] | None = None) -> str:
    values = env if env is not None else os.environ
    desktop_value = str(values.get("NEWSROOM_DESKTOP_CANONICAL_ORIGIN") or "").strip()
    legacy_value = str(values.get("NEWSROOM_CANONICAL_ORIGIN") or "").strip()
    desktop_origin = _validated_loopback_origin(desktop_value) if desktop_value else None
    legacy_origin = _validated_loopback_origin(legacy_value) if legacy_value else None
    if desktop_origin and legacy_origin and desktop_origin != legacy_origin:
        raise ResearchDesktopAuthError(
            "desktop_origin_conflict",
            "Desktop canonical origin settings disagree",
            status_code=500,
        )
    return desktop_origin or legacy_origin or _validated_loopback_origin(DEFAULT_DESKTOP_CANONICAL_ORIGIN)


def _validated_loopback_origin(value: str) -> str:
    origin = str(value or "").strip().rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ResearchDesktopAuthError(
            "desktop_origin_invalid",
            "Configured desktop origin must be an absolute loopback HTTP(S) origin",
            status_code=500,
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def require_desktop_origin(request: Request) -> None:
    if request.method.upper() not in _MUTATING_METHODS:
        return
    origin = str(request.headers.get("origin") or "").strip().rstrip("/")
    if not origin or origin != desktop_canonical_origin():
        raise ResearchDesktopAuthError(
            "desktop_origin_forbidden",
            "Mutation origin is not the configured desktop origin",
            status_code=403,
        )


def desktop_session_token(request: Request) -> str | None:
    value = request.headers.get("x-newsroom-session") or request.cookies.get("newsroom_session")
    token = str(value or "").strip()
    return token or None


def authenticate_desktop_actor(
    request: Request,
    auth_service: AuthApplicationService,
    *,
    allow_anonymous_auth: bool = False,
) -> ActorContext | None:
    require_desktop_origin(request)
    if allow_anonymous_auth and request.url.path in _ANONYMOUS_AUTH_PATHS:
        return None
    token = desktop_session_token(request)
    if token is None:
        raise ResearchDesktopAuthError(
            "auth_required",
            "Valid account session required",
            status_code=401,
        )
    try:
        session = auth_service.get_session(token)
    except AuthSessionInvalidError as exc:
        raise ResearchDesktopAuthError(
            "auth_required",
            "Valid account session required",
            status_code=401,
        ) from exc
    user = session.user
    return ActorContext(
        actor_id=user.userId,
        actor_type="user",
        roles=[user.role],
        request_id=str(getattr(request.state, "request_id", "") or ""),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata={"user_id": user.userId, "auth_session_id": session.session.sessionId},
    )



def desktop_operator_path_forbidden(path: str) -> bool:
    return (
        path.startswith("/api/v2/graph-runs")
        or path.startswith("/api/v1/reports")
        or path.startswith("/api/v1/search/reports")
        or path.startswith("/api/v1/storage/")
        or path.startswith("/api/v1/memory/")
        or path.startswith("/api/v1/events/replay-reports")
        or path.startswith("/api/v1/events/projections/runs/")
        or path == "/api/v1/mcp/resources/read"
        or (path.startswith("/api/v1/mcp/tools/") and path.endswith("/call"))
    )


def desktop_path_requires_session(path: str) -> bool:
    return path.startswith("/api/v1/research/")


def desktop_anonymous_auth_path(path: str) -> bool:
    return path in _ANONYMOUS_AUTH_PATHS


def desktop_auth_path(path: str) -> bool:
    return path.startswith("/api/v1/auth/")


def desktop_legacy_operation_path(path: str, method: str) -> bool:
    if method.upper() != "POST" or path.startswith("/api/v1/research/desktop/"):
        return False
    if path == "/api/v1/research/imports" or path.endswith("/retry") and "/api/v1/research/imports/" in path:
        return True
    if path in {"/api/v1/research/papers/parse", "/api/v1/research/papers/analyze"}:
        return True
    return path.endswith("/ask") or path.endswith("/rag-ask")


__all__ = [
    "DEFAULT_DESKTOP_CANONICAL_ORIGIN",
    "ResearchDesktopAuthError",
    "authenticate_desktop_actor",
    "desktop_anonymous_auth_path",
    "desktop_auth_path",
    "desktop_legacy_operation_path",
    "desktop_operator_path_forbidden",
    "desktop_canonical_origin",
    "desktop_path_requires_session",
    "desktop_session_token",
    "local_desktop_profile_enabled",
    "local_desktop_runtime_identity",
    "require_desktop_origin",
]
