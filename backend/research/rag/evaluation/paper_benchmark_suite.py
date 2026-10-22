from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol, Sequence

from backend.research.document.models import PaperChunk
from backend.research.application.llm_client import _browser_ua_transport
from backend.research.rag.adapters.paper_chunk_adapter import paper_chunk_to_rag_evidence
from backend.research.rag.evaluation.paper_answer_eval import (
    OVERCONSERVATIVE_ABSTENTION_REASON,
    EvidenceAnswerEvaluator,
    EvidenceAnswerSample,
    WRONG_ABSTENTION_REASON,
)
from backend.research.rag.evaluation.paper_evidence_eval import (
    EvidenceGoldenSetBuilder,
    EvidenceQAPair,
    EvidenceRetrievalEvaluator,
    formula_failure_diagnostics,
    save_evidence_golden_set,
)
from backend.research.rag.evaluation.paper_evaluation_report import EvidenceRegressionReport
from backend.research.rag.evaluation.paper_fixed_window_baseline import FixedWindowBaselineChunker, FixedWindowChunkerConfig
from backend.research.rag.evaluation.paper_generation_eval import GenerationEvaluator, GenerationEvalResult
from backend.research.rag.retrieval.paper_answer_generator import (
    DEFAULT_MAX_CONTEXT_CHARS,
    AnswerGenerator,
    GeneratedAnswer,
)
from backend.research.ports.paper_answer_model import PaperAnswerModelError, PaperAnswerModelPort
from framework.rag.evaluation import AnswerOutcome
from backend.research.rag.visual.page_visual_chunks import build_page_visual_chunks
from backend.research.rag.evaluation.evidence_eval_runner import (
    _build_live_retriever,
    _lightweight_reranker_enabled,
    _load_chunks_from_papers_dir,
)
from backend.research.rag.retrieval.contracts import RetrievalRequest, RetrievalResult
from backend.research.rag.retrieval.policies import (
    PAPER_BLIND_SEMANTIC_RAG_V1_POLICY,
    PAPER_HYBRID_RRF_RAG_V1_POLICY,
)
from framework.llm.clients.openai_compatible import LLMRetryPolicy, OpenAICompatibleClient, OpenAICompatibleConfig
from framework.llm.models.request import LLMRequest
from framework.shared.env import load_root_env

LLMCall = Callable[[str], Awaitable[str]]
GenerationTrack = Literal["end_to_end", "oracle_context"]

DEFAULT_SPLIT_RATIOS = (0.6, 0.2, 0.2)
DEFAULT_TARGET_QA_TYPES = (
    "citation_qa",
    "experiment_result_qa",
    "figure_qa",
    "formula_explanation_qa",
    "formula_qa",
    "table_qa",
)
PROMOTION_THRESHOLDS = {
    "overall_hit_at_3": 0.45,
    "overall_hit_at_5": 0.50,
    "overall_hit_at_10": 0.55,
    "overall_mrr": 0.30,
    "overall_evidence_coverage_at_5": 0.45,
    "overall_source_locator_coverage_at_5": 0.90,
    "formula_qa_hit_at_10": 0.45,
    "citation_qa_hit_at_10": 0.60,
    "figure_qa_hit_at_10": 0.58,
    "table_qa_hit_at_10": 0.60,
    "answer_success": 0.60,
    "strict_equivalent_hit_at_10_gap": 0.25,
    "true_missing_gold_rate": 0.25,
    "gold_judge_pass_rate": 0.90,
    "gold_judge_error_rate": 0.05,
    "human_spot_check_pass_rate": 0.90,
    "claim_support_rate": 0.85,
    "citation_claim_support_rate": 0.80,
    "unsupported_claim_rate": 0.10,
    "judge_human_agreement": 0.80,
    "ambiguous_question_rate": 0.15,
    "caption_copy_rate": 0.02,
}
_SPOT_CHECK_LABELS = frozenset({"pass", "warning", "fail", "needs_fix"})
_SPOT_CHECK_BOOLEAN_FIELDS = (
    "gold_evidence_ok",
    "retrieval_ok",
    "context_ok",
    "answer_ok",
    "faithfulness_ok",
    "citation_ok",
)
_ANSWER_FIX_REASON_ACTIONS = {
    "missing_gold_in_retrieval": "improve_retrieval_policy",
    "missing_gold_in_llm_context": "expand_context_assembler",
    "fact_match_low": "fix_answer_prompt",
    "unsupported_claim": "fix_answer_prompt",
    "contradicted_claim": "fix_answer_prompt",
    "wrong_citation": "fix_citation_mapping",
    "missing_citation": "fix_citation_mapping",
    OVERCONSERVATIVE_ABSTENTION_REASON: "fix_answer_prompt",
    WRONG_ABSTENTION_REASON: "fix_answer_prompt",
    "gold_evidence_bad": "fix_gold_evidence",
    "judge_human_conflict": "manual_review_required",
    "judge_error": "manual_review_required",
}
_ABSTENTION_FAILURE_REASON_ALIASES = frozenset({
    "abstention_expected",
    WRONG_ABSTENTION_REASON,
    "unexpected_abstention",
    "abstention_mismatch",
})
_GOLD_EVIDENCE_PREVIEW_CHARS = 4000


@dataclass(frozen=True)
class BenchmarkSplit:
    name: str
    paper_ids: tuple[str, ...]
    pair_count: int
    qa_type_counts: dict[str, int]


@dataclass(frozen=True)
class GoldEvidenceAuditItem:
    question: str
    paper_id: str
    qa_type: str
    status: str
    reason: str
    gold_chunk_ids: tuple[str, ...] = ()
    missing_chunk_ids: tuple[str, ...] = ()
    answer_facts_present: bool = False
    source_locator_count: int = 0
    image_ref_count: int = 0
    answer_facts: tuple[str, ...] = ()
    equivalent_gold_chunk_ids: tuple[str, ...] = ()
    supporting_evidence_group_id: str = ""
    evidence_previews: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "paper_id": self.paper_id,
            "qa_type": self.qa_type,
            "status": self.status,
            "reason": self.reason,
            "gold_chunk_ids": list(self.gold_chunk_ids),
            "missing_chunk_ids": list(self.missing_chunk_ids),
            "answer_facts_present": self.answer_facts_present,
            "source_locator_count": self.source_locator_count,
            "image_ref_count": self.image_ref_count,
            "answer_facts": list(self.answer_facts),
            "equivalent_gold_chunk_ids": list(self.equivalent_gold_chunk_ids),
            "supporting_evidence_group_id": self.supporting_evidence_group_id,
            "evidence_previews": [dict(item) for item in self.evidence_previews],
        }


@dataclass(frozen=True)
class GoldEvidenceAuditReport:
    sample_size: int
    passed: int
    warning: int
    failed: int
    by_qa_type: dict[str, dict[str, int]] = field(default_factory=dict)
    items: tuple[GoldEvidenceAuditItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "passed": self.passed,
            "warning": self.warning,
            "failed": self.failed,
            "by_qa_type": {
                qa_type: dict(counts)
                for qa_type, counts in sorted(self.by_qa_type.items())
            },
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class GoldEvidenceJudgeItem:
    question: str
    paper_id: str
    qa_type: str
    status: str
    reason: str
    supported: bool = False
    confidence: float = 0.0
    question_clear: bool | None = None
    gold_evidence_complete: bool | None = None
    equivalent_gold_needed: bool | None = None
    bad_gold_reason: str = ""
    suggested_action: str = ""
    gold_chunk_ids: tuple[str, ...] = ()
    equivalent_gold_chunk_ids: tuple[str, ...] = ()
    raw_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "paper_id": self.paper_id,
            "qa_type": self.qa_type,
            "status": self.status,
            "reason": self.reason,
            "supported": self.supported,
            "confidence": self.confidence,
            "question_clear": self.question_clear,
            "gold_evidence_complete": self.gold_evidence_complete,
            "equivalent_gold_needed": self.equivalent_gold_needed,
            "bad_gold_reason": self.bad_gold_reason,
            "suggested_action": self.suggested_action,
            "gold_chunk_ids": list(self.gold_chunk_ids),
            "equivalent_gold_chunk_ids": list(self.equivalent_gold_chunk_ids),
            "raw_response": dict(self.raw_response),
        }


@dataclass(frozen=True)
class GoldEvidenceJudgeReport:
    mode: str
    provider: str
    model: str
    sample_size: int
    passed: int
    warning: int
    failed: int
    error: int
    items: tuple[GoldEvidenceJudgeItem, ...] = ()

    @property
    def pass_rate(self) -> float:
        return self.passed / self.sample_size if self.sample_size else 0.0

    @property
    def error_rate(self) -> float:
        return self.error / self.sample_size if self.sample_size else 0.0

    @property
    def by_qa_type(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for qa_type in sorted({item.qa_type for item in self.items}):
            counts = Counter(item.status for item in self.items if item.qa_type == qa_type)
            out[qa_type] = {
                "pass": counts.get("pass", 0),
                "warning": counts.get("warning", 0),
                "fail": counts.get("fail", 0),
                "error": counts.get("error", 0),
            }
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "provider": self.provider,
            "model": self.model,
            "sample_size": self.sample_size,
            "passed": self.passed,
            "warning": self.warning,
            "failed": self.failed,
            "error": self.error,
            "pass_rate": self.pass_rate,
            "error_rate": self.error_rate,
            "by_qa_type": self.by_qa_type,
            "items": [item.to_dict() for item in self.items],
        }


class GoldEvidenceJudge(Protocol):
    def judge(self, items: Sequence[GoldEvidenceAuditItem]) -> GoldEvidenceJudgeReport:
        ...


@dataclass(frozen=True)
class SpotCheckReport:
    sample_path: str
    sample_size: int
    annotation_path: str = ""
    annotated_count: int = 0
    label_counts: dict[str, int] = field(default_factory=dict)
    by_qa_type: dict[str, dict[str, int]] = field(default_factory=dict)
    schema_error_count: int = 0
    boolean_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    calibration: dict[str, Any] = field(default_factory=dict)
    conflict_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        pass_count = int(self.label_counts.get("pass", 0))
        warning_count = int(self.label_counts.get("warning", 0))
        fail_count = int(self.label_counts.get("fail", 0)) + int(self.label_counts.get("needs_fix", 0))
        boolean_rates = {
            f"human_{field.removesuffix('_ok')}_ok_rate": _boolean_true_rate(counts)
            for field, counts in sorted(self.boolean_counts.items())
        }
        return {
            "sample_path": self.sample_path,
            "sample_size": self.sample_size,
            "annotation_path": self.annotation_path,
            "annotated_count": self.annotated_count,
            "human_spot_check_pass_rate": pass_count / self.annotated_count if self.annotated_count else 0.0,
            "pass_rate": pass_count / self.annotated_count if self.annotated_count else 0.0,
            "warning_count": warning_count,
            "fail_count": fail_count,
            "schema_error_count": self.schema_error_count,
            **boolean_rates,
            "label_counts": dict(sorted(self.label_counts.items())),
            "by_qa_type": {
                qa_type: dict(counts)
                for qa_type, counts in sorted(self.by_qa_type.items())
            },
            "human_by_qa_type": {
                qa_type: dict(counts)
                for qa_type, counts in sorted(self.by_qa_type.items())
            },
            "boolean_counts": {
                field: dict(counts)
                for field, counts in sorted(self.boolean_counts.items())
            },
            "judge_human_calibration": dict(self.calibration),
            "conflict_count": self.conflict_count,
        }


@dataclass(frozen=True)
class QuestionAmbiguityAuditItem:
    question: str
    paper_id: str
    qa_type: str
    reasons: tuple[str, ...]
    gold_chunk_ids: tuple[str, ...] = ()
    semantic_anchors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "paper_id": self.paper_id,
            "qa_type": self.qa_type,
            "reasons": list(self.reasons),
            "gold_chunk_ids": list(self.gold_chunk_ids),
            "semantic_anchors": list(self.semantic_anchors),
        }


@dataclass(frozen=True)
class QuestionAmbiguityAuditReport:
    total: int
    duplicate_questions: int
    ambiguous_questions: int
    missing_semantic_anchor: int
    label_leakage: int
    caption_copy: int
    by_qa_type: dict[str, dict[str, int]] = field(default_factory=dict)
    items: tuple[QuestionAmbiguityAuditItem, ...] = ()

    @property
    def duplicate_question_rate(self) -> float:
        return self.duplicate_questions / self.total if self.total else 0.0

    @property
    def ambiguous_question_rate(self) -> float:
        return self.ambiguous_questions / self.total if self.total else 0.0

    @property
    def missing_semantic_anchor_rate(self) -> float:
        return self.missing_semantic_anchor / self.total if self.total else 0.0

    @property
    def label_leakage_rate(self) -> float:
        return self.label_leakage / self.total if self.total else 0.0

    @property
    def caption_copy_rate(self) -> float:
        return self.caption_copy / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "duplicate_questions": self.duplicate_questions,
            "duplicate_question_rate": self.duplicate_question_rate,
            "ambiguous_questions": self.ambiguous_questions,
            "ambiguous_question_rate": self.ambiguous_question_rate,
            "missing_semantic_anchor": self.missing_semantic_anchor,
            "missing_semantic_anchor_rate": self.missing_semantic_anchor_rate,
            "label_leakage": self.label_leakage,
            "label_leakage_rate": self.label_leakage_rate,
            "caption_copy": self.caption_copy,
            "caption_copy_rate": self.caption_copy_rate,
            "by_qa_type": {
                qa_type: dict(counts)
                for qa_type, counts in sorted(self.by_qa_type.items())
            },
            "items": [item.to_dict() for item in self.items],
        }


class OpenAICompatibleGoldEvidenceJudge:
    """Optional LLM audit for whether gold evidence supports a QA pair."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        max_evidence_chars: int = 1600,
        client: OpenAICompatibleClient | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("gold evidence LLM judge requires OPENAI_BASE_URL or NEWS_GOLD_JUDGE_BASE_URL")
        if not os.environ.get(api_key_env):
            raise ValueError(f"gold evidence LLM judge requires {api_key_env}")
        self._model = model
        self._max_evidence_chars = max(200, int(max_evidence_chars))
        self._client = client or OpenAICompatibleClient(
            OpenAICompatibleConfig(
                provider="openai-compatible-gold-judge",
                base_url=base_url.rstrip("/"),
                model=model,
                api_key_env=api_key_env,
                timeout_seconds=120.0,
            ),
            transport=_browser_ua_transport,
            retry_policy=LLMRetryPolicy(max_attempts=4, retry_delay_seconds=(1.0, 2.0, 4.0)),
        )

    @classmethod
    def from_env(cls, *, max_evidence_chars: int = 1600) -> "OpenAICompatibleGoldEvidenceJudge":
        load_root_env()
        base_url = os.environ.get("NEWS_GOLD_JUDGE_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
        model = os.environ.get("NEWS_GOLD_JUDGE_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-5.4-mini"
        api_key_env = os.environ.get("NEWS_GOLD_JUDGE_API_KEY_ENV") or "OPENAI_API_KEY"
        return cls(
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            max_evidence_chars=max_evidence_chars,
        )

    def judge(self, items: Sequence[GoldEvidenceAuditItem]) -> GoldEvidenceJudgeReport:
        judged = tuple(self._judge_one(item) for item in items)
        counts = Counter(item.status for item in judged)
        return GoldEvidenceJudgeReport(
            mode="llm",
            provider="openai-compatible",
            model=self._model,
            sample_size=len(judged),
            passed=counts.get("pass", 0),
            warning=counts.get("warning", 0),
            failed=counts.get("fail", 0),
            error=counts.get("error", 0),
            items=judged,
        )

    def _judge_one(self, item: GoldEvidenceAuditItem) -> GoldEvidenceJudgeItem:
        try:
            response = self._client.complete(LLMRequest(
                messages=[{"role": "user", "content": _judge_prompt(item, self._max_evidence_chars)}],
                max_tokens=300,
                temperature=0,
                response_format={"type": "json_object"},
            ))
            payload = _extract_json_object(response.content or "")
        except Exception as exc:  # noqa: BLE001 - audit must not crash the suite mid-sample
            return GoldEvidenceJudgeItem(
                question=item.question,
                paper_id=item.paper_id,
                qa_type=item.qa_type,
                status="error",
                reason=type(exc).__name__,
                gold_chunk_ids=item.gold_chunk_ids,
                equivalent_gold_chunk_ids=item.equivalent_gold_chunk_ids,
            )

        supported = bool(payload.get("supported"))
        confidence = _clamped_float(payload.get("confidence"))
        reason = str(payload.get("reason") or "").strip()[:500] or "no_reason"
        bad_gold_reason = str(payload.get("bad_gold_reason") or "").strip()[:120]
        suggested_action = str(payload.get("suggested_action") or "").strip()[:120]
        status = "pass" if supported and confidence >= 0.6 else "warning" if supported else "fail"
        return GoldEvidenceJudgeItem(
            question=item.question,
            paper_id=item.paper_id,
            qa_type=item.qa_type,
            status=status,
            reason=reason,
            supported=supported,
            confidence=confidence,
            question_clear=_optional_bool(payload.get("question_clear")),
            gold_evidence_complete=_optional_bool(payload.get("gold_evidence_complete")),
            equivalent_gold_needed=_optional_bool(payload.get("equivalent_gold_needed")),
            bad_gold_reason=bad_gold_reason,
            suggested_action=suggested_action,
            gold_chunk_ids=item.gold_chunk_ids,
            equivalent_gold_chunk_ids=item.equivalent_gold_chunk_ids,
            raw_response=payload,
        )


@dataclass(frozen=True)
class PolicyPromotionCheck:
    check_id: str
    label: str
    status: str
    actual: Any = None
    threshold: Any = None
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "label": self.label,
            "status": self.status,
            "actual": self.actual,
            "threshold": self.threshold,
            "details": self.details,
        }


@dataclass(frozen=True)
class PolicyPromotionChecklist:
    policy_name: str
    ready_for_promotion: bool
    reported_split: str
    tuning_split: str
    thresholds: dict[str, float]
    checks: tuple[PolicyPromotionCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        status_counts = Counter(check.status for check in self.checks)
        return {
            "policy_name": self.policy_name,
            "ready_for_promotion": self.ready_for_promotion,
            "reported_split": self.reported_split,
            "tuning_split": self.tuning_split,
            "thresholds": dict(self.thresholds),
            "status_counts": dict(sorted(status_counts.items())),
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass
class BenchmarkSuiteResult:
    output_dir: Path
    papers_total: int
    chunks_total: int
    pairs_total: int
    split_seed: str
    splits: dict[str, BenchmarkSplit]
    target_qa_counts: dict[str, int]
    question_profile: str
    generation_track: GenerationTrack
    question_audit: QuestionAmbiguityAuditReport
    gold_audit: GoldEvidenceAuditReport
    gold_judge: GoldEvidenceJudgeReport | None
    spot_check: SpotCheckReport | None
    policy_promotion_checklist: PolicyPromotionChecklist
    candidate_test_report: dict[str, Any]
    baseline_test_report: dict[str, Any] | None
    ab_report: dict[str, Any] | None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        test_policy = "candidate is evaluated on the test split"
        if self.baseline_test_report is not None:
            test_policy = "candidate and fixed-window baseline are evaluated only on the test split"
        return {
            "output_dir": str(self.output_dir),
            "papers_total": self.papers_total,
            "chunks_total": self.chunks_total,
            "pairs_total": self.pairs_total,
            "split_seed": self.split_seed,
            "splits": {name: _split_to_dict(split) for name, split in self.splits.items()},
            "target_qa_counts": dict(self.target_qa_counts),
            "evaluation_protocol": {
                "train_split": "train",
                "tuning_split": "dev",
                "reported_split": "test",
                "test_policy": test_policy,
                "question_profile": self.question_profile,
                "generation_track": self.generation_track,
                "blind_test": _is_blind_question_profile(self.question_profile),
                "detemplate_policy": _detemplate_policy_for_profile(self.question_profile),
            },
            "question_audit": self.question_audit.to_dict(),
            "gold_audit": self.gold_audit.to_dict(),
            "gold_judge": self.gold_judge.to_dict() if self.gold_judge is not None else None,
            "spot_check": self.spot_check.to_dict() if self.spot_check is not None else None,
            "gold_quality": _gold_quality_summary(
                self.question_profile,
                self.gold_judge,
                self.spot_check,
            ),
            "policy_promotion_checklist": self.policy_promotion_checklist.to_dict(),
            "candidate_test_report": self.candidate_test_report,
            "baseline_test_report": self.baseline_test_report,
            "ab_report": self.ab_report,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class BenchmarkSuiteConfig:
    papers_dir: Path
    output_dir: Path
    image_root: Path | None = None
    retrieval_policy: str = "paper_visual_rag_tuned"
    max_pairs_per_type: int = 100
    min_papers: int = 20
    target_min_per_type: int = 50
    split_seed: str = "paper-rag-benchmark-v1"
    split_ratios: tuple[float, float, float] = DEFAULT_SPLIT_RATIOS
    include_negative: bool = True
    question_profile: str = "template"
    visual: bool = True
    page_visual: bool = True
    render_page_visual: bool = False
    lightweight_reranker: bool = False
    gold_audit_sample_size: int = 30
    gold_judge_mode: str = "none"
    gold_judge_sample_size: int | None = None
    gold_judge_max_evidence_chars: int = 1600
    gold_evidence_judge: GoldEvidenceJudge | None = None
    answer_eval_enabled: bool = False
    answer_eval_sample_size: int | None = None
    answer_max_context_chunks: int = 5
    answer_max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
    answer_model: PaperAnswerModelPort | None = None
    answer_judge_mode: str = "none"
    answer_judge_sample_size: int | None = None
    answer_judge_llm_call: LLMCall | None = None
    spot_check_sample_size: int = 0
    spot_check_annotations_path: Path | None = None
    quality_thresholds: dict[str, float] = field(default_factory=dict)
    include_fixed_window_baseline: bool = False
    fixed_window_tokens: int = 220
    fixed_window_overlap_tokens: int | None = None
    generation_track: GenerationTrack = "end_to_end"

    def __post_init__(self) -> None:
        if self.generation_track not in ("end_to_end", "oracle_context"):
            raise ValueError("generation_track must be 'end_to_end' or 'oracle_context'")


def run_benchmark_suite(config: BenchmarkSuiteConfig) -> BenchmarkSuiteResult:
    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    question_profile = _normalize_question_profile(config.question_profile)
    base_chunks, chunks = _load_suite_chunk_sets(config)
    gold_chunks = _gold_builder_chunks(base_chunks)
    paper_ids = sorted({chunk.paper_id for chunk in gold_chunks})
    pairs = EvidenceGoldenSetBuilder(
        max_pairs_per_type=config.max_pairs_per_type,
        include_negative=config.include_negative,
        question_profile=question_profile,
    ).build(gold_chunks)
    question_audit = audit_question_ambiguity(pairs, gold_chunks)
    target_counts = _target_counts(pairs)
    split_map = split_paper_ids(
        paper_ids,
        seed=config.split_seed,
        ratios=config.split_ratios,
    )
    split_pairs = {
        name: [pair for pair in pairs if pair.paper_id in set(ids)]
        for name, ids in split_map.items()
    }
    splits = {
        name: BenchmarkSplit(
            name=name,
            paper_ids=tuple(ids),
            pair_count=len(split_pairs[name]),
            qa_type_counts=dict(sorted(Counter(pair.qa_type for pair in split_pairs[name]).items())),
        )
        for name, ids in split_map.items()
    }

    for name, current_pairs in split_pairs.items():
        split_dir = output_dir / name
        split_dir.mkdir(parents=True, exist_ok=True)
        save_evidence_golden_set(current_pairs, split_dir / "golden_set.json")

    test_pairs = split_pairs.get("test", [])
    audit = audit_gold_evidence(
        test_pairs,
        chunks,
        sample_size=_effective_gold_audit_sample_size(config),
        seed=config.split_seed,
    )
    judge_report = _run_gold_judge(config, audit)
    candidate_report, spot_check = _evaluate_candidate(config, chunks, test_pairs, output_dir / "test" / "candidate")
    baseline_report = None
    ab_report = None
    if config.include_fixed_window_baseline:
        baseline_report = _evaluate_fixed_window_baseline(config, chunks, test_pairs, output_dir / "test" / "fixed_window")
        ab_report = _compare_reports(candidate_report, baseline_report)

    promotion_checklist = _build_policy_promotion_checklist(
        config=config,
        question_profile=question_profile,
        splits=splits,
        question_audit=question_audit,
        gold_audit=audit,
        judge_report=judge_report,
        spot_check=spot_check,
        candidate_report=candidate_report,
    )
    warnings = _suite_warnings(
        paper_count=len(paper_ids),
        target_counts=target_counts,
        config=config,
        splits=splits,
        audit=audit,
        judge_report=judge_report,
        candidate_report=candidate_report,
    )
    result = BenchmarkSuiteResult(
        output_dir=output_dir,
        papers_total=len(paper_ids),
        chunks_total=len(chunks),
        pairs_total=len(pairs),
        split_seed=config.split_seed,
        splits=splits,
        target_qa_counts=target_counts,
        question_profile=question_profile,
        generation_track=config.generation_track,
        question_audit=question_audit,
        gold_audit=audit,
        gold_judge=judge_report,
        spot_check=spot_check,
        policy_promotion_checklist=promotion_checklist,
        candidate_test_report=candidate_report,
        baseline_test_report=baseline_report,
        ab_report=ab_report,
        warnings=warnings,
    )
    _write_suite_report(result)
    return result


def _effective_gold_audit_sample_size(config: BenchmarkSuiteConfig) -> int:
    configured = max(0, int(config.gold_audit_sample_size))
    mode = str(config.gold_judge_mode or "none").strip().casefold()
    if mode in {"", "none", "off", "false", "0"} or config.gold_judge_sample_size is None:
        return configured
    requested = max(0, int(config.gold_judge_sample_size))
    if requested <= 0:
        return configured
    return max(configured, requested + max(10, len(DEFAULT_TARGET_QA_TYPES)))


def split_paper_ids(
    paper_ids: Sequence[str],
    *,
    seed: str,
    ratios: tuple[float, float, float] = DEFAULT_SPLIT_RATIOS,
) -> dict[str, list[str]]:
    if len(ratios) != 3:
        raise ValueError("split ratios must contain train/dev/test")
    if any(value < 0 for value in ratios) or sum(ratios) <= 0:
        raise ValueError("split ratios must be non-negative and non-empty")
    ordered = sorted(set(paper_ids), key=lambda paper_id: _stable_split_key(seed, paper_id))
    total = len(ordered)
    if total == 0:
        return {"train": [], "dev": [], "test": []}
    train_count = int(total * ratios[0] / sum(ratios))
    dev_count = int(total * ratios[1] / sum(ratios))
    if total >= 3:
        train_count = max(1, train_count)
        dev_count = max(1, dev_count)
        if train_count + dev_count >= total:
            dev_count = max(1, total - train_count - 1)
    test_count = max(0, total - train_count - dev_count)
    if total >= 3 and test_count == 0:
        test_count = 1
        if dev_count > 1:
            dev_count -= 1
        else:
            train_count = max(1, train_count - 1)
    return {
        "train": ordered[:train_count],
        "dev": ordered[train_count:train_count + dev_count],
        "test": ordered[train_count + dev_count:],
    }


def _normalize_question_profile(profile: str) -> str:
    normalized = str(profile or "template").strip().casefold()
    if normalized in {"", "template"}:
        return "template"
    if normalized in {"blind", "blind_detemplated", "detemplated"}:
        return "blind_detemplated"
    if normalized in {"blind_semantic", "semantic", "semantic_blind"}:
        return "blind_semantic"
    raise ValueError("question_profile must be 'template', 'blind_detemplated', or 'blind_semantic'")


def _is_blind_question_profile(profile: str) -> bool:
    return _normalize_question_profile(profile) != "template"


def _detemplate_policy_for_profile(profile: str) -> str:
    normalized = _normalize_question_profile(profile)
    if normalized == "blind_detemplated":
        return "remove_labels_caption_quote_v1"
    if normalized == "blind_semantic":
        return "semantic_anchors_no_labels_v1"
    return ""


def audit_question_ambiguity(
    pairs: Sequence[EvidenceQAPair],
    chunks: Sequence[PaperChunk],
) -> QuestionAmbiguityAuditReport:
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    normalized_groups: dict[tuple[str, str, str], list[tuple[int, EvidenceQAPair]]] = {}
    for index, pair in enumerate(pairs):
        key = (pair.paper_id, pair.qa_type, _normalize_question_text(pair.question))
        normalized_groups.setdefault(key, []).append((index, pair))

    duplicate_indexes: set[int] = set()
    ambiguous_indexes: set[int] = set()
    for grouped in normalized_groups.values():
        if len(grouped) <= 1:
            continue
        duplicate_indexes.update(index for index, _pair in grouped)
        gold_signatures = {
            tuple(pair.gold_chunk_ids)
            for _index, pair in grouped
            if pair.expected_behavior == "answer"
        }
        if len(gold_signatures) > 1:
            ambiguous_indexes.update(index for index, _pair in grouped)

    missing_anchor_indexes: set[int] = set()
    label_leakage_indexes: set[int] = set()
    caption_copy_indexes: set[int] = set()
    flagged_items: list[QuestionAmbiguityAuditItem] = []
    by_qa_type: dict[str, Counter[str]] = {}

    for index, pair in enumerate(pairs):
        reasons: list[str] = []
        anchors = tuple(str(anchor) for anchor in pair.metadata.get("semantic_anchors") or [])
        if index in duplicate_indexes:
            reasons.append("duplicate_question")
        if index in ambiguous_indexes:
            reasons.append("ambiguous_question")
        if _missing_semantic_anchor(pair):
            missing_anchor_indexes.add(index)
            reasons.append("missing_semantic_anchor")
        if _question_has_label_leakage(pair.question):
            label_leakage_indexes.add(index)
            reasons.append("label_leakage")
        if _question_copies_source_text(pair, chunks_by_id):
            caption_copy_indexes.add(index)
            reasons.append("caption_copy")

        counts = by_qa_type.setdefault(pair.qa_type, Counter())
        counts["total"] += 1
        for reason in reasons:
            counts[reason] += 1
        if reasons:
            flagged_items.append(QuestionAmbiguityAuditItem(
                question=pair.question,
                paper_id=pair.paper_id,
                qa_type=pair.qa_type,
                reasons=tuple(reasons),
                gold_chunk_ids=tuple(pair.gold_chunk_ids),
                semantic_anchors=anchors,
            ))

    return QuestionAmbiguityAuditReport(
        total=len(pairs),
        duplicate_questions=len(duplicate_indexes),
        ambiguous_questions=len(ambiguous_indexes),
        missing_semantic_anchor=len(missing_anchor_indexes),
        label_leakage=len(label_leakage_indexes),
        caption_copy=len(caption_copy_indexes),
        by_qa_type={qa_type: dict(counter) for qa_type, counter in by_qa_type.items()},
        items=tuple(flagged_items[:100]),
    )


def _normalize_question_text(question: str) -> str:
    return " ".join(str(question or "").casefold().split())


def _missing_semantic_anchor(pair: EvidenceQAPair) -> bool:
    if pair.expected_behavior != "answer":
        return False
    profile = str(pair.metadata.get("question_profile") or "template")
    anchors = [
        str(anchor).strip()
        for anchor in pair.metadata.get("semantic_anchors") or []
        if str(anchor).strip()
    ]
    if _normalize_question_profile(profile) == "blind_semantic":
        return len(anchors) < 2
    return len(_semantic_question_tokens(pair.question)) < 2


def _semantic_question_tokens(question: str) -> list[str]:
    stopwords = {
        "about", "does", "evidence", "explain", "explains", "ground", "grounds", "paper",
        "passage", "question", "reported", "result", "results", "section", "show", "shows",
        "suggest", "table", "figure", "formula", "equation", "what", "which", "where", "with",
        "used", "overall", "quantitative", "visual", "mathematical", "relation",
    }
    out: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", str(question or "")):
        normalized = token.casefold()
        if normalized in stopwords or normalized in seen:
            continue
        out.append(token)
        seen.add(normalized)
    return out


def _question_has_label_leakage(question: str) -> bool:
    text = str(question or "")
    return bool(re.search(
        r"\b(?:figure|fig\.?|table|equation|eq\.?)\s+[A-Za-z0-9_.:-]+\b",
        text,
        flags=re.IGNORECASE,
    ))


def _question_copies_source_text(pair: EvidenceQAPair, chunks_by_id: dict[str, PaperChunk]) -> bool:
    question_tokens = _copy_check_tokens(pair.question)
    if len(question_tokens) < 8:
        return False
    for text in _source_texts_for_copy_audit(pair, chunks_by_id):
        if _longest_common_token_run(question_tokens, _copy_check_tokens(text)) >= 8:
            return True
    return False


def _source_texts_for_copy_audit(pair: EvidenceQAPair, chunks_by_id: dict[str, PaperChunk]) -> list[str]:
    texts: list[str] = []
    texts.extend(pair.answer_facts)
    for chunk_id in pair.gold_chunk_ids:
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None:
            continue
        texts.extend([
            str(chunk.metadata.get("caption_text") or ""),
            str(chunk.metadata.get("surya_caption") or ""),
            str(chunk.metadata.get("visual_description") or ""),
            str(chunk.metadata.get("semantic_text") or ""),
            str(chunk.metadata.get("table_text") or ""),
            _caption_block_for_audit(chunk.content),
        ])
    return [text for text in texts if str(text).strip()]


def _copy_check_tokens(text: str) -> list[str]:
    return [
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9]+", str(text or ""))
        if len(token) > 1
    ]


def _longest_common_token_run(left: list[str], right: list[str]) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for left_token in left:
        current = [0] * (len(right) + 1)
        for index, right_token in enumerate(right, start=1):
            if left_token != right_token:
                continue
            current[index] = previous[index - 1] + 1
            best = max(best, current[index])
        previous = current
    return best


def _caption_block_for_audit(content: str) -> str:
    marker = "caption:"
    normalized = str(content or "").casefold()
    index = normalized.find(marker)
    if index < 0:
        return ""
    tail = str(content or "")[index + len(marker):]
    lines: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if stripped.endswith(":") and lines:
            break
        lines.append(stripped)
    return " ".join(lines)


def audit_gold_evidence(
    pairs: Sequence[EvidenceQAPair],
    chunks: Sequence[PaperChunk],
    *,
    sample_size: int,
    seed: str,
) -> GoldEvidenceAuditReport:
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    sample = _stratified_pair_sample(list(pairs), sample_size=max(0, sample_size), seed=f"{seed}:gold_audit")
    items = tuple(_audit_pair(pair, chunks_by_id) for pair in sample)
    counts = Counter(item.status for item in items)
    by_qa_type: dict[str, dict[str, int]] = {}
    for qa_type in sorted({item.qa_type for item in items}):
        type_counts = Counter(item.status for item in items if item.qa_type == qa_type)
        by_qa_type[qa_type] = {
            "pass": type_counts.get("pass", 0),
            "warning": type_counts.get("warning", 0),
            "fail": type_counts.get("fail", 0),
        }
    return GoldEvidenceAuditReport(
        sample_size=len(items),
        passed=counts.get("pass", 0),
        warning=counts.get("warning", 0),
        failed=counts.get("fail", 0),
        by_qa_type=by_qa_type,
        items=items,
    )


def _load_suite_chunk_sets(config: BenchmarkSuiteConfig) -> tuple[list[PaperChunk], list[PaperChunk]]:
    base_chunks = _load_chunks_from_papers_dir(config.papers_dir)
    chunks = list(base_chunks)
    if config.page_visual:
        chunks.extend(build_page_visual_chunks(
            chunks,
            papers_dir=config.papers_dir,
            render_pages=config.render_page_visual,
        ))
    return base_chunks, chunks


def _gold_builder_chunks(chunks: Sequence[PaperChunk]) -> list[PaperChunk]:
    return [
        chunk for chunk in chunks
        if not chunk.metadata.get("page_visual") and not chunk.metadata.get("is_parent")
    ]


def _evaluate_candidate(
    config: BenchmarkSuiteConfig,
    chunks: list[PaperChunk],
    pairs: list[EvidenceQAPair],
    output_dir: Path,
) -> tuple[dict[str, Any], SpotCheckReport | None]:
    retriever, visual_store = _build_live_retriever(
        chunks,
        visual_enabled=config.visual,
        image_root=config.image_root or config.papers_dir,
        retrieval_policy=config.retrieval_policy,
        lightweight_reranker=config.lightweight_reranker,
    )
    retrieval = EvidenceRetrievalEvaluator(retriever).evaluate(pairs)
    formula_diagnostics = formula_failure_diagnostics(retrieval, top_k=10)
    answer_samples: list[EvidenceAnswerSample] = []
    generated_answers: list[GeneratedAnswer | None] = []
    answer_result = None
    generation_result = None
    answer_judge_records: list[tuple[EvidenceAnswerSample, GeneratedAnswer]] = []
    if _answers_requested(config):
        answer_samples, generated_answers = asyncio.run(_generate_answer_samples(config, retriever, pairs))
        if config.answer_eval_enabled:
            answer_result = EvidenceAnswerEvaluator().evaluate(answer_samples)
        if _answer_judge_enabled(config):
            answer_judge_records = _answer_judge_sample_records(answer_samples, generated_answers, config)
            judge_answers = [generated for _sample, generated in answer_judge_records]
            generation_result = asyncio.run(_run_answer_judge(config, judge_answers))
            _write_answer_judge_artifacts(
                output_dir,
                answer_judge_records,
                generation_result,
                answer_result=answer_result,
            )
        _write_answer_samples(
            output_dir / "answer_samples.jsonl",
            answer_samples,
            generated_answers,
            answer_result=answer_result,
        )
    metadata = {
        "mode": "candidate",
        "retrieval_policy": retriever.policy.name,
        "question_profile": _normalize_question_profile(config.question_profile),
        "generation_track": config.generation_track,
        "blind_test": _is_blind_question_profile(config.question_profile),
        "chunks_total": len(chunks),
        "visual_fusion_enabled": visual_store is not None,
        "visual_indexed_chunks": len(getattr(visual_store, "_vectors", {})) if visual_store is not None else 0,
        "lightweight_reranker_enabled": _lightweight_reranker_enabled(
            config.retrieval_policy,
            explicit=config.lightweight_reranker,
        ),
        "answer_eval_enabled": config.answer_eval_enabled,
        "answer_judge_mode": config.answer_judge_mode,
        "answer_samples": len(answer_samples),
    }
    report = EvidenceRegressionReport(
        retrieval=retrieval,
        answer=answer_result,
        generation=generation_result,
        thresholds=dict(config.quality_thresholds),
        metadata=metadata,
    )
    report.write(output_dir)
    _write_formula_retrieval_diagnostics(output_dir, formula_diagnostics)
    payload = json.loads((output_dir / "evidence_regression_report.json").read_text(encoding="utf-8"))
    payload["formula_retrieval_diagnostics"] = _formula_diagnostics_summary(formula_diagnostics)
    spot_check = _write_spot_check_report(
        config,
        output_dir=output_dir,
        answer_samples=answer_samples,
        generated_answers=generated_answers,
        answer_result=answer_result,
        generation_result=generation_result,
        answer_judge_records=answer_judge_records,
    )
    _write_answer_fix_artifacts(
        output_dir,
        answer_judge_records=answer_judge_records,
        generation_result=generation_result,
        spot_check=spot_check,
    )
    if spot_check is not None:
        payload["spot_check"] = spot_check.to_dict()
    (output_dir / "evidence_regression_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload, spot_check


def _write_formula_retrieval_diagnostics(output_dir: Path, diagnostics: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "formula_retrieval_diagnostics.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_jsonl(output_dir / "formula_retrieval_failures.jsonl", list(diagnostics.get("items") or []))


def _formula_diagnostics_summary(diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        "top_k": diagnostics.get("top_k", 10),
        "total_failures": diagnostics.get("total_failures", 0),
        "reason_counts": dict(diagnostics.get("reason_counts") or {}),
    }


def _answers_requested(config: BenchmarkSuiteConfig) -> bool:
    return (
        config.answer_eval_enabled
        or _answer_judge_enabled(config)
        or config.spot_check_sample_size > 0
    )


def _answer_judge_enabled(config: BenchmarkSuiteConfig) -> bool:
    mode = str(config.answer_judge_mode or "none").strip().casefold()
    return mode not in {"", "none", "off", "false", "0"}


async def _generate_answer_samples(
    config: BenchmarkSuiteConfig,
    retriever: Any,
    pairs: list[EvidenceQAPair],
) -> tuple[list[EvidenceAnswerSample], list[GeneratedAnswer | None]]:
    answer_model = config.answer_model
    if answer_model is None:
        from backend.research.application.llm_client import build_unity_paper_answer_model
        answer_model = build_unity_paper_answer_model(max_tokens=700)
    generator = AnswerGenerator(
        answer_model,
        max_context_chunks=config.answer_max_context_chunks,
        max_context_chars=config.answer_max_context_chars,
    )
    samples: list[EvidenceAnswerSample] = []
    generated: list[GeneratedAnswer | None] = []
    for pair in _answer_generation_pairs(pairs, config):
        retrieval = retriever.retrieve(RetrievalRequest(
            paper_id=pair.paper_id,
            question=pair.question,
            limit=10,
        ))
        if config.generation_track == "oracle_context":
            required_context_ids = _evidence_pack_required_context_ids(pair)
            retrieval, evidence_pack_metadata = _hydrate_retrieval_with_evidence_pack(
                retrieval, pair, chunk_lookup=retriever, required_context_ids=required_context_ids,
            )
        elif config.generation_track == "end_to_end":
            required_context_ids = []
            evidence_pack_metadata = {}
        else:
            raise ValueError("generation_track must be 'end_to_end' or 'oracle_context'")
        try:
            if config.generation_track == "oracle_context":
                answer = await generator.generate(
                    pair.question,
                    retrieval,
                    required_context_ids=required_context_ids,
                )
            else:
                answer = await generator.generate(pair.question, retrieval)
        except PaperAnswerModelError as exc:
            samples.append(EvidenceAnswerSample(
                pair=pair,
                answer="",
                outcome=AnswerOutcome.FAILED,
                metadata={
                    "model_error_code": exc.code,
                    "model_error_message": str(exc),
                    "retrieved_chunk_ids": [
                        chunk.chunk_id for chunk in getattr(retrieval, "child_chunks", [])
                    ],
                },
            ))
            generated.append(None)
            continue
        context_chunks = _context_chunks_for_answer(retrieval, answer.context_chunk_ids)
        context_score_breakdowns = _context_score_breakdowns(context_chunks)
        answer.context_metadata.update(evidence_pack_metadata)
        answer.context_metadata["context_score_breakdowns"] = context_score_breakdowns
        cited_chunk_ids = list(answer.cited_chunk_ids)
        context_by_id = {chunk.chunk_id: chunk for chunk in context_chunks}
        cited_locators = [
            locator for chunk_id in cited_chunk_ids
            if (locator := _chunk_source_locator(context_by_id.get(chunk_id)))
        ]
        samples.append(EvidenceAnswerSample(
            pair=pair,
            answer="" if answer.abstained else answer.answer,
            outcome=AnswerOutcome.ABSTAINED if answer.abstained else AnswerOutcome.ANSWERED,
            cited_chunk_ids=cited_chunk_ids,
            cited_source_locators=cited_locators,
            context_chunk_ids=list(answer.context_chunk_ids),
            metadata={
                "context_count": len(answer.context_chunk_ids),
                "citation_count": len(cited_chunk_ids),
                "retrieved_chunk_ids": list(answer.context_metadata.get("retrieved_chunk_ids") or []),
                "context_selection_strategy": answer.context_metadata.get("context_selection_strategy", ""),
                "context_source_buckets": dict(answer.context_metadata.get("context_source_buckets") or {}),
                "context_role_buckets": dict(answer.context_metadata.get("context_role_buckets") or {}),
                "primary_evidence_ids": list(answer.context_metadata.get("primary_evidence_ids") or []),
                "interpretation_context_ids": list(
                    answer.context_metadata.get("interpretation_context_ids") or []
                ),
                "locator_context": list(answer.context_metadata.get("locator_context") or []),
                "context_relationships": list(answer.context_metadata.get("context_relationships") or []),
                "required_context_ids": list(answer.context_metadata.get("required_context_ids") or []),
                "equivalent_required_context_ids": list(pair.equivalent_gold_chunk_ids or pair.gold_chunk_ids),
                "supporting_evidence_group_id": pair.supporting_evidence_group_id,
                "supporting_evidence_group": dict(pair.supporting_evidence_group),
                "evidence_pack_required_context_ids": list(
                    answer.context_metadata.get("evidence_pack_required_context_ids") or []
                ),
                "evidence_pack_expanded_chunk_ids": list(
                    answer.context_metadata.get("evidence_pack_expanded_chunk_ids") or []
                ),
                "evidence_pack_expansions": list(answer.context_metadata.get("evidence_pack_expansions") or []),
                "selected_required_context_ids": list(
                    answer.context_metadata.get("selected_required_context_ids") or []
                ),
                "missing_required_context_ids": list(
                    answer.context_metadata.get("missing_required_context_ids") or []
                ),
                "required_context_coverage": answer.context_metadata.get("required_context_coverage"),
                "gold_context_coverage": _coverage(answer.context_chunk_ids, pair.gold_chunk_ids),
                "context_score_breakdowns": context_score_breakdowns,
            },
        ))
        generated.append(answer)
    return samples, generated


def _evidence_pack_required_context_ids(pair: EvidenceQAPair) -> list[str]:
    group = dict(pair.supporting_evidence_group or {})
    return _unique_texts([
        *pair.required_primary_evidence_ids,
        *list(group.get("primary_evidence_ids") or []),
        *pair.gold_chunk_ids,
        *list(group.get("interpretation_context_ids") or []),
        *pair.acceptable_support_evidence_ids,
    ]) or list(pair.gold_chunk_ids)


def _hydrate_retrieval_with_evidence_pack(
    retrieval: RetrievalResult,
    pair: EvidenceQAPair,
    *,
    chunk_lookup: Any,
    required_context_ids: list[str],
) -> tuple[RetrievalResult, dict[str, Any]]:
    group = dict(pair.supporting_evidence_group or {})
    group_id = str(pair.supporting_evidence_group_id or group.get("group_id") or "").strip()
    metadata: dict[str, Any] = {
        "evidence_pack_group_id": group_id,
        "evidence_pack_required_context_ids": list(required_context_ids),
        "evidence_pack_hit_chunk_ids": [],
        "evidence_pack_expanded_chunk_ids": [],
        "evidence_pack_expansions": [],
    }
    get_chunk = getattr(chunk_lookup, "get_chunk", None)
    if not group_id or not callable(get_chunk):
        return retrieval, metadata

    primary_ids = _unique_texts([
        *pair.required_primary_evidence_ids,
        *list(group.get("primary_evidence_ids") or []),
        *pair.gold_chunk_ids,
    ])
    interpretation_ids = _unique_texts([
        *list(group.get("interpretation_context_ids") or []),
        *pair.acceptable_support_evidence_ids,
    ])
    group_ids = set(_unique_texts([
        *primary_ids,
        *interpretation_ids,
        *pair.equivalent_gold_chunk_ids,
        *list(group.get("equivalent_evidence_ids") or []),
    ]))
    existing_chunks = _retrieval_context_candidates(retrieval)
    existing_ids = {chunk.chunk_id for chunk in existing_chunks}
    hit_ids = [chunk.chunk_id for chunk in existing_chunks if chunk.chunk_id in group_ids]
    metadata["evidence_pack_hit_chunk_ids"] = hit_ids
    if not hit_ids:
        return retrieval, metadata

    annotated_parent_chunks = _annotate_evidence_pack_chunks(
        retrieval.parent_chunks,
        group_id=group_id,
        primary_ids=primary_ids,
        interpretation_ids=interpretation_ids,
    )
    annotated_child_chunks = _annotate_evidence_pack_chunks(
        retrieval.child_chunks,
        group_id=group_id,
        primary_ids=primary_ids,
        interpretation_ids=interpretation_ids,
    )
    annotated_ref_chunks = _annotate_evidence_pack_chunks(
        retrieval.ref_chunks,
        group_id=group_id,
        primary_ids=primary_ids,
        interpretation_ids=interpretation_ids,
    )

    expanded: list[PaperChunk] = []
    expansions: list[dict[str, Any]] = []
    expanded_from_id = hit_ids[0]
    for rank, target_id in enumerate(required_context_ids, start=1):
        if target_id in existing_ids:
            continue
        chunk = get_chunk(target_id)
        if chunk is None or chunk.paper_id != pair.paper_id:
            continue
        role = "primary_evidence" if target_id in set(primary_ids) else "interpretation_context"
        reason = _evidence_pack_expansion_reason(pair.qa_type, role)
        edge = (
            "supporting_evidence_group.primary_evidence_ids"
            if role == "primary_evidence"
            else "supporting_evidence_group.interpretation_context_ids"
        )
        hydrated = _with_evidence_pack_metadata(
            chunk,
            group_id=group_id,
            role=role,
            expanded_from_chunk_id=expanded_from_id,
            reason=reason,
            edge=edge,
            rank=rank,
        )
        expanded.append(hydrated)
        existing_ids.add(target_id)
        expansions.append({
            "group_id": group_id,
            "expanded_from_chunk_id": expanded_from_id,
            "expanded_to_chunk_id": target_id,
            "expansion_reason": reason,
            "expansion_edge": edge,
            "evidence_group_role": role,
            "rank": rank,
        })

    if not expanded:
        return (
            RetrievalResult(
                parent_chunks=annotated_parent_chunks,
                child_chunks=annotated_child_chunks,
                ref_chunks=annotated_ref_chunks,
                intent=retrieval.intent,
                metadata={**dict(retrieval.metadata), **metadata},
            ),
            metadata,
        )
    metadata["evidence_pack_expanded_chunk_ids"] = [chunk.chunk_id for chunk in expanded]
    metadata["evidence_pack_expansions"] = expansions
    return (
        RetrievalResult(
            parent_chunks=annotated_parent_chunks,
            child_chunks=annotated_child_chunks,
            ref_chunks=_dedupe_paper_chunks([*annotated_ref_chunks, *expanded]),
            intent=retrieval.intent,
            metadata={**dict(retrieval.metadata), **metadata},
        ),
        metadata,
    )


def _annotate_evidence_pack_chunks(
    chunks: Iterable[PaperChunk],
    *,
    group_id: str,
    primary_ids: list[str],
    interpretation_ids: list[str],
) -> list[PaperChunk]:
    out: list[PaperChunk] = []
    primary_set = set(primary_ids)
    interpretation_set = set(interpretation_ids)
    for chunk in chunks:
        if chunk.chunk_id in primary_set:
            out.append(_with_evidence_group_role_metadata(chunk, group_id=group_id, role="primary_evidence"))
        elif chunk.chunk_id in interpretation_set:
            out.append(_with_evidence_group_role_metadata(chunk, group_id=group_id, role="interpretation_context"))
        else:
            out.append(chunk)
    return out


def _with_evidence_group_role_metadata(
    chunk: PaperChunk,
    *,
    group_id: str,
    role: str,
) -> PaperChunk:
    metadata = dict(chunk.metadata)
    metadata.update({
        "evidence_group_id": group_id,
        "evidence_group_role": role,
    })
    return chunk.model_copy(update={"metadata": metadata})


def _retrieval_context_candidates(retrieval: RetrievalResult) -> list[PaperChunk]:
    return _dedupe_paper_chunks([
        *retrieval.child_chunks,
        *retrieval.ref_chunks,
        *retrieval.parent_chunks,
    ])


def _dedupe_paper_chunks(chunks: Iterable[PaperChunk]) -> list[PaperChunk]:
    seen: set[str] = set()
    out: list[PaperChunk] = []
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        out.append(chunk)
    return out


def _evidence_pack_expansion_reason(qa_type: str, role: str) -> str:
    prefix = "evidence"
    normalized = str(qa_type or "").casefold()
    if "formula" in normalized:
        prefix = "formula"
    elif "table" in normalized or "result" in normalized:
        prefix = "table"
    elif "figure" in normalized:
        prefix = "figure"
    elif "citation" in normalized:
        prefix = "citation"
    return f"{prefix}_group_{role}"


def _with_evidence_pack_metadata(
    chunk: PaperChunk,
    *,
    group_id: str,
    role: str,
    expanded_from_chunk_id: str,
    reason: str,
    edge: str,
    rank: int,
) -> PaperChunk:
    metadata = dict(chunk.metadata)
    metadata.update({
        "expanded_from_chunk_id": expanded_from_chunk_id,
        "expansion_reason": reason,
        "expansion_edge": edge,
        "expansion_rank": rank,
        "evidence_group_id": group_id,
        "evidence_group_role": role,
        "evidence_pack_expansion": True,
    })
    return chunk.model_copy(update={"metadata": metadata})


def _answer_generation_pairs(pairs: list[EvidenceQAPair], config: BenchmarkSuiteConfig) -> list[EvidenceQAPair]:
    sample_size = _answer_generation_sample_size(config)
    if sample_size is None:
        return list(pairs)
    return _stable_sample(list(pairs), sample_size=max(0, sample_size), seed=f"{config.split_seed}:answer_eval")


def _answer_generation_sample_size(config: BenchmarkSuiteConfig) -> int | None:
    if config.answer_eval_enabled:
        return config.answer_eval_sample_size
    requested_sizes: list[int | None] = []
    if _answer_judge_enabled(config):
        requested_sizes.append(config.answer_judge_sample_size)
    if config.spot_check_sample_size > 0:
        requested_sizes.append(config.spot_check_sample_size)
    if not requested_sizes:
        return 0
    if any(size is None for size in requested_sizes):
        return None
    return max(max(0, int(size)) for size in requested_sizes if size is not None)


def _build_answer_judge_llm_call(*, max_tokens: int) -> LLMCall:
    from backend.research.application.llm_client import build_unity_judge_llm_call
    return build_unity_judge_llm_call(max_tokens=max_tokens, temperature=0)


def _context_chunks_for_answer(retrieval: Any, context_chunk_ids: list[str]) -> list[PaperChunk]:
    candidates = [
        *getattr(retrieval, "child_chunks", []),
        *getattr(retrieval, "ref_chunks", []),
        *getattr(retrieval, "parent_chunks", []),
    ]
    by_id = {chunk.chunk_id: chunk for chunk in candidates}
    return [chunk for chunk_id in context_chunk_ids if (chunk := by_id.get(chunk_id)) is not None]


def _chunk_source_locator(chunk: PaperChunk | None) -> str:
    if chunk is None:
        return ""
    return str(chunk.metadata.get("source_locator") or chunk.metadata.get("source_ref") or "")


def _write_answer_samples(
    path: Path,
    samples: list[EvidenceAnswerSample],
    generated_answers: list[GeneratedAnswer | None],
    *,
    answer_result: Any | None = None,
) -> None:
    score_by_key = _answer_score_by_key(answer_result)
    records = [
        _answer_sample_record(
            sample,
            generated,
            score=score_by_key.get(_answer_sample_key(sample)),
        )
        for sample, generated in zip(samples, generated_answers, strict=True)
    ]
    _write_jsonl(path, records)


def _answer_sample_record(
    sample: EvidenceAnswerSample,
    generated: GeneratedAnswer | None,
    *,
    score: Any | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "question": sample.pair.question,
        "paper_id": sample.pair.paper_id,
        "qa_type": sample.pair.qa_type,
        "expected_behavior": sample.pair.expected_behavior,
        "outcome": sample.outcome.value,
        "answer": sample.answer,
        "answer_facts": list(sample.pair.answer_facts),
        "gold_chunk_ids": list(sample.pair.gold_chunk_ids),
        "equivalent_gold_chunk_ids": list(sample.pair.equivalent_gold_chunk_ids),
        "supporting_evidence_group_id": sample.pair.supporting_evidence_group_id,
        "supporting_evidence_group": dict(sample.pair.supporting_evidence_group),
        "gold_source_locators": list(sample.pair.gold_source_locators),
        "context_chunk_ids": list(sample.context_chunk_ids),
        "cited_chunk_ids": list(sample.cited_chunk_ids),
        "cited_source_locators": list(sample.cited_source_locators),
        "contexts": list(generated.contexts) if generated is not None else [],
        "context_score_breakdowns": dict(sample.metadata.get("context_score_breakdowns") or {}),
        "metadata": dict(sample.metadata),
        "context_metadata": dict(generated.context_metadata) if generated is not None else {},
    }
    if score is not None:
        record["deterministic_scores"] = {
            "fact_coverage": score.fact_coverage,
            "fact_match_coverage": score.fact_coverage,
            "retrieval_context_coverage": score.retrieval_context_coverage,
            "citation_grounding": score.citation_grounding,
            "citation_gold_coverage": score.citation_gold_coverage,
            "strict_retrieval_context_coverage": score.strict_retrieval_context_coverage,
            "equivalent_retrieval_context_coverage": score.equivalent_retrieval_context_coverage,
            "strict_citation_gold_coverage": score.strict_citation_gold_coverage,
            "equivalent_citation_gold_coverage": score.equivalent_citation_gold_coverage,
            "equivalent_gold_supported": score.equivalent_gold_supported,
            "claim_support_coverage": score.claim_support_coverage,
            "diagnostic_tags": list(score.diagnostic_tags),
            "source_locator_grounding": score.source_locator_grounding,
            "abstention_correct": score.abstention_correct,
            "answer_success": score.answer_success,
            "failure_reason": score.failure_reason,
            "matched_facts": list(score.matched_facts),
            "missing_facts": list(score.missing_facts),
        }
    return record


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _context_score_breakdowns(chunks: list[PaperChunk]) -> dict[str, dict[str, float]]:
    return {
        chunk.chunk_id: breakdown
        for chunk in chunks
        if (breakdown := paper_chunk_to_rag_evidence(chunk).score_breakdown.to_dict())
    }


def _answer_judge_sample_records(
    answer_samples: list[EvidenceAnswerSample],
    generated_answers: list[GeneratedAnswer | None],
    config: BenchmarkSuiteConfig,
) -> list[tuple[EvidenceAnswerSample, GeneratedAnswer]]:
    records = [
        (sample, generated)
        for sample, generated in zip(answer_samples, generated_answers, strict=True)
        if sample.outcome is AnswerOutcome.ANSWERED and generated is not None
    ]
    sample_size = config.answer_judge_sample_size
    if sample_size is None:
        return records
    return records[:max(0, sample_size)]


async def _run_answer_judge(
    config: BenchmarkSuiteConfig,
    generated_answers: list[GeneratedAnswer],
) -> GenerationEvalResult:
    mode = str(config.answer_judge_mode or "none").strip().casefold()
    if mode != "llm":
        raise ValueError("answer_judge_mode must be 'none' or 'llm'")
    llm_call = config.answer_judge_llm_call or _build_answer_judge_llm_call(max_tokens=900)
    return await GenerationEvaluator(llm_call).evaluate(generated_answers)


def _write_answer_judge_artifacts(
    output_dir: Path,
    answer_judge_records: list[tuple[EvidenceAnswerSample, GeneratedAnswer]],
    generation_result: GenerationEvalResult,
    *,
    answer_result: Any | None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    score_by_key = _answer_score_by_key(answer_result)
    records = []
    failures = []
    for index, (sample, generated) in enumerate(answer_judge_records):
        judgment = _generation_judgment_at(generation_result, index)
        record = _answer_sample_record(
            sample,
            generated,
            score=score_by_key.get(_answer_sample_key(sample)),
        )
        if judgment is not None:
            record["llm_judge"] = judgment.to_dict()
        records.append(record)
        if judgment is not None and _answer_judge_failure_reasons(record):
            failures.append(_answer_judge_failure_record(record))
    (output_dir / "answer_judge_report.json").write_text(
        json.dumps(_answer_judge_report_payload(generation_result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_jsonl(output_dir / "answer_judge_samples.jsonl", records)
    _write_jsonl(output_dir / "answer_judge_failures.jsonl", failures)


def _answer_judge_report_payload(generation_result: GenerationEvalResult) -> dict[str, Any]:
    return {
        "total": len(generation_result.per_sample),
        "claim_support_rate": generation_result.claim_support_rate_score(),
        "contradiction_rate": generation_result.contradiction_rate_score(),
        "unsupported_claim_rate": generation_result.unsupported_claim_rate_score(),
        "citation_grounding_rate": generation_result.citation_grounding_rate_score(),
        "citation_claim_support_rate": generation_result.citation_claim_support_rate_score(),
        "wrong_citation_rate": generation_result.wrong_citation_rate_score(),
        "missing_citation_rate": generation_result.missing_citation_rate_score(),
        "grounded_answer_rate": generation_result.grounded_answer_rate_score(),
        "answer_faithfulness": generation_result.faithfulness_score(),
        "answer_relevance": generation_result.answer_relevancy_score(),
        "context_precision": generation_result.context_precision_score(),
        "judge_error_rate": generation_result.judge_error_rate(),
        "items": [judgment.to_dict() for judgment in generation_result.sample_judgments],
    }


def _write_answer_fix_artifacts(
    output_dir: Path,
    *,
    answer_judge_records: list[tuple[EvidenceAnswerSample, GeneratedAnswer]],
    generation_result: GenerationEvalResult | None,
    spot_check: SpotCheckReport | None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    if generation_result is not None:
        for index, (sample, generated) in enumerate(answer_judge_records):
            judgment = _generation_judgment_at(generation_result, index)
            if judgment is None:
                continue
            base = _answer_sample_record(sample, generated)
            base["llm_judge"] = judgment.to_dict()
            reasons = _answer_judge_failure_reasons(base)
            if not reasons:
                continue
            items.append(_answer_fix_record(base, reasons=reasons))
    if spot_check is not None:
        for conflict in (spot_check.calibration or {}).get("conflicts", []) or []:
            items.append({
                "paper_id": conflict.get("paper_id", ""),
                "qa_type": conflict.get("qa_type", ""),
                "question": conflict.get("question", ""),
                "failure_reasons": ["judge_human_conflict"],
                "suggested_action": _ANSWER_FIX_REASON_ACTIONS["judge_human_conflict"],
                "source": "human_spot_check_conflict",
                "details": conflict,
            })
    action_counts = Counter(str(item.get("suggested_action") or "") for item in items)
    reason_counts: Counter[str] = Counter()
    for item in items:
        reason_counts.update(item.get("failure_reasons") or [])
    manifest = {
        "total": len(items),
        "reason_counts": dict(sorted(reason_counts.items())),
        "action_counts": dict(sorted(action_counts.items())),
        "items": items,
    }
    (output_dir / "answer_fix_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _answer_judge_failure_record(record: dict[str, Any]) -> dict[str, Any]:
    return _answer_fix_record(record, reasons=_answer_judge_failure_reasons(record))


def _answer_fix_record(record: dict[str, Any], *, reasons: list[str]) -> dict[str, Any]:
    action = _suggested_answer_fix_action(reasons)
    return {
        "paper_id": record.get("paper_id", ""),
        "qa_type": record.get("qa_type", ""),
        "question": record.get("question", ""),
        "failure_reasons": reasons,
        "suggested_action": action,
        "source": "answer_judge",
        "gold_chunk_ids": list(record.get("gold_chunk_ids") or []),
        "context_chunk_ids": list(record.get("context_chunk_ids") or []),
        "cited_chunk_ids": list(record.get("cited_chunk_ids") or []),
        "deterministic_failure_reason": ((record.get("deterministic_scores") or {}).get("failure_reason") or ""),
        "llm_scores": ((record.get("llm_judge") or {}).get("scores") or {}),
    }


def _answer_judge_failure_reasons(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    outcome = str(record.get("outcome") or "")
    expected_behavior = str(record.get("expected_behavior") or "answer")
    if outcome in {AnswerOutcome.ABSTAINED.value, AnswerOutcome.INSUFFICIENT_EVIDENCE.value}:
        if expected_behavior == "answer":
            reasons.append(OVERCONSERVATIVE_ABSTENTION_REASON)
    elif expected_behavior == "abstain" and outcome == AnswerOutcome.ANSWERED.value:
        reasons.append(WRONG_ABSTENTION_REASON)
    deterministic = record.get("deterministic_scores") or {}
    deterministic_reason = str(deterministic.get("failure_reason") or "")
    if deterministic_reason and deterministic_reason not in _ABSTENTION_FAILURE_REASON_ALIASES:
        reasons.append(deterministic_reason)
    judge = record.get("llm_judge") or {}
    scores = judge.get("scores") or {}
    if bool(scores.get("judge_error")) or judge.get("status") == "error":
        reasons.append("judge_error")
    if _safe_float(scores.get("claim_support_rate")) < PROMOTION_THRESHOLDS["claim_support_rate"]:
        reasons.append("unsupported_claim")
    if _safe_float(scores.get("contradiction_rate")) > 0.0:
        reasons.append("contradicted_claim")
    if _safe_float(scores.get("wrong_citation_rate")) > 0.0:
        reasons.append("wrong_citation")
    if _safe_float(scores.get("missing_citation_rate")) > 0.0:
        reasons.append("missing_citation")
    if _safe_float(scores.get("citation_claim_support_rate")) < PROMOTION_THRESHOLDS["citation_claim_support_rate"]:
        reasons.append("wrong_citation")
    return _unique_texts(reasons)


def _suggested_answer_fix_action(reasons: list[str]) -> str:
    for reason in reasons:
        action = _ANSWER_FIX_REASON_ACTIONS.get(reason)
        if action:
            return action
    return "manual_review_required"


def _write_spot_check_report(
    config: BenchmarkSuiteConfig,
    *,
    output_dir: Path,
    answer_samples: list[EvidenceAnswerSample],
    generated_answers: list[GeneratedAnswer | None],
    answer_result: Any | None,
    generation_result: GenerationEvalResult | None,
    answer_judge_records: list[tuple[EvidenceAnswerSample, GeneratedAnswer]],
) -> SpotCheckReport | None:
    if config.spot_check_sample_size <= 0:
        return _spot_check_report_from_annotations(
            config,
            output_dir=output_dir,
            sample_size=0,
            answer_judge_by_key=_answer_judge_by_key(answer_judge_records, generation_result),
        )
    score_by_key = _answer_score_by_key(answer_result)
    judge_by_key = _answer_judge_by_key(answer_judge_records, generation_result)
    records = [
        _answer_sample_record(
            sample,
            generated,
            score=score_by_key.get(_answer_sample_key(sample)),
        )
        for sample, generated in zip(answer_samples, generated_answers, strict=True)
    ]
    for record in records:
        judgment = judge_by_key.get(_answer_record_key(record))
        if judgment is not None:
            record["llm_judge"] = judgment.to_dict()
    records = _spot_check_sample_records(records, config)
    path = output_dir / "spot_check_samples.jsonl"
    _write_jsonl(path, records)
    annotation_summary = _spot_check_report_from_annotations(
        config,
        output_dir=output_dir,
        sample_size=len(records),
        sample_path=path,
        answer_judge_by_key=judge_by_key,
    )
    return annotation_summary or SpotCheckReport(
        sample_path=str(path),
        sample_size=len(records),
    )


def _answer_score_by_key(answer_result: Any | None) -> dict[tuple[str, str, str], Any]:
    if answer_result is None:
        return {}
    return {
        _answer_sample_key(score.sample): score
        for score in answer_result.scores
    }


def _answer_sample_key(sample: EvidenceAnswerSample) -> tuple[str, str, str]:
    return (sample.pair.paper_id, sample.pair.qa_type, sample.pair.question)


def _spot_check_sample_records(records: list[dict[str, Any]], config: BenchmarkSuiteConfig) -> list[dict[str, Any]]:
    sample_size = max(0, config.spot_check_sample_size)
    if sample_size <= 0:
        return []
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    by_type: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_type.setdefault(str(record.get("qa_type") or "unknown"), []).append(record)
    qa_order = [
        *DEFAULT_TARGET_QA_TYPES,
        "negative_qa",
        *sorted(set(by_type) - set(DEFAULT_TARGET_QA_TYPES) - {"negative_qa"}),
    ]
    for qa_type in qa_order:
        candidates = by_type.get(qa_type) or []
        if not candidates or len(selected) >= sample_size:
            continue
        chosen = sorted(candidates, key=lambda record: _spot_check_priority(record, config))[0]
        selected.append(chosen)
        seen.add(_answer_record_key(chosen))
    for record in sorted(records, key=lambda item: _spot_check_priority(item, config)):
        if len(selected) >= sample_size:
            break
        key = _answer_record_key(record)
        if key in seen:
            continue
        selected.append(record)
        seen.add(key)
    return selected


def _spot_check_priority(record: dict[str, Any], config: BenchmarkSuiteConfig) -> tuple[int, int, int, str]:
    scores = record.get("deterministic_scores") or {}
    judge_scores = ((record.get("llm_judge") or {}).get("scores") or {})
    deterministic_failed = scores.get("answer_success") is False
    llm_pass = _llm_judge_passes(judge_scores)
    llm_failed = llm_pass is False
    conflict = scores.get("answer_success") is not None and llm_pass is not None and bool(scores.get("answer_success")) != llm_pass
    complex_type = str(record.get("qa_type") or "") in {
        "formula_qa",
        "formula_explanation_qa",
        "table_qa",
        "figure_qa",
        "experiment_result_qa",
        "negative_qa",
    }
    key = _stable_split_key(config.split_seed, f"spot:{record.get('paper_id')}:{record.get('question')}")
    return (
        0 if deterministic_failed or llm_failed else 1,
        0 if conflict else 1,
        0 if complex_type else 1,
        key,
    )


def _spot_check_report_from_annotations(
    config: BenchmarkSuiteConfig,
    *,
    output_dir: Path,
    sample_size: int,
    sample_path: Path | None = None,
    answer_judge_by_key: dict[tuple[str, str, str], Any] | None = None,
) -> SpotCheckReport | None:
    path = config.spot_check_annotations_path
    if path is None:
        return None
    if not path.exists():
        raise FileNotFoundError(f"spot check annotations not found: {path}")
    counts: Counter[str] = Counter()
    by_qa_type: dict[str, Counter[str]] = {}
    boolean_counts: dict[str, Counter[str]] = {field: Counter() for field in _SPOT_CHECK_BOOLEAN_FIELDS}
    calibration_counts: Counter[str] = Counter()
    calibration_by_type: dict[str, Counter[str]] = {}
    conflicts: list[dict[str, Any]] = []
    annotated = 0
    schema_errors = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            item = json.loads(line)
            label = _spot_check_label(item)
            counts[label] += 1
            qa_type = str(item.get("qa_type") or "unknown").strip() or "unknown"
            type_counts = by_qa_type.setdefault(qa_type, Counter())
            type_counts[label] += 1
            if _spot_check_schema_errors(item):
                schema_errors += 1
            for field in _SPOT_CHECK_BOOLEAN_FIELDS:
                if isinstance(item.get(field), bool):
                    boolean_counts[field]["true" if item[field] else "false"] += 1
            calibration_item = _spot_check_calibration_item(item, answer_judge_by_key or {})
            if calibration_item:
                calibration_counts[calibration_item["bucket"]] += 1
                calibration_counts[calibration_item["outcome"]] += 1
                type_calibration = calibration_by_type.setdefault(qa_type, Counter())
                type_calibration[calibration_item["bucket"]] += 1
                type_calibration[calibration_item["outcome"]] += 1
                if calibration_item["bucket"] == "conflict":
                    conflicts.append(calibration_item)
            annotated += 1
    calibration = _spot_check_calibration_summary(calibration_counts)
    calibration["judge_by_qa_type"] = {
        qa_type: _spot_check_calibration_summary(counter)
        for qa_type, counter in sorted(calibration_by_type.items())
    }
    calibration["conflicts"] = conflicts
    conflicts_path = output_dir / "human_spot_check_conflicts.jsonl"
    _write_jsonl(conflicts_path, conflicts)
    return SpotCheckReport(
        sample_path=str(sample_path or output_dir / "spot_check_samples.jsonl"),
        sample_size=sample_size,
        annotation_path=str(path),
        annotated_count=annotated,
        label_counts=dict(counts),
        by_qa_type={qa_type: dict(counter) for qa_type, counter in by_qa_type.items()},
        schema_error_count=schema_errors,
        boolean_counts={field: dict(counter) for field, counter in boolean_counts.items()},
        calibration=calibration,
        conflict_count=len(conflicts),
    )


def _spot_check_label(item: dict[str, Any]) -> str:
    label = str(
        item.get("label")
        or item.get("status")
        or item.get("verdict")
        or "unknown"
    ).strip().casefold() or "unknown"
    return label if label in _SPOT_CHECK_LABELS else "unknown"


def _spot_check_schema_errors(item: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("paper_id", "qa_type", "question", "label", "reason"):
        if not str(item.get(key) or "").strip():
            errors.append(f"missing_{key}")
    label = str(item.get("label") or item.get("status") or item.get("verdict") or "").strip().casefold()
    if label not in _SPOT_CHECK_LABELS:
        errors.append("invalid_label")
    for key in _SPOT_CHECK_BOOLEAN_FIELDS:
        if key in item and not isinstance(item[key], bool):
            errors.append(f"invalid_{key}")
    if "correct_support_chunk_ids" in item and not isinstance(item["correct_support_chunk_ids"], list):
        errors.append("invalid_correct_support_chunk_ids")
    return errors


def _spot_check_calibration_item(
    item: dict[str, Any],
    answer_judge_by_key: dict[tuple[str, str, str], Any],
) -> dict[str, Any] | None:
    key = (
        str(item.get("paper_id") or ""),
        str(item.get("qa_type") or ""),
        str(item.get("question") or ""),
    )
    judgment = answer_judge_by_key.get(key)
    if judgment is None:
        return None
    human_pass = _human_annotation_passes(item)
    llm_pass = _llm_judge_passes(judgment.scores.to_dict())
    if llm_pass is None:
        return None
    bucket = "agree" if human_pass == llm_pass else "conflict"
    if human_pass and llm_pass:
        outcome = "true_positive"
    elif human_pass and not llm_pass:
        outcome = "false_negative"
    elif not human_pass and llm_pass:
        outcome = "false_positive"
    else:
        outcome = "true_negative"
    return {
        "bucket": bucket,
        "outcome": outcome,
        "paper_id": key[0],
        "qa_type": key[1],
        "question": key[2],
        "human_pass": human_pass,
        "llm_pass": llm_pass,
        "human_label": _spot_check_label(item),
        "llm_status": judgment.status,
        "llm_scores": judgment.scores.to_dict(),
        "reason": str(item.get("reason") or ""),
    }


def _human_annotation_passes(item: dict[str, Any]) -> bool:
    label = _spot_check_label(item)
    if label != "pass":
        return False
    for field in ("gold_evidence_ok", "answer_ok", "faithfulness_ok", "citation_ok"):
        if item.get(field) is False:
            return False
    return True


def _spot_check_calibration_summary(counts: Counter[str]) -> dict[str, Any]:
    agree = int(counts.get("agree", 0))
    conflict = int(counts.get("conflict", 0))
    compared = agree + conflict
    tp = int(counts.get("true_positive", 0))
    tn = int(counts.get("true_negative", 0))
    fp = int(counts.get("false_positive", 0))
    fn = int(counts.get("false_negative", 0))
    return {
        "compared_count": compared,
        "judge_human_agreement": agree / compared if compared else 0.0,
        "conflict_count": conflict,
        "true_positive": tp,
        "true_negative": tn,
        "false_positive": fp,
        "false_negative": fn,
        "judge_precision": tp / (tp + fp) if (tp + fp) else 0.0,
        "judge_recall": tp / (tp + fn) if (tp + fn) else 0.0,
        "judge_false_positive_rate": fp / (fp + tn) if (fp + tn) else 0.0,
        "judge_false_negative_rate": fn / (fn + tp) if (fn + tp) else 0.0,
    }


def _answer_judge_by_key(
    answer_judge_records: list[tuple[EvidenceAnswerSample, GeneratedAnswer]],
    generation_result: GenerationEvalResult | None,
) -> dict[tuple[str, str, str], Any]:
    if generation_result is None:
        return {}
    out: dict[tuple[str, str, str], Any] = {}
    for index, (sample, _generated) in enumerate(answer_judge_records):
        judgment = _generation_judgment_at(generation_result, index)
        if judgment is not None:
            out[_answer_sample_key(sample)] = judgment
    return out


def _generation_judgment_at(generation_result: GenerationEvalResult, index: int) -> Any | None:
    if 0 <= index < len(generation_result.sample_judgments):
        return generation_result.sample_judgments[index]
    if 0 <= index < len(generation_result.per_sample):
        score = generation_result.per_sample[index]
        return type("_ScoreOnlyJudgment", (), {
            "scores": score,
            "status": "error" if score.judge_error else "pass",
            "to_dict": lambda self: {"scores": score.to_dict(), "status": self.status},
        })()
    return None


def _answer_record_key(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("paper_id") or ""),
        str(record.get("qa_type") or ""),
        str(record.get("question") or ""),
    )


def _llm_judge_passes(scores: dict[str, Any]) -> bool | None:
    if not scores:
        return None
    if bool(scores.get("judge_error")):
        return False
    return (
        _safe_float(scores.get("claim_support_rate")) >= PROMOTION_THRESHOLDS["claim_support_rate"]
        and _safe_float(scores.get("citation_claim_support_rate")) >= PROMOTION_THRESHOLDS["citation_claim_support_rate"]
        and _safe_float(scores.get("unsupported_claim_rate")) <= PROMOTION_THRESHOLDS["unsupported_claim_rate"]
    )


def _boolean_true_rate(counts: dict[str, int]) -> float:
    true_count = int(counts.get("true", 0))
    false_count = int(counts.get("false", 0))
    total = true_count + false_count
    return true_count / total if total else 0.0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _evaluate_fixed_window_baseline(
    config: BenchmarkSuiteConfig,
    chunks: list[PaperChunk],
    pairs: list[EvidenceQAPair],
    output_dir: Path,
) -> dict[str, Any]:
    baseline_chunks = FixedWindowBaselineChunker(FixedWindowChunkerConfig(
        window_tokens=config.fixed_window_tokens,
        overlap_tokens=config.fixed_window_overlap_tokens,
    )).chunk(chunks)
    retriever, _visual_store = _build_live_retriever(
        baseline_chunks,
        visual_enabled=False,
        image_root=None,
        retrieval_policy="",
    )
    retrieval = EvidenceRetrievalEvaluator(retriever).evaluate(pairs)
    metadata = {
        "mode": "fixed_window_baseline",
        "chunks_total": len(baseline_chunks),
        "window_tokens": config.fixed_window_tokens,
        "overlap_tokens": FixedWindowChunkerConfig(
            window_tokens=config.fixed_window_tokens,
            overlap_tokens=config.fixed_window_overlap_tokens,
        ).overlap_tokens,
    }
    report = EvidenceRegressionReport(retrieval=retrieval, metadata=metadata)
    report.write(output_dir)
    return json.loads((output_dir / "evidence_regression_report.json").read_text(encoding="utf-8"))


def _compare_reports(candidate_report: dict[str, Any], baseline_report: dict[str, Any]) -> dict[str, Any]:
    candidate = candidate_report.get("retrieval") or {}
    baseline = baseline_report.get("retrieval") or {}
    deltas: dict[str, Any] = {
        "mrr": _metric(candidate, "mrr") - _metric(baseline, "mrr"),
        "by_k": {},
    }
    for k in ("1", "3", "5", "10"):
        c = ((candidate.get("by_k") or {}).get(k) or {})
        b = ((baseline.get("by_k") or {}).get(k) or {})
        deltas["by_k"][k] = {
            "hit_rate": _metric(c, "hit_rate") - _metric(b, "hit_rate"),
            "evidence_coverage": _metric(c, "evidence_coverage") - _metric(b, "evidence_coverage"),
            "source_locator_coverage": _metric(c, "source_locator_coverage") - _metric(b, "source_locator_coverage"),
        }
    relative = {
        "mrr": _relative_improvement(_metric(candidate, "mrr"), _metric(baseline, "mrr")),
        "by_k": {},
    }
    for k in ("1", "3", "5", "10"):
        c = ((candidate.get("by_k") or {}).get(k) or {})
        b = ((baseline.get("by_k") or {}).get(k) or {})
        relative["by_k"][k] = {
            "hit_rate": _relative_improvement(_metric(c, "hit_rate"), _metric(b, "hit_rate")),
            "evidence_coverage": _relative_improvement(_metric(c, "evidence_coverage"), _metric(b, "evidence_coverage")),
            "source_locator_coverage": _relative_improvement(_metric(c, "source_locator_coverage"), _metric(b, "source_locator_coverage")),
        }
    return {
        "baseline_name": "fixed_window",
        "candidate_name": str((candidate_report.get("metadata") or {}).get("retrieval_policy") or "candidate"),
        "baseline": _summary_from_report(baseline_report),
        "candidate": _summary_from_report(candidate_report),
        "macro_by_qa_type": _macro_by_qa_type(candidate, baseline),
        "deltas": deltas,
        "relative_improvement": relative,
    }


def _run_gold_judge(
    config: BenchmarkSuiteConfig,
    audit: GoldEvidenceAuditReport,
) -> GoldEvidenceJudgeReport | None:
    mode = str(config.gold_judge_mode or "none").strip().casefold()
    if mode in {"", "none", "off", "false", "0"}:
        return None
    items = [item for item in audit.items if _gold_judge_eligible(item)]
    if config.gold_judge_sample_size is not None:
        items = _gold_judge_sample(
            items,
            sample_size=max(0, config.gold_judge_sample_size),
            seed=f"{config.split_seed}:gold_judge",
        )
    judge = config.gold_evidence_judge
    if judge is None:
        if mode != "llm":
            raise ValueError("gold_evidence_judge is required when gold_judge_mode is not 'none' or 'llm'")
        judge = OpenAICompatibleGoldEvidenceJudge.from_env(
            max_evidence_chars=config.gold_judge_max_evidence_chars,
        )
    return judge.judge(items)


def _audit_pair(pair: EvidenceQAPair, chunks_by_id: dict[str, PaperChunk]) -> GoldEvidenceAuditItem:
    missing = tuple(chunk_id for chunk_id in pair.gold_chunk_ids if chunk_id not in chunks_by_id)
    gold_chunks = [chunks_by_id[chunk_id] for chunk_id in pair.gold_chunk_ids if chunk_id in chunks_by_id]
    preview_ids = _gold_audit_preview_chunk_ids(pair)
    preview_chunks = [chunks_by_id[chunk_id] for chunk_id in preview_ids if chunk_id in chunks_by_id]
    answer_facts_present = any(str(fact).strip() for fact in pair.answer_facts)
    locator_count = sum(1 for chunk in gold_chunks if chunk.metadata.get("source_locator") or chunk.metadata.get("source_ref"))
    image_count = sum(1 for chunk in gold_chunks if chunk.metadata.get("image_ref"))
    figure_evidence_count = sum(1 for chunk in gold_chunks if _has_usable_figure_evidence(chunk))
    required_types = set(pair.required_evidence_types)
    available_types = {_evidence_type(chunk) for chunk in gold_chunks}
    if missing:
        status = "fail"
        reason = "missing_gold_chunks"
    elif required_types and not required_types.issubset(available_types):
        status = "warning"
        reason = "required_evidence_type_not_fully_represented"
    elif pair.qa_type == "figure_qa" and image_count == 0 and figure_evidence_count == 0:
        status = "warning"
        reason = "visual_qa_without_image_ref"
    elif not answer_facts_present and pair.expected_behavior == "answer":
        status = "warning"
        reason = "answerable_pair_without_answer_facts"
    else:
        status = "pass"
        reason = "ok"
    return GoldEvidenceAuditItem(
        question=pair.question,
        paper_id=pair.paper_id,
        qa_type=pair.qa_type,
        status=status,
        reason=reason,
        gold_chunk_ids=tuple(pair.gold_chunk_ids),
        missing_chunk_ids=missing,
        answer_facts_present=answer_facts_present,
        source_locator_count=locator_count,
        image_ref_count=image_count,
        answer_facts=tuple(str(fact) for fact in pair.answer_facts if str(fact).strip())[:3],
        equivalent_gold_chunk_ids=tuple(pair.equivalent_gold_chunk_ids),
        supporting_evidence_group_id=pair.supporting_evidence_group_id,
        evidence_previews=tuple(_evidence_preview(chunk) for chunk in preview_chunks[:8]),
    )


def _gold_judge_eligible(item: GoldEvidenceAuditItem) -> bool:
    return item.qa_type != "negative_qa" and bool(item.gold_chunk_ids)


def _has_usable_figure_evidence(chunk: PaperChunk) -> bool:
    if chunk.metadata.get("image_ref"):
        return True
    for key in ("visual_description", "caption_text", "surya_caption"):
        if _meaningful_text(str(chunk.metadata.get(key) or "")):
            return True
    caption = _caption_block_for_audit(chunk.content)
    if _meaningful_text(caption):
        return True
    return _meaningful_text(chunk.content, min_chars=48)


def _gold_audit_preview_chunk_ids(pair: EvidenceQAPair) -> list[str]:
    group = dict(pair.supporting_evidence_group or {})
    return _unique_texts([
        *pair.gold_chunk_ids,
        *pair.equivalent_gold_chunk_ids,
        *list(group.get("primary_evidence_ids") or []),
        *list(group.get("interpretation_context_ids") or []),
        *list(group.get("equivalent_evidence_ids") or []),
        *pair.acceptable_support_evidence_ids,
    ]) or list(pair.gold_chunk_ids)


def _meaningful_text(value: str, *, min_chars: int = 12) -> bool:
    normalized = " ".join(str(value or "").split())
    return len(normalized) >= min_chars


def _target_counts(pairs: Iterable[EvidenceQAPair]) -> dict[str, int]:
    counts = Counter(pair.qa_type for pair in pairs if pair.qa_type in DEFAULT_TARGET_QA_TYPES)
    return dict(sorted(counts.items()))


def _build_policy_promotion_checklist(
    *,
    config: BenchmarkSuiteConfig,
    question_profile: str,
    splits: dict[str, BenchmarkSplit],
    question_audit: QuestionAmbiguityAuditReport,
    gold_audit: GoldEvidenceAuditReport,
    judge_report: GoldEvidenceJudgeReport | None,
    spot_check: SpotCheckReport | None,
    candidate_report: dict[str, Any],
) -> PolicyPromotionChecklist:
    retrieval = candidate_report.get("retrieval") or {}
    metadata = candidate_report.get("metadata") or {}
    answer = candidate_report.get("answer") or None
    policy_name = str(metadata.get("retrieval_policy") or config.retrieval_policy or "")
    promoted_policies = {PAPER_BLIND_SEMANTIC_RAG_V1_POLICY, PAPER_HYBRID_RRF_RAG_V1_POLICY}
    checks = [
        _promotion_check(
            "policy_name",
            "Explicit promotion-eligible Paper RAG policy selected",
            policy_name in promoted_policies,
            actual=policy_name,
            threshold=sorted(promoted_policies),
        ),
        _promotion_check(
            "question_profile",
            "Blind semantic question profile selected",
            _normalize_question_profile(question_profile) == "blind_semantic",
            actual=question_profile,
            threshold="blind_semantic",
        ),
        _promotion_check(
            "split_protocol",
            "Train/dev/test split is present and test is reported",
            all(splits.get(name) and splits[name].pair_count > 0 for name in ("train", "dev", "test")),
            actual={name: splits[name].pair_count for name in ("train", "dev", "test") if name in splits},
            threshold="non-empty train/dev/test",
            details="tuning_split=dev; reported_split=test",
        ),
        _promotion_check(
            "gold_audit",
            "Gold evidence audit has no warning or failure",
            gold_audit.failed == 0 and gold_audit.warning == 0,
            actual={"warning": gold_audit.warning, "failed": gold_audit.failed},
            threshold={"warning": 0, "failed": 0},
        ),
        _gold_judge_quality_check(
            config=config,
            question_profile=question_profile,
            judge_report=judge_report,
        ),
        _human_spot_check_quality_check(spot_check),
        _promotion_check(
            "ambiguity_audit",
            "Blind questions remain low ambiguity without label leakage",
            (
                question_audit.label_leakage == 0
                and question_audit.ambiguous_question_rate <= PROMOTION_THRESHOLDS["ambiguous_question_rate"]
                and question_audit.caption_copy_rate <= PROMOTION_THRESHOLDS["caption_copy_rate"]
            ),
            actual={
                "ambiguous_question_rate": round(question_audit.ambiguous_question_rate, 6),
                "label_leakage": question_audit.label_leakage,
                "caption_copy_rate": round(question_audit.caption_copy_rate, 6),
            },
            threshold={
                "ambiguous_question_rate": PROMOTION_THRESHOLDS["ambiguous_question_rate"],
                "label_leakage": 0,
                "caption_copy_rate": PROMOTION_THRESHOLDS["caption_copy_rate"],
            },
        ),
        _promotion_check(
            "route_distribution",
            "Route distribution is present",
            bool(retrieval.get("route_distribution")),
            actual=bool(retrieval.get("route_distribution")),
        ),
        _promotion_check(
            "field_embedding_distribution",
            "Field embedding distribution is present",
            _field_distribution_present(retrieval),
            actual=(retrieval.get("field_embedding_distribution") or {}).get("matched_evidence_count", 0),
            threshold="matched_evidence_count > 0",
        ),
        _promotion_check(
            "rerank_distribution",
            "Rerank distribution is present",
            _rerank_distribution_present(retrieval),
            actual=(retrieval.get("rerank_distribution") or {}).get("reranked_evidence_count", 0),
            threshold="reranked_evidence_count > 0",
        ),
        _metric_promotion_check(
            "overall_hit_at_3",
            "Overall Hit@3 meets staged retrieval gate",
            _hit_at_k(retrieval, 3),
            PROMOTION_THRESHOLDS["overall_hit_at_3"],
        ),
        _metric_promotion_check(
            "overall_hit_at_5",
            "Overall Hit@5 meets staged retrieval gate",
            _hit_at_k(retrieval, 5),
            PROMOTION_THRESHOLDS["overall_hit_at_5"],
        ),
        _metric_promotion_check(
            "overall_hit_at_10",
            "Overall Hit@10 meets PRD V5 gate",
            _hit_at_10(retrieval),
            PROMOTION_THRESHOLDS["overall_hit_at_10"],
        ),
        _metric_promotion_check(
            "overall_evidence_coverage_at_5",
            "Overall evidence coverage@5 meets staged retrieval gate",
            _by_k_metric(retrieval, 5, "evidence_coverage"),
            PROMOTION_THRESHOLDS["overall_evidence_coverage_at_5"],
        ),
        _metric_promotion_check(
            "overall_source_locator_coverage_at_5",
            "Overall source locator coverage@5 meets staged retrieval gate",
            _by_k_metric(retrieval, 5, "source_locator_coverage"),
            PROMOTION_THRESHOLDS["overall_source_locator_coverage_at_5"],
        ),
        _promotion_check(
            "top_k_retrieval_metrics",
            "Top-k retrieval metrics include @3, @5, and @10",
            _top_k_metrics_present(retrieval),
            actual=sorted((retrieval.get("by_k") or {}).keys()),
            threshold=["3", "5", "10"],
        ),
        _metric_promotion_check(
            "overall_mrr",
            "Overall MRR meets PRD V5 gate",
            _metric(retrieval, "mrr"),
            PROMOTION_THRESHOLDS["overall_mrr"],
        ),
        _metric_promotion_check(
            "formula_qa_hit_at_10",
            "Formula QA Hit@10 meets PRD V5 gate",
            _qa_type_hit_at_10(retrieval, "formula_qa"),
            PROMOTION_THRESHOLDS["formula_qa_hit_at_10"],
        ),
        _metric_promotion_check(
            "citation_qa_hit_at_10",
            "Citation QA Hit@10 meets PRD V5 gate",
            _qa_type_hit_at_10(retrieval, "citation_qa"),
            PROMOTION_THRESHOLDS["citation_qa_hit_at_10"],
        ),
        _metric_promotion_check(
            "figure_qa_hit_at_10",
            "Figure QA Hit@10 meets PRD V5 gate",
            _qa_type_hit_at_10(retrieval, "figure_qa"),
            PROMOTION_THRESHOLDS["figure_qa_hit_at_10"],
        ),
        _metric_promotion_check(
            "table_qa_hit_at_10",
            "Table QA Hit@10 meets PRD V5 gate",
            _qa_type_hit_at_10(retrieval, "table_qa"),
            PROMOTION_THRESHOLDS["table_qa_hit_at_10"],
        ),
        _promotion_check(
            "by_qa_type_metrics",
            "By-QA-type retrieval metrics are present",
            bool(retrieval.get("by_qa_type")),
            actual=sorted((retrieval.get("by_qa_type") or {}).keys()),
        ),
        _answer_success_check(answer),
        _answer_judge_quality_check(
            config=config,
            generation=candidate_report.get("generation") if isinstance(candidate_report, dict) else None,
        ),
        _strict_equivalent_gap_check(retrieval),
        _answer_diagnostics_check(answer),
        _true_missing_gold_rate_check(answer),
        _claim_support_check(answer),
        _promotion_check(
            "failure_reasons",
            "Answer failure reasons are reported",
            isinstance(answer, dict) and "failure_reason_counts" in answer,
            actual=(answer or {}).get("failure_reason_counts") if isinstance(answer, dict) else None,
            threshold="answer.failure_reason_counts present",
        ),
    ]
    ready = all(check.status == "pass" for check in checks)
    return PolicyPromotionChecklist(
        policy_name=policy_name,
        ready_for_promotion=ready,
        reported_split="test",
        tuning_split="dev",
        thresholds=dict(PROMOTION_THRESHOLDS),
        checks=tuple(checks),
    )


def _promotion_check(
    check_id: str,
    label: str,
    passed: bool,
    *,
    actual: Any = None,
    threshold: Any = None,
    details: str = "",
    status: str | None = None,
) -> PolicyPromotionCheck:
    return PolicyPromotionCheck(
        check_id=check_id,
        label=label,
        status=status or ("pass" if passed else "fail"),
        actual=actual,
        threshold=threshold,
        details=details,
    )


def _metric_promotion_check(
    check_id: str,
    label: str,
    actual: float,
    threshold: float,
) -> PolicyPromotionCheck:
    return _promotion_check(
        check_id,
        label,
        actual >= threshold,
        actual=round(actual, 6),
        threshold=threshold,
    )


def _answer_success_check(answer: dict[str, Any] | None) -> PolicyPromotionCheck:
    if not isinstance(answer, dict):
        return _promotion_check(
            "answer_success",
            "Answer success meets PRD V5 gate",
            False,
            actual=None,
            threshold=PROMOTION_THRESHOLDS["answer_success"],
            details="answer evaluation was not run",
        )
    return _metric_promotion_check(
        "answer_success",
        "Answer success meets PRD V5 gate",
        _metric(answer, "success_rate"),
        PROMOTION_THRESHOLDS["answer_success"],
    )


def _answer_judge_quality_check(
    *,
    config: BenchmarkSuiteConfig,
    generation: dict[str, Any] | None,
) -> PolicyPromotionCheck:
    if not _answer_judge_enabled(config):
        return _promotion_check(
            "answer_judge_quality",
            "Structured answer judge quality is summarized when enabled",
            True,
            actual={"enabled": False},
            threshold="optional unless --answer-judge is enabled",
            details="answer judge not enabled",
        )
    if not isinstance(generation, dict):
        return _promotion_check(
            "answer_judge_quality",
            "Structured answer judge quality meets claim/citation gates",
            False,
            actual=None,
            threshold="generation metrics present",
            details="answer judge was enabled but no generation report was produced",
        )
    claim_support = _metric(generation, "claim_support_rate")
    citation_support = _metric(generation, "citation_claim_support_rate")
    unsupported = _metric(generation, "unsupported_claim_rate")
    judge_error = _metric(generation, "judge_error_rate")
    passed = (
        claim_support >= PROMOTION_THRESHOLDS["claim_support_rate"]
        and citation_support >= PROMOTION_THRESHOLDS["citation_claim_support_rate"]
        and unsupported <= PROMOTION_THRESHOLDS["unsupported_claim_rate"]
        and judge_error == 0.0
    )
    return _promotion_check(
        "answer_judge_quality",
        "Structured answer judge quality meets claim/citation gates",
        passed,
        actual={
            "claim_support_rate": round(claim_support, 6),
            "citation_claim_support_rate": round(citation_support, 6),
            "unsupported_claim_rate": round(unsupported, 6),
            "judge_error_rate": round(judge_error, 6),
        },
        threshold={
            "claim_support_rate": PROMOTION_THRESHOLDS["claim_support_rate"],
            "citation_claim_support_rate": PROMOTION_THRESHOLDS["citation_claim_support_rate"],
            "unsupported_claim_rate_max": PROMOTION_THRESHOLDS["unsupported_claim_rate"],
            "judge_error_rate": 0.0,
        },
    )


def _strict_equivalent_gap_check(retrieval: dict[str, Any]) -> PolicyPromotionCheck:
    by_10 = (retrieval.get("by_k") or {}).get("10") or {}
    strict_hit = _metric(by_10, "strict_hit_rate")
    equivalent_hit = _metric(by_10, "equivalent_hit_rate")
    gap = max(0.0, equivalent_hit - strict_hit)
    return _promotion_check(
        "strict_equivalent_hit_at_10_gap",
        "Strict/equivalent Hit@10 gap is reported and bounded",
        "strict_hit_rate" in by_10
        and "equivalent_hit_rate" in by_10
        and gap <= PROMOTION_THRESHOLDS["strict_equivalent_hit_at_10_gap"],
        actual={
            "strict_hit_at_10": round(strict_hit, 6),
            "equivalent_hit_at_10": round(equivalent_hit, 6),
            "gap": round(gap, 6),
        },
        threshold={"max_gap": PROMOTION_THRESHOLDS["strict_equivalent_hit_at_10_gap"]},
        details="prevents promotion when equivalent expansion hides a strict-gold regression",
    )


def _gold_judge_quality_check(
    *,
    config: BenchmarkSuiteConfig,
    question_profile: str,
    judge_report: GoldEvidenceJudgeReport | None,
) -> PolicyPromotionCheck:
    blind = _is_blind_question_profile(question_profile)
    if judge_report is None:
        status = "warning" if blind else "pass"
        return _promotion_check(
            "gold_judge_quality",
            "Gold judge quality is available for blind semantic gold evidence",
            not blind,
            actual={"enabled": False},
            threshold={
                "pass_rate": PROMOTION_THRESHOLDS["gold_judge_pass_rate"],
                "failed": 0,
                "error_rate": PROMOTION_THRESHOLDS["gold_judge_error_rate"],
            },
            details="blind benchmark is missing gold judge audit" if blind else "gold judge not required",
            status=status,
        )

    expected = config.gold_judge_sample_size
    sample_ok = True if expected is None else judge_report.sample_size >= max(0, expected)
    pass_rate = judge_report.pass_rate
    error_rate = judge_report.error_rate
    passed = (
        sample_ok
        and pass_rate >= PROMOTION_THRESHOLDS["gold_judge_pass_rate"]
        and judge_report.failed == 0
        and error_rate <= PROMOTION_THRESHOLDS["gold_judge_error_rate"]
    )
    return _promotion_check(
        "gold_judge_quality",
        "Gold judge quality meets blind semantic audit gate",
        passed,
        actual={
            "sample_size": judge_report.sample_size,
            "expected_sample_size": expected,
            "pass_rate": round(pass_rate, 6),
            "failed": judge_report.failed,
            "warning": judge_report.warning,
            "error": judge_report.error,
            "error_rate": round(error_rate, 6),
            "by_qa_type": judge_report.by_qa_type,
        },
        threshold={
            "pass_rate": PROMOTION_THRESHOLDS["gold_judge_pass_rate"],
            "failed": 0,
            "error_rate": PROMOTION_THRESHOLDS["gold_judge_error_rate"],
        },
    )


def _human_spot_check_quality_check(spot_check: SpotCheckReport | None) -> PolicyPromotionCheck:
    if spot_check is None or not spot_check.annotation_path:
        return _promotion_check(
            "human_spot_check_quality",
            "Human spot-check quality is summarized when annotations are provided",
            True,
            actual={"annotation_path": "", "annotated_count": 0},
            threshold="optional unless annotations are provided",
            details="no human annotations provided",
        )
    payload = spot_check.to_dict()
    pass_rate = float(payload.get("pass_rate") or 0.0)
    fail_count = int(payload.get("fail_count") or 0)
    schema_error_count = int(payload.get("schema_error_count") or 0)
    calibration = payload.get("judge_human_calibration") or {}
    compared = int(calibration.get("compared_count") or 0)
    agreement = float(calibration.get("judge_human_agreement") or 0.0)
    calibration_ok = compared == 0 or agreement >= PROMOTION_THRESHOLDS["judge_human_agreement"]
    passed = (
        spot_check.annotated_count > 0
        and pass_rate >= PROMOTION_THRESHOLDS["human_spot_check_pass_rate"]
        and fail_count == 0
        and schema_error_count == 0
        and calibration_ok
    )
    return _promotion_check(
        "human_spot_check_quality",
        "Human spot-check annotations meet quality gate",
        passed,
        actual={
            "annotated_count": spot_check.annotated_count,
            "pass_rate": round(pass_rate, 6),
            "fail_count": fail_count,
            "schema_error_count": schema_error_count,
            "judge_human_agreement": round(agreement, 6),
            "judge_human_compared_count": compared,
        },
        threshold={
            "pass_rate": PROMOTION_THRESHOLDS["human_spot_check_pass_rate"],
            "fail_count": 0,
            "schema_error_count": 0,
            "judge_human_agreement": PROMOTION_THRESHOLDS["judge_human_agreement"],
        },
    )


def _answer_diagnostics_check(answer: dict[str, Any] | None) -> PolicyPromotionCheck:
    if not isinstance(answer, dict):
        return _promotion_check(
            "answer_diagnostics",
            "Answer diagnostics include equivalent and failure split metrics",
            False,
            actual=None,
            threshold="answer diagnostic fields present",
            details="answer evaluation was not run",
        )
    required = {
        "equivalent_supported_rate",
        "true_missing_gold_rate",
        "claim_support_coverage",
        "diagnostic_tag_counts",
    }
    missing = sorted(key for key in required if key not in answer)
    return _promotion_check(
        "answer_diagnostics",
        "Answer diagnostics include equivalent and failure split metrics",
        not missing,
        actual={key: answer.get(key) for key in sorted(required)},
        threshold="all required diagnostics present",
        details=f"missing={missing}" if missing else "",
    )


def _true_missing_gold_rate_check(answer: dict[str, Any] | None) -> PolicyPromotionCheck:
    if not isinstance(answer, dict):
        return _promotion_check(
            "true_missing_gold_rate",
            "True missing gold rate is bounded",
            False,
            actual=None,
            threshold=PROMOTION_THRESHOLDS["true_missing_gold_rate"],
            details="answer evaluation was not run",
        )
    actual = _metric(answer, "true_missing_gold_rate")
    return _promotion_check(
        "true_missing_gold_rate",
        "True missing gold rate is bounded",
        actual <= PROMOTION_THRESHOLDS["true_missing_gold_rate"],
        actual=round(actual, 6),
        threshold={"max": PROMOTION_THRESHOLDS["true_missing_gold_rate"]},
    )


def _claim_support_check(answer: dict[str, Any] | None) -> PolicyPromotionCheck:
    if not isinstance(answer, dict):
        return _promotion_check(
            "claim_support_coverage",
            "Claim support coverage is visible for citation QA",
            False,
            actual=None,
            threshold="metric present",
            details="answer evaluation was not run",
        )
    by_type = answer.get("by_qa_type") or {}
    citation = by_type.get("citation_qa") or {}
    actual = citation.get("claim_support_coverage", answer.get("claim_support_coverage"))
    return _promotion_check(
        "claim_support_coverage",
        "Claim support coverage is visible for citation QA",
        actual is not None,
        actual=actual,
        threshold="metric present",
    )


def _field_distribution_present(retrieval: dict[str, Any]) -> bool:
    distribution = retrieval.get("field_embedding_distribution") or {}
    return bool(distribution.get("matched_evidence_count") or distribution.get("search_hits_by_field"))


def _rerank_distribution_present(retrieval: dict[str, Any]) -> bool:
    distribution = retrieval.get("rerank_distribution") or {}
    return int(distribution.get("reranked_evidence_count") or 0) > 0


def _qa_type_hit_at_10(retrieval: dict[str, Any], qa_type: str) -> float:
    by_type = retrieval.get("by_qa_type") or {}
    return _hit_at_10(by_type.get(qa_type) or {})


def _suite_warnings(
    *,
    paper_count: int,
    target_counts: dict[str, int],
    config: BenchmarkSuiteConfig,
    splits: dict[str, BenchmarkSplit],
    audit: GoldEvidenceAuditReport,
    judge_report: GoldEvidenceJudgeReport | None,
    candidate_report: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if paper_count < config.min_papers:
        warnings.append(f"paper_count_below_target:{paper_count}<{config.min_papers}")
    for qa_type in DEFAULT_TARGET_QA_TYPES:
        count = target_counts.get(qa_type, 0)
        if count < config.target_min_per_type:
            warnings.append(f"qa_type_count_below_target:{qa_type}:{count}<{config.target_min_per_type}")
    for name in ("train", "dev", "test"):
        if not splits.get(name) or splits[name].pair_count == 0:
            warnings.append(f"empty_split:{name}")
    warnings.extend(_split_distribution_warnings(splits))
    if audit.failed:
        warnings.append(f"gold_audit_failed:{audit.failed}")
    if audit.warning:
        warnings.append(f"gold_audit_warning:{audit.warning}")
    if judge_report is not None:
        if judge_report.failed:
            warnings.append(f"gold_judge_failed:{judge_report.failed}")
        if judge_report.warning:
            warnings.append(f"gold_judge_warning:{judge_report.warning}")
        if judge_report.error:
            warnings.append(f"gold_judge_error:{judge_report.error}")
    elif _is_blind_question_profile(config.question_profile):
        warnings.append(f"{_normalize_question_profile(config.question_profile)}_without_gold_judge")
    if candidate_report.get("passed") is False:
        warnings.append("candidate_quality_gate_failed")
        for issue in candidate_report.get("issues") or []:
            warnings.append(f"candidate_quality_issue:{issue}")
    return warnings


def _gold_quality_summary(
    question_profile: str,
    judge_report: GoldEvidenceJudgeReport | None,
    spot_check: SpotCheckReport | None,
) -> dict[str, Any]:
    judge_enabled = judge_report is not None
    judge_pass_rate = judge_report.pass_rate if judge_report is not None else 0.0
    judge_error_rate = judge_report.error_rate if judge_report is not None else 0.0
    human_payload = spot_check.to_dict() if spot_check is not None else {}
    human_annotated = int(human_payload.get("annotated_count") or 0)
    human_pass_rate = float(human_payload.get("pass_rate") or 0.0)
    human_schema_errors = int(human_payload.get("schema_error_count") or 0)
    human_fail_count = int(human_payload.get("fail_count") or 0)
    judge_passed = (
        judge_report is not None
        and judge_report.failed == 0
        and judge_report.error_rate <= PROMOTION_THRESHOLDS["gold_judge_error_rate"]
        and judge_report.pass_rate >= PROMOTION_THRESHOLDS["gold_judge_pass_rate"]
    )
    human_passed = (
        human_annotated > 0
        and human_pass_rate >= PROMOTION_THRESHOLDS["human_spot_check_pass_rate"]
        and human_schema_errors == 0
        and human_fail_count == 0
    )
    return {
        "blind_question_profile": _is_blind_question_profile(question_profile),
        "judge_enabled": judge_enabled,
        "judge_sample_size": judge_report.sample_size if judge_report is not None else 0,
        "judge_pass_rate": judge_pass_rate,
        "judge_failed": judge_report.failed if judge_report is not None else 0,
        "judge_warning": judge_report.warning if judge_report is not None else 0,
        "judge_error": judge_report.error if judge_report is not None else 0,
        "judge_error_rate": judge_error_rate,
        "human_annotation_path": human_payload.get("annotation_path", ""),
        "human_annotated_count": human_annotated,
        "human_spot_check_pass_rate": human_pass_rate,
        "human_spot_check_fail_count": human_fail_count,
        "human_spot_check_schema_error_count": human_schema_errors,
        "judge_audited": judge_passed,
        "fully_audited": judge_passed and human_passed,
    }


def _write_gold_fix_artifacts(output_dir: Path, judge_report: GoldEvidenceJudgeReport | None) -> None:
    failure_items: list[dict[str, Any]] = []
    warning_items: list[dict[str, Any]] = []
    if judge_report is not None:
        for item in judge_report.items:
            if item.status == "pass":
                continue
            record = _gold_fix_record(item)
            if item.status == "warning":
                warning_items.append(record)
            else:
                failure_items.append(record)
    _write_jsonl(output_dir / "gold_judge_failures.jsonl", failure_items)
    _write_jsonl(output_dir / "gold_judge_warnings.jsonl", warning_items)
    all_items = [*failure_items, *warning_items]
    action_counts = Counter(str(item.get("suggested_action") or "") for item in all_items)
    manifest = {
        "total": len(all_items),
        "failure_count": len(failure_items),
        "warning_count": len(warning_items),
        "action_counts": dict(sorted(action_counts.items())),
        "items": all_items,
    }
    (output_dir / "gold_fix_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _gold_fix_record(item: GoldEvidenceJudgeItem) -> dict[str, Any]:
    return {
        "paper_id": item.paper_id,
        "qa_type": item.qa_type,
        "question": item.question,
        "status": item.status,
        "supported": item.supported,
        "confidence": item.confidence,
        "question_clear": item.question_clear,
        "gold_evidence_complete": item.gold_evidence_complete,
        "equivalent_gold_needed": item.equivalent_gold_needed,
        "bad_gold_reason": item.bad_gold_reason,
        "gold_chunk_ids": list(item.gold_chunk_ids),
        "equivalent_gold_chunk_ids": list(item.equivalent_gold_chunk_ids),
        "judge_reason": item.reason,
        "suggested_action": _suggested_gold_fix_action(item),
    }


def _suggested_gold_fix_action(item: GoldEvidenceJudgeItem) -> str:
    if item.suggested_action:
        return item.suggested_action
    if item.bad_gold_reason == "question_ambiguous":
        return "rewrite_question"
    if item.bad_gold_reason in {"missing_required_context", "formula_context_missing", "table_context_missing"}:
        return "add_context_gold"
    if item.bad_gold_reason in {"gold_chunk_not_supporting", "wrong_equivalent_gold"}:
        return "add_equivalent_gold"
    if item.status == "error":
        return "manual_review_required"
    if item.status == "warning":
        return "add_equivalent_gold"
    if item.qa_type in {"citation_qa", "formula_qa", "formula_explanation_qa"}:
        return "add_context_gold"
    return "drop_question"


def _split_distribution_warnings(splits: dict[str, BenchmarkSplit]) -> list[str]:
    test = splits.get("test")
    if test is None or test.pair_count <= 0:
        return []
    warnings: list[str] = []
    answerable_count = sum(
        count for qa_type, count in test.qa_type_counts.items()
        if qa_type != "negative_qa"
    )
    if answerable_count <= 0:
        return warnings
    for qa_type, count in sorted(test.qa_type_counts.items()):
        if qa_type == "negative_qa":
            continue
        share = count / answerable_count
        if share >= 0.45:
            warnings.append(f"test_split_qa_type_dominates:{qa_type}:{share:.2f}")
    return warnings


def _write_suite_report(result: BenchmarkSuiteResult) -> None:
    payload = result.to_dict()
    (result.output_dir / "benchmark_suite_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    checklist = payload["policy_promotion_checklist"]
    (result.output_dir / "policy_promotion_checklist.json").write_text(
        json.dumps(checklist, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (result.output_dir / "policy_promotion_checklist.md").write_text(
        _policy_promotion_markdown(checklist, title_level=1),
        encoding="utf-8",
    )
    _write_gold_fix_artifacts(result.output_dir, result.gold_judge)
    (result.output_dir / "benchmark_suite_report.md").write_text(_suite_markdown(payload), encoding="utf-8")


def _policy_promotion_markdown(checklist: dict[str, Any], *, title_level: int) -> str:
    return "\n".join(_policy_promotion_markdown_lines(checklist, title_level=title_level)).rstrip() + "\n"


def _policy_promotion_markdown_lines(checklist: dict[str, Any], *, title_level: int) -> list[str]:
    prefix = "#" * max(1, min(6, title_level))
    lines = [
        f"{prefix} Policy Promotion Checklist",
        "",
        f"- policy: `{checklist.get('policy_name', '')}`",
        f"- ready_for_promotion: `{bool(checklist.get('ready_for_promotion'))}`",
        f"- tuning split: `{checklist.get('tuning_split', 'dev')}`",
        f"- reported split: `{checklist.get('reported_split', 'test')}`",
        "",
        "| Check | Status | Actual | Threshold | Details |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in checklist.get("checks") or []:
        lines.append(
            "| "
            + " | ".join([
                str(check.get("label") or check.get("check_id") or ""),
                f"`{check.get('status', '')}`",
                _format_check_value(check.get("actual")),
                _format_check_value(check.get("threshold")),
                str(check.get("details") or ""),
            ])
            + " |"
        )
    return lines


def _format_check_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"`{value:.3f}`"
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    if len(text) > 120:
        text = text[:117] + "..."
    return f"`{text}`"


def _optional_metric(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.3f}"


def _suite_markdown(payload: dict[str, Any]) -> str:
    candidate = payload["candidate_test_report"]["retrieval"]
    baseline_report = payload.get("baseline_test_report")
    baseline = (baseline_report or {}).get("retrieval") or None
    ab_report = payload.get("ab_report") or None
    relative_mrr = ((ab_report or {}).get("relative_improvement") or {}).get("mrr")
    macro = (ab_report or {}).get("macro_by_qa_type") or {}
    lines = [
        "# Paper RAG Benchmark Suite",
        "",
        f"- papers: `{payload['papers_total']}`",
        f"- chunks: `{payload['chunks_total']}`",
        f"- pairs: `{payload['pairs_total']}`",
        f"- warnings: `{len(payload['warnings'])}`",
        f"- reported split: `{payload['evaluation_protocol']['reported_split']}`",
        f"- question profile: `{payload['evaluation_protocol'].get('question_profile', 'template')}`",
        f"- blind test: `{payload['evaluation_protocol'].get('blind_test', False)}`",
        "",
        "## Test Metrics",
        "",
        f"- candidate Hit@3/5/10: `{_hit_at_k(candidate, 3):.3f}` / "
        f"`{_hit_at_k(candidate, 5):.3f}` / `{_hit_at_k(candidate, 10):.3f}`",
        f"- candidate equivalent Hit@3/5/10: `{_by_k_metric(candidate, 3, 'equivalent_hit_rate'):.3f}` / "
        f"`{_by_k_metric(candidate, 5, 'equivalent_hit_rate'):.3f}` / "
        f"`{_by_k_metric(candidate, 10, 'equivalent_hit_rate'):.3f}`",
        f"- candidate evidence coverage@3/5/10: `{_by_k_metric(candidate, 3, 'evidence_coverage'):.3f}` / "
        f"`{_by_k_metric(candidate, 5, 'evidence_coverage'):.3f}` / "
        f"`{_by_k_metric(candidate, 10, 'evidence_coverage'):.3f}`",
        f"- candidate source locator coverage@3/5/10: `{_by_k_metric(candidate, 3, 'source_locator_coverage'):.3f}` / "
        f"`{_by_k_metric(candidate, 5, 'source_locator_coverage'):.3f}` / "
        f"`{_by_k_metric(candidate, 10, 'source_locator_coverage'):.3f}`",
        f"- candidate MRR: `{candidate['mrr']:.3f}`",
    ]
    if baseline is not None and ab_report is not None:
        lines.extend([
            f"- fixed-window Hit@3/5/10: `{_hit_at_k(baseline, 3):.3f}` / "
            f"`{_hit_at_k(baseline, 5):.3f}` / `{_hit_at_k(baseline, 10):.3f}`",
            f"- fixed-window MRR: `{baseline['mrr']:.3f}`",
            f"- MRR delta: `{ab_report['deltas']['mrr']:.3f}`",
            f"- MRR relative improvement: `{_format_optional_ratio(relative_mrr)}`",
        ])
    if macro:
        lines.extend([
            f"- candidate macro Hit@3/5/10: `{macro['candidate']['macro_hit_at_3']:.3f}` / "
            f"`{macro['candidate']['macro_hit_at_5']:.3f}` / "
            f"`{macro['candidate']['macro_hit_at_10']:.3f}`",
            f"- candidate macro MRR: `{macro['candidate']['macro_mrr']:.3f}`",
            f"- fixed-window macro Hit@3/5/10: `{macro['baseline']['macro_hit_at_3']:.3f}` / "
            f"`{macro['baseline']['macro_hit_at_5']:.3f}` / "
            f"`{macro['baseline']['macro_hit_at_10']:.3f}`",
            f"- fixed-window macro MRR: `{macro['baseline']['macro_mrr']:.3f}`",
        ])
    promotion = payload.get("policy_promotion_checklist") or {}
    if promotion:
        lines.extend(["", *_policy_promotion_markdown_lines(promotion, title_level=2)])
    gold_quality = payload.get("gold_quality") or {}
    if gold_quality:
        lines.extend([
            "",
            "## Gold Quality",
            "",
            f"- judge_enabled: `{gold_quality.get('judge_enabled', False)}`",
            f"- judge_sample_size: `{gold_quality.get('judge_sample_size', 0)}`",
            f"- judge_pass_rate: `{float(gold_quality.get('judge_pass_rate') or 0.0):.3f}`",
            f"- judge_failed/warning/error: `{gold_quality.get('judge_failed', 0)}/{gold_quality.get('judge_warning', 0)}/{gold_quality.get('judge_error', 0)}`",
            f"- human_annotated_count: `{gold_quality.get('human_annotated_count', 0)}`",
            f"- human_spot_check_pass_rate: `{float(gold_quality.get('human_spot_check_pass_rate') or 0.0):.3f}`",
            f"- judge_audited: `{gold_quality.get('judge_audited', False)}`",
            f"- fully_audited: `{gold_quality.get('fully_audited', False)}`",
        ])
    score_breakdown = candidate.get("score_breakdown_summary") or {}
    score_components = score_breakdown.get("components") or {}
    if score_components:
        lines.extend([
            "",
            "## Score Breakdown",
            "",
            f"- top_k: `{score_breakdown.get('top_k')}`",
            f"- evidence_count: `{score_breakdown.get('evidence_count', 0)}`",
            "",
            "| Component | count | avg | min | max |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for component, stats in score_components.items():
            lines.append(
                f"| `{component}` | `{stats.get('count', 0)}` | "
                f"`{stats.get('avg', 0.0):.3f}` | `{stats.get('min', 0.0):.3f}` | "
                f"`{stats.get('max', 0.0):.3f}` |"
            )
    field_embedding_distribution = candidate.get("field_embedding_distribution") or {}
    field_by_name = field_embedding_distribution.get("by_field") or {}
    field_search_hits = field_embedding_distribution.get("search_hits_by_field") or {}
    if field_by_name or field_search_hits:
        lines.extend([
            "",
            "## Field Embedding Distribution",
            "",
            f"- top_k: `{field_embedding_distribution.get('top_k')}`",
            f"- matched_evidence_count: `{field_embedding_distribution.get('matched_evidence_count', 0)}`",
        ])
        if field_by_name:
            lines.extend([
                "",
                "| Field | count | avg | max |",
                "| --- | ---: | ---: | ---: |",
            ])
            for field_name, stats in sorted(field_by_name.items()):
                lines.append(
                    f"| `{field_name}` | `{stats.get('count', 0)}` | "
                    f"`{stats.get('avg_score', 0.0):.3f}` | `{stats.get('max_score', 0.0):.3f}` |"
                )
        if field_search_hits:
            lines.extend(["", "| Search hit field | count |", "| --- | ---: |"])
            for field_name, count in sorted(field_search_hits.items()):
                lines.append(f"| `{field_name}` | `{count}` |")
    rerank_distribution = candidate.get("rerank_distribution") or {}
    if rerank_distribution.get("reranked_evidence_count"):
        lines.extend([
            "",
            "## Rerank Distribution",
            "",
            f"- reranker enabled samples: `{rerank_distribution.get('reranker_enabled_sample_count', 0)}`",
            f"- reranked evidence count: `{rerank_distribution.get('reranked_evidence_count', 0)}`",
            f"- avg/min/max: `{rerank_distribution.get('avg_score', 0.0):.3f}` / "
            f"`{rerank_distribution.get('min_score', 0.0):.3f}` / "
            f"`{rerank_distribution.get('max_score', 0.0):.3f}`",
        ])
        enabled_intents = rerank_distribution.get("enabled_intents") or {}
        if enabled_intents:
            lines.extend(["", "| Intent | enabled samples |", "| --- | ---: |"])
            for intent, count in sorted(enabled_intents.items()):
                lines.append(f"| `{intent}` | `{count}` |")
    route_distribution = candidate.get("route_distribution") or {}
    intent_distribution = candidate.get("intent_distribution") or {}
    intent_confusion = candidate.get("intent_confusion") or {}
    if route_distribution or intent_distribution:
        lines.extend(["", "## Route Distribution", ""])
        if intent_distribution:
            lines.extend([
                "| Intent | count |",
                "| --- | ---: |",
            ])
            for intent, count in sorted(intent_distribution.items()):
                lines.append(f"| `{intent}` | `{count}` |")
        if route_distribution:
            lines.extend([
                "",
                "| Recall route | count |",
                "| --- | ---: |",
            ])
            for route_name, count in sorted(route_distribution.items()):
                lines.append(f"| `{route_name}` | `{count}` |")
    if intent_confusion:
        lines.extend([
            "",
            "## Intent Confusion",
            "",
            "| QA type | routed intents |",
            "| --- | --- |",
        ])
        for qa_type, counts in sorted(intent_confusion.items()):
            rendered = ", ".join(
                f"`{intent}`={count}"
                for intent, count in sorted(counts.items())
            )
            lines.append(f"| `{qa_type}` | {rendered} |")
    lines.extend(["", "## Test Metrics By QA Type", ""])
    if baseline is not None:
        lines.extend([
            "| QA type | n | candidate Hit@3 | candidate Hit@5 | candidate Hit@10 | candidate MRR | fixed-window Hit@3 | fixed-window Hit@5 | fixed-window Hit@10 | fixed-window MRR |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            *_qa_type_metric_rows(candidate, baseline),
        ])
    else:
        lines.extend([
            "| QA type | n | candidate Hit@3 | candidate Hit@5 | candidate Hit@10 | candidate MRR |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
            *_qa_type_metric_rows(candidate, None),
        ])
    lines.extend([
        "",
        "## QA Type Counts",
    ])
    for qa_type, count in payload["target_qa_counts"].items():
        lines.append(f"- {qa_type}: `{count}`")
    question_audit = payload.get("question_audit") or {}
    if question_audit:
        lines.extend([
            "",
            "## Question Ambiguity Audit",
            "",
            f"- duplicate question rate: `{_metric(question_audit, 'duplicate_question_rate'):.3f}`",
            f"- ambiguous question rate: `{_metric(question_audit, 'ambiguous_question_rate'):.3f}`",
            f"- missing semantic anchor rate: `{_metric(question_audit, 'missing_semantic_anchor_rate'):.3f}`",
            f"- label leakage rate: `{_metric(question_audit, 'label_leakage_rate'):.3f}`",
            f"- caption copy rate: `{_metric(question_audit, 'caption_copy_rate'):.3f}`",
        ])
        by_qa_type = question_audit.get("by_qa_type") or {}
        if by_qa_type:
            lines.extend([
                "",
                "| QA type | n | duplicate | ambiguous | missing anchor | label leakage | caption copy |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ])
            for qa_type, counts in sorted(by_qa_type.items()):
                lines.append(
                    f"| {qa_type} | {counts.get('total', 0)} | "
                    f"{counts.get('duplicate_question', 0)} | {counts.get('ambiguous_question', 0)} | "
                    f"{counts.get('missing_semantic_anchor', 0)} | {counts.get('label_leakage', 0)} | "
                    f"{counts.get('caption_copy', 0)} |"
                )
    answer = payload["candidate_test_report"].get("answer")
    if answer:
        lines.extend([
            "",
            "## Answer Metrics",
            "",
            f"- answer success rate: `{_optional_metric(answer.get('success_rate'))}`",
            f"- answer fact coverage: `{_optional_metric(answer.get('answer_fact_coverage'))}`",
            f"- retrieval context coverage: `{_optional_metric(answer.get('retrieval_context_coverage'))}`",
            f"- citation grounding: `{_optional_metric(answer.get('citation_grounding_score'))}`",
            f"- citation gold coverage: `{_optional_metric(answer.get('citation_gold_coverage'))}`",
            f"- equivalent supported rate: `{_optional_metric(answer.get('equivalent_supported_rate'))}`",
            f"- claim support coverage: `{_optional_metric(answer.get('claim_support_coverage'))}`",
            f"- true missing gold rate: `{_optional_metric(answer.get('true_missing_gold_rate'))}`",
            f"- source locator grounding: `{_optional_metric(answer.get('source_locator_grounding_score'))}`",
            f"- abstention accuracy: `{_optional_metric(answer.get('abstention_accuracy'))}`",
        ])
        failure_counts = answer.get("failure_reason_counts") or {}
        if failure_counts:
            lines.append("- failure reasons:")
            for reason, count in sorted(failure_counts.items()):
                lines.append(f"  - `{reason}`: `{count}`")
        diagnostic_counts = answer.get("diagnostic_tag_counts") or {}
        if diagnostic_counts:
            lines.append("- diagnostic tags:")
            for tag, count in sorted(diagnostic_counts.items()):
                lines.append(f"  - `{tag}`: `{count}`")
    generation = payload["candidate_test_report"].get("generation")
    if generation:
        lines.extend([
            "",
            "## Generation Judge",
            "",
            f"- faithfulness: `{generation['faithfulness']:.3f}`",
            f"- answer relevancy: `{generation['answer_relevancy']:.3f}`",
            f"- context precision: `{generation['context_precision']:.3f}`",
            f"- claim support rate: `{generation.get('claim_support_rate', 0.0):.3f}`",
            f"- unsupported claim rate: `{generation.get('unsupported_claim_rate', 0.0):.3f}`",
            f"- citation claim support rate: `{generation.get('citation_claim_support_rate', 0.0):.3f}`",
            f"- wrong citation rate: `{generation.get('wrong_citation_rate', 0.0):.3f}`",
            f"- missing citation rate: `{generation.get('missing_citation_rate', 0.0):.3f}`",
            f"- judge error rate: `{generation.get('judge_error_rate', 0.0):.3f}`",
            f"- judged samples: `{generation['total']}`",
        ])
    spot_check = payload.get("spot_check")
    if spot_check:
        lines.extend([
            "",
            "## Spot Check",
            "",
            f"- sample_path: `{spot_check['sample_path']}`",
            f"- sample_size: `{spot_check['sample_size']}`",
            f"- annotated_count: `{spot_check['annotated_count']}`",
            f"- pass_rate: `{float(spot_check.get('pass_rate') or 0.0):.3f}`",
            f"- human_answer_ok_rate: `{float(spot_check.get('human_answer_ok_rate') or 0.0):.3f}`",
            f"- human_faithfulness_ok_rate: `{float(spot_check.get('human_faithfulness_ok_rate') or 0.0):.3f}`",
            f"- human_citation_ok_rate: `{float(spot_check.get('human_citation_ok_rate') or 0.0):.3f}`",
            f"- warning_count: `{spot_check.get('warning_count', 0)}`",
            f"- fail_count: `{spot_check.get('fail_count', 0)}`",
            f"- schema_error_count: `{spot_check.get('schema_error_count', 0)}`",
        ])
        calibration = spot_check.get("judge_human_calibration") or {}
        if calibration:
            lines.extend([
                f"- judge_human_agreement: `{float(calibration.get('judge_human_agreement') or 0.0):.3f}`",
                f"- judge_precision: `{float(calibration.get('judge_precision') or 0.0):.3f}`",
                f"- judge_recall: `{float(calibration.get('judge_recall') or 0.0):.3f}`",
                f"- judge_false_positive_rate: `{float(calibration.get('judge_false_positive_rate') or 0.0):.3f}`",
                f"- judge_false_negative_rate: `{float(calibration.get('judge_false_negative_rate') or 0.0):.3f}`",
                f"- conflict_count: `{spot_check.get('conflict_count', 0)}`",
            ])
        if spot_check.get("label_counts"):
            for label, count in sorted(spot_check["label_counts"].items()):
                lines.append(f"- {label}: `{count}`")
        if spot_check.get("by_qa_type"):
            lines.extend([
                "",
                "| QA type | pass | warning | fail | needs_fix | unknown |",
                "| --- | ---: | ---: | ---: | ---: | ---: |",
            ])
            for qa_type, counts in sorted(spot_check["by_qa_type"].items()):
                lines.append(
                    f"| `{qa_type}` | `{counts.get('pass', 0)}` | `{counts.get('warning', 0)}` | "
                    f"`{counts.get('fail', 0)}` | `{counts.get('needs_fix', 0)}` | `{counts.get('unknown', 0)}` |"
                )
    lines.extend([
        "",
        "## Splits",
    ])
    for name, split in payload["splits"].items():
        lines.append(f"- {name}: `{len(split['paper_ids'])}` papers, `{split['pair_count']}` pairs")
    audit_by_type = (payload.get("gold_audit") or {}).get("by_qa_type") or {}
    if audit_by_type:
        lines.extend([
            "",
            "## Gold Audit By QA Type",
            "",
            "| QA type | pass | warning | fail |",
            "| --- | ---: | ---: | ---: |",
        ])
        for qa_type, counts in sorted(audit_by_type.items()):
            lines.append(
                f"| {qa_type} | {counts.get('pass', 0)} | "
                f"{counts.get('warning', 0)} | {counts.get('fail', 0)} |"
            )
    if payload.get("gold_judge"):
        judge = payload["gold_judge"]
        lines.extend([
            "",
            "## Gold Judge",
            "",
            f"- mode: `{judge['mode']}`",
            f"- model: `{judge['model']}`",
            f"- sample_size: `{judge['sample_size']}`",
            f"- pass_rate: `{float(judge.get('pass_rate') or 0.0):.3f}`",
            f"- error_rate: `{float(judge.get('error_rate') or 0.0):.3f}`",
            f"- pass/warning/fail/error: `{judge['passed']}/{judge['warning']}/{judge['failed']}/{judge['error']}`",
        ])
        if judge.get("by_qa_type"):
            lines.extend([
                "",
                "| QA type | pass | warning | fail | error |",
                "| --- | ---: | ---: | ---: | ---: |",
            ])
            for qa_type, counts in sorted(judge["by_qa_type"].items()):
                lines.append(
                    f"| `{qa_type}` | `{counts.get('pass', 0)}` | `{counts.get('warning', 0)}` | "
                    f"`{counts.get('fail', 0)}` | `{counts.get('error', 0)}` |"
                )
    if payload["warnings"]:
        lines.extend(["", "## Warnings", *[f"- `{warning}`" for warning in payload["warnings"]]])
    return "\n".join(lines) + "\n"


def _qa_type_metric_rows(candidate: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    rows: list[str] = []
    candidate_by_type = candidate.get("by_qa_type") or {}
    baseline_by_type = (baseline or {}).get("by_qa_type") or {}
    for qa_type in sorted(set(candidate_by_type) | set(baseline_by_type)):
        current = candidate_by_type.get(qa_type) or {}
        base = baseline_by_type.get(qa_type) or {}
        answerable_total = int(current.get("answerable_total") or base.get("answerable_total") or 0)
        if answerable_total <= 0:
            continue
        values = [
            qa_type,
            str(answerable_total),
            f"{_hit_at_k(current, 3):.3f}",
            f"{_hit_at_k(current, 5):.3f}",
            f"{_hit_at_10(current):.3f}",
            f"{_metric(current, 'mrr'):.3f}",
        ]
        if baseline is not None:
            values.extend([
                f"{_hit_at_k(base, 3):.3f}",
                f"{_hit_at_k(base, 5):.3f}",
                f"{_hit_at_10(base):.3f}",
                f"{_metric(base, 'mrr'):.3f}",
            ])
        rows.append("| " + " | ".join(values) + " |")
    if baseline is not None:
        return rows or ["| none | 0 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |"]
    return rows or ["| none | 0 | 0.000 | 0.000 | 0.000 | 0.000 |"]


def _macro_by_qa_type(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate": _macro_summary(candidate),
        "baseline": _macro_summary(baseline),
    }


def _macro_summary(retrieval: dict[str, Any]) -> dict[str, Any]:
    values = [
        item for item in (retrieval.get("by_qa_type") or {}).values()
        if int(item.get("answerable_total") or 0) > 0
    ]
    return {
        "qa_types": len(values),
        "macro_hit_at_3": _average([_hit_at_k(item, 3) for item in values]),
        "macro_hit_at_5": _average([_hit_at_k(item, 5) for item in values]),
        "macro_hit_at_10": _average([_hit_at_10(item) for item in values]),
        "macro_mrr": _average([_metric(item, "mrr") for item in values]),
    }


def _hit_at_10(retrieval: dict[str, Any]) -> float:
    return _hit_at_k(retrieval, 10)


def _hit_at_k(retrieval: dict[str, Any], k: int) -> float:
    return _by_k_metric(retrieval, k, "hit_rate")


def _by_k_metric(retrieval: dict[str, Any], k: int, key: str) -> float:
    return _metric(((retrieval.get("by_k") or {}).get(str(k)) or {}), key)


def _top_k_metrics_present(retrieval: dict[str, Any]) -> bool:
    by_k = retrieval.get("by_k") or {}
    required = ("3", "5", "10")
    required_metrics = (
        "hit_rate",
        "equivalent_hit_rate",
        "evidence_coverage",
        "source_locator_coverage",
        "ndcg",
    )
    return all(
        k in by_k and all(metric in (by_k.get(k) or {}) for metric in required_metrics)
        for k in required
    )


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _coverage(retrieved: list[str], required: list[str]) -> float | None:
    required_set = set(_unique_texts(required))
    if not required_set:
        return None
    return len(required_set.intersection(_unique_texts(retrieved))) / len(required_set)


def _unique_texts(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _summary_from_report(report: dict[str, Any]) -> dict[str, Any]:
    retrieval = report.get("retrieval") or {}
    by_k = retrieval.get("by_k") or {}
    return {
        "answerable_total": retrieval.get("answerable_total", 0),
        "hit_at_3": (by_k.get("3") or {}).get("hit_rate", 0.0),
        "hit_at_5": (by_k.get("5") or {}).get("hit_rate", 0.0),
        "hit_at_10": (by_k.get("10") or {}).get("hit_rate", 0.0),
        "evidence_coverage_at_3": (by_k.get("3") or {}).get("evidence_coverage", 0.0),
        "evidence_coverage_at_5": (by_k.get("5") or {}).get("evidence_coverage", 0.0),
        "evidence_coverage_at_10": (by_k.get("10") or {}).get("evidence_coverage", 0.0),
        "source_locator_coverage_at_3": (by_k.get("3") or {}).get("source_locator_coverage", 0.0),
        "source_locator_coverage_at_5": (by_k.get("5") or {}).get("source_locator_coverage", 0.0),
        "source_locator_coverage_at_10": (by_k.get("10") or {}).get("source_locator_coverage", 0.0),
        "mrr": retrieval.get("mrr", 0.0),
    }


def _metric(values: dict[str, Any], key: str) -> float:
    try:
        return float(values.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _relative_improvement(candidate: float, baseline: float) -> float | None:
    if baseline <= 0.0:
        return None
    return (candidate - baseline) / baseline


def _format_optional_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _split_to_dict(split: BenchmarkSplit) -> dict[str, Any]:
    return {
        "name": split.name,
        "paper_ids": list(split.paper_ids),
        "pair_count": split.pair_count,
        "qa_type_counts": dict(split.qa_type_counts),
    }


def _stable_split_key(seed: str, paper_id: str) -> str:
    return hashlib.sha256(f"{seed}:{paper_id}".encode("utf-8")).hexdigest()


def _stable_sample(values: list[EvidenceQAPair], *, sample_size: int, seed: str) -> list[EvidenceQAPair]:
    ordered = sorted(values, key=lambda pair: _stable_split_key(seed, f"{pair.paper_id}:{pair.question}"))
    return ordered[:sample_size] if sample_size else []


def _stratified_pair_sample(values: list[EvidenceQAPair], *, sample_size: int, seed: str) -> list[EvidenceQAPair]:
    if sample_size <= 0:
        return []
    selected: list[EvidenceQAPair] = []
    seen: set[tuple[str, str, str]] = set()
    by_type: dict[str, list[EvidenceQAPair]] = {}
    for pair in values:
        by_type.setdefault(pair.qa_type, []).append(pair)
    for qa_type in [*DEFAULT_TARGET_QA_TYPES, *sorted(set(by_type) - set(DEFAULT_TARGET_QA_TYPES))]:
        candidates = by_type.get(qa_type) or []
        if not candidates or len(selected) >= sample_size:
            continue
        chosen = _ordered_pairs(candidates, seed=f"{seed}:{qa_type}")[0]
        selected.append(chosen)
        seen.add(_pair_sample_key(chosen))
    for pair in _ordered_pairs(values, seed=f"{seed}:fill"):
        if len(selected) >= sample_size:
            break
        key = _pair_sample_key(pair)
        if key in seen:
            continue
        selected.append(pair)
        seen.add(key)
    return selected


def _gold_judge_sample(
    items: list[GoldEvidenceAuditItem],
    *,
    sample_size: int,
    seed: str,
) -> list[GoldEvidenceAuditItem]:
    if sample_size <= 0:
        return []
    items = [item for item in items if _gold_judge_eligible(item)]
    selected: list[GoldEvidenceAuditItem] = []
    seen: set[tuple[str, str, str]] = set()
    by_type: dict[str, list[GoldEvidenceAuditItem]] = {}
    for item in items:
        by_type.setdefault(item.qa_type, []).append(item)
    for qa_type in [*DEFAULT_TARGET_QA_TYPES, *sorted(set(by_type) - set(DEFAULT_TARGET_QA_TYPES))]:
        candidates = by_type.get(qa_type) or []
        if not candidates or len(selected) >= sample_size:
            continue
        chosen = _ordered_gold_audit_items(candidates, seed=f"{seed}:{qa_type}")[0]
        selected.append(chosen)
        seen.add(_gold_audit_item_key(chosen))
    for item in _ordered_gold_audit_items(items, seed=f"{seed}:fill"):
        if len(selected) >= sample_size:
            break
        key = _gold_audit_item_key(item)
        if key in seen:
            continue
        selected.append(item)
        seen.add(key)
    return selected


def _ordered_pairs(values: Iterable[EvidenceQAPair], *, seed: str) -> list[EvidenceQAPair]:
    return sorted(
        values,
        key=lambda pair: (
            _pair_risk_rank(pair),
            _stable_split_key(seed, f"{pair.paper_id}:{pair.qa_type}:{pair.question}"),
        ),
    )


def _ordered_gold_audit_items(values: Iterable[GoldEvidenceAuditItem], *, seed: str) -> list[GoldEvidenceAuditItem]:
    return sorted(
        values,
        key=lambda item: (
            _gold_audit_risk_rank(item),
            _stable_split_key(seed, f"{item.paper_id}:{item.qa_type}:{item.question}"),
        ),
    )


def _pair_risk_rank(pair: EvidenceQAPair) -> int:
    if pair.qa_type in {"citation_qa", "formula_qa", "formula_explanation_qa"}:
        return 0
    if pair.qa_type in {"figure_qa", "table_qa", "experiment_result_qa"}:
        return 1
    return 2


def _gold_audit_risk_rank(item: GoldEvidenceAuditItem) -> int:
    if item.status in {"fail", "warning"}:
        return 0
    if item.qa_type in {"citation_qa", "formula_qa", "formula_explanation_qa"}:
        return 1
    if not item.answer_facts_present:
        return 2
    return 3


def _pair_sample_key(pair: EvidenceQAPair) -> tuple[str, str, str]:
    return (pair.paper_id, pair.qa_type, pair.question)


def _gold_audit_item_key(item: GoldEvidenceAuditItem) -> tuple[str, str, str]:
    return (item.paper_id, item.qa_type, item.question)


def _evidence_type(chunk: PaperChunk) -> str:
    if chunk.chunk_type == "formula" or chunk.has_formula:
        return "formula"
    if chunk.chunk_type == "figure" or chunk.has_figure:
        return "figure"
    if chunk.chunk_type == "table" or chunk.has_table:
        return "table"
    return chunk.chunk_type


def _evidence_preview(chunk: PaperChunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "chunk_type": chunk.chunk_type,
        "section_title": chunk.section_title,
        "source_locator": chunk.metadata.get("source_locator") or chunk.metadata.get("source_ref") or "",
        "image_ref": chunk.metadata.get("image_ref") or "",
        "content_preview": " ".join(chunk.content.split())[:_GOLD_EVIDENCE_PREVIEW_CHARS],
    }


def _judge_prompt(item: GoldEvidenceAuditItem, max_evidence_chars: int) -> str:
    evidence_lines = []
    for preview in item.evidence_previews:
        evidence_lines.append(json.dumps({
            "chunk_id": preview.get("chunk_id"),
            "chunk_type": preview.get("chunk_type"),
            "section_title": preview.get("section_title"),
            "source_locator": preview.get("source_locator"),
            "content_preview": str(preview.get("content_preview") or "")[:max_evidence_chars],
        }, ensure_ascii=False))
    return "\n".join([
        "You are auditing a paper QA benchmark gold evidence item.",
        "Return JSON only with keys: supported(boolean), confidence(number 0-1), reason(string), "
        "question_clear(boolean), gold_evidence_complete(boolean), equivalent_gold_needed(boolean), "
        "bad_gold_reason(string), suggested_action(string).",
        "Judge whether the evidence previews are enough to support the question and expected answer facts.",
        "Use bad_gold_reason values such as question_ambiguous, gold_chunk_not_supporting, "
        "missing_required_context, formula_context_missing, table_context_missing, figure_image_missing, "
        "source_locator_missing, or empty string.",
        "Use suggested_action values such as keep, add_equivalent_gold, add_context_gold, rewrite_question, "
        "drop_question, or manual_review_required.",
        "",
        f"Question: {item.question}",
        f"QA type: {item.qa_type}",
        f"Answer facts: {json.dumps(list(item.answer_facts), ensure_ascii=False)}",
        "Evidence previews:",
        *evidence_lines,
    ])


def _extract_json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("judge response must be a JSON object")
    return payload


def _clamped_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    return None


__all__ = [
    "BenchmarkSplit",
    "BenchmarkSuiteConfig",
    "BenchmarkSuiteResult",
    "GoldEvidenceAuditItem",
    "GoldEvidenceAuditReport",
    "GoldEvidenceJudgeItem",
    "GoldEvidenceJudgeReport",
    "OpenAICompatibleGoldEvidenceJudge",
    "PolicyPromotionCheck",
    "PolicyPromotionChecklist",
    "audit_gold_evidence",
    "run_benchmark_suite",
    "split_paper_ids",
]
