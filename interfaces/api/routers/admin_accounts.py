"""Administrator account management for the hosted service."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from interfaces.api.deps import ApiRouteHelpers, ApiServices
from interfaces.api.routers.account_errors import run_account_call, signed_in
from interfaces.services.auth_service import AuthUser

AccountRole = Literal["admin", "user"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCreateRequest(_Strict):
    username: str = Field(..., min_length=3, max_length=64)
    role: AccountRole = "user"
    # Omitted to have the server generate a one-time initial password.
    password: str | None = Field(default=None, min_length=8, max_length=256)


class AccountPasswordResetRequest(_Strict):
    password: str | None = Field(default=None, min_length=8, max_length=256)


class AccountRoleRequest(_Strict):
    role: AccountRole


def create_router(services: ApiServices, helpers: ApiRouteHelpers) -> APIRouter:
    runtime = services.hosted_runtime
    if runtime is None:
        raise RuntimeError("administrator routes require the hosted runtime")
    accounts = runtime.accounts
    router = APIRouter()

    @router.get("/api/v1/admin/accounts")
    def list_accounts():
        return run_account_call(
            helpers,
            lambda: {"accounts": [item.to_dict() for item in accounts.list_accounts()]},
        )

    @router.post("/api/v1/admin/accounts")
    def create_account(payload: AccountCreateRequest):
        def call():
            account, generated = accounts.create_account(
                username=payload.username, role=payload.role, password=payload.password
            )
            return _with_password(account, generated)

        return run_account_call(helpers, call)

    @router.post("/api/v1/admin/accounts/{user_id}/disable")
    def disable_account(user_id: str, request: Request):
        return run_account_call(
            helpers,
            lambda: {
                "account": accounts.set_enabled(
                    actor_user_id=signed_in(request).user_id, user_id=user_id, enabled=False
                ).to_dict()
            },
        )

    @router.post("/api/v1/admin/accounts/{user_id}/enable")
    def enable_account(user_id: str, request: Request):
        return run_account_call(
            helpers,
            lambda: {
                "account": accounts.set_enabled(
                    actor_user_id=signed_in(request).user_id, user_id=user_id, enabled=True
                ).to_dict()
            },
        )

    @router.post("/api/v1/admin/accounts/{user_id}/password")
    def reset_password(user_id: str, payload: AccountPasswordResetRequest):
        def call():
            account, generated = accounts.reset_password(user_id=user_id, password=payload.password)
            return _with_password(account, generated)

        return run_account_call(helpers, call)

    @router.put("/api/v1/admin/accounts/{user_id}/role")
    def change_role(user_id: str, payload: AccountRoleRequest):
        return run_account_call(
            helpers,
            lambda: {"account": accounts.change_role(user_id=user_id, role=payload.role).to_dict()},
        )

    return router


def _with_password(account: AuthUser, generated: str | None) -> dict:
    payload: dict = {"account": account.to_dict()}
    if generated is not None:
        # Shown once to the administrator; the server keeps only the hash.
        payload["initialPassword"] = generated
    return payload


__all__ = ["create_router"]
