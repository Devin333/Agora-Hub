"""Self-service routes for the signed-in hosted account."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from interfaces.api.deps import ApiRouteHelpers, ApiServices
from interfaces.api.routers.account_errors import run_account_call, signed_in
from interfaces.composition.hosted import HostedRuntime
from interfaces.services.hosted_boundary import session_token


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PasswordChangeRequest(_Strict):
    currentPassword: str = Field(..., min_length=1, max_length=256)
    newPassword: str = Field(..., min_length=8, max_length=256)


class ModelCredentialSaveRequest(_Strict):
    providerId: str = Field(..., min_length=1, max_length=64)
    model: str = Field(..., min_length=1, max_length=128)
    # Omitted to change only the model of the stored provider.
    apiKey: str | None = Field(default=None, min_length=1, max_length=512)
    expectedRevision: int | None = Field(default=None, ge=1)


class ModelCredentialDeleteRequest(_Strict):
    expectedRevision: int | None = Field(default=None, ge=1)


def create_router(services: ApiServices, helpers: ApiRouteHelpers) -> APIRouter:
    runtime = services.hosted_runtime
    if runtime is None:
        raise RuntimeError("account routes require the hosted runtime")
    router = APIRouter()

    @router.get("/api/v1/account")
    def get_account(request: Request):
        return run_account_call(
            helpers,
            lambda: {"account": runtime.auth.get_account(signed_in(request).user_id).to_dict()},
        )

    @router.post("/api/v1/account/password")
    def change_password(payload: PasswordChangeRequest, request: Request):
        def call():
            refreshed = runtime.auth.change_own_password(
                session_token=session_token(request),
                current_password=payload.currentPassword,
                new_password=payload.newPassword,
            )
            # The old session is void now; the BFF swaps in this token.
            return {"session": refreshed.to_dict(include_token=True)}

        return run_account_call(helpers, call)

    @router.get("/api/v1/account/model-credential")
    def get_model_credential(request: Request):
        return run_account_call(helpers, lambda: _credential_payload(runtime, signed_in(request).user_id))

    @router.put("/api/v1/account/model-credential")
    def save_model_credential(payload: ModelCredentialSaveRequest, request: Request):
        def call():
            user_id = signed_in(request).user_id
            runtime.model_credentials.save(
                user_id,
                provider_id=payload.providerId,
                model=payload.model,
                api_key=payload.apiKey,
                expected_revision=payload.expectedRevision,
            )
            return _credential_payload(runtime, user_id)

        return run_account_call(helpers, call)

    @router.delete("/api/v1/account/model-credential")
    def delete_model_credential(request: Request):
        def call():
            user_id = signed_in(request).user_id
            runtime.model_credentials.delete(user_id)
            return _credential_payload(runtime, user_id)

        return run_account_call(helpers, call)

    @router.post("/api/v1/account/model-credential/verify")
    def verify_model_credential(request: Request):
        def call():
            user_id = signed_in(request).user_id
            runtime.model_credentials.verify(user_id)
            return _credential_payload(runtime, user_id)

        return run_account_call(helpers, call)

    return router


def _credential_payload(runtime: HostedRuntime, user_id: str) -> dict:
    return {
        "credential": runtime.model_credentials.view(user_id).to_dict(),
        "catalog": runtime.model_credentials.catalog.to_dict(),
    }


__all__ = ["create_router"]
