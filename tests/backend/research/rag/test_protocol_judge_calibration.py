from __future__ import annotations

import asyncio

import pytest

from backend.research.ports.fact_entailment_judge import (
    EntailmentLabel,
    FactEntailmentJudgment,
    FactEntailmentRequest,
    FactJudgeIdentity,
    TargetJudgment,
)
from backend.research.rag.evaluation.protocol_fact_extraction import build_reference_projections
from backend.research.rag.evaluation.protocol_fact_mutation import MutationKind, generate_variants
from backend.research.rag.evaluation.protocol_judge_calibration import (
    JudgeCalibrationGate,
    calibrate_judges,
    consensus_record,
)
from backend.research.rag.evaluation.protocol_semantic_facts import FactRelation
from tests.backend.research.rag.test_protocol_fact_extraction import (
    EVIDENCE_TEXT,
    _numeric_draft,
    _numeric_reference,
    _result,
)

QUESTION = "What accuracy does Model A reach on the test split?"


def _variants():
    reference, projections = _variants_inputs()
    return reference, generate_variants("case-1", reference, projections)


class _Judge:
    """Labels a variant entailed only when it is the unmodified original."""

    def __init__(self, provider: str, *, lazy: bool = False) -> None:
        self._identity = FactJudgeIdentity(
            provider=provider, model_id=f"{provider}-m", model_revision="r1", prompt_version="v1",
        )
        self._lazy = lazy

    @property
    def identity(self) -> FactJudgeIdentity:
        return self._identity

    async def judge(self, request: FactEntailmentRequest) -> FactEntailmentJudgment:
        exact = request.claim_text in request.targets[0].supporting_text
        label = EntailmentLabel.ENTAILED if (self._lazy or exact) else EntailmentLabel.CONTRADICTED
        return FactEntailmentJudgment(
            request_id=request.request_id,
            judge=self._identity,
            judgments=(TargetJudgment(target_id=request.targets[0].target_id, label=label),),
        )


def test_mutations_are_deterministic_and_known_by_construction() -> None:
    _, (variants, report) = _variants()
    by_kind = {item.kind: item for item in variants}
    assert by_kind[MutationKind.IDENTITY].text == EVIDENCE_TEXT
    assert by_kind[MutationKind.IDENTITY].expect_entailed is True
    shifted = by_kind[MutationKind.NUMERIC_SHIFT]
    # 0.953 + 0.048 would exceed a fraction's ceiling, so the shift goes down.
    assert shifted.text == "The model obtains 0.905 accuracy on test under seed 7."
    assert shifted.deterministic_relation is FactRelation.CONFLICT
    assert by_kind[MutationKind.CONDITION_SWAP].text == "The model obtains 0.953 accuracy on test under seed 8."
    assert by_kind[MutationKind.CONDITION_SWAP].deterministically_detected is True
    assert by_kind[MutationKind.SPLIT_SWAP].text == "The model obtains 0.953 accuracy on dev under seed 7."
    assert "every dataset" in by_kind[MutationKind.OVERGENERALIZATION].text
    assert all(not item.expect_entailed for item in variants if item.kind is not MutationKind.IDENTITY)
    # Fraction values have no percent/point unit to swap, and the dataset id is
    # not written in the statement: both are reported as skipped, not invented.
    skipped = {item.kind for item in report.skipped}
    assert {MutationKind.UNIT_SWAP, MutationKind.DATASET_SWAP} <= skipped
    assert generate_variants("case-1", *_variants_inputs()) == (variants, report)


def _variants_inputs():
    reference = _numeric_reference()
    projections, _ = build_reference_projections(
        reference,
        {"ref-1": _result("ref-1", _numeric_draft(EVIDENCE_TEXT, "0.953", 0.953, "fraction"))},
    )
    return reference, projections


def test_good_panel_passes_and_backs_an_automated_consensus_record() -> None:
    reference, (variants, report) = _variants()
    judges = [_Judge("openai"), _Judge("dashscope")]
    calibration = asyncio.run(calibrate_judges(
        variants, report,
        references={"case-1": reference},
        questions={"case-1": QUESTION},
        judges=judges,
        gate=JudgeCalibrationGate(min_variants=3),
    ))
    assert calibration.passed, calibration.failures
    assert calibration.consensus.detection_rate == 1.0
    assert calibration.consensus.false_alarm_rate == 0.0
    assert calibration.deterministic_detection_rate == 1.0
    record = consensus_record(calibration, judges)
    assert record.calibration_run_id == calibration.calibration_run_id
    assert record.calibration_detection_rate == 1.0


def test_a_judge_that_accepts_everything_fails_the_gate() -> None:
    reference, (variants, report) = _variants()
    judges = [_Judge("openai", lazy=True), _Judge("dashscope", lazy=True)]
    calibration = asyncio.run(calibrate_judges(
        variants, report,
        references={"case-1": reference},
        questions={"case-1": QUESTION},
        judges=judges,
        gate=JudgeCalibrationGate(min_variants=3),
    ))
    assert not calibration.passed
    assert "consensus detection rate is below the gate" in calibration.failures
    with pytest.raises(ValueError, match="failed calibration"):
        consensus_record(calibration, judges)


def test_too_few_variants_fail_the_default_gate() -> None:
    reference, (variants, report) = _variants()
    calibration = asyncio.run(calibrate_judges(
        variants, report,
        references={"case-1": reference},
        questions={"case-1": QUESTION},
        judges=[_Judge("openai"), _Judge("dashscope")],
    ))
    assert not calibration.passed
    assert calibration.failures[0].startswith("only ")


def test_percent_statement_gets_numeric_unit_and_negation_mutations() -> None:
    from backend.research.ports.answer_fact_extraction import DraftCondition, FactDraft
    from backend.research.rag.evaluation.protocol_common import content_digest
    from backend.research.rag.evaluation.protocol_dataset import ReferenceClaim, ReferenceLabel

    text = "Model A is 95.3% accurate on the test split."
    base = _numeric_reference()
    anchor = base.evidence_anchors[0].model_copy(update={
        "source_excerpt": text, "source_excerpt_digest": content_digest(text),
    })
    reference = ReferenceLabel.model_validate({
        **base.model_dump(),
        "reference_claims": (ReferenceClaim(claim_id="ref-1", claim_text=text).model_dump(),),
        "evidence_anchors": (anchor.model_dump(),),
    })
    draft = FactDraft(
        statement=text, value_text="95.3%", numeric_value=95.3, unit="percent",
        object_id="Model A", split_id="test", statistic="accuracy",
        conditions=(DraftCondition(name="seed", value="7"),),
    )
    projections, _ = build_reference_projections(reference, {"ref-1": _result("ref-1", draft)})
    variants, _ = generate_variants("case-2", reference, projections)
    by_kind = {item.kind: item for item in variants}
    assert by_kind[MutationKind.NUMERIC_SHIFT].text == "Model A is 90.5% accurate on the test split."
    assert by_kind[MutationKind.UNIT_SWAP].text == "Model A is 95.3 percentage points accurate on the test split."
    assert by_kind[MutationKind.UNIT_SWAP].deterministic_relation is FactRelation.CONFLICT
    assert by_kind[MutationKind.NEGATION].text == "Model A is not 95.3% accurate on the test split."
