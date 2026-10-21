"""Judge calibration against injected errors, and the gate that guards it.

Every variant from ``protocol_fact_mutation`` is judged against its original
reference claim by every judge. Because the expected label is known by
construction, the run measures, per judge and for the unanimous consensus:

* detection rate: mutants judged not entailed;
* false-alarm rate: unmodified statements judged not entailed;
* coverage (consensus only): pairs where the judges agreed at all.

The gate fails a run whose consensus would let errors through, flag correct
statements, or decline to decide too often; a failed calibration means judge
resolutions of that panel must not be applied. Nothing here needs a human.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import Field

from backend.research.ports.fact_entailment_judge import (
    EntailmentLabel,
    EntailmentTarget,
    FactEntailmentJudgePort,
    FactEntailmentRequest,
    JudgeTargetKind,
)
from backend.research.rag.evaluation.protocol_claim_assessment import SemanticDecision
from backend.research.rag.evaluation.protocol_common import (
    NonEmptyStr,
    ProtocolModel,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import (
    AutomatedConsensusRecord,
    ConsensusJudge,
    ReferenceLabel,
)
from backend.research.rag.evaluation.protocol_fact_mutation import FactVariant, MutationReport
from backend.research.rag.evaluation.protocol_judge_consensus import (
    ConsensusReport,
    collect_judgments,
    merge_consensus,
)

JUDGE_CALIBRATION_RULE_VERSION = "judge-calibration/v1"


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


class JudgeCalibrationStats(ProtocolModel):
    judge: NonEmptyStr
    mutants: int = Field(ge=0, strict=True)
    detected: int = Field(ge=0, strict=True)
    originals: int = Field(ge=0, strict=True)
    false_alarms: int = Field(ge=0, strict=True)
    failed_requests: int = Field(ge=0, strict=True)

    @property
    def detection_rate(self) -> float | None:
        return _rate(self.detected, self.mutants)

    @property
    def false_alarm_rate(self) -> float | None:
        return _rate(self.false_alarms, self.originals)


class ConsensusCalibrationStats(ProtocolModel):
    variants: int = Field(ge=0, strict=True)
    resolved: int = Field(ge=0, strict=True)
    resolved_mutants: int = Field(ge=0, strict=True)
    detected: int = Field(ge=0, strict=True)
    resolved_originals: int = Field(ge=0, strict=True)
    false_alarms: int = Field(ge=0, strict=True)

    @property
    def coverage(self) -> float | None:
        return _rate(self.resolved, self.variants)

    @property
    def detection_rate(self) -> float | None:
        return _rate(self.detected, self.resolved_mutants)

    @property
    def false_alarm_rate(self) -> float | None:
        return _rate(self.false_alarms, self.resolved_originals)


class JudgeCalibrationGate(ProtocolModel):
    min_variants: int = Field(default=50, ge=1, strict=True)
    min_consensus_detection_rate: float = Field(default=0.95, ge=0.0, le=1.0)
    max_consensus_false_alarm_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    min_consensus_coverage: float = Field(default=0.80, ge=0.0, le=1.0)
    min_judge_detection_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    require_deterministic_detection: bool = True


class JudgeCalibrationReport(ProtocolModel):
    rule_version: NonEmptyStr = JUDGE_CALIBRATION_RULE_VERSION
    calibration_run_id: NonEmptyStr
    gate: JudgeCalibrationGate
    mutation: MutationReport
    judges: tuple[JudgeCalibrationStats, ...] = Field(min_length=2)
    consensus: ConsensusCalibrationStats
    consensus_report: ConsensusReport
    deterministic_mutants: int = Field(ge=0, strict=True)
    deterministic_detected: int = Field(ge=0, strict=True)
    passed: bool
    failures: tuple[NonEmptyStr, ...] = ()

    @property
    def deterministic_detection_rate(self) -> float | None:
        return _rate(self.deterministic_detected, self.deterministic_mutants)


def build_calibration_requests(
    variants: Sequence[FactVariant],
    references: Mapping[str, ReferenceLabel],
    questions: Mapping[str, str],
) -> tuple[FactEntailmentRequest, ...]:
    """One request per variant: the variant text judged against its original claim."""

    requests: list[FactEntailmentRequest] = []
    for variant in variants:
        reference = references[variant.case_id]
        claim = next(item for item in reference.reference_claims if item.claim_id == variant.reference_claim_id)
        excerpts = tuple(
            anchor.source_excerpt for anchor in reference.evidence_anchors
            if variant.reference_claim_id in anchor.claim_ids
        )
        requests.append(FactEntailmentRequest(
            request_id=variant.variant_id,
            question=questions[variant.case_id],
            claim_id=variant.variant_id,
            claim_text=variant.text,
            target_kind=JudgeTargetKind.REFERENCE,
            targets=(EntailmentTarget(
                target_id=variant.reference_claim_id,
                text=claim.claim_text,
                supporting_text=excerpts,
            ),),
        ))
    return tuple(requests)


def _gate_failures(
    gate: JudgeCalibrationGate,
    *,
    variants: int,
    judges: Sequence[JudgeCalibrationStats],
    consensus: ConsensusCalibrationStats,
    deterministic_mutants: int,
    deterministic_detected: int,
) -> list[str]:
    failures: list[str] = []
    if variants < gate.min_variants:
        failures.append(f"only {variants} calibration variants; at least {gate.min_variants} required")

    def below(value: float | None, threshold: float) -> bool:
        return value is None or value < threshold

    if below(consensus.coverage, gate.min_consensus_coverage):
        failures.append("consensus coverage is below the gate")
    if below(consensus.detection_rate, gate.min_consensus_detection_rate):
        failures.append("consensus detection rate is below the gate")
    if consensus.false_alarm_rate is None or consensus.false_alarm_rate > gate.max_consensus_false_alarm_rate:
        failures.append("consensus false-alarm rate exceeds the gate")
    for judge in judges:
        if below(judge.detection_rate, gate.min_judge_detection_rate):
            failures.append(f"judge {judge.judge} detection rate is below the gate")
    if gate.require_deterministic_detection and deterministic_detected != deterministic_mutants:
        failures.append("deterministic comparison missed a structured mutation")
    return failures


async def calibrate_judges(
    variants: Sequence[FactVariant],
    mutation: MutationReport,
    *,
    references: Mapping[str, ReferenceLabel],
    questions: Mapping[str, str],
    judges: Sequence[FactEntailmentJudgePort],
    gate: JudgeCalibrationGate = JudgeCalibrationGate(),
    max_concurrency: int = 4,
) -> JudgeCalibrationReport:
    requests = build_calibration_requests(variants, references, questions)
    collection = await collect_judgments(requests, judges, max_concurrency=max_concurrency)
    by_id = {item.variant_id: item for item in variants}
    judge_stats: list[JudgeCalibrationStats] = []
    for judge_index, key in enumerate(collection.judge_keys):
        mutants = detected = originals = false_alarms = failed = 0
        for request, per_request in zip(collection.requests, collection.answers, strict=True):
            answer = per_request[judge_index]
            if isinstance(answer, str):
                failed += 1
                continue
            label = answer.judgments[0].label
            if by_id[request.request_id].expect_entailed:
                originals += 1
                false_alarms += label is not EntailmentLabel.ENTAILED
            else:
                mutants += 1
                detected += label is not EntailmentLabel.ENTAILED
        judge_stats.append(JudgeCalibrationStats(
            judge=key, mutants=mutants, detected=detected,
            originals=originals, false_alarms=false_alarms, failed_requests=failed,
        ))
    resolutions, consensus_report = merge_consensus(collection)
    resolved_mutants = consensus_detected = resolved_originals = consensus_false = 0
    for resolution in resolutions:
        variant = by_id[resolution.claim_id]
        not_entailed = resolution.decision is not SemanticDecision.SUPPORTED
        if variant.expect_entailed:
            resolved_originals += 1
            consensus_false += not_entailed
        else:
            resolved_mutants += 1
            consensus_detected += not_entailed
    consensus = ConsensusCalibrationStats(
        variants=len(variants),
        resolved=len(resolutions),
        resolved_mutants=resolved_mutants,
        detected=consensus_detected,
        resolved_originals=resolved_originals,
        false_alarms=consensus_false,
    )
    structured = [item for item in variants if not item.expect_entailed and item.deterministic_relation is not None]
    deterministic_detected = sum(bool(item.deterministically_detected) for item in structured)
    failures = _gate_failures(
        gate,
        variants=len(variants),
        judges=judge_stats,
        consensus=consensus,
        deterministic_mutants=len(structured),
        deterministic_detected=deterministic_detected,
    )
    run_id = "calibration-" + content_digest({
        "rule_version": JUDGE_CALIBRATION_RULE_VERSION,
        "judges": sorted(collection.judge_keys),
        "variants": [item.model_dump(mode="json") for item in variants],
    })[:16]
    return JudgeCalibrationReport(
        calibration_run_id=run_id,
        gate=gate,
        mutation=mutation,
        judges=tuple(judge_stats),
        consensus=consensus,
        consensus_report=consensus_report,
        deterministic_mutants=len(structured),
        deterministic_detected=deterministic_detected,
        passed=not failures,
        failures=tuple(failures),
    )


def consensus_record(
    report: JudgeCalibrationReport,
    judges: Sequence[FactEntailmentJudgePort],
) -> AutomatedConsensusRecord:
    """The review record a passing calibration lets a judge panel sign."""

    if not report.passed:
        raise ValueError("a failed calibration cannot back automated consensus approval")
    detection = report.consensus.detection_rate
    false_alarm = report.consensus.false_alarm_rate
    assert detection is not None and false_alarm is not None
    return AutomatedConsensusRecord(
        judges=tuple(
            ConsensusJudge(
                provider=judge.identity.provider,
                model_id=judge.identity.model_id,
                model_revision=judge.identity.model_revision,
                prompt_version=judge.identity.prompt_version,
            )
            for judge in judges
        ),
        unanimous=True,
        calibration_run_id=report.calibration_run_id,
        calibration_detection_rate=detection,
        calibration_false_alarm_rate=false_alarm,
    )


__all__ = [
    "JUDGE_CALIBRATION_RULE_VERSION",
    "ConsensusCalibrationStats",
    "JudgeCalibrationGate",
    "JudgeCalibrationReport",
    "JudgeCalibrationStats",
    "build_calibration_requests",
    "calibrate_judges",
    "consensus_record",
]
