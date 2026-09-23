"""Cross-object runtime contract checks owned by the Harness.

The individual models remain authoritative for their schemas.  This module
only binds those existing writers at admission and acceptance boundaries; it
does not own a second registry or persistence store.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from framework.harness.control_plane.errors import HarnessValidationError


HARNESS_RUNTIME_CONTRACT_VERSION = "newsroom.harness-runtime-contract/v1"


@dataclass(frozen=True, slots=True)
class RuntimeContractBinding:
    """Read-only view of the current owners of cross-runtime contracts."""

    schema_version: str
    owners: Mapping[str, str]

    @classmethod
    def current(cls) -> "RuntimeContractBinding":
        # Imports stay local so owner modules can use this validator without a
        # module-import cycle.  Values are copied from the existing owner
        # constants and cannot be supplied by callers.
        from framework.execution_environment.models import EXECUTION_PROFILE_SCHEMA
        from framework.harness.artifacts.terminal_manifest import GRAPH_TERMINAL_MANIFEST_SCHEMA
        from framework.harness.control_plane.budget_reservation import BUDGET_RESERVATION_SCHEMA
        from framework.harness.subagents.models import SUBAGENT_INVOCATION_SCHEMA_V3
        from framework.harness.subagents.transcript import (
            SUBAGENT_BUNDLE_SCHEMA_V3,
            SUBAGENT_RECEIPT_SCHEMA_V3,
            SUBAGENT_TRANSCRIPT_SCHEMA_V3,
        )
        from framework.harness.task_plan.attempt_history import TASK_ATTEMPT_HISTORY_SCHEMA
        from framework.harness.task_plan.continuation import PARENT_CONTINUATION_SCHEMA
        from framework.harness.task_plan.parallel import (
            DISPATCH_GROUP_SCHEMA,
            DISPATCH_WAVE_SCHEMA,
            PARALLEL_DISPATCH_REQUEST_SCHEMA,
            PARALLEL_DISPATCH_RESULT_SCHEMA,
            TASK_RESERVATION_SCHEMA,
        )
        from framework.harness.task_plan.schema import (
            DEFAULT_TASK_PLAN_SCHEMA_REGISTRY,
            GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
            GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
            GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
            TASK_PLAN_RUNTIME_VERSION,
        )
        from framework.harness.task_plan.store import TASK_PLAN_EVENT_SCHEMA

        owners = {
            "execution_profile": EXECUTION_PROFILE_SCHEMA,
            "task_plan_runtime": TASK_PLAN_RUNTIME_VERSION,
            "validated_task_plan": GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
            "task_instance": GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
            "task_plan_projection": GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
            "task_plan_event": TASK_PLAN_EVENT_SCHEMA,
            "dispatch_request": PARALLEL_DISPATCH_REQUEST_SCHEMA,
            "dispatch_result": PARALLEL_DISPATCH_RESULT_SCHEMA,
            "dispatch_group": DISPATCH_GROUP_SCHEMA,
            "dispatch_wave": DISPATCH_WAVE_SCHEMA,
            "task_reservation": TASK_RESERVATION_SCHEMA,
            "attempt_history": TASK_ATTEMPT_HISTORY_SCHEMA,
            "subagent_invocation": SUBAGENT_INVOCATION_SCHEMA_V3,
            "subagent_transcript": SUBAGENT_TRANSCRIPT_SCHEMA_V3,
            "subagent_receipt": SUBAGENT_RECEIPT_SCHEMA_V3,
            "subagent_bundle": SUBAGENT_BUNDLE_SCHEMA_V3,
            "budget_reservation": BUDGET_RESERVATION_SCHEMA,
            "artifact_manifest": GRAPH_TERMINAL_MANIFEST_SCHEMA,
            "parent_continuation": PARENT_CONTINUATION_SCHEMA,
        }
        # Exercise the existing TaskPlan registry as part of the binding.  A
        # stale owner constant must fail closed rather than silently becoming a
        # second source of truth here.
        for kind in ("validated_plan", "task_instance", "plan_projection"):
            registration = DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
                kind, owners[
                    {
                        "validated_plan": "validated_task_plan",
                        "task_instance": "task_instance",
                        "plan_projection": "task_plan_projection",
                    }[kind]
                ]
            )
            if registration.writer_schema != owners[
                {
                    "validated_plan": "validated_task_plan",
                    "task_instance": "task_instance",
                    "plan_projection": "task_plan_projection",
                }[kind]
            ]:
                raise HarnessValidationError(
                    "runtime contract owner registry drift",
                    code="RUNTIME_CONTRACT_OWNER_DRIFT",
                )
        return cls(
            schema_version=HARNESS_RUNTIME_CONTRACT_VERSION,
            owners=MappingProxyType(owners),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "owners": dict(self.owners)}


def runtime_contract_binding() -> RuntimeContractBinding:
    return RuntimeContractBinding.current()


def validate_parallel_dispatch_contract(request: Any, group: Any | None = None) -> None:
    """Validate immutable admission bindings before capacity or worker work."""

    binding = runtime_contract_binding()
    from framework.harness.task_plan.parallel import PARALLEL_DISPATCH_REQUEST_SCHEMA
    from framework.harness.task_plan.parallel_admission import validate_group_plan_binding
    from framework.harness.task_plan.schema import (
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY,
        TaskPlanContractKind,
    )
    from framework.harness.task_plan.models import ValidatedTaskPlan

    if getattr(request, "schema_version", None) != PARALLEL_DISPATCH_REQUEST_SCHEMA:
        raise HarnessValidationError(
            "unsupported parallel dispatch request schema",
            code="RUNTIME_CONTRACT_SCHEMA_MISMATCH",
        )
    plan = getattr(request, "plan", None)
    if not isinstance(plan, ValidatedTaskPlan):
        raise HarnessValidationError("dispatch plan is not validated", code="RUNTIME_CONTRACT_IDENTITY_MISMATCH")
    DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
        TaskPlanContractKind.VALIDATED_PLAN, plan.schema_version
    )
    instances = tuple(getattr(request, "task_instances", ()))
    if tuple(item.plan_checksum for item in instances) != (plan.plan_checksum,) * len(instances):
        raise HarnessValidationError(
            "dispatch task instance checksum differs from accepted plan",
            code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH",
        )
    if group is not None:
        snapshot = group.to_dict() if hasattr(group, "to_dict") else group
        if not isinstance(snapshot, Mapping):
            raise HarnessValidationError("dispatch group snapshot is invalid", code="RUNTIME_CONTRACT_SCHEMA_MISMATCH")
        validate_group_plan_binding(snapshot, plan)


def validate_task_result_contract(
    plan: Any,
    projection: Any,
    result: Any,
) -> None:
    """Validate result identity and transition before a result is persisted."""

    runtime_contract_binding()
    from framework.harness.task_plan.models import TaskLifecycle, TaskPlanProjection, ValidatedTaskPlan
    from framework.harness.task_plan.store import TASK_PLAN_RESULT_SCHEMA_V3, TaskResultRecord

    if not isinstance(plan, ValidatedTaskPlan) or not isinstance(projection, TaskPlanProjection):
        raise HarnessValidationError("result acceptance requires validated plan and projection", code="RUNTIME_CONTRACT_IDENTITY_MISMATCH")
    if not isinstance(result, TaskResultRecord) or result.schema_version != TASK_PLAN_RESULT_SCHEMA_V3:
        raise HarnessValidationError("unsupported task result schema", code="RUNTIME_CONTRACT_SCHEMA_MISMATCH")
    if not result.matches_plan_identity(plan) or not projection.matches_plan_identity(plan):
        raise HarnessValidationError("result scope differs from accepted plan", code="RUNTIME_CONTRACT_SCOPE_MISMATCH")
    task = next((item for item in projection.tasks if item.task_id == result.task_id), None)
    definition = next((item for item in plan.tasks if item.task_id == result.task_id), None)
    if task is None or definition is None:
        raise HarnessValidationError("result references unknown task", code="RUNTIME_CONTRACT_REFERENCE_MISMATCH")
    if (
        definition.task_definition_checksum != result.task_checksum
        or definition.binding_checksum != result.binding_checksum
        or definition.worker_ref != result.worker_ref
    ):
        raise HarnessValidationError("result binding checksum differs from plan", code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH")
    if task.active_instance_id != result.task_instance_id or task.attempts != result.attempt:
        raise HarnessValidationError("result belongs to a different attempt", code="RUNTIME_CONTRACT_TRANSITION_INVALID")
    if task.status in {TaskLifecycle.SUCCEEDED, TaskLifecycle.SKIPPED}:
        raise HarnessValidationError("result transition is already terminal", code="RUNTIME_CONTRACT_TRANSITION_INVALID")


def validate_history_read_contract(plans: Any, events: Any, results: Any) -> None:
    """Fail closed when a history read mixes unsupported owner contracts."""

    runtime_contract_binding()
    from framework.harness.task_plan.models import ValidatedTaskPlan
    from framework.harness.task_plan.store import TASK_PLAN_EVENT_SCHEMAS, TASK_PLAN_RESULT_SCHEMA_V3

    for plan in plans:
        if not isinstance(plan, ValidatedTaskPlan):
            raise HarnessValidationError("history contains an invalid plan", code="RUNTIME_CONTRACT_SCHEMA_MISMATCH")
    for event in events:
        if getattr(event, "schema_version", None) not in TASK_PLAN_EVENT_SCHEMAS:
            raise HarnessValidationError("history contains an unsupported event", code="RUNTIME_CONTRACT_SCHEMA_MISMATCH")
    for result in results:
        if getattr(result, "schema_version", None) != TASK_PLAN_RESULT_SCHEMA_V3:
            raise HarnessValidationError("history contains an unsupported result", code="RUNTIME_CONTRACT_SCHEMA_MISMATCH")


__all__ = [
    "HARNESS_RUNTIME_CONTRACT_VERSION",
    "RuntimeContractBinding",
    "runtime_contract_binding",
    "validate_history_read_contract",
    "validate_parallel_dispatch_contract",
    "validate_task_result_contract",
]
