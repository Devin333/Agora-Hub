from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from backend.research.document.models import PaperChunk
from backend.research.domain.document import ResearchDocument
from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION, SCHEMA_VERSION, ProtocolModel, content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import (
    AlternativeSufficientSet, EvidenceAnchor, EvidenceLocation, ReferenceClaim,
    RequiredEvidenceGroup,
)
from backend.research.rag.evaluation.protocol_evidence_mapping import (
    CheckedSourceText, ChunkSourceSpan, ChunkSpanDescriptor, IndexBinding,
    IndexEvidenceMapping, SourceLocatorRegion, build_index_evidence_mapping,
    calculate_index_content_sha256, text_sha256,
)
from backend.research.rag.evaluation.qasper_protocol import (
    QASPER_MAPPING_VERSION, QasperMappingResult,
)


class QasperMappingError(ValueError):
    """Raised when QASPER inputs cannot be bound to supplied source/index data."""


class FrozenAnchorStrictChunks(ProtocolModel):
    anchor_id: str
    paths: tuple[tuple[str, ...], ...]


class FrozenAnchorPath(ProtocolModel):
    anchor_id: str
    relation: Literal["exact_child", "parent", "adjacent", "normalized_span"]
    chunk_ids: tuple[str, ...]


class FrozenQasperCase(ProtocolModel):
    case_id: str
    query: str
    paper_id: str
    question_type: str
    sections: tuple[str, ...]
    raw_evidence_count: int
    float_evidence_count: int
    unanswerable_count: int
    mapping_result: QasperMappingResult
    strict_anchors: tuple[FrozenAnchorStrictChunks, ...]
    relations: tuple[FrozenAnchorPath, ...]


class FrozenQasperDataset(ProtocolModel):
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    binding: IndexBinding
    corpus_sha256: str
    index_sha256: str
    cases: tuple[FrozenQasperCase, ...]
    digest: str

    def verify_digest(self) -> None:
        payload = self.model_dump(mode="json", exclude={"digest"})
        if self.digest != content_digest(payload):
            raise QasperMappingError("frozen QASPER dataset digest mismatch")
        for case in self.cases:
            if case.mapping_result.mapping is not None:
                case.mapping_result.mapping.verify_digest()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_with_offsets(text: str) -> tuple[str, tuple[int, ...], tuple[int, ...]]:
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pending: int | None = None
    for offset, char in enumerate(text):
        if char.isspace():
            if chars and pending is None:
                pending = offset
            continue
        if pending is not None:
            chars.append(" ")
            starts.append(pending)
            ends.append(offset)
            pending = None
        for folded in char.casefold():
            chars.append(folded)
            starts.append(offset)
            ends.append(offset + 1)
    return "".join(chars), tuple(starts), tuple(ends)


def _unique_span(haystack: str, needle: str) -> tuple[int, int] | None:
    target, _, _ = _normalized_with_offsets(needle)
    normalized, starts, ends = _normalized_with_offsets(haystack)
    if not target:
        return None
    first = normalized.find(target)
    if first < 0 or normalized.find(target, first + 1) >= 0:
        return None
    last = first + len(target) - 1
    start, end = starts[first], ends[last]
    leading = len(needle) - len(needle.lstrip())
    trailing = len(needle) - len(needle.rstrip())
    while leading and start > 0 and haystack[start - 1].isspace():
        start -= 1
        leading -= 1
    while trailing and end < len(haystack) and haystack[end].isspace():
        end += 1
        trailing -= 1
    return start, end


def _paper_rows(raw_dataset: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    wrapped = raw_dataset.get("papers", raw_dataset)
    if not isinstance(wrapped, Mapping):
        raise QasperMappingError("raw QASPER dataset must map paper IDs to paper records")
    dataset_id = str(raw_dataset.get("dataset_id") or "qasper")
    dataset_version = str(raw_dataset.get("dataset_version") or raw_dataset.get("version") or "qasper-raw/v1")
    papers = {
        str(paper_id): paper for paper_id, paper in wrapped.items()
        if paper_id not in {"dataset_id", "dataset_version", "version"}
    }
    if not papers or any(not paper_id.strip() or not isinstance(paper, Mapping) for paper_id, paper in papers.items()):
        raise QasperMappingError("raw QASPER dataset contains invalid paper records")
    return dataset_id, dataset_version, papers


def _qa_rows(paper: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    qas = paper.get("qas") or ()
    if isinstance(qas, Sequence) and not isinstance(qas, (str, bytes)):
        return tuple(item for item in qas if isinstance(item, Mapping))
    if isinstance(qas, Mapping):
        questions = qas.get("question") or ()
        answers = qas.get("answers") or ()
        types = qas.get("question_type") or qas.get("question_types") or ()
        ids = qas.get("question_id") or qas.get("question_ids") or ()
        if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
            raise QasperMappingError("QASPER qas.question must be a sequence")
        return tuple({
            "question": question,
            "answers": answers[index] if index < len(answers) else (),
            "question_type": types[index] if isinstance(types, Sequence) and index < len(types) else "unknown",
            "question_id": ids[index] if isinstance(ids, Sequence) and index < len(ids) else str(index),
        } for index, question in enumerate(questions))
    raise QasperMappingError("paper qas must be a sequence or QASPER question object")


def _answer_rows(qa: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    answers = qa.get("answers") or ()
    if isinstance(answers, Mapping):
        answers = (answers,)
    if isinstance(answers, Sequence) and not isinstance(answers, (str, bytes)):
        flattened: list[Mapping[str, Any]] = []
        for item in answers:
            if isinstance(item, Mapping):
                flattened.append(item)
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                flattened.extend(nested for nested in item if isinstance(nested, Mapping))
        return tuple(flattened)
    return ()


def _answer_content(annotation: Mapping[str, Any]) -> Mapping[str, Any]:
    answer = annotation.get("answer", annotation)
    return answer if isinstance(answer, Mapping) else {}


def _normalized_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _build_source(document: ResearchDocument) -> tuple[CheckedSourceText, dict[str, tuple[int, int]]]:
    pieces: list[str] = []
    sections: dict[str, tuple[int, int]] = {}
    regions: list[SourceLocatorRegion] = []
    cursor = 0
    for section in document.sections:
        text = section.text.strip()
        if not text:
            continue
        if pieces:
            pieces.append("\n\n")
            cursor += 2
        start, end = cursor, cursor + len(text)
        pieces.append(text)
        cursor = end
        sections[section.title] = (start, end)
        regions.append(SourceLocatorRegion(
            source_start=start, source_end=end, content_type="text", section=section.title,
            paragraph_id=None, element_id=None, table_id=None, figure_id=None,
            pdf_page_index=None, row_labels=(), column_labels=(), cell_references=(),
        ))
    fulltext = "".join(pieces)
    if not fulltext:
        raise QasperMappingError(f"document {document.paper_id!r} has no checked full text")
    source_hash = document.source_hash
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise QasperMappingError(f"document {document.paper_id!r} source_hash is not SHA-256")
    version = str(document.source_snapshot_id or document.parser_version or "qasper-document/v1")
    source = CheckedSourceText.create(
        paper_id=document.paper_id, document_version=version, source_sha256=source_hash,
        normalized_text=fulltext, locator_regions=regions,
    )
    return source, sections


def _case_anchor_data(
    *, paper_id: str, case_id: str, qa: Mapping[str, Any], source: CheckedSourceText,
    section_ranges: Mapping[str, tuple[int, int]],
) -> tuple[tuple[EvidenceAnchor, ...], tuple[RequiredEvidenceGroup, ...], str, int, int, tuple[str, ...], str]:
    answers = _answer_rows(qa)
    unanswerable_count = 0
    raw_evidence_count = 0
    float_count = 0
    anchors_by_key: dict[str, EvidenceAnchor] = {}
    alternative_sets: list[AlternativeSufficientSet] = []
    sections: set[str] = set()
    answer_text = ""
    for answer_index, annotation in enumerate(answers):
        answer = _answer_content(annotation)
        if answer.get("unanswerable"):
            unanswerable_count += 1
            continue
        answer_text = answer_text or str(answer.get("free_form_answer") or "").strip()
        raw_evidence = answer.get("evidence") or ()
        if not isinstance(raw_evidence, Sequence) or isinstance(raw_evidence, (str, bytes)):
            continue
        anchor_ids: list[str] = []
        for evidence in raw_evidence:
            excerpt = str(evidence).strip()
            if not excerpt:
                continue
            raw_evidence_count += 1
            if excerpt.casefold().startswith("float selected"):
                float_count += 1
                continue
            key = _normalized_key(excerpt)
            anchor = anchors_by_key.get(key)
            if anchor is None:
                span = _unique_span(source.normalized_text, excerpt)
                section_title: str | None = None
                if span is not None:
                    matching = [name for name, (start, end) in section_ranges.items()
                                if start <= span[0] and span[1] <= end]
                    if len(matching) == 1:
                        section_title = matching[0]
                        sections.add(section_title)
                anchor_id = "qasper-anchor-" + _sha256(f"{case_id}\0{key}")[:24]
                location = EvidenceLocation(
                    content_type="text", pdf_page_index=None, printed_page=None,
                    section=section_title or "unresolved-qasper-location", paragraph_id=None,
                    char_start=None, char_end=None, element_id=None, table_id=None,
                    row_labels=(), column_labels=(), cell_references=(), figure_id=None,
                    caption=None, normalized_fulltext_sha256=source.normalized_text_sha256,
                )
                anchor = EvidenceAnchor(
                    anchor_id=anchor_id, paper_id=paper_id, document_version=source.document_version,
                    source_sha256=source.source_sha256,
                    claim_ids=("qasper-claim-" + _sha256(case_id)[:20],),
                    location=location, source_excerpt=excerpt,
                    source_excerpt_digest=content_digest(excerpt),
                )
                anchors_by_key[key] = anchor
            anchor_ids.append(anchor.anchor_id)
        if anchor_ids:
            alternative_sets.append(AlternativeSufficientSet(
                set_id=f"annotation-{answer_index + 1}",
                required_anchor_ids=tuple(dict.fromkeys(anchor_ids)),
            ))
    query = str(qa.get("question") or qa.get("query") or "").strip()
    if not query:
        raise QasperMappingError(f"QASPER case {case_id!r} has an empty question")
    anchors = tuple(anchors_by_key.values())
    groups: tuple[RequiredEvidenceGroup, ...] = ()
    if alternative_sets:
        claim_id = anchors[0].claim_ids[0]
        # Repeated annotators can produce identical evidence sets.  They are
        # one logical alternative for retrieval coverage and the shared mapping
        # contract correctly rejects duplicate alternatives.
        unique_alternatives = {item.required_anchor_ids: item for item in alternative_sets}
        groups = (RequiredEvidenceGroup(
            group_id="qasper-required-evidence", claim_ids=(claim_id,),
            alternative_sufficient_sets=tuple(unique_alternatives.values()),
        ),)
        # Every alternative must support the group claim; all anchors share it.
    return anchors, groups, query, raw_evidence_count, float_count, tuple(sorted(sections)), answer_text


@dataclass(frozen=True)
class _MappingCase:
    dataset_id: str
    dataset_version: str
    case_id: str
    case_digest: str
    answerability: str
    evidence_anchors: tuple[EvidenceAnchor, ...]
    required_evidence_groups: tuple[RequiredEvidenceGroup, ...]


def _make_binding(documents: Sequence[ResearchDocument], chunks: Sequence[PaperChunk], corpus_sha: str, index_sha: str) -> IndexBinding:
    parser_signature = sorted({(doc.parser_backend or "unknown", doc.parser_version or "unknown") for doc in documents})
    chunk_types = sorted({(chunk.parse_source, chunk.chunk_type) for chunk in chunks})
    parser_identity = content_digest(parser_signature)
    chunker_identity = content_digest(chunk_types)
    return IndexBinding(
        corpus_snapshot_id="qasper-corpus-" + corpus_sha[:20],
        index_snapshot_id="qasper-index-" + index_sha[:20],
        index_content_sha256=index_sha,
        parser_id="qasper-supplied-document-parser", parser_version="observed-" + parser_identity[:16],
        parser_config_sha256=parser_identity,
        chunker_id="qasper-supplied-chunk-snapshot", chunker_version="observed-" + chunker_identity[:16],
        chunker_config_sha256=chunker_identity,
        index_config_sha256=content_digest({"index_sha256": index_sha, "chunk_types": chunk_types}),
    )


def _chunk_descriptors(
    chunks: Sequence[PaperChunk], sources: Mapping[str, CheckedSourceText],
) -> tuple[ChunkSpanDescriptor, ...]:
    descriptors: list[ChunkSpanDescriptor] = []
    for chunk in chunks:
        source = sources.get(chunk.paper_id)
        if source is None:
            continue
        locator = str(chunk.metadata.get("source_locator") or f"qasper://{chunk.paper_id}/chunk/{chunk.chunk_id}")
        metadata_hash = str(chunk.metadata.get("source_sha256") or chunk.metadata.get("source_hash") or source.source_sha256)
        metadata_version = str(chunk.metadata.get("document_version") or source.document_version)
        if metadata_hash != source.source_sha256 or metadata_version != source.document_version:
            continue
        spans: list[ChunkSourceSpan] = []
        found = _unique_span(source.normalized_text, chunk.content)
        if found is not None:
            start, end = found
        else:
            start, end = -1, -1
        if found is not None and end - start == len(chunk.content):
            spans.append(ChunkSourceSpan(
                source_start=start, source_end=end, chunk_start=0, chunk_end=len(chunk.content),
                source_locator=locator, normalized_text_sha256=source.normalized_text_sha256,
            ))
        descriptors.append(ChunkSpanDescriptor.for_chunk(
            chunk, document_version=source.document_version, source_sha256=source.source_sha256,
            source_locator=locator, spans=spans,
        ))
    return tuple(descriptors)


def wrap_qasper_mapping(mapping: IndexEvidenceMapping) -> QasperMappingResult:
    """Expose a frozen index mapping without consulting ranked candidates."""
    mapping.verify_digest()
    dispositions = {anchor.disposition for anchor in mapping.anchor_mappings}
    if mapping.answerability == "unanswerable":
        disposition, reason = "not_applicable", "QASPER case is marked unanswerable."
    elif mapping.answerability == "needs_review" or "needs_review" in dispositions:
        disposition, reason = "needs_review", "One or more gold anchors require review or are ambiguous."
    elif "system_failure" in dispositions:
        disposition, reason = "system_failure", "One or more gold anchors are missing or partially mapped."
    else:
        disposition, reason = "valid", None
    return QasperMappingResult(case_id=mapping.case_id, disposition=disposition, mapping=mapping, reason=reason)


def _frozen_case(
    mapping: IndexEvidenceMapping, *, qa: Mapping[str, Any], paper_id: str,
    question_type: str, sections: tuple[str, ...], raw_evidence_count: int,
    float_count: int, unanswerable_count: int, query: str, chunks_by_id: Mapping[str, PaperChunk],
    parent_chunk_ids: set[str],
) -> FrozenQasperCase:
    strict: list[FrozenAnchorStrictChunks] = []
    relations: list[FrozenAnchorPath] = []
    for anchor in mapping.anchor_mappings:
        paths = tuple(tuple(path.chunk_ids) for path in anchor.chunk_sets)
        strict_paths: list[tuple[str, ...]] = []
        if anchor.status == "exact":
            strict_paths.extend(paths)
        else:
            # An exact child remains strict when an alternate parent path exists.
            strict_paths.extend(
                path for path in paths if len(path) == 1
                and chunks_by_id.get(path[0]) is not None
                and chunks_by_id[path[0]].parent_chunk_id is not None
            )
        strict.append(FrozenAnchorStrictChunks(
            anchor_id=anchor.anchor_id,
            paths=tuple(dict.fromkeys(strict_paths)),
        ))
        for path in anchor.chunk_sets:
            ids = tuple(path.chunk_ids)
            relations.append(FrozenAnchorPath(anchor_id=anchor.anchor_id, relation="normalized_span", chunk_ids=ids))
            if len(ids) > 1:
                relations.append(FrozenAnchorPath(anchor_id=anchor.anchor_id, relation="adjacent", chunk_ids=ids))
            path_chunks = [chunks_by_id.get(chunk_id) for chunk_id in ids]
            if any(chunk is not None and chunk.parent_chunk_id for chunk in path_chunks):
                relations.append(FrozenAnchorPath(anchor_id=anchor.anchor_id, relation="exact_child", chunk_ids=ids))
            if any(chunk is not None and (
                chunk.metadata.get("is_parent")
                or chunk.chunk_id in parent_chunk_ids
            ) for chunk in path_chunks):
                relations.append(FrozenAnchorPath(anchor_id=anchor.anchor_id, relation="parent", chunk_ids=ids))
    unique_relations = tuple(dict.fromkeys(relations))
    return FrozenQasperCase(
        case_id=mapping.case_id, query=query, paper_id=paper_id,
        question_type=question_type, sections=sections,
        raw_evidence_count=raw_evidence_count, float_evidence_count=float_count,
        unanswerable_count=unanswerable_count, mapping_result=wrap_qasper_mapping(mapping),
        strict_anchors=tuple(strict), relations=unique_relations,
    )


def freeze_qasper_dataset(
    raw: Mapping[str, Any], documents: Sequence[ResearchDocument], chunks: Sequence[PaperChunk],
) -> FrozenQasperDataset:
    """Deterministically map raw QASPER evidence to one supplied immutable index snapshot."""
    dataset_id, dataset_version, papers = _paper_rows(raw)
    documents_by_id = {doc.paper_id: doc for doc in documents}
    if len(documents_by_id) != len(documents) or set(documents_by_id) != set(papers):
        raise QasperMappingError("document paper IDs must uniquely match raw dataset papers")
    chunks_by_paper: dict[str, list[PaperChunk]] = {paper_id: [] for paper_id in papers}
    chunks_by_id: dict[str, PaperChunk] = {}
    for chunk in chunks:
        if chunk.chunk_id in chunks_by_id or chunk.paper_id not in papers:
            raise QasperMappingError("chunks must have unique IDs and belong to a raw dataset paper")
        chunks_by_id[chunk.chunk_id] = chunk
        chunks_by_paper[chunk.paper_id].append(chunk)
    sources_by_id: dict[str, CheckedSourceText] = {}
    sections_by_paper: dict[str, dict[str, tuple[int, int]]] = {}
    for paper_id, document in documents_by_id.items():
        source, section_ranges = _build_source(document)
        sources_by_id[paper_id] = source
        sections_by_paper[paper_id] = section_ranges
    index_sha = calculate_index_content_sha256(chunks)
    corpus_sha = content_digest([
        {"paper_id": paper_id, "source_sha256": source.source_sha256,
         "normalized_text_sha256": source.normalized_text_sha256}
        for paper_id, source in sorted(sources_by_id.items())
    ])
    dataset_digest = content_digest(raw)
    binding = _make_binding(documents, chunks, corpus_sha, index_sha)
    descriptors = _chunk_descriptors(chunks, sources_by_id)
    mutable_descriptors: dict[str, list[ChunkSpanDescriptor]] = {paper_id: [] for paper_id in papers}
    for descriptor in descriptors:
        mutable_descriptors[descriptor.paper_id].append(descriptor)
    descriptors_by_paper = {paper_id: tuple(rows) for paper_id, rows in mutable_descriptors.items()}
    parent_chunk_ids = {item.parent_chunk_id for item in chunks if item.parent_chunk_id}
    cases: list[FrozenQasperCase] = []
    for paper_id, paper in papers.items():
        for qa_index, qa in enumerate(_qa_rows(paper)):
            question_id = str(qa.get("question_id") or qa.get("id") or qa_index)
            case_id = f"{paper_id}:{question_id}"
            anchors, groups, query, evidence_count, float_count, sections, answer = _case_anchor_data(
                paper_id=paper_id, case_id=case_id, qa=qa, source=sources_by_id[paper_id],
                section_ranges=sections_by_paper[paper_id],
            )
            annotations = _answer_rows(qa)
            unanswerable_count = sum(bool(_answer_content(item).get("unanswerable")) for item in annotations)
            answerability = "unanswerable" if annotations and unanswerable_count == len(annotations) else (
                "answerable" if anchors and groups else "needs_review"
            )
            case_digest = content_digest({"dataset_digest": dataset_digest, "case_id": case_id, "query": query,
                                          "anchors": anchors, "groups": groups})
            map_case = _MappingCase(dataset_id=dataset_id, dataset_version=dataset_version,
                case_id=case_id, case_digest=case_digest, answerability=answerability,
                evidence_anchors=anchors, required_evidence_groups=groups)
            try:
                mapping = build_index_evidence_mapping(
                    case=map_case, dataset_digest=dataset_digest, mapping_version=QASPER_MAPPING_VERSION,
                    binding=binding, actual_chunks=tuple(chunks_by_paper[paper_id]),
                    checked_sources=(sources_by_id[paper_id],),
                    chunk_span_descriptors=descriptors_by_paper[paper_id],
                    indexed_chunk_scope="single_paper",
                    validated_index_content_sha256=index_sha,
                )
            except (ValueError, TypeError) as exc:
                raise QasperMappingError(f"failed to freeze evidence mapping for {case_id}: {exc}") from exc
            cases.append(_frozen_case(
                mapping, qa=qa, paper_id=paper_id,
                question_type=str(qa.get("question_type") or qa.get("qa_type") or "unknown"),
                sections=sections, raw_evidence_count=evidence_count, float_count=float_count,
                unanswerable_count=unanswerable_count, query=query, chunks_by_id=chunks_by_id,
                parent_chunk_ids=parent_chunk_ids,
            ))
    payload = {
        "dataset_id": dataset_id, "dataset_version": dataset_version,
        "dataset_digest": dataset_digest, "binding": binding,
        "corpus_sha256": corpus_sha, "index_sha256": index_sha, "cases": tuple(cases),
    }
    frozen = FrozenQasperDataset(**payload, digest=content_digest(payload))
    frozen.verify_digest()
    return frozen
