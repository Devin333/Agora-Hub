from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from framework.harness.rag.models import (
    FORBIDDEN_RAG_PLAN_KEYS,
    RetrievalOperation,
    RetrievalPlanCandidate,
    RetrievalStepSpec,
    RAGSessionSpec,
)
from framework.harness.rag.planner import DeterministicRAGPlanner, RAGPlanner, WorkerRAGPlanner


CANDIDATE_EXPERIMENT_VERSION = "research-retrieval-candidate-v1"
CANDIDATE_EXPERIMENT_CONFIG_VERSION = "research-retrieval-candidate-config-v1"
_SUPPORTED_CONSTRAINT_KEYS = frozenset(
    {
        "entity",
        "entities",
        "language",
        "languages",
        "metric",
        "metrics",
        "negation",
        "negations",
        "source",
        "sources",
        "source_ref",
        "source_refs",
        "time",
        "time_range",
        "version",
        "versions",
    }
)
_NEGATORS = frozenset({"no", "not", "never", "without", "exclude", "excluding", "不", "无", "未", "不要", "排除"})
_TOKEN_RE = re.compile(r"[^\W_]+(?:[._:/-][^\W_]+)*", re.UNICODE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_TIME_RE = re.compile(r"\b(?:19|20)\d{2}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?\b")
_VERSION_RE = re.compile(r"\b(?:v(?:ersion)?\s*)\d+(?:\.\d+){0,3}\b|\b\d+\.\d+(?:\.\d+){0,2}\b", re.IGNORECASE)
_METRIC_RE = re.compile(
    r"\b(?:accuracy|precision|recall|f1(?:[- ]?score)?|auc|bleu|rouge|latency|throughput|"
    r"error rate|success rate|hit rate|mrr|map|ndcg|percent(?:age)?|%)\b",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r"[\"']([^\"']{2,80})[\"']")
_ENTITY_RE = re.compile(r"\b(?:[A-Z]{2,}[A-Za-z0-9._-]*|[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+)\b")


@dataclass(frozen=True)
class RetrievalCandidateExperimentConfig:
    enabled: bool = False
    max_candidate_queries: int = 2
    max_worker_calls: int = 1
    timeout_seconds: float = 8.0
    max_transport_attempts: int = 1
    allowed_expansion_terms: tuple[str, ...] = ()
    configured_hard_constraints: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": CANDIDATE_EXPERIMENT_CONFIG_VERSION,
            "experiment_version": CANDIDATE_EXPERIMENT_VERSION,
            "enabled": self.enabled,
            "max_candidate_queries": self.max_candidate_queries,
            "max_worker_calls": self.max_worker_calls,
            "timeout_seconds": self.timeout_seconds,
            "max_transport_attempts": self.max_transport_attempts,
            "allowed_expansion_terms": list(self.allowed_expansion_terms),
            "configured_hard_constraints": {key: list(value) for key, value in self.configured_hard_constraints.items()},
        }

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool")
        for field_name in ("max_candidate_queries", "max_worker_calls", "max_transport_attempts"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field_name} must be an integer")
        if self.max_candidate_queries <= 0:
            raise ValueError("max_candidate_queries must be greater than zero")
        if self.max_worker_calls < 0:
            raise ValueError("max_worker_calls must not be negative")
        if self.max_transport_attempts <= 0:
            raise ValueError("max_transport_attempts must be greater than zero")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be a number")
        if not math.isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        expansion_terms = _strict_config_text_values(
            self.allowed_expansion_terms,
            field_name="allowed_expansion_terms",
        )
        object.__setattr__(self, "allowed_expansion_terms", expansion_terms)
        if not isinstance(self.configured_hard_constraints, Mapping):
            raise TypeError("configured_hard_constraints must be a mapping")
        normalized: dict[str, tuple[str, ...]] = {}
        for key, value in self.configured_hard_constraints.items():
            if not isinstance(key, str):
                raise TypeError("configured_hard_constraints keys must be strings")
            if not key or key != key.strip():
                raise ValueError(
                    "configured_hard_constraints keys must be non-empty canonical text"
                )
            values = _strict_config_text_values(
                value,
                field_name=f"configured_hard_constraints[{key!r}]",
            )
            if not values:
                raise ValueError(
                    "configured hard constraints require non-empty textual values"
                )
            normalized[key] = values
        object.__setattr__(
            self,
            "configured_hard_constraints",
            MappingProxyType(normalized),
        )



@dataclass(frozen=True)
class CandidatePlanEvaluation:
    experiment_version: str
    enabled: bool
    accepted: bool
    used_fallback: bool
    reason: str
    original_query: str
    candidate_id: str | None = None
    candidate_queries: tuple[str, ...] = ()
    violations: tuple[dict[str, Any], ...] = ()
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_version": self.experiment_version,
            "enabled": self.enabled,
            "accepted": self.accepted,
            "used_fallback": self.used_fallback,
            "reason": self.reason,
            "original_query": self.original_query,
            "candidate_id": self.candidate_id,
            "candidate_queries": list(self.candidate_queries),
            "violations": [dict(item) for item in self.violations],
            "fallback_reason": self.fallback_reason,
        }


class ResearchCandidatePlanner:
    """Research-owned, default-off admission boundary for worker query candidates."""

    def __init__(
        self,
        worker: Any | None,
        *,
        config: RetrievalCandidateExperimentConfig | None = None,
        fallback: RAGPlanner | None = None,
        min_round_index: int = 0,
    ) -> None:
        self.config = config or RetrievalCandidateExperimentConfig()
        self.fallback = fallback or DeterministicRAGPlanner()
        self.worker = worker
        self.min_round_index = max(0, int(min_round_index))
        self.last_evaluation: CandidatePlanEvaluation | None = None
        self._worker_planner = (
            WorkerRAGPlanner(
                worker,
                fallback=self.fallback,
                min_round_index=self.min_round_index,
                max_worker_calls=self.config.max_worker_calls,
                max_candidate_queries=self.config.max_candidate_queries,
                timeout_seconds=self.config.timeout_seconds,
                max_transport_attempts=self.config.max_transport_attempts,
                fallback_on_error=True,
            )
            if worker is not None and self.config.enabled
            else None
        )

    def plan(
        self,
        spec: RAGSessionSpec,
        *,
        round_index: int,
        gap_report: dict[str, Any],
        executed_queries: tuple[str, ...] = (),
    ) -> RetrievalPlanCandidate:
        baseline = self.fallback.plan(
            spec,
            round_index=round_index,
            gap_report=gap_report,
            executed_queries=executed_queries,
        )
        if not self.config.enabled:
            return self._fallback(
                baseline,
                CandidatePlanEvaluation(
                    experiment_version=CANDIDATE_EXPERIMENT_VERSION,
                    enabled=False,
                    accepted=False,
                    used_fallback=True,
                    reason="experiment_disabled",
                    fallback_reason="experiment_disabled",
                    original_query=spec.goal.question,
                ),
            )
        if self._worker_planner is None:
            return self._fallback(
                baseline,
                CandidatePlanEvaluation(
                    experiment_version=CANDIDATE_EXPERIMENT_VERSION,
                    enabled=True,
                    accepted=False,
                    used_fallback=True,
                    reason="worker_unavailable",
                    fallback_reason="worker_unavailable",
                    original_query=spec.goal.question,
                ),
            )
        if self._worker_planner.worker_calls >= spec.budget.max_worker_calls:
            return self._fallback(
                baseline,
                CandidatePlanEvaluation(
                    experiment_version=CANDIDATE_EXPERIMENT_VERSION,
                    enabled=True,
                    accepted=False,
                    used_fallback=True,
                    reason="worker_budget_exhausted",
                    fallback_reason="worker_budget_exhausted",
                    original_query=spec.goal.question,
                ),
            )
        candidate = self._worker_planner.plan(
            spec,
            round_index=round_index,
            gap_report=gap_report,
            executed_queries=executed_queries,
        )
        worker_fallback = dict(candidate.metadata).get("candidate_fallback")
        if isinstance(worker_fallback, dict):
            reason = _stable_worker_fallback_reason(worker_fallback.get("reason"))
            evaluation = CandidatePlanEvaluation(
                experiment_version=CANDIDATE_EXPERIMENT_VERSION,
                enabled=True,
                accepted=False,
                used_fallback=True,
                reason=reason,
                fallback_reason=reason,
                original_query=spec.goal.question,
            )
            return self._fallback(baseline, evaluation)
        try:
            evaluation = evaluate_retrieval_candidate(
                spec,
                candidate,
                config=self.config,
                executed_queries=executed_queries,
            )
        except Exception:  # deterministic fail-closed validator boundary
            return self._fallback(
                baseline,
                CandidatePlanEvaluation(
                    experiment_version=CANDIDATE_EXPERIMENT_VERSION,
                    enabled=True,
                    accepted=False,
                    used_fallback=True,
                    reason="candidate_validation_failed",
                    fallback_reason="candidate_validation_failed",
                    original_query=spec.goal.question,
                ),
            )
        if not evaluation.accepted:
            return self._fallback(baseline, evaluation)
        self.last_evaluation = evaluation
        lineage = evaluation.to_dict()
        configuration = self.config.to_dict()
        steps = tuple(
            replace(
                step,
                metadata={
                    **dict(step.metadata),
                    **(
                        {"scope_ref": str(spec.source_policy["scope_ref"])}
                        if spec.source_policy.get("scope_ref")
                        else {}
                    ),
                    "candidate_experiment": lineage,
                    "candidate_experiment_config": configuration,
                    "original_query": spec.goal.question,
                },
            )
            for step in candidate.queries
        )
        return replace(
            candidate,
            queries=steps,
            metadata={
                **dict(candidate.metadata),
                "candidate_experiment": lineage,
                "candidate_experiment_config": configuration,
                "planner": "research_candidate_experiment",
            },
        )

    def _fallback(
        self,
        baseline: RetrievalPlanCandidate,
        evaluation: CandidatePlanEvaluation,
    ) -> RetrievalPlanCandidate:
        self.last_evaluation = evaluation
        lineage = evaluation.to_dict()
        configuration = self.config.to_dict()
        queries = tuple(
            replace(
                step,
                metadata={
                    **dict(step.metadata),
                    "candidate_experiment": lineage,
                    "candidate_experiment_config": configuration,
                    "original_query": evaluation.original_query,
                },
            )
            for step in baseline.queries
        )
        return replace(
            baseline,
            queries=queries,
            metadata={
                **dict(baseline.metadata),
                "candidate_experiment": lineage,
                "candidate_experiment_config": configuration,
                "planner": "deterministic",
            },
        )


def evaluate_retrieval_candidate(
    spec: RAGSessionSpec,
    candidate: RetrievalPlanCandidate,
    *,
    config: RetrievalCandidateExperimentConfig,
    executed_queries: tuple[str, ...] = (),
) -> CandidatePlanEvaluation:
    violations: list[dict[str, Any]] = []
    forbidden_paths = _find_forbidden_paths(candidate.to_dict())
    if forbidden_paths:
        violations.append(
            _violation(
                "worker_control_field_rejected",
                "candidate contains nested control-plane fields",
                actual=list(forbidden_paths),
            )
        )
    queries = tuple(step.query if isinstance(step.query, str) else "" for step in candidate.queries)
    if not queries or any(not query.strip() for query in queries):
        violations.append(_violation("invalid_query", "candidate queries must be non-empty"))
    if len(queries) > config.max_candidate_queries:
        violations.append(
            _violation(
                "candidate_limit_exceeded",
                "candidate query count exceeds the configured limit",
                expected=config.max_candidate_queries,
                actual=len(queries),
            )
        )
    remaining_queries = max(0, spec.budget.max_queries - len(executed_queries))
    if len(queries) > remaining_queries:
        violations.append(
            _violation(
                "candidate_budget_exceeded",
                "candidate query count exceeds the remaining session query budget",
                expected=remaining_queries,
                actual=len(queries),
            )
        )
    if candidate.source_reading_plan or candidate.memory_recall_plan:
        violations.append(
            _violation(
                "unsupported_operation",
                "V1 candidates may propose search queries only",
            )
        )
    allowed_corpora = set(spec.allowed_corpora)
    for step in candidate.queries:
        if step.operation != RetrievalOperation.SEARCH_CORPUS:
            violations.append(_violation("unsupported_operation", "candidate step is not search_corpus"))
        if step.source_refs or step.max_source_reads or step.memory_namespace:
            violations.append(
                _violation(
                    "unsupported_operation",
                    "V1 search candidates cannot add source reads or memory access",
                )
            )
        expected_scope = str(spec.source_policy.get("scope_ref") or "").strip()
        candidate_scope = str(step.metadata.get("scope_ref") or "").strip()
        if candidate_scope and candidate_scope != expected_scope:
            violations.append(
                _violation(
                    "source_constraint_changed",
                    "candidate scope differs from the trusted session scope",
                    expected=expected_scope or None,
                    actual=candidate_scope,
                )
            )
        if step.metadata.get("tool_name"):
            violations.append(
                _violation(
                    "unsupported_operation",
                    "V1 search candidates cannot request tool execution",
                )
            )
        if step.corpus not in allowed_corpora:
            violations.append(
                _violation(
                    "source_constraint_changed",
                    "candidate corpus is outside the allowed source scope",
                    actual=step.corpus,
                )
            )
        forbidden = sorted(FORBIDDEN_RAG_PLAN_KEYS.intersection(step.metadata))
        if forbidden:
            violations.append(
                _violation(
                    "worker_control_field_rejected",
                    "candidate step contains control-plane fields",
                    actual=forbidden,
                )
            )
    forbidden_metadata = sorted(FORBIDDEN_RAG_PLAN_KEYS.intersection(candidate.metadata))
    if forbidden_metadata:
        violations.append(
            _violation(
                "worker_control_field_rejected",
                "candidate contains control-plane fields",
                actual=forbidden_metadata,
            )
        )

    protected, constraint_errors = _protected_constraints(spec, config)
    violations.extend(constraint_errors)
    for constraint_type, values in protected.items():
        for value in values:
            if not all(_contains_phrase(query, value) for query in queries):
                violations.append(
                    _violation(
                        f"{constraint_type}_constraint_missing",
                        "candidate omitted a protected hard constraint",
                        expected=value,
                    )
                )
    allowed_expansions = tuple(dict.fromkeys(
        (*config.allowed_expansion_terms, *spec.goal.required_evidence_types)
    ))
    for query in queries:
        if not _is_constrained_query_expansion(
            original=spec.goal.question,
            candidate=query,
            allowed_expansions=allowed_expansions,
        ):
            violations.append(
                _violation(
                    "unbounded_candidate_expansion",
                    "V1 candidates must preserve the original query verbatim and append only configured expansion terms",
                    expected=list(allowed_expansions),
                )
            )
    original_negations = _negation_signatures(spec.goal.question)
    for query in queries:
        candidate_negations = _negation_signatures(query)
        if candidate_negations != original_negations:
            violations.append(
                _violation(
                    "negation_constraint_changed",
                    "candidate changed, omitted, or introduced a protected negation",
                    expected=sorted(original_negations),
                    actual=sorted(candidate_negations),
                )
            )
    reason = "candidate_accepted" if not violations else str(violations[0]["code"])
    return CandidatePlanEvaluation(
        experiment_version=CANDIDATE_EXPERIMENT_VERSION,
        enabled=True,
        accepted=not violations,
        used_fallback=bool(violations),
        reason=reason,
        fallback_reason=None if not violations else reason,
        original_query=spec.goal.question,
        candidate_id=candidate.candidate_id,
        candidate_queries=queries,
        violations=tuple(violations),
    )


def _protected_constraints(
    spec: RAGSessionSpec,
    config: RetrievalCandidateExperimentConfig,
) -> tuple[dict[str, tuple[str, ...]], list[dict[str, Any]]]:
    constraints: dict[str, list[str]] = {
        "entity": [str(item) for item in spec.goal.target_entities if str(item).strip()],
    }
    errors: list[dict[str, Any]] = []
    for key, raw in spec.goal.constraints.items():
        normalized_key = str(key).casefold()
        values = _text_values(raw)
        if not values:
            errors.append(_violation(
                "configured_constraint_invalid",
                "configured hard constraints must use non-empty textual scalar or list values",
                constraint_key=str(key),
            ))
            continue
        if normalized_key in _SUPPORTED_CONSTRAINT_KEYS:
            constraint_type = _constraint_type(normalized_key)
        else:
            constraint_type = "configured"
        constraints.setdefault(constraint_type, []).extend(values)
    for key, values in config.configured_hard_constraints.items():
        constraints.setdefault(_constraint_type(key.casefold()), []).extend(values)

    question = spec.goal.question
    inferred = {
        "time": tuple(match.group(0) for match in _TIME_RE.finditer(question)),
        "version": tuple(match.group(0) for match in _VERSION_RE.finditer(question)),
        "metric": tuple(match.group(0) for match in _METRIC_RE.finditer(question)),
        "entity": tuple(
            dict.fromkeys(
                [match.group(1) for match in _QUOTED_RE.finditer(question)]
                + [match.group(0) for match in _ENTITY_RE.finditer(question)]
            )
        ),
    }
    for key, values in inferred.items():
        constraints.setdefault(key, []).extend(values)
    return ({
        key: tuple(dict.fromkeys(value for value in values if value.strip()))
        for key, values in constraints.items()
    }, errors)


def _constraint_type(key: str) -> str:
    if key.startswith("entit"):
        return "entity"
    if key.startswith("source"):
        return "source"
    if key.startswith("language"):
        return "language"
    if key.startswith("version"):
        return "version"
    if key.startswith("metric"):
        return "metric"
    if key.startswith("negation"):
        return "negation"
    if key.startswith("time"):
        return "time"
    return "configured"


def _strict_config_text_values(value: Any, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{field_name} must be a list or tuple of canonical text values")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field_name} must contain only strings")
        if not item or item != item.strip():
            raise ValueError(f"{field_name} must contain non-empty canonical text")
        normalized.append(item)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(normalized)


def _stable_worker_fallback_reason(value: Any) -> str:
    reason = value if isinstance(value, str) else ""
    return reason if reason in {
        "before_min_round",
        "worker_budget_exhausted",
        "worker_failed",
        "worker_timeout",
        "candidate_missing",
        "candidate_invalid",
    } else "worker_failed"


def _find_forbidden_paths(value: Any, *, path: str = "") -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            item_path = f"{path}.{key_text}" if path else key_text
            if key_text in FORBIDDEN_RAG_PLAN_KEYS:
                found.append(item_path)
            found.extend(_find_forbidden_paths(item, path=item_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            found.extend(_find_forbidden_paths(item, path=f"{path}[{index}]"))
    return tuple(found)


def _text_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (tuple, list, set, frozenset)):
        if any(not isinstance(item, str) for item in value):
            return ()
        return tuple(item.strip() for item in value if item.strip())
    return ()


def _contains_phrase(text: str, phrase: str) -> bool:
    normalized_text = _normalize_text(text)
    normalized_phrase = _normalize_text(phrase)
    if not normalized_phrase:
        return False
    if _CJK_RE.search(normalized_phrase):
        return normalized_phrase in normalized_text
    haystack = _tokens(normalized_text)
    needle = _tokens(normalized_phrase)
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(haystack[index:index + width] == needle for index in range(len(haystack) - width + 1))


def _is_constrained_query_expansion(
    *,
    original: str,
    candidate: str,
    allowed_expansions: tuple[str, ...],
) -> bool:
    if candidate == original:
        return True
    prefix = original + " "
    if not candidate.startswith(prefix):
        return False
    suffix_tokens = _tokens(_normalize_text(candidate[len(prefix):]))
    if not suffix_tokens:
        return False
    allowed_phrases = tuple(
        phrase
        for phrase in (_tokens(item) for item in allowed_expansions)
        if phrase
    )
    if not allowed_phrases:
        return False
    reachable = {0}
    for index in range(len(suffix_tokens)):
        if index not in reachable:
            continue
        for phrase in allowed_phrases:
            end = index + len(phrase)
            if suffix_tokens[index:end] == phrase:
                reachable.add(end)
    return len(suffix_tokens) in reachable


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _TOKEN_RE.finditer(str(text)))


def _negation_signatures(text: str) -> frozenset[str]:
    tokens = _tokens(text)
    signatures: set[str] = set()
    index = 0
    while index < len(tokens):
        if tokens[index] not in _NEGATORS:
            index += 1
            continue
        start = index
        while index < len(tokens) and tokens[index] in _NEGATORS:
            index += 1
        parity = (index - start) % 2
        phrase = " ".join(tokens[index:index + 4])
        signatures.add(f"{parity}:{phrase}")
    return frozenset(signatures)


def _violation(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


__all__ = [
    "CANDIDATE_EXPERIMENT_CONFIG_VERSION",
    "CANDIDATE_EXPERIMENT_VERSION",
    "CandidatePlanEvaluation",
    "ResearchCandidatePlanner",
    "RetrievalCandidateExperimentConfig",
    "evaluate_retrieval_candidate",
]
