from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Request

from interfaces.composition.research import build_research_application_service
from interfaces.models import ActorContext, actor_context_from_headers
from interfaces.services.artifact_service import ArtifactInspectionService
from interfaces.services.auth_service import AuthApplicationService
from interfaces.services.diagnose_service import DiagnosticApplicationService
from interfaces.services.entity_service import EntityTrackingApplicationService
from interfaces.services.memory_service import MemoryApplicationService
from interfaces.services.mcp_service import MCPApplicationService
from interfaces.services.event_operator_factory import event_operator_service_from_actor
from interfaces.services.event_operator_service import EventOperatorApplicationService
from interfaces.services.harness_graph_service import HarnessGraphApplicationService
from interfaces.services.harness_wait_service import HarnessWaitApplicationService
from interfaces.services.project_service import ProjectApplicationService
from interfaces.services.research_service import ResearchApplicationService
from interfaces.services.research_history_service import ResearchHistoryService
from interfaces.services.report_service import ReportApplicationService
from interfaces.services.run_inspection_service import GraphRunInspectionService
from interfaces.services.run_inspection_factory import (
    graph_run_inspection_service_from_env,
)
from interfaces.services.run_operation_service import (
    GraphRunOperationApplicationService,
)
from interfaces.services.run_service import RunApplicationService
from interfaces.services.schedule_service import ScheduleApplicationService
from interfaces.services.source_service import SourceApplicationService
from interfaces.services.storage_service import StorageApplicationService
from interfaces.services.subscription_service import SubscriptionApplicationService
from interfaces.services.worker_service import WorkerApplicationService
from framework.events.runtime.projection import RuntimeOperatorStatusService
from framework.execution_environment.composition import RuntimeExecutionComposition


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
ResearchHistoryServiceFactory = Callable[[], ResearchHistoryService]
RuntimeOperatorStatusServiceFactory = Callable[[], RuntimeOperatorStatusService]


@dataclass(frozen=True)
class ApiServices:
    worker_service_factory: WorkerServiceFactory
    run_service_factory: RunServiceFactory
    graph_run_operation_service_factory: GraphRunOperationServiceFactory
    report_service_factory: ReportServiceFactory
    memory_service_factory: MemoryServiceFactory
    diagnostic_service_factory: DiagnosticServiceFactory
    source_service_factory: SourceServiceFactory
    entity_service_factory: EntityServiceFactory
    subscription_service_factory: SubscriptionServiceFactory
    mcp_service_factory: MCPServiceFactory
    event_operator_service_factory: EventOperatorServiceFactory
    graph_run_inspection_service_factory: GraphRunInspectionServiceFactory
    artifact_service_factory: ArtifactInspectionServiceFactory
    storage_service_factory: StorageServiceFactory
    schedule_service_factory: ScheduleServiceFactory
    auth_service_factory: AuthServiceFactory
    project_service_factory: ProjectServiceFactory
    research_service_factory: ResearchServiceFactory
    research_history_service_factory: ResearchHistoryServiceFactory
    harness_graph_service_factory: HarnessGraphServiceFactory | None = None
    harness_wait_service_factory: HarnessWaitServiceFactory | None = None
    runtime_operator_status_service_factory: RuntimeOperatorStatusServiceFactory | None = None
    runtime_execution_composition: RuntimeExecutionComposition | None = None


@dataclass(frozen=True)
class ApiRouteHelpers:
    success: Callable[[dict[str, Any]], dict[str, Any]]
    error: Callable[..., Any]
    model_to_dict: Callable[[Any], dict[str, Any]]
    run_result_response: Callable[[Any], dict[str, Any]]
    provided_values: Callable[..., dict[str, Any]]
    artifact_lookup_ids: Callable[[str, str | None], tuple[str, str]]
    run_progress_sse_frames: Callable[[dict[str, Any]], Any]
    run_events_sse_frames: Callable[[dict[str, Any]], Any]


def build_api_services(
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
    harness_graph_service_factory: HarnessGraphServiceFactory | None = None,
    harness_wait_service_factory: HarnessWaitServiceFactory | None = None,
    runtime_operator_status_service_factory: RuntimeOperatorStatusServiceFactory | None = None,
    runtime_execution_composition: RuntimeExecutionComposition | None = None,
) -> ApiServices:
    return ApiServices(
        worker_service_factory=worker_service_factory,
        run_service_factory=run_service_factory,
        graph_run_operation_service_factory=graph_run_operation_service_factory,
        report_service_factory=report_service_factory,
        memory_service_factory=memory_service_factory,
        diagnostic_service_factory=diagnostic_service_factory,
        source_service_factory=source_service_factory,
        entity_service_factory=entity_service_factory,
        subscription_service_factory=subscription_service_factory,
        mcp_service_factory=mcp_service_factory,
        event_operator_service_factory=event_operator_service_factory,
        graph_run_inspection_service_factory=graph_run_inspection_service_factory,
        artifact_service_factory=artifact_service_factory,
        storage_service_factory=storage_service_factory,
        schedule_service_factory=schedule_service_factory,
        auth_service_factory=auth_service_factory,
        project_service_factory=project_service_factory,
        research_service_factory=research_service_factory,
        research_history_service_factory=research_history_service_factory,
        harness_graph_service_factory=harness_graph_service_factory,
        harness_wait_service_factory=harness_wait_service_factory,
        runtime_operator_status_service_factory=runtime_operator_status_service_factory,
        runtime_execution_composition=runtime_execution_composition,
    )


def get_actor_context(request: Request) -> ActorContext:
    actor = getattr(request.state, "actor_context", None)
    if isinstance(actor, ActorContext):
        return actor
    return actor_context_from_headers(request.headers, request_id="")
