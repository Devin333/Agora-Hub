from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from framework.harness import (
    DeterministicGate,
    DeterministicGateRegistry,
    GateContext,
    GateReference,
    GateRegistration,
    HarnessGateResult,
)

from backend.research.benchmark.gates import validate_benchmark_score_refs, validate_score_range
from backend.research.benchmark.models import ResearchScore
from backend.research.domain import (
    EvidenceRef,
    GateResult,
    PaperSourceRecord,
    ResearchAnalysis,
    ResearchClaim,
    ResearchDocument,
    ResearchEvidencePack,
    ResearchPaper,
    ResearchQualityResult,
    ResearchReaderPayload,
    SourceLineage,
    ThreeMinuteRead,
)
from backend.research.paper_card.gates import (
    validate_github_metrics_source,
    validate_paper_card_code_url,
    validate_paper_card_required_fields,
    validate_paper_card_summary_evidence,
)
from backend.research.paper_card.models import ResearchPaperCard
from backend.research.rag.models import ResearchRAGContext
from backend.research.reader.gates import (
    validate_reader_navigation,
    validate_reader_payload_schema,
    validate_reader_source_lineage,
)
from backend.research.services.citation_verifier import CitationVerifier
from backend.research.services.quality_gate import ResearchQualityGate
from backend.research.graphs.claim_evidence_eligibility_gate import (
    ResearchClaimEvidenceEligibilityGate,
)


class PaperSourceLineageGateAdapter(DeterministicGate):
    gate_name = "PaperSourceLineageGate"
    gate_version = "1"

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        paper, failure = _model_from_output(
            context,
            output_key="paper",
            model_type=ResearchPaper,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        assert isinstance(paper, ResearchPaper)
        source_record, failure = _model_from_output(
            context,
            output_key="source_record",
            model_type=PaperSourceRecord,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        assert isinstance(source_record, PaperSourceRecord)

        lineage, failure = _source_lineage_from_output(context, gate_name=self.gate_name)
        if failure is not None:
            return failure
        assert lineage is not None

        expected_paper_id = str(context.run_spec.inputs.get("paper_id") or "")
        expected_source_ref = str(context.run_spec.inputs.get("source_ref") or "")
        failures: list[GateResult] = []
        if paper.paper_id != expected_paper_id:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "paper source does not match the requested paper",
                    metadata={"expected_paper_id": expected_paper_id, "actual_paper_id": paper.paper_id},
                )
            )
        if (
            source_record.paper_id != expected_paper_id
            or source_record.source_url != paper.source_url
            or not source_record.source_hash
        ):
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "paper source record does not match the requested paper identity",
                    metadata={
                        "expected_paper_id": expected_paper_id,
                        "record_paper_id": source_record.paper_id,
                        "paper_source_url": paper.source_url,
                        "record_source_url": source_record.source_url,
                        "source_hash_present": bool(source_record.source_hash),
                    },
                )
            )
        if expected_source_ref not in lineage.source_refs:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "paper source output does not preserve the requested source ref",
                    metadata={"expected_source_ref": expected_source_ref},
                )
            )
        return _from_domain_results(self.gate_name, failures or [GateResult.pass_(self.gate_name)])


class ResearchDocumentSchemaGateAdapter(DeterministicGate):
    gate_name = "ResearchDocumentSchemaGate"
    gate_version = "1"

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        document, failure = _model_from_output(
            context,
            output_key="document",
            model_type=ResearchDocument,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        assert isinstance(document, ResearchDocument)

        lineage, failure = _source_lineage_from_output(context, gate_name=self.gate_name)
        if failure is not None:
            return failure
        assert lineage is not None
        failures: list[GateResult] = []
        expected_paper_id = _expected_paper_id(context)
        source_record, prior_failure = _prior_model_from_state(
            context,
            state_output_key="paper_source",
            payload_key="source_record",
            model_type=PaperSourceRecord,
            gate_name=self.gate_name,
        )
        if prior_failure is not None:
            return prior_failure
        assert isinstance(source_record, PaperSourceRecord)
        if document.paper_id != expected_paper_id:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "document output does not match the requested paper",
                    metadata={
                        "expected_paper_id": expected_paper_id,
                        "actual_paper_id": document.paper_id,
                    },
                )
            )
        if (
            document.source_hash != source_record.source_hash
            or document.lineage.source_hash != source_record.source_hash
        ):
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "document source hash does not match the verified source record",
                    metadata={
                        "record_source_hash": source_record.source_hash,
                        "document_source_hash": document.source_hash,
                        "lineage_source_hash": document.lineage.source_hash,
                    },
                )
            )
        outside_source_scope = sorted(
            ref
            for ref in _document_source_refs(document)
            if not _source_ref_matches_record(ref, source_record)
        )
        if outside_source_scope:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "document contains source refs outside the verified source record",
                    metadata={"outside_source_refs": outside_source_scope},
                )
            )
        if set(lineage.source_refs) != set(document.lineage.source_refs):
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "document output source refs do not match document lineage",
                    metadata={
                        "output_source_refs": lineage.source_refs,
                        "document_source_refs": document.lineage.source_refs,
                    },
                )
            )
        return _from_domain_results(self.gate_name, failures or [GateResult.pass_(self.gate_name)])


class ResearchRAGContextProjectionGateAdapter(DeterministicGate):
    gate_name = "ResearchRAGContextProjectionGate"
    gate_version = "1"

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        rag_context, failure = _model_from_output(
            context,
            output_key="research_rag_context",
            model_type=ResearchRAGContext,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        assert isinstance(rag_context, ResearchRAGContext)

        lineage, failure = _source_lineage_from_output(context, gate_name=self.gate_name)
        if failure is not None:
            return failure
        assert lineage is not None
        failures: list[GateResult] = []
        expected_paper_id = _expected_paper_id(context)
        if rag_context.paper_id != expected_paper_id:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "RAG projection does not match the requested paper",
                    metadata={
                        "expected_paper_id": expected_paper_id,
                        "actual_paper_id": rag_context.paper_id,
                    },
                )
            )
        document, prior_failure = _prior_model_from_state(
            context,
            state_output_key="document",
            payload_key="document",
            model_type=ResearchDocument,
            gate_name=self.gate_name,
        )
        if prior_failure is not None:
            return prior_failure
        assert isinstance(document, ResearchDocument)
        allowed_source_refs = _document_source_refs(document)
        outside_scope = sorted(
            set(rag_context.goal.allowed_source_refs).union(rag_context.source_refs)
            - allowed_source_refs
        )
        if outside_scope:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "RAG projection contains source refs outside the verified document scope",
                    metadata={"outside_source_refs": outside_scope},
                )
            )
        if set(lineage.source_refs) != set(rag_context.source_refs):
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "RAG projection source refs do not match the Research context",
                    metadata={
                        "output_source_refs": lineage.source_refs,
                        "context_source_refs": rag_context.source_refs,
                    },
                )
            )
        if set(rag_context.lineage.source_refs) != set(rag_context.source_refs):
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "RAG context lineage does not match its accepted source refs",
                    metadata={
                        "lineage_source_refs": rag_context.lineage.source_refs,
                        "context_source_refs": rag_context.source_refs,
                    },
                )
            )
        return _from_domain_results(self.gate_name, failures or [GateResult.pass_(self.gate_name)])


class ResearchEvidenceCoverageGateAdapter(DeterministicGate):
    gate_name = "ResearchEvidenceCoverageGate"
    gate_version = "1"

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        evidence_pack, failure = _model_from_output(
            context,
            output_key="evidence_pack",
            model_type=ResearchEvidencePack,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        assert isinstance(evidence_pack, ResearchEvidencePack)

        failures: list[GateResult] = []
        expected_paper_id = _expected_paper_id(context)
        if evidence_pack.paper_id != expected_paper_id:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "evidence pack does not match the requested paper",
                    metadata={
                        "expected_paper_id": expected_paper_id,
                        "actual_paper_id": evidence_pack.paper_id,
                    },
                )
            )
        allowed_source_refs, prior_failure = _verified_evidence_source_scope(
            context,
            gate_name=self.gate_name,
        )
        if prior_failure is not None:
            return prior_failure
        assert allowed_source_refs is not None
        item_paper_ids = sorted(
            {item.paper_id for item in evidence_pack.items if item.paper_id != expected_paper_id}
        )
        if item_paper_ids:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "evidence items do not match the requested paper",
                    metadata={"unexpected_paper_ids": item_paper_ids},
                )
            )
        evidence_source_refs = set(evidence_pack.lineage.source_refs)
        for item in evidence_pack.items:
            evidence_source_refs.add(item.source_ref)
            evidence_source_refs.update(item.lineage.source_refs)
        outside_scope = sorted(evidence_source_refs - allowed_source_refs)
        if outside_scope:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "evidence pack contains source refs outside the verified retrieval scope",
                    metadata={"outside_source_refs": outside_scope},
                )
            )
        if not evidence_pack.items:
            failures.append(GateResult.fail(self.gate_name, "research evidence pack is empty"))
        if evidence_pack.missing_information:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "research evidence pack has unresolved information gaps",
                    metadata={"missing_information": evidence_pack.missing_information},
                )
            )
        return _from_domain_results(self.gate_name, failures or [GateResult.pass_(self.gate_name)])


class SummarySchemaGateAdapter(DeterministicGate):
    gate_name = "SummarySchemaGate"
    gate_version = "1"

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        _, failure = _model_from_output(
            context,
            output_key="three_minute_read",
            model_type=ThreeMinuteRead,
            gate_name=self.gate_name,
        )
        return failure or _from_domain_results(self.gate_name, [GateResult.pass_(self.gate_name)])


class SummaryEvidenceCoverageGateAdapter(DeterministicGate):
    gate_name = "SummaryEvidenceCoverageGate"
    gate_version = "1"

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        output, failure = _worker_output(context, gate_name=self.gate_name)
        if failure is not None:
            return failure
        assert output is not None
        raw_refs = output.get("summary_evidence_refs")
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str | bytes):
            return _invalid_input(
                self.gate_name,
                "summary_evidence_refs must be a sequence",
                output_key="summary_evidence_refs",
            )
        if not raw_refs:
            return _from_domain_results(
                self.gate_name,
                [GateResult.fail(self.gate_name, "summary requires evidence refs")],
            )
        try:
            refs = [EvidenceRef.model_validate(item) for item in raw_refs]
        except ValidationError as exc:
            return _validation_failure(self.gate_name, "summary_evidence_refs", exc)
        evidence_pack, prior_failure = _prior_evidence_pack(
            context,
            gate_name=self.gate_name,
        )
        if prior_failure is not None:
            return prior_failure
        assert evidence_pack is not None
        evidence_by_id = {item.evidence_id: item for item in evidence_pack.items}
        invalid_refs = sorted(
            ref.evidence_id
            for ref in refs
            if ref.evidence_id not in evidence_by_id
            or ref.source_ref != evidence_by_id[ref.evidence_id].source_ref
        )
        results = (
            [
                GateResult.fail(
                    self.gate_name,
                    "summary references evidence outside the verified evidence pack",
                    metadata={"invalid_evidence_ids": invalid_refs},
                )
            ]
            if invalid_refs
            else [
                GateResult.pass_(
                    self.gate_name,
                    metadata={"evidence_ref_count": len(refs)},
                )
            ]
        )
        return _from_domain_results(self.gate_name, results)


class BenchmarkEvidenceLineageGateAdapter(DeterministicGate):
    gate_name = "BenchmarkEvidenceLineageGate"
    gate_version = "1"

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        output, failure = _worker_output(context, gate_name=self.gate_name)
        if failure is not None:
            return failure
        assert output is not None
        raw_scores = output.get("scores")
        if not isinstance(raw_scores, Sequence) or isinstance(raw_scores, str | bytes):
            return _invalid_input(self.gate_name, "scores must be a sequence", output_key="scores")

        scores: list[ResearchScore] = []
        try:
            scores.extend(ResearchScore.model_validate(item) for item in raw_scores)
        except ValidationError as exc:
            return _validation_failure(self.gate_name, "scores", exc)

        evidence_pack, prior_failure = _prior_evidence_pack(
            context,
            gate_name=self.gate_name,
        )
        if prior_failure is not None:
            return prior_failure
        assert evidence_pack is not None
        expected_paper_id = _expected_paper_id(context)
        allowed_source_refs = _evidence_pack_source_refs(evidence_pack)
        domain_results = [
            result
            for score in scores
            for result in (validate_benchmark_score_refs(score), validate_score_range(score))
        ]
        for score in scores:
            if score.paper_id != expected_paper_id:
                domain_results.append(
                    GateResult.fail(
                        self.gate_name,
                        "benchmark score does not match the requested paper",
                        metadata={
                            "score_id": score.score_id,
                            "expected_paper_id": expected_paper_id,
                            "actual_paper_id": score.paper_id,
                        },
                    )
                )
            outside_scope = sorted(set(score.source_refs) - allowed_source_refs)
            if outside_scope:
                domain_results.append(
                    GateResult.fail(
                        self.gate_name,
                        "benchmark score contains source refs outside the verified evidence pack",
                        metadata={
                            "score_id": score.score_id,
                            "outside_source_refs": outside_scope,
                        },
                    )
                )
        if not domain_results:
            domain_results.append(
                GateResult.pass_(self.gate_name, metadata={"scores_present": False})
            )
        return _from_domain_results(self.gate_name, domain_results)


class ClaimEvidenceGateAdapter(DeterministicGate):
    gate_name = "ClaimEvidenceGate"
    gate_version = "1"

    def __init__(self, verifier: CitationVerifier | None = None) -> None:
        self._verifier = verifier or CitationVerifier()

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        output, failure = _worker_output(context, gate_name=self.gate_name)
        if failure is not None:
            return failure
        assert output is not None
        raw_claims = output.get("claim_models")
        if not isinstance(raw_claims, Sequence) or isinstance(raw_claims, str | bytes):
            return _invalid_input(
                self.gate_name,
                "claim_models must be a sequence",
                output_key="claim_models",
            )
        if not raw_claims:
            return _from_domain_results(
                self.gate_name,
                [GateResult.fail(self.gate_name, "claim verification requires claims")],
            )
        try:
            claims = [ResearchClaim.model_validate(item) for item in raw_claims]
        except ValidationError as exc:
            return _validation_failure(self.gate_name, "claim_models", exc)

        evidence_pack, failure = _prior_evidence_pack(
            context,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        assert isinstance(evidence_pack, ResearchEvidencePack)
        return _from_domain_results(
            self.gate_name,
            self._verifier.verify_claims(claims, evidence_pack),
        )


class ResearchQualityGateAdapter(DeterministicGate):
    gate_name = "ResearchQualityGate"
    gate_version = "1"

    def __init__(self, quality_gate: ResearchQualityGate | None = None) -> None:
        self._quality_gate = quality_gate or ResearchQualityGate()

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        analysis, failure = _model_from_output(
            context,
            output_key="analysis",
            model_type=ResearchAnalysis,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        quality, failure = _model_from_output(
            context,
            output_key="research_quality",
            model_type=ResearchQualityResult,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        assert isinstance(analysis, ResearchAnalysis)
        assert isinstance(quality, ResearchQualityResult)

        expected = self._quality_gate.evaluate(
            target_id=quality.target_id,
            target_type=quality.target_type,
            gate_results=quality.gate_results,
        )
        failures: list[GateResult] = []
        expected_paper_id = _expected_paper_id(context)
        evidence_pack, prior_failure = _prior_evidence_pack(
            context,
            gate_name=self.gate_name,
        )
        if prior_failure is not None:
            return prior_failure
        assert evidence_pack is not None
        if (
            analysis.paper_id != expected_paper_id
            or quality.target_id != expected_paper_id
            or quality.target_type != "summary"
            or analysis.evidence_pack_id != evidence_pack.pack_id
        ):
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "Research quality target does not match the verified run scope",
                    metadata={
                        "expected_paper_id": expected_paper_id,
                        "analysis_paper_id": analysis.paper_id,
                        "expected_evidence_pack_id": evidence_pack.pack_id,
                        "analysis_evidence_pack_id": analysis.evidence_pack_id,
                        "quality_target_id": quality.target_id,
                        "quality_target_type": quality.target_type,
                    },
                )
            )
        if quality.passed != expected.passed or quality.score != expected.score:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "Research quality result is inconsistent with its domain gate results",
                    metadata={
                        "expected_passed": expected.passed,
                        "actual_passed": quality.passed,
                        "expected_score": expected.score,
                        "actual_score": quality.score,
                    },
                )
            )
        if not quality.passed:
            failures.append(
                GateResult.fail(
                    self.gate_name,
                    "Research quality result did not pass",
                    metadata={
                        "failed_gates": [
                            result.gate_name
                            for result in quality.gate_results
                            if not result.passed
                        ]
                    },
                )
            )
        return _from_domain_results(
            self.gate_name,
            failures or [GateResult.pass_(self.gate_name)],
            details={"score": quality.score},
        )


class ReaderPayloadSchemaGateAdapter(DeterministicGate):
    gate_name = "ReaderPayloadSchemaGate"
    gate_version = "1"

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        payload, failure = _model_from_output(
            context,
            output_key="reader_payload",
            model_type=ResearchReaderPayload,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        assert isinstance(payload, ResearchReaderPayload)
        domain_results = [
            validate_reader_payload_schema(payload),
            validate_reader_source_lineage(payload),
            validate_reader_navigation(payload),
        ]
        expected_paper_id = _expected_paper_id(context)
        if payload.paper.paper_id != expected_paper_id:
            domain_results.append(
                GateResult.fail(
                    self.gate_name,
                    "reader payload does not match the requested paper",
                    metadata={
                        "expected_paper_id": expected_paper_id,
                        "actual_paper_id": payload.paper.paper_id,
                    },
                )
            )
        output = context.worker_result.output if context.worker_result is not None else {}
        if output.get("reader_issue") is not None:
            domain_results.append(
                GateResult.fail(
                    "ReaderIssueGate",
                    "reader payload requires repair",
                    metadata={"reader_issue": output["reader_issue"]},
                )
            )
        return _from_domain_results(self.gate_name, domain_results)


class ResearchPaperCardGateAdapter(DeterministicGate):
    gate_name = "ResearchPaperCardGate"
    gate_version = "1"

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        card, failure = _model_from_output(
            context,
            output_key="paper_card",
            model_type=ResearchPaperCard,
            gate_name=self.gate_name,
        )
        if failure is not None:
            return failure
        assert isinstance(card, ResearchPaperCard)
        results = [
            validate_paper_card_required_fields(card),
            validate_github_metrics_source(card),
            validate_paper_card_summary_evidence(card),
            validate_paper_card_code_url(card),
        ]
        expected_paper_id = _expected_paper_id(context)
        paper, prior_failure = _prior_model_from_state(
            context,
            state_output_key="paper_source",
            payload_key="paper",
            model_type=ResearchPaper,
            gate_name=self.gate_name,
        )
        if prior_failure is not None:
            return prior_failure
        assert isinstance(paper, ResearchPaper)
        if card.paper_id != expected_paper_id or card.source_url != paper.source_url:
            results.append(
                GateResult.fail(
                    self.gate_name,
                    "paper card does not match the verified run scope",
                    metadata={
                        "expected_paper_id": expected_paper_id,
                        "actual_paper_id": card.paper_id,
                        "requested_source_ref": str(
                            context.run_spec.inputs.get("source_ref") or ""
                        ),
                        "verified_source_ref": paper.source_url,
                        "actual_source_ref": card.source_url,
                    },
                )
            )
        return _from_domain_results(self.gate_name, results)


_PAPER_ANALYSIS_BASE_GATE_TYPES = (
    PaperSourceLineageGateAdapter,
    ResearchDocumentSchemaGateAdapter,
    ResearchRAGContextProjectionGateAdapter,
    ResearchEvidenceCoverageGateAdapter,
    SummarySchemaGateAdapter,
    SummaryEvidenceCoverageGateAdapter,
    BenchmarkEvidenceLineageGateAdapter,
    ClaimEvidenceGateAdapter,
    ResearchQualityGateAdapter,
    ReaderPayloadSchemaGateAdapter,
    ResearchPaperCardGateAdapter,
)

# The default/static and ordinary dynamic Graphs execute this exact inventory.
# Keep this public name scoped to that long-standing contract.
PAPER_ANALYSIS_GATE_REFERENCES = tuple(
    f"{gate_type.gate_name}@{gate_type.gate_version}"
    for gate_type in _PAPER_ANALYSIS_BASE_GATE_TYPES
)

# The opt-in quality Graph executes the base inventory plus the exact V1 claim
# eligibility gate. The registry exposes the union so either selected Graph can
# resolve every declared gate without making the opt-in gate a default runtime
# requirement.
PAPER_ANALYSIS_QUALITY_GATE_REFERENCES = (
    *PAPER_ANALYSIS_GATE_REFERENCES,
    "ResearchClaimEvidenceEligibilityGate@1",
)
PAPER_ANALYSIS_AVAILABLE_GATE_REFERENCES = (
    PAPER_ANALYSIS_QUALITY_GATE_REFERENCES
)
_PAPER_ANALYSIS_AVAILABLE_GATE_TYPES = (
    *_PAPER_ANALYSIS_BASE_GATE_TYPES,
    ResearchClaimEvidenceEligibilityGate,
)


def build_paper_analysis_gate_registry() -> DeterministicGateRegistry:
    gates = tuple(
        gate_type() for gate_type in _PAPER_ANALYSIS_AVAILABLE_GATE_TYPES
    )
    return DeterministicGateRegistry(
        GateRegistration(
            reference=GateReference(gate_id=gate.gate_name, version=gate.gate_version),
            gate=gate,
            dependencies=tuple(
                GateReference.parse(reference)
                for reference in getattr(gate, "gate_dependencies", ())
            ),
        )
        for gate in gates
    )


def _expected_paper_id(context: GateContext) -> str:
    return str(context.run_spec.inputs.get("paper_id") or "")


def _prior_model_from_state(
    context: GateContext,
    *,
    state_output_key: str,
    payload_key: str,
    model_type: type[BaseModel],
    gate_name: str,
) -> tuple[BaseModel | None, HarnessGateResult | None]:
    prior_output = context.outputs.get(state_output_key)
    if not isinstance(prior_output, Mapping):
        return None, _invalid_input(
            gate_name,
            f"verified prior output {state_output_key} is required",
            state_output_key=state_output_key,
        )
    payload = prior_output.get(payload_key)
    if not isinstance(payload, Mapping):
        return None, _invalid_input(
            gate_name,
            f"verified prior payload {payload_key} is required",
            state_output_key=state_output_key,
            payload_key=payload_key,
        )
    try:
        return model_type.model_validate(dict(payload)), None
    except ValidationError as exc:
        return None, _validation_failure(gate_name, payload_key, exc)


def _prior_evidence_pack(
    context: GateContext,
    *,
    gate_name: str,
) -> tuple[ResearchEvidencePack | None, HarnessGateResult | None]:
    value, failure = _prior_model_from_state(
        context,
        state_output_key="evidence_pack",
        payload_key="evidence_pack",
        model_type=ResearchEvidencePack,
        gate_name=gate_name,
    )
    if failure is not None:
        return None, failure
    assert isinstance(value, ResearchEvidencePack)
    return value, None


def _document_source_refs(document: ResearchDocument) -> set[str]:
    refs = set(document.lineage.source_refs)
    refs.update(section.source_ref for section in document.sections)
    refs.update(figure.source_ref for figure in document.figures)
    refs.update(table.source_ref for table in document.tables)
    refs.update(equation.source_ref for equation in document.equations)
    return refs


def _source_ref_matches_record(
    source_ref: str,
    source_record: PaperSourceRecord,
) -> bool:
    metadata_refs = {
        str(source_record.metadata.get(key) or "").strip()
        for key in ("source_ref", "pdf_url")
    }
    prefixes = {
        source_record.source_url,
        f"paper://{source_record.paper_id}",
        f"{source_record.source_type}://{source_record.paper_id}",
        f"source://{source_record.source_id}",
        *metadata_refs,
    }
    for prefix in prefixes:
        normalized = prefix.rstrip("/")
        if not normalized:
            continue
        if source_ref == normalized or source_ref.startswith(
            (f"{normalized}/", f"{normalized}#", f"{normalized}?"),
        ):
            return True
    return False


def _evidence_pack_source_refs(evidence_pack: ResearchEvidencePack) -> set[str]:
    refs = set(evidence_pack.lineage.source_refs)
    for item in evidence_pack.items:
        refs.add(item.source_ref)
        refs.update(item.lineage.source_refs)
    return refs


def _verified_evidence_source_scope(
    context: GateContext,
    *,
    gate_name: str,
) -> tuple[set[str] | None, HarnessGateResult | None]:
    document_value, failure = _prior_model_from_state(
        context,
        state_output_key="document",
        payload_key="document",
        model_type=ResearchDocument,
        gate_name=gate_name,
    )
    if failure is not None:
        return None, failure
    assert isinstance(document_value, ResearchDocument)
    allowed = _document_source_refs(document_value)

    rag_value, failure = _prior_model_from_state(
        context,
        state_output_key="research_rag_context",
        payload_key="research_rag_context",
        model_type=ResearchRAGContext,
        gate_name=gate_name,
    )
    if failure is not None:
        return None, failure
    assert isinstance(rag_value, ResearchRAGContext)
    allowed.update(rag_value.source_refs)
    allowed.update(rag_value.lineage.source_refs)
    return allowed, None


def _worker_output(
    context: GateContext,
    *,
    gate_name: str,
) -> tuple[dict[str, Any] | None, HarnessGateResult | None]:
    if context.worker_result is None:
        return None, _invalid_input(gate_name, "worker result is required")
    return context.worker_result.output, None


def _model_from_output(
    context: GateContext,
    *,
    output_key: str,
    model_type: type[BaseModel],
    gate_name: str,
) -> tuple[BaseModel | None, HarnessGateResult | None]:
    output, failure = _worker_output(context, gate_name=gate_name)
    if failure is not None:
        return None, failure
    assert output is not None
    payload = output.get(output_key)
    if not isinstance(payload, Mapping):
        return None, _invalid_input(
            gate_name,
            f"{output_key} must be an object",
            output_key=output_key,
        )
    try:
        return model_type.model_validate(dict(payload)), None
    except ValidationError as exc:
        return None, _validation_failure(gate_name, output_key, exc)


def _source_lineage_from_output(
    context: GateContext,
    *,
    gate_name: str,
) -> tuple[SourceLineage | None, HarnessGateResult | None]:
    output, failure = _worker_output(context, gate_name=gate_name)
    if failure is not None:
        return None, failure
    assert output is not None
    raw_refs = output.get("source_refs")
    if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str | bytes):
        return None, _invalid_input(
            gate_name,
            "source_refs must be a sequence",
            output_key="source_refs",
        )
    try:
        lineage = SourceLineage(source_refs=[str(item) for item in raw_refs]).require_refs()
    except (TypeError, ValueError) as exc:
        return None, _invalid_input(
            gate_name,
            "source_refs do not form valid Research source lineage",
            output_key="source_refs",
            error_type=type(exc).__name__,
        )
    return lineage, None


def _validation_failure(
    gate_name: str,
    output_key: str,
    exc: ValidationError,
) -> HarnessGateResult:
    errors = [
        {
            "path": ".".join(str(part) for part in error.get("loc", ())),
            "type": str(error.get("type") or "validation_error"),
            "message": str(error.get("msg") or "invalid value"),
        }
        for error in exc.errors(include_url=False)
    ]
    return _invalid_input(
        gate_name,
        f"{output_key} does not match the Research domain model",
        output_key=output_key,
        errors=errors,
    )


def _invalid_input(gate_name: str, reason: str, **details: Any) -> HarnessGateResult:
    return HarnessGateResult(
        gate_name=gate_name,
        passed=False,
        reason=reason,
        details={"reason_code": "research_gate_input_invalid", **details},
    )


def _from_domain_results(
    gate_name: str,
    results: Sequence[GateResult],
    *,
    details: Mapping[str, Any] | None = None,
) -> HarnessGateResult:
    failed = [result for result in results if not result.passed]
    reasons = [reason for result in failed for reason in result.reasons]
    return HarnessGateResult(
        gate_name=gate_name,
        passed=not failed,
        reason="; ".join(reasons) or (None if not failed else "Research domain rule failed"),
        details={
            "reason_code": "research_domain_rule_failed" if failed else "research_domain_rule_passed",
            "domain_results": [result.to_dict() for result in results],
            **dict(details or {}),
        },
    )


__all__ = [
    "BenchmarkEvidenceLineageGateAdapter",
    "ClaimEvidenceGateAdapter",
    "PAPER_ANALYSIS_AVAILABLE_GATE_REFERENCES",
    "PAPER_ANALYSIS_GATE_REFERENCES",
    "PAPER_ANALYSIS_QUALITY_GATE_REFERENCES",
    "PaperSourceLineageGateAdapter",
    "ReaderPayloadSchemaGateAdapter",
    "ResearchDocumentSchemaGateAdapter",
    "ResearchClaimEvidenceEligibilityGate",
    "ResearchEvidenceCoverageGateAdapter",
    "ResearchPaperCardGateAdapter",
    "ResearchQualityGateAdapter",
    "ResearchRAGContextProjectionGateAdapter",
    "SummaryEvidenceCoverageGateAdapter",
    "SummarySchemaGateAdapter",
    "build_paper_analysis_gate_registry",
]
