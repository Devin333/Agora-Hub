from __future__ import annotations

from collections.abc import Mapping, Sequence as SequenceCollection
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeAlias

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.canonical import (
    exact_reference,
    freeze_json,
    optional_text,
    required_text,
    thaw_json,
)
from framework.harness.graph.conditions import (
    ConditionAll,
    ConditionAny,
    ConditionPredicate,
    HarnessCondition,
    condition_from_dict,
)
from framework.harness.graph.versioning import HARNESS_GRAPH_DSL_SCHEMA


class ParallelAllFailurePolicy(StrEnum):
    FAIL_FAST = "fail_fast"
    WAIT_ALL = "wait_all"
    COMPENSATE = "compensate"


class ParallelAnyCancellationPolicy(StrEnum):
    CANCEL_LOSERS = "cancel_losers"
    WAIT_FOR_LOSERS = "wait_for_losers"


class ParallelAnyFailurePolicy(StrEnum):
    FAIL_ALL = "fail_all"
    COMPENSATE = "compensate"


class WaitKind(StrEnum):
    SIGNAL = "signal"
    TIMER = "timer"
    APPROVAL = "approval"


class WaitTimeoutAction(StrEnum):
    HALT = "halt"
    FAIL = "fail"
    ROUTE = "route"


@dataclass(frozen=True, slots=True)
class StepRef:
    step_id: str
    node_id: str | None = None

    def __post_init__(self) -> None:
        step_id = required_text(self.step_id, "step_ref.step_id")
        node_id = optional_text(self.node_id, "step_ref.node_id") or step_id
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "node_id", node_id)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "step", "step_id": self.step_id, "node_id": self.node_id}


@dataclass(frozen=True, slots=True)
class Pass:
    """Deterministic control-only node that completes as soon as it is ready.

    ``Pass`` is the explicit no-op route for Choice defaults and loop exits:
    it performs no work, produces no output and never dispatches a worker.
    """

    pass_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "pass_id", required_text(self.pass_id, "pass.pass_id"))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "pass", "pass_id": self.pass_id}


@dataclass(frozen=True, slots=True)
class Sequence:
    children: tuple["HarnessGraphExpression", ...]
    sequence_id: str | None = None

    def __post_init__(self) -> None:
        children = _expressions(self.children, "sequence.children", allow_empty=False)
        sequence_id = optional_text(self.sequence_id, "sequence.sequence_id")
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "sequence_id", sequence_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "sequence",
            "sequence_id": self.sequence_id,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class ChoiceBranch:
    branch_id: str
    child: "HarnessGraphExpression"
    priority: int
    condition: HarnessCondition | None = None
    is_default: bool = False
    output_namespace: str | None = None

    def __post_init__(self) -> None:
        branch_id = required_text(self.branch_id, "choice_branch.branch_id")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise HarnessValidationError(
                "choice branch priority must be an integer",
                code="invalid_choice_priority",
                details={"branch_id": branch_id},
            )
        _expression(self.child, "choice_branch.child")
        if self.condition is not None and not _is_condition(self.condition):
            raise HarnessValidationError(
                "choice branch condition must be a HarnessCondition",
                code="invalid_choice_condition",
                details={"branch_id": branch_id},
            )
        if self.is_default and self.condition is not None:
            raise HarnessValidationError(
                "default choice branch cannot declare a condition",
                code="default_choice_has_condition",
                details={"branch_id": branch_id},
            )
        if not self.is_default and self.condition is None:
            raise HarnessValidationError(
                "non-default choice branch requires a condition",
                code="choice_condition_missing",
                details={"branch_id": branch_id},
            )
        object.__setattr__(self, "branch_id", branch_id)
        object.__setattr__(
            self,
            "output_namespace",
            optional_text(self.output_namespace, "choice_branch.output_namespace"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "child": self.child.to_dict(),
            "priority": self.priority,
            "condition": None if self.condition is None else self.condition.to_dict(),
            "is_default": self.is_default,
            "output_namespace": self.output_namespace,
        }


@dataclass(frozen=True, slots=True)
class Choice:
    choice_id: str
    branches: tuple[ChoiceBranch, ...]

    def __post_init__(self) -> None:
        choice_id = required_text(self.choice_id, "choice.choice_id")
        branches = tuple(self.branches)
        if not branches or not all(
            isinstance(branch, ChoiceBranch) for branch in branches
        ):
            raise HarnessValidationError(
                "choice must contain ChoiceBranch values",
                code="invalid_choice_branches",
                details={"choice_id": choice_id},
            )
        object.__setattr__(self, "choice_id", choice_id)
        object.__setattr__(self, "branches", branches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "choice",
            "choice_id": self.choice_id,
            "branches": [branch.to_dict() for branch in self.branches],
        }


@dataclass(frozen=True, slots=True)
class ParallelBranch:
    branch_id: str
    child: "HarnessGraphExpression"
    output_namespace: str

    def __post_init__(self) -> None:
        branch_id = required_text(self.branch_id, "parallel_branch.branch_id")
        _expression(self.child, "parallel_branch.child")
        namespace = required_text(
            self.output_namespace, "parallel_branch.output_namespace"
        )
        object.__setattr__(self, "branch_id", branch_id)
        object.__setattr__(self, "output_namespace", namespace)

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "child": self.child.to_dict(),
            "output_namespace": self.output_namespace,
        }


@dataclass(frozen=True, slots=True)
class PureMerge:
    merge_ref: str
    output_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "merge_ref",
            exact_reference(self.merge_ref, "pure_merge.merge_ref"),
        )
        object.__setattr__(
            self,
            "output_keys",
            _text_tuple(
                self.output_keys,
                "pure_merge.output_keys",
                allow_empty=False,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "pure",
            "merge_ref": self.merge_ref,
            "output_keys": list(self.output_keys),
        }


@dataclass(frozen=True, slots=True)
class VerifiedAggregation:
    step: StepRef
    branch_inputs_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.step, StepRef):
            raise HarnessValidationError(
                "verified aggregation requires a StepRef",
                code="invalid_merge_contract",
            )
        object.__setattr__(
            self,
            "branch_inputs_key",
            required_text(
                self.branch_inputs_key,
                "verified_aggregation.branch_inputs_key",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "aggregation_step",
            "step": self.step.to_dict(),
            "branch_inputs_key": self.branch_inputs_key,
        }


ParallelMerge: TypeAlias = PureMerge | VerifiedAggregation


@dataclass(frozen=True, slots=True)
class ParallelAll:
    fork_id: str
    join_id: str
    branches: tuple[ParallelBranch, ...]
    failure_policy: ParallelAllFailurePolicy | str = ParallelAllFailurePolicy.FAIL_FAST
    merge: ParallelMerge | None = None

    def __post_init__(self) -> None:
        fork_id = required_text(self.fork_id, "parallel_all.fork_id")
        join_id = required_text(self.join_id, "parallel_all.join_id")
        branches = _parallel_branches(self.branches, "parallel_all.branches")
        if self.merge is not None and not isinstance(
            self.merge,
            PureMerge | VerifiedAggregation,
        ):
            raise HarnessValidationError(
                "parallel_all.merge must be PureMerge or VerifiedAggregation",
                code="invalid_merge_contract",
            )
        object.__setattr__(self, "fork_id", fork_id)
        object.__setattr__(self, "join_id", join_id)
        object.__setattr__(self, "branches", branches)
        object.__setattr__(
            self, "failure_policy", ParallelAllFailurePolicy(self.failure_policy)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "parallel_all",
            "fork_id": self.fork_id,
            "join_id": self.join_id,
            "branches": [branch.to_dict() for branch in self.branches],
            "failure_policy": self.failure_policy.value,
            "merge": None if self.merge is None else self.merge.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ParallelAny:
    fork_id: str
    join_id: str
    branches: tuple[ParallelBranch, ...]
    cancellation_policy: ParallelAnyCancellationPolicy | str = (
        ParallelAnyCancellationPolicy.CANCEL_LOSERS
    )
    failure_policy: ParallelAnyFailurePolicy | str = ParallelAnyFailurePolicy.FAIL_ALL

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "fork_id", required_text(self.fork_id, "parallel_any.fork_id")
        )
        object.__setattr__(
            self, "join_id", required_text(self.join_id, "parallel_any.join_id")
        )
        object.__setattr__(
            self,
            "branches",
            _parallel_branches(self.branches, "parallel_any.branches"),
        )
        object.__setattr__(
            self,
            "cancellation_policy",
            ParallelAnyCancellationPolicy(self.cancellation_policy),
        )
        object.__setattr__(
            self, "failure_policy", ParallelAnyFailurePolicy(self.failure_policy)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "parallel_any",
            "fork_id": self.fork_id,
            "join_id": self.join_id,
            "branches": [branch.to_dict() for branch in self.branches],
            "cancellation_policy": self.cancellation_policy.value,
            "failure_policy": self.failure_policy.value,
        }


@dataclass(frozen=True, slots=True)
class BoundedLoop:
    loop_id: str
    body: "HarnessGraphExpression"
    condition: HarnessCondition
    max_iterations: int
    exit: "HarnessGraphExpression | None" = None
    exhaustion: "HarnessGraphExpression | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "loop_id", required_text(self.loop_id, "loop.loop_id"))
        _expression(self.body, "loop.body")
        if not _is_condition(self.condition):
            raise HarnessValidationError(
                "loop condition must be a HarnessCondition",
                code="invalid_loop_condition",
            )
        if not isinstance(self.max_iterations, int) or isinstance(
            self.max_iterations, bool
        ):
            raise HarnessValidationError(
                "loop max_iterations must be an integer",
                code="invalid_loop_bound",
            )
        if self.exit is not None:
            _expression(self.exit, "loop.exit")
        if self.exhaustion is not None:
            _expression(self.exhaustion, "loop.exhaustion")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "bounded_loop",
            "loop_id": self.loop_id,
            "body": self.body.to_dict(),
            "condition": self.condition.to_dict(),
            "max_iterations": self.max_iterations,
            "exit": None if self.exit is None else self.exit.to_dict(),
            "exhaustion": None
            if self.exhaustion is None
            else self.exhaustion.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class WaitTimeoutPolicy:
    action: WaitTimeoutAction | str
    target_node_id: str | None = None

    def __post_init__(self) -> None:
        action = WaitTimeoutAction(self.action)
        target = optional_text(self.target_node_id, "wait_timeout.target_node_id")
        if action == WaitTimeoutAction.ROUTE and target is None:
            raise HarnessValidationError(
                "route timeout policy requires target_node_id",
                code="wait_timeout_target_missing",
            )
        if action != WaitTimeoutAction.ROUTE and target is not None:
            raise HarnessValidationError(
                "non-route timeout policy cannot declare target_node_id",
                code="wait_timeout_target_forbidden",
            )
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "target_node_id", target)

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action.value, "target_node_id": self.target_node_id}


@dataclass(frozen=True, slots=True)
class Wait:
    wait_id: str
    kind: WaitKind | str
    correlation: Mapping[str, Any]
    signal_type: str
    signal_version: str
    tenant_scope_path: str
    identity_scope_path: str
    timeout_policy: WaitTimeoutPolicy | None = None
    deadline_input_path: str | None = None
    resume_node_id: str | None = None

    def __post_init__(self) -> None:
        wait_id = required_text(self.wait_id, "wait.wait_id")
        kind = WaitKind(self.kind)
        correlation = freeze_json(self.correlation, "wait.correlation")
        if not isinstance(correlation, Mapping) or not correlation:
            raise HarnessValidationError(
                "wait correlation must be a non-empty canonical object",
                code="wait_correlation_missing",
                details={"wait_id": wait_id},
            )
        if self.timeout_policy is not None and not isinstance(
            self.timeout_policy, WaitTimeoutPolicy
        ):
            raise HarnessValidationError(
                "wait timeout_policy must be WaitTimeoutPolicy",
                code="invalid_wait_timeout_policy",
            )
        deadline_path = optional_text(
            self.deadline_input_path, "wait.deadline_input_path"
        )
        if kind == WaitKind.TIMER and deadline_path is None:
            raise HarnessValidationError(
                "timer wait requires deadline_input_path",
                code="timer_deadline_missing",
                details={"wait_id": wait_id},
            )
        object.__setattr__(self, "wait_id", wait_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "correlation", correlation)
        object.__setattr__(
            self, "signal_type", required_text(self.signal_type, "wait.signal_type")
        )
        object.__setattr__(
            self,
            "signal_version",
            required_text(self.signal_version, "wait.signal_version"),
        )
        object.__setattr__(
            self,
            "tenant_scope_path",
            required_text(self.tenant_scope_path, "wait.tenant_scope_path"),
        )
        object.__setattr__(
            self,
            "identity_scope_path",
            required_text(self.identity_scope_path, "wait.identity_scope_path"),
        )
        object.__setattr__(self, "deadline_input_path", deadline_path)
        object.__setattr__(
            self,
            "resume_node_id",
            _approval_resume_node_id(
                self.resume_node_id,
                kind=kind,
                correlation=correlation,
                wait_id=wait_id,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "wait",
            "wait_id": self.wait_id,
            "wait_kind": self.kind.value,
            "correlation": thaw_json(self.correlation),
            "signal_type": self.signal_type,
            "signal_version": self.signal_version,
            "tenant_scope_path": self.tenant_scope_path,
            "identity_scope_path": self.identity_scope_path,
            "timeout_policy": (
                None if self.timeout_policy is None else self.timeout_policy.to_dict()
            ),
            "deadline_input_path": self.deadline_input_path,
            # Emitted only when declared so pre-existing Graph checksums stay
            # byte-identical for non re-entrant Waits.
            **(
                {}
                if self.resume_node_id is None
                else {"resume_node_id": self.resume_node_id}
            ),
        }


def _approval_resume_node_id(
    value: str | None,
    *,
    kind: WaitKind,
    correlation: Mapping[str, Any],
    wait_id: str,
) -> str | None:
    resume_node_id = optional_text(value, "wait.resume_node_id")
    if resume_node_id is None:
        return None
    if kind is not WaitKind.APPROVAL:
        raise HarnessValidationError(
            "only approval Waits can re-enter a node with a redeemable grant",
            code="wait_resume_node_requires_approval",
            details={"wait_id": wait_id},
        )
    approval_path = correlation.get("approval_id")
    if not isinstance(approval_path, str) or not approval_path.strip():
        raise HarnessValidationError(
            "re-entrant approval Wait must correlate on approval_id",
            code="wait_resume_approval_id_missing",
            details={"wait_id": wait_id},
        )
    return resume_node_id


@dataclass(frozen=True, slots=True)
class CompensationBinding:
    binding_id: str
    for_node_id: str
    compensation_step_id: str
    handler_ref: str
    activity_contract_ref: str
    scope: str = "node_instance"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "binding_id",
            required_text(self.binding_id, "compensation.binding_id"),
        )
        object.__setattr__(
            self,
            "for_node_id",
            required_text(self.for_node_id, "compensation.for_node_id"),
        )
        object.__setattr__(
            self,
            "compensation_step_id",
            required_text(
                self.compensation_step_id, "compensation.compensation_step_id"
            ),
        )
        object.__setattr__(
            self,
            "handler_ref",
            exact_reference(self.handler_ref, "compensation.handler_ref"),
        )
        object.__setattr__(
            self,
            "activity_contract_ref",
            exact_reference(
                self.activity_contract_ref,
                "compensation.activity_contract_ref",
            ),
        )
        object.__setattr__(
            self, "scope", required_text(self.scope, "compensation.scope")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "for_node_id": self.for_node_id,
            "compensation_step_id": self.compensation_step_id,
            "handler_ref": self.handler_ref,
            "activity_contract_ref": self.activity_contract_ref,
            "scope": self.scope,
        }


HarnessGraphExpression: TypeAlias = (
    StepRef
    | Pass
    | Sequence
    | Choice
    | ParallelAll
    | ParallelAny
    | BoundedLoop
    | Wait
)


@dataclass(frozen=True, slots=True)
class HarnessGraphSpec:
    graph_id: str
    root: HarnessGraphExpression
    compensations: tuple[CompensationBinding, ...] = ()
    input_keys: tuple[str, ...] = ()
    terminal_output_keys: tuple[str, ...] = ()
    schema_version: str = HARNESS_GRAPH_DSL_SCHEMA
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        graph_id = required_text(self.graph_id, "graph.graph_id")
        _expression(self.root, "graph.root")
        compensations = tuple(self.compensations)
        if not all(isinstance(item, CompensationBinding) for item in compensations):
            raise HarnessValidationError(
                "graph compensations must contain CompensationBinding values",
                code="invalid_compensation_contract",
            )
        output_keys = tuple(
            required_text(item, "graph.terminal_output_keys")
            for item in self.terminal_output_keys
        )
        input_keys = tuple(
            required_text(item, "graph.input_keys") for item in self.input_keys
        )
        if len(set(input_keys)) != len(input_keys):
            raise HarnessValidationError(
                "input_keys must not contain duplicates",
                code="duplicate_graph_input_key",
            )
        if len(set(output_keys)) != len(output_keys):
            raise HarnessValidationError(
                "terminal_output_keys must not contain duplicates",
                code="duplicate_terminal_output_key",
            )
        if self.schema_version != HARNESS_GRAPH_DSL_SCHEMA:
            raise HarnessValidationError(
                "unsupported graph DSL schema",
                code="unsupported_graph_schema",
                details={"schema_version": str(self.schema_version)},
            )
        metadata = freeze_json(self.metadata, "graph.metadata")
        if not isinstance(metadata, Mapping):
            raise HarnessValidationError(
                "graph metadata must be an object",
                code="invalid_graph_metadata",
            )
        object.__setattr__(self, "graph_id", graph_id)
        object.__setattr__(self, "compensations", compensations)
        object.__setattr__(self, "input_keys", input_keys)
        object.__setattr__(self, "terminal_output_keys", output_keys)
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "graph_id": self.graph_id,
            "root": self.root.to_dict(),
            "compensations": [binding.to_dict() for binding in self.compensations],
            "input_keys": list(self.input_keys),
            "terminal_output_keys": list(self.terminal_output_keys),
            "metadata": thaw_json(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessGraphSpec":
        payload = dict(value)
        payload.setdefault("input_keys", ())
        _exact_keys(
            payload,
            {
                "schema_version",
                "graph_id",
                "root",
                "compensations",
                "input_keys",
                "terminal_output_keys",
                "metadata",
            },
            "graph",
        )
        raw_compensations = _array(
            payload["compensations"],
            "graph.compensations",
        )
        return cls(
            schema_version=payload["schema_version"],
            graph_id=payload["graph_id"],
            root=expression_from_dict(payload["root"]),
            compensations=tuple(
                _compensation_from_dict(item) for item in raw_compensations
            ),
            input_keys=tuple(_array(payload["input_keys"], "graph.input_keys")),
            terminal_output_keys=tuple(
                _array(
                    payload["terminal_output_keys"],
                    "graph.terminal_output_keys",
                )
            ),
            metadata=payload["metadata"],
        )


def expression_from_dict(value: Mapping[str, Any]) -> HarnessGraphExpression:
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            "graph expression must be an object",
            code="invalid_graph_expression",
        )
    kind = value.get("kind")
    if kind == "step":
        _exact_keys(value, {"kind", "step_id", "node_id"}, "step expression")
        return StepRef(step_id=value["step_id"], node_id=value["node_id"])
    if kind == "pass":
        _exact_keys(value, {"kind", "pass_id"}, "pass expression")
        return Pass(pass_id=value["pass_id"])
    if kind == "sequence":
        _exact_keys(value, {"kind", "sequence_id", "children"}, "sequence expression")
        return Sequence(
            sequence_id=value["sequence_id"],
            children=tuple(
                expression_from_dict(item)
                for item in _array(value["children"], "sequence.children")
            ),
        )
    if kind == "choice":
        _exact_keys(value, {"kind", "choice_id", "branches"}, "choice expression")
        return Choice(
            choice_id=value["choice_id"],
            branches=tuple(
                _choice_branch_from_dict(item)
                for item in _array(value["branches"], "choice.branches")
            ),
        )
    if kind in {"parallel_all", "parallel_any"}:
        common = {"kind", "fork_id", "join_id", "branches"}
        branches = tuple(
            _parallel_branch_from_dict(item)
            for item in _array(value["branches"], f"{kind}.branches")
        )
        if kind == "parallel_all":
            _exact_keys(value, common | {"failure_policy", "merge"}, kind)
            return ParallelAll(
                fork_id=value["fork_id"],
                join_id=value["join_id"],
                branches=branches,
                failure_policy=value["failure_policy"],
                merge=_parallel_merge_from_dict(value["merge"]),
            )
        _exact_keys(value, common | {"cancellation_policy", "failure_policy"}, kind)
        return ParallelAny(
            fork_id=value["fork_id"],
            join_id=value["join_id"],
            branches=branches,
            cancellation_policy=value["cancellation_policy"],
            failure_policy=value["failure_policy"],
        )
    if kind == "bounded_loop":
        _exact_keys(
            value,
            {
                "kind",
                "loop_id",
                "body",
                "condition",
                "max_iterations",
                "exit",
                "exhaustion",
            },
            "bounded_loop expression",
        )
        return BoundedLoop(
            loop_id=value["loop_id"],
            body=expression_from_dict(value["body"]),
            condition=condition_from_dict(value["condition"]),
            max_iterations=value["max_iterations"],
            exit=None if value["exit"] is None else expression_from_dict(value["exit"]),
            exhaustion=(
                None
                if value["exhaustion"] is None
                else expression_from_dict(value["exhaustion"])
            ),
        )
    if kind == "wait":
        wait_fields = {
            "kind",
            "wait_id",
            "wait_kind",
            "correlation",
            "signal_type",
            "signal_version",
            "tenant_scope_path",
            "identity_scope_path",
            "timeout_policy",
            "deadline_input_path",
        }
        if "resume_node_id" in value:
            wait_fields.add("resume_node_id")
        _exact_keys(value, wait_fields, "wait expression")
        timeout = value["timeout_policy"]
        return Wait(
            wait_id=value["wait_id"],
            kind=value["wait_kind"],
            correlation=value["correlation"],
            signal_type=value["signal_type"],
            signal_version=value["signal_version"],
            tenant_scope_path=value["tenant_scope_path"],
            identity_scope_path=value["identity_scope_path"],
            timeout_policy=None
            if timeout is None
            else _wait_timeout_from_dict(timeout),
            deadline_input_path=value["deadline_input_path"],
            resume_node_id=value.get("resume_node_id"),
        )
    raise HarnessValidationError(
        "unsupported graph expression kind",
        code="unsupported_graph_node_kind",
        details={"kind": str(kind)},
    )


def _choice_branch_from_dict(value: Mapping[str, Any]) -> ChoiceBranch:
    _exact_keys(
        value,
        {
            "branch_id",
            "child",
            "priority",
            "condition",
            "is_default",
            "output_namespace",
        },
        "choice branch",
    )
    return ChoiceBranch(
        branch_id=value["branch_id"],
        child=expression_from_dict(value["child"]),
        priority=value["priority"],
        condition=(
            None
            if value["condition"] is None
            else condition_from_dict(value["condition"])
        ),
        is_default=value["is_default"],
        output_namespace=value["output_namespace"],
    )


def _parallel_branch_from_dict(value: Mapping[str, Any]) -> ParallelBranch:
    _exact_keys(value, {"branch_id", "child", "output_namespace"}, "parallel branch")
    return ParallelBranch(
        branch_id=value["branch_id"],
        child=expression_from_dict(value["child"]),
        output_namespace=value["output_namespace"],
    )


def _parallel_merge_from_dict(value: Any) -> ParallelMerge | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            "parallel merge must be an object",
            code="invalid_merge_contract",
        )
    kind = value.get("kind")
    if kind == "pure":
        _exact_keys(value, {"kind", "merge_ref", "output_keys"}, "pure merge")
        return PureMerge(
            merge_ref=value["merge_ref"],
            output_keys=tuple(_array(value["output_keys"], "pure_merge.output_keys")),
        )
    if kind == "aggregation_step":
        _exact_keys(
            value,
            {"kind", "step", "branch_inputs_key"},
            "verified aggregation",
        )
        step = expression_from_dict(value["step"])
        if not isinstance(step, StepRef):
            raise HarnessValidationError(
                "verified aggregation requires a StepRef",
                code="invalid_merge_contract",
            )
        return VerifiedAggregation(
            step=step,
            branch_inputs_key=value["branch_inputs_key"],
        )
    raise HarnessValidationError(
        "unsupported parallel merge kind",
        code="invalid_merge_contract",
        details={"kind": str(kind)},
    )


def _wait_timeout_from_dict(value: Mapping[str, Any]) -> WaitTimeoutPolicy:
    _exact_keys(value, {"action", "target_node_id"}, "wait timeout")
    return WaitTimeoutPolicy(
        action=value["action"], target_node_id=value["target_node_id"]
    )


def _compensation_from_dict(value: Mapping[str, Any]) -> CompensationBinding:
    _exact_keys(
        value,
        {
            "binding_id",
            "for_node_id",
            "compensation_step_id",
            "handler_ref",
            "activity_contract_ref",
            "scope",
        },
        "compensation binding",
    )
    return CompensationBinding(
        binding_id=value["binding_id"],
        for_node_id=value["for_node_id"],
        compensation_step_id=value["compensation_step_id"],
        handler_ref=value["handler_ref"],
        activity_contract_ref=value["activity_contract_ref"],
        scope=value["scope"],
    )


def _expression(value: Any, field_name: str) -> HarnessGraphExpression:
    if not isinstance(
        value,
        StepRef
        | Pass
        | Sequence
        | Choice
        | ParallelAll
        | ParallelAny
        | BoundedLoop
        | Wait,
    ):
        raise HarnessValidationError(
            f"{field_name} must be a graph expression",
            code="invalid_graph_expression",
            details={"field": field_name},
        )
    return value


def _expressions(
    values: tuple[HarnessGraphExpression, ...],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[HarnessGraphExpression, ...]:
    expressions = tuple(values)
    if not allow_empty and not expressions:
        raise HarnessValidationError(
            f"{field_name} must not be empty",
            code="empty_graph_expression",
        )
    for value in expressions:
        _expression(value, field_name)
    return expressions


def _parallel_branches(
    values: tuple[ParallelBranch, ...],
    field_name: str,
) -> tuple[ParallelBranch, ...]:
    branches = tuple(values)
    if not branches or not all(
        isinstance(branch, ParallelBranch) for branch in branches
    ):
        raise HarnessValidationError(
            f"{field_name} must contain ParallelBranch values",
            code="invalid_parallel_branches",
        )
    return branches


def _is_condition(value: Any) -> bool:
    return isinstance(value, ConditionPredicate | ConditionAll | ConditionAny)


def _text_tuple(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    values = tuple(
        required_text(item, field_name)
        for item in _array(value, field_name)
    )
    if not allow_empty and not values:
        raise HarnessValidationError(
            f"{field_name} must not be empty",
            code="invalid_merge_contract",
        )
    if len(values) != len(set(values)):
        raise HarnessValidationError(
            f"{field_name} must contain unique values",
            code="invalid_merge_contract",
        )
    return tuple(sorted(values))


def _array(value: Any, field_name: str) -> tuple[Any, ...]:
    if not isinstance(value, SequenceCollection) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise HarnessValidationError(
            f"{field_name} must be an array",
            code="invalid_graph_contract",
        )
    return tuple(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            f"{field_name} must be an object",
            code="invalid_graph_contract",
        )
    actual = set(value)
    if actual != expected:
        raise HarnessValidationError(
            f"{field_name} fields do not match its schema",
            code="invalid_graph_contract",
            details={
                "missing": sorted(expected.difference(actual)),
                "unknown": sorted(str(item) for item in actual.difference(expected)),
            },
        )


__all__ = [
    "BoundedLoop",
    "Choice",
    "ChoiceBranch",
    "CompensationBinding",
    "HarnessGraphExpression",
    "HarnessGraphSpec",
    "ParallelAll",
    "ParallelAllFailurePolicy",
    "ParallelAny",
    "ParallelAnyCancellationPolicy",
    "ParallelAnyFailurePolicy",
    "ParallelBranch",
    "ParallelMerge",
    "Pass",
    "PureMerge",
    "Sequence",
    "StepRef",
    "VerifiedAggregation",
    "Wait",
    "WaitKind",
    "WaitTimeoutAction",
    "WaitTimeoutPolicy",
    "expression_from_dict",
]
