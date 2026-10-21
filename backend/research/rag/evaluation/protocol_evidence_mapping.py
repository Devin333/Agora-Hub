"""Index-snapshot-bound evidence mapping for the Research RAG protocol.

The service maps frozen source anchors to actual ``PaperChunk`` instances. It
never derives gold evidence from questions or reference answers and does not
own persistence; canonical JSON helpers are supplied for outer artifact stores.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION, SCHEMA_VERSION, ImmutableVersionStr, NonEmptyStr,
    ProtocolModel, Sha256Digest, canonical_json_bytes, content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import (
    EvaluationCase, EvidenceAnchor, EvidenceLocation, RequiredEvidenceGroup,
)
from framework.rag.core import build_chunk_semantic_key, content_fingerprint

MappingStatus = Literal["exact", "equivalent", "partial", "ambiguous", "missing"]
MappingDisposition = Literal["valid", "needs_review", "system_failure"]
SpanConfidence = Literal["verified", "unknown", "ambiguous"]


class EvidenceMappingDataError(ValueError):
    """The supplied source/index identity cannot represent the frozen mapping."""


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _unique_normalized_span(content: str, excerpt: str) -> tuple[int, int] | None:
    """Locate one normalized excerpt while preserving original offsets.

    Unicode case-folding may expand one source character into multiple code
    points, while whitespace normalization collapses a run into one space.
    Every emitted normalized code point therefore retains its original range.
    """
    target = _normalized(excerpt)
    if not target:
        return None

    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pending_space_start: int | None = None
    for index, char in enumerate(content):
        if char.isspace():
            if chars and pending_space_start is None:
                pending_space_start = index
            continue
        if pending_space_start is not None:
            chars.append(" ")
            starts.append(pending_space_start)
            ends.append(index)
            pending_space_start = None
        for folded_char in char.casefold():
            chars.append(folded_char)
            starts.append(index)
            ends.append(index + 1)

    normalized_content = "".join(chars)
    first = normalized_content.find(target)
    if first < 0 or normalized_content.find(target, first + 1) >= 0:
        return None
    last = first + len(target) - 1
    return starts[first], ends[last]


class SourceLocatorRegion(ProtocolModel):
    """Checked locator-to-text projection supplied by the source reader."""
    source_start: int = Field(ge=0, strict=True)
    source_end: int = Field(gt=0, strict=True)
    content_type: Literal["text", "table", "figure", "equation"]
    section: NonEmptyStr | None
    paragraph_id: NonEmptyStr | None
    element_id: NonEmptyStr | None
    table_id: NonEmptyStr | None
    figure_id: NonEmptyStr | None
    pdf_page_index: int | None = Field(ge=0, strict=True)
    row_labels: tuple[NonEmptyStr, ...] = ()
    column_labels: tuple[NonEmptyStr, ...] = ()
    cell_references: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _valid_region(self) -> "SourceLocatorRegion":
        if self.source_end <= self.source_start:
            raise ValueError("source_end must be greater than source_start")
        return self


class CheckedSourceText(ProtocolModel):
    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_sha256: Sha256Digest
    normalized_text: NonEmptyStr
    normalized_text_sha256: Sha256Digest
    locator_regions: tuple[SourceLocatorRegion, ...] = ()

    @classmethod
    def create(cls, *, paper_id: str, document_version: str, source_sha256: str,
               normalized_text: str,
               locator_regions: Sequence[SourceLocatorRegion] = ()) -> "CheckedSourceText":
        return cls(paper_id=paper_id, document_version=document_version,
                   source_sha256=source_sha256, normalized_text=normalized_text,
                   normalized_text_sha256=text_sha256(normalized_text),
                   locator_regions=tuple(locator_regions))

    @model_validator(mode="after")
    def _verified_text(self) -> "CheckedSourceText":
        if self.normalized_text_sha256 != text_sha256(self.normalized_text):
            raise ValueError("normalized_text_sha256 does not match normalized_text")
        return self


class ChunkSourceSpan(ProtocolModel):
    """Typed projection from a chunk range to normalized source text."""
    source_start: int = Field(ge=0, strict=True)
    source_end: int = Field(gt=0, strict=True)
    chunk_start: int = Field(ge=0, strict=True)
    chunk_end: int = Field(gt=0, strict=True)
    source_locator: NonEmptyStr
    normalized_text_sha256: Sha256Digest
    confidence: SpanConfidence = "verified"

    @model_validator(mode="after")
    def _valid_span(self) -> "ChunkSourceSpan":
        if self.source_end <= self.source_start or self.chunk_end <= self.chunk_start:
            raise ValueError("source and chunk spans must be non-empty")
        if self.confidence == "verified" and (self.source_end - self.source_start) != (self.chunk_end - self.chunk_start):
            raise ValueError("verified source/chunk spans require offset-preserving lengths")
        return self


class ChunkSpanDescriptor(ProtocolModel):
    """Caller-owned source projection for an actual index chunk."""
    chunk_id: NonEmptyStr
    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_sha256: Sha256Digest
    chunk_content_sha256: Sha256Digest
    source_locator: NonEmptyStr
    spans: tuple[ChunkSourceSpan, ...] = ()

    @classmethod
    def for_chunk(cls, chunk: PaperChunk, *, document_version: str,
                  source_sha256: str, source_locator: str,
                  spans: Sequence[ChunkSourceSpan] = ()) -> "ChunkSpanDescriptor":
        return cls(chunk_id=chunk.chunk_id, paper_id=chunk.paper_id,
                   document_version=document_version, source_sha256=source_sha256,
                   chunk_content_sha256=chunk_content_sha256(chunk.content),
                   source_locator=source_locator, spans=tuple(spans))


class IndexBinding(ProtocolModel):
    corpus_snapshot_id: NonEmptyStr
    index_snapshot_id: NonEmptyStr
    index_content_sha256: Sha256Digest
    parser_id: NonEmptyStr
    parser_version: ImmutableVersionStr
    parser_config_sha256: Sha256Digest
    chunker_id: NonEmptyStr
    chunker_version: ImmutableVersionStr
    chunker_config_sha256: Sha256Digest
    index_config_sha256: Sha256Digest


class IndexedChunkIdentity(ProtocolModel):
    chunk_id: NonEmptyStr
    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_sha256: Sha256Digest
    chunk_content_sha256: Sha256Digest
    chunk_content_length: int = Field(ge=1, strict=True)
    source_locator: NonEmptyStr
    spans: tuple[ChunkSourceSpan, ...]


class AnchorChunkContribution(ProtocolModel):
    chunk_id: NonEmptyStr
    source_start: int = Field(ge=0, strict=True)
    source_end: int = Field(gt=0, strict=True)
    chunk_start: int = Field(ge=0, strict=True)
    chunk_end: int = Field(gt=0, strict=True)
    source_locator: NonEmptyStr

    @model_validator(mode="after")
    def _valid_ranges(self) -> "AnchorChunkContribution":
        if self.source_end <= self.source_start or self.chunk_end <= self.chunk_start:
            raise ValueError("anchor contributions require non-empty source and chunk ranges")
        return self


class AnchorChunkSet(ProtocolModel):
    set_id: NonEmptyStr
    chunk_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    contributions: tuple[AnchorChunkContribution, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent_chunks(self) -> "AnchorChunkSet":
        if len(set(self.chunk_ids)) != len(self.chunk_ids):
            raise ValueError("anchor chunk sets must not repeat chunk IDs")
        contribution_ids = {item.chunk_id for item in self.contributions}
        if contribution_ids != set(self.chunk_ids):
            raise ValueError("chunk_ids must exactly match chunks with real contributions")
        contribution_keys = [
            (item.chunk_id, item.source_start, item.source_end,
             item.chunk_start, item.chunk_end, item.source_locator)
            for item in self.contributions
        ]
        if len(contribution_keys) != len(set(contribution_keys)):
            raise ValueError("anchor chunk sets must not repeat contributions")
        return self


class AnchorEvidenceMapping(ProtocolModel):
    anchor_id: NonEmptyStr
    status: MappingStatus
    disposition: MappingDisposition
    anchor_source_sha256: Sha256Digest
    anchor_location_digest: Sha256Digest
    anchor_excerpt_digest: Sha256Digest
    source_start: int | None = Field(default=None, ge=0, strict=True)
    source_end: int | None = Field(default=None, gt=0, strict=True)
    chunk_sets: tuple[AnchorChunkSet, ...]
    partial_chunk_ids: tuple[NonEmptyStr, ...]
    coverage_fraction: float = Field(ge=0.0, le=1.0)
    basis: tuple[NonEmptyStr, ...] = Field(min_length=1)
    reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _consistent_mapping(self) -> "AnchorEvidenceMapping":
        if (self.source_start is None) != (self.source_end is None):
            raise ValueError("source range endpoints must be supplied together")
        if self.source_start is not None and self.source_end is not None:
            if self.source_end <= self.source_start:
                raise ValueError("source_end must exceed source_start")
        if len(self.partial_chunk_ids) != len(set(self.partial_chunk_ids)):
            raise ValueError("partial_chunk_ids must be unique")
        set_ids = [item.set_id for item in self.chunk_sets]
        if len(set_ids) != len(set(set_ids)):
            raise ValueError("anchor chunk set IDs must be unique")
        if self.status == "exact":
            if len(self.chunk_sets) != 1 or self.coverage_fraction != 1.0 or self.disposition != "valid":
                raise ValueError("exact mappings require one valid full chunk set")
        elif self.status == "equivalent":
            if len(self.chunk_sets) < 2 or self.coverage_fraction != 1.0 or self.disposition != "valid":
                raise ValueError("equivalent mappings require multiple valid full chunk sets")
        elif self.status == "partial":
            if self.chunk_sets or not self.partial_chunk_ids or self.disposition != "system_failure":
                raise ValueError("partial mappings require only partial chunks and system_failure")
            if not (0.0 < self.coverage_fraction < 1.0):
                raise ValueError("partial mappings require coverage strictly between zero and one")
        elif self.status == "ambiguous":
            if self.chunk_sets or self.coverage_fraction != 0.0 or self.disposition != "needs_review":
                raise ValueError("ambiguous mappings require needs_review without claimed coverage")
        elif self.status == "missing":
            if self.chunk_sets or self.partial_chunk_ids or self.coverage_fraction != 0.0:
                raise ValueError("missing mappings cannot claim chunks or coverage")
            if self.disposition != "system_failure":
                raise ValueError("missing mappings require system_failure")
        if self.status in {"exact", "equivalent", "partial", "missing"} and self.source_start is None:
            raise ValueError(f"{self.status} mappings require an explicit source range")
        if self.status in {"exact", "equivalent"} and self.partial_chunk_ids:
            raise ValueError("complete mappings cannot also claim partial chunk IDs")
        return self

class EvidenceGroupBinding(ProtocolModel):
    group_id: NonEmptyStr
    alternatives: tuple[tuple[NonEmptyStr, ...], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_alternatives(self) -> "EvidenceGroupBinding":
        for alternative in self.alternatives:
            if not alternative or len(alternative) != len(set(alternative)):
                raise ValueError("evidence alternatives must be non-empty and contain unique anchor IDs")
        if len(self.alternatives) != len(set(self.alternatives)):
            raise ValueError("evidence group alternatives must be unique")
        return self


class _IndexEvidenceMappingContent(ProtocolModel):
    record_type: Literal["evidence_mapping"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    mapping_version: ImmutableVersionStr
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    dataset_digest: Sha256Digest
    case_id: NonEmptyStr
    case_digest: Sha256Digest
    answerability: Literal["answerable", "unanswerable", "needs_review"]
    binding: IndexBinding
    indexed_chunk_scope: Literal["full_index", "single_paper"] = "full_index"
    indexed_chunks: tuple[IndexedChunkIdentity, ...]
    anchor_mappings: tuple[AnchorEvidenceMapping, ...]
    required_evidence_groups: tuple[EvidenceGroupBinding, ...]

    @model_validator(mode="after")
    def _unique_and_referentially_sound(self) -> "_IndexEvidenceMappingContent":
        _validate_mapping_content(self)
        return self


def _validate_mapping_content(mapping: "_IndexEvidenceMappingContent") -> None:
    chunk_ids = [item.chunk_id for item in mapping.indexed_chunks]
    anchor_ids = [item.anchor_id for item in mapping.anchor_mappings]
    group_ids = [item.group_id for item in mapping.required_evidence_groups]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise EvidenceMappingDataError("indexed_chunks contains duplicate chunk IDs")
    if len(anchor_ids) != len(set(anchor_ids)):
        raise EvidenceMappingDataError("anchor_mappings contains duplicate anchor IDs")
    if len(group_ids) != len(set(group_ids)):
        raise EvidenceMappingDataError("required_evidence_groups contains duplicate group IDs")
    if mapping.indexed_chunk_scope == "single_paper":
        paper_ids = {item.paper_id for item in mapping.indexed_chunks}
        if len(paper_ids) > 1:
            raise EvidenceMappingDataError("single-paper indexed chunk scope contains multiple papers")

    indexed = {item.chunk_id: item for item in mapping.indexed_chunks}
    known_chunks = set(indexed)
    for item in mapping.indexed_chunks:
        try:
            IndexedChunkIdentity.model_validate(item.model_dump(mode="python"))
        except ValueError as exc:
            raise EvidenceMappingDataError(
                f"indexed chunk {getattr(item, 'chunk_id', 'unknown')!r} is invalid"
            ) from exc
        span_keys = [
            (span.source_start, span.source_end, span.chunk_start, span.chunk_end,
             span.source_locator, span.normalized_text_sha256, span.confidence)
            for span in item.spans
        ]
        if len(span_keys) != len(set(span_keys)):
            raise EvidenceMappingDataError(f"indexed chunk {item.chunk_id!r} repeats a source span")
        if any(span.chunk_end > item.chunk_content_length for span in item.spans):
            raise EvidenceMappingDataError(f"indexed chunk {item.chunk_id!r} has a span outside chunk content")

    for anchor in mapping.anchor_mappings:
        unknown_partial = set(anchor.partial_chunk_ids) - known_chunks
        if unknown_partial:
            raise EvidenceMappingDataError(
                f"anchor {anchor.anchor_id!r} partial chunks are outside the bound index"
            )
        path_signatures: set[tuple[str, ...]] = set()
        for chunk_set in anchor.chunk_sets:
            contribution_ids = {item.chunk_id for item in chunk_set.contributions}
            if contribution_ids != set(chunk_set.chunk_ids):
                raise EvidenceMappingDataError(
                    f"anchor {anchor.anchor_id!r} chunk set contains unused or missing chunk IDs"
                )
            signature = tuple(sorted(chunk_set.chunk_ids))
            if signature in path_signatures:
                raise EvidenceMappingDataError(
                    f"anchor {anchor.anchor_id!r} repeats an equivalent chunk path"
                )
            path_signatures.add(signature)
            if anchor.source_start is None or anchor.source_end is None:
                raise EvidenceMappingDataError(
                    f"anchor {anchor.anchor_id!r} has contributions without an explicit source range"
                )
            for contribution in chunk_set.contributions:
                indexed_chunk = indexed.get(contribution.chunk_id)
                if indexed_chunk is None:
                    raise EvidenceMappingDataError(
                        f"anchor {anchor.anchor_id!r} references a chunk outside the bound index"
                    )
                if not (anchor.source_start <= contribution.source_start
                        < contribution.source_end <= anchor.source_end):
                    raise EvidenceMappingDataError(
                        f"anchor {anchor.anchor_id!r} contribution exceeds its source range"
                    )
                if contribution.chunk_end > indexed_chunk.chunk_content_length:
                    raise EvidenceMappingDataError(
                        f"anchor {anchor.anchor_id!r} contribution exceeds chunk content"
                    )
                if indexed_chunk.spans:
                    matched_span = any(
                        span.confidence == "verified"
                        and span.source_locator == contribution.source_locator
                        and span.source_start <= contribution.source_start
                        and contribution.source_end <= span.source_end
                        and contribution.chunk_start
                            == span.chunk_start + contribution.source_start - span.source_start
                        and contribution.chunk_end
                            == span.chunk_start + contribution.source_end - span.source_start
                        for span in indexed_chunk.spans
                    )
                    if not matched_span:
                        raise EvidenceMappingDataError(
                            f"anchor {anchor.anchor_id!r} contribution is not backed by a verified chunk span"
                        )
                elif contribution.source_locator != indexed_chunk.source_locator:
                    raise EvidenceMappingDataError(
                        f"anchor {anchor.anchor_id!r} contribution locator disagrees with its chunk"
                    )
            if _coverage_fraction(chunk_set.contributions, anchor.source_start, anchor.source_end) != 1.0:
                raise EvidenceMappingDataError(
                    f"anchor {anchor.anchor_id!r} chunk set does not completely cover the source range"
                )
        try:
            AnchorEvidenceMapping.model_validate(anchor.model_dump(mode="python"))
        except ValueError as exc:
            raise EvidenceMappingDataError(
                f"anchor mapping {getattr(anchor, 'anchor_id', 'unknown')!r} is invalid"
            ) from exc

    known_anchors = set(anchor_ids)
    for group in mapping.required_evidence_groups:
        try:
            EvidenceGroupBinding.model_validate(group.model_dump(mode="python"))
        except ValueError as exc:
            raise EvidenceMappingDataError(
                f"evidence group {getattr(group, 'group_id', 'unknown')!r} is invalid"
            ) from exc
        for alternative in group.alternatives:
            if not set(alternative).issubset(known_anchors):
                raise EvidenceMappingDataError(
                    f"evidence group {group.group_id!r} references an unknown anchor"
                )


class IndexEvidenceMapping(_IndexEvidenceMappingContent):
    mapping_digest: Sha256Digest
    _DIGEST_FIELD: ClassVar[str] = "mapping_digest"

    @classmethod
    def create(cls, **content: Any) -> "IndexEvidenceMapping":
        if cls._DIGEST_FIELD in content:
            raise ValueError("mapping_digest is calculated; do not provide it to create()")
        normalized = _IndexEvidenceMappingContent.model_validate(content)
        payload = normalized.model_dump(mode="json")
        return cls.model_validate_json(json.dumps({**payload, "mapping_digest": content_digest(payload)}, ensure_ascii=False))

    def digest_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={self._DIGEST_FIELD})

    def verify_digest(self) -> None:
        _validate_mapping_content(self)
        if self.mapping_digest != content_digest(self.digest_payload()):
            raise EvidenceMappingDataError("mapping_digest does not match mapping content")

    @model_validator(mode="after")
    def _verify_digest(self) -> "IndexEvidenceMapping":
        self.verify_digest()
        return self


def mapping_to_json(mapping: IndexEvidenceMapping) -> str:
    mapping.verify_digest()
    return canonical_json_bytes(mapping).decode("utf-8")


def mapping_from_json(payload: str | bytes) -> IndexEvidenceMapping:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    try:
        json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceMappingDataError("invalid evidence mapping JSON") from exc
    mapping = IndexEvidenceMapping.model_validate_json(raw)
    mapping.verify_digest()
    if canonical_json_bytes(mapping) != raw:
        raise EvidenceMappingDataError("evidence mapping JSON is not canonical")
    return mapping


def calculate_index_content_sha256(chunks: Sequence[PaperChunk]) -> str:
    payload = [{
        "chunk_id": chunk.chunk_id, "paper_id": chunk.paper_id,
        "parse_source": chunk.parse_source, "chunk_type": chunk.chunk_type,
        "parent_chunk_id": chunk.parent_chunk_id,
        "content_sha256": chunk_content_sha256(chunk.content),
        "metadata": chunk.metadata,
    } for chunk in chunks]
    return content_digest(payload)


def validate_mapping_binding(
    mapping: IndexEvidenceMapping, *, dataset_id: str, dataset_version: str,
    dataset_digest: str, corpus_snapshot_id: str, index_snapshot_id: str,
    index_content_sha256: str, parser_config_sha256: str,
    chunker_config_sha256: str, index_config_sha256: str,
) -> None:
    mapping.verify_digest()
    actual = (mapping.dataset_id, mapping.dataset_version, mapping.dataset_digest,
              mapping.binding.corpus_snapshot_id, mapping.binding.index_snapshot_id,
              mapping.binding.index_content_sha256,
              mapping.binding.parser_config_sha256,
              mapping.binding.chunker_config_sha256,
              mapping.binding.index_config_sha256)
    expected = (dataset_id, dataset_version, dataset_digest, corpus_snapshot_id,
                index_snapshot_id, index_content_sha256, parser_config_sha256,
                chunker_config_sha256, index_config_sha256)
    if actual != expected:
        raise EvidenceMappingDataError("evidence mapping is stale for the requested dataset/index binding")


def _metadata_descriptor(chunk: PaperChunk) -> ChunkSpanDescriptor | None:
    metadata = chunk.metadata
    version = str(metadata.get("document_version") or "").strip()
    source_hash = str(metadata.get("source_sha256") or metadata.get("source_hash") or "").strip()
    locator = str(metadata.get("source_locator") or "").strip()
    if not version or len(source_hash) != 64 or not locator:
        return None
    raw_spans = metadata.get("source_spans") or metadata.get("citation_spans") or ()
    if isinstance(raw_spans, Mapping):
        raw_spans = (raw_spans,)
    spans: list[ChunkSourceSpan] = []
    if isinstance(raw_spans, Sequence) and not isinstance(raw_spans, (str, bytes)):
        for raw in raw_spans:
            if not isinstance(raw, Mapping):
                continue
            try:
                spans.append(ChunkSourceSpan(
                    source_start=int(raw["source_start"]), source_end=int(raw["source_end"]),
                    chunk_start=int(raw.get("chunk_start", 0)),
                    chunk_end=int(raw.get("chunk_end", len(chunk.content))),
                    source_locator=str(raw.get("source_locator") or locator),
                    normalized_text_sha256=str(raw["normalized_text_sha256"]),
                    confidence=str(raw.get("confidence") or "verified"),
                ))
            except (KeyError, TypeError, ValueError):
                continue
    return ChunkSpanDescriptor.for_chunk(chunk, document_version=version,
                                         source_sha256=source_hash,
                                         source_locator=locator, spans=spans)


def project_actual_index(actual_chunks: Sequence[PaperChunk], *,
                         descriptors: Sequence[ChunkSpanDescriptor] = ()) -> tuple[IndexedChunkIdentity, ...]:
    by_id: dict[str, PaperChunk] = {}
    for chunk in actual_chunks:
        if chunk.chunk_id in by_id:
            raise EvidenceMappingDataError(f"actual index repeats chunk_id {chunk.chunk_id!r}")
        by_id[chunk.chunk_id] = chunk
    provided = {item.chunk_id: item for item in descriptors}
    if len(provided) != len(tuple(descriptors)):
        raise EvidenceMappingDataError("chunk span descriptors repeat a chunk_id")
    unexpected = set(provided) - set(by_id)
    if unexpected:
        raise EvidenceMappingDataError(f"chunk descriptors reference absent actual chunks: {sorted(unexpected)}")

    projected: list[IndexedChunkIdentity] = []
    for chunk in actual_chunks:
        descriptor = provided.get(chunk.chunk_id) or _metadata_descriptor(chunk)
        if descriptor is None:
            locator = str(chunk.metadata.get("source_locator") or f"chunk://{chunk.paper_id}/{chunk.chunk_id}")
            source_hash = str(chunk.metadata.get("source_sha256") or chunk.metadata.get("source_hash") or "")
            version = str(chunk.metadata.get("document_version") or "unknown")
            if len(source_hash) != 64:
                source_hash = "0" * 64
            descriptor = ChunkSpanDescriptor.for_chunk(chunk, document_version=version,
                                                        source_sha256=source_hash,
                                                        source_locator=locator)
        _validate_descriptor(chunk, descriptor)
        projected.append(IndexedChunkIdentity(
            **descriptor.model_dump(mode="python"),
            chunk_content_length=len(chunk.content),
        ))
    return tuple(projected)


def _validate_descriptor(chunk: PaperChunk, descriptor: ChunkSpanDescriptor) -> None:
    if descriptor.chunk_id != chunk.chunk_id or descriptor.paper_id != chunk.paper_id:
        raise EvidenceMappingDataError("chunk descriptor identity does not match the actual PaperChunk")
    if descriptor.chunk_content_sha256 != chunk_content_sha256(chunk.content):
        raise EvidenceMappingDataError(f"chunk descriptor content is stale for {chunk.chunk_id!r}")
    metadata = chunk.metadata
    metadata_locator = str(metadata.get("source_locator") or "").strip()
    if metadata_locator and metadata_locator != descriptor.source_locator:
        raise EvidenceMappingDataError(f"source_locator disagrees for chunk {chunk.chunk_id!r}")
    metadata_source_hash = str(metadata.get("source_sha256") or metadata.get("source_hash") or "").strip()
    if metadata_source_hash and metadata_source_hash != descriptor.source_sha256:
        raise EvidenceMappingDataError(f"source hash disagrees for chunk {chunk.chunk_id!r}")
    metadata_version = str(metadata.get("document_version") or "").strip()
    if metadata_version and metadata_version != descriptor.document_version:
        raise EvidenceMappingDataError(f"document version disagrees for chunk {chunk.chunk_id!r}")
    metadata_content_hash = str(metadata.get("content_hash") or "").strip()
    if metadata_content_hash and metadata_content_hash != content_fingerprint(chunk.content):
        # PaperDocumentChunker may compute semantic identity from the chunk's
        # non-overlapping main span while the indexed content also contains an
        # adjacent-sentence overlap.  In that case the semantic fingerprint is
        # not an integrity hash for the complete indexed text.  Validate its
        # structured identity, while descriptor.chunk_content_sha256 above
        # remains the authoritative digest of the actual chunk content.
        parts = metadata.get("semantic_key_parts")
        semantic_key = str(metadata.get("semantic_key") or "").strip()
        if (not isinstance(parts, Mapping)
                or str(parts.get("content_hash") or "").strip() != metadata_content_hash
                or not semantic_key):
            raise EvidenceMappingDataError(f"content_hash metadata is stale for chunk {chunk.chunk_id!r}")
        expected_key = build_chunk_semantic_key(
            document_id=chunk.paper_id,
            chunk_type=chunk.chunk_type,
            section_title=chunk.section_title,
            source_locator=descriptor.source_locator,
            content=chunk.content,
            content_hash=metadata_content_hash,
        ).key
        if semantic_key != expected_key:
            raise EvidenceMappingDataError(f"semantic key metadata is stale for chunk {chunk.chunk_id!r}")
    for span in descriptor.spans:
        if span.chunk_end > len(chunk.content):
            raise EvidenceMappingDataError(f"chunk span exceeds actual content for {chunk.chunk_id!r}")


def build_index_evidence_mapping(
    *, case: EvaluationCase, dataset_digest: str, mapping_version: str,
    binding: IndexBinding, actual_chunks: Sequence[PaperChunk],
    checked_sources: Sequence[CheckedSourceText],
    chunk_span_descriptors: Sequence[ChunkSpanDescriptor] = (),
    indexed_chunk_scope: Literal["full_index", "single_paper"] = "full_index",
    validated_index_content_sha256: str | None = None,
) -> IndexEvidenceMapping:
    """Build one case mapping from frozen anchors and the actual index snapshot."""
    if indexed_chunk_scope == "full_index":
        if validated_index_content_sha256 is not None:
            raise EvidenceMappingDataError("full-index mapping cannot use a delegated index validation")
        if binding.index_content_sha256 != calculate_index_content_sha256(actual_chunks):
            raise EvidenceMappingDataError("index_content_sha256 does not match actual PaperChunk snapshot")
    elif indexed_chunk_scope == "single_paper":
        if validated_index_content_sha256 != binding.index_content_sha256:
            raise EvidenceMappingDataError("single-paper mapping requires validation of the bound full index")
        paper_ids = {chunk.paper_id for chunk in actual_chunks}
        if len(paper_ids) > 1 or (paper_ids and case.evidence_anchors
                and next(iter(paper_ids)) != case.evidence_anchors[0].paper_id):
            raise EvidenceMappingDataError("single-paper mapping contains chunks outside its evidence paper")
        if paper_ids and any(anchor.paper_id not in paper_ids for anchor in case.evidence_anchors):
            raise EvidenceMappingDataError("single-paper mapping omits a chunk scope for an evidence paper")
    else:
        raise EvidenceMappingDataError("unsupported indexed chunk scope")
    indexed = project_actual_index(actual_chunks, descriptors=chunk_span_descriptors)
    sources = {(item.paper_id, item.document_version, item.source_sha256): item
               for item in checked_sources}
    if len(sources) != len(tuple(checked_sources)):
        raise EvidenceMappingDataError("checked source artifacts repeat a source revision")
    revision_hashes: dict[tuple[str, str], set[str]] = {}
    for item in checked_sources:
        revision_hashes.setdefault((item.paper_id, item.document_version), set()).add(
            item.source_sha256
        )
    conflicts = [identity for identity, hashes in revision_hashes.items() if len(hashes) > 1]
    if conflicts:
        raise EvidenceMappingDataError(
            f"checked source artifacts conflict for immutable revisions: {sorted(conflicts)}"
        )
    anchor_mappings = tuple(
        _map_anchor(anchor, sources=sources, indexed=indexed, actual_chunks=actual_chunks,
                    indexed_chunk_scope=indexed_chunk_scope)
        for anchor in case.evidence_anchors
    )
    mapping = IndexEvidenceMapping.create(
        record_type="evidence_mapping", schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION, mapping_version=mapping_version,
        dataset_id=case.dataset_id, dataset_version=case.dataset_version,
        dataset_digest=dataset_digest, case_id=case.case_id,
        case_digest=case.case_digest, answerability=case.answerability,
        binding=binding, indexed_chunk_scope=indexed_chunk_scope,
        indexed_chunks=indexed, anchor_mappings=anchor_mappings,
        required_evidence_groups=tuple(_group_binding(group) for group in case.required_evidence_groups),
    )
    mapping.verify_digest()
    return mapping


def _group_binding(group: RequiredEvidenceGroup) -> EvidenceGroupBinding:
    return EvidenceGroupBinding(
        group_id=group.group_id,
        alternatives=tuple(item.required_anchor_ids for item in group.alternative_sufficient_sets),
    )


def _map_anchor(
    anchor: EvidenceAnchor, *,
    sources: Mapping[tuple[str, str, str], CheckedSourceText],
    indexed: Sequence[IndexedChunkIdentity], actual_chunks: Sequence[PaperChunk],
    indexed_chunk_scope: Literal["full_index", "single_paper"],
) -> AnchorEvidenceMapping:
    key = (anchor.paper_id, anchor.document_version, anchor.source_sha256)
    source = sources.get(key)
    if source is None:
        conflicting_revision = any(
            item.paper_id == anchor.paper_id
            and item.document_version == anchor.document_version
            and item.source_sha256 != anchor.source_sha256
            for item in sources.values()
        )
        if conflicting_revision:
            raise EvidenceMappingDataError(
                f"checked source revision/hash is stale for anchor {anchor.anchor_id!r}"
            )
        raise EvidenceMappingDataError(
            f"checked source exact identity is missing for anchor {anchor.anchor_id!r}"
        )
    if anchor.location.normalized_fulltext_sha256 not in {None, source.normalized_text_sha256}:
        raise EvidenceMappingDataError(f"normalized source artifact is stale for anchor {anchor.anchor_id!r}")

    resolved = _resolve_anchor_range(anchor, source)
    if resolved is None:
        return AnchorEvidenceMapping(
            anchor_id=anchor.anchor_id, status="ambiguous", disposition="needs_review",
            anchor_source_sha256=anchor.source_sha256,
            anchor_location_digest=content_digest(anchor.location),
            anchor_excerpt_digest=anchor.source_excerpt_digest,
            source_start=None, source_end=None, chunk_sets=(), partial_chunk_ids=(),
            coverage_fraction=0.0, basis=("source_anchor_locator_not_uniquely_resolved",),
            reason="source anchor has no reliable locator/content range in the checked source artifact",
        )
    source_start, source_end, anchor_basis = resolved
    if _normalized(source.normalized_text[source_start:source_end]) != _normalized(anchor.source_excerpt):
        raise EvidenceMappingDataError(f"source excerpt does not match checked source range for anchor {anchor.anchor_id!r}")

    actual_by_id = {item.chunk_id: item for item in actual_chunks}
    candidates: dict[str, list[AnchorChunkContribution]] = {}
    undecidable: list[str] = []
    for item in indexed:
        item_key = (item.paper_id, item.document_version, item.source_sha256)
        if item_key != key:
            if item.paper_id != anchor.paper_id or indexed_chunk_scope == "single_paper":
                continue
            locator_match = _locator_matches(
                anchor.location, item, actual_by_id[item.chunk_id]
            )
            if item.document_version == anchor.document_version:
                if item.source_sha256 == "0" * 64:
                    if locator_match:
                        undecidable.append(item.chunk_id)
                else:
                    raise EvidenceMappingDataError(
                        f"actual index source revision/hash conflicts for anchor {anchor.anchor_id!r}"
                    )
            elif item.document_version == "unknown" and locator_match:
                undecidable.append(item.chunk_id)
            continue
        contributions: list[AnchorChunkContribution] = []
        for span in item.spans:
            if span.normalized_text_sha256 != source.normalized_text_sha256:
                raise EvidenceMappingDataError(f"chunk span uses a stale normalized source for {item.chunk_id!r}")
            if span.confidence != "verified":
                if _ranges_overlap(source_start, source_end, span.source_start, span.source_end):
                    undecidable.append(item.chunk_id)
                continue
            overlap_start = max(source_start, span.source_start)
            overlap_end = min(source_end, span.source_end)
            if overlap_start >= overlap_end:
                continue
            chunk_start = span.chunk_start + (overlap_start - span.source_start)
            chunk_end = chunk_start + (overlap_end - overlap_start)
            chunk = actual_by_id[item.chunk_id]
            if _normalized(chunk.content[chunk_start:chunk_end]) != _normalized(source.normalized_text[overlap_start:overlap_end]):
                raise EvidenceMappingDataError(f"chunk/source span content disagrees for {item.chunk_id!r}")
            contributions.append(AnchorChunkContribution(
                chunk_id=item.chunk_id, source_start=overlap_start, source_end=overlap_end,
                chunk_start=chunk_start, chunk_end=chunk_end, source_locator=span.source_locator,
            ))
        if not contributions and _locator_matches(anchor.location, item, actual_by_id[item.chunk_id]):
            located = _unique_normalized_span(actual_by_id[item.chunk_id].content, anchor.source_excerpt)
            if located is not None:
                contributions.append(AnchorChunkContribution(
                    chunk_id=item.chunk_id, source_start=source_start, source_end=source_end,
                    chunk_start=located[0], chunk_end=located[1], source_locator=item.source_locator,
                ))
            elif not item.spans:
                undecidable.append(item.chunk_id)
        if contributions:
            candidates[item.chunk_id] = contributions

    full_sets = _minimal_covering_sets(candidates, source_start, source_end)
    if full_sets is None:
        return AnchorEvidenceMapping(
            anchor_id=anchor.anchor_id, status="ambiguous", disposition="needs_review",
            anchor_source_sha256=anchor.source_sha256,
            anchor_location_digest=content_digest(anchor.location),
            anchor_excerpt_digest=anchor.source_excerpt_digest,
            source_start=source_start, source_end=source_end, chunk_sets=(),
            partial_chunk_ids=tuple(sorted(candidates)), coverage_fraction=0.0,
            basis=tuple(dict.fromkeys((*anchor_basis, "candidate_cover_paths_exceed_safe_enumeration"))),
            reason="equivalent sufficient chunk paths cannot be exhaustively adjudicated",
        )
    if full_sets:
        return AnchorEvidenceMapping(
            anchor_id=anchor.anchor_id,
            status="exact" if len(full_sets) == 1 else "equivalent", disposition="valid",
            anchor_source_sha256=anchor.source_sha256,
            anchor_location_digest=content_digest(anchor.location),
            anchor_excerpt_digest=anchor.source_excerpt_digest,
            source_start=source_start, source_end=source_end, chunk_sets=full_sets,
            partial_chunk_ids=(), coverage_fraction=1.0,
            basis=tuple(dict.fromkeys((*anchor_basis, "actual_chunk_content_and_source_span_verified"))),
            reason=None,
        )
    fraction = _coverage_fraction([value for values in candidates.values() for value in values],
                                  source_start, source_end)
    if fraction > 0:
        return AnchorEvidenceMapping(
            anchor_id=anchor.anchor_id, status="partial", disposition="system_failure",
            anchor_source_sha256=anchor.source_sha256,
            anchor_location_digest=content_digest(anchor.location),
            anchor_excerpt_digest=anchor.source_excerpt_digest,
            source_start=source_start, source_end=source_end, chunk_sets=(),
            partial_chunk_ids=tuple(sorted(candidates)), coverage_fraction=fraction,
            basis=tuple(dict.fromkeys((*anchor_basis, "actual_chunks_cover_only_part_of_anchor"))),
            reason="parser/chunker output does not fully cover the valid source anchor",
        )
    if undecidable:
        return AnchorEvidenceMapping(
            anchor_id=anchor.anchor_id, status="ambiguous", disposition="needs_review",
            anchor_source_sha256=anchor.source_sha256,
            anchor_location_digest=content_digest(anchor.location),
            anchor_excerpt_digest=anchor.source_excerpt_digest,
            source_start=source_start, source_end=source_end, chunk_sets=(),
            partial_chunk_ids=tuple(sorted(set(undecidable))), coverage_fraction=0.0,
            basis=tuple(dict.fromkeys((*anchor_basis, "candidate_chunks_lack_reliable_source_spans"))),
            reason="candidate chunks cannot be adjudicated from reliable spans/content",
        )
    return AnchorEvidenceMapping(
        anchor_id=anchor.anchor_id, status="missing", disposition="system_failure",
        anchor_source_sha256=anchor.source_sha256,
        anchor_location_digest=content_digest(anchor.location),
        anchor_excerpt_digest=anchor.source_excerpt_digest,
        source_start=source_start, source_end=source_end, chunk_sets=(), partial_chunk_ids=(),
        coverage_fraction=0.0,
        basis=tuple(dict.fromkeys((*anchor_basis, "valid_source_anchor_absent_from_actual_index"))),
        reason="parser/chunker/index omitted evidence present in the checked source",
    )


def _resolve_anchor_range(anchor: EvidenceAnchor,
                          source: CheckedSourceText) -> tuple[int, int, tuple[str, ...]] | None:
    location = anchor.location
    if location.char_start is not None and location.char_end is not None:
        if location.char_end > len(source.normalized_text):
            raise EvidenceMappingDataError(f"anchor range exceeds checked source for {anchor.anchor_id!r}")
        return location.char_start, location.char_end, ("frozen_normalized_text_char_range",)
    matching = [region for region in source.locator_regions if _region_matches(location, region)]
    verified = [region for region in matching
                if _normalized(source.normalized_text[region.source_start:region.source_end])
                == _normalized(anchor.source_excerpt)]
    if len(verified) == 1:
        region = verified[0]
        return region.source_start, region.source_end, ("checked_source_locator_region",)
    located = _unique_normalized_span(source.normalized_text, anchor.source_excerpt)
    if located is not None and any(
        _region_matches(location, region)
        and region.source_start <= located[0]
        and region.source_end >= located[1]
        for region in matching
    ):
        return located[0], located[1], ("unique_checked_source_excerpt_with_locator",)
    return None


def _region_matches(location: EvidenceLocation, region: SourceLocatorRegion) -> bool:
    if location.content_type != region.content_type:
        return False
    for field in ("section", "paragraph_id", "element_id", "table_id", "figure_id", "pdf_page_index"):
        required = getattr(location, field)
        if required is not None and getattr(region, field) != required:
            return False
    for field in ("row_labels", "column_labels", "cell_references"):
        required = set(getattr(location, field))
        if required and not required.issubset(set(getattr(region, field))):
            return False
    return True


def _locator_matches(location: EvidenceLocation, indexed: IndexedChunkIdentity,
                     chunk: PaperChunk) -> bool:
    metadata = chunk.metadata
    if location.table_id is not None and str(metadata.get("table_id") or "") != location.table_id:
        return False
    if location.figure_id is not None and str(metadata.get("figure_id") or chunk.figure_id or "") != location.figure_id:
        return False
    if location.element_id is not None and str(metadata.get("element_id") or metadata.get("equation_id") or "") != location.element_id:
        return False
    if location.paragraph_id is not None:
        paragraph = str(metadata.get("paragraph_id") or metadata.get("section_id") or "")
        if paragraph and paragraph != location.paragraph_id:
            return False
    if location.section is not None and chunk.section_title:
        if _normalized(location.section) != _normalized(chunk.section_title):
            return False
    if location.pdf_page_index is not None:
        page = metadata.get("pdf_page_index", metadata.get("page"))
        if page is not None:
            try:
                if int(page) != location.pdf_page_index:
                    return False
            except (TypeError, ValueError):
                return False
    return bool(indexed.source_locator)


def _minimal_covering_sets(candidates: Mapping[str, Sequence[AnchorChunkContribution]],
                           source_start: int, source_end: int) -> tuple[AnchorChunkSet, ...] | None:
    intervals_by_id = {
        chunk_id: tuple((max(source_start, item.source_start), min(source_end, item.source_end))
                        for item in contributions
                        if _ranges_overlap(source_start, source_end,
                                           item.source_start, item.source_end))
        for chunk_id, contributions in candidates.items()
    }
    solutions: list[frozenset[str]] = []
    visited: set[frozenset[str]] = set()
    states = 0

    def first_gap(selected: frozenset[str]) -> int | None:
        intervals = sorted(interval for chunk_id in selected
                           for interval in intervals_by_id[chunk_id])
        cursor = source_start
        for start, end in intervals:
            if start > cursor:
                return cursor
            cursor = max(cursor, end)
            if cursor >= source_end:
                return None
        return cursor if cursor < source_end else None

    def search(selected: frozenset[str]) -> bool:
        nonlocal states
        states += 1
        if states > 10_000 or len(solutions) > 256:
            return False
        if selected in visited or any(solution.issubset(selected) for solution in solutions):
            return True
        visited.add(selected)
        gap = first_gap(selected)
        if gap is None:
            solutions[:] = [solution for solution in solutions if not selected.issubset(solution)]
            solutions.append(selected)
            return True
        choices = [chunk_id for chunk_id, intervals in intervals_by_id.items()
                   if chunk_id not in selected
                   and any(start <= gap < end for start, end in intervals)]
        for chunk_id in sorted(choices):
            if not search(selected | {chunk_id}):
                return False
        return True

    if not search(frozenset()):
        return None
    result: list[AnchorChunkSet] = []
    for index, selected in enumerate(sorted(solutions, key=lambda item: (len(item), sorted(item))), start=1):
        subset = tuple(sorted(selected))
        contributions = tuple(item for chunk_id in subset for item in candidates[chunk_id]
                              if _ranges_overlap(source_start, source_end,
                                                 item.source_start, item.source_end))
        result.append(AnchorChunkSet(set_id=f"path-{index}", chunk_ids=subset,
                                     contributions=contributions))
    return tuple(result)


def _coverage_fraction(contributions: Sequence[AnchorChunkContribution],
                       source_start: int, source_end: int) -> float:
    intervals = sorted((max(source_start, item.source_start), min(source_end, item.source_end))
                       for item in contributions
                       if _ranges_overlap(source_start, source_end,
                                          item.source_start, item.source_end))
    if not intervals:
        return 0.0
    covered = 0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            covered += current_end - current_start
            current_start, current_end = start, end
    covered += current_end - current_start
    return min(1.0, covered / (source_end - source_start))


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


__all__ = [
    "AnchorChunkContribution", "AnchorChunkSet", "AnchorEvidenceMapping",
    "CheckedSourceText", "ChunkSourceSpan", "ChunkSpanDescriptor",
    "EvidenceGroupBinding", "EvidenceMappingDataError", "IndexBinding",
    "IndexEvidenceMapping", "IndexedChunkIdentity", "SourceLocatorRegion",
    "build_index_evidence_mapping", "calculate_index_content_sha256",
    "chunk_content_sha256", "mapping_from_json", "mapping_to_json",
    "project_actual_index", "text_sha256", "validate_mapping_binding",
]
