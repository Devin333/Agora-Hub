from __future__ import annotations

from backend.research.domain.common import SourceLineage, stable_research_id
from backend.research.domain.document import ResearchDocument
from backend.research.domain.evidence import ResearchEvidenceItem, ResearchEvidencePack
from backend.research.services.claim_evidence_eligibility import (
    numeric_comparison_claim_id,
    numeric_comparison_table_locator,
    trusted_numeric_comparison_contracts,
)


class ResearchEvidenceBuilder:
    def build_from_document(
        self,
        *,
        pack_id: str | None = None,
        document: ResearchDocument,
        include_numeric_comparison_tables: bool = False,
    ) -> ResearchEvidencePack:
        items = [
            ResearchEvidenceItem(
                evidence_id=stable_research_id("evidence", document.paper_id, section.section_id),
                paper_id=document.paper_id,
                title=section.title,
                summary=section.text[:300],
                evidence_type="section",
                source_ref=section.source_ref,
                span_refs=[section.section_id],
                confidence=1.0,
                lineage=SourceLineage(source_refs=[section.source_ref], source_hash=document.source_hash),
            )
            for section in document.sections
            # A heading with no body of its own ("6 Results" above "6.1 ...")
            # structures the document but is not evidence.
            if section.text.strip()
        ]
        tables = document.tables if include_numeric_comparison_tables else ()
        for table in tables:
            for contract in trusted_numeric_comparison_contracts(table):
                locator = numeric_comparison_table_locator(
                    table_id=table.table_id,
                    label_column=contract.conditions["label_column"],
                    subject_label=contract.subject_label,
                    comparator_label=contract.comparator_label,
                    value_column=contract.conditions["value_column"],
                )
                claim_id = numeric_comparison_claim_id(
                    paper_id=document.paper_id,
                    table_id=table.table_id,
                    contract=contract,
                )
                items.append(ResearchEvidenceItem(
                    evidence_id=stable_research_id(
                        "evidence",
                        document.paper_id,
                        table.table_id,
                        contract.checksum,
                    ),
                    paper_id=document.paper_id,
                    title=table.caption,
                    summary=(
                        f"{contract.subject_label}={contract.subject_value} {contract.unit}; "
                        f"{contract.comparator_label}={contract.comparator_value} {contract.unit}; "
                        f"{contract.metric_id}; {dict(contract.conditions)}"
                    ),
                    evidence_type="numeric_comparison_table",
                    source_ref=table.source_ref,
                    span_refs=[locator],
                    claim_refs=[claim_id],
                    confidence=1.0,
                    lineage=SourceLineage(
                        source_refs=[table.source_ref],
                        source_hash=document.source_hash,
                    ),
                    metadata={
                        "claim_type": "numeric_comparison_result_v1",
                        "claim_contract": contract.to_dict(),
                    },
                ))
        source_refs = sorted(
            {item.source_ref for item in items} or set(document.lineage.source_refs)
        )
        coverage = {"sections": 1.0 if document.sections else 0.0}
        missing_information = [] if document.sections else ["sections"]
        if include_numeric_comparison_tables:
            has_numeric_comparison = any(
                item.evidence_type == "numeric_comparison_table" for item in items
            )
            coverage["numeric_comparison_tables"] = (
                1.0 if has_numeric_comparison else 0.0
            )
            missing_information = [
                *([] if document.sections else ["sections"]),
                *([] if has_numeric_comparison else ["numeric_comparison_tables"]),
            ]
        return ResearchEvidencePack(
            pack_id=pack_id or stable_research_id("evidence_pack", document.paper_id, document.source_hash),
            paper_id=document.paper_id,
            items=items,
            coverage=coverage,
            missing_information=missing_information,
            lineage=SourceLineage(source_refs=source_refs, source_hash=document.source_hash),
            actor_scope=(
                dict(document.actor_scope)
                if include_numeric_comparison_tables
                else {}
            ),
        )


__all__ = ["ResearchEvidenceBuilder"]
