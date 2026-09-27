from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from framework.events.canonical import checksum_for
from framework.harness import (
    ArtifactWriteRequest,
    ContextCompactionActionType,
    ContextCompactionPolicy,
    ContextCompactionRuntime,
    ContextCompactionRuntimeRequest,
    ContextCompactionRuntimeStatus,
    ContextCompactionReplayReader,
    ContextGroup,
    ContextGroupKind,
    ContextGroupMaterializer,
    ContextGroupMember,
    ContextGroupMemberKind,
    ContextMaterializationRequest,
    ContextPhysicalAdmissionEvidence,
    ContextPhysicalMaterialization,
    ContextReconstructionPolicy,
    ContextSemanticSnapshot,
    ContextSemanticSnapshotKind,
    ContextVerifiedArtifactStore,
    FakeArtifactPort,
    HarnessEventType,
    HarnessValidationError,
    InMemoryHarnessEventPort,
)


def _policy(*, max_input_tokens: int = 25) -> ContextCompactionPolicy:
    return ContextCompactionPolicy(
        policy_revision="policy-runtime-v1",
        action_order=(ContextCompactionActionType.DROP_RECONSTRUCTABLE_GROUP,),
        max_actions=2,
        max_summary_calls=0,
        max_replans=1,
        max_llm_calls=0,
        max_input_tokens=max_input_tokens,
        max_cost_usd=0.0,
        max_turns=2,
        keep_recent_complete_turns=1,
    )


def _source(policy: ContextCompactionPolicy, *, reconstructable: bool = True):
    base = ContextGroupMaterializer().materialize(
        ContextMaterializationRequest(
            run_id="run-context-runtime",
            stage_id="step-context",
            task_binding_ref="task://context-runtime",
            policy_revision=policy.policy_revision,
            physical_profile_revision="profile-runtime-v1",
            messages=(
                {"role": "user", "content_ref": "message://user"},
                {"role": "assistant", "content_ref": "message://assistant"},
            ),
        )
    )
    if not reconstructable:
        return base
    extra = ContextGroup(
        group_kind=ContextGroupKind.RECONSTRUCTABLE,
        members=(
            ContextGroupMember(
                member_kind=ContextGroupMemberKind.REFERENCE,
                content_ref="artifact://prior-result#sha256=abc",
                ordinal=0,
                source_refs=("source://prior-result",),
            ),
        ),
        source_refs=("source://prior-result",),
        reconstruction_policy=ContextReconstructionPolicy.DURABLE_REF,
        reconstruction_ref="artifact://prior-result#sha256=abc",
    )
    return ContextSemanticSnapshot(
        run_id=base.run_id,
        stage_id=base.stage_id,
        task_binding_ref=base.task_binding_ref,
        groups=(*base.groups, extra),
        policy_revision=base.policy_revision,
        physical_profile_revision=base.physical_profile_revision,
        snapshot_kind=ContextSemanticSnapshotKind.SOURCE,
    )


@dataclass
class _PhysicalRuntime:
    max_input_tokens: int
    reject_result: bool = False

    def materialize(self, snapshot, *, deployment_id):
        del deployment_id
        return ContextPhysicalMaterialization(
            result_snapshot=snapshot,
            deployment_id="deployment-runtime",
            profile_revision="profile-runtime-v1",
            materialization_revision="materialization-runtime-v1",
            request={
                "snapshot_id": snapshot.snapshot_id,
                "group_ids": [group.group_id for group in snapshot.groups],
            },
            fixed_input_tokens=5,
            group_input_tokens={group.group_id: 10 for group in snapshot.groups},
        )

    def admit(self, materialization):
        input_tokens = materialization.fixed_input_tokens + sum(
            materialization.group_input_tokens.values()
        )
        is_result = materialization.result_snapshot.snapshot_kind is ContextSemanticSnapshotKind.RESULT
        admitted = input_tokens <= self.max_input_tokens and not (
            self.reject_result and is_result
        )
        return ContextPhysicalAdmissionEvidence(
            source_snapshot_id=materialization.result_snapshot.snapshot_id,
            source_snapshot_checksum=materialization.result_snapshot.checksum,
            prepared_fingerprint=checksum_for(materialization.request),
            physical_profile_revision=materialization.profile_revision,
            tokenizer_revision="tokenizer-runtime-v1",
            normalizer_revision="normalizer-runtime-v1",
            materialization_revision=materialization.materialization_revision,
            admission_status="admitted" if admitted else "input_limit_exceeded",
            admitted=admitted,
            input_tokens=input_tokens,
            max_input_tokens=self.max_input_tokens,
            fixed_input_tokens=materialization.fixed_input_tokens,
            group_input_tokens=materialization.group_input_tokens,
        )


def _runtime(physical, *, event_port=None, runtime_event_sink=None):
    artifacts = FakeArtifactPort()
    events = event_port or InMemoryHarnessEventPort()
    return (
        ContextCompactionRuntime(
            materializer=physical,
            admission_verifier=physical,
            artifact_port=artifacts,
            event_port=events,
            runtime_event_sink=runtime_event_sink,
        ),
        artifacts,
        events,
    )


def test_runtime_commits_verified_result_before_authorizing_dispatch() -> None:
    policy = _policy()
    source = _source(policy)
    runtime, artifacts, events = _runtime(_PhysicalRuntime(max_input_tokens=25))

    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.status is ContextCompactionRuntimeStatus.VERIFIED
    assert result.dispatch_authorized is True
    assert result.authorizes_dispatch(result.final_admission.prepared_fingerprint)
    assert not result.authorizes_dispatch("sha256:stale")
    assert result.result_snapshot is not None
    assert len(result.result_snapshot.groups) == len(source.groups) - 1
    assert result.activation_event_id is not None
    assert result.durable_refs.compression_record is not None
    assert len(artifacts.storage) >= 8
    assert events.events[-1].event_type is HarnessEventType.CONTEXT_COMPACTION_VERIFIED


def test_runtime_records_no_compaction_without_fabricated_record() -> None:
    policy = _policy(max_input_tokens=30)
    source = _source(policy, reconstructable=False)
    runtime, _, events = _runtime(_PhysicalRuntime(max_input_tokens=30))

    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.status is ContextCompactionRuntimeStatus.NO_COMPACTION_REQUIRED
    assert result.dispatch_authorized is True
    assert result.result_snapshot == source
    assert result.durable_refs.compression_record is None
    assert [event.event_type for event in events.events] == [
        HarnessEventType.CONTEXT_COMPACTION_PLANNED
    ]


def test_second_physical_gate_failure_never_emits_verified_event() -> None:
    policy = _policy()
    source = _source(policy)
    runtime, _, events = _runtime(
        _PhysicalRuntime(max_input_tokens=25, reject_result=True)
    )

    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.status is ContextCompactionRuntimeStatus.POST_COMPACTION_VERIFY_FAILED
    assert result.dispatch_authorized is False
    assert result.activation_event_id is None
    assert HarnessEventType.CONTEXT_COMPACTION_VERIFIED not in {
        event.event_type for event in events.events
    }
    assert events.events[-1].event_type is HarnessEventType.CONTEXT_COMPACTION_REJECTED


class _FailVerifiedEventPort(InMemoryHarnessEventPort):
    def record(self, event):
        if event.event_type is HarnessEventType.CONTEXT_COMPACTION_VERIFIED:
            raise RuntimeError("simulated durable event failure")
        return super().record(event)


class _FailRejectedEventPort(InMemoryHarnessEventPort):
    def record(self, event):
        if event.event_type is HarnessEventType.CONTEXT_COMPACTION_REJECTED:
            raise RuntimeError("simulated durable rejection event failure")
        return super().record(event)


class _RuntimeEventSink:
    def __init__(self, *, fail_event_type: str | None = None) -> None:
        self.fail_event_type = fail_event_type
        self.events = []

    def publish(self, event):
        self.events.append(event)
        if event.event_type.value == self.fail_event_type:
            raise RuntimeError("simulated canonical runtime event failure")
        return event


def test_verified_event_append_is_activation_commit_boundary() -> None:
    policy = _policy()
    source = _source(policy)
    events = _FailVerifiedEventPort()
    runtime, _, _ = _runtime(_PhysicalRuntime(max_input_tokens=25), event_port=events)

    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.status is ContextCompactionRuntimeStatus.DURABLE_COMMIT_FAILED
    assert result.dispatch_authorized is False
    assert result.activation_event_id is None
    assert HarnessEventType.CONTEXT_COMPACTION_VERIFIED not in {
        event.event_type for event in events.events
    }


def test_protected_only_overflow_fails_closed() -> None:
    policy = _policy(max_input_tokens=5)
    source = _source(policy, reconstructable=False)
    runtime, _, events = _runtime(_PhysicalRuntime(max_input_tokens=5))

    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.status is ContextCompactionRuntimeStatus.PROTECTED_CONTEXT_EXCEEDS_WINDOW
    assert result.dispatch_authorized is False
    assert result.durable_refs.compression_record is None
    assert HarnessEventType.CONTEXT_COMPACTION_VERIFIED not in {
        event.event_type for event in events.events
    }
    assert events.events[-1].event_type is HarnessEventType.CONTEXT_COMPACTION_REJECTED
    assert events.events[-1].payload["planning_status"] == (
        "protected_context_exceeds_window"
    )


def test_planning_rejection_event_failure_is_a_durable_commit_failure() -> None:
    policy = _policy(max_input_tokens=5)
    source = _source(policy, reconstructable=False)
    events = _FailRejectedEventPort()
    runtime, _, _ = _runtime(
        _PhysicalRuntime(max_input_tokens=5),
        event_port=events,
    )

    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.status is ContextCompactionRuntimeStatus.DURABLE_COMMIT_FAILED
    assert result.dispatch_authorized is False
    assert result.activation_event_id is None
    assert [event.event_type for event in events.events] == [
        HarnessEventType.CONTEXT_COMPACTION_PLANNED
    ]


def test_configured_runtime_event_sink_failure_aborts_rejected_result() -> None:
    policy = _policy()
    source = _source(policy)
    sink = _RuntimeEventSink(fail_event_type="context_compaction_rejected")
    runtime, _, legacy_events = _runtime(
        _PhysicalRuntime(max_input_tokens=25, reject_result=True),
        runtime_event_sink=sink,
    )

    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.status is ContextCompactionRuntimeStatus.DURABLE_COMMIT_FAILED
    assert result.reason_code == "canonical_event_commit_failed"
    assert legacy_events.events[-1].event_type is HarnessEventType.CONTEXT_COMPACTION_REJECTED
    assert sink.events[-1].event_type.value == "context_compaction_rejected"


def test_configured_runtime_event_sink_uses_distinct_canonical_identity() -> None:
    policy = _policy(max_input_tokens=30)
    source = _source(policy, reconstructable=False)
    sink = _RuntimeEventSink()
    runtime, _, legacy_events = _runtime(
        _PhysicalRuntime(max_input_tokens=30),
        runtime_event_sink=sink,
    )

    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.status is ContextCompactionRuntimeStatus.NO_COMPACTION_REQUIRED
    assert len(legacy_events.events) == len(sink.events) == 1
    assert sink.events[0].event_id == f"context-runtime:{legacy_events.events[0].event_id}"


def test_runtime_without_canonical_sink_preserves_existing_result_contract() -> None:
    policy = _policy(max_input_tokens=30)
    source = _source(policy, reconstructable=False)
    runtime, _, legacy_events = _runtime(
        _PhysicalRuntime(max_input_tokens=30),
        runtime_event_sink=None,
    )

    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.status is ContextCompactionRuntimeStatus.NO_COMPACTION_REQUIRED
    assert result.activation_event_id == legacy_events.events[-1].event_id


@pytest.mark.parametrize("fingerprint", ["", " ", "sha256:other"])
def test_dispatch_proof_rejects_missing_or_stale_prepared_identity(fingerprint) -> None:
    policy = _policy(max_input_tokens=30)
    source = _source(policy, reconstructable=False)
    runtime, _, _ = _runtime(_PhysicalRuntime(max_input_tokens=30))
    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    assert result.dispatch_authorized is True
    assert result.authorizes_dispatch(fingerprint) is False


def test_verified_runtime_evidence_replays_without_side_effects() -> None:
    policy = _policy()
    source = _source(policy)
    runtime, artifacts, events = _runtime(_PhysicalRuntime(max_input_tokens=25))
    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )

    report = ContextCompactionReplayReader(
        ContextVerifiedArtifactStore(artifacts)
    ).replay(result.durable_refs, activation_event=events.events[-1])

    assert report.verification_classification == "versioned_verified_evidence"
    assert report.source_snapshot_id == source.snapshot_id
    assert report.result_snapshot_id == result.result_snapshot.snapshot_id
    assert report.prepared_fingerprint == result.final_admission.prepared_fingerprint
    assert report.side_effects_replayed is False


@pytest.mark.parametrize(
    "ref_field",
    ["source_snapshot", "result_snapshot", "aggregate_verification", "compression_record"],
)
def test_replay_rejects_tampered_durable_evidence(ref_field: str) -> None:
    policy = _policy()
    source = _source(policy)
    runtime, artifacts, events = _runtime(_PhysicalRuntime(max_input_tokens=25))
    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )
    artifact_ref = getattr(result.durable_refs, ref_field)
    artifacts.storage[artifact_ref.ref]["metadata"]["tampered"] = True

    with pytest.raises(HarnessValidationError, match="checksum mismatch"):
        ContextCompactionReplayReader(ContextVerifiedArtifactStore(artifacts)).replay(
            result.durable_refs,
            activation_event=events.events[-1],
        )


def test_replay_rejects_stale_activation_event() -> None:
    policy = _policy()
    source = _source(policy)
    runtime, artifacts, events = _runtime(_PhysicalRuntime(max_input_tokens=25))
    result = runtime.run(
        ContextCompactionRuntimeRequest(
            source_snapshot=source,
            policy=policy,
            deployment_id="deployment-runtime",
        )
    )
    stale_event = replace(
        events.events[-1],
        payload={**events.events[-1].payload, "prepared_fingerprint": "sha256:stale"},
    )

    with pytest.raises(HarnessValidationError, match="activation event is stale"):
        ContextCompactionReplayReader(ContextVerifiedArtifactStore(artifacts)).replay(
            result.durable_refs,
            activation_event=stale_event,
        )


def test_checksum_bound_external_artifact_is_verified() -> None:
    artifacts = FakeArtifactPort()
    external = artifacts.write_artifact(
        ArtifactWriteRequest(
            artifact_type="context-summary-candidate",
            payload={"summary": "candidate"},
        )
    )
    checksum_ref = f"{external.ref}#sha256={external.checksum.removeprefix('sha256:')}"
    store = ContextVerifiedArtifactStore(artifacts)

    assert store.read_checksum_bound_artifact(checksum_ref)["payload"] == {
        "summary": "candidate"
    }
    artifacts.storage[external.ref]["payload"]["summary"] = "tampered"
    with pytest.raises(HarnessValidationError, match="checksum mismatch"):
        store.read_checksum_bound_artifact(checksum_ref)
