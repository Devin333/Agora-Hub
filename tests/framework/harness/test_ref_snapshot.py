from __future__ import annotations

from dataclasses import replace

import pytest

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_CONDITION_POLICY_VERSION,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
)
from framework.harness.ref_authority import (
    REF_KIND_INPUT,
    REF_KIND_RESULT,
    RefAccessMode,
    RefAccessPolicy,
    RefDescriptor,
    RefScope,
)
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from framework.harness.subagents.transcript import SubAgentAttemptIdentity
from framework.shared.graph_identity import GraphExecutionIdentity


RUN_ID = "run-1"
STAGE_ID = "stage-1"
TENANT_ID = "tenant-1"
CHILD_RUN_ID = "child-run-1"
STAGE_BINDING_CHECKSUM = checksum_for({"stage": STAGE_ID})
TASK_POLICY_CHECKSUM = checksum_for({"task_policy": "policy-1"})


def _execution_identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id=RUN_ID,
        graph_id="research.graph",
        graph_version="1.0.0",
        graph_ref="research.graph@1.0.0",
        graph_checksum=checksum_for({"graph": "research.graph@1.0.0"}),
        node_id="research-node",
        node_instance_id="research-node-instance-1",
        activity_id="research-activity-1",
        attempt=1,
    )


def _attempt(
    execution: GraphExecutionIdentity,
    *,
    task_attempt: int = 1,
    task_instance_id: str = "task-instance-1",
    activity_attempt: int | None = None,
) -> SubAgentAttemptIdentity:
    return SubAgentAttemptIdentity(
        invocation_id="subagent-invocation://child-run-1",
        parent_run_id=execution.run_id,
        child_run_id=CHILD_RUN_ID,
        graph_id=execution.graph_id,
        graph_version=execution.graph_version,
        graph_ref=execution.graph_ref,
        graph_schema_version=GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
        compiler_version=HARNESS_GRAPH_ONLY_COMPILER_VERSION,
        condition_policy_version=HARNESS_CONDITION_POLICY_VERSION,
        graph_checksum=execution.graph_checksum,
        stage_id=STAGE_ID,
        stage_binding_checksum=STAGE_BINDING_CHECKSUM,
        stage_identity_schema="newsroom.harness-stage-identity/v1",
        stage_identity_checksum=checksum_for({"stage_identity": STAGE_ID}),
        plan_id="plan-1",
        plan_version=1,
        plan_checksum=checksum_for({"plan": "plan-1"}),
        task_id="task-1",
        task_definition_checksum=checksum_for({"task": "task-1"}),
        context_envelope_id="context-envelope-1",
        context_envelope_checksum=checksum_for({"context": "context-envelope-1"}),
        node_id=execution.node_id,
        node_instance_id=execution.node_instance_id,
        activity_id=execution.activity_id,
        activity_attempt=(
            execution.attempt if activity_attempt is None else activity_attempt
        ),
        task_instance_id=task_instance_id,
        attempt=task_attempt,
        subagent_id="research-worker",
    )


def _descriptor(
    ref: str,
    *,
    owner_id: str,
    ref_kind: str,
    artifact_type: str,
    scope: RefScope = RefScope.PRIVATE,
) -> RefDescriptor:
    return RefDescriptor(
        ref=ref,
        run_id=RUN_ID,
        stage_id=STAGE_ID,
        tenant_id=TENANT_ID,
        owner_id=owner_id,
        access_mode=RefAccessMode.READ_ONLY,
        artifact_type=artifact_type,
        source_checksum=checksum_for({"ref": ref}),
        ref_kind=ref_kind,
        scope=scope,
    )


def _policy(
    descriptors: tuple[RefDescriptor, ...],
    *,
    owner_id: str,
    shared: tuple[str, ...] = (),
    writable: tuple[str, ...] = (),
) -> RefAccessPolicy:
    return RefAccessPolicy(
        policy_id="policy.refs",
        version="1",
        run_id=RUN_ID,
        stage_id=STAGE_ID,
        tenant_id=TENANT_ID,
        owner_id=owner_id,
        allowed_refs=tuple(item.ref for item in descriptors),
        allowed_artifact_types=tuple(
            sorted({item.artifact_type for item in descriptors})
        ),
        allowed_ref_kinds=tuple(sorted({item.ref_kind for item in descriptors})),
        shared_read_only_refs=shared,
        writable_refs=writable,
        pinned_checksums={
            item.ref: item.source_checksum
            for item in descriptors
        },
    )


def _root_snapshot() -> RefAuthoritySnapshot:
    descriptor = _descriptor(
        "artifact://run-1/input-1",
        owner_id="root-owner",
        ref_kind=REF_KIND_INPUT,
        artifact_type="research_input",
        scope=RefScope.SHARED_READ_ONLY,
    )
    return RefAuthoritySnapshot(
        execution_identity=_execution_identity(),
        stage_id=STAGE_ID,
        stage_binding_checksum=STAGE_BINDING_CHECKSUM,
        task_policy_checksum=TASK_POLICY_CHECKSUM,
        source_checksum=checksum_for({"source": "root-input"}),
        policy=_policy(
            (descriptor,),
            owner_id="root-owner",
            shared=(descriptor.ref,),
        ),
        descriptors=(descriptor,),
    )


def _child_snapshot(
    parent: RefAuthoritySnapshot,
    *,
    attempt: SubAgentAttemptIdentity | None = None,
) -> RefAuthoritySnapshot:
    accepted_attempt = attempt or _attempt(parent.execution_identity)
    return RefAuthoritySnapshot(
        execution_identity=parent.execution_identity,
        stage_id=parent.stage_id,
        stage_binding_checksum=parent.stage_binding_checksum,
        task_policy_checksum=parent.task_policy_checksum,
        source_checksum=parent.source_checksum,
        policy=_policy(
            parent.descriptors,
            owner_id=CHILD_RUN_ID,
            shared=tuple(item.ref for item in parent.descriptors),
        ),
        descriptors=parent.descriptors,
        phase=RefSnapshotPhase.CHILD_INPUT,
        parent_snapshot_ref=parent.snapshot_ref,
        attempt_identity=accepted_attempt,
    )


def _result_snapshot(
    child: RefAuthoritySnapshot,
    *,
    attempt: SubAgentAttemptIdentity | None = None,
) -> RefAuthoritySnapshot:
    result = _descriptor(
        "artifact://run-1/result-1",
        owner_id=CHILD_RUN_ID,
        ref_kind=REF_KIND_RESULT,
        artifact_type="research_result",
    )
    return RefAuthoritySnapshot(
        execution_identity=child.execution_identity,
        stage_id=child.stage_id,
        stage_binding_checksum=child.stage_binding_checksum,
        task_policy_checksum=child.task_policy_checksum,
        source_checksum=checksum_for({"source": "result-1"}),
        policy=_policy((result,), owner_id=CHILD_RUN_ID),
        descriptors=(result,),
        phase=RefSnapshotPhase.RESULT_ACCEPTANCE,
        parent_snapshot_ref=child.snapshot_ref,
        attempt_identity=attempt or child.attempt_identity,
    )


def test_snapshot_round_trip_and_tamper_detection() -> None:
    snapshot = _root_snapshot()

    assert RefAuthoritySnapshot.from_dict(snapshot.to_dict()) == snapshot
    tampered = snapshot.to_dict()
    tampered["source_checksum"] = checksum_for({"source": "tampered"})

    with pytest.raises(HarnessValidationError) as raised:
        RefAuthoritySnapshot.from_dict(tampered)
    assert raised.value.code == "REF_CHECKSUM_MISMATCH"


def test_snapshot_requires_exact_pinned_descriptors() -> None:
    root = _root_snapshot()
    extra = _descriptor(
        "artifact://run-1/input-2",
        owner_id="root-owner",
        ref_kind=REF_KIND_INPUT,
        artifact_type="research_input",
        scope=RefScope.SHARED_READ_ONLY,
    )
    expanded_policy = _policy(
        (*root.descriptors, extra),
        owner_id="root-owner",
        shared=tuple(item.ref for item in (*root.descriptors, extra)),
    )

    with pytest.raises(HarnessValidationError, match="exactly the pinned allowlist"):
        replace(root, policy=expanded_policy)


def test_root_child_and_result_snapshots_validate_exact_parent_chain() -> None:
    root = _root_snapshot()
    child = _child_snapshot(root)
    result = _result_snapshot(child)

    child.validate_parent(root)
    result.validate_parent(child)
    with pytest.raises(HarnessValidationError) as wrong_parent:
        result.validate_parent(root)
    assert wrong_parent.value.code == "REF_SNAPSHOT_BINDING_MISMATCH"


def test_child_snapshot_cannot_expand_parent_authority() -> None:
    root = _root_snapshot()
    extra = _descriptor(
        "artifact://run-1/child-only",
        owner_id=CHILD_RUN_ID,
        ref_kind=REF_KIND_INPUT,
        artifact_type="research_input",
    )
    child = RefAuthoritySnapshot(
        execution_identity=root.execution_identity,
        stage_id=root.stage_id,
        stage_binding_checksum=root.stage_binding_checksum,
        task_policy_checksum=root.task_policy_checksum,
        source_checksum=root.source_checksum,
        policy=_policy((extra,), owner_id=CHILD_RUN_ID),
        descriptors=(extra,),
        phase=RefSnapshotPhase.CHILD_INPUT,
        parent_snapshot_ref=root.snapshot_ref,
        attempt_identity=_attempt(root.execution_identity),
    )

    with pytest.raises(HarnessValidationError) as raised:
        child.validate_parent(root)
    assert raised.value.code == "REF_UNAUTHORIZED"


def test_result_snapshot_rejects_replaced_task_attempt_identity() -> None:
    root = _root_snapshot()
    child = _child_snapshot(root)
    replaced_attempt = replace(child.attempt_identity, attempt=2)
    result = _result_snapshot(child, attempt=replaced_attempt)

    with pytest.raises(HarnessValidationError) as raised:
        result.validate_parent(child)
    assert raised.value.code == "REF_SNAPSHOT_BINDING_MISMATCH"


def test_snapshot_rejects_replaced_graph_activity_attempt_identity() -> None:
    root = _root_snapshot()
    mismatched = _attempt(root.execution_identity, activity_attempt=2)

    with pytest.raises(HarnessValidationError) as raised:
        _child_snapshot(root, attempt=mismatched)
    assert raised.value.code == "REF_SNAPSHOT_BINDING_MISMATCH"
