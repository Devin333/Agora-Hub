"""Resumable progress for a QASPER answer run.

A run appends one line per finished case to a checkpoint beside its output. The
first line binds the checkpoint to the exact measurement (identity, sample and
case selection); a resumed run must reproduce that binding, so cases measured
under one setup are never mixed into a report for another.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from backend.research.ports.qasper_evaluation import QasperRunCheckpointStore
from backend.research.rag.evaluation.qasper_answer_report import QasperAnswerCase

CHECKPOINT_PROTOCOL = "qasper_answer_checkpoint_v1"
# Filled in from what the run observes, not part of the setup being measured.
_RUN_OBSERVED_IDENTITY_FIELDS = frozenset({"reranker_activation", "answer_models"})


class QasperAnswerCheckpointError(ValueError):
    """The checkpoint does not belong to this run setup or is corrupt."""


class QasperAnswerCheckpoint:
    """Checkpoint of one output directory, bound to one measurement."""

    def __init__(
        self,
        store: QasperRunCheckpointStore,
        directory: str,
        *,
        identity: Mapping[str, Any],
        sample: Mapping[str, Any],
        case_ids: Sequence[str],
    ) -> None:
        self._store = store
        self._directory = directory
        self._header = _canonical({
            "kind": "header",
            "protocol": CHECKPOINT_PROTOCOL,
            "identity": {
                key: value for key, value in identity.items()
                if key not in _RUN_OBSERVED_IDENTITY_FIELDS
            },
            "sample": dict(sample),
            "case_ids": list(case_ids),
        })
        self._case_ids = frozenset(case_ids)

    def resume(self) -> dict[str, QasperAnswerCase]:
        """Return the cases already finished, starting a new checkpoint if there is none."""
        lines = self._store.read_lines(self._directory)
        if not lines:
            self._store.append_line(self._directory, _encode(self._header))
            return {}
        if _decode(lines[0], "header") != self._header:
            raise QasperAnswerCheckpointError(
                "checkpoint was written by a different run setup; "
                "use a new output directory or remove the checkpoint"
            )
        finished: dict[str, QasperAnswerCase] = {}
        for index, line in enumerate(lines[1:], start=2):
            row = _decode(line, f"line {index}")
            if row.get("kind") != "case" or not isinstance(row.get("case"), Mapping):
                raise QasperAnswerCheckpointError(f"checkpoint line {index} is not a case record")
            try:
                case = QasperAnswerCase.from_dict(row["case"])
            except (TypeError, ValueError) as exc:
                raise QasperAnswerCheckpointError(f"checkpoint line {index} holds an invalid case") from exc
            if case.case_id not in self._case_ids or case.case_id in finished:
                raise QasperAnswerCheckpointError(
                    f"checkpoint line {index} repeats or falls outside the case selection"
                )
            finished[case.case_id] = case
        return finished

    def record(self, case: QasperAnswerCase) -> None:
        self._store.append_line(self._directory, _encode({"kind": "case", "case": case.to_dict()}))

    def discard(self) -> None:
        self._store.discard(self._directory)


def _canonical(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode(line: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QasperAnswerCheckpointError(f"checkpoint {label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise QasperAnswerCheckpointError(f"checkpoint {label} must be an object")
    return value


__all__ = ["CHECKPOINT_PROTOCOL", "QasperAnswerCheckpoint", "QasperAnswerCheckpointError"]
