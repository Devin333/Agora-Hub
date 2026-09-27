"""Cross-object runtime contract checks owned by the Harness.

The individual models remain authoritative for their schemas.  This module
only binds those existing writers at admission and acceptance boundaries; it
does not own a second registry or persistence store.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
        from framework.harness.subagents.supervisor import CHILD_AGENT_HANDLE_SCHEMA_VERSION
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
            "child_agent_handle": CHILD_AGENT_HANDLE_SCHEMA_VERSION,
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
    from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
    from framework.harness.task_plan.models import TaskInstance, ValidatedTaskPlan
    from framework.harness.task_plan.parallel import (
        PARALLEL_DISPATCH_REQUEST_SCHEMA,
        DispatchGroup,
        ParallelDispatchRequest,
        _admission_policy_checksum,
    )
    from framework.harness.task_plan.parallel_admission import validate_group_plan_binding
    from framework.harness.task_plan.scheduler import task_instance_for_attempt
    from framework.harness.task_plan.schema import (
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY,
        TaskPlanContractKind,
    )

    if (
        type(request) is not ParallelDispatchRequest
        or request.schema_version != PARALLEL_DISPATCH_REQUEST_SCHEMA
    ):
        raise HarnessValidationError(
            "parallel dispatch requires the canonical request type and schema",
            code="RUNTIME_CONTRACT_SCHEMA_MISMATCH",
        )
    plan = request.plan
    if type(plan) is not ValidatedTaskPlan:
        raise HarnessValidationError("dispatch plan is not validated", code="RUNTIME_CONTRACT_IDENTITY_MISMATCH")
    DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
        TaskPlanContractKind.VALIDATED_PLAN, plan.schema_version
    )
    try:
        ValidatedTaskPlan.from_dict(plan.to_dict())
    except (HarnessValidationError, TypeError, ValueError, AttributeError, KeyError) as exc:
        raise HarnessValidationError(
            "dispatch plan checksum differs from canonical content",
            code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH",
        ) from exc

    definitions = {item.task_id: item for item in plan.tasks}
    instances = tuple(request.task_instances)
    if any(type(item) is not TaskInstance for item in instances):
        raise HarnessValidationError(
            "dispatch request contains a non-canonical task instance",
            code="RUNTIME_CONTRACT_SCHEMA_MISMATCH",
        )
    if len({item.task_id for item in instances}) != len(instances):
        raise HarnessValidationError(
            "dispatch request contains duplicate task references",
            code="RUNTIME_CONTRACT_REFERENCE_MISMATCH",
        )
    for instance in instances:
        if instance.schema_version != binding.owners["task_instance"]:
            raise HarnessValidationError(
                "unsupported task instance schema",
                code="RUNTIME_CONTRACT_SCHEMA_MISMATCH",
            )
        if instance.plan_checksum != plan.plan_checksum:
            raise HarnessValidationError(
                "dispatch task instance checksum differs from accepted plan",
                code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH",
            )
        if not instance.matches_plan_identity(plan):
            raise HarnessValidationError(
                "dispatch task instance scope differs from accepted plan",
                code="RUNTIME_CONTRACT_SCOPE_MISMATCH",
            )
        definition = definitions.get(instance.task_id)
        if definition is None:
            raise HarnessValidationError(
                "dispatch task instance references an unknown task",
                code="RUNTIME_CONTRACT_REFERENCE_MISMATCH",
            )
        if instance.worker_ref != definition.worker_ref:
            raise HarnessValidationError(
                "dispatch task instance uses an unapproved worker capability",
                code="RUNTIME_CONTRACT_CAPABILITY_MISMATCH",
            )
        if instance.task_definition_checksum != definition.task_definition_checksum:
            raise HarnessValidationError(
                "dispatch task definition checksum differs from accepted plan",
                code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH",
            )
        expected = task_instance_for_attempt(plan, instance.task_id, instance.attempt)
        if (
            instance.task_instance_id != expected.task_instance_id
            or instance.idempotency_key != expected.idempotency_key
            or instance.fencing_token != expected.fencing_token
        ):
            raise HarnessValidationError(
                "dispatch task instance has a forged attempt identity",
                code="RUNTIME_CONTRACT_IDENTITY_MISMATCH",
            )
        try:
            canonical = TaskInstance.from_dict(instance.to_dict())
        except (HarnessValidationError, TypeError, ValueError, AttributeError, KeyError) as exc:
            raise HarnessValidationError(
                "dispatch task instance checksum differs from canonical content",
                code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH",
            ) from exc
        if canonical != expected:
            raise HarnessValidationError(
                "dispatch task instance differs from the accepted task binding",
                code="RUNTIME_CONTRACT_REFERENCE_MISMATCH",
            )

    try:
        ledger = TaskPlanBudgetLedger.from_snapshot(request.budget_snapshot)
    except (HarnessValidationError, TypeError, ValueError, AttributeError, KeyError) as exc:
        raise HarnessValidationError(
            "dispatch budget snapshot is invalid",
            code="RUNTIME_CONTRACT_SCHEMA_MISMATCH",
        ) from exc
    if (
        (ledger.run_id, ledger.stage_id, ledger.policy_ref)
        != (plan.run_id, plan.stage_id, plan.policy_ref)
    ):
        raise HarnessValidationError(
            "dispatch budget belongs to a different run, stage, or policy",
            code="RUNTIME_CONTRACT_SCOPE_MISMATCH",
        )
    if dict(ledger.parent_allocation) != plan.limits.aggregate_task_budget.to_dict():
        raise HarnessValidationError(
            "dispatch budget policy differs from the accepted plan",
            code="RUNTIME_CONTRACT_POLICY_MISMATCH",
        )

    # Re-run the canonical request owner's validation so post-construction
    # mutation of capacity, policy, reference, or identity fields cannot cross
    # the admission boundary.  The reconstructed value is deliberately not a
    # second schema owner.
    try:
        canonical_request = replace(request)
    except (HarnessValidationError, TypeError, ValueError, AttributeError, KeyError) as exc:
        raise HarnessValidationError(
            "parallel dispatch request no longer satisfies its owner contract",
            code="RUNTIME_CONTRACT_POLICY_MISMATCH",
        ) from exc
    if canonical_request != request:
        raise HarnessValidationError(
            "parallel dispatch request differs from its canonical owner model",
            code="RUNTIME_CONTRACT_IDENTITY_MISMATCH",
        )

    if group is not None:
        if type(group) is not DispatchGroup:
            raise HarnessValidationError(
                "dispatch group must use the canonical owner type",
                code="RUNTIME_CONTRACT_SCHEMA_MISMATCH",
            )
        snapshot = group.to_dict()
        try:
            canonical_group = DispatchGroup.from_dict(snapshot)
        except (HarnessValidationError, TypeError, ValueError, AttributeError, KeyError) as exc:
            raise HarnessValidationError(
                "dispatch group identity or checksum is not canonical",
                code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH",
            ) from exc
        if canonical_group != group:
            raise HarnessValidationError(
                "dispatch group differs from its canonical owner model",
                code="RUNTIME_CONTRACT_IDENTITY_MISMATCH",
            )
        validate_group_plan_binding(snapshot, plan)
        if group.admission_policy_checksum != _admission_policy_checksum(request):
            raise HarnessValidationError(
                "dispatch group policy differs from the admitted request",
                code="RUNTIME_CONTRACT_POLICY_MISMATCH",
            )
        if (
            group.admitted_at_ms != request.group_admitted_at_ms
            or group.absolute_deadline_ms != request.group_absolute_deadline_ms
            or group.correlation_id != (request.correlation_id or f"group-{plan.plan_id}")
            or group.join_policy.value != request.join_policy.value
            or group.max_waves != request.max_waves
        ):
            raise HarnessValidationError(
                "dispatch group transition metadata differs from the admitted request",
                code="RUNTIME_CONTRACT_IDENTITY_MISMATCH",
            )
        if group.state.value != "ADMITTED":
            raise HarnessValidationError(
                "dispatch group has not completed the admission transition",
                code="RUNTIME_CONTRACT_TRANSITION_INVALID",
            )


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

    binding = runtime_contract_binding()
    from framework.harness.task_plan.models import ValidatedTaskPlan
    from framework.harness.task_plan.store import (
        TASK_PLAN_EVENT_SCHEMAS,
        TASK_PLAN_RESULT_SCHEMA_V3,
        TaskPlanEvent,
        TaskResultRecord,
    )

    canonical_plans: list[ValidatedTaskPlan] = []
    for plan in plans:
        if (
            type(plan) is not ValidatedTaskPlan
            or plan.schema_version != binding.owners["validated_task_plan"]
        ):
            raise HarnessValidationError(
                "history contains an invalid plan",
                code="RUNTIME_CONTRACT_SCHEMA_MISMATCH",
            )
        try:
            canonical = ValidatedTaskPlan.from_dict(plan.to_dict())
        except (
            HarnessValidationError,
            TypeError,
            ValueError,
            AttributeError,
            KeyError,
        ) as exc:
            raise HarnessValidationError(
                "history plan checksum differs from canonical content",
                code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH",
            ) from exc
        if canonical != plan:
            raise HarnessValidationError(
                "history plan differs from its canonical owner model",
                code="RUNTIME_CONTRACT_IDENTITY_MISMATCH",
            )
        canonical_plans.append(plan)

    plans_by_identity = {
        (plan.plan_id, plan.version): plan for plan in canonical_plans
    }
    max_plan_version = max(
        (plan.version for plan in canonical_plans),
        default=0,
    )
    if len(plans_by_identity) != len(canonical_plans):
        raise HarnessValidationError(
            "history contains duplicate plan identities",
            code="RUNTIME_CONTRACT_REFERENCE_MISMATCH",
        )

    for event in events:
        if (
            type(event) is not TaskPlanEvent
            or event.schema_version not in TASK_PLAN_EVENT_SCHEMAS
        ):
            raise HarnessValidationError(
                "history contains an unsupported event",
                code="RUNTIME_CONTRACT_SCHEMA_MISMATCH",
            )
        try:
            canonical = TaskPlanEvent.from_dict(event.to_dict())
        except (
            HarnessValidationError,
            TypeError,
            ValueError,
            AttributeError,
            KeyError,
        ) as exc:
            raise HarnessValidationError(
                "history event checksum differs from canonical content",
                code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH",
            ) from exc
        if canonical != event:
            raise HarnessValidationError(
                "history event differs from its canonical owner model",
                code="RUNTIME_CONTRACT_IDENTITY_MISMATCH",
            )
        if event.plan_id is None:
            if event.plan_version is not None or not any(
                event.matches_contract_identity(plan) for plan in canonical_plans
            ):
                raise HarnessValidationError(
                    "history event scope differs from the supplied plans",
                    code="RUNTIME_CONTRACT_SCOPE_MISMATCH",
                )
            continue
        if event.plan_version is None:
            raise HarnessValidationError(
                "history event plan identity is incomplete",
                code="RUNTIME_CONTRACT_IDENTITY_MISMATCH",
            )
        plan = plans_by_identity.get((event.plan_id, event.plan_version))
        if plan is None:
            if event.plan_version > max_plan_version:
                if not any(
                    event.matches_contract_identity(candidate)
                    for candidate in canonical_plans
                ):
                    raise HarnessValidationError(
                        "history event scope differs from the supplied plans",
                        code="RUNTIME_CONTRACT_SCOPE_MISMATCH",
                    )
                continue
            raise HarnessValidationError(
                "history event references an unknown plan",
                code="RUNTIME_CONTRACT_REFERENCE_MISMATCH",
            )
        if not event.matches_contract_identity(plan):
            raise HarnessValidationError(
                "history event scope differs from its plan",
                code="RUNTIME_CONTRACT_SCOPE_MISMATCH",
            )

    for result in results:
        if (
            type(result) is not TaskResultRecord
            or result.schema_version != TASK_PLAN_RESULT_SCHEMA_V3
        ):
            raise HarnessValidationError(
                "history contains an unsupported result",
                code="RUNTIME_CONTRACT_SCHEMA_MISMATCH",
            )
        try:
            canonical = TaskResultRecord.from_dict(result.to_dict())
        except (
            HarnessValidationError,
            TypeError,
            ValueError,
            AttributeError,
            KeyError,
        ) as exc:
            raise HarnessValidationError(
                "history result checksum differs from canonical content",
                code="RUNTIME_CONTRACT_CHECKSUM_MISMATCH",
            ) from exc
        if canonical != result:
            raise HarnessValidationError(
                "history result differs from its canonical owner model",
                code="RUNTIME_CONTRACT_IDENTITY_MISMATCH",
            )
        plan = plans_by_identity.get((result.plan_id, result.plan_version))
        if plan is None:
            raise HarnessValidationError(
                "history result references an unknown plan",
                code="RUNTIME_CONTRACT_REFERENCE_MISMATCH",
            )
        if not result.matches_plan_identity(plan):
            raise HarnessValidationError(
                "history result scope differs from its plan",
                code="RUNTIME_CONTRACT_SCOPE_MISMATCH",
            )


__all__ = [
    "HARNESS_RUNTIME_CONTRACT_VERSION",
    "RuntimeContractBinding",
    "runtime_contract_binding",
    "validate_history_read_contract",
    "validate_parallel_dispatch_contract",
    "validate_task_result_contract",
]
