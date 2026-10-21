from __future__ import annotations

from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_evidence_mapping import EvidenceGroupBinding
from backend.research.rag.evaluation.qasper_mapping import (
    FrozenQasperCase, FrozenQasperDataset, freeze_qasper_dataset, wrap_qasper_mapping,
)
from backend.research.rag.evaluation.qasper_protocol import (
    QasperChannelProvenance, QasperMetricDefinition, RankedResult,
)
from backend.research.rag.evaluation.qasper_scoring import score_qasper_metrics
from tests.backend.research.rag.test_qasper_mapping import _chunk, _inputs


def _ranked(chunk_id: str, rank: int) -> RankedResult:
    return RankedResult(
        chunk_id=chunk_id, rank=rank, stage="fused", raw_score=1.0,
        provenance=(QasperChannelProvenance(
            channel="dense", query_id="q", query_sha256="a" * 64,
            channel_rank=rank, raw_score=1.0,
        ),),
    )


def _definition(metric_id: str, version: str, k: int | None) -> QasperMetricDefinition:
    return QasperMetricDefinition(metric_id=metric_id, version=version, k=k)


def _all_definitions(k: int) -> tuple[QasperMetricDefinition, ...]:
    return (
        _definition("strict_hit", "qasper-strict-retrieval/v1", k),
        _definition("equivalent_hit", "qasper-equivalent-retrieval/v1", k),
        _definition("strict_coverage", "qasper-strict-retrieval/v1", k),
        _definition("equivalent_coverage", "qasper-equivalent-retrieval/v1", k),
        _definition("strict_mrr", "qasper-strict-retrieval/v1", None),
        _definition("equivalent_mrr", "qasper-equivalent-retrieval/v1", None),
        _definition("strict_ndcg", "qasper-strict-retrieval/v1", k),
        _definition("equivalent_ndcg", "qasper-equivalent-retrieval/v1", k),
    )


def _freeze(text: str, evidence: str, chunks: tuple, *, answers=None) -> FrozenQasperDataset:
    raw, documents, actual_chunks = _inputs(text, evidence, chunks, answers=answers)
    return freeze_qasper_dataset(raw, documents, actual_chunks)


def test_duplicate_rows_do_not_inflate_metrics_or_improve_original_rank() -> None:
    source = "Gold evidence."
    dataset = _freeze(source, source, (_chunk("gold", source),))
    metrics = score_qasper_metrics(
        dataset.cases[0], (_ranked("noise", 1), _ranked("gold", 3), _ranked("gold", 4)),
        _all_definitions(4), final_stage="fused",
    )
    by_id = {metric.metric_id: metric for metric in metrics}
    assert by_id["strict_hit"].numerator == 1
    assert by_id["strict_coverage"].numerator == 1
    assert by_id["strict_coverage"].denominator == 1
    assert by_id["strict_mrr"].value == 1 / 3
    assert by_id["strict_mrr"].mapping_version == dataset.cases[0].mapping_result.mapping_version
    assert 0 <= by_id["strict_ndcg"].value < 1


def test_equivalent_adjacent_path_requires_every_chunk_and_ndcg_is_bounded() -> None:
    source = "Evidence supports the result."
    left, right = "Evidence supports", "supports the result."
    dataset = _freeze(source, source, (_chunk("left", left), _chunk("right", right)))
    case = dataset.cases[0]
    partial = score_qasper_metrics(case, (_ranked("left", 1),), _all_definitions(4), final_stage="fused")
    partial_metrics = {metric.metric_id: metric for metric in partial}
    assert partial_metrics["equivalent_hit"].numerator == 0
    assert partial_metrics["equivalent_coverage"].numerator == 0

    complete = score_qasper_metrics(
        case, (_ranked("left", 1), _ranked("right", 3)), _all_definitions(4), final_stage="fused",
    )
    by_id = {metric.metric_id: metric for metric in complete}
    assert by_id["equivalent_hit"].numerator == 1
    assert by_id["equivalent_mrr"].value == 1 / 3
    assert 0 <= by_id["equivalent_ndcg"].value <= 1


def test_parent_child_equivalence_keeps_strict_child_and_null_metrics_on_ambiguity() -> None:
    source = "Parent and child evidence."
    dataset = _freeze(source, source, (
        _chunk("parent", source), _chunk("child", source, parent="parent"),
    ))
    case = dataset.cases[0]
    parent_only = score_qasper_metrics(case, (_ranked("parent", 1),), _all_definitions(4), final_stage="fused")
    parent_metrics = {metric.metric_id: metric for metric in parent_only}
    assert parent_metrics["strict_hit"].numerator == 0
    assert parent_metrics["equivalent_hit"].numerator == 1
    child_metrics = score_qasper_metrics(case, (_ranked("child", 1),), _all_definitions(4), final_stage="fused")
    assert {metric.metric_id: metric for metric in child_metrics}["strict_hit"].numerator == 1

    ambiguous = _freeze(
        "Repeated evidence. Repeated evidence.", "Repeated evidence.",
        (_chunk("whole", "Repeated evidence. Repeated evidence."),),
    ).cases[0]
    null_metrics = score_qasper_metrics(ambiguous, (), _all_definitions(4), final_stage="fused")
    assert all(metric.value is None and metric.numerator is None and metric.denominator is None
               for metric in null_metrics)
    assert all(metric.status == "needs_review" for metric in null_metrics)


def test_multiple_required_groups_keep_independent_coverage_denominators() -> None:
    source = "Alpha evidence. Beta evidence."
    answers = [{"answer": {"free_form_answer": "Both", "evidence": ["Alpha evidence.", "Beta evidence."]}}]
    dataset = _freeze(source, "", (_chunk("alpha", "Alpha evidence."), _chunk("beta", "Beta evidence.")), answers=answers)
    case = dataset.cases[0]
    mapping = case.mapping_result.mapping
    assert mapping is not None and len(mapping.anchor_mappings) == 2
    groups = tuple(EvidenceGroupBinding(
        group_id=f"required-{index}", alternatives=((anchor.anchor_id,),),
    ) for index, anchor in enumerate(mapping.anchor_mappings, start=1))
    changed = mapping.model_copy(update={"required_evidence_groups": groups, "mapping_digest": "0" * 64})
    changed = changed.model_copy(update={"mapping_digest": content_digest(changed.digest_payload())})
    frozen_case = case.model_copy(update={"mapping_result": wrap_qasper_mapping(changed)})
    metrics = score_qasper_metrics(frozen_case, (_ranked("alpha", 1),), _all_definitions(3), final_stage="fused")
    by_id = {metric.metric_id: metric for metric in metrics}
    assert by_id["equivalent_coverage"].numerator == 1
    assert by_id["equivalent_coverage"].denominator == 2
    assert by_id["equivalent_coverage"].value == 0.5



def test_shared_parent_never_makes_binary_gain_ndcg_exceed_one() -> None:
    source = "Alpha evidence. Beta evidence."
    answers = [{"answer": {"free_form_answer": "Both", "evidence": ["Alpha evidence.", "Beta evidence."]}}]
    parent = _chunk("parent", source)
    children = (_chunk("child-a", "Alpha evidence.", parent="parent"),
                _chunk("child-b", "Beta evidence.", parent="parent"))
    case = _freeze(source, "", (parent, *children), answers=answers).cases[0]
    metrics = score_qasper_metrics(case, (_ranked("parent", 1),), _all_definitions(3), final_stage="fused")
    by_id = {metric.metric_id: metric for metric in metrics}
    assert by_id["equivalent_hit"].numerator == 1
    assert by_id["equivalent_coverage"].numerator == 1
    assert by_id["equivalent_ndcg"].value <= 1


def test_scoring_selects_final_stage_from_report_without_rank_collisions() -> None:
    case = _freeze("Gold evidence.", "Gold evidence.", (_chunk("gold", "Gold evidence."),)).cases[0]
    definitions = _all_definitions(3)
    candidate = _ranked("noise", 1).model_copy(update={"stage": "candidate"})
    fused = _ranked("gold", 2).model_copy(update={"stage": "fused"})
    final = _ranked("gold", 1).model_copy(update={"stage": "reranked"})
    results = score_qasper_metrics(case, (candidate, fused, final), definitions, final_stage="reranked")
    assert {item.metric_id: item for item in results}["strict_mrr"].value == 1.0


def test_explicit_empty_reranked_stage_never_falls_back_to_fused_hits() -> None:
    case = _freeze(
        "Gold evidence.", "Gold evidence.", (_chunk("gold", "Gold evidence."),)
    ).cases[0]
    candidate = _ranked("gold", 1).model_copy(update={"stage": "candidate"})
    fused = _ranked("gold", 1).model_copy(update={"stage": "fused"})

    metrics = score_qasper_metrics(
        case, (candidate, fused), _all_definitions(5), final_stage="reranked"
    )

    by_id = {metric.metric_id: metric for metric in metrics}
    assert by_id["strict_hit"].value == 0
    assert by_id["equivalent_hit"].value == 0
    assert by_id["strict_mrr"].value == 0
