from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.research.application.personal_memory import PersonalMemoryService
from framework.events import (
    EventTelemetry,
    TelemetryInstrumentationScope,
    TelemetryResource,
    TelemetrySpanLink,
    TracePropagationError,
    W3CSpanContext,
    W3CTracePropagator,
    default_event_telemetry,
    trace_context_scope,
)
from framework.events.runtime.projection import RuntimeOperatorStatusService
from framework.execution_environment.composition import RuntimeExecutionComposition
from framework.tool.governance.redaction import redact_sensitive_values
from framework.shared.public_errors import project_public_error
from interfaces.models import (
    ActorContext,
    ArtifactRef,
    RunResponse,
    actor_context_from_headers,
)
from interfaces.models.actor import (
    RESEARCH_ARTIFACT_READ_PERMISSION,
    RESEARCH_CATALOG_READ_PERMISSION,
    RESEARCH_PAPER_INGEST_PERMISSION,
    RESEARCH_PAPER_PARSE_PERMISSION,
    RESEARCH_PAPER_REFRESH_PERMISSION,
)
from interfaces.models.contracts import RunStatus
from interfaces.api.deps import ApiRouteHelpers, build_api_services
from interfaces.api.rate_limit import Clock, InMemoryRateLimiter
from interfaces.api.warmup import BackgroundWarmup
from interfaces.api.openapi import configure_openapi_contract
from interfaces.api.routers import include_routers, personal_memory
from interfaces.api.responses import (
    REQUEST_ID_HEADER,
    current_request_id,
    error,
    model_to_dict,
    new_request_id,
    request_id_from_header,
    reset_request_id,
    set_request_id,
    success,
)
from interfaces.api.errors import http_error_code
from interfaces.composition.personal_memory import (
    PersonalMemoryRuntimeResolver,
    build_personal_memory_runtime,
)
from interfaces.composition.research import (
    build_default_harness_wait_service,
    build_research_application_service,
    default_source_runtime_provider,
)
from interfaces.composition.runtime_execution import (
    build_process_execution_composition,
)
from interfaces.events import AuditEmitter, audit_emitter_from_env
from framework.shared.env import load_root_env
from interfaces.services.auth_service import AuthApplicationService
from interfaces.services.diagnose_service import DiagnosticApplicationService
from interfaces.services.run_report_projection import project_run_report_for_interface
from interfaces.services.entity_service import EntityTrackingApplicationService
from interfaces.services.event_operator_factory import event_operator_service_from_actor
from interfaces.services.event_operator_service import EventOperatorApplicationService
from interfaces.services.harness_graph_service import HarnessGraphApplicationService
from interfaces.services.harness_wait_service import HarnessWaitApplicationService
from interfaces.services.memory_service import MemoryApplicationService
from interfaces.services.mcp_service import MCPApplicationService
from interfaces.services.project_service import ProjectApplicationService
from interfaces.services.personal_memory_service import PersonalMemoryApplicationService
from interfaces.services.research_service import ResearchApplicationService
from interfaces.services.research_desktop_service import ResearchDesktopApplicationService
from interfaces.services.research_desktop_control import ResearchDesktopControlError
from interfaces.services.research_desktop_auth import (
    ResearchDesktopAuthError,
    authenticate_desktop_actor,
    desktop_auth_path,
    desktop_legacy_operation_path,
    desktop_operator_path_forbidden,
    desktop_path_requires_session,
    local_desktop_profile_enabled,
    require_desktop_origin,
)
from interfaces.services.research_history_service import ResearchHistoryService
from interfaces.services.guided_research_service import GuidedResearchService
from interfaces.composition.guided_research import build_guided_research_service
from interfaces.services.report_service import ReportApplicationService
from interfaces.services.run_inspection_service import GraphRunInspectionService
from interfaces.services.run_inspection_factory import (
    graph_run_inspection_service_from_env,
)
from interfaces.services.run_event_sse import run_events_sse_frames as _durable_run_events_sse_frames
from interfaces.services.run_operation_service import (
    GraphRunOperationApplicationService,
)
from interfaces.services.run_service import RunApplicationService
from interfaces.services.schedule_service import ScheduleApplicationService
from interfaces.services.source_service import SourceApplicationService
from interfaces.services.source_runtime import SourceRuntimeProvider
from interfaces.services.storage_service import StorageApplicationService
from interfaces.services.subscription_service import SubscriptionApplicationService
from interfaces.services.worker_service import WorkerApplicationService
from interfaces.services.artifact_service import ArtifactInspectionService


WorkerServiceFactory = Callable[[], WorkerApplicationService]
RunServiceFactory = Callable[[], RunApplicationService]
GraphRunOperationServiceFactory = Callable[[], GraphRunOperationApplicationService]
ReportServiceFactory = Callable[[], ReportApplicationService]
MemoryServiceFactory = Callable[[], MemoryApplicationService]
DiagnosticServiceFactory = Callable[[], DiagnosticApplicationService]
SourceServiceFactory = Callable[[], SourceApplicationService]
EntityServiceFactory = Callable[[], EntityTrackingApplicationService]
SubscriptionServiceFactory = Callable[[], SubscriptionApplicationService]
MCPServiceFactory = Callable[[], MCPApplicationService]
EventOperatorServiceFactory = Callable[[ActorContext], EventOperatorApplicationService]
HarnessGraphServiceFactory = Callable[[ActorContext], HarnessGraphApplicationService]
HarnessWaitServiceFactory = Callable[[ActorContext], HarnessWaitApplicationService]
GraphRunInspectionServiceFactory = Callable[[], GraphRunInspectionService]
ArtifactInspectionServiceFactory = Callable[[], ArtifactInspectionService]
StorageServiceFactory = Callable[[], StorageApplicationService]
ScheduleServiceFactory = Callable[[], ScheduleApplicationService]
AuthServiceFactory = Callable[[], AuthApplicationService]
ProjectServiceFactory = Callable[[], ProjectApplicationService]
ResearchServiceFactory = Callable[[], ResearchApplicationService]
ResearchDesktopServiceFactory = Callable[[], ResearchDesktopApplicationService]
PersonalMemoryServiceFactory = Callable[[], PersonalMemoryApplicationService]
PersonalMemoryCoreFactory = Callable[[], PersonalMemoryService]
ResearchHistoryServiceFactory = Callable[[], ResearchHistoryService]
GuidedResearchServiceFactory = Callable[[], GuidedResearchService]
RuntimeOperatorStatusServiceFactory = Callable[[], RuntimeOperatorStatusService]
AuditEmitterFactory = Callable[[], AuditEmitter | None]
ApiKeyRoles = Mapping[str, str | Sequence[str]]


def create_app(
    *,
    worker_service_factory: WorkerServiceFactory = WorkerApplicationService,
    run_service_factory: RunServiceFactory = RunApplicationService,
    graph_run_operation_service_factory: GraphRunOperationServiceFactory = GraphRunOperationApplicationService,
    report_service_factory: ReportServiceFactory = ReportApplicationService,
    memory_service_factory: MemoryServiceFactory = MemoryApplicationService,
    diagnostic_service_factory: DiagnosticServiceFactory = DiagnosticApplicationService,
    source_service_factory: SourceServiceFactory = SourceApplicationService,
    entity_service_factory: EntityServiceFactory = EntityTrackingApplicationService,
    subscription_service_factory: SubscriptionServiceFactory = SubscriptionApplicationService,
    mcp_service_factory: MCPServiceFactory = MCPApplicationService,
    event_operator_service_factory: EventOperatorServiceFactory = event_operator_service_from_actor,
    graph_run_inspection_service_factory: GraphRunInspectionServiceFactory = graph_run_inspection_service_from_env,
    artifact_service_factory: ArtifactInspectionServiceFactory = ArtifactInspectionService,
    storage_service_factory: StorageServiceFactory = StorageApplicationService,
    schedule_service_factory: ScheduleServiceFactory = ScheduleApplicationService,
    auth_service_factory: AuthServiceFactory = AuthApplicationService,
    project_service_factory: ProjectServiceFactory = ProjectApplicationService,
    research_service_factory: ResearchServiceFactory = build_research_application_service,
    research_history_service_factory: ResearchHistoryServiceFactory = ResearchHistoryService,
    research_desktop_service_factory: ResearchDesktopServiceFactory | None = None,
    personal_memory_service_factory: PersonalMemoryServiceFactory | None = None,
    personal_memory_core_factory: PersonalMemoryCoreFactory | None = None,
    guided_research_service_factory: GuidedResearchServiceFactory = build_guided_research_service,
    harness_graph_service_factory: HarnessGraphServiceFactory | None = None,
    harness_wait_service_factory: HarnessWaitServiceFactory | None = None,
    runtime_operator_status_service_factory: RuntimeOperatorStatusServiceFactory | None = None,
    runtime_execution_composition: RuntimeExecutionComposition | None = None,
    audit_emitter_factory: AuditEmitterFactory | None = audit_emitter_from_env,
    api_token: str | None = None,
    api_keys: ApiKeyRoles | None = None,
    api_rate_limit_per_minute: int | None = None,
    rate_limit_clock: Clock | None = None,
    trace_propagator: W3CTracePropagator | None = None,
    telemetry: EventTelemetry | None = None,
) -> FastAPI:
    api = FastAPI(title="Agora Hub API", version="0.1.0")
    if runtime_execution_composition is None:
        runtime_execution_composition = build_process_execution_composition()
    if not isinstance(runtime_execution_composition, RuntimeExecutionComposition):
        raise TypeError("runtime_execution_composition must be RuntimeExecutionComposition")
    runtime_execution_composition.verify_integrity()
    api.state.runtime_execution_composition = runtime_execution_composition
    resolved_api_keys = _build_api_key_registry(api_token=api_token, api_keys=api_keys)
    rate_limiter = _build_rate_limiter(api_rate_limit_per_minute, clock=rate_limit_clock)
    audit_emitter = audit_emitter_factory() if audit_emitter_factory else None
    actual_trace_propagator = trace_propagator or W3CTracePropagator()
    actual_telemetry = telemetry or default_event_telemetry(
        resource=TelemetryResource(service_name="newsroom-api"),
        scope=TelemetryInstrumentationScope(name="interfaces.api", version="1"),
    )

    default_personal_memory_runtime: PersonalMemoryRuntimeResolver | None = None
    resolved_personal_memory_service_factory = personal_memory_service_factory
    if resolved_personal_memory_service_factory is None:
        runtime_resolver = PersonalMemoryRuntimeResolver(
            build_personal_memory_runtime
        )
        default_personal_memory_runtime = runtime_resolver

        def resolved_personal_memory_service_factory() -> PersonalMemoryApplicationService:
            return runtime_resolver.get().service

        @api.on_event("shutdown")
        def _close_personal_memory_runtime() -> None:
            runtime_resolver.close()

    resolved_research_desktop_service_factory = research_desktop_service_factory
    if resolved_research_desktop_service_factory is None:
        if personal_memory_core_factory is not None:
            personal_memory_core_lock = Lock()
            resolved_personal_memory_core: PersonalMemoryService | None = None

            def resolve_personal_memory_core() -> PersonalMemoryService:
                nonlocal resolved_personal_memory_core
                with personal_memory_core_lock:
                    if resolved_personal_memory_core is None:
                        resolved_personal_memory_core = personal_memory_core_factory()
                    return resolved_personal_memory_core

            def resolved_research_desktop_service_factory() -> ResearchDesktopApplicationService:
                return ResearchDesktopApplicationService(
                    research=research_service_factory(),
                    personal_memory=resolve_personal_memory_core(),
                )

        elif default_personal_memory_runtime is not None:

            def resolved_research_desktop_service_factory() -> ResearchDesktopApplicationService:
                return ResearchDesktopApplicationService(
                    research=research_service_factory(),
                    personal_memory=default_personal_memory_runtime.get().application,
                )

        else:

            def resolved_research_desktop_service_factory() -> ResearchDesktopApplicationService:
                return ResearchDesktopApplicationService(
                    research=research_service_factory(),
                )

    desktop_auth_rate_limiter = (
        InMemoryRateLimiter(
            limit=max(1, min(60, int(os.environ.get("NEWSROOM_DESKTOP_AUTH_RATE_LIMIT_PER_MINUTE", "10")))),
            window_seconds=60,
            clock=rate_limit_clock,
        )
        if local_desktop_profile_enabled()
        else None
    )


    if rate_limiter is not None:

        @api.middleware("http")
        async def rate_limit_middleware(request: Request, call_next):
            if _requires_api_auth(request.url.path):
                decision = rate_limiter.check(_client_rate_limit_key(request))
                if not decision.allowed:
                    return _error(
                        status_code=429,
                        code="rate_limited",
                        message="API rate limit exceeded",
                        details={
                            "limit": decision.limit,
                            "window_seconds": rate_limiter.window_seconds,
                            "remaining": decision.remaining,
                        },
                        retryable=True,
                        headers={"Retry-After": str(decision.retry_after_seconds)},
                    )
            return await call_next(request)

    if resolved_api_keys:

        @api.middleware("http")
        async def bearer_token_rbac(request: Request, call_next):
            if not _requires_api_auth(request.url.path):
                return await call_next(request)
            if local_desktop_profile_enabled() and (
                desktop_path_requires_session(request.url.path)
                or desktop_auth_path(request.url.path)
            ):
                return await call_next(request)
            actor = _authorized_api_actor(request, resolved_api_keys)
            if actor is None:
                return _error(
                    status_code=401,
                    code="unauthorized",
                    message="valid bearer token required",
                    user_action_required=True,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.actor_context = actor
            required_permission = _required_api_permission(request.method, request.url.path)
            if required_permission and not actor.has_permission(required_permission):
                return _error(
                    status_code=403,
                    code="forbidden",
                    message=f"missing required permission: {required_permission}",
                    details={
                        "required_permission": required_permission,
                        "roles": actor.roles,
                    },
                    user_action_required=True,
                )
            return await call_next(request)

    if local_desktop_profile_enabled():

        @api.middleware("http")
        async def local_desktop_session_boundary(request: Request, call_next):
            path = request.url.path
            try:
                if desktop_operator_path_forbidden(path):
                    return _error(
                        status_code=403,
                        code="desktop_operator_api_disabled",
                        message="This operator API is unavailable in local desktop profile",
                        user_action_required=True,
                    )
                if desktop_path_requires_session(path):
                    actor = authenticate_desktop_actor(
                        request,
                        auth_service_factory(),
                    )
                    request.state.actor_context = actor
                    if desktop_legacy_operation_path(path, request.method):
                        return _error(
                            status_code=409,
                            code="desktop_request_contract_required",
                            message="Use the durable Research desktop operation endpoint",
                            user_action_required=True,
                        )
                elif desktop_auth_path(path):
                    require_desktop_origin(request)
                    if desktop_auth_rate_limiter is not None and request.method.upper() == "POST":
                        decision = desktop_auth_rate_limiter.check(_client_rate_limit_key(request))
                        if not decision.allowed:
                            return _error(
                                status_code=429,
                                code="auth_rate_limited",
                                message="Authentication rate limit exceeded",
                                details={
                                    "limit": decision.limit,
                                    "window_seconds": desktop_auth_rate_limiter.window_seconds,
                                    "remaining": decision.remaining,
                                },
                                retryable=True,
                                user_action_required=True,
                                headers={"Retry-After": str(decision.retry_after_seconds)},
                            )
            except ResearchDesktopAuthError as exc:
                return _error(
                    status_code=exc.status_code,
                    code=exc.code,
                    message=str(exc),
                    user_action_required=True,
                )
            lease_id = None
            desktop_service = None
            try:
                is_desktop_writer = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and (
                    desktop_path_requires_session(path) or desktop_auth_path(path)
                )
                if is_desktop_writer:
                    desktop_service = resolved_research_desktop_service_factory()
                    lease_id = desktop_service.begin_business_operation(
                        operation=f"{request.method.upper()} {path}"
                    )
                return await call_next(request)
            except ResearchDesktopControlError as exc:
                return _error(
                    status_code=exc.status_code,
                    code=exc.code,
                    message=str(exc),
                    retryable=exc.retryable,
                    user_action_required=True,
                )
            finally:
                if lease_id is not None and desktop_service is not None:
                    desktop_service.end_business_operation(lease_id)

    @api.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = getattr(request.state, "request_id", None)
        if not isinstance(request_id, str) or not request_id:
            request_id = _request_id_from_header(
                request.headers.get(REQUEST_ID_HEADER)
            ) or _new_request_id()
        request.state.request_id = request_id
        context_token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(context_token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @api.middleware("http")
    async def trace_context_middleware(request: Request, call_next):
        try:
            extracted = actual_trace_propagator.extract_span(
                _http_trace_carrier(request.headers)
            )
            local_context = extracted.child().context
            propagation_result = (
                "accepted" if extracted.accepted_remote else "restarted"
            )
        except TracePropagationError:
            extracted = None
            local_context = W3CSpanContext.root()
            propagation_result = "invalid"
        request.state.trace_context = local_context
        request.state.trace_diagnostics = (
            extracted.diagnostics if extracted is not None else ("invalid_carrier",)
        )
        link = TelemetrySpanLink.from_context(
            extracted.remote_context if extracted is not None else None,
            relationship="http_request",
        )
        with trace_context_scope(local_context), actual_telemetry.start_span(
            "newsroom.http.server",
            attributes={
                "newsroom.component": "api",
                "newsroom.operation": "request",
                "newsroom.transport": "http",
                "newsroom.propagation.result": propagation_result,
            },
            links=(link,),
        ):
            response = await call_next(request)
        injected = actual_trace_propagator.inject(local_context)
        if "traceparent" in injected:
            response.headers["traceparent"] = injected["traceparent"]
        actual_telemetry.add_counter(
            "trace_propagation_total",
            labels={"boundary": "http", "result": propagation_result},
        )
        return response

    @api.middleware("http")
    async def audit_middleware(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            _emit_api_audit(
                audit_emitter,
                request,
                request_id=_request_state_id(request),
                status_code=500,
                result="failed",
            )
            raise
        request_id = response.headers.get(REQUEST_ID_HEADER) or _request_state_id(request)
        _emit_api_audit(
            audit_emitter,
            request,
            request_id=request_id,
            status_code=response.status_code,
            result=_audit_result(response.status_code),
        )
        return response

    @api.middleware("http")
    async def personal_memory_private_response(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/api/v1/research/desktop/memory" or path.startswith(
            "/api/v1/research/desktop/memory/"
        ):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return _error(
            status_code=422,
            code="invalid_request",
            message="request validation failed",
            details=_validation_error_details(exc.errors()),
            user_action_required=True,
        )

    @api.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        return _error(
            status_code=exc.status_code,
            code=_http_error_code(exc.status_code),
            message=str(exc.detail or "HTTP error"),
            headers=exc.headers,
        )

    @api.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = _request_state_id(request)
        context_token = set_request_id(request_id)
        try:
            projected = project_public_error(
                exc,
                context="http",
                operation=f"{request.method} {request.url.path}",
            )
            return _error(
                status_code=500,
                code="internal_error",
                message=projected.error_message,
                details={"error_id": projected.error_id},
                headers={REQUEST_ID_HEADER: request_id},
            )
        finally:
            reset_request_id(context_token)

    source_runtime_provider: SourceRuntimeProvider | None = None
    if source_service_factory is SourceApplicationService:
        source_runtime_provider = default_source_runtime_provider()
        source_service_factory = source_runtime_provider.source_service_factory
    if worker_service_factory is WorkerApplicationService:

        def default_worker_service_factory() -> WorkerApplicationService:
            kwargs: dict[str, Any] = {
                "runtime_execution_composition": runtime_execution_composition,
            }
            if source_runtime_provider is not None:
                kwargs["source_service_factory"] = (
                    source_runtime_provider.source_service_factory
                )
            elif source_service_factory is not SourceApplicationService:
                kwargs["source_service_factory"] = source_service_factory
            return WorkerApplicationService(**kwargs)

        worker_service_factory = default_worker_service_factory

    if diagnostic_service_factory is DiagnosticApplicationService:

        def default_diagnostic_service_factory() -> DiagnosticApplicationService:
            return DiagnosticApplicationService(
                runtime_execution_composition=runtime_execution_composition,
            )

        diagnostic_service_factory = default_diagnostic_service_factory

    resolved_mcp_service_factory = _mcp_service_factory(
        mcp_service_factory=mcp_service_factory,
        source_runtime_provider=source_runtime_provider,
        event_operator_service_factory=event_operator_service_factory,
        worker_service_factory=worker_service_factory,
        run_service_factory=run_service_factory,
        graph_run_operation_service_factory=graph_run_operation_service_factory,
        report_service_factory=report_service_factory,
        memory_service_factory=memory_service_factory,
        diagnostic_service_factory=diagnostic_service_factory,
        source_service_factory=source_service_factory,
        entity_service_factory=entity_service_factory,
        subscription_service_factory=subscription_service_factory,
        graph_run_inspection_service_factory=graph_run_inspection_service_factory,
        artifact_service_factory=artifact_service_factory,
        storage_service_factory=storage_service_factory,
        harness_wait_service_factory=harness_wait_service_factory,
        research_service_factory=research_service_factory,
        runtime_execution_composition=runtime_execution_composition,
    )
    services = build_api_services(
        worker_service_factory=worker_service_factory,
        run_service_factory=run_service_factory,
        graph_run_operation_service_factory=graph_run_operation_service_factory,
        report_service_factory=report_service_factory,
        memory_service_factory=memory_service_factory,
        diagnostic_service_factory=diagnostic_service_factory,
        source_service_factory=source_service_factory,
        entity_service_factory=entity_service_factory,
        subscription_service_factory=subscription_service_factory,
        mcp_service_factory=resolved_mcp_service_factory,
        event_operator_service_factory=event_operator_service_factory,
        graph_run_inspection_service_factory=graph_run_inspection_service_factory,
        artifact_service_factory=artifact_service_factory,
        storage_service_factory=storage_service_factory,
        schedule_service_factory=schedule_service_factory,
        auth_service_factory=auth_service_factory,
        project_service_factory=project_service_factory,
        research_service_factory=research_service_factory,
        research_history_service_factory=research_history_service_factory,
        guided_research_service_factory=guided_research_service_factory,
        research_desktop_service_factory=resolved_research_desktop_service_factory,
        personal_memory_service_factory=resolved_personal_memory_service_factory,
        harness_graph_service_factory=harness_graph_service_factory,
        harness_wait_service_factory=harness_wait_service_factory,
        runtime_operator_status_service_factory=runtime_operator_status_service_factory,
        runtime_execution_composition=runtime_execution_composition,
    )
    helpers = ApiRouteHelpers(
        success=_success,
        error=_error,
        model_to_dict=_model_to_dict,
        run_result_response=_run_result_response,
        provided_values=_provided_values,
        artifact_lookup_ids=_artifact_lookup_ids,
        run_progress_sse_frames=_run_progress_sse_frames,
        run_events_sse_frames=_run_events_sse_frames,
    )
    include_routers(api, services=services, helpers=helpers)
    # Keep router registration in this composition root so shared router registry files
    # remain untouched while preserving the concrete-route introspection contract.
    memory_router = personal_memory.create_router(services, helpers)
    api.router.routes.extend(memory_router.routes)
    mark_routes_changed = getattr(api.router, "_mark_routes_changed", None)
    if callable(mark_routes_changed):
        mark_routes_changed()
    configure_openapi_contract(api)
    # The global operation-id helper currently collapses every Research PUT to
    # research_put; pin this second PUT endpoint to a stable unique contract id.
    for route in api.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == "/api/v1/research/desktop/memory/policy"
            and "PUT" in (route.methods or set())
        ):
            route.operation_id = "research_update_desktop_memory_policy"
    api.openapi_schema = None

    # Warm the cross-encoder weights so the first /rag-ask is fast (~18s cold load)
    # without holding back the listening socket for it. Opt-out via
    # NEWS_PRELOAD_RERANKER=0 (tests/CI). Failure is non-fatal.
    reranker_warmup = BackgroundWarmup("reranker", _preload_reranker)
    api.state.reranker_warmup = reranker_warmup

    @api.on_event("startup")
    def _start_reranker_warmup() -> None:
        if os.getenv("NEWS_PRELOAD_RERANKER", "1") in ("1", "true", "yes"):
            reranker_warmup.start()

    return api


def _preload_reranker() -> None:
    from interfaces.services.paper_rag_factory import preload_reranker

    preload_reranker()


def _run_result_response(result) -> dict[str, Any]:
    payload = result.to_dict()
    response = _model_to_dict(_run_response_from_payload(payload))
    payload.update(response)
    payload["interface"] = response
    return payload


def _mcp_service_factory(
    *,
    mcp_service_factory: MCPServiceFactory,
    source_runtime_provider: SourceRuntimeProvider | None,
    event_operator_service_factory: EventOperatorServiceFactory,
    worker_service_factory: WorkerServiceFactory,
    run_service_factory: RunServiceFactory,
    graph_run_operation_service_factory: GraphRunOperationServiceFactory,
    report_service_factory: ReportServiceFactory,
    memory_service_factory: MemoryServiceFactory,
    diagnostic_service_factory: DiagnosticServiceFactory,
    source_service_factory: SourceServiceFactory,
    entity_service_factory: EntityServiceFactory,
    subscription_service_factory: SubscriptionServiceFactory,
    graph_run_inspection_service_factory: GraphRunInspectionServiceFactory,
    artifact_service_factory: ArtifactInspectionServiceFactory,
    storage_service_factory: StorageServiceFactory,
    harness_wait_service_factory: HarnessWaitServiceFactory | None,
    research_service_factory: ResearchServiceFactory,
    runtime_execution_composition: RuntimeExecutionComposition,
) -> MCPServiceFactory:
    if mcp_service_factory is not MCPApplicationService:
        return mcp_service_factory

    def factory() -> MCPApplicationService:
        return MCPApplicationService(
            source_runtime_provider=source_runtime_provider,
            event_operator_service_factory=event_operator_service_factory,
            worker_service_factory=worker_service_factory,
            run_service_factory=run_service_factory,
            graph_run_operation_service_factory=graph_run_operation_service_factory,
            report_service_factory=report_service_factory,
            memory_service_factory=memory_service_factory,
            diagnostic_service_factory=diagnostic_service_factory,
            source_service_factory=(
                None if source_runtime_provider is not None else source_service_factory
            ),
            entity_service_factory=entity_service_factory,
            subscription_service_factory=subscription_service_factory,
            graph_run_inspection_service_factory=graph_run_inspection_service_factory,
            artifact_service_factory=artifact_service_factory,
            storage_service_factory=storage_service_factory,
            harness_wait_service_factory=harness_wait_service_factory,
            research_service_factory=research_service_factory,
            runtime_execution_composition=runtime_execution_composition,
        )

    return factory


def _run_response_from_payload(payload: dict[str, Any]) -> RunResponse:
    run_status = str(payload.get("status") or "")
    report_projection = project_run_report_for_interface(payload)
    return RunResponse(
        run_id=payload.get("run_id"),
        graph_id=payload.get("graph_id"),
        graph_version=payload.get("graph_version"),
        graph_ref=payload.get("graph_ref"),
        graph_checksum=payload.get("graph_checksum"),
        status=_interface_status(run_status),
        run_status=run_status or None,
        report_status=report_projection.report_status,
        report_id=report_projection.report_id,
        manifest_ref=_manifest_ref_from_payload(payload),
        artifact_refs=_artifact_refs_from_payload(payload),
        diagnostics=_diagnostics_from_payload(payload),
        message=payload.get("error", {}).get("message") if isinstance(payload.get("error"), dict) else None,
    )


def _interface_status(run_status: str) -> RunStatus:
    normalized = run_status.strip().lower()
    if normalized == "succeeded":
        return "succeeded"
    if normalized == "failed":
        return "failed"
    if normalized == "blocked":
        return "blocked"
    if normalized == "cancelled":
        return "cancelled"
    if normalized in {"running", "created", "pending"}:
        return "running"
    return "accepted"


def _manifest_ref_from_payload(payload: dict[str, Any]) -> ArtifactRef | None:
    manifest_path = payload.get("manifest_path")
    if not manifest_path:
        return None
    return ArtifactRef(
        artifact_id="manifest",
        run_id=_optional_str(payload.get("run_id")),
        artifact_type="manifest",
        path=str(manifest_path),
        content_type="application/json",
        redacted=True,
    )


def _artifact_refs_from_payload(payload: dict[str, Any]) -> list[ArtifactRef]:
    manifest_ref = _manifest_ref_from_payload(payload)
    manifest = _manifest_from_payload(payload)
    artifacts = manifest.get("artifacts") if manifest is not None else None
    refs: list[ArtifactRef] = []
    if manifest_ref is not None:
        refs.append(manifest_ref)
    if not isinstance(artifacts, dict):
        return refs
    run_id = _optional_str(payload.get("run_id"))
    for artifact_id, path in sorted(artifacts.items()):
        if not path:
            continue
        if manifest_ref is not None and str(artifact_id) == manifest_ref.artifact_id:
            continue
        refs.append(
            ArtifactRef(
                artifact_id=str(artifact_id),
                run_id=run_id,
                artifact_type=str(artifact_id),
                path=str(path),
                content_type=_artifact_content_type(str(path)),
                redacted=True,
            )
        )
    return refs


def _manifest_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    manifest = payload.get("manifest")
    if isinstance(manifest, dict):
        return manifest
    manifest_path = payload.get("manifest_path")
    if not manifest_path:
        return None
    try:
        loaded = json.loads(Path(str(manifest_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _artifact_content_type(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".jsonl"):
        return "application/x-ndjson"
    if lowered.endswith(".md"):
        return "text/markdown"
    if lowered.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def _diagnostics_from_payload(payload: dict[str, Any]) -> list[str]:
    diagnostics = []
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        diagnostics.append(str(error["message"]))
    manifest = payload.get("manifest")
    if isinstance(manifest, dict):
        route = manifest.get("quality_route")
        if route in {"blocked", "human_review"}:
            diagnostics.append(f"quality_route={route}")
    return diagnostics


def _artifact_lookup_ids(artifact_id: str, run_id: str | None) -> tuple[str, str]:
    artifact = artifact_id.strip()
    if not artifact:
        raise ValueError("artifact_id is required")
    if run_id:
        return run_id, artifact
    if ":" not in artifact:
        raise ValueError("run_id is required unless artifact_id uses run_id:artifact_key")
    resolved_run_id, artifact_key = artifact.split(":", 1)
    if not resolved_run_id or not artifact_key:
        raise ValueError("artifact_id must use run_id:artifact_key")
    return resolved_run_id, artifact_key


def _success(data: dict) -> dict:
    return success(data)


def _run_progress_sse_frames(payload: dict[str, Any]):
    run_id = str(payload.get("run_id") or "")
    durable_source = (
        payload.get("source") == "durable_store"
        and payload.get("availability") == "available"
    )
    for index, event in enumerate(payload.get("events") or []):
        event_payload = event if isinstance(event, dict) else {}
        sequence = event_payload.get("stream_sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            sequence = index
        event_id = event_payload.get("sse_resume_cursor") if durable_source else None
        yield _sse_frame(
            "run.progress",
            {
                "run_id": run_id,
                "sequence": sequence,
                "event": redact_sensitive_values(event_payload),
            },
            event_id=(event_id if isinstance(event_id, str) else None),
        )
    yield _sse_frame(
        "run.progress.done",
        {
            "run_id": run_id,
            "event_count": int(payload.get("event_count") or 0),
            "events_path": payload.get("events_path"),
            "availability": payload.get("availability"),
            "source": payload.get("source"),
            "high_watermark": payload.get("high_watermark"),
            "next_sequence_cursor": payload.get("next_sequence_cursor"),
            "projection_status": payload.get("projection_status"),
            "projection_checksum": payload.get("projection_checksum"),
            "projection_high_watermark": payload.get("projection_high_watermark"),
            "unavailable_reason_class": payload.get("unavailable_reason_class"),
            "sse_resume_cursor": payload.get("sse_resume_cursor"),
        },
    )


def _run_events_sse_frames(payload: dict[str, Any]):
    yield from _durable_run_events_sse_frames(payload)


def _sse_frame(
    event_name: str,
    data: dict[str, Any],
    *,
    event_id: str | None = None,
) -> str:
    id_line = "" if event_id is None else f"id: {event_id}\n"
    return (
        f"{id_line}event: {event_name}\n"
        f"data: {json.dumps(redact_sensitive_values(data), ensure_ascii=False, sort_keys=True)}\n\n"
    )


def _error(**kwargs):
    return error(**kwargs)


def _model_to_dict(model) -> dict:
    return model_to_dict(model)


def _provided_values(**values) -> dict:
    return {key: value for key, value in values.items() if value is not None}


def _optional_str(value) -> str | None:
    return str(value) if value is not None else None


def _request_id() -> str:
    return current_request_id()


def _new_request_id() -> str:
    return new_request_id()


def _request_id_from_header(value: str | None) -> str | None:
    return request_id_from_header(value)


def _request_state_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    if isinstance(value, str) and value:
        return value
    request_id = _request_id_from_header(request.headers.get(REQUEST_ID_HEADER)) or _request_id()
    request.state.request_id = request_id
    return request_id


def _http_trace_carrier(headers: Any) -> dict[str, str]:
    allowed = {"traceparent", "tracestate", "baggage"}
    carrier: dict[str, str] = {}
    raw_headers = getattr(headers, "raw", None)
    if raw_headers is not None:
        for raw_key, raw_value in raw_headers:
            key = bytes(raw_key).decode("latin-1").casefold()
            if key not in allowed:
                continue
            if key in carrier:
                raise TracePropagationError(f"duplicate_{key}")
            carrier[key] = bytes(raw_value).decode("latin-1")
        return carrier
    for key in allowed:
        value = headers.get(key)
        if value is not None:
            carrier[key] = str(value)
    return carrier


def _normalize_api_token(api_token: str | None) -> str | None:
    if api_token is None:
        return None
    token = api_token.strip()
    return token or None


def _api_token_from_env(env: Mapping[str, str] | None = None) -> str | None:
    values = env if env is not None else os.environ
    return _normalize_api_token(values.get("NEWSROOM_API_TOKEN")) or _normalize_api_token(values.get("NEWS_API_TOKEN"))


def _build_api_key_registry(
    *,
    api_token: str | None,
    api_keys: ApiKeyRoles | None,
    env: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    registry: dict[str, list[str]] = {}
    legacy_token = _normalize_api_token(api_token)
    if legacy_token:
        registry[legacy_token] = ["admin"]
    for token, roles in (api_keys or {}).items():
        normalized = _normalize_api_token(str(token))
        if normalized:
            registry[normalized] = _normalize_roles(roles)
    for token, roles in _api_keys_from_env(env=env).items():
        registry[token] = roles
    return registry


def _api_keys_from_env(*, env: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    values = env if env is not None else os.environ
    raw = (values.get("NEWS_API_KEYS") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _api_keys_from_text(raw)
    if not isinstance(parsed, dict):
        return {}
    registry: dict[str, list[str]] = {}
    for token, roles in parsed.items():
        normalized = _normalize_api_token(str(token))
        if normalized:
            registry[normalized] = _normalize_roles(roles)
    return registry


def _api_keys_from_text(raw: str) -> dict[str, list[str]]:
    registry: dict[str, list[str]] = {}
    for entry in raw.split(";"):
        text = entry.strip()
        if not text:
            continue
        separator = "=" if "=" in text else ":"
        token, found, roles = text.partition(separator)
        normalized = _normalize_api_token(token)
        if found and normalized:
            registry[normalized] = _normalize_roles(roles)
    return registry


def _normalize_roles(roles: Any) -> list[str]:
    if roles is None:
        return ["read-only"]
    if isinstance(roles, str):
        normalized = [item.strip() for item in roles.split(",") if item.strip()]
        return normalized or ["read-only"]
    if isinstance(roles, Sequence):
        normalized = [str(item).strip() for item in roles if str(item).strip()]
        return normalized or ["read-only"]
    role = str(roles).strip()
    return [role] if role else ["read-only"]


def _requires_api_auth(path: str) -> bool:
    return path.startswith("/api/")


def _authorized_api_actor(
    request: Request,
    api_keys: Mapping[str, list[str]],
) -> ActorContext | None:
    token = _bearer_token(request.headers.get("authorization"))
    if token is None:
        return None
    for expected_token, roles in api_keys.items():
        if hmac.compare_digest(token, expected_token):
            tenant_id = str(os.environ.get("NEWS_TENANT_ID") or "").strip()
            return ActorContext(
                actor_id=_api_key_actor_id(expected_token),
                actor_type="mcp_client" if "mcp_client" in roles else "service",
                roles=list(roles),
                request_id=_request_id(),
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                metadata={"tenant_id": tenant_id} if tenant_id else {},
            )
    return None


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    stripped = token.strip()
    return stripped or None


def _api_key_actor_id(token: str) -> str:
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"api-key:{fingerprint}"


def _required_api_permission(method: str, path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 3 or parts[0] != "api":
        return None
    resource = parts[2]
    method = method.upper()
    if resource == "admin":
        return "admin:diagnose"
    if resource == "mcp":
        return "read:reports"
    if resource == "events":
        return "events:read" if method == "GET" else "events:operate"
    if resource in {"runs", "graph-runs"}:
        if (
            method == "POST"
            and len(parts) == 7
            and parts[4] == "waits"
            and parts[6] == "approval"
        ):
            return "manage:approvals"
        return "read:reports" if method == "GET" else "write:runs"
    if resource == "reports":
        if method == "GET":
            return "read:reports"
        if path.endswith("/publish"):
            return "manage:approvals"
        return "read:reports"
    if resource == "search":
        return "read:reports"
    if resource == "memory":
        return "read:reports"
    if resource == "sources":
        return "read:reports"
    if resource == "projects":
        return "read:reports" if method == "GET" else "write:runs"
    if resource == "research":
        if method == "GET":
            if "/artifacts/" in path:
                return RESEARCH_ARTIFACT_READ_PERMISSION
            return RESEARCH_CATALOG_READ_PERMISSION
        if path.endswith("/papers/analyze"):
            # The pre-v1 analysis workflow is a run operation. Keep its
            # established permission contract separate from paper parsing.
            return "write:runs"
        if path.endswith("/catalog/refresh"):
            return RESEARCH_PAPER_REFRESH_PERMISSION
        if path.endswith("/papers/parse"):
            return RESEARCH_PAPER_PARSE_PERMISSION
        if path.endswith("/papers/ingest"):
            return RESEARCH_PAPER_INGEST_PERMISSION
        return RESEARCH_PAPER_PARSE_PERMISSION
    if resource in {"workers", "queues"}:
        return "read:reports"
    if resource == "schedules":
        return "read:reports" if method == "GET" else "manage:schedules"
    if resource == "approvals":
        return "read:reports" if method == "GET" else "manage:approvals"
    if resource == "entities":
        return "read:reports" if method == "GET" else "write:runs"
    if resource == "subscriptions":
        return "read:reports" if method == "GET" else "manage:schedules"
    if resource == "storage":
        return "admin:storage"
    if resource == "artifacts":
        return "read:reports"
    return None


def _build_rate_limiter(
    limit: int | None,
    *,
    clock: Clock | None = None,
) -> InMemoryRateLimiter | None:
    if limit is None:
        return None
    return InMemoryRateLimiter(limit=limit, window_seconds=60, clock=clock)


def _client_rate_limit_key(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


def _emit_api_audit(
    audit_emitter: AuditEmitter | None,
    request: Request,
    *,
    request_id: str,
    status_code: int,
    result: str,
) -> None:
    if audit_emitter is None:
        return
    path = request.url.path
    actor = getattr(request.state, "actor_context", None)
    if not isinstance(actor, ActorContext):
        actor = actor_context_from_headers(
            request.headers,
            request_id=request_id,
            ip_address=request.client.host if request.client else None,
        )
    permission = _required_api_permission(request.method, path)
    run_id = request.query_params.get("run_id") or request.query_params.get("runId")
    scope_ref = actor.metadata.get("scope_ref") if isinstance(actor.metadata, Mapping) else None
    if not scope_ref:
        scope_material = {
            key: actor.metadata.get(key)
            for key in ("tenant_id", "user_id", "memory_namespace")
            if isinstance(actor.metadata, Mapping) and actor.metadata.get(key)
        }
        if scope_material:
            scope_ref = hashlib.sha256(
                json.dumps(scope_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
    reason_code = "success" if status_code < 400 else _http_error_code(status_code)
    audit_emitter.emit(
        actor=actor,
        action=_api_audit_action(request.method, path),
        resource_type=_api_resource_type(path),
        resource_id=_api_resource_id(path),
        result=result,  # type: ignore[arg-type]
        permission=permission,
        scope_ref=str(scope_ref) if scope_ref else None,
        reason_code=reason_code,
        request_id=request_id,
        run_id=run_id,
        correlation_id=request_id,
        metadata={
            "method": request.method,
            "path": path,
            "status_code": status_code,
            "query": dict(request.query_params),
        },
    )


def _audit_result(status_code: int) -> str:
    if status_code in {401, 403, 429}:
        return "blocked"
    if status_code >= 400:
        return "failed"
    return "succeeded"


def _api_audit_action(method: str, path: str) -> str:
    prefix = "api_request"
    if (
        path.startswith("/api/v2/graph-runs/")
        and path.endswith("/approval")
        and method == "POST"
    ):
        return "graph_wait_approval_decision_submitted"
    if path.startswith("/api/v1/artifacts"):
        return "artifact_read"
    if path.startswith("/api/v1/reports") and method == "GET":
        return "report_downloaded"
    return f"{prefix}_{method.lower()}"


def _api_resource_type(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[0] == "api":
        return parts[2]
    if parts:
        return parts[0]
    return "api"


def _api_resource_id(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 4 and parts[0] == "api":
        return parts[3]
    return None


def _optional_positive_int_env(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _validation_error_details(errors: Sequence[Any]) -> dict[str, Any]:
    return {
        "errors": [
            {
                "loc": [str(part) for part in _mapping_or_empty(error).get("loc", [])],
                "message": str(_mapping_or_empty(error).get("msg") or "invalid value"),
                "type": str(_mapping_or_empty(error).get("type") or "validation_error"),
            }
            for error in errors
        ]
    }


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _http_error_code(status_code: int) -> str:
    return http_error_code(status_code)


load_root_env()

app = create_app(
    api_token=_api_token_from_env(),
    api_keys=_api_keys_from_env(),
    api_rate_limit_per_minute=_optional_positive_int_env("NEWS_API_RATE_LIMIT_PER_MINUTE"),
    harness_wait_service_factory=build_default_harness_wait_service,
)
