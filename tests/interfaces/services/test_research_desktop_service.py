from __future__ import annotations

from interfaces.services.research_desktop_service import (
    _ask_selection,
    _normalized_upload_filename,
    _report_markdown,
    _report_projection,
)


def test_report_projection_is_exact_run_bound_and_does_not_expose_private_locators() -> None:
    private_ref = "file:///C:/Users/alice/private.pdf#page=3"
    public_ref = "https://example.test/paper.pdf#page=3"
    run = {
        "runId": "run-1",
        "paperId": "2401.01234",
        "status": "succeeded",
        "disposition": "accepted",
        "accepted": True,
        "qualityPassed": True,
        "eligibleForAcceptedExport": True,
        "artifactStatus": "valid",
        "artifactReason": None,
        "analysis": {
            "paper_id": "2401.01234",
            "summary": {
                "problem": "Grounded paper analysis is required.",
                "core_idea": "A source-grounded method.",
                "key_contributions": ["Bounded evidence retrieval."],
                "method_summary": "Retrieve evidence before generation.",
                "experiment_summary": "The accepted run passed its deterministic gates.",
                "limitations": ["One accepted document."],
                "why_it_matters": "The answer can be audited.",
                "read_next": ["Methods"],
                "evidence_refs": [
                    {"evidence_id": "ev-private", "source_ref": private_ref},
                    {"evidence_id": "ev-public", "source_ref": public_ref, "section_id": "methods"},
                ],
                "confidence": 0.9,
                "quality_flags": [{
                    "flag_type": "bounded", "severity": "low",
                    "message": "Evidence scope is bounded.",
                    "evidence_refs": ["ev-public"],
                }],
            },
            "contributions": ["Bounded evidence retrieval."],
            "methods": ["Retrieve evidence before generation."],
            "experiments": ["Accepted deterministic gate result."],
            "limitations": ["One accepted document."],
            "reproducibility": ["Use the exact source revision."],
            "related_work": [],
            "claims": [{
                "claim_id": "claim-1", "text": "Evidence is retrieved before generation.",
                "claim_type": "method", "section_id": "methods",
                "evidence_ids": ["ev-public"], "confidence": 0.9,
            }],
            "evidence_pack_id": "pack-1",
            "quality": {"evidence_item_count": 2, "llm_candidate_warnings": []},
            "metadata": {"workspace": "C:\\private\\workspace"},
        },
        "quality": {
            "result_id": "quality-1", "target_id": "2401.01234",
            "target_type": "summary", "passed": True, "score": 0.9,
            "gate_results": [], "quality_flags": [],
            "metadata": {"path": private_ref},
        },
        "reader": {
            "document": {
                "sections": [
                    {"section_id": "methods", "source_ref": public_ref, "page_start": 3, "page_end": 3},
                    {"section_id": "private-methods", "source_ref": private_ref, "page_start": 3, "page_end": 3},
                ]
            },
            "evidence": {
                "items": [
                    {"evidence_id": "ev-public", "source_ref": public_ref, "span_refs": ["source://paper?section=methods&page=3"]},
                    {"evidence_id": "ev-private", "source_ref": private_ref, "span_refs": ["source://paper?section=private-methods&page=3"]},
                ]
            },
        },
    }

    report = _report_projection(run, source_revision="sha256:" + "a" * 64)
    encoded = str(report)

    assert report["runId"] == "run-1"
    assert report["eligibility"]["acceptedExport"] is True
    assert private_ref not in encoded
    assert "C:\\private" not in encoded
    valid = [item for item in report["citations"] if item["valid"]]
    assert len(valid) == 2
    assert {item["sectionId"] for item in valid} == {"methods", "private-methods"}
    assert all("url" not in item for item in valid if item["sectionId"] == "private-methods")
    assert report["artifactRefs"] == [] and report["traceRef"] is None
    assert report["analysis"]["summary"]["evidenceIds"] == ["ev-private", "ev-public"]
    assert report["analysis"]["summary"]["quality_flags"][0]["evidenceIds"] == ["ev-public"]
    assert report["analysis"]["quality"] == {
        "evidence_item_count": 2, "llm_candidate_warnings": [],
    }
    assert "evidence_refs" not in str(report["analysis"])


def test_markdown_export_uses_typed_analysis_and_quality_projection() -> None:
    report = {
        "runId": "run-1",
        "paperId": "paper-1",
        "sourceRevision": "rev-1",
        "disposition": "accepted",
        "analysis": {
            "summary": {"core_idea": "Core verified idea."},
            "methods": ["Bounded retrieval."],
            "limitations": ["Only evaluated on one corpus."],
        },
        "quality": {"passed": True, "gate_results": []},
        "diagnostics": {"title": "Verified report"},
        "citations": [],
    }

    markdown = _report_markdown(report)

    assert "# Verified report" in markdown
    assert "Core verified idea." in markdown
    assert "## Methods" in markdown and "Bounded retrieval." in markdown
    assert "## Limitations" in markdown and "one corpus" in markdown
    assert "Quality passed: `true`" in markdown


def test_upload_filename_and_selection_contracts_are_bounded_and_stable() -> None:
    assert _normalized_upload_filename("%E8%AE%BA%E6%96%87%2520literal.pdf") == "论文%20literal.pdf"
    assert _ask_selection({"sectionId": "methods", "page": 3}) == {"sectionId": "methods", "page": 3}


def test_report_projection_rejects_empty_accepted_source_and_wrong_section() -> None:
    import pytest
    from interfaces.services.research_desktop_service import ResearchDesktopServiceError

    base = {
        "runId": "run-1", "paperId": "paper-1", "status": "succeeded",
        "disposition": "accepted", "accepted": True, "qualityPassed": True,
        "eligibleForAcceptedExport": True, "artifactStatus": "valid",
        "analysis": {}, "quality": {"passed": True}, "reader": {},
    }
    with pytest.raises(ResearchDesktopServiceError) as empty:
        _report_projection(base, source_revision="sha256:" + "a" * 64)
    assert empty.value.code == "report_projection_invalid"

    source_ref = "file:///private/paper.pdf"
    run = {
        **base,
        "analysis": {
            "paper_id": "paper-1",
            "summary": {
                "problem": "P", "core_idea": "C",
                "key_contributions": [], "method_summary": "M",
                "experiment_summary": "E", "limitations": [],
                "why_it_matters": "W", "read_next": [],
                "evidence_refs": [{
                    "evidence_id": "ev-1", "source_ref": source_ref,
                    "section_id": "invented-section",
                }],
                "confidence": 1.0, "quality_flags": [],
            },
            "contributions": [], "methods": [], "experiments": [],
            "limitations": [], "reproducibility": [], "related_work": [],
            "claims": [], "evidence_pack_id": "pack-1", "quality": {},
        },
        "quality": {
            "result_id": "quality-1", "target_id": "paper-1",
            "target_type": "summary", "passed": True, "score": 1.0,
            "gate_results": [], "quality_flags": [],
        },
        "reader": {
            "document": {"sections": [{
                "section_id": "methods", "source_ref": source_ref,
                "page_start": 2, "page_end": 2,
            }]},
            "evidence": {"items": [{
                "evidence_id": "ev-1", "source_ref": source_ref,
                "span_refs": ["section=invented-section"],
            }]},
        },
    }
    report = _report_projection(run, source_revision="sha256:" + "a" * 64)
    assert report["citations"][0]["valid"] is False
    assert report["citations"][0]["reasonCode"] == "span_locator_not_verified"
