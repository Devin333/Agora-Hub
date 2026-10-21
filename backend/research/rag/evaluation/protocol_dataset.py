"""Strict dataset contracts for the Research RAG evaluation protocol.

The models in this module describe immutable source-anchored cases. They do
not describe index/chunk mappings: those are versioned derivative artifacts.
"""

from __future__ import annotations

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

DatasetLifecycleState = Literal["draft", "annotated", "reviewed", "frozen"]
ReleasePurpose = Literal["legacy_dev_candidate", "pilot_dev", "dev_regression", "heldout_qualification"]
DatasetSplit = Literal["dev", "heldout"]
QuestionType = Literal["method", "experiment", "result", "table", "figure", "equation", "cross_paragraph", "unanswerable", "other"]
Answerability = Literal["answerable", "unanswerable", "needs_review"]
ExpectedBehavior = Literal["answer", "abstain"]
ReviewStatus = Literal["pending", "needs_review", "approved", "rejected"]
ActorKind = Literal["human", "llm", "rule"]
AuthoringMethod = Literal["abstract_only", "real_user", "fulltext_targeted", "template", "legacy_import"]
ExposureStatus = Literal["clean", "exposed", "unknown"]
ExposureSubject = Literal["paper_fulltext", "question", "reference", "score_feedback"]
ExposurePurpose = Literal["normal_corpus_indexing", "question_authoring", "annotation", "prompt_tuning", "retrieval_tuning", "reranker_tuning", "generator_selection", "threshold_tuning", "evaluation", "unknown"]
SourceCompleteness = Literal["complete", "incomplete", "unknown"]
AnchorContentType = Literal["text", "table", "figure", "equation"]
VisibleMaterial = Literal["title", "abstract", "declared_scope", "fulltext", "retrieval_output"]

_TUNING_PURPOSES = frozenset({"prompt_tuning", "retrieval_tuning", "reranker_tuning", "generator_selection", "threshold_tuning"})
_LOCAL_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def _wire_json(value: Any) -> str:
    def encode(item: Any) -> Any:
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        raise TypeError(f"value of type {type(item).__name__} is not JSON serializable")

    return json.dumps(value, default=encode, ensure_ascii=False, allow_nan=False)


def _ensure_unique(values: Sequence[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicate values")


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _reject_local_path(value: str, field_name: str) -> str:
    if _LOCAL_ABSOLUTE_PATH.match(value) or value.lower().startswith("file:"):
        raise ValueError(f"{field_name} must be a portable URI or artifact reference, not a local path")
    return value


class ScopedPaper(ProtocolModel):
    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_sha256: Sha256Digest


class QuestionScope(ProtocolModel):
    """Semantic source scope; tenant, actor, budget and run IDs belong elsewhere."""
    sources: tuple[ScopedPaper, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_sources(self) -> "QuestionScope":
        keys = [(item.paper_id, item.document_version, item.source_sha256) for item in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("scope.sources must not contain duplicate paper revisions")
        return self


class QuestionInput(ProtocolModel):
    """The complete and only DTO allowed into retrieval/end-to-end execution."""
    question_text: NonEmptyStr
    scope: QuestionScope


class AssistanceRecord(ProtocolModel):
    actor_kind: ActorKind
    actor_id: NonEmptyStr
    model_or_rule_version: ImmutableVersionStr | None
    input_scope: tuple[VisibleMaterial, ...] = Field(min_length=1)
    generation_config_digest: Sha256Digest | None

    @model_validator(mode="after")
    def _validate_assistance(self) -> "AssistanceRecord":
        _ensure_unique(self.input_scope, "input_scope")
        if self.actor_kind == "human" and self.model_or_rule_version is not None:
            raise ValueError("human assistance must not claim a model_or_rule_version")
        if self.actor_kind != "human" and self.model_or_rule_version is None:
            raise ValueError("automated assistance requires model_or_rule_version")
        return self


class AuthorInputManifest(ProtocolModel):
    visible_materials: tuple[VisibleMaterial, ...] = Field(min_length=1)
    input_digest: Sha256Digest

    @model_validator(mode="after")
    def _unique_materials(self) -> "AuthorInputManifest":
        _ensure_unique(self.visible_materials, "visible_materials")
        return self


class SourceRecord(ProtocolModel):
    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_uri: NonEmptyStr
    source_sha256: Sha256Digest
    retrieved_at: datetime
    fulltext_artifact_digest: Sha256Digest
    completeness: SourceCompleteness
    access_status: Literal["available", "restricted", "unavailable"]
    source_format: Literal["pdf", "latex", "html", "other"]
    license_reference_id: NonEmptyStr

    @field_validator("source_uri")
    @classmethod
    def _portable_source_uri(cls, value: str) -> str:
        return _reject_local_path(value, "source_uri")

    @field_validator("retrieved_at")
    @classmethod
    def _aware_retrieved_at(cls, value: datetime) -> datetime:
        return _aware(value, "retrieved_at")  # type: ignore[return-value]


class CaseProvenance(ProtocolModel):
    authoring_method: AuthoringMethod
    original_dataset_id: NonEmptyStr | None
    original_case_id: NonEmptyStr | None
    derived_from_case_ids: tuple[NonEmptyStr, ...]
    question_author_id: NonEmptyStr
    author_input_manifest: AuthorInputManifest
    question_digest: Sha256Digest
    question_frozen_at: datetime
    annotator_id: NonEmptyStr
    annotation_input_manifest: AuthorInputManifest
    annotation_started_at: datetime
    assistance: tuple[AssistanceRecord, ...]
    sources: tuple[SourceRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_provenance(self) -> "CaseProvenance":
        _ensure_unique(self.derived_from_case_ids, "derived_from_case_ids")
        _aware(self.question_frozen_at, "question_frozen_at")
        _aware(self.annotation_started_at, "annotation_started_at")
        if self.annotation_started_at <= self.question_frozen_at:
            raise ValueError("annotation_started_at must be after question_frozen_at")
        keys = [(s.paper_id, s.document_version, s.source_sha256) for s in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("provenance.sources must not contain duplicate revisions")
        if self.authoring_method == "abstract_only":
            allowed = {"title", "abstract", "declared_scope"}
            if set(self.author_input_manifest.visible_materials) != allowed:
                raise ValueError("abstract_only authoring must be limited to title, abstract, and declared_scope")
            if self.question_author_id == self.annotator_id:
                raise ValueError("abstract_only authoring requires an independent annotator")
            for assistance in self.assistance:
                if not set(assistance.input_scope).issubset(allowed):
                    raise ValueError("abstract_only assistance exceeded the allowed input scope")
        if "fulltext" not in self.annotation_input_manifest.visible_materials or "declared_scope" not in self.annotation_input_manifest.visible_materials:
            raise ValueError("annotation input must include fulltext and declared_scope")
        return self


class ExposureEvent(ProtocolModel):
    subject: ExposureSubject
    purpose: ExposurePurpose
    exposed_to: NonEmptyStr
    occurred_at: datetime
    evidence_reference: NonEmptyStr | None

    @field_validator("occurred_at")
    @classmethod
    def _aware_occurred_at(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")  # type: ignore[return-value]

    @field_validator("evidence_reference")
    @classmethod
    def _portable_evidence_reference(cls, value: str | None) -> str | None:
        return None if value is None else _reject_local_path(value, "evidence_reference")


class ExposureHistory(ProtocolModel):
    status: ExposureStatus
    events: tuple[ExposureEvent, ...]
    assessed_at: datetime
    assessed_by: NonEmptyStr
    assessment_evidence_reference: NonEmptyStr | None

    @field_validator("assessed_at")
    @classmethod
    def _aware_assessed_at(cls, value: datetime) -> datetime:
        return _aware(value, "assessed_at")  # type: ignore[return-value]

    @field_validator("assessment_evidence_reference")
    @classmethod
    def _portable_assessment_reference(cls, value: str | None) -> str | None:
        return None if value is None else _reject_local_path(value, "assessment_evidence_reference")

    @model_validator(mode="after")
    def _status_matches_events(self) -> "ExposureHistory":
        has_tuning = any(event.purpose in _TUNING_PURPOSES for event in self.events)
        has_unknown = any(event.purpose == "unknown" for event in self.events)
        if self.status == "clean" and (has_tuning or has_unknown):
            raise ValueError("clean exposure status conflicts with tuning or unknown exposure events")
        if self.status == "clean" and self.assessment_evidence_reference is None:
            raise ValueError("clean exposure status requires auditable assessment evidence")
        if self.status == "exposed" and not self.events:
            raise ValueError("exposed status requires at least one exposure event")
        return self


class HumanReview(ProtocolModel):
    status: ReviewStatus
    reviewer_id: NonEmptyStr | None
    reviewer_kind: ActorKind | None
    reviewed_at: datetime | None
    notes: NonEmptyStr | None
    source_completeness: SourceCompleteness
    checked_scope: QuestionScope | None
    unanswerable_reason: NonEmptyStr | None
    evidence_reference: NonEmptyStr | None

    @field_validator("evidence_reference")
    @classmethod
    def _portable_review_evidence(cls, value: str | None) -> str | None:
        return None if value is None else _reject_local_path(value, "evidence_reference")

    @model_validator(mode="after")
    def _validate_review(self) -> "HumanReview":
        _aware(self.reviewed_at, "reviewed_at")
        if self.status == "approved":
            if self.reviewer_kind != "human" or self.reviewer_id is None or self.reviewed_at is None:
                raise ValueError("approved review requires a named human reviewer and reviewed_at")
            if self.source_completeness != "complete" or self.checked_scope is None:
                raise ValueError("approved review requires complete sources and an explicitly checked scope")
            if self.evidence_reference is None:
                raise ValueError("approved review requires an external evidence reference")
        return self


class ReferenceClaim(ProtocolModel):
    claim_id: NonEmptyStr
    claim_text: NonEmptyStr


class EvidenceLocation(ProtocolModel):
    content_type: AnchorContentType
    pdf_page_index: int | None
    printed_page: NonEmptyStr | None
    section: NonEmptyStr | None
    paragraph_id: NonEmptyStr | None
    char_start: int | None = Field(ge=0)
    char_end: int | None = Field(gt=0)
    element_id: NonEmptyStr | None
    table_id: NonEmptyStr | None
    row_labels: tuple[NonEmptyStr, ...]
    column_labels: tuple[NonEmptyStr, ...]
    cell_references: tuple[NonEmptyStr, ...]
    figure_id: NonEmptyStr | None
    caption: NonEmptyStr | None
    normalized_fulltext_sha256: Sha256Digest | None

    @model_validator(mode="after")
    def _validate_location(self) -> "EvidenceLocation":
        if self.pdf_page_index is not None and self.pdf_page_index < 0:
            raise ValueError("pdf_page_index must be non-negative")
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("char_start and char_end must be provided together")
        if self.char_start is not None and self.char_end is not None and self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        _ensure_unique(self.row_labels, "row_labels")
        _ensure_unique(self.column_labels, "column_labels")
        _ensure_unique(self.cell_references, "cell_references")
        if self.content_type == "table":
            has_semantic_cells = bool(self.cell_references) or bool(self.row_labels and self.column_labels)
            if self.table_id is None or not has_semantic_cells:
                raise ValueError("table evidence requires table_id and either cells or both row and column labels")
        if self.content_type == "figure" and self.figure_id is None:
            raise ValueError("figure evidence requires figure_id")
        if self.content_type == "equation" and self.element_id is None:
            raise ValueError("equation evidence requires element_id")
        if (self.paragraph_id is not None or self.char_start is not None) and self.normalized_fulltext_sha256 is None:
            raise ValueError("paragraph and character locators require normalized_fulltext_sha256")
        if not any(value is not None for value in (self.pdf_page_index, self.section, self.paragraph_id, self.char_start, self.element_id, self.table_id, self.figure_id)):
            raise ValueError("evidence location requires at least one reproducible locator")
        return self


class EvidenceAnchor(ProtocolModel):
    anchor_id: NonEmptyStr
    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_sha256: Sha256Digest
    claim_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    location: EvidenceLocation
    source_excerpt: NonEmptyStr
    source_excerpt_digest: Sha256Digest

    @model_validator(mode="after")
    def _validate_anchor(self) -> "EvidenceAnchor":
        _ensure_unique(self.claim_ids, "claim_ids")
        if self.source_excerpt_digest != content_digest(self.source_excerpt):
            raise ValueError("source_excerpt_digest does not match source_excerpt")
        return self


class AlternativeSufficientSet(ProtocolModel):
    set_id: NonEmptyStr
    required_anchor_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_anchor_ids(self) -> "AlternativeSufficientSet":
        _ensure_unique(self.required_anchor_ids, "required_anchor_ids")
        return self


class RequiredEvidenceGroup(ProtocolModel):
    group_id: NonEmptyStr
    claim_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    alternative_sufficient_sets: tuple[AlternativeSufficientSet, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_members(self) -> "RequiredEvidenceGroup":
        _ensure_unique(self.claim_ids, "claim_ids")
        _ensure_unique([item.set_id for item in self.alternative_sufficient_sets], "alternative_sufficient_sets.set_id")
        return self


class ReferenceLabel(ProtocolModel):
    """Privileged answer-book projection for oracle/scoring responsibilities only."""
    answerability: Answerability
    expected_behavior: ExpectedBehavior | None
    reference_answer: NonEmptyStr | None
    reference_claims: tuple[ReferenceClaim, ...]
    evidence_anchors: tuple[EvidenceAnchor, ...]
    required_evidence_groups: tuple[RequiredEvidenceGroup, ...]
    provenance: CaseProvenance
    review: HumanReview


class QuestionRecord(ProtocolModel):
    """Physical questions.jsonl projection of one logical case."""
    record_type: Literal["case"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    case_id: NonEmptyStr
    case_digest: Sha256Digest
    question_digest: Sha256Digest
    question_input: QuestionInput

    @model_validator(mode="after")
    def _verify_question_digest(self) -> "QuestionRecord":
        if self.question_digest != content_digest(self.question_input):
            raise ValueError("question_digest does not match question_input")
        return self


class ReferenceRecord(ProtocolModel):
    """Physical references.jsonl projection of one logical case."""
    record_type: Literal["case"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    case_id: NonEmptyStr
    case_digest: Sha256Digest
    reference_digest: Sha256Digest
    reference_label: ReferenceLabel

    @model_validator(mode="after")
    def _verify_reference_digest(self) -> "ReferenceRecord":
        if self.reference_digest != content_digest(self.reference_label):
            raise ValueError("reference_digest does not match reference_label")
        return self


class _EvaluationCaseContent(ProtocolModel):
    record_type: Literal["case"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    case_id: NonEmptyStr
    question_text: NonEmptyStr
    scope: QuestionScope
    question_type: QuestionType
    paper_group_id: NonEmptyStr
    split: DatasetSplit
    answerability: Answerability
    expected_behavior: ExpectedBehavior | None
    reference_answer: NonEmptyStr | None
    reference_claims: tuple[ReferenceClaim, ...]
    evidence_anchors: tuple[EvidenceAnchor, ...]
    required_evidence_groups: tuple[RequiredEvidenceGroup, ...]
    provenance: CaseProvenance
    exposure_history: ExposureHistory
    review: HumanReview

    @model_validator(mode="after")
    def _validate_case_content(self) -> "_EvaluationCaseContent":
        claim_ids = [claim.claim_id for claim in self.reference_claims]
        anchor_ids = [anchor.anchor_id for anchor in self.evidence_anchors]
        group_ids = [group.group_id for group in self.required_evidence_groups]
        _ensure_unique(claim_ids, "reference_claims.claim_id")
        _ensure_unique(anchor_ids, "evidence_anchors.anchor_id")
        _ensure_unique(group_ids, "required_evidence_groups.group_id")
        claim_id_set, anchor_id_set = set(claim_ids), set(anchor_ids)
        scope_keys = {(s.paper_id, s.document_version, s.source_sha256) for s in self.scope.sources}
        provenance_keys = {(s.paper_id, s.document_version, s.source_sha256) for s in self.provenance.sources}
        if provenance_keys != scope_keys:
            raise ValueError("provenance.sources must exactly match question scope sources")
        sources_by_key = {(s.paper_id, s.document_version, s.source_sha256): s for s in self.provenance.sources}
        for anchor in self.evidence_anchors:
            if not set(anchor.claim_ids).issubset(claim_id_set):
                raise ValueError(f"anchor {anchor.anchor_id!r} references an unknown claim_id")
            source_key = (anchor.paper_id, anchor.document_version, anchor.source_sha256)
            if source_key not in scope_keys:
                raise ValueError(f"anchor {anchor.anchor_id!r} references a source outside question scope")
            source = sources_by_key[source_key]
            if anchor.location.normalized_fulltext_sha256 is not None and anchor.location.normalized_fulltext_sha256 != source.fulltext_artifact_digest:
                raise ValueError(f"anchor {anchor.anchor_id!r} normalized fulltext digest does not match its source artifact")
            if source.source_format == "latex" and (anchor.location.pdf_page_index is not None or anchor.location.printed_page is not None):
                raise ValueError(f"anchor {anchor.anchor_id!r} cannot use PDF page locators for a LaTeX source")
        for group in self.required_evidence_groups:
            if not set(group.claim_ids).issubset(claim_id_set):
                raise ValueError(f"evidence group {group.group_id!r} references an unknown claim_id")
            for sufficient_set in group.alternative_sufficient_sets:
                if not set(sufficient_set.required_anchor_ids).issubset(anchor_id_set):
                    raise ValueError(f"evidence set {sufficient_set.set_id!r} references an unknown anchor_id")
                supported = {claim_id for anchor in self.evidence_anchors if anchor.anchor_id in sufficient_set.required_anchor_ids for claim_id in anchor.claim_ids}
                if not set(group.claim_ids).issubset(supported):
                    raise ValueError(f"evidence set {sufficient_set.set_id!r} does not support every claim in its group")
        if self.provenance.question_digest != content_digest(self.question_projection()):
            raise ValueError("provenance.question_digest does not match the question projection")
        if self.answerability == "answerable":
            if self.expected_behavior != "answer" or self.reference_answer is None:
                raise ValueError("answerable cases require expected_behavior='answer' and a reference_answer")
            if not self.reference_claims or not self.required_evidence_groups or not self.evidence_anchors:
                raise ValueError("answerable cases require claims, evidence anchors, and all-of evidence groups")
            if self.review.status != "approved" or self.review.checked_scope != self.scope:
                raise ValueError("answerable cases require final human approval of the exact question scope")
            grouped_claims = {claim_id for group in self.required_evidence_groups for claim_id in group.claim_ids}
            if grouped_claims != claim_id_set:
                raise ValueError("every answerable reference claim must belong to a required evidence group")
        elif self.answerability == "unanswerable":
            if self.expected_behavior != "abstain" or self.reference_answer is not None:
                raise ValueError("unanswerable cases require expected_behavior='abstain' and explicit null answer")
            if self.required_evidence_groups:
                raise ValueError("unanswerable cases must use an explicit empty evidence-group list")
            if self.review.status != "approved" or self.review.unanswerable_reason is None or self.review.checked_scope != self.scope:
                raise ValueError("unanswerable approval requires human approval, a reason, and the exact checked scope")
        else:
            if self.expected_behavior is not None or self.reference_answer is not None:
                raise ValueError("needs_review cases require explicit null behavior and reference_answer")
            if self.review.status != "needs_review":
                raise ValueError("needs_review answerability requires review.status='needs_review'")
        if self.split == "heldout" and self.review.status == "approved":
            if self.provenance.authoring_method == "legacy_import":
                raise ValueError("legacy imports cannot be approved as heldout cases")
            if self.exposure_history.status != "clean" or any(event.purpose in _TUNING_PURPOSES for event in self.exposure_history.events):
                raise ValueError("approved heldout cases require known clean history without tuning exposure")
        return self

    def question_projection(self) -> QuestionInput:
        return QuestionInput(question_text=self.question_text, scope=self.scope)

    def reference_projection(self) -> ReferenceLabel:
        return ReferenceLabel(answerability=self.answerability, expected_behavior=self.expected_behavior, reference_answer=self.reference_answer, reference_claims=self.reference_claims, evidence_anchors=self.evidence_anchors, required_evidence_groups=self.required_evidence_groups, provenance=self.provenance, review=self.review)


class EvaluationCase(_EvaluationCaseContent):
    case_digest: Sha256Digest
    _DIGEST_FIELD: ClassVar[str] = "case_digest"

    @classmethod
    def create(cls, **content: Any) -> "EvaluationCase":
        if cls._DIGEST_FIELD in content:
            raise ValueError("case_digest is calculated; do not provide it to create()")
        normalized = _EvaluationCaseContent.model_validate_json(_wire_json(content))
        payload = normalized.model_dump(mode="json")
        return cls.model_validate_json(_wire_json({**payload, "case_digest": content_digest(payload)}))

    def digest_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={self._DIGEST_FIELD})

    def calculated_digest(self) -> str:
        return content_digest(self.digest_payload())

    def verify_digest(self) -> None:
        if self.case_digest != self.calculated_digest():
            raise ValueError("case_digest does not match case content")

    @model_validator(mode="after")
    def _verify_case_digest(self) -> "EvaluationCase":
        self.verify_digest()
        return self

    def question_record(self) -> QuestionRecord:
        projection = self.question_projection()
        return QuestionRecord(
            record_type="case",
            schema_version=self.schema_version,
            protocol_version=self.protocol_version,
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            case_id=self.case_id,
            case_digest=self.case_digest,
            question_digest=content_digest(projection),
            question_input=projection,
        )

    def reference_record(self) -> ReferenceRecord:
        projection = self.reference_projection()
        return ReferenceRecord(
            record_type="case",
            schema_version=self.schema_version,
            protocol_version=self.protocol_version,
            dataset_id=self.dataset_id,
            dataset_version=self.dataset_version,
            case_id=self.case_id,
            case_digest=self.case_digest,
            reference_digest=content_digest(projection),
            reference_label=projection,
        )

    def to_evidence_qa_pair_diagnostic(self, *, verified_anchor_chunk_ids: Mapping[str, Sequence[str]] | None) -> Any:
        """Project to the legacy evaluator only with a validated derivative mapping."""
        from backend.research.rag.evaluation.paper_evidence_eval import EvidenceQAPair
        if self.answerability == "needs_review":
            raise ValueError("needs_review cases cannot be projected into a decided diagnostic behavior")
        if len(self.scope.sources) != 1:
            raise ValueError("legacy EvidenceQAPair diagnostic projection supports exactly one scoped paper")
        chunk_ids: list[str] = []
        if self.answerability == "answerable":
            if verified_anchor_chunk_ids is None:
                raise ValueError("answerable diagnostic projection requires verified anchor mappings")
            missing = [a.anchor_id for a in self.evidence_anchors if a.anchor_id not in verified_anchor_chunk_ids]
            if missing:
                raise ValueError(f"verified anchor mapping is missing anchors: {missing}")
            for anchor in self.evidence_anchors:
                chunk_ids.extend(str(value).strip() for value in verified_anchor_chunk_ids[anchor.anchor_id])
            chunk_ids = list(dict.fromkeys(value for value in chunk_ids if value))
            if not chunk_ids:
                raise ValueError("verified anchor mappings produced no chunk IDs")
        return EvidenceQAPair(question=self.question_text, paper_id=self.scope.sources[0].paper_id, qa_type=self.question_type, gold_chunk_ids=chunk_ids, equivalent_gold_chunk_ids=chunk_ids, gold_claim_ids=[claim.claim_id for claim in self.reference_claims], required_evidence_types=list(dict.fromkeys(anchor.location.content_type for anchor in self.evidence_anchors)), gold_source_locators=[anchor.location.element_id or anchor.location.paragraph_id or anchor.anchor_id for anchor in self.evidence_anchors], answer_facts=[claim.claim_text for claim in self.reference_claims], expected_behavior=self.expected_behavior or "abstain", metadata={"protocol_version": self.protocol_version, "dataset_id": self.dataset_id, "dataset_version": self.dataset_version, "case_digest": self.case_digest, "projection": "source_anchor_diagnostic"})


class CaseManifestEntry(ProtocolModel):
    case_id: NonEmptyStr
    case_digest: Sha256Digest
    question_digest: Sha256Digest
    reference_digest: Sha256Digest
    paper_group_id: NonEmptyStr
    split: DatasetSplit
    review_status: ReviewStatus


class PaperGroup(ProtocolModel):
    paper_group_id: NonEmptyStr
    split: DatasetSplit
    sources: tuple[ScopedPaper, ...] = Field(min_length=1)
    grouping_basis: NonEmptyStr
    dispute_resolution: NonEmptyStr | None

    @model_validator(mode="after")
    def _unique_group_sources(self) -> "PaperGroup":
        keys = [(s.paper_id, s.document_version, s.source_sha256) for s in self.sources]
        if len(keys) != len(set(keys)):
            raise ValueError("paper group sources must be unique")
        return self


class SplitManifest(ProtocolModel):
    method: NonEmptyStr
    method_version: ImmutableVersionStr
    deterministic_seed: NonEmptyStr | None
    groups: tuple[PaperGroup, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_groups(self) -> "SplitManifest":
        _ensure_unique([group.paper_group_id for group in self.groups], "split_manifest.groups.paper_group_id")
        source_group: dict[tuple[str, str, str], str] = {}
        paper_split: dict[str, DatasetSplit] = {}
        for group in self.groups:
            for source in group.sources:
                key = (source.paper_id, source.document_version, source.source_sha256)
                previous_group = source_group.setdefault(key, group.paper_group_id)
                if previous_group != group.paper_group_id:
                    raise ValueError("one paper revision cannot belong to multiple paper groups")
                previous_split = paper_split.setdefault(source.paper_id, group.split)
                if previous_split != group.split:
                    raise ValueError("versions of one paper cannot cross dev and heldout splits")
        return self


class DatasetReviewManifest(ProtocolModel):
    status: ReviewStatus
    reviewer_id: NonEmptyStr | None
    reviewer_kind: ActorKind | None
    reviewed_at: datetime | None
    approved_case_ids: tuple[NonEmptyStr, ...]
    corpus_checked: bool
    split_checked: bool
    contract_checked: bool
    evidence_reference: NonEmptyStr | None

    @field_validator("evidence_reference")
    @classmethod
    def _portable_dataset_review_evidence(cls, value: str | None) -> str | None:
        return None if value is None else _reject_local_path(value, "evidence_reference")

    @model_validator(mode="after")
    def _validate_dataset_review(self) -> "DatasetReviewManifest":
        _ensure_unique(self.approved_case_ids, "review_manifest.approved_case_ids")
        _aware(self.reviewed_at, "reviewed_at")
        if self.status == "approved":
            if self.reviewer_kind != "human" or self.reviewer_id is None or self.reviewed_at is None:
                raise ValueError("dataset approval requires a named human reviewer and reviewed_at")
            if not (self.corpus_checked and self.split_checked and self.contract_checked):
                raise ValueError("dataset approval requires corpus, split, and contract checks")
            if self.evidence_reference is None:
                raise ValueError("dataset approval requires an external evidence reference")
        return self


class LicenseReference(ProtocolModel):
    reference_id: NonEmptyStr
    reference_uri: NonEmptyStr
    terms_digest: Sha256Digest
    permitted_use: Literal["internal_evaluation", "redistributable", "restricted_evaluation"]

    @field_validator("reference_uri")
    @classmethod
    def _portable_reference_uri(cls, value: str) -> str:
        return _reject_local_path(value, "reference_uri")


class AuthoringProtocol(ProtocolModel):
    protocol_id: NonEmptyStr
    protocol_version: ImmutableVersionStr
    protocol_digest: Sha256Digest


class _DatasetManifestContent(ProtocolModel):
    record_type: Literal["dataset"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    lifecycle_state: DatasetLifecycleState
    release_purpose: ReleasePurpose
    corpus_snapshot_id: ImmutableVersionStr
    corpus_snapshot_digest: Sha256Digest
    paper_versions: tuple[SourceRecord, ...] = Field(min_length=1)
    case_manifest: tuple[CaseManifestEntry, ...] = Field(min_length=1)
    split_manifest: SplitManifest
    review_manifest: DatasetReviewManifest
    annotation_version: ImmutableVersionStr
    license_reference: LicenseReference
    authoring_protocol: AuthoringProtocol
    supersedes: ImmutableVersionStr | None
    correction_reason: NonEmptyStr | None
    frozen_at: datetime | None

    @model_validator(mode="after")
    def _validate_dataset_content(self) -> "_DatasetManifestContent":
        case_ids = [item.case_id for item in self.case_manifest]
        _ensure_unique(case_ids, "case_manifest.case_id")
        source_keys = [(s.paper_id, s.document_version, s.source_sha256) for s in self.paper_versions]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("paper_versions must not contain duplicate source revisions")
        if any(source.license_reference_id != self.license_reference.reference_id for source in self.paper_versions):
            raise ValueError("every paper_versions source must reference the dataset license_reference")
        groups = {group.paper_group_id: group for group in self.split_manifest.groups}
        group_sources = {(s.paper_id, s.document_version, s.source_sha256) for group in self.split_manifest.groups for s in group.sources}
        if group_sources != set(source_keys):
            raise ValueError("split_manifest sources must exactly match paper_versions")
        for item in self.case_manifest:
            group = groups.get(item.paper_group_id)
            if group is None:
                raise ValueError(f"case {item.case_id!r} references an unknown paper_group_id")
            if group.split != item.split:
                raise ValueError(f"case {item.case_id!r} split conflicts with its paper group")
        approved = set(self.review_manifest.approved_case_ids)
        if self.release_purpose == "heldout_qualification" and any(item.split != "heldout" for item in self.case_manifest):
            raise ValueError("heldout qualification releases may contain only heldout cases")
        if self.release_purpose != "heldout_qualification" and any(item.split != "dev" for item in self.case_manifest):
            raise ValueError("development and pilot releases may contain only dev cases")
        if not approved.issubset(set(case_ids)):
            raise ValueError("review_manifest contains an unknown approved case_id")
        if (self.supersedes is None) != (self.correction_reason is None):
            raise ValueError("supersedes and correction_reason must be provided together")
        _aware(self.frozen_at, "frozen_at")
        if self.lifecycle_state == "frozen":
            if self.frozen_at is None or self.review_manifest.status != "approved":
                raise ValueError("frozen datasets require frozen_at and approved human review")
            if approved != set(case_ids) or any(item.review_status != "approved" for item in self.case_manifest):
                raise ValueError("every case must be explicitly approved before freezing")
            if any(source.completeness != "complete" or source.access_status != "available" for source in self.paper_versions):
                raise ValueError("frozen datasets require complete, available source materials")
        elif self.frozen_at is not None:
            raise ValueError("non-frozen datasets must use explicit null frozen_at")
        return self


class DatasetManifest(_DatasetManifestContent):
    dataset_digest: Sha256Digest
    _DIGEST_FIELD: ClassVar[str] = "dataset_digest"

    @classmethod
    def create(cls, **content: Any) -> "DatasetManifest":
        if cls._DIGEST_FIELD in content:
            raise ValueError("dataset_digest is calculated; do not provide it to create()")
        normalized = _DatasetManifestContent.model_validate_json(_wire_json(content))
        payload = normalized.model_dump(mode="json")
        return cls.model_validate_json(_wire_json({**payload, "dataset_digest": content_digest(payload)}))

    def digest_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={self._DIGEST_FIELD})

    def calculated_digest(self) -> str:
        return content_digest(self.digest_payload())

    def verify_digest(self) -> None:
        if self.dataset_digest != self.calculated_digest():
            raise ValueError("dataset_digest does not match manifest content")

    @model_validator(mode="after")
    def _verify_dataset_digest(self) -> "DatasetManifest":
        self.verify_digest()
        return self

    def validate_cases(self, cases: Sequence[EvaluationCase]) -> None:
        by_id: dict[str, EvaluationCase] = {}
        for case in cases:
            if case.case_id in by_id:
                raise ValueError(f"duplicate case_id: {case.case_id}")
            by_id[case.case_id] = case
        expected = {entry.case_id: entry for entry in self.case_manifest}
        if set(by_id) != set(expected):
            missing, unexpected = sorted(set(expected) - set(by_id)), sorted(set(by_id) - set(expected))
            raise ValueError(f"case membership mismatch; missing={missing}, unexpected={unexpected}")
        for case_id, case in by_id.items():
            entry = expected[case_id]
            if case.dataset_id != self.dataset_id or case.dataset_version != self.dataset_version:
                raise ValueError(f"case {case_id!r} references a different dataset version")
            if case.case_digest != entry.case_digest:
                raise ValueError(f"case {case_id!r} digest does not match case_manifest")
            if content_digest(case.question_projection()) != entry.question_digest:
                raise ValueError(f"case {case_id!r} question digest does not match case_manifest")
            if content_digest(case.reference_projection()) != entry.reference_digest:
                raise ValueError(f"case {case_id!r} reference digest does not match case_manifest")
            if case.paper_group_id != entry.paper_group_id or case.split != entry.split:
                raise ValueError(f"case {case_id!r} group/split does not match case_manifest")
            group = next(group for group in self.split_manifest.groups if group.paper_group_id == entry.paper_group_id)
            group_sources = {(source.paper_id, source.document_version, source.source_sha256) for source in group.sources}
            case_sources = {(source.paper_id, source.document_version, source.source_sha256) for source in case.scope.sources}
            if not case_sources.issubset(group_sources):
                raise ValueError(f"case {case_id!r} scope contains a source outside its paper group")
            manifest_sources = {source.model_dump_json(): source for source in self.paper_versions}
            for source in case.provenance.sources:
                if source.model_dump_json() not in manifest_sources:
                    raise ValueError(f"case {case_id!r} provenance source does not exactly match paper_versions")
                if source.license_reference_id != self.license_reference.reference_id:
                    raise ValueError(f"case {case_id!r} source license does not match dataset license_reference")
            if case.review.status != entry.review_status:
                raise ValueError(f"case {case_id!r} review status does not match case_manifest")
            if self.lifecycle_state == "frozen" and case.answerability == "needs_review":
                raise ValueError(f"needs_review case {case_id!r} cannot enter a frozen dataset")


__all__ = ["DatasetLifecycleState", "ReleasePurpose", "DatasetSplit", "QuestionType", "Answerability", "ExpectedBehavior", "ReviewStatus", "ActorKind", "AuthoringMethod", "ExposureStatus", "ExposureSubject", "ExposurePurpose", "SourceCompleteness", "AnchorContentType", "VisibleMaterial", "ScopedPaper", "QuestionScope", "QuestionInput", "AssistanceRecord", "AuthorInputManifest", "SourceRecord", "CaseProvenance", "ExposureEvent", "ExposureHistory", "HumanReview", "ReferenceClaim", "EvidenceLocation", "EvidenceAnchor", "AlternativeSufficientSet", "RequiredEvidenceGroup", "ReferenceLabel", "QuestionRecord", "ReferenceRecord", "EvaluationCase", "CaseManifestEntry", "PaperGroup", "SplitManifest", "DatasetReviewManifest", "LicenseReference", "AuthoringProtocol", "DatasetManifest"]
