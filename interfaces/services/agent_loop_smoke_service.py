from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import patch
from uuid import uuid4

from framework.agent.loop.runner import AgentRunner
from framework.agent.models import AgentLoopPolicy, AgentLoopResult, AgentSpec
from framework.events.canonical import checksum_for
from framework.harness.agent_loop import (
    AGENT_LOOP_GRAPH_ACTIVITY_TASK_SCHEMA,
    AgentLoopGraphActivityOutput,
    AgentLoopGraphArtifactRecorder,
    build_agent_loop_graph_activity_binding_bundle,
)
from framework.harness.artifacts import (
    ArtifactRef,
    ArtifactWriteRequest,
    GraphResultArtifactReadPort,
    GraphTerminalArtifact,
    GraphTerminalManifest,
    GraphTerminalManifestV2,
    GraphTerminalManifestPort,
    GraphTerminalStatus,
    RunBoundArtifactPort,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.gates import HarnessGateResult
from framework.harness.control_plane.graph_runtime import (
    HarnessGraphActivity,
    HarnessGraphActivityResult,
    HarnessGraphActivityResultStatus,
    graph_reference,
)
from framework.harness.graph import GraphExecutionVersionManifest
from framework.harness.control_plane.graph_state import (
    HarnessNodeInstanceIdentity,
)
from framework.harness.control_plane.node_output import (
    HarnessNodeOutputCommit,
    InMemoryHarnessNodeOutputResource,
)
from framework.harness.graph import (
    HarnessContractKind,
    HarnessContractReference,
    HarnessExecutableNode,
    HarnessGraphPreflight,
    HarnessLeafActivityKind,
    HarnessWorkerType,
    NormalizedHarnessGraph,
    graph_activity_input_checksum,
)
from framework.harness.graph.reference import HarnessGraphReference
from framework.harness.graph.bindings import HarnessActivityUsage
from framework.harness.graph.validation.models import (
    HarnessGraphValidationResult,
)
from framework.harness.graph.validation.registry import (
    HarnessGraphRegistrySnapshot,
    graph_contract_references,
)
from framework.harness.control_plane.activity_execution import HarnessGraphActivityExecutionInput
from framework.harness.runtime import (
    HarnessGraphPhysicalActivityExecutionResult,
    HarnessGraphPhysicalActivityExecutor,
)
from framework.harness.workers.result import (
    HarnessWorkerResult,
    HarnessWorkerStatus,
)
from framework.llm import FakeLLMClient
from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.shared.attempts import AttemptSupervisor
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.redaction import redact_sensitive_values
from framework.shared.time import utc_now
from framework.tool import ToolRegistry


AGENT_LOOP_SMOKE_GRAPH_ID = "test-agent-loop.graph"
AGENT_LOOP_SMOKE_GRAPH_VERSION = "1"
AGENT_LOOP_SMOKE_NODE_ID = "run-agent-loop"
AGENT_LOOP_SMOKE_RESULT_KEY = "agent_loop_result"
AGENT_LOOP_SMOKE_VERIFY_GATE_REF = "test-agent-loop.verify@1"
AGENT_LOOP_SMOKE_OUTCOME_SCHEMA = "newsroom.test-agent-loop-outcome/v1"
AGENT_LOOP_SMOKE_RESULT_SCHEMA = "newsroom.test-agent-loop-result/v1"
AGENT_LOOP_SMOKE_EVENT_TYPES = (
    "agent_started",
    "iteration_started",
    "llm_call",
    "action_parsed",
    "tool_call",
    "tool_observation",
    "iteration_started",
    "llm_call",
    "action_parsed",
    "judge_retry",
    "iteration_started",
    "llm_call",
    "action_parsed",
    "judge_accept",
    "final_output",
    "agent_completed",
)

_GRAPH_REF = HarnessContractReference(
    HarnessContractKind.GRAPH,
    AGENT_LOOP_SMOKE_GRAPH_ID,
    AGENT_LOOP_SMOKE_GRAPH_VERSION,
)
_STEP_REF = HarnessContractReference(
    HarnessContractKind.STEP,
    "test-agent-loop.run-agent-loop",
    "1",
)
_WORKER_REF = HarnessContractReference(
    HarnessContractKind.WORKER,
    "test-agent-loop.agent",
    "1",
)
_ACTIVITY_REF = HarnessContractReference(
    HarnessContractKind.ACTIVITY,
    "test-agent-loop.agent-loop-activity",
    "1",
)
_VERIFY_GATE_CONTRACT_REF = HarnessContractReference(
    HarnessContractKind.GATE,
    "test-agent-loop.verify",
    "1",
)
_NETWORK_GUARD_LOCK = threading.Lock()


class AgentLoopSmokeArtifactPort(
    RunBoundArtifactPort,
    GraphResultArtifactReadPort,
    GraphTerminalManifestPort,
    Protocol,
):
    def list_staged_artifacts(
        self,
        run_id: str,
    ) -> tuple[GraphTerminalArtifact, ...]: ...


@dataclass(frozen=True, slots=True)
class AgentLoopGraphSmokeResult:
    status: str
    run_id: str
    graph_ref: HarnessGraphReference
    node_instance_id: str
    artifact_path: str
    output: Mapping[str, Any]
    metrics: Mapping[str, Any]
    event_types: tuple[str, ...]
    preflight_ref: str
    activity_receipt_ref: str
    verify_evidence_ref: str
    manifest_hash: str
    network_calls: int
    schema_version: str = AGENT_LOOP_SMOKE_RESULT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "run_id": self.run_id,
            "graph_ref": _graph_ref_text(self.graph_ref),
            "graph": self.graph_ref.to_dict(),
            "node_instance_id": self.node_instance_id,
            "artifact_path": self.artifact_path,
            "output": dict(self.output),
            "metrics": dict(self.metrics),
            "event_types": list(self.event_types),
            "preflight_ref": self.preflight_ref,
            "activity_receipt_ref": self.activity_receipt_ref,
            "verify_evidence_ref": self.verify_evidence_ref,
            "manifest_hash": self.manifest_hash,
            "network_calls": self.network_calls,
            "llm_calls": int(self.metrics.get("llm_calls", 0)),
            "tool_calls": int(self.metrics.get("tool_calls", 0)),
            "token_usage": dict(self.metrics.get("token_usage", {})),
        }


@dataclass(frozen=True, slots=True)
class _SmokeVerifyContext:
    preflight: HarnessGraphValidationResult
    receipt: HarnessGraphPhysicalActivityExecutionResult
    result: AgentLoopResult
    network_calls: int

    @property
    def input_ref(self) -> str:
        return checksum_for(
            {
                "preflight": self.preflight.to_dict(),
                "activity": self.receipt.activity.to_dict(),
                "execution_input": self.receipt.execution_input.to_dict(),
                "worker_result_ref": (
                    None
                    if self.receipt.worker_result is None
                    else self.receipt.worker_result.candidate_result_ref
                ),
                "node_output_commit_ref": (
                    None
                    if self.receipt.node_output_commit is None
                    else self.receipt.node_output_commit.commit_ref
                ),
                "graph_result_ref": (
                    None
                    if self.receipt.graph_result is None
                    else self.receipt.graph_result.result_checksum
                ),
                "agent_result": _agent_result_verify_projection(self.result),
                "network_calls": self.network_calls,
            }
        )


class AgentLoopGraphSmokeVerifyGate:
    gate_name = "test-agent-loop.verify"
    gate_version = "1"

    def evaluate(self, context: _SmokeVerifyContext) -> HarnessGateResult:
        failures: list[str] = []
        receipt = context.receipt
        worker = receipt.worker_result
        commit = receipt.node_output_commit
        graph_result = receipt.graph_result

        if not context.preflight.is_valid:
            failures.append("preflight_invalid")
        if worker is None or worker.status is not HarnessWorkerStatus.SUCCEEDED:
            failures.append("worker_candidate_not_succeeded")
        if commit is None:
            failures.append("node_output_not_committed")
        if graph_result is None or (
            graph_result.status is not HarnessGraphActivityResultStatus.SUCCEEDED
        ):
            failures.append("graph_activity_result_not_succeeded")
        if context.network_calls != 0:
            failures.append("network_access_attempted")

        activity_output: AgentLoopGraphActivityOutput | None = None
        if worker is not None:
            raw_output = worker.output.get(AGENT_LOOP_SMOKE_RESULT_KEY)
            if isinstance(raw_output, Mapping):
                try:
                    activity_output = AgentLoopGraphActivityOutput.from_dict(raw_output)
                except (HarnessValidationError, TypeError, ValueError):
                    failures.append("agent_loop_output_invalid")
            else:
                failures.append("agent_loop_output_missing")

        event_types = _event_types(context.result)
        metrics = context.result.metrics.to_dict()
        requested_tools, requested_tools_valid = _worker_requested_tools(worker)
        final = context.result.output.get("analysis_result")
        if not context.result.success or context.result.status.value not in {
            "succeeded",
            "accepted",
        }:
            failures.append("agent_loop_result_not_succeeded")
        if not isinstance(final, Mapping) or final.get("confidence") != "high":
            failures.append("structured_output_not_accepted")
        if context.result.verdict is None or (
            context.result.verdict.decision.value != "accept"
        ):
            failures.append("judge_did_not_accept")
        if metrics.get("llm_calls") != 3:
            failures.append("llm_call_count_invalid")
        if metrics.get("tool_calls") != 1:
            failures.append("tool_call_count_invalid")
        if metrics.get("judge_retries") != 1:
            failures.append("judge_retry_count_invalid")
        if metrics.get("token_usage", {}).get("total_tokens") != 60:
            failures.append("token_usage_invalid")
        if event_types != AGENT_LOOP_SMOKE_EVENT_TYPES:
            failures.append("event_sequence_invalid")
        if not requested_tools_valid or requested_tools != ["memory.recall"]:
            failures.append("tool_policy_evidence_invalid")
        if not _verified_memory_observation(context):
            failures.append("memory_recall_evidence_invalid")
        if activity_output is not None:
            projected_metrics = activity_output.result.get("metrics")
            if not isinstance(projected_metrics, Mapping) or any(
                projected_metrics.get(key) != metrics.get(key)
                for key in ("iterations", "llm_calls", "tool_calls", "judge_retries")
            ):
                failures.append("candidate_metrics_mismatch")
            if activity_output.artifact_refs != worker.artifacts:
                failures.append("candidate_artifact_refs_mismatch")
            if activity_output.result.get("llm_call_artifact_count") != 3:
                failures.append("candidate_artifact_count_invalid")
            if activity_output.approval_wait is not None:
                failures.append("unexpected_approval_wait")
        if worker is not None and worker.metrics != metrics:
            failures.append("worker_metrics_mismatch")
        if commit is not None:
            expected_output_ref = (
                None
                if activity_output is None
                else checksum_for(
                    {AGENT_LOOP_SMOKE_RESULT_KEY: activity_output.to_dict()}
                )
            )
            if expected_output_ref is None or commit.candidate.output_refs.get(
                AGENT_LOOP_SMOKE_RESULT_KEY
            ) != expected_output_ref:
                failures.append("committed_output_ref_mismatch")

        passed = not failures
        result = HarnessGateResult(
            gate_name=self.gate_name,
            passed=passed,
            reason=None if passed else "test AgentLoop Graph verification failed",
            details={
                "failures": failures,
                "event_types": list(event_types),
                "metrics": metrics,
                "requested_tools": requested_tools,
                "node_instance_id": receipt.activity.node_instance_id,
                "node_output_commit_ref": (
                    None if commit is None else commit.commit_ref
                ),
                "graph_result_ref": (
                    None if graph_result is None else graph_result.result_checksum
                ),
                "network_calls": context.network_calls,
            },
        )
        return result.with_evidence(
            gate_reference=AGENT_LOOP_SMOKE_VERIFY_GATE_REF,
            input_ref=context.input_ref,
            reason_code=(
                "test_agent_loop_graph_verified"
                if passed
                else "test_agent_loop_graph_rejected"
            ),
        )


class AgentLoopGraphSmokeApplicationService:
    """Run the offline AgentLoop fixture through Graph-native contracts."""

    def __init__(
        self,
        *,
        artifact_port: AgentLoopSmokeArtifactPort,
        conversation_store: Any,
        artifact_root: str | Path,
        memory_recall_factory: Callable[[HarnessGraphActivity, str, AgentSpec], ExecutionMemoryRecallPort],
        clock: Callable[[], datetime] = utc_now,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        for method_name in (
            "bind_run",
            "write_artifact",
            "read_artifact",
            "read_graph_result_artifact",
            "list_staged_artifacts",
            "write_terminal_manifest",
        ):
            if not callable(getattr(artifact_port, method_name, None)):
                raise TypeError(
                    f"artifact_port must expose {method_name}(...)"
                )
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(memory_recall_factory):
            raise TypeError("memory_recall_factory must be callable")
        if run_id_factory is not None and not callable(run_id_factory):
            raise TypeError("run_id_factory must be callable")
        self._artifact_port = artifact_port
        self._conversation_store = conversation_store
        self._artifact_root = Path(artifact_root)
        self._clock = clock
        self._memory_recall_factory = memory_recall_factory
        self._run_id_factory = run_id_factory or (
            lambda: f"test-agent-loop-{uuid4().hex}"
        )

    def run(
        self,
        *,
        topic: str,
        run_id: str | None = None,
    ) -> AgentLoopGraphSmokeResult:
        topic = _required_text(topic, "topic")
        resolved_run_id = _required_text(
            run_id if run_id is not None else self._run_id_factory(),
            "run_id",
        )
        graph = build_test_agent_loop_graph()
        preflight = HarnessGraphPreflight().validate(
            graph,
            registry=HarnessGraphRegistrySnapshot(
                references=graph_contract_references(graph),
            ),
        )
        preflight.raise_if_invalid()
        preflight_ref = checksum_for(preflight.to_dict())
        started_at = self._clock()
        graph_ref = graph_reference(graph)
        raw_task = {
            "schema_version": AGENT_LOOP_GRAPH_ACTIVITY_TASK_SCHEMA,
            "inputs": {"topic": topic},
            "conversation_id": f"{resolved_run_id}-conversation",
            "resume_from_cursor": False,
        }
        checkpoint_ref = _checkpoint_ref(
            resolved_run_id,
            graph_ref,
            phase="execute",
        )
        activity = _activity(
            run_id=resolved_run_id,
            graph_ref=graph_ref,
            raw_task=raw_task,
        )
        execution_input = HarnessGraphActivityExecutionInput.for_activity(
            activity,
            task=raw_task,
            leaf_activity_kind=HarnessLeafActivityKind.AGENT_LOOP,
            required_usage=HarnessActivityUsage.SERIAL,
            graph_checkpoint_ref=checkpoint_ref,
            output_keys=(AGENT_LOOP_SMOKE_RESULT_KEY,),
        )
        agent = _agent_spec(topic)
        recording_runner = _RecordingRunner(
            AgentRunner(
                llm_client=_fake_llm(topic),
                tool_registry=ToolRegistry(),
                conversation_store=self._conversation_store,
            ),
            memory_recall=self._memory_recall_factory(activity, topic, agent),
        )
        bundle = build_agent_loop_graph_activity_binding_bundle(
            worker_ref=_WORKER_REF,
            activity_ref=_ACTIVITY_REF,
            agent_runner=recording_runner,
            agent=agent,
            artifact_recorder=AgentLoopGraphArtifactRecorder(
                self._artifact_port
            ),
            result_output_key=AGENT_LOOP_SMOKE_RESULT_KEY,
        )
        executor = HarnessGraphPhysicalActivityExecutor(
            binding_authority=bundle.authority,
            input_resolver=_InputResolver(execution_input),
            node_output_resource=InMemoryHarnessNodeOutputResource(),
            result_committer=_ResultCommitter(),
            supervisor=AttemptSupervisor(
                clock=lambda: self._clock().timestamp()
            ),
            clock=self._clock,
        )
        with _deny_network_connections() as network_attempts:
            receipt = executor.execute(
                activity,
                attempt_id=f"{resolved_run_id}-agent-loop-attempt-1",
            )
        agent_result = recording_runner.result
        if agent_result is None:
            raise HarnessValidationError(
                "AgentLoop smoke runner did not produce a result",
                code="test_agent_loop_graph_result_missing",
            )
        verify = AgentLoopGraphSmokeVerifyGate().evaluate(
            _SmokeVerifyContext(
                preflight=preflight,
                receipt=receipt,
                result=agent_result,
                network_calls=len(network_attempts),
            )
        )
        if not verify.passed:
            raise HarnessValidationError(
                verify.reason or "test AgentLoop Graph VERIFY failed",
                code="test_agent_loop_graph_verify_failed",
                details=verify.details,
            )
        verify_evidence_ref = checksum_for(verify.to_dict())
        activity_receipt_ref = _activity_receipt_ref(receipt)
        outcome_ref = self._record_outcome(
            run_id=resolved_run_id,
            graph_ref=graph_ref,
            preflight=preflight,
            receipt=receipt,
            verify=verify,
            result=agent_result,
            network_calls=len(network_attempts),
        )
        staged = self._artifact_port.list_staged_artifacts(resolved_run_id)
        _assert_internal_graph_artifacts(staged)
        completed_at = self._clock()
        terminal_state_ref = checksum_for(
            {
                "run_id": resolved_run_id,
                "graph": graph_ref.to_dict(),
                "activity_receipt_ref": activity_receipt_ref,
                "verify_evidence_ref": verify_evidence_ref,
                "outcome_artifact_ref": outcome_ref.ref,
            }
        )
        terminal = GraphTerminalManifest(
            tenant_id="local-smoke",
            run_id=resolved_run_id,
            graph_id=graph.graph_id,
            graph_version=graph.identity_version,
            graph_schema_version=graph.schema_version,
            compiler_version=graph.compiler_version,
            normalized_graph_checksum=graph.checksum or "",
            status=GraphTerminalStatus.SUCCEEDED,
            started_at=started_at,
            completed_at=completed_at,
            terminal_state_ref=terminal_state_ref,
            checkpoint_ref=(
                f"graph-state://{resolved_run_id}/"
                f"{terminal_state_ref.removeprefix('sha256:')}"
            ),
            terminal_node_ids=graph.terminal_node_ids,
            gate_evidence_refs=(verify_evidence_ref,),
            artifacts=staged,
            publication=None,
        )
        manifest = GraphTerminalManifestV2(
            terminal=terminal,
            execution_versions=GraphExecutionVersionManifest.from_normalized_graph(
                graph
            ),
        )
        committed_manifest = self._artifact_port.write_terminal_manifest(manifest)
        if committed_manifest != manifest:
            raise HarnessValidationError(
                "Artifact owner returned a conflicting terminal manifest",
                code="test_agent_loop_terminal_manifest_mismatch",
            )
        persisted_outcome = self._artifact_port.read_graph_result_artifact(
            outcome_ref.ref,
            expected_run_id=resolved_run_id,
        )
        if persisted_outcome.get("payload", {}).get(
            "verify_evidence_ref"
        ) != verify_evidence_ref:
            raise HarnessValidationError(
                "persisted smoke outcome does not match VERIFY evidence",
                code="test_agent_loop_outcome_integrity_mismatch",
            )
        final_output = agent_result.output.get("analysis_result")
        metrics = agent_result.metrics.to_dict()
        return AgentLoopGraphSmokeResult(
            status=committed_manifest.status.value,
            run_id=resolved_run_id,
            graph_ref=graph_ref,
            node_instance_id=activity.node_instance_id,
            artifact_path=str(
                (self._artifact_root / resolved_run_id / "manifest.json").resolve()
            ),
            output=dict(final_output) if isinstance(final_output, Mapping) else {},
            metrics=metrics,
            event_types=_event_types(agent_result),
            preflight_ref=preflight_ref,
            activity_receipt_ref=activity_receipt_ref,
            verify_evidence_ref=verify_evidence_ref,
            manifest_hash=committed_manifest.manifest_hash or "",
            network_calls=len(network_attempts),
        )

    def _record_outcome(
        self,
        *,
        run_id: str,
        graph_ref: HarnessGraphReference,
        preflight: HarnessGraphValidationResult,
        receipt: HarnessGraphPhysicalActivityExecutionResult,
        verify: HarnessGateResult,
        result: AgentLoopResult,
        network_calls: int,
    ) -> ArtifactRef:
        verify_evidence_ref = checksum_for(verify.to_dict())
        projection = {
            "schema_version": AGENT_LOOP_SMOKE_OUTCOME_SCHEMA,
            "run_id": run_id,
            "graph": graph_ref.to_dict(),
            "node_id": receipt.activity.node_id,
            "node_instance_id": receipt.activity.node_instance_id,
            "preflight": preflight.to_dict(),
            "activity_receipt": _activity_receipt_projection(receipt),
            "verify": verify.to_dict(),
            "verify_evidence_ref": verify_evidence_ref,
            "output": redact_sensitive_values(dict(result.output)),
            "events": redact_sensitive_values([dict(item) for item in result.events]),
            "metrics": result.metrics.to_dict(),
            "network_calls": network_calls,
        }
        identity_checksum = checksum_for(projection)
        artifact_type = (
            "graph-result-" + identity_checksum.removeprefix("sha256:")
        )
        request = ArtifactWriteRequest(
            artifact_type=artifact_type,
            payload={**projection, "outcome_checksum": identity_checksum},
            metadata={
                "artifact_schema_version": AGENT_LOOP_SMOKE_OUTCOME_SCHEMA,
                "artifact_role": "agent_loop_smoke_outcome",
                "run_id": run_id,
                "graph_id": graph_ref.graph_id,
                "node_id": receipt.activity.node_id,
                "node_instance_id": receipt.activity.node_instance_id,
                "activity_id": receipt.activity.activity_id,
                "attempt_id": _physical_attempt_id(receipt),
                "candidate_checksum": (
                    receipt.node_output_commit.candidate.candidate_ref
                    if receipt.node_output_commit is not None
                    else None
                ),
                "verify_evidence_ref": verify_evidence_ref,
                "graph_result_ref_only": True,
                "identity_checksum": identity_checksum,
                "required_for_replay": True,
                "required_for_publication": False,
                "redacted": True,
                "llm_calls": result.metrics.llm_calls,
                "tool_calls": result.metrics.tool_calls,
                "token_usage": result.metrics.token_usage.to_dict(),
            },
        )
        with self._artifact_port.bind_run(run_id):
            artifact_ref = self._artifact_port.write_artifact(request)
            persisted = self._artifact_port.read_artifact(artifact_ref.ref)
        if persisted != request.to_dict():
            raise HarnessValidationError(
                "AgentLoop smoke outcome failed Artifact owner read-back",
                code="test_agent_loop_outcome_integrity_mismatch",
            )
        return artifact_ref


def _verified_memory_observation(context: _SmokeVerifyContext) -> bool:
    observations = [
        event.get("observation") for event in context.result.events
        if event.get("event_type") == "tool_observation"
    ]
    if len(observations) != 1 or not isinstance(observations[0], Mapping):
        return False
    result = observations[0].get("result")
    if not isinstance(result, Mapping) or result.get("status") != "succeeded":
        return False
    output = result.get("output")
    if not isinstance(output, Mapping) or output.get("result_count") != 1:
        return False
    diagnostics = output.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        return False
    refs = diagnostics.get("namespace_refs")
    checksums = diagnostics.get("namespace_checksums")
    if (
        not isinstance(refs, list)
        or len(refs) != 1
        or not all(isinstance(ref, str) and ref.startswith("memory-namespace://") for ref in refs)
        or not isinstance(checksums, Mapping)
    ):
        return False
    if set(checksums) != set(refs) or not all(
        isinstance(value, str) and value.startswith("sha256:")
        for value in (diagnostics.get("input_snapshot_ref"), *checksums.values())
    ):
        return False
    activity = context.receipt.activity
    expected = GraphExecutionIdentity(
        run_id=activity.run_id, graph_id=activity.graph_ref.graph_id,
        graph_version=activity.graph_ref.identity_version,
        graph_ref=activity.graph_ref.identity_ref.exact_ref,
        graph_checksum=activity.graph_ref.checksum, node_id=activity.node_id,
        node_instance_id=activity.node_instance_id,
        activity_id=activity.activity_id, attempt=activity.attempt,
    )
    return diagnostics.get("execution_identity") == expected.to_dict()


def build_test_agent_loop_graph() -> NormalizedHarnessGraph:
    return NormalizedHarnessGraph(
        graph_id=AGENT_LOOP_SMOKE_GRAPH_ID,
        graph_version=_GRAPH_REF.version,
        graph_ref=_GRAPH_REF,
        definition_schema_version="newsroom.harness-graph-definition/v6",
        definition_checksum=checksum_for(
            {
                "graph_id": AGENT_LOOP_SMOKE_GRAPH_ID,
                "graph_version": AGENT_LOOP_SMOKE_GRAPH_VERSION,
                "fixture": "offline",
            }
        ),
        nodes=(
            HarnessExecutableNode(
                node_id=AGENT_LOOP_SMOKE_NODE_ID,
                step_id=AGENT_LOOP_SMOKE_NODE_ID,
                declaration_order=0,
                step_ref=_STEP_REF,
                worker_ref=_WORKER_REF,
                activity_ref=_ACTIVITY_REF,
                gate_refs=(_VERIFY_GATE_CONTRACT_REF,),
                input_keys=("topic",),
                output_keys=(AGENT_LOOP_SMOKE_RESULT_KEY,),
                metadata={
                    "worker_type": HarnessWorkerType.AGENT_LOOP.value,
                    "leaf_activity_kind": HarnessLeafActivityKind.AGENT_LOOP.value,
                    "fixture": "offline",
                },
            ),
        ),
        edges=(),
        entry_node_ids=(AGENT_LOOP_SMOKE_NODE_ID,),
        terminal_node_ids=(AGENT_LOOP_SMOKE_NODE_ID,),
        input_keys=("topic",),
        terminal_output_keys=(AGENT_LOOP_SMOKE_RESULT_KEY,),
        terminal_policy_ref=HarnessContractReference(
            HarnessContractKind.TERMINAL_POLICY,
            "test-agent-loop.terminal",
            "1",
        ),
        terminal_policy=_smoke_terminal_policy(),
    )


def _smoke_terminal_policy():
    from framework.harness.side_effects.models import (
        HarnessSideEffectHandlerReference,
        HarnessTerminalSideEffectPolicy,
    )

    return HarnessTerminalSideEffectPolicy(
        policy_id="test-agent-loop.terminal",
        version="1",
        handler=HarnessSideEffectHandlerReference(
            handler_id="test-agent-loop.terminal-handler",
            version="1",
        ),
        kind="test-agent-loop-terminal",
        requires_approval=False,
        retry_limit=1,
        not_required_evidence_ref=checksum_for(
            {"fixture": "test-agent-loop", "evidence": "not-required"}
        ),
    )


class _InputResolver:
    def __init__(self, value: HarnessGraphActivityExecutionInput) -> None:
        self._value = value

    def resolve_execution_input(
        self,
        _activity: HarnessGraphActivity,
    ) -> HarnessGraphActivityExecutionInput:
        return self._value


class _ResultCommitter:
    def __init__(self) -> None:
        self._results: dict[str, HarnessGraphActivityResult] = {}

    def commit_execution_result(
        self,
        *,
        activity: HarnessGraphActivity,
        execution_input: HarnessGraphActivityExecutionInput,
        worker_result: HarnessWorkerResult | None,
        node_output_commit: HarnessNodeOutputCommit | None,
        result: HarnessGraphActivityResult,
    ) -> HarnessGraphActivityResult:
        del execution_input, worker_result, node_output_commit
        existing = self._results.get(activity.activity_id)
        if existing is not None and existing != result:
            raise HarnessValidationError(
                "Graph smoke activity result commit conflicts",
                code="test_agent_loop_activity_result_conflict",
            )
        self._results[activity.activity_id] = result
        return result


class _RecordingRunner:
    def __init__(
        self, delegate: AgentRunner, *, memory_recall: ExecutionMemoryRecallPort,
    ) -> None:
        self._delegate = delegate
        self._memory_recall = memory_recall
        self.result: AgentLoopResult | None = None

    def run(
        self,
        agent: AgentSpec,
        inputs: dict[str, Any],
        **kwargs: Any,
    ) -> AgentLoopResult:
        result = self._delegate.run(
            agent, inputs, memory_recall=self._memory_recall, **kwargs,
        )
        self.result = result
        return result


def _activity(
    *,
    run_id: str,
    graph_ref: HarnessGraphReference,
    raw_task: Mapping[str, Any],
) -> HarnessGraphActivity:
    identity = HarnessNodeInstanceIdentity(
        run_id=run_id,
        graph_checksum=graph_ref.checksum,
        node_id=AGENT_LOOP_SMOKE_NODE_ID,
        activation_ordinal=1,
    )
    return HarnessGraphActivity(
        run_id=run_id,
        graph_ref=graph_ref,
        node_id=AGENT_LOOP_SMOKE_NODE_ID,
        node_instance_id=identity.instance_id,
        step_ref=_STEP_REF,
        worker_ref=_WORKER_REF,
        activity_ref=_ACTIVITY_REF,
        attempt=1,
        input_ref=graph_activity_input_checksum(dict(raw_task)),
        causal_decision_checksum=checksum_for(
            {
                "run_id": run_id,
                "graph_checksum": graph_ref.checksum,
                "node_instance_id": identity.instance_id,
                "decision": "dispatch",
            }
        ),
        causal_decision_sequence=1,
        fencing_generation=1,
        tenant_scope_ref=checksum_for({"tenant_id": "local-smoke"}),
        identity_scope_ref=checksum_for({"identity": "test-agent-loop"}),
        subject_scope_ref=checksum_for({"topic": raw_task["inputs"]}),
    )


def _agent_spec(topic: str) -> AgentSpec:
    return AgentSpec(
        agent_id="test-analyst",
        name="Test Analyst",
        role="AnalystAgent",
        goal=f"Analyze deterministic fixture context for {topic}",
        instructions="Use allowed tools only and return JSON actions.",
        input_keys=["topic"],
        output_key="analysis_result",
        allowed_tools=["memory.recall"],
        memory_enabled=True,
        loop_policy=AgentLoopPolicy(
            memory_recall_enabled=False, memory_write_enabled=False,
        ),
    )


def _fake_llm(topic: str) -> FakeLLMClient:
    return FakeLLMClient(
        [
            json.dumps(
                {
                    "action_type": "tool_call",
                    "tool_name": "memory.recall",
                    "tool_args": {"query": topic},
                },
                sort_keys=True,
            ),
            json.dumps(
                {
                    "action_type": "final_output",
                    "output": {"wrong_key": {"summary": "missing"}},
                },
                sort_keys=True,
            ),
            json.dumps(
                {
                    "action_type": "final_output",
                    "output": {
                        "analysis_result": {
                            "summary": (
                                "Deterministic AgentLoop analysis for " + topic + "."
                            ),
                            "tool_used": "memory.recall",
                            "confidence": "high",
                        }
                    },
                },
                sort_keys=True,
            ),
        ]
    )


@contextmanager
def _deny_network_connections() -> Iterator[list[str]]:
    attempts: list[str] = []

    def reject(*_args: Any, **_kwargs: Any) -> Any:
        attempts.append("blocked")
        raise HarnessValidationError(
            "test AgentLoop Graph attempted real network access",
            code="test_agent_loop_network_access_forbidden",
        )

    with _NETWORK_GUARD_LOCK:
        with (
            patch.object(socket.socket, "connect", reject),
            patch.object(socket.socket, "connect_ex", reject),
            patch("socket.create_connection", reject),
        ):
            yield attempts


def _activity_receipt_projection(
    receipt: HarnessGraphPhysicalActivityExecutionResult,
) -> dict[str, Any]:
    attempt = receipt.attempt
    return {
        "activity": receipt.activity.to_dict(),
        "execution_input": receipt.execution_input.to_dict(),
        "attempt": (
            None
            if attempt is None
            else {
                "state": attempt.outcome.state.value,
                "started": attempt.outcome.started,
                "timed_out": attempt.outcome.timed_out,
                "termination_confirmed": attempt.outcome.termination_confirmed,
                "indeterminate": attempt.outcome.indeterminate,
                "reason_code": attempt.outcome.reason_code,
                "admission_ref": (
                    None if attempt.admission is None else attempt.admission.admission_ref
                ),
            }
        ),
        "worker_result_ref": (
            None
            if receipt.worker_result is None
            else receipt.worker_result.candidate_result_ref
        ),
        "node_output_commit": (
            None
            if receipt.node_output_commit is None
            else receipt.node_output_commit.to_dict()
        ),
        "graph_result": (
            None if receipt.graph_result is None else receipt.graph_result.to_dict()
        ),
        "recovered_output": receipt.recovered_output,
    }


def _activity_receipt_ref(
    receipt: HarnessGraphPhysicalActivityExecutionResult,
) -> str:
    return checksum_for(_activity_receipt_projection(receipt))


def _agent_result_verify_projection(result: AgentLoopResult) -> dict[str, Any]:
    return {
        "success": result.success,
        "status": result.status.value,
        "output": redact_sensitive_values(dict(result.output)),
        "verdict": None if result.verdict is None else result.verdict.to_dict(),
        "metrics": result.metrics.to_dict(),
        "event_types": list(_event_types(result)),
        "llm_call_artifact_ids": [
            item.artifact_id for item in result.llm_call_artifacts
        ],
    }


def _physical_attempt_id(
    receipt: HarnessGraphPhysicalActivityExecutionResult,
) -> str:
    attempt = receipt.attempt
    context = None if attempt is None else attempt.outcome.context
    if context is None:
        raise HarnessValidationError(
            "test AgentLoop Graph has no admitted physical attempt identity",
            code="test_agent_loop_attempt_identity_missing",
        )
    return context.attempt_id


def _event_types(result: AgentLoopResult) -> tuple[str, ...]:
    return tuple(str(item.get("event_type") or "unknown") for item in result.events)


def _worker_requested_tools(
    worker: HarnessWorkerResult | None,
) -> tuple[list[str], bool]:
    if worker is None:
        return [], False
    raw_tools = worker.diagnostics.get("requested_tools")
    if not isinstance(raw_tools, list):
        return [], False
    tools: list[str] = []
    for value in raw_tools:
        if (
            not isinstance(value, str)
            or not value
            or value.strip() != value
        ):
            return [], False
        tools.append(value)
    return tools, tools == sorted(set(tools))


def _assert_internal_graph_artifacts(
    artifacts: tuple[GraphTerminalArtifact, ...],
) -> None:
    if not artifacts:
        raise HarnessValidationError(
            "test AgentLoop Graph produced no staged artifacts",
            code="test_agent_loop_artifacts_missing",
        )
    invalid: list[str] = []
    for artifact in artifacts:
        prefix = "graph-result-"
        suffix = (
            artifact.artifact_key.removeprefix(prefix)
            if artifact.artifact_key.startswith(prefix)
            else ""
        )
        if (
            len(suffix) != 64
            or any(character not in "0123456789abcdef" for character in suffix)
            or artifact.metadata.get("graph_result_ref_only") is not True
            or artifact.metadata.get("identity_checksum") != f"sha256:{suffix}"
        ):
            invalid.append(artifact.artifact_key)
    if invalid:
        raise HarnessValidationError(
            "test AgentLoop Graph staged non-internal artifacts",
            code="test_agent_loop_artifact_acceptance_failed",
            details={"artifact_keys": sorted(invalid)},
        )


def _checkpoint_ref(
    run_id: str,
    graph_ref: HarnessGraphReference,
    *,
    phase: str,
) -> str:
    digest = checksum_for(
        {
            "run_id": run_id,
            "graph_checksum": graph_ref.checksum,
            "phase": phase,
        }
    ).removeprefix("sha256:")
    return f"graph-state://{run_id}/{digest}"


def _graph_ref_text(graph_ref: HarnessGraphReference) -> str:
    return (
        f"{graph_ref.graph_id}@{graph_ref.identity_version}#"
        f"{graph_ref.checksum}"
    )


def _required_text(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 2048
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise HarnessValidationError(
            f"{field_name} must be non-blank text",
            code="test_agent_loop_request_invalid",
        )
    return value


__all__ = [
    "AGENT_LOOP_SMOKE_EVENT_TYPES",
    "AGENT_LOOP_SMOKE_GRAPH_ID",
    "AGENT_LOOP_SMOKE_GRAPH_VERSION",
    "AGENT_LOOP_SMOKE_NODE_ID",
    "AGENT_LOOP_SMOKE_OUTCOME_SCHEMA",
    "AGENT_LOOP_SMOKE_RESULT_SCHEMA",
    "AGENT_LOOP_SMOKE_VERIFY_GATE_REF",
    "AgentLoopGraphSmokeApplicationService",
    "AgentLoopGraphSmokeResult",
    "AgentLoopGraphSmokeVerifyGate",
    "AgentLoopSmokeArtifactPort",
    "build_test_agent_loop_graph",
]
