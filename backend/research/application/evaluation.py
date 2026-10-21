"""Application boundary for Research RAG dataset validation and publication.

This service composes a byte-returning read port with the pure protocol codec.
It intentionally does not implement a formal runner, CLI, filesystem adapter,
or reference-bearing RAG execution port.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from typing import Any

from pydantic import ValidationError

from backend.research.ports.evaluation_dataset import (
    DatasetBundleBytes,
    DatasetObjectDigests,
    DatasetPublicationParent,
    DatasetPublicationReceipt,
    DatasetPublishPort,
    DatasetReadPort,
    DatasetRevision,
    DatasetRevisionNotFoundError,
)
from backend.research.ports.evaluation_sources import (
    CorpusSnapshotRevision,
    EvidenceMappingReadPort,
    EvidenceMappingRevision,
    Sha256BlobIdentity,
    SourceCorpusReadPort,
)
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_corpus import (
    CorpusContractError,
    CorpusMaterialBytes,
    CorpusMaterialIdentity,
    SourceCorpusManifest,
    verify_source_corpus,
)
from backend.research.rag.evaluation.protocol_evidence_mapping import (
    EvidenceMappingDataError,
    IndexEvidenceMapping,
    mapping_from_json,
)
from backend.research.rag.evaluation.protocol_dataset_io import (
    BlindCaseInput,
    DatasetBundle,
    DatasetLoadResult,
    DatasetValidationIssue,
    DatasetValidationMode,
    DatasetValidationReport,
    ensure_frozen_revision_is_not_replaced,
    load_dataset_bundle,
    serialize_dataset_bundle,
)


@dataclass(frozen=True, slots=True)
class EvaluationDatasetValidationResult:
    """Dataset validation plus optional external corpus/mapping preflight."""

    dataset_result: DatasetLoadResult
    report: DatasetValidationReport
    diagnostics: dict[str, Any]
    preflight_status: str
    preflight_complete: bool

    @property
    def bundle(self) -> DatasetBundle | None:
        return self.dataset_result.bundle


class EvaluationApplicationService:
    """Read and validate dataset revisions without starting an evaluation run."""

    def __init__(
        self,
        *,
        dataset_reader: DatasetReadPort,
        dataset_publisher: DatasetPublishPort | None = None,
        source_corpus_reader: SourceCorpusReadPort | None = None,
        evidence_mapping_reader: EvidenceMappingReadPort | None = None,
    ) -> None:
        self._dataset_reader = dataset_reader
        self._dataset_publisher = dataset_publisher
        self._source_corpus_reader = source_corpus_reader
        self._evidence_mapping_reader = evidence_mapping_reader

    def validate_dataset(
        self,
        revision: DatasetRevision,
        *,
        mode: DatasetValidationMode = DatasetValidationMode.DRAFT,
    ) -> DatasetLoadResult:
        objects = self._dataset_reader.read_dataset_objects(revision)
        if type(objects) is not DatasetBundleBytes:
            raise TypeError("DatasetReadPort must return DatasetBundleBytes containing concrete bytes")
        for field_name in ("manifest_json", "questions_jsonl", "references_jsonl"):
            if type(getattr(objects, field_name)) is not bytes:
                raise TypeError(f"DatasetReadPort returned non-bytes {field_name}")
        result = load_dataset_bundle(objects, mode=mode)
        if result.bundle is not None:
            manifest = result.bundle.manifest
            if manifest.dataset_id != revision.dataset_id or manifest.dataset_version != revision.dataset_version:
                # Treat a port returning the wrong immutable revision as a
                # contract error rather than silently validating another one.
                from backend.research.rag.evaluation.protocol_dataset_io import (
                    DatasetLoadResult,
                    DatasetValidationReport,
                )

                issue = DatasetValidationIssue(
                    code="port.revision_mismatch",
                    location="manifest.json",
                    reason="loaded dataset identity does not match the requested revision",
                )
                result = DatasetLoadResult(
                    bundle=None,
                    report=DatasetValidationReport(
                        valid=False,
                        ready_for_execution_data=False,
                        formal_ready=False,
                        issues=(*result.report.issues, issue),
                        readiness_requirements=result.report.readiness_requirements,
                    ),
                )
        return result

    def validate_dataset_preflight(
        self,
        revision: DatasetRevision,
        *,
        mode: DatasetValidationMode = DatasetValidationMode.DRAFT,
        validate_source_corpus: bool = False,
        require_complete_corpus: bool = False,
        validate_mappings: bool = False,
    ) -> EvaluationDatasetValidationResult:
        """Validate dataset bytes and requested external artifacts without execution.

        Source and mapping readers return only concrete bytes.  This method owns
        protocol decoding, identity binding, and structured failure attribution.
        It never downloads ``source_uri`` values, opens caller-supplied paths,
        constructs an index, or starts a model/judge call.
        """

        dataset_result = self.validate_dataset(revision, mode=mode)
        diagnostics = self.describe_dataset(dataset_result)
        report = dataset_result.report
        if dataset_result.bundle is None:
            return EvaluationDatasetValidationResult(
                dataset_result=dataset_result,
                report=report,
                diagnostics=diagnostics,
                preflight_status="invalid",
                preflight_complete=False,
            )

        bundle = dataset_result.bundle
        hard_issues = list(report.issues)
        readiness = list(report.readiness_requirements)
        corpus_manifest: SourceCorpusManifest | None = None
        corpus_disposition = "not_evaluated"
        mapping_disposition = "not_evaluated"

        source_requested = validate_source_corpus or require_complete_corpus
        if source_requested:
            corpus_diagnostics, corpus_manifest, corpus_disposition, corpus_hard = (
                self._validate_source_corpus(bundle)
            )
            diagnostics["source_corpus"] = corpus_diagnostics
            hard_issues.extend(corpus_hard)
            readiness = [
                item for item in readiness
                if item.code != "readiness.source_corpus_validation_required"
            ]
            if corpus_disposition != "verified":
                readiness.append(DatasetValidationIssue(
                    code="readiness.complete_source_corpus_required",
                    location="source_corpus",
                    reason="complete source corpus bytes must be verified before formal execution",
                ))
        else:
            diagnostics["source_corpus"] = {
                "status": "not_evaluated",
                "reason": "no corpus root was supplied",
                "planned_case_count": len(bundle.cases),
            }

        if validate_mappings:
            mapping_diagnostics, mapping_disposition, mapping_hard = self._validate_mappings(
                revision=revision,
                bundle=bundle,
                corpus_manifest=corpus_manifest,
            )
            diagnostics["mapping"] = mapping_diagnostics
            hard_issues.extend(mapping_hard)
            if mapping_disposition != "valid":
                readiness.append(DatasetValidationIssue(
                    code="readiness.valid_evidence_mappings_required",
                    location="mapping",
                    reason="all planned cases require valid, resolved evidence mapping diagnostics",
                ))
        elif diagnostics.get("mapping", {}).get("status") == "not_evaluated":
            diagnostics["mapping"]["actual_index_binding"] = {
                "status": "not_evaluated",
                "reason": "actual index snapshot/configuration and native PaperChunk bytes were not supplied",
            }

        # validate-dataset is an offline preflight.  Even fully verified source
        # and mapping artifacts do not prove actual adapters, an approved
        # budget, execution, scoring, or a quality gate.
        readiness.append(DatasetValidationIssue(
            code="readiness.actual_runtime_and_budget_required",
            location="formal_preflight",
            reason="actual adapters, approved budget, execution, and scoring remain unevaluated",
        ))
        readiness = list(_deduplicate_issues(readiness))
        valid = report.valid and not hard_issues
        source_satisfied = not source_requested or corpus_disposition == "verified"
        mapping_satisfied = not validate_mappings or mapping_disposition == "valid"
        preflight_complete = valid and source_satisfied and mapping_satisfied
        status = (
            "invalid" if not valid
            else "system_failure" if mapping_disposition == "system_failure"
            else "needs_review" if not preflight_complete
            else "validated"
        )
        combined_report = type(report)(
            valid=valid,
            ready_for_execution_data=(
                report.ready_for_execution_data and preflight_complete
            ),
            formal_ready=False,
            issues=tuple(hard_issues),
            readiness_requirements=tuple(readiness),
        )
        return EvaluationDatasetValidationResult(
            dataset_result=dataset_result,
            report=combined_report,
            diagnostics=diagnostics,
            preflight_status=status,
            preflight_complete=preflight_complete,
        )

    def _validate_source_corpus(
        self,
        bundle: DatasetBundle,
    ) -> tuple[dict[str, Any], SourceCorpusManifest | None, str, tuple[Any, ...]]:
        planned_case_ids = tuple(case.case_id for case in bundle.cases)
        if self._source_corpus_reader is None:
            return (
                {
                    "status": "needs_review",
                    "reason": "source corpus validation was requested but no reader is configured",
                    "planned_case_ids": list(planned_case_ids),
                    "case_statuses": [
                        {"case_id": case_id, "disposition": "needs_review"}
                        for case_id in planned_case_ids
                    ],
                },
                None,
                "needs_review",
                (),
            )

        revision = CorpusSnapshotRevision(bundle.manifest.corpus_snapshot_id)
        try:
            raw_manifest = self._source_corpus_reader.read_corpus_manifest(revision)
            if type(raw_manifest) is not bytes:
                raise TypeError("SourceCorpusReadPort must return concrete manifest bytes")
            _decode_strict_json_object(raw_manifest, "corpus manifest")
            corpus = SourceCorpusManifest.model_validate_json(raw_manifest)
            corpus.verify_digest()
        except OSError as exc:
            return _missing_corpus_diagnostics(planned_case_ids, exc), None, "needs_review", ()
        except (TypeError, ValueError, ValidationError, CorpusContractError) as exc:
            issue = DatasetValidationIssue(
                code="corpus.manifest_invalid",
                location="source_corpus/manifest.json",
                reason=_safe_contract_reason(exc, "source corpus manifest is invalid"),
            )
            return (
                {
                    "status": "invalid",
                    "planned_case_ids": list(planned_case_ids),
                    "case_statuses": [
                        {"case_id": case_id, "disposition": "needs_review"}
                        for case_id in planned_case_ids
                    ],
                    "issues": [_dataset_issue_dict(issue)],
                },
                None,
                "invalid",
                (issue,),
            )

        material_objects: list[CorpusMaterialBytes] = []
        read_errors: list[dict[str, Any]] = []
        cache: dict[str, bytes] = {}
        for entry in corpus.sources:
            for descriptor in entry.materials:
                try:
                    content = cache.get(descriptor.sha256)
                    if content is None:
                        content = self._source_corpus_reader.read_corpus_material(
                            revision,
                            Sha256BlobIdentity(descriptor.sha256),
                        )
                        if type(content) is not bytes:
                            raise TypeError("SourceCorpusReadPort must return concrete material bytes")
                        cache[descriptor.sha256] = content
                    material_objects.append(CorpusMaterialBytes(
                        identity=CorpusMaterialIdentity(
                            paper_id=entry.source.paper_id,
                            document_version=entry.source.document_version,
                            source_sha256=entry.source.source_sha256,
                            material_id=descriptor.material_id,
                            kind=descriptor.kind,
                        ),
                        content_bytes=content,
                    ))
                except OSError as exc:
                    read_errors.append(_storage_issue_dict(exc, descriptor.material_id))

        scopes = {case.case_id: case.scope for case in bundle.cases}
        try:
            verification = verify_source_corpus(
                corpus=corpus,
                dataset=bundle.manifest,
                question_scopes=scopes,
                material_objects=tuple(material_objects),
            )
        except CorpusContractError as exc:
            issue = DatasetValidationIssue(
                code="corpus.binding_invalid",
                location="source_corpus",
                reason=_safe_contract_reason(exc, "source corpus binding is invalid"),
            )
            return (
                {
                    "status": "invalid",
                    "corpus_snapshot_id": corpus.corpus_snapshot_id,
                    "corpus_snapshot_digest": corpus.corpus_snapshot_digest,
                    "planned_case_ids": list(planned_case_ids),
                    "case_statuses": [
                        {"case_id": case_id, "disposition": "needs_review"}
                        for case_id in planned_case_ids
                    ],
                    "issues": [_dataset_issue_dict(issue), *read_errors],
                },
                corpus,
                "invalid",
                (issue,),
            )

        issues = [
            {
                "code": item.code,
                "disposition": item.disposition,
                "location": item.location,
                "reason": item.message,
                "case_ids": list(item.case_ids),
            }
            for item in verification.issues
        ]
        diagnostics = {
            "status": verification.disposition,
            "schema_version": corpus.schema_version,
            "protocol_version": corpus.protocol_version,
            "corpus_snapshot_id": corpus.corpus_snapshot_id,
            "corpus_snapshot_digest": corpus.corpus_snapshot_digest,
            "source_count": len(corpus.sources),
            "distractor_source_count": sum(entry.role == "distractor" for entry in corpus.sources),
            "declared_material_count": sum(len(entry.materials) for entry in corpus.sources),
            "verified_material_count": len(verification.verified_materials),
            "planned_case_ids": list(verification.planned_case_ids),
            "case_statuses": [item.model_dump(mode="json") for item in verification.case_statuses],
            "issues": [*read_errors, *issues],
        }
        return diagnostics, corpus, verification.disposition, ()

    def _validate_mappings(
        self,
        *,
        revision: DatasetRevision,
        bundle: DatasetBundle,
        corpus_manifest: SourceCorpusManifest | None,
    ) -> tuple[dict[str, Any], str, tuple[Any, ...]]:
        cases = {case.case_id: case for case in bundle.cases}
        if self._evidence_mapping_reader is None:
            return (
                {
                    "status": "needs_review",
                    "reason": "mapping validation was requested but no reader is configured",
                    "planned_case_ids": list(cases),
                    "case_statuses": [
                        {"case_id": case_id, "disposition": "needs_review"}
                        for case_id in cases
                    ],
                    "actual_index_binding": _actual_index_not_evaluated(),
                },
                "needs_review",
                (),
            )

        hard_issues: list[Any] = []
        case_diagnostics: list[dict[str, Any]] = []
        dispositions: list[str] = []
        for case_id, case in cases.items():
            try:
                raw = self._evidence_mapping_reader.read_evidence_mapping(
                    EvidenceMappingRevision(revision, case_id)
                )
                if type(raw) is not bytes:
                    raise TypeError("EvidenceMappingReadPort must return concrete bytes")
                mapping = mapping_from_json(raw)
            except OSError as exc:
                dispositions.append("needs_review")
                case_diagnostics.append({
                    "case_id": case_id,
                    "disposition": "needs_review",
                    "required_evidence_anchor_count": len(case.evidence_anchors),
                    "mapping_anchor_count": None,
                    "issues": [_storage_issue_dict(exc, f"mapping:{case_id}")],
                })
                continue
            except (TypeError, ValueError, ValidationError, EvidenceMappingDataError) as exc:
                issue = DatasetValidationIssue(
                    code="mapping.artifact_invalid",
                    location=f"mapping/{case_id}",
                    reason=_safe_contract_reason(exc, "evidence mapping artifact is invalid"),
                )
                hard_issues.append(issue)
                dispositions.append("invalid")
                case_diagnostics.append({
                    "case_id": case_id,
                    "disposition": "invalid",
                    "required_evidence_anchor_count": len(case.evidence_anchors),
                    "mapping_anchor_count": None,
                    "issues": [_dataset_issue_dict(issue)],
                })
                continue

            binding_errors = _mapping_binding_errors(
                mapping=mapping,
                case=case,
                bundle=bundle,
                corpus_manifest=corpus_manifest,
            )
            if binding_errors:
                issue = DatasetValidationIssue(
                    code="mapping.binding_stale",
                    location=f"mapping/{case_id}",
                    reason="; ".join(binding_errors),
                )
                hard_issues.append(issue)
                dispositions.append("invalid")
                case_diagnostics.append({
                    "case_id": case_id,
                    "disposition": "invalid",
                    "mapping_digest": mapping.mapping_digest,
                    "required_evidence_anchor_count": len(case.evidence_anchors),
                    "mapping_anchor_count": len(mapping.anchor_mappings),
                    "issues": [_dataset_issue_dict(issue)],
                })
                continue

            anchor_status_counts = Counter(item.status for item in mapping.anchor_mappings)
            anchor_disposition_counts = Counter(item.disposition for item in mapping.anchor_mappings)
            disposition = (
                "system_failure" if anchor_disposition_counts["system_failure"]
                else "needs_review" if anchor_disposition_counts["needs_review"]
                else "valid"
            )
            dispositions.append(disposition)
            mapping_issues = [
                {
                    "code": f"mapping.anchor_{anchor.status}",
                    "disposition": anchor.disposition,
                    "location": f"mapping/{case_id}/anchors/{anchor.anchor_id}",
                    "reason": anchor.reason or "mapping status recorded by the verified artifact",
                    "case_ids": [case_id],
                    "anchor_ids": [anchor.anchor_id],
                }
                for anchor in mapping.anchor_mappings
                if anchor.disposition != "valid"
            ]
            case_diagnostics.append({
                "case_id": case_id,
                "disposition": disposition,
                "schema_version": mapping.schema_version,
                "protocol_version": mapping.protocol_version,
                "mapping_version": mapping.mapping_version,
                "mapping_digest": mapping.mapping_digest,
                "dataset_binding": "validated",
                "corpus_binding": "validated",
                "case_binding": "validated",
                "anchor_binding": "validated",
                "group_binding": "validated",
                "required_evidence_anchor_count": len(case.evidence_anchors),
                "mapping_anchor_count": len(mapping.anchor_mappings),
                "required_evidence_group_count": len(case.required_evidence_groups),
                "mapping_group_count": len(mapping.required_evidence_groups),
                "anchor_status_counts": _sorted_counts(anchor_status_counts),
                "anchor_disposition_counts": _sorted_counts(anchor_disposition_counts),
                "issues": mapping_issues,
            })

        overall = (
            "invalid" if "invalid" in dispositions
            else "system_failure" if "system_failure" in dispositions
            else "needs_review" if "needs_review" in dispositions
            else "valid"
        )
        return (
            {
                "status": overall,
                "planned_case_ids": list(cases),
                "case_statuses": [
                    {"case_id": item["case_id"], "disposition": item["disposition"]}
                    for item in case_diagnostics
                ],
                "cases": case_diagnostics,
                "actual_index_binding": _actual_index_not_evaluated(),
            },
            overall,
            tuple(hard_issues),
        )

    def publish_dataset(
        self,
        bundle: DatasetBundle,
        *,
        parent: DatasetPublicationParent | None = None,
    ) -> DatasetPublicationReceipt:
        """Validate/serialize an already frozen bundle, then publish it once.

        This operation does not author, annotate, approve, authenticate reviewers, or freeze anything.
        Frozen data readiness is enough for storage publication; the independent
        source-corpus qualification remains required before a formal RAG run.
        """

        if self._dataset_publisher is None:
            raise RuntimeError("dataset publication requires an injected DatasetPublishPort")
        if type(bundle) is not DatasetBundle:
            raise TypeError("publication requires an existing DatasetBundle")
        objects = serialize_dataset_bundle(bundle.manifest, bundle.cases)
        candidate = load_dataset_bundle(objects, mode=DatasetValidationMode.FROZEN).require_bundle()
        if candidate != bundle:
            raise ValueError("bundle physical projections do not match its validated logical cases")
        manifest = candidate.manifest
        revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
        if manifest.frozen_at < manifest.review_manifest.reviewed_at or any(
            manifest.frozen_at < case.review.reviewed_at for case in candidate.cases
        ):
            raise ValueError("freeze timestamp must not precede the declared approvals")

        # This preflight is not the concurrency arbiter. The publication port's
        # atomic no-replace commit remains authoritative for competing writers.
        try:
            existing = self.validate_dataset(revision, mode=DatasetValidationMode.FROZEN).require_bundle()
        except DatasetRevisionNotFoundError:
            existing = None
        if existing is not None:
            ensure_frozen_revision_is_not_replaced(existing, candidate)

        change_scope: tuple[str, ...] = ()
        if manifest.supersedes is None:
            if parent is not None:
                raise ValueError("an initial release must not claim a correction parent")
        else:
            if type(parent) is not DatasetPublicationParent:
                raise ValueError("correction publication requires a pinned parent revision, digest and evidence")
            if (
                parent.revision.dataset_id != revision.dataset_id
                or parent.revision.dataset_version != manifest.supersedes
            ):
                raise ValueError("correction parent does not match the manifest supersedes identity")
            previous = self.validate_dataset(parent.revision, mode=DatasetValidationMode.FROZEN).require_bundle()
            ensure_frozen_revision_is_not_replaced(previous, candidate)
            if parent.revision == revision:
                raise ValueError("corrections must create a new dataset_version")
            if previous.manifest.dataset_digest != parent.dataset_digest:
                raise ValueError("correction parent digest does not match the frozen revision")
            if manifest.dataset_digest == parent.dataset_digest:
                raise ValueError("corrections must create a new dataset_digest")
            _validate_correction(previous, candidate, parent)
            change_scope = _correction_change_scope(previous, candidate)

        receipt = self._dataset_publisher.publish_dataset_objects(
            revision,
            objects,
            dataset_digest=manifest.dataset_digest,
            parent=parent,
            change_scope=change_scope,
        )
        if type(receipt) is not DatasetPublicationReceipt:
            raise TypeError("DatasetPublishPort must return a strict DatasetPublicationReceipt")
        if (
            receipt.revision != revision
            or receipt.dataset_digest != manifest.dataset_digest
            or receipt.object_digests != DatasetObjectDigests.from_objects(objects)
            or receipt.parent != parent
            or receipt.change_scope != change_scope
        ):
            raise ValueError("publication receipt does not match the requested dataset bytes and lineage")
        return receipt

    @staticmethod
    def describe_dataset(result: DatasetLoadResult) -> dict[str, Any]:
        """Project validation diagnostics for interface serialization.

        This is a deterministic application projection. It reports only the
        three-object dataset contract and deliberately leaves source-to-chunk
        mapping qualification to its independent artifact validation.
        """

        bundle = result.bundle
        if bundle is None:
            return _unavailable_diagnostics()

        manifest = bundle.manifest
        case_split_counts = Counter(entry.split for entry in manifest.case_manifest)
        group_split_counts = Counter(
            group.split for group in manifest.split_manifest.groups
        )
        completeness_counts = Counter(
            source.completeness for source in manifest.paper_versions
        )
        access_counts = Counter(
            source.access_status for source in manifest.paper_versions
        )
        format_counts = Counter(
            source.source_format for source in manifest.paper_versions
        )
        review_counts = Counter(
            entry.review_status for entry in manifest.case_manifest
        )
        question_type_counts = Counter(case.question_type for case in bundle.cases)
        answerability_counts = Counter(case.answerability for case in bundle.cases)

        return {
            "schema": {
                "status": "validated",
                "record_type": manifest.record_type,
                "schema_version": manifest.schema_version,
                "protocol_version": manifest.protocol_version,
            },
            "review": {
                "status": manifest.review_manifest.status,
                "reviewer_kind": manifest.review_manifest.reviewer_kind,
                "reviewed_at": _json_value(manifest.review_manifest.reviewed_at),
                "corpus_checked": manifest.review_manifest.corpus_checked,
                "split_checked": manifest.review_manifest.split_checked,
                "contract_checked": manifest.review_manifest.contract_checked,
                "approved_case_count": len(
                    manifest.review_manifest.approved_case_ids
                ),
                "case_status_counts": _sorted_counts(review_counts),
            },
            "materials": {
                "corpus_snapshot_id": manifest.corpus_snapshot_id,
                "source_count": len(manifest.paper_versions),
                "completeness_counts": _sorted_counts(completeness_counts),
                "access_status_counts": _sorted_counts(access_counts),
                "source_format_counts": _sorted_counts(format_counts),
            },
            "split": {
                "method": manifest.split_manifest.method,
                "method_version": manifest.split_manifest.method_version,
                "paper_group_count": len(manifest.split_manifest.groups),
                "group_counts": _sorted_counts(group_split_counts),
                "case_counts": _sorted_counts(case_split_counts),
            },
            "summary": {
                "lifecycle_state": manifest.lifecycle_state,
                "release_purpose": manifest.release_purpose,
                "case_count": len(bundle.cases),
                "question_record_count": len(bundle.question_records),
                "reference_record_count": len(bundle.reference_records),
                "question_type_counts": _sorted_counts(question_type_counts),
                "answerability_counts": _sorted_counts(answerability_counts),
            },
            "digests": {
                "dataset_digest": manifest.dataset_digest,
                "corpus_snapshot_digest": manifest.corpus_snapshot_digest,
                "case_digest_count": len(manifest.case_manifest),
            },
            "mapping": {
                "status": "not_evaluated",
                "source_anchor_count": sum(
                    len(case.evidence_anchors) for case in bundle.cases
                ),
                "cases_with_source_anchors": sum(
                    bool(case.evidence_anchors) for case in bundle.cases
                ),
                "corpus_snapshot_id": manifest.corpus_snapshot_id,
                "reason": (
                    "source-to-chunk mapping artifacts are outside the "
                    "three-object dataset contract and require independent "
                    "validation"
                ),
            },
        }

    @staticmethod
    def blind_inputs(bundle: DatasetBundle) -> tuple[BlindCaseInput, ...]:
        """Project clean inputs; references remain owned by evaluator duties."""

        return bundle.blind_inputs()


def _decode_strict_json_object(raw: bytes, label: str) -> dict[str, Any]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} must be concrete bytes")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label} contains duplicate JSON field {key!r}")
            value[key] = item
        return value

    def reject_constant(value: str) -> Any:
        raise ValueError(f"{label} contains non-standard JSON constant {value!r}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if type(value) is not dict:
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _missing_corpus_diagnostics(
    planned_case_ids: tuple[str, ...],
    exc: OSError,
) -> dict[str, Any]:
    return {
        "status": "needs_review",
        "planned_case_ids": list(planned_case_ids),
        "case_statuses": [
            {"case_id": case_id, "disposition": "needs_review"}
            for case_id in planned_case_ids
        ],
        "issues": [_storage_issue_dict(exc, "source_corpus")],
    }


def _storage_issue_dict(exc: OSError, fallback_location: str) -> dict[str, Any]:
    code = getattr(exc, "code", "evaluation_source.read_failed")
    location = getattr(exc, "location", fallback_location)
    reason = getattr(exc, "reason", "requested evaluation source bytes are unavailable")
    if not isinstance(code, str) or not code:
        code = "evaluation_source.read_failed"
    if not isinstance(location, str) or not location:
        location = fallback_location
    if not isinstance(reason, str) or not reason:
        reason = "requested evaluation source bytes are unavailable"
    return {
        "code": code,
        "disposition": "needs_review",
        "location": location,
        "reason": reason,
    }


def _dataset_issue_dict(issue: Any) -> dict[str, str]:
    return {"code": issue.code, "location": issue.location, "reason": issue.reason}


def _safe_contract_reason(exc: Exception, fallback: str) -> str:
    message = str(exc).strip()
    if not message or len(message) > 1000 or any(separator in message for separator in ("F:\\", "C:\\", "/tmp/")):
        return fallback
    return message


def _actual_index_not_evaluated() -> dict[str, str]:
    return {
        "status": "not_evaluated",
        "reason": (
            "expected parser/chunker/index configuration, serialized index identity, "
            "and native PaperChunk content were not supplied; stored mapping strings "
            "are not accepted as proof of the actual index"
        ),
    }


def _mapping_binding_errors(
    *,
    mapping: IndexEvidenceMapping,
    case: Any,
    bundle: DatasetBundle,
    corpus_manifest: SourceCorpusManifest | None,
) -> list[str]:
    manifest = bundle.manifest
    errors: list[str] = []
    expected_identity = (
        manifest.dataset_id,
        manifest.dataset_version,
        manifest.dataset_digest,
        case.case_id,
        case.case_digest,
        case.answerability,
        manifest.corpus_snapshot_id,
    )
    actual_identity = (
        mapping.dataset_id,
        mapping.dataset_version,
        mapping.dataset_digest,
        mapping.case_id,
        mapping.case_digest,
        mapping.answerability,
        mapping.binding.corpus_snapshot_id,
    )
    if actual_identity != expected_identity:
        errors.append("dataset/case/corpus identity does not match the frozen dataset")
    if corpus_manifest is not None and mapping.binding.corpus_snapshot_id != corpus_manifest.corpus_snapshot_id:
        errors.append("mapping corpus snapshot does not match the verified source corpus")

    expected_anchors = {
        anchor.anchor_id: (
            anchor.source_sha256,
            content_digest(anchor.location),
            anchor.source_excerpt_digest,
        )
        for anchor in case.evidence_anchors
    }
    actual_anchors = {
        anchor.anchor_id: (
            anchor.anchor_source_sha256,
            anchor.anchor_location_digest,
            anchor.anchor_excerpt_digest,
        )
        for anchor in mapping.anchor_mappings
    }
    if actual_anchors != expected_anchors:
        errors.append("mapping anchors do not exactly bind the case source anchors")

    expected_groups = {
        group.group_id: tuple(
            item.required_anchor_ids for item in group.alternative_sufficient_sets
        )
        for group in case.required_evidence_groups
    }
    actual_groups = {
        group.group_id: group.alternatives
        for group in mapping.required_evidence_groups
    }
    if actual_groups != expected_groups:
        errors.append("mapping evidence groups do not exactly bind the case alternatives")
    return errors


def _deduplicate_issues(issues: list[Any]) -> tuple[Any, ...]:
    unique: dict[tuple[str, str, str], Any] = {}
    for issue in issues:
        unique[(issue.code, issue.location, issue.reason)] = issue
    return tuple(unique.values())


def _validate_correction(
    previous: DatasetBundle,
    candidate: DatasetBundle,
    parent: DatasetPublicationParent,
) -> None:
    old, new = previous.manifest, candidate.manifest
    if parent.evidence_reference != new.review_manifest.evidence_reference:
        raise ValueError("correction evidence must match the declared external re-review approval")
    if parent.evidence_reference == old.review_manifest.evidence_reference:
        raise ValueError("corrections require new external re-review evidence")
    if new.review_manifest.reviewed_at <= old.frozen_at or new.frozen_at <= old.frozen_at:
        raise ValueError("correction approval and freeze must follow the parent freeze")
    if new.release_purpose == "heldout_qualification" and old.release_purpose != "heldout_qualification":
        raise ValueError("a correction must not relabel a development release as independent heldout data")

    old_by_id = {case.case_id: case for case in previous.cases}
    old_by_question = {content_digest(case.question_projection()): case for case in previous.cases}
    for case in candidate.cases:
        retained = old_by_id.get(case.case_id) or old_by_question.get(content_digest(case.question_projection()))
        if retained is None:
            continue
        if retained.split == "dev" and case.split == "heldout":
            raise ValueError("an existing development question cannot become blind heldout through a correction")
        old_events = {content_digest(event) for event in retained.exposure_history.events}
        new_events = {content_digest(event) for event in case.exposure_history.events}
        if not old_events.issubset(new_events):
            raise ValueError("corrections must preserve the recorded exposure history of retained questions")
        before = retained.model_dump(mode="json", exclude={"dataset_version", "case_digest", "review"})
        after = case.model_dump(mode="json", exclude={"dataset_version", "case_digest", "review"})
        if before != after and case.review.reviewed_at <= old.frozen_at:
            raise ValueError("changed cases require a new declared review after the parent freeze")


def _correction_change_scope(previous: DatasetBundle, candidate: DatasetBundle) -> tuple[str, ...]:
    old = previous.manifest.model_dump(mode="json")
    new = candidate.manifest.model_dump(mode="json")
    identity_fields = {
        "dataset_version",
        "dataset_digest",
        "case_manifest",
        "supersedes",
        "correction_reason",
        "frozen_at",
    }
    changes = {
        f"manifest.{field}" for field in new
        if field not in identity_fields and old[field] != new[field]
    }
    before = {case.case_id: case for case in previous.cases}
    after = {case.case_id: case for case in candidate.cases}
    if before.keys() != after.keys():
        changes.add("cases.membership")
    for case_id in before.keys() & after.keys():
        old_case = before[case_id].model_dump(mode="json")
        new_case = after[case_id].model_dump(mode="json")
        changes.update(
            f"cases.{case_id}.{field}" for field in new_case
            if field not in {"dataset_version", "case_digest"} and old_case[field] != new_case[field]
        )
    if not changes:
        raise ValueError("a correction must declare actual content or governance changes")
    return tuple(sorted(changes))


def _unavailable_diagnostics() -> dict[str, Any]:
    unavailable = {
        "status": "unavailable",
        "reason": "dataset contract is invalid",
    }
    return {
        "schema": dict(unavailable),
        "review": dict(unavailable),
        "materials": dict(unavailable),
        "split": dict(unavailable),
        "summary": dict(unavailable),
        "digests": dict(unavailable),
        "mapping": {
            "status": "not_evaluated",
            "reason": (
                "mapping validation requires a valid dataset and separate "
                "mapping artifacts"
            ),
        },
    }


def _sorted_counts(counts: Counter[str]) -> dict[str, int]:
    return {key: counts[key] for key in sorted(counts)}


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else value


__all__ = [
    "EvaluationApplicationService",
    "EvaluationDatasetValidationResult",
]
