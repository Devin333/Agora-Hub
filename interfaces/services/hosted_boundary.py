"""Default-deny request boundary for the hosted paper-agent service.

The hosted profile serves exactly one product: the paper workspace reached
through the web BFF. Everything the boundary does not recognise is answered as
absent, so a new router never becomes reachable from the internet by accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address

from fastapi import Request

from interfaces.models import ActorContext
from interfaces.services.auth_service import (
    AuthApplicationService,
    AuthSessionInvalidError,
    AuthSessionResult,
)
from interfaces.services.deployment_profile import HostedSettings

SESSION_HEADER = "x-newsroom-session"
SESSION_COOKIE = "newsroom_session"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
READ_METHODS = frozenset({"GET", "HEAD"})

_PUBLIC_ROUTES = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/health/live"),
        ("GET", "/health/ready"),
        ("GET", "/api/v1/auth/methods"),
        ("GET", "/api/v1/auth/session"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
    }
)
LOGIN_PATH = "/api/v1/auth/login"
_RESEARCH_PREFIX = "/api/v1/research/"
_DESKTOP_PREFIX = "/api/v1/research/desktop/"
_RESEARCH_MUTATIONS = frozenset(
    {
        ("PUT", "/api/v1/research/history"),
        ("POST", "/api/v1/research/guided/intent"),
        ("POST", "/api/v1/research/guided/search"),
    }
)
_ACCOUNT_ROOT = "/api/v1/account"
_ADMIN_ACCOUNTS_ROOT = "/api/v1/admin/accounts"
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~:@!$&'()*+,;=%/-]*$")


class HostedRouteAccess(StrEnum):
    PUBLIC = "public"
    ACCOUNT = "account"
    ADMIN = "admin"
    RESEARCH = "research"
    UNAVAILABLE = "unavailable"


class HostedBoundaryError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class HostedSessionActor:
    actor: ActorContext
    session: AuthSessionResult

    @property
    def user_id(self) -> str:
        return self.session.user.userId

    @property
    def is_admin(self) -> bool:
        return self.session.user.role == "admin"


def classify_hosted_route(method: str, path: str) -> HostedRouteAccess:
    """Map one request onto the hosted allow-list; unknown means unavailable."""

    verb = "GET" if method.upper() == "HEAD" else method.upper()
    if not _SAFE_PATH.fullmatch(path) or "//" in path or "/../" in f"{path}/":
        return HostedRouteAccess.UNAVAILABLE
    if (verb, path) in _PUBLIC_ROUTES:
        return HostedRouteAccess.PUBLIC
    if _is_under(path, _ACCOUNT_ROOT) and verb in READ_METHODS | MUTATING_METHODS:
        return HostedRouteAccess.ACCOUNT
    if _is_under(path, _ADMIN_ACCOUNTS_ROOT) and verb in READ_METHODS | MUTATING_METHODS:
        return HostedRouteAccess.ADMIN
    if path.startswith(_DESKTOP_PREFIX) and verb in READ_METHODS | MUTATING_METHODS:
        return HostedRouteAccess.RESEARCH
    if path.startswith(_RESEARCH_PREFIX):
        # Outside the durable desktop operations, the research surface is read
        # only. Legacy synchronous writes (imports, parse, analyze, ask,
        # catalog refresh) bypass admission, ownership receipts and quotas.
        if verb in READ_METHODS or (verb, path) in _RESEARCH_MUTATIONS:
            return HostedRouteAccess.RESEARCH
    return HostedRouteAccess.UNAVAILABLE


def require_trusted_origin(request: Request, settings: HostedSettings) -> None:
    if request.method.upper() not in MUTATING_METHODS:
        return
    origin = str(request.headers.get("origin") or "").strip().rstrip("/").lower()
    if origin != settings.public_origin:
        raise HostedBoundaryError(
            "origin_forbidden",
            "Request origin is not the configured service origin",
            status_code=403,
        )


def client_address(request: Request, settings: HostedSettings) -> str:
    """Return the first untrusted hop, reading forwarded headers only from proxies."""

    peer = request.client.host if request.client else ""
    if not _trusted(peer, settings):
        return peer or "unknown"
    forwarded = [
        item.strip()
        for item in str(request.headers.get("x-forwarded-for") or "").split(",")
        if item.strip()
    ]
    for hop in reversed(forwarded):
        if not _valid_ip(hop):
            return peer or "unknown"
        if not _trusted(hop, settings):
            return hop
    return forwarded[0] if forwarded else (peer or "unknown")


def session_token(request: Request) -> str | None:
    value = request.headers.get(SESSION_HEADER) or request.cookies.get(SESSION_COOKIE)
    token = str(value or "").strip()
    return token or None


def authenticate_session(
    request: Request,
    auth_service: AuthApplicationService,
    *,
    settings: HostedSettings,
) -> HostedSessionActor:
    token = session_token(request)
    if token is None:
        raise HostedBoundaryError("auth_required", "Valid account session required", status_code=401)
    try:
        session = auth_service.get_session(token)
    except AuthSessionInvalidError as exc:
        raise HostedBoundaryError(
            "auth_required", "Valid account session required", status_code=401
        ) from exc
    user = session.user
    actor = ActorContext(
        actor_id=user.userId,
        actor_type="user",
        # Account roles (admin/user) govern account administration only and
        # are checked by this boundary; they never map onto operator RBAC
        # roles, so a hosted administrator gains no operator permissions.
        roles=[],
        request_id=str(getattr(request.state, "request_id", "") or ""),
        ip_address=client_address(request, settings),
        user_agent=request.headers.get("user-agent"),
        metadata={
            "user_id": user.userId,
            "auth_session_id": session.sessionId,
            "account_role": user.role,
        },
    )
    return HostedSessionActor(actor=actor, session=session)


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _valid_ip(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


def _trusted(address: str, settings: HostedSettings) -> bool:
    try:
        parsed = ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in settings.trusted_proxies)


__all__ = [
    "HostedBoundaryError",
    "HostedRouteAccess",
    "HostedSessionActor",
    "LOGIN_PATH",
    "MUTATING_METHODS",
    "SESSION_COOKIE",
    "SESSION_HEADER",
    "authenticate_session",
    "classify_hosted_route",
    "client_address",
    "require_trusted_origin",
    "session_token",
]
