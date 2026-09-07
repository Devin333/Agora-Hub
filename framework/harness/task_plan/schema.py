from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import required_text


TASK_PLAN_RUNTIME_VERSION = "newsroom.harness-task-plan-runtime/v1"
TASK_PLAN_EVENT_SCHEMA_V2 = "newsroom.harness-task-plan-event/v2"
TASK_PLAN_EVENT_SCHEMAS = (
    TASK_PLAN_EVENT_SCHEMA_V2,
)
TASK_DEFINITION_SCHEMA = "newsroom.harness-task-definition/v1"
RESOLVED_TASK_DEFINITION_SCHEMA = "newsroom.harness-resolved-task-definition/v1"
GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA = "newsroom.harness-task-plan-candidate/v2"
GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA = "newsroom.harness-task-plan/v2"
GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA = "newsroom.harness-task-plan-patch/v2"
TASK_PLAN_POLICY_SCHEMA = "newsroom.harness-task-plan-policy/v1"
GRAPH_ONLY_TASK_INSTANCE_SCHEMA = "newsroom.harness-task-instance/v3"
GRAPH_ONLY_TASK_PROJECTION_SCHEMA = "newsroom.harness-task-projection/v3"
GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA = (
    "newsroom.harness-task-plan-projection/v3"
)
TASK_RESULT_REFERENCE_SCHEMA = "newsroom.harness-task-result-reference/v1"
TASK_CAPABILITY_BINDING_SCHEMA = "newsroom.harness-task-capability-binding/v1"
GRAPH_ONLY_TASK_PLAN_STAGE_BINDING_SCHEMA = (
    "newsroom.harness-task-plan-stage-binding/v2"
)
GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA = (
    "newsroom.harness-task-plan-stage-identity/v2"
)


class TaskPlanContractKind(StrEnum):
    TASK_DEFINITION = "task_definition"
    RESOLVED_TASK_DEFINITION = "resolved_task_definition"
    PLAN_CANDIDATE = "plan_candidate"
    VALIDATED_PLAN = "validated_plan"
    PLAN_PATCH = "plan_patch"
    POLICY = "policy"
    TASK_INSTANCE = "task_instance"
    TASK_PROJECTION = "task_projection"
    PLAN_PROJECTION = "plan_projection"
    RESULT_REFERENCE = "result_reference"
    CAPABILITY_BINDING = "capability_binding"
    STAGE_BINDING = "stage_binding"
    STAGE_IDENTITY = "stage_identity"


@dataclass(frozen=True, slots=True)
class TaskPlanSchemaRegistration:
    contract_kind: TaskPlanContractKind | str
    writer_schema: str
    readable_schemas: tuple[str, ...]
    executable_schemas: tuple[str, ...]

    def __post_init__(self) -> None:
        kind = TaskPlanContractKind(self.contract_kind)
        writer = required_text(self.writer_schema, "writer_schema")
        readable = tuple(sorted(required_text(item, "readable_schemas") for item in self.readable_schemas))
        executable = tuple(sorted(required_text(item, "executable_schemas") for item in self.executable_schemas))
        if len(readable) != len(set(readable)) or len(executable) != len(set(executable)):
            raise HarnessValidationError(
                "TaskPlan schema registrations must be unique",
                code="duplicate_task_plan_schema",
            )
        if writer not in readable or writer not in executable:
            raise HarnessValidationError(
                "TaskPlan writer schema must be readable and executable",
                code="invalid_task_plan_writer_schema",
                details={"contract_kind": kind.value, "writer_schema": writer},
            )
        if not set(executable).issubset(readable):
            raise HarnessValidationError(
                "TaskPlan executable schemas must also be readable",
                code="invalid_task_plan_executable_schema",
                details={"contract_kind": kind.value},
            )
        object.__setattr__(self, "contract_kind", kind)
        object.__setattr__(self, "writer_schema", writer)
        object.__setattr__(self, "readable_schemas", readable)
        object.__setattr__(self, "executable_schemas", executable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_kind": self.contract_kind.value,
            "writer_schema": self.writer_schema,
            "readable_schemas": list(self.readable_schemas),
            "executable_schemas": list(self.executable_schemas),
        }


class TaskPlanSchemaRegistry:
    def __init__(self, registrations: tuple[TaskPlanSchemaRegistration, ...]) -> None:
        by_kind: dict[TaskPlanContractKind, TaskPlanSchemaRegistration] = {}
        for registration in registrations:
            if not isinstance(registration, TaskPlanSchemaRegistration):
                raise TypeError("registrations must contain TaskPlanSchemaRegistration values")
            if registration.contract_kind in by_kind:
                raise HarnessValidationError(
                    "TaskPlan schema contract kind is already registered",
                    code="duplicate_task_plan_contract_kind",
                    details={"contract_kind": registration.contract_kind.value},
                )
            by_kind[registration.contract_kind] = registration
        missing = sorted(kind.value for kind in set(TaskPlanContractKind) - set(by_kind))
        if missing:
            raise HarnessValidationError(
                "TaskPlan schema registry is incomplete",
                code="incomplete_task_plan_schema_registry",
                details={"missing": missing},
            )
        self._by_kind: Mapping[TaskPlanContractKind, TaskPlanSchemaRegistration] = MappingProxyType(by_kind)

    @property
    def registrations(self) -> tuple[TaskPlanSchemaRegistration, ...]:
        return tuple(self._by_kind[kind] for kind in TaskPlanContractKind)

    def require_readable(
        self,
        contract_kind: TaskPlanContractKind | str,
        schema: str,
    ) -> TaskPlanSchemaRegistration:
        kind = TaskPlanContractKind(contract_kind)
        registration = self._by_kind[kind]
        if schema not in registration.readable_schemas:
            raise HarnessValidationError(
                "unsupported TaskPlan contract schema",
                code="unsupported_task_plan_schema",
                details={"contract_kind": kind.value, "schema": str(schema)},
            )
        return registration

    def require_executable(
        self,
        contract_kind: TaskPlanContractKind | str,
        schema: str,
    ) -> TaskPlanSchemaRegistration:
        registration = self.require_readable(contract_kind, schema)
        if schema not in registration.executable_schemas:
            raise HarnessValidationError(
                "TaskPlan contract schema is read-only",
                code="task_plan_schema_not_executable",
                details={
                    "contract_kind": registration.contract_kind.value,
                    "schema": str(schema),
                },
            )
        return registration

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_version": TASK_PLAN_RUNTIME_VERSION,
            "registrations": [registration.to_dict() for registration in self.registrations],
        }


_WRITERS: Mapping[TaskPlanContractKind, str] = MappingProxyType(
    {
        TaskPlanContractKind.TASK_DEFINITION: TASK_DEFINITION_SCHEMA,
        TaskPlanContractKind.RESOLVED_TASK_DEFINITION: RESOLVED_TASK_DEFINITION_SCHEMA,
        # Graph v2 is the sole TaskPlan contract. Historical legacy data is
        # intentionally handled outside the runtime by migration tooling.
        TaskPlanContractKind.PLAN_CANDIDATE: GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA,
        TaskPlanContractKind.VALIDATED_PLAN: GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
        TaskPlanContractKind.PLAN_PATCH: GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA,
        TaskPlanContractKind.POLICY: TASK_PLAN_POLICY_SCHEMA,
        TaskPlanContractKind.TASK_INSTANCE: GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
        TaskPlanContractKind.TASK_PROJECTION: GRAPH_ONLY_TASK_PROJECTION_SCHEMA,
        TaskPlanContractKind.PLAN_PROJECTION: GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
        TaskPlanContractKind.RESULT_REFERENCE: TASK_RESULT_REFERENCE_SCHEMA,
        TaskPlanContractKind.CAPABILITY_BINDING: TASK_CAPABILITY_BINDING_SCHEMA,
        TaskPlanContractKind.STAGE_BINDING: GRAPH_ONLY_TASK_PLAN_STAGE_BINDING_SCHEMA,
        TaskPlanContractKind.STAGE_IDENTITY: GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA,
    }
)

_ADDITIONAL_SCHEMAS: Mapping[TaskPlanContractKind, tuple[str, ...]] = (
    MappingProxyType(
        {
            TaskPlanContractKind.PLAN_CANDIDATE: (
                GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA,
            ),
            TaskPlanContractKind.VALIDATED_PLAN: (
                GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
            ),
            TaskPlanContractKind.PLAN_PATCH: (
                GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA,
            ),
            TaskPlanContractKind.TASK_INSTANCE: (
                GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
            ),
            TaskPlanContractKind.TASK_PROJECTION: (
                GRAPH_ONLY_TASK_PROJECTION_SCHEMA,
            ),
            TaskPlanContractKind.PLAN_PROJECTION: (
                GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
            ),
            TaskPlanContractKind.STAGE_BINDING: (
                GRAPH_ONLY_TASK_PLAN_STAGE_BINDING_SCHEMA,
            ),
            TaskPlanContractKind.STAGE_IDENTITY: (
                GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA,
            ),
        }
    )
)

DEFAULT_TASK_PLAN_SCHEMA_REGISTRY = TaskPlanSchemaRegistry(
    tuple(
        TaskPlanSchemaRegistration(
            contract_kind=kind,
            writer_schema=schema,
            readable_schemas=tuple(
                sorted(
                    {
                        schema,
                        *_ADDITIONAL_SCHEMAS.get(kind, ()),
                    }
                )
            ),
            executable_schemas=(schema,),
        )
        for kind, schema in _WRITERS.items()
    )
)


__all__ = [
    "DEFAULT_TASK_PLAN_SCHEMA_REGISTRY",
    "GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA",
    "GRAPH_ONLY_TASK_INSTANCE_SCHEMA",
    "GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA",
    "GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA",
    "GRAPH_ONLY_TASK_PROJECTION_SCHEMA",
    "GRAPH_ONLY_TASK_PLAN_STAGE_BINDING_SCHEMA",
    "GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA",
    "GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA",
    "RESOLVED_TASK_DEFINITION_SCHEMA",
    "TASK_CAPABILITY_BINDING_SCHEMA",
    "TASK_DEFINITION_SCHEMA",
    "TASK_PLAN_EVENT_SCHEMA_V2",
    "TASK_PLAN_EVENT_SCHEMAS",
    "TASK_PLAN_POLICY_SCHEMA",
    "TASK_PLAN_RUNTIME_VERSION",
    "TASK_RESULT_REFERENCE_SCHEMA",
    "TaskPlanContractKind",
    "TaskPlanSchemaRegistration",
    "TaskPlanSchemaRegistry",
]
