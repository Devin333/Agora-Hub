"""Deterministic authoring lifecycle for protocol evaluation datasets.

This module contains no persistence, authentication, agent, Harness, or run-time
question generation. It validates immutable values and returns new ones. Callers
must authenticate actors, verify external evidence references, and persist each
transition atomically; an AuthoringAction declaration cannot prove authority.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator

from backend.research.rag.evaluation.protocol_common import (
    ImmutableVersionStr,
    NonEmptyStr,
    PROTOCOL_VERSION,
    ProtocolModel,
    SCHEMA_VERSION,
    Sha256Digest,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import (
    ActorKind,
    AssistanceRecord,
    AuthorInputManifest,
    AuthoringMethod,
    CaseProvenance,
    DatasetManifest,
    DatasetSplit,
    EvaluationCase,
    ExposureHistory,
    ReferenceReview,
    QuestionInput,
    QuestionRecord,
    QuestionType,
    ReferenceLabel,
    ReferenceRecord,
    SourceRecord,
)

CandidateLifecycleState = Literal["draft", "annotated", "reviewed"]
DispositionStatus = Literal["needs_review", "rejected", "retired", "revoked"]
ActionName = Literal[
    "import_question", "annotate_fulltext", "review_case", "revise_question",
    "mark_needs_review", "reject_case", "retire_case", "revoke_case",
    "begin_correction", "freeze_dataset",
]
ActorSource = Literal[
    "authenticated_human", "external_import", "llm_assistance", "deterministic_service",
]

_LOCAL_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


class AuthoringLifecycleError(ValueError):
    """Raised when an authoring transition would violate the protocol."""


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _portable_reference(value: str, field_name: str) -> str:
    if _LOCAL_ABSOLUTE_PATH.match(value) or value.lower().startswith("file:"):
        raise ValueError(f"{field_name} must be an external portable reference, not a local path")
    return value


def _assistance_digests(records: Sequence[AssistanceRecord]) -> set[str]:
    return {content_digest(record) for record in records}


class AuthoringAction(ProtocolModel):
    """Attributed audit assertion supplied by an authenticated outer caller."""

    action: ActionName
    actor_kind: ActorKind
    actor_id: NonEmptyStr
    actor_source: ActorSource
    occurred_at: datetime
    evidence_reference: NonEmptyStr

    @field_validator("occurred_at")
    @classmethod
    def _aware_occurred_at(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")

    @field_validator("evidence_reference")
    @classmethod
    def _external_evidence_reference(cls, value: str) -> str:
        return _portable_reference(value, "evidence_reference")

    @model_validator(mode="after")
    def _source_matches_actor_kind(self) -> "AuthoringAction":
        expected = {
            "authenticated_human": "human",
            "llm_assistance": "llm",
            "deterministic_service": "rule",
        }.get(self.actor_source)
        if expected is not None and self.actor_kind != expected:
            raise ValueError(f"actor_source={self.actor_source!r} requires actor_kind={expected!r}")
        return self


class PriorCaseReference(ProtocolModel):
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    dataset_digest: Sha256Digest | None
    case_id: NonEmptyStr
    case_digest: Sha256Digest
    question_digest: Sha256Digest
    reference_digest: Sha256Digest

    @classmethod
    def from_case(cls, case: EvaluationCase, *, dataset_digest: str | None = None) -> "PriorCaseReference":
        return cls(
            dataset_id=case.dataset_id,
            dataset_version=case.dataset_version,
            dataset_digest=dataset_digest,
            case_id=case.case_id,
            case_digest=case.case_digest,
            question_digest=content_digest(case.question_projection()),
            reference_digest=content_digest(case.reference_projection()),
        )


class QuestionFreezeRecord(ProtocolModel):
    """Question-first evidence captured before full-text annotation starts."""

    question_input: QuestionInput
    question_digest: Sha256Digest
    authoring_method: AuthoringMethod
    question_author_id: NonEmptyStr
    author_input_manifest: AuthorInputManifest
    frozen_at: datetime
    imported_by: AuthoringAction
    original_dataset_id: NonEmptyStr | None
    original_case_id: NonEmptyStr | None
    derived_from_case_ids: tuple[NonEmptyStr, ...]
    assistance: tuple[AssistanceRecord, ...]
    sources: tuple[SourceRecord, ...] = Field(min_length=1)

    @field_validator("frozen_at")
    @classmethod
    def _aware_frozen_at(cls, value: datetime) -> datetime:
        return _aware(value, "frozen_at")

    @model_validator(mode="after")
    def _validate_question_freeze(self) -> "QuestionFreezeRecord":
        if self.imported_by.action not in {"import_question", "revise_question"}:
            raise ValueError("question import requires import_question or revise_question attribution")
        if self.question_digest != content_digest(self.question_input):
            raise ValueError("question_digest does not match the frozen QuestionInput")
        if self.frozen_at > self.imported_by.occurred_at:
            raise ValueError("question must be frozen no later than its import transition")
        if (self.original_dataset_id is None) != (self.original_case_id is None):
            raise ValueError("original_dataset_id and original_case_id must be provided together")
        if len(self.derived_from_case_ids) != len(set(self.derived_from_case_ids)):
            raise ValueError("derived_from_case_ids must not contain duplicates")
        scope_sources = {
            (source.paper_id, source.document_version, source.source_sha256)
            for source in self.question_input.scope.sources
        }
        provenance_sources = {
            (source.paper_id, source.document_version, source.source_sha256)
            for source in self.sources
        }
        if scope_sources != provenance_sources:
            raise ValueError("question sources must exactly match QuestionInput.scope")
        visible = set(self.author_input_manifest.visible_materials)
        if self.authoring_method == "abstract_only":
            if visible != {"title", "abstract", "declared_scope"}:
                raise ValueError("abstract_only import requires exactly title, abstract, and declared_scope")
        elif self.authoring_method == "fulltext_targeted":
            if not {"fulltext", "declared_scope"}.issubset(visible):
                raise ValueError("fulltext_targeted import requires fulltext and declared_scope")
        if self.authoring_method != "legacy_import" and "retrieval_output" in visible:
            raise ValueError("independent candidate questions cannot be generated from retrieval output")
        for assistance in self.assistance:
            if self.authoring_method == "abstract_only" and not set(assistance.input_scope).issubset(
                {"title", "abstract", "declared_scope"}
            ):
                raise ValueError("abstract_only assistance exceeded the question-first input boundary")
            if self.authoring_method != "legacy_import" and "retrieval_output" in assistance.input_scope:
                raise ValueError("independent assistance cannot inspect target retrieval output")
        return self


class CandidateImport(ProtocolModel):
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    case_id: NonEmptyStr
    question_type: QuestionType
    paper_group_id: NonEmptyStr
    split: DatasetSplit
    question_freeze: QuestionFreezeRecord
    exposure_history: ExposureHistory
    prior_case: PriorCaseReference | None = None


class CandidateDisposition(ProtocolModel):
    status: DispositionStatus
    destination: NonEmptyStr
    reason: NonEmptyStr
    related_case_id: NonEmptyStr
    original_case_id: NonEmptyStr
    prior_case: PriorCaseReference | None
    decided_by: AuthoringAction

    @model_validator(mode="after")
    def _validate_disposition(self) -> "CandidateDisposition":
        expected_action = {
            "needs_review": "mark_needs_review",
            "rejected": "reject_case",
            "retired": "retire_case",
            "revoked": "revoke_case",
        }[self.status]
        if self.decided_by.action != expected_action:
            raise ValueError(f"{self.status} disposition requires action={expected_action!r}")
        if self.decided_by.actor_kind != "human":
            raise ValueError("candidate disposition decisions require a named human actor")
        return self


class AuthoringCandidate(ProtocolModel):
    """Immutable aggregate that moves through draft, annotated, and reviewed."""

    lifecycle_state: CandidateLifecycleState
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    case_id: NonEmptyStr
    question_type: QuestionType
    paper_group_id: NonEmptyStr
    split: DatasetSplit
    question_freeze: QuestionFreezeRecord
    exposure_history: ExposureHistory
    prior_case: PriorCaseReference | None
    annotation_action: AuthoringAction | None
    reference_label: ReferenceLabel | None
    reviewed_case: EvaluationCase | None
    disposition: CandidateDisposition | None

    @model_validator(mode="after")
    def _validate_state(self) -> "AuthoringCandidate":
        if self.prior_case is not None and self.prior_case.case_id not in self.question_freeze.derived_from_case_ids:
            raise ValueError("a corrected or revised candidate must retain its prior case association")
        if self.disposition is not None:
            if self.disposition.related_case_id != self.case_id:
                raise ValueError("disposition related_case_id must match candidate.case_id")
            if self.disposition.prior_case != self.prior_case:
                raise ValueError("disposition must retain the candidate prior_case reference")
        if self.lifecycle_state == "draft":
            if self.annotation_action is not None or self.reference_label is not None or self.reviewed_case is not None:
                raise ValueError("draft candidates cannot carry annotation or review output")
        elif self.lifecycle_state == "annotated":
            if self.annotation_action is None or self.reference_label is None or self.reviewed_case is not None:
                raise ValueError("annotated candidates require annotation evidence but no reviewed case")
            if self.annotation_action.action != "annotate_fulltext":
                raise ValueError("annotated candidates require annotate_fulltext attribution")
            if self.reference_label.review.status not in {"pending", "needs_review"}:
                raise ValueError("annotation cannot smuggle an approved or rejected final review")
        else:
            if self.annotation_action is None or self.reference_label is None or self.reviewed_case is None:
                raise ValueError("reviewed candidates require annotation, reference label, and EvaluationCase")
            if self.reference_label.review.status != "approved" or self.reviewed_case.review.status != "approved":
                raise ValueError("reviewed candidates require approved human review")
            if self.reviewed_case.question_projection() != self.question_freeze.question_input:
                raise ValueError("reviewed case question differs from the frozen question")
            if self.reviewed_case.reference_projection() != self.reference_label:
                raise ValueError("reviewed case reference differs from the reviewed annotation")
            if self.reviewed_case.exposure_history != self.exposure_history:
                raise ValueError("reviewed case exposure history differs from the candidate")
        return self


class CorrectionContext(ProtocolModel):
    dataset_id: NonEmptyStr
    prior_dataset_version: ImmutableVersionStr
    prior_dataset_digest: Sha256Digest
    prior_release_digest: Sha256Digest
    new_dataset_version: ImmutableVersionStr
    reason: NonEmptyStr
    initiated_by: AuthoringAction

    @model_validator(mode="after")
    def _validate_context(self) -> "CorrectionContext":
        if self.prior_dataset_version == self.new_dataset_version:
            raise ValueError("a correction must create a distinct dataset_version")
        if self.initiated_by.action != "begin_correction" or self.initiated_by.actor_kind != "human":
            raise ValueError("dataset correction requires an attributed human begin_correction action")
        return self


class CorrectionBinding(ProtocolModel):
    prior_dataset_version: ImmutableVersionStr
    prior_dataset_digest: Sha256Digest
    prior_release_digest: Sha256Digest
    new_dataset_version: ImmutableVersionStr
    new_dataset_digest: Sha256Digest
    reason: NonEmptyStr
    initiated_by: AuthoringAction



class _FrozenDatasetReleaseContent(ProtocolModel):
    lifecycle_state: Literal["frozen"]
    manifest: DatasetManifest
    cases: tuple[EvaluationCase, ...] = Field(min_length=1)
    questions: tuple[QuestionRecord, ...] = Field(min_length=1)
    references: tuple[ReferenceRecord, ...] = Field(min_length=1)
    dispositions: tuple[CandidateDisposition, ...]
    published_by: AuthoringAction
    correction: CorrectionBinding | None

    @model_validator(mode="after")
    def _validate_release(self) -> "_FrozenDatasetReleaseContent":
        if self.manifest.lifecycle_state != "frozen":
            raise ValueError("only a frozen DatasetManifest can be published")
        self.manifest.validate_cases(self.cases)
        case_ids = [case.case_id for case in self.cases]
        expected_ids = [entry.case_id for entry in self.manifest.case_manifest]
        if case_ids != expected_ids:
            raise ValueError("release cases must follow exact case_manifest order")
        if self.questions != tuple(case.question_record() for case in self.cases):
            raise ValueError("question projection mismatch in frozen release")
        if self.references != tuple(case.reference_record() for case in self.cases):
            raise ValueError("reference projection mismatch in frozen release")
        if self.published_by.action != "freeze_dataset" or self.published_by.actor_kind != "human":
            raise ValueError("freezing requires an attributed human action")
        review = self.manifest.review_manifest
        if self.published_by.actor_id != review.reviewer_id:
            raise ValueError("freeze actor must match the approved dataset reviewer")
        if self.published_by.occurred_at != self.manifest.frozen_at:
            raise ValueError("freeze action time must equal manifest.frozen_at")
        disposition_ids = [item.related_case_id for item in self.dispositions]
        if len(disposition_ids) != len(set(disposition_ids)):
            raise ValueError("frozen release dispositions must have unique related_case_id values")
        if set(disposition_ids).intersection(case_ids):
            raise ValueError("a frozen member cannot also have a non-member disposition")
        if self.correction is None:
            if self.manifest.supersedes is not None:
                raise ValueError("a superseding manifest requires a prior digest correction binding")
        else:
            if self.manifest.supersedes != self.correction.prior_dataset_version:
                raise ValueError("manifest.supersedes does not match correction prior version")
            if self.manifest.dataset_version != self.correction.new_dataset_version:
                raise ValueError("correction binding new version does not match manifest")
            if self.manifest.dataset_digest != self.correction.new_dataset_digest:
                raise ValueError("correction binding new digest does not match manifest")
            if self.manifest.correction_reason != self.correction.reason:
                raise ValueError("correction reason does not match manifest")
        return self


class FrozenDatasetRelease(_FrozenDatasetReleaseContent):
    """Read-only publication with separated question/reference projections."""

    release_digest: Sha256Digest
    _DIGEST_FIELD: ClassVar[str] = "release_digest"

    @classmethod
    def create(cls, **content: Any) -> "FrozenDatasetRelease":
        if cls._DIGEST_FIELD in content:
            raise ValueError("release_digest is calculated; do not provide it to create()")
        normalized = _FrozenDatasetReleaseContent.model_validate(content)
        return cls(**normalized.model_dump(mode="python"), release_digest=content_digest(normalized))

    def calculated_digest(self) -> str:
        return content_digest(self.model_dump(mode="json", exclude={self._DIGEST_FIELD}))

    def verify_integrity(self) -> None:
        self.manifest.verify_digest()
        self.manifest.validate_cases(self.cases)
        for case in self.cases:
            case.verify_digest()
        if self.release_digest != self.calculated_digest():
            raise AuthoringLifecycleError("release_digest does not match frozen release content")

    @model_validator(mode="after")
    def _verify_release_digest(self) -> "FrozenDatasetRelease":
        if self.release_digest != self.calculated_digest():
            raise ValueError("release_digest does not match frozen release content")
        return self


def import_question_candidate(request: CandidateImport) -> AuthoringCandidate:
    """Import a question-first candidate without creating any annotation."""

    if request.question_freeze.imported_by.action != "import_question":
        raise AuthoringLifecycleError("initial candidate import requires action='import_question'")
    return AuthoringCandidate(
        lifecycle_state="draft",
        dataset_id=request.dataset_id,
        dataset_version=request.dataset_version,
        case_id=request.case_id,
        question_type=request.question_type,
        paper_group_id=request.paper_group_id,
        split=request.split,
        question_freeze=request.question_freeze,
        exposure_history=request.exposure_history,
        prior_case=request.prior_case,
        annotation_action=None,
        reference_label=None,
        reviewed_case=None,
        disposition=None,
    )


def _require_active(candidate: AuthoringCandidate) -> None:
    if candidate.disposition is not None:
        raise AuthoringLifecycleError(
            f"candidate {candidate.case_id!r} is {candidate.disposition.status} and routed to "
            f"{candidate.disposition.destination!r}"
        )


def _validate_annotation_semantics(candidate: AuthoringCandidate, reference: ReferenceLabel) -> None:
    if reference.provenance.question_digest != candidate.question_freeze.question_digest:
        raise AuthoringLifecycleError("annotation provenance does not bind the frozen question digest")
    if reference.answerability == "answerable":
        if reference.expected_behavior != "answer" or reference.reference_answer is None:
            raise AuthoringLifecycleError("answerable annotation requires answer behavior and a reference answer")
        if not reference.reference_claims or not reference.evidence_anchors or not reference.required_evidence_groups:
            raise AuthoringLifecycleError("answerable annotation requires claims, anchors, and evidence groups")
    elif reference.answerability == "unanswerable":
        if reference.expected_behavior != "abstain" or reference.reference_answer is not None:
            raise AuthoringLifecycleError("unanswerable annotation requires abstain and explicit null answer")
        if reference.required_evidence_groups:
            raise AuthoringLifecycleError("unanswerable annotation requires an explicit empty evidence-group list")
        if any(
            source.completeness != "complete" or source.access_status != "available"
            for source in reference.provenance.sources
        ):
            raise AuthoringLifecycleError("missing or incomplete materials must be needs_review, not unanswerable")
        review = reference.review
        if (
            review.source_completeness != "complete"
            or review.checked_scope != candidate.question_freeze.question_input.scope
        ):
            raise AuthoringLifecycleError("unanswerable annotation requires a complete exact-scope full-text check")
        if review.unanswerable_reason is None:
            raise AuthoringLifecycleError("unanswerable annotation requires the full-text no-answer reason")
    else:
        if reference.expected_behavior is not None or reference.reference_answer is not None:
            raise AuthoringLifecycleError("needs_review annotation requires explicit null behavior and answer")
        if reference.review.status != "needs_review":
            raise AuthoringLifecycleError("needs_review answerability requires review.status='needs_review'")


def annotate_candidate(
    candidate: AuthoringCandidate,
    *,
    provenance: CaseProvenance,
    reference_label: ReferenceLabel,
    annotated_by: AuthoringAction,
) -> AuthoringCandidate:
    """Attach independent full-text annotation after the question freeze."""

    _require_active(candidate)
    if candidate.lifecycle_state != "draft":
        raise AuthoringLifecycleError("only draft candidates can be annotated")
    if annotated_by.action != "annotate_fulltext" or annotated_by.actor_kind != "human":
        raise AuthoringLifecycleError("full-text annotation requires a named human primary annotator")
    freeze = candidate.question_freeze
    if provenance.authoring_method != freeze.authoring_method:
        raise AuthoringLifecycleError("annotation changed the recorded authoring_method")
    if (
        provenance.original_dataset_id != freeze.original_dataset_id
        or provenance.original_case_id != freeze.original_case_id
    ):
        raise AuthoringLifecycleError("annotation changed the imported source relationship")
    if provenance.derived_from_case_ids != freeze.derived_from_case_ids:
        raise AuthoringLifecycleError("annotation changed derived_from_case_ids")
    if provenance.question_author_id != freeze.question_author_id:
        raise AuthoringLifecycleError("annotation changed question_author_id")
    if provenance.author_input_manifest != freeze.author_input_manifest:
        raise AuthoringLifecycleError("annotation changed author_input_manifest")
    if provenance.question_digest != freeze.question_digest or provenance.question_frozen_at != freeze.frozen_at:
        raise AuthoringLifecycleError("annotation is not bound to the frozen question")
    if provenance.sources != freeze.sources:
        raise AuthoringLifecycleError("annotation sources differ from the question-first import")
    if provenance.annotator_id != annotated_by.actor_id:
        raise AuthoringLifecycleError("annotation actor does not match provenance.annotator_id")
    if provenance.annotation_started_at <= freeze.frozen_at:
        raise AuthoringLifecycleError("annotation must begin after the question is frozen")
    if annotated_by.occurred_at < provenance.annotation_started_at:
        raise AuthoringLifecycleError("annotation action cannot predate annotation_started_at")
    if not _assistance_digests(freeze.assistance).issubset(_assistance_digests(provenance.assistance)):
        raise AuthoringLifecycleError("annotation provenance dropped recorded question assistance")
    if reference_label.provenance != provenance:
        raise AuthoringLifecycleError("ReferenceLabel.provenance must equal the submitted CaseProvenance")
    if reference_label.review.status == "approved":
        raise AuthoringLifecycleError("annotation cannot auto-approve itself; use a separate human review transition")
    _validate_annotation_semantics(candidate, reference_label)
    return AuthoringCandidate(
        **candidate.model_dump(
            mode="python",
            exclude={"lifecycle_state", "annotation_action", "reference_label", "reviewed_case"},
        ),
        lifecycle_state="annotated",
        annotation_action=annotated_by,
        reference_label=reference_label,
        reviewed_case=None,
    )


def review_candidate(
    candidate: AuthoringCandidate,
    *,
    review: ReferenceReview,
    reviewed_by: AuthoringAction,
) -> AuthoringCandidate:
    """Apply a human ReferenceReview and construct EvaluationCase.

    This is the hand-authored lifecycle; benchmark gold and automated consensus
    labels are built by their own adapters, never through this human workflow.
    """

    _require_active(candidate)
    if (
        candidate.lifecycle_state != "annotated"
        or candidate.reference_label is None
        or candidate.annotation_action is None
    ):
        raise AuthoringLifecycleError("only annotated candidates can be reviewed")
    if reviewed_by.action != "review_case" or reviewed_by.actor_kind != "human":
        raise AuthoringLifecycleError("final review requires a named human review action")
    if (
        review.status != "approved"
        or review.authority != "human"
        or review.reviewer_kind != "human"
    ):
        raise AuthoringLifecycleError("LLM/rule output cannot be used as final human approval")
    if review.reviewer_id != reviewed_by.actor_id or review.reviewed_at != reviewed_by.occurred_at:
        raise AuthoringLifecycleError("review action must match ReferenceReview reviewer and timestamp")
    if review.evidence_reference != reviewed_by.evidence_reference:
        raise AuthoringLifecycleError("review action must bind the ReferenceReview external evidence reference")
    provenance = candidate.reference_label.provenance
    if review.reviewed_at is None or review.reviewed_at <= provenance.annotation_started_at:
        raise AuthoringLifecycleError("final review must occur after full-text annotation starts")
    if review.reviewed_at < candidate.annotation_action.occurred_at:
        raise AuthoringLifecycleError("final review cannot predate the annotation transition")
    reviewed_reference = ReferenceLabel(
        answerability=candidate.reference_label.answerability,
        expected_behavior=candidate.reference_label.expected_behavior,
        reference_answer=candidate.reference_label.reference_answer,
        reference_claims=candidate.reference_label.reference_claims,
        evidence_anchors=candidate.reference_label.evidence_anchors,
        required_evidence_groups=candidate.reference_label.required_evidence_groups,
        provenance=provenance,
        review=review,
    )
    _validate_annotation_semantics(candidate, reviewed_reference)
    question = candidate.question_freeze.question_input
    case = EvaluationCase.create(
        record_type="case",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        dataset_id=candidate.dataset_id,
        dataset_version=candidate.dataset_version,
        case_id=candidate.case_id,
        question_text=question.question_text,
        scope=question.scope,
        question_type=candidate.question_type,
        paper_group_id=candidate.paper_group_id,
        split=candidate.split,
        answerability=reviewed_reference.answerability,
        expected_behavior=reviewed_reference.expected_behavior,
        reference_answer=reviewed_reference.reference_answer,
        reference_claims=reviewed_reference.reference_claims,
        evidence_anchors=reviewed_reference.evidence_anchors,
        required_evidence_groups=reviewed_reference.required_evidence_groups,
        provenance=reviewed_reference.provenance,
        exposure_history=candidate.exposure_history,
        review=reviewed_reference.review,
    )
    return AuthoringCandidate(
        **candidate.model_dump(mode="python", exclude={"lifecycle_state", "reference_label", "reviewed_case"}),
        lifecycle_state="reviewed",
        reference_label=reviewed_reference,
        reviewed_case=case,
    )


def record_candidate_disposition(
    candidate: AuthoringCandidate,
    *,
    status: DispositionStatus,
    destination: str,
    reason: str,
    decided_by: AuthoringAction,
) -> AuthoringCandidate:
    """Preserve routing and original-case association for a non-member."""

    if candidate.disposition is not None:
        raise AuthoringLifecycleError("candidate already has a disposition")
    original_case_id = (
        candidate.question_freeze.original_case_id
        or (candidate.prior_case.case_id if candidate.prior_case is not None else None)
        or candidate.case_id
    )
    disposition = CandidateDisposition(
        status=status,
        destination=destination,
        reason=reason,
        related_case_id=candidate.case_id,
        original_case_id=original_case_id,
        prior_case=candidate.prior_case,
        decided_by=decided_by,
    )
    return AuthoringCandidate(**candidate.model_dump(mode="python", exclude={"disposition"}), disposition=disposition)


def revise_candidate_question(candidate: AuthoringCandidate, request: CandidateImport) -> AuthoringCandidate:
    """Replace question text and reset annotation/review to a new draft chain."""

    _require_active(candidate)
    if request.dataset_id != candidate.dataset_id or request.dataset_version != candidate.dataset_version:
        raise AuthoringLifecycleError("question revision must stay in the candidate dataset identity")
    if request.case_id != candidate.case_id:
        raise AuthoringLifecycleError("question revision must retain case_id")
    if request.question_freeze.imported_by.action != "revise_question":
        raise AuthoringLifecycleError("question revision requires action='revise_question'")
    if request.question_freeze.question_input.question_text == candidate.question_freeze.question_input.question_text:
        raise AuthoringLifecycleError("revise_candidate_question requires changed question text")
    latest_prior_time = candidate.question_freeze.frozen_at
    if candidate.reviewed_case is not None and candidate.reviewed_case.review.reviewed_at is not None:
        latest_prior_time = max(latest_prior_time, candidate.reviewed_case.review.reviewed_at)
    if request.question_freeze.frozen_at <= latest_prior_time:
        raise AuthoringLifecycleError("revised question must have a new freeze time after the prior chain")
    prior = candidate.prior_case
    if candidate.reviewed_case is not None:
        prior = PriorCaseReference.from_case(candidate.reviewed_case, dataset_digest=None)
    if prior is not None and prior.case_id not in request.question_freeze.derived_from_case_ids:
        raise AuthoringLifecycleError("revised question must preserve the prior case in derived_from_case_ids")
    normalized = CandidateImport(**request.model_dump(mode="python", exclude={"prior_case"}), prior_case=prior)
    return AuthoringCandidate(
        lifecycle_state="draft",
        dataset_id=normalized.dataset_id,
        dataset_version=normalized.dataset_version,
        case_id=normalized.case_id,
        question_type=normalized.question_type,
        paper_group_id=normalized.paper_group_id,
        split=normalized.split,
        question_freeze=normalized.question_freeze,
        exposure_history=normalized.exposure_history,
        prior_case=normalized.prior_case,
        annotation_action=None,
        reference_label=None,
        reviewed_case=None,
        disposition=None,
    )



def begin_dataset_correction(
    prior_release: FrozenDatasetRelease,
    *,
    new_dataset_version: str,
    reason: str,
    initiated_by: AuthoringAction,
) -> CorrectionContext:
    """Bind a correction to the immutable prior version and release digests."""

    prior_release.verify_integrity()
    return CorrectionContext(
        dataset_id=prior_release.manifest.dataset_id,
        prior_dataset_version=prior_release.manifest.dataset_version,
        prior_dataset_digest=prior_release.manifest.dataset_digest,
        prior_release_digest=prior_release.release_digest,
        new_dataset_version=new_dataset_version,
        reason=reason,
        initiated_by=initiated_by,
    )


def import_correction_candidate(
    context: CorrectionContext,
    prior_case: EvaluationCase,
    request: CandidateImport,
) -> AuthoringCandidate:
    """Start a new-version draft without copying annotation or approval."""

    if prior_case.dataset_id != context.dataset_id or prior_case.dataset_version != context.prior_dataset_version:
        raise AuthoringLifecycleError("prior case does not belong to the correction's frozen version")
    if request.dataset_id != context.dataset_id or request.dataset_version != context.new_dataset_version:
        raise AuthoringLifecycleError("correction candidate must target the declared new dataset version")
    if request.question_freeze.frozen_at <= context.initiated_by.occurred_at:
        raise AuthoringLifecycleError("correction question freeze must occur after correction initiation")
    if request.question_freeze.original_case_id != prior_case.case_id:
        raise AuthoringLifecycleError("correction candidate must preserve original_case_id")
    if prior_case.case_id not in request.question_freeze.derived_from_case_ids:
        raise AuthoringLifecycleError("correction candidate must preserve derived_from_case_ids")
    prior_reference = PriorCaseReference.from_case(prior_case, dataset_digest=context.prior_dataset_digest)
    if request.prior_case is not None and request.prior_case != prior_reference:
        raise AuthoringLifecycleError("correction request prior_case does not match the frozen case")
    normalized = CandidateImport(
        **request.model_dump(mode="python", exclude={"prior_case"}),
        prior_case=prior_reference,
    )
    return import_question_candidate(normalized)


def record_prior_case_disposition(
    context: CorrectionContext,
    prior_case: EvaluationCase,
    *,
    status: DispositionStatus,
    destination: str,
    reason: str,
    decided_by: AuthoringAction,
) -> CandidateDisposition:
    """Record where an omitted prior case went without rewriting that case."""

    if prior_case.dataset_id != context.dataset_id or prior_case.dataset_version != context.prior_dataset_version:
        raise AuthoringLifecycleError("disposition prior case is outside the correction source version")
    return CandidateDisposition(
        status=status,
        destination=destination,
        reason=reason,
        related_case_id=prior_case.case_id,
        original_case_id=prior_case.case_id,
        prior_case=PriorCaseReference.from_case(prior_case, dataset_digest=context.prior_dataset_digest),
        decided_by=decided_by,
    )


def _validate_correction_candidates(
    *,
    context: CorrectionContext,
    prior_release: FrozenDatasetRelease,
    candidates: Sequence[AuthoringCandidate],
    dispositions: Sequence[CandidateDisposition],
    manifest: DatasetManifest,
) -> CorrectionBinding:
    prior_release.verify_integrity()
    if (
        prior_release.manifest.dataset_id != context.dataset_id
        or prior_release.manifest.dataset_version != context.prior_dataset_version
        or prior_release.manifest.dataset_digest != context.prior_dataset_digest
        or prior_release.release_digest != context.prior_release_digest
    ):
        raise AuthoringLifecycleError("correction context does not bind the supplied immutable prior release")
    if manifest.dataset_id != context.dataset_id or manifest.dataset_version != context.new_dataset_version:
        raise AuthoringLifecycleError("correction manifest identity does not match CorrectionContext")
    if manifest.supersedes != context.prior_dataset_version or manifest.correction_reason != context.reason:
        raise AuthoringLifecycleError("correction manifest must bind prior version and correction reason")
    if manifest.dataset_digest == context.prior_dataset_digest:
        raise AuthoringLifecycleError("correction must have a new dataset digest")
    prior_by_id = {case.case_id: case for case in prior_release.cases}
    for candidate in candidates:
        case = candidate.reviewed_case
        if case is None:
            raise AuthoringLifecycleError("correction candidate is missing its reviewed EvaluationCase")
        provenance = case.provenance
        review = case.review
        if provenance.question_frozen_at <= context.initiated_by.occurred_at:
            raise AuthoringLifecycleError(
                "new dataset versions must rebuild question freeze after correction initiation"
            )
        if provenance.annotation_started_at <= context.initiated_by.occurred_at:
            raise AuthoringLifecycleError("new dataset versions must be annotated again after correction initiation")
        if review.reviewed_at is None or review.reviewed_at <= context.initiated_by.occurred_at:
            raise AuthoringLifecycleError("new dataset versions must be reviewed again after correction initiation")
        old = prior_by_id.get(candidate.prior_case.case_id) if candidate.prior_case is not None else None
        if case.case_id in prior_by_id and old is None:
            raise AuthoringLifecycleError("a retained prior case requires an exact prior_case binding")
        if old is not None:
            expected = PriorCaseReference.from_case(old, dataset_digest=context.prior_dataset_digest)
            if candidate.prior_case != expected:
                raise AuthoringLifecycleError("candidate prior_case binding does not match the frozen prior case")
            if review.evidence_reference == old.review.evidence_reference:
                raise AuthoringLifecycleError("new dataset version cannot carry forward prior review evidence")
    retained_prior_ids = {candidate.prior_case.case_id for candidate in candidates if candidate.prior_case is not None}
    removed_prior_ids = set(prior_by_id) - retained_prior_ids
    disposition_by_id = {item.related_case_id: item for item in dispositions}
    if len(disposition_by_id) != len(dispositions):
        raise AuthoringLifecycleError("correction dispositions contain duplicate related_case_id values")
    if set(disposition_by_id) != removed_prior_ids:
        raise AuthoringLifecycleError(
            "removed prior cases require exact dispositions; "
            f"missing={sorted(removed_prior_ids - set(disposition_by_id))}, "
            f"unexpected={sorted(set(disposition_by_id) - removed_prior_ids)}"
        )
    for case_id, disposition in disposition_by_id.items():
        expected = PriorCaseReference.from_case(prior_by_id[case_id], dataset_digest=context.prior_dataset_digest)
        if disposition.prior_case != expected or disposition.original_case_id != case_id:
            raise AuthoringLifecycleError("removed-case disposition lost its exact prior case association")
    return CorrectionBinding(
        prior_dataset_version=context.prior_dataset_version,
        prior_dataset_digest=context.prior_dataset_digest,
        prior_release_digest=context.prior_release_digest,
        new_dataset_version=context.new_dataset_version,
        new_dataset_digest=manifest.dataset_digest,
        reason=context.reason,
        initiated_by=context.initiated_by,
    )


def freeze_dataset(
    *,
    manifest: DatasetManifest,
    reviewed_candidates: Sequence[AuthoringCandidate],
    published_by: AuthoringAction,
    prior_release: FrozenDatasetRelease | None = None,
    correction_context: CorrectionContext | None = None,
    dispositions: Sequence[CandidateDisposition] = (),
) -> FrozenDatasetRelease:
    """Freeze only an already human-reviewed case set and validated manifest."""

    if not reviewed_candidates:
        raise AuthoringLifecycleError("cannot freeze an empty reviewed candidate set")
    by_id: dict[str, AuthoringCandidate] = {}
    for candidate in reviewed_candidates:
        _require_active(candidate)
        if candidate.lifecycle_state != "reviewed" or candidate.reviewed_case is None:
            raise AuthoringLifecycleError("freeze requires reviewed candidates with EvaluationCase values")
        if candidate.case_id in by_id:
            raise AuthoringLifecycleError(f"duplicate reviewed candidate: {candidate.case_id}")
        by_id[candidate.case_id] = candidate
    ordered_ids = [entry.case_id for entry in manifest.case_manifest]
    if set(by_id) != set(ordered_ids):
        raise AuthoringLifecycleError("reviewed candidate membership does not match manifest.case_manifest")
    ordered_candidates = tuple(by_id[case_id] for case_id in ordered_ids)
    cases = tuple(candidate.reviewed_case for candidate in ordered_candidates)
    if any(case is None for case in cases):
        raise AuthoringLifecycleError("reviewed candidate is missing EvaluationCase")
    typed_cases = tuple(case for case in cases if case is not None)
    manifest.validate_cases(typed_cases)
    correction: CorrectionBinding | None = None
    if correction_context is None:
        if prior_release is not None or manifest.supersedes is not None or manifest.correction_reason is not None:
            raise AuthoringLifecycleError("superseding releases require prior_release and CorrectionContext")
        if dispositions:
            raise AuthoringLifecycleError("base releases cannot attach prior-version dispositions")
    else:
        if prior_release is None:
            raise AuthoringLifecycleError("correction freeze requires the immutable prior release")
        correction = _validate_correction_candidates(
            context=correction_context,
            prior_release=prior_release,
            candidates=ordered_candidates,
            dispositions=dispositions,
            manifest=manifest,
        )
    return FrozenDatasetRelease.create(
        lifecycle_state="frozen",
        manifest=manifest,
        cases=typed_cases,
        questions=tuple(case.question_record() for case in typed_cases),
        references=tuple(case.reference_record() for case in typed_cases),
        dispositions=tuple(dispositions),
        published_by=published_by,
        correction=correction,
    )


def assert_frozen_release_unchanged(
    release: FrozenDatasetRelease,
    *,
    observed_manifest: DatasetManifest | None = None,
    observed_cases: Sequence[EvaluationCase] | None = None,
) -> None:
    """Reject same-version replacement or projection/member tampering."""

    release.verify_integrity()
    if observed_manifest is not None:
        if (
            observed_manifest.dataset_id != release.manifest.dataset_id
            or observed_manifest.dataset_version != release.manifest.dataset_version
            or observed_manifest.dataset_digest != release.manifest.dataset_digest
        ):
            raise AuthoringLifecycleError("frozen dataset version was replaced or its digest changed")
    if observed_cases is not None:
        observed = tuple(observed_cases)
        expected = release.cases
        if len(observed) != len(expected) or any(
            left.case_id != right.case_id or left.case_digest != right.case_digest
            for left, right in zip(observed, expected, strict=True)
        ):
            raise AuthoringLifecycleError("frozen case membership or content changed")
        release.manifest.validate_cases(observed)


import_candidate = import_question_candidate
freeze_release = freeze_dataset
verify_frozen_release = assert_frozen_release_unchanged


__all__ = [
    "ActionName", "ActorSource", "AuthoringAction", "AuthoringCandidate",
    "AuthoringLifecycleError", "CandidateDisposition", "CandidateImport",
    "CandidateLifecycleState", "CorrectionBinding", "CorrectionContext",
    "DispositionStatus", "FrozenDatasetRelease", "PriorCaseReference",
    "QuestionFreezeRecord", "annotate_candidate", "assert_frozen_release_unchanged",
    "begin_dataset_correction", "freeze_dataset", "freeze_release", "import_candidate",
    "import_correction_candidate", "import_question_candidate", "record_candidate_disposition",
    "record_prior_case_disposition", "review_candidate", "revise_candidate_question",
    "verify_frozen_release",
]
