from __future__ import annotations

"""Application composition for source-bound Research claim eligibility."""

from collections.abc import Mapping

from backend.research.domain.claim_review import (
    AssertionKind,
    ClaimEligibilityDecision,
    ClaimRevision,
    EvidenceSourceLocator,
    NumericComparisonEligibilityBundle,
    NumericComparisonObservation,
    ObservationProducer,
    ReviewBinding,
    ReviewObservation,
    V1_NUMERIC_COMPARISON_CLAIM_TYPE,
)
from backend.research.domain.document import ResearchDocument, ResearchTable
from backend.research.domain.evidence import ResearchEvidencePack
from backend.research.domain.paper import PaperSourceRecord
from backend.research.services.claim_evidence_eligibility import (
    NumericComparisonEligibilityResolver,
    RESEARCH_CLAIM_ELIGIBILITY_GATE_REF,
    RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF,
    numeric_comparison_claim_id,
    numeric_comparison_table_locator,
    render_numeric_comparison_claim_text,
    source_ref_matches_record,
    trusted_claim_evidence_binding_values,
    trusted_numeric_comparison_contracts,
)


class ClaimEvidenceEligibilityService:
    """Compose accepted table data into the replayable deterministic bundle."""

    def __init__(
        self,
        resolver: NumericComparisonEligibilityResolver | None = None,
    ) -> None:
        self._resolver = resolver or NumericComparisonEligibilityResolver()

    def collect_bundle(
        self,
        *,
        tenant_id: str,
        actor_scope: Mapping[str, str],
        run_id: str,
        source: PaperSourceRecord,
        document: ResearchDocument,
        evidence_pack: ResearchEvidencePack,
        objective_revision: str,
        producer_id: str,
        table_id: str | None = None,
        producer_type: ObservationProducer = ObservationProducer.PARSER,
        reviews: tuple[ReviewObservation, ...] = (),
        rule_version: str = "research-numeric-comparison-rule-1",
        strategy_version: str = "research-numeric-comparison-strategy-1",
    ) -> NumericComparisonEligibilityBundle:
        if source.paper_id != document.paper_id or source.paper_id != evidence_pack.paper_id:
            raise ValueError("accepted source, document, and evidence pack must share one paper")
        _validate_collection_scope(
            tenant_id=tenant_id,
            actor_scope=actor_scope,
            source=source,
            document=document,
            evidence_pack=evidence_pack,
        )
        candidates = [
            (table, contract)
            for table in document.tables
            if table_id is None or table.table_id == table_id
            for contract in trusted_numeric_comparison_contracts(table)
        ]
        if len(candidates) != 1:
            raise ValueError(
                "V1 eligibility collection requires exactly one structured numeric comparison"
            )
        table, contract = candidates[0]
        if not source_ref_matches_record(table.source_ref, source):
            raise ValueError("accepted comparison table does not belong to the paper source")
        claim_id = numeric_comparison_claim_id(
            paper_id=document.paper_id,
            table_id=table.table_id,
            contract=contract,
        )
        locator_text = numeric_comparison_table_locator(
            table_id=table.table_id,
            label_column=contract.conditions["label_column"],
            subject_label=contract.subject_label,
            comparator_label=contract.comparator_label,
            value_column=contract.conditions["value_column"],
        )
        matching_evidence = [
            item
            for item in evidence_pack.items
            if item.paper_id == document.paper_id
            and item.evidence_type == "numeric_comparison_table"
            and item.source_ref == table.source_ref
            and locator_text in item.span_refs
            and claim_id in item.claim_refs
            and item.lineage.source_hash == document.source_hash
            and table.source_ref in item.lineage.source_refs
            and item.metadata.get("claim_type") == V1_NUMERIC_COMPARISON_CLAIM_TYPE
            and item.metadata.get("claim_contract") == contract.to_dict()
        ]
        if len(matching_evidence) != 1:
            raise ValueError(
                "accepted evidence pack must contain exactly one typed table binding"
            )
        trusted = trusted_claim_evidence_binding_values(
            source,
            document,
            evidence_pack,
        )
        claim = ClaimRevision(
            claim_id=claim_id,
            revision=1,
            text=render_numeric_comparison_claim_text(contract),
            assertion_kind=AssertionKind.REPORTED_RESULT,
            claim_type=V1_NUMERIC_COMPARISON_CLAIM_TYPE,
            scope={"paper_id": document.paper_id},
            objective_revision=objective_revision,
        )
        locator = EvidenceSourceLocator(
            source_id=source.source_id,
            source_revision=trusted["evidence_revision"],
            source_checksum=trusted["evidence_checksum"],
            locator_type="research_table_cells",
            locator=locator_text,
        )
        binding = ReviewBinding(
            tenant_id=tenant_id,
            actor_scope=actor_scope,
            run_id=run_id,
            paper_id=document.paper_id,
            paper_snapshot_checksum=trusted["paper_snapshot_checksum"],
            evidence_revision=trusted["evidence_revision"],
            evidence_checksum=trusted["evidence_checksum"],
            claim=claim,
            rule_version=rule_version,
            strategy_version=strategy_version,
            evidence_locators=(locator,),
            applicable_conditions=contract.conditions,
        )
        return self.prepare_bundle(
            binding=binding,
            table=table,
            producer_id=producer_id,
            producer_type=producer_type,
            reviews=reviews,
        )

    def collect_and_evaluate(
        self,
        *,
        tenant_id: str,
        actor_scope: Mapping[str, str],
        run_id: str,
        source: PaperSourceRecord,
        document: ResearchDocument,
        evidence_pack: ResearchEvidencePack,
        objective_revision: str,
        producer_id: str,
        table_id: str | None = None,
        producer_type: ObservationProducer = ObservationProducer.PARSER,
        reviews: tuple[ReviewObservation, ...] = (),
        rule_version: str = "research-numeric-comparison-rule-1",
        strategy_version: str = "research-numeric-comparison-strategy-1",
    ) -> tuple[NumericComparisonEligibilityBundle, ClaimEligibilityDecision]:
        bundle = self.collect_bundle(
            tenant_id=tenant_id,
            actor_scope=actor_scope,
            run_id=run_id,
            source=source,
            document=document,
            evidence_pack=evidence_pack,
            objective_revision=objective_revision,
            producer_id=producer_id,
            table_id=table_id,
            producer_type=producer_type,
            reviews=reviews,
            rule_version=rule_version,
            strategy_version=strategy_version,
        )
        return bundle, self.evaluate(bundle)

    def prepare_bundle(
        self,
        *,
        binding: ReviewBinding,
        table: ResearchTable,
        producer_id: str,
        producer_type: ObservationProducer = ObservationProducer.PARSER,
        reviews: tuple[ReviewObservation, ...] = (),
    ) -> NumericComparisonEligibilityBundle:
        contracts = trusted_numeric_comparison_contracts(table)
        matching = [
            contract
            for contract in contracts
            if numeric_comparison_claim_id(
                paper_id=binding.paper_id,
                table_id=table.table_id,
                contract=contract,
            ) == binding.claim.claim_id
        ]
        if len(matching) != 1:
            raise ValueError(
                "review binding must identify exactly one trusted table comparison"
            )
        contract = matching[0]
        if binding.claim.text != render_numeric_comparison_claim_text(contract):
            raise ValueError("claim text does not match the trusted table comparison")
        locator_text = numeric_comparison_table_locator(
            table_id=table.table_id,
            label_column=contract.conditions["label_column"],
            subject_label=contract.subject_label,
            comparator_label=contract.comparator_label,
            value_column=contract.conditions["value_column"],
        )
        locators = [
            locator for locator in binding.evidence_locators
            if locator.locator == locator_text
        ]
        if len(locators) != 1:
            raise ValueError(
                "review binding must contain exactly one trusted table cell locator"
            )
        locator = locators[0]
        observation = NumericComparisonObservation(
            observation_id=(
                f"{producer_type.value}:{producer_id}:"
                f"{binding.claim.claim_id}:r{binding.claim.revision}"
            ),
            claim_checksum=binding.claim.checksum,
            source_locator_checksum=locator.checksum,
            producer_id=producer_id,
            producer_type=producer_type,
            metric_id=contract.metric_id,
            subject_label=contract.subject_label,
            comparator_label=contract.comparator_label,
            subject_value=contract.subject_value,
            comparator_value=contract.comparator_value,
            unit=contract.unit,
            comparator=contract.comparator,
            difference_value=contract.difference_value,
            difference_unit=contract.difference_unit,
            conditions=contract.conditions,
        )
        return NumericComparisonEligibilityBundle(
            binding=binding,
            claim_contract=contract,
            observations=(observation,),
            reviews=reviews,
        )

    def evaluate(
        self,
        bundle: NumericComparisonEligibilityBundle,
    ) -> ClaimEligibilityDecision:
        return self._resolver.evaluate(bundle)

    def prepare_and_evaluate(
        self,
        *,
        binding: ReviewBinding,
        table: ResearchTable,
        producer_id: str,
        producer_type: ObservationProducer = ObservationProducer.PARSER,
        reviews: tuple[ReviewObservation, ...] = (),
    ) -> tuple[NumericComparisonEligibilityBundle, ClaimEligibilityDecision]:
        bundle = self.prepare_bundle(
            binding=binding,
            table=table,
            producer_id=producer_id,
            producer_type=producer_type,
            reviews=reviews,
        )
        return bundle, self.evaluate(bundle)


def _validate_collection_scope(
    *,
    tenant_id: str,
    actor_scope: Mapping[str, str],
    source: PaperSourceRecord,
    document: ResearchDocument,
    evidence_pack: ResearchEvidencePack,
) -> None:
    if tenant_id is None or any(value is None for value in actor_scope.values()):
        raise ValueError("claim collection requires a canonical non-null actor scope")
    normalized_tenant = str(tenant_id).strip()
    normalized_scope = {
        str(key): str(value).strip()
        for key, value in actor_scope.items()
        if str(value).strip()
    }
    if not normalized_tenant or normalized_scope.get("tenant_id") != normalized_tenant:
        raise ValueError("claim collection requires one canonical tenant actor scope")
    for owner, actual_scope in (
        ("paper source", source.actor_scope),
        ("document", document.actor_scope),
        ("evidence pack", evidence_pack.actor_scope),
    ):
        mismatched = {
            key: value
            for key, value in actual_scope.items()
            if normalized_scope.get(key) != value
        }
        mismatched.update({
            key: value
            for key, value in normalized_scope.items()
            if key != "tenant_id" and actual_scope.get(key) != value
        })
        if mismatched:
            raise ValueError(f"accepted {owner} actor scope does not match claim collection")


__all__ = [
    "ClaimEvidenceEligibilityService",
    "RESEARCH_CLAIM_ELIGIBILITY_GATE_REF",
    "RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF",
    "numeric_comparison_claim_id",
    "numeric_comparison_table_locator",
    "render_numeric_comparison_claim_text",
    "trusted_claim_evidence_binding_values",
    "trusted_numeric_comparison_contracts",
]
