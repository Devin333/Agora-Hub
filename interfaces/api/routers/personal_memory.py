from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.research.application.personal_memory import PersonalMemoryStateError
from backend.research.domain.personal_memory import PersonalMemoryStatus
from backend.research.ports.personal_memory import (
    PersonalMemoryStoreConflictError,
    PersonalMemoryStoreCorruptionError,
    PersonalMemoryStoreError,
    PersonalMemoryStoreNotFoundError,
    PersonalMemoryStoreUnavailableError,
    PersonalMemoryStoreValidationError,
)
from interfaces.api.deps import ApiRouteHelpers, ApiServices
from interfaces.models import ActorContext
from interfaces.services.personal_memory_service import PersonalMemoryApplicationService
from interfaces.services.deployment_profile import hosted_profile_enabled
from interfaces.services.research_desktop_service import ResearchDesktopOwner


_MEMORY_PREFIX = "/api/v1/research/desktop/memory"
_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
_DEFAULT_STATUSES = (
    PersonalMemoryStatus.CANDIDATE,
    PersonalMemoryStatus.ACTIVE,
    PersonalMemoryStatus.DISABLED,
)


class MemoryRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MemorySourceRef(MemoryRequestModel):
    source_type: Literal[
        "user_input",
        "reading_note",
        "paper",
        "evidence",
        "conversation",
        "import",
        "other",
    ]
    source_ref: str = Field(min_length=1, max_length=1_024)
    source_revision: str | None = Field(default=None, min_length=1, max_length=256)
    locator: str | None = Field(default=None, min_length=1, max_length=512)
    label: str | None = Field(default=None, min_length=1, max_length=256)


class MemoryContentFields(MemoryRequestModel):
    content: str = Field(min_length=1, max_length=8_000)
    category: Literal["research_interest", "reading_preference", "background"]
    source_refs: list[MemorySourceRef] = Field(min_length=1, max_length=32)


class CreateMemoryRequest(MemoryContentFields):
    creation: Literal["confirmed", "candidate"]
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$",
    )


class MutableMemoryFields(MemoryRequestModel):
    content: str | None = Field(default=None, min_length=1, max_length=8_000)
    category: Literal["research_interest", "reading_preference", "background"] | None = None
    source_refs: list[MemorySourceRef] | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> "MutableMemoryFields":
        for field_name in ("content", "category", "source_refs"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self

    def changes(self) -> dict[str, Any]:
        return self.model_dump(
            include={"content", "category", "source_refs"} & self.model_fields_set,
        )


class UpdateMemoryRequest(MutableMemoryFields):
    revision: int = Field(ge=1)

    @model_validator(mode="after")
    def require_change(self) -> "UpdateMemoryRequest":
        if not self.changes():
            raise ValueError("at least one mutable field is required")
        return self


class ConfirmMemoryRequest(MutableMemoryFields):
    revision: int = Field(ge=1)


class MemoryRevisionRequest(MemoryRequestModel):
    revision: int = Field(ge=1)


class MemoryStatusRequest(MemoryRevisionRequest):
    status: Literal["active", "disabled"]


class MemoryRecallRequest(MemoryRequestModel):
    query: str = Field(min_length=1, max_length=512)
    limit: int = Field(default=5, ge=1, le=20)
    use_memory: bool = True


class MemoryPolicyRequest(MemoryRequestModel):
    allow_remote_processing: bool
    revision: int = Field(ge=1)


def create_router(services: ApiServices, helpers: ApiRouteHelpers) -> APIRouter:
    router = APIRouter()

    @router.get(_MEMORY_PREFIX)
    def list_memories(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[int | None, Query(ge=0, le=10_000)] = None,
        status: Annotated[
            list[Literal["candidate", "active", "disabled", "deleted"]] | None,
            Query(),
        ] = None,
    ):
        if status and (len(status) > 4 or len(set(status)) != len(status)):
            return helpers.error(
                status_code=400,
                code="invalid_request",
                message="Personal memory status filter is invalid.",
                headers=_PRIVATE_HEADERS,
                user_action_required=True,
            )
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.list(
                owner,
                statuses=status or _DEFAULT_STATUSES,
                limit=limit,
                offset=cursor or 0,
            ),
        )

    @router.post(_MEMORY_PREFIX)
    def create_memory(request: Request, body: CreateMemoryRequest):
        def operation(
            service: PersonalMemoryApplicationService,
            owner: ResearchDesktopOwner,
        ) -> dict[str, Any]:
            values = {
                "content": body.content,
                "category": body.category,
                "source_refs": [item.model_dump() for item in body.source_refs],
                "request_id": body.request_id,
            }
            if body.creation == "candidate":
                return service.create_candidate(owner, **values)
            return service.create_confirmed(owner, **values)

        return _memory_response(helpers, request, services, operation)

    @router.get(f"{_MEMORY_PREFIX}/export")
    def export_memories(
        request: Request,
        include_deleted: bool = Query(default=False),
    ):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.export(
                owner,
                include_deleted=include_deleted,
            ),
        )

    @router.post(f"{_MEMORY_PREFIX}/recall")
    def recall_memories(request: Request, body: MemoryRecallRequest):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.recall(
                owner,
                query=body.query,
                limit=body.limit,
                use_memory=body.use_memory,
            ),
        )

    @router.get(f"{_MEMORY_PREFIX}/policy")
    def get_policy(request: Request):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.get_policy(owner),
        )

    @router.put(f"{_MEMORY_PREFIX}/policy")
    def update_policy(request: Request, body: MemoryPolicyRequest):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.update_policy(
                owner,
                allow_remote_processing=body.allow_remote_processing,
                revision=body.revision,
            ),
        )

    @router.post(f"{_MEMORY_PREFIX}/rebuild-index")
    def rebuild_memory_index(request: Request):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.rebuild_recall_index(owner),
        )

    @router.get(f"{_MEMORY_PREFIX}/info")
    def memory_info(request: Request):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.info(owner),
        )

    @router.get(f"{_MEMORY_PREFIX}/{{memory_id}}")
    def get_memory(
        request: Request,
        memory_id: Annotated[str, Path(pattern=r"^pmem_[0-9a-f]{32}$")],
    ):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.get(owner, memory_id),
        )

    @router.patch(f"{_MEMORY_PREFIX}/{{memory_id}}")
    def update_memory(
        request: Request,
        body: UpdateMemoryRequest,
        memory_id: Annotated[str, Path(pattern=r"^pmem_[0-9a-f]{32}$")],
    ):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.update(
                owner,
                memory_id,
                revision=body.revision,
                changes=body.changes(),
            ),
        )

    @router.delete(f"{_MEMORY_PREFIX}/{{memory_id}}")
    def delete_memory(
        request: Request,
        body: Annotated[MemoryRevisionRequest, Body()],
        memory_id: Annotated[str, Path(pattern=r"^pmem_[0-9a-f]{32}$")],
    ):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.delete(
                owner,
                memory_id,
                revision=body.revision,
            ),
        )

    @router.post(f"{_MEMORY_PREFIX}/{{memory_id}}/confirm")
    def confirm_memory(
        request: Request,
        body: ConfirmMemoryRequest,
        memory_id: Annotated[str, Path(pattern=r"^pmem_[0-9a-f]{32}$")],
    ):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.confirm(
                owner,
                memory_id,
                revision=body.revision,
                changes=body.changes(),
            ),
        )

    @router.post(f"{_MEMORY_PREFIX}/{{memory_id}}/status")
    def update_memory_status(
        request: Request,
        body: MemoryStatusRequest,
        memory_id: Annotated[str, Path(pattern=r"^pmem_[0-9a-f]{32}$")],
    ):
        return _memory_response(
            helpers,
            request,
            services,
            lambda service, owner: service.set_status(
                owner,
                memory_id,
                status=body.status,
                revision=body.revision,
            ),
        )

    return router


def _memory_response(
    helpers: ApiRouteHelpers,
    request: Request,
    services: ApiServices,
    operation: Callable[
        [PersonalMemoryApplicationService, ResearchDesktopOwner], dict[str, Any]
    ],
):
    if not hosted_profile_enabled():
        return helpers.error(
            status_code=404,
            code="not_found",
            message="Resource not found",
            headers=_PRIVATE_HEADERS,
        )
    try:
        payload = operation(_service(services), _owner(request))
        return JSONResponse(
            content=helpers.success(payload),
            headers=_PRIVATE_HEADERS,
        )
    except PermissionError:
        return helpers.error(
            status_code=401,
            code="auth_required",
            message="Valid account session required",
            headers=_PRIVATE_HEADERS,
            user_action_required=True,
        )
    except PersonalMemoryStoreNotFoundError as exc:
        return _store_error(helpers, exc, status_code=404)
    except PersonalMemoryStoreConflictError as exc:
        return _store_error(helpers, exc, status_code=409)
    except PersonalMemoryStateError:
        return helpers.error(
            status_code=409,
            code="personal_memory_state_invalid",
            message="Personal memory state conflicts with the request.",
            headers=_PRIVATE_HEADERS,
            user_action_required=True,
        )
    except PersonalMemoryStoreValidationError as exc:
        return _store_error(helpers, exc, status_code=400)
    except PersonalMemoryStoreUnavailableError as exc:
        return _store_error(helpers, exc, status_code=503)
    except PersonalMemoryStoreCorruptionError as exc:
        return _store_error(helpers, exc, status_code=503)
    except PersonalMemoryStoreError as exc:
        return _store_error(helpers, exc, status_code=500)
    except (TypeError, ValueError):
        return helpers.error(
            status_code=400,
            code="invalid_request",
            message="Personal memory request is invalid.",
            headers=_PRIVATE_HEADERS,
            user_action_required=True,
        )


def _store_error(
    helpers: ApiRouteHelpers,
    exc: PersonalMemoryStoreError,
    *,
    status_code: int,
):
    return helpers.error(
        status_code=status_code,
        code=exc.code,
        message=exc.public_message,
        details={"reason": exc.reason_code},
        retryable=exc.retryable,
        user_action_required=status_code in {400, 404, 409},
        headers=_PRIVATE_HEADERS,
    )


def _service(services: ApiServices) -> PersonalMemoryApplicationService:
    factory = services.personal_memory_service_factory
    if factory is None:
        raise RuntimeError("personal memory service factory is unavailable")
    service = factory()
    if not isinstance(service, PersonalMemoryApplicationService):
        raise TypeError("personal memory service factory returned an invalid service")
    return service


def _owner(request: Request) -> ResearchDesktopOwner:
    actor = getattr(request.state, "actor_context", None)
    if not isinstance(actor, ActorContext) or actor.actor_type != "user":
        raise PermissionError("authenticated user required")
    metadata = actor.metadata if isinstance(actor.metadata, dict) else {}
    user_id = str(metadata.get("user_id") or actor.actor_id).strip()
    if not user_id:
        raise PermissionError("authenticated user required")
    return ResearchDesktopOwner(
        user_id=user_id,
        tenant_id=_optional_actor_value(metadata.get("tenant_id")),
        memory_namespace=_optional_actor_value(metadata.get("memory_namespace")),
    )


def _optional_actor_value(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = ["create_router"]
