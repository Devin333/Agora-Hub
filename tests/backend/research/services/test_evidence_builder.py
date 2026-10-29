from __future__ import annotations

from backend.research.services import ResearchEvidenceBuilder
from tests.backend.research.helpers import sample_document


def test_evidence_builder_records_section_lineage() -> None:
    evidence_pack = ResearchEvidenceBuilder().build_from_document(document=sample_document())

    assert evidence_pack.items[0].source_ref == "paper://paper-1/sec-intro"
    assert evidence_pack.lineage.source_refs == ["paper://paper-1/sec-intro"]
    assert evidence_pack.coverage["sections"] == 1.0


def test_body_less_parent_heading_is_structure_not_evidence() -> None:
    from backend.research.domain import ResearchSection

    document = sample_document()
    parent = ResearchSection(
        section_id="sec-results",
        title="3 Results",
        level=1,
        text="",
        source_ref="paper://paper-1/sec-results",
    )
    child = ResearchSection(
        section_id="sec-translation",
        title="3.1 Machine Translation",
        level=2,
        text="The model reaches 28.4 BLEU on the translation benchmark.",
        source_ref="paper://paper-1/sec-translation",
    )
    document = document.model_copy(update={"sections": [*document.sections, parent, child]})

    pack = ResearchEvidenceBuilder().build_from_document(document=document)

    titles = {item.title for item in pack.items}
    assert "3 Results" not in titles
    assert "3.1 Machine Translation" in titles
    assert all(item.summary for item in pack.items)


def test_evidence_builder_materializes_typed_numeric_table_binding() -> None:
    from backend.research.domain import ResearchTable
    from backend.research.services.claim_evidence_eligibility import (
        numeric_comparison_claim_id,
        trusted_numeric_comparison_contracts,
    )

    document = sample_document().model_copy(update={
        "tables": [
            ResearchTable(
                table_id="table-results",
                caption="Accuracy benchmark",
                source_ref="paper://paper-1/table-results",
                columns=["Method", "Accuracy (%)", "Dataset", "Split"],
                rows=[
                    {
                        "Method": "u-net",
                        "Accuracy (%)": "92%",
                        "Dataset": "segmentation-v1",
                        "Split": "test",
                    },
                    {
                        "Method": "baseline",
                        "Accuracy (%)": "83%",
                        "Dataset": "segmentation-v1",
                        "Split": "test",
                    },
                ],
            )
        ]
    })

    disabled_pack = ResearchEvidenceBuilder().build_from_document(document=document)
    assert all(
        item.evidence_type != "numeric_comparison_table"
        for item in disabled_pack.items
    )
    assert disabled_pack.coverage == {"sections": 1.0}

    evidence_pack = ResearchEvidenceBuilder().build_from_document(
        document=document,
        include_numeric_comparison_tables=True,
    )
    table_item = next(
        item for item in evidence_pack.items
        if item.evidence_type == "numeric_comparison_table"
    )
    contract = trusted_numeric_comparison_contracts(document.tables[0])[0]

    assert table_item.claim_refs == [
        numeric_comparison_claim_id(
            paper_id=document.paper_id,
            table_id="table-results",
            contract=contract,
        )
    ]
    assert table_item.span_refs == [
        "table:table-results;label_column:Method;subject:u-net;comparator:baseline;value_column:Accuracy (%)"
    ]
    assert table_item.metadata["claim_contract"]["conditions"] == {
        "dataset": "segmentation-v1",
        "split": "test",
        "table_id": "table-results",
        "label_column": "Method",
        "value_column": "Accuracy (%)",
        "source_scale": "percent",
    }
    assert evidence_pack.coverage["numeric_comparison_tables"] == 1.0


def test_typed_numeric_table_metadata_rejects_ambiguous_percent_units() -> None:
    from backend.research.domain import ResearchTable
    from backend.research.services.claim_evidence_eligibility import (
        trusted_numeric_comparison_contracts,
    )

    table = ResearchTable(
        table_id="table-results",
        caption="Accuracy benchmark",
        source_ref="paper://paper-1/table-results",
        columns=["Method", "Accuracy", "Unit", "Scale", "Dataset", "Split"],
        rows=[
            {
                "Method": "u-net",
                "Accuracy": 92,
                "Unit": "percent",
                "Scale": "fraction",
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
            {
                "Method": "baseline",
                "Accuracy": 83,
                "Unit": "percent",
                "Scale": "fraction",
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
        ],
    )

    try:
        trusted_numeric_comparison_contracts(table)
    except ValueError as exc:
        assert "fraction-scale percent" in str(exc)
    else:  # pragma: no cover - explicit unit boundary assertion
        raise AssertionError("ambiguous percent values were accepted")



def test_percent_header_does_not_guess_fraction_scale_from_magnitude() -> None:
    from backend.research.domain import ResearchTable
    from backend.research.services.claim_evidence_eligibility import (
        trusted_numeric_comparison_contracts,
    )

    table = ResearchTable(
        table_id="table-results",
        caption="Accuracy benchmark",
        source_ref="paper://paper-1/table-results",
        columns=["Method", "Accuracy (%)", "Dataset", "Split"],
        rows=[
            {
                "Method": "u-net",
                "Accuracy (%)": 0.92,
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
            {
                "Method": "baseline",
                "Accuracy (%)": 0.83,
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
        ],
    )

    contract = trusted_numeric_comparison_contracts(table)[0]

    assert contract.subject_value == "0.92"
    assert contract.comparator_value == "0.83"
    assert contract.conditions["source_scale"] == "percent"


def test_explicit_fraction_scale_converts_fraction_values_to_percent() -> None:
    from backend.research.domain import ResearchTable
    from backend.research.services.claim_evidence_eligibility import (
        trusted_numeric_comparison_contracts,
    )

    table = ResearchTable(
        table_id="table-results",
        caption="Accuracy benchmark",
        source_ref="paper://paper-1/table-results",
        columns=["Method", "Accuracy", "Unit", "Scale", "Dataset", "Split"],
        rows=[
            {
                "Method": "u-net",
                "Accuracy": 0.92,
                "Unit": "percent",
                "Scale": "fraction",
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
            {
                "Method": "baseline",
                "Accuracy": 0.83,
                "Unit": "percent",
                "Scale": "fraction",
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
        ],
    )

    contract = trusted_numeric_comparison_contracts(table)[0]

    assert contract.subject_value == "92"
    assert contract.comparator_value == "83"
    assert contract.conditions["source_scale"] == "fraction"


def test_numeric_comparison_rejects_non_finite_and_condition_drift() -> None:
    from backend.research.domain import ResearchTable
    from backend.research.services.claim_evidence_eligibility import (
        trusted_numeric_comparison_contracts,
    )

    for invalid_value in ("NaN", "inf", "-inf"):
        table = ResearchTable(
            table_id="table-results",
            caption="Accuracy benchmark",
            source_ref="paper://paper-1/table-results",
            columns=["Method", "Accuracy (%)", "Dataset", "Split"],
            rows=[
                {
                    "Method": "u-net",
                    "Accuracy (%)": invalid_value,
                    "Dataset": "segmentation-v1",
                    "Split": "test",
                },
                {
                    "Method": "baseline",
                    "Accuracy (%)": "83%",
                    "Dataset": "segmentation-v1",
                    "Split": "test",
                },
            ],
        )
        try:
            trusted_numeric_comparison_contracts(table)
        except ValueError as exc:
            assert "finite" in str(exc) or "between" in str(exc)
        else:  # pragma: no cover - explicit finite-number assertion
            raise AssertionError(f"non-finite value {invalid_value} was accepted")

    drifted = ResearchTable(
        table_id="table-results",
        caption="Accuracy benchmark",
        source_ref="paper://paper-1/table-results",
        columns=["Method", "Accuracy (%)", "Dataset", "Split"],
        rows=[
            {
                "Method": "u-net",
                "Accuracy (%)": "92%",
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
            {
                "Method": "baseline",
                "Accuracy (%)": "83%",
                "Dataset": "segmentation-v2",
                "Split": "test",
            },
        ],
    )
    try:
        trusted_numeric_comparison_contracts(drifted)
    except ValueError as exc:
        assert "shared Dataset" in str(exc)
    else:  # pragma: no cover - explicit condition-boundary assertion
        raise AssertionError("cross-dataset comparison was accepted")
