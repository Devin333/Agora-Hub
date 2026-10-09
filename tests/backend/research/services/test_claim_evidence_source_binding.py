from __future__ import annotations

import pytest

from backend.research.domain import ResearchTable
from backend.research.services.claim_evidence_eligibility import (
    trusted_numeric_comparison_contracts,
)


def _metadata_table(*, dataset: str = "atlas-v1", scale: str = "fraction") -> ResearchTable:
    return ResearchTable(
        table_id="table-results",
        caption="Accuracy benchmark",
        source_ref="paper://atlas-paper/table-results",
        columns=["Method", "Accuracy", "Unit", "Scale", "Dataset", "Split"],
        rows=[
            {
                "Method": "atlas",
                "Accuracy": 0.92,
                "Unit": "percent",
                "Scale": "fraction",
                "Dataset": "atlas-v1",
                "Split": "test",
            },
            {
                "Method": "baseline",
                "Accuracy": 0.83,
                "Unit": "percent",
                "Scale": "fraction",
                "Dataset": "atlas-v1",
                "Split": "test",
            },
        ],
        metadata={
            "numeric_comparisons": [
                {
                    "metric_id": "accuracy",
                    "unit": "percent",
                    "difference_unit": "percentage_point",
                    "scale": scale,
                    "label_column": "Method",
                    "value_column": "Accuracy",
                    "subject_label": "atlas",
                    "comparator_label": "baseline",
                    "comparator": "greater_than",
                    "dataset": dataset,
                    "split": "test",
                }
            ]
        },
    )


def test_metadata_contract_is_derived_from_real_unit_scale_and_condition_cells() -> None:
    contract = trusted_numeric_comparison_contracts(_metadata_table())[0]

    assert contract.subject_value == "92"
    assert contract.comparator_value == "83"
    assert contract.conditions == {
        "dataset": "atlas-v1",
        "split": "test",
        "table_id": "table-results",
        "label_column": "Method",
        "value_column": "Accuracy",
        "source_scale": "fraction",
    }


def test_metadata_cannot_relabel_real_rows_to_another_dataset_or_scale() -> None:
    for table, expected in (
        (_metadata_table(dataset="other-dataset"), "Dataset"),
        (_metadata_table(scale="percent"), "source scale"),
    ):
        try:
            trusted_numeric_comparison_contracts(table)
        except ValueError as exc:
            assert expected in str(exc)
        else:  # pragma: no cover - explicit source-binding assertion
            raise AssertionError("unverified metadata source binding was accepted")


def test_metadata_conditions_cannot_override_reserved_bindings() -> None:
    table = _metadata_table()
    metadata = dict(table.metadata)
    comparison = dict(metadata["numeric_comparisons"][0])
    comparison["conditions"] = {"dataset": "forged-dataset"}
    metadata["numeric_comparisons"] = [comparison]
    forged = table.model_copy(update={"metadata": metadata})

    try:
        trusted_numeric_comparison_contracts(forged)
    except ValueError as exc:
        assert "cannot override reserved bindings" in str(exc)
    else:  # pragma: no cover - explicit reserved-key assertion
        raise AssertionError("reserved table condition override was accepted")


def _percent_header_table(header: str = "Accuracy (%)") -> ResearchTable:
    return ResearchTable(
        table_id="real-results",
        caption="Accuracy",
        source_ref="paper://atlas-paper/table-results",
        columns=["Method", header, "Dataset", "Split"],
        rows=[
            {"Method": "atlas", header: "92%", "Dataset": "atlas-v1", "Split": "test"},
            {"Method": "baseline", header: "83%", "Dataset": "atlas-v1", "Split": "test"},
        ],
    )


@pytest.mark.parametrize("header", ["Accuracy (%)", "Accuracy(%)", "accuracy (%)"])
def test_real_percent_header_spacing_variants_preserve_cell_binding(header: str) -> None:
    contract = trusted_numeric_comparison_contracts(_percent_header_table(header))[0]

    assert contract.metric_id == "accuracy"
    assert contract.subject_value == "92"
    assert contract.conditions["value_column"] == header


@pytest.mark.parametrize("column,value", [("Unit", "ms"), ("Scale", "fraction")])
def test_percent_header_cannot_override_conflicting_real_unit_or_scale(column: str, value: str) -> None:
    table = _percent_header_table()
    table = table.model_copy(update={
        "columns": [*table.columns, column],
        "rows": [{**row, column: value} for row in table.rows],
    })

    with pytest.raises(ValueError, match="conflicts"):
        trusted_numeric_comparison_contracts(table)


@pytest.mark.parametrize("raw,expected", [(1, "1"), (0.92, "0.92"), ("0.92%", "0.92"), ("100%", "100")])
def test_percent_header_does_not_guess_fraction_scale(raw: object, expected: str) -> None:
    table = _percent_header_table()
    rows = [dict(row) for row in table.rows]
    rows[0]["Accuracy (%)"] = raw
    contract = trusted_numeric_comparison_contracts(table.model_copy(update={"rows": rows}))[0]

    assert contract.subject_value == expected
    assert contract.conditions["source_scale"] == "percent"


@pytest.mark.parametrize("raw", ["NaN", "inf", "-inf", "101%", "-1%", True])
def test_percent_header_rejects_nonfinite_out_of_range_and_boolean_cells(raw: object) -> None:
    table = _percent_header_table()
    rows = [dict(row) for row in table.rows]
    rows[0]["Accuracy (%)"] = raw

    with pytest.raises(ValueError):
        trusted_numeric_comparison_contracts(table.model_copy(update={"rows": rows}))


def test_declared_percent_column_with_missing_value_cell_is_explicitly_rejected() -> None:
    table = _percent_header_table()
    rows = [dict(row) for row in table.rows]
    del rows[0]["Accuracy (%)"]

    with pytest.raises(ValueError, match="value column"):
        trusted_numeric_comparison_contracts(table.model_copy(update={"rows": rows}))


def test_multiple_accuracy_percent_headers_are_ambiguous() -> None:
    table = _percent_header_table()
    table = table.model_copy(update={"columns": [*table.columns, "Accuracy(%)"]})

    with pytest.raises(ValueError, match="ambiguous"):
        trusted_numeric_comparison_contracts(table)


def test_explicit_selector_cannot_compare_the_same_method_to_itself() -> None:
    table = _metadata_table()
    metadata = dict(table.metadata)
    selector = {**metadata["numeric_comparisons"][0], "comparator_label": "atlas", "comparator": "equal"}
    metadata["numeric_comparisons"] = [selector]

    with pytest.raises(ValueError, match="distinct method"):
        trusted_numeric_comparison_contracts(table.model_copy(update={"metadata": metadata}))
