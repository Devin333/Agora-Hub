from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.research.rag.evaluation.document_parsing_protocol import (
    load_parsing_evaluation_manifest,
    summarize_parsing_evaluation_manifest,
)


DEFAULT_ACCEPTANCE_THRESHOLDS: dict[str, float] = {
    "min_requested_papers": 20.0,
    "min_parse_success_rate": 0.95,
    "min_penalized_quality_score": 0.65,
    "min_element_source_locator_coverage": 0.80,
    "min_bbox_coverage": 0.50,
    "min_rag_hit_at_10": 0.55,
    "min_rag_evidence_coverage_at_5": 0.45,
    "min_rag_source_locator_coverage_at_5": 0.80,
}


@dataclass(frozen=True)
class ParserArtifactInput:
    name: str
    papers_dir: Path
    rag_report_path: Path | None = None
    ingest_manifest_path: Path | None = None
    parsing_manifest_path: Path | None = None


@dataclass(frozen=True)
class ParserBakeoffReportConfig:
    inputs: tuple[ParserArtifactInput, ...]
    output_json: Path
    output_markdown: Path
    acceptance_thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_ACCEPTANCE_THRESHOLDS))


@dataclass(frozen=True)
class ParserBakeoffReport:
    parser_count: int
    paper_ids: tuple[str, ...]
    parsers: dict[str, dict[str, Any]] = field(default_factory=dict)
    recommendations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parser_count": self.parser_count,
            "paper_ids": list(self.paper_ids),
            "parsers": self.parsers,
            "recommendations": self.recommendations,
        }


def build_parser_bakeoff_report(config: ParserBakeoffReportConfig) -> ParserBakeoffReport:
    parser_payloads: dict[str, dict[str, Any]] = {}
    all_paper_ids: set[str] = set()
    for parser_input in config.inputs:
        documents = _load_documents(parser_input.papers_dir)
        ingest_manifest = _load_ingest_manifest(parser_input.ingest_manifest_path)
        scoped_documents = _scope_documents(documents, ingest_manifest)
        all_paper_ids.update(_manifest_paper_ids(ingest_manifest))
        all_paper_ids.update(_artifact_paper_id(doc) for doc in scoped_documents if _artifact_paper_id(doc))
        parser_payloads[parser_input.name] = _parser_summary(
            parser_input.name,
            parser_input.papers_dir,
            scoped_documents,
            rag_report_path=parser_input.rag_report_path,
            ingest_manifest_path=parser_input.ingest_manifest_path,
            ingest_manifest=ingest_manifest,
            parsing_manifest_path=parser_input.parsing_manifest_path,
        )
    report = ParserBakeoffReport(
        parser_count=len(parser_payloads),
        paper_ids=tuple(sorted(all_paper_ids)),
        parsers=parser_payloads,
        recommendations=_recommend(parser_payloads, config.acceptance_thresholds),
    )
    _write_report(report, config.output_json, config.output_markdown)
    return report


def _load_documents(papers_dir: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted(papers_dir.rglob("research_document.json")):
        artifact_paper_id = path.parent.name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            documents.append({
                "paper_id": artifact_paper_id,
                "_artifact_paper_id": artifact_paper_id,
                "_reported_paper_id": None,
                "_load_error": f"{type(exc).__name__}: {exc}",
                "_path": str(path),
            })
            continue
        if isinstance(payload, dict):
            reported_paper_id = str(payload.get("paper_id") or "")
            payload["_artifact_paper_id"] = artifact_paper_id
            payload["_reported_paper_id"] = reported_paper_id
            if reported_paper_id and reported_paper_id != artifact_paper_id:
                payload["_paper_id_mismatch"] = {
                    "artifact_paper_id": artifact_paper_id,
                    "reported_paper_id": reported_paper_id,
                }
            payload["_path"] = str(path)
            documents.append(payload)
    return documents


def _load_ingest_manifest(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        return {"_manifest_status": "missing", "_manifest_path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "_manifest_status": "load_error",
            "_manifest_path": str(path),
            "_manifest_error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "_manifest_status": "load_error",
            "_manifest_path": str(path),
            "_manifest_error": "manifest root is not an object",
        }
    payload["_manifest_status"] = "loaded"
    payload["_manifest_path"] = str(path)
    return payload


def _scope_documents(
    documents: list[dict[str, Any]],
    ingest_manifest: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    requested_ids = set(_manifest_paper_ids(ingest_manifest))
    if not requested_ids:
        return documents
    return [doc for doc in documents if _artifact_paper_id(doc) in requested_ids]


def _manifest_paper_ids(ingest_manifest: dict[str, Any] | None) -> list[str]:
    if not ingest_manifest or ingest_manifest.get("_manifest_status") != "loaded":
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for item in ingest_manifest.get("items") or []:
        if not isinstance(item, dict):
            continue
        paper_id = str(item.get("paper_id") or item.get("arxiv_id") or "").strip()
        if paper_id and paper_id not in seen:
            seen.add(paper_id)
            ids.append(paper_id)
    return ids


def _parser_summary(
    name: str,
    papers_dir: Path,
    documents: list[dict[str, Any]],
    *,
    rag_report_path: Path | None,
    ingest_manifest_path: Path | None,
    ingest_manifest: dict[str, Any] | None,
    parsing_manifest_path: Path | None,
) -> dict[str, Any]:
    succeeded = [doc for doc in documents if not doc.get("_load_error")]
    failed = [doc for doc in documents if doc.get("_load_error")]
    manifest_summary = _manifest_summary(ingest_manifest, succeeded)
    parser_metrics = _parser_metrics(succeeded, failed, manifest_summary)
    rag_metrics = _rag_metrics(rag_report_path)
    requested_ids = _manifest_paper_ids(ingest_manifest)
    paper_ids = requested_ids or [_artifact_paper_id(doc) for doc in documents if _artifact_paper_id(doc)]
    summary = {
        "name": name,
        "papers_dir": str(papers_dir),
        "paper_count": manifest_summary.get("requested") or len(documents),
        "artifact_document_count": len(documents),
        "paper_ids": paper_ids,
        "parser_metrics": parser_metrics,
        "rag_metrics": rag_metrics,
        "ingest_manifest": manifest_summary,
        "ingest_manifest_path": str(ingest_manifest_path) if ingest_manifest_path else None,
        "load_errors": failed,
        "failed_items": manifest_summary.get("failed_items") or [],
        "paper_id_mismatches": [
            doc["_paper_id_mismatch"]
            for doc in documents
            if isinstance(doc.get("_paper_id_mismatch"), dict)
        ],
        "parsing_protocol": _parsing_protocol_summary(parsing_manifest_path),
    }
    summary["penalized_metrics"] = _penalized_metrics(summary)
    return summary


def _parsing_protocol_summary(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "status": "not_evaluated",
            "manifest_path": None,
            "reason": "no parsing evaluation manifest was provided",
            "quality_gate": {"evaluated": False, "passed": None},
        }
    try:
        manifest = load_parsing_evaluation_manifest(path)
    except (OSError, UnicodeError, ValueError):
        return {
            "status": "invalid",
            "manifest_path": str(path),
            "reason": "parsing evaluation manifest is unavailable or invalid",
            "quality_gate": {"evaluated": False, "passed": None},
        }

    summary = summarize_parsing_evaluation_manifest(manifest)
    return {
        "status": "reported",
        "manifest_path": str(path),
        "fingerprints": manifest.fingerprints.model_dump(mode="json"),
        **summary,
    }


def _manifest_summary(
    ingest_manifest: dict[str, Any] | None,
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    if ingest_manifest is None:
        return {"status": "not_provided"}
    status = str(ingest_manifest.get("_manifest_status") or "unknown")
    if status != "loaded":
        return {
            "status": status,
            "path": ingest_manifest.get("_manifest_path"),
            "reason": ingest_manifest.get("_manifest_error"),
        }
    items = [item for item in (ingest_manifest.get("items") or []) if isinstance(item, dict)]
    failed_items = [item for item in items if item.get("status") == "failed"]
    successful_status_items = [
        item for item in items if item.get("status") in {"succeeded", "skipped"}
    ]
    loaded_ids = {_artifact_paper_id(doc) for doc in documents if _artifact_paper_id(doc)}
    missing_successful_items = [
        item for item in successful_status_items
        if str(item.get("paper_id") or item.get("arxiv_id") or "") not in loaded_ids
    ]
    return {
        "status": "loaded",
        "path": ingest_manifest.get("_manifest_path"),
        "backend": ingest_manifest.get("backend"),
        "papers_dir": ingest_manifest.get("papers_dir"),
        "requested": _int_or_zero(ingest_manifest.get("requested")) or len(items),
        "succeeded": _int_or_zero(ingest_manifest.get("succeeded")),
        "skipped": _int_or_zero(ingest_manifest.get("skipped")),
        "failed": _int_or_zero(ingest_manifest.get("failed")),
        "loaded_success_count": len(documents),
        "failed_items": failed_items,
        "missing_successful_items": missing_successful_items,
    }


def _parser_metrics(
    documents: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    manifest_summary: dict[str, Any],
) -> dict[str, Any]:
    requested_count = _metric_denominator(documents, failed, manifest_summary)
    parse_success_count = len(documents)
    parse_success_rate = _safe_div(parse_success_count, requested_count)
    section_counts = [len(doc.get("sections") or []) for doc in documents]
    figure_counts = [len(doc.get("figures") or []) for doc in documents]
    table_counts = [len(doc.get("tables") or []) for doc in documents]
    equation_counts = [len(doc.get("equations") or []) for doc in documents]
    text_chars = [
        sum(len(str(section.get("text") or "")) for section in (doc.get("sections") or []))
        for doc in documents
    ]
    durations = [
        _float_or_none((doc.get("metadata") or {}).get("parser_duration_seconds"))
        for doc in documents
    ]
    durations = [value for value in durations if value is not None]
    warnings = [
        len((doc.get("metadata") or {}).get("parser_warnings") or [])
        for doc in documents
    ]
    return {
        "parse_success_count": parse_success_count,
        "parse_requested_count": requested_count,
        "parse_success_rate": parse_success_rate,
        "parse_duration_seconds_avg": _avg(durations),
        "section_count_avg": _avg(section_counts),
        "text_chars_avg": _avg(text_chars),
        "formula_count_avg": _avg(equation_counts),
        "table_count_avg": _avg(table_counts),
        "table_rows_coverage": _element_coverage(documents, "tables", _has_table_rows),
        "figure_count_avg": _avg(figure_counts),
        "image_ref_coverage": _element_coverage(documents, "figures", _has_image_ref),
        "caption_source_locator_coverage": _caption_source_locator_coverage(documents),
        "element_source_locator_coverage": _element_source_locator_coverage(documents),
        "bbox_coverage": _bbox_coverage(documents),
        "parser_warning_count": sum(warnings),
        "parser_warning_count_avg": _avg(warnings),
    }


def _metric_denominator(
    documents: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    manifest_summary: dict[str, Any],
) -> int:
    if manifest_summary.get("status") == "loaded":
        return _int_or_zero(manifest_summary.get("requested"))
    return len(documents) + len(failed)


def _rag_metrics(rag_report_path: Path | None) -> dict[str, Any]:
    if rag_report_path is None:
        return {"status": "not_provided"}
    if not rag_report_path.exists():
        return {"status": "missing", "path": str(rag_report_path)}
    try:
        payload = json.loads(rag_report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "load_error", "path": str(rag_report_path), "reason": f"{type(exc).__name__}: {exc}"}
    legacy_candidate_report = ((payload.get("reports") or {}).get("candidate") or {})
    candidate = (
        legacy_candidate_report.get("metrics")
        or ((payload.get("candidate_test_report") or {}).get("retrieval") or {})
    )
    by_type = legacy_candidate_report.get("by_qa_type") or candidate.get("by_qa_type") or {}
    return {
        "status": "loaded",
        "path": str(rag_report_path),
        "papers_total": payload.get("papers_total"),
        "chunks_total": payload.get("chunks_total"),
        "pairs_total": payload.get("pairs_total"),
        "reported_split": ((payload.get("evaluation_protocol") or {}).get("reported_split")),
        "hit_at_3": _metric_at_k(candidate, 3, "hit_rate"),
        "hit_at_5": _metric_at_k(candidate, 5, "hit_rate"),
        "hit_at_10": _metric_at_k(candidate, 10, "hit_rate"),
        "equivalent_hit_at_10": _metric_at_k(candidate, 10, "equivalent_hit_rate"),
        "mrr": candidate.get("mrr"),
        "ndcg_at_5": _metric_at_k(candidate, 5, "ndcg"),
        "evidence_coverage_at_5": _metric_at_k(candidate, 5, "evidence_coverage"),
        "evidence_coverage_at_10": _metric_at_k(candidate, 10, "evidence_coverage"),
        "source_locator_coverage_at_5": _metric_at_k(candidate, 5, "source_locator_coverage"),
        "source_locator_coverage_at_10": _metric_at_k(candidate, 10, "source_locator_coverage"),
        "formula_explanation_qa_mrr": _qa_metric(by_type, "formula_explanation_qa", "mrr"),
        "figure_qa_evidence_coverage_at_5": _qa_metric_at_k(by_type, "figure_qa", 5, "evidence_coverage"),
        "table_qa_evidence_coverage_at_5": _qa_metric_at_k(by_type, "table_qa", 5, "evidence_coverage"),
    }


def _metric_at_k(metrics: dict[str, Any], k: int, name: str) -> Any:
    by_k = metrics.get("by_k") or {}
    item = by_k.get(str(k)) or by_k.get(k) or {}
    if isinstance(item, dict):
        return item.get(name)
    return None


def _qa_metric(by_type: dict[str, Any], qa_type: str, name: str) -> Any:
    item = by_type.get(qa_type) or {}
    if isinstance(item, dict):
        return item.get(name)
    return None


def _qa_metric_at_k(by_type: dict[str, Any], qa_type: str, k: int, name: str) -> Any:
    item = by_type.get(qa_type) or {}
    if not isinstance(item, dict):
        return None
    return _metric_at_k(item, k, name)


def _has_table_rows(table: dict[str, Any]) -> bool:
    return bool(table.get("rows"))


def _has_image_ref(item: dict[str, Any]) -> bool:
    return bool(item.get("image_ref") or (item.get("metadata") or {}).get("image_ref"))


def _element_coverage(
    documents: list[dict[str, Any]],
    element_key: str,
    predicate: Any,
) -> float:
    total = 0
    matched = 0
    for doc in documents:
        for item in doc.get(element_key) or []:
            if not isinstance(item, dict):
                continue
            total += 1
            if predicate(item):
                matched += 1
    return _safe_div(matched, total)


def _caption_source_locator_coverage(documents: list[dict[str, Any]]) -> float:
    captioned = [
        item for item in _visual_elements(documents)
        if str(item.get("caption") or "").strip()
    ]
    with_locator = [item for item in captioned if _source_locator(item)]
    return _safe_div(len(with_locator), len(captioned))


def _element_source_locator_coverage(documents: list[dict[str, Any]]) -> float:
    elements = _all_elements(documents)
    return _safe_div(sum(1 for item in elements if _source_locator(item)), len(elements))


def _bbox_coverage(documents: list[dict[str, Any]]) -> float:
    elements = _all_elements(documents)
    return _safe_div(sum(1 for item in elements if _pdf_rect(item)), len(elements))


def _visual_elements(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc in documents:
        out.extend(item for item in (doc.get("figures") or []) if isinstance(item, dict))
        out.extend(item for item in (doc.get("tables") or []) if isinstance(item, dict))
    return out


def _all_elements(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc in documents:
        out.extend(item for item in (doc.get("figures") or []) if isinstance(item, dict))
        out.extend(item for item in (doc.get("tables") or []) if isinstance(item, dict))
        out.extend(item for item in (doc.get("equations") or []) if isinstance(item, dict))
    return out


def _source_locator(item: dict[str, Any]) -> str:
    return str((item.get("metadata") or {}).get("source_locator") or item.get("source_ref") or "")


def _pdf_rect(item: dict[str, Any]) -> Any:
    return (item.get("metadata") or {}).get("pdf_rect")


def _artifact_paper_id(document: dict[str, Any]) -> str:
    return str(document.get("_artifact_paper_id") or document.get("paper_id") or "")


def _recommend(
    parser_payloads: dict[str, dict[str, Any]],
    acceptance_thresholds: Mapping[str, float],
) -> dict[str, Any]:
    return {
        "best_overall_parser": _best_parser(parser_payloads, _overall_score),
        "best_penalized_parser": _best_parser(parser_payloads, _penalized_score),
        "best_formula_parser": _best_parser(parser_payloads, lambda item: _metric(item, "formula_count_avg") + _metric(item, "bbox_coverage")),
        "best_table_parser": _best_parser(parser_payloads, lambda item: _metric(item, "table_count_avg") + _metric(item, "table_rows_coverage")),
        "best_figure_parser": _best_parser(parser_payloads, lambda item: _metric(item, "figure_count_avg") + _metric(item, "image_ref_coverage")),
        "deployment_cost_notes": "Compare GPU time, image size, model cache size, and Docker network reliability before choosing a default.",
        "recommended_default_parser": _best_parser(parser_payloads, _penalized_score),
        "recommended_fallback_parser": _second_best_parser(parser_payloads, _penalized_score),
        "cascade_acceptance": _cascade_acceptance(parser_payloads, acceptance_thresholds),
    }


def _overall_score(item: dict[str, Any]) -> float:
    metrics = item.get("parser_metrics") or {}
    rag = item.get("rag_metrics") or {}
    parse_success_rate = float(metrics.get("parse_success_rate") or 0.0)
    quality_score = (
        parse_success_rate * 3.0
        + float(metrics.get("element_source_locator_coverage") or 0.0)
        + float(metrics.get("bbox_coverage") or 0.0)
        + float(metrics.get("table_rows_coverage") or 0.0)
        + float(metrics.get("image_ref_coverage") or 0.0)
        + float(rag.get("hit_at_10") or 0.0)
        + float(rag.get("mrr") or 0.0)
        - min(float(metrics.get("parser_warning_count_avg") or 0.0), 5.0) * 0.1
    )
    return quality_score * parse_success_rate


def _penalized_metrics(item: dict[str, Any]) -> dict[str, Any]:
    raw_quality_score = _raw_quality_score(item)
    penalty_details = _penalty_details(item)
    penalty_total = min(1.0, sum(float(detail["penalty"]) for detail in penalty_details))
    return {
        "raw_quality_score": round(raw_quality_score, 6),
        "penalty_total": round(penalty_total, 6),
        "penalized_quality_score": round(max(0.0, raw_quality_score - penalty_total), 6),
        "penalty_details": penalty_details,
    }


def _raw_quality_score(item: dict[str, Any]) -> float:
    metrics = item.get("parser_metrics") or {}
    rag = item.get("rag_metrics") or {}
    coverage_score = _avg_available([
        metrics.get("caption_source_locator_coverage"),
        metrics.get("element_source_locator_coverage"),
        metrics.get("bbox_coverage"),
        metrics.get("table_rows_coverage"),
        metrics.get("image_ref_coverage"),
    ])
    yield_score = _avg_available([
        _bounded_ratio(metrics.get("section_count_avg"), 6.0),
        _bounded_ratio(metrics.get("text_chars_avg"), 8000.0),
        _bounded_ratio(metrics.get("formula_count_avg"), 2.0),
        _bounded_ratio(metrics.get("table_count_avg"), 2.0),
        _bounded_ratio(metrics.get("figure_count_avg"), 2.0),
    ])
    rag_score = _avg_available([
        rag.get("hit_at_5"),
        rag.get("hit_at_10"),
        rag.get("mrr"),
        rag.get("evidence_coverage_at_5"),
        rag.get("source_locator_coverage_at_5"),
    ])
    if rag.get("status") == "loaded":
        return _clamp(0.45 * coverage_score + 0.25 * yield_score + 0.30 * rag_score)
    return _clamp(0.65 * coverage_score + 0.35 * yield_score)


def _penalty_details(item: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = item.get("parser_metrics") or {}
    rag = item.get("rag_metrics") or {}
    manifest = item.get("ingest_manifest") or {}
    requested = max(1, int(metrics.get("parse_requested_count") or item.get("paper_count") or 0))
    parse_failure_rate = 1.0 - float(metrics.get("parse_success_rate") or 0.0)
    missing_successful_items = manifest.get("missing_successful_items") or []
    load_errors = item.get("load_errors") or []
    warning_avg = float(metrics.get("parser_warning_count_avg") or 0.0)
    penalties = [
        _penalty(
            "ingest_failure_rate",
            "Requested papers without loaded parser artifacts",
            parse_failure_rate,
            0.45,
        ),
        _penalty(
            "missing_successful_artifact_rate",
            "Manifest-successful papers missing from artifact directory",
            _safe_div(len(missing_successful_items), requested),
            0.25,
        ),
        _penalty(
            "load_error_rate",
            "research_document.json artifacts that could not be loaded",
            _safe_div(len(load_errors), requested),
            0.25,
        ),
        _penalty(
            "parser_warning_rate",
            "Average parser warnings per loaded document",
            min(warning_avg / 5.0, 1.0),
            0.10,
        ),
        _penalty(
            "source_locator_gap",
            "Missing element source locators",
            1.0 - float(metrics.get("element_source_locator_coverage") or 0.0),
            0.15,
        ),
        _penalty(
            "bbox_gap",
            "Missing parser element bounding boxes",
            1.0 - float(metrics.get("bbox_coverage") or 0.0),
            0.10,
        ),
    ]
    if rag.get("status") != "loaded":
        penalties.append(_penalty(
            "rag_report_unavailable",
            "RAG benchmark report is not loaded for this parser",
            1.0,
            0.15,
            actual=rag.get("status"),
        ))
    return [penalty for penalty in penalties if float(penalty["penalty"]) > 0.0]


def _penalty(
    penalty_id: str,
    label: str,
    rate: float,
    weight: float,
    *,
    actual: Any | None = None,
) -> dict[str, Any]:
    bounded = _clamp(rate)
    return {
        "penalty_id": penalty_id,
        "label": label,
        "rate": round(bounded, 6),
        "weight": weight,
        "penalty": round(bounded * weight, 6),
        "actual": actual if actual is not None else round(bounded, 6),
    }


def _cascade_acceptance(
    parser_payloads: dict[str, dict[str, Any]],
    acceptance_thresholds: Mapping[str, float],
) -> dict[str, Any]:
    thresholds = dict(DEFAULT_ACCEPTANCE_THRESHOLDS)
    thresholds.update({key: float(value) for key, value in acceptance_thresholds.items()})
    cascade = parser_payloads.get("cascade")
    if cascade is None:
        checks = [_acceptance_check(
            "parser_present",
            "Parser named cascade is present",
            False,
            actual=sorted(parser_payloads),
            threshold="cascade",
        )]
        return {
            "parser": "cascade",
            "ready": False,
            "thresholds": thresholds,
            "checks": checks,
        }

    metrics = cascade.get("parser_metrics") or {}
    rag = cascade.get("rag_metrics") or {}
    manifest = cascade.get("ingest_manifest") or {}
    requested_source = "ingest_manifest" if manifest.get("status") == "loaded" else "artifact_count"
    requested_count = int(cascade.get("paper_count") or 0)
    checks = [
        _acceptance_check(
            "parser_present",
            "Parser named cascade is present",
            True,
            actual="cascade",
            threshold="cascade",
        ),
        _acceptance_check(
            "min_requested_papers",
            "Cascade bake-off requested paper count meets threshold",
            requested_count >= thresholds["min_requested_papers"],
            actual=requested_count,
            threshold=thresholds["min_requested_papers"],
            details=f"source={requested_source}",
        ),
        _metric_acceptance_check(
            "min_parse_success_rate",
            "Cascade parse success rate meets threshold",
            metrics.get("parse_success_rate"),
            thresholds["min_parse_success_rate"],
        ),
        _metric_acceptance_check(
            "min_penalized_quality_score",
            "Cascade penalized quality score meets threshold",
            (cascade.get("penalized_metrics") or {}).get("penalized_quality_score"),
            thresholds["min_penalized_quality_score"],
        ),
        _metric_acceptance_check(
            "min_element_source_locator_coverage",
            "Cascade element source locator coverage meets threshold",
            metrics.get("element_source_locator_coverage"),
            thresholds["min_element_source_locator_coverage"],
        ),
        _metric_acceptance_check(
            "min_bbox_coverage",
            "Cascade bounding-box coverage meets threshold",
            metrics.get("bbox_coverage"),
            thresholds["min_bbox_coverage"],
        ),
        _metric_acceptance_check(
            "min_rag_hit_at_10",
            "Cascade RAG Hit@10 meets threshold",
            rag.get("hit_at_10"),
            thresholds["min_rag_hit_at_10"],
        ),
        _metric_acceptance_check(
            "min_rag_evidence_coverage_at_5",
            "Cascade RAG evidence coverage@5 meets threshold",
            rag.get("evidence_coverage_at_5"),
            thresholds["min_rag_evidence_coverage_at_5"],
        ),
        _metric_acceptance_check(
            "min_rag_source_locator_coverage_at_5",
            "Cascade RAG source locator coverage@5 meets threshold",
            rag.get("source_locator_coverage_at_5"),
            thresholds["min_rag_source_locator_coverage_at_5"],
        ),
    ]
    return {
        "parser": "cascade",
        "ready": all(check["status"] == "pass" for check in checks),
        "thresholds": thresholds,
        "checks": checks,
    }


def _metric_acceptance_check(
    check_id: str,
    label: str,
    actual: Any,
    threshold: float,
) -> dict[str, Any]:
    value = _float_or_none(actual)
    return _acceptance_check(
        check_id,
        label,
        value is not None and value >= threshold,
        actual=value,
        threshold=threshold,
    )


def _acceptance_check(
    check_id: str,
    label: str,
    passed: bool,
    *,
    actual: Any,
    threshold: Any,
    details: str = "",
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "label": label,
        "status": "pass" if passed else "fail",
        "actual": actual,
        "threshold": threshold,
        "details": details,
    }


def _penalized_score(item: dict[str, Any]) -> float:
    return float((item.get("penalized_metrics") or {}).get("penalized_quality_score") or 0.0)


def _metric(item: dict[str, Any], name: str) -> float:
    return float(((item.get("parser_metrics") or {}).get(name)) or 0.0)


def _best_parser(parser_payloads: dict[str, dict[str, Any]], scorer: Any) -> str | None:
    if not parser_payloads:
        return None
    return max(parser_payloads, key=lambda name: scorer(parser_payloads[name]))


def _second_best_parser(parser_payloads: dict[str, dict[str, Any]], scorer: Any) -> str | None:
    ranked = sorted(parser_payloads, key=lambda name: scorer(parser_payloads[name]), reverse=True)
    return ranked[1] if len(ranked) > 1 else None


def _write_report(report: ParserBakeoffReport, output_json: Path, output_markdown: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_markdown.write_text(_markdown(payload), encoding="utf-8")


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Paper Parser Bake-off Report",
        "",
        f"- parsers: `{payload.get('parser_count', 0)}`",
        f"- papers: `{len(payload.get('paper_ids') or [])}`",
        "",
        "## Parser-Level Metrics",
        "",
        "| Parser | papers | success | sections avg | text chars avg | formulas avg | tables avg | table rows | figures avg | images | locators | bbox | warnings |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in (payload.get("parsers") or {}).items():
        metrics = item.get("parser_metrics") or {}
        lines.append(
            "| "
            + " | ".join([
                name,
                str(item.get("paper_count", 0)),
                f"{_fmt_pct(metrics.get('parse_success_rate'))} ({int(metrics.get('parse_success_count') or 0)}/{int(metrics.get('parse_requested_count') or 0)})",
                _fmt_num(metrics.get("section_count_avg")),
                _fmt_num(metrics.get("text_chars_avg")),
                _fmt_num(metrics.get("formula_count_avg")),
                _fmt_num(metrics.get("table_count_avg")),
                _fmt_pct(metrics.get("table_rows_coverage")),
                _fmt_num(metrics.get("figure_count_avg")),
                _fmt_pct(metrics.get("image_ref_coverage")),
                _fmt_pct(metrics.get("element_source_locator_coverage")),
                _fmt_pct(metrics.get("bbox_coverage")),
                _fmt_num(metrics.get("parser_warning_count")),
            ])
            + " |"
        )
    lines.extend([
        "",
        "## RAG-Level Metrics",
        "",
        "| Parser | papers | pairs | Hit@3 | Hit@5 | Hit@10 | Eq Hit@10 | MRR | NDCG@5 | Evidence@5 | Evidence@10 | Locator@5 | Locator@10 | formula-exp MRR | figure cov@5 | table cov@5 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for name, item in (payload.get("parsers") or {}).items():
        metrics = item.get("rag_metrics") or {}
        lines.append(
            "| "
            + " | ".join([
                name,
                str(metrics.get("papers_total") or "-"),
                str(metrics.get("pairs_total") or "-"),
                _fmt_pct(metrics.get("hit_at_3")),
                _fmt_pct(metrics.get("hit_at_5")),
                _fmt_pct(metrics.get("hit_at_10")),
                _fmt_pct(metrics.get("equivalent_hit_at_10")),
                _fmt_num(metrics.get("mrr")),
                _fmt_num(metrics.get("ndcg_at_5")),
                _fmt_pct(metrics.get("evidence_coverage_at_5")),
                _fmt_pct(metrics.get("evidence_coverage_at_10")),
                _fmt_pct(metrics.get("source_locator_coverage_at_5")),
                _fmt_pct(metrics.get("source_locator_coverage_at_10")),
                _fmt_num(metrics.get("formula_explanation_qa_mrr")),
                _fmt_pct(metrics.get("figure_qa_evidence_coverage_at_5")),
                _fmt_pct(metrics.get("table_qa_evidence_coverage_at_5")),
            ])
            + " |"
        )
    lines.extend([
        "",
        "## Document Parsing Protocol (descriptive; not an acceptance gate)",
        "",
        "| Parser | status | run | track | run status | reported / planned pages | metric denominators | measured runtime |",
        "|---|---|---|---|---|---:|---|---|",
    ])
    for name, item in (payload.get("parsers") or {}).items():
        parsing = item.get("parsing_protocol") or {"status": "not_evaluated"}
        page_summary = parsing.get("page_summary") or {}
        denominator_summary = parsing.get("metric_denominators") or {}
        runtime = parsing.get("runtime_summary") or {}
        runtime_text = (
            f"n={runtime.get('measured_pages', 0)}, total={_fmt_num(runtime.get('total_seconds'))}s, "
            f"median={_fmt_num(runtime.get('median_seconds'))}s, p95={_fmt_num(runtime.get('p95_seconds'))}s"
            if runtime.get("measured_pages")
            else "not recorded"
        )
        denominators = ", ".join(
            f"{metric}={details.get('scored_denominator', 0)}/{details.get('reported_outcomes', 0)}"
            for metric, details in sorted(denominator_summary.items())
        ) or "-"
        lines.append(
            "| " + " | ".join([
                name,
                str(parsing.get("status") or "not_evaluated"),
                str(parsing.get("run_id") or "-"),
                str(parsing.get("track") or "-"),
                str(parsing.get("run_status") or "-"),
                f"{page_summary.get('reported_pages', 0)} / {page_summary.get('planned_pages', 0)}",
                denominators,
                runtime_text,
            ]) + " |"
        )
        fingerprints = parsing.get("fingerprints") or {}
        fingerprint_refs = []
        for key in ("parser", "parser_image_digest", "parser_config_sha256", "code_revision"):
            identity = fingerprints.get(key) or {}
            fingerprint_refs.append(f"{key}={identity.get('value') or '-'}")
        if parsing.get("status") == "reported":
            lines.append(f"  - {name} fingerprint refs: " + "; ".join(fingerprint_refs))
        if parsing.get("reason"):
            lines.append(f"  - {name} parsing manifest: {parsing['reason']}")
    lines.extend([
        "",
        "Parsing protocol summaries (including runtime where recorded) are descriptive only; they do not change legacy parser scoring or acceptance.",
    ])
    lines.extend([
        "",
        "## Parser Scoring",
        "",
        "| Parser | raw quality | penalty total | penalized quality | top penalties |",
        "|---|---:|---:|---:|---|",
    ])
    for name, item in (payload.get("parsers") or {}).items():
        metrics = item.get("penalized_metrics") or {}
        penalties = metrics.get("penalty_details") or []
        top_penalties = ", ".join(
            f"{penalty.get('penalty_id')}={_fmt_num(penalty.get('penalty'))}"
            for penalty in penalties[:3]
        )
        lines.append(
            "| "
            + " | ".join([
                name,
                _fmt_num(metrics.get("raw_quality_score")),
                _fmt_num(metrics.get("penalty_total")),
                _fmt_num(metrics.get("penalized_quality_score")),
                top_penalties or "-",
            ])
            + " |"
        )
    acceptance = (payload.get("recommendations") or {}).get("cascade_acceptance")
    if isinstance(acceptance, dict):
        lines.extend([
            "",
            "## Cascade Acceptance",
            "",
            f"- parser: `{acceptance.get('parser')}`",
            f"- ready: `{bool(acceptance.get('ready'))}`",
            "",
            "| Check | status | actual | threshold | details |",
            "|---|---|---:|---:|---|",
        ])
        for check in acceptance.get("checks") or []:
            lines.append(
                "| "
                + " | ".join([
                    str(check.get("check_id") or ""),
                    str(check.get("status") or ""),
                    _fmt_value(check.get("actual")),
                    _fmt_value(check.get("threshold")),
                    str(check.get("details") or "-"),
                ])
                + " |"
            )
    failure_lines: list[str] = []
    for name, item in (payload.get("parsers") or {}).items():
        failed_items = item.get("failed_items") or []
        missing_items = ((item.get("ingest_manifest") or {}).get("missing_successful_items") or [])
        if failed_items or missing_items:
            failure_lines.append(f"### {name}")
            for failed in failed_items:
                failure_lines.append(
                    f"- failed `{failed.get('paper_id') or failed.get('arxiv_id')}`: {failed.get('reason') or 'unknown'}"
                )
            for missing in missing_items:
                failure_lines.append(
                    f"- missing artifact `{missing.get('paper_id') or missing.get('arxiv_id')}`: manifest status `{missing.get('status')}`"
                )
    if failure_lines:
        lines.extend(["", "## Ingest Failures", "", *failure_lines])
    lines.extend(["", "## Recommendations", ""])
    for key, value in (payload.get("recommendations") or {}).items():
        if key == "cascade_acceptance":
            continue
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1%}"


def _fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


def _fmt_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return _fmt_num(value)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "-"
    return str(value)


def _safe_div(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return float(numerator) / float(denominator)


def _avg(values: Sequence[int | float]) -> float:
    if not values:
        return 0.0
    return float(sum(values)) / float(len(values))


def _avg_available(values: Sequence[Any]) -> float:
    numeric_values = [
        value
        for value in (_float_or_none(value) for value in values)
        if value is not None
    ]
    return _avg(numeric_values)


def _bounded_ratio(value: Any, target: float) -> float | None:
    numeric = _float_or_none(value)
    if numeric is None:
        return None
    return _clamp(_safe_div(numeric, target))


def _clamp(value: Any, lower: float = 0.0, upper: float = 1.0) -> float:
    numeric = _float_or_none(value)
    if numeric is None:
        return lower
    return max(lower, min(upper, numeric))


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ParserArtifactInput",
    "ParserBakeoffReport",
    "ParserBakeoffReportConfig",
    "build_parser_bakeoff_report",
]
