from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness import ContextCacheScope, ContextSegmentType, FakeRAGSessionController, HarnessValidationError, RAGContextPackAssembler
from framework.harness.context.fake import FakeContextAssembler
from framework.harness.rag.fake import fake_rag_session_spec
from framework.harness.rag.models import EvidenceCandidate
from framework.harness.rag.policy import RAGExecutionPolicy


def test_context_pack_assembler_routes_rag_payload_through_context_assembler() -> None:
    original_spec = fake_rag_session_spec()
    spec = replace(
        original_spec,
        context_policy={
            **original_spec.context_policy,
            "max_input_tokens": 8_192,
            "max_output_tokens": 2_048,
        },
    )
    assembler = RAGContextPackAssembler(FakeContextAssembler())
    evidence = (
        EvidenceCandidate(
            evidence_id="evidence:method",
            title="Method paragraph",
            summary="The reader repair retrieves method spans with preserved lineage.",
            source_ref="source://paper#method",
            span_refs=("source://paper#method:p3",),
            evidence_type="method",
            confidence=0.9,
            freshness="static-fixture",
            lineage=("retrieval.fake", "source.read"),
            artifact_refs=("artifact://paper/method-image",),
        ),
    )

    pack = assembler.assemble(
        spec=spec,
        accepted_evidence=evidence,
        artifact_refs=("artifact://retrieval/request-1",),
        budget_snapshot=None,
        policy=RAGExecutionPolicy.from_session_spec(spec),
    )

    envelope = assembler.envelopes[0]
    assert envelope.budget is not None
    assert envelope.budget.max_input_tokens == 8_192
    assert envelope.budget.max_output_tokens == 2_048
    assert pack.budget_snapshot is not None
    assert (
        pack.budget_snapshot.context_tokens_used
        <= original_spec.budget.max_context_tokens
    )
    assert pack.metadata["context_snapshot_ref"]
    assert pack.artifact_refs == ("artifact://paper/method-image", "artifact://retrieval/request-1")
    assert pack.evidence_trace == (
        {
            "status": "accepted",
            "evidence_id": "evidence:method",
            "evidence_type": "method",
            "source_ref": "source://paper#method",
            "span_refs": ["source://paper#method:p3"],
            "artifact_refs": ["artifact://paper/method-image"],
            "lineage": ["retrieval.fake", "source.read"],
            "confidence": 0.9,
            "score_breakdown": {},
        },
    )
    assert envelope.stable_prefix[ContextSegmentType.GLOBAL_POLICY.value]
    assert envelope.artifact_refs == pack.artifact_refs
    assert envelope.metadata["evidence_trace"] == [dict(pack.evidence_trace[0])]
    assert ContextSegmentType.EVIDENCE_MEMORY.value in envelope.dynamic_tail
    assert all(segment.cache_scope == ContextCacheScope.STABLE_PREFIX for segment in envelope.segments[:3])
    assert all(segment.cache_scope == ContextCacheScope.DYNAMIC_TAIL for segment in envelope.segments[3:])


def test_fake_runtime_context_pack_keeps_dynamic_results_out_of_stable_prefix() -> None:
    controller = FakeRAGSessionController()
    result = controller.run_fake_session()

    envelope = controller.context_pack_assembler.envelopes[0]

    assert result.context_pack is not None
    assert result.context_pack.metadata["stable_prefix_contains_dynamic_rag"] is False
    assert "accepted_evidence" not in envelope.stable_prefix.get(ContextSegmentType.GLOBAL_POLICY.value, {})
    assert ContextSegmentType.EVIDENCE_MEMORY.value in envelope.dynamic_tail


@pytest.mark.parametrize("invalid_limit", [True, "8192", 0])
def test_context_pack_assembler_rejects_invalid_model_input_limit(
    invalid_limit: object,
) -> None:
    original_spec = fake_rag_session_spec()
    spec = replace(
        original_spec,
        context_policy={
            **original_spec.context_policy,
            "max_input_tokens": invalid_limit,
        },
    )

    with pytest.raises(
        HarnessValidationError,
        match="context_policy.max_input_tokens",
    ):
        RAGContextPackAssembler(FakeContextAssembler()).assemble(
            spec=spec,
            accepted_evidence=(),
            policy=RAGExecutionPolicy.from_session_spec(spec),
        )


def _candidate(index: int, words: int) -> EvidenceCandidate:
    return EvidenceCandidate(
        evidence_id=f"evidence:{index}",
        title=f"Passage {index}",
        summary=" ".join(f"w{index}" for _ in range(words)),
        source_ref=f"source://paper#p{index}",
        span_refs=(f"source://paper#p{index}:s1",),
        evidence_type="method",
        confidence=0.9,
        freshness="static-fixture",
        lineage=("retrieval.fake",),
    )


def test_context_pack_keeps_the_rank_ordered_prefix_that_fits_the_token_budget() -> None:
    original_spec = fake_rag_session_spec()
    spec = replace(
        original_spec,
        budget=replace(original_spec.budget, max_context_items=8, max_context_tokens=100),
        context_policy={**original_spec.context_policy, "max_input_tokens": 8_192},
    )
    assembler = RAGContextPackAssembler(FakeContextAssembler())
    # Each candidate costs its summary words plus two title words.
    evidence = (_candidate(1, 40), _candidate(2, 40), _candidate(3, 40), _candidate(4, 5))

    pack = assembler.assemble(
        spec=spec,
        accepted_evidence=evidence,
        memory_context=({"memory_ref": "memory://hit", "summary": "a b c d e f g h i j k l m n o p q r s t"},),
        policy=RAGExecutionPolicy.from_session_spec(spec),
    )

    # The short fourth passage does not jump the queue past the third, which no longer fits.
    assert [item.evidence_id for item in pack.accepted_evidence] == ["evidence:1", "evidence:2"]
    assert pack.metadata["context_budget_dropped_evidence_ids"] == ["evidence:3", "evidence:4"]
    assert [row["status"] for row in pack.evidence_trace] == [
        "accepted", "accepted", "budget_dropped", "budget_dropped",
    ]
    # Memory only gets what evidence left (16 tokens), so the 20-token hit is dropped.
    assert pack.memory_context == ()
    assert pack.metadata["context_budget_dropped_memory_hits"] == 1
    assert pack.budget_snapshot.context_tokens_used == 84
    assert pack.budget_snapshot.context_items_used == 2
    assert assembler.envelopes[0].metadata["accepted_evidence"] == [
        item.to_dict() for item in pack.accepted_evidence
    ]
    assert "dropped 2 over the context budget" in pack.assembly_summary


def test_context_pack_reports_items_beyond_the_item_budget_as_dropped() -> None:
    original_spec = fake_rag_session_spec()
    spec = replace(original_spec, budget=replace(original_spec.budget, max_context_items=2, max_context_tokens=1024))

    pack = RAGContextPackAssembler(FakeContextAssembler()).assemble(
        spec=spec,
        accepted_evidence=(_candidate(1, 3), _candidate(2, 3), _candidate(3, 3)),
        policy=RAGExecutionPolicy.from_session_spec(spec),
    )

    assert [item.evidence_id for item in pack.accepted_evidence] == ["evidence:1", "evidence:2"]
    assert pack.metadata["context_budget_dropped_evidence_ids"] == ["evidence:3"]
    assert pack.budget_snapshot.context_tokens_used == 10
