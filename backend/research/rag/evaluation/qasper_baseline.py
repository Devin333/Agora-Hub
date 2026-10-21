from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypeAlias

from backend.research.document.chunker import PaperDocumentChunker
from backend.research.domain.document import ResearchDocument
from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_common import ExecutionProfile
from backend.research.rag.evaluation.protocol_execution import ArtifactReference, CodeIdentity
from backend.research.rag.evaluation.qasper_protocol import (
    HistoricalCodeClaim,
    LegacyMetricSummary,
    QasperBaselineSource,
    QasperBaselineSnapshot,
    QasperEmbeddingCacheIdentity,
)

import numpy as np


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_FILE_READ_SIZE = 1024 * 1024
ExpectedFileSha256: TypeAlias = Mapping[str, str | tuple[str, ...]]


class QasperBaselineCaptureError(ValueError):
    """Raised when an offline QASPER baseline cannot be frozen safely."""


def sha256_file(path: str | Path, *, chunk_size: int = DEFAULT_FILE_READ_SIZE) -> str:
    """Hash an external file without loading it into memory."""

    candidate = Path(path)
    if not candidate.is_file():
        raise QasperBaselineCaptureError(f"required external file is missing: {candidate}")
    if chunk_size <= 0:
        raise QasperBaselineCaptureError("chunk_size must be positive")
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as stream:
            while block := stream.read(chunk_size):
                digest.update(block)
    except OSError as exc:
        raise QasperBaselineCaptureError(f"cannot read external file {candidate}: {exc}") from exc
    return digest.hexdigest()


def _load_json(path: str | Path, *, label: str) -> Any:
    candidate = Path(path)
    if not candidate.is_file():
        raise QasperBaselineCaptureError(f"{label} is missing: {candidate}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise QasperBaselineCaptureError(
                    f"{label} contains duplicate JSON object key: {key!r}"
                )
            result[key] = value
        return result

    try:
        with candidate.open("r", encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=reject_duplicate_keys)
    except QasperBaselineCaptureError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QasperBaselineCaptureError(f"invalid {label} JSON {candidate}: {exc}") from exc


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QasperBaselineCaptureError(f"{label} must be a JSON object")
    return value


def _normalize_evidence(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def _is_float_evidence(text: str) -> bool:
    return text.startswith("FLOAT SELECTED")


def _artifact(*, kind: str, digest: str) -> ArtifactReference:
    # Local paths are deliberately not serialized.  The digest is the portable
    # identity and the URI is not a path that could accidentally be dereferenced.
    return ArtifactReference(
        reference_id=f"qasper-baseline/{kind}/{digest}",
        kind=kind,
        uri=f"external-sha256://{digest}",
        content_sha256=digest,
    )


def _validate_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise QasperBaselineCaptureError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_dataset(dataset: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    if not dataset:
        raise QasperBaselineCaptureError("dataset must not be empty")
    normalized: dict[str, Mapping[str, Any]] = {}
    for paper_id, paper in dataset.items():
        if not isinstance(paper_id, str) or not paper_id.strip():
            raise QasperBaselineCaptureError("dataset contains an empty paper ID")
        if paper_id in normalized:
            raise QasperBaselineCaptureError(f"dataset contains duplicate paper ID: {paper_id}")
        normalized[paper_id] = _require_mapping(paper, f"paper {paper_id}")
    return normalized


def _build_documents(dataset: Mapping[str, Mapping[str, Any]]) -> tuple[list[ResearchDocument], list[PaperChunk]]:
    documents: list[ResearchDocument] = []
    chunks: list[PaperChunk] = []
    chunker = PaperDocumentChunker()
    for paper_id, paper in dataset.items():
        full_text = paper.get("full_text") or []
        if not isinstance(full_text, list):
            raise QasperBaselineCaptureError(f"paper {paper_id} full_text must be a list")
        sections: list[dict[str, Any]] = []
        for section_index, section in enumerate(full_text):
            section_map = _require_mapping(section, f"paper {paper_id} full_text[{section_index}]")
            raw_paragraphs = section_map.get("paragraphs") or []
            if not isinstance(raw_paragraphs, list):
                raise QasperBaselineCaptureError(
                    f"paper {paper_id} section {section_index} paragraphs must be a list"
                )
            paragraphs = [str(paragraph) for paragraph in raw_paragraphs if isinstance(paragraph, str) and paragraph.strip()]
            if not paragraphs:
                continue
            section_id = f"qasper-sec-{section_index}"
            sections.append(
                {
                    "section_id": section_id,
                    "title": str(section_map.get("section_name") or f"Section {section_index + 1}"),
                    "level": 1,
                    "text": "\n\n".join(paragraphs),
                    "source_ref": f"qasper://{paper_id}/section/{section_index}",
                }
            )
        # This is the legacy adapter's source identity, retained explicitly as
        # an observed reconstruction rule rather than a historical snapshot.
        source_hash = hashlib.sha256(
            json.dumps(paper, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        try:
            document = ResearchDocument.model_validate(
                {
                    "paper_id": paper_id,
                    "source_hash": source_hash,
                    "title": paper.get("title"),
                    "abstract": paper.get("abstract"),
                    "sections": sections,
                    "lineage": {
                        "source_refs": [f"qasper://{paper_id}"],
                        "source_hash": source_hash,
                    },
                    "metadata": {
                        "dataset": "QASPER v0.3",
                        "text_only_diagnostic": True,
                        "source_hash_algorithm": "sha256(json.dumps(paper, sort_keys=True, ensure_ascii=False))",
                    },
                }
            )
        except Exception as exc:
            raise QasperBaselineCaptureError(f"cannot build ResearchDocument for {paper_id}: {exc}") from exc
        documents.append(document)
        chunks.extend(chunker.chunk(document, "html"))
    return documents, chunks


def _validate_models(
    dataset_ids: set[str],
    documents: Sequence[ResearchDocument],
    chunks: Sequence[PaperChunk],
) -> None:
    if any(not isinstance(document, ResearchDocument) for document in documents):
        raise QasperBaselineCaptureError("documents must contain only ResearchDocument values")
    if any(not isinstance(chunk, PaperChunk) for chunk in chunks):
        raise QasperBaselineCaptureError("chunks must contain only PaperChunk values")
    document_ids = [document.paper_id for document in documents]
    if len(document_ids) != len(set(document_ids)):
        raise QasperBaselineCaptureError("reconstructed documents contain duplicate paper IDs")
    if set(document_ids) != dataset_ids:
        raise QasperBaselineCaptureError("document paper IDs do not match dataset paper IDs")
    chunk_ids = [chunk.chunk_id for chunk in chunks]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise QasperBaselineCaptureError("corpus contains duplicate chunk IDs")
    if any(chunk.paper_id not in dataset_ids for chunk in chunks):
        raise QasperBaselineCaptureError("corpus contains a chunk for an unknown paper")


def _derive_counts(
    dataset: Mapping[str, Mapping[str, Any]],
    chunks: Sequence[PaperChunk],
) -> dict[str, int]:
    counts = {
        "papers": len(dataset),
        "questions_total": 0,
        "questions_with_text_evidence": 0,
        "questions_without_mappable_text_evidence": 0,
        "text_evidence_items_total": 0,
        "text_evidence_items_mapped": 0,
        "float_evidence_items_excluded": 0,
        "answer_annotations_unanswerable": 0,
    }
    paragraph_chunk_keys = {
        (chunk.paper_id, str(chunk.metadata.get("section_id") or ""), int(chunk.metadata.get("para_index", -1)))
        for chunk in chunks
        if not chunk.metadata.get("is_parent", False) and chunk.chunk_type == "paragraph"
    }
    for paper_id, paper in dataset.items():
        normalized_locations: dict[str, list[tuple[int, int]]] = {}
        for section_index, section in enumerate(paper.get("full_text") or []):
            if not isinstance(section, Mapping):
                continue
            for paragraph_index, paragraph in enumerate(section.get("paragraphs") or []):
                if isinstance(paragraph, str) and paragraph.strip():
                    normalized_locations.setdefault(_normalize_evidence(paragraph), []).append(
                        (section_index, paragraph_index)
                    )
        for qa in paper.get("qas") or []:
            if not isinstance(qa, Mapping):
                raise QasperBaselineCaptureError("each QA entry must be an object")
            counts["questions_total"] += 1
            mapped_for_question = False
            seen_evidence: set[str] = set()
            for annotation in qa.get("answers") or []:
                if not isinstance(annotation, Mapping):
                    raise QasperBaselineCaptureError("each answer annotation must be an object")
                answer = annotation.get("answer") or {}
                if not isinstance(answer, Mapping):
                    raise QasperBaselineCaptureError("answer must be an object")
                if answer.get("unanswerable"):
                    counts["answer_annotations_unanswerable"] += 1
                    continue
                for raw_evidence in answer.get("evidence") or []:
                    evidence = str(raw_evidence).strip()
                    if not evidence:
                        continue
                    if _is_float_evidence(evidence):
                        counts["float_evidence_items_excluded"] += 1
                        continue
                    normalized = _normalize_evidence(evidence)
                    if normalized in seen_evidence:
                        continue
                    seen_evidence.add(normalized)
                    counts["text_evidence_items_total"] += 1
                    locations = normalized_locations.get(normalized, [])
                    mapped = any(
                        (paper_id, f"qasper-sec-{section_index}", paragraph_index) in paragraph_chunk_keys
                        for section_index, paragraph_index in locations
                    )
                    counts["text_evidence_items_mapped"] += int(mapped)
                    mapped_for_question = mapped_for_question or mapped
            if mapped_for_question:
                counts["questions_with_text_evidence"] += 1
    counts["questions_without_mappable_text_evidence"] = (
        counts["questions_total"] - counts["questions_with_text_evidence"]
    )
    return counts


def validate_embedding_cache(
    cache_path: str | Path,
    *,
    expected_dimension: int | None = None,
) -> dict[str, Any]:
    """Validate the offline NPZ contract and return only small metadata."""

    path = Path(cache_path)
    digest = sha256_file(path)
    try:
        with np.load(path, allow_pickle=False) as loaded:
            names = set(loaded.files)
            if not {"keys", "vectors"}.issubset(names):
                raise QasperBaselineCaptureError("embedding cache must contain keys and vectors arrays")
            keys = loaded["keys"]
            vectors = loaded["vectors"]
            if keys.ndim != 1 or vectors.ndim != 2:
                raise QasperBaselineCaptureError("embedding cache keys must be 1-D and vectors must be 2-D")
            if vectors.shape[0] != keys.shape[0]:
                raise QasperBaselineCaptureError("embedding cache keys and vectors row counts differ")
            if not np.issubdtype(keys.dtype, np.str_):
                raise QasperBaselineCaptureError("embedding cache keys must be string keys")
            key_values = [str(value) for value in keys.tolist()]
            if len(key_values) != len(set(key_values)):
                raise QasperBaselineCaptureError("embedding cache contains duplicate keys")
            if any(not SHA256_RE.fullmatch(value) for value in key_values):
                raise QasperBaselineCaptureError("embedding cache keys must be lowercase SHA-256 text digests")
            dimension = int(vectors.shape[1])
            if expected_dimension is not None and dimension != expected_dimension:
                raise QasperBaselineCaptureError(
                    f"embedding cache dimension {dimension} does not match expected {expected_dimension}"
                )
            if not np.issubdtype(vectors.dtype, np.floating):
                raise QasperBaselineCaptureError("embedding cache vectors must use a floating dtype")
            if not np.isfinite(vectors).all():
                raise QasperBaselineCaptureError("embedding cache vectors must contain only finite values")
            return {
                "sha256": digest,
                "rows": int(vectors.shape[0]),
                "dimension": dimension,
                "keys_digest": content_digest(sorted(key_values)),
                "dtype": str(vectors.dtype),
            }
    except QasperBaselineCaptureError:
        raise
    except (OSError, ValueError, EOFError) as exc:
        raise QasperBaselineCaptureError(f"invalid embedding cache {path}: {exc}") from exc


def _legacy_counts(summary: Mapping[str, Any]) -> dict[str, int]:
    mapping = _require_mapping(summary.get("mapping"), "legacy result mapping")
    required = (
        "papers",
        "questions_total",
        "questions_with_text_evidence",
        "questions_without_mappable_text_evidence",
        "text_evidence_items_total",
        "text_evidence_items_mapped",
        "float_evidence_items_excluded",
        "answer_annotations_unanswerable",
    )
    result: dict[str, int] = {}
    for field in required:
        value = mapping.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise QasperBaselineCaptureError(f"legacy result mapping.{field} must be a non-negative integer")
        result[field] = value
    return result


def _model_dump(value: ResearchDocument | PaperChunk) -> dict[str, Any]:
    return value.model_dump(mode="json")


def capture_qasper_baseline(
    *,
    dataset_path: str | Path,
    embedding_cache_path: str | Path,
    legacy_result_path: str | Path,
    source_script_paths: Iterable[str | Path],
    observed_reconstruction_code_identity: CodeIdentity,
    historical_code_claim: HistoricalCodeClaim,
    embedding_model_id: str | None = None,
    embedding_model_version: str | None = None,
    embedding_provider: str | None = None,
    expected_file_sha256: ExpectedFileSha256 | None = None,
    expected_counts: Mapping[str, int] | None = None,
    embedding_dimension: int | None = None,
    documents: Sequence[ResearchDocument] | None = None,
    chunks: Sequence[PaperChunk] | None = None,
    allow_legacy_count_mismatch: bool = False,
) -> QasperBaselineSnapshot:
    """Capture a small, diagnostic-only, offline QASPER baseline snapshot.

    The function hashes and validates external inputs but never copies their
    contents into the repository, calls a provider, invokes an external script,
    writes production state, or performs scoring/retrieval.
    """

    if not isinstance(allow_legacy_count_mismatch, bool):
        raise QasperBaselineCaptureError("allow_legacy_count_mismatch must be a boolean")
    if not isinstance(observed_reconstruction_code_identity, CodeIdentity):
        raise QasperBaselineCaptureError(
            "observed_reconstruction_code_identity must be the typed CodeIdentity value"
        )
    if not isinstance(historical_code_claim, HistoricalCodeClaim):
        raise QasperBaselineCaptureError("historical_code_claim must be the typed HistoricalCodeClaim value")
    dataset_file = Path(dataset_path)
    cache_file = Path(embedding_cache_path)
    result_file = Path(legacy_result_path)
    script_files = tuple(Path(path) for path in source_script_paths)
    if not script_files:
        raise QasperBaselineCaptureError("at least one source script path is required")

    file_digests = {
        "dataset": sha256_file(dataset_file),
        "embedding_cache": sha256_file(cache_file),
        "legacy_result": sha256_file(result_file),
        "source_scripts": tuple(sha256_file(path) for path in script_files),
    }
    if expected_file_sha256 is not None:
        allowed = {"dataset", "embedding_cache", "legacy_result", "source_scripts"}
        unknown = set(expected_file_sha256) - allowed
        if unknown:
            raise QasperBaselineCaptureError(f"unknown expected_file_sha256 keys: {sorted(unknown)}")
        for key in ("dataset", "embedding_cache", "legacy_result"):
            if key not in expected_file_sha256:
                continue
            expected_digest = expected_file_sha256[key]
            if not isinstance(expected_digest, str):
                raise QasperBaselineCaptureError(f"expected_file_sha256[{key}] must be a SHA-256 string")
            if _validate_digest(expected_digest, f"expected_file_sha256[{key}]") != file_digests[key]:
                raise QasperBaselineCaptureError(f"tampered {key}: expected digest does not match observed content")
        if "source_scripts" in expected_file_sha256:
            expected_scripts = expected_file_sha256["source_scripts"]
            if not isinstance(expected_scripts, tuple) or any(not isinstance(item, str) for item in expected_scripts):
                raise QasperBaselineCaptureError("expected_file_sha256[source_scripts] must be a tuple of SHA-256 strings")
            validated_scripts = tuple(
                _validate_digest(item, "expected_file_sha256[source_scripts]") for item in expected_scripts
            )
            if validated_scripts != file_digests["source_scripts"]:
                raise QasperBaselineCaptureError("tampered source scripts: expected digest does not match observed content")

    dataset_raw = _validate_dataset(_load_json(dataset_file, label="dataset"))
    summary = _require_mapping(_load_json(result_file, label="legacy result"), "legacy result")
    legacy_counts = _legacy_counts(summary)

    identity = _require_mapping(summary.get("embedding_identity"), "legacy result embedding_identity")
    summary_provider = identity.get("provider")
    summary_model = identity.get("model")
    summary_dimension = identity.get("dimension")
    if not isinstance(summary_provider, str) or not summary_provider.strip():
        raise QasperBaselineCaptureError("legacy result embedding_identity.provider must be non-empty")
    if not isinstance(summary_model, str) or not summary_model.strip():
        raise QasperBaselineCaptureError("legacy result embedding_identity.model must be non-empty")
    if isinstance(summary_dimension, bool) or not isinstance(summary_dimension, int) or summary_dimension <= 0:
        raise QasperBaselineCaptureError("legacy result embedding_identity.dimension must be a positive integer")
    if embedding_provider is not None and embedding_provider != summary_provider:
        raise QasperBaselineCaptureError("explicit embedding_provider does not match legacy result identity")
    if embedding_model_id is not None and embedding_model_id != summary_model:
        raise QasperBaselineCaptureError("explicit embedding_model_id does not match legacy result identity")
    resolved_model_id = embedding_model_id or summary_model
    resolved_provider = embedding_provider or summary_provider
    resolved_version = embedding_model_version or identity.get("version") or identity.get("revision")
    if not isinstance(resolved_version, str) or not resolved_version.strip():
        raise QasperBaselineCaptureError(
            "embedding_model_version is required when legacy result embedding_identity has no version/revision"
        )
    if embedding_dimension is not None and embedding_dimension != summary_dimension:
        raise QasperBaselineCaptureError("explicit embedding_dimension does not match legacy result identity")
    cache_metadata = validate_embedding_cache(cache_file, expected_dimension=summary_dimension)
    dataset_ids = set(dataset_raw)
    if (documents is None) != (chunks is None):
        raise QasperBaselineCaptureError("documents and chunks must be supplied together")
    reconstructed_documents, reconstructed_chunks = _build_documents(dataset_raw)
    if documents is None:
        observed_documents, observed_chunks = reconstructed_documents, reconstructed_chunks
    else:
        observed_documents, observed_chunks = list(documents), list(chunks)
        _validate_models(dataset_ids, observed_documents, observed_chunks)
        expected_document_digest = content_digest([
            _model_dump(document) for document in sorted(reconstructed_documents, key=lambda item: item.paper_id)
        ])
        expected_chunk_digest = content_digest([
            _model_dump(chunk) for chunk in sorted(reconstructed_chunks, key=lambda item: item.chunk_id)
        ])
        supplied_document_digest = content_digest([
            _model_dump(document) for document in sorted(observed_documents, key=lambda item: item.paper_id)
        ])
        supplied_chunk_digest = content_digest([
            _model_dump(chunk) for chunk in sorted(observed_chunks, key=lambda item: item.chunk_id)
        ])
        if supplied_document_digest != expected_document_digest or supplied_chunk_digest != expected_chunk_digest:
            raise QasperBaselineCaptureError(
                "supplied documents/chunks do not match deterministic QASPER full_text reconstruction"
            )
    _validate_models(dataset_ids, observed_documents, observed_chunks)
    derived_counts = _derive_counts(dataset_raw, observed_chunks)
    if legacy_counts != derived_counts and not allow_legacy_count_mismatch:
        raise QasperBaselineCaptureError(
            f"dataset and legacy result counts disagree: derived={derived_counts}, legacy={legacy_counts}"
        )
    if expected_counts is not None:
        unknown_count_keys = set(expected_counts) - set(derived_counts)
        if unknown_count_keys:
            raise QasperBaselineCaptureError(f"unknown expected_counts keys: {sorted(unknown_count_keys)}")
        for key, value in expected_counts.items():
            if isinstance(value, bool) or not isinstance(value, int) or derived_counts[key] != value:
                raise QasperBaselineCaptureError(
                    f"count mismatch for {key}: observed={derived_counts.get(key)!r}, expected={value!r}"
                )

    corpus_payload = [
        _model_dump(document) for document in sorted(observed_documents, key=lambda item: item.paper_id)
    ]
    chunk_payload = [
        _model_dump(chunk) for chunk in sorted(observed_chunks, key=lambda item: item.chunk_id)
    ]
    corpus_digest = content_digest(corpus_payload)
    chunk_digest = content_digest(chunk_payload)
    dataset_artifact = _artifact(kind="dataset", digest=file_digests["dataset"])
    cache_artifact = _artifact(kind="embedding_cache", digest=file_digests["embedding_cache"])
    result_artifact = _artifact(kind="legacy_result", digest=file_digests["legacy_result"])
    corpus_artifact = _artifact(kind="corpus", digest=corpus_digest)
    chunks_artifact = _artifact(kind="chunks", digest=chunk_digest)
    # The protocol has one reconstruction_script role; bind every supplied
    # script digest as one deterministic aggregate while retaining per-file
    # expected-digest checks at the API boundary.
    script_artifact = _artifact(
        kind="reconstruction_script",
        digest=content_digest(list(file_digests["source_scripts"])),
    )
    sources = (
        QasperBaselineSource(role="dataset", artifact=dataset_artifact),
        QasperBaselineSource(role="corpus", artifact=corpus_artifact),
        QasperBaselineSource(role="chunks", artifact=chunks_artifact),
        QasperBaselineSource(role="embedding_cache", artifact=cache_artifact),
        QasperBaselineSource(role="raw_result", artifact=result_artifact),
        QasperBaselineSource(role="reconstruction_script", artifact=script_artifact),
    )

    for metric_group in (summary.get("strict_metrics"), summary.get("equivalent_metrics")):
        if not isinstance(metric_group, Mapping) or not metric_group:
            raise QasperBaselineCaptureError("legacy result must include non-empty strict_metrics and equivalent_metrics objects")
    legacy_summaries = tuple(
        LegacyMetricSummary(
            metric_id=(
                f"legacy_{metric_id}"
                if str(metric_id).startswith(f"{group_name}_")
                else f"legacy_{group_name}_{metric_id}"
            ),
            value=float(value),
            denominator=legacy_counts["questions_with_text_evidence"],
        )
        for group_name, group in (("strict", summary.get("strict_metrics")), ("equivalent", summary.get("equivalent_metrics")))
        for metric_id, value in group.items()
    )
    return QasperBaselineSnapshot.create(
        baseline_id="qasper-dev-v0.3-diagnostic-baseline",
        dataset_id="qasper-dev-v0.3",
        dataset_version="qasper-v0.3",
        dataset_sha256=file_digests["dataset"],
        corpus_id="qasper-full-text-research-document-observed-reconstruction",
        corpus_version="observed-reconstruction/v1",
        corpus_sha256=corpus_digest,
        chunking_version="PaperDocumentChunker/html/observed-reconstruction-v1",
        chunk_count=len(observed_chunks),
        dataset_case_count=derived_counts["questions_total"],
        evaluable_case_count=derived_counts["questions_with_text_evidence"],
        embedding_cache_id=f"{resolved_provider}/{resolved_model_id}",
        embedding_cache_sha256=file_digests["embedding_cache"],
        embedding_model_id=f"{resolved_provider}/{resolved_model_id}",
        embedding_model_version=resolved_version,
        embedding_dimension=summary_dimension,
        embedding_cache_identity=QasperEmbeddingCacheIdentity(
            rows=cache_metadata["rows"],
            dimension=cache_metadata["dimension"],
            dtype=cache_metadata["dtype"],
            keys_sha256=cache_metadata["keys_digest"],
        ),
        raw_result_sha256=file_digests["legacy_result"],
        historical_code_claim=historical_code_claim,
        observed_reconstruction_code_identity=observed_reconstruction_code_identity,
        legacy_summary=legacy_summaries,
        chunk_sha256=chunk_digest,
        paper_count=len(observed_documents),
        float_evidence_count=derived_counts["float_evidence_items_excluded"],
        unanswerable_annotation_count=derived_counts["answer_annotations_unanswerable"],
        sources=sources,
        execution_profile=ExecutionProfile.FIXTURE,
        allow_legacy_count_mismatch=allow_legacy_count_mismatch,
    )


__all__ = [
    "QasperBaselineCaptureError",
    "capture_qasper_baseline",
    "sha256_file",
    "validate_embedding_cache",
]
