from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from framework.harness.context.models import ContextGraphIdentity
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.retrieval.evidence_pack import EvidencePack
from framework.shared.json import stable_json_dumps, to_jsonable
from framework.shared.time import format_datetime, utc_now


FORBIDDEN_RAG_PLAN_KEYS = frozenset(
    {
        "allowed_tools",
        "eligible",
        "eligibility",
        "eligibility_decision",
        "halt_graph",
        "memory_write",
        "next_step",
        "passed",
        "promote_skill",
        "publication",
        "qualification",
        "qualification_ref",
        "qualification_status",
        "publish",
        "publish_artifact",
        "quality_decision",
        "quality_passed",
        "route",
        "routing",
        "skill_promotion",
        "tool_authorization",
        "write_memory",
    }
)


class RetrievalOperation(StrEnum):
    SEARCH_CORPUS = "search_corpus"
    READ_SOURCE = "read_source"
    RECALL_MEMORY = "recall_memory"
    VERIFY_SOURCE = "verify_source"
    CHECK_GAP = "check_gap"
    ASSEMBLE_CONTEXT = "assemble_context"


class RAGSessionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    ANSWERED = "answered"
    ABSTAINED = "abstained"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    HALTED = "halted"
    FAILED = "failed"


@dataclass(frozen=True)
class RAGBudget:
    max_rounds: int
    max_replans: int
    max_queries: int
    max_source_reads: int
    max_memory_hits: int
    max_context_items: int
    max_context_tokens: int
    max_worker_calls: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_rounds",
            "max_replans",
            "max_queries",
            "max_source_reads",
            "max_memory_hits",
            "max_context_items",
            "max_context_tokens",
            "max_worker_calls",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise HarnessValidationError(f"{field_name} must be a non-negative integer")
        if self.max_rounds <= 0:
            raise HarnessValidationError("max_rounds must be greater than zero")

    @classmethod
    def safe_default(cls) -> "RAGBudget":
        return cls(
            max_rounds=2,
            max_replans=1,
            max_queries=4,
            max_source_reads=8,
            max_memory_hits=4,
            max_context_items=8,
            max_context_tokens=2048,
            max_worker_calls=8,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_rounds": self.max_rounds,
            "max_replans": self.max_replans,
            "max_queries": self.max_queries,
            "max_source_reads": self.max_source_reads,
            "max_memory_hits": self.max_memory_hits,
            "max_context_items": self.max_context_items,
            "max_context_tokens": self.max_context_tokens,
            "max_worker_calls": self.max_worker_calls,
        }


@dataclass(frozen=True)
class RetrievalGoal:
    goal_id: str
    question: str
    required_evidence_types: tuple[str, ...]
    target_entities: tuple[str, ...] = ()
    known_context_refs: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    constraints: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.goal_id).strip():
            raise HarnessValidationError("goal_id is required")
        if not str(self.question).strip():
            raise HarnessValidationError("question is required")
        if not self.required_evidence_types:
            raise HarnessValidationError("required_evidence_types are required")
        object.__setattr__(self, "required_evidence_types", tuple(str(item) for item in self.required_evidence_types))
        object.__setattr__(self, "target_entities", tuple(str(item) for item in self.target_entities))
        object.__setattr__(self, "known_context_refs", tuple(str(ref) for ref in self.known_context_refs))
        object.__setattr__(self, "missing_information", tuple(str(item) for item in self.missing_information))
        object.__setattr__(self, "constraints", dict(self.constraints))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "question": self.question,
            "required_evidence_types": list(self.required_evidence_types),
            "target_entities": list(self.target_entities),
            "known_context_refs": list(self.known_context_refs),
            "missing_information": list(self.missing_information),
            "constraints": to_jsonable(self.constraints),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True)
class RAGSessionSpec:
    session_id: str
    graph_identity: ContextGraphIdentity
    goal: RetrievalGoal
    allowed_corpora: tuple[str, ...]
    allowed_memory_namespaces: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    source_policy: dict[str, Any] = field(default_factory=dict)
    budget: RAGBudget = field(default_factory=RAGBudget.safe_default)
    context_policy: dict[str, Any] = field(default_factory=dict)
    generation_policy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.session_id).strip():
            raise HarnessValidationError("session_id is required")
        if not isinstance(self.graph_identity, ContextGraphIdentity):
            raise HarnessValidationError(
                "RAGSessionSpec requires an exact Graph identity",
                code="rag_graph_identity_required",
            )
        if not isinstance(self.goal, RetrievalGoal):
            raise HarnessValidationError("goal must be RetrievalGoal")
        if not self.allowed_corpora:
            raise HarnessValidationError("allowed_corpora must be explicitly declared")
        if not self.allowed_memory_namespaces:
            raise HarnessValidationError("allowed_memory_namespaces must be explicitly declared")
        if not self.allowed_tools:
            raise HarnessValidationError("allowed_tools must be explicitly declared")
        if not isinstance(self.budget, RAGBudget):
            raise HarnessValidationError("budget must be RAGBudget")
        object.__setattr__(self, "allowed_corpora", tuple(str(item) for item in self.allowed_corpora))
        object.__setattr__(
            self,
            "allowed_memory_namespaces",
            tuple(str(item) for item in self.allowed_memory_namespaces),
        )
        object.__setattr__(self, "allowed_tools", tuple(str(item) for item in self.allowed_tools))
        object.__setattr__(self, "source_policy", dict(self.source_policy))
        object.__setattr__(self, "context_policy", dict(self.context_policy))
        object.__setattr__(self, "generation_policy", dict(self.generation_policy))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "graph_identity": self.graph_identity.to_dict(),
            "goal": self.goal.to_dict(),
            "allowed_corpora": list(self.allowed_corpora),
            "allowed_memory_namespaces": list(self.allowed_memory_namespaces),
            "allowed_tools": list(self.allowed_tools),
            "source_policy": to_jsonable(self.source_policy),
            "budget": self.budget.to_dict(),
            "context_policy": to_jsonable(self.context_policy),
            "generation_policy": to_jsonable(self.generation_policy),
            "metadata": to_jsonable(self.metadata),
        }

    @property
    def run_id(self) -> str:
        return self.graph_identity.run_id

    @property
    def stage_id(self) -> str:
        return self.graph_identity.stage_id


@dataclass(frozen=True)
class RetrievalStepSpec:
    step_id: str
    operation: RetrievalOperation | str
    query: str | None = None
    corpus: str | None = None
    memory_namespace: str | None = None
    source_refs: tuple[str, ...] = ()
    max_results: int = 5
    max_source_reads: int = 0
    timeout_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.step_id).strip():
            raise HarnessValidationError("step_id is required")
        if self.max_results < 0 or self.max_source_reads < 0:
            raise HarnessValidationError("max_results and max_source_reads must not be negative")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise HarnessValidationError("timeout_seconds must be greater than zero")
        object.__setattr__(self, "operation", RetrievalOperation(self.operation))
        object.__setattr__(self, "source_refs", tuple(str(ref) for ref in self.source_refs))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "operation": self.operation.value,
            "query": self.query,
            "corpus": self.corpus,
            "memory_namespace": self.memory_namespace,
            "source_refs": list(self.source_refs),
            "max_results": self.max_results,
            "max_source_reads": self.max_source_reads,
            "timeout_seconds": self.timeout_seconds,
            "metadata": to_jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RetrievalStepSpec":
        return cls(
            step_id=str(payload.get("step_id", "")),
            operation=payload.get("operation", ""),
            query=payload.get("query"),
            corpus=payload.get("corpus"),
            memory_namespace=payload.get("memory_namespace"),
            source_refs=tuple(payload.get("source_refs", ())),
            max_results=int(payload.get("max_results", 5)),
            max_source_reads=int(payload.get("max_source_reads", 0)),
            timeout_seconds=payload.get("timeout_seconds"),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class RetrievalPlanCandidate:
    candidate_id: str
    queries: tuple[RetrievalStepSpec, ...]
    source_reading_plan: tuple[RetrievalStepSpec, ...] = ()
    memory_recall_plan: tuple[RetrievalStepSpec, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    expected_gaps: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.candidate_id).strip():
            raise HarnessValidationError("candidate_id is required")
        if not self.queries:
            raise HarnessValidationError("queries are required")
        if not 0 <= self.confidence <= 1:
            raise HarnessValidationError("confidence must be between 0 and 1")
        forbidden = sorted(FORBIDDEN_RAG_PLAN_KEYS.intersection(self.metadata))
        if forbidden:
            raise HarnessValidationError("RetrievalPlanCandidate contains flow-control fields", details={"forbidden": forbidden})
        object.__setattr__(
            self,
            "queries",
            tuple(step if isinstance(step, RetrievalStepSpec) else RetrievalStepSpec.from_dict(step) for step in self.queries),
        )
        object.__setattr__(
            self,
            "source_reading_plan",
            tuple(
                step if isinstance(step, RetrievalStepSpec) else RetrievalStepSpec.from_dict(step)
                for step in self.source_reading_plan
            ),
        )
        object.__setattr__(
            self,
            "memory_recall_plan",
            tuple(
                step if isinstance(step, RetrievalStepSpec) else RetrievalStepSpec.from_dict(step)
                for step in self.memory_recall_plan
            ),
        )
        object.__setattr__(self, "expected_evidence", tuple(str(item) for item in self.expected_evidence))
        object.__setattr__(self, "expected_gaps", tuple(str(item) for item in self.expected_gaps))
        object.__setattr__(self, "risks", tuple(str(item) for item in self.risks))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def steps(self) -> tuple[RetrievalStepSpec, ...]:
        return self.queries + self.source_reading_plan + self.memory_recall_plan

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "queries": [step.to_dict() for step in self.queries],
            "source_reading_plan": [step.to_dict() for step in self.source_reading_plan],
            "memory_recall_plan": [step.to_dict() for step in self.memory_recall_plan],
            "expected_evidence": list(self.expected_evidence),
            "expected_gaps": list(self.expected_gaps),
            "risks": list(self.risks),
            "confidence": self.confidence,
            "metadata": to_jsonable(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RetrievalPlanCandidate":
        return cls(
            candidate_id=str(payload.get("candidate_id", "")),
            queries=tuple(RetrievalStepSpec.from_dict(item) for item in payload.get("queries", ())),
            source_reading_plan=tuple(
                RetrievalStepSpec.from_dict(item) for item in payload.get("source_reading_plan", ())
            ),
            memory_recall_plan=tuple(
                RetrievalStepSpec.from_dict(item) for item in payload.get("memory_recall_plan", ())
            ),
            expected_evidence=tuple(payload.get("expected_evidence", ())),
            expected_gaps=tuple(payload.get("expected_gaps", ())),
            risks=tuple(payload.get("risks", ())),
            confidence=float(payload.get("confidence", 0.0)),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class EvidenceCandidate:
    evidence_id: str
    title: str
    summary: str
    source_ref: str
    span_refs: tuple[str, ...]
    evidence_type: str
    claim_refs: tuple[str, ...] = ()
    confidence: float = 0.0
    freshness: str = "unknown"
    lineage: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("evidence_id", "title", "summary", "source_ref", "evidence_type"):
            if not str(getattr(self, field_name)).strip():
                raise HarnessValidationError(f"{field_name} is required")
        if not self.span_refs:
            raise HarnessValidationError("span_refs are required")
        if not self.lineage:
            raise HarnessValidationError("lineage is required")
        if not 0 <= self.confidence <= 1:
            raise HarnessValidationError("confidence must be between 0 and 1")
        object.__setattr__(self, "span_refs", tuple(str(ref) for ref in self.span_refs))
        object.__setattr__(self, "claim_refs", tuple(str(ref) for ref in self.claim_refs))
        object.__setattr__(self, "lineage", tuple(str(item) for item in self.lineage))
        object.__setattr__(self, "artifact_refs", tuple(str(ref) for ref in self.artifact_refs if str(ref).strip()))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_evidence_pack(
        cls,
        pack: EvidencePack,
        *,
        evidence_type: str,
        artifact_refs: tuple[str, ...] = (),
    ) -> "EvidenceCandidate":
        pack_artifact_refs = _artifact_refs_from_metadata(pack.metadata)
        resolved_evidence_type = str(pack.metadata.get("evidence_type") or "").strip()
        candidate_evidence_type = (
            resolved_evidence_type
            if pack.metadata.get("evidence_type_source") == "content_resolved" and resolved_evidence_type
            else evidence_type
        )
        return cls(
            evidence_id=pack.evidence_id,
            title=pack.title,
            summary=pack.summary,
            source_ref=pack.source_refs[0],
            span_refs=tuple(pack.metadata.get("section_refs", pack.source_refs)),
            evidence_type=candidate_evidence_type,
            claim_refs=tuple(pack.metadata.get("claim_refs", ())),
            confidence=pack.confidence,
            freshness=pack.freshness,
            lineage=pack.lineage,
            artifact_refs=tuple(dict.fromkeys((*pack_artifact_refs, *artifact_refs))),
            metadata=pack.metadata,
        )

    def to_evidence_pack(self) -> EvidencePack:
        return EvidencePack(
            evidence_id=self.evidence_id,
            title=self.title,
            summary=self.summary,
            source_refs=(self.source_ref, *self.span_refs),
            confidence=self.confidence,
            freshness=self.freshness,
            lineage=self.lineage,
            metadata={
                **self.metadata,
                "evidence_type": self.evidence_type,
                "claim_refs": list(self.claim_refs),
                "artifact_refs": list(self.artifact_refs),
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "title": self.title,
            "summary": self.summary,
            "source_ref": self.source_ref,
            "span_refs": list(self.span_refs),
            "evidence_type": self.evidence_type,
            "claim_refs": list(self.claim_refs),
            "confidence": self.confidence,
            "freshness": self.freshness,
            "lineage": list(self.lineage),
            "artifact_refs": list(self.artifact_refs),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True)
class RetrievalStepResult:
    step_id: str
    operation: RetrievalOperation | str
    graph_identity: ContextGraphIdentity | None = None
    items: tuple[EvidenceCandidate | dict[str, Any], ...] = ()
    source_refs: tuple[str, ...] = ()
    memory_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    latency_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.step_id).strip():
            raise HarnessValidationError("step_id is required")
        if self.latency_ms < 0:
            raise HarnessValidationError("latency_ms must not be negative")
        object.__setattr__(self, "operation", RetrievalOperation(self.operation))
        if self.graph_identity is not None and not isinstance(self.graph_identity, ContextGraphIdentity):
            object.__setattr__(self, "graph_identity", ContextGraphIdentity.from_dict(self.graph_identity))
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "source_refs", tuple(str(ref) for ref in self.source_refs))
        object.__setattr__(self, "memory_refs", tuple(str(ref) for ref in self.memory_refs))
        object.__setattr__(self, "artifact_refs", tuple(str(ref) for ref in self.artifact_refs))
        object.__setattr__(self, "errors", tuple(str(error) for error in self.errors))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "operation": self.operation.value,
            "graph_identity": self.graph_identity.to_dict() if self.graph_identity else None,
            "items": to_jsonable(list(self.items)),
            "source_refs": list(self.source_refs),
            "memory_refs": list(self.memory_refs),
            "artifact_refs": list(self.artifact_refs),
            "errors": list(self.errors),
            "latency_ms": self.latency_ms,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True)
class RAGBudgetSnapshot:
    rounds_used: int = 0
    replans_used: int = 0
    queries_used: int = 0
    source_reads_used: int = 0
    memory_hits_used: int = 0
    context_items_used: int = 0
    context_tokens_used: int = 0
    worker_calls_used: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "rounds_used": self.rounds_used,
            "replans_used": self.replans_used,
            "queries_used": self.queries_used,
            "source_reads_used": self.source_reads_used,
            "memory_hits_used": self.memory_hits_used,
            "context_items_used": self.context_items_used,
            "context_tokens_used": self.context_tokens_used,
            "worker_calls_used": self.worker_calls_used,
        }

    def with_usage(
        self,
        *,
        rounds: int = 0,
        replans: int = 0,
        queries: int = 0,
        source_reads: int = 0,
        memory_hits: int = 0,
        context_items: int = 0,
        context_tokens: int = 0,
        worker_calls: int = 0,
    ) -> "RAGBudgetSnapshot":
        return RAGBudgetSnapshot(
            rounds_used=self.rounds_used + rounds,
            replans_used=self.replans_used + replans,
            queries_used=self.queries_used + queries,
            source_reads_used=self.source_reads_used + source_reads,
            memory_hits_used=self.memory_hits_used + memory_hits,
            context_items_used=self.context_items_used + context_items,
            context_tokens_used=self.context_tokens_used + context_tokens,
            worker_calls_used=self.worker_calls_used + worker_calls,
        )


@dataclass(frozen=True)
class RAGContextPack:
    pack_id: str
    query: str
    evidence: tuple[EvidencePack, ...] = ()
    context_refs: tuple[str, ...] = ()
    goal: RetrievalGoal | None = None
    accepted_evidence: tuple[EvidenceCandidate, ...] = ()
    rejected_evidence: tuple[EvidenceCandidate, ...] = ()
    conflicting_evidence: tuple[EvidenceCandidate, ...] = ()
    memory_context: tuple[dict[str, Any], ...] = ()
    source_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    evidence_trace: tuple[dict[str, Any], ...] = ()
    gap_report: dict[str, Any] = field(default_factory=dict)
    budget_snapshot: RAGBudgetSnapshot | None = None
    assembly_summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.pack_id).strip():
            raise HarnessValidationError("pack_id is required")
        if not str(self.query).strip():
            raise HarnessValidationError("query is required")
        if not all(isinstance(item, EvidencePack) for item in self.evidence):
            raise HarnessValidationError("evidence must be EvidencePack values")
        if not all(isinstance(item, EvidenceCandidate) for item in self.accepted_evidence):
            raise HarnessValidationError("accepted_evidence must be EvidenceCandidate values")
        if not all(isinstance(item, EvidenceCandidate) for item in self.rejected_evidence):
            raise HarnessValidationError("rejected_evidence must be EvidenceCandidate values")
        if not all(isinstance(item, EvidenceCandidate) for item in self.conflicting_evidence):
            raise HarnessValidationError("conflicting_evidence must be EvidenceCandidate values")
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "context_refs", tuple(str(ref) for ref in self.context_refs))
        object.__setattr__(self, "accepted_evidence", tuple(self.accepted_evidence))
        object.__setattr__(self, "rejected_evidence", tuple(self.rejected_evidence))
        object.__setattr__(self, "conflicting_evidence", tuple(self.conflicting_evidence))
        object.__setattr__(self, "memory_context", tuple(dict(item) for item in self.memory_context))
        object.__setattr__(self, "source_refs", tuple(str(ref) for ref in self.source_refs))
        object.__setattr__(self, "artifact_refs", tuple(str(ref) for ref in self.artifact_refs if str(ref).strip()))
        object.__setattr__(self, "evidence_trace", tuple(dict(item) for item in self.evidence_trace))
        object.__setattr__(self, "gap_report", dict(self.gap_report))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def accepted_packs(self) -> tuple[EvidencePack, ...]:
        return self.evidence or tuple(item.to_evidence_pack() for item in self.accepted_evidence)

    @property
    def context_pack_id(self) -> str:
        return self.pack_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "query": self.query,
            "evidence": [item.to_dict() for item in self.evidence],
            "context_refs": list(self.context_refs),
            "goal": self.goal.to_dict() if self.goal else None,
            "accepted_evidence": [item.to_dict() for item in self.accepted_evidence],
            "rejected_evidence": [item.to_dict() for item in self.rejected_evidence],
            "conflicting_evidence": [item.to_dict() for item in self.conflicting_evidence],
            "memory_context": to_jsonable(list(self.memory_context)),
            "source_refs": list(self.source_refs),
            "artifact_refs": list(self.artifact_refs),
            "evidence_trace": to_jsonable(list(self.evidence_trace)),
            "gap_report": to_jsonable(self.gap_report),
            "budget_snapshot": self.budget_snapshot.to_dict() if self.budget_snapshot else None,
            "assembly_summary": self.assembly_summary,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True)
class RAGTranscript:
    transcript_id: str
    session_id: str
    events: tuple[dict[str, Any], ...]
    status: RAGSessionStatus | str
    created_at: Any = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not str(self.transcript_id).strip():
            raise HarnessValidationError("transcript_id is required")
        if not str(self.session_id).strip():
            raise HarnessValidationError("session_id is required")
        object.__setattr__(self, "events", tuple(dict(event) for event in self.events))
        object.__setattr__(self, "status", RAGSessionStatus(self.status))

    @property
    def ref(self) -> str:
        if self.transcript_id.startswith("rag-transcript://"):
            return self.transcript_id
        return f"rag-transcript://{self.transcript_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "transcript_id": self.transcript_id,
            "session_id": self.session_id,
            "events": to_jsonable(list(self.events)),
            "status": self.status.value,
            "created_at": format_datetime(self.created_at),
            "ref": self.ref,
        }


@dataclass(frozen=True)
class AnswerClaim:
    claim_id: str
    text: str
    evidence_ids: tuple[str, ...]
    span_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.claim_id).strip():
            raise HarnessValidationError("claim_id is required")
        if not str(self.text).strip():
            raise HarnessValidationError("claim text is required")
        object.__setattr__(self, "evidence_ids", tuple(str(item) for item in self.evidence_ids))
        object.__setattr__(self, "span_refs", tuple(str(item) for item in self.span_refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "span_refs": list(self.span_refs),
        }


@dataclass(frozen=True)
class GroundedAnswerCandidate:
    answer_id: str
    question: str
    answer_text: str
    cited_evidence_ids: tuple[str, ...]
    claims: tuple[AnswerClaim, ...]
    abstained: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.answer_id).strip():
            raise HarnessValidationError("answer_id is required")
        if not str(self.question).strip():
            raise HarnessValidationError("answer question is required")
        object.__setattr__(self, "answer_text", str(self.answer_text))
        object.__setattr__(self, "cited_evidence_ids", tuple(str(item) for item in self.cited_evidence_ids))
        object.__setattr__(
            self,
            "claims",
            tuple(item if isinstance(item, AnswerClaim) else AnswerClaim(**item) for item in self.claims),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_id": self.answer_id,
            "question": self.question,
            "answer_text": self.answer_text,
            "cited_evidence_ids": list(self.cited_evidence_ids),
            "claims": [claim.to_dict() for claim in self.claims],
            "abstained": self.abstained,
            "metadata": to_jsonable(self.metadata),
        }


def ensure_jsonable_rag_model(value: Any) -> None:
    stable_json_dumps(to_jsonable(value))


def _artifact_refs_from_metadata(metadata: dict[str, Any]) -> tuple[str, ...]:
    raw = metadata.get("artifact_refs") or metadata.get("artifact_ref") or ()
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (tuple, list)):
        return ()
    return tuple(str(ref) for ref in raw if str(ref).strip())


__all__ = [
    "FORBIDDEN_RAG_PLAN_KEYS",
    "AnswerClaim",
    "EvidenceCandidate",
    "GroundedAnswerCandidate",
    "RAGBudget",
    "RAGBudgetSnapshot",
    "RAGContextPack",
    "RAGSessionSpec",
    "RAGSessionStatus",
    "RAGTranscript",
    "RetrievalGoal",
    "RetrievalOperation",
    "RetrievalPlanCandidate",
    "RetrievalStepResult",
    "RetrievalStepSpec",
    "ensure_jsonable_rag_model",
]
