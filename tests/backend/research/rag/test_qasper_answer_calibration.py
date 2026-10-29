from __future__ import annotations

import pytest

from backend.research.rag.evaluation.qasper_answer_calibration import (
    CalibrationLabelError,
    build_calibration_cases,
    render_calibration_sheet,
    render_label_template,
    score_calibration,
    select_calibration_cases,
)


def _case(case_id: str, stratum: str, correct: bool, *, outcome: str = "answered", error: str = "") -> dict:
    return {
        "case_id": case_id,
        "stratum": stratum,
        "question": f"question {case_id}?",
        "references": ["ref"],
        "answer": "answer [1] and [2]",
        "outcome": outcome,
        "context_chunk_ids": ["c1", "c2", "c3"],
        "judge": {
            "correct": correct,
            "citations": [{"index": 1, "supports": True}, {"index": 2, "supports": False}],
            "rationale": "because",
            "error": error,
        },
    }


def _pool() -> list[dict]:
    cases = []
    for stratum in ("extractive", "boolean"):
        for verdict in (True, False):
            cases += [_case(f"{stratum}-{verdict}-{i}", stratum, verdict) for i in range(5)]
    cases.append(_case("abstained", "extractive", True, outcome="abstained"))
    cases.append(_case("judge-failed", "extractive", True, error="judge_call_failed"))
    return cases


def test_selection_balances_cells_and_skips_unjudged_cases() -> None:
    selected = select_calibration_cases(_pool(), size=8, seed="s")

    ids = [case["case_id"] for case in selected]
    assert "abstained" not in ids and "judge-failed" not in ids
    cells = {(case["stratum"], case["judge"]["correct"]) for case in selected}
    assert len(cells) == 4
    assert all(sum((c["stratum"], c["judge"]["correct"]) == cell for c in selected) == 2 for cell in cells)


def test_selection_is_deterministic_and_bounded_by_the_pool() -> None:
    assert select_calibration_cases(_pool(), size=6, seed="s") == select_calibration_cases(_pool(), size=6, seed="s")
    assert len(select_calibration_cases(_pool(), size=500, seed="s")) == 20


def test_cases_carry_cited_passage_text_by_marker_index() -> None:
    built = build_calibration_cases([_case("a", "extractive", True)], passage_text=lambda cid: f"text of {cid}")

    passages = built[0].passages
    assert [(p.index, p.chunk_id, p.text, p.judge_supports) for p in passages] == [
        (1, "c1", "text of c1", True), (2, "c2", "text of c2", False),
    ]
    sheet = render_calibration_sheet(built, run_directory="run")
    assert "text of c2" in sheet and "question a?" in sheet


def test_out_of_context_citation_is_rejected() -> None:
    case = _case("a", "extractive", True)
    case["judge"]["citations"].append({"index": 9, "supports": True})
    with pytest.raises(ValueError, match="outside its context"):
        build_calibration_cases([case], passage_text=str)


def _manifest_cases() -> list[dict]:
    selected = [_case(f"k{i}", "extractive", i % 2 == 0) for i in range(4)]
    return [case.to_dict() for case in build_calibration_cases(selected, passage_text=str)]


def test_scoring_reports_agreement_kappa_and_citations() -> None:
    cases = _manifest_cases()
    template = render_label_template(build_calibration_cases(
        [_case(f"k{i}", "extractive", i % 2 == 0) for i in range(4)], passage_text=str,
    ))
    rows = template.splitlines()
    # judge: k0 yes, k1 no, k2 yes, k3 no. Human disagrees on k2; k3 left unlabeled.
    filled = "\n".join([
        rows[0],
        rows[1].replace("yes,,,", "yes,yes,,"),
        rows[2].replace("no,,,", "no,no,2;1,"),
        rows[3].replace("yes,,,", "yes,no,,judge too lenient"),
        rows[4],
    ])

    result = score_calibration(cases, filled)

    assert result["labeled"] == 3
    assert result["unlabeled"] == ["k3"]
    assert result["correctness_agreement"]["value"] == pytest.approx(2 / 3)
    assert result["judge_false_accept"] == {"numerator": 1, "denominator": 2, "value": 0.5}
    assert result["disagreements"] == [
        {"case_id": "k2", "judge_correct": True, "human_correct": False, "notes": "judge too lenient"},
    ]
    # Judge: [1] supports, [2] not. k0/k2 human leaves blank -> [2] disagrees; k1 marks both.
    assert result["citation_agreement"] == {"numerator": 3, "denominator": 6, "value": 0.5}
    assert result["cohen_kappa"] is not None


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("1,unknown,yes,yes,,", "unknown case"),
        ("1,k0,yes,maybe,,", "yes or no"),
        ("1,k0,yes,yes,7,", "does not have"),
    ],
)
def test_invalid_labels_are_rejected_with_case_diagnostics(row: str, message: str) -> None:
    header = "case_no,case_id,judge_correct,human_correct,human_unsupported_citations,notes"
    with pytest.raises(CalibrationLabelError, match=message):
        score_calibration(_manifest_cases(), f"{header}\n{row}\n")
