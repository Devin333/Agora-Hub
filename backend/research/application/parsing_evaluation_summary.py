from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.research.rag.evaluation.document_parsing_acceptance import (
    ParsingAcceptanceProfile,
    evaluate_document_parsing_acceptance,
    load_acceptance_profile,
)
from backend.research.rag.evaluation.document_parsing_protocol import (
    ParsingEvaluationManifest,
    load_parsing_evaluation_manifest,
    summarize_parsing_evaluation_manifest,
)


class ParsingEvaluationSummaryService:
    """Load and summarize recorded parsing manifests; never run evaluators."""

    def summarize_file(self, path: str | Path) -> dict[str, Any]:
        return self.summarize(load_parsing_evaluation_manifest(path))

    def summarize(self, manifest: ParsingEvaluationManifest) -> dict[str, Any]:
        return summarize_parsing_evaluation_manifest(manifest)

    def evaluate_acceptance(
        self,
        *,
        baseline: ParsingEvaluationManifest | None,
        candidate: ParsingEvaluationManifest | None,
        profile: ParsingAcceptanceProfile | None,
    ) -> dict[str, Any]:
        return evaluate_document_parsing_acceptance(baseline, candidate, profile)

    def evaluate_acceptance_files(
        self,
        *,
        baseline_path: str | Path | None,
        candidate_path: str | Path | None,
        profile_path: str | Path | None,
    ) -> dict[str, Any]:
        baseline = None if baseline_path is None else load_parsing_evaluation_manifest(baseline_path)
        candidate = None if candidate_path is None else load_parsing_evaluation_manifest(candidate_path)
        profile = None if profile_path is None else load_acceptance_profile(str(profile_path))
        return evaluate_document_parsing_acceptance(baseline, candidate, profile)


__all__ = ["ParsingEvaluationSummaryService"]
