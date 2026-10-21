"""Composition root for Research RAG evaluation interfaces."""

from __future__ import annotations

from os import PathLike

from backend.research.application.evaluation import EvaluationApplicationService
from infrastructure.research.evaluation_dataset import FilesystemEvaluationDatasetReader
from infrastructure.research.evaluation_sources import (
    FilesystemEvidenceMappingReader,
    FilesystemSourceCorpusReader,
)


def build_evaluation_application_service(
    *,
    dataset_root: str | PathLike[str],
    corpus_root: str | PathLike[str] | None = None,
    mapping_root: str | PathLike[str] | None = None,
) -> EvaluationApplicationService:
    """Build the read-only dataset and optional external-source preflight."""

    return EvaluationApplicationService(
        dataset_reader=FilesystemEvaluationDatasetReader(dataset_root),
        source_corpus_reader=(
            None if corpus_root is None else FilesystemSourceCorpusReader(corpus_root)
        ),
        evidence_mapping_reader=(
            None if mapping_root is None else FilesystemEvidenceMappingReader(mapping_root)
        ),
    )


__all__ = ["build_evaluation_application_service"]
