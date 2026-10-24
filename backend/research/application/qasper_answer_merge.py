"""Strict failed-generation recovery for QASPER answer runs."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from backend.research.ports.qasper_evaluation import QasperArtifactStore
from backend.research.rag.evaluation.qasper_answer_report import QasperAnswerCase, QasperAnswerSummary

MERGE_PROTOCOL = "qasper_end_to_end_answer_merge_v2"
_REQUIRED: dict[str, type] = {
    "dataset_sha256": str,
    "mapping_digest": str,
    "embedding_cache_sha256": str,
    "embedding_model": str,
    "embedding_dimension": int,
    "retrieval_policy_settings": dict,
    "retrieval_profile_fingerprint": str,
    "context_budget": dict,
    "context_runtime": dict,
    "answer_prompt_version": str,
    "answer_protocol_version": str,
    "answer_model_identity": dict,
}
_NON_MEASUREMENT_IDENTITY_FIELDS = {"code", "reranker_activation", "answer_models"}
_REFERENCE_FIELDS = ("paper_id", "question", "stratum", "references", "reference_types", "mapping_disposition")


class QasperAnswerMergeError(ValueError):
    """The runs cannot be merged without mixing measurements or provenance."""


@dataclass(frozen=True)
class _Run:
    directory: str
    report_bytes: bytes
    answers_bytes: bytes
    sample_bytes: bytes
    report: dict[str, Any]
    sample: dict[str, Any]
    raw: tuple[dict[str, Any], ...]
    cases: tuple[QasperAnswerCase, ...]
    line_hashes: tuple[str, ...]
    sha256: dict[str, str]


def prepare_failed_answer_recovery(artifacts: QasperArtifactStore, directory: str) -> dict[str, Any]:
    run = _read_run(artifacts, directory)
    _require_complete(run)
    return _prepare(run)


def require_same_measurement(source_identity: Mapping[str, Any], current_identity: Mapping[str, Any]) -> None:
    source = _identity(source_identity, "source identity")
    current = _identity(current_identity, "current identity")
    differing: list[str] = []
    for field in _REQUIRED:
        if source[field] != current[field]:
            differing.append(field)
    optional_fields = (
        set(source) | set(current)
    ) - set(_REQUIRED) - _NON_MEASUREMENT_IDENTITY_FIELDS
    for field in sorted(optional_fields):
        sh = field in source and source[field] is not None
        ch = field in current and current[field] is not None
        if sh != ch:
            differing.append(f"{field} (one-sided missing)")
        elif sh and source[field] != current[field]:
            differing.append(field)
    if differing:
        raise QasperAnswerMergeError("runs measure different setups: " + ", ".join(sorted(differing)))


def merge_answer_runs(
    artifacts: QasperArtifactStore, *, base_directory: str, rerun_directory: str, output_directory: str,
) -> dict[str, Any]:
    if output_directory in {base_directory, rerun_directory}:
        raise QasperAnswerMergeError("output directory must differ from parent directories")
    base = _read_run(artifacts, base_directory)
    rerun = _read_run(artifacts, rerun_directory)
    _require_complete(base)
    if base.report["protocol"] != rerun.report["protocol"]:
        raise QasperAnswerMergeError("runs use different evaluation protocols")
    require_same_measurement(base.report["identity"], rerun.report["identity"])
    if base.sample != rerun.sample:
        raise QasperAnswerMergeError("recovery sample does not exactly match source sample")
    prepared = _prepare(base)
    if rerun.report.get("recovery_source") != prepared:
        raise QasperAnswerMergeError("recovery_source does not match named parent artifacts")
    selected = prepared["case_ids"]
    sel = rerun.report.get("case_selection")
    if not isinstance(sel, Mapping) or sel.get("mode") != "case_ids" or sel.get("case_ids") != selected:
        raise QasperAnswerMergeError("recovery must contain every source generation failure in sample order")
    base_ids = [c.case_id for c in base.cases]
    rerun_ids = [c.case_id for c in rerun.cases]
    if rerun_ids != selected:
        raise QasperAnswerMergeError("recovery answers do not exactly match selected generation failures")
    base_by_id = {c.case_id: i for i, c in enumerate(base.cases)}
    rerun_by_id = {c.case_id: i for i, c in enumerate(rerun.cases)}
    for case_id in selected:
        _same_reference(base.raw[base_by_id[case_id]], rerun.raw[rerun_by_id[case_id]], case_id)
    merged_raw: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    for index, case in enumerate(base.cases):
        ri = rerun_by_id.get(case.case_id)
        if ri is None:
            merged_raw.append(copy.deepcopy(base.raw[index]))
            lineage.append({"case_id": case.case_id, "selected_source": "base", "base_record_sha256": base.line_hashes[index]})
        else:
            merged_raw.append(copy.deepcopy(rerun.raw[ri]))
            lineage.append({"case_id": case.case_id, "selected_source": "rerun", "base_record_sha256": base.line_hashes[index], "rerun_record_sha256": rerun.line_hashes[ri]})
    merged = tuple(_decode_case(row, "merged answer") for row in merged_raw)
    metrics = QasperAnswerSummary(merged).to_dict()
    base_eff = _efficiency(base, "base")
    rerun_eff = _efficiency(rerun, "rerun")
    cost = {
        "scope": "all recorded base and recovery attempts",
        "selected_record_efficiency": copy.deepcopy(metrics["efficiency"]),
        "parent_efficiency": {"base": copy.deepcopy(base_eff), "rerun": copy.deepcopy(rerun_eff)},
        "cumulative_attempt_tokens": _sum_token_maps(
            _attempt_tokens(base, base_eff, "base"),
            _attempt_tokens(rerun, rerun_eff, "rerun"),
        ),
        "note": "selected-record metrics exclude superseded attempts; cumulative tokens include recorded base and recovery attempts",
    }
    report = copy.deepcopy(base.report)
    merged_observed_models = sorted({case.model for case in merged if case.model})
    report.update({
        "merge_protocol": MERGE_PROTOCOL,
        "identity": {
            **copy.deepcopy(base.report["identity"]),
            "answer_models": merged_observed_models,
            "code": {
                "kind": "answer_failure_recovery",
                "base": copy.deepcopy(base.report["identity"]["code"]),
                "rerun": copy.deepcopy(rerun.report["identity"]["code"]),
            },
        },
        "sample": copy.deepcopy(base.sample),
        "evaluated_cases": len(merged),
        "case_selection": {"mode": "full_sample"},
        "recovery_source": {**copy.deepcopy(prepared), "rerun": _parent(rerun)},
        "merge": {"base_run": base_directory, "rerun_run": rerun_directory, "replaced_cases": len(selected), "replaced_case_ids": list(selected), "still_generation_failures": [c.case_id for c in merged if c.needs_generation_rerun], "case_lineage": lineage, "cost_accounting": cost},
        "cost_accounting": cost,
        "metrics": metrics,
    })
    artifacts.publish(output_directory, {"sample.json": base.sample_bytes, "answers.jsonl": b"".join(_json(row) + b"\n" for row in merged_raw), "report.json": _json(report, pretty=True)})
    return report


def _read_run(store: QasperArtifactStore, directory: str) -> _Run:
    report_bytes = _read(store, directory, "report.json")
    answers_bytes = _read(store, directory, "answers.jsonl")
    sample_bytes = _read(store, directory, "sample.json")
    report = _obj(_decode(report_bytes, f"{directory}/report.json"), "report")
    sample = _obj(_decode(sample_bytes, f"{directory}/sample.json"), "sample")
    raw, hashes = _answers(answers_bytes, directory)
    cases = tuple(_decode_case(row, f"{directory}/answers.jsonl") for row in raw)
    run = _Run(directory, report_bytes, answers_bytes, sample_bytes, report, sample, raw, cases, hashes, {"report": _sha(report_bytes), "answers": _sha(answers_bytes), "sample": _sha(sample_bytes)})
    _validate(run)
    return run


def _read(store: QasperArtifactStore, directory: str, name: str) -> bytes:
    try:
        data = store.read(directory, name)
    except Exception as exc:
        raise QasperAnswerMergeError(f"unable to read required artifact {directory}/{name}") from exc
    if not isinstance(data, bytes):
        raise QasperAnswerMergeError(f"artifact {directory}/{name} must be bytes")
    return data


def _decode(data: bytes, label: str) -> Any:
    try:
        text = data.decode("utf-8")
        return json.loads(text, object_pairs_hook=lambda pairs: _unique(pairs, label), parse_constant=lambda value: _bad_number(label, value))
    except QasperAnswerMergeError:
        raise
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise QasperAnswerMergeError(f"{label} is not valid JSON") from exc


def _unique(pairs: Sequence[tuple[str, Any]], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QasperAnswerMergeError(f"{label} contains duplicate JSON key {key!r}")
        result[key] = value
    return result


def _bad_number(label: str, value: str) -> None:
    raise QasperAnswerMergeError(f"{label} contains invalid JSON number {value}")


def _answers(data: bytes, directory: str) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QasperAnswerMergeError(f"{directory}/answers.jsonl is not valid UTF-8") from exc
    rows: list[dict[str, Any]] = []
    hashes: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        label = f"{directory}/answers.jsonl line {number}"
        row = _obj(_decode(line.encode("utf-8"), label), label)
        rows.append(row)
        hashes.append(_sha(line.encode("utf-8")))
    return tuple(rows), tuple(hashes)


def _decode_case(row: Mapping[str, Any], label: str) -> QasperAnswerCase:
    try:
        return QasperAnswerCase.from_dict(row)
    except (TypeError, ValueError, KeyError) as exc:
        raise QasperAnswerMergeError(f"invalid {label}: {exc}") from exc


def _validate(run: _Run) -> None:
    if not isinstance(run.report.get("protocol"), str) or not run.report["protocol"]:
        raise QasperAnswerMergeError(f"{run.directory} report.protocol must be a non-empty string")
    _identity(run.report.get("identity"), f"{run.directory} identity")
    if run.report.get("sample") != run.sample:
        raise QasperAnswerMergeError(f"{run.directory} report.sample does not match sample.json")
    sample_ids = _sample_ids(run.sample, run.directory)
    ids = [c.case_id for c in run.cases]
    _unique_ids(ids, f"{run.directory} answers")
    if any(case_id not in set(sample_ids) for case_id in ids):
        raise QasperAnswerMergeError(f"{run.directory} answers contain unknown case ID")
    evaluated = run.report.get("evaluated_cases")
    if isinstance(evaluated, bool) or not isinstance(evaluated, int) or evaluated != len(ids):
        raise QasperAnswerMergeError(f"{run.directory} evaluated_cases does not match answers")
    selection = run.report.get("case_selection")
    if not isinstance(selection, Mapping):
        raise QasperAnswerMergeError(f"{run.directory} case_selection must be an object")
    mode = selection.get("mode")
    if mode == "full_sample":
        expected = sample_ids
    elif mode == "case_ids":
        expected = _strings(selection.get("case_ids"), "case_selection.case_ids")
        _unique_ids(expected, f"{run.directory} selection")
        if expected != [case_id for case_id in sample_ids if case_id in set(expected)]:
            raise QasperAnswerMergeError(f"{run.directory} case selection is not in sample order")
    else:
        raise QasperAnswerMergeError(f"{run.directory} case_selection mode is invalid")
    if ids != expected:
        raise QasperAnswerMergeError(f"{run.directory} answers order/count does not match case_selection")
    _validate_recorded_models(run)


def _require_complete(run: _Run) -> None:
    if run.report["case_selection"].get("mode") != "full_sample":
        raise QasperAnswerMergeError("answer recovery source must be a complete full-sample run")
    _efficiency(run, "source")


def _prepare(run: _Run) -> dict[str, Any]:
    return {"case_ids": [c.case_id for c in run.cases if c.needs_generation_rerun], "seed": run.sample["seed"], "per_type": run.sample["per_type"], "identity": copy.deepcopy(run.report["identity"]), "parent": _parent(run)}


def _parent(run: _Run) -> dict[str, Any]:
    return {"directory": run.directory, "sha256": dict(run.sha256), "code": copy.deepcopy(run.report["identity"]["code"])}


def _same_reference(base: Mapping[str, Any], current: Mapping[str, Any], case_id: str) -> None:
    differing = [field for field in _REFERENCE_FIELDS if base.get(field) != current.get(field)]
    if differing:
        raise QasperAnswerMergeError(f"recovery case {case_id} changed question/reference identity: {', '.join(differing)}")


def _identity(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise QasperAnswerMergeError(f"{label} must be an object")
    identity = dict(value)
    for field, typ in _REQUIRED.items():
        if field not in identity:
            raise QasperAnswerMergeError(f"{label} is missing required field {field}")
        val = identity[field]
        valid = (
            _known_string(val)
            if typ is str
            else (isinstance(val, int) and not isinstance(val, bool) and val > 0)
            if typ is int
            else (isinstance(val, Mapping) and bool(val))
        )
        if not valid:
            raise QasperAnswerMergeError(f"{label}.{field} has invalid type or value")
    model_identity = identity["answer_model_identity"]
    assert isinstance(model_identity, Mapping)
    if not _known_string(model_identity.get("provider")) or not _known_string(model_identity.get("model")):
        raise QasperAnswerMergeError(
            f"{label}.answer_model_identity requires known provider and model"
        )
    max_tokens = model_identity.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise QasperAnswerMergeError(
            f"{label}.answer_model_identity requires positive max_tokens"
        )
    if "temperature" not in model_identity:
        raise QasperAnswerMergeError(
            f"{label}.answer_model_identity requires explicit temperature"
        )
    temperature = model_identity["temperature"]
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(temperature)
    ):
        raise QasperAnswerMergeError(
            f"{label}.answer_model_identity.temperature must be finite or null"
        )
    if not _known_string(model_identity.get("endpoint_sha256")):
        raise QasperAnswerMergeError(
            f"{label}.answer_model_identity requires endpoint_sha256"
        )
    if not isinstance(identity.get("code"), Mapping) or not identity["code"]:
        raise QasperAnswerMergeError(f"{label}.code must be an object")
    return identity


def _validate_recorded_models(run: _Run) -> None:
    identity = run.report["identity"]
    configured = identity["answer_model_identity"]["model"]
    observed = sorted({case.model for case in run.cases if case.model})
    reported = identity.get("answer_models")
    if reported is not None:
        if (
            not isinstance(reported, list)
            or any(not _known_string(item) for item in reported)
            or reported != observed
        ):
            raise QasperAnswerMergeError(
                f"{run.directory} identity.answer_models does not match observed case models"
            )
    mismatched = [case.case_id for case in run.cases if case.model and case.model != configured]
    if mismatched:
        raise QasperAnswerMergeError(
            f"{run.directory} case.model differs from answer_model_identity.model: {mismatched!r}"
        )
    missing = [
        case.case_id
        for case in run.cases
        if not case.model
        and (
            case.outcome.value == "answered"
            or case.generation_ms is not None
            or case.model_usage is not None
        )
    ]
    if missing:
        raise QasperAnswerMergeError(
            f"{run.directory} generated cases are missing recorded model identity: {missing!r}"
        )


def _known_string(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value.strip().lower() not in {"unknown", "unset", "none", "n/a"}
    )


def _sample_ids(sample: Mapping[str, Any], directory: str) -> list[str]:
    ids = _strings(sample.get("case_ids"), f"{directory} sample.case_ids")
    if not ids:
        raise QasperAnswerMergeError(f"{directory} sample.case_ids must not be empty")
    _unique_ids(ids, f"{directory} sample")
    if not isinstance(sample.get("seed"), str) or not sample["seed"]:
        raise QasperAnswerMergeError(f"{directory} sample.seed is invalid")
    if isinstance(sample.get("per_type"), bool) or not isinstance(sample.get("per_type"), int) or sample["per_type"] <= 0:
        raise QasperAnswerMergeError(f"{directory} sample.per_type is invalid")
    return ids


def _efficiency(run: _Run, label: str) -> dict[str, Any]:
    metrics = run.report.get("metrics")
    value = metrics.get("efficiency") if isinstance(metrics, Mapping) else None
    if not isinstance(value, Mapping):
        raise QasperAnswerMergeError(f"{label} report.metrics.efficiency must be an object")
    return dict(value)


def _attempt_tokens(run: _Run, efficiency: Mapping[str, Any], label: str) -> dict[str, int]:
    prior = run.report.get("cost_accounting")
    if isinstance(prior, Mapping) and "cumulative_attempt_tokens" in prior:
        return _token_map(prior["cumulative_attempt_tokens"], f"{label} cumulative_attempt_tokens")
    return _token_map(efficiency.get("tokens"), f"{label} efficiency.tokens")


def _sum_token_maps(*token_maps: Mapping[str, int]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for tokens in token_maps:
        for key, val in tokens.items():
            totals[key] = totals.get(key, 0) + val
    return dict(sorted(totals.items()))


def _token_map(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise QasperAnswerMergeError(f"{label} must be an object")
    result: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise QasperAnswerMergeError(f"{label} is invalid")
        result[key] = item
    return result


def _obj(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QasperAnswerMergeError(f"{label} must be an object")
    return value


def _strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise QasperAnswerMergeError(f"{label} must be an array of non-empty strings")
    return list(value)


def _unique_ids(values: Sequence[str], label: str) -> None:
    if len(set(values)) != len(values):
        raise QasperAnswerMergeError(f"{label} contain duplicate case IDs")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {"ensure_ascii": False, "sort_keys": True}
    if pretty:
        options["indent"] = 2
    return json.dumps(value, **options).encode("utf-8")


__all__ = ["MERGE_PROTOCOL", "QasperAnswerMergeError", "merge_answer_runs", "prepare_failed_answer_recovery", "require_same_measurement"]
