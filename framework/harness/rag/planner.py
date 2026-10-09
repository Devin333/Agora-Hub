from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.rag.models import (
    FORBIDDEN_RAG_PLAN_KEYS,
    RetrievalOperation,
    RetrievalPlanCandidate,
    RetrievalStepSpec,
    RAGSessionSpec,
)
from framework.harness.workers.result import HarnessWorkerResult, HarnessWorkerStatus
from framework.shared.graph_identity import GraphExecutionIdentity


@runtime_checkable
class RAGPlanner(Protocol):
    def plan(
        self,
        spec: RAGSessionSpec,
        *,
        round_index: int,
        gap_report: dict[str, Any],
        executed_queries: tuple[str, ...] = (),
    ) -> RetrievalPlanCandidate:
        ...


class DeterministicRAGPlanner:
    def plan(
        self,
        spec: RAGSessionSpec,
        *,
        round_index: int,
        gap_report: dict[str, Any],
        executed_queries: tuple[str, ...] = (),
    ) -> RetrievalPlanCandidate:
        suffix = f"round-{round_index + 1}"
        query_text = spec.goal.question
        missing_evidence_types = tuple(str(item) for item in gap_report.get("missing_evidence_types", ()) if str(item).strip())
        if missing_evidence_types:
            query_text = f"{query_text} {' '.join(missing_evidence_types)}"
        unsupported_claims = _unsupported_claim_texts(gap_report)
        if unsupported_claims:
            query_text = f"{query_text} {' '.join(unsupported_claims)}"
        evidence_type = (
            missing_evidence_types[0]
            if missing_evidence_types
            else spec.goal.required_evidence_types[min(round_index, len(spec.goal.required_evidence_types) - 1)]
        )
        query_step = RetrievalStepSpec(
            step_id=f"{spec.session_id}:query:{suffix}",
            operation=RetrievalOperation.SEARCH_CORPUS,
            query=query_text,
            corpus=spec.allowed_corpora[0],
            max_results=min(5, max(spec.budget.max_queries, 1)),
            metadata={"scope_ref": spec.source_policy.get("scope_ref"), "evidence_type": evidence_type},
        )
        memory_step = RetrievalStepSpec(
            step_id=f"{spec.session_id}:memory:{suffix}",
            operation=RetrievalOperation.RECALL_MEMORY,
            query=query_text,
            memory_namespace=spec.allowed_memory_namespaces[0],
            max_results=_per_round_limit(spec.budget.max_memory_hits, spec.budget.max_rounds, ceiling=3),
            metadata={"scope_ref": spec.source_policy.get("scope_ref")},
        )
        return RetrievalPlanCandidate(
            candidate_id=f"{spec.session_id}:plan:{suffix}",
            queries=(query_step,),
            memory_recall_plan=(memory_step,),
            expected_evidence=spec.goal.required_evidence_types,
            expected_gaps=tuple(gap_report.get("missing_evidence_types", ())),
            risks=("bounded_retrieval_budget",),
            confidence=0.6,
            metadata={"planner": "deterministic"},
        )


class WorkerRAGPlanner:
    def __init__(
        self,
        worker: Any,
        fallback: RAGPlanner | None = None,
        *,
        min_round_index: int = 0,
        max_worker_calls: int | None = None,
        max_candidate_queries: int | None = None,
        timeout_seconds: float | None = None,
        max_transport_attempts: int | None = None,
        fallback_on_error: bool = False,
    ) -> None:
        self.worker = worker
        self.fallback = fallback or DeterministicRAGPlanner()
        self.min_round_index = max(0, int(min_round_index))
        self.max_worker_calls = None if max_worker_calls is None else max(0, int(max_worker_calls))
        self.max_candidate_queries = (
            None if max_candidate_queries is None else max(1, int(max_candidate_queries))
        )
        self.timeout_seconds = None if timeout_seconds is None else float(timeout_seconds)
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise HarnessValidationError("timeout_seconds must be greater than zero")
        self.max_transport_attempts = None if max_transport_attempts is None else max(1, int(max_transport_attempts))
        self.fallback_on_error = bool(fallback_on_error)
        self.requests: list[dict[str, Any]] = []
        self.worker_calls = 0

    def plan(
        self,
        spec: RAGSessionSpec,
        *,
        round_index: int,
        gap_report: dict[str, Any],
        executed_queries: tuple[str, ...] = (),
    ) -> RetrievalPlanCandidate:
        if round_index < self.min_round_index:
            return self._fallback_plan(
                spec,
                round_index=round_index,
                gap_report=gap_report,
                executed_queries=executed_queries,
                reason="before_min_round",
            )
        if self.max_worker_calls is not None and self.worker_calls >= self.max_worker_calls:
            return self._fallback_plan(
                spec,
                round_index=round_index,
                gap_report=gap_report,
                executed_queries=executed_queries,
                reason="worker_budget_exhausted",
            )
        request = {
            "task_type": "rag_plan_candidate",
            "session": spec.to_dict(),
            "round_index": round_index,
            "gap_report": dict(gap_report),
            "executed_queries": list(executed_queries),
            "candidate_query_limit": self.max_candidate_queries,
            "timeout_seconds": self.timeout_seconds,
            "max_transport_attempts": self.max_transport_attempts,
            "forbidden_fields": sorted(FORBIDDEN_RAG_PLAN_KEYS),
        }
        self.requests.append(request)
        execution_identity = (
            spec.graph_identity.to_graph_execution_identity()
            if spec.graph_identity.has_physical_activity
            else None
        )
        self.worker_calls += 1
        try:
            result = self._call_worker(
                request,
                execution_identity=execution_identity,
            )
            if result.status != HarnessWorkerStatus.SUCCEEDED:
                failure_code = str(result.diagnostics.get("failure_code") or "worker_failed")
                if failure_code not in {"worker_failed", "worker_timeout"}:
                    failure_code = "worker_failed"
                return self._fallback_plan(
                    spec,
                    round_index=round_index,
                    gap_report=gap_report,
                    executed_queries=executed_queries,
                    reason=failure_code,
                )
            forbidden = _find_forbidden_fields(result.output)
            if forbidden:
                raise HarnessValidationError(
                    "RAG planner worker returned flow-control fields",
                    details={"forbidden": list(forbidden)},
                )
            candidate_payload = result.output.get("candidate") or result.output.get("retrieval_plan_candidate")
            if candidate_payload is None:
                return self._fallback_plan(
                    spec,
                    round_index=round_index,
                    gap_report=gap_report,
                    executed_queries=executed_queries,
                    reason="candidate_missing",
                )
            candidate = (
                candidate_payload
                if isinstance(candidate_payload, RetrievalPlanCandidate)
                else RetrievalPlanCandidate.from_dict(candidate_payload)
                if isinstance(candidate_payload, dict)
                else None
            )
            if candidate is None:
                raise HarnessValidationError("RAG planner worker returned unsupported candidate payload")
            if self.max_candidate_queries is not None and len(candidate.queries) > self.max_candidate_queries:
                raise HarnessValidationError(
                    "RAG planner worker exceeded candidate query limit",
                    details={
                        "query_count": len(candidate.queries),
                        "limit": self.max_candidate_queries,
                    },
                )
            candidate_forbidden = _find_forbidden_fields(candidate.to_dict())
            if candidate_forbidden:
                raise HarnessValidationError(
                    "RAG planner worker returned flow-control fields",
                    details={"forbidden": list(candidate_forbidden)},
                )
            if "candidate_fallback" in candidate.metadata:
                raise HarnessValidationError(
                    "RAG planner worker returned a reserved fallback marker"
                )
            return candidate
        except TimeoutError as exc:
            return self._fallback_plan(
                spec,
                round_index=round_index,
                gap_report=gap_report,
                executed_queries=executed_queries,
                reason="worker_timeout",
            )
        except Exception as exc:
            if not self.fallback_on_error:
                raise
            return self._fallback_plan(
                spec,
                round_index=round_index,
                gap_report=gap_report,
                executed_queries=executed_queries,
                reason="candidate_invalid",
            )

    def _fallback_plan(
        self,
        spec: RAGSessionSpec,
        *,
        round_index: int,
        gap_report: dict[str, Any],
        executed_queries: tuple[str, ...],
        reason: str,
    ) -> RetrievalPlanCandidate:
        plan = self.fallback.plan(
            spec,
            round_index=round_index,
            gap_report=gap_report,
            executed_queries=executed_queries,
        )
        metadata = dict(plan.metadata)
        metadata["candidate_fallback"] = {
            "reason": reason,
            "original_query": spec.goal.question,
        }
        return replace(plan, metadata=metadata)


    def _call_worker(
        self,
        request: dict[str, Any],
        *,
        execution_identity: GraphExecutionIdentity | None,
    ) -> HarnessWorkerResult:
        generate = getattr(self.worker, "generate", None)
        if callable(generate):
            if execution_identity is None:
                return generate(request)
            return generate(request, execution_identity=execution_identity)
        execute = getattr(self.worker, "execute", None)
        if callable(execute):
            if execution_identity is None:
                return execute(request)
            return execute(request, execution_identity=execution_identity)
        raise HarnessValidationError("RAG planner worker must expose generate or execute")


def _find_forbidden_fields(value: Any, *, path: str = "") -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, (RetrievalPlanCandidate, RetrievalStepSpec)):
        return _find_forbidden_fields(value.to_dict(), path=path)
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            item_path = f"{path}.{key_text}" if path else key_text
            if key_text in FORBIDDEN_RAG_PLAN_KEYS:
                found.append(item_path)
            found.extend(_find_forbidden_fields(item, path=item_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_find_forbidden_fields(item, path=f"{path}[{index}]"))
    return tuple(found)


def _per_round_limit(total: int, rounds: int, *, ceiling: int) -> int:
    return max(1, min(ceiling, total // max(rounds, 1) or total or 1))


def _unsupported_claim_texts(gap_report: dict[str, Any]) -> tuple[str, ...]:
    texts: list[str] = []
    for item in gap_report.get("unsupported_claims", ()):
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
        else:
            text = str(item).strip()
        if text:
            texts.append(text[:160])
    return tuple(dict.fromkeys(texts[:3]))


__all__ = ["DeterministicRAGPlanner", "RAGPlanner", "WorkerRAGPlanner"]
