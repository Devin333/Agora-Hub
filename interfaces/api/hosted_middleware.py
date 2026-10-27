"""HTTP wiring of the hosted request boundary.

The order is fixed and every step fails closed: route allow-list, origin,
sign-in throttle, session, administrator role, per-account write throttle,
then the request runs with the account's model credential bound.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from framework.llm.clients import bind_llm_endpoint_source
from interfaces.api.rate_limit import Clock, InMemoryRateLimiter, RateLimitDecision
from interfaces.composition.hosted import HostedRuntime
from interfaces.services.hosted_boundary import (
    LOGIN_PATH,
    MUTATING_METHODS,
    HostedBoundaryError,
    HostedRouteAccess,
    authenticate_session,
    classify_hosted_route,
    client_address,
    require_trusted_origin,
)

RATE_WINDOW_SECONDS = 60
ErrorResponder = Callable[..., Any]


def install_hosted_boundary(
    api: FastAPI,
    *,
    runtime: HostedRuntime,
    error: ErrorResponder,
    clock: Clock | None = None,
) -> None:
    settings = runtime.settings
    login_limiter = InMemoryRateLimiter(
        limit=settings.login_rate_limit_per_minute,
        window_seconds=RATE_WINDOW_SECONDS,
        clock=clock,
    )
    research_limiter = InMemoryRateLimiter(
        limit=settings.research_rate_limit_per_minute,
        window_seconds=RATE_WINDOW_SECONDS,
        clock=clock,
    )

    @api.middleware("http")
    async def hosted_session_boundary(request: Request, call_next):
        method = request.method.upper()
        path = request.url.path
        try:
            access = classify_hosted_route(method, path)
            if access is HostedRouteAccess.UNAVAILABLE:
                raise HostedBoundaryError(
                    "hosted_route_unavailable", "Not found", status_code=404
                )
            require_trusted_origin(request, settings)
            if access is HostedRouteAccess.PUBLIC:
                if method == "POST" and path == LOGIN_PATH:
                    _throttle(
                        login_limiter,
                        f"login:{client_address(request, settings)}",
                        code="auth_rate_limited",
                        message="Too many sign-in attempts; wait and try again",
                    )
                return await call_next(request)
            # Session lookup reads the account store; keep it off the loop.
            signed_in = await run_in_threadpool(
                authenticate_session, request, runtime.auth, settings=settings
            )
            if access is HostedRouteAccess.ADMIN and not signed_in.is_admin:
                raise HostedBoundaryError(
                    "admin_required", "Administrator role required", status_code=403
                )
            if access is HostedRouteAccess.RESEARCH and method in MUTATING_METHODS:
                _throttle(
                    research_limiter,
                    f"research:{signed_in.user_id}",
                    code="research_rate_limited",
                    message="Too many research requests; wait and try again",
                )
        except HostedBoundaryError as exc:
            return _boundary_error(error, exc)
        except _Throttled as exc:
            return error(
                status_code=429,
                code=exc.code,
                message=exc.message,
                details={
                    "limit": exc.decision.limit,
                    "window_seconds": RATE_WINDOW_SECONDS,
                    "remaining": exc.decision.remaining,
                },
                retryable=True,
                user_action_required=True,
                headers={"Retry-After": str(exc.decision.retry_after_seconds)},
            )
        request.state.actor_context = signed_in.actor
        request.state.hosted_session = signed_in
        # Bound before the downstream task starts, so the endpoint, its
        # thread pool hop and any streamed body all see this account's key.
        with bind_llm_endpoint_source(
            runtime.model_credentials.endpoint_source(signed_in.user_id)
        ):
            return await call_next(request)


class _Throttled(Exception):
    def __init__(self, code: str, message: str, decision: RateLimitDecision) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.decision = decision


def _throttle(limiter: InMemoryRateLimiter, key: str, *, code: str, message: str) -> None:
    decision = limiter.check(key)
    if not decision.allowed:
        raise _Throttled(code, message, decision)


def _boundary_error(error: ErrorResponder, exc: HostedBoundaryError):
    return error(
        status_code=exc.status_code,
        code=exc.code,
        message=str(exc),
        user_action_required=exc.status_code in {401, 403},
    )


__all__ = ["install_hosted_boundary"]
