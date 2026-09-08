from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.memory.ports import ExecutionBoundMemoryPort, MemoryPort
from framework.harness.mcp.policy import MCPToolRequest
from framework.harness.mcp.ports import MCPToolPort
from framework.harness.rag.answer_gate import RAGAnswerGate, unsupported_claims_from_answer_gate
from framework.harness.rag.answer_worker import AnswerWorkerPort
from framework.harness.rag.context_pack_assembler import RAGContextPackAssembler
from framework.harness.rag.gates import RAGGateSuite, RAGGateResult, failed_gate_dicts
from framework.harness.rag.models import (
    EvidenceCandidate,
    GroundedAnswerCandidate,
    RAGBudgetSnapshot,
    RAGContextPack,
    RAGSessionSpec,
    RAGSessionStatus,
    RAGTranscript,
    RetrievalOperation,
    RetrievalPlanCandidate,
    RetrievalStepResult,
    RetrievalStepSpec,
)
from framework.harness.rag.metrics import RAGSessionMetrics, build_rag_session_metrics
from framework.harness.rag.planner import DeterministicRAGPlanner, RAGPlanner
from framework.harness.rag.policy import RAGDecision, RAGDecisionType, RAGExecutionPolicy, normalize_query
from framework.harness.rag.source_verifier import SourceVerifier
from framework.harness.rag.telemetry import RAGTelemetry, RAGTelemetrySpan
from framework.harness.rag.visibility import evidence_visible_to_tenant
from framework.harness.retrieval.ports import RetrievalPort
from framework.harness.retrieval.request import RetrievalRequest
from framework.shared.json import to_jsonable


@runtime_checkable
class RAGSessionController(Protocol):
    def run(self, spec: RAGSessionSpec) -> "RAGSessionResult":
        ...


@dataclass(frozen=True)
class RAGSessionResult:
    status: RAGSessionStatus
    context_pack: RAGContextPack | None
    transcript: RAGTranscript
    decision: RAGDecision
    answer: GroundedAnswerCandidate | None = None
    metrics: RAGSessionMetrics | None = None
    accepted_evidence: tuple[EvidenceCandidate, ...] = ()
    rejected_evidence: tuple[EvidenceCandidate, ...] = ()
    conflicting_evidence: tuple[EvidenceCandidate, ...] = ()
    gap_report: dict[str, Any] = field(default_factory=dict)
    budget_snapshot: RAGBudgetSnapshot = field(default_factory=RAGBudgetSnapshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "context_pack": self.context_pack.to_dict() if self.context_pack else None,
            "transcript": self.transcript.to_dict(),
            "decision": self.decision.to_dict(),
            "answer": self.answer.to_dict() if self.answer else None,
            "metrics": self.metrics.to_dict() if self.metrics else {},
            "accepted_evidence": [item.to_dict() for item in self.accepted_evidence],
            "rejected_evidence": [item.to_dict() for item in self.rejected_evidence],
            "conflicting_evidence": [item.to_dict() for item in self.conflicting_evidence],
            "gap_report": to_jsonable(self.gap_report),
            "budget_snapshot": self.budget_snapshot.to_dict(),
        }


@dataclass
class RAGSessionState:
    spec: RAGSessionSpec
    budget_snapshot: RAGBudgetSnapshot = field(default_factory=RAGBudgetSnapshot)
    executed_queries: set[str] = field(default_factory=set)
    accepted_evidence: list[EvidenceCandidate] = field(default_factory=list)
    rejected_evidence: list[EvidenceCandidate] = field(default_factory=list)
    conflicting_evidence: list[EvidenceCandidate] = field(default_factory=list)
    memory_context: list[dict[str, Any]] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    gap_report: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    telemetry: RAGTelemetrySpan | None = None


class BoundedRAGSessionController(RAGSessionController):
    def __init__(
        self,
        *,
        retrieval: RetrievalPort,
        memory: MemoryPort | None = None,
        tool_port: MCPToolPort | None = None,
        planner: RAGPlanner | None = None,
        source_verifier: SourceVerifier | None = None,
        context_pack_assembler: RAGContextPackAssembler | None = None,
        answer_worker: AnswerWorkerPort | None = None,
        answer_gate: RAGAnswerGate | None = None,
        gates: RAGGateSuite | None = None,
        telemetry: RAGTelemetry | None = None,
    ) -> None:
        self.retrieval = retrieval
        self.memory = memory
        self.tool_port = tool_port
        self.planner = planner or DeterministicRAGPlanner()
        self.source_verifier = source_verifier or SourceVerifier()
        self.context_pack_assembler = context_pack_assembler or RAGContextPackAssembler()
        self.answer_worker = answer_worker
        self.answer_gate = answer_gate or RAGAnswerGate()
        self.gates = gates or RAGGateSuite()
        self.telemetry = telemetry or RAGTelemetry()

    def run(self, spec: RAGSessionSpec) -> RAGSessionResult:
        self._validate_memory_execution(spec)
        policy = RAGExecutionPolicy.from_session_spec(spec)
        with self.telemetry.start_session(spec, policy) as telemetry:
            result = self._run_with_policy(spec, policy, telemetry)
            if result.metrics is not None:
                telemetry.finish_session(status=result.status, decision=result.decision, metrics=result.metrics)
            return result

    def _validate_memory_execution(self, spec: RAGSessionSpec) -> None:
        if self.memory is None:
            return
        if not spec.graph_identity.has_physical_activity:
            if isinstance(self.memory, ExecutionBoundMemoryPort):
                raise HarnessValidationError("bound RAG memory requires a physical Graph execution", code="REF_SNAPSHOT_BINDING_MISMATCH")
            return
        if not isinstance(self.memory, ExecutionBoundMemoryPort):
            raise HarnessValidationError("physical RAG execution requires bound memory", code="REF_UNAUTHORIZED")
        self.memory.validate_execution(spec.graph_identity.to_graph_execution_identity())

    def _run_with_policy(
        self,
        spec: RAGSessionSpec,
        policy: RAGExecutionPolicy,
        telemetry: RAGTelemetrySpan,
    ) -> RAGSessionResult:
        state = RAGSessionState(
            spec=spec,
            gap_report={"missing_evidence_types": list(spec.goal.required_evidence_types)},
            telemetry=telemetry,
        )
        self._event(
            state,
            "rag_session_started",
            {
                "session": spec.to_dict(),
                "policy": policy.to_dict(),
            },
        )
        decision = RAGDecision(RAGDecisionType.CONTINUE_RETRIEVAL, "session started", budget_snapshot=state.budget_snapshot)
        status = RAGSessionStatus.INSUFFICIENT_EVIDENCE
        pack: RAGContextPack | None = None
        answer: GroundedAnswerCandidate | None = None

        for round_index in range(spec.budget.max_rounds):
            state.budget_snapshot = state.budget_snapshot.with_usage(rounds=1)
            budget_gate = self.gates.budget.evaluate(state.budget_snapshot, policy)
            if not budget_gate.passed:
                decision = self._halt(state, "round budget exhausted", (budget_gate,))
                status = RAGSessionStatus.HALTED
                break

            plan = self.planner.plan(
                spec,
                round_index=round_index,
                gap_report=state.gap_report,
                executed_queries=tuple(sorted(state.executed_queries)),
            )
            self._event(state, "rag_plan_candidate_created", {"round_index": round_index, "plan": plan.to_dict()})
            projected = _add_snapshots(state.budget_snapshot, policy.projected_usage(plan))
            plan_results = self.gates.verify_plan(
                plan,
                spec=spec,
                policy=policy,
                executed_queries=state.executed_queries,
                projected_snapshot=projected,
            )
            self._event(state, "rag_plan_verified", {"round_index": round_index, "gate_results": _results(plan_results)})
            if any(result.passed is False for result in plan_results):
                decision = self._handle_plan_failure(state, plan_results)
                if decision.decision_type == RAGDecisionType.REPLAN_QUERIES:
                    continue
                status = RAGSessionStatus.HALTED
                break

            step_results = self._execute_plan(plan, state, policy)
            state.artifact_refs.extend(_dedupe_texts(ref for result in step_results for ref in result.artifact_refs))
            for step_result in step_results:
                self._event(state, "rag_step_executed", step_result.to_dict())
            verification = self.source_verifier.verify(
                tuple(_result_evidence(step_results)),
                policy=policy,
                question=spec.goal.question,
            )
            state.accepted_evidence.extend(_dedupe_evidence(verification.accepted, state.accepted_evidence))
            state.rejected_evidence.extend(_dedupe_evidence(verification.rejected, state.rejected_evidence))
            state.conflicting_evidence.extend(_dedupe_evidence(verification.conflicting, state.conflicting_evidence))
            self._event(state, "rag_source_verified", verification.to_dict())

            source_results = self.gates.verify_sources(
                tuple(state.accepted_evidence),
                spec=spec,
                policy=policy,
                memory_context=tuple(state.memory_context),
            )
            self._record_gate_failures(state, source_results)
            state.gap_report = _gap_report(
                spec,
                tuple(state.accepted_evidence),
                source_results,
                rejected=tuple(state.rejected_evidence),
            )
            if _coverage_passed(source_results):
                decision = RAGDecision(
                    RAGDecisionType.ASSEMBLE_CONTEXT,
                    "required evidence coverage satisfied",
                    gate_results=_results(source_results),
                    budget_snapshot=state.budget_snapshot,
                )
                break
            if self._can_replan(state, spec):
                state.budget_snapshot = state.budget_snapshot.with_usage(replans=1)
                decision = RAGDecision(
                    RAGDecisionType.REPLAN_QUERIES,
                    "evidence coverage is incomplete and replan budget remains",
                    gate_results=_results(source_results),
                    budget_snapshot=state.budget_snapshot,
                    metadata={"gap_report": to_jsonable(state.gap_report)},
                )
                self._event(state, "rag_replanned", decision.to_dict())
                continue
            decision = RAGDecision(
                RAGDecisionType.INSUFFICIENT_EVIDENCE,
                "evidence coverage is incomplete and no controlled replan remains",
                gate_results=_results(source_results),
                budget_snapshot=state.budget_snapshot,
                metadata={"gap_report": to_jsonable(state.gap_report)},
            )
            status = RAGSessionStatus.INSUFFICIENT_EVIDENCE
            break

        if decision.decision_type == RAGDecisionType.ASSEMBLE_CONTEXT:
            pack = self.context_pack_assembler.assemble(
                spec=spec,
                accepted_evidence=tuple(state.accepted_evidence[: spec.budget.max_context_items]),
                rejected_evidence=tuple(state.rejected_evidence),
                conflicting_evidence=tuple(state.conflicting_evidence),
                memory_context=tuple(state.memory_context[: spec.budget.max_memory_hits]),
                artifact_refs=_dedupe_texts(state.artifact_refs),
                gap_report=state.gap_report,
                budget_snapshot=state.budget_snapshot,
                policy=policy,
            )
            if pack.budget_snapshot is not None:
                state.budget_snapshot = pack.budget_snapshot
            pack_results = self.gates.verify_context_pack(pack, policy=policy)
            self._event(state, "rag_context_pack_assembled", {"pack": pack.to_dict(), "gate_results": _results(pack_results)})
            self._record_gate_failures(state, pack_results)
            if all(result.passed for result in pack_results):
                if policy.generation_enabled and self.answer_worker is not None:
                    answer, decision, status, pack = self._run_generation_phase(state, spec, policy, pack)
                else:
                    decision = RAGDecision(
                        RAGDecisionType.RETURN_CONTEXT_PACK,
                        "context pack verified",
                        gate_results=_results(pack_results),
                        budget_snapshot=pack.budget_snapshot,
                        metadata={"context_pack_id": pack.pack_id},
                    )
                    status = RAGSessionStatus.SUCCEEDED
                    self._event(state, "rag_context_pack_returned", decision.to_dict())
            else:
                decision = self._halt(state, "context pack gate failed", pack_results)
                status = RAGSessionStatus.HALTED
                pack = None
        elif decision.decision_type == RAGDecisionType.HALTED:
            status = RAGSessionStatus.HALTED
        else:
            self._event(state, "rag_halted", decision.to_dict())

        transcript = RAGTranscript(
            transcript_id=f"rag-transcript://{spec.session_id}/{uuid4().hex[:8]}",
            session_id=spec.session_id,
            events=tuple(state.events),
            status=status,
        )
        metrics = build_rag_session_metrics(
            status=status,
            decision=decision,
            events=tuple(state.events),
            budget_snapshot=state.budget_snapshot,
            accepted_evidence=tuple(state.accepted_evidence),
            rejected_evidence=tuple(state.rejected_evidence),
            conflicting_evidence=tuple(state.conflicting_evidence),
            memory_context=tuple(state.memory_context),
            artifact_refs=tuple(state.artifact_refs),
            answer=answer,
        )
        return RAGSessionResult(
            status=status,
            context_pack=pack,
            transcript=transcript,
            decision=decision,
            answer=answer,
            metrics=metrics,
            accepted_evidence=tuple(state.accepted_evidence),
            rejected_evidence=tuple(state.rejected_evidence),
            conflicting_evidence=tuple(state.conflicting_evidence),
            gap_report=dict(state.gap_report),
            budget_snapshot=state.budget_snapshot,
        )

    def _run_generation_phase(
        self,
        state: RAGSessionState,
        spec: RAGSessionSpec,
        policy: RAGExecutionPolicy,
        pack: RAGContextPack,
    ) -> tuple[GroundedAnswerCandidate | None, RAGDecision, RAGSessionStatus, RAGContextPack]:
        if self.answer_worker is None:
            decision = RAGDecision(
                RAGDecisionType.RETURN_CONTEXT_PACK,
                "context pack verified",
                budget_snapshot=pack.budget_snapshot,
                metadata={"context_pack_id": pack.pack_id},
            )
            return None, decision, RAGSessionStatus.SUCCEEDED, pack
        current_pack = pack
        max_attempts = policy.max_generation_attempts
        execution_identity = (
            spec.graph_identity.to_graph_execution_identity()
            if spec.graph_identity.has_physical_activity
            else None
        )
        for attempt_index in range(max_attempts):
            if execution_identity is None:
                candidate = self.answer_worker.generate_answer(
                    question=spec.goal.question,
                    pack=current_pack,
                )
            else:
                candidate = self.answer_worker.generate_answer(
                    question=spec.goal.question,
                    pack=current_pack,
                    execution_identity=execution_identity,
                )
            self._event(
                state,
                "rag_answer_candidate_created",
                {
                    **candidate.to_dict(),
                    "generation_attempt": attempt_index + 1,
                    "max_generation_attempts": max_attempts,
                },
            )
            answer_results = self.answer_gate.evaluate(candidate, current_pack)
            self._event(
                state,
                "rag_answer_verified",
                {
                    "generation_attempt": attempt_index + 1,
                    "max_generation_attempts": max_attempts,
                    "gate_results": _results(answer_results),
                },
            )
            if all(result.passed for result in answer_results):
                if candidate.abstained:
                    decision = RAGDecision(
                        RAGDecisionType.ABSTAIN,
                        "answer worker produced a verified abstention",
                        gate_results=_results(answer_results),
                        budget_snapshot=current_pack.budget_snapshot,
                        metadata={"context_pack_id": current_pack.pack_id, "answer_id": candidate.answer_id},
                    )
                    self._event(state, "rag_abstained", decision.to_dict())
                    return candidate, decision, RAGSessionStatus.ABSTAINED, current_pack
                decision = RAGDecision(
                    RAGDecisionType.RETURN_ANSWER,
                    "answer verified",
                    gate_results=_results(answer_results),
                    budget_snapshot=current_pack.budget_snapshot,
                    metadata={"context_pack_id": current_pack.pack_id, "answer_id": candidate.answer_id},
                )
                self._event(state, "rag_answer_returned", decision.to_dict())
                return candidate, decision, RAGSessionStatus.ANSWERED, current_pack

            unsupported_claims = unsupported_claims_from_answer_gate(answer_results)
            if unsupported_claims and attempt_index + 1 < max_attempts:
                supplemental_pack = self._run_supplemental_round(
                    state,
                    spec,
                    policy,
                    unsupported_claims=unsupported_claims,
                    failed_answer_results=answer_results,
                    attempt_index=attempt_index,
                )
                if supplemental_pack is not None:
                    current_pack = supplemental_pack
                    continue

            decision = RAGDecision(
                RAGDecisionType.ABSTAIN,
                "answer gate failed",
                gate_results=failed_gate_dicts(answer_results),
                budget_snapshot=state.budget_snapshot,
                metadata={
                    "context_pack_id": current_pack.pack_id,
                    "answer_id": candidate.answer_id,
                    "generation_attempt": attempt_index + 1,
                    "max_generation_attempts": max_attempts,
                    "unsupported_claims": list(unsupported_claims),
                },
            )
            self._event(state, "rag_abstained", decision.to_dict())
            return candidate, decision, RAGSessionStatus.ABSTAINED, current_pack

        raise AssertionError("generation loop must return from an answer attempt")

    def _run_supplemental_round(
        self,
        state: RAGSessionState,
        spec: RAGSessionSpec,
        policy: RAGExecutionPolicy,
        *,
        unsupported_claims: tuple[dict[str, Any], ...],
        failed_answer_results: tuple[RAGGateResult, ...],
        attempt_index: int,
    ) -> RAGContextPack | None:
        generation_attempt = attempt_index + 1
        state.gap_report = _with_unsupported_claims(state.gap_report, unsupported_claims)
        self._event(
            state,
            "rag_answer_supplemental_gap_created",
            {
                "generation_attempt": generation_attempt,
                "unsupported_claims": list(unsupported_claims),
                "gate_results": failed_gate_dicts(failed_answer_results),
            },
        )
        skip_reason_code = _supplemental_skip_reason_code(state, policy)
        if skip_reason_code:
            self._event(
                state,
                "rag_answer_supplemental_round_skipped",
                {
                    "generation_attempt": generation_attempt,
                    "reason": _supplemental_skip_reason(skip_reason_code),
                    "reason_code": skip_reason_code,
                    "budget_snapshot": state.budget_snapshot.to_dict(),
                    "max_supplemental_rounds": policy.max_supplemental_rounds,
                    "supplemental_rounds_started": _event_count(
                        state,
                        "rag_answer_supplemental_round_started",
                    ),
                },
            )
            return None

        round_index = state.budget_snapshot.rounds_used + _event_count(
            state,
            "rag_answer_supplemental_round_started",
        )
        self._event(
            state,
            "rag_answer_supplemental_round_started",
            {
                "generation_attempt": generation_attempt,
                "round_index": round_index,
                "gap_report": to_jsonable(state.gap_report),
            },
        )
        plan = self.planner.plan(
            spec,
            round_index=round_index,
            gap_report=state.gap_report,
            executed_queries=tuple(sorted(state.executed_queries)),
        )
        self._event(
            state,
            "rag_plan_candidate_created",
            {"round_index": round_index, "supplemental": True, "plan": plan.to_dict()},
        )
        projected = _add_snapshots(state.budget_snapshot, policy.projected_usage(plan))
        plan_results = self.gates.verify_plan(
            plan,
            spec=spec,
            policy=policy,
            executed_queries=state.executed_queries,
            projected_snapshot=projected,
        )
        self._event(
            state,
            "rag_plan_verified",
            {"round_index": round_index, "supplemental": True, "gate_results": _results(plan_results)},
        )
        if any(result.passed is False for result in plan_results):
            self._record_gate_failures(state, plan_results)
            self._event(
                state,
                "rag_answer_supplemental_round_failed",
                {
                    "generation_attempt": generation_attempt,
                    "reason": "supplemental plan gate failed",
                    "gate_results": failed_gate_dicts(plan_results),
                },
            )
            return None

        step_results = self._execute_plan(plan, state, policy)
        state.artifact_refs.extend(_dedupe_texts(ref for result in step_results for ref in result.artifact_refs))
        for step_result in step_results:
            self._event(state, "rag_step_executed", {**step_result.to_dict(), "supplemental": True})
        verification = self.source_verifier.verify(
            tuple(_result_evidence(step_results)),
            policy=policy,
            question=spec.goal.question,
        )
        state.accepted_evidence.extend(_dedupe_evidence(verification.accepted, state.accepted_evidence))
        state.rejected_evidence.extend(_dedupe_evidence(verification.rejected, state.rejected_evidence))
        state.conflicting_evidence.extend(_dedupe_evidence(verification.conflicting, state.conflicting_evidence))
        self._event(state, "rag_source_verified", {**verification.to_dict(), "supplemental": True})

        source_results = self.gates.verify_sources(
            tuple(state.accepted_evidence),
            spec=spec,
            policy=policy,
            memory_context=tuple(state.memory_context),
        )
        self._record_gate_failures(state, source_results)
        state.gap_report = _with_unsupported_claims(
            _gap_report(
                spec,
                tuple(state.accepted_evidence),
                source_results,
                rejected=tuple(state.rejected_evidence),
            ),
            unsupported_claims,
        )
        if not _coverage_passed(source_results):
            self._event(
                state,
                "rag_answer_supplemental_round_failed",
                {
                    "generation_attempt": generation_attempt,
                    "reason": "supplemental evidence coverage incomplete",
                    "gap_report": to_jsonable(state.gap_report),
                    "gate_results": failed_gate_dicts(source_results),
                },
            )
            return None

        pack = self.context_pack_assembler.assemble(
            spec=spec,
            accepted_evidence=tuple(state.accepted_evidence[: spec.budget.max_context_items]),
            rejected_evidence=tuple(state.rejected_evidence),
            conflicting_evidence=tuple(state.conflicting_evidence),
            memory_context=tuple(state.memory_context[: spec.budget.max_memory_hits]),
            artifact_refs=_dedupe_texts(state.artifact_refs),
            gap_report=state.gap_report,
            budget_snapshot=state.budget_snapshot,
            policy=policy,
        )
        pack_results = self.gates.verify_context_pack(pack, policy=policy)
        self._event(
            state,
            "rag_context_pack_assembled",
            {"supplemental": True, "pack": pack.to_dict(), "gate_results": _results(pack_results)},
        )
        self._record_gate_failures(state, pack_results)
        if not all(result.passed for result in pack_results):
            self._event(
                state,
                "rag_answer_supplemental_round_failed",
                {
                    "generation_attempt": generation_attempt,
                    "reason": "supplemental context pack gate failed",
                    "gate_results": failed_gate_dicts(pack_results),
                },
            )
            return None

        self._event(
            state,
            "rag_answer_supplemental_round_completed",
            {
                "generation_attempt": generation_attempt,
                "context_pack_id": pack.pack_id,
                "accepted_evidence_ids": [item.evidence_id for item in pack.accepted_evidence],
            },
        )
        return pack

    def _execute_plan(
        self,
        plan: RetrievalPlanCandidate,
        state: RAGSessionState,
        policy: RAGExecutionPolicy,
    ) -> tuple[RetrievalStepResult, ...]:
        results: list[RetrievalStepResult] = []
        for step in plan.steps:
            with self.telemetry.start_step(step, state.spec) as step_span:
                if step.operation == RetrievalOperation.SEARCH_CORPUS:
                    result = self._execute_search(step, state)
                    state.budget_snapshot = state.budget_snapshot.with_usage(queries=1, worker_calls=1)
                elif step.operation == RetrievalOperation.READ_SOURCE:
                    result = self._execute_source_read(step, state)
                    state.budget_snapshot = state.budget_snapshot.with_usage(
                        source_reads=max(len(step.source_refs), step.max_source_reads, 1),
                        worker_calls=1,
                    )
                elif step.operation == RetrievalOperation.RECALL_MEMORY:
                    result = self._execute_memory_recall(step, state)
                    state.budget_snapshot = state.budget_snapshot.with_usage(
                        memory_hits=len(result.memory_refs),
                        worker_calls=1,
                    )
                else:
                    result = RetrievalStepResult(
                        step_id=step.step_id,
                        operation=step.operation,
                        graph_identity=state.spec.graph_identity,
                        errors=(f"operation {step.operation.value} is verified but not directly executed",),
                    )
                budget_result = self.gates.budget.evaluate(state.budget_snapshot, policy)
                step_span.finish_step(result)
            results.append(result)
            if not budget_result.passed:
                self._event(state, "rag_gate_failed", budget_result.to_dict())
                break
        return tuple(results)

    def _execute_search(self, step: RetrievalStepSpec, state: RAGSessionState) -> RetrievalStepResult:
        query = str(step.query or "")
        state.executed_queries.add(normalize_query(query))
        scope_metadata = _session_scope_metadata(state.spec)
        filters = dict(step.metadata.get("filters", {}))
        for key in ("tenant_id", "tenant", "workspace_id", "tenant_ids", "allowed_tenant_ids"):
            filters.pop(key, None)
        filters["tenant_id"] = scope_metadata.get("tenant_id") or None
        for key in ("paper_id", "run_id", "user_id"):
            if scope_metadata.get(key):
                filters[key] = scope_metadata[key]
        collection = self.retrieval.retrieve(
            RetrievalRequest(
                query=query,
                scope=step.corpus or "default",
                filters=filters,
                limit=max(step.max_results, 1),
                context_refs=state.spec.goal.known_context_refs,
                metadata={**dict(step.metadata), "rag_step_id": step.step_id, **scope_metadata},
            )
        )
        evidence_type = str(step.metadata.get("evidence_type") or _default_evidence_type(state.spec))
        artifact_refs = (collection.request_ref,) if collection.request_ref else ()
        tenant_id = scope_metadata.get("tenant_id") or None
        candidates = tuple(
            EvidenceCandidate.from_evidence_pack(
                pack,
                evidence_type=evidence_type,
                artifact_refs=artifact_refs,
            )
            for pack in collection.packs
        )
        candidates = tuple(
            candidate
            for candidate in candidates
            if evidence_visible_to_tenant(candidate, tenant_id=tenant_id)
        )
        return RetrievalStepResult(
            step_id=step.step_id,
            operation=step.operation,
            graph_identity=state.spec.graph_identity,
            items=candidates,
            source_refs=tuple(ref for item in candidates for ref in (item.source_ref, *item.span_refs)),
            artifact_refs=artifact_refs,
            metadata=collection.metadata,
        )

    def _execute_source_read(self, step: RetrievalStepSpec, state: RAGSessionState) -> RetrievalStepResult:
        if self.tool_port is not None and step.metadata.get("tool_name"):
            request = MCPToolRequest(
                tool_name=str(step.metadata["tool_name"]),
                arguments={"source_refs": list(step.source_refs), "query": step.query},
                timeout_seconds=step.timeout_seconds,
                metadata={"rag_step_id": step.step_id},
            )
            result = self.tool_port.call_tool(request)
            errors = () if result.status.value == "succeeded" else (result.error or "tool call failed",)
            return RetrievalStepResult(
                step_id=step.step_id,
                operation=step.operation,
                graph_identity=state.spec.graph_identity,
                source_refs=step.source_refs,
                artifact_refs=tuple(result.artifacts),
                errors=errors,
                metadata={"tool_result": result.to_dict()},
            )
        return RetrievalStepResult(
            step_id=step.step_id,
            operation=step.operation,
            graph_identity=state.spec.graph_identity,
            source_refs=step.source_refs,
            metadata={"read_mode": "source_refs_only"},
        )

    def _execute_memory_recall(self, step: RetrievalStepSpec, state: RAGSessionState) -> RetrievalStepResult:
        if self.memory is None:
            return RetrievalStepResult(
                step_id=step.step_id,
                operation=step.operation,
                graph_identity=state.spec.graph_identity,
                errors=("memory port is not configured",),
            )
        self._validate_memory_execution(state.spec)
        hits = self.memory.recall(
            {
                "query": step.query,
                "namespace": step.memory_namespace,
                "limit": step.max_results,
                "goal": state.spec.goal.to_dict(),
                "tenant_id": state.spec.source_policy.get("tenant_id"),
                "owner_id": _session_scope_metadata(state.spec).get("user_id"),
                "execution_identity": (
                    state.spec.graph_identity.to_graph_execution_identity().to_dict()
                    if state.spec.graph_identity.has_physical_activity else None
                ),
            }
        )
        accepted_hits = []
        memory_refs = []
        for hit in hits[: step.max_results]:
            if str(hit.get("namespace")) != step.memory_namespace:
                continue
            accepted_hits.append(dict(hit))
            memory_refs.append(str(hit.get("memory_ref", hit.get("ref", f"memory://{len(memory_refs) + 1}"))))
        state.memory_context.extend(accepted_hits)
        return RetrievalStepResult(
            step_id=step.step_id,
            operation=step.operation,
            graph_identity=state.spec.graph_identity,
            items=tuple(accepted_hits),
            memory_refs=tuple(memory_refs),
            metadata={"namespace": step.memory_namespace, "returned": len(hits), "accepted": len(accepted_hits)},
        )

    def _handle_plan_failure(self, state: RAGSessionState, results: tuple[RAGGateResult, ...]) -> RAGDecision:
        self._record_gate_failures(state, results)
        if state.budget_snapshot.replans_used < state.spec.budget.max_replans:
            state.budget_snapshot = state.budget_snapshot.with_usage(replans=1)
            decision = RAGDecision(
                RAGDecisionType.REPLAN_QUERIES,
                "plan gate failed and replan budget remains",
                gate_results=failed_gate_dicts(results),
                budget_snapshot=state.budget_snapshot,
            )
            self._event(state, "rag_replanned", decision.to_dict())
            return decision
        return self._halt(state, "plan gate failed and no replan budget remains", results)

    def _can_replan(self, state: RAGSessionState, spec: RAGSessionSpec) -> bool:
        return (
            state.budget_snapshot.replans_used < spec.budget.max_replans
            and state.budget_snapshot.rounds_used < spec.budget.max_rounds
        )

    def _halt(self, state: RAGSessionState, reason: str, results: tuple[RAGGateResult, ...]) -> RAGDecision:
        decision = RAGDecision(
            RAGDecisionType.HALTED,
            reason,
            gate_results=failed_gate_dicts(results) or _results(results),
            budget_snapshot=state.budget_snapshot,
        )
        self._event(state, "rag_halted", decision.to_dict())
        return decision

    def _record_gate_failures(self, state: RAGSessionState, results: tuple[RAGGateResult, ...]) -> None:
        for result in results:
            if not result.passed:
                self._event(state, "rag_gate_failed", result.to_dict())

    def _event(self, state: RAGSessionState, event_type: str, payload: dict[str, Any]) -> None:
        json_payload = to_jsonable(payload)
        state.events.append({"event_type": event_type, "payload": json_payload})
        if state.telemetry is not None:
            state.telemetry.add_event(event_type, json_payload)


def _add_snapshots(current: RAGBudgetSnapshot, projected: RAGBudgetSnapshot) -> RAGBudgetSnapshot:
    return current.with_usage(
        rounds=0,
        replans=0,
        queries=projected.queries_used,
        source_reads=projected.source_reads_used,
        memory_hits=projected.memory_hits_used,
        context_items=projected.context_items_used,
        context_tokens=projected.context_tokens_used,
        worker_calls=projected.worker_calls_used,
    )


def _result_evidence(results: tuple[RetrievalStepResult, ...]) -> tuple[EvidenceCandidate, ...]:
    evidence: list[EvidenceCandidate] = []
    for result in results:
        for item in result.items:
            if isinstance(item, EvidenceCandidate):
                evidence.append(item)
    return tuple(evidence)


def _dedupe_evidence(
    candidates: tuple[EvidenceCandidate, ...],
    existing: list[EvidenceCandidate],
) -> tuple[EvidenceCandidate, ...]:
    seen = {item.evidence_id for item in existing}
    deduped = []
    for candidate in candidates:
        if candidate.evidence_id in seen:
            continue
        seen.add(candidate.evidence_id)
        deduped.append(candidate)
    return tuple(deduped)


def _dedupe_texts(values: Any) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out)


def _coverage_passed(results: tuple[RAGGateResult, ...]) -> bool:
    return all(result.passed for result in results if result.gate_name in {"rag_evidence_coverage", "rag_lineage", "rag_source_quality", "rag_memory_relevance"})


def _gap_report(
    spec: RAGSessionSpec,
    accepted: tuple[EvidenceCandidate, ...],
    results: tuple[RAGGateResult, ...],
    rejected: tuple[EvidenceCandidate, ...] = (),
) -> dict[str, Any]:
    present = {item.evidence_type for item in accepted}
    missing = [item for item in spec.goal.required_evidence_types if item not in present]
    return {
        "missing_evidence_types": missing,
        "accepted_evidence_ids": [item.evidence_id for item in accepted],
        "gate_results": _results(results),
        "rejection_summary": _rejection_summary(rejected),
    }


def _with_unsupported_claims(
    gap_report: dict[str, Any],
    unsupported_claims: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    return {
        **dict(gap_report),
        "unsupported_claims": [dict(item) for item in unsupported_claims],
    }


def _results(results: tuple[RAGGateResult, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(result.to_dict() for result in results)


def _supplemental_skip_reason_code(state: RAGSessionState, policy: RAGExecutionPolicy) -> str:
    if _event_count(state, "rag_answer_supplemental_round_started") >= policy.max_supplemental_rounds:
        return "supplemental_round_budget_exhausted"
    return ""


def _supplemental_skip_reason(reason_code: str) -> str:
    if reason_code == "supplemental_round_budget_exhausted":
        return "supplemental round budget exhausted"
    return reason_code.replace("_", " ")


def _event_count(state: RAGSessionState, event_type: str) -> int:
    return sum(1 for event in state.events if event.get("event_type") == event_type)


def _rejection_summary(rejected: tuple[EvidenceCandidate, ...]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for candidate in rejected:
        reason = str(candidate.metadata.get("rejection_reason") or "source_verification_failed")
        entry = summary.setdefault(reason, {"count": 0, "evidence_types": {}})
        entry["count"] += 1
        evidence_types = entry["evidence_types"]
        evidence_types[candidate.evidence_type] = evidence_types.get(candidate.evidence_type, 0) + 1
    return summary


def _default_evidence_type(spec: RAGSessionSpec) -> str:
    return spec.goal.required_evidence_types[0]


def _session_scope_metadata(spec: RAGSessionSpec) -> dict[str, str]:
    values: dict[str, str] = {"run_id": spec.run_id}
    paper_id = spec.metadata.get("paper_id") or spec.goal.metadata.get("paper_id")
    if str(paper_id or "").strip():
        values["paper_id"] = str(paper_id).strip()
    for key in ("tenant_id", "user_id", "memory_namespace"):
        raw = spec.metadata.get(key) or spec.goal.metadata.get(key)
        text = str(raw or "").strip()
        if text:
            values[key] = text
    return values


def _empty_context_pack(spec: RAGSessionSpec) -> RAGContextPack:
    return RAGContextPack(
        pack_id=f"rag-context://{spec.session_id}/empty",
        query=spec.goal.question,
        goal=spec.goal,
        gap_report={"missing_evidence_types": list(spec.goal.required_evidence_types)},
        budget_snapshot=RAGBudgetSnapshot(),
        assembly_summary="No verified context pack was produced.",
        metadata={"status": RAGSessionStatus.INSUFFICIENT_EVIDENCE.value},
    )


__all__ = [
    "BoundedRAGSessionController",
    "RAGSessionController",
    "RAGSessionResult",
    "RAGSessionState",
]
