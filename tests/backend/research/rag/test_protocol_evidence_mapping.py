from __future__ import annotations

import pytest
from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.protocol_common import canonical_json_bytes, content_digest
from backend.research.rag.evaluation.protocol_dataset import (
    AlternativeSufficientSet, EvaluationCase, EvidenceAnchor, EvidenceLocation,
    RequiredEvidenceGroup,
)
from backend.research.rag.evaluation.protocol_evidence_mapping import (
    CheckedSourceText, ChunkSourceSpan, ChunkSpanDescriptor,
    EvidenceMappingDataError, IndexBinding, build_index_evidence_mapping,
    calculate_index_content_sha256, mapping_from_json, mapping_to_json, text_sha256,
    validate_mapping_binding,
)

SOURCE_SHA = "a" * 64
DATASET_SHA = "d" * 64
CONFIG_SHA = "c" * 64


def _anchor(anchor_id: str, excerpt: str, start: int, end: int,
            *, source_text: str | None = None, content_type: str = "text") -> EvidenceAnchor:
    location = EvidenceLocation(
        content_type=content_type, pdf_page_index=0, printed_page=None,
        section="Results", paragraph_id="p1", char_start=start, char_end=end,
        element_id=None, table_id="table-1" if content_type == "table" else None,
        row_labels=("Accuracy",) if content_type == "table" else (),
        column_labels=("Model",) if content_type == "table" else (),
        cell_references=(), figure_id=None, caption=None,
        normalized_fulltext_sha256=text_sha256(source_text or excerpt),
    )
    return EvidenceAnchor(
        anchor_id=anchor_id, paper_id="paper-1", document_version="v1",
        source_sha256=SOURCE_SHA, claim_ids=("claim-1",), location=location,
        source_excerpt=excerpt, source_excerpt_digest=content_digest(excerpt),
    )


def _case(anchors: tuple[EvidenceAnchor, ...]) -> EvaluationCase:
    group = RequiredEvidenceGroup(
        group_id="group-1", claim_ids=("claim-1",),
        alternative_sufficient_sets=tuple(
            AlternativeSufficientSet(set_id=f"alt-{index}", required_anchor_ids=(anchor.anchor_id,))
            for index, anchor in enumerate(anchors, start=1)
        ),
    )
    return EvaluationCase.model_construct(
        record_type="case", schema_version="1.0", protocol_version="research-rag-eval/v1",
        dataset_id="dataset-1", dataset_version="1.0.0", case_id="case-1",
        case_digest="e" * 64, answerability="answerable",
        evidence_anchors=anchors, required_evidence_groups=(group,),
    )


def _chunk(chunk_id: str, content: str, locator: str = "paper://paper-1#page=0") -> PaperChunk:
    return PaperChunk(chunk_id=chunk_id, paper_id="paper-1", parse_source="pdf",
                      content=content, section_title="Results",
                      metadata={"source_locator": locator, "document_version": "v1",
                                "source_sha256": SOURCE_SHA})


def _descriptor(chunk: PaperChunk, source_start: int, source_end: int,
                source_digest: str) -> ChunkSpanDescriptor:
    return ChunkSpanDescriptor.for_chunk(
        chunk, document_version="v1", source_sha256=SOURCE_SHA,
        source_locator=str(chunk.metadata["source_locator"]),
        spans=(ChunkSourceSpan(
            source_start=source_start, source_end=source_end,
            chunk_start=0, chunk_end=len(chunk.content),
            source_locator=str(chunk.metadata["source_locator"]),
            normalized_text_sha256=source_digest,
        ),),
    )


def _binding(chunks: tuple[PaperChunk, ...], *, snapshot: str = "index-1") -> IndexBinding:
    return IndexBinding(
        corpus_snapshot_id="corpus-1", index_snapshot_id=snapshot,
        index_content_sha256=calculate_index_content_sha256(chunks),
        parser_id="paper-parser", parser_version="1.0.0", parser_config_sha256=CONFIG_SHA,
        chunker_id="paper-chunker", chunker_version="1.0.0", chunker_config_sha256=CONFIG_SHA,
        index_config_sha256=CONFIG_SHA,
    )


def test_maps_one_anchor_across_actual_chunks_and_round_trips_canonically() -> None:
    text = "prefix ABCDEFGHIJ suffix"
    start = text.index("ABCDEFGHIJ")
    source = CheckedSourceText.create(paper_id="paper-1", document_version="v1",
                                      source_sha256=SOURCE_SHA, normalized_text=text)
    chunks = (_chunk("c1", "ABCDE"), _chunk("c2", "FGHIJ"))
    mapping = build_index_evidence_mapping(
        case=_case((_anchor("a", "ABCDEFGHIJ", start, start + 10, source_text=text),)),
        dataset_digest=DATASET_SHA, mapping_version="1.0.0", binding=_binding(chunks),
        actual_chunks=chunks, checked_sources=(source,),
        chunk_span_descriptors=(
            _descriptor(chunks[0], start, start + 5, source.normalized_text_sha256),
            _descriptor(chunks[1], start + 5, start + 10, source.normalized_text_sha256),
        ),
    )

    anchor = mapping.anchor_mappings[0]
    assert anchor.status == "exact"
    assert anchor.disposition == "valid"
    assert anchor.chunk_sets[0].chunk_ids == ("c1", "c2")
    wire = mapping_to_json(mapping)
    assert mapping_from_json(wire) == mapping
    with pytest.raises(EvidenceMappingDataError, match="not canonical"):
        mapping_from_json("\n" + wire)


def test_records_multiple_equivalent_full_chunk_sets_and_partial_coverage() -> None:
    text = "ABCDEFGHIJ"
    source = CheckedSourceText.create(paper_id="paper-1", document_version="v1",
                                      source_sha256=SOURCE_SHA, normalized_text=text)
    full1, full2 = _chunk("full-1", text), _chunk("full-2", text)
    mapping = build_index_evidence_mapping(
        case=_case((_anchor("a", text, 0, 10, source_text=text),)), dataset_digest=DATASET_SHA,
        mapping_version="1.0.0", binding=_binding((full1, full2)),
        actual_chunks=(full1, full2), checked_sources=(source,),
        chunk_span_descriptors=(
            _descriptor(full1, 0, 10, source.normalized_text_sha256),
            _descriptor(full2, 0, 10, source.normalized_text_sha256),
        ),
    )
    assert mapping.anchor_mappings[0].status == "equivalent"
    assert {item.chunk_ids for item in mapping.anchor_mappings[0].chunk_sets} == {("full-1",), ("full-2",)}

    partial = _chunk("partial", "ABCDE")
    partial_mapping = build_index_evidence_mapping(
        case=_case((_anchor("a", text, 0, 10, source_text=text),)), dataset_digest=DATASET_SHA,
        mapping_version="1.0.0", binding=_binding((partial,)), actual_chunks=(partial,),
        checked_sources=(source,),
        chunk_span_descriptors=(_descriptor(partial, 0, 5, source.normalized_text_sha256),),
    )
    assert partial_mapping.anchor_mappings[0].status == "partial"
    assert partial_mapping.anchor_mappings[0].disposition == "system_failure"
    assert partial_mapping.anchor_mappings[0].coverage_fraction == 0.5


def test_unreliable_locator_is_needs_review_but_valid_table_parser_loss_is_system_failure() -> None:
    text = "Accuracy Model 92.1"
    source = CheckedSourceText.create(paper_id="paper-1", document_version="v1",
                                      source_sha256=SOURCE_SHA, normalized_text=text)
    ambiguous_location = EvidenceLocation(
        content_type="text", pdf_page_index=0, printed_page=None, section="Missing",
        paragraph_id=None, char_start=None, char_end=None, element_id=None,
        table_id=None, row_labels=(), column_labels=(), cell_references=(),
        figure_id=None, caption=None, normalized_fulltext_sha256=None,
    )
    ambiguous = EvidenceAnchor(
        anchor_id="amb", paper_id="paper-1", document_version="v1",
        source_sha256=SOURCE_SHA, claim_ids=("claim-1",), location=ambiguous_location,
        source_excerpt="Accuracy", source_excerpt_digest=content_digest("Accuracy"),
    )
    ambiguous_mapping = build_index_evidence_mapping(
        case=_case((ambiguous,)), dataset_digest=DATASET_SHA, mapping_version="1.0.0",
        binding=_binding(()), actual_chunks=(), checked_sources=(source,),
    )
    assert ambiguous_mapping.anchor_mappings[0].status == "ambiguous"
    assert ambiguous_mapping.anchor_mappings[0].disposition == "needs_review"

    table = _anchor("table", text, 0, len(text), source_text=text, content_type="table")
    missing_mapping = build_index_evidence_mapping(
        case=_case((table,)), dataset_digest=DATASET_SHA, mapping_version="1.0.0",
        binding=_binding(()), actual_chunks=(), checked_sources=(source,),
    )
    assert missing_mapping.anchor_mappings[0].status == "missing"
    assert missing_mapping.anchor_mappings[0].disposition == "system_failure"
    assert "absent_from_actual_index" in missing_mapping.anchor_mappings[0].basis[-1]



def test_unknown_chunk_source_projection_is_needs_review_not_keyword_complete() -> None:
    text = "ABCDEFGHIJ"
    source = CheckedSourceText.create(paper_id="paper-1", document_version="v1",
                                      source_sha256=SOURCE_SHA, normalized_text=text)
    anchor = _anchor("a", text, 0, 10, source_text=text)
    chunk = PaperChunk(chunk_id="unknown", paper_id="paper-1", parse_source="pdf",
                       content=text, section_title="Results",
                       metadata={"source_locator": "paper://paper-1#page=0"})
    mapping = build_index_evidence_mapping(
        case=_case((anchor,)), dataset_digest=DATASET_SHA, mapping_version="1.0.0",
        binding=_binding((chunk,)), actual_chunks=(chunk,), checked_sources=(source,),
    )
    result = mapping.anchor_mappings[0]
    assert result.status == "ambiguous"
    assert result.disposition == "needs_review"
    assert result.chunk_sets == ()
    assert result.partial_chunk_ids == ("unknown",)


def test_stale_source_index_and_external_metadata_are_data_errors() -> None:
    text = "ABCDEFGHIJ"
    source = CheckedSourceText.create(paper_id="paper-1", document_version="v1",
                                      source_sha256=SOURCE_SHA, normalized_text=text)
    chunk = _chunk("c1", text)
    descriptor = _descriptor(chunk, 0, 10, source.normalized_text_sha256)
    with pytest.raises(EvidenceMappingDataError, match="index_content_sha256"):
        build_index_evidence_mapping(
            case=_case((_anchor("a", text, 0, 10, source_text=text),)), dataset_digest=DATASET_SHA,
            mapping_version="1.0.0", binding=_binding((), snapshot="old-index"),
            actual_chunks=(chunk,), checked_sources=(source,),
            chunk_span_descriptors=(descriptor,),
        )

    stale_source = CheckedSourceText.create(paper_id="paper-1", document_version="v1",
                                            source_sha256="b" * 64, normalized_text=text)
    with pytest.raises(EvidenceMappingDataError, match="stale"):
        build_index_evidence_mapping(
            case=_case((_anchor("a", text, 0, 10, source_text=text),)), dataset_digest=DATASET_SHA,
            mapping_version="1.0.0", binding=_binding((chunk,)), actual_chunks=(chunk,),
            checked_sources=(stale_source,), chunk_span_descriptors=(descriptor,),
        )

    bad_descriptor = descriptor.model_copy(update={"source_locator": "paper://wrong"})
    with pytest.raises(EvidenceMappingDataError, match="source_locator disagrees"):
        build_index_evidence_mapping(
            case=_case((_anchor("a", text, 0, 10, source_text=text),)), dataset_digest=DATASET_SHA,
            mapping_version="1.0.0", binding=_binding((chunk,)), actual_chunks=(chunk,),
            checked_sources=(source,), chunk_span_descriptors=(bad_descriptor,),
        )


def test_validate_mapping_binding_rejects_old_snapshot_or_config() -> None:
    text = "ABCDEFGHIJ"
    source = CheckedSourceText.create(paper_id="paper-1", document_version="v1",
                                      source_sha256=SOURCE_SHA, normalized_text=text)
    chunk = _chunk("c1", text)
    binding = _binding((chunk,))
    mapping = build_index_evidence_mapping(
        case=_case((_anchor("a", text, 0, 10, source_text=text),)), dataset_digest=DATASET_SHA,
        mapping_version="1.0.0", binding=binding, actual_chunks=(chunk,),
        checked_sources=(source,),
        chunk_span_descriptors=(_descriptor(chunk, 0, 10, source.normalized_text_sha256),),
    )
    with pytest.raises(EvidenceMappingDataError, match="stale"):
        validate_mapping_binding(
            mapping, dataset_id="dataset-1", dataset_version="1.0.0",
            dataset_digest=DATASET_SHA, corpus_snapshot_id="corpus-1",
            index_snapshot_id="index-2", index_content_sha256=binding.index_content_sha256,
            parser_config_sha256=CONFIG_SHA, chunker_config_sha256=CONFIG_SHA,
            index_config_sha256=CONFIG_SHA,
        )



def _rehash_mapping(mapping):
    return mapping.model_copy(update={
        "mapping_digest": content_digest(mapping.digest_payload()),
    })


def test_unicode_casefold_and_whitespace_mapping_preserves_original_offsets() -> None:
    source_text = "prefix Straße\t \nvalue suffix"
    source_excerpt = "Straße\t \nvalue"
    source_start = source_text.index("Straße")
    source_end = source_start + len(source_excerpt)
    source = CheckedSourceText.create(
        paper_id="paper-1", document_version="v1",
        source_sha256=SOURCE_SHA, normalized_text=source_text,
    )
    chunk_text = "payload STRASSE \r\n value tail"
    chunk = _chunk("unicode", chunk_text)

    mapping = build_index_evidence_mapping(
        case=_case((_anchor(
            "unicode-anchor", "STRASSE value", source_start, source_end,
            source_text=source_text,
        ),)),
        dataset_digest=DATASET_SHA, mapping_version="1.0.0",
        binding=_binding((chunk,)), actual_chunks=(chunk,), checked_sources=(source,),
    )

    contribution = mapping.anchor_mappings[0].chunk_sets[0].contributions[0]
    expected_start = chunk_text.index("STRASSE")
    expected_end = chunk_text.index("value") + len("value")
    assert (contribution.chunk_start, contribution.chunk_end) == (expected_start, expected_end)
    assert chunk_text[contribution.chunk_start:contribution.chunk_end] == "STRASSE \r\n value"

def test_other_document_versions_are_legal_distractors_but_never_v1_evidence() -> None:
    text = "ABCDEFGHIJ"
    source_v1 = CheckedSourceText.create(
        paper_id="paper-1", document_version="v1",
        source_sha256=SOURCE_SHA, normalized_text=text,
    )
    source_v2 = CheckedSourceText.create(
        paper_id="paper-1", document_version="v2",
        source_sha256="b" * 64, normalized_text=text,
    )
    chunk_v1 = _chunk("v1-evidence", text)
    chunk_v2 = PaperChunk(
        chunk_id="v2-distractor", paper_id="paper-1", parse_source="pdf",
        content=text, section_title="Results",
        metadata={
            "source_locator": "paper://paper-1#page=0",
            "document_version": "v2", "source_sha256": "b" * 64,
        },
    )

    mapping = build_index_evidence_mapping(
        case=_case((_anchor("a", text, 0, len(text), source_text=text),)),
        dataset_digest=DATASET_SHA, mapping_version="1.0.0",
        binding=_binding((chunk_v2, chunk_v1)), actual_chunks=(chunk_v2, chunk_v1),
        checked_sources=(source_v2, source_v1),
    )

    assert mapping.anchor_mappings[0].status == "exact"
    assert mapping.anchor_mappings[0].chunk_sets[0].chunk_ids == ("v1-evidence",)

    conflicting_v1 = PaperChunk(
        chunk_id="v1-conflict", paper_id="paper-1", parse_source="pdf",
        content=text, section_title="Results",
        metadata={
            "source_locator": "paper://paper-1#page=0",
            "document_version": "v1", "source_sha256": "b" * 64,
        },
    )
    with pytest.raises(EvidenceMappingDataError, match="conflicts"):
        build_index_evidence_mapping(
            case=_case((_anchor("a", text, 0, len(text), source_text=text),)),
            dataset_digest=DATASET_SHA, mapping_version="1.0.0",
            binding=_binding((conflicting_v1,)), actual_chunks=(conflicting_v1,),
            checked_sources=(source_v1,),
        )


def test_rehashed_model_copy_cannot_forge_invalid_mapping_cross_fields() -> None:
    text = "ABCDEFGHIJ"
    source = CheckedSourceText.create(
        paper_id="paper-1", document_version="v1",
        source_sha256=SOURCE_SHA, normalized_text=text,
    )
    first, second, distractor = (
        _chunk("c1", "ABCDE"), _chunk("c2", "FGHIJ"), _chunk("c3", "noise"),
    )
    mapping = build_index_evidence_mapping(
        case=_case((_anchor("a", text, 0, len(text), source_text=text),)),
        dataset_digest=DATASET_SHA, mapping_version="1.0.0",
        binding=_binding((first, second, distractor)),
        actual_chunks=(first, second, distractor), checked_sources=(source,),
        chunk_span_descriptors=(
            _descriptor(first, 0, 5, source.normalized_text_sha256),
            _descriptor(second, 5, 10, source.normalized_text_sha256),
        ),
    )
    anchor = mapping.anchor_mappings[0]
    chunk_set = anchor.chunk_sets[0]

    incomplete_last = chunk_set.contributions[-1].model_copy(
        update={"source_end": 9, "chunk_end": 4}
    )
    incomplete_set = chunk_set.model_copy(
        update={"contributions": (*chunk_set.contributions[:-1], incomplete_last)}
    )
    incomplete_anchor = anchor.model_copy(update={"chunk_sets": (incomplete_set,)})
    incomplete = _rehash_mapping(mapping.model_copy(
        update={"anchor_mappings": (incomplete_anchor,)}
    ))
    with pytest.raises(EvidenceMappingDataError, match="completely cover"):
        incomplete.verify_digest()

    unused_set = chunk_set.model_copy(update={"chunk_ids": (*chunk_set.chunk_ids, "c3")})
    unused_anchor = anchor.model_copy(update={"chunk_sets": (unused_set,)})
    unused = _rehash_mapping(mapping.model_copy(update={"anchor_mappings": (unused_anchor,)}))
    with pytest.raises(EvidenceMappingDataError, match="unused or missing"):
        unused.verify_digest()

    invalid_range_anchor = anchor.model_copy(update={"source_end": anchor.source_start})
    invalid_range = _rehash_mapping(mapping.model_copy(
        update={"anchor_mappings": (invalid_range_anchor,)}
    ))
    with pytest.raises(EvidenceMappingDataError, match="source range"):
        invalid_range.verify_digest()

    group = mapping.required_evidence_groups[0]
    duplicate_groups = _rehash_mapping(mapping.model_copy(
        update={"required_evidence_groups": (group, group)}
    ))
    with pytest.raises(EvidenceMappingDataError, match="duplicate group IDs"):
        duplicate_groups.verify_digest()
    with pytest.raises(ValueError):
        mapping_from_json(canonical_json_bytes(duplicate_groups))
