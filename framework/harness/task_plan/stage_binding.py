from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.graph.model import HarnessExecutableNode, NormalizedHarnessGraph
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
)
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    exact_reference,
    frozen_mapping,
    identifier,
    thaw_mapping,
)
from framework.harness.task_plan.schema import (
    DEFAULT_TASK_PLAN_SCHEMA_REGISTRY,
    GRAPH_ONLY_TASK_PLAN_STAGE_BINDING_SCHEMA,
    GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
    TASK_PLAN_EVENT_SCHEMA_V2,
    TaskPlanContractKind,
)


_SUPPORT_REFERENCE_FIELDS = (
    "candidate_builder_ref",
    "capability_registry_ref",
    "gate_registry_ref",
    "aggregator_ref",
    "checkpoint_ref",
    "result_store_ref",
)
_EXACT_SCHEMA_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:/+-]*/v[1-9][0-9]*\Z"
)


@dataclass(frozen=True, slots=True)
class TaskPlanStageBinding:
    """Immutable authority binding for one TaskPlan stage in a frozen Graph."""

    graph: NormalizedHarnessGraph = field(repr=False)
    stage_id: str
    schema_version: str | None = None
    node_id: str = field(init=False)
    step_ref: str = field(init=False)
    worker_ref: str = field(init=False)
    activity_ref: str = field(init=False)
    policy_ref: str = field(init=False)
    task_plan_schema: str = field(init=False)
    required_output_roles: tuple[str, ...] = field(init=False)
    support_refs: Mapping[str, str] = field(init=False)
    binding_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.graph, NormalizedHarnessGraph):
            raise TypeError("graph must be NormalizedHarnessGraph")
        if self.graph.schema_version != GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA:
            raise HarnessValidationError(
                "TaskPlan stage binding requires the live Graph v2 schema",
                code="legacy_task_plan_graph_schema_forbidden",
                details={"schema_version": self.graph.schema_version},
            )
        expected_schema = GRAPH_ONLY_TASK_PLAN_STAGE_BINDING_SCHEMA
        schema_version = (
            expected_schema if self.schema_version is None else self.schema_version
        )
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.STAGE_BINDING,
            schema_version,
        )
        if schema_version != expected_schema:
            raise HarnessValidationError(
                "TaskPlan stage binding schema does not match its normalized Graph",
                code="task_plan_stage_binding_schema_mismatch",
                details={
                    "schema_version": schema_version,
                    "expected_schema_version": expected_schema,
                },
            )
        object.__setattr__(self, "schema_version", schema_version)
        stage_id = identifier(self.stage_id, "stage_id")
        matches = tuple(
            node
            for node in self.graph.nodes
            if isinstance(node, HarnessExecutableNode) and node.step_id == stage_id
        )
        if len(matches) != 1:
            raise HarnessValidationError(
                "TaskPlan stage binding requires one executable Graph stage",
                code="task_plan_stage_binding_missing",
                details={"stage_id": stage_id, "matches": len(matches)},
            )
        node = matches[0]
        if node.metadata.get("worker_type") not in {
            HarnessWorkerType.TASK_PLAN.value, HarnessWorkerType.AGENT_LOOP.value,
        }:
            raise HarnessValidationError(
                "TaskPlan stage binding requires a TASK_PLAN or declared AGENT_LOOP worker",
                code="dynamic_task_plan_worker_type_mismatch",
                details={"stage_id": stage_id, "node_id": node.node_id},
            )
        if (
            self.graph.schema_version == GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA
            and node.metadata.get("binding_source") != "graph_definition"
        ):
            raise HarnessValidationError(
                "Graph-only TaskPlan stage must come from its frozen definition binding",
                code="graph_task_plan_binding_source_invalid",
                details={"stage_id": stage_id, "node_id": node.node_id},
            )
        declaration = node.metadata.get("step_metadata")
        if node.metadata.get("worker_type") == HarnessWorkerType.AGENT_LOOP.value and (
            not isinstance(declaration, Mapping) or declaration.get("agent_loop_delegation") is not True
        ):
            raise HarnessValidationError(
                "AgentLoop TaskPlan authority requires an explicit delegation binding",
                code="graph_agent_delegation_binding_missing",
            )
        if not isinstance(declaration, Mapping) or declaration.get("dynamic_stage") is not True:
            raise HarnessValidationError(
                "TaskPlan stage binding requires an explicit dynamic stage declaration",
                code="dynamic_task_plan_stage_marker_missing",
                details={"stage_id": stage_id, "node_id": node.node_id},
            )
        if node.side_effect_ref is not None:
            raise HarnessValidationError(
                "dynamic TaskPlan stage cannot own a side-effect handler",
                code="dynamic_task_plan_side_effect_forbidden",
                details={"stage_id": stage_id, "node_id": node.node_id},
            )

        try:
            policy_ref = exact_reference(
                declaration.get("task_plan_policy_ref"),
                "task_plan_policy_ref",
            )
        except (HarnessValidationError, TypeError) as exc:
            raise HarnessValidationError(
                "dynamic TaskPlan stage requires an exact policy ref",
                code="dynamic_task_plan_policy_missing_or_inexact",
                details={"stage_id": stage_id},
            ) from exc

        task_plan_schema = declaration.get("task_plan_schema")
        try:
            DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
                TaskPlanContractKind.VALIDATED_PLAN,
                str(task_plan_schema),
            )
        except (HarnessValidationError, TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "dynamic TaskPlan stage requires an exact executable schema",
                code="dynamic_task_plan_schema_missing_or_inexact",
                details={"stage_id": stage_id, "schema": str(task_plan_schema)},
            ) from exc
        expected_task_plan_schema = GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA
        if task_plan_schema != expected_task_plan_schema:
            raise HarnessValidationError(
                "dynamic TaskPlan schema does not match its Graph identity",
                code="dynamic_task_plan_schema_identity_mismatch",
                details={
                    "stage_id": stage_id,
                    "schema": str(task_plan_schema),
                    "expected_schema": expected_task_plan_schema,
                },
            )

        raw_roles = declaration.get("required_output_roles")
        if (
            not isinstance(raw_roles, (tuple, list))
            or not raw_roles
            or any(not isinstance(item, str) or not item.strip() for item in raw_roles)
            or len(set(raw_roles)) != len(raw_roles)
        ):
            raise HarnessValidationError(
                "dynamic TaskPlan stage requires non-empty unique output roles",
                code="dynamic_task_plan_required_roles_invalid",
                details={"stage_id": stage_id},
            )
        roles = tuple(sorted(item.strip() for item in raw_roles))

        support = declaration.get("task_plan_support")
        if not isinstance(support, Mapping):
            raise HarnessValidationError(
                "dynamic TaskPlan stage support declaration is missing",
                code="dynamic_task_plan_support_missing",
                details={"stage_id": stage_id},
            )
        normalized_support: dict[str, str] = {}
        missing: list[str] = []
        inexact: list[str] = []
        for name in _SUPPORT_REFERENCE_FIELDS:
            value = support.get(name)
            if not isinstance(value, str) or not value.strip():
                missing.append(name)
                continue
            try:
                normalized_support[name] = exact_reference(value, name)
            except HarnessValidationError:
                inexact.append(name)
        event_schema = support.get("event_schema")
        if not isinstance(event_schema, str) or not event_schema.strip():
            missing.append("event_schema")
        elif _EXACT_SCHEMA_PATTERN.fullmatch(event_schema) is None:
            inexact.append("event_schema")
        else:
            normalized_support["event_schema"] = event_schema
        if missing:
            raise HarnessValidationError(
                "dynamic TaskPlan support declarations are incomplete",
                code="dynamic_task_plan_support_incomplete",
                details={"stage_id": stage_id, "missing": sorted(missing)},
            )
        if inexact:
            raise HarnessValidationError(
                "dynamic TaskPlan support declarations must pin exact versions",
                code="dynamic_task_plan_support_inexact",
                details={"stage_id": stage_id, "inexact": sorted(inexact)},
            )
        expected_event_schema = TASK_PLAN_EVENT_SCHEMA_V2
        if normalized_support["event_schema"] != expected_event_schema:
            raise HarnessValidationError(
                "dynamic TaskPlan event schema does not match its Graph identity",
                code="dynamic_task_plan_event_schema_identity_mismatch",
                details={
                    "stage_id": stage_id,
                    "schema": normalized_support["event_schema"],
                    "expected_schema": expected_event_schema,
                },
            )
        if self.graph.checksum is None:
            raise HarnessValidationError(
                "TaskPlan stage binding requires a checksummed Graph",
                code="task_plan_stage_graph_checksum_missing",
                details={"stage_id": stage_id},
            )

        object.__setattr__(self, "stage_id", stage_id)
        object.__setattr__(self, "node_id", node.node_id)
        object.__setattr__(self, "step_ref", node.step_ref.exact_ref)
        object.__setattr__(self, "worker_ref", node.worker_ref.exact_ref)
        object.__setattr__(self, "activity_ref", node.activity_ref.exact_ref)
        object.__setattr__(self, "policy_ref", policy_ref)
        object.__setattr__(self, "task_plan_schema", str(task_plan_schema))
        object.__setattr__(self, "required_output_roles", roles)
        object.__setattr__(
            self,
            "support_refs",
            frozen_mapping(
                dict(sorted(normalized_support.items())),
                "task_plan_stage_binding.support_refs",
            ),
        )
        object.__setattr__(
            self,
            "binding_checksum",
            canonical_payload_checksum(self.checksum_projection()),
        )

    @property
    def is_agent_delegation(self) -> bool:
        return any(
            node.node_id == self.node_id
            and node.metadata.get("worker_type") == HarnessWorkerType.AGENT_LOOP.value
            for node in self.graph.nodes if isinstance(node, HarnessExecutableNode)
        )

    @property
    def graph_id(self) -> str:
        return self.graph.graph_id

    @property
    def graph_version(self) -> str:
        return self.graph.identity_version

    @property
    def graph_checksum(self) -> str:
        assert self.graph.checksum is not None
        return self.graph.checksum

    def checksum_projection(self) -> dict[str, Any]:
        projection = {
            "schema_version": self.schema_version,
            "graph_schema_version": self.graph.schema_version,
            "compiler_version": self.graph.compiler_version,
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "graph_checksum": self.graph_checksum,
        }
        assert self.graph.graph_ref is not None
        projection.update(
            {
                "condition_policy_version": self.graph.condition_policy_version,
                "graph_ref": self.graph.graph_ref.exact_ref,
            }
        )
        projection.update(
            {
                "node_id": self.node_id,
                "stage_id": self.stage_id,
                "step_ref": self.step_ref,
                "worker_ref": self.worker_ref,
                "activity_ref": self.activity_ref,
                "policy_ref": self.policy_ref,
                "task_plan_schema": self.task_plan_schema,
                "required_output_roles": list(self.required_output_roles),
                "support_refs": thaw_mapping(self.support_refs),
            }
        )
        return projection

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "binding_checksum": self.binding_checksum,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        graph: NormalizedHarnessGraph,
    ) -> "TaskPlanStageBinding":
        if not isinstance(value, Mapping):
            raise HarnessValidationError(
                "TaskPlan stage binding must be an object",
                code="task_plan_stage_binding_projection_invalid",
            )
        stage_id = value.get("stage_id")
        schema_version = value.get("schema_version")
        if not isinstance(stage_id, str) or not isinstance(schema_version, str):
            raise HarnessValidationError(
                "TaskPlan stage binding identity fields are invalid",
                code="task_plan_stage_binding_projection_invalid",
            )
        restored = cls(
            graph=graph,
            stage_id=stage_id,
            schema_version=schema_version,
        )
        expected = restored.to_dict()
        if set(value) != set(expected):
            raise HarnessValidationError(
                "TaskPlan stage binding fields do not match its schema",
                code="task_plan_stage_binding_projection_invalid",
                details={
                    "missing": sorted(set(expected).difference(value)),
                    "unexpected": sorted(
                        str(item) for item in set(value).difference(expected)
                    ),
                },
            )
        if dict(value) != expected:
            raise HarnessValidationError(
                "TaskPlan stage binding does not match its frozen Graph",
                code="task_plan_stage_binding_checksum_invalid",
            )
        return restored


__all__ = ["TaskPlanStageBinding"]
