from __future__ import annotations

import re
from typing import Any

from framework.agent.artifacts import ArtifactManager
from framework.tool.models import ToolDefinition
from framework.tool.registry import ToolRegistry
from framework.tool.runtime.errors import effect_not_applied_on
from framework.shared.graph_identity import GraphExecutionIdentity
from backend.layers.output.postgres_tools import REPOSITORY_NO_EFFECT_ERROR_TYPES
from backend.layers.output.records import OutputReport, OutputReportRecord, render_output_report_markdown
from backend.layers.output.validation import ensure_storable_json, optional_score

# Report search and publish go through the report repository (Postgres in
# hosted deployments); they declare their own bound.
REPORT_STORE_TIMEOUT_SECONDS = 30.0


def register_report_tools(
    registry: ToolRegistry,
    *,
    artifact_manager: ArtifactManager | None = None,
    run_id: str | None = None,
    execution_identity: GraphExecutionIdentity | None = None,
    persistence_repository: Any | None = None,
    report_service: Any | None = None,
) -> None:
    if execution_identity is not None and not isinstance(
        execution_identity,
        GraphExecutionIdentity,
    ):
        raise TypeError("execution_identity must be GraphExecutionIdentity")
    registry.register(
        ToolDefinition(
            name="report.render_markdown",
            description="Render an output report payload to markdown.",
            input_schema={
                "required": ["report"],
                "properties": {"report": {"type": "object"}},
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: {"markdown": render_output_report_markdown(_final_report(args["report"]))},
    )
    registry.register(
        ToolDefinition(
            name="report.render_json",
            description="Normalize an output report payload to JSON.",
            input_schema={
                "required": ["report"],
                "properties": {"report": {"type": "object"}},
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: {"report": _final_report(args["report"]).to_dict()},
    )
    if report_service is not None:
        registry.register(
            ToolDefinition(
                name="report.search",
                description="Search persisted report summaries through the configured report service.",
                input_schema={
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                side_effect="read_only",
                concurrency_safe=True,
                timeout_seconds=REPORT_STORE_TIMEOUT_SECONDS,
                max_result_bytes=500_000,
            ),
            lambda args: _search_reports(args, report_service=report_service),
        )
    registry.register(
        ToolDefinition(
            name="report.validate",
            description="Validate an output report payload without rendering it.",
            input_schema={
                "required": ["report"],
                "properties": {"report": {"type": "object"}},
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: _validate_report(args["report"]),
    )
    if artifact_manager is not None and execution_identity is not None:
        if run_id is not None and run_id != execution_identity.run_id:
            raise ValueError("run_id must match execution_identity.run_id")
        bound_run_id = execution_identity.run_id
        registry.register(
            ToolDefinition(
                name="report.export",
                description="Export an output report payload to a run artifact.",
                input_schema={
                    "required": ["report"],
                    "properties": {
                        "report": {"type": "object"},
                        "format": {
                            "type": "string",
                            "enum": ["markdown", "json"],
                        },
                        "path": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                side_effect="writes_local_state",
                metadata={"no_effect_error_types": ["ArtifactPathError"]},
            ),
            lambda args: _export_report(
                args,
                artifact_manager=artifact_manager,
                run_id=bound_run_id,
            ),
            graph_identity=execution_identity,
        )
    if persistence_repository is not None and execution_identity is not None:
        if run_id is not None and run_id != execution_identity.run_id:
            raise ValueError("run_id must match execution_identity.run_id")
        registry.register(
            ToolDefinition(
                name="report.publish",
                description="Publish an output report payload to the configured persistence repository.",
                input_schema={
                    "required": ["report"],
                    "properties": {
                        "report": {"type": "object"},
                        "report_id": {"type": "string"},
                        "status": {"type": "string"},
                        "report_markdown": {"type": "string"},
                        "quality_score": {"type": "number"},
                        "citation_coverage_score": {"type": "number"},
                        "manifest_path": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                side_effect="publishing",
                requires_approval=True,
                concurrency_safe=False,
                timeout_seconds=REPORT_STORE_TIMEOUT_SECONDS,
                max_result_bytes=100_000,
                metadata={
                    "writes_report_repository": True,
                    "no_effect_error_types": list(REPOSITORY_NO_EFFECT_ERROR_TYPES),
                },
            ),
            lambda args: _publish_report(
                args,
                persistence_repository=persistence_repository,
                execution_identity=execution_identity,
            ),
            graph_identity=execution_identity,
        )


def _validate_report(payload: Any) -> dict[str, Any]:
    try:
        report = _final_report(payload)
    except Exception as exc:
        return {
            "valid": False,
            "errors": [str(exc)],
            "section_count": 0,
            "source_url_count": 0,
        }
    return {
        "valid": True,
        "errors": [],
        "section_count": len(report.sections),
        "source_url_count": len(report.source_urls),
    }


def _search_reports(args: dict[str, Any], *, report_service: Any) -> dict[str, Any]:
    query = str(args["query"]).strip()
    if not query:
        raise ValueError("query is required")
    result = report_service.search_reports(query=query, limit=_limit(args.get("limit")))
    if hasattr(result, "to_dict"):
        return result.to_dict()
    return dict(result)


def _export_report(
    args: dict[str, Any],
    *,
    artifact_manager: ArtifactManager,
    run_id: str,
) -> dict[str, Any]:
    with effect_not_applied_on():
        report = _final_report(args["report"])
        export_format = str(args.get("format") or "markdown")
        if export_format not in {"json", "markdown"}:
            raise ValueError(f"unsupported report export format: {export_format}")
        relative_path = str(args.get("path") or _default_export_path(report, export_format))
    if export_format == "json":
        target = artifact_manager.write_json(run_id, relative_path, report.to_dict())
        content_type = "application/json"
    else:
        target = artifact_manager.write_text(run_id, relative_path, render_output_report_markdown(report))
        content_type = "text/markdown"
    return {
        "artifact_id": f"report:{relative_path}",
        "relative_path": relative_path,
        "format": export_format,
        "content_type": content_type,
        "size_bytes": target.stat().st_size,
    }


def _publish_report(
    args: dict[str, Any],
    *,
    persistence_repository: Any,
    execution_identity: GraphExecutionIdentity,
) -> dict[str, Any]:
    if not isinstance(execution_identity, GraphExecutionIdentity):
        raise TypeError("execution_identity must be GraphExecutionIdentity")
    with effect_not_applied_on():
        supplied_run_id = args.get("run_id")
        if supplied_run_id is not None and _required_text(supplied_run_id, "run_id") != execution_identity.run_id:
            raise ValueError("run_id must match execution_identity.run_id")
        run_id = execution_identity.run_id
        report = _final_report(args["report"])
        status = _optional_text(args.get("status")) or "final"
        report_id = _optional_text(args.get("report_id")) or f"{run_id}:{status}"
        report_markdown = args.get("report_markdown")
        if report_markdown is None:
            report_markdown = render_output_report_markdown(report)
        record = OutputReportRecord(
            report_id=report_id,
            run_id=run_id,
            status=status,
            title=report.title,
            report_json=report.to_dict(),
            report_markdown=str(report_markdown),
            quality_score=optional_score(args.get("quality_score"), "quality_score"),
            citation_coverage_score=optional_score(
                args.get("citation_coverage_score"),
                "citation_coverage_score",
            ),
            manifest_path=_optional_text(args.get("manifest_path")),
        )
    migrate = getattr(persistence_repository, "migrate", None)
    if callable(migrate):
        migrate()
    persistence_repository.save_report(record)
    return {
        "published": True,
        "report_id": record.report_id,
        "run_id": record.run_id,
        "status": record.status,
        "title": record.title,
        "repository": type(persistence_repository).__name__,
    }


def _default_export_path(report: OutputReport, export_format: str) -> str:
    extension = "json" if export_format == "json" else "md"
    slug = re.sub(r"[^a-z0-9]+", "-", report.title.casefold()).strip("-")
    if not slug:
        slug = "report"
    return f"reports/{slug}.{extension}"


def _final_report(payload: Any) -> OutputReport:
    """The one report rule shared by render, validate, export and publish.

    A payload that renders must also be storable, so ``report.validate`` and
    the writers can never disagree about the same report.
    """

    if not isinstance(payload, dict):
        raise ValueError("report must be an object")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("report.title is required")
    sections = payload.get("sections") or []
    if not isinstance(sections, list):
        raise ValueError("report.sections must be a list")
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValueError(f"report.sections[{index}] must be an object")
        section_title = section.get("title")
        if not isinstance(section_title, str) or not section_title.strip():
            raise ValueError(f"report.sections[{index}].title is required")
        content = section.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError(f"report.sections[{index}].content must be text")
    source_urls = payload.get("source_urls") or []
    if not isinstance(source_urls, list):
        raise ValueError("report.source_urls must be a list")
    for index, url in enumerate(source_urls):
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"report.source_urls[{index}] must be a non-empty string")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("report.metadata must be an object")
    report = OutputReport(
        title=title.strip(),
        sections=[dict(section) for section in sections],
        source_urls=[url.strip() for url in source_urls],
        metadata=dict(metadata),
    )
    ensure_storable_json(report.to_dict(), "report")
    return report


def _required_text(value: Any, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _limit(value: Any) -> int:
    if value is None:
        return 20
    return max(1, min(int(value), 100))
