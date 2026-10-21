"""Physical codec and validation for frozen Research RAG dataset bundles.

The bundle is deliberately composed from three independent byte objects:
``manifest.json``, ``questions.jsonl``, and ``references.jsonl``.  This module
contains no filesystem writer and never exposes reference-bearing records as a
retrieval/end-to-end input DTO.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Literal, Sequence

from pydantic import ValidationError

from backend.research.ports.evaluation_dataset import DatasetBundleBytes
from backend.research.rag.evaluation.protocol_common import canonical_json_bytes
from backend.research.rag.evaluation.protocol_dataset import (
    DatasetManifest,
    DatasetSplit,
    EvaluationCase,
    ExposureHistory,
    QuestionInput,
    QuestionRecord,
    QuestionType,
    ReferenceRecord,
)


class DatasetValidationMode(str, Enum):
    """The contract level requested by a caller."""

    DRAFT = "draft"
    FROZEN = "frozen"


@dataclass(frozen=True, slots=True)
class DatasetValidationIssue:
    code: str
    location: str
    reason: str


@dataclass(frozen=True, slots=True)
class DatasetValidationReport:
    """Contract validity and formal-run readiness are intentionally separate.

    ``valid`` means that the supplied three-object bundle satisfies the selected
    dataset contract.  ``formal_ready`` additionally requires formal-run inputs
    that are outside this codec, notably a validated source-corpus object.  It
    is never a benchmark quality or publication decision.
    """

    valid: bool
    ready_for_execution_data: bool
    formal_ready: bool
    issues: tuple[DatasetValidationIssue, ...]
    readiness_requirements: tuple[DatasetValidationIssue, ...]


class _StoredQuestionRecord(QuestionRecord):
    """Non-reference case fields needed to reconstruct and validate a case.

    Only ``question_input`` is eligible for projection to an ordinary RAG
    adapter.  The remaining fields are dataset-governance metadata retained by
    the evaluator and are not part of :class:`QuestionInput`.
    """

    question_type: QuestionType
    paper_group_id: str
    split: DatasetSplit
    exposure_history: ExposureHistory


@dataclass(frozen=True, slots=True)
class BlindCaseInput:
    """A clean execution projection with no reference-bearing handle."""

    case_id: str
    question_input: QuestionInput


@dataclass(frozen=True, slots=True)
class DatasetBundle:
    manifest: DatasetManifest
    cases: tuple[EvaluationCase, ...]
    question_records: tuple[QuestionRecord, ...]
    reference_records: tuple[ReferenceRecord, ...]

    def blind_inputs(self) -> tuple[BlindCaseInput, ...]:
        """Return only case identity plus the strict two-field QuestionInput."""

        return tuple(
            BlindCaseInput(case_id=record.case_id, question_input=record.question_input)
            for record in self.question_records
        )


@dataclass(frozen=True, slots=True)
class DatasetLoadResult:
    bundle: DatasetBundle | None
    report: DatasetValidationReport

    def require_bundle(self) -> DatasetBundle:
        if self.bundle is None:
            raise DatasetBundleValidationError(self.report)
        return self.bundle


class DatasetBundleValidationError(ValueError):
    def __init__(self, report: DatasetValidationReport) -> None:
        self.report = report
        summary = "; ".join(f"{issue.location}: {issue.reason}" for issue in report.issues)
        super().__init__(summary or "dataset bundle validation failed")


class FrozenDatasetRevisionConflict(ValueError):
    """Raised when a frozen dataset version would be changed in place."""


class _DuplicateJsonKey(ValueError):
    pass


_FORBIDDEN_QUESTION_KEYS = frozenset(
    {
        "reference_label",
        "reference_answer",
        "reference_claims",
        "required_evidence_groups",
        "evidence_anchors",
        "answerability",
        "expected_behavior",
        "gold_chunk_ids",
        "gold_context",
        "reference_path",
        "reference_callback",
        "answer_book_path",
        "answer_book_callback",
    }
)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant {value!r} is not allowed")


def _decode_json(raw: bytes, *, location: str) -> tuple[Any | None, DatasetValidationIssue | None]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, DatasetValidationIssue("json.invalid_utf8", location, str(exc))
    try:
        return (
            json.loads(
                text,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_non_json_constant,
            ),
            None,
        )
    except _DuplicateJsonKey as exc:
        return None, DatasetValidationIssue("json.duplicate_key", location, str(exc))
    except (json.JSONDecodeError, ValueError) as exc:
        return None, DatasetValidationIssue("json.invalid", location, str(exc))


def _decode_jsonl(raw: bytes, *, object_name: str) -> tuple[list[tuple[int, Any]], list[DatasetValidationIssue]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [], [DatasetValidationIssue("json.invalid_utf8", object_name, str(exc))]
    if not text:
        return [], [DatasetValidationIssue("jsonl.empty", object_name, "JSONL object contains no records")]

    records: list[tuple[int, Any]] = []
    issues: list[DatasetValidationIssue] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        location = f"{object_name}:{line_number}"
        if not line.strip():
            issues.append(DatasetValidationIssue("jsonl.blank_line", location, "blank JSONL records are not allowed"))
            continue
        value, issue = _decode_json(line.encode("utf-8"), location=location)
        if issue is not None:
            issues.append(issue)
        else:
            records.append((line_number, value))
    if not records and not issues:
        issues.append(DatasetValidationIssue("jsonl.empty", object_name, "JSONL object contains no records"))
    return records, issues


def _validation_reason(exc: ValidationError) -> str:
    details: list[str] = []
    for error in exc.errors(include_url=False):
        path = ".".join(str(part) for part in error["loc"])
        details.append(f"{path}: {error['msg']}" if path else error["msg"])
    return "; ".join(details)


def _find_forbidden_question_key(value: Any, *, path: str = "") -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            current = f"{path}.{key}" if path else key
            if key in _FORBIDDEN_QUESTION_KEYS:
                return current
            found = _find_forbidden_question_key(nested, path=current)
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found = _find_forbidden_question_key(nested, path=f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _identity_issue(
    *,
    record: QuestionRecord | ReferenceRecord,
    manifest: DatasetManifest,
    location: str,
) -> DatasetValidationIssue | None:
    if record.dataset_id != manifest.dataset_id or record.dataset_version != manifest.dataset_version:
        return DatasetValidationIssue(
            "record.dataset_identity_mismatch",
            location,
            "record dataset_id/dataset_version does not match manifest",
        )
    if record.schema_version != manifest.schema_version or record.protocol_version != manifest.protocol_version:
        return DatasetValidationIssue(
            "record.protocol_identity_mismatch",
            location,
            "record schema_version/protocol_version does not match manifest",
        )
    return None


def _ordered_case_ids(records: Sequence[QuestionRecord | ReferenceRecord]) -> list[str]:
    return [record.case_id for record in records]


def _duplicate_case_issues(
    records: Sequence[QuestionRecord | ReferenceRecord], *, object_name: str
) -> list[DatasetValidationIssue]:
    seen: set[str] = set()
    issues: list[DatasetValidationIssue] = []
    for index, record in enumerate(records, start=1):
        if record.case_id in seen:
            issues.append(
                DatasetValidationIssue(
                    "record.duplicate_case_id",
                    f"{object_name}:{index}",
                    f"duplicate case_id {record.case_id!r}",
                )
            )
        seen.add(record.case_id)
    return issues


def _build_case(
    question: _StoredQuestionRecord,
    reference: ReferenceRecord,
) -> EvaluationCase:
    label = reference.reference_label
    return EvaluationCase.model_validate(
        {
            "record_type": "case",
            "schema_version": question.schema_version,
            "protocol_version": question.protocol_version,
            "dataset_id": question.dataset_id,
            "dataset_version": question.dataset_version,
            "case_id": question.case_id,
            "case_digest": question.case_digest,
            "question_text": question.question_input.question_text,
            "scope": question.question_input.scope,
            "question_type": question.question_type,
            "paper_group_id": question.paper_group_id,
            "split": question.split,
            "answerability": label.answerability,
            "expected_behavior": label.expected_behavior,
            "reference_answer": label.reference_answer,
            "reference_claims": label.reference_claims,
            "evidence_anchors": label.evidence_anchors,
            "required_evidence_groups": label.required_evidence_groups,
            "provenance": label.provenance,
            "exposure_history": question.exposure_history,
            "review": label.review,
        }
    )


def load_dataset_bundle(
    objects: DatasetBundleBytes,
    *,
    mode: DatasetValidationMode = DatasetValidationMode.DRAFT,
) -> DatasetLoadResult:
    """Decode and validate a three-object dataset bundle without side effects."""

    issues: list[DatasetValidationIssue] = []
    manifest_value, manifest_issue = _decode_json(objects.manifest_json, location="manifest.json")
    manifest: DatasetManifest | None = None
    if manifest_issue is not None:
        issues.append(manifest_issue)
    elif not isinstance(manifest_value, dict):
        issues.append(DatasetValidationIssue("manifest.not_object", "manifest.json", "manifest must be a JSON object"))
    else:
        try:
            manifest = DatasetManifest.model_validate_json(canonical_json_bytes(manifest_value))
        except ValidationError as exc:
            issues.append(DatasetValidationIssue("manifest.invalid", "manifest.json", _validation_reason(exc)))

    question_values, question_parse_issues = _decode_jsonl(objects.questions_jsonl, object_name="questions.jsonl")
    reference_values, reference_parse_issues = _decode_jsonl(objects.references_jsonl, object_name="references.jsonl")
    issues.extend(question_parse_issues)
    issues.extend(reference_parse_issues)

    questions: list[_StoredQuestionRecord] = []
    for line_number, value in question_values:
        location = f"questions.jsonl:{line_number}"
        if not isinstance(value, dict):
            issues.append(DatasetValidationIssue("record.not_object", location, "question record must be a JSON object"))
            continue
        leaked_key = _find_forbidden_question_key(value)
        if leaked_key is not None:
            issues.append(
                DatasetValidationIssue(
                    "question.reference_leak",
                    f"{location}.{leaked_key}",
                    "reference-bearing content is forbidden in questions.jsonl",
                )
            )
            continue
        try:
            questions.append(_StoredQuestionRecord.model_validate_json(canonical_json_bytes(value)))
        except ValidationError as exc:
            issues.append(DatasetValidationIssue("question.invalid", location, _validation_reason(exc)))

    references: list[ReferenceRecord] = []
    for line_number, value in reference_values:
        location = f"references.jsonl:{line_number}"
        if not isinstance(value, dict):
            issues.append(DatasetValidationIssue("record.not_object", location, "reference record must be a JSON object"))
            continue
        try:
            references.append(ReferenceRecord.model_validate_json(canonical_json_bytes(value)))
        except ValidationError as exc:
            issues.append(DatasetValidationIssue("reference.invalid", location, _validation_reason(exc)))

    issues.extend(_duplicate_case_issues(questions, object_name="questions.jsonl"))
    issues.extend(_duplicate_case_issues(references, object_name="references.jsonl"))

    cases: list[EvaluationCase] = []
    if manifest is not None:
        for index, record in enumerate(questions, start=1):
            mismatch = _identity_issue(record=record, manifest=manifest, location=f"questions.jsonl:{index}")
            if mismatch is not None:
                issues.append(mismatch)
        for index, record in enumerate(references, start=1):
            mismatch = _identity_issue(record=record, manifest=manifest, location=f"references.jsonl:{index}")
            if mismatch is not None:
                issues.append(mismatch)

        expected_order = [entry.case_id for entry in manifest.case_manifest]
        question_order = _ordered_case_ids(questions)
        reference_order = _ordered_case_ids(references)
        if question_order != expected_order:
            issues.append(
                DatasetValidationIssue(
                    "bundle.question_order_mismatch",
                    "questions.jsonl",
                    f"case order must match manifest; expected={expected_order}, actual={question_order}",
                )
            )
        if reference_order != expected_order:
            issues.append(
                DatasetValidationIssue(
                    "bundle.reference_order_mismatch",
                    "references.jsonl",
                    f"case order must match manifest; expected={expected_order}, actual={reference_order}",
                )
            )

        question_by_id = {record.case_id: record for record in questions}
        reference_by_id = {record.case_id: record for record in references}
        for entry in manifest.case_manifest:
            question = question_by_id.get(entry.case_id)
            reference = reference_by_id.get(entry.case_id)
            if question is None or reference is None:
                missing = []
                if question is None:
                    missing.append("question")
                if reference is None:
                    missing.append("reference")
                issues.append(
                    DatasetValidationIssue(
                        "bundle.missing_member",
                        f"case_manifest.{entry.case_id}",
                        f"missing {' and '.join(missing)} record",
                    )
                )
                continue
            if question.case_digest != reference.case_digest:
                issues.append(
                    DatasetValidationIssue(
                        "bundle.case_digest_mismatch",
                        f"case_manifest.{entry.case_id}",
                        "question and reference records disagree on case_digest",
                    )
                )
                continue
            if question.case_digest != entry.case_digest:
                issues.append(
                    DatasetValidationIssue(
                        "bundle.case_digest_mismatch",
                        f"case_manifest.{entry.case_id}",
                        "physical records do not match the manifest case_digest",
                    )
                )
                continue
            if question.question_digest != entry.question_digest:
                issues.append(
                    DatasetValidationIssue(
                        "bundle.question_digest_mismatch",
                        f"case_manifest.{entry.case_id}",
                        "question record does not match the manifest question_digest",
                    )
                )
                continue
            if reference.reference_digest != entry.reference_digest:
                issues.append(
                    DatasetValidationIssue(
                        "bundle.reference_digest_mismatch",
                        f"case_manifest.{entry.case_id}",
                        "reference record does not match the manifest reference_digest",
                    )
                )
                continue
            if question.paper_group_id != entry.paper_group_id or question.split != entry.split:
                issues.append(
                    DatasetValidationIssue(
                        "bundle.group_split_mismatch",
                        f"case_manifest.{entry.case_id}",
                        "question governance metadata does not match manifest group/split",
                    )
                )
                continue
            try:
                cases.append(_build_case(question, reference))
            except ValidationError as exc:
                issues.append(
                    DatasetValidationIssue(
                        "bundle.case_invalid",
                        f"case_manifest.{entry.case_id}",
                        _validation_reason(exc),
                    )
                )

        if len(cases) == len(manifest.case_manifest):
            try:
                manifest.validate_cases(cases)
            except ValueError as exc:
                issues.append(DatasetValidationIssue("bundle.manifest_case_mismatch", "manifest.json.case_manifest", str(exc)))

        if mode is DatasetValidationMode.FROZEN and manifest.lifecycle_state != "frozen":
            issues.append(
                DatasetValidationIssue(
                    "requirement.dataset_not_frozen",
                    "manifest.json.lifecycle_state",
                    "frozen validation requires lifecycle_state='frozen'",
                )
            )

    blocking_issues = tuple(issues)
    bundle: DatasetBundle | None = None
    if not blocking_issues and manifest is not None:
        bundle = DatasetBundle(
            manifest=manifest,
            cases=tuple(cases),
            question_records=tuple(questions),
            reference_records=tuple(references),
        )

    valid = not blocking_issues and bundle is not None
    ready_for_execution_data = bool(
        valid and manifest is not None and manifest.lifecycle_state == "frozen"
    )
    readiness_requirements: list[DatasetValidationIssue] = []
    if valid and not ready_for_execution_data:
        readiness_requirements.append(
            DatasetValidationIssue(
                "readiness.dataset_not_frozen",
                "manifest.json.lifecycle_state",
                "execution data readiness requires a frozen dataset revision",
            )
        )
    if valid:
        readiness_requirements.append(
            DatasetValidationIssue(
                "readiness.source_corpus_validation_required",
                "corpus",
                "formal execution additionally requires independent validation of the complete source-corpus object against corpus_snapshot_id and corpus_snapshot_digest",
            )
        )

    report = DatasetValidationReport(
        valid=valid,
        ready_for_execution_data=ready_for_execution_data,
        formal_ready=ready_for_execution_data and not readiness_requirements,
        issues=tuple(issues),
        readiness_requirements=tuple(readiness_requirements),
    )
    return DatasetLoadResult(bundle=bundle, report=report)


def _jsonl(records: Iterable[Any]) -> bytes:
    return b"".join(canonical_json_bytes(record) + b"\n" for record in records)


def serialize_dataset_bundle(
    manifest: DatasetManifest,
    cases: Sequence[EvaluationCase],
) -> DatasetBundleBytes:
    """Create canonical portable projections in manifest membership order.

    Persistence is intentionally left to an outer composition layer.  The
    serializer validates full logical cases before emitting either projection.
    """

    manifest.verify_digest()
    manifest.validate_cases(cases)
    by_id = {case.case_id: case for case in cases}
    ordered_cases = tuple(by_id[entry.case_id] for entry in manifest.case_manifest)
    question_records = tuple(
        _StoredQuestionRecord(
            **case.question_record().model_dump(mode="python"),
            question_type=case.question_type,
            paper_group_id=case.paper_group_id,
            split=case.split,
            exposure_history=case.exposure_history,
        )
        for case in ordered_cases
    )
    reference_records = tuple(case.reference_record() for case in ordered_cases)
    return DatasetBundleBytes(
        manifest_json=canonical_json_bytes(manifest),
        questions_jsonl=_jsonl(question_records),
        references_jsonl=_jsonl(reference_records),
    )


def ensure_frozen_revision_is_not_replaced(
    existing: DatasetBundle,
    candidate: DatasetBundle,
) -> None:
    """Reject an in-place content change to an already frozen revision."""

    old = existing.manifest
    new = candidate.manifest
    if (
        old.lifecycle_state == "frozen"
        and old.dataset_id == new.dataset_id
        and old.dataset_version == new.dataset_version
        and old.dataset_digest != new.dataset_digest
    ):
        raise FrozenDatasetRevisionConflict(
            f"frozen dataset {old.dataset_id!r} version {old.dataset_version!r} cannot be replaced in place"
        )


__all__ = [
    "BlindCaseInput",
    "DatasetBundle",
    "DatasetBundleValidationError",
    "DatasetLoadResult",
    "DatasetValidationIssue",
    "DatasetValidationMode",
    "DatasetValidationReport",
    "FrozenDatasetRevisionConflict",
    "ensure_frozen_revision_is_not_replaced",
    "load_dataset_bundle",
    "serialize_dataset_bundle",
]


