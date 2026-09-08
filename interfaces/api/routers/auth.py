from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from fastapi import APIRouter, Header

from interfaces.api.deps import ApiRouteHelpers, ApiServices
from interfaces.services.auth_service import (
    AuthAlreadyInitializedError,
    AuthInvalidCredentialsError,
    AuthSessionInvalidError,
)
from interfaces.services.account_auth.errors import PublicAuthError, AuthChallengeRateLimitedError


class AuthCredentialsRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)


class AuthLogoutRequest(BaseModel):
    sessionToken: str | None = Field(default=None, max_length=512)


class AuthOtpChallengeRequest(BaseModel):
    channel: Literal["phone", "email"]
    destination: str = Field(..., min_length=3, max_length=254)
    consent: bool


class AuthOtpVerifyRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)


class AuthOAuthAuthorizeRequest(BaseModel):
    consent: bool


class AuthOAuthCallbackRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=4096)
    state: str = Field(..., min_length=1, max_length=512)


def create_router(services: ApiServices, helpers: ApiRouteHelpers) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/auth/methods")
    def methods():
        return helpers.success(services.auth_service_factory().public_methods())

    @router.post("/api/v1/auth/otp/challenges")
    def create_otp_challenge(
        request: AuthOtpChallengeRequest,
        x_newsroom_auth_binding: str | None = Header(default=None),
    ):
        try:
            result = services.auth_service_factory().request_otp(
                channel=request.channel,
                destination=request.destination,
                consent=request.consent,
                browser_binding=x_newsroom_auth_binding or "",
            )
        except PublicAuthError as exc:
            return _public_auth_error(helpers, exc)
        return helpers.success(result)

    @router.post("/api/v1/auth/otp/challenges/{challenge_id}/verify")
    def verify_otp(
        challenge_id: str,
        request: AuthOtpVerifyRequest,
        x_newsroom_auth_binding: str | None = Header(default=None),
    ):
        try:
            result = services.auth_service_factory().verify_otp(
                challenge_id=challenge_id,
                code=request.code,
                browser_binding=x_newsroom_auth_binding or "",
            )
        except PublicAuthError as exc:
            return _public_auth_error(helpers, exc)
        return helpers.success({"session": result.to_dict(include_token=True)})

    @router.post("/api/v1/auth/oauth/{provider}/authorize")
    def authorize_oauth(
        provider: str,
        request: AuthOAuthAuthorizeRequest,
        x_newsroom_auth_binding: str | None = Header(default=None),
    ):
        try:
            result = services.auth_service_factory().start_oauth(
                provider=provider,
                consent=request.consent,
                browser_binding=x_newsroom_auth_binding or "",
            )
        except PublicAuthError as exc:
            return _public_auth_error(helpers, exc)
        return helpers.success(result)

    @router.post("/api/v1/auth/oauth/{provider}/callback")
    def complete_oauth(
        provider: str,
        request: AuthOAuthCallbackRequest,
        x_newsroom_auth_binding: str | None = Header(default=None),
    ):
        try:
            result = services.auth_service_factory().complete_oauth(
                provider=provider,
                code=request.code,
                state=request.state,
                browser_binding=x_newsroom_auth_binding or "",
            )
        except PublicAuthError as exc:
            return _public_auth_error(helpers, exc)
        return helpers.success({"session": result.to_dict(include_token=True)})

    @router.post("/api/v1/auth/bootstrap")
    def bootstrap(request: AuthCredentialsRequest):
        try:
            result = services.auth_service_factory().bootstrap(
                username=request.username,
                password=request.password,
            )
        except AuthAlreadyInitializedError as exc:
            return helpers.error(
                status_code=409,
                code=exc.code,
                message=str(exc),
                user_action_required=True,
            )
        except ValueError as exc:
            return helpers.error(
                status_code=400,
                code="auth_invalid_request",
                message=str(exc),
                user_action_required=True,
            )
        return helpers.success({"session": result.to_dict(include_token=True)})

    @router.post("/api/v1/auth/login")
    def login(request: AuthCredentialsRequest):
        try:
            result = services.auth_service_factory().login(
                username=request.username,
                password=request.password,
            )
        except AuthInvalidCredentialsError as exc:
            return helpers.error(
                status_code=401,
                code=exc.code,
                message=str(exc),
                user_action_required=True,
            )
        except ValueError as exc:
            return helpers.error(
                status_code=400,
                code="auth_invalid_request",
                message=str(exc),
                user_action_required=True,
            )
        return helpers.success({"session": result.to_dict(include_token=True)})

    @router.post("/api/v1/auth/logout")
    def logout(request: AuthLogoutRequest, x_newsroom_session: str | None = Header(default=None)):
        token = request.sessionToken or x_newsroom_session
        revoked = services.auth_service_factory().logout(token)
        return helpers.success({"revoked": revoked})

    @router.get("/api/v1/auth/session")
    def session(x_newsroom_session: str | None = Header(default=None)):
        auth_service = services.auth_service_factory()
        if not x_newsroom_session:
            return helpers.success({"initialized": auth_service.is_initialized(), "session": None})
        try:
            result = auth_service.get_session(x_newsroom_session)
        except AuthSessionInvalidError:
            return helpers.success({"initialized": auth_service.is_initialized(), "session": None})
        return helpers.success({"initialized": True, "session": result.to_dict()})

    return router


def _public_auth_error(helpers: ApiRouteHelpers, exc: PublicAuthError):
    headers = None
    details = None
    if isinstance(exc, AuthChallengeRateLimitedError):
        headers = {"Retry-After": str(exc.retry_after)}
        details = {"retryAfter": exc.retry_after}
    return helpers.error(
        status_code=exc.status_code,
        code=exc.code,
        message=str(exc),
        details=details,
        retryable=exc.retryable,
        user_action_required=exc.status_code in {400, 401, 410, 429},
        headers=headers,
    )
