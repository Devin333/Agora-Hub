from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from framework.agent.models import AgentLoopPolicy, AgentSpec
from framework.events.canonical import checksum_for
from framework.execution_environment import ExecutionEnvironmentRegistry, ExecutionProfile
from framework.governance.budget import BudgetScopeType
from framework.harness.context.models import ContextEnvelope
from framework.harness.control_plane.activity_execution import (
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.graph_runtime import HarnessGraphActivity
from framework.harness.graph.activity import graph_activity_input_checksum
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.graph.reference import HarnessGraphReference
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_authority import (
    REF_KIND_INPUT,
    REF_KIND_RESULT,
    RefAccessMode,
    RefAccessPolicy,
    RefDescriptor,
    RefScope,
)
from framework.harness.ref_dependency_binding import DependencyResultBinding
from framework.harness.ref_snapshot import RefAuthoritySnapshot
from framework.harness.subagents.agent_runner import ChildAgentRunnerAdapter
from framework.harness.subagents.fake import FakeSubAgentRuntime, fake_subagent_spec
from framework.harness.subagents.models import SubAgentInvocation
from framework.harness.subagents.transcript import SubAgentAttemptIdentity
from framework.harness.subagents.tool_evidence import (
    ChildToolEvidenceLimits,
    ChildToolEvidenceScope,
)
from framework.llm import FakeLLMClient, GlobalBudgetPolicy, GlobalBudgetTracker
from framework.shared.attempts import AttemptIdentity, AttemptOutcome
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import (
    ToolCall,
    ToolDefinition,
    ToolPolicy,
    ToolRegistry,
    ToolRunnerTurnEvidence,
    ToolSideEffect,
)
from infrastructure.storage.conversation import LocalJsonConversationStore


_CHECKSUM = "sha256:" + "a" * 64


class _SnapshotStore:
    is_durable = True

    def __init__(self) -> None:
        self._by_ref: dict[tuple[str, str], RefAuthoritySnapshot] = {}
        self._by_binding: dict[tuple[str, str], RefAuthoritySnapshot] = {}

    def commit(self, snapshot: RefAuthoritySnapshot) -> str:
        ref_key = (snapshot.run_id, snapshot.snapshot_ref)
        binding_key = (snapshot.run_id, snapshot.binding_key)
        existing = self._by_binding.get(binding_key)
        if existing is not None and existing != snapshot:
            raise HarnessValidationError("snapshot binding conflict", code="REF_SNAPSHOT_CONFLICT")
        self._by_ref[ref_key] = snapshot
        self._by_binding[binding_key] = snapshot
        return snapshot.snapshot_ref

    def get(self, *, run_id: str, snapshot_ref: str) -> RefAuthoritySnapshot:
        try:
            return self._by_ref[(run_id, snapshot_ref)]
        except KeyError as exc:
            raise HarnessValidationError("snapshot missing", code="REF_SNAPSHOT_MISSING") from exc

    def find(self, *, run_id: str, binding_key: str) -> RefAuthoritySnapshot | None:
        return self._by_binding.get((run_id, binding_key))


class _Evidence:
    is_durable = True

    def __init__(self, scope: ChildToolEvidenceScope) -> None:
        self.scope = scope
        self.events: list[str] = []

    def commit_runner_turn(self, turn) -> ToolRunnerTurnEvidence:
        self.events.append("runner_turn")
        return ToolRunnerTurnEvidence(
            ref=f"tool-turn://{self.scope.child_scope_key}/{turn['iteration']}",
            checksum=checksum_for(dict(turn)),
        )

    def register_call(self, call: ToolCall) -> None:
        self.events.append("register")

    def bind_tool(self, call: ToolCall, definition: ToolDefinition) -> None:
        self.events.append("bind")

    def admit_attempt(self, call: ToolCall, identity: AttemptIdentity) -> None:
        self.events.append("admit")

    def record_attempt_terminal(
        self,
        call: ToolCall,
        outcome: AttemptOutcome[Any],
    ) -> None:
        self.events.append("terminal")

    def commit_observation(
        self,
        observation,
        definition: ToolDefinition | None,
    ) -> str:
        self.events.append("observation")
        return f"tool-evidence://{observation.call.call_id}"


class _InputReader:
    def __init__(self) -> None:
        self.grants: list[RefAuthoritySnapshot] = []

    def read(self, grant: RefAuthoritySnapshot) -> dict[str, Any]:
        self.grants.append(grant)
        return {grant.descriptors[0].ref: {"paper_id": "paper-1"}}


def _agent() -> AgentSpec:
    return AgentSpec(
        agent_id="critic",
        name="Evidence critic",
        instructions="Use only admitted inputs and tools.",
        output_schema={
            "required": ["result"],
            "properties": {"result": {"type": "string"}},
        },
        allowed_tools=["paper.lookup", "private.lookup"],
        loop_policy=AgentLoopPolicy(max_iterations=4, max_tool_calls=4, allow_subagents=True),
        tool_policy=ToolPolicy(
            allowed_tools=["paper.lookup", "private.lookup"],
            require_explicit_allowlist=True,
            require_approval_for_side_effects=False,
            max_tool_calls_per_agent=4,
            max_tool_calls_per_iteration=2,
        ),
        allowed_subagents=["nested"],
    )


def _registry(
    tool_calls: list[dict[str, Any]],
    *,
    explicit_profile: bool = True,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="paper.lookup",
            input_schema={
                "type": "object",
                "properties": {"paper_id": {"type": "string"}},
                "required": ["paper_id"],
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.READ_ONLY,
            concurrency_safe=True,
            metadata=(
                {
                    "execution_profile": (
                        ExecutionProfile.trusted_in_process().to_dict()
                    ),
                }
                if explicit_profile
                else {}
            ),
        ),
        lambda arguments: tool_calls.append(dict(arguments)) or {"title": "Verified"},
    )
    registry.register(
        ToolDefinition(name="private.lookup", side_effect=ToolSideEffect.READ_ONLY),
        lambda arguments: (_ for _ in ()).throw(AssertionError("private tool must stay hidden")),
    )
    return registry


def _activity(
    identity: SubAgentAttemptIdentity,
    *,
    node_id: str | None = None,
) -> HarnessGraphActivity:
    graph_ref = HarnessGraphReference(
        graph_id=identity.graph_id,
        schema_version=identity.graph_schema_version,
        compiler_version=identity.compiler_version,
        condition_policy_version=identity.condition_policy_version,
        checksum=identity.graph_checksum,
        graph_ref=HarnessContractReference(
            HarnessContractKind.GRAPH,
            identity.graph_id,
            identity.graph_version,
        ),
    )
    return HarnessGraphActivity(
        run_id=identity.parent_run_id,
        graph_ref=graph_ref,
        node_id=node_id or identity.node_id,
        node_instance_id=identity.node_instance_id,
        step_ref=HarnessContractReference(HarnessContractKind.STEP, "child-step", "1"),
        worker_ref=HarnessContractReference(HarnessContractKind.WORKER, "child-worker", "1"),
        activity_ref=HarnessContractReference(HarnessContractKind.ACTIVITY, "child-activity", "1"),
        attempt=identity.activity_attempt,
        input_ref=graph_activity_input_checksum({"accepted": True}),
        causal_decision_checksum=checksum_for({"decision": "child"}),
        causal_decision_sequence=1,
        fencing_generation=1,
    )


def _bound_invocation(sequence: int) -> tuple[SubAgentInvocation, HarnessGraphActivityTaskContext]:
    spec = fake_subagent_spec(
        allowed_tools=("paper.lookup",),
        budget={"max_turns": 3, "max_tool_calls": 1, "max_memory_ops": 0},
    )
    base = FakeSubAgentRuntime(spec).build_invocation(parent_run_id="run-1")
    child_run_id = f"run-1:critic:{sequence}"
    invocation_id = f"invocation://run-1:critic:{sequence}"
    seed_attempt = replace(
        base.attempt_identity,
        child_run_id=child_run_id,
        invocation_id=invocation_id,
        node_instance_id=f"child-node:{sequence}",
    )
    activity = _activity(seed_attempt)
    graph_identity = replace(
        base.context_envelope.context_pack.graph_identity,
        node_instance_id=activity.node_instance_id,
        activity_id=activity.activity_id,
    )
    context = ContextEnvelope.for_graph(
        envelope_id=f"context://{child_run_id}",
        graph_identity=graph_identity,
        task_execution_identity=base.context_envelope.context_pack.task_execution_identity,
        phase="EXECUTE",
        worker_id="critic",
        worker_type="subagent",
        dynamic_tail={
            "objective": "Review the admitted paper evidence.",
            "input_refs": list(base.input_refs),
        },
        token_estimate=10,
    )
    attempt = replace(
        seed_attempt,
        activity_id=activity.activity_id,
        context_envelope_id=context.envelope_id,
        context_envelope_checksum=context.checksum,
    )
    envelope = replace(
        base.context_envelope,
        child_run_id=child_run_id,
        context_pack=context,
    )
    invocation = replace(
        base,
        child_run_id=child_run_id,
        invocation_id=invocation_id,
        context_envelope=envelope,
        attempt_identity=attempt,
    )
    return invocation, HarnessGraphActivityTaskContext(
        activity=activity,
        graph_checkpoint_ref=f"checkpoint://run-1/child-{sequence}",
    )


def _with_declared_refs(
    invocation: SubAgentInvocation,
    declared_refs: tuple[str, ...],
    *,
    resolved_refs: tuple[str, ...] | None = None,
) -> SubAgentInvocation:
    admitted_refs = invocation.input_refs if resolved_refs is None else resolved_refs
    context = replace(
        invocation.context_envelope.context_pack,
        dynamic_tail={
            "objective": "Review the admitted paper evidence.",
            "input_refs": list(declared_refs),
        },
        checksum=None,
    )
    attempt = replace(
        invocation.attempt_identity,
        context_envelope_id=context.envelope_id,
        context_envelope_checksum=context.checksum,
    )
    envelope = replace(
        invocation.context_envelope,
        context_pack=context,
        allowed_input_refs=admitted_refs,
    )
    return replace(
        invocation,
        input_refs=admitted_refs,
        context_envelope=envelope,
        attempt_identity=attempt,
    )


def _admission(
    invocation: SubAgentInvocation,
    store: _SnapshotStore | None = None,
) -> tuple[HarnessRefAdmissionService, RefAuthoritySnapshot | None]:
    snapshots = store or _SnapshotStore()
    service = HarnessRefAdmissionService(snapshots)
    if store is None:
        return service, None
    execution = RefAuthoritySnapshot.execution_for_attempt(invocation.attempt_identity)
    descriptor = RefDescriptor(
        ref=invocation.input_refs[0],
        run_id=invocation.parent_run_id,
        stage_id=invocation.stage_id,
        tenant_id="tenant-1",
        owner_id="parent",
        access_mode=RefAccessMode.READ_ONLY,
        artifact_type="research_input",
        source_checksum=checksum_for({"paper": "paper-1"}),
        ref_kind=REF_KIND_INPUT,
        scope=RefScope.SHARED_READ_ONLY,
    )
    root = RefAuthoritySnapshot(
        execution_identity=execution,
        stage_id=invocation.stage_id,
        stage_binding_checksum=invocation.attempt_identity.stage_binding_checksum,
        task_policy_checksum=checksum_for({"policy": "task-plan"}),
        source_checksum=checksum_for({"source": "parent"}),
        policy=RefAccessPolicy(
            policy_id="parent-inputs",
            version="1",
            run_id=invocation.parent_run_id,
            stage_id=invocation.stage_id,
            tenant_id="tenant-1",
            owner_id="parent",
            allowed_refs=(descriptor.ref,),
            allowed_artifact_types=(descriptor.artifact_type,),
            allowed_ref_kinds=(REF_KIND_INPUT,),
            shared_read_only_refs=(descriptor.ref,),
            pinned_checksums={descriptor.ref: descriptor.source_checksum},
        ),
        descriptors=(descriptor,),
    )
    snapshots.commit(root)
    return service, service.admit_child_inputs(
        root,
        attempt_identity=invocation.attempt_identity,
        input_refs=invocation.input_refs,
    )


def _tracker(invocation: SubAgentInvocation) -> GlobalBudgetTracker:
    execution = RefAuthoritySnapshot.execution_for_attempt(invocation.attempt_identity)
    return GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=10),
        execution_identity=execution,
    ).child_tracker(
        invocation.attempt_identity.identity_checksum,
        scope_type=BudgetScopeType.SUBAGENT,
    )


def _evidence(
    invocation: SubAgentInvocation,
    grant: RefAuthoritySnapshot,
) -> _Evidence:
    execution = RefAuthoritySnapshot.execution_for_attempt(invocation.attempt_identity)
    return _Evidence(ChildToolEvidenceScope(
        parent_graph_identity=execution,
        stage_id=invocation.stage_id,
        plan_id=invocation.attempt_identity.plan_id,
        plan_version=invocation.attempt_identity.plan_version,
        group_id="group-1",
        wave_id="wave-1",
        task_id=invocation.task_id,
        task_instance_id=invocation.task_instance_id,
        task_attempt=invocation.attempt,
        task_instance_checksum=_CHECKSUM,
        binding_ref="worker://critic@1",
        binding_checksum=checksum_for({"binding": "critic"}),
        spawn_operation_key="spawn:critic:1",
        owner_scope=invocation.child_run_id,
        tenant_id="tenant-1",
        input_grant_ref=grant.snapshot_ref,
        input_grant_checksum=grant.snapshot_checksum,
        policy_checksum=grant.task_policy_checksum,
        reservation_key="reservation:critic:1",
        reservation_revision=1,
        allocation_checksum=checksum_for({"allocation": "critic"}),
        limits=ChildToolEvidenceLimits(max_logical_calls=1, max_physical_attempts=1),
    ))


def _adapter(tmp_path, llm: FakeLLMClient, registry: ToolRegistry) -> ChildAgentRunnerAdapter:
    return ChildAgentRunnerAdapter(
        registered_agent=_agent(),
        llm_client=llm,
        tool_registry=registry,
        conversation_store=LocalJsonConversationStore(tmp_path),
        execution_environment=ExecutionEnvironmentRegistry(),
        require_explicit_execution_profile=True,
    )


def test_real_runner_preserves_graph_scope_isolates_conversations_and_bounds_tools(tmp_path) -> None:
    tool_calls: list[dict[str, Any]] = []
    registry = _registry(tool_calls)
    llm = FakeLLMClient([
        json.dumps({"action_type": "tool_call", "tool_name": "paper.lookup", "tool_args": {"paper_id": "paper-1"}}),
        json.dumps({"action_type": "final_output", "output": {"result": "first"}}),
        json.dumps({"action_type": "tool_call", "tool_name": "paper.lookup", "tool_args": {"paper_id": "paper-2"}}),
        json.dumps({"action_type": "final_output", "output": {"result": "second"}}),
    ])
    adapter = _adapter(tmp_path, llm, registry)

    conversations: list[str] = []
    expected_llm_identities: list[GraphExecutionIdentity] = []
    for sequence in (1, 2):
        invocation, context = _bound_invocation(sequence)
        service, grant = _admission(invocation, _SnapshotStore())
        assert grant is not None
        evidence = _evidence(invocation, grant)
        reader = _InputReader()

        result = adapter.invoke(
            invocation,
            parent_task_context=context,
            ref_admission_service=service,
            global_budget_tracker=_tracker(invocation),
            tool_execution_evidence=evidence,
            input_reader=reader,
        )

        assert result.success is True
        expected_llm_identities.extend(
            [RefAuthoritySnapshot.execution_for_attempt(invocation.attempt_identity)] * 2
        )
        assert reader.grants == [grant]
        assert evidence.events == ["runner_turn", "register", "bind", "admit", "terminal", "observation"]
        conversation_id = (
            "child-agent-"
            + invocation.attempt_identity.identity_checksum.removeprefix("sha256:")
        )
        conversations.append(conversation_id)
        messages = adapter._conversation_store.read_messages(conversation_id)
        assert messages[0].content == {
            "objective": "Review the admitted paper evidence.",
            "input_refs": list(invocation.input_refs),
            "resolved_inputs": {invocation.input_refs[0]: {"paper_id": "paper-1"}},
        }
        assert all(message.run_id == invocation.parent_run_id for message in messages)
        assert all(message.graph_checkpoint_ref == context.graph_checkpoint_ref for message in messages)

    assert conversations[0] != conversations[1]
    assert tool_calls == [{"paper_id": "paper-1"}, {"paper_id": "paper-2"}]
    assert [request.execution_identity for request in llm.requests] == expected_llm_identities
    assert all(
        [tool["name"] for tool in request.tools] == ["paper.lookup"]
        for request in llm.requests
    )


def test_declared_dependency_ref_must_match_its_committed_exact_replacement() -> None:
    invocation, _ = _bound_invocation(1)
    _, original_grant = _admission(invocation, _SnapshotStore())
    assert original_grant is not None
    logical_ref = "task://structure/output"
    exact_ref = "artifact://run-1/results/structure"
    producer = replace(
        invocation.attempt_identity,
        invocation_id="invocation://run-1:structure:1",
        child_run_id="run-1:structure:1",
        task_id="structure",
        task_instance_id="structure-instance-1",
        task_definition_checksum=checksum_for({"task": "structure"}),
        subagent_id="structure",
    )
    descriptor = RefDescriptor(
        ref=exact_ref,
        run_id=invocation.parent_run_id,
        stage_id=invocation.stage_id,
        tenant_id="tenant-1",
        owner_id=producer.child_run_id,
        access_mode=RefAccessMode.READ_ONLY,
        artifact_type="subagent_output",
        source_checksum=checksum_for({"result": "structure"}),
        ref_kind=REF_KIND_RESULT,
        scope=RefScope.PRIVATE,
    )
    binding = DependencyResultBinding(
        logical_ref=logical_ref,
        producer_attempt=producer,
        source_snapshot_ref=checksum_for({"snapshot": "structure"}),
        accepted_result_checksum=descriptor.source_checksum,
        output_role="structure",
        descriptor=descriptor,
    )
    admitted = _with_declared_refs(
        invocation,
        (logical_ref,),
        resolved_refs=(exact_ref,),
    )
    shared = binding.shared_descriptor
    dependency_grant = replace(
        original_grant,
        attempt_identity=admitted.attempt_identity,
        policy=replace(
            original_grant.policy,
            allowed_refs=(exact_ref,),
            allowed_artifact_types=(shared.artifact_type,),
            allowed_ref_kinds=(shared.ref_kind,),
            shared_read_only_refs=(exact_ref,),
            pinned_checksums={exact_ref: shared.source_checksum},
        ),
        descriptors=(shared,),
        dependency_bindings=(binding,),
    )

    assert ChildAgentRunnerAdapter._accepted_objective(
        admitted,
        dependency_grant,
    ) == "Review the admitted paper evidence."

    mismatched = _with_declared_refs(
        admitted,
        ("task://other/output",),
        resolved_refs=(exact_ref,),
    )
    with pytest.raises(HarnessValidationError) as raised:
        ChildAgentRunnerAdapter._accepted_objective(mismatched, dependency_grant)
    assert raised.value.code == "subagent_runner_input_scope_mismatch"


def test_adapter_snapshots_registration_and_strict_execution_profile_setting(tmp_path) -> None:
    registered = _agent()
    adapter = ChildAgentRunnerAdapter(
        registered_agent=registered,
        llm_client=FakeLLMClient([]),
        tool_registry=ToolRegistry(),
        conversation_store=LocalJsonConversationStore(tmp_path),
        execution_environment=ExecutionEnvironmentRegistry(),
        require_explicit_execution_profile=True,
    )

    registered.allowed_tools.clear()
    assert registered.tool_policy is not None
    registered.tool_policy.allowed_tools.clear()

    assert adapter._registered_agent.allowed_tools == ["paper.lookup", "private.lookup"]
    assert adapter._registered_agent.tool_policy is not registered.tool_policy
    assert adapter._registered_agent.tool_policy.allowed_tools == [
        "paper.lookup",
        "private.lookup",
    ]
    assert adapter._require_explicit_execution_profile is True


def test_strict_execution_profile_rejects_unclassified_tool_before_handler(tmp_path) -> None:
    invocation, context = _bound_invocation(1)
    service, grant = _admission(invocation, _SnapshotStore())
    assert grant is not None
    tool_calls: list[dict[str, Any]] = []
    llm = FakeLLMClient([
        json.dumps({
            "action_type": "tool_call",
            "tool_name": "paper.lookup",
            "tool_args": {"paper_id": "paper-1"},
        }),
        json.dumps({
            "action_type": "final_output",
            "output": {"result": "handled rejection"},
        }),
    ])
    adapter = _adapter(
        tmp_path,
        llm,
        _registry(tool_calls, explicit_profile=False),
    )

    result = adapter.invoke(
        invocation,
        parent_task_context=context,
        ref_admission_service=service,
        global_budget_tracker=_tracker(invocation),
        tool_execution_evidence=_evidence(invocation, grant),
    )

    assert llm.call_count >= 1
    assert tool_calls == []
    first_observation = result.to_dict()["trajectory"][0]["observations"][0]
    assert first_observation["status"] == "failed"
    assert "explicitly declare" in first_observation["result"]["error_message"]


@pytest.mark.parametrize(
    "failure",
    ["missing_grant", "conflicting_grant", "conflicting_checkpoint"],
)
def test_scope_failure_prevents_llm_and_tool_calls(tmp_path, failure: str) -> None:
    invocation, context = _bound_invocation(1)
    tool_calls: list[dict[str, Any]] = []
    llm = FakeLLMClient([json.dumps({"action_type": "final_output", "output": {"result": "unsafe"}})])
    adapter = _adapter(tmp_path, llm, _registry(tool_calls))
    store = None if failure == "missing_grant" else _SnapshotStore()
    service, grant = _admission(invocation, store)
    if failure == "conflicting_grant":
        assert store is not None and grant is not None
        root = next(
            snapshot
            for snapshot in store._by_ref.values()
            if snapshot.parent_snapshot_ref is None
        )
        store._by_binding[(invocation.parent_run_id, grant.binding_key)] = root
    if failure == "conflicting_checkpoint":
        context = HarnessGraphActivityTaskContext(
            activity=_activity(invocation.attempt_identity, node_id="other-node"),
            graph_checkpoint_ref="checkpoint://run-1/other",
        )
    evidence_grant = grant
    if evidence_grant is None:
        _, evidence_grant = _admission(invocation, _SnapshotStore())
    assert evidence_grant is not None
    evidence = _evidence(invocation, evidence_grant)

    with pytest.raises(HarnessValidationError) as raised:
        adapter.invoke(
            invocation,
            parent_task_context=context,
            ref_admission_service=service,
            global_budget_tracker=_tracker(invocation),
            tool_execution_evidence=evidence,
        )

    assert raised.value.code in {
        "REF_SNAPSHOT_MISSING",
        "REF_SNAPSHOT_BINDING_MISMATCH",
        "subagent_runner_checkpoint_mismatch",
    }
    assert llm.call_count == 0
    assert tool_calls == []


def test_conflicting_budget_or_evidence_scope_prevents_execution(tmp_path) -> None:
    invocation, context = _bound_invocation(1)
    tool_calls: list[dict[str, Any]] = []
    llm = FakeLLMClient([])
    adapter = _adapter(tmp_path, llm, _registry(tool_calls))
    service, grant = _admission(invocation, _SnapshotStore())
    assert grant is not None
    execution = RefAuthoritySnapshot.execution_for_attempt(invocation.attempt_identity)
    wrong_tracker = GlobalBudgetTracker(
        GlobalBudgetPolicy(), execution_identity=execution,
    ).child_tracker("other-attempt")

    with pytest.raises(HarnessValidationError) as budget_error:
        adapter.invoke(
            invocation,
            parent_task_context=context,
            ref_admission_service=service,
            global_budget_tracker=wrong_tracker,
            tool_execution_evidence=_evidence(invocation, grant),
        )
    assert budget_error.value.code == "subagent_runner_budget_scope_mismatch"

    valid_tracker = _tracker(invocation)
    conflicting = _evidence(invocation, grant)
    conflicting.scope = replace(conflicting.scope, task_id="other-task")
    with pytest.raises(HarnessValidationError) as evidence_error:
        adapter.invoke(
            invocation,
            parent_task_context=context,
            ref_admission_service=service,
            global_budget_tracker=valid_tracker,
            tool_execution_evidence=conflicting,
        )
    assert evidence_error.value.code == "subagent_runner_evidence_scope_mismatch"
    assert llm.call_count == 0
    assert tool_calls == []
