from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from framework.agent.models import (
    AgentLoopIssue,
    AgentLoopResult,
    AgentLoopStatus,
    AgentLoopStopReason,
    AgentSpec,
)
from framework.events.canonical import checksum_for
from framework.harness.agent_loop.artifacts import (
    AgentLoopGraphArtifactContext,
    AgentLoopGraphArtifactRecorder,
)
from framework.harness.control_plane.gate_registry import (
    DeterministicGateRegistry,
    GateReference,
    GateRegistration,
)
from framework.harness.control_plane.gates import (
    DeterministicGate,
    GateContext,
    HarnessGateResult,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.activity import (
    HarnessLeafActivityKind,
    HarnessStepSpec,
    HarnessWorkerType,
)
from framework.harness.graph.bindings import (
    HarnessActivityCapabilities,
    HarnessActivityContractBinding,
    HarnessActivityUsage,
    HarnessLeafActivityBinding,
    HarnessRuntimeBindingAuthority,
    HarnessWorkerBinding,
)
from framework.harness.graph.canonical import (
    freeze_json,
    mapping_to_dict,
)
from framework.harness.graph.conditions import ConditionPredicate
from framework.harness.graph.dsl import Wait
from framework.harness.graph.model import (
    HarnessContractKind,
    HarnessContractReference,
)
from framework.harness.side_effects.registry import HarnessSideEffectRegistry
from framework.harness.control_plane.activity_execution import (
    HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
    HarnessGraphActivityTaskContext,
)
from framework.harness.workers.result import (
    HarnessWorkerEvidence,
    HarnessWorkerResult,
    HarnessWorkerStatus,
)
from framework.shared.redaction import redact_sensitive_values
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.harness.ref_snapshot import RefAuthoritySnapshot


AGENT_LOOP_GRAPH_ACTIVITY_TASK_SCHEMA = (
    "newsroom.agent-loop-graph-activity-task/v1"
)
AGENT_LOOP_GRAPH_ACTIVITY_OUTPUT_SCHEMA = (
    "newsroom.agent-loop-graph-activity-output/v2"
)
AGENT_LOOP_GRAPH_APPROVAL_REQUEST_SCHEMA = (
    "newsroom.agent-loop-graph-approval-request/v1"
)
AGENT_LOOP_GRAPH_WAIT_CANDIDATE_SCHEMA = (
    "newsroom.agent-loop-graph-wait-candidate/v2"
)
AGENT_LOOP_GRAPH_APPROVAL_WAIT_FACT_SCHEMA = (
    "newsroom.agent-loop-graph-approval-wait-fact/v1"
)
AGENT_LOOP_GRAPH_WAIT_EVIDENCE_TYPE = "agent_loop_graph_wait_candidate"
AGENT_LOOP_GRAPH_WAIT_GATE_REF = "agent_loop_wait_candidate@1"
AGENT_LOOP_GRAPH_APPROVAL_SIGNAL_TYPE = "newsroom.agent-loop.approval"
AGENT_LOOP_GRAPH_APPROVAL_SIGNAL_VERSION = "1"
AGENT_LOOP_GRAPH_APPROVAL_WAIT_BINDING_SCHEMA = (
    "newsroom.agent-loop-graph-approval-wait-binding/v1"
)
AGENT_LOOP_GRAPH_BINDING_MANIFEST_SCHEMA = (
    "newsroom.agent-loop-graph-activity-binding/v2"
)


class AgentLoopRunnerPort(Protocol):
    def run(
        self,
        agent: AgentSpec,
        inputs: dict[str, Any],
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
        node_instance_id: str | None = None,
        graph_checkpoint_ref: str | None = None,
        resume_from_cursor: bool = False,
        memory_recall: ExecutionMemoryRecallPort | None = None,
    ) -> AgentLoopResult: ...


@runtime_checkable
class AgentLoopInputAdmissionPort(Protocol):
    def admit_parent_inputs(self, task: Mapping[str, Any], *, agent: AgentSpec) -> RefAuthoritySnapshot: ...

    def parent_memory_recall(self, snapshot: RefAuthoritySnapshot) -> ExecutionMemoryRecallPort: ...


@dataclass(frozen=True, slots=True)
class AgentLoopGraphActivityTask:
    """Strict worker input whose Graph identity comes only from Harness."""

    inputs: Mapping[str, Any]
    conversation_id: str | None
    resume_from_cursor: bool
    task_context: HarnessGraphActivityTaskContext
    schema_version: str = AGENT_LOOP_GRAPH_ACTIVITY_TASK_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.inputs, Mapping):
            raise _activity_error("AgentLoop inputs must be an object")
        inputs = freeze_json(dict(self.inputs), "$.agent_loop_graph_task.inputs")
        if not isinstance(inputs, Mapping):  # pragma: no cover - guarded above
            raise AssertionError("canonical AgentLoop inputs must remain a mapping")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(
            self,
            "conversation_id",
            _optional_text(self.conversation_id, "conversation_id"),
        )
        if not isinstance(self.resume_from_cursor, bool):
            raise _activity_error("resume_from_cursor must be a boolean")
        if self.resume_from_cursor and self.conversation_id is None:
            raise _activity_error(
                "cursor resume requires an explicit conversation_id"
            )
        if not isinstance(self.task_context, HarnessGraphActivityTaskContext):
            raise TypeError(
                "task_context must be HarnessGraphActivityTaskContext"
            )
        if self.schema_version != AGENT_LOOP_GRAPH_ACTIVITY_TASK_SCHEMA:
            raise HarnessValidationError(
                "unsupported AgentLoop Graph activity task schema",
                code="unsupported_agent_loop_graph_activity_schema",
            )

    @classmethod
    def from_worker_task(
        cls,
        value: Mapping[str, Any],
    ) -> AgentLoopGraphActivityTask:
        expected = {
            "schema_version",
            "inputs",
            "conversation_id",
            "resume_from_cursor",
            HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
        }
        if set(value) == expected:
            payload = _exact_mapping(value, expected, "activity task")
            raw_context = payload.pop(HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY)
            if not isinstance(raw_context, Mapping):
                raise _activity_error("Harness Graph activity context must be an object")
            return cls(
                inputs=payload["inputs"],
                conversation_id=payload["conversation_id"],
                resume_from_cursor=payload["resume_from_cursor"],
                task_context=HarnessGraphActivityTaskContext.from_dict(raw_context),
                schema_version=payload["schema_version"],
            )

        graph_task_fields = {
            "run_id",
            "step_id",
            "worker_type",
            "inputs",
            "metadata",
            HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
        }
        payload = _exact_mapping(value, graph_task_fields, "Graph activity task")
        raw_context = payload[HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY]
        if not isinstance(raw_context, Mapping):
            raise _activity_error("Harness Graph activity context must be an object")
        context = HarnessGraphActivityTaskContext.from_dict(raw_context)
        if (
            payload["run_id"] != context.activity.run_id
            or payload["step_id"] != context.activity.node_id
            or payload["worker_type"] != HarnessWorkerType.AGENT_LOOP.value
            or not isinstance(payload["metadata"], Mapping)
        ):
            raise _activity_error("Graph AgentLoop task conflicts with Harness context")
        task_inputs = payload["inputs"]
        if not isinstance(task_inputs, Mapping):
            raise _activity_error("Graph AgentLoop task inputs must be an object")
        exact_inputs = _exact_mapping(
            task_inputs,
            {"inputs", "conversation_id", "resume_from_cursor"},
            "Graph AgentLoop task inputs",
        )
        return cls(
            inputs=exact_inputs["inputs"],
            conversation_id=exact_inputs["conversation_id"],
            resume_from_cursor=exact_inputs["resume_from_cursor"],
            task_context=context,
        )


@dataclass(frozen=True, slots=True, order=True)
class AgentLoopGraphApprovalRequest:
    approval_id: str
    approval_kind: str
    tool_name: str
    control_action: str | None = None
    escalation_type: str | None = None
    schema_version: str = AGENT_LOOP_GRAPH_APPROVAL_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        for field_name in ("approval_id", "approval_kind", "tool_name"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        for field_name in ("control_action", "escalation_type"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        if self.schema_version != AGENT_LOOP_GRAPH_APPROVAL_REQUEST_SCHEMA:
            raise HarnessValidationError(
                "unsupported AgentLoop Graph approval request schema",
                code="unsupported_agent_loop_graph_activity_schema",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "approval_kind": self.approval_kind,
            "tool_name": self.tool_name,
            "control_action": self.control_action,
            "escalation_type": self.escalation_type,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> AgentLoopGraphApprovalRequest:
        return cls(
            **_exact_mapping(
                value,
                {
                    "schema_version",
                    "approval_id",
                    "approval_kind",
                    "tool_name",
                    "control_action",
                    "escalation_type",
                },
                "approval request",
            )
        )


@dataclass(frozen=True, slots=True)
class AgentLoopGraphWaitCandidate:
    """Candidate evidence only; Harness owns durable Wait registration."""

    run_id: str
    graph_id: str
    graph_version: str
    graph_checksum: str
    node_id: str
    node_instance_id: str
    activity_id: str
    activity_attempt: int
    graph_checkpoint_ref: str
    task_context_checksum: str
    tenant_scope_ref: str
    identity_scope_ref: str
    agent_id: str
    conversation_id: str | None
    approval_requests: tuple[AgentLoopGraphApprovalRequest, ...]
    schema_version: str = AGENT_LOOP_GRAPH_WAIT_CANDIDATE_SCHEMA
    candidate_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "run_id",
            "graph_id",
            "graph_version",
            "node_id",
            "node_instance_id",
            "activity_id",
            "graph_checkpoint_ref",
            "agent_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if (
            isinstance(self.activity_attempt, bool)
            or not isinstance(self.activity_attempt, int)
            or self.activity_attempt < 1
        ):
            raise _wait_error("activity_attempt must be a positive integer")
        object.__setattr__(
            self,
            "graph_checksum",
            _checksum(self.graph_checksum, "graph_checksum"),
        )
        object.__setattr__(
            self,
            "task_context_checksum",
            _checksum(self.task_context_checksum, "task_context_checksum"),
        )
        for field_name in ("tenant_scope_ref", "identity_scope_ref"):
            object.__setattr__(
                self,
                field_name,
                _checksum(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "conversation_id",
            _optional_text(self.conversation_id, "conversation_id"),
        )
        requests = tuple(self.approval_requests)
        if not requests or not all(
            isinstance(item, AgentLoopGraphApprovalRequest) for item in requests
        ):
            raise _wait_error(
                "waiting candidate requires typed approval requests"
            )
        canonical = tuple(
            sorted(
                requests,
                key=lambda item: (
                    item.approval_id,
                    item.approval_kind,
                    item.tool_name,
                ),
            )
        )
        identities = tuple(
            (item.approval_id, item.approval_kind) for item in canonical
        )
        if canonical != requests or len(identities) != len(set(identities)):
            raise _wait_error(
                "waiting candidate approval requests must be canonical and unique"
            )
        object.__setattr__(self, "approval_requests", requests)
        if self.schema_version != AGENT_LOOP_GRAPH_WAIT_CANDIDATE_SCHEMA:
            raise HarnessValidationError(
                "unsupported AgentLoop Graph waiting candidate schema",
                code="unsupported_agent_loop_graph_activity_schema",
            )
        object.__setattr__(
            self,
            "candidate_checksum",
            checksum_for(self.checksum_projection()),
        )

    @classmethod
    def from_result(
        cls,
        *,
        task: AgentLoopGraphActivityTask,
        agent_id: str,
        result: AgentLoopResult,
    ) -> AgentLoopGraphWaitCandidate:
        if result.status is not AgentLoopStatus.WAITING_FOR_APPROVAL:
            raise _wait_error(
                "waiting candidate requires a waiting AgentLoop result"
            )
        if (
            result.diagnostics is None
            or result.diagnostics.status
            is not AgentLoopStatus.WAITING_FOR_APPROVAL
            or result.diagnostics.stop_reason
            is not AgentLoopStopReason.TOOL_APPROVAL_REQUIRED
        ):
            raise _wait_error(
                "waiting AgentLoop result lacks deterministic approval diagnostics"
            )
        requests = tuple(
            sorted(
                (
                    _approval_request_from_issue(issue)
                    for issue in result.diagnostics.issues
                    if issue.code == "tool_approval_required"
                ),
                key=lambda item: (
                    item.approval_id,
                    item.approval_kind,
                    item.tool_name,
                ),
            )
        )
        activity = task.task_context.activity
        return cls(
            run_id=activity.run_id,
            graph_id=activity.graph_ref.graph_id,
            graph_version=activity.graph_ref.identity_version,
            graph_checksum=activity.graph_ref.checksum,
            node_id=activity.node_id,
            node_instance_id=activity.node_instance_id,
            activity_id=activity.activity_id,
            activity_attempt=activity.attempt,
            graph_checkpoint_ref=task.task_context.graph_checkpoint_ref,
            task_context_checksum=task.task_context.context_checksum,
            tenant_scope_ref=activity.tenant_scope_ref,
            identity_scope_ref=activity.identity_scope_ref,
            agent_id=agent_id,
            conversation_id=task.conversation_id,
            approval_requests=requests,
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "graph_checksum": self.graph_checksum,
            "node_id": self.node_id,
            "node_instance_id": self.node_instance_id,
            "activity_id": self.activity_id,
            "activity_attempt": self.activity_attempt,
            "graph_checkpoint_ref": self.graph_checkpoint_ref,
            "task_context_checksum": self.task_context_checksum,
            "tenant_scope_ref": self.tenant_scope_ref,
            "identity_scope_ref": self.identity_scope_ref,
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
            "approval_requests": [
                item.to_dict() for item in self.approval_requests
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "candidate_checksum": self.candidate_checksum,
        }

    def worker_evidence(self) -> HarnessWorkerEvidence:
        return HarnessWorkerEvidence(
            evidence_type=AGENT_LOOP_GRAPH_WAIT_EVIDENCE_TYPE,
            payload=self.to_dict(),
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> AgentLoopGraphWaitCandidate:
        payload = _exact_mapping(
            value,
            {
                "schema_version",
                "run_id",
                "graph_id",
                "graph_version",
                "graph_checksum",
                "node_id",
                "node_instance_id",
                "activity_id",
                "activity_attempt",
                "graph_checkpoint_ref",
                "task_context_checksum",
                "tenant_scope_ref",
                "identity_scope_ref",
                "agent_id",
                "conversation_id",
                "approval_requests",
                "candidate_checksum",
            },
            "waiting candidate",
        )
        supplied_checksum = payload.pop("candidate_checksum")
        raw_requests = payload.get("approval_requests")
        if not isinstance(raw_requests, list):
            raise _wait_error("approval_requests must be an array")
        payload["approval_requests"] = tuple(
            AgentLoopGraphApprovalRequest.from_dict(item)
            for item in raw_requests
        )
        candidate = cls(**payload)
        if supplied_checksum != candidate.candidate_checksum:
            raise _wait_error("waiting candidate checksum does not match")
        return candidate


@dataclass(frozen=True, slots=True)
class AgentLoopGraphApprovalWaitFact:
    """Bounded control fact consumed only by an explicit Graph approval Wait."""

    candidate_checksum: str
    approval_id: str
    approval_kind: str
    tool_name: str
    approval_request_checksum: str
    graph_checkpoint_ref: str
    tenant_scope_ref: str
    identity_scope_ref: str
    schema_version: str = AGENT_LOOP_GRAPH_APPROVAL_WAIT_FACT_SCHEMA
    correlation_ref: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "candidate_checksum",
            "approval_request_checksum",
            "tenant_scope_ref",
            "identity_scope_ref",
        ):
            object.__setattr__(
                self,
                field_name,
                _checksum(getattr(self, field_name), field_name),
            )
        for field_name in (
            "approval_id",
            "approval_kind",
            "tool_name",
            "graph_checkpoint_ref",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if self.schema_version != AGENT_LOOP_GRAPH_APPROVAL_WAIT_FACT_SCHEMA:
            raise HarnessValidationError(
                "unsupported AgentLoop Graph approval Wait fact schema",
                code="unsupported_agent_loop_graph_activity_schema",
            )
        object.__setattr__(
            self,
            "correlation_ref",
            checksum_for(self.correlation_projection()),
        )

    @classmethod
    def from_candidate(
        cls,
        candidate: AgentLoopGraphWaitCandidate,
    ) -> AgentLoopGraphApprovalWaitFact:
        if not isinstance(candidate, AgentLoopGraphWaitCandidate):
            raise TypeError("candidate must be AgentLoopGraphWaitCandidate")
        if len(candidate.approval_requests) != 1:
            raise _wait_error(
                "Graph approval Wait requires exactly one approval request"
            )
        request = candidate.approval_requests[0]
        return cls(
            candidate_checksum=candidate.candidate_checksum,
            approval_id=request.approval_id,
            approval_kind=request.approval_kind,
            tool_name=request.tool_name,
            approval_request_checksum=checksum_for(request.to_dict()),
            graph_checkpoint_ref=candidate.graph_checkpoint_ref,
            tenant_scope_ref=candidate.tenant_scope_ref,
            identity_scope_ref=candidate.identity_scope_ref,
        )

    def correlation_projection(self) -> dict[str, str]:
        return {
            "approval_id": self.approval_id,
            "approval_kind": self.approval_kind,
            "tool_name": self.tool_name,
            "approval_request_checksum": self.approval_request_checksum,
            "candidate_checksum": self.candidate_checksum,
            "graph_checkpoint_ref": self.graph_checkpoint_ref,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            **self.correlation_projection(),
            "tenant_scope_ref": self.tenant_scope_ref,
            "identity_scope_ref": self.identity_scope_ref,
            "correlation_ref": self.correlation_ref,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> AgentLoopGraphApprovalWaitFact:
        payload = _exact_mapping(
            value,
            {
                "schema_version",
                "candidate_checksum",
                "approval_id",
                "approval_kind",
                "tool_name",
                "approval_request_checksum",
                "graph_checkpoint_ref",
                "tenant_scope_ref",
                "identity_scope_ref",
                "correlation_ref",
            },
            "approval Wait fact",
        )
        supplied_correlation = payload.pop("correlation_ref")
        fact = cls(**payload)
        if supplied_correlation != fact.correlation_ref:
            raise _wait_error("approval Wait correlation checksum does not match")
        return fact


@dataclass(frozen=True, slots=True)
class AgentLoopGraphActivityOutput:
    task_context_checksum: str
    agent_id: str
    conversation_id: str | None
    result: Mapping[str, Any]
    artifact_refs: tuple[str, ...]
    artifact_receipt_checksum: str
    approval_wait: AgentLoopGraphApprovalWaitFact | None = None
    schema_version: str = AGENT_LOOP_GRAPH_ACTIVITY_OUTPUT_SCHEMA
    waiting: bool = field(init=False)
    output_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_context_checksum",
            _checksum(self.task_context_checksum, "task_context_checksum"),
        )
        object.__setattr__(self, "agent_id", _required_text(self.agent_id, "agent_id"))
        object.__setattr__(
            self,
            "conversation_id",
            _optional_text(self.conversation_id, "conversation_id"),
        )
        if not isinstance(self.result, Mapping):
            raise _activity_error("AgentLoop result projection must be an object")
        result = freeze_json(
            dict(self.result),
            "$.agent_loop_graph_activity_output.result",
        )
        if not isinstance(result, Mapping):  # pragma: no cover - guarded above
            raise AssertionError("canonical AgentLoop result must remain a mapping")
        object.__setattr__(self, "result", result)
        refs = tuple(_required_text(item, "artifact_ref") for item in self.artifact_refs)
        if len(refs) != len(set(refs)):
            raise _activity_error("AgentLoop artifact refs must be unique")
        object.__setattr__(self, "artifact_refs", refs)
        object.__setattr__(
            self,
            "artifact_receipt_checksum",
            _checksum(
                self.artifact_receipt_checksum,
                "artifact_receipt_checksum",
            ),
        )
        if self.approval_wait is not None and not isinstance(
            self.approval_wait,
            AgentLoopGraphApprovalWaitFact,
        ):
            raise TypeError(
                "approval_wait must be AgentLoopGraphApprovalWaitFact"
            )
        waiting = self.approval_wait is not None
        if (
            result.get("status") == AgentLoopStatus.WAITING_FOR_APPROVAL.value
        ) != waiting:
            raise _activity_error(
                "AgentLoop result status and approval Wait fact do not match"
            )
        object.__setattr__(self, "waiting", waiting)
        if self.schema_version != AGENT_LOOP_GRAPH_ACTIVITY_OUTPUT_SCHEMA:
            raise HarnessValidationError(
                "unsupported AgentLoop Graph activity output schema",
                code="unsupported_agent_loop_graph_activity_schema",
            )
        object.__setattr__(
            self,
            "output_checksum",
            checksum_for(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_context_checksum": self.task_context_checksum,
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
            "result": mapping_to_dict(self.result),
            "artifact_refs": list(self.artifact_refs),
            "artifact_receipt_checksum": self.artifact_receipt_checksum,
            "approval_wait": (
                None
                if self.approval_wait is None
                else self.approval_wait.to_dict()
            ),
            "waiting": self.waiting,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "output_checksum": self.output_checksum,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> AgentLoopGraphActivityOutput:
        payload = _exact_mapping(
            value,
            {
                "schema_version",
                "task_context_checksum",
                "agent_id",
                "conversation_id",
                "result",
                "artifact_refs",
                "artifact_receipt_checksum",
                "approval_wait",
                "waiting",
                "output_checksum",
            },
            "activity output",
        )
        supplied_checksum = payload.pop("output_checksum")
        supplied_waiting = payload.pop("waiting")
        if not isinstance(supplied_waiting, bool):
            raise _activity_error("waiting must be a boolean")
        raw_refs = payload.get("artifact_refs")
        if not isinstance(raw_refs, list):
            raise _activity_error("artifact_refs must be an array")
        payload["artifact_refs"] = tuple(raw_refs)
        raw_wait = payload.get("approval_wait")
        if raw_wait is not None:
            if not isinstance(raw_wait, Mapping):
                raise _activity_error("approval_wait must be an object")
            payload["approval_wait"] = AgentLoopGraphApprovalWaitFact.from_dict(
                raw_wait
            )
        output = cls(**payload)
        if (
            supplied_waiting != output.waiting
            or supplied_checksum != output.output_checksum
        ):
            raise _activity_error("AgentLoop activity output checksum does not match")
        return output

    @property
    def wait_candidate_checksum(self) -> str | None:
        return (
            None
            if self.approval_wait is None
            else self.approval_wait.candidate_checksum
        )


@dataclass(frozen=True, slots=True)
class AgentLoopGraphWorker:
    """Candidate-only Graph worker for exactly one configured AgentSpec."""

    worker_id: str
    worker_version: str
    activity_contract_id: str
    activity_contract_version: str
    agent_runner: AgentLoopRunnerPort = field(repr=False)
    agent: AgentSpec
    artifact_recorder: AgentLoopGraphArtifactRecorder = field(repr=False)
    result_output_key: str = "agent_loop_result"
    worker_type: HarnessWorkerType = HarnessWorkerType.AGENT_LOOP
    input_admission: AgentLoopInputAdmissionPort | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        reference = HarnessContractReference(
            HarnessContractKind.WORKER,
            _required_text(self.worker_id, "worker_id"),
            _required_text(self.worker_version, "worker_version"),
        )
        object.__setattr__(self, "worker_id", reference.contract_id)
        object.__setattr__(self, "worker_version", reference.version)
        activity_reference = HarnessContractReference(
            HarnessContractKind.ACTIVITY,
            _required_text(self.activity_contract_id, "activity_contract_id"),
            _required_text(
                self.activity_contract_version,
                "activity_contract_version",
            ),
        )
        object.__setattr__(
            self,
            "activity_contract_id",
            activity_reference.contract_id,
        )
        object.__setattr__(
            self,
            "activity_contract_version",
            activity_reference.version,
        )
        if not callable(getattr(self.agent_runner, "run", None)):
            raise TypeError("agent_runner must expose run(...)")
        if self.input_admission is not None and not isinstance(self.input_admission, AgentLoopInputAdmissionPort):
            raise TypeError("input_admission must implement AgentLoopInputAdmissionPort")
        if not isinstance(self.agent, AgentSpec):
            raise TypeError("agent must be AgentSpec")
        if not isinstance(self.artifact_recorder, AgentLoopGraphArtifactRecorder):
            raise TypeError(
                "artifact_recorder must be AgentLoopGraphArtifactRecorder"
            )
        object.__setattr__(
            self,
            "result_output_key",
            _required_text(self.result_output_key, "result_output_key"),
        )
        if self.worker_type is not HarnessWorkerType.AGENT_LOOP:
            raise TypeError("AgentLoop Graph worker type must be AGENT_LOOP")
        HarnessWorkerResult(
            status=HarnessWorkerStatus.SUCCEEDED,
            output={self.result_output_key: {}},
        )

    @property
    def worker_ref(self) -> HarnessContractReference:
        return HarnessContractReference(
            HarnessContractKind.WORKER,
            self.worker_id,
            self.worker_version,
        )

    @property
    def activity_ref(self) -> HarnessContractReference:
        return HarnessContractReference(
            HarnessContractKind.ACTIVITY,
            self.activity_contract_id,
            self.activity_contract_version,
        )

    def execute(self, task: dict[str, Any]) -> HarnessWorkerResult:
        parsed = AgentLoopGraphActivityTask.from_worker_task(task)
        activity = parsed.task_context.activity
        if (
            activity.worker_ref != self.worker_ref
            or activity.activity_ref != self.activity_ref
        ):
            raise HarnessValidationError(
                "AgentLoop Graph worker does not match the durable activity",
                code="agent_loop_graph_activity_binding_mismatch",
                details={
                    "expected_worker_ref": self.worker_ref.exact_ref,
                    "actual_worker_ref": activity.worker_ref.exact_ref,
                    "expected_activity_ref": self.activity_ref.exact_ref,
                    "actual_activity_ref": activity.activity_ref.exact_ref,
                },
            )
        recall_options = {}
        if self.input_admission is not None:
            snapshot = self.input_admission.admit_parent_inputs(task, agent=self.agent)
            recall_options["memory_recall"] = self.input_admission.parent_memory_recall(snapshot)
        result = self.agent_runner.run(
            self.agent,
            mapping_to_dict(parsed.inputs),
            conversation_id=parsed.conversation_id,
            run_id=activity.run_id,
            graph_id=activity.graph_ref.graph_id,
            graph_version=activity.graph_ref.identity_version,
            graph_ref=activity.graph_ref.identity_ref.exact_ref,
            graph_checksum=activity.graph_ref.checksum,
            node_id=activity.node_id,
            node_instance_id=activity.node_instance_id,
            graph_checkpoint_ref=parsed.task_context.graph_checkpoint_ref,
            activity_id=activity.activity_id,
            attempt=activity.attempt,
            resume_from_cursor=parsed.resume_from_cursor,
            **recall_options,
        )
        if not isinstance(result, AgentLoopResult):
            raise HarnessValidationError(
                "AgentRunner returned an invalid AgentLoop result",
                code="agent_loop_graph_activity_result_invalid",
            )
        _assert_agent_result_identity(result, agent_id=self.agent.agent_id)
        worker_status = _worker_status(result)
        wait_candidate = (
            AgentLoopGraphWaitCandidate.from_result(
                task=parsed,
                agent_id=self.agent.agent_id,
                result=result,
            )
            if result.status is AgentLoopStatus.WAITING_FOR_APPROVAL
            else None
        )
        approval_wait = (
            None
            if wait_candidate is None
            else AgentLoopGraphApprovalWaitFact.from_candidate(wait_candidate)
        )
        result_projection = _agent_result_projection(
            result,
            execution_identity=GraphExecutionIdentity(
                run_id=activity.run_id,
                graph_id=activity.graph_ref.graph_id,
                graph_version=activity.graph_ref.identity_version,
                graph_ref=activity.graph_ref.identity_ref.exact_ref,
                graph_checksum=activity.graph_ref.checksum,
                node_id=activity.node_id,
                node_instance_id=activity.node_instance_id,
                activity_id=activity.activity_id,
                attempt=activity.attempt,
            ),
            max_memory_candidates=(
                self.agent.loop_policy.max_tool_calls + 1
                if self.agent.loop_policy.memory_write_enabled
                else 0
            ),
        )
        preliminary_evidence = (
            () if wait_candidate is None else (wait_candidate.worker_evidence(),)
        )
        error = (
            None
            if worker_status is HarnessWorkerStatus.SUCCEEDED
            else _result_error(result)
        )
        HarnessWorkerResult(
            status=worker_status,
            output={self.result_output_key: {"result": result_projection}},
            diagnostics=_worker_diagnostics(result, wait_candidate),
            metrics=result.metrics.to_dict(),
            evidence=preliminary_evidence,
            error=error,
        )

        artifact_context = AgentLoopGraphArtifactContext.from_task_context(
            parsed.task_context,
            graph_version=activity.graph_ref.identity_version,
            agent_id=self.agent.agent_id,
            conversation_id=parsed.conversation_id,
        )
        receipt = self.artifact_recorder.record(
            context=artifact_context,
            artifacts=tuple(result.llm_call_artifacts),
        )
        output = AgentLoopGraphActivityOutput(
            task_context_checksum=parsed.task_context.context_checksum,
            agent_id=self.agent.agent_id,
            conversation_id=parsed.conversation_id,
            result=result_projection,
            artifact_refs=receipt.artifact_refs,
            artifact_receipt_checksum=receipt.receipt_checksum,
            approval_wait=approval_wait,
        )
        evidence = [receipt.worker_evidence()]
        if wait_candidate is not None:
            evidence.append(wait_candidate.worker_evidence())
        return HarnessWorkerResult(
            status=worker_status,
            output={self.result_output_key: output.to_dict()},
            artifacts=receipt.artifact_refs,
            diagnostics=_worker_diagnostics(result, wait_candidate),
            metrics=result.metrics.to_dict(),
            evidence=tuple(evidence),
            error=error,
        )


@dataclass(frozen=True, slots=True)
class AgentLoopGraphWaitCandidateGate(DeterministicGate):
    """Verify that a waiting candidate is bounded and internally consistent."""

    result_output_key: str = "agent_loop_result"
    tenant_scope_input_key: str = "tenant_scope_ref"
    identity_scope_input_key: str = "identity_scope_ref"
    gate_name: str = field(default="agent_loop_wait_candidate", init=False)
    gate_version: str = field(default="1", init=False)
    gate_dependencies: tuple[str, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "result_output_key",
            "tenant_scope_input_key",
            "identity_scope_input_key",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        worker_result = context.worker_result
        if worker_result is None:
            return self._failure(
                "AgentLoop worker result is required",
                "agent_loop_wait_worker_result_missing",
            )
        if worker_result.status is not HarnessWorkerStatus.SUCCEEDED:
            return self._failure(
                "AgentLoop candidate activity did not succeed",
                "agent_loop_wait_worker_status_invalid",
            )
        if worker_result.error is not None:
            return self._failure(
                "successful AgentLoop candidate cannot carry an error",
                "agent_loop_wait_worker_error_present",
            )
        raw_output = worker_result.output.get(self.result_output_key)
        if not isinstance(raw_output, Mapping):
            return self._failure(
                "AgentLoop activity output is missing",
                "agent_loop_wait_output_missing",
            )
        try:
            output = AgentLoopGraphActivityOutput.from_dict(raw_output)
        except (HarnessValidationError, TypeError, ValueError):
            return self._failure(
                "AgentLoop activity output is invalid",
                "agent_loop_wait_output_invalid",
            )
        evidence = tuple(
            item
            for item in worker_result.evidence
            if item.evidence_type == AGENT_LOOP_GRAPH_WAIT_EVIDENCE_TYPE
        )
        if output.approval_wait is None:
            if evidence:
                return self._failure(
                    "non-waiting AgentLoop output carries waiting evidence",
                    "agent_loop_wait_evidence_unexpected",
                )
            return HarnessGateResult(
                gate_name=self.gate_name,
                passed=True,
                details={
                    "reason_code": "agent_loop_candidate_not_waiting",
                    "waiting": False,
                    "output_checksum": output.output_checksum,
                },
            )
        if len(evidence) != 1:
            return self._failure(
                "waiting AgentLoop output requires one exact evidence record",
                "agent_loop_wait_evidence_count_invalid",
            )
        try:
            candidate = AgentLoopGraphWaitCandidate.from_dict(evidence[0].payload)
            expected_fact = AgentLoopGraphApprovalWaitFact.from_candidate(candidate)
        except (HarnessValidationError, TypeError, ValueError):
            return self._failure(
                "AgentLoop waiting evidence is invalid",
                "agent_loop_wait_evidence_invalid",
            )
        run_inputs = context.run_spec.inputs
        graph_ref = context.graph_state.graph_ref
        node_state = context.node_state
        if node_state is None:
            return self._failure(
                "AgentLoop Gate requires an exact Graph node instance",
                "agent_loop_wait_node_state_missing",
            )
        expected_tenant_scope = run_inputs.get(self.tenant_scope_input_key)
        expected_identity_scope = run_inputs.get(self.identity_scope_input_key)
        mismatches = tuple(
            field_name
            for field_name, expected, actual in (
                ("approval_wait", expected_fact, output.approval_wait),
                (
                    "task_context_checksum",
                    candidate.task_context_checksum,
                    output.task_context_checksum,
                ),
                ("agent_id", candidate.agent_id, output.agent_id),
                (
                    "conversation_id",
                    candidate.conversation_id,
                    output.conversation_id,
                ),
                (
                    "run_id",
                    context.run_spec.run_id,
                    candidate.run_id,
                ),
                (
                    "graph_id",
                    graph_ref.graph_id,
                    candidate.graph_id,
                ),
                (
                    "graph_version",
                    graph_ref.identity_version,
                    candidate.graph_version,
                ),
                (
                    "graph_checksum",
                    graph_ref.checksum,
                    candidate.graph_checksum,
                ),
                (
                    "node_id",
                    context.step_spec.step_id,
                    candidate.node_id,
                ),
                (
                    "node_instance_id",
                    node_state.metadata.get(
                        "node_instance_id",
                        node_state.instance_id,
                    ),
                    candidate.node_instance_id,
                ),
                (
                    "activity_attempt",
                    node_state.attempt,
                    candidate.activity_attempt,
                ),
                (
                    "result_status",
                    AgentLoopStatus.WAITING_FOR_APPROVAL.value,
                    output.result.get("status"),
                ),
                ("result_success", False, output.result.get("success")),
                (
                    "tenant_scope_ref",
                    expected_tenant_scope,
                    output.approval_wait.tenant_scope_ref,
                ),
                (
                    "identity_scope_ref",
                    expected_identity_scope,
                    output.approval_wait.identity_scope_ref,
                ),
            )
            if expected != actual
        )
        if mismatches:
            return self._failure(
                "AgentLoop waiting output does not match its evidence",
                "agent_loop_wait_output_evidence_mismatch",
                mismatches=mismatches,
            )
        return HarnessGateResult(
            gate_name=self.gate_name,
            passed=True,
            details={
                "reason_code": "agent_loop_wait_candidate_verified",
                "waiting": True,
                "candidate_checksum": candidate.candidate_checksum,
                "approval_id": output.approval_wait.approval_id,
                "correlation_ref": output.approval_wait.correlation_ref,
                "output_checksum": output.output_checksum,
            },
        )

    def _failure(
        self,
        reason: str,
        reason_code: str,
        *,
        mismatches: tuple[str, ...] = (),
    ) -> HarnessGateResult:
        details: dict[str, Any] = {"reason_code": reason_code}
        if mismatches:
            details["mismatches"] = list(mismatches)
        return HarnessGateResult(
            gate_name=self.gate_name,
            passed=False,
            reason=reason,
            details=details,
        )


@dataclass(frozen=True, slots=True)
class AgentLoopGraphApprovalWaitBinding:
    """Declaration-only bridge from verified AgentLoop output to Graph Wait."""

    source_node_id: str
    result_output_key: str
    wait_id: str
    tenant_scope_input_key: str = "tenant_scope_ref"
    identity_scope_input_key: str = "identity_scope_ref"
    schema_version: str = AGENT_LOOP_GRAPH_APPROVAL_WAIT_BINDING_SCHEMA

    def __post_init__(self) -> None:
        for field_name in (
            "source_node_id",
            "result_output_key",
            "wait_id",
            "tenant_scope_input_key",
            "identity_scope_input_key",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if self.schema_version != AGENT_LOOP_GRAPH_APPROVAL_WAIT_BINDING_SCHEMA:
            raise HarnessValidationError(
                "unsupported AgentLoop Graph approval Wait binding schema",
                code="unsupported_agent_loop_graph_activity_schema",
            )

    @property
    def gate_ref(self) -> str:
        return AGENT_LOOP_GRAPH_WAIT_GATE_REF

    @property
    def control_fact_paths(self) -> tuple[str, ...]:
        prefix = f"{self.result_output_key}.approval_wait"
        return tuple(
            sorted(
                (
                    f"{self.result_output_key}.waiting",
                    *(
                        f"{prefix}.{field_name}"
                        for field_name in (
                            "schema_version",
                            "candidate_checksum",
                            "approval_id",
                            "approval_kind",
                            "tool_name",
                            "approval_request_checksum",
                            "graph_checkpoint_ref",
                            "tenant_scope_ref",
                            "identity_scope_ref",
                            "correlation_ref",
                        )
                    ),
                )
            )
        )

    @property
    def output_path(self) -> str:
        return (
            f"node.outputs.{self.source_node_id}."
            f"{self.result_output_key}.approval_wait"
        )

    @property
    def waiting_condition_path(self) -> str:
        return f"node.outputs.{self.result_output_key}.waiting"

    def waiting_condition(self) -> ConditionPredicate:
        return ConditionPredicate(self.waiting_condition_path, "equals", True)

    def wait_expression(self) -> Wait:
        prefix = self.output_path
        return Wait(
            wait_id=self.wait_id,
            kind="approval",
            correlation={
                field_name: f"{prefix}.{field_name}"
                for field_name in (
                    "schema_version",
                    "candidate_checksum",
                    "approval_id",
                    "approval_kind",
                    "tool_name",
                    "approval_request_checksum",
                    "graph_checkpoint_ref",
                    "tenant_scope_ref",
                    "identity_scope_ref",
                    "correlation_ref",
                )
            },
            signal_type=AGENT_LOOP_GRAPH_APPROVAL_SIGNAL_TYPE,
            signal_version=AGENT_LOOP_GRAPH_APPROVAL_SIGNAL_VERSION,
            tenant_scope_path=f"graph.inputs.{self.tenant_scope_input_key}",
            identity_scope_path=f"graph.inputs.{self.identity_scope_input_key}",
        )

    def assert_step_contract(self, step: HarnessStepSpec) -> None:
        if not isinstance(step, HarnessStepSpec):
            raise TypeError("step must be HarnessStepSpec")
        mismatches: list[str] = []
        if getattr(step, "step_id", None) != self.source_node_id:
            mismatches.append("source_node_id")
        if getattr(step, "output_key", None) != self.result_output_key:
            mismatches.append("result_output_key")
        if getattr(step, "quality_gate", None) != self.gate_ref:
            mismatches.append("gate_ref")
        metadata = getattr(step, "metadata", None)
        raw_paths = (
            metadata.get("control_fact_paths")
            if isinstance(metadata, Mapping)
            else None
        )
        if tuple(sorted(raw_paths or ())) != self.control_fact_paths:
            mismatches.append("control_fact_paths")
        if isinstance(metadata, Mapping) and metadata.get("approval_required") is True:
            mismatches.append("approval_policy_must_use_graph_wait")
        if mismatches:
            raise HarnessValidationError(
                "AgentLoop approval Wait binding does not match its Step contract",
                code="agent_loop_graph_wait_binding_mismatch",
                details={"mismatches": sorted(mismatches)},
            )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_node_id": self.source_node_id,
            "result_output_key": self.result_output_key,
            "wait_id": self.wait_id,
            "tenant_scope_input_key": self.tenant_scope_input_key,
            "identity_scope_input_key": self.identity_scope_input_key,
            "gate_ref": self.gate_ref,
            "waiting_control_fact_path": f"{self.result_output_key}.waiting",
            "waiting_condition": self.waiting_condition().to_dict(),
            "control_fact_paths": list(self.control_fact_paths),
            "wait": self.wait_expression().to_dict(),
            "requires_deterministic_wait_branch": True,
            "registers_graph_wait": False,
        }


@dataclass(frozen=True, slots=True)
class AgentLoopGraphActivityContract:
    """Serial-only Graph activity contract; dispatch is owned by the executor."""

    activity_contract_id: str
    activity_contract_version: str
    capabilities: HarnessActivityCapabilities = field(
        default_factory=HarnessActivityCapabilities
    )

    def __post_init__(self) -> None:
        reference = HarnessContractReference(
            HarnessContractKind.ACTIVITY,
            _required_text(self.activity_contract_id, "activity_contract_id"),
            _required_text(
                self.activity_contract_version,
                "activity_contract_version",
            ),
        )
        object.__setattr__(
            self,
            "activity_contract_id",
            reference.contract_id,
        )
        object.__setattr__(
            self,
            "activity_contract_version",
            reference.version,
        )
        if not isinstance(self.capabilities, HarnessActivityCapabilities):
            raise TypeError("capabilities must be HarnessActivityCapabilities")

    def dispatch(self, _request: object) -> None:
        raise HarnessValidationError(
            "AgentLoop Graph activity must use the physical Graph executor",
            code="agent_loop_legacy_dispatch_forbidden",
        )


@dataclass(frozen=True, slots=True)
class AgentLoopGraphActivityBindingBundle:
    worker_binding: HarnessWorkerBinding
    activity_binding: HarnessActivityContractBinding
    leaf_binding: HarnessLeafActivityBinding
    wait_gate_registration: GateRegistration
    authority: HarnessRuntimeBindingAuthority

    def __post_init__(self) -> None:
        if not isinstance(
            self.worker_binding.implementation,
            AgentLoopGraphWorker,
        ):
            raise TypeError(
                "worker binding must contain AgentLoopGraphWorker"
            )
        if not isinstance(
            self.activity_binding.implementation,
            AgentLoopGraphActivityContract,
        ):
            raise TypeError(
                "activity binding must contain AgentLoopGraphActivityContract"
            )
        if not isinstance(self.wait_gate_registration, GateRegistration):
            raise TypeError("wait_gate_registration must be GateRegistration")
        if (
            str(self.wait_gate_registration.reference)
            != AGENT_LOOP_GRAPH_WAIT_GATE_REF
            or not isinstance(
                self.wait_gate_registration.gate,
                AgentLoopGraphWaitCandidateGate,
            )
            or self.wait_gate_registration.gate.result_output_key
            != self.worker_binding.implementation.result_output_key
        ):
            raise HarnessValidationError(
                "AgentLoop Graph wait gate binding is inconsistent",
                code="agent_loop_graph_activity_binding_mismatch",
            )
        resolved = self.authority.resolve_leaf_activity(
            worker_ref=self.worker_binding.reference,
            activity_ref=self.activity_binding.reference,
            expected_leaf_activity_kind=HarnessLeafActivityKind.AGENT_LOOP,
            required_usage=HarnessActivityUsage.SERIAL,
        )
        if (
            resolved.worker != self.worker_binding
            or resolved.activity != self.activity_binding
            or resolved.registration != self.leaf_binding
        ):
            raise HarnessValidationError(
                "AgentLoop Graph binding bundle is inconsistent",
                code="agent_loop_graph_activity_binding_mismatch",
            )

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": AGENT_LOOP_GRAPH_BINDING_MANIFEST_SCHEMA,
            "installs_runtime_authority": False,
            "worker_ref": self.worker_binding.reference.to_dict(),
            "activity_ref": self.activity_binding.reference.to_dict(),
            "leaf_activity_kind": self.leaf_binding.leaf_activity_kind.value,
            "required_usage": HarnessActivityUsage.SERIAL.value,
            "agent_id": self.worker_binding.implementation.agent.agent_id,
            "result_output_key": (
                self.worker_binding.implementation.result_output_key
            ),
            "artifact_owner_required": True,
            "wait_candidate_gate_ref": AGENT_LOOP_GRAPH_WAIT_GATE_REF,
            "tenant_scope_input_key": (
                self.wait_gate_registration.gate.tenant_scope_input_key
            ),
            "identity_scope_input_key": (
                self.wait_gate_registration.gate.identity_scope_input_key
            ),
            "waiting_candidate_worker_status": HarnessWorkerStatus.SUCCEEDED.value,
            "publishes_terminal_manifest": False,
            "registers_graph_wait": False,
        }


def build_agent_loop_graph_activity_binding_bundle(
    *,
    worker_ref: HarnessContractReference,
    activity_ref: HarnessContractReference,
    agent_runner: AgentLoopRunnerPort,
    agent: AgentSpec,
    artifact_recorder: AgentLoopGraphArtifactRecorder,
    result_output_key: str = "agent_loop_result",
    tenant_scope_input_key: str = "tenant_scope_ref",
    identity_scope_input_key: str = "identity_scope_ref",
    side_effect_registry: HarnessSideEffectRegistry | None = None,
    input_admission: AgentLoopInputAdmissionPort | None = None,
) -> AgentLoopGraphActivityBindingBundle:
    if not isinstance(worker_ref, HarnessContractReference) or (
        worker_ref.contract_kind is not HarnessContractKind.WORKER
    ):
        raise TypeError("worker_ref must be a worker HarnessContractReference")
    if not isinstance(activity_ref, HarnessContractReference) or (
        activity_ref.contract_kind is not HarnessContractKind.ACTIVITY
    ):
        raise TypeError("activity_ref must be an activity HarnessContractReference")
    if side_effect_registry is not None and not isinstance(
        side_effect_registry,
        HarnessSideEffectRegistry,
    ):
        raise TypeError("side_effect_registry must be HarnessSideEffectRegistry")
    worker = AgentLoopGraphWorker(
        worker_id=worker_ref.contract_id,
        worker_version=worker_ref.version,
        activity_contract_id=activity_ref.contract_id,
        activity_contract_version=activity_ref.version,
        agent_runner=agent_runner,
        agent=agent,
        artifact_recorder=artifact_recorder,
        result_output_key=result_output_key,
        input_admission=input_admission,
    )
    activity_contract = AgentLoopGraphActivityContract(
        activity_contract_id=activity_ref.contract_id,
        activity_contract_version=activity_ref.version,
    )
    worker_binding = HarnessWorkerBinding(
        reference=worker_ref,
        worker_type=HarnessWorkerType.AGENT_LOOP,
        implementation=worker,
    )
    activity_binding = HarnessActivityContractBinding(
        reference=activity_ref,
        implementation=activity_contract,
    )
    leaf_binding = HarnessLeafActivityBinding(
        leaf_activity_kind=HarnessLeafActivityKind.AGENT_LOOP,
        worker_ref=worker_ref,
        activity_ref=activity_ref,
    )
    wait_gate_registration = GateRegistration(
        reference=GateReference.parse(AGENT_LOOP_GRAPH_WAIT_GATE_REF),
        gate=AgentLoopGraphWaitCandidateGate(
            result_output_key=result_output_key,
            tenant_scope_input_key=tenant_scope_input_key,
            identity_scope_input_key=identity_scope_input_key,
        ),
    )
    authority = HarnessRuntimeBindingAuthority(
        workers=(worker_binding,),
        activities=(activity_binding,),
        leaf_activities=(leaf_binding,),
        gate_registry=DeterministicGateRegistry((wait_gate_registration,)),
        side_effect_registry=side_effect_registry,
    )
    return AgentLoopGraphActivityBindingBundle(
        worker_binding=worker_binding,
        activity_binding=activity_binding,
        leaf_binding=leaf_binding,
        wait_gate_registration=wait_gate_registration,
        authority=authority,
    )


def _agent_result_projection(
    result: AgentLoopResult,
    *,
    execution_identity: GraphExecutionIdentity,
    max_memory_candidates: int,
) -> dict[str, Any]:
    if result.memory_ops:
        raise HarnessValidationError(
            "Graph AgentLoop cannot carry direct memory operations",
            code="agent_loop_graph_memory_side_effect_forbidden",
        )
    memory_candidates = _validated_memory_candidates(
        result.memory_candidates,
        execution_identity=execution_identity,
        max_candidates=max_memory_candidates,
    )
    serialized = result.to_dict()
    payload = {
        "success": result.success,
        "status": result.status.value,
        "output": serialized["output"],
        "final_output": serialized["final_output"],
        "verdict": serialized["verdict"],
        "iterations": result.iterations,
        "metrics": result.metrics.to_dict(),
        "diagnostics": serialized["diagnostics"],
        "termination_reason": serialized["termination_reason"],
        "max_steps_reached": result.max_steps_reached,
        "trace_id": serialized["trace_id"],
        "trace_ref": result.trace_ref,
        "warnings": list(result.warnings),
        "action_count": len(result.actions),
        "event_count": len(result.events),
        "event_types": [
            str(item.get("event_type") or item.get("type") or "unknown")
            for item in result.events
        ],
        "trajectory_count": len(result.trajectory),
        "tool_call_count": len(result.tool_calls),
        "llm_call_artifact_count": len(result.llm_call_artifacts),
        "memory_candidates": memory_candidates,
    }
    frozen = freeze_json(
        redact_sensitive_values(payload),
        "$.agent_loop_graph_activity.result",
    )
    if not isinstance(frozen, Mapping):  # pragma: no cover - invariant
        raise AssertionError("AgentLoop result projection must remain a mapping")
    return mapping_to_dict(frozen)


def _validated_memory_candidates(
    candidates: list[dict[str, Any]],
    *,
    execution_identity: GraphExecutionIdentity,
    max_candidates: int,
) -> list[dict[str, Any]]:
    if len(candidates) > max_candidates:
        raise HarnessValidationError(
            "Graph AgentLoop memory candidate budget exceeded",
            code="agent_loop_graph_memory_candidate_budget_exceeded",
            details={
                "candidate_count": len(candidates),
                "max_candidates": max_candidates,
            },
        )
    expected_identity = execution_identity.to_dict()
    normalized: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, Mapping):
            raise HarnessValidationError(
                "Graph AgentLoop memory candidate must be an object",
                code="agent_loop_graph_memory_candidate_invalid",
            )
        candidate = dict(raw_candidate)
        metadata = candidate.get("metadata")
        refs = candidate.get("refs")
        candidate_id = candidate.get("memory_id")
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("candidate_only") is not True
            or metadata.get("source") != "agent_loop"
            or metadata.get("graph_identity") != expected_identity
            or not isinstance(refs, Mapping)
            or any(refs.get(key) != value for key, value in expected_identity.items())
            or candidate.get("scope") != "graph"
            or candidate.get("namespace") != "agent.loop"
            or not isinstance(candidate_id, str)
            or not candidate_id.startswith("agent-loop:")
            or not isinstance(candidate.get("content"), str)
            or len(candidate["content"]) > 16_448
        ):
            raise HarnessValidationError(
                "Graph AgentLoop memory candidate is not candidate-only or has mismatched identity",
                code="agent_loop_graph_memory_candidate_invalid",
            )
        if candidate_id in candidate_ids:
            raise HarnessValidationError(
                "Graph AgentLoop memory candidate ids must be unique",
                code="agent_loop_graph_memory_candidate_invalid",
            )
        candidate_ids.add(candidate_id)
        normalized.append(candidate)
    return normalized


def _worker_status(result: AgentLoopResult) -> HarnessWorkerStatus:
    if result.success:
        if result.status not in {
            AgentLoopStatus.SUCCEEDED,
            AgentLoopStatus.ACCEPTED,
        }:
            raise HarnessValidationError(
                "successful AgentLoop result has an invalid status",
                code="agent_loop_graph_activity_result_invalid",
            )
        return HarnessWorkerStatus.SUCCEEDED
    if result.status in {
        AgentLoopStatus.SUCCEEDED,
        AgentLoopStatus.ACCEPTED,
        AgentLoopStatus.PENDING,
        AgentLoopStatus.RUNNING,
    }:
        raise HarnessValidationError(
            "unsuccessful AgentLoop result has an invalid status",
            code="agent_loop_graph_activity_result_invalid",
        )
    if result.status is AgentLoopStatus.WAITING_FOR_APPROVAL:
        # The activity successfully produced a candidate. Only an explicit,
        # deterministic Graph Wait may turn that candidate into outer waiting.
        return HarnessWorkerStatus.SUCCEEDED
    if result.status in {AgentLoopStatus.BLOCKED, AgentLoopStatus.STALLED}:
        return HarnessWorkerStatus.BLOCKED
    return HarnessWorkerStatus.FAILED


def _assert_agent_result_identity(
    result: AgentLoopResult,
    *,
    agent_id: str,
) -> None:
    diagnostics = result.diagnostics
    if diagnostics is None:
        return
    mismatches = tuple(
        field_name
        for field_name, expected, actual in (
            ("agent_id", agent_id, diagnostics.agent_id),
            ("status", result.status, diagnostics.status),
        )
        if expected != actual
    )
    if mismatches:
        raise HarnessValidationError(
            "AgentLoop diagnostics do not match the bound agent result",
            code="agent_loop_graph_activity_result_invalid",
            details={"mismatches": list(mismatches)},
        )


def _approval_request_from_issue(
    issue: AgentLoopIssue,
) -> AgentLoopGraphApprovalRequest:
    if not isinstance(issue.metadata, Mapping):
        raise _wait_error("approval diagnostics metadata must be an object")
    return AgentLoopGraphApprovalRequest(
        approval_id=issue.metadata.get("approval_id"),
        approval_kind=issue.metadata.get("approval_kind"),
        tool_name=issue.tool_name,
        control_action=issue.metadata.get("control_action"),
        escalation_type=issue.metadata.get("escalation_type"),
    )


def _worker_diagnostics(
    result: AgentLoopResult,
    wait_candidate: AgentLoopGraphWaitCandidate | None,
) -> dict[str, Any]:
    diagnostics = result.diagnostics
    return {
        "agent_loop_status": result.status.value,
        "requested_tools": _requested_tools(result),
        "stop_reason": (
            result.termination_reason
            or (
                None
                if diagnostics is None
                else diagnostics.stop_reason.value
            )
        ),
        "trace_ref": result.trace_ref,
        "wait_candidate_checksum": (
            None
            if wait_candidate is None
            else wait_candidate.candidate_checksum
        ),
    }


def _requested_tools(result: AgentLoopResult) -> list[str]:
    names = {
        action.tool_name
        for action in result.actions
        if action.is_tool_call()
        and isinstance(action.tool_name, str)
        and action.tool_name
    }
    names.update(
        str(item["tool_name"])
        for item in result.tool_calls
        if isinstance(item, Mapping)
        and isinstance(item.get("tool_name"), str)
        and item["tool_name"]
    )
    if result.diagnostics is not None:
        names.update(
            issue.tool_name
            for issue in result.diagnostics.issues
            if isinstance(issue.tool_name, str) and issue.tool_name
        )
    return sorted(names)


def _result_error(result: AgentLoopResult) -> str | None:
    if result.error is None:
        return None
    value = result.to_dict().get("error")
    if isinstance(value, Mapping):
        message = value.get("message")
        text = str(message) if message is not None else str(dict(value))
    else:
        text = str(value)
    return str(redact_sensitive_values(text))


def _exact_mapping(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise _activity_error(f"AgentLoop Graph {label} fields are invalid")
    return dict(value)


def _required_text(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _activity_error(f"{field_name} must be non-blank text")
    return value


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _checksum(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    if not text.startswith("sha256:") or len(text) != 71:
        raise _activity_error(f"{field_name} must be a sha256 checksum")
    try:
        int(text.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise _activity_error(f"{field_name} must be a sha256 checksum") from exc
    return text


def _activity_error(message: str) -> HarnessValidationError:
    return HarnessValidationError(
        message,
        code="agent_loop_graph_activity_contract_invalid",
    )


def _wait_error(message: str) -> HarnessValidationError:
    return HarnessValidationError(
        message,
        code="agent_loop_graph_wait_candidate_invalid",
    )


__all__ = [
    "AGENT_LOOP_GRAPH_ACTIVITY_OUTPUT_SCHEMA",
    "AGENT_LOOP_GRAPH_ACTIVITY_TASK_SCHEMA",
    "AGENT_LOOP_GRAPH_APPROVAL_SIGNAL_TYPE",
    "AGENT_LOOP_GRAPH_APPROVAL_SIGNAL_VERSION",
    "AGENT_LOOP_GRAPH_APPROVAL_WAIT_BINDING_SCHEMA",
    "AGENT_LOOP_GRAPH_APPROVAL_WAIT_FACT_SCHEMA",
    "AGENT_LOOP_GRAPH_APPROVAL_REQUEST_SCHEMA",
    "AGENT_LOOP_GRAPH_BINDING_MANIFEST_SCHEMA",
    "AGENT_LOOP_GRAPH_WAIT_CANDIDATE_SCHEMA",
    "AGENT_LOOP_GRAPH_WAIT_EVIDENCE_TYPE",
    "AGENT_LOOP_GRAPH_WAIT_GATE_REF",
    "AgentLoopGraphActivityBindingBundle",
    "AgentLoopGraphActivityContract",
    "AgentLoopGraphActivityOutput",
    "AgentLoopGraphActivityTask",
    "AgentLoopGraphApprovalWaitBinding",
    "AgentLoopGraphApprovalWaitFact",
    "AgentLoopGraphApprovalRequest",
    "AgentLoopGraphWaitCandidate",
    "AgentLoopGraphWaitCandidateGate",
    "AgentLoopGraphWorker",
    "AgentLoopRunnerPort",
    "build_agent_loop_graph_activity_binding_bundle",
]
