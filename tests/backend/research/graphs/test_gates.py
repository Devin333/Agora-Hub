from __future__ import annotations

from types import SimpleNamespace

import pytest

from framework.harness import DeterministicGate

from backend.research.graphs.gates import (
    PAPER_ANALYSIS_AVAILABLE_GATE_REFERENCES,
    PAPER_ANALYSIS_GATE_REFERENCES,
    PAPER_ANALYSIS_QUALITY_GATE_REFERENCES,
    build_paper_analysis_gate_registry,
)


def test_paper_analysis_registry_resolves_every_exact_reference() -> None:
    registry = build_paper_analysis_gate_registry()

    bindings = [
        registry.resolve(reference)
        for reference in PAPER_ANALYSIS_AVAILABLE_GATE_REFERENCES
    ]

    assert [str(binding.reference) for binding in bindings] == list(
        PAPER_ANALYSIS_AVAILABLE_GATE_REFERENCES
    )
    assert all(isinstance(binding.gate, DeterministicGate) for binding in bindings)
    assert len({type(binding.gate) for binding in bindings}) == len(
        PAPER_ANALYSIS_AVAILABLE_GATE_REFERENCES
    )


def test_default_and_quality_gate_inventories_are_explicit() -> None:
    eligibility_ref = "ResearchClaimEvidenceEligibilityGate@1"

    assert eligibility_ref not in PAPER_ANALYSIS_GATE_REFERENCES
    assert PAPER_ANALYSIS_QUALITY_GATE_REFERENCES == (
        *PAPER_ANALYSIS_GATE_REFERENCES,
        eligibility_ref,
    )
    assert (
        PAPER_ANALYSIS_AVAILABLE_GATE_REFERENCES
        == PAPER_ANALYSIS_QUALITY_GATE_REFERENCES
    )


@pytest.mark.parametrize(
    "reference",
    PAPER_ANALYSIS_AVAILABLE_GATE_REFERENCES,
)
def test_paper_analysis_gate_fails_closed_without_current_worker_result(reference: str) -> None:
    gate = build_paper_analysis_gate_registry().resolve(reference).gate

    result = gate.evaluate(SimpleNamespace(worker_result=None))

    assert result.gate_name == reference.removesuffix("@1")
    assert result.passed is False
    assert result.details["reason_code"] == "research_gate_input_invalid"
