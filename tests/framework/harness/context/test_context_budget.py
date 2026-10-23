from __future__ import annotations

from dataclasses import dataclass

import pytest

from framework.harness import (
    ContextCompactionActionType,
    ContextCompactionPolicy,
    ContextCompactionRuntime,
    ContextAssembler,
    ContextBudget,
    ContextBudgetGate,
    ContextCompressionLevel,
    ContextEnvelope,
    ContextSegment,
    ContextPhysicalAdmissionEvidence,
    ContextPhysicalMaterialization,
    FakeArtifactPort,
    HarnessValidationError,
    InMemoryHarnessEventPort,
)
from framework.events.canonical import checksum_for
from tests.framework.harness.context.test_context_models import _graph_identity


def test_context_budget_gate_detects_over_budget_envelope() -> None:
    envelope = ContextEnvelope.for_graph(
        envelope_id="context://over-budget",
        graph_identity=_graph_identity(),
        phase="EXECUTE",
        worker_id="context-worker",
        worker_type="function",
        budget=ContextBudget(
            max_input_tokens=100,
            max_output_tokens=100,
            max_context_segments=6,
            max_evidence_items=8,
            max_memory_items=6,
            max_artifact_refs=12,
        ),
        segments=(
            ContextSegment(
                segment_id="global-policy",
                segment_type="global_policy",
                content_ref="policy://global",
                summary="Harness controls Graph execution.",
                token_estimate=120,
                provenance_refs=("policy://global",),
                cache_scope="stable_prefix",
            ),
        ),
        token_estimate=120,
    )

    result = ContextBudgetGate().evaluate(envelope)

    assert result.passed is False
    assert "input_tokens" in result.details["violations"]


def _evidence_envelope(evidence_refs: tuple[str, ...]) -> ContextEnvelope:
    return ContextEnvelope.for_graph(
        envelope_id="context://evidence-count",
        graph_identity=_graph_identity(),
        phase="VERIFY",
        worker_id="context-worker",
        worker_type="context",
        budget=ContextBudget(
            max_input_tokens=4_000,
            max_output_tokens=100,
            max_context_segments=6,
            max_evidence_items=8,
            max_memory_items=6,
            max_artifact_refs=12,
        ),
        segments=(
            ContextSegment(
                segment_id="evidence-memory",
                segment_type="evidence_memory",
                content_ref="evidence-memory://current",
                summary="Accepted evidence.",
                token_estimate=200,
                provenance_refs=("source://paper",),
                cache_scope="dynamic_tail",
            ),
        ),
        evidence_refs=evidence_refs,
        token_estimate=200,
    )


def test_evidence_memory_segment_is_not_counted_as_an_evidence_item() -> None:
    at_cap = ContextBudgetGate().evaluate(_evidence_envelope(tuple(f"evidence://{i}" for i in range(8))))
    over_cap = ContextBudgetGate().evaluate(_evidence_envelope(tuple(f"evidence://{i}" for i in range(9))))

    assert at_cap.passed is True
    assert at_cap.details["usage"]["evidence_items"] == 8
    assert over_cap.passed is False
    assert over_cap.details["violations"]["evidence_items"] == {"used": 9, "max": 8}


def test_context_assembler_fails_closed_without_verified_compaction_runtime() -> None:
    assembler = ContextAssembler()

    with pytest.raises(HarnessValidationError, match="verified compaction runtime"):
        assembler.assemble(
            {
                "graph_identity": _graph_identity(),
                "phase": "EXECUTE",
                "worker_id": "context-worker",
                "worker_type": "function",
                "budget": _budget(250),
                "evidence_memory_tokens": 300,
                "current_task_tokens": 200,
            }
        )

    assert any(
        event["event_type"] == "context_compaction_rejected"
        for event in assembler.events
    )


@dataclass
class _PhysicalContext:
    max_input_tokens: int

    def materialize(self, snapshot, *, deployment_id):
        counts = {
            group.group_id: int(group.semantic_metadata.get("token_estimate", 2))
            for group in snapshot.groups
        }
        return ContextPhysicalMaterialization(
            result_snapshot=snapshot,
            deployment_id=deployment_id,
            profile_revision="profile-assembler-v1",
            materialization_revision="materializer-assembler-v1",
            request={
                "snapshot_id": snapshot.snapshot_id,
                "groups": [group.group_id for group in snapshot.groups],
            },
            fixed_input_tokens=5,
            group_input_tokens=counts,
        )

    def admit(self, materialization):
        input_tokens = materialization.fixed_input_tokens + sum(
            materialization.group_input_tokens.values()
        )
        admitted = input_tokens <= self.max_input_tokens
        return ContextPhysicalAdmissionEvidence(
            source_snapshot_id=materialization.result_snapshot.snapshot_id,
            source_snapshot_checksum=materialization.result_snapshot.checksum,
            prepared_fingerprint=checksum_for(materialization.request),
            physical_profile_revision=materialization.profile_revision,
            tokenizer_revision="tokenizer-assembler-v1",
            normalizer_revision="normalizer-assembler-v1",
            materialization_revision=materialization.materialization_revision,
            admission_status="admitted" if admitted else "input_limit_exceeded",
            admitted=admitted,
            input_tokens=input_tokens,
            max_input_tokens=self.max_input_tokens,
            fixed_input_tokens=materialization.fixed_input_tokens,
            group_input_tokens=materialization.group_input_tokens,
        )


def test_context_assembler_uses_verified_runtime_instead_of_estimated_halving() -> None:
    physical = _PhysicalContext(max_input_tokens=100)
    artifacts = FakeArtifactPort()
    assembler = ContextAssembler(
        compaction_runtime=ContextCompactionRuntime(
            materializer=physical,
            admission_verifier=physical,
            artifact_port=artifacts,
            event_port=InMemoryHarnessEventPort(),
        ),
        deployment_id="deployment-assembler",
        physical_profile_revision="profile-assembler-v1",
        compaction_policy=ContextCompactionPolicy(
            policy_revision="policy-assembler-v1",
            action_order=(ContextCompactionActionType.REPLACE_WITH_REFERENCE,),
            max_actions=1,
            max_summary_calls=0,
            max_replans=1,
            max_llm_calls=0,
            max_input_tokens=100,
            max_cost_usd=0.0,
            max_turns=1,
        ),
    )
    segments = _segments_with_reconstructable_run_state()

    envelope = assembler.assemble(
        {
            "graph_identity": _graph_identity(),
            "phase": "EXECUTE",
            "worker_id": "context-worker",
            "worker_type": "function",
            "current_task_ref": "task://verified-budget",
            "segments": segments,
            "budget": _budget(100),
        }
    )

    assert envelope.metadata["context_verification_classification"] == (
        "versioned_verified_evidence"
    )
    assert envelope.metadata["context_dispatch_authorized"] is False
    assert envelope.metadata["context_dispatch_authorization_required"] is True
    assert envelope.metadata["context_prepared_fingerprint"].startswith("sha256:")
    assert envelope.metadata["context_durable_refs"]["compression_record"]
    assert envelope.token_estimate <= 100
    assert all(
        segment.compression_level is ContextCompressionLevel.C1_CANONICAL_RECORD
        for segment in envelope.segments
    )


def test_context_assembler_builds_a_fresh_runtime_from_the_bound_envelope() -> None:
    physical = _PhysicalContext(max_input_tokens=1_000)
    artifacts = FakeArtifactPort()
    envelopes: list[str] = []

    def runtime_factory(envelope, request):
        envelopes.append(envelope.envelope_id)
        assert request["graph_identity"] == _graph_identity()
        return ContextCompactionRuntime(
            materializer=physical,
            admission_verifier=physical,
            artifact_port=artifacts,
            event_port=InMemoryHarnessEventPort(),
        )

    assembler = ContextAssembler(
        compaction_runtime_factory=runtime_factory,
        deployment_id="deployment-assembler",
        physical_profile_revision="profile-assembler-v1",
    )

    envelope = assembler.assemble(
        {
            "graph_identity": _graph_identity(),
            "phase": "EXECUTE",
            "worker_id": "context-worker",
            "worker_type": "function",
            "current_task_ref": "task://runtime-factory",
            "budget": _budget(1_000),
        }
    )

    assert envelopes == [envelope.envelope_id]
    assert envelope.metadata["context_verification_classification"] == (
        "versioned_no_compaction_evidence"
    )


def test_physical_admission_cannot_be_bypassed_by_estimate() -> None:
    physical = _PhysicalContext(max_input_tokens=30)
    artifacts = FakeArtifactPort()
    assembler = ContextAssembler(
        compaction_runtime=ContextCompactionRuntime(
            materializer=physical,
            admission_verifier=physical,
            artifact_port=artifacts,
            event_port=InMemoryHarnessEventPort(),
        ),
        deployment_id="deployment-assembler",
        physical_profile_revision="profile-assembler-v1",
    )

    with pytest.raises(HarnessValidationError, match="did not authorize"):
        assembler.assemble(
            {
                "graph_identity": _graph_identity(),
                "phase": "EXECUTE",
                "worker_id": "context-worker",
                "worker_type": "function",
                "current_task_ref": "task://physical-reject",
                "budget": _budget(4096),
            }
        )


def _budget(max_input_tokens: int) -> ContextBudget:
    return ContextBudget(
        max_input_tokens=max_input_tokens,
        max_output_tokens=100,
        max_context_segments=6,
        max_evidence_items=8,
        max_memory_items=6,
        max_artifact_refs=12,
    )


def _segments_with_reconstructable_run_state() -> tuple[ContextSegment, ...]:
    types = (
        "global_policy",
        "graph",
        "worker_contract",
        "run_state",
        "evidence_memory",
        "current_task",
    )
    segments = []
    for index, segment_type in enumerate(types):
        metadata = {}
        tokens = 10
        if segment_type == "run_state":
            tokens = 100
            metadata = {
                "reconstruction_ref": (
                    "artifact://run-state/1#sha256=" + "a" * 64
                )
            }
        segments.append(
            ContextSegment(
                segment_id=f"segment-{index}",
                segment_type=segment_type,
                content_ref=f"context-part://{segment_type}",
                summary=segment_type,
                token_estimate=tokens,
                provenance_refs=(f"context-part://{segment_type}",),
                cache_scope="stable_prefix" if index < 3 else "dynamic_tail",
                metadata=metadata,
            )
        )
    return tuple(segments)
