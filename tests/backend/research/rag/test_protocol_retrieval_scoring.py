from __future__ import annotations

import math

import pytest

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_evidence_mapping import (
    AnchorChunkContribution, AnchorChunkSet, AnchorEvidenceMapping,
    EvidenceGroupBinding, IndexBinding, IndexEvidenceMapping,
    IndexedChunkIdentity, calculate_index_content_sha256, chunk_content_sha256,
)
from backend.research.rag.evaluation.protocol_retrieval_scoring import (
    FinalContextChunk, FrozenQrelUnit, FrozenQrels, RetrievalScoreIdentity,
    RetrievalScoringConfig, score_retrieval,
)

SOURCE_SHA = "a" * 64
DATASET_SHA = "d" * 64
CONFIG_SHA = "c" * 64
VARIANT_SHA = "f" * 64


def _chunk(chunk_id: str, content: str) -> PaperChunk:
    return PaperChunk(chunk_id=chunk_id, paper_id="paper-1", parse_source="pdf",
                      content=content,
                      metadata={"source_locator": f"paper://paper-1#{chunk_id}",
                                "document_version": "v1", "source_sha256": SOURCE_SHA})


def _indexed(chunk: PaperChunk) -> IndexedChunkIdentity:
    return IndexedChunkIdentity(
        chunk_id=chunk.chunk_id, paper_id=chunk.paper_id, document_version="v1",
        source_sha256=SOURCE_SHA, chunk_content_sha256=chunk_content_sha256(chunk.content),
        chunk_content_length=len(chunk.content),
        source_locator=str(chunk.metadata["source_locator"]), spans=(),
    )


def _anchor(anchor_id: str, paths: tuple[tuple[PaperChunk, int, int], ...]) -> AnchorEvidenceMapping:
    # Each tuple is one sufficient path when it contains a single chunk.  The
    # helper below accepts repeated path numbers via a grouped variant.
    sets = tuple(
        AnchorChunkSet(
            set_id=f"{anchor_id}-path-{index}", chunk_ids=(chunk.chunk_id,),
            contributions=(AnchorChunkContribution(
                chunk_id=chunk.chunk_id, source_start=0, source_end=end - start,
                chunk_start=start, chunk_end=end,
                source_locator=str(chunk.metadata["source_locator"]),
            ),),
        )
        for index, (chunk, start, end) in enumerate(paths, start=1)
    )
    return AnchorEvidenceMapping(
        anchor_id=anchor_id, anchor_location_digest="b" * 64, status="exact" if len(sets) == 1 else "equivalent",
        disposition="valid", anchor_source_sha256=SOURCE_SHA,
        anchor_excerpt_digest=content_digest(anchor_id), source_start=0,
        source_end=max(item.source_end for path in sets for item in path.contributions),
        chunk_sets=sets, partial_chunk_ids=(), coverage_fraction=1.0,
        basis=("test-verified-source-span",), reason=None,
    )


def _cross_anchor(anchor_id: str, first: PaperChunk, second: PaperChunk) -> AnchorEvidenceMapping:
    return AnchorEvidenceMapping(
        anchor_id=anchor_id, anchor_location_digest="b" * 64, status="exact", disposition="valid",
        anchor_source_sha256=SOURCE_SHA, anchor_excerpt_digest=content_digest(anchor_id),
        source_start=0, source_end=10,
        chunk_sets=(AnchorChunkSet(
            set_id=f"{anchor_id}-path-1", chunk_ids=(first.chunk_id, second.chunk_id),
            contributions=(
                AnchorChunkContribution(chunk_id=first.chunk_id, source_start=0, source_end=5,
                    chunk_start=0, chunk_end=5, source_locator=str(first.metadata["source_locator"])),
                AnchorChunkContribution(chunk_id=second.chunk_id, source_start=5, source_end=10,
                    chunk_start=0, chunk_end=5, source_locator=str(second.metadata["source_locator"])),
            ),
        ),), partial_chunk_ids=(), coverage_fraction=1.0,
        basis=("test-verified-source-span",), reason=None,
    )


def _missing_anchor(anchor_id: str) -> AnchorEvidenceMapping:
    return AnchorEvidenceMapping(
        anchor_id=anchor_id, anchor_location_digest="b" * 64, status="missing", disposition="system_failure",
        anchor_source_sha256=SOURCE_SHA, anchor_excerpt_digest=content_digest(anchor_id),
        source_start=0, source_end=5, chunk_sets=(), partial_chunk_ids=(),
        coverage_fraction=0.0, basis=("valid_source_anchor_absent_from_actual_index",),
        reason="parser omitted table evidence",
    )


def _mapping(chunks: tuple[PaperChunk, ...], anchors: tuple[AnchorEvidenceMapping, ...],
             groups: tuple[EvidenceGroupBinding, ...], *, answerability: str = "answerable") -> IndexEvidenceMapping:
    binding = IndexBinding(
        corpus_snapshot_id="corpus-1", index_snapshot_id="index-1",
        index_content_sha256=calculate_index_content_sha256(chunks),
        parser_id="parser", parser_version="1.0.0", parser_config_sha256=CONFIG_SHA,
        chunker_id="chunker", chunker_version="1.0.0", chunker_config_sha256=CONFIG_SHA,
        index_config_sha256=CONFIG_SHA,
    )
    return IndexEvidenceMapping.create(
        record_type="evidence_mapping", schema_version="1.0",
        protocol_version="research-rag-eval/v1", mapping_version="1.0.0",
        dataset_id="dataset-1", dataset_version="1.0.0", dataset_digest=DATASET_SHA,
        case_id="case-1", case_digest="e" * 64, answerability=answerability,
        binding=binding, indexed_chunks=tuple(_indexed(chunk) for chunk in chunks),
        anchor_mappings=anchors, required_evidence_groups=groups,
    )


def _values(result) -> dict[str, float | None]:
    return {item.metric_id: item.value for item in result.observations}


def _binary_qrels(*units: tuple[str, tuple[str, ...]]) -> FrozenQrels:
    return FrozenQrels(version="1.0.0", gain_mode="binary",
                       units=tuple(FrozenQrelUnit(unit_id=name, relevance=1.0,
                                                  candidate_chunk_ids=candidates)
                                   for name, candidates in units))


def test_group_coverage_alternatives_necessary_group_and_cross_chunk_anchor() -> None:
    ca1, ca2, cb, cc, cd = (_chunk("a1", "AAAAA"), _chunk("a2", "BBBBB"),
                            _chunk("b", "EEEEE"), _chunk("c", "CCCCC"), _chunk("d", "DDDDD"))
    mapping = _mapping(
        (ca1, ca2, cb, cc, cd),
        (_cross_anchor("a", ca1, ca2), _anchor("b", ((cb, 0, 5),)),
         _anchor("c", ((cc, 0, 5),)), _anchor("d", ((cd, 0, 5),))),
        (EvidenceGroupBinding(group_id="g1", alternatives=(("a", "b"), ("c",))),
         EvidenceGroupBinding(group_id="g2", alternatives=(("d",),))),
    )
    qrels = _binary_qrels(("qa", ("a1", "a2")), ("qc", ("c",)), ("qd", ("d",)))

    partial = score_retrieval(mapping=mapping, ranked_chunks=(ca1, ca2), qrels=qrels,
                              config=RetrievalScoringConfig(k_values=(1, 2)))
    assert _values(partial)["retrieval.evidence_recall_at_1"] == 0.0
    assert _values(partial)["retrieval.evidence_recall_at_2"] == pytest.approx(0.25)
    assert _values(partial)["retrieval.hit_at_1"] == 1.0
    assert _values(partial)["retrieval.hit_at_2"] == 1.0
    assert _values(partial)["retrieval.mrr"] == 1.0  # first chunk is valid partial gold evidence

    alternative = score_retrieval(mapping=mapping, ranked_chunks=(cc,), qrels=qrels,
                                  config=RetrievalScoringConfig(k_values=(1,)))
    assert _values(alternative)["retrieval.evidence_recall_at_1"] == 0.5


def test_duplicate_unknown_and_overshoot_retain_raw_positions_without_false_coverage() -> None:
    ca, cb, unknown = _chunk("a", "AAAAA"), _chunk("b", "BBBBB"), _chunk("x", "XXXXX")
    mapping = _mapping(
        (ca, cb), (_anchor("a", ((ca, 0, 5),)), _anchor("b", ((cb, 0, 5),))),
        (EvidenceGroupBinding(group_id="g", alternatives=(("a", "b"),)),),
    )
    result = score_retrieval(
        mapping=mapping, ranked_chunks=(ca, ca, unknown, cb),
        qrels=_binary_qrels(("a", ("a",)), ("b", ("b",))),
        config=RetrievalScoringConfig(k_values=(1, 3)),
    )
    values = _values(result)
    assert values["retrieval.evidence_recall_at_3"] == 0.5
    assert values["retrieval.mrr"] == 1.0
    assert {item.code for item in result.diagnostics} >= {
        "duplicate_retrieval_id", "unknown_retrieval_id", "retrieval_overshoot"
    }
    ndcg3 = next(item for item in result.observations if item.metric_id == "retrieval.ndcg_at_3")
    assert ndcg3.value == pytest.approx(1.0 / (1.0 + 1.0 / math.log2(3)))
    assert ndcg3.provenance["idcg"] == pytest.approx(1.0 + 1.0 / math.log2(3))
    assert ndcg3.provenance["dedupe_unit"] == "qrel_unit_id_and_candidate_chunk_id"


def test_parser_loss_stays_in_denominator_and_scores_zero_not_na() -> None:
    ca = _chunk("a", "AAAAA")
    mapping = _mapping(
        (ca,), (_anchor("a", ((ca, 0, 5),)), _missing_anchor("table")),
        (EvidenceGroupBinding(group_id="text", alternatives=(("a",),)),
         EvidenceGroupBinding(group_id="table", alternatives=(("table",),))),
    )
    result = score_retrieval(mapping=mapping, ranked_chunks=(ca,),
                             qrels=_binary_qrels(("a", ("a",))),
                             config=RetrievalScoringConfig(k_values=(1,)))
    observation = next(item for item in result.observations
                       if item.metric_id == "retrieval.evidence_recall_at_1")
    assert observation.status == "ok"
    assert observation.value == 0.5
    assert any(item.code == "parser_evidence_loss" for item in result.diagnostics)



def test_partial_only_anchor_counts_hit_without_claiming_full_coverage() -> None:
    chunk = _chunk("partial", "AAAAA")
    partial_anchor = AnchorEvidenceMapping(
        anchor_id="a", anchor_location_digest="b" * 64, status="partial",
        disposition="system_failure", anchor_source_sha256=SOURCE_SHA,
        anchor_excerpt_digest=content_digest("a"), source_start=0, source_end=10,
        chunk_sets=(), partial_chunk_ids=("partial",), coverage_fraction=0.5,
        basis=("actual_chunks_cover_only_part_of_anchor",),
        reason="parser retained only half of the valid anchor",
    )
    mapping = _mapping(
        (chunk,), (partial_anchor,),
        (EvidenceGroupBinding(group_id="g", alternatives=(("a",),)),),
    )
    result = score_retrieval(
        mapping=mapping, ranked_chunks=(chunk,),
        qrels=_binary_qrels(("partial-anchor-a", ("partial",))),
        config=RetrievalScoringConfig(k_values=(1,)),
    )
    values = _values(result)
    assert values["retrieval.hit_at_1"] == 1.0
    assert values["retrieval.mrr"] == 1.0
    assert values["retrieval.evidence_recall_at_1"] == 0.0


def test_retrieved_full_but_actual_context_truncation_is_not_full() -> None:
    chunk = _chunk("a", "ABCDEFGHIJ")
    mapping = _mapping(
        (chunk,), (_anchor("a", ((chunk, 0, 10),)),),
        (EvidenceGroupBinding(group_id="g", alternatives=(("a",),)),),
    )
    result = score_retrieval(
        mapping=mapping, ranked_chunks=(chunk,),
        final_context=(FinalContextChunk(chunk_id="a", content="ABC", chunk_start=0, chunk_end=3),),
        qrels=_binary_qrels(("a", ("a",))), config=RetrievalScoringConfig(k_values=(1,)),
    )
    values = _values(result)
    assert values["retrieval.evidence_recall_at_1"] == 1.0
    assert values["retrieval.context_evidence_coverage"] == 0.0
    assert result.context_fingerprint is not None


def test_binary_and_graded_ndcg_use_frozen_full_ideal_and_raw_rank() -> None:
    high, low, junk = _chunk("high", "HHHHH"), _chunk("low", "LLLLL"), _chunk("junk", "JJJJJ")
    mapping = _mapping(
        (high, low, junk), (_anchor("high", ((high, 0, 5),)), _anchor("low", ((low, 0, 5),))),
        (EvidenceGroupBinding(group_id="g", alternatives=(("high", "low"),)),),
    )
    binary = score_retrieval(
        mapping=mapping, ranked_chunks=(junk, high),
        qrels=_binary_qrels(("high", ("high",)), ("low", ("low",))),
        config=RetrievalScoringConfig(k_values=(2,)),
    )
    binary_ndcg = _values(binary)["retrieval.ndcg_at_2"]
    assert binary_ndcg == pytest.approx((1 / math.log2(3)) / (1 + 1 / math.log2(3)))

    graded_qrels = FrozenQrels(
        version="1.0.0", gain_mode="graded",
        units=(FrozenQrelUnit(unit_id="high", relevance=3.0, candidate_chunk_ids=("high",)),
               FrozenQrelUnit(unit_id="low", relevance=1.0, candidate_chunk_ids=("low",))),
    )
    graded = score_retrieval(mapping=mapping, ranked_chunks=(low, high), qrels=graded_qrels,
                             config=RetrievalScoringConfig(k_values=(2,)))
    expected = (1 + 7 / math.log2(3)) / (7 + 1 / math.log2(3))
    assert _values(graded)["retrieval.ndcg_at_2"] == pytest.approx(expected)


def test_ndcg_ideal_denominator_does_not_shrink_to_returned_result_count() -> None:
    high, low = _chunk("high", "HHHHH"), _chunk("low", "LLLLL")
    mapping = _mapping(
        (high, low),
        (_anchor("high", ((high, 0, 5),)), _anchor("low", ((low, 0, 5),))),
        (EvidenceGroupBinding(group_id="g", alternatives=(("high", "low"),)),),
    )
    result = score_retrieval(
        mapping=mapping, ranked_chunks=(high,),
        qrels=_binary_qrels(("high", ("high",)), ("low", ("low",))),
        config=RetrievalScoringConfig(k_values=(2,)),
    )
    observation = next(item for item in result.observations
                       if item.metric_id == "retrieval.ndcg_at_2")
    assert observation.value == pytest.approx(1.0 / (1.0 + 1.0 / math.log2(3)))
    assert observation.provenance["ideal_unit_count"] == 2


def test_negative_is_not_applicable_and_observations_project_without_fake_provenance() -> None:
    mapping = _mapping((), (), (), answerability="unanswerable")
    result = score_retrieval(mapping=mapping, ranked_chunks=(), qrels=None,
                             config=RetrievalScoringConfig(k_values=(1,)))
    assert all(item.status == "not_applicable" and item.value is None
               for item in result.observations)

    identity = RetrievalScoreIdentity(
        dataset_id="dataset-1", dataset_version="1.0.0", dataset_digest=DATASET_SHA,
        run_id="run-1", case_id="case-1", attempt_id="attempt-1", track="retrieval",
        execution_profile="production", variant_fingerprint=VARIANT_SHA, repetition=1,
        scope="retrieval", scoring_revision="1.0.0",
    )
    scores = result.to_evaluation_scores(identity)
    assert all(score.status.value == "not_applicable" for score in scores)
    assert all(score.source.value == "rule" for score in scores)
    assert all("model" not in score.provenance for score in scores)



@pytest.mark.parametrize("relevance", [True, float("nan"), float("inf"), 1024.0])
def test_qrels_reject_non_finite_boolean_and_overflowing_gain(relevance: object) -> None:
    with pytest.raises(ValueError):
        FrozenQrelUnit(
            unit_id="bad", relevance=relevance,
            candidate_chunk_ids=("candidate",),
        )


def test_qrels_reject_candidate_membership_across_equivalence_units() -> None:
    with pytest.raises(ValueError, match="only one equivalence unit"):
        FrozenQrels(
            version="1.0.0", gain_mode="graded",
            units=(
                FrozenQrelUnit(
                    unit_id="first", relevance=3.0,
                    candidate_chunk_ids=("shared", "first-only"),
                ),
                FrozenQrelUnit(
                    unit_id="second", relevance=1.0,
                    candidate_chunk_ids=("shared", "second-only"),
                ),
            ),
        )


def test_needs_review_answerability_is_not_reclassified_as_reviewed_negative() -> None:
    mapping = _mapping((), (), (), answerability="needs_review")
    result = score_retrieval(
        mapping=mapping, ranked_chunks=(), qrels=None,
        config=RetrievalScoringConfig(k_values=(1,)),
    )
    assert all(item.status == "needs_review" and item.value is None
               for item in result.observations)
    assert all("answerability" in (item.reason or "") for item in result.observations)


def test_verified_truncated_context_slice_can_receive_full_coverage() -> None:
    chunk = _chunk("a", "ABCDEFGHIJ")
    mapping = _mapping(
        (chunk,), (_anchor("a", ((chunk, 2, 5),)),),
        (EvidenceGroupBinding(group_id="g", alternatives=(("a",),)),),
    )
    result = score_retrieval(
        mapping=mapping, ranked_chunks=(chunk,),
        final_context=(FinalContextChunk(
            chunk_id="a", content="CDE", chunk_start=2, chunk_end=5,
        ),),
        qrels=_binary_qrels(("a", ("a",))),
        config=RetrievalScoringConfig(k_values=(1,)),
    )
    observation = next(item for item in result.observations
                       if item.metric_id == "retrieval.context_evidence_coverage")
    assert observation.status == "ok"
    assert observation.value == 1.0


@pytest.mark.parametrize(
    "context",
    [
        FinalContextChunk(chunk_id="unknown", content="XXX"),
        FinalContextChunk(chunk_id="a", content="ZZZ", chunk_start=0, chunk_end=3),
    ],
)
def test_unverifiable_final_context_is_error_not_measured_zero(
    context: FinalContextChunk,
) -> None:
    chunk = _chunk("a", "ABCDEFGHIJ")
    mapping = _mapping(
        (chunk,), (_anchor("a", ((chunk, 0, 10),)),),
        (EvidenceGroupBinding(group_id="g", alternatives=(("a",),)),),
    )
    result = score_retrieval(
        mapping=mapping, ranked_chunks=(chunk,), final_context=(context,),
        qrels=_binary_qrels(("a", ("a",))),
        config=RetrievalScoringConfig(k_values=(1,)),
    )
    observation = next(item for item in result.observations
                       if item.metric_id == "retrieval.context_evidence_coverage")
    assert observation.status == "error"
    assert observation.value is None
    assert any(item.code in {"unknown_context_id", "context_content_mismatch"}
               for item in result.diagnostics)


def test_ambiguous_implicit_context_slice_requires_review() -> None:
    chunk = _chunk("a", "ABCABC")
    mapping = _mapping(
        (chunk,), (_anchor("a", ((chunk, 0, 6),)),),
        (EvidenceGroupBinding(group_id="g", alternatives=(("a",),)),),
    )
    result = score_retrieval(
        mapping=mapping, ranked_chunks=(chunk,),
        final_context=(FinalContextChunk(chunk_id="a", content="ABC"),),
        qrels=_binary_qrels(("a", ("a",))),
        config=RetrievalScoringConfig(k_values=(1,)),
    )
    observation = next(item for item in result.observations
                       if item.metric_id == "retrieval.context_evidence_coverage")
    assert observation.status == "needs_review"
    assert observation.value is None



def _parser_loss_qrels() -> FrozenQrels:
    return FrozenQrels(
        version="1.0.0", gain_mode="binary",
        units=(
            FrozenQrelUnit(
                unit_id="text-evidence", relevance=1.0,
                candidate_chunk_ids=("a",), source_anchor_id="a",
            ),
            FrozenQrelUnit(
                unit_id="table-evidence", relevance=1.0,
                candidate_chunk_ids=(), source_anchor_id="table",
                absence_reason="parser_evidence_loss",
            ),
        ),
    )


def test_parser_loss_qrel_keeps_frozen_full_ideal_without_fake_chunk_id() -> None:
    text_chunk = _chunk("a", "AAAAA")
    mapping = _mapping(
        (text_chunk,),
        (_anchor("a", ((text_chunk, 0, 5),)), _missing_anchor("table")),
        (
            EvidenceGroupBinding(group_id="text", alternatives=(("a",),)),
            EvidenceGroupBinding(group_id="table", alternatives=(("table",),)),
        ),
    )
    qrels = _parser_loss_qrels()

    result = score_retrieval(
        mapping=mapping, ranked_chunks=(text_chunk,), qrels=qrels,
        config=RetrievalScoringConfig(k_values=(1, 2)),
    )
    values = _values(result)
    expected_idcg = 1.0 + 1.0 / math.log2(3)
    assert values["retrieval.hit_at_1"] == 1.0
    assert values["retrieval.evidence_recall_at_1"] == 0.5
    assert values["retrieval.ndcg_at_2"] == pytest.approx(1.0 / expected_idcg)

    ndcg = next(item for item in result.observations
                if item.metric_id == "retrieval.ndcg_at_2")
    assert ndcg.status == "ok"
    assert ndcg.provenance["scoring_unit"] == "source_evidence_relevance"
    assert ndcg.provenance["candidate_binding"] == "equivalent_index_chunk_ids"
    assert ndcg.provenance["group_coverage_semantics"] == "independent_metric"
    assert ndcg.provenance["ideal_unit_count"] == 2
    assert ndcg.provenance["unmapped_unit_count"] == 1
    assert ndcg.provenance["unmapped_units"] == [{
        "unit_id": "table-evidence",
        "source_anchor_id": "table",
        "absence_reason": "parser_evidence_loss",
    }]
    assert qrels.units[1].candidate_chunk_ids == ()


def test_partial_and_missing_parser_strategies_both_keep_full_idcg() -> None:
    text_chunk = _chunk("a", "AAAAA")
    partial_chunk = _chunk("table-partial", "VALUE")
    partial_anchor = AnchorEvidenceMapping(
        anchor_id="table", anchor_location_digest="b" * 64, status="partial",
        disposition="system_failure", anchor_source_sha256=SOURCE_SHA,
        anchor_excerpt_digest=content_digest("table"), source_start=0, source_end=10,
        chunk_sets=(), partial_chunk_ids=("table-partial",), coverage_fraction=0.5,
        basis=("actual_chunks_cover_only_part_of_anchor",),
        reason="parser retained only part of the table evidence",
    )
    groups = (
        EvidenceGroupBinding(group_id="text", alternatives=(("a",),)),
        EvidenceGroupBinding(group_id="table", alternatives=(("table",),)),
    )
    mappings = (
        _mapping(
            (text_chunk,),
            (_anchor("a", ((text_chunk, 0, 5),)), _missing_anchor("table")),
            groups,
        ),
        _mapping(
            (text_chunk, partial_chunk),
            (_anchor("a", ((text_chunk, 0, 5),)), partial_anchor),
            groups,
        ),
    )

    expected_idcg = 1.0 + 1.0 / math.log2(3)
    for mapping in mappings:
        result = score_retrieval(
            mapping=mapping, ranked_chunks=(text_chunk,), qrels=_parser_loss_qrels(),
            config=RetrievalScoringConfig(k_values=(2,)),
        )
        ndcg = next(item for item in result.observations
                    if item.metric_id == "retrieval.ndcg_at_2")
        assert ndcg.status == "ok"
        assert ndcg.provenance["idcg"] == pytest.approx(expected_idcg)
        assert ndcg.value == pytest.approx(1.0 / expected_idcg)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"source_anchor_id": "table"},
        {"absence_reason": "parser_evidence_loss"},
    ],
)
def test_empty_qrel_candidates_require_typed_parser_loss_anchor_binding(
    kwargs: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="empty qrel candidates require"):
        FrozenQrelUnit(
            unit_id="table-evidence", relevance=1.0,
            candidate_chunk_ids=(), **kwargs,
        )


def test_parser_loss_absence_binding_rejects_valid_or_unknown_anchor() -> None:
    chunk = _chunk("a", "AAAAA")
    mapping = _mapping(
        (chunk,), (_anchor("a", ((chunk, 0, 5),)),),
        (EvidenceGroupBinding(group_id="g", alternatives=(("a",),)),),
    )

    for anchor_id, reason_fragment in (
        ("a", "conflicts with a source anchor that has a valid mapping path"),
        ("unknown", "unknown source anchor"),
    ):
        qrels = FrozenQrels(
            version="1.0.0", gain_mode="binary",
            units=(FrozenQrelUnit(
                unit_id="missing", relevance=1.0, candidate_chunk_ids=(),
                source_anchor_id=anchor_id,
                absence_reason="parser_evidence_loss",
            ),),
        )
        result = score_retrieval(
            mapping=mapping, ranked_chunks=(chunk,), qrels=qrels,
            config=RetrievalScoringConfig(k_values=(1,)),
        )
        observation = next(item for item in result.observations
                           if item.metric_id == "retrieval.ndcg_at_1")
        assert observation.status == "error"
        assert observation.value is None
        assert reason_fragment in (observation.reason or "")



def test_scoring_boundary_revalidates_rehashed_or_model_copied_empty_qrel() -> None:
    chunk = _chunk("a", "AAAAA")
    mapping = _mapping(
        (chunk,),
        (_anchor("a", ((chunk, 0, 5),)), _missing_anchor("table")),
        (
            EvidenceGroupBinding(group_id="text", alternatives=(("a",),)),
            EvidenceGroupBinding(group_id="table", alternatives=(("table",),)),
        ),
    )
    qrels = _parser_loss_qrels()
    forged_empty = qrels.units[1].model_copy(update={"absence_reason": None})
    forged_qrels = qrels.model_copy(update={"units": (qrels.units[0], forged_empty)})

    result = score_retrieval(
        mapping=mapping, ranked_chunks=(chunk,), qrels=forged_qrels,
        config=RetrievalScoringConfig(k_values=(2,)),
    )
    observation = next(item for item in result.observations
                       if item.metric_id == "retrieval.ndcg_at_2")
    assert observation.status == "error"
    assert observation.value is None
    assert "scoring-unit contract" in (observation.reason or "")
