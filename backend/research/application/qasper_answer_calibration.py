"""Export a human calibration set from a completed QASPER answer run and score it."""
from __future__ import annotations

import json
from typing import Any

from backend.research.application.qasper_assets import QasperAssetService
from backend.research.ports.qasper_evaluation import QasperArtifactStore
from backend.research.rag.evaluation.qasper_answer_calibration import (
    CALIBRATION_PROTOCOL,
    build_calibration_cases,
    render_calibration_sheet,
    render_label_template,
    score_calibration,
    select_calibration_cases,
)
from backend.research.rag.retrieval.paper_answer_generator import render_answer_context

LABELS_FILE = "labels.csv"


class QasperAnswerCalibrationService:
    def __init__(self, assets: QasperAssetService, artifacts: QasperArtifactStore) -> None:
        self._assets = assets
        self._artifacts = artifacts

    def export(self, *, run_directory: str, output_directory: str, size: int, seed: str) -> dict[str, Any]:
        report = json.loads(self._artifacts.read(run_directory, "report.json"))
        answers = self._artifacts.read(run_directory, "answers.jsonl").decode("utf-8")
        cases = [json.loads(line) for line in answers.splitlines() if line.strip()]
        assets = self._assets.read_assets()
        # Passages are re-rendered from the dataset, so it must be the one the run used.
        if assets.dataset_sha256 != report["identity"]["dataset_sha256"]:
            raise ValueError("dataset does not match the answer run being calibrated")
        chunks = {chunk.chunk_id: chunk for chunk in assets.chunks}

        def passage_text(chunk_id: str) -> str:
            if chunk_id not in chunks:
                raise ValueError(f"cited chunk {chunk_id} is absent from the dataset")
            return render_answer_context(chunks[chunk_id])

        selected = select_calibration_cases(cases, size=size, seed=seed)
        calibration = build_calibration_cases(selected, passage_text=passage_text)
        manifest = {
            "protocol": CALIBRATION_PROTOCOL,
            "run_directory": run_directory,
            "run_protocol": report.get("protocol"),
            "dataset_sha256": assets.dataset_sha256,
            "judge_prompt_version": report["identity"].get("judge_prompt_version"),
            "seed": seed,
            "requested": size,
            "selected": len(calibration),
            "by_stratum_and_verdict": _cell_counts(calibration),
            "cases": [case.to_dict() for case in calibration],
        }
        self._artifacts.publish(output_directory, {
            "calibration.json": json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
            "calibration.md": render_calibration_sheet(calibration, run_directory=run_directory).encode("utf-8"),
            "labels.template.csv": render_label_template(calibration).encode("utf-8"),
        })
        return {key: value for key, value in manifest.items() if key != "cases"}

    def score(self, *, calibration_directory: str) -> dict[str, Any]:
        manifest = json.loads(self._artifacts.read(calibration_directory, "calibration.json"))
        labels = self._artifacts.read(calibration_directory, LABELS_FILE).decode("utf-8-sig")
        return {
            "run_directory": manifest["run_directory"],
            "judge_prompt_version": manifest["judge_prompt_version"],
            **score_calibration(manifest["cases"], labels),
        }


def _cell_counts(cases) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        key = f"{case.stratum}:{'judge_correct' if case.judge_correct else 'judge_incorrect'}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


__all__ = ["LABELS_FILE", "QasperAnswerCalibrationService"]
