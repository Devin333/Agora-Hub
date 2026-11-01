"""Self-service routes for the signed-in hosted account."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from interfaces.api.deps import ApiRouteHelpers, ApiServices
from interfaces.api.routers.account_errors import run_account_call, signed_in
from interfaces.services.hosted_boundary import session_token
from interfaces.services.model_connections import ConnectionDraft


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PasswordChangeRequest(_Strict):
    currentPassword: str = Field(..., min_length=1, max_length=256)
    newPassword: str = Field(..., min_length=8, max_length=256)


class ModelConnectionFields(_Strict):
    providerId: str = Field(..., min_length=1, max_length=64)
    model: str = Field(..., min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=64)
    reasoningEffort: str | None = Field(default=None, max_length=16)
    # Omitted on update to keep the stored key; required on create.
    apiKey: str | None = Field(default=None, min_length=1, max_length=512)
    # Custom providers only; catalog providers take these from the catalog.
    baseUrl: str | None = Field(default=None, max_length=2048)
    contextWindowTokens: int | None = None
    maxOutputTokens: int | None = None
    reasoningParameter: str | None = Field(default=None, max_length=64)
    structuredOutput: str | None = Field(default=None, max_length=32)

    def draft(self) -> ConnectionDraft:
        return ConnectionDraft(
            provider_id=self.providerId,
            model=self.model,
            label=self.label,
            reasoning_effort=self.reasoningEffort,
            api_key=self.apiKey,
            base_url=self.baseUrl,
            context_window_tokens=self.contextWindowTokens,
            max_output_tokens=self.maxOutputTokens,
            reasoning_parameter=self.reasoningParameter,
            structured_output=self.structuredOutput,
        )


class ModelConnectionWriteRequest(ModelConnectionFields):
    # The account revision the browser last saw; a stale one is a conflict.
    expectedRevision: int = Field(..., ge=0)


class ModelAssignmentRequest(_Strict):
    # Purpose -> connection id, or null to clear; purposes not named keep theirs.
    assignments: dict[str, str | None] = Field(..., max_length=16)
    expectedRevision: int = Field(..., ge=0)


class ModelDiscoveryRequest(_Strict):
    providerId: str = Field(..., min_length=1, max_length=64)
    baseUrl: str | None = Field(default=None, max_length=2048)
    # Either a key typed into the form, or the connection whose stored key
    # may be reused because the provider and address are unchanged.
    apiKey: str | None = Field(default=None, min_length=1, max_length=512)
    connectionId: str | None = Field(default=None, min_length=1, max_length=64)


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

    connections = runtime.model_connections

    @router.get("/api/v1/account/model-connections")
    def list_model_connections(request: Request):
        return run_account_call(helpers, lambda: connections.view(signed_in(request).user_id))

    @router.post("/api/v1/account/model-connections")
    def create_model_connection(payload: ModelConnectionWriteRequest, request: Request):
        return run_account_call(
            helpers,
            lambda: connections.create(
                signed_in(request).user_id, payload.draft(), expected_revision=payload.expectedRevision
            ),
        )

    @router.patch("/api/v1/account/model-connections/{connection_id}")
    def update_model_connection(connection_id: str, payload: ModelConnectionWriteRequest, request: Request):
        return run_account_call(
            helpers,
            lambda: connections.update(
                signed_in(request).user_id,
                connection_id,
                payload.draft(),
                expected_revision=payload.expectedRevision,
            ),
        )

    @router.delete("/api/v1/account/model-connections/{connection_id}")
    def delete_model_connection(
        connection_id: str,
        request: Request,
        expectedRevision: int = Query(..., ge=0),
    ):
        return run_account_call(
            helpers,
            lambda: connections.delete(
                signed_in(request).user_id, connection_id, expected_revision=expectedRevision
            ),
        )

    @router.post("/api/v1/account/model-connections/{connection_id}/verify")
    def verify_model_connection(connection_id: str, request: Request):
        return run_account_call(helpers, lambda: connections.verify(signed_in(request).user_id, connection_id))

    @router.put("/api/v1/account/model-connections/assignments")
    def assign_model_connections(payload: ModelAssignmentRequest, request: Request):
        return run_account_call(
            helpers,
            lambda: connections.assign(
                signed_in(request).user_id, payload.assignments, expected_revision=payload.expectedRevision
            ),
        )

    @router.post("/api/v1/account/model-connections/discover")
    def discover_models(payload: ModelDiscoveryRequest, request: Request):
        return run_account_call(
            helpers,
            lambda: {
                "models": list(
                    connections.discover(
                        signed_in(request).user_id,
                        provider_id=payload.providerId,
                        base_url=payload.baseUrl,
                        api_key=payload.apiKey,
                        connection_id=payload.connectionId,
                    )
                )
            },
        )

    return router


__all__ = ["create_router"]
