from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.qasper_baseline import _build_documents
from framework.rag.core import build_chunk_semantic_key
from backend.research.rag.evaluation.qasper_mapping import (
    FrozenQasperDataset, freeze_qasper_dataset,
)


def _inputs(text: str, evidence: str, chunks: tuple[PaperChunk, ...], *, answers=None):
    qa = {
        "question_id": "q1", "question": "What is the evidence?",
        "question_type": "factoid",
        "answers": answers or [{"answer": {"free_form_answer": "answer", "evidence": [evidence]}}],
    }
    raw = {"paper-1": {
        "title": "Paper", "abstract": "",
        "full_text": [{"section_name": "Results", "paragraphs": [text]}],
        "qas": [qa],
    }}
    documents, _ = _build_documents(raw)
    return raw, documents, chunks


def _chunk(chunk_id: str, text: str, *, parent: str | None = None) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id, paper_id="paper-1", parse_source="html", content=text,
        parent_chunk_id=parent, section_title="Results",
        metadata={"source_locator": "qasper://paper-1/results"},
    )


def _freeze(text: str, evidence: str, chunks: tuple[PaperChunk, ...], *, answers=None) -> FrozenQasperDataset:
    raw, documents, actual_chunks = _inputs(text, evidence, chunks, answers=answers)
    return freeze_qasper_dataset(raw, documents, actual_chunks)


def test_freeze_generates_stable_dataset_binding_and_normalized_gold_mapping() -> None:
    source = "The result is STRASSE   robust."
    dataset = _freeze(source, "the result is strasse robust.", (_chunk("whole", source),))
    case = dataset.cases[0]
    assert dataset.dataset_id == "qasper"
    assert dataset.dataset_digest
    assert dataset.corpus_sha256
    assert dataset.index_sha256 == dataset.binding.index_content_sha256
    assert case.mapping_result.disposition == "valid"
    assert case.raw_evidence_count == 1
    assert case.sections == ("Results",)
    assert case.strict_anchors[0].paths == (("whole",),)
    assert dataset.verify_digest() is None
    raw, documents, chunks = _inputs(
        source, "the result is strasse robust.", (_chunk("whole", source),),
    )
    assert dataset.digest == freeze_qasper_dataset(raw, documents, chunks).digest


def test_parent_closure_and_adjacent_chunks_are_frozen_as_complete_paths() -> None:
    source = "Evidence supports the result."
    parent_child = _freeze(source, source, (
        _chunk("parent", source), _chunk("child", source, parent="parent"),
    ))
    relation_types = {item.relation for item in parent_child.cases[0].relations}
    assert parent_child.cases[0].mapping_result.mapping.anchor_mappings[0].status == "equivalent"
    assert {"parent", "exact_child", "normalized_span"}.issubset(relation_types)

    left, right = "Evidence supports", "supports the result."
    adjacent = _freeze(source, source, (_chunk("left", left), _chunk("right", right)))
    paths = adjacent.cases[0].mapping_result.mapping.anchor_mappings[0].chunk_sets
    assert any(set(path.chunk_ids) == {"left", "right"} for path in paths)
    assert any(item.relation == "adjacent" and set(item.chunk_ids) == {"left", "right"}
               for item in adjacent.cases[0].relations)


def test_ambiguous_missing_float_and_unanswerable_evidence_are_retained() -> None:
    ambiguous = _freeze(
        "Repeated evidence. Repeated evidence.", "Repeated evidence.",
        (_chunk("whole", "Repeated evidence. Repeated evidence."),),
    )
    assert ambiguous.cases[0].mapping_result.disposition == "needs_review"
    assert ambiguous.cases[0].mapping_result.mapping.anchor_mappings[0].status == "ambiguous"

    missing = _freeze("Expected evidence.", "Expected evidence.", ())
    assert missing.cases[0].mapping_result.disposition == "system_failure"
    assert missing.cases[0].mapping_result.mapping.anchor_mappings[0].status == "missing"

    unanswerable = _freeze(
        "Text.", "", (), answers=[{"answer": {"unanswerable": True, "evidence": ["FLOAT SELECTED"]}}],
    )
    case = unanswerable.cases[0]
    assert case.mapping_result.disposition == "not_applicable"
    assert case.float_evidence_count == 0
    assert case.unanswerable_count == 1



def test_duplicate_normalized_gold_is_deduplicated_and_annotations_remain_alternatives() -> None:
    source = "Alpha evidence. Beta evidence."
    answers = [
        {"answer": {"free_form_answer": "A", "evidence": ["Alpha evidence.", "  alpha   evidence.  "]}},
        {"answer": {"free_form_answer": "B", "evidence": ["Beta evidence."]}},
    ]
    dataset = _freeze(source, "", (_chunk("whole", source),), answers=answers)
    mapping = dataset.cases[0].mapping_result.mapping
    assert mapping is not None
    assert dataset.cases[0].raw_evidence_count == 3
    assert len(mapping.anchor_mappings) == 2
    assert len(mapping.required_evidence_groups) == 1
    alternatives = mapping.required_evidence_groups[0].alternatives
    assert len(alternatives) == 2
    assert {len(item) for item in alternatives} == {1}


def test_semantic_identity_for_non_overlapping_main_span_binds_full_chunk_digest() -> None:
    full_text = "Previous sentence. Main indexed paragraph."
    semantic_text = "Main indexed paragraph."
    locator = "qasper://paper-1/results"
    semantic = build_chunk_semantic_key(
        document_id="paper-1", chunk_type="paragraph", section_title="Results",
        source_locator=locator, content=semantic_text,
    )
    chunk = PaperChunk(
        chunk_id="overlap", paper_id="paper-1", parse_source="html",
        content=full_text, section_title="Results",
        metadata={"source_locator": locator, "semantic_key": semantic.key,
                  "semantic_key_parts": dict(semantic.parts),
                  "content_hash": semantic.content_hash},
    )
    frozen = _freeze(full_text, full_text, (chunk,))
    assert frozen.cases[0].mapping_result.disposition == "valid"
    assert frozen.cases[0].strict_anchors[0].paths == (("overlap",),)
