from __future__ import annotations

import json
import inspect
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from framework.agent.loop.runner import AgentRunner
from framework.agent.models import (
    AgentLoopDiagnosticSeverity,
    AgentLoopDiagnostics,
    AgentLoopIssue,
    AgentLoopMetrics,
    AgentLoopResult,
    AgentLoopStatus,
    AgentLoopStopReason,
    AgentSpec,
    LLMCallArtifact,
)
from framework.events.canonical import checksum_for
from framework.events.runtime.publisher import EventRuntime
from framework.events.runtime.models import StreamReadRequest
from framework.events.schema import EventSecurityProjector, default_event_schema_catalog
from framework.harness.agent_loop import (
    AGENT_LOOP_GRAPH_ACTIVITY_TASK_SCHEMA,
    AgentLoopGraphApprovalWaitBinding,
)
from framework.harness.control_plane import (
    HarnessRunSpec,
    InMemoryHarnessEventPort,
)
from framework.harness.control_plane.durable_events import (
    DurableHarnessTransitionPort,
    HarnessEventCanonicalAdapter,
)
from framework.harness.control_plane.activity_execution import (
    HarnessGraphActivityExecutionInput,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.graph_runtime import HarnessGraphActivity
from framework.harness.graph import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_CONDITION_POLICY_VERSION,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
    HarnessContractKind,
    HarnessContractReference,
    HarnessLeafActivityKind,
    HarnessGraphDefinition,
    HarnessGraphLeafBinding,
    HarnessGraphSpec,
    HarnessStepSpec,
    HarnessWorkerType,
    Choice,
    ChoiceBranch,
    Sequence,
    StepRef,
    Wait,
    graph_activity_input_checksum,
)
from framework.harness.graph.bindings import HarnessActivityUsage
from framework.harness.graph.reference import HarnessGraphReference
from framework.harness.side_effects import (
    CountingHarnessSideEffectHandler,
    HarnessSideEffectDisposition,
    HarnessSideEffectHandlerBinding,
    HarnessSideEffectRegistry,
    HarnessTerminalSideEffectPolicy,
    InMemoryHarnessSideEffectStore,
)
from framework.llm import FakeLLMClient
from framework.tool import ToolRegistry
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from infrastructure.storage.conversation import LocalJsonConversationStore
from infrastructure.storage.harness import (
    SQLiteHarnessNodeOutputResource,
    SQLiteHarnessSideEffectStore,
)
from infrastructure.storage.events import SQLiteEventStore
from infrastructure.storage.events.activity_store import SQLiteRecordedActivityStore
from interfaces.composition.agent_loop_graph import (
    build_agent_loop_graph_application_service,
    build_agent_loop_graph_runtime_composition,
)
from interfaces.composition.runtime_execution import build_process_execution_composition
from framework.execution_environment.errors import RuntimeCompositionDriftError
from interfaces.services.agent_loop_graph_service import (
    AgentLoopGraphApplicationService,
    SQLiteAgentLoopActivityResultStore,
)
from framework.harness.control_plane.graph_state import HarnessNodeInstanceIdentity


WORKER_REF = HarnessContractReference(
    HarnessContractKind.WORKER, "production.agent-loop", "1"
)
_DURABLE_ACTIVITY_KEY = Fernet.generate_key()
ACTIVITY_REF = HarnessContractReference(
    HarnessContractKind.ACTIVITY, "production.agent-loop", "1"
)
GRAPH_REF = HarnessGraphReference(
    graph_id="production.agent-loop.graph",
    graph_ref=HarnessContractReference(
        HarnessContractKind.GRAPH, "production.agent-loop.graph", "1"
    ),
    schema_version=GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    compiler_version=HARNESS_GRAPH_ONLY_COMPILER_VERSION,
    condition_policy_version=HARNESS_CONDITION_POLICY_VERSION,
    checksum=checksum_for({"graph": "production.agent-loop.graph", "version": "1"}),
)


def _runner(root: Path, topic: str) -> AgentRunner:
    return AgentRunner(
        llm_client=FakeLLMClient(
            [
                json.dumps(
                    {
                        "action_type": "final_output",
                        "output": {"analysis_result": {"topic": topic}},
                    }
                )
            ]
        ),
        tool_registry=ToolRegistry(),
        conversation_store=LocalJsonConversationStore(root / "conversation"),
    )


def _agent() -> AgentSpec:
    return AgentSpec(
        agent_id="production-agent",
        name="Production Agent",
        role="Analyst",
        goal="Produce one bounded result",
        instructions="Return one JSON result.",
        output_key="analysis_result",
    )


class _WaitingAgentRunner(AgentRunner):
    """Graph-dispatch fake: the Harness owns the resulting Wait registration."""

    def run(self, *_args, **_kwargs) -> AgentLoopResult:
        return AgentLoopResult(
            success=False,
            status=AgentLoopStatus.WAITING_FOR_APPROVAL,
            iterations=1,
            metrics=AgentLoopMetrics(
                iterations=1,
                llm_calls=1,
                tool_approval_requests=1,
            ),
            diagnostics=AgentLoopDiagnostics(
                agent_id="production-agent",
                status=AgentLoopStatus.WAITING_FOR_APPROVAL,
                stop_reason=AgentLoopStopReason.TOOL_APPROVAL_REQUIRED,
                summary="approval required",
                healthy=False,
                severity=AgentLoopDiagnosticSeverity.WARNING,
                iterations=1,
                approval_requests=1,
                issues=[
                    AgentLoopIssue(
                        code="tool_approval_required",
                        message="approval required",
                        severity=AgentLoopDiagnosticSeverity.WARNING,
                        iteration=1,
                        tool_name="test.approval",
                        metadata={
                            "approval_id": "approval-agent-loop-1",
                            "approval_kind": "tool_approval",
                        },
                    ),
                ],
            ),
            llm_call_artifacts=[
                LLMCallArtifact(
                    artifact_id="production-agent:llm_call:1",
                    iteration=1,
                    request={"messages": []},
                    response={"tool_call": "test.approval"},
                    metadata={"agent_id": "production-agent", "provider": "fake"},
                ),
            ],
            error="approval required",
            termination_reason=AgentLoopStopReason.TOOL_APPROVAL_REQUIRED.value,
        )


def _activity_and_input(run_id: str, *, leaf_kind=HarnessLeafActivityKind.AGENT_LOOP):
    task = {
        "schema_version": AGENT_LOOP_GRAPH_ACTIVITY_TASK_SCHEMA,
        "inputs": {"topic": "recovery-topic"},
        "conversation_id": f"{run_id}-conversation",
        "resume_from_cursor": False,
    }
    identity = HarnessNodeInstanceIdentity(
        run_id=run_id,
        graph_checksum=GRAPH_REF.checksum,
        node_id="run-agent-loop",
        activation_ordinal=1,
    )
    activity = HarnessGraphActivity(
        run_id=run_id,
        graph_ref=GRAPH_REF,
        node_id="run-agent-loop",
        node_instance_id=identity.instance_id,
        step_ref=HarnessContractReference(HarnessContractKind.STEP, "run-agent-loop", "1"),
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
        attempt=1,
        input_ref=graph_activity_input_checksum(task),
        causal_decision_checksum=checksum_for({"decision": "dispatch", "run_id": run_id}),
        causal_decision_sequence=1,
        fencing_generation=1,
        tenant_scope_ref=checksum_for({"tenant": "production"}),
        identity_scope_ref=checksum_for({"run": run_id}),
        subject_scope_ref=checksum_for({"topic": "recovery-topic"}),
    )
    execution_input = HarnessGraphActivityExecutionInput.for_activity(
        activity,
        task=task,
        leaf_activity_kind=leaf_kind,
        required_usage=HarnessActivityUsage.SERIAL,
        graph_checkpoint_ref=f"graph-state://{run_id}/checkpoint",
        output_keys=("agent_loop_result",),
    )
    return activity, execution_input


def _service(tmp_path: Path) -> AgentLoopGraphApplicationService:
    root = tmp_path / "artifacts"
    return build_agent_loop_graph_application_service(
        agent_runner=_runner(root, "recovery-topic"),
        artifact_port=FilesystemHarnessArtifactPort(root),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
        result_committer=SQLiteAgentLoopActivityResultStore(root / "results.sqlite3"),
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
    )


def _durable_event_port(root: Path) -> DurableHarnessTransitionPort:
    event_store = SQLiteEventStore(root / "events.sqlite3")
    activity_store = SQLiteRecordedActivityStore(
        root / "activities.sqlite3",
        encryption_key=_DURABLE_ACTIVITY_KEY,
    )
    runtime = EventRuntime(
        store=event_store,
        schema_catalog=default_event_schema_catalog(),
        security_projector=EventSecurityProjector(
            secure_payload_store=activity_store,
        ),
    )
    return DurableHarnessTransitionPort(
        runtime,
        event_store,
        secure_activity_store=activity_store,
        adapter=HarnessEventCanonicalAdapter(tenant_id="production"),
    )


def _terminal_side_effects_for(
    database: Path,
) -> tuple[
    HarnessSideEffectRegistry,
    SQLiteHarnessSideEffectStore,
    CountingHarnessSideEffectHandler,
]:
    store = SQLiteHarnessSideEffectStore(database)
    handler = CountingHarnessSideEffectHandler(
        store,
        disposition=HarnessSideEffectDisposition.ACCEPTED,
    )
    return (
        HarnessSideEffectRegistry(
            (
                HarnessSideEffectHandlerBinding(
                    "production.agent-loop-terminal@1",
                    "artifact",
                    handler,
                ),
            )
        ),
        store,
        handler,
    )


def test_composition_factory_requires_explicit_durable_ports(tmp_path: Path) -> None:
    signature = inspect.signature(build_agent_loop_graph_application_service)
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in signature.parameters.values()
    )
    service = _service(tmp_path)
    assert isinstance(service, AgentLoopGraphApplicationService)


def test_service_executes_and_recovers_without_second_worker_call(tmp_path: Path) -> None:
    service = _service(tmp_path)
    activity, execution_input = _activity_and_input("agent-loop-production-1")
    first = service.execute(
        activity=activity,
        execution_input=execution_input,
        agent=_agent(),
        attempt_id="agent-loop-production-1-attempt-1",
    )
    assert first.node_output_commit is not None
    second = service.execute(
        activity=activity,
        execution_input=execution_input,
        agent=_agent(),
    )
    assert second.recovered_output is True
    assert second.node_output_commit == first.node_output_commit
    assert second.graph_result == first.graph_result


def test_result_store_reads_only_the_exact_graph_activity_partition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "partitioned-results"
    store = SQLiteAgentLoopActivityResultStore(root / "results.sqlite3")
    service = build_agent_loop_graph_application_service(
        agent_runner=_runner(root, "partition-topic"),
        artifact_port=FilesystemHarnessArtifactPort(root),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
        result_committer=store,
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
    )
    activity, execution_input = _activity_and_input("agent-loop-partition-1")
    receipt = service.execute(
        activity=activity,
        execution_input=execution_input,
        agent=_agent(),
    )
    assert receipt.graph_result is not None
    assert store.read(activity) == receipt.graph_result

    other_activity, _ = _activity_and_input("agent-loop-partition-2")
    assert store.read(other_activity) is None


def test_result_store_quarantines_legacy_activity_id_only_schema(tmp_path: Path) -> None:
    path = tmp_path / "legacy-results.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE agent_loop_activity_results ("
        "activity_id TEXT PRIMARY KEY, activity_checksum TEXT NOT NULL, "
        "result_checksum TEXT NOT NULL, result_json TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO agent_loop_activity_results VALUES (?, ?, ?, ?)",
        ("ambiguous-activity", "sha256:" + "0" * 64, "sha256:" + "1" * 64, "{}"),
    )
    connection.commit()
    connection.close()

    store = SQLiteAgentLoopActivityResultStore(path)
    activity, _ = _activity_and_input("agent-loop-legacy-schema")
    assert store.read(activity) is None
    columns = {
        row[1]
        for row in store._connection.execute(  # noqa: SLF001 - migration assertion
            "PRAGMA table_info(agent_loop_activity_results)"
        ).fetchall()
    }
    assert {"run_id", "graph_checksum", "node_instance_id", "attempt"}.issubset(
        columns
    )
    retained = store._connection.execute(  # noqa: SLF001 - migration assertion
        "SELECT activity_id FROM agent_loop_activity_results_legacy_untrusted"
    ).fetchone()
    assert retained[0] == "ambiguous-activity"
    store.close()


def test_runtime_composition_installs_agent_loop_into_the_graph_dispatcher(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime-composition"
    event_port = _durable_event_port(root)
    runner = _runner(root, "runtime-topic")
    side_effect_store = InMemoryHarnessSideEffectStore()
    side_effect_registry = HarnessSideEffectRegistry(
        (
            HarnessSideEffectHandlerBinding(
                "production.agent-loop-terminal@1",
                "artifact",
                CountingHarnessSideEffectHandler(
                    side_effect_store,
                    disposition=HarnessSideEffectDisposition.ACCEPTED,
                ),
            ),
        )
    )
    runtime = build_agent_loop_graph_runtime_composition(
        agent_runner=runner,
        agent=_agent(),
        artifact_port=FilesystemHarnessArtifactPort(root),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
        event_port=event_port,
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
        side_effect_registry=side_effect_registry,
        side_effect_store=side_effect_store,
    )
    run_spec = _runtime_run_spec(
        "agent-loop-runtime-composition",
        identity_scope_ref=checksum_for("production"),
    )

    result = runtime.run(run_spec)

    assert result.succeeded is True
    assert result.worker_results
    assert runtime.binding_bundle.authority.resolve_gate(
        "agent_loop_wait_candidate@1"
    )[0].gate is runtime.binding_bundle.wait_gate_registration.gate
    assert any(
        event.event_type.value == "graph_worker_result_recorded"
        for event in runtime.control_plane.event_port.read_history(run_spec.run_id)
    )
    assert runtime.runtime_event_publisher is event_port.runtime_event_publisher
    assert runner.runtime_event_sink is runtime.runtime_event_publisher
    assert runtime.runtime_event_publisher.tenant_id == "production"
    assert runtime.runtime_event_publisher.runtime is not None
    assert runtime.runtime_event_publisher.is_durable is True
    stored = SQLiteEventStore(root / "events.sqlite3").read_stream(
        StreamReadRequest(stream_id=run_spec.run_id, tenant_id="production")
    ).events
    assert any(
        event.event_type in {"turn_started", "turn_stopped"}
        for event in stored
    ), [event.to_dict() for event in stored]
    assert all(event.tenant_id == "production" for event in stored)
    assert all(event.record_checksum.startswith("sha256:") for event in stored)
    assert runtime.runtime_event_publisher is runtime.control_plane.event_port.runtime_event_publisher
    assert runtime.runtime_event_publisher is not None


def test_runtime_composition_rejects_durable_port_without_canonical_runtime_publisher(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime-missing-runtime-publisher"

    event_port = InMemoryHarnessEventPort()
    event_port.is_durable = True

    with pytest.raises(ValueError, match="canonical durable runtime event publisher"):
        build_agent_loop_graph_runtime_composition(
            agent_runner=_runner(root, "runtime-topic"),
            agent=_agent(),
            artifact_port=FilesystemHarnessArtifactPort(root),
            node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
            event_port=event_port,
            worker_ref=WORKER_REF,
            activity_ref=ACTIVITY_REF,
        )


def test_runtime_composition_binds_shared_execution_registry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime-composition-shared"
    composition = build_process_execution_composition()
    runner = AgentRunner(
        llm_client=FakeLLMClient(
            [
                json.dumps(
                    {
                        "action_type": "final_output",
                        "output": {"analysis_result": {"topic": "runtime-topic"}},
                    }
                )
            ]
        ),
        tool_registry=ToolRegistry(),
        conversation_store=LocalJsonConversationStore(root / "conversation"),
        execution_environment=composition.execution_registry,
        require_explicit_execution_profile=composition.require_explicit_execution_profile,
    )
    runtime = build_agent_loop_graph_runtime_composition(
        agent_runner=runner,
        agent=_agent(),
        artifact_port=FilesystemHarnessArtifactPort(root),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
        event_port=_durable_event_port(root),
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
        runtime_execution_composition=composition,
    )

    assert runtime.runtime_execution_composition is composition


def test_runtime_composition_rejects_runner_registry_drift(tmp_path: Path) -> None:
    root = tmp_path / "runtime-composition-drift"
    composition = build_process_execution_composition()
    with pytest.raises(RuntimeCompositionDriftError, match="execution registry"):
        build_agent_loop_graph_runtime_composition(
            agent_runner=_runner(root, "runtime-topic"),
            agent=_agent(),
            artifact_port=FilesystemHarnessArtifactPort(root),
            node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
            event_port=_durable_event_port(root),
            worker_ref=WORKER_REF,
            activity_ref=ACTIVITY_REF,
            runtime_execution_composition=composition,
        )


def test_runtime_composition_registers_approval_wait_through_harness_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime-wait"
    runner = _WaitingAgentRunner(
        llm_client=FakeLLMClient(
            []
        ),
        tool_registry=ToolRegistry(),
        conversation_store=LocalJsonConversationStore(root / "conversation"),
    )
    side_effect_store = InMemoryHarnessSideEffectStore()
    side_effect_registry = HarnessSideEffectRegistry(
        (
            HarnessSideEffectHandlerBinding(
                "production.agent-loop-terminal@1",
                "artifact",
                CountingHarnessSideEffectHandler(side_effect_store),
            ),
        )
    )
    runtime = build_agent_loop_graph_runtime_composition(
        agent_runner=runner,
        agent=_agent(),
        artifact_port=FilesystemHarnessArtifactPort(root),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
        event_port=_durable_event_port(root),
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
        side_effect_registry=side_effect_registry,
        side_effect_store=side_effect_store,
    )
    result = runtime.run(
        _approval_runtime_run_spec(
            "agent-loop-approval-wait",
            identity_scope_ref=checksum_for("production"),
        )
    )

    assert result.status.value == "waiting_approval"
    assert len(result.state.wait_registrations) == 1
    registration = result.state.wait_registrations[0]
    assert registration.kind.value == "approval"
    assert registration.status.value == "registered"
    assert any(
        decision.decision_type.value == "register_wait"
        for decision in result.decisions
    )


def test_runtime_composition_rejects_in_memory_event_port(tmp_path: Path) -> None:
    root = tmp_path / "runtime-rejects-memory"
    with pytest.raises(ValueError, match="durable HarnessTransitionPort"):
        build_agent_loop_graph_runtime_composition(
            agent_runner=_runner(root, "runtime-topic"),
            agent=_agent(),
            artifact_port=FilesystemHarnessArtifactPort(root),
            node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
            event_port=InMemoryHarnessEventPort(),
            worker_ref=WORKER_REF,
            activity_ref=ACTIVITY_REF,
        )


def test_runtime_composition_recovers_from_durable_history_in_new_instance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime-recovery"
    run_spec = _runtime_run_spec(
        "agent-loop-cross-instance-recovery",
        identity_scope_ref=checksum_for("production"),
    )
    registry, side_effect_store, first_handler = _terminal_side_effects_for(
        root / "side-effects.sqlite3"
    )
    first = build_agent_loop_graph_runtime_composition(
        agent_runner=_runner(root, "runtime-topic"),
        agent=_agent(),
        artifact_port=FilesystemHarnessArtifactPort(root),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
        event_port=_durable_event_port(root),
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
        side_effect_registry=registry,
        side_effect_store=side_effect_store,
    )
    assert first.run(run_spec).succeeded is True

    second_registry, second_side_effect_store, second_handler = (
        _terminal_side_effects_for(root / "side-effects.sqlite3")
    )
    second = build_agent_loop_graph_runtime_composition(
        agent_runner=_runner(root, "runtime-topic"),
        agent=_agent(),
        artifact_port=FilesystemHarnessArtifactPort(root),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
        event_port=_durable_event_port(root),
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
        side_effect_registry=second_registry,
        side_effect_store=second_side_effect_store,
    )
    recovered = second.recover_and_run(run_spec)
    assert recovered.succeeded is True
    assert recovered.state is not None
    assert recovered.state.outcome.value == "succeeded"
    assert first_handler.effect_count == 1
    assert second_handler.effect_count == 0


def test_runtime_composition_resumes_approval_in_new_instance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime-approval-recovery"
    run_spec = _approval_runtime_run_spec(
        "agent-loop-cross-instance-approval",
        identity_scope_ref=checksum_for("production"),
    )
    registry, side_effect_store, _first_handler = _terminal_side_effects_for(
        root / "side-effects.sqlite3"
    )
    first = build_agent_loop_graph_runtime_composition(
        agent_runner=_WaitingAgentRunner(
            llm_client=FakeLLMClient([]),
            tool_registry=ToolRegistry(),
            conversation_store=LocalJsonConversationStore(root / "conversation"),
        ),
        agent=_agent(),
        artifact_port=FilesystemHarnessArtifactPort(root),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
        event_port=_durable_event_port(root),
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
        side_effect_registry=registry,
        side_effect_store=side_effect_store,
    )
    waiting = first.run(run_spec)
    assert waiting.status.value == "waiting_approval"

    second_registry, second_side_effect_store, _second_handler = (
        _terminal_side_effects_for(root / "side-effects.sqlite3")
    )
    second = build_agent_loop_graph_runtime_composition(
        agent_runner=_WaitingAgentRunner(
            llm_client=FakeLLMClient([]),
            tool_registry=ToolRegistry(),
            conversation_store=LocalJsonConversationStore(root / "conversation"),
        ),
        agent=_agent(),
        artifact_port=FilesystemHarnessArtifactPort(root),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
        event_port=_durable_event_port(root),
        worker_ref=WORKER_REF,
        activity_ref=ACTIVITY_REF,
        side_effect_registry=second_registry,
        side_effect_store=second_side_effect_store,
    )
    resumed = second.resume_after_approval(
        run_spec,
        approved=True,
        approval_ref=checksum_for({"approval": run_spec.run_id}),
        actor_identity_scope_ref=checksum_for({"actor": "reviewer"}),
    )
    assert resumed.succeeded is True


def test_service_rejects_non_agent_loop_leaf_before_worker(tmp_path: Path) -> None:
    service = _service(tmp_path)
    activity, execution_input = _activity_and_input(
        "agent-loop-production-invalid",
        leaf_kind=HarnessLeafActivityKind.FUNCTION,
    )
    with pytest.raises(HarnessValidationError, match="agent_loop"):
        service.execute(
            activity=activity,
            execution_input=execution_input,
            agent=_agent(),
        )


def _runtime_run_spec(
    run_id: str,
    *,
    identity_scope_ref: str | None = None,
) -> HarnessRunSpec:
    step = HarnessStepSpec(
        "run-agent-loop",
        HarnessWorkerType.AGENT_LOOP,
        input_keys=("inputs", "conversation_id", "resume_from_cursor"),
        output_key="agent_loop_result",
    )
    definition = HarnessGraphDefinition(
        graph_id="production.agent-loop.runtime",
        graph_version="1",
        root=HarnessGraphSpec(
            graph_id="production.agent-loop.runtime",
            root=StepRef("run-agent-loop"),
            input_keys=(
                "inputs",
                "conversation_id",
                "resume_from_cursor",
                "tenant_scope_ref",
                "identity_scope_ref",
            ),
            terminal_output_keys=("agent_loop_result",),
        ),
        activities=(step,),
        leaf_activity_bindings=(
            HarnessGraphLeafBinding(
                activity_id="run-agent-loop",
                leaf_activity_kind=HarnessLeafActivityKind.AGENT_LOOP,
                worker_ref=WORKER_REF,
                activity_ref=ACTIVITY_REF,
            ),
        ),
        task_plan_stage_bindings=(),
        committed_output_bindings=(),
        repair_bindings=(),
        terminal_side_effect_policy=HarnessTerminalSideEffectPolicy(
            policy_id="production.agent-loop-terminal",
            version="1",
            handler="production.agent-loop-terminal@1",
            kind="artifact",
            requires_approval=False,
            retry_limit=1,
            not_required_evidence_ref=checksum_for(
                {"terminal": "production.agent-loop"}
            ),
        ),
    )
    tenant_scope_ref = checksum_for({"tenant": "production"})
    identity_scope_ref = identity_scope_ref or checksum_for({"identity": run_id})
    return HarnessRunSpec(
        run_id=run_id,
        graph=definition,
        inputs={
            "inputs": {"topic": "runtime-topic"},
            "conversation_id": f"{run_id}-conversation",
            "resume_from_cursor": False,
            "tenant_scope_ref": tenant_scope_ref,
            "identity_scope_ref": identity_scope_ref,
        },
        metadata={
            "tenant_scope_ref": tenant_scope_ref,
            "identity_scope_ref": identity_scope_ref,
            "subject_scope_ref": checksum_for({"subject": run_id}),
        },
    )


def _approval_runtime_run_spec(
    run_id: str,
    *,
    identity_scope_ref: str | None = None,
) -> HarnessRunSpec:
    wait_binding = AgentLoopGraphApprovalWaitBinding(
        source_node_id="run-agent-loop",
        result_output_key="agent_loop_result",
        wait_id="agent-loop-approval",
    )
    step = HarnessStepSpec(
        "run-agent-loop",
        HarnessWorkerType.AGENT_LOOP,
        input_keys=("inputs", "conversation_id", "resume_from_cursor"),
        output_key="agent_loop_result",
        quality_gate=wait_binding.gate_ref,
        metadata={
            "control_fact_paths": wait_binding.control_fact_paths,
            "tool_allowlist": ["test.approval"],
        },
    )
    fallback_wait = Wait(
        wait_id="unexpected-agent-loop-result",
        kind="signal",
        correlation={"run_id": "graph.inputs.identity_scope_ref"},
        signal_type="newsroom.agent-loop.unexpected-result",
        signal_version="1",
        tenant_scope_path="graph.inputs.tenant_scope_ref",
        identity_scope_path="graph.inputs.identity_scope_ref",
    )
    definition = HarnessGraphDefinition(
        graph_id="production.agent-loop.approval",
        graph_version="1",
        root=HarnessGraphSpec(
            graph_id="production.agent-loop.approval",
            root=Sequence(
                (
                    StepRef("run-agent-loop"),
                    Choice(
                        "agent-loop-approval-choice",
                        (
                            ChoiceBranch(
                                "waiting",
                                wait_binding.wait_expression(),
                                priority=1,
                                condition=wait_binding.waiting_condition(),
                            ),
                            ChoiceBranch(
                                "unexpected",
                                fallback_wait,
                                priority=2,
                                is_default=True,
                            ),
                        ),
                    ),
                )
            ),
            input_keys=(
                "inputs",
                "conversation_id",
                "resume_from_cursor",
                "tenant_scope_ref",
                "identity_scope_ref",
            ),
        ),
        activities=(step,),
        leaf_activity_bindings=(
            HarnessGraphLeafBinding(
                activity_id="run-agent-loop",
                leaf_activity_kind=HarnessLeafActivityKind.AGENT_LOOP,
                worker_ref=WORKER_REF,
                activity_ref=ACTIVITY_REF,
            ),
        ),
        task_plan_stage_bindings=(),
        committed_output_bindings=(),
        repair_bindings=(),
        terminal_side_effect_policy=HarnessTerminalSideEffectPolicy(
            policy_id="production.agent-loop-terminal",
            version="1",
            handler="production.agent-loop-terminal@1",
            kind="artifact",
            requires_approval=False,
            retry_limit=1,
            not_required_evidence_ref=checksum_for(
                {"terminal": "production.agent-loop"}
            ),
        ),
    )
    tenant_scope_ref = checksum_for({"tenant": "production"})
    identity_scope_ref = identity_scope_ref or checksum_for({"identity": run_id})
    return HarnessRunSpec(
        run_id=run_id,
        graph=definition,
        inputs={
            "inputs": {"topic": "approval-topic"},
            "conversation_id": f"{run_id}-conversation",
            "resume_from_cursor": False,
            "tenant_scope_ref": tenant_scope_ref,
            "identity_scope_ref": identity_scope_ref,
        },
        metadata={
            "tenant_scope_ref": tenant_scope_ref,
            "identity_scope_ref": identity_scope_ref,
            "subject_scope_ref": checksum_for({"subject": run_id}),
        },
    )
