from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.protocol_dataset import (
    AUTOMATED_REVIEW_POLICY,
    HUMAN_ONLY_REVIEW_POLICY,
    AutomatedConsensusRecord,
    ConsensusJudge,
    DatasetReviewManifest,
    GoldAnnotationSource,
    QuestionScope,
    ReferenceReview,
    ReviewAcceptancePolicy,
    ScopedPaper,
)

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
SCOPE = QuestionScope(
    sources=(ScopedPaper(paper_id="paper-1", document_version="v1", source_sha256="a" * 64),)
)


def _gold() -> GoldAnnotationSource:
    return GoldAnnotationSource(
        dataset_id="qasper",
        dataset_version="0.3",
        dataset_digest="b" * 64,
        original_case_id="question-1",
        annotation_fields=("free_form_answer", "evidence"),
        annotator_count=2,
    )


def _consensus(*, providers: tuple[str, str] = ("openai", "dashscope"), detection: float = 0.97,
               unanimous: bool = True) -> AutomatedConsensusRecord:
    return AutomatedConsensusRecord(
        judges=tuple(
            ConsensusJudge(provider=name, model_id=f"{name}-model", model_revision=None, prompt_version="1.0")
            for name in providers
        ),
        unanimous=unanimous,
        calibration_run_id="calibration-1",
        calibration_detection_rate=detection,
        calibration_false_alarm_rate=0.02,
    )


def _review(authority: str, **overrides) -> ReferenceReview:
    kind = {"human": "human", "dataset_gold": "rule", "automated_consensus": "llm"}[authority]
    payload = {
        "status": "approved",
        "authority": authority,
        "reviewer_id": f"{authority}-reviewer",
        "reviewer_kind": kind,
        "reviewed_at": NOW,
        "notes": None,
        "source_completeness": "complete",
        "checked_scope": SCOPE,
        "unanswerable_reason": None,
        "evidence_reference": "artifact://review/1",
        "gold_source": _gold() if authority == "dataset_gold" else None,
        "consensus": _consensus() if authority == "automated_consensus" else None,
    }
    payload.update(overrides)
    return ReferenceReview(**payload)


def test_each_authority_requires_its_own_actor_kind_and_record() -> None:
    assert _review("human").authority == "human"
    assert _review("dataset_gold").gold_source is not None
    assert _review("automated_consensus").consensus is not None
    with pytest.raises(ValidationError, match="reviewer_kind='rule'"):
        _review("dataset_gold", reviewer_kind="llm")
    with pytest.raises(ValidationError, match="gold_source is required"):
        _review("dataset_gold", gold_source=None)
    with pytest.raises(ValidationError, match="consensus is required"):
        _review("human", consensus=_consensus())
    with pytest.raises(ValidationError, match="named human"):
        _review("human", reviewer_kind="llm")


def test_consensus_needs_distinct_providers_and_unanimity() -> None:
    with pytest.raises(ValidationError, match="distinct providers"):
        _consensus(providers=("openai", "OpenAI"))
    with pytest.raises(ValidationError, match="unanimous"):
        _review("automated_consensus", consensus=_consensus(unanimous=False))


def test_policy_accepts_only_listed_authorities_and_calibrated_consensus() -> None:
    assert HUMAN_ONLY_REVIEW_POLICY.accepts(_review("human"))
    assert not HUMAN_ONLY_REVIEW_POLICY.accepts(_review("dataset_gold"))
    assert AUTOMATED_REVIEW_POLICY.accepts(_review("dataset_gold"))
    assert AUTOMATED_REVIEW_POLICY.accepts(_review("automated_consensus"))
    weak = _review("automated_consensus", consensus=_consensus(detection=0.80))
    assert AUTOMATED_REVIEW_POLICY.rejection_reason(weak) == (
        "judge calibration detection rate is below the policy threshold"
    )
    strict = ReviewAcceptancePolicy(
        policy_id="three-judges",
        policy_version="1.0",
        accepted_authorities=("automated_consensus",),
        min_consensus_judges=3,
    )
    assert "fewer judges" in (strict.rejection_reason(_review("automated_consensus")) or "")
    pending = _review("human", status="needs_review", reviewer_id=None, reviewer_kind=None,
                      reviewed_at=None, evidence_reference=None)
    assert AUTOMATED_REVIEW_POLICY.rejection_reason(pending) == "review status is 'needs_review'"


def test_dataset_review_manifest_authority_binds_reviewer_kind() -> None:
    def manifest(authority: str, kind: str) -> DatasetReviewManifest:
        return DatasetReviewManifest(
            status="approved",
            authority=authority,
            reviewer_id="qasper-gold-builder",
            reviewer_kind=kind,
            reviewed_at=NOW,
            approved_case_ids=("case-1",),
            corpus_checked=True,
            split_checked=True,
            contract_checked=True,
            evidence_reference="artifact://dataset-review/1",
        )

    assert manifest("dataset_gold", "rule").authority == "dataset_gold"
    with pytest.raises(ValidationError, match="named human"):
        manifest("human", "rule")
    with pytest.raises(ValidationError, match="reviewer_kind='llm'"):
        manifest("automated_consensus", "rule")
