#!/usr/bin/env python3
"""Run and summarize the fixed-input Jev reranking pilot.

This module is experiment-only. It consumes a prepared, fingerprinted manifest and
never changes the production retrieval path. Run it with ``python -m`` so imports
resolve from the repository root::

    python -m scripts.jev_rerank_experiment run --arm lexical \
        --dataset manifest.json --output lexical.json
    python -m scripts.jev_rerank_experiment summarize \
        --reports lexical.json bge.json jev.json --output summary.json
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.research.rag.retrieval.paper_lightweight_reranker import (
    LightweightLexicalReranker,
)
from infrastructure.external.reranker import (
    DEFAULT_RERANKER_MODEL,
    CrossEncoderReranker,
)
from scripts.jev_rerank_dataset import (
    DatasetPreparationError,
    load_manifest,
    manifest_fingerprint,
)


ARM_SCHEMA = "agorahub.jev-rerank-arm-report/v1"
SUMMARY_SCHEMA = "agorahub.jev-rerank-summary/v1"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-1.13.0"
DEFAULT_BATCH_SIZE = 8
DEFAULT_MAX_CALLS = 100
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_LENGTH = 512
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
JEV_SCORE_CRITERIA = (
    "Unrelated to the query and does not provide useful evidence.",
    "Shares the topic or terms but does not help answer the query.",
    "Provides partial, indirect, or incomplete evidence for the query.",
    "Directly provides strong evidence needed to answer the query.",
)
EXPECTED_ARMS = ("lexical", "bge", "jev")


class ExperimentError(RuntimeError):
    """Base class for safe, user-facing experiment errors."""


class ConfigurationError(ExperimentError):
    """Raised when CLI configuration cannot produce a valid experiment."""


class ReportValidationError(ExperimentError):
    """Raised when an arm report cannot be compared safely."""


class ProviderResponseError(ExperimentError):
    """Raised when Jev returns a malformed successful response."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.observed_usage: dict[str, int] | None = None


class ProviderRequestError(ExperimentError):
    """Raised for a sanitized Jev transport or HTTP failure."""

    def __init__(self, code: str, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.fatal = fatal


class JevCaseError(ExperimentError):
    """Carries successful batch observations when a later batch fails."""

    def __init__(
        self,
        cause: Exception,
        *,
        scores: Sequence[float],
        observations: Sequence[Mapping[str, Any]],
        latency_ms: float,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.scores = list(scores)
        self.observations = [dict(item) for item in observations]
        self.latency_ms = latency_ms


@dataclass(frozen=True)
class JevBatchResult:
    scores: tuple[float, ...]
    answers: tuple[dict[str, Any], ...]
    usage: dict[str, int]
    resolved_model: str
    latency_ms: float


Clock = Callable[[], float]
Sender = Callable[[str, Mapping[str, Any], str, float], Mapping[str, Any]]


def stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a small dotenv file without mutating process environment."""

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ConfigurationError(f"Could not read env file: {path}") from exc
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(
                f"Invalid env file entry at line {line_number}: expected KEY=VALUE"
            )
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise ConfigurationError(f"Invalid env key at line {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _api_key(env_file: Path | None, environ: Mapping[str, str]) -> str:
    if env_file is not None:
        file_values = load_env_file(env_file)
        value = file_values.get("TYPESAFE_API_KEY", "").strip()
        if value:
            return value
    return str(environ.get("TYPESAFE_API_KEY", "")).strip()


def _candidate_texts(case: Mapping[str, Any]) -> list[str]:
    return [str(candidate["text"]) for candidate in case["candidates"]]


def _validate_local_scores(scores: Sequence[Any], expected: int) -> list[float]:
    if len(scores) != expected:
        raise ExperimentError(
            f"Reranker returned {len(scores)} scores for {expected} candidates"
        )
    validated: list[float] = []
    for score in scores:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ExperimentError("Reranker returned a non-numeric score")
        numeric = float(score)
        if not math.isfinite(numeric):
            raise ExperimentError("Reranker returned a non-finite score")
        validated.append(numeric)
    return validated


def stable_ranking(
    case: Mapping[str, Any], scores: Sequence[float]
) -> list[dict[str, Any]]:
    candidates = case["candidates"]
    validated = _validate_local_scores(scores, len(candidates))
    order = sorted(range(len(candidates)), key=lambda index: (-validated[index], index))
    return [
        {
            "id": str(candidates[index]["id"]),
            "score": validated[index],
            "original_index": index,
        }
        for index in order
    ]


def _metric_for_case(
    ranking: Sequence[Mapping[str, Any]], gold_ids: Sequence[str]
) -> dict[str, float]:
    gold = set(gold_ids)
    ranked_ids = [str(item["id"]) for item in ranking]
    relevant_ranks = [
        index + 1 for index, candidate_id in enumerate(ranked_ids) if candidate_id in gold
    ]
    reciprocal_rank = 1.0 / relevant_ranks[0] if relevant_ranks else 0.0
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank in relevant_ranks
        if rank <= 5
    )
    ideal_count = min(len(gold), 5)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return {
        "hit_at_1": float(any(rank <= 1 for rank in relevant_ranks)),
        "hit_at_3": float(any(rank <= 3 for rank in relevant_ranks)),
        "hit_at_5": float(any(rank <= 5 for rank in relevant_ranks)),
        "reciprocal_rank": reciprocal_rank,
        "ndcg_at_5": dcg / idcg if idcg else 0.0,
    }


def quality_metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    completed = [case for case in cases if case.get("status") == "complete"]
    if not completed:
        return {
            "evaluated_cases": 0,
            "hit_at_1": None,
            "hit_at_3": None,
            "hit_at_5": None,
            "mrr": None,
            "binary_ndcg_at_5": None,
        }
    values = [
        _metric_for_case(case["ranking"], case["gold_ids"]) for case in completed
    ]
    return {
        "evaluated_cases": len(completed),
        "hit_at_1": statistics.fmean(item["hit_at_1"] for item in values),
        "hit_at_3": statistics.fmean(item["hit_at_3"] for item in values),
        "hit_at_5": statistics.fmean(item["hit_at_5"] for item in values),
        "mrr": statistics.fmean(item["reciprocal_rank"] for item in values),
        "binary_ndcg_at_5": statistics.fmean(item["ndcg_at_5"] for item in values),
    }


def percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (percent / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def latency_metrics(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [
        float(case["latency_ms"])
        for case in cases
        if case.get("status") == "complete" and case.get("latency_ms") is not None
    ]
    return {
        "measured_cases": len(values),
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
    }


def dataset_coverage(manifest: Mapping[str, Any]) -> dict[str, Any]:
    case_coverages: list[dict[str, Any]] = []
    total_gold = 0
    covered_gold = 0
    cases_with_gold = 0
    for case in manifest["cases"]:
        gold = set(str(item) for item in case["gold_ids"])
        candidates = {str(item["id"]) for item in case["candidates"]}
        present = sorted(gold & candidates)
        missing = sorted(gold - candidates)
        total_gold += len(gold)
        covered_gold += len(present)
        cases_with_gold += int(bool(present))
        case_coverages.append(
            {
                "id": str(case["id"]),
                "present_gold_ids": present,
                "missing_gold_ids": missing,
            }
        )
    count = len(manifest["cases"])
    return {
        "case_count": count,
        "cases_with_candidate_gold": cases_with_gold,
        "case_recall_upper_bound": cases_with_gold / count if count else None,
        "gold_id_count": total_gold,
        "covered_gold_id_count": covered_gold,
        "gold_id_recall_upper_bound": covered_gold / total_gold if total_gold else None,
        "per_case": case_coverages,
    }


def _base_report(
    manifest: Mapping[str, Any],
    *,
    arm: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    fingerprint = manifest_fingerprint(manifest)
    input_case_ids = [str(case["id"]) for case in manifest["cases"]]
    candidate_orders = {
        str(case["id"]): [str(item["id"]) for item in case["candidates"]]
        for case in manifest["cases"]
    }
    config_copy = dict(config)
    return {
        "schema": ARM_SCHEMA,
        "created_at": utc_now(),
        "arm": arm,
        "status": "running",
        "dataset": {
            "fingerprint": fingerprint,
            "metadata": manifest.get("metadata", {}),
            "case_ids": input_case_ids,
            "candidate_order_fingerprint": sha256_json(candidate_orders),
            "coverage": dataset_coverage(manifest),
        },
        "config": config_copy,
        "config_fingerprint": sha256_json(config_copy),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "cold_load": None,
        "warmup": None,
        "cases": [],
        "failures": [],
        "usage": {
            "calls": 0,
            "calls_with_known_usage": 0,
            "calls_with_unknown_usage": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_input_cost": None,
            "known_input_cost_lower_bound": None,
            "currency": None,
            "pricing": "unavailable",
        },
        "metrics": {},
        "limitations": [
            "Closed-pool pilot over a 35-chunk checked-in snapshot, not a production RAG evaluation.",
            "Binary metrics use recorded gold only; unlabelled relevant passages may be penalized.",
        ],
    }


def _finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    cases = report["cases"]
    completed = sum(case.get("status") == "complete" for case in cases)
    failed = sum(case.get("status") == "failed" for case in cases)
    expected = len(report["dataset"]["case_ids"])
    if report["status"] == "running":
        report["status"] = (
            "complete" if completed == len(cases) else "partial" if completed else "failed"
        )
    report["metrics"] = {
        "completion": {
            "total_cases": expected,
            "completed_cases": completed,
            "failed_cases": failed,
            "unmeasured_cases": max(0, expected - completed - failed),
            "completion_rate": completed / expected if expected else 0.0,
        },
        "quality": quality_metrics(cases),
        "latency": latency_metrics(cases),
    }
    usage = report["usage"]
    price = report["config"].get("input_price_per_million")
    if price is not None:
        usage["currency"] = "USD"
        known_cost = usage["input_tokens"] * float(price) / 1_000_000
        if usage["calls_with_unknown_usage"]:
            usage["known_input_cost_lower_bound"] = known_cost
            usage["pricing"] = "unavailable_incomplete_usage"
        else:
            usage["estimated_input_cost"] = known_cost
            usage["pricing"] = "explicit_cli_estimate"
    return report


def _failure(case_id: str, phase: str, exc: Exception) -> dict[str, str]:
    code = getattr(exc, "code", type(exc).__name__)
    return {
        "case_id": case_id,
        "phase": phase,
        "code": str(code),
        "message": str(exc),
    }


def _run_local_arm(
    manifest: Mapping[str, Any],
    *,
    arm: str,
    model: str | None,
    batch_size: int,
    max_length: int,
    clock: Clock,
) -> dict[str, Any]:
    if arm == "lexical":
        reranker: Any = LightweightLexicalReranker()
        config = {
            "implementation": (
                "backend.research.rag.retrieval.paper_lightweight_reranker."
                "LightweightLexicalReranker"
            ),
            "model": None,
            "device": "cpu",
            "batch_size": None,
            "max_length": None,
            "input_price_per_million": None,
        }
        report = _base_report(manifest, arm=arm, config=config)
        report["cold_load"] = {"latency_ms": 0.0, "note": "no model load"}
    else:
        model_name = model or DEFAULT_RERANKER_MODEL
        model_path = Path(model_name)
        model_revision = (
            model_path.name
            if model_path.exists() and model_path.parent.name == "snapshots"
            else None
        )
        config = {
            "implementation": "infrastructure.external.reranker.CrossEncoderReranker",
            "model": model_name,
            "model_revision": model_revision,
            "device": "cpu",
            "batch_size": batch_size,
            "max_length": max_length,
            "input_price_per_million": None,
        }
        report = _base_report(manifest, arm=arm, config=config)
        try:
            import torch

            report["environment"]["torch_version"] = str(torch.__version__)
            report["environment"]["torch_threads"] = int(torch.get_num_threads())
            report["environment"]["cuda_available"] = bool(torch.cuda.is_available())
        except Exception as exc:
            raise ConfigurationError("BGE requires a working CPU torch installation") from exc
        reranker = CrossEncoderReranker(
            model_name=model_name,
            device="cpu",
            max_length=max_length,
            batch_size=batch_size,
        )
        started = clock()
        try:
            reranker._ensure_model()
        except Exception as exc:
            raise ConfigurationError(f"Could not load BGE model: {exc}") from exc
        report["cold_load"] = {"latency_ms": (clock() - started) * 1000.0}

    cases = manifest["cases"]
    if not cases:
        raise ConfigurationError("Dataset contains no cases")
    first = cases[0]
    started = clock()
    warmup_scores = reranker.score(str(first["query"]), _candidate_texts(first))
    _validate_local_scores(warmup_scores, len(first["candidates"]))
    report["warmup"] = {
        "case_id": str(first["id"]),
        "status": "complete",
        "latency_ms": (clock() - started) * 1000.0,
        "calls": 1,
    }
    report["usage"]["calls"] = 1

    for case in cases:
        case_id = str(case["id"])
        started = clock()
        try:
            scores = reranker.score(str(case["query"]), _candidate_texts(case))
            ranking = stable_ranking(case, scores)
            report["cases"].append(
                {
                    "id": case_id,
                    "paper_id": str(case["paper_id"]),
                    "query": str(case["query"]),
                    "status": "complete",
                    "gold_ids": list(case["gold_ids"]),
                    "candidate_ids": [str(item["id"]) for item in case["candidates"]],
                    "ranking": ranking,
                    "latency_ms": (clock() - started) * 1000.0,
                    "observations": None,
                }
            )
        except Exception as exc:
            failure = _failure(case_id, "measured", exc)
            report["failures"].append(failure)
            report["cases"].append(
                {
                    "id": case_id,
                    "paper_id": str(case["paper_id"]),
                    "query": str(case["query"]),
                    "status": "failed",
                    "gold_ids": list(case["gold_ids"]),
                    "candidate_ids": [str(item["id"]) for item in case["candidates"]],
                    "ranking": None,
                    "latency_ms": (clock() - started) * 1000.0,
                    "error": failure,
                    "observations": None,
                }
            )
        report["usage"]["calls"] += 1
    if arm == "bge":
        report["limitations"].append(
            f"BGE used its native max_length={max_length} token truncation on CPU."
        )
        report["limitations"].append("Local CPU compute cost is unknown, not zero.")
    return _finalize_report(report)


def build_jev_payload(
    case: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], *, model: str
) -> tuple[dict[str, Any], tuple[str, ...]]:
    passages = [
        {"id": str(candidate["id"]), "text": str(candidate["text"])}
        for candidate in candidates
    ]
    questions: dict[str, Any] = {}
    question_ids: list[str] = []
    for index, candidate in enumerate(candidates):
        question_id = f"passage_{index:03d}"
        passage_id = str(candidate["id"])
        question_ids.append(question_id)
        questions[question_id] = {
            "type": "score",
            "instructions": (
                f"How relevant is passage `{passage_id}` to answering `query`? "
                f"Evaluate only passage `{passage_id}` in `passages`."
            ),
            "criteria": list(JEV_SCORE_CRITERIA),
        }
    return (
        {
            "model": model,
            "state": {"query": str(case["query"]), "passages": passages},
            "questions": questions,
        },
        tuple(question_ids),
    )


def _number(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderResponseError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ProviderResponseError(f"{label} must be finite and in [{low}, {high}]")
    return result


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderResponseError(f"{label} must be a non-negative integer")
    return value


def validate_jev_response(
    payload: Mapping[str, Any], *, expected_model: str, question_ids: Sequence[str]
) -> tuple[tuple[float, ...], tuple[dict[str, Any], ...], dict[str, int], str]:
    if not isinstance(payload, Mapping):
        raise ProviderResponseError("Provider response must be a JSON object")
    resolved_model = payload.get("model")
    if resolved_model != expected_model:
        raise ProviderResponseError(
            f"Provider model mismatch: expected {expected_model!r}, got {resolved_model!r}"
        )
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise ProviderResponseError("Provider response answers must be an object")
    expected_ids = set(question_ids)
    actual_ids = set(answers)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ProviderResponseError(
            f"Provider answer IDs mismatch; missing={missing}, extra={extra}"
        )
    validated_answers: list[dict[str, Any]] = []
    scores: list[float] = []
    expected_legend = {str(index): text for index, text in enumerate(JEV_SCORE_CRITERIA)}
    expected_levels = set(expected_legend)
    for question_id in question_ids:
        answer = answers[question_id]
        if not isinstance(answer, Mapping) or answer.get("type") != "score":
            raise ProviderResponseError(f"answers.{question_id} must be a score answer")
        score = _number(answer.get("score"), f"answers.{question_id}.score", 0.0, 3.0)
        confidence = _number(
            answer.get("confidence"), f"answers.{question_id}.confidence", 0.0, 1.0
        )
        legend = answer.get("legend")
        if legend != expected_legend:
            raise ProviderResponseError(f"answers.{question_id}.legend did not match rubric")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, Mapping) or set(probabilities) != expected_levels:
            raise ProviderResponseError(
                f"answers.{question_id}.probabilities must contain levels 0..3"
            )
        validated_probabilities = {
            level: _number(
                probabilities[level],
                f"answers.{question_id}.probabilities.{level}",
                0.0,
                1.0,
            )
            for level in sorted(expected_levels)
        }
        probability_sum = sum(validated_probabilities.values())
        if not math.isclose(probability_sum, 1.0, abs_tol=1e-6):
            raise ProviderResponseError(
                f"answers.{question_id}.probabilities must sum to 1"
            )
        weighted_score = sum(
            int(level) * probability for level, probability in validated_probabilities.items()
        )
        if not math.isclose(score, weighted_score, abs_tol=1e-6):
            raise ProviderResponseError(
                f"answers.{question_id}.score did not match its probabilities"
            )
        scores.append(score / 3.0)
        validated_answers.append(
            {
                "type": "score",
                "score": score,
                "normalized_score": score / 3.0,
                "confidence": confidence,
                "legend": dict(legend),
                "probabilities": validated_probabilities,
            }
        )
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise ProviderResponseError("Provider response usage must be an object")
    validated_usage = {
        "input_tokens": _nonnegative_int(usage.get("input_tokens"), "usage.input_tokens"),
        "output_tokens": _nonnegative_int(
            usage.get("output_tokens"), "usage.output_tokens"
        ),
    }
    return tuple(scores), tuple(validated_answers), validated_usage, str(resolved_model)


def extract_jev_usage(payload: Mapping[str, Any]) -> dict[str, int]:
    """Extract billable observations independently from answer validation."""

    if not isinstance(payload, Mapping):
        raise ProviderResponseError("Provider response must be a JSON object")
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise ProviderResponseError("Provider response usage must be an object")
    return {
        "input_tokens": _nonnegative_int(usage.get("input_tokens"), "usage.input_tokens"),
        "output_tokens": _nonnegative_int(
            usage.get("output_tokens"), "usage.output_tokens"
        ),
    }


def post_jev_json(
    endpoint: str, payload: Mapping[str, Any], api_key: str, timeout: float
) -> Mapping[str, Any]:
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AgoraHub-JevRerankExperiment/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        code = int(exc.code)
        fatal = code in {401, 403, 404, 422}
        raise ProviderRequestError(
            f"http_{code}", f"TypeSafe request failed with HTTP {code}", fatal=fatal
        ) from None
    except (URLError, TimeoutError, OSError) as exc:
        name = type(exc).__name__
        raise ProviderRequestError(
            "transport_error", f"TypeSafe transport failed ({name})"
        ) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderRequestError("response_too_large", "TypeSafe response exceeded 4 MiB")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderRequestError(
            "invalid_json", f"TypeSafe returned invalid JSON ({type(exc).__name__})"
        ) from None
    if not isinstance(decoded, Mapping):
        raise ProviderRequestError("invalid_json_shape", "TypeSafe response must be an object")
    return decoded


def _jev_batch(
    case: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    model: str,
    api_key: str,
    timeout: float,
    sender: Sender,
    clock: Clock,
) -> JevBatchResult:
    request_payload, question_ids = build_jev_payload(case, candidates, model=model)
    started = clock()
    response = sender(JEV_ENDPOINT, request_payload, api_key, timeout)
    latency_ms = (clock() - started) * 1000.0
    observed_usage: dict[str, int] | None = None
    try:
        observed_usage = extract_jev_usage(response)
    except ProviderResponseError:
        pass
    try:
        scores, answers, usage, resolved_model = validate_jev_response(
            response, expected_model=model, question_ids=question_ids
        )
    except ProviderResponseError as exc:
        exc.observed_usage = observed_usage
        raise
    return JevBatchResult(scores, answers, usage, resolved_model, latency_ms)


def _required_jev_calls(manifest: Mapping[str, Any], batch_size: int) -> int:
    cases = manifest["cases"]
    warmup = math.ceil(len(cases[0]["candidates"]) / batch_size) if cases else 0
    measured = sum(math.ceil(len(case["candidates"]) / batch_size) for case in cases)
    return warmup + measured


def _add_usage(report: dict[str, Any], usage: Mapping[str, int]) -> None:
    report["usage"]["calls_with_known_usage"] += 1
    report["usage"]["input_tokens"] += int(usage["input_tokens"])
    report["usage"]["output_tokens"] += int(usage["output_tokens"])


def _run_jev_case(
    case: Mapping[str, Any],
    *,
    model: str,
    api_key: str,
    batch_size: int,
    timeout: float,
    sender: Sender,
    clock: Clock,
    report: dict[str, Any],
) -> tuple[list[float], list[dict[str, Any]], float]:
    scores: list[float] = []
    observations: list[dict[str, Any]] = []
    total_latency = 0.0
    candidates = case["candidates"]
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset : offset + batch_size]
        report["usage"]["calls"] += 1
        batch_started = clock()
        try:
            result = _jev_batch(
                case,
                batch,
                model=model,
                api_key=api_key,
                timeout=timeout,
                sender=sender,
                clock=clock,
            )
        except Exception as exc:
            total_latency += (clock() - batch_started) * 1000.0
            observed_usage = getattr(exc, "observed_usage", None)
            if observed_usage is not None:
                _add_usage(report, observed_usage)
            else:
                report["usage"]["calls_with_unknown_usage"] += 1
            raise JevCaseError(
                exc,
                scores=scores,
                observations=observations,
                latency_ms=total_latency,
            ) from exc
        _add_usage(report, result.usage)
        total_latency += result.latency_ms
        batch_observations = []
        for candidate, answer in zip(batch, result.answers, strict=True):
            item = {"candidate_id": str(candidate["id"]), **answer}
            batch_observations.append(item)
        observations.append(
            {
                "offset": offset,
                "candidate_ids": [str(candidate["id"]) for candidate in batch],
                "latency_ms": result.latency_ms,
                "resolved_model": result.resolved_model,
                "usage": result.usage,
                "answers": batch_observations,
            }
        )
        scores.extend(result.scores)
    return scores, observations, total_latency


def _run_jev_arm(
    manifest: Mapping[str, Any],
    *,
    model: str,
    batch_size: int,
    max_calls: int,
    timeout: float,
    input_price_per_million: float | None,
    env_file: Path | None,
    environ: Mapping[str, str],
    sender: Sender,
    clock: Clock,
) -> dict[str, Any]:
    config = {
        "endpoint": JEV_ENDPOINT,
        "model": model,
        "batch_size": batch_size,
        "max_calls": max_calls,
        "timeout_seconds": timeout,
        "score_criteria": list(JEV_SCORE_CRITERIA),
        "score_normalization": "score / 3",
        "input_price_per_million": input_price_per_million,
        "retry_policy": "none",
    }
    report = _base_report(manifest, arm="jev", config=config)
    report["cold_load"] = {"latency_ms": None, "note": "hosted API"}
    key = _api_key(env_file, environ)
    if not key:
        report["status"] = "blocked"
        report["blocked_reason"] = "missing_TYPESAFE_API_KEY"
        report["warmup"] = {"status": "not_run", "reason": "missing credential"}
        report["limitations"].append("Jev was unmeasured because credentials were absent.")
        return _finalize_report(report)
    required_calls = _required_jev_calls(manifest, batch_size)
    if required_calls > max_calls:
        raise ConfigurationError(
            f"--max-calls={max_calls} is insufficient; this dataset requires "
            f"{required_calls} calls including warmup"
        )
    cases = manifest["cases"]
    if not cases:
        raise ConfigurationError("Dataset contains no cases")
    first = cases[0]
    try:
        _scores, observations, warmup_latency = _run_jev_case(
            first,
            model=model,
            api_key=key,
            batch_size=batch_size,
            timeout=timeout,
            sender=sender,
            clock=clock,
            report=report,
        )
        report["warmup"] = {
            "case_id": str(first["id"]),
            "status": "complete",
            "latency_ms": warmup_latency,
            "calls": len(observations),
            "usage": {
                "input_tokens": sum(item["usage"]["input_tokens"] for item in observations),
                "output_tokens": sum(item["usage"]["output_tokens"] for item in observations),
            },
        }
    except Exception as exc:
        cause = exc.cause if isinstance(exc, JevCaseError) else exc
        failure = _failure(str(first["id"]), "warmup", cause)
        report["failures"].append(failure)
        report["warmup"] = {
            "case_id": str(first["id"]),
            "status": "failed",
            "error": failure,
            "partial_observations": (
                exc.observations if isinstance(exc, JevCaseError) else []
            ),
            "latency_ms": exc.latency_ms if isinstance(exc, JevCaseError) else None,
        }
        if isinstance(cause, ProviderRequestError) and cause.fatal:
            report["status"] = "blocked"
            report["blocked_reason"] = cause.code
            report["limitations"].append("Jev measured cases were not run after a fatal warmup error.")
            return _finalize_report(report)

    for case in cases:
        case_id = str(case["id"])
        started = clock()
        try:
            scores, observations, request_latency = _run_jev_case(
                case,
                model=model,
                api_key=key,
                batch_size=batch_size,
                timeout=timeout,
                sender=sender,
                clock=clock,
                report=report,
            )
            report["cases"].append(
                {
                    "id": case_id,
                    "paper_id": str(case["paper_id"]),
                    "query": str(case["query"]),
                    "status": "complete",
                    "gold_ids": list(case["gold_ids"]),
                    "candidate_ids": [str(item["id"]) for item in case["candidates"]],
                    "ranking": stable_ranking(case, scores),
                    "latency_ms": (clock() - started) * 1000.0,
                    "request_latency_ms": request_latency,
                    "observations": observations,
                }
            )
        except Exception as exc:
            cause = exc.cause if isinstance(exc, JevCaseError) else exc
            failure = _failure(case_id, "measured", cause)
            report["failures"].append(failure)
            report["cases"].append(
                {
                    "id": case_id,
                    "paper_id": str(case["paper_id"]),
                    "query": str(case["query"]),
                    "status": "failed",
                    "gold_ids": list(case["gold_ids"]),
                    "candidate_ids": [str(item["id"]) for item in case["candidates"]],
                    "ranking": None,
                    "latency_ms": (clock() - started) * 1000.0,
                    "request_latency_ms": (
                        exc.latency_ms if isinstance(exc, JevCaseError) else None
                    ),
                    "error": failure,
                    "observations": (
                        exc.observations if isinstance(exc, JevCaseError) else []
                    ),
                }
            )
            if isinstance(cause, ProviderRequestError) and cause.fatal:
                remaining = cases[len(report["cases"]) :]
                for skipped in remaining:
                    skipped_failure = {
                        "case_id": str(skipped["id"]),
                        "phase": "measured",
                        "code": "skipped_after_fatal_provider_error",
                        "message": "Not run after fatal provider error",
                    }
                    report["failures"].append(skipped_failure)
                    report["cases"].append(
                        {
                            "id": str(skipped["id"]),
                            "paper_id": str(skipped["paper_id"]),
                            "query": str(skipped["query"]),
                            "status": "failed",
                            "gold_ids": list(skipped["gold_ids"]),
                            "candidate_ids": [
                                str(item["id"]) for item in skipped["candidates"]
                            ],
                            "ranking": None,
                            "latency_ms": None,
                            "error": skipped_failure,
                            "observations": None,
                        }
                    )
                break
    report["limitations"].append(
        "Jev latency includes network time; local BGE latency does not."
    )
    return _finalize_report(report)


def run_arm(
    dataset_path: Path,
    *,
    arm: str,
    model: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_calls: int = DEFAULT_MAX_CALLS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    input_price_per_million: float | None = None,
    env_file: Path | None = None,
    max_length: int = DEFAULT_MAX_LENGTH,
    environ: Mapping[str, str] | None = None,
    sender: Sender = post_jev_json,
    clock: Clock = time.perf_counter,
) -> dict[str, Any]:
    if arm not in EXPECTED_ARMS:
        raise ConfigurationError(f"Unknown arm: {arm}")
    if batch_size < 1:
        raise ConfigurationError("--batch-size must be positive")
    if arm == "jev" and batch_size > 8:
        raise ConfigurationError("Jev --batch-size must not exceed 8")
    if max_calls < 1:
        raise ConfigurationError("--max-calls must be positive")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigurationError("--timeout must be a positive finite number")
    if max_length < 1:
        raise ConfigurationError("--max-length must be positive")
    if input_price_per_million is not None and (
        not math.isfinite(input_price_per_million) or input_price_per_million < 0
    ):
        raise ConfigurationError("--input-price-per-million must be non-negative")
    manifest = load_manifest(dataset_path)
    if arm in {"lexical", "bge"}:
        return _run_local_arm(
            manifest,
            arm=arm,
            model=model,
            batch_size=batch_size,
            max_length=max_length,
            clock=clock,
        )
    return _run_jev_arm(
        manifest,
        model=model or DEFAULT_JEV_MODEL,
        batch_size=batch_size,
        max_calls=max_calls,
        timeout=timeout,
        input_price_per_million=input_price_per_million,
        env_file=env_file,
        environ=os.environ if environ is None else environ,
        sender=sender,
        clock=clock,
    )


def _load_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportValidationError(f"Could not read report {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != ARM_SCHEMA:
        raise ReportValidationError(f"Invalid arm report schema: {path}")
    arm = payload.get("arm")
    if arm not in EXPECTED_ARMS:
        raise ReportValidationError(f"Invalid arm in report {path}: {arm!r}")
    dataset = payload.get("dataset")
    if not isinstance(dataset, Mapping) or not _is_fingerprint(dataset.get("fingerprint")):
        raise ReportValidationError(f"Report lacks dataset fingerprint: {path}")
    if not _is_fingerprint(dataset.get("candidate_order_fingerprint")):
        raise ReportValidationError(f"Report lacks candidate order fingerprint: {path}")
    case_ids = dataset.get("case_ids")
    if (
        not isinstance(case_ids, list)
        or any(not isinstance(item, str) or not item for item in case_ids)
        or len(case_ids) != len(set(case_ids))
    ):
        raise ReportValidationError(f"Report has invalid dataset case IDs: {path}")
    config = payload.get("config")
    if not isinstance(config, Mapping):
        raise ReportValidationError(f"Report config must be an object: {path}")
    if payload.get("config_fingerprint") != sha256_json(config):
        raise ReportValidationError(f"Report config fingerprint mismatch: {path}")
    status = payload.get("status")
    if status not in {"complete", "partial", "failed", "blocked"}:
        raise ReportValidationError(f"Report has invalid status: {path}")
    cases_by_id = _cases_by_id(payload)
    unknown_ids = sorted(set(cases_by_id) - set(case_ids))
    if unknown_ids:
        raise ReportValidationError(f"Report contains unknown cases {unknown_ids}: {path}")
    if status != "blocked" and set(cases_by_id) != set(case_ids):
        raise ReportValidationError(f"Report does not account for every dataset case: {path}")
    for case_id, case in cases_by_id.items():
        _validate_report_case(case_id, case, path)
    if status != "blocked":
        observed_candidate_orders = {
            case_id: list(cases_by_id[case_id]["candidate_ids"]) for case_id in case_ids
        }
        if sha256_json(observed_candidate_orders) != dataset["candidate_order_fingerprint"]:
            raise ReportValidationError(
                f"Report candidate order fingerprint does not match its cases: {path}"
            )
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise ReportValidationError(f"Report usage must be an object: {path}")
    if arm == "jev":
        calls = _report_nonnegative_int(usage.get("calls"), "usage.calls", path)
        known = _report_nonnegative_int(
            usage.get("calls_with_known_usage"), "usage.calls_with_known_usage", path
        )
        unknown = _report_nonnegative_int(
            usage.get("calls_with_unknown_usage"), "usage.calls_with_unknown_usage", path
        )
        if known + unknown != calls:
            raise ReportValidationError(
                f"Jev usage call accounting does not add up: {path}"
            )
    return payload


def _is_fingerprint(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _report_nonnegative_int(value: Any, label: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportValidationError(f"{label} must be a non-negative integer: {path}")
    return value


def _validate_report_case(
    case_id: str, case: Mapping[str, Any], path: Path
) -> None:
    status = case.get("status")
    if status not in {"complete", "failed"}:
        raise ReportValidationError(f"Case {case_id} has invalid status: {path}")
    candidate_ids = case.get("candidate_ids")
    if (
        not isinstance(candidate_ids, list)
        or any(not isinstance(item, str) or not item for item in candidate_ids)
        or len(candidate_ids) != len(set(candidate_ids))
    ):
        raise ReportValidationError(f"Case {case_id} has invalid candidate IDs: {path}")
    ranking = case.get("ranking")
    if status == "failed":
        if ranking is not None:
            raise ReportValidationError(f"Failed case {case_id} must not have a ranking: {path}")
        return
    if not isinstance(ranking, list) or len(ranking) != len(candidate_ids):
        raise ReportValidationError(f"Case {case_id} ranking is incomplete: {path}")
    ranked_ids: list[str] = []
    for item in ranking:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            raise ReportValidationError(f"Case {case_id} has invalid ranking item: {path}")
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ReportValidationError(f"Case {case_id} has invalid ranking score: {path}")
        ranked_ids.append(item["id"])
    if len(ranked_ids) != len(set(ranked_ids)) or set(ranked_ids) != set(candidate_ids):
        raise ReportValidationError(f"Case {case_id} ranking IDs do not match candidates: {path}")


def _cases_by_id(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    cases = report.get("cases")
    if not isinstance(cases, list):
        raise ReportValidationError(f"{report.get('arm')} report cases must be an array")
    result: dict[str, Mapping[str, Any]] = {}
    for case in cases:
        if not isinstance(case, Mapping) or not isinstance(case.get("id"), str):
            raise ReportValidationError(f"{report.get('arm')} report has invalid case")
        if case["id"] in result:
            raise ReportValidationError(
                f"{report.get('arm')} report duplicates case {case['id']}"
            )
        result[case["id"]] = case
    return result


def _paired_comparisons(reports: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    arms = [arm for arm in EXPECTED_ARMS if arm in reports]
    for left_index, left_arm in enumerate(arms):
        for right_arm in arms[left_index + 1 :]:
            left_cases = _cases_by_id(reports[left_arm])
            right_cases = _cases_by_id(reports[right_arm])
            paired_ids = [
                case_id
                for case_id in reports[left_arm]["dataset"]["case_ids"]
                if case_id in left_cases
                and case_id in right_cases
                and left_cases[case_id].get("status") == "complete"
                and right_cases[case_id].get("status") == "complete"
            ]
            left_metrics = quality_metrics([left_cases[item] for item in paired_ids])
            right_metrics = quality_metrics([right_cases[item] for item in paired_ids])
            delta: dict[str, float | None] = {}
            for key in ("hit_at_1", "hit_at_3", "hit_at_5", "mrr", "binary_ndcg_at_5"):
                left_value = left_metrics[key]
                right_value = right_metrics[key]
                delta[key] = (
                    None
                    if left_value is None or right_value is None
                    else float(right_value) - float(left_value)
                )
            comparisons.append(
                {
                    "left_arm": left_arm,
                    "right_arm": right_arm,
                    "paired_case_count": len(paired_ids),
                    "paired_case_ids": paired_ids,
                    "left_quality": left_metrics,
                    "right_quality": right_metrics,
                    "right_minus_left": delta,
                    "winner": None,
                    "winner_reason": "No automatic winner is selected by this pilot.",
                }
            )
    return comparisons


def summarize_reports(report_paths: Sequence[Path]) -> dict[str, Any]:
    if not report_paths:
        raise ReportValidationError("At least one arm report is required")
    reports: dict[str, dict[str, Any]] = {}
    for path in report_paths:
        report = _load_report(path)
        arm = report["arm"]
        if arm in reports:
            raise ReportValidationError(f"Duplicate report for arm {arm}")
        reports[arm] = report
    fingerprints = {report["dataset"]["fingerprint"] for report in reports.values()}
    if len(fingerprints) != 1:
        raise ReportValidationError(
            "Dataset fingerprint mismatch: reports do not use identical inputs"
        )
    candidate_fingerprints = {
        report["dataset"].get("candidate_order_fingerprint")
        for report in reports.values()
    }
    if len(candidate_fingerprints) != 1 or None in candidate_fingerprints:
        raise ReportValidationError(
            "Candidate order fingerprint mismatch: reports are not directly comparable"
        )
    case_id_lists = {
        tuple(report["dataset"].get("case_ids", ())) for report in reports.values()
    }
    if len(case_id_lists) != 1:
        raise ReportValidationError("Case order mismatch between reports")
    fingerprint = next(iter(fingerprints))
    arm_summaries: dict[str, Any] = {}
    for arm in EXPECTED_ARMS:
        if arm not in reports:
            arm_summaries[arm] = {
                "status": "unmeasured",
                "reason": "arm report was not supplied",
                "metrics": None,
                "usage": None,
                "warmup": None,
                "cold_load": None,
            }
            continue
        report = reports[arm]
        arm_summaries[arm] = {
            "status": report.get("status"),
            "metrics": report.get("metrics"),
            "usage": report.get("usage"),
            "warmup": report.get("warmup"),
            "cold_load": report.get("cold_load"),
            "failure_count": len(report.get("failures", [])),
            "config": report.get("config"),
            "config_fingerprint": report.get("config_fingerprint"),
        }
        if report.get("blocked_reason"):
            arm_summaries[arm]["blocked_reason"] = report["blocked_reason"]
    missing_or_unmeasured = [
        arm
        for arm, summary in arm_summaries.items()
        if summary["status"] not in {"complete", "partial"}
    ]
    comparison_fingerprint = sha256_json(
        {
            "dataset_fingerprint": fingerprint,
            "candidate_order_fingerprint": next(iter(candidate_fingerprints)),
            "arm_config_fingerprints": {
                arm: reports[arm].get("config_fingerprint") if arm in reports else None
                for arm in EXPECTED_ARMS
            },
        }
    )
    return {
        "schema": SUMMARY_SCHEMA,
        "created_at": utc_now(),
        "dataset_fingerprint": fingerprint,
        "candidate_order_fingerprint": next(iter(candidate_fingerprints)),
        "comparison_fingerprint": comparison_fingerprint,
        "dataset": next(iter(reports.values()))["dataset"],
        "arms": arm_summaries,
        "paired_comparisons": _paired_comparisons(reports),
        "winner": None,
        "winner_reason": (
            "No winner: unmeasured or blocked arms: " + ", ".join(missing_or_unmeasured)
            if missing_or_unmeasured
            else "No automatic winner is selected; interpret paired metrics and limitations."
        ),
        "limitations": [
            "Closed-pool pilot over a checked-in 35-chunk snapshot; not production RAG evidence.",
            "Metrics use recorded binary gold only and may penalize relevant unlabelled passages.",
            "Hosted Jev latency includes network time; local BGE latency does not.",
        ],
    }


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise ConfigurationError(f"Refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    except FileExistsError as exc:
        raise ConfigurationError(f"Refusing to overwrite existing output: {path}") from exc


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run one reranking arm")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--arm", choices=EXPECTED_ARMS, required=True)
    run.add_argument(
        "--model",
        help=(
            "Model id/path. Defaults to BAAI/bge-reranker-v2-m3 for bge and "
            "jev-1.13.0 for jev."
        ),
    )
    run.add_argument("--env-file", type=Path)
    run.add_argument("--batch-size", type=_positive_int, default=DEFAULT_BATCH_SIZE)
    run.add_argument("--max-calls", type=_positive_int, default=DEFAULT_MAX_CALLS)
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--input-price-per-million", type=float)
    run.add_argument("--max-length", type=_positive_int, default=DEFAULT_MAX_LENGTH)

    summarize = commands.add_parser("summarize", help="Combine matching arm reports")
    summarize.add_argument("--reports", type=Path, nargs="+", required=True)
    summarize.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output.exists():
            raise ConfigurationError(f"Refusing to overwrite existing output: {args.output}")
        if args.command == "run":
            report = run_arm(
                args.dataset,
                arm=args.arm,
                model=args.model,
                batch_size=args.batch_size,
                max_calls=args.max_calls,
                timeout=args.timeout,
                input_price_per_million=args.input_price_per_million,
                env_file=args.env_file,
                max_length=args.max_length,
            )
            write_json(args.output, report)
            print(
                f"[{report['status']}] wrote {args.arm} report to {args.output}; "
                f"dataset={report['dataset']['fingerprint']}"
            )
            return 2 if report["status"] == "blocked" else 0 if report["status"] == "complete" else 1
        summary = summarize_reports(args.reports)
        write_json(args.output, summary)
        print(
            f"[complete] wrote summary to {args.output}; "
            f"dataset={summary['dataset_fingerprint']}"
        )
        return 0
    except (
        ConfigurationError,
        DatasetPreparationError,
        ReportValidationError,
        ExperimentError,
    ) as exc:
        print(f"[failed] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
