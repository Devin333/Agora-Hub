"""Reproducible production Research PDF parsing evaluation runner.

Calls the public Research composition; never invokes Marker directly, creates a
Graph identity, or accepts gold/scorer mappings as parser input.
"""
from __future__ import annotations

import argparse
from asyncio import CancelledError
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Iterator, Mapping, Sequence

import pymupdf

from backend.research.application.parsing_production_evaluation import (
    DurableExecutionEvidence,
    ProductionParsingDocumentInput,
    ProductionParsingEvaluationError,
    ProductionParsingEvaluationService,
    ProductionParsingRuntimeRequest,
    ProductionParsingRuntimeResult,
    validate_unique_document_sources,
)
from backend.research.domain.document import ResearchDocument
from backend.research.rag.evaluation.document_parsing_export import PageConvention, export_research_document
from backend.research.rag.evaluation.document_parsing_render import render_production_pages, write_production_predictions
from backend.research.application.parse_paper import ParsePaperRequest

INPUT_SCHEMA_VERSION = "research-pdf-parsing-inputs/v1"
OUTPUT_SCHEMA_VERSION = "research-production-parsing-run/v1"
WEIGHTS_MANIFEST_SCHEMA = "newsroom-marker-weights-manifest.v1"
MARKER_OFFLINE_FONT_PATH = "static/fonts/GoNotoCurrent-Regular.ttf"
MARKER_OFFLINE_FONT_SHA256 = "882afbab965608c2d2bc627fd8016b962aa5a6be2d358f9de24a7b5967c5632e"
MARKER_OFFLINE_FONT_SIZE = 14_700_060
_SOURCE_BUFFER_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class FrozenInputManifest:
    schema_version: str
    run_id: str
    baseline_identity: str
    documents: tuple[ProductionParsingDocumentInput, ...]


class ProductionCompositionRuntimeAdapter:
    """Adapter over the composition-owned deterministic production parsing graph."""

    def __init__(self, *, composition: Any, research_root: Path, marker_image: str,
                 marker_cache: Path, marker_weights_manifest: Path, repository_root: Path,
                 marker_timeout_seconds: int = 1800,
                 reading_order_profile: str = "backend_preserved") -> None:
        if not getattr(composition, "available", False):
            raise RuntimeError(
                f"Research production composition is unavailable: {getattr(composition, 'availability_error', None)}"
            )
        self._composition = composition
        self._graph_service = composition.production_parsing_evaluation_service
        if self._graph_service is None:
            raise RuntimeError("Research composition has no production parsing evaluation service")
        self._service = composition.service
        self._research_root = research_root
        self._marker_image = marker_image
        self._marker_cache = marker_cache
        self._weights_manifest = marker_weights_manifest
        self._weights_digest = verify_marker_weights_manifest(marker_cache, marker_weights_manifest)
        execution = composition.execution_composition
        if execution is None:
            raise RuntimeError("Research production composition has no execution composition")
        self._composition_fingerprint = str(execution.fingerprint)
        self._runtime_config_fingerprint = str(self._service.capability_runtime_identity())
        self._repository_root = repository_root
        self._marker_timeout_seconds = marker_timeout_seconds
        self._reading_order_profile = reading_order_profile
        self._source_identity = runtime_source_identity(repository_root)
        self._code_revision = repository_revision(repository_root)

    @property
    def source_identity(self) -> Mapping[str, Any]:
        return self._source_identity

    def assert_source_identity(self) -> None:
        current = runtime_source_identity(self._repository_root)
        if current["source_content_sha256"] != self._source_identity["source_content_sha256"]:
            raise ProductionParsingEvaluationError(
                "runtime_source_identity_changed",
                "production source files changed during the frozen run",
                details={
                    "expected": self._source_identity["source_content_sha256"],
                    "actual": current["source_content_sha256"],
                },
            )

    def parse_document(self, request: ProductionParsingRuntimeRequest) -> ProductionParsingRuntimeResult:
        source = request.document
        safe_paper_id = f"eval-{source.source_sha256[:32]}"
        stage_dir = self._research_root / "evaluation-inputs" / request.run_id
        stage_dir.mkdir(parents=True, exist_ok=False)
        staged_source = stage_dir / f"{safe_paper_id}.pdf"
        shutil.copyfile(source.source_path, staged_source)
        if sha256_file(staged_source) != source.source_sha256:
            raise ProductionParsingEvaluationError(
                "staged_source_hash_mismatch", "staged production source differs from frozen input"
            )
        graph_result = self._graph_service.run(ParsePaperRequest(
            source=staged_source.as_uri(),
            source_type="local",
            run_id=request.run_id,
            options={
                "quality_profile": "reading",
                "include_catalog": True,
                "include_chunks": True,
                "include_evidence": False,
                "max_attempts": 1,
                "timeout_seconds": self._marker_timeout_seconds,
                "refresh": True,
            },
            metadata={
                "paper_id": safe_paper_id,
                "title": source.document_id,
                "title_source": "production_evaluation_manifest",
                "evaluation_document_id": source.document_id,
                "evaluation_input_mode": source.input_mode,
                "expected_source_sha256": source.source_sha256,
            },
        ))
        parsed = graph_result.parse_result
        document = parsed.document
        if document is None:
            raise ProductionParsingEvaluationError(
                "research_document_missing", "production graph returned no ResearchDocument"
            )
        chunk_payload = dict(graph_result.chunk_payload)
        manifest_path = str(chunk_payload.get("manifest_path") or "").strip()
        if not manifest_path:
            raise ProductionParsingEvaluationError(
                "chunk_manifest_path_missing", "persisted chunk payload has no manifest path"
            )
        chunk_manifest = _load_json_object(Path(manifest_path))
        return ProductionParsingRuntimeResult(
            document=document,
            chunk_manifest=chunk_manifest,
            chunk_payload=chunk_payload,
            backend_raw_refs=_backend_raw_refs(document),
            execution_evidence=DurableExecutionEvidence.from_dict(
                graph_result.execution_evidence
            ),
            durable_phase_records=tuple(graph_result.durable_phase_records),
            durable_harness_events=tuple(graph_result.durable_harness_events),
            chunk_artifact=dict(graph_result.chunk_artifact),
            runtime_fingerprints=self._runtime_fingerprints(
                document, graph_result.execution_evidence
            ),
            parser_attempts=tuple(dict(item) for item in parsed.parser_attempts),
        )

    def read(
        self, ref: str, *, actor_scope: Mapping[str, str],
        include_payload: bool = False, max_chars: int = 200_000,
    ) -> Mapping[str, Any]:
        """Use the existing public artifact API, not direct filesystem access."""
        from interfaces.services.research_service import ResearchActorInput
        return self._service.get_artifact(
            ref, include_payload=include_payload, max_chars=max_chars,
            actor=ResearchActorInput(
                tenant_id=actor_scope.get("tenant_id"),
                user_id=actor_scope.get("user_id"),
                memory_namespace=actor_scope.get("memory_namespace"),
            ),
        )

    def _runtime_fingerprints(
        self, document: ResearchDocument, execution_evidence: Mapping[str, Any]
    ) -> dict[str, str]:
        fidelity = document.fidelity
        parser_fingerprint = fidelity.parser_fingerprint if fidelity else str(
            document.metadata.get("parse_cache_fingerprint") or ""
        )
        normalization_version = (
            fidelity.normalization_version
            if fidelity is not None
            else str(document.normalization_version or "")
        )
        normalizer = canonical_fingerprint({
            "normalization_version": normalization_version,
            "normalizer_source": module_file_identity(
                "backend.research.document.marker_pdf_parser"
            ),
        })
        fidelity_profile = fidelity.profile_fingerprint if fidelity else ""
        return {
            "composition": self._composition_fingerprint,
            "runtime_config": self._runtime_config_fingerprint,
            "parser": parser_fingerprint,
            "parser_image_digest": _image_digest(self._marker_image),
            "parser_weights_digest": self._weights_digest,
            "parser_config_sha256": _parser_config_fingerprint(
                execution_evidence=execution_evidence,
                cache=self._marker_cache,
                weights_manifest=self._weights_manifest,
                reading_order_profile=self._reading_order_profile,
                recovery_enabled=False,
                allow_abstract_fallback=False,
            ),
            "document_schema": source_file_identity(ResearchDocument),
            "normalizer": normalizer,
            "normalization_version": normalization_version,
            "fidelity_profile": fidelity_profile,
            "exporter": module_file_identity("backend.research.rag.evaluation.document_parsing_export"),
            "chunker": module_file_identity("backend.research.document.chunker"),
            "code_revision": self._code_revision,
            "source_content_sha256": str(self._source_identity["source_content_sha256"]),
            "source_manifest_sha256": str(self._source_identity["source_manifest_sha256"]),
            "source": f"sha256:{document.source_hash}",
        }

def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare-cache-manifest":
            payload = build_marker_weights_manifest(args.cache_dir, args.output)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        return run_production_evaluation(
            input_manifest=args.input_manifest, output=args.output,
            marker_image=args.marker_image, marker_cache=args.marker_cache,
            marker_weights_manifest=args.marker_weights_manifest,
            repository_root=args.repository_root,
            marker_timeout_seconds=args.marker_timeout_seconds,
            marker_device=args.marker_device,
            reading_order_profile=args.reading_order_profile,
        )
    except (OSError, ValueError, RuntimeError, ProductionParsingEvaluationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def run_production_evaluation(*, input_manifest: Path, output: Path, marker_image: str,
                              marker_cache: Path, marker_weights_manifest: Path,
                              repository_root: Path, marker_timeout_seconds: int = 1800,
                              marker_device: str = "cpu",
                              reading_order_profile: str = "backend_preserved",
                              composition_factory: Any | None = None,
                              runtime_adapter_factory: Any = ProductionCompositionRuntimeAdapter) -> int:
    manifest = load_input_manifest(input_manifest)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"output directory already exists: {output}")
    marker_cache = marker_cache.resolve(strict=True)
    marker_weights_manifest = marker_weights_manifest.resolve(strict=True)
    repository_root = repository_root.resolve(strict=True)
    if not marker_cache.is_dir() or not any(marker_cache.iterdir()):
        raise ValueError("marker cache must be an existing non-empty directory")
    if type(marker_timeout_seconds) is not int or marker_timeout_seconds <= 0:
        raise ValueError("marker_timeout_seconds must be a positive integer")
    if "@sha256:" not in marker_image or not _image_digest(marker_image):
        raise ValueError("marker_image must be an immutable repository@sha256:<digest> reference")

    output.mkdir(parents=True, exist_ok=False)
    runtime_root = output / "_runtime" / "research"
    runtime_root.mkdir(parents=True, exist_ok=True)
    frozen_input_bytes = input_manifest.read_bytes()
    (output / "frozen-input-manifest.json").write_bytes(frozen_input_bytes)
    _write_json(output / "source-manifest-identity.json", {
        "schema_version": INPUT_SCHEMA_VERSION,
        "input_manifest_sha256": f"sha256:{sha256(frozen_input_bytes).hexdigest()}",
        "run_id": manifest.run_id, "baseline_identity": manifest.baseline_identity,
        "document_count": len(manifest.documents),
    })
    environment = production_environment(
        research_root=runtime_root, marker_image=marker_image,
        marker_cache=marker_cache, marker_weights_manifest=marker_weights_manifest,
        marker_timeout_seconds=marker_timeout_seconds, marker_device=marker_device,
        reading_order_profile=reading_order_profile,
    )
    if composition_factory is None:
        from interfaces.composition.research import build_research_runtime_composition
        composition_factory = build_research_runtime_composition

    summaries: list[dict[str, Any]] = []
    cancelled = False
    with patched_environment(environment):
        composition = None
        try:
            composition = composition_factory()
            adapter = runtime_adapter_factory(
                composition=composition, research_root=runtime_root,
                marker_image=marker_image, marker_cache=marker_cache,
                marker_weights_manifest=marker_weights_manifest,
                repository_root=repository_root,
                marker_timeout_seconds=marker_timeout_seconds,
                reading_order_profile=reading_order_profile,
            )
            service = ProductionParsingEvaluationService(adapter, artifact_reader=adapter)
        except Exception as exc:  # noqa: BLE001 - preflight must ledger every source
            diagnostic = ProductionParsingEvaluationError(
                "configuration_preflight_failed",
                "production parsing configuration preflight failed",
                details={
                    "cause_type": type(exc).__name__,
                    "cause_message": str(exc),
                },
            )
            for index, document_input in enumerate(manifest.documents, start=1):
                document_run_id = (
                    f"{manifest.run_id}-{index:04d}-{document_input.source_sha256[:12]}"
                )
                target = output / "documents" / document_input.document_id
                target.mkdir(parents=True, exist_ok=False)
                summaries.append(_write_failure_artifacts(
                    target=target,
                    document=document_input,
                    run_id=document_run_id,
                    error=diagnostic,
                    status="not_run",
                    reason_code="configuration_preflight_failed",
                ))
            run_payload = {
                "schema_version": OUTPUT_SCHEMA_VERSION,
                "run_id": manifest.run_id,
                "baseline_identity": manifest.baseline_identity,
                "execution_kind": "production",
                "track": "production_document",
                "run_status": "configuration_preflight_failed",
                "requested_documents": len(manifest.documents),
                "completed_documents": 0,
                "failed_documents": 0,
                "cancelled_documents": 0,
                "not_run_documents": len(manifest.documents),
                "documents": summaries,
                "preflight_error": diagnostic.details,
                "diagnostic_disclaimer": (
                    "Execution did not start and this output is not a production score. "
                    "Historical direct-subprocess raw reruns remain diagnostic and are not B1."
                ),
            }
            _write_json(output / "preflight-error.json", {
                "reason_code": diagnostic.code,
                **diagnostic.details,
            })
            _write_json(output / "run.json", run_payload)
            close = getattr(composition, "close", None) if composition is not None else None
            if callable(close):
                close()
            print(json.dumps(run_payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 2
        try:
            source_identity = dict(getattr(adapter, "source_identity", runtime_source_identity(repository_root)))
            _write_json(output / "runtime-source-manifest.json", source_identity)
            for index, document_input in enumerate(manifest.documents, start=1):
                document_run_id = f"{manifest.run_id}-{index:04d}-{document_input.source_sha256[:12]}"
                target = output / "documents" / document_input.document_id
                target.mkdir(parents=True, exist_ok=False)
                try:
                    assert_source_identity = getattr(adapter, "assert_source_identity", None)
                    if callable(assert_source_identity):
                        assert_source_identity()
                    observed_pages = inspect_pdf_page_count(document_input.source_path)
                    if observed_pages != document_input.expected_pages:
                        raise ProductionParsingEvaluationError(
                            "source_page_count_mismatch",
                            "source PDF page count differs from frozen manifest",
                            details={"expected": document_input.expected_pages, "actual": observed_pages},
                        )
                    evaluated = service.evaluate_document(
                        run_id=document_run_id, document=document_input
                    )
                    if callable(assert_source_identity):
                        assert_source_identity()
                    summary = _write_success_artifacts(
                        target=target, result=evaluated, page_count=observed_pages
                    )
                except (KeyboardInterrupt, CancelledError) as exc:
                    summary = _write_failure_artifacts(
                        target=target, document=document_input,
                        run_id=document_run_id, error=exc, status="cancelled",
                        reason_code="run_cancelled",
                    )
                    summaries.append(summary)
                    for remaining_index, remaining in enumerate(
                        manifest.documents[index:], start=index + 1
                    ):
                        remaining_run_id = (
                            f"{manifest.run_id}-{remaining_index:04d}-{remaining.source_sha256[:12]}"
                        )
                        remaining_target = output / "documents" / remaining.document_id
                        remaining_target.mkdir(parents=True, exist_ok=False)
                        summaries.append(_write_failure_artifacts(
                            target=remaining_target, document=remaining,
                            run_id=remaining_run_id, error=None, status="not_run",
                            reason_code="run_cancelled",
                        ))
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001 - every source gets a terminal ledger
                    summary = _write_failure_artifacts(
                        target=target, document=document_input,
                        run_id=document_run_id, error=exc,
                    )
                summaries.append(summary)
        finally:
            close = getattr(composition, "close", None) if composition is not None else None
            if callable(close):
                close()

    status_counts = {
        status: sum(1 for item in summaries if item.get("status") == status)
        for status in ("succeeded", "failed", "cancelled", "not_run")
    }
    run_payload = {
        "schema_version": OUTPUT_SCHEMA_VERSION, "run_id": manifest.run_id,
        "baseline_identity": manifest.baseline_identity,
        "execution_kind": "production", "track": "production_document",
        "requested_documents": len(manifest.documents),
        "completed_documents": status_counts["succeeded"],
        "failed_documents": status_counts["failed"],
        "cancelled_documents": status_counts["cancelled"],
        "not_run_documents": status_counts["not_run"],
        "documents": summaries,
        "diagnostic_disclaimer": (
            "Historical direct-subprocess raw reruns remain diagnostic and are not B1. "
            "This run is B1_current only when every document carries production composition evidence."
        ),
    }
    _write_json(output / "run.json", run_payload)
    print(json.dumps(run_payload, ensure_ascii=False, indent=2, sort_keys=True))
    if cancelled:
        return 130
    return 0 if status_counts["failed"] == 0 else 1


def load_input_manifest(path: Path) -> FrozenInputManifest:
    payload = _load_json_object(path)
    _require_exact_keys(payload, {"schema_version", "run_id", "baseline_identity", "documents"},
                        "input manifest")
    if payload["schema_version"] != INPUT_SCHEMA_VERSION:
        raise ValueError(f"unsupported input schema: {payload['schema_version']}")
    run_id = _required_text(payload["run_id"], "run_id")
    baseline_identity = _required_text(payload["baseline_identity"], "baseline_identity")
    if not baseline_identity.startswith("B1_current"):
        raise ValueError("baseline_identity must explicitly identify B1_current")
    raw_documents = payload["documents"]
    if not isinstance(raw_documents, list):
        raise ValueError("documents must be a list")
    allowed = {"document_id", "source_path", "source_sha256", "input_mode",
               "exposure", "expected_pages"}
    documents: list[ProductionParsingDocumentInput] = []
    for index, raw in enumerate(raw_documents):
        if not isinstance(raw, dict):
            raise ValueError(f"documents[{index}] must be an object")
        _require_exact_keys(raw, allowed, f"documents[{index}]")
        source_path = Path(_required_text(raw["source_path"], "source_path"))
        if not source_path.is_absolute():
            raise ValueError(f"documents[{index}].source_path must be absolute")
        documents.append(ProductionParsingDocumentInput(
            document_id=raw["document_id"],
            source_path=source_path,
            source_sha256=_required_text(raw["source_sha256"], "source_sha256"),
            input_mode=_required_text(raw["input_mode"], "input_mode"),
            exposure=_required_text(raw["exposure"], "exposure"),
            expected_pages=raw["expected_pages"],
        ))
    return FrozenInputManifest(
        schema_version=INPUT_SCHEMA_VERSION, run_id=run_id,
        baseline_identity=baseline_identity,
        documents=validate_unique_document_sources(documents),
    )

def production_environment(*, research_root: Path, marker_image: str, marker_cache: Path,
                           marker_weights_manifest: Path,
                           marker_timeout_seconds: int, marker_device: str = "cpu",
                           reading_order_profile: str = "backend_preserved") -> dict[str, str]:
    return {
        "NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED": "1",
        "NEWS_RESEARCH_ROOT": str(research_root),
        "NEWSROOM_PARSER_RUN_ROOT": str(research_root / "parser-runs"),
        "NEWS_DATABASE_DSN": "",
        "NEWS_RESEARCH_ARTIFACT_ROOT": str(research_root / "artifacts"),
        "NEWS_ARTIFACT_ROOT": str(research_root / "artifacts"),
        "NEWS_RESEARCH_RUN_STORE_ROOT": str(research_root / "runs"),
        "NEWS_RESEARCH_RAG_LOCAL_ROOT": str(research_root / "chunks"),
        "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT": str(research_root / "rag" / "transcripts"),
        "NEWS_RESEARCH_RAG_BACKEND": "local", "NEWS_RAG_MEMORY": "false",
        "NEWS_RESEARCH_PARSER_BACKENDS": "marker",
        "NEWS_RESEARCH_ALLOW_ABSTRACT_FALLBACK": "false",
        "NEWS_RESEARCH_PARSER_TIMEOUT_SECONDS": str(marker_timeout_seconds),
        "NEWS_RESEARCH_PARSER_READING_ORDER_PROFILE": reading_order_profile,
        "NEWS_RESEARCH_PARSER_RECOVERY_ENABLED": "false",
        "NEWS_RESEARCH_PARSER_RECOVERY_PARSER_ENABLED": "false",
        "NEWS_RESEARCH_PARSER_RECOVERY_FORMULA_ROI_ENABLED": "false",
        "NEWS_RESEARCH_PARSER_RECOVERY_TABLE_ROI_ENABLED": "false",
        "NEWSROOM_MARKER_DOCKER_IMAGE": marker_image,
        "NEWSROOM_MARKER_DOCKER_ARGS": "",
        "NEWSROOM_MARKER_CACHE_DIR": str(marker_cache),
        "NEWSROOM_MARKER_WEIGHTS_MANIFEST": str(marker_weights_manifest),
        "NEWSROOM_MARKER_TIMEOUT_SECONDS": str(marker_timeout_seconds),
        "NEWSROOM_MARKER_TORCH_DEVICE": marker_device,
    }


def build_marker_weights_manifest(cache_dir: Path, output: Path) -> dict[str, Any]:
    cache_dir = cache_dir.resolve(strict=True)
    if not cache_dir.is_dir() or not any(cache_dir.iterdir()):
        raise ValueError("cache_dir must be an existing non-empty directory")
    output = output.resolve()
    try:
        output.relative_to(cache_dir)
    except ValueError:
        pass
    else:
        raise ValueError("weights manifest must be outside the mounted cache directory")
    if output.exists():
        raise FileExistsError(f"weights manifest already exists: {output}")
    entries: list[dict[str, Any]] = []
    for path in sorted(cache_dir.rglob("*"), key=lambda item: item.as_posix()):
        if _is_link_or_junction(path):
            raise ValueError(f"marker cache contains a link or junction: {path}")
        if path.is_file():
            entries.append({"path": path.relative_to(cache_dir).as_posix(),
                            "sha256": sha256_file(path), "size": path.stat().st_size})
    if not entries:
        raise ValueError("marker cache contains no regular files")
    payload = {"schema": WEIGHTS_MANIFEST_SCHEMA, "files": entries}
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output, payload)
    return {"schema": WEIGHTS_MANIFEST_SCHEMA, "manifest_path": str(output),
            "file_count": len(entries), "total_bytes": sum(item["size"] for item in entries),
            "verified_manifest_sha256": verify_marker_weights_manifest(cache_dir, output)}


def verify_marker_weights_manifest(cache_dir: Path, manifest_path: Path) -> str:
    payload = _load_json_object(manifest_path)
    _require_exact_keys(payload, {"schema", "files"}, "weights manifest")
    if payload["schema"] != WEIGHTS_MANIFEST_SCHEMA:
        raise ValueError("unsupported marker weights manifest schema")
    files = payload["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("marker weights manifest files must be a non-empty list")
    expected_paths: set[str] = set()
    canonical: list[dict[str, Any]] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            raise ValueError(f"weights manifest files[{index}] must be an object")
        _require_exact_keys(entry, {"path", "sha256", "size"}, f"weights files[{index}]")
        relative = _required_text(entry["path"], "path")
        relative_path = Path(relative)
        if relative in expected_paths or relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("weights manifest contains a duplicate or unsafe path")
        digest = _required_text(entry["sha256"], "sha256").casefold()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("weights file sha256 is invalid")
        size = entry["size"]
        if type(size) is not int or size < 0:
            raise ValueError("weights file size must be a non-negative integer")
        path = cache_dir / relative_path
        if _is_link_or_junction(path) or not path.is_file():
            raise ValueError(f"weights manifest file is missing or unsafe: {relative}")
        if path.stat().st_size != size or sha256_file(path) != digest:
            raise ValueError(f"weights manifest file identity mismatch: {relative}")
        expected_paths.add(relative)
        canonical.append({"path": relative, "sha256": digest, "size": size})
    required_font = next(
        (entry for entry in canonical if entry["path"] == MARKER_OFFLINE_FONT_PATH),
        None,
    )
    if required_font != {
        "path": MARKER_OFFLINE_FONT_PATH,
        "sha256": MARKER_OFFLINE_FONT_SHA256,
        "size": MARKER_OFFLINE_FONT_SIZE,
    }:
        raise ValueError(
            "weights manifest must include the verified official Marker offline font"
        )
    actual_paths = {path.relative_to(cache_dir).as_posix()
                    for path in cache_dir.rglob("*") if path.is_file()}
    if actual_paths != expected_paths:
        raise ValueError("weights manifest does not cover the exact cache file set")
    encoded = json.dumps(
        {"schema": WEIGHTS_MANIFEST_SCHEMA,
         "files": sorted(canonical, key=lambda item: item["path"])},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


def repository_revision(repository_root: Path) -> str:
    return _git(repository_root, "rev-parse", "HEAD").decode("ascii").strip()


def runtime_source_identity(repository_root: Path) -> dict[str, Any]:
    roots = (
        "backend", "framework", "infrastructure", "interfaces", "scripts",
        "docker", "requirements",
    )
    named_files = ("pyproject.toml", "uv.lock", "requirements.txt")
    files: list[dict[str, Any]] = []
    for root_name in roots:
        root = repository_root / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if not path.is_file() or _is_link_or_junction(path):
                continue
            relative = path.relative_to(repository_root).as_posix()
            if any(part in {"__pycache__", ".pytest_cache", "node_modules"} for part in path.parts):
                continue
            files.append({
                "path": relative,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            })
    for name in named_files:
        path = repository_root / name
        if path.is_file() and not _is_link_or_junction(path):
            files.append({"path": name, "sha256": sha256_file(path), "size": path.stat().st_size})
    files.sort(key=lambda item: item["path"])
    canonical = {"schema": "research-runtime-source-manifest/v1", "files": files}
    encoded = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    content_projection = [(item["path"], item["sha256"]) for item in files]
    content_encoded = json.dumps(content_projection, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return {
        **canonical,
        "source_content_sha256": f"sha256:{sha256(content_encoded).hexdigest()}",
        "source_manifest_sha256": f"sha256:{sha256(encoded).hexdigest()}",
    }


def repository_identity(repository_root: Path) -> tuple[str, str]:
    """Compatibility projection: revision plus runtime-source content identity."""
    identity = runtime_source_identity(repository_root)
    return repository_revision(repository_root), str(identity["source_content_sha256"])

def inspect_pdf_page_count(path: Path) -> int:
    with pymupdf.open(path) as document:
        if document.page_count <= 0:
            raise ValueError("source PDF contains no pages")
        return int(document.page_count)

def _write_success_artifacts(*, target: Path, result: Any, page_count: int) -> dict[str, Any]:
    document = result.document
    export = export_research_document(
        document, page_count=page_count,
        page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED,
    )
    rendered = render_production_pages(export)
    write_production_predictions(
        rendered, page_filenames={page: f"page-{page:04d}.md"
                                  for page in range(1, page_count + 1)},
        output_path=target / "predictions",
    )
    _write_json(target / "research-document.json", document.model_dump(mode="json"))
    _write_json(target / "production-export.json", export.model_dump(mode="json"))
    _write_json(target / "chunk-manifest.json", dict(result.chunk_manifest))
    _write_json(target / "document-chunks.json", dict(result.chunk_payload))
    _write_json(target / "chunk-artifact-evidence.json", {
        key: value for key, value in result.chunk_artifact.items() if key != "payload"
    })
    _write_json(target / "backend-raw-refs.json", dict(result.backend_raw_refs))
    _write_json(target / "execution-evidence.json", result.execution_evidence.to_dict())
    _write_json(target / "runtime-fingerprints.json", dict(result.runtime_fingerprints))
    _write_json(target / "parser-attempts.json", list(result.parser_attempts))
    with (target / "durable-phase-records.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in result.durable_phase_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    _write_json(target / "durable-harness-events.json", {
        "schema": "research-production-parsing-harness-evidence/v1",
        "events": list(result.durable_harness_events),
    })
    payload = {
        "document_id": result.document_input.document_id, "run_id": result.run_id,
        "status": "succeeded", "source_sha256": result.document_input.source_sha256,
        "expected_pages": page_count, "terminal_pages": len(rendered),
        "document_status": document.status,
        "parser_backend": document.metadata.get("parser_backend") or document.parser_backend,
        "chunk_count": len(result.chunk_payload.get("chunks", [])),
        "execution_evidence_ref": result.execution_evidence.ref,
        "evidence_checks": dict(result.evidence_checks),
    }
    _write_json(target / "result.json", payload)
    return payload


_SENSITIVE_DIAGNOSTIC_KEY = re.compile(
    r"(?:secret|token|password|api[_-]?key|authorization|credential|cookie)",
    re.IGNORECASE,
)


def _safe_error_diagnostic(
    error: BaseException | None, *, fallback: str
) -> tuple[str, dict[str, Any]]:
    if error is None:
        return fallback, {}
    code = str(getattr(error, "code", None) or type(error).__name__).strip()
    raw_details = getattr(error, "details", {})
    details = _sanitize_diagnostic_value(raw_details, depth=0)
    if not isinstance(details, dict):
        details = {"value": details}
    message = str(getattr(error, "message", None) or str(error)).strip()
    if message:
        details.setdefault("message", _sanitize_diagnostic_text(message))
    return code, details


def _sanitize_diagnostic_value(value: Any, *, depth: int) -> Any:
    if depth > 8:
        return "<diagnostic-depth-limit>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_diagnostic_text(value)
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:100]:
            key = str(raw_key)[:200]
            sanitized[key] = (
                "<redacted>"
                if _SENSITIVE_DIAGNOSTIC_KEY.search(key)
                else _sanitize_diagnostic_value(raw_value, depth=depth + 1)
            )
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [
            _sanitize_diagnostic_value(item, depth=depth + 1)
            for item in list(value)[:100]
        ]
    return _sanitize_diagnostic_text(str(value))


def _sanitize_diagnostic_text(value: str) -> str:
    text = value[:8_000]
    text = re.sub(
        r"(?i)(api[_-]?key|token|password|authorization|credential)\s*[:=]\s*[^,;\s]+",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted>", text)
    return text


def _write_failure_artifacts(*, target: Path, document: ProductionParsingDocumentInput,
                             run_id: str, error: BaseException | None,
                             status: str = "failed", reason_code: str | None = None) -> dict[str, Any]:
    if status not in {"failed", "cancelled", "not_run"}:
        raise ValueError("failure artifact status is invalid")
    code, details = _safe_error_diagnostic(error, fallback=status)
    code = reason_code or code
    _write_json(target / "page-ledger.json", {
        "schema_version": "research-production-parsing-failure-ledger/v1",
        "requested_pages": document.expected_pages, "terminal_pages": document.expected_pages,
        "pages": [{"page": page, "status": status, "reason": str(code)}
                  for page in range(1, document.expected_pages + 1)],
    })
    payload = {
        "document_id": document.document_id, "run_id": run_id, "status": status,
        "source_sha256": document.source_sha256, "expected_pages": document.expected_pages,
        "terminal_pages": document.expected_pages, "reason_code": str(code),
        "error_type": type(error).__name__ if error is not None else None, "details": details,
    }
    _write_json(target / "result.json", payload)
    return payload


def _backend_raw_refs(document: ResearchDocument) -> dict[str, str]:
    refs: dict[str, str] = {}
    for key in ("parser_output_ref", "parser_content_ref"):
        value = str(document.metadata.get(key) or "").strip()
        if value:
            refs[key] = value
    return refs


def _local_artifact_path(value: str) -> Path:
    if value.startswith("file://"):
        from urllib.parse import unquote, urlsplit
        parsed = urlsplit(value)
        path = unquote(parsed.path)
        if parsed.netloc:
            path = f"//{parsed.netloc}{path}"
        return Path(path)
    return Path(value)


def source_file_identity(value: Any) -> str:
    import inspect
    return f"sha256:{sha256(Path(inspect.getfile(value)).read_bytes()).hexdigest()}"


def module_file_identity(module_name: str) -> str:
    import importlib
    module = importlib.import_module(module_name)
    path = Path(str(module.__file__)).resolve(strict=True)
    return f"sha256:{sha256(path.read_bytes()).hexdigest()}"


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_SOURCE_BUFFER_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


def _parser_config_fingerprint(
    *,
    execution_evidence: Mapping[str, Any],
    cache: Path,
    weights_manifest: Path,
    reading_order_profile: str,
    recovery_enabled: bool,
    allow_abstract_fallback: bool,
) -> str:
    request = execution_evidence.get("request")
    if not isinstance(request, Mapping):
        raise ValueError("execution evidence request is required for parser config fingerprint")
    payload = {
        "image": request.get("image"),
        "argv": request.get("argv"),
        "environment": request.get("environment"),
        "profile": request.get("profile"),
        "resource_limits": request.get("resource_limits"),
        "timeout_seconds": request.get("timeout_seconds"),
        "cache": str(cache),
        "weights_manifest": f"sha256:{sha256(weights_manifest.read_bytes()).hexdigest()}",
        "backend": "marker",
        "reading_order_profile": reading_order_profile,
        "recovery_enabled": recovery_enabled,
        "allow_abstract_fallback": allow_abstract_fallback,
    }
    return canonical_fingerprint(payload)


def _image_digest(image: str) -> str:
    marker = "@sha256:"
    if marker not in image:
        return ""
    digest = image.rsplit(marker, 1)[1].strip().casefold()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return ""
    return f"sha256:{digest}"


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(["git", *args], cwd=root, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace").strip())
    return completed.stdout


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True

def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")


def _require_exact_keys(payload: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{label} keys differ; missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
        )


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


@contextmanager
def patched_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update({key: str(value) for key, value in values.items()})
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run production Research PDF parsing through the public composition boundary."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser(
        "prepare-cache-manifest",
        help="Build the immutable Marker weights manifest outside the cache directory.",
    )
    prepare.add_argument("--cache-dir", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    run = subparsers.add_parser("run", help="Run the frozen source manifest through production parsing.")
    run.add_argument("--input-manifest", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--marker-image", required=True)
    run.add_argument("--marker-cache", type=Path, required=True)
    run.add_argument("--marker-weights-manifest", type=Path, required=True)
    run.add_argument("--repository-root", type=Path, default=Path.cwd())
    run.add_argument("--marker-timeout-seconds", type=int, default=1800)
    run.add_argument("--marker-device", choices=("cpu", "cuda"), default="cpu")
    run.add_argument(
        "--reading-order-profile",
        choices=("backend_preserved", "layout_admitted_v1"),
        default="backend_preserved",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
