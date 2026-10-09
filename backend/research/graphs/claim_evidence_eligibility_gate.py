from __future__ import annotations

"""Harness adapter for exact-version Research claim eligibility decisions."""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from framework.events.canonical import checksum_for
from framework.harness import (
    DeterministicGate,
    GateContext,
    GateReference,
    GateRegistration,
    HarnessGateResult,
)

from backend.research.services.claim_evidence_eligibility import (
    NumericComparisonEligibilityResolver,
    build_qualified_claim_projection,
    source_ref_matches_record,
    RESEARCH_CLAIM_ELIGIBILITY_GATE_REF,
    numeric_comparison_claim_id,
    numeric_comparison_table_locator,
    trusted_claim_evidence_binding_values,
    trusted_numeric_comparison_contracts,
)
from backend.research.domain import (
    PaperSourceRecord,
    ResearchDocument,
    ResearchEvidencePack,
    ResearchClaim,
)
from backend.research.domain.claim_review import (
    ClaimEligibilityDecision,
    NumericComparisonEligibilityBundle,
    ResearchProgressObservation,
)


CLAIM_ELIGIBILITY_OUTPUT_KEY = "claim_evidence_eligibility"
_CANDIDATE_CONTROL_FIELDS = frozenset({
    "passed",
    "eligible",
    "publish",
    "route",
    "qualification_ref",
    "memory_write",
    "tool_authorization",
})
_LOCATOR_TYPE = "research_table_cells"


class ResearchClaimEvidenceEligibilityGate(DeterministicGate):
    gate_name = "ResearchClaimEvidenceEligibilityGate"
    gate_version = "1"
    gate_dependencies = ("ClaimEvidenceGate@1",)

    def __init__(self, resolver: NumericComparisonEligibilityResolver | None = None) -> None:
        self._resolver = resolver or NumericComparisonEligibilityResolver()

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        if context.worker_result is None:
            return _failure("claim eligibility worker result is required", "research_gate_input_invalid")
        output = context.worker_result.output
        forbidden = sorted(_CANDIDATE_CONTROL_FIELDS.intersection(output))
        if forbidden:
            return _failure(
                "candidate attempted to provide Harness control fields",
                "candidate_control_field_rejected",
                forbidden_fields=forbidden,
            )
        if CLAIM_ELIGIBILITY_OUTPUT_KEY not in output:
            paper_id = str(context.run_spec.inputs.get("paper_id") or "").strip()
            evidence_context_refs = {
                key: checksum_for(value)
                for key, value in context.outputs.items()
                if key in {"paper_source", "document", "evidence_pack"}
                and isinstance(value, Mapping)
            }
            if not paper_id or not evidence_context_refs:
                return _failure(
                    "missing V1 progress context is incomplete",
                    "research_gate_input_invalid",
                    missing_progress_context=[
                        name
                        for name, present in (
                            ("paper_id", bool(paper_id)),
                            ("evidence_context_refs", bool(evidence_context_refs)),
                        )
                        if not present
                    ],
                )
            progress = ResearchProgressObservation.insufficient_verified_progress(
                run_id=context.run_spec.run_id,
                paper_id=paper_id,
                evidence_context_refs=evidence_context_refs,
            )
            result = _failure(
                "no V1 numeric comparison result was collected",
                "claim_eligibility_insufficient_evidence",
                progress_observations=[progress.to_dict()],
                progress_observation_refs=[progress.checksum],
            )
            return result.with_evidence(
                gate_reference=RESEARCH_CLAIM_ELIGIBILITY_GATE_REF,
                input_ref=checksum_for({
                    "run_id": context.run_spec.run_id,
                    "trusted_outputs": context.outputs,
                    "candidate": output,
                }),
                reason_code="claim_eligibility_insufficient_evidence",
            )
        raw_payload = output.get(CLAIM_ELIGIBILITY_OUTPUT_KEY)
        if not isinstance(raw_payload, Mapping):
            return _failure(
                f"{CLAIM_ELIGIBILITY_OUTPUT_KEY} must be an object",
                "research_gate_input_invalid",
            )
        payload_forbidden = sorted(_CANDIDATE_CONTROL_FIELDS.intersection(raw_payload))
        if payload_forbidden:
            return _failure(
                "candidate eligibility payload contains control fields",
                "candidate_control_field_rejected",
                forbidden_fields=payload_forbidden,
            )
        if set(raw_payload) != {"bundle", "decision"}:
            return _failure(
                "claim eligibility payload must contain only bundle and decision",
                "research_gate_input_invalid",
                payload_fields=sorted(map(str, raw_payload)),
            )
        try:
            bundle = NumericComparisonEligibilityBundle.from_dict(raw_payload["bundle"])
            candidate_decision = ClaimEligibilityDecision.from_dict(raw_payload["decision"])
        except (TypeError, ValueError) as exc:
            return _failure(
                "claim eligibility payload is invalid",
                "claim_eligibility_replay_invalid",
                error_type=type(exc).__name__,
                error=str(exc),
            )

        trusted, failure = _trusted_research_inputs(context)
        if failure is not None:
            return failure
        assert trusted is not None
        source, document, evidence_pack = trusted
        trust_failure = _validate_trusted_binding(
            context,
            bundle,
            source=source,
            document=document,
            evidence_pack=evidence_pack,
        )
        if trust_failure is not None:
            return trust_failure

        try:
            replayed_decision = self._resolver.evaluate(bundle)
        except (TypeError, ValueError) as exc:
            return _failure(
                "claim eligibility payload failed deterministic replay",
                "claim_eligibility_replay_invalid",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        if candidate_decision != replayed_decision:
            return _failure(
                "candidate decision does not match deterministic replay",
                "claim_eligibility_replay_mismatch",
                candidate_decision_ref=candidate_decision.checksum,
                replayed_decision_ref=replayed_decision.checksum,
            )
        if replayed_decision.eligible:
            projection_failure = _validate_claim_projection(
                output,
                bundle=bundle,
                decision=replayed_decision,
                evidence_pack=evidence_pack,
            )
            if projection_failure is not None:
                return projection_failure
        status = replayed_decision.resolution.status.value
        bundle_ref = checksum_for(bundle.to_dict())
        result = HarnessGateResult(
            gate_name=self.gate_name,
            passed=replayed_decision.eligible,
            reason=None if replayed_decision.eligible else f"claim is not eligible: {status}",
            details={
                "reason_code": (
                    "claim_eligibility_supported"
                    if replayed_decision.eligible
                    else f"claim_eligibility_{status}"
                ),
                "bundle_ref": bundle_ref,
                "decision_ref": replayed_decision.checksum,
                "qualification_ref": replayed_decision.qualification_ref if replayed_decision.eligible else None,
                "binding_ref": replayed_decision.binding_checksum,
                "resolution_ref": replayed_decision.resolution.checksum,
                "resolver_ref": replayed_decision.resolver_ref,
                "source_locator_refs": list(replayed_decision.source_locator_checksums),
                "typed_observation_refs": list(replayed_decision.typed_observation_checksums),
                "trusted_source_ref": checksum_for(source.model_dump(mode="json")),
                "trusted_document_ref": checksum_for(document.model_dump(mode="json")),
                "trusted_evidence_pack_ref": checksum_for(evidence_pack.model_dump(mode="json")),
                "progress_observations": [
                    item.to_dict()
                    for item in replayed_decision.progress_observations
                ],
                "progress_observation_refs": [
                    item.checksum
                    for item in replayed_decision.progress_observations
                ],
            },
        )
        return result.with_evidence(
            gate_reference=RESEARCH_CLAIM_ELIGIBILITY_GATE_REF,
            input_ref=bundle_ref,
            reason_code=str(result.details["reason_code"]),
        )


def claim_evidence_eligibility_gate_registration() -> GateRegistration:
    gate = ResearchClaimEvidenceEligibilityGate()
    return GateRegistration(
        reference=GateReference.parse(RESEARCH_CLAIM_ELIGIBILITY_GATE_REF),
        gate=gate,
    )


def build_claim_evidence_eligibility_payload(
    bundle: NumericComparisonEligibilityBundle,
    *,
    resolver: NumericComparisonEligibilityResolver | None = None,
) -> dict[str, Any]:
    resolver = resolver or NumericComparisonEligibilityResolver()
    decision = resolver.evaluate(bundle)
    return {"bundle": bundle.to_dict(), "decision": decision.to_dict()}


def _validate_claim_projection(
    output: Mapping[str, Any],
    *,
    bundle: NumericComparisonEligibilityBundle,
    decision: ClaimEligibilityDecision,
    evidence_pack: ResearchEvidencePack,
) -> HarnessGateResult | None:
    raw_claims = output.get("claim_models")
    if not isinstance(raw_claims, Sequence) or isinstance(raw_claims, str | bytes):
        return _failure(
            "eligible V1 claim requires claim_models in the VERIFY output",
            "claim_projection_missing",
        )
    claims: list[ResearchClaim] = []
    try:
        for raw_claim in raw_claims:
            if not isinstance(raw_claim, Mapping):
                raise TypeError("claim_models entries must be objects")
            claims.append(ResearchClaim.model_validate(raw_claim))
    except (TypeError, ValueError, ValidationError) as exc:
        return _failure(
            "claim_models cannot be replayed for V1 qualification",
            "claim_projection_invalid",
            error_type=type(exc).__name__,
            error=str(exc),
        )
    try:
        expected = build_qualified_claim_projection(bundle, decision, evidence_pack)
    except ValueError as exc:
        return _failure(
            "eligible claim projection does not have one replayable evidence binding",
            "claim_projection_invalid",
            error=str(exc),
        )
    qualified = [
        claim
        for claim in claims
        if claim.claim_id == decision.claim_id
        or claim.metadata.get("eligibility_gate_ref") == decision.gate_ref
        or claim.metadata.get("qualification_ref") == decision.qualification_ref
    ]
    if len(qualified) != 1:
        return _failure(
            "VERIFY output must contain exactly one projection of the eligible V1 claim",
            "claim_projection_missing" if not qualified else "claim_projection_ambiguous",
            matching_claim_ids=[claim.claim_id for claim in qualified],
        )
    unauthorized = [
        claim.claim_id
        for claim in claims
        if claim is not qualified[0]
        and (
            claim.metadata.get("conclusion_status") == "verified_conclusion"
            or claim.metadata.get("qualification_ref") is not None
            or claim.metadata.get("eligibility_gate_ref") is not None
            or claim.metadata.get("eligibility_decision_ref") is not None
        )
    ]
    if unauthorized:
        return _failure(
            "candidate claims cannot inherit or self-report verified qualification",
            "claim_projection_unauthorized_qualification",
            claim_ids=unauthorized,
        )
    actual = qualified[0].to_dict()
    if actual != expected:
        return _failure(
            "published V1 claim does not match the replayed eligibility decision",
            "claim_projection_mismatch",
            expected_claim_ref=checksum_for(expected),
            actual_claim_ref=checksum_for(actual),
        )
    return None


def _trusted_research_inputs(
    context: GateContext,
) -> tuple[tuple[PaperSourceRecord, ResearchDocument, ResearchEvidencePack] | None, HarnessGateResult | None]:
    models: list[Any] = []
    for state_key, payload_key, model_type in (
        ("paper_source", "source_record", PaperSourceRecord),
        ("document", "document", ResearchDocument),
        ("evidence_pack", "evidence_pack", ResearchEvidencePack),
    ):
        prior = context.outputs.get(state_key)
        if not isinstance(prior, Mapping):
            return None, _failure(
                f"verified prior output {state_key} is required",
                "trusted_research_evidence_missing",
                state_output_key=state_key,
            )
        raw = prior.get(payload_key)
        if not isinstance(raw, Mapping):
            return None, _failure(
                f"verified prior payload {payload_key} is required",
                "trusted_research_evidence_missing",
                state_output_key=state_key,
                payload_key=payload_key,
            )
        try:
            models.append(model_type.model_validate(dict(raw)))
        except ValidationError as exc:
            return None, _failure(
                f"verified prior payload {payload_key} is invalid",
                "trusted_research_evidence_invalid",
                payload_key=payload_key,
                errors=exc.errors(include_url=False),
            )
    source, document, evidence_pack = models
    return (source, document, evidence_pack), None


def _validate_trusted_binding(
    context: GateContext,
    bundle: NumericComparisonEligibilityBundle,
    *,
    source: PaperSourceRecord,
    document: ResearchDocument,
    evidence_pack: ResearchEvidencePack,
) -> HarnessGateResult | None:
    binding = bundle.binding
    expected_paper_id = str(context.run_spec.inputs.get("paper_id") or "").strip()
    if not expected_paper_id or {
        binding.paper_id,
        binding.claim.scope.get("paper_id"),
        source.paper_id,
        document.paper_id,
        evidence_pack.paper_id,
    } != {expected_paper_id}:
        return _failure(
            "claim evidence belongs to another paper",
            "claim_evidence_paper_scope_mismatch",
            expected_paper_id=expected_paper_id,
        )
    if binding.run_id != context.run_spec.run_id:
        return _failure(
            "claim evidence belongs to another run",
            "claim_evidence_run_scope_mismatch",
        )
    trusted_input_scope = {
        key: str(context.run_spec.inputs.get(key) or "").strip()
        for key in ("tenant_id", "user_id", "memory_namespace")
        if str(context.run_spec.inputs.get(key) or "").strip()
    }
    expected_tenant_id = str(
        context.run_spec.inputs.get("tenant_id")
        or context.run_spec.metadata.get("graph_terminal_tenant_id")
        or ""
    ).strip()
    if not expected_tenant_id:
        return _failure(
            "run-owned Research tenant identity is required",
            "claim_evidence_tenant_scope_missing",
        )
    expected_binding_scope = {
        **trusted_input_scope,
        "tenant_id": expected_tenant_id,
    }
    if (
        binding.tenant_id != expected_tenant_id
        or any(
            binding.actor_scope.get(key) != value
            for key, value in expected_binding_scope.items()
        )
    ):
        return _failure(
            "claim evidence actor scope does not match the run",
            "claim_evidence_actor_scope_mismatch",
        )
    for owner, actual_scope in (
        ("paper_source", source.actor_scope),
        ("document", document.actor_scope),
        ("evidence_pack", evidence_pack.actor_scope),
    ):
        if (
            any(
                actual_scope.get(key) != value
                for key, value in trusted_input_scope.items()
            )
            or (
                actual_scope.get("tenant_id") is not None
                and actual_scope.get("tenant_id") != expected_tenant_id
            )
        ):
            return _failure(
                f"{owner} actor scope does not match the run",
                "trusted_research_actor_scope_mismatch",
                owner=owner,
            )

    try:
        trusted = trusted_claim_evidence_binding_values(source, document, evidence_pack)
    except ValueError as exc:
        return _failure(
            "trusted Research artifacts do not share one source revision",
            "trusted_research_lineage_mismatch",
            error=str(exc),
        )
    if (
        binding.paper_snapshot_checksum != trusted["paper_snapshot_checksum"]
        or binding.evidence_revision != trusted["evidence_revision"]
        or binding.evidence_checksum != trusted["evidence_checksum"]
    ):
        return _failure(
            "claim review binding does not match trusted Graph evidence",
            "claim_evidence_revision_mismatch",
            expected=trusted,
        )
    if not bundle.binding.evidence_locators:
        return _failure("source locator is required", "claim_source_locator_missing")
    for locator in bundle.binding.evidence_locators:
        if (
            locator.source_id != source.source_id
            or locator.source_revision != trusted["evidence_revision"]
            or locator.source_checksum != trusted["evidence_checksum"]
            or locator.locator_type != _LOCATOR_TYPE
        ):
            return _failure(
                "source locator does not match trusted Graph evidence",
                "claim_source_locator_mismatch",
            )
        if locator.availability.value == "available":
            failure = _validate_table_locator(
                locator.locator,
                bundle,
                source=source,
                document=document,
                evidence_pack=evidence_pack,
            )
            if failure is not None:
                return failure
    return None


def _validate_table_locator(
    locator: str,
    bundle: NumericComparisonEligibilityBundle,
    *,
    source: PaperSourceRecord,
    document: ResearchDocument,
    evidence_pack: ResearchEvidencePack,
) -> HarnessGateResult | None:
    conditions = bundle.claim_contract.conditions
    required = {"dataset", "split", "table_id", "label_column", "value_column"}
    if not required.issubset(conditions):
        return _failure(
            "numeric comparison conditions are missing trusted table bindings",
            "claim_condition_binding_missing",
            missing=sorted(required - set(conditions)),
        )
    matching_tables = [table for table in document.tables if table.table_id == conditions["table_id"]]
    if len(matching_tables) != 1:
        return _failure(
            "contracted Research table is missing or ambiguous",
            "claim_source_locator_unresolved",
        )
    table = matching_tables[0]
    if not source_ref_matches_record(table.source_ref, source):
        return _failure(
            "contracted table is not bound to the trusted paper source",
            "claim_source_lineage_mismatch",
        )
    try:
        trusted_contracts = trusted_numeric_comparison_contracts(table)
    except (TypeError, ValueError) as exc:
        return _failure(
            "trusted table comparison metadata is invalid",
            "claim_typed_observation_unavailable",
            error=str(exc),
        )
    if bundle.claim_contract not in trusted_contracts:
        return _failure(
            "typed comparison does not match trusted table conditions, metric, unit, or cells",
            "claim_typed_observation_mismatch",
            trusted_contract_refs=[item.checksum for item in trusted_contracts],
        )
    expected_claim_id = numeric_comparison_claim_id(
        paper_id=document.paper_id,
        table_id=table.table_id,
        contract=bundle.claim_contract,
    )
    if bundle.binding.claim.claim_id != expected_claim_id:
        return _failure(
            "claim identity does not match the trusted table comparison",
            "claim_identity_binding_mismatch",
            expected_claim_id=expected_claim_id,
        )
    expected_locator = numeric_comparison_table_locator(
        table_id=table.table_id,
        label_column=conditions["label_column"],
        subject_label=bundle.claim_contract.subject_label,
        comparator_label=bundle.claim_contract.comparator_label,
        value_column=conditions["value_column"],
    )
    if locator != expected_locator:
        return _failure(
            "source locator does not identify the contracted table cells",
            "claim_source_locator_mismatch",
            expected_locator=expected_locator,
        )
    evidence_matches = [
        item
        for item in evidence_pack.items
        if item.paper_id == document.paper_id
        and item.evidence_type == "numeric_comparison_table"
        and item.source_ref == table.source_ref
        and locator in item.span_refs
        and bundle.binding.claim.claim_id in item.claim_refs
        and item.lineage.source_hash == document.source_hash
        and table.source_ref in item.lineage.source_refs
        and item.metadata.get("claim_type") == bundle.binding.claim.claim_type
        and item.metadata.get("claim_contract") == bundle.claim_contract.to_dict()
    ]
    if len(evidence_matches) != 1:
        return _failure(
            "exactly one accepted evidence item must bind the claim to the trusted table cells",
            "claim_evidence_pack_binding_missing",
        )
    return None



def _failure(reason: str, reason_code: str, **details: Any) -> HarnessGateResult:
    return HarnessGateResult(
        gate_name=ResearchClaimEvidenceEligibilityGate.gate_name,
        passed=False,
        reason=reason,
        details={"reason_code": reason_code, **details},
    )


__all__ = [
    "CLAIM_ELIGIBILITY_OUTPUT_KEY",
    "ResearchClaimEvidenceEligibilityGate",
    "build_claim_evidence_eligibility_payload",
    "claim_evidence_eligibility_gate_registration",
    "numeric_comparison_table_locator",
    "trusted_claim_evidence_binding_values",
]
