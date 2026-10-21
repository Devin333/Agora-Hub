"""Complete source-corpus contracts for the Research RAG evaluation protocol.

This module verifies immutable corpus manifests against concrete source bytes.
It deliberately keeps source verification separate from parser/index outcomes:
missing or unverifiable source material is ``needs_review``; loss after a fully
verified source corpus reaches indexing is a ``system_failure``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    ImmutableVersionStr,
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import (
    DatasetManifest,
    QuestionScope,
    SourceRecord,
)
from backend.research.rag.evaluation.protocol_execution import CorpusProvenance

CorpusSourceRole = Literal["question_scope", "distractor"]
CorpusMaterialKind = Literal["primary_source", "fulltext", "attachment"]
CorpusMaterialUse = Literal["source_verification", "retrieval_index", "oracle_context"]
CorpusDisposition = Literal["verified", "needs_review", "system_failure"]
CaseCorpusDisposition = Literal["ready", "needs_review", "system_failure"]
IndexArtifactType = Literal[
    "source_material",
    "question",
    "reference",
    "derived_note",
    "tuning_feedback",
    "gold_snippet",
]

_FORBIDDEN_INDEX_ARTIFACT_TYPES = frozenset(
    {"question", "reference", "derived_note", "tuning_feedback", "gold_snippet"}
)
_LOCAL_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


class CorpusContractError(ValueError):
    """The corpus, dataset, or question-scope identities are incompatible."""


def _wire_json(value: Any) -> str:
    def encode(item: Any) -> Any:
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        raise TypeError(f"value of type {type(item).__name__} is not JSON serializable")

    return json.dumps(value, default=encode, ensure_ascii=False, allow_nan=False)


def sha256_bytes(content: bytes) -> str:
    """Return the lowercase SHA-256 digest of concrete file bytes."""

    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    return hashlib.sha256(content).hexdigest()


def _portable_reference(value: str, field_name: str) -> str:
    if _LOCAL_ABSOLUTE_PATH.match(value) or value.lower().startswith("file:"):
        raise ValueError(f"{field_name} must be a portable artifact reference, not a local path")
    return value


def _source_key(source: SourceRecord) -> tuple[str, str, str]:
    return source.paper_id, source.document_version, source.source_sha256


def _scope_key(scope_source: Any) -> tuple[str, str, str]:
    return scope_source.paper_id, scope_source.document_version, scope_source.source_sha256


class CorpusMaterialDescriptor(ProtocolModel):
    """A declared immutable file in one paper revision's complete inventory."""

    material_id: NonEmptyStr
    kind: CorpusMaterialKind
    artifact_reference: NonEmptyStr
    media_type: NonEmptyStr
    sha256: Sha256Digest
    byte_length: int = Field(gt=0, strict=True)
    required: bool
    uses: tuple[CorpusMaterialUse, ...] = Field(min_length=1)

    @field_validator("artifact_reference")
    @classmethod
    def _portable_artifact_reference(cls, value: str) -> str:
        return _portable_reference(value, "artifact_reference")

    @model_validator(mode="after")
    def _validate_uses(self) -> "CorpusMaterialDescriptor":
        if len(self.uses) != len(set(self.uses)):
            raise ValueError("material uses must not contain duplicate values")
        if self.kind in {"primary_source", "fulltext"} and not self.required:
            raise ValueError("primary_source and fulltext materials must be required")
        if self.kind == "primary_source" and "source_verification" not in self.uses:
            raise ValueError("primary_source must be used for source_verification")
        if self.kind == "fulltext" and not {"source_verification", "retrieval_index"}.issubset(self.uses):
            raise ValueError("fulltext must be used for source_verification and retrieval_index")
        if self.kind == "attachment" and self.required and "retrieval_index" not in self.uses:
            raise ValueError("required attachments must be included in retrieval_index")
        return self


class CorpusSourceEntry(ProtocolModel):
    """One exact ``SourceRecord`` plus its declared primary/fulltext/attachments."""

    source: SourceRecord
    role: CorpusSourceRole
    materials: tuple[CorpusMaterialDescriptor, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _validate_inventory(self) -> "CorpusSourceEntry":
        ids = [material.material_id for material in self.materials]
        if len(ids) != len(set(ids)):
            raise ValueError("source materials must not contain duplicate material_id values")
        kind_digests = [(material.kind, material.sha256) for material in self.materials]
        if len(kind_digests) != len(set(kind_digests)):
            raise ValueError("source materials must not duplicate a kind/content digest pair")
        primary = [item for item in self.materials if item.kind == "primary_source"]
        fulltext = [item for item in self.materials if item.kind == "fulltext"]
        if len(primary) != 1:
            raise ValueError("each source requires exactly one primary_source material")
        if len(fulltext) != 1:
            raise ValueError("each source requires exactly one fulltext material")
        if primary[0].sha256 != self.source.source_sha256:
            raise ValueError("primary_source digest must match SourceRecord.source_sha256")
        if fulltext[0].sha256 != self.source.fulltext_artifact_digest:
            raise ValueError("fulltext digest must match SourceRecord.fulltext_artifact_digest")
        return self


class _SourceCorpusManifestContent(ProtocolModel):
    record_type: Literal["corpus"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    corpus_snapshot_id: ImmutableVersionStr
    inventory_version: ImmutableVersionStr
    sources: tuple[CorpusSourceEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_sources(self) -> "_SourceCorpusManifestContent":
        source_keys = [_source_key(entry.source) for entry in self.sources]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("corpus sources must not contain duplicate paper revisions")
        material_ids = [material.material_id for entry in self.sources for material in entry.materials]
        if len(material_ids) != len(set(material_ids)):
            raise ValueError("material_id values must be unique across the corpus")
        return self


class SourceCorpusManifest(_SourceCorpusManifestContent):
    """Versioned complete source inventory referenced by ``DatasetManifest``."""

    corpus_snapshot_digest: Sha256Digest
    _DIGEST_FIELD: ClassVar[str] = "corpus_snapshot_digest"

    @classmethod
    def create(cls, **content: Any) -> "SourceCorpusManifest":
        if cls._DIGEST_FIELD in content:
            raise ValueError("corpus_snapshot_digest is calculated; do not provide it to create()")
        normalized = _SourceCorpusManifestContent.model_validate_json(_wire_json(content))
        payload = normalized.model_dump(mode="json")
        return cls.model_validate_json(
            _wire_json({**payload, cls._DIGEST_FIELD: content_digest(payload)})
        )

    def digest_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={self._DIGEST_FIELD})

    def calculated_digest(self) -> str:
        return content_digest(self.digest_payload())

    def verify_digest(self) -> None:
        if self.corpus_snapshot_digest != self.calculated_digest():
            raise CorpusContractError("corpus_snapshot_digest does not match manifest content")

    @model_validator(mode="after")
    def _verify_digest(self) -> "SourceCorpusManifest":
        self.verify_digest()
        return self


class CorpusMaterialIdentity(ProtocolModel):
    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_sha256: Sha256Digest
    material_id: NonEmptyStr
    kind: CorpusMaterialKind

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.paper_id, self.document_version, self.source_sha256, self.material_id


class CorpusMaterialBytes(ProtocolModel):
    """Concrete immutable bytes supplied by a caller, never a metadata-only claim."""

    identity: CorpusMaterialIdentity
    content_bytes: bytes = Field(min_length=1)


class CorpusVerificationIssue(ProtocolModel):
    code: NonEmptyStr
    disposition: Literal["needs_review", "system_failure"]
    location: NonEmptyStr
    message: NonEmptyStr
    case_ids: tuple[NonEmptyStr, ...]


class CaseCorpusStatus(ProtocolModel):
    case_id: NonEmptyStr
    disposition: CaseCorpusDisposition


class VerifiedMaterial(ProtocolModel):
    identity: CorpusMaterialIdentity
    sha256: Sha256Digest
    byte_length: int = Field(gt=0, strict=True)
    uses: tuple[CorpusMaterialUse, ...]


class SourceCorpusVerification(ProtocolModel):
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    corpus_snapshot_id: ImmutableVersionStr
    corpus_snapshot_digest: Sha256Digest
    disposition: Literal["verified", "needs_review"]
    planned_case_ids: tuple[NonEmptyStr, ...]
    case_statuses: tuple[CaseCorpusStatus, ...]
    verified_materials: tuple[VerifiedMaterial, ...]
    issues: tuple[CorpusVerificationIssue, ...]

    @property
    def formal_ready(self) -> bool:
        return self.disposition == "verified"


class IndexInputArtifact(ProtocolModel):
    """One concrete index input; source claims are checked against verified bytes."""

    artifact_id: NonEmptyStr
    artifact_type: IndexArtifactType
    content_bytes: bytes = Field(min_length=1)
    source_identity: CorpusMaterialIdentity | None

    @model_validator(mode="after")
    def _validate_source_binding_shape(self) -> "IndexInputArtifact":
        if self.artifact_type == "source_material" and self.source_identity is None:
            raise ValueError("source_material index artifacts require source_identity")
        if self.artifact_type != "source_material" and self.source_identity is not None:
            raise ValueError("non-source index artifacts must not masquerade with source_identity")
        return self


class IndexCorpusVerification(ProtocolModel):
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    corpus_snapshot_id: ImmutableVersionStr
    corpus_snapshot_digest: Sha256Digest
    disposition: CorpusDisposition
    planned_case_ids: tuple[NonEmptyStr, ...]
    case_statuses: tuple[CaseCorpusStatus, ...]
    accepted_artifact_ids: tuple[NonEmptyStr, ...]
    issues: tuple[CorpusVerificationIssue, ...]

    @property
    def formal_ready(self) -> bool:
        return self.disposition == "verified"


def _material_identity(entry: CorpusSourceEntry, material: CorpusMaterialDescriptor) -> CorpusMaterialIdentity:
    return CorpusMaterialIdentity(
        paper_id=entry.source.paper_id,
        document_version=entry.source.document_version,
        source_sha256=entry.source.source_sha256,
        material_id=material.material_id,
        kind=material.kind,
    )


def _sorted_issues(issues: Sequence[CorpusVerificationIssue]) -> tuple[CorpusVerificationIssue, ...]:
    return tuple(sorted(issues, key=lambda issue: (issue.location, issue.code, issue.message)))


def _case_statuses(
    case_ids: Sequence[str], disposition: CaseCorpusDisposition
) -> tuple[CaseCorpusStatus, ...]:
    return tuple(CaseCorpusStatus(case_id=case_id, disposition=disposition) for case_id in case_ids)


def _validate_dataset_and_scopes(
    corpus: SourceCorpusManifest,
    dataset: DatasetManifest,
    question_scopes: Mapping[str, QuestionScope],
) -> tuple[str, ...]:
    corpus.verify_digest()
    dataset.verify_digest()
    if corpus.dataset_id != dataset.dataset_id or corpus.dataset_version != dataset.dataset_version:
        raise CorpusContractError("corpus dataset identity does not match DatasetManifest")
    if corpus.corpus_snapshot_id != dataset.corpus_snapshot_id:
        raise CorpusContractError("corpus_snapshot_id does not match DatasetManifest")
    if corpus.corpus_snapshot_digest != dataset.corpus_snapshot_digest:
        raise CorpusContractError("corpus_snapshot_digest does not match DatasetManifest")

    dataset_sources = {source.model_dump_json(): source for source in dataset.paper_versions}
    corpus_sources = {entry.source.model_dump_json(): entry for entry in corpus.sources}
    if set(dataset_sources) != set(corpus_sources):
        raise CorpusContractError("corpus SourceRecord values must exactly match DatasetManifest.paper_versions")

    planned_case_ids = tuple(item.case_id for item in dataset.case_manifest)
    if set(question_scopes) != set(planned_case_ids) or len(question_scopes) != len(planned_case_ids):
        missing = sorted(set(planned_case_ids) - set(question_scopes))
        unexpected = sorted(set(question_scopes) - set(planned_case_ids))
        raise CorpusContractError(
            f"question scope membership mismatch; missing={missing}, unexpected={unexpected}"
        )
    if any(not isinstance(scope, QuestionScope) for scope in question_scopes.values()):
        raise TypeError("question_scopes values must be QuestionScope instances")

    entry_by_key = {_source_key(entry.source): entry for entry in corpus.sources}
    scoped_keys: set[tuple[str, str, str]] = set()
    groups = {group.paper_group_id: group for group in dataset.split_manifest.groups}
    entries = {item.case_id: item for item in dataset.case_manifest}
    for case_id in planned_case_ids:
        scope = question_scopes[case_id]
        case_entry = entries[case_id]
        group = groups[case_entry.paper_group_id]
        group_keys = {_scope_key(source) for source in group.sources}
        for source in scope.sources:
            key = _scope_key(source)
            corpus_entry = entry_by_key.get(key)
            if corpus_entry is None:
                raise CorpusContractError(f"question {case_id!r} scope contains an unknown source revision")
            if key not in group_keys:
                raise CorpusContractError(f"question {case_id!r} scope contains a source outside its paper group")
            if corpus_entry.role != "question_scope":
                raise CorpusContractError(f"question {case_id!r} scope points at a declared distractor")
            scoped_keys.add(key)

    for key, entry in entry_by_key.items():
        if entry.role == "question_scope" and key not in scoped_keys:
            raise CorpusContractError("question_scope corpus source is not used by any QuestionScope")
        if entry.role == "distractor" and key in scoped_keys:
            raise CorpusContractError("distractor corpus source must not appear in a QuestionScope")
    return planned_case_ids


def verify_source_corpus(
    *,
    corpus: SourceCorpusManifest,
    dataset: DatasetManifest,
    question_scopes: Mapping[str, QuestionScope],
    material_objects: Sequence[CorpusMaterialBytes],
) -> SourceCorpusVerification:
    """Verify the complete manifest against concrete source/attachment bytes.

    Structural identity or illegal scope is rejected with ``CorpusContractError``.
    Missing, inaccessible, wrongly-versioned, duplicated, or digest-mismatched
    source material remains visible as ``needs_review`` and never becomes an
    index/parser failure.
    """

    planned_case_ids = _validate_dataset_and_scopes(corpus, dataset, question_scopes)
    all_case_ids = tuple(planned_case_ids)
    descriptors: dict[tuple[str, str, str, str], tuple[CorpusSourceEntry, CorpusMaterialDescriptor]] = {}
    for entry in corpus.sources:
        for material in entry.materials:
            descriptors[_material_identity(entry, material).key] = (entry, material)

    issues: list[CorpusVerificationIssue] = []
    supplied: dict[tuple[str, str, str, str], CorpusMaterialBytes] = {}
    for index, material_object in enumerate(material_objects):
        key = material_object.identity.key
        location = f"material_objects[{index}]"
        if key in supplied:
            issues.append(
                CorpusVerificationIssue(
                    code="source.duplicate_material_object",
                    disposition="needs_review",
                    location=location,
                    message="the same corpus material was supplied more than once",
                    case_ids=all_case_ids,
                )
            )
            continue
        supplied[key] = material_object
        descriptor_pair = descriptors.get(key)
        if descriptor_pair is None:
            issues.append(
                CorpusVerificationIssue(
                    code="source.unknown_material_or_version",
                    disposition="needs_review",
                    location=location,
                    message="supplied bytes do not identify a declared corpus material revision",
                    case_ids=all_case_ids,
                )
            )
            continue
        _, descriptor = descriptor_pair
        if material_object.identity.kind != descriptor.kind:
            issues.append(
                CorpusVerificationIssue(
                    code="source.material_kind_mismatch",
                    disposition="needs_review",
                    location=location,
                    message="supplied material kind does not match the corpus inventory",
                    case_ids=all_case_ids,
                )
            )

    verified: list[VerifiedMaterial] = []
    for entry_index, entry in enumerate(corpus.sources):
        source_location = f"sources[{entry_index}]"
        if entry.source.completeness != "complete":
            issues.append(
                CorpusVerificationIssue(
                    code="source.completeness_not_verified",
                    disposition="needs_review",
                    location=f"{source_location}.source.completeness",
                    message="source material is not declared complete",
                    case_ids=all_case_ids,
                )
            )
        if entry.source.access_status != "available":
            issues.append(
                CorpusVerificationIssue(
                    code="source.not_available",
                    disposition="needs_review",
                    location=f"{source_location}.source.access_status",
                    message="source material is not available for complete verification",
                    case_ids=all_case_ids,
                )
            )
        for material_index, descriptor in enumerate(entry.materials):
            identity = _material_identity(entry, descriptor)
            material_location = f"{source_location}.materials[{material_index}]"
            material_object = supplied.get(identity.key)
            if material_object is None:
                if descriptor.required:
                    issues.append(
                        CorpusVerificationIssue(
                            code="source.required_material_missing",
                            disposition="needs_review",
                            location=material_location,
                            message="required primary/fulltext/attachment bytes were not supplied",
                            case_ids=all_case_ids,
                        )
                    )
                continue
            if material_object.identity.kind != descriptor.kind:
                continue
            actual_length = len(material_object.content_bytes)
            actual_digest = sha256_bytes(material_object.content_bytes)
            material_valid = True
            if actual_length != descriptor.byte_length:
                material_valid = False
                issues.append(
                    CorpusVerificationIssue(
                        code="source.byte_length_mismatch",
                        disposition="needs_review",
                        location=material_location,
                        message="concrete material byte length does not match the corpus inventory",
                        case_ids=all_case_ids,
                    )
                )
            if actual_digest != descriptor.sha256:
                material_valid = False
                issues.append(
                    CorpusVerificationIssue(
                        code="source.digest_mismatch",
                        disposition="needs_review",
                        location=material_location,
                        message="concrete material bytes do not match the declared SHA-256 digest",
                        case_ids=all_case_ids,
                    )
                )
            if material_valid:
                verified.append(
                    VerifiedMaterial(
                        identity=identity,
                        sha256=actual_digest,
                        byte_length=actual_length,
                        uses=descriptor.uses,
                    )
                )

    sorted_issues = _sorted_issues(issues)
    disposition: Literal["verified", "needs_review"] = "needs_review" if sorted_issues else "verified"
    case_disposition: CaseCorpusDisposition = "needs_review" if sorted_issues else "ready"
    return SourceCorpusVerification(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        corpus_snapshot_id=corpus.corpus_snapshot_id,
        corpus_snapshot_digest=corpus.corpus_snapshot_digest,
        disposition=disposition,
        planned_case_ids=planned_case_ids,
        case_statuses=_case_statuses(planned_case_ids, case_disposition),
        verified_materials=tuple(verified),
        issues=sorted_issues,
    )


def verify_index_corpus(
    *,
    corpus: SourceCorpusManifest,
    dataset: DatasetManifest,
    question_scopes: Mapping[str, QuestionScope],
    material_objects: Sequence[CorpusMaterialBytes],
    artifacts: Sequence[IndexInputArtifact],
) -> IndexCorpusVerification:
    """Validate concrete index inputs and reject evaluation-artifact pollution.

    A source verification problem stays ``needs_review``. Once the source
    corpus is verified, missing/truncated/wrong-scope material or any question,
    reference, derived-note, tuning-feedback, or gold-snippet input is a
    ``system_failure``. All planned cases remain in the returned denominator.
    """

    source_verification = verify_source_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=question_scopes,
        material_objects=material_objects,
    )
    actual_source_objects = {item.identity.key: item for item in material_objects}

    planned_case_ids = source_verification.planned_case_ids
    all_case_ids = tuple(planned_case_ids)
    if not source_verification.formal_ready:
        issue = CorpusVerificationIssue(
            code="index.source_corpus_needs_review",
            disposition="needs_review",
            location="source_verification",
            message="index qualification cannot proceed before complete source bytes are verified",
            case_ids=all_case_ids,
        )
        issues = _sorted_issues((*source_verification.issues, issue))
        return IndexCorpusVerification(
            dataset_id=corpus.dataset_id,
            dataset_version=corpus.dataset_version,
            corpus_snapshot_id=corpus.corpus_snapshot_id,
            corpus_snapshot_digest=corpus.corpus_snapshot_digest,
            disposition="needs_review",
            planned_case_ids=planned_case_ids,
            case_statuses=_case_statuses(planned_case_ids, "needs_review"),
            accepted_artifact_ids=(),
            issues=issues,
        )

    descriptors: dict[tuple[str, str, str, str], CorpusMaterialDescriptor] = {}
    expected_required: set[tuple[str, str, str, str]] = set()
    for entry in corpus.sources:
        for material in entry.materials:
            identity = _material_identity(entry, material)
            descriptors[identity.key] = material
            if material.required and "retrieval_index" in material.uses:
                expected_required.add(identity.key)

    issues: list[CorpusVerificationIssue] = []
    accepted_ids: list[str] = []
    seen_artifact_ids: set[str] = set()
    seen_materials: set[tuple[str, str, str, str]] = set()
    accepted_materials: set[tuple[str, str, str, str]] = set()
    for index, artifact in enumerate(artifacts):
        location = f"artifacts[{index}]"
        if artifact.artifact_id in seen_artifact_ids:
            issues.append(
                CorpusVerificationIssue(
                    code="index.duplicate_artifact_id",
                    disposition="system_failure",
                    location=location,
                    message="index artifact_id values must be unique",
                    case_ids=all_case_ids,
                )
            )
            continue
        seen_artifact_ids.add(artifact.artifact_id)
        if artifact.artifact_type in _FORBIDDEN_INDEX_ARTIFACT_TYPES:
            issues.append(
                CorpusVerificationIssue(
                    code=f"index.forbidden_{artifact.artifact_type}",
                    disposition="system_failure",
                    location=location,
                    message=(
                        "evaluation questions, labels, derived notes, feedback, and gold snippets "
                        "are forbidden index inputs"
                    ),
                    case_ids=all_case_ids,
                )
            )
            continue

        identity = artifact.source_identity
        if identity is None:  # protected by the model validator
            continue
        key = identity.key
        descriptor = descriptors.get(key)
        if descriptor is None:
            issues.append(
                CorpusVerificationIssue(
                    code="index.unknown_source_material",
                    disposition="system_failure",
                    location=location,
                    message="index input is not a declared source material revision",
                    case_ids=all_case_ids,
                )
            )
            continue
        if key in seen_materials:
            issues.append(
                CorpusVerificationIssue(
                    code="index.duplicate_source_material",
                    disposition="system_failure",
                    location=location,
                    message="the same source material was submitted to indexing more than once",
                    case_ids=all_case_ids,
                )
            )
            continue
        seen_materials.add(key)
        if identity.kind != descriptor.kind:
            issues.append(
                CorpusVerificationIssue(
                    code="index.material_kind_mismatch",
                    disposition="system_failure",
                    location=location,
                    message="index source kind does not match the corpus inventory",
                    case_ids=all_case_ids,
                )
            )
            continue
        if "retrieval_index" not in descriptor.uses:
            issues.append(
                CorpusVerificationIssue(
                    code="index.material_outside_usage_scope",
                    disposition="system_failure",
                    location=location,
                    message="source material is real but is not authorized for retrieval indexing",
                    case_ids=all_case_ids,
                )
            )
            continue
        actual_digest = sha256_bytes(artifact.content_bytes)
        original = actual_source_objects.get(key)
        if (
            original is None
            or artifact.content_bytes != original.content_bytes
            or len(artifact.content_bytes) != descriptor.byte_length
            or actual_digest != descriptor.sha256
        ):
            issues.append(
                CorpusVerificationIssue(
                    code="index.source_bytes_mismatch",
                    disposition="system_failure",
                    location=location,
                    message=(
                        "index input bytes are truncated, altered, or metadata-only and do not "
                        "match the verified source"
                    ),
                    case_ids=all_case_ids,
                )
            )
            continue
        accepted_ids.append(artifact.artifact_id)
        accepted_materials.add(key)

    missing = expected_required - accepted_materials
    for key in sorted(missing):
        descriptor = descriptors[key]
        issues.append(
            CorpusVerificationIssue(
                code="index.required_material_missing",
                disposition="system_failure",
                location=f"materials.{descriptor.material_id}",
                message="a required fulltext or attachment was omitted from the index input",
                case_ids=all_case_ids,
            )
        )

    sorted_issues = _sorted_issues(issues)
    disposition: CorpusDisposition = "system_failure" if sorted_issues else "verified"
    case_disposition: CaseCorpusDisposition = "system_failure" if sorted_issues else "ready"
    return IndexCorpusVerification(
        dataset_id=corpus.dataset_id,
        dataset_version=corpus.dataset_version,
        corpus_snapshot_id=corpus.corpus_snapshot_id,
        corpus_snapshot_digest=corpus.corpus_snapshot_digest,
        disposition=disposition,
        planned_case_ids=planned_case_ids,
        case_statuses=_case_statuses(planned_case_ids, case_disposition),
        accepted_artifact_ids=tuple(accepted_ids),
        issues=sorted_issues,
    )


def build_verified_corpus_provenance(
    *,
    corpus: SourceCorpusManifest,
    dataset: DatasetManifest,
    question_scopes: Mapping[str, QuestionScope],
    material_objects: Sequence[CorpusMaterialBytes],
    artifacts: Sequence[IndexInputArtifact],
    index_snapshot_id: str,
    index_content_bytes: bytes,
    evidence_references: Sequence[str],
) -> CorpusProvenance:
    """Verify concrete input again before projecting execution provenance.

    Saved reports or booleans are never accepted as a substitute for source and
    index bytes. This proves the input-corpus contract, not successful parsing
    or an independently verified output index snapshot.
    """

    if not isinstance(index_content_bytes, bytes) or not index_content_bytes:
        raise CorpusContractError("index_content_bytes must contain the concrete serialized index")
    index_verification = verify_index_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=question_scopes,
        material_objects=material_objects,
        artifacts=artifacts,
    )
    if not index_verification.formal_ready:
        raise CorpusContractError(
            f"corpus cannot be projected as complete and clean: {index_verification.disposition}"
        )
    return CorpusProvenance(
        corpus_snapshot_id=corpus.corpus_snapshot_id,
        corpus_content_sha256=corpus.corpus_snapshot_digest,
        source_material="complete_fulltext",
        source_completeness="complete",
        index_snapshot_id=index_snapshot_id,
        index_content_sha256=sha256_bytes(index_content_bytes),
        index_not_applicable_reason=None,
        gold_labels_indexed=False,
        previous_answers_indexed=False,
        evidence_references=tuple(evidence_references),
    )


__all__ = [
    "CaseCorpusDisposition",
    "CaseCorpusStatus",
    "CorpusContractError",
    "CorpusDisposition",
    "CorpusMaterialBytes",
    "CorpusMaterialDescriptor",
    "CorpusMaterialIdentity",
    "CorpusMaterialKind",
    "CorpusMaterialUse",
    "CorpusSourceEntry",
    "CorpusSourceRole",
    "CorpusVerificationIssue",
    "IndexArtifactType",
    "IndexCorpusVerification",
    "IndexInputArtifact",
    "SourceCorpusManifest",
    "SourceCorpusVerification",
    "VerifiedMaterial",
    "build_verified_corpus_provenance",
    "sha256_bytes",
    "verify_index_corpus",
    "verify_source_corpus",
]
