from __future__ import annotations

from typing import Any, Literal, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from interfaces.api.deps import ApiRouteHelpers, ApiServices
from interfaces.models import ActorContext
from interfaces.services.research_desktop_service import (
    ResearchDesktopApplicationService,
    ResearchDesktopOwner,
    ResearchDesktopServiceError,
)
from interfaces.services.research_import_service import MAX_IMPORT_BYTES, ResearchImportError


class DesktopRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_personal_memory_null(cls, value: Any) -> Any:
        if (
            isinstance(value, Mapping)
            and "personalMemory" in value
            and value["personalMemory"] is None
        ):
            raise ValueError("personalMemory must be omitted when unused")
        return value


class DesktopPersonalMemoryRef(DesktopRequestModel):
    memoryId: str = Field(pattern=r"^pmem_[0-9a-f]{32}$")
    revision: int = Field(ge=1)


class DesktopPersonalMemorySelection(DesktopRequestModel):
    refs: list[DesktopPersonalMemoryRef] = Field(min_length=1, max_length=20)
    policyRevision: int = Field(ge=1)

    @model_validator(mode="after")
    def unique_memory_ids(self) -> "DesktopPersonalMemorySelection":
        if len({item.memoryId for item in self.refs}) != len(self.refs):
            raise ValueError("personalMemory refs must have unique memoryId values")
        return self


class DesktopPrepareRequest(DesktopRequestModel):
    requestId: str = Field(min_length=8, max_length=128)
    generation: str = Field(pattern=r"^gen_[0-9a-f]{32}$")
    requestFingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    paperId: str = Field(min_length=1, max_length=256)
    sourceUrl: str = Field(min_length=1, max_length=4000)
    sourceType: str | None = Field(default=None, max_length=64)


class DesktopAnalyzeRequest(DesktopRequestModel):
    requestId: str = Field(min_length=8, max_length=128)
    generation: str = Field(pattern=r"^gen_[0-9a-f]{32}$")
    requestFingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    paperId: str = Field(min_length=1, max_length=256)
    sourceRevision: str = Field(min_length=1, max_length=512)
    sourceFingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sourceUrl: str | None = Field(default=None, max_length=4000)
    pdfUrl: str | None = Field(default=None, max_length=4000)
    objective: str | None = Field(default=None, max_length=2000)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    personalMemory: DesktopPersonalMemorySelection | None = None


class DesktopAskRequest(DesktopRequestModel):
    requestId: str = Field(min_length=8, max_length=128)
    generation: str = Field(pattern=r"^gen_[0-9a-f]{32}$")
    requestFingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runId: str = Field(min_length=1, max_length=256)
    paperId: str = Field(min_length=1, max_length=256)
    sourceRevision: str = Field(min_length=1, max_length=512)
    sourceFingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    question: str = Field(min_length=1, max_length=4000)
    selection: dict[str, Any] = Field(default_factory=dict)
    locale: str | None = Field(default=None, max_length=32)
    personalMemory: DesktopPersonalMemorySelection | None = None


class DesktopSaveReportRequest(DesktopRequestModel):
    reportId: str = Field(min_length=4, max_length=128)
    runId: str = Field(min_length=1, max_length=256)
    sourceRevision: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=500)


class DesktopDraftMaterial(DesktopRequestModel):
    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=10_000)


class DesktopDraftExportRequest(DesktopRequestModel):
    title: str = Field(min_length=1, max_length=500)
    question: str = Field(min_length=1, max_length=2000)
    notes: str = Field(default="", max_length=30_000)
    materials: list[DesktopDraftMaterial] = Field(default_factory=list, max_length=100)
    format: Literal["markdown", "json"]


class DesktopImportHeaders(DesktopRequestModel):
    requestId: str
    generation: str
    requestFingerprint: str
    sourceRevision: str
    sourceFingerprint: str
    filename: str | None = None

    @field_validator("requestId")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if not 8 <= len(value) <= 128:
            raise ValueError("request id length is invalid")
        return value



def create_router(services: ApiServices, helpers: ApiRouteHelpers) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/research/desktop/identity")
    def identity(request: Request):
        return _desktop_response(
            helpers,
            lambda service, owner: service.identity(owner),
            services=services,
            request=request,
        )

    @router.get("/api/v1/research/desktop/capabilities")
    def capabilities(request: Request):
        return _desktop_response(
            helpers,
            lambda service, owner: service.capabilities(owner),
            services=services,
            request=request,
        )

    @router.get("/api/v1/research/desktop/papers/{paper_id}/reader")
    def reader(request: Request, paper_id: str):
        return _desktop_response(
            helpers,
            lambda service, owner: service.get_reader(owner, paper_id=paper_id),
            services=services,
            request=request,
        )

    @router.get("/api/v1/research/desktop/papers/{paper_id}/source")
    def private_paper_source(
        request: Request,
        paper_id: str,
        sourceRevision: str = Query(min_length=1, max_length=512),
        sourceFingerprint: str = Query(pattern=r"^sha256:[0-9a-fA-F]{64}$"),
    ):
        try:
            source = _service(services).get_private_source(
                _owner(request),
                paper_id=paper_id,
                source_revision=sourceRevision,
                source_fingerprint=sourceFingerprint,
            )
            encoded_name = quote(str(source["filename"]), safe="")
            return Response(
                content=source["content"],
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f"inline; filename*=UTF-8''{encoded_name}",
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except (ResearchDesktopServiceError, ResearchImportError) as exc:
            error = exc if isinstance(exc, ResearchDesktopServiceError) else ResearchDesktopServiceError(
                exc.code, str(exc), status_code=exc.status_code, retryable=exc.retryable
            )
            return _desktop_error(helpers, error)

    @router.post("/api/v1/research/desktop/operations/prepare")
    def prepare(request: Request, payload: DesktopPrepareRequest):
        return _desktop_response(
            helpers,
            lambda service, owner: service.submit_prepare(
                owner,
                request_id=payload.requestId,
                generation=payload.generation,
                request_fingerprint=payload.requestFingerprint,
                paper_id=payload.paperId,
                source_url=payload.sourceUrl,
                source_type=payload.sourceType,
            ),
            services=services,
            request=request,
        )

    @router.post("/api/v1/research/desktop/operations/analyze")
    def analyze(request: Request, payload: DesktopAnalyzeRequest):
        return _desktop_response(
            helpers,
            lambda service, owner: service.submit_analysis(
                owner,
                request_id=payload.requestId,
                generation=payload.generation,
                request_fingerprint=payload.requestFingerprint,
                paper_id=payload.paperId,
                source_revision=payload.sourceRevision,
                source_fingerprint=payload.sourceFingerprint,
                source_url=payload.sourceUrl,
                pdf_url=payload.pdfUrl,
                objective=payload.objective,
                constraints=payload.constraints,
                personal_memory=(
                    payload.personalMemory.model_dump()
                    if payload.personalMemory is not None
                    else None
                ),
            ),
            services=services,
            request=request,
        )

    @router.post("/api/v1/research/desktop/operations/ask")
    def ask(request: Request, payload: DesktopAskRequest):
        return _desktop_response(
            helpers,
            lambda service, owner: service.submit_ask(
                owner,
                request_id=payload.requestId,
                generation=payload.generation,
                request_fingerprint=payload.requestFingerprint,
                run_id=payload.runId,
                paper_id=payload.paperId,
                source_revision=payload.sourceRevision,
                source_fingerprint=payload.sourceFingerprint,
                question=payload.question,
                selection=payload.selection,
                locale=payload.locale,
                personal_memory=(
                    payload.personalMemory.model_dump()
                    if payload.personalMemory is not None
                    else None
                ),
            ),
            services=services,
            request=request,
        )

    @router.post("/api/v1/research/desktop/operations/import")
    async def import_pdf(
        request: Request,
        x_newsroom_request_id: str = Header(alias="X-Newsroom-Request-Id"),
        x_newsroom_generation: str = Header(alias="X-Newsroom-Generation"),
        x_newsroom_request_fingerprint: str = Header(alias="X-Newsroom-Request-Fingerprint"),
        x_newsroom_source_revision: str = Header(alias="X-Newsroom-Source-Revision"),
        x_newsroom_source_fingerprint: str = Header(alias="X-Newsroom-Source-Fingerprint"),
        x_filename: str | None = Header(default=None, alias="X-Filename"),
    ):
        try:
            headers = DesktopImportHeaders(
                requestId=x_newsroom_request_id,
                generation=x_newsroom_generation,
                requestFingerprint=x_newsroom_request_fingerprint,
                sourceRevision=x_newsroom_source_revision,
                sourceFingerprint=x_newsroom_source_fingerprint,
                filename=x_filename,
            )
            content = bytearray()
            async for chunk in request.stream():
                if len(content) + len(chunk) > MAX_IMPORT_BYTES:
                    raise ResearchDesktopServiceError(
                        "pdf_too_large",
                        "PDF exceeds the 50 MB limit",
                        status_code=413,
                        user_action_required=True,
                    )
                content.extend(chunk)
            service = _service(services)
            owner = _owner(request)
            return helpers.success(
                service.submit_import(
                    owner,
                    request_id=headers.requestId,
                    generation=headers.generation,
                    request_fingerprint=headers.requestFingerprint,
                    content=bytes(content),
                    filename=headers.filename,
                    source_revision=headers.sourceRevision,
                    source_fingerprint=headers.sourceFingerprint,
                )
            )
        except ResearchDesktopServiceError as exc:
            return _desktop_error(helpers, exc)

    @router.get("/api/v1/research/desktop/operations/{request_id}")
    def operation(request: Request, request_id: str, generation: str = Query(pattern=r"^gen_[0-9a-f]{32}$")):
        return _desktop_response(
            helpers,
            lambda service, owner: service.get_operation(owner, request_id=request_id, generation=generation),
            services=services,
            request=request,
        )

    @router.get("/api/v1/research/desktop/reports")
    def reports(request: Request):
        return _desktop_response(
            helpers,
            lambda service, owner: service.list_saved_reports(owner),
            services=services,
            request=request,
        )

    @router.post("/api/v1/research/desktop/reports")
    def save_report(request: Request, payload: DesktopSaveReportRequest):
        return _desktop_response(
            helpers,
            lambda service, owner: service.save_report(
                owner,
                report_id=payload.reportId,
                run_id=payload.runId,
                source_revision=payload.sourceRevision,
                title=payload.title,
            ),
            services=services,
            request=request,
        )

    @router.get("/api/v1/research/desktop/reports/{report_id}")
    def saved_report(request: Request, report_id: str):
        return _desktop_response(
            helpers,
            lambda service, owner: service.get_saved_report(owner, report_id=report_id),
            services=services,
            request=request,
        )

    @router.get("/api/v1/research/desktop/reports/runs/{run_id}")
    def report(request: Request, run_id: str, sourceRevision: str = Query(min_length=1, max_length=512)):
        return _desktop_response(
            helpers,
            lambda service, owner: service.get_report(owner, run_id=run_id, source_revision=sourceRevision),
            services=services,
            request=request,
        )

    @router.get("/api/v1/research/desktop/reports/{report_id}/export")
    def export_saved_report(
        request: Request,
        report_id: str,
        format: Literal["markdown", "json"] = Query(),
    ):
        try:
            service = _service(services)
            owner = _owner(request)
            saved = service.get_saved_report(owner, report_id=report_id)
            report_projection = saved.get("report") if isinstance(saved.get("report"), Mapping) else {}
            reference = saved.get("reference") if isinstance(saved.get("reference"), Mapping) else {}
            eligibility = report_projection.get("eligibility") if isinstance(report_projection.get("eligibility"), Mapping) else {}
            if eligibility.get("acceptedExport") is not True:
                raise ResearchDesktopServiceError(
                    "report_not_accepted",
                    "Saved report is not eligible for accepted export",
                    status_code=422,
                    user_action_required=True,
                )
            export = service.export_report(
                owner,
                run_id=str(reference["runId"]),
                source_revision=str(reference["sourceRevision"]),
                format=format,
            )
            return _download(export)
        except ResearchDesktopServiceError as exc:
            return _desktop_error(helpers, exc)

    @router.get("/api/v1/research/desktop/reports/runs/{run_id}/export")
    def export_report(
        request: Request,
        run_id: str,
        sourceRevision: str = Query(min_length=1, max_length=512),
        format: Literal["markdown", "json"] = Query(),
    ):
        try:
            export = _service(services).export_report(
                _owner(request),
                run_id=run_id,
                source_revision=sourceRevision,
                format=format,
            )
            return _download(export)
        except ResearchDesktopServiceError as exc:
            return _desktop_error(helpers, exc)

    @router.post("/api/v1/research/desktop/reports/drafts/export")
    def export_draft(request: Request, payload: DesktopDraftExportRequest):
        try:
            export = _service(services).export_draft(
                _owner(request),
                title=payload.title,
                question=payload.question,
                notes=payload.notes,
                materials=[item.model_dump() for item in payload.materials],
                format=payload.format,
            )
            return _download(export)
        except ResearchDesktopServiceError as exc:
            return _desktop_error(helpers, exc)

    return router


def _desktop_response(
    helpers: ApiRouteHelpers,
    call: Any,
    *,
    services: ApiServices,
    request: Request,
):
    try:
        return helpers.success(call(_service(services), _owner(request)))
    except ResearchDesktopServiceError as exc:
        return _desktop_error(helpers, exc)


def _service(services: ApiServices) -> ResearchDesktopApplicationService:
    factory = services.research_desktop_service_factory
    if factory is None:
        raise ResearchDesktopServiceError(
            "research_desktop_unavailable",
            "Research desktop application service is unavailable",
            status_code=503,
        )
    return factory()


def _owner(request: Request) -> ResearchDesktopOwner:
    actor = getattr(request.state, "actor_context", None)
    if not isinstance(actor, ActorContext) or actor.actor_type != "user":
        raise ResearchDesktopServiceError("auth_required", "Valid account session required", status_code=401)
    metadata = actor.metadata if isinstance(actor.metadata, dict) else {}
    user_id = str(metadata.get("user_id") or actor.actor_id).strip()
    if not user_id:
        raise ResearchDesktopServiceError("auth_required", "Valid account session required", status_code=401)
    return ResearchDesktopOwner(
        user_id=user_id,
        tenant_id=_optional_actor_value(metadata.get("tenant_id")),
        memory_namespace=_optional_actor_value(metadata.get("memory_namespace")),
    )


def _optional_actor_value(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _desktop_error(helpers: ApiRouteHelpers, exc: ResearchDesktopServiceError):
    return helpers.error(
        status_code=exc.status_code,
        code=exc.code,
        message=str(exc),
        details=exc.details or None,
        retryable=exc.retryable,
        user_action_required=exc.user_action_required,
    )


def _download(export: dict[str, Any]) -> Response:
    filename = str(export["filename"])
    encoded = quote(filename, safe="")
    return Response(
        content=str(export["content"]).encode("utf-8"),
        media_type=str(export["contentType"]),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


__all__ = [
    "DesktopAnalyzeRequest",
    "DesktopAskRequest",
    "DesktopDraftExportRequest",
    "DesktopSaveReportRequest",
    "create_router",
]
