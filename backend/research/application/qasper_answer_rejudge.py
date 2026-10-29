"""Recover failed QASPER judges without changing frozen answers or retrieval.

The service reads a completed answer run once, verifies every selected judge request
against its immutable case record, invokes the judge at most once per selected case,
and publishes a complete child run with explicit parent hashes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from backend.research.application.qasper_assets import QasperAssetService
from backend.research.ports.qasper_evaluation import QasperArtifactStore
from backend.research.rag.evaluation.qasper_answer_judge import (
    JUDGE_PROMPT_VERSION,
    JudgeCall,
    JudgeRequest,
    judge_answer,
)
from backend.research.rag.evaluation.qasper_answer_metrics import references_for_question
from backend.research.rag.evaluation.qasper_answer_report import QasperAnswerCase, QasperAnswerSummary
from backend.research.rag.evaluation.qasper_mapping import freeze_qasper_dataset
from backend.research.rag.retrieval.paper_answer_generator import render_answer_context
from framework.rag.evaluation.answer_metrics import AnswerOutcome
from framework.rag.generation import partition_citation_markers

REJUDGE_PROTOCOL = "qasper_answer_rejudge_v1"


class QasperAnswerRejudgeError(ValueError):
    """The source run is unsafe to rejudge without changing its measurement."""


@dataclass(frozen=True)
class QasperAnswerRejudgeConfig:
    source_directory: str
    output_directory: str
    include_missing_judge: bool = False

    def __post_init__(self) -> None:
        if not self.source_directory.strip():
            raise ValueError("source_directory is required")
        if not self.output_directory.strip():
            raise ValueError("output_directory is required")
        if self.source_directory == self.output_directory:
            raise ValueError("output_directory must differ from source_directory")
        if not isinstance(self.include_missing_judge, bool):
            raise TypeError("include_missing_judge must be a boolean")


@dataclass(frozen=True)
class QasperAnswerRejudgeProgress:
    """Progress callback payload, emitted immediately before and after each judge call."""

    phase: Literal["before", "after"]
    index: int
    total: int
    case_id: str
    recovered: bool | None


ProgressCallback = Callable[[QasperAnswerRejudgeProgress], None]


class QasperAnswerRejudgeService:
    def __init__(
        self,
        artifacts: QasperArtifactStore,
        *,
        judge_call: JudgeCall,
        config: QasperAnswerRejudgeConfig,
        assets: QasperAssetService | None = None,
        judge_model_identity: Mapping[str, Any] | str | None = None,
        legacy_judge_model_identity: Mapping[str, Any] | str | None = None,
        code_identity: Mapping[str, Any] | str | None = None,
        progress: ProgressCallback | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._artifacts = artifacts
        self._judge_call = judge_call
        self._config = config
        self._assets = assets
        self._judge_model_identity = _model_identity(judge_model_identity)
        self._legacy_judge_model_identity = _optional_model_identity(legacy_judge_model_identity)
        self._code_identity = _code_identity(code_identity)
        self._progress = progress
        self._clock = clock

    def run(self) -> dict[str, Any]:
        source = self._read_source_once()
        report = _json_object(source["report.json"], name="report.json")
        sample = _json_object(source["sample.json"], name="sample.json")
        records = _decode_cases(source["answers.jsonl"])
        cases = [record.case for record in records]
        _validate_source(report, sample, records)

        selected = [record for record in records if self._selected(record.case)]
        legacy_context = (
            self._legacy_context(report)
            if any(record.case.judge_request is None for record in selected)
            else None
        )
        _validate_judge_identity(
            report, current=self._judge_model_identity, legacy=legacy_context, selected=bool(selected),
        )
        requests = {
            record.case.case_id: self._request_for(record.case, legacy_context)
            for record in selected
        }
        updated_cases: list[QasperAnswerCase] = []
        output_rows: list[dict[str, Any]] = []
        recovered_ids: list[str] = []
        still_failed_ids: list[str] = []
        selected_ids = {record.case.case_id for record in selected}
        selected_index = 0
        judge_elapsed_ms = 0.0

        for source_record in records:
            case = source_record.case
            if case.case_id not in selected_ids:
                updated_cases.append(case)
                output_rows.append(dict(source_record.raw))
                continue
            selected_index += 1
            request = requests[case.case_id]
            if self._progress is not None:
                self._progress(QasperAnswerRejudgeProgress(
                    phase="before", index=selected_index, total=len(selected),
                    case_id=case.case_id, recovered=None,
                ))
            started = self._clock()
            verdict = asyncio.run(judge_answer(self._judge_call, request))
            elapsed_ms = max((self._clock() - started) * 1000, 0.001)
            judge_elapsed_ms += elapsed_ms
            recovered = not verdict.judge_error
            if recovered:
                recovered_ids.append(case.case_id)
            else:
                still_failed_ids.append(case.case_id)
            result = replace(
                case,
                judge=verdict,
                judge_request=request,
                rejudge_attempt={
                    "protocol": REJUDGE_PROTOCOL,
                    "attempt": 1,
                    "request_digest": request.digest(),
                    "source_judge_error": case.judge.error if case.judge is not None else "missing",
                    "judge_model_identity": self._judge_model_identity,
                    "recovered": recovered,
                    "judge_elapsed_ms": elapsed_ms,
                    "judge_usage": "unknown",
                },
            )
            updated_cases.append(result)
            output_row = dict(source_record.raw)
            output_row.update({
                "judge": verdict.to_dict(),
                "judge_request": request.to_dict(),
                "rejudge_attempt": result.rejudge_attempt,
                "end_to_end_correct": result.end_to_end_correct(),
            })
            output_rows.append(output_row)
            if self._progress is not None:
                self._progress(QasperAnswerRejudgeProgress(
                    phase="after", index=selected_index, total=len(selected),
                    case_id=case.case_id, recovered=recovered,
                ))

        output_report = self._report(
            report,
            tuple(updated_cases),
            source=source,
            selected=[record.case.case_id for record in selected],
            recovered=recovered_ids,
            still_failed=still_failed_ids,
            legacy=legacy_context,
            judge_elapsed_ms=judge_elapsed_ms,
        )
        answers = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in output_rows).encode("utf-8")
        self._artifacts.publish(self._config.output_directory, {
            "sample.json": source["sample.json"],
            "answers.jsonl": answers,
            "report.json": _json_bytes(output_report),
        })
        return output_report

    def _read_source_once(self) -> dict[str, bytes]:
        return {
            name: self._artifacts.read(self._config.source_directory, name)
            for name in ("report.json", "answers.jsonl", "sample.json")
        }

    def _selected(self, case: QasperAnswerCase) -> bool:
        if case.outcome is not AnswerOutcome.ANSWERED:
            return False
        if case.judge is not None and case.judge.judge_error:
            return True
        return self._config.include_missing_judge and case.judge is None

    def _legacy_context(self, report: Mapping[str, Any]) -> "_LegacyContext":
        if self._assets is None:
            raise QasperAnswerRejudgeError(
                "legacy source cases lack judge_request snapshots; QasperAssetService is required"
            )
        loaded = self._assets.read_assets()
        frozen = freeze_qasper_dataset(loaded.raw_dataset, loaded.documents, loaded.chunks)
        identity = _mapping(report.get("identity"), "report.identity")
        _require_identity(identity, "dataset_sha256", loaded.dataset_sha256)
        _require_identity(identity, "mapping_digest", frozen.digest)
        cache_fields = ("embedding_cache_sha256", "embedding_model", "embedding_dimension")
        missing_identity = [field for field in cache_fields if field not in identity]
        if missing_identity:
            raise QasperAnswerRejudgeError(
                "legacy source lacks required cache identity: " + ", ".join(missing_identity)
            )
        fingerprint = identity.get("retrieval_profile_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise QasperAnswerRejudgeError("legacy source lacks retrieval_profile_fingerprint")
        context_runtime = identity.get("context_runtime")
        if not isinstance(context_runtime, Mapping) or not context_runtime:
            raise QasperAnswerRejudgeError("legacy source lacks context_runtime identity")
        cache = self._assets.cache_identity()
        _require_identity(identity, "embedding_cache_sha256", cache.sha256)
        _require_identity(identity, "embedding_model", cache.model_id)
        _require_identity(identity, "embedding_dimension", cache.dimension)
        historical_verified = [
            "dataset_sha256", "mapping_digest", "embedding_cache_sha256",
            "embedding_model", "embedding_dimension",
        ]
        historical_unavailable: list[str] = []
        for field, actual in (
            ("corpus_sha256", frozen.corpus_sha256),
            ("index_sha256", frozen.index_sha256),
        ):
            if field in identity:
                _require_identity(identity, field, actual)
                historical_verified.append(field)
            else:
                historical_unavailable.append(field)
        recorded_judge_model = identity.get("judge_model_identity")
        if _is_complete_judge_identity(recorded_judge_model):
            source_judge_model = dict(recorded_judge_model)
            declared_model = (
                _identity_model(self._legacy_judge_model_identity)
                if self._legacy_judge_model_identity is not None
                else None
            )
            if declared_model is not None and declared_model != _identity_model(source_judge_model):
                raise QasperAnswerRejudgeError(
                    "explicit legacy judge model identity conflicts with the source report"
                )
            legacy_comparison_scope = "full_configuration"
            legacy_unverified_fields: list[str] = []
        else:
            if self._legacy_judge_model_identity is None:
                raise QasperAnswerRejudgeError(
                    "legacy source lacks a complete judge model identity; "
                    "an explicit legacy declaration is required"
                )
            source_judge_model = self._legacy_judge_model_identity
            recorded_model = _identity_model(recorded_judge_model)
            declared_model = _identity_model(source_judge_model)
            if recorded_model is not None and recorded_model != declared_model:
                raise QasperAnswerRejudgeError(
                    "explicit legacy judge model identity conflicts with the source report"
                )
            legacy_comparison_scope = "model_only"
            legacy_unverified_fields = [
                "provider", "max_tokens", "temperature", "endpoint_sha256",
            ]

        qas: dict[str, Mapping[str, Any]] = {}
        papers = loaded.raw_dataset.get("papers", loaded.raw_dataset)
        for paper_id, paper in papers.items():
            for position, qa in enumerate(paper.get("qas") or ()):
                question_id = str(qa.get("question_id") or qa.get("id") or position)
                case_id = f"{paper_id}:{question_id}"
                if case_id in qas:
                    raise QasperAnswerRejudgeError(f"duplicate dataset case id: {case_id}")
                qas[case_id] = qa
        frozen_cases = {case.case_id: case for case in frozen.cases}
        if len(frozen_cases) != len(frozen.cases):
            raise QasperAnswerRejudgeError("frozen mapping contains duplicate case ids")
        chunks = {chunk.chunk_id: chunk for chunk in loaded.chunks}
        if len(chunks) != len(loaded.chunks):
            raise QasperAnswerRejudgeError("frozen assets contain duplicate chunk ids")
        return _LegacyContext(
            qas=qas,
            frozen_cases=frozen_cases,
            chunks=chunks,
            source_judge_model_identity=source_judge_model,
            reconstruction_manifest={
                "current_assets": {
                    "dataset_sha256": loaded.dataset_sha256,
                    "mapping_digest": frozen.digest,
                    "corpus_sha256": frozen.corpus_sha256,
                    "index_sha256": frozen.index_sha256,
                    "embedding_cache_sha256": cache.sha256,
                    "embedding_model": cache.model_id,
                    "embedding_dimension": cache.dimension,
                },
                "historical_identity_verified": historical_verified,
                "historical_identity_recorded_only": [
                    "retrieval_profile_fingerprint", "context_runtime",
                ],
                "historical_identity_unavailable": historical_unavailable,
                "legacy_judge_identity": {
                    "declared": source_judge_model,
                    "comparison_scope": legacy_comparison_scope,
                    "unverified_fields": legacy_unverified_fields,
                },
            },
        )

    def _request_for(self, case: QasperAnswerCase, legacy: "_LegacyContext | None") -> JudgeRequest:
        _validate_case_citations(case)
        if case.judge_request is not None:
            request = case.judge_request
        else:
            if legacy is None:
                raise QasperAnswerRejudgeError(f"missing legacy reconstruction context for {case.case_id}")
            qa = legacy.qas.get(case.case_id)
            frozen = legacy.frozen_cases.get(case.case_id)
            if qa is None or frozen is None:
                raise QasperAnswerRejudgeError(f"case {case.case_id} is absent from frozen QASPER assets")
            references = tuple(item.answer for item in references_for_question(qa))
            if frozen.paper_id != case.paper_id or frozen.query != case.question.strip():
                raise QasperAnswerRejudgeError(f"case {case.case_id} does not match its frozen mapping")
            if str(qa.get("question") or "") != case.question or references != case.references:
                raise QasperAnswerRejudgeError(f"case {case.case_id} does not match its frozen dataset")
            passages: list[tuple[int, str]] = []
            markers = partition_citation_markers(case.answer, context_count=len(case.context_chunk_ids))
            for index in markers.valid_indexes:
                chunk_id = case.context_chunk_ids[index - 1]
                chunk = legacy.chunks.get(chunk_id)
                if chunk is None:
                    raise QasperAnswerRejudgeError(
                        f"case {case.case_id} cites missing frozen chunk {chunk_id}"
                    )
                if chunk.paper_id != case.paper_id:
                    raise QasperAnswerRejudgeError(
                        f"case {case.case_id} cites a chunk from another paper"
                    )
                passages.append((index, render_answer_context(chunk)))
            request = JudgeRequest(
                question=case.question,
                references=case.references,
                answer=case.answer,
                cited_passages=passages,
            )
        _validate_request(case, request)
        return request

    def _report(
        self,
        source_report: Mapping[str, Any],
        cases: tuple[QasperAnswerCase, ...],
        *,
        source: Mapping[str, bytes],
        selected: list[str],
        recovered: list[str],
        still_failed: list[str],
        legacy: "_LegacyContext | None",
        judge_elapsed_ms: float,
    ) -> dict[str, Any]:
        report = dict(source_report)
        identity = dict(_mapping(source_report.get("identity"), "report.identity"))
        source_model = (
            legacy.source_judge_model_identity
            if legacy is not None
            else identity.get("judge_model_identity", "unknown")
        )
        identity["judge_models"] = {
            "source": source_model if source_model not in (None, "") else "unknown",
            "rejudge": self._judge_model_identity,
        }
        report["identity"] = identity
        metrics = dict(_mapping(source_report.get("metrics"), "report.metrics"))
        has_source_efficiency = "efficiency" in metrics
        source_efficiency = metrics.get("efficiency")
        metrics.update(QasperAnswerSummary(cases).to_dict())
        if has_source_efficiency:
            metrics["efficiency"] = source_efficiency
        report["metrics"] = metrics
        report["rejudge_protocol"] = REJUDGE_PROTOCOL
        report["rejudge"] = {
            "source_directory": self._config.source_directory,
            "parents": {
                name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
                for name, content in source.items()
            },
            "judge_prompt_version": JUDGE_PROMPT_VERSION,
            "judge_model_identity": self._judge_model_identity,
            "code_identity": self._code_identity,
            "include_missing_judge": self._config.include_missing_judge,
            "selected": len(selected),
            "selected_case_ids": selected,
            "recovered": len(recovered),
            "recovered_case_ids": recovered,
            "still_failed": len(still_failed),
            "still_failed_case_ids": still_failed,
            "attempts": len(selected),
            "judge_calls": {
                "count": len(selected),
                "elapsed_ms": judge_elapsed_ms,
                "usage": "unknown",
            },
            **(
                {"legacy_reconstruction": legacy.reconstruction_manifest}
                if legacy is not None
                else {}
            ),
        }
        return report


@dataclass(frozen=True)
class _LegacyContext:
    qas: Mapping[str, Mapping[str, Any]]
    frozen_cases: Mapping[str, Any]
    chunks: Mapping[str, Any]
    source_judge_model_identity: Mapping[str, Any] | str
    reconstruction_manifest: Mapping[str, Any]


@dataclass(frozen=True)
class _SourceCase:
    raw: Mapping[str, Any]
    case: QasperAnswerCase


def _validate_source(
    report: Mapping[str, Any], sample: Mapping[str, Any], records: list[_SourceCase],
) -> None:
    if report.get("protocol") != "qasper_end_to_end_answer_v1":
        raise QasperAnswerRejudgeError("source report uses an unsupported evaluation protocol")
    identity = _mapping(report.get("identity"), "report.identity")
    if identity.get("judge_prompt_version") != JUDGE_PROMPT_VERSION:
        raise QasperAnswerRejudgeError("source run uses a different or unknown judge prompt version")
    if report.get("sample") != sample:
        raise QasperAnswerRejudgeError("report.sample does not match sample.json")
    if not isinstance(report.get("metrics"), Mapping):
        raise QasperAnswerRejudgeError("report.metrics must be an object")
    case_ids = [record.case.case_id for record in records]
    if len(set(case_ids)) != len(case_ids):
        raise QasperAnswerRejudgeError("source answers contain duplicate case ids")
    sample_ids = sample.get("case_ids")
    if not isinstance(sample_ids, list) or any(not isinstance(item, str) for item in sample_ids):
        raise QasperAnswerRejudgeError("sample.case_ids must be an array of strings")
    if len(set(sample_ids)) != len(sample_ids):
        raise QasperAnswerRejudgeError("sample contains duplicate case ids")
    selection = report.get("case_selection")
    if selection is None:
        # Historical qasper_end_to_end_answer_v1 reports predate explicit selection
        # metadata. They are admissible only when answers exactly equal the full sample.
        mode = "legacy_full_sample"
        selection = {}
    elif not isinstance(selection, Mapping):
        raise QasperAnswerRejudgeError("report.case_selection must be an object")
    else:
        mode = selection.get("mode")
        if mode not in {"full_sample", "case_ids", "max_cases"}:
            raise QasperAnswerRejudgeError("report case selection mode is unsupported")
    expected_ids = sample_ids
    if mode == "case_ids":
        selected = selection.get("case_ids")
        if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
            raise QasperAnswerRejudgeError("report case selection is invalid")
        if len(set(selected)) != len(selected) or not set(selected) <= set(sample_ids):
            raise QasperAnswerRejudgeError("report case selection is not a unique sample subset")
        expected_ids = [case_id for case_id in sample_ids if case_id in set(selected)]
        if selected != expected_ids:
            raise QasperAnswerRejudgeError("report case selection is not in sample order")
    elif mode == "max_cases":
        limit = selection.get("max_cases")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise QasperAnswerRejudgeError("report max_cases selection is invalid")
        expected_ids = sample_ids[:limit]
    if case_ids != expected_ids:
        raise QasperAnswerRejudgeError("answers case ids or order do not match the source sample")
    evaluated = report.get("evaluated_cases")
    if isinstance(evaluated, bool) or not isinstance(evaluated, int) or evaluated != len(records):
        raise QasperAnswerRejudgeError("report evaluated_cases does not match answers.jsonl")


def _validate_judge_identity(
    report: Mapping[str, Any],
    *,
    current: Mapping[str, Any] | str,
    legacy: _LegacyContext | None,
    selected: bool,
) -> None:
    if not selected:
        return
    if current == "unknown":
        raise QasperAnswerRejudgeError("the current judge model identity must be explicit")
    current_model = _identity_model(current)
    if current_model is None:
        raise QasperAnswerRejudgeError("the current judge identity must include a model")
    identity = _mapping(report.get("identity"), "report.identity")
    source = identity.get("judge_model_identity")
    if _is_complete_judge_identity(source):
        _validate_full_judge_identity(current, "current judge_model_identity")
        if source != current:
            raise QasperAnswerRejudgeError("current judge identity does not match the source run")
        return
    if legacy is not None:
        source_model = _identity_model(legacy.source_judge_model_identity)
        if current_model is None or source_model is None or current_model != source_model:
            raise QasperAnswerRejudgeError(
                "current judge model does not match the explicit legacy judge model"
            )
        return
    _validate_full_judge_identity(source, "source judge_model_identity")
    _validate_full_judge_identity(current, "current judge_model_identity")
    if source != current:
        raise QasperAnswerRejudgeError("current judge identity does not match the source run")


def _validate_case_citations(case: QasperAnswerCase) -> None:
    if case.outcome is AnswerOutcome.ANSWERED and not case.answer.strip():
        raise QasperAnswerRejudgeError(f"answered case {case.case_id} has an empty answer")
    if len(set(case.context_chunk_ids)) != len(case.context_chunk_ids):
        raise QasperAnswerRejudgeError(f"case {case.case_id} has duplicate context chunk ids")
    markers = partition_citation_markers(case.answer, context_count=len(case.context_chunk_ids))
    if markers.invalid_markers:
        raise QasperAnswerRejudgeError(f"case {case.case_id} contains invalid citation markers")
    expected = tuple(case.context_chunk_ids[index - 1] for index in markers.valid_indexes)
    if expected != case.cited_chunk_ids:
        raise QasperAnswerRejudgeError(f"case {case.case_id} cited chunk ids do not match its answer markers")


def _validate_request(case: QasperAnswerCase, request: JudgeRequest) -> None:
    markers = partition_citation_markers(case.answer, context_count=len(case.context_chunk_ids))
    if request.question != case.question:
        raise QasperAnswerRejudgeError(f"case {case.case_id} judge request question mismatch")
    if tuple(request.references) != case.references:
        raise QasperAnswerRejudgeError(f"case {case.case_id} judge request references mismatch")
    if request.answer != case.answer:
        raise QasperAnswerRejudgeError(f"case {case.case_id} judge request answer mismatch")
    if tuple(index for index, _ in request.cited_passages) != markers.valid_indexes:
        raise QasperAnswerRejudgeError(f"case {case.case_id} judge request cited markers mismatch")


def _require_identity(identity: Mapping[str, Any], field: str, actual: Any) -> None:
    if identity.get(field) != actual:
        raise QasperAnswerRejudgeError(f"source {field} does not match frozen assets")


def _decode_cases(payload: bytes) -> list[_SourceCase]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QasperAnswerRejudgeError(f"answers.jsonl is not UTF-8: {exc}") from exc
    cases: list[_SourceCase] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = _strict_json(line)
            if not isinstance(raw, Mapping):
                raise ValueError("row must be an object")
            cases.append(_SourceCase(raw=dict(raw), case=QasperAnswerCase.from_dict(raw)))
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise QasperAnswerRejudgeError(
                f"invalid answers.jsonl row {line_number}: {exc}"
            ) from exc
    return cases


def _json_object(payload: bytes, *, name: str) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
        value = _strict_json(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QasperAnswerRejudgeError(f"invalid {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise QasperAnswerRejudgeError(f"{name} must contain a JSON object")
    return value


def _strict_json(text: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=object_pairs)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QasperAnswerRejudgeError(f"{name} must be an object")
    return value


def _model_identity(value: Mapping[str, Any] | str | None) -> Mapping[str, Any] | str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("judge_model_identity must be a string, mapping, or null")


def _optional_model_identity(value: Mapping[str, Any] | str | None) -> Mapping[str, Any] | str | None:
    if value is None:
        return None
    return _model_identity(value)


def _identity_model(value: Mapping[str, Any] | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value and value != "unknown" else None
    for key in ("model", "model_id"):
        model = value.get(key)
        if isinstance(model, str) and model:
            return model
    return None


def _validate_full_judge_identity(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise QasperAnswerRejudgeError(f"{label} must be a complete object")
    for field in ("provider", "model"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise QasperAnswerRejudgeError(f"{label}.{field} must be known")
    max_tokens = value.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise QasperAnswerRejudgeError(f"{label}.max_tokens must be a positive integer")
    temperature = value.get("temperature")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
    ):
        raise QasperAnswerRejudgeError(f"{label}.temperature must be finite")
    endpoint = value.get("endpoint_sha256")
    if not isinstance(endpoint, str) or re.fullmatch(r"[0-9a-f]{64}", endpoint) is None:
        raise QasperAnswerRejudgeError(f"{label}.endpoint_sha256 must be a lowercase sha256")


def _is_complete_judge_identity(value: Any) -> bool:
    required = {"provider", "model", "max_tokens", "temperature", "endpoint_sha256"}
    if not isinstance(value, Mapping) or not required <= set(value):
        return False
    _validate_full_judge_identity(value, "judge_model_identity")
    return True


def _code_identity(value: Mapping[str, Any] | str | None) -> Mapping[str, Any] | str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("code_identity must be a string, mapping, or null")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


__all__ = [
    "REJUDGE_PROTOCOL",
    "ProgressCallback",
    "QasperAnswerRejudgeConfig",
    "QasperAnswerRejudgeError",
    "QasperAnswerRejudgeProgress",
    "QasperAnswerRejudgeService",
]
