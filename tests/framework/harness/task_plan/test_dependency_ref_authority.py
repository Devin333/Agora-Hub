from __future__ import annotations

from dataclasses import replace

import pytest

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import RefScope
from framework.harness.ref_results import HarnessResultRefAuthority
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from framework.harness.task_plan.canonical import task_output_reference_producer
from framework.harness.task_plan.dependency_refs import AcceptedDependencyResultResolver
from framework.harness.task_plan.policy import TaskPlanPolicy
from infrastructure.storage.harness import FilesystemSubAgentTranscriptStore
from tests.framework.harness.agent_loop.test_parent_ref_admission import _setup
from tests.framework.harness.test_ref_results import _no_payload_reads
from tests.framework.harness.test_ref_snapshot_store import _store


DEPENDENCY = "task://structure/output"


def _completed(tmp_path):
    setup = _setup(tmp_path, dependency_ref=DEPENDENCY)
    result = setup.graph_runtime.run(setup.spec)
    assert result.succeeded, result
    assert len(setup.child_calls) == 2
    execution = setup.admission.snapshot.execution_identity
    plan = setup.task_store.plan(execution.run_id, setup.policy.stage_id)
    consumer = next(task for task in plan.tasks if task.task_id == "contribution")
    resolver = AcceptedDependencyResultResolver(store=setup.task_store, authority=setup.authority, policy=setup.policy)
    return setup, execution, plan, consumer, resolver


def test_real_dependent_worker_receives_only_accepted_public_output_and_reopens(tmp_path, monkeypatch):
    setup, execution, plan, consumer, resolver = _completed(tmp_path)
    assert setup.child_inputs[1]["dependency_outputs"] == {DEPENDENCY: {"summary": "completed"}}
    with monkeypatch.context() as guarded:
        _no_payload_reads(guarded, setup.transcripts)
        bindings = resolver.resolve(plan=plan, task=consumer, execution_identity=execution)
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.descriptor.scope is RefScope.PRIVATE
    assert binding.shared_descriptor.scope is RefScope.SHARED_READ_ONLY
    assert binding.descriptor.artifact_type == "subagent_output"
    consumer_identity = next(
        item[0]["attempt_identity"] for item in setup.child_calls
        if item[0]["attempt_identity"]["task_id"] == "contribution"
    )
    from framework.harness.subagents.transcript import SubAgentAttemptIdentity

    identity = SubAgentAttemptIdentity.from_dict(consumer_identity)
    grant = setup.grants.find(run_id=plan.run_id, binding_key=RefAuthoritySnapshot.attempt_binding_key(identity, RefSnapshotPhase.CHILD_INPUT))
    assert grant.dependency_bindings == bindings
    assert RefAuthoritySnapshot.from_dict(grant.to_dict()) == grant
    assert all(item.artifact_type not in {"subagent_context", "subagent_transcript"} for item in grant.descriptors)
    high = setup.events.get_stream_high_watermark(f"run:{plan.run_id}", tenant_id="control")
    reopened, events = _store(tmp_path / "grants")
    transcripts = FilesystemSubAgentTranscriptStore(tmp_path / "transcripts")
    authority = HarnessResultRefAuthority(reopened, transcript_store=transcripts, artifact_descriptors=setup.child_artifacts, tenant_id="production")
    assert reopened.get(run_id=plan.run_id, snapshot_ref=grant.snapshot_ref) == grant
    assert authority.read_dependency_outputs(identity, input_refs=(binding.descriptor.ref,)) == {
        DEPENDENCY: {"summary": "completed"},
    }
    assert events.get_stream_high_watermark(f"run:{plan.run_id}", tenant_id="control") == high
    assert len(setup.child_calls) == 2


@pytest.mark.parametrize("ref,share", ((DEPENDENCY, False), ("task://structure/transcript", True), ("task:structure/context", True)))
def test_unshared_or_private_dependency_is_rejected_before_any_worker(tmp_path, ref, share):
    setup = _setup(tmp_path, dependency_ref=ref, share_dependency=share)
    setup.graph_runtime.run(setup.spec)
    assert setup.child_calls == []
    assert setup.task_store.plan(setup.spec.run_id, setup.policy.stage_id) is None


@pytest.mark.parametrize("change", ("physical_execution", "receipt", "policy", "changed_producer"))
def test_dependency_resolution_rejects_stale_authority_before_payload_io(tmp_path, monkeypatch, change):
    setup, execution, plan, consumer, resolver = _completed(tmp_path)
    _no_payload_reads(monkeypatch, setup.transcripts)
    if change == "physical_execution":
        execution = replace(execution, activity_id="different-physical-attempt")
    elif change == "receipt":
        records = setup.task_store.results_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version)
        monkeypatch.setattr(setup.task_store, "results_for", lambda *args: tuple(
            replace(record, subagent_output_checksum=checksum_for("wrong-output")) if record.task_id == "structure" else record
            for record in records
        ))
    elif change == "policy":
        resolver = AcceptedDependencyResultResolver(store=setup.task_store, authority=setup.authority, policy=replace(setup.policy, shared_dependency_output_roles=()))
    else:
        old = plan
        producer = next(item for item in old.tasks if item.task_id == "structure")
        changed = replace(producer, task=replace(producer.task, objective="Changed producer contract"))
        plan = replace(old, version=2, tasks=tuple(changed if item.task_id == producer.task_id else item for item in old.tasks))
        projection = replace(setup.task_store.load_projection(old.run_id, old.stage_id), plan_version=2, plan_checksum=plan.plan_checksum)
        records = setup.task_store.results_for(old.run_id, old.stage_id, old.plan_id, old.version)
        monkeypatch.setattr(setup.task_store, "plan", lambda run, stage, version=None: old if version == old.version else plan)
        monkeypatch.setattr(setup.task_store, "load_projection", lambda *args: projection)
        monkeypatch.setattr(setup.task_store, "results_for", lambda *args: records)
    with pytest.raises(HarnessValidationError):
        resolver.resolve(plan=plan, task=consumer, execution_identity=execution)


def test_replacement_reuses_only_unchanged_accepted_producer(tmp_path, monkeypatch):
    setup, execution, original, consumer, resolver = _completed(tmp_path)
    expected = resolver.resolve(plan=original, task=consumer, execution_identity=execution)
    replacement = replace(original, version=2, parent_plan_id=original.plan_id)
    projection = replace(setup.task_store.load_projection(original.run_id, original.stage_id), plan_version=2, plan_checksum=replacement.plan_checksum)
    records = setup.task_store.results_for(original.run_id, original.stage_id, original.plan_id, original.version)
    monkeypatch.setattr(setup.task_store, "plan", lambda run, stage, version=None: original if version == original.version else replacement)
    monkeypatch.setattr(setup.task_store, "load_projection", lambda *args: projection)
    monkeypatch.setattr(setup.task_store, "results_for", lambda *args: records)
    _no_payload_reads(monkeypatch, setup.transcripts)
    assert resolver.resolve(plan=replacement, task=consumer, execution_identity=execution) == expected


def test_dependency_policy_roundtrip_and_selector_contract():
    from tests.framework.harness.agent_loop.test_orchestration_runtime import _runtime

    runtime, _ = _runtime()
    policy = runtime._policy_registry.policies[0]
    assert policy.shared_dependency_output_roles == ()
    assert "shared_dependency_output_roles" not in policy.to_dict()
    assert TaskPlanPolicy.from_dict(policy.to_dict()) == policy
    shared = replace(policy, shared_dependency_output_roles=("structure",))
    assert shared.policy_checksum != policy.policy_checksum
    assert TaskPlanPolicy.from_dict(shared.to_dict()) == shared
    with pytest.raises(HarnessValidationError):
        replace(policy, shared_dependency_output_roles=("unknown-output-role",))
    for ref in ("structure", "task:structure", "task://structure", DEPENDENCY, "task://structure#output"):
        assert task_output_reference_producer(ref, ("structure",)) == "structure"
    for ref in ("task://structure/private", "task:structure#transcript", "task://structure/output/extra", "task:structure#output"):
        with pytest.raises(HarnessValidationError):
            task_output_reference_producer(ref, ("structure",))


@pytest.mark.parametrize("change", ("missing_source", "altered_descriptor", "undeclared_read"))
def test_derived_grant_cannot_forge_source_or_expand_payload_access(tmp_path, monkeypatch, change):
    setup, execution, plan, consumer, resolver = _completed(tmp_path)
    binding = resolver.resolve(plan=plan, task=consumer, execution_identity=execution)[0]
    from framework.harness.subagents.transcript import SubAgentAttemptIdentity

    identity = SubAgentAttemptIdentity.from_dict(setup.child_calls[1][0]["attempt_identity"])
    grant = setup.grants.find(run_id=plan.run_id, binding_key=RefAuthoritySnapshot.attempt_binding_key(identity, RefSnapshotPhase.CHILD_INPUT))
    _no_payload_reads(monkeypatch, setup.transcripts)
    with pytest.raises(HarnessValidationError):
        if change == "undeclared_read":
            setup.authority.read_dependency_outputs(identity, input_refs=("private-sibling-ref",))
        else:
            changed = replace(binding, source_snapshot_ref=checksum_for("missing-source")) if change == "missing_source" else replace(
                binding, descriptor=replace(binding.descriptor, scope=RefScope.SHARED_READ_ONLY),
            )
            setup.grants.commit(replace(grant, dependency_bindings=(changed,)))
