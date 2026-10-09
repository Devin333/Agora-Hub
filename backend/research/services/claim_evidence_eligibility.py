from __future__ import annotations

"""Research-owned deterministic eligibility for V1 numeric comparison claims."""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from backend.research.domain.claim_review import (
    CheckExecutionStatus,
    CheckObservation,
    CheckResult,
    ClaimConclusionStatus,
    ClaimEligibilityDecision,
    EvidenceAvailability,
    EvidenceRequirement,
    NumericComparator,
    NumericComparisonClaim,
    NumericComparisonEligibilityBundle,
    NumericComparisonObservation,
    RequirementStatus,
    ResearchProgressObservation,
    V1_NUMERIC_COMPARISON_CLAIM_TYPE,
)
from framework.events.canonical import checksum_for

from backend.research.domain.common import stable_research_id
from backend.research.domain.document import ResearchDocument, ResearchTable
from backend.research.domain.evidence import ResearchClaim, ResearchEvidencePack
from backend.research.domain.paper import PaperSourceRecord
from backend.research.services.claim_review_resolver import ClaimReviewResolver


RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF = "ResearchNumericComparisonResolver@1"
RESEARCH_CLAIM_ELIGIBILITY_GATE_REF = "ResearchClaimEvidenceEligibilityGate@1"
NUMERIC_COMPARISON_TABLE_METADATA_KEY = "numeric_comparisons"


def trusted_claim_evidence_binding_values(
    source: PaperSourceRecord,
    document: ResearchDocument,
    evidence_pack: ResearchEvidencePack,
) -> dict[str, str]:
    """Derive replay-stable binding values from accepted Graph outputs."""

    if source.paper_id != document.paper_id or source.paper_id != evidence_pack.paper_id:
        raise ValueError("accepted source, document, and evidence pack must share one paper")
    source_hash = str(source.source_hash or "").strip()
    if not source_hash:
        raise ValueError("accepted paper source requires a content hash")
    if document.source_hash != source_hash:
        raise ValueError("accepted document does not match the paper source hash")
    if evidence_pack.lineage.source_hash != document.source_hash:
        raise ValueError("accepted evidence pack does not match the document source hash")
    if (
        document.lineage.source_hash is not None
        and document.lineage.source_hash != source_hash
    ):
        raise ValueError("accepted document lineage does not match the paper source hash")
    declared_revisions = {
        str(metadata.get("source_revision") or "").strip()
        for metadata in (
            source.metadata, document.metadata, document.lineage.metadata,
            evidence_pack.metadata, evidence_pack.lineage.metadata,
        )
        if str(metadata.get("source_revision") or "").strip()
    }
    if len(declared_revisions) > 1:
        raise ValueError("accepted artifacts declare conflicting source revisions")
    declared_snapshots = {
        str(value).strip()
        for value in (
            document.source_snapshot_id,
            source.metadata.get("source_snapshot_id"),
            document.lineage.metadata.get("source_snapshot_id"),
        )
        if value is not None and str(value).strip()
    }
    if len(declared_snapshots) > 1:
        raise ValueError("accepted artifacts declare conflicting source snapshots")
    revision = next(iter(declared_revisions), str(
        document.source_snapshot_id or document.source_hash
    ).strip())
    if not revision:
        raise ValueError("accepted Research evidence requires a source revision")
    return {
        "paper_snapshot_checksum": checksum_for(source.model_dump(mode="json")),
        "evidence_revision": revision,
        "evidence_checksum": checksum_for({
            "document": document.model_dump(mode="json"),
            "evidence_pack": evidence_pack.model_dump(mode="json"),
        }),
    }


def source_ref_matches_record(source_ref: str, source: PaperSourceRecord) -> bool:
    metadata_refs = {
        str(source.metadata.get(key) or "").strip()
        for key in ("source_ref", "pdf_url")
    }
    prefixes = {
        source.source_url,
        f"paper://{source.paper_id}",
        f"{source.source_type}://{source.paper_id}",
        f"source://{source.source_id}",
        *metadata_refs,
    }
    return any(
        source_ref == prefix.rstrip("/")
        or source_ref.startswith((f"{prefix.rstrip('/')}/", f"{prefix.rstrip('/')}#", f"{prefix.rstrip('/')}?"))
        for prefix in prefixes
        if prefix.rstrip("/")
    )


def numeric_comparison_table_locator(
    *,
    table_id: str,
    label_column: str,
    subject_label: str,
    comparator_label: str,
    value_column: str,
) -> str:
    return (
        f"table:{table_id};label_column:{label_column};subject:{subject_label};"
        f"comparator:{comparator_label};value_column:{value_column}"
    )


def numeric_comparison_claim_id(
    *,
    paper_id: str,
    table_id: str,
    contract: NumericComparisonClaim,
) -> str:
    return stable_research_id(
        "claim",
        paper_id,
        table_id,
        contract.checksum,
    )


def trusted_numeric_comparison_contracts(
    table: ResearchTable,
) -> tuple[NumericComparisonClaim, ...]:
    """Derive V1 contracts from real table cells and source-declared units."""

    raw_specs = table.metadata.get(NUMERIC_COMPARISON_TABLE_METADATA_KEY, ())
    if raw_specs in (None, ()):
        derived = _derive_v1_accuracy_percent_contract(table)
        return (derived,) if derived is not None else ()
    if not isinstance(raw_specs, list):
        raise ValueError(f"table metadata {NUMERIC_COMPARISON_TABLE_METADATA_KEY} must be a list")
    contracts: list[NumericComparisonClaim] = []
    for raw in raw_specs:
        if not isinstance(raw, Mapping):
            raise ValueError("numeric comparison table metadata entries must be objects")
        metric_id = _required_metadata_text(raw, "metric_id")
        unit = _required_metadata_text(raw, "unit")
        if unit.casefold() != "percent":
            raise ValueError("V1 numeric comparison table metadata unit must be percent")
        difference_unit = _required_metadata_text(raw, "difference_unit")
        source_scale = _required_metadata_text(raw, "scale").casefold()
        if source_scale not in {"fraction", "percent"}:
            raise ValueError("V1 percent comparison scale must be fraction or percent")
        if difference_unit != "percentage_point":
            raise ValueError("V1 percent comparisons require percentage_point differences")
        label_column = _required_metadata_text(raw, "label_column")
        value_column = _required_metadata_text(raw, "value_column")
        subject_label = _required_metadata_text(raw, "subject_label")
        comparator_label = _required_metadata_text(raw, "comparator_label")
        if subject_label.casefold() == comparator_label.casefold():
            raise ValueError("V1 comparison requires two distinct method labels")
        comparator = NumericComparator(_required_metadata_text(raw, "comparator"))
        dataset = _required_metadata_text(raw, "dataset")
        split = _required_metadata_text(raw, "split")
        extra_conditions = raw.get("conditions", {})
        if not isinstance(extra_conditions, Mapping):
            raise ValueError("numeric comparison conditions must be an object")
        normalized_extra_conditions = {
            str(key).strip(): str(value).strip()
            for key, value in extra_conditions.items()
        }
        reserved_conditions = {
            "dataset",
            "split",
            "table_id",
            "label_column",
            "value_column",
            "source_scale",
        }
        overridden = {
            key
            for key in normalized_extra_conditions
            if key.casefold() in reserved_conditions
        }
        if overridden:
            raise ValueError(
                "numeric comparison conditions cannot override reserved bindings: "
                + ", ".join(sorted(overridden))
            )
        if any(not key or not value for key, value in normalized_extra_conditions.items()):
            raise ValueError("numeric comparison conditions cannot be empty")
        subject_row = _unique_table_row(
            table,
            label_column=label_column,
            label=subject_label,
        )
        comparator_row = _unique_table_row(
            table,
            label_column=label_column,
            label=comparator_label,
        )
        _validate_percent_source_declaration(
            table,
            rows=(subject_row, comparator_row),
            metric_id=metric_id,
            value_column=value_column,
            source_scale=source_scale,
        )
        _validate_condition_cells(
            table,
            rows=(subject_row, comparator_row),
            expected={
                "Dataset": dataset,
                "Split": split,
                **normalized_extra_conditions,
            },
        )
        conditions = {
            "dataset": dataset,
            "split": split,
            "table_id": table.table_id,
            "label_column": label_column,
            "value_column": value_column,
            "source_scale": source_scale,
            **normalized_extra_conditions,
        }
        subject_value = _table_percent_value(
            subject_row,
            value_column=value_column,
            source_scale=source_scale,
        )
        comparator_value = _table_percent_value(
            comparator_row,
            value_column=value_column,
            source_scale=source_scale,
        )
        difference = subject_value - comparator_value
        contracts.append(NumericComparisonClaim(
            metric_id=metric_id,
            subject_label=subject_label,
            comparator_label=comparator_label,
            subject_value=_decimal_contract_text(subject_value),
            comparator_value=_decimal_contract_text(comparator_value),
            unit="percent",
            comparator=comparator,
            difference_value=_decimal_contract_text(difference),
            difference_unit="percentage_point",
            conditions=conditions,
        ))
    return tuple(contracts)


def _unique_table_row(
    table: ResearchTable,
    *,
    label_column: str,
    label: str,
) -> Mapping[str, Any]:
    if label_column not in table.columns:
        raise ValueError(f"table label column {label_column} is missing")
    rows = [row for row in table.rows if str(row.get(label_column) or "").strip() == label]
    if len(rows) != 1:
        raise ValueError(f"table row for {label} is missing or ambiguous")
    return rows[0]


def _table_percent_value(
    row: Mapping[str, Any],
    *,
    value_column: str,
    source_scale: str,
) -> Decimal:
    if value_column not in row:
        raise ValueError(f"table value column {value_column} is missing")
    return _strict_percent_value(row[value_column], source_scale=source_scale)


def _validate_percent_source_declaration(
    table: ResearchTable,
    *,
    rows: tuple[Mapping[str, Any], Mapping[str, Any]],
    metric_id: str,
    value_column: str,
    source_scale: str,
) -> None:
    if value_column not in table.columns:
        raise ValueError(f"table value column {value_column} is missing")
    column_metric = _metric_id_from_value_column(value_column)
    if metric_id.casefold() != "accuracy" or column_metric != "accuracy":
        raise ValueError("numeric comparison metric_id does not match the value column")
    if "%" in value_column:
        if value_column.replace(" ", "").casefold() != "accuracy(%)":
            raise ValueError("V1 percent value column must declare Accuracy (%)")
        if source_scale != "percent":
            raise ValueError("percent value column requires percent source scale")
        for row in rows:
            if "Unit" in table.columns or "Unit" in row:
                if str(row.get("Unit") or "").strip().casefold() != "percent":
                    raise ValueError("percent value column conflicts with Unit cells")
            if "Scale" in table.columns or "Scale" in row:
                if str(row.get("Scale") or "").strip().casefold() != "percent":
                    raise ValueError("percent value column conflicts with Scale cells")
        return
    if value_column.casefold() != "accuracy":
        raise ValueError("V1 accuracy value column has an unsupported unit declaration")
    if not {"Unit", "Scale"}.issubset(table.columns):
        raise ValueError(
            "percent comparison requires a percent value column or Unit/Scale cells"
        )
    unit_values = {
        str(row.get("Unit") or "").strip().casefold()
        for row in rows
    }
    scale_values = {
        str(row.get("Scale") or "").strip().casefold()
        for row in rows
    }
    if unit_values != {"percent"}:
        raise ValueError("V1 numeric comparison requires Unit=percent in both rows")
    if scale_values != {source_scale}:
        raise ValueError("numeric comparison source scale does not match both table rows")


def _metric_id_from_value_column(value_column: str) -> str:
    base = value_column.split("(", 1)[0].strip().casefold()
    normalized = "_".join(part for part in base.replace("-", " ").split() if part)
    if not normalized:
        raise ValueError("numeric comparison value column has no metric identity")
    return normalized


def _validate_condition_cells(
    table: ResearchTable,
    *,
    rows: tuple[Mapping[str, Any], Mapping[str, Any]],
    expected: Mapping[str, str],
) -> None:
    for column, expected_value in expected.items():
        if column not in table.columns:
            raise ValueError(f"numeric comparison condition column {column} is missing")
        actual_values = {
            str(row.get(column) or "").strip()
            for row in rows
        }
        if actual_values != {expected_value}:
            raise ValueError(
                f"numeric comparison condition {column} does not match both table rows"
            )


class NumericComparisonEligibilityResolver:
    """Resolve one source-bound numeric comparison without candidate authority."""

    def __init__(self, resolver: ClaimReviewResolver | None = None) -> None:
        self._resolver = resolver or ClaimReviewResolver()

    def evaluate(
        self,
        bundle: NumericComparisonEligibilityBundle,
    ) -> ClaimEligibilityDecision:
        if not isinstance(bundle, NumericComparisonEligibilityBundle):
            raise TypeError("bundle must be a NumericComparisonEligibilityBundle")
        binding = bundle.binding
        claim = binding.claim
        if claim.claim_type != V1_NUMERIC_COMPARISON_CLAIM_TYPE:
            raise ValueError(
                f"claim_type must be {V1_NUMERIC_COMPARISON_CLAIM_TYPE}"
            )
        if claim.text != render_numeric_comparison_claim_text(bundle.claim_contract):
            raise ValueError("claim text does not match the typed numeric comparison contract")
        if not binding.evidence_locators:
            raise ValueError("V1 numeric comparison requires source locators")
        if not binding.applicable_conditions:
            raise ValueError("V1 numeric comparison requires applicable conditions")
        if dict(binding.applicable_conditions) != dict(bundle.claim_contract.conditions):
            raise ValueError("claim contract conditions do not match the review binding")

        locators_by_checksum = {
            locator.checksum: locator for locator in binding.evidence_locators
        }
        available_locators = tuple(
            locator
            for locator in binding.evidence_locators
            if locator.availability is EvidenceAvailability.AVAILABLE
        )
        source_unavailable = not available_locators
        source_binding_mismatch = any(
            locator.source_revision != binding.evidence_revision
            or locator.source_checksum != binding.evidence_checksum
            for locator in available_locators
        )

        observations = _deduplicate_observations(bundle.observations)
        bound_observations: list[NumericComparisonObservation] = []
        binding_failures: list[str] = []
        observation_failures: list[str] = []
        for observation in observations:
            if observation.claim_checksum != claim.checksum:
                binding_failures.append("observation_claim_revision_mismatch")
                continue
            if observation.source_locator_checksum not in locators_by_checksum:
                binding_failures.append("observation_source_locator_mismatch")
                continue
            if locators_by_checksum[observation.source_locator_checksum].availability is not EvidenceAvailability.AVAILABLE:
                binding_failures.append("observation_source_unavailable")
                continue
            if dict(observation.conditions) != dict(bundle.claim_contract.conditions):
                binding_failures.append("observation_condition_mismatch")
                continue
            bound_observations.append(observation)
            if not _observation_matches_contract(observation, bundle.claim_contract):
                observation_failures.append("typed_observation_contract_mismatch")

        evidence_refs = tuple(locator.checksum for locator in binding.evidence_locators)
        requirement_status = (
            RequirementStatus.PRESENT
            if bound_observations and not source_unavailable and not binding_failures
            else RequirementStatus.MISSING
        )
        requirements = (
            EvidenceRequirement(
                requirement_id="v1_numeric_comparison_observation",
                requirement_type=V1_NUMERIC_COMPARISON_CLAIM_TYPE,
                status=requirement_status,
                evidence_refs=evidence_refs if requirement_status is RequirementStatus.PRESENT else (),
            ),
        )
        if source_unavailable:
            check = CheckObservation(
                check_id="v1_numeric_comparison_source_binding",
                binding_checksum=binding.checksum,
                checker_id=RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF,
                checker_version="1",
                execution_status=CheckExecutionStatus.UNAVAILABLE,
                reason_codes=("source_unavailable",),
                evidence_refs=evidence_refs,
                input_checksum=bundle.claim_contract.checksum,
            )
        else:
            failures = tuple(dict.fromkeys(
                (["source_revision_or_checksum_mismatch"] if source_binding_mismatch else [])
                + observation_failures
                + ([] if _contract_comparator_holds(bundle.claim_contract) else ["claim_comparator_not_satisfied"])
            ))
            check = CheckObservation(
                check_id="v1_numeric_comparison_source_binding",
                binding_checksum=binding.checksum,
                checker_id=RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF,
                checker_version="1",
                execution_status=CheckExecutionStatus.COMPLETED,
                result=CheckResult.FAIL if failures else CheckResult.PASS,
                reason_codes=failures,
                evidence_refs=evidence_refs,
                input_checksum=bundle.claim_contract.checksum,
            )

        resolution = self._resolver.resolve(
            binding,
            requirements=requirements,
            checks=(check,),
            observations=bundle.reviews,
        )
        eligible = resolution.status is ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE
        reason_codes = tuple(dict.fromkeys((*binding_failures, *check.reason_codes, *resolution.reasons)))
        progress = () if eligible else (
            ResearchProgressObservation.insufficient_verified_progress(
                run_id=binding.run_id,
                paper_id=binding.paper_id,
                claim_id=claim.claim_id,
                resolver_status=resolution.status,
                evidence_context_refs={
                    "binding": binding.checksum,
                    "resolution": resolution.checksum,
                    "claim_contract": bundle.claim_contract.checksum,
                    "source_context": checksum_for({
                        "source_locator_checksums": evidence_refs,
                        "typed_observation_checksums": tuple(
                            item.checksum for item in observations
                        ),
                    }),
                },
            ),
        )
        return ClaimEligibilityDecision(
            binding_checksum=binding.checksum,
            claim_id=claim.claim_id,
            claim_revision=claim.revision,
            claim_contract_checksum=bundle.claim_contract.checksum,
            resolution=resolution,
            resolver_ref=RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF,
            gate_ref=RESEARCH_CLAIM_ELIGIBILITY_GATE_REF,
            source_locator_checksums=evidence_refs,
            typed_observation_checksums=tuple(item.checksum for item in observations),
            eligible=eligible,
            reason_codes=reason_codes,
            progress_observations=progress,
        )


def _derive_v1_accuracy_percent_contract(
    table: ResearchTable,
) -> NumericComparisonClaim | None:
    common_columns = {"Method", "Dataset", "Split"}
    if not common_columns.issubset(table.columns) or len(table.rows) != 2:
        return None
    percent_columns = [
        column for column in table.columns
        if column.replace(" ", "").casefold() == "accuracy(%)"
    ]
    if len(percent_columns) > 1:
        raise ValueError("V1 accuracy comparison has ambiguous percent value columns")
    if percent_columns:
        value_column = percent_columns[0]
        source_scale = "percent"
    elif {"Accuracy", "Unit", "Scale"}.issubset(table.columns):
        unit_values = {
            str(row.get("Unit") or "").strip().casefold()
            for row in table.rows
        }
        scale_values = {
            str(row.get("Scale") or "").strip().casefold()
            for row in table.rows
        }
        if unit_values != {"percent"}:
            raise ValueError("V1 accuracy comparison requires Unit=percent")
        if len(scale_values) != 1 or not scale_values.issubset({"fraction", "percent"}):
            raise ValueError(
                "V1 accuracy comparison requires one Scale of fraction or percent"
            )
        value_column = "Accuracy"
        source_scale = next(iter(scale_values))
    else:
        return None
    baseline_rows = [
        row for row in table.rows
        if str(row.get("Method") or "").strip().casefold() == "baseline"
    ]
    subject_rows = [
        row for row in table.rows
        if str(row.get("Method") or "").strip().casefold() != "baseline"
    ]
    if len(baseline_rows) != 1 or len(subject_rows) != 1:
        return None
    baseline_row = baseline_rows[0]
    subject_row = subject_rows[0]
    subject_label = str(subject_row.get("Method") or "").strip()
    if not subject_label:
        raise ValueError("V1 accuracy comparison requires a subject Method")
    dataset_values = {
        str(row.get("Dataset") or "").strip() for row in table.rows
    }
    split_values = {
        str(row.get("Split") or "").strip() for row in table.rows
    }
    if len(dataset_values) != 1 or "" in dataset_values:
        raise ValueError("V1 accuracy comparison requires one shared Dataset")
    if len(split_values) != 1 or "" in split_values:
        raise ValueError("V1 accuracy comparison requires one shared Split")
    _validate_percent_source_declaration(
        table,
        rows=(subject_row, baseline_row),
        metric_id="accuracy",
        value_column=value_column,
        source_scale=source_scale,
    )
    subject_value = _table_percent_value(
        subject_row,
        value_column=value_column,
        source_scale=source_scale,
    )
    comparator_value = _table_percent_value(
        baseline_row,
        value_column=value_column,
        source_scale=source_scale,
    )
    return NumericComparisonClaim(
        metric_id="accuracy",
        subject_label=subject_label,
        comparator_label=str(baseline_row["Method"]).strip(),
        subject_value=_decimal_contract_text(subject_value),
        comparator_value=_decimal_contract_text(comparator_value),
        unit="percent",
        comparator=NumericComparator.GREATER_THAN,
        difference_value=_decimal_contract_text(subject_value - comparator_value),
        difference_unit="percentage_point",
        conditions={
            "dataset": next(iter(dataset_values)),
            "split": next(iter(split_values)),
            "table_id": table.table_id,
            "label_column": "Method",
            "value_column": value_column,
            "source_scale": source_scale,
        },
    )


def _required_metadata_text(raw: Mapping[str, Any], key: str) -> str:
    value = str(raw.get(key) or "").strip()
    if not value:
        raise ValueError(f"numeric comparison table metadata requires {key}")
    return value


def _strict_percent_value(raw: Any, *, source_scale: str) -> Decimal:
    text = str(raw).strip()
    has_percent_suffix = text.endswith("%")
    numeric_text = text[:-1].strip() if has_percent_suffix else text
    try:
        value = Decimal(numeric_text)
    except InvalidOperation as exc:
        raise ValueError("percent table cell must be numeric") from exc
    if not value.is_finite():
        raise ValueError("percent table cell must be finite")
    if has_percent_suffix:
        if source_scale != "percent":
            raise ValueError("percent suffix conflicts with fraction source scale")
        if value < 0 or value > 100:
            raise ValueError("percent-scale values must be between 0 and 100")
        return value
    if source_scale == "fraction":
        if value < 0 or value > 1:
            raise ValueError(
                "fraction-scale percent values must be between 0 and 1"
            )
        return value * 100
    if source_scale == "percent":
        if value < 0 or value > 100:
            raise ValueError(
                "percent-scale values must be between 0 and 100"
            )
        return value
    raise ValueError("percent source scale must be fraction or percent")


def _decimal_contract_text(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def render_numeric_comparison_claim_text(contract: NumericComparisonClaim) -> str:
    conditions = ", ".join(
        f"{key}={value}" for key, value in sorted(contract.conditions.items())
    )
    return (
        f"{contract.subject_label} reports {contract.subject_value} {contract.unit} "
        f"{contract.metric_id} compared with {contract.comparator_label} at "
        f"{contract.comparator_value} {contract.unit} under {conditions}; "
        f"difference {contract.difference_value} {contract.difference_unit}; "
        f"comparator {contract.comparator.value}."
    )


def build_qualified_claim_projection(
    bundle: NumericComparisonEligibilityBundle,
    decision: ClaimEligibilityDecision,
    evidence_pack: ResearchEvidencePack,
) -> dict[str, Any]:
    """Project the only publishable ResearchClaim shape for an eligible V1 bundle."""

    claim = bundle.binding.claim
    replayed = NumericComparisonEligibilityResolver().evaluate(bundle)
    if (
        not decision.eligible
        or decision != replayed
        or decision.claim_id != claim.claim_id
        or decision.claim_revision != claim.revision
        or decision.binding_checksum != bundle.binding.checksum
        or decision.claim_contract_checksum != bundle.claim_contract.checksum
    ):
        raise ValueError("qualified claim projection requires one matching eligible decision")
    evidence_items = tuple(
        item for item in evidence_pack.items
        if item.paper_id == bundle.binding.paper_id
        and item.evidence_type == "numeric_comparison_table"
        and claim.claim_id in item.claim_refs
        and item.metadata.get("claim_type") == V1_NUMERIC_COMPARISON_CLAIM_TYPE
        and item.metadata.get("claim_contract") == bundle.claim_contract.to_dict()
        and item.lineage.source_hash == evidence_pack.lineage.source_hash
        and item.source_ref in item.lineage.source_refs
        and any(
            locator.availability is EvidenceAvailability.AVAILABLE
            and locator.locator in item.span_refs
            for locator in bundle.binding.evidence_locators
        )
    )
    if len(evidence_items) != 1:
        raise ValueError("qualified claim projection requires exactly one accepted typed table evidence item")
    projected = ResearchClaim(
        claim_id=claim.claim_id,
        text=claim.text,
        claim_type="experiment",
        evidence_ids=[item.evidence_id for item in evidence_items],
        confidence=1.0,
        metadata={
            "conclusion_status": "verified_conclusion",
            "claim_revision": claim.revision,
            "objective_revision": claim.objective_revision,
            "qualification_ref": decision.qualification_ref,
            "eligibility_gate_ref": decision.gate_ref,
            "eligibility_resolver_ref": decision.resolver_ref,
            "eligibility_decision_ref": decision.checksum,
            "review_binding_ref": decision.binding_checksum,
            "source_locator_checksums": list(decision.source_locator_checksums),
            "source_refs": sorted({item.source_ref for item in evidence_items}),
            "evidence_revision": bundle.binding.evidence_revision,
            "evidence_checksum": bundle.binding.evidence_checksum,
        },
    )
    return projected.to_dict()


def _deduplicate_observations(
    observations: tuple[NumericComparisonObservation, ...],
) -> tuple[NumericComparisonObservation, ...]:
    by_identity: dict[tuple[object, ...], NumericComparisonObservation] = {}
    by_id: dict[str, NumericComparisonObservation] = {}
    for observation in sorted(observations, key=lambda item: (item.observation_id, item.checksum)):
        previous = by_id.get(observation.observation_id)
        if previous is not None and previous != observation:
            raise ValueError(
                f"conflicting duplicate observation_id: {observation.observation_id}"
            )
        by_id[observation.observation_id] = observation
        semantic_identity = (
            observation.claim_checksum,
            observation.source_locator_checksum,
            observation.metric_id,
            observation.subject_label,
            observation.comparator_label,
            observation.subject_value,
            observation.comparator_value,
            observation.unit,
            observation.comparator,
            observation.difference_value,
            observation.difference_unit,
            tuple(sorted(observation.conditions.items())),
        )
        by_identity.setdefault(semantic_identity, observation)
    return tuple(by_identity.values())


def _observation_matches_contract(
    observation: NumericComparisonObservation,
    contract: NumericComparisonClaim,
) -> bool:
    return (
        observation.metric_id == contract.metric_id
        and observation.subject_label == contract.subject_label
        and observation.comparator_label == contract.comparator_label
        and observation.subject_value == contract.subject_value
        and observation.comparator_value == contract.comparator_value
        and observation.unit == contract.unit
        and observation.comparator is contract.comparator
        and observation.difference_value == contract.difference_value
        and observation.difference_unit == contract.difference_unit
        and dict(observation.conditions) == dict(contract.conditions)
    )


def _contract_comparator_holds(contract: NumericComparisonClaim) -> bool:
    subject = Decimal(contract.subject_value)
    comparator = Decimal(contract.comparator_value)
    operations = {
        NumericComparator.GREATER_THAN: subject > comparator,
        NumericComparator.GREATER_THAN_OR_EQUAL: subject >= comparator,
        NumericComparator.LESS_THAN: subject < comparator,
        NumericComparator.LESS_THAN_OR_EQUAL: subject <= comparator,
        NumericComparator.EQUAL: subject == comparator,
    }
    return operations[contract.comparator]


__all__ = [
    "NumericComparisonEligibilityResolver",
    "NUMERIC_COMPARISON_TABLE_METADATA_KEY",
    "RESEARCH_CLAIM_ELIGIBILITY_GATE_REF",
    "RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF",
    "build_qualified_claim_projection",
    "source_ref_matches_record",
    "numeric_comparison_claim_id",
    "numeric_comparison_table_locator",
    "render_numeric_comparison_claim_text",
    "trusted_claim_evidence_binding_values",
    "trusted_numeric_comparison_contracts",
]
