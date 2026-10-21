"""Deterministic retrieval scoring over index-bound evidence mappings."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION, SCHEMA_VERSION, EvaluationTrack, ExecutionProfile,
    ImmutableVersionStr, NonEmptyStr, ProtocolModel, Sha256Digest, content_digest,
)
from backend.research.rag.evaluation.protocol_evidence_mapping import (
    AnchorChunkContribution, AnchorEvidenceMapping, EvidenceMappingDataError,
    IndexEvidenceMapping, chunk_content_sha256,
)
from backend.research.rag.evaluation.protocol_scoring import (
    EvaluationScore, ScoreSource, ScoreStatus,
)
from framework.rag.evaluation import RetrievalMetricCase, hit_at_k, reciprocal_rank

DEFAULT_K_VALUES = (1, 3, 5, 10)
RETRIEVAL_METRIC_VERSION = "1.0"
ScoreState = Literal["ok", "error", "not_applicable", "needs_review"]
ContextVerificationState = Literal["ok", "error", "needs_review"]
GainMode = Literal["binary", "graded"]
QrelScoringUnit = Literal["source_evidence_relevance"]
QrelAbsenceReason = Literal["parser_evidence_loss"]


class FinalContextChunk(ProtocolModel):
    """Actual selected/truncated text sent to the answer model."""
    chunk_id: NonEmptyStr
    content: NonEmptyStr
    chunk_start: int | None = Field(default=None, ge=0, strict=True)
    chunk_end: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def _range_pair(self) -> "FinalContextChunk":
        if (self.chunk_start is None) != (self.chunk_end is None):
            raise ValueError("context chunk_start/chunk_end must be supplied together")
        if self.chunk_start is not None and self.chunk_end is not None:
            if self.chunk_end <= self.chunk_start:
                raise ValueError("context chunk_end must exceed chunk_start")
            if self.chunk_end - self.chunk_start != len(self.content):
                raise ValueError("context range length must equal actual context content length")
        return self


class FrozenQrelUnit(ProtocolModel):
    """Independent source-evidence relevance unit used by standard nDCG.

    Candidate chunk IDs are equivalent index realizations of this unit.  They
    are not necessary evidence groups and do not define group-aware recall.
    A unit may have no candidate only when an existing frozen source anchor is
    explicitly bound to parser-caused evidence loss for the current mapping.
    """
    unit_id: NonEmptyStr
    relevance: float = Field(gt=0.0)
    candidate_chunk_ids: tuple[NonEmptyStr, ...]
    scoring_unit: QrelScoringUnit = "source_evidence_relevance"
    source_anchor_id: NonEmptyStr | None = None
    absence_reason: QrelAbsenceReason | None = None

    @field_validator("relevance", mode="before")
    @classmethod
    def _finite_relevance(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("qrel relevance must be a finite numeric gain level")
        relevance = float(value)
        if not math.isfinite(relevance) or relevance <= 0.0:
            raise ValueError("qrel relevance must be finite and positive")
        try:
            gain = math.pow(2.0, relevance) - 1.0
        except OverflowError as exc:
            raise ValueError("qrel relevance overflows the graded gain function") from exc
        if not math.isfinite(gain):
            raise ValueError("qrel relevance overflows the graded gain function")
        return relevance

    @model_validator(mode="after")
    def _unique_candidates(self) -> "FrozenQrelUnit":
        if len(self.candidate_chunk_ids) != len(set(self.candidate_chunk_ids)):
            raise ValueError("qrel candidate_chunk_ids must be unique")
        if not self.candidate_chunk_ids:
            if self.source_anchor_id is None or self.absence_reason != "parser_evidence_loss":
                raise ValueError(
                    "empty qrel candidates require a frozen source_anchor_id and "
                    "absence_reason=parser_evidence_loss"
                )
        elif self.absence_reason is not None:
            raise ValueError(
                "qrel absence_reason is valid only when no index candidate exists"
            )
        return self


class FrozenQrels(ProtocolModel):
    version: ImmutableVersionStr
    gain_mode: GainMode
    units: tuple[FrozenQrelUnit, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_units(self) -> "FrozenQrels":
        ids = [item.unit_id for item in self.units]
        if len(ids) != len(set(ids)):
            raise ValueError("qrel unit_id values must be unique")
        candidate_owner: dict[str, str] = {}
        for unit in self.units:
            for candidate in unit.candidate_chunk_ids:
                previous = candidate_owner.setdefault(candidate, unit.unit_id)
                if previous != unit.unit_id:
                    raise ValueError(
                        "a qrel candidate chunk may belong to only one equivalence unit"
                    )
        if self.gain_mode == "binary" and any(item.relevance != 1.0 for item in self.units):
            raise ValueError("binary qrels require relevance=1 for every unit")
        return self


class RetrievalScoringConfig(ProtocolModel):
    k_values: tuple[int, ...] = DEFAULT_K_VALUES
    mrr_cutoff: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def _valid_k_values(self) -> "RetrievalScoringConfig":
        if not self.k_values or any(isinstance(k, bool) or k <= 0 for k in self.k_values):
            raise ValueError("k_values must contain positive integers")
        if len(self.k_values) != len(set(self.k_values)):
            raise ValueError("k_values must be unique")
        return self


class RetrievalDiagnostic(ProtocolModel):
    code: Literal[
        "duplicate_retrieval_id", "unknown_retrieval_id", "retrieval_overshoot",
        "duplicate_context_id", "unknown_context_id", "context_content_mismatch",
        "parser_evidence_loss", "gold_mapping_error",
    ]
    position: int | None = Field(default=None, ge=1, strict=True)
    chunk_id: NonEmptyStr | None
    detail: NonEmptyStr


class RetrievalMetricObservation(ProtocolModel):
    metric_id: NonEmptyStr
    version: ImmutableVersionStr
    k: int | None = Field(default=None, ge=1, strict=True)
    value: float | None
    status: ScoreState
    numerator: int | None = Field(default=None, ge=0, strict=True)
    denominator: int | None = Field(default=None, ge=0, strict=True)
    reason: NonEmptyStr | None
    provenance: dict[str, Any]
    evidence: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _status_contract(self) -> "RetrievalMetricObservation":
        if self.status == "ok":
            if self.value is None or not math.isfinite(self.value):
                raise ValueError("ok metric observations require a finite value")
        elif self.value is not None:
            raise ValueError("non-ok metric observations require value=null")
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("metric counts must both be null or both be present")
        return self


class RetrievalScoreIdentity(ProtocolModel):
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    dataset_digest: Sha256Digest
    run_id: NonEmptyStr
    case_id: NonEmptyStr
    attempt_id: NonEmptyStr
    track: Literal[EvaluationTrack.RETRIEVAL, EvaluationTrack.END_TO_END]
    execution_profile: ExecutionProfile
    variant_fingerprint: Sha256Digest
    repetition: int = Field(ge=1, strict=True)
    scope: NonEmptyStr
    scoring_revision: ImmutableVersionStr

    @field_validator("track", mode="before")
    @classmethod
    def _parse_track(cls, value: Any) -> Any:
        return EvaluationTrack(value) if isinstance(value, str) else value

    @field_validator("execution_profile", mode="before")
    @classmethod
    def _parse_execution_profile(cls, value: Any) -> Any:
        return ExecutionProfile(value) if isinstance(value, str) else value


class RetrievalScoreResult(ProtocolModel):
    mapping_digest: Sha256Digest
    context_fingerprint: Sha256Digest | None
    observations: tuple[RetrievalMetricObservation, ...]
    diagnostics: tuple[RetrievalDiagnostic, ...]

    def to_evaluation_scores(self, identity: RetrievalScoreIdentity) -> tuple[EvaluationScore, ...]:
        scores: list[EvaluationScore] = []
        for observation in self.observations:
            provenance = {
                **observation.provenance,
                "mapping_digest": self.mapping_digest,
                "context_fingerprint": self.context_fingerprint,
            }
            scores.append(EvaluationScore(
                record_type="score", schema_version=SCHEMA_VERSION,
                protocol_version=PROTOCOL_VERSION,
                dataset_id=identity.dataset_id, dataset_version=identity.dataset_version,
                dataset_digest=identity.dataset_digest, run_id=identity.run_id,
                case_id=identity.case_id, attempt_id=identity.attempt_id,
                track=identity.track, execution_profile=identity.execution_profile,
                variant_fingerprint=identity.variant_fingerprint,
                repetition=identity.repetition, scope=identity.scope,
                scoring_revision=identity.scoring_revision,
                metric_id=observation.metric_id, version=observation.version,
                value=observation.value, status=ScoreStatus(observation.status),
                numerator=observation.numerator, denominator=observation.denominator,
                source=ScoreSource.RULE, provenance=provenance,
                provenance_digest=content_digest(provenance), evidence=observation.evidence,
                reason=observation.reason,
            ))
        return tuple(scores)


def score_retrieval(
    *, mapping: IndexEvidenceMapping, ranked_chunks: Sequence[PaperChunk],
    final_context: Sequence[FinalContextChunk] = (),
    qrels: FrozenQrels | None = None,
    config: RetrievalScoringConfig = RetrievalScoringConfig(),
) -> RetrievalScoreResult:
    """Score actual ranking/context; positions are retained even when diagnostic."""
    mapping.verify_digest()
    if mapping.answerability == "unanswerable":
        return _non_applicable(mapping, config, "positive retrieval metrics do not apply to reviewed negative cases")
    if mapping.answerability == "needs_review":
        return _review_mapping(mapping, config, "answerability has not been reviewed and cannot enter a positive or negative denominator")
    if not mapping.required_evidence_groups or not mapping.anchor_mappings:
        return _invalid_mapping(mapping, config, "answerable mapping has no frozen evidence denominator")
    if any(item.disposition == "needs_review" for item in mapping.anchor_mappings):
        return _review_mapping(mapping, config, "gold mapping is ambiguous or lacks a reliable source basis")

    indexed = {item.chunk_id: item for item in mapping.indexed_chunks}
    diagnostics: list[RetrievalDiagnostic] = []
    ranked_ids: list[str] = []
    seen: set[str] = set()
    for position, chunk in enumerate(ranked_chunks, start=1):
        ranked_ids.append(chunk.chunk_id)
        expected = indexed.get(chunk.chunk_id)
        if chunk.chunk_id in seen:
            diagnostics.append(RetrievalDiagnostic(code="duplicate_retrieval_id", position=position,
                chunk_id=chunk.chunk_id, detail="duplicate result retained at its original rank"))
        seen.add(chunk.chunk_id)
        if expected is None:
            diagnostics.append(RetrievalDiagnostic(code="unknown_retrieval_id", position=position,
                chunk_id=chunk.chunk_id, detail="result is not a member of the bound index snapshot"))
        else:
            _validate_actual_chunk_identity(expected, chunk)
    max_k = max(config.k_values)
    if len(ranked_ids) > max_k:
        diagnostics.append(RetrievalDiagnostic(code="retrieval_overshoot", position=max_k + 1,
            chunk_id=ranked_ids[max_k], detail="ranking contains positions beyond configured metric cutoffs"))

    for item in mapping.anchor_mappings:
        if item.disposition == "system_failure":
            diagnostics.append(RetrievalDiagnostic(code="parser_evidence_loss", position=None,
                chunk_id=None, detail=f"anchor {item.anchor_id}: {item.reason or item.status}"))

    valid_hit_ids = _valid_gold_hit_ids(mapping)
    evidence_ref = f"mapping://{mapping.mapping_digest}"
    observations: list[RetrievalMetricObservation] = []
    for k in config.k_values:
        hit_value, _ = _raw_hit_metrics(ranked_ids[:k], valid_hit_ids)
        observations.append(_ok_observation(
            metric_id=f"retrieval.hit_at_{k}", k=k, value=hit_value,
            numerator=int(hit_value), denominator=1,
            provenance={"basis": "framework.hit_at_k", "rank_positions_retained": True},
            evidence=(evidence_ref,),
        ))
        coverage = _group_coverage(mapping, set(ranked_ids[:k]))
        observations.append(_ok_observation(
            metric_id=f"retrieval.evidence_recall_at_{k}", k=k, value=coverage,
            numerator=None, denominator=None,
            provenance={"basis": "necessary_group_max_alternative_anchor_coverage",
                        "necessary_group_count": len(mapping.required_evidence_groups)},
            evidence=(evidence_ref,),
        ))
        observations.append(_ndcg_observation(mapping, ranked_ids, qrels, k, evidence_ref))

    _, mrr_value = _raw_hit_metrics(ranked_ids, valid_hit_ids, config.mrr_cutoff)
    observations.append(_ok_observation(
        metric_id="retrieval.mrr", k=config.mrr_cutoff,
        value=mrr_value, numerator=None, denominator=None,
        provenance={"basis": "framework.reciprocal_rank", "raw_rank": True,
                    "cutoff": config.mrr_cutoff}, evidence=(evidence_ref,),
    ))

    context_fingerprint, context_ids, context_diagnostics, context_state, context_reason = _verify_context(
        mapping, final_context=final_context, indexed_chunks=indexed,
        actual_ranked={chunk.chunk_id: chunk for chunk in ranked_chunks},
    )
    diagnostics.extend(context_diagnostics)
    context_evidence = (evidence_ref, f"context://{context_fingerprint}")
    if context_state == "ok":
        context_coverage = _group_coverage_from_context(
            mapping, final_context, context_ids,
            {chunk.chunk_id: chunk for chunk in ranked_chunks},
        )
        observations.append(_ok_observation(
            metric_id="retrieval.context_evidence_coverage", k=None, value=context_coverage,
            numerator=None, denominator=None,
            provenance={"basis": "actual_selected_and_truncated_context",
                        "context_fingerprint": context_fingerprint},
            evidence=context_evidence,
        ))
    else:
        observations.append(_state_observation(
            metric_id="retrieval.context_evidence_coverage", k=None, status=context_state,
            reason=context_reason or "final context could not be verified",
            provenance={"basis": "actual_selected_and_truncated_context",
                        "context_fingerprint": context_fingerprint,
                        "verification": context_state},
            evidence=context_evidence,
        ))
    return RetrievalScoreResult(mapping_digest=mapping.mapping_digest,
                                context_fingerprint=context_fingerprint,
                                observations=tuple(observations), diagnostics=tuple(diagnostics))


def _validate_actual_chunk_identity(expected: Any, chunk: PaperChunk) -> None:
    if expected.paper_id != chunk.paper_id:
        raise EvidenceMappingDataError(f"retrieved chunk paper identity is stale for {chunk.chunk_id!r}")
    if expected.chunk_content_sha256 != chunk_content_sha256(chunk.content):
        raise EvidenceMappingDataError(f"retrieved chunk content is stale for {chunk.chunk_id!r}")
    metadata = chunk.metadata
    version = str(metadata.get("document_version") or "").strip()
    source_hash = str(metadata.get("source_sha256") or metadata.get("source_hash") or "").strip()
    locator = str(metadata.get("source_locator") or "").strip()
    if version and version != expected.document_version:
        raise EvidenceMappingDataError(f"retrieved chunk document version is stale for {chunk.chunk_id!r}")
    if source_hash and source_hash != expected.source_sha256:
        raise EvidenceMappingDataError(f"retrieved chunk source hash is stale for {chunk.chunk_id!r}")
    if locator and locator != expected.source_locator:
        raise EvidenceMappingDataError(f"retrieved chunk source locator is stale for {chunk.chunk_id!r}")


def _valid_gold_hit_ids(mapping: IndexEvidenceMapping) -> set[str]:
    hit_ids: set[str] = set()
    for anchor in mapping.anchor_mappings:
        if anchor.disposition == "valid":
            for path in anchor.chunk_sets:
                hit_ids.update(path.chunk_ids)
        elif anchor.disposition == "system_failure":
            hit_ids.update(anchor.partial_chunk_ids)
    return hit_ids


def _raw_hit_metrics(ranked_ids: Sequence[str], valid_hit_ids: set[str], cutoff: int | None = None) -> tuple[float, float]:
    considered = ranked_ids if cutoff is None else ranked_ids[:cutoff]
    case = RetrievalMetricCase(
        case_id="retrieval-hit", gold_evidence_ids=tuple(sorted(valid_hit_ids)),
        ranked_evidence_ids=tuple(considered),
    )
    return hit_at_k(case, len(considered)), reciprocal_rank(case)



def _covered_anchor_ids(mapping: IndexEvidenceMapping, available_ids: set[str]) -> set[str]:
    return {anchor.anchor_id for anchor in mapping.anchor_mappings
            if anchor.disposition == "valid"
            and any(set(path.chunk_ids).issubset(available_ids) for path in anchor.chunk_sets)}


def _group_coverage(mapping: IndexEvidenceMapping, available_ids: set[str]) -> float:
    covered = _covered_anchor_ids(mapping, available_ids)
    values = [max(len(covered.intersection(alternative)) / len(alternative)
                  for alternative in group.alternatives)
              for group in mapping.required_evidence_groups]
    return sum(values) / len(values)


def _verify_context(
    mapping: IndexEvidenceMapping, *, final_context: Sequence[FinalContextChunk],
    indexed_chunks: Mapping[str, Any], actual_ranked: Mapping[str, PaperChunk],
) -> tuple[str, set[str], list[RetrievalDiagnostic], ContextVerificationState, str | None]:
    del mapping  # identity is represented by indexed_chunks and actual_ranked
    diagnostics: list[RetrievalDiagnostic] = []
    ids: set[str] = set()
    seen: set[str] = set()
    state: ContextVerificationState = "ok"
    reasons: list[str] = []
    for position, item in enumerate(final_context, start=1):
        ids.add(item.chunk_id)
        if item.chunk_id in seen:
            diagnostics.append(RetrievalDiagnostic(code="duplicate_context_id", position=position,
                chunk_id=item.chunk_id, detail="duplicate context fragment retained for content coverage"))
        seen.add(item.chunk_id)
        expected = indexed_chunks.get(item.chunk_id)
        actual = actual_ranked.get(item.chunk_id)
        if expected is None or actual is None:
            diagnostics.append(RetrievalDiagnostic(code="unknown_context_id", position=position,
                chunk_id=item.chunk_id, detail="context fragment cannot be verified against an actual ranked chunk"))
            state = "error"
            reasons.append(f"context position {position} references an unknown or unranked chunk")
            continue
        resolved, issue_state, detail = _resolve_context_range(item, actual)
        if resolved is None:
            diagnostics.append(RetrievalDiagnostic(code="context_content_mismatch", position=position,
                chunk_id=item.chunk_id, detail=detail))
            if issue_state == "error" or state == "ok":
                state = issue_state or "error"
            reasons.append(f"context position {position}: {detail}")
    fingerprint = content_digest([item.model_dump(mode="json") for item in final_context])
    return fingerprint, ids, diagnostics, state, "; ".join(reasons) or None


def _resolve_context_range(
    item: FinalContextChunk, actual: PaperChunk,
) -> tuple[tuple[int, int] | None, ContextVerificationState | None, str]:
    if item.chunk_start is not None and item.chunk_end is not None:
        if item.chunk_end > len(actual.content):
            return None, "error", "explicit context range exceeds actual chunk content"
        if actual.content[item.chunk_start:item.chunk_end] != item.content:
            return None, "error", "explicit context range does not match actual chunk content"
        return (item.chunk_start, item.chunk_end), None, "verified explicit context range"
    if item.content == actual.content:
        return (0, len(actual.content)), None, "verified complete chunk"
    first = actual.content.find(item.content)
    if first < 0:
        return None, "error", "context text does not occur in actual chunk content"
    if actual.content.find(item.content, first + 1) >= 0:
        return None, "needs_review", "context text occurs more than once and has no explicit range"
    return (first, first + len(item.content)), None, "verified unique context slice"


def _context_range(item: FinalContextChunk, actual: PaperChunk) -> tuple[int, int] | None:
    resolved, _, _ = _resolve_context_range(item, actual)
    return resolved


def _group_coverage_from_context(
    mapping: IndexEvidenceMapping, final_context: Sequence[FinalContextChunk],
    context_ids: set[str], actual_ranked: Mapping[str, PaperChunk],
) -> float:
    ranges: dict[str, list[tuple[int, int]]] = {}
    for item in final_context:
        actual = actual_ranked.get(item.chunk_id)
        if actual is None:
            continue
        expected = next((chunk for chunk in mapping.indexed_chunks if chunk.chunk_id == item.chunk_id), None)
        if expected is None or expected.chunk_content_sha256 != chunk_content_sha256(actual.content):
            continue
        resolved = _context_range(item, actual)
        if resolved is not None:
            ranges.setdefault(item.chunk_id, []).append(resolved)
    covered: set[str] = set()
    for anchor in mapping.anchor_mappings:
        if anchor.disposition != "valid":
            continue
        for path in anchor.chunk_sets:
            if not set(path.chunk_ids).issubset(context_ids):
                continue
            if all(_range_covered(ranges.get(item.chunk_id, ()), item.chunk_start, item.chunk_end)
                   for item in path.contributions):
                covered.add(anchor.anchor_id)
                break
    values = [max(len(covered.intersection(alternative)) / len(alternative)
                  for alternative in group.alternatives)
              for group in mapping.required_evidence_groups]
    return sum(values) / len(values)


def _range_covered(ranges: Sequence[tuple[int, int]], start: int, end: int) -> bool:
    clipped = sorted((max(start, left), min(end, right)) for left, right in ranges
                     if max(start, left) < min(end, right))
    if not clipped:
        return False
    cursor = start
    for left, right in clipped:
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def _ndcg_observation(mapping: IndexEvidenceMapping, ranked_ids: Sequence[str],
                      qrels: FrozenQrels | None, k: int,
                      evidence_ref: str) -> RetrievalMetricObservation:
    if qrels is None:
        return _state_observation(
            metric_id=f"retrieval.ndcg_at_{k}", k=k, status="error",
            reason="frozen qrels are required for standard nDCG",
            provenance={"basis": "frozen_qrels_missing"}, evidence=(evidence_ref,),
        )
    binding_error = _qrel_mapping_error(mapping, qrels)
    if binding_error is not None:
        reason, binding_provenance = binding_error
        return _state_observation(
            metric_id=f"retrieval.ndcg_at_{k}", k=k, status="error",
            reason=reason,
            provenance={"qrels_version": qrels.version, "gain_mode": qrels.gain_mode,
                        "failure": "gold_mapping_error", **binding_provenance},
            evidence=(evidence_ref,),
        )
    candidate_to_units: dict[str, list[FrozenQrelUnit]] = {}
    for unit in qrels.units:
        for candidate in unit.candidate_chunk_ids:
            candidate_to_units.setdefault(candidate, []).append(unit)
    seen_units: set[str] = set()
    seen_candidates: set[str] = set()
    dcg = 0.0
    for rank, chunk_id in enumerate(ranked_ids[:k], start=1):
        if chunk_id in seen_candidates:
            continue
        seen_candidates.add(chunk_id)
        available = [unit for unit in candidate_to_units.get(chunk_id, ())
                     if unit.unit_id not in seen_units]
        if not available:
            continue
        unit = sorted(available, key=lambda item: (-_gain(item, qrels.gain_mode), item.unit_id))[0]
        seen_units.add(unit.unit_id)
        dcg += _gain(unit, qrels.gain_mode) / math.log2(rank + 1)
    ideal_gains = sorted((_gain(unit, qrels.gain_mode) for unit in qrels.units), reverse=True)
    idcg = sum(gain / math.log2(rank + 1)
               for rank, gain in enumerate(ideal_gains[:k], start=1))
    if idcg <= 0:
        return _state_observation(
            metric_id=f"retrieval.ndcg_at_{k}", k=k, status="error",
            reason="frozen qrels do not construct a positive ideal denominator",
            provenance={"qrels_version": qrels.version, "gain_mode": qrels.gain_mode},
            evidence=(evidence_ref,),
        )
    absent_units = [
        {"unit_id": unit.unit_id, "source_anchor_id": unit.source_anchor_id,
         "absence_reason": unit.absence_reason}
        for unit in qrels.units if not unit.candidate_chunk_ids
    ]
    return _ok_observation(
        metric_id=f"retrieval.ndcg_at_{k}", k=k, value=dcg / idcg,
        numerator=None, denominator=None,
        provenance={"basis": "standard_dcg_frozen_full_ideal",
                    "qrels_version": qrels.version, "gain_mode": qrels.gain_mode,
                    "scoring_unit": "source_evidence_relevance",
                    "candidate_binding": "equivalent_index_chunk_ids",
                    "group_coverage_semantics": "independent_metric",
                    "dcg": dcg, "idcg": idcg, "ideal_unit_count": len(qrels.units),
                    "unmapped_unit_count": len(absent_units),
                    "unmapped_units": absent_units,
                    "dedupe_unit": "qrel_unit_id_and_candidate_chunk_id",
                    "raw_rank": True},
        evidence=(evidence_ref,),
    )


def _qrel_mapping_error(
    mapping: IndexEvidenceMapping, qrels: FrozenQrels,
) -> tuple[str, dict[str, Any]] | None:
    try:
        FrozenQrels.model_validate(qrels.model_dump(mode="python"))
    except ValueError as exc:
        return (
            "frozen qrels violate the scoring-unit contract",
            {"validation_error": str(exc)},
        )
    valid_ids = {chunk.chunk_id for chunk in mapping.indexed_chunks}
    anchors = {anchor.anchor_id: anchor for anchor in mapping.anchor_mappings}
    for unit in qrels.units:
        if unit.candidate_chunk_ids:
            unknown_candidates = set(unit.candidate_chunk_ids) - valid_ids
            if unknown_candidates:
                return (
                    "frozen qrels contain an out-of-snapshot scoring unit",
                    {"unit_id": unit.unit_id,
                     "unknown_candidate_chunk_ids": sorted(unknown_candidates)},
                )
            if unit.source_anchor_id is not None and unit.source_anchor_id not in anchors:
                return (
                    "frozen qrel unit binds an unknown source anchor",
                    {"unit_id": unit.unit_id,
                     "source_anchor_id": unit.source_anchor_id},
                )
            continue

        anchor = anchors.get(unit.source_anchor_id or "")
        if anchor is None:
            return (
                "parser-loss qrel unit binds an unknown source anchor",
                {"unit_id": unit.unit_id,
                 "source_anchor_id": unit.source_anchor_id,
                 "absence_reason": unit.absence_reason},
            )
        if anchor.disposition != "system_failure" or anchor.chunk_sets:
            return (
                "parser-loss qrel unit conflicts with a source anchor that has a valid mapping path",
                {"unit_id": unit.unit_id, "source_anchor_id": anchor.anchor_id,
                 "anchor_status": anchor.status,
                 "anchor_disposition": anchor.disposition,
                 "absence_reason": unit.absence_reason},
            )
    return None


def _gain(unit: FrozenQrelUnit, mode: GainMode) -> float:
    return 1.0 if mode == "binary" else (2.0 ** unit.relevance) - 1.0


def _ok_observation(*, metric_id: str, k: int | None, value: float,
                    numerator: int | None, denominator: int | None,
                    provenance: dict[str, Any],
                    evidence: tuple[str, ...]) -> RetrievalMetricObservation:
    return RetrievalMetricObservation(
        metric_id=metric_id, version=RETRIEVAL_METRIC_VERSION, k=k,
        value=value, status="ok", numerator=numerator, denominator=denominator,
        reason=None, provenance=provenance, evidence=evidence,
    )


def _state_observation(*, metric_id: str, k: int | None, status: ScoreState,
                       reason: str, provenance: dict[str, Any],
                       evidence: tuple[str, ...]) -> RetrievalMetricObservation:
    return RetrievalMetricObservation(
        metric_id=metric_id, version=RETRIEVAL_METRIC_VERSION, k=k,
        value=None, status=status, numerator=None, denominator=None,
        reason=reason, provenance=provenance, evidence=evidence,
    )


def _metric_shapes(config: RetrievalScoringConfig) -> list[tuple[str, int | None]]:
    result: list[tuple[str, int | None]] = []
    for k in config.k_values:
        result.extend(((f"retrieval.hit_at_{k}", k),
                       (f"retrieval.evidence_recall_at_{k}", k),
                       (f"retrieval.ndcg_at_{k}", k)))
    result.extend((("retrieval.mrr", config.mrr_cutoff),
                   ("retrieval.context_evidence_coverage", None)))
    return result


def _non_applicable(mapping: IndexEvidenceMapping, config: RetrievalScoringConfig,
                    reason: str) -> RetrievalScoreResult:
    evidence = (f"mapping://{mapping.mapping_digest}",)
    observations = tuple(_state_observation(metric_id=metric_id, k=k,
        status="not_applicable", reason=reason,
        provenance={"answerability": mapping.answerability}, evidence=evidence)
        for metric_id, k in _metric_shapes(config))
    return RetrievalScoreResult(mapping_digest=mapping.mapping_digest,
                                context_fingerprint=None, observations=observations,
                                diagnostics=())


def _invalid_mapping(mapping: IndexEvidenceMapping, config: RetrievalScoringConfig,
                     reason: str) -> RetrievalScoreResult:
    evidence = (f"mapping://{mapping.mapping_digest}",)
    observations = tuple(_state_observation(metric_id=metric_id, k=k,
        status="error", reason=reason, provenance={"failure": "gold_mapping_error"},
        evidence=evidence) for metric_id, k in _metric_shapes(config))
    diagnostic = RetrievalDiagnostic(code="gold_mapping_error", position=None,
                                     chunk_id=None, detail=reason)
    return RetrievalScoreResult(mapping_digest=mapping.mapping_digest,
                                context_fingerprint=None, observations=observations,
                                diagnostics=(diagnostic,))


def _review_mapping(mapping: IndexEvidenceMapping, config: RetrievalScoringConfig,
                    reason: str) -> RetrievalScoreResult:
    evidence = (f"mapping://{mapping.mapping_digest}",)
    observations = tuple(_state_observation(metric_id=metric_id, k=k,
        status="needs_review", reason=reason,
        provenance={"failure": "gold_mapping_error"}, evidence=evidence)
        for metric_id, k in _metric_shapes(config))
    diagnostic = RetrievalDiagnostic(code="gold_mapping_error", position=None,
                                     chunk_id=None, detail=reason)
    return RetrievalScoreResult(mapping_digest=mapping.mapping_digest,
                                context_fingerprint=None, observations=observations,
                                diagnostics=(diagnostic,))


__all__ = [
    "DEFAULT_K_VALUES", "FinalContextChunk", "FrozenQrelUnit", "FrozenQrels",
    "QrelAbsenceReason", "QrelScoringUnit",
    "RetrievalDiagnostic", "RetrievalMetricObservation",
    "RetrievalScoreIdentity", "RetrievalScoreResult", "RetrievalScoringConfig",
    "score_retrieval",
]
