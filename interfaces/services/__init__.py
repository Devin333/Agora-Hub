"""Application service package.

Service implementations remain public package exports, but are imported on
first access so lightweight service utilities do not initialize the full
application dependency graph.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AuthApplicationService",
    "DiagnosticApplicationService",
    "EntityTrackingApplicationService",
    "EventDeliveryOperationsService",
    "EventAuthorizationContext",
    "EventAuthorizationDecision",
    "EventAuthorizationRequest",
    "EventAuthorizerPort",
    "EventProjectionService",
    "EventQuarantineService",
    "EventReaderService",
    "EventReplayService",
    "EventStreamReadResult",
    "HarnessWaitApplicationService",
    "HarnessGraphApplicationService",
    "HarnessGraphReplayResult",
    "HarnessGraphRunOperationResult",
    "HarnessWaitInspectionResult",
    "HarnessWaitInspectionListResult",
    "HarnessWaitOperationResult",
    "DurableHarnessWaitApprovalResolver",
    "HarnessWaitRuntimeRegistration",
    "HarnessWaitRuntimeRegistry",
    "HarnessWaitRuntimeStoreError",
    "HarnessWaitRuntimeUnavailableError",
    "ArtifactInspectionService",
    "MemoryApplicationService",
    "MCPApplicationService",
    "ProjectApplicationService",
    "ProjectsApplicationService",
    "ResearchApplicationService",
    "ReportApplicationService",
    "GraphRunInspectionService",
    "build_graph_run_inspection_service",
    "graph_run_inspection_service_from_env",
    "RunApplicationService",
    "ScheduleApplicationService",
    "SourceApplicationService",
    "StorageApplicationService",
    "SubscriptionApplicationService",
    "ToolApplicationService",
]

_LAZY_EXPORTS = {
    "AuthApplicationService": "auth_service",
    "RunApplicationService": "run_service",
    "ArtifactInspectionService": "artifact_service",
    "DiagnosticApplicationService": "diagnose_service",
    "EntityTrackingApplicationService": "entity_service",
    "EventDeliveryOperationsService": "event_delivery_operations_service",
    "EventProjectionService": "event_projection_service",
    "EventQuarantineService": "event_quarantine_service",
    "EventAuthorizationContext": "event_reader_service",
    "EventAuthorizationDecision": "event_reader_service",
    "EventAuthorizationRequest": "event_reader_service",
    "EventAuthorizerPort": "event_reader_service",
    "EventReaderService": "event_reader_service",
    "EventStreamReadResult": "event_reader_service",
    "EventReplayService": "event_replay_service",
    "HarnessGraphApplicationService": "harness_graph_service",
    "HarnessGraphReplayResult": "harness_graph_service",
    "HarnessGraphRunOperationResult": "harness_graph_service",
    "HarnessWaitApplicationService": "harness_wait_service",
    "HarnessWaitInspectionResult": "harness_wait_service",
    "HarnessWaitInspectionListResult": "harness_wait_service",
    "HarnessWaitOperationResult": "harness_wait_service",
    "DurableHarnessWaitApprovalResolver": "harness_wait_runtime",
    "HarnessWaitRuntimeRegistration": "harness_wait_runtime",
    "HarnessWaitRuntimeRegistry": "harness_wait_runtime",
    "HarnessWaitRuntimeStoreError": "harness_wait_runtime",
    "HarnessWaitRuntimeUnavailableError": "harness_wait_runtime",
    "MemoryApplicationService": "memory_service",
    "MCPApplicationService": "mcp_service",
    "ProjectApplicationService": "project_service",
    "ProjectsApplicationService": "project_service",
    "ResearchApplicationService": "research_service",
    "ReportApplicationService": "report_service",
    "GraphRunInspectionService": "run_inspection_service",
    "build_graph_run_inspection_service": "run_inspection_factory",
    "graph_run_inspection_service_from_env": "run_inspection_factory",
    "SourceApplicationService": "source_service",
    "ScheduleApplicationService": "schedule_service",
    "StorageApplicationService": "storage_service",
    "SubscriptionApplicationService": "subscription_service",
    "ToolApplicationService": "tool_service",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
