from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header
from pydantic import BaseModel, ConfigDict, Field

from interfaces.api.deps import ApiRouteHelpers, ApiServices
from interfaces.services.auth_service import AuthSessionInvalidError
from interfaces.services.research_workspace_model import ResearchActivity, WorkspaceItems
from interfaces.services.research_conversation_model import ResearchConversation
from interfaces.services.research_history_service import (
    MAX_GROUPS,
    MAX_VISITS,
    ResearchHistoryConflictError,
    ResearchHistoryError,
    ResearchHistoryStorageError,
)


class ResearchHistoryVisitInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    module: str
    question: str
    href: str
    scrollY: int | float
    createdAt: int | float
    updatedAt: int | float
    title: str
    groupId: str | None
    isFavorite: bool
    deletedAt: int | float | None
    archivedAt: int | None = None
    activity: ResearchActivity | None = None
    conversation: ResearchConversation | None = None


class ResearchHistoryGroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    name: str
    createdAt: int | float
    updatedAt: int | float


class ResearchHistoryReplaceRequest(WorkspaceItems):
    model_config = ConfigDict(extra="forbid", strict=True)

    revision: int = Field(ge=0)
    visits: list[ResearchHistoryVisitInput] = Field(max_length=MAX_VISITS)
    groups: list[ResearchHistoryGroupInput] = Field(max_length=MAX_GROUPS)


def create_router(services: ApiServices, helpers: ApiRouteHelpers) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/research/history")
    def get_history(x_newsroom_session: str | None = Header(default=None)):
        try:
            user_id = _authenticated_user_id(services, x_newsroom_session)
            result = services.research_history_service_factory().read(user_id=user_id)
        except AuthSessionInvalidError as exc:
            return helpers.error(
                status_code=401,
                code=exc.code,
                message="Valid account session required",
                user_action_required=True,
            )
        except ResearchHistoryStorageError as exc:
            return _history_error(helpers, exc)
        return helpers.success(result.to_dict())

    @router.put("/api/v1/research/history")
    def replace_history(
        request: ResearchHistoryReplaceRequest,
        x_newsroom_session: str | None = Header(default=None),
    ):
        try:
            user_id = _authenticated_user_id(services, x_newsroom_session)
            result = services.research_history_service_factory().replace(
                user_id=user_id,
                revision=request.revision,
                visits=[item.model_dump(exclude_unset=True) for item in request.visits],
                groups=[item.model_dump() for item in request.groups],
                workspace_items=request.model_dump(exclude={"revision", "visits", "groups"}, exclude_unset=True),
            )
        except AuthSessionInvalidError as exc:
            return helpers.error(
                status_code=401,
                code=exc.code,
                message="Valid account session required",
                user_action_required=True,
            )
        except (ResearchHistoryConflictError, ResearchHistoryError, ResearchHistoryStorageError) as exc:
            return _history_error(helpers, exc)
        return helpers.success(result.to_dict())

    return router


def _authenticated_user_id(services: ApiServices, session_token: str | None) -> str:
    return services.auth_service_factory().get_session(session_token).user.userId


def _history_error(helpers: ApiRouteHelpers, exc: Any):
    return helpers.error(
        status_code=exc.status_code,
        code=exc.code,
        message=str(exc),
        details=getattr(exc, "details", None),
        retryable=isinstance(exc, ResearchHistoryStorageError),
        user_action_required=isinstance(exc, (ResearchHistoryError, ResearchHistoryConflictError)),
    )


__all__ = ["ResearchHistoryReplaceRequest", "create_router"]
