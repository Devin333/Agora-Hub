from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.protocol_authoring import (
    AuthoringAction,
    AuthoringLifecycleError,
    CandidateImport,
    QuestionFreezeRecord,
    annotate_candidate,
    assert_frozen_release_unchanged,
    begin_dataset_correction,
    freeze_dataset,
    import_correction_candidate,
    import_question_candidate,
    record_candidate_disposition,
    record_prior_case_disposition,
    review_candidate,
    revise_candidate_question,
)
from backend.research.rag.evaluation.protocol_common import PROTOCOL_VERSION, SCHEMA_VERSION, content_digest
from backend.research.rag.evaluation.protocol_dataset import (
    AssistanceRecord,
    AuthorInputManifest,
    CaseProvenance,
    DatasetManifest,
    ExposureHistory,
    HumanReview,
    QuestionInput,
    QuestionScope,
    ReferenceLabel,
    ScopedPaper,
    SourceRecord,
)

T0 = datetime(2026, 2, 1, 10, 0, tzinfo=timezone.utc)
SOURCE_SHA = "a" * 64
FULLTEXT_SHA = "b" * 64


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _action(
    action: str,
    minutes: int,
    *,
    actor_id: str,
    kind: str = "human",
    source: str = "authenticated_human",
    evidence: str | None = None,
) -> AuthoringAction:
    return AuthoringAction(
        action=action,
        actor_kind=kind,
        actor_id=actor_id,
        actor_source=source,
        occurred_at=_at(minutes),
        evidence_reference=evidence or f"artifact://authoring/{action}/{minutes}",
    )


def _scope() -> QuestionScope:
    return QuestionScope(
        sources=(ScopedPaper(paper_id="paper-1", document_version="v1", source_sha256=SOURCE_SHA),)
    )


def _source(*, completeness: str = "complete", access_status: str = "available") -> SourceRecord:
    return SourceRecord(
        paper_id="paper-1",
        document_version="v1",
        source_uri="https://example.test/paper-1-v1.pdf",
        source_sha256=SOURCE_SHA,
        retrieved_at=_at(0),
        fulltext_artifact_digest=FULLTEXT_SHA,
        completeness=completeness,
        access_status=access_status,
        source_format="pdf",
        license_reference_id="license-1",
    )


def _exposure(*, status: str = "clean") -> ExposureHistory:
    return ExposureHistory(
        status=status,
        events=(),
        assessed_at=_at(0),
        assessed_by="exposure-reviewer",
        assessment_evidence_reference="artifact://exposure/assessment" if status == "clean" else None,
    )


def _assistance() -> AssistanceRecord:
    return AssistanceRecord(
        actor_kind="llm",
        actor_id="draft-helper",
        model_or_rule_version="model/v1",
        input_scope=("title", "abstract", "declared_scope"),
        generation_config_digest="c" * 64,
    )


def _request(
    *,
    dataset_version: str = "1.0.0",
    split: str = "dev",
    question_text: str = "Which objective does the paper optimize?",
    freeze_minute: int = 1,
    action_name: str = "import_question",
    source: SourceRecord | None = None,
    exposure: ExposureHistory | None = None,
    original_dataset_id: str | None = None,
    original_case_id: str | None = None,
    derived_from_case_ids: tuple[str, ...] = (),
) -> CandidateImport:
    question = QuestionInput(question_text=question_text, scope=_scope())
    freeze = QuestionFreezeRecord(
        question_input=question,
        question_digest=content_digest(question),
        authoring_method="abstract_only",
        question_author_id="question-author",
        author_input_manifest=AuthorInputManifest(
            visible_materials=("title", "abstract", "declared_scope"), input_digest="d" * 64
        ),
        frozen_at=_at(freeze_minute),
        imported_by=_action(action_name, freeze_minute, actor_id="question-author"),
        original_dataset_id=original_dataset_id,
        original_case_id=original_case_id,
        derived_from_case_ids=derived_from_case_ids,
        assistance=(_assistance(),),
        sources=(source or _source(),),
    )
    return CandidateImport(
        dataset_id="dataset-1",
        dataset_version=dataset_version,
        case_id="case-1",
        question_type="method",
        paper_group_id="family-1",
        split=split,
        question_freeze=freeze,
        exposure_history=exposure or _exposure(),
        prior_case=None,
    )


def _provenance(request: CandidateImport, *, annotation_minute: int = 2) -> CaseProvenance:
    freeze = request.question_freeze
    return CaseProvenance(
        authoring_method=freeze.authoring_method,
        original_dataset_id=freeze.original_dataset_id,
        original_case_id=freeze.original_case_id,
        derived_from_case_ids=freeze.derived_from_case_ids,
        question_author_id=freeze.question_author_id,
        author_input_manifest=freeze.author_input_manifest,
        question_digest=freeze.question_digest,
        question_frozen_at=freeze.frozen_at,
        annotator_id="annotator",
        annotation_input_manifest=AuthorInputManifest(
            visible_materials=("fulltext", "declared_scope"), input_digest="e" * 64
        ),
        annotation_started_at=_at(annotation_minute),
        assistance=freeze.assistance,
        sources=freeze.sources,
    )


def _pending_review(
    *,
    status: str = "pending",
    completeness: str = "complete",
    checked: bool = False,
    unanswerable_reason: str | None = None,
) -> HumanReview:
    return HumanReview(
        status=status,
        reviewer_id=None,
        reviewer_kind=None,
        reviewed_at=None,
        notes="Pending independent human review.",
        source_completeness=completeness,
        checked_scope=_scope() if checked else None,
        unanswerable_reason=unanswerable_reason,
        evidence_reference=None,
    )


def _answerable_reference(provenance: CaseProvenance, *, review: HumanReview | None = None) -> ReferenceLabel:
    excerpt = "The paper optimizes an evidence-grounded contrastive objective."
    return ReferenceLabel(
        answerability="answerable",
        expected_behavior="answer",
        reference_answer="It optimizes an evidence-grounded contrastive objective.",
        reference_claims=(
            {"claim_id": "claim-1", "claim_text": "The objective is evidence-grounded contrastive learning."},
        ),
        evidence_anchors=(
            {
                "anchor_id": "anchor-1",
                "paper_id": "paper-1",
                "document_version": "v1",
                "source_sha256": SOURCE_SHA,
                "claim_ids": ("claim-1",),
                "location": {
                    "content_type": "text",
                    "pdf_page_index": 2,
                    "printed_page": "3",
                    "section": "Method",
                    "paragraph_id": "method-p1",
                    "char_start": 10,
                    "char_end": 70,
                    "element_id": None,
                    "table_id": None,
                    "row_labels": (),
                    "column_labels": (),
                    "cell_references": (),
                    "figure_id": None,
                    "caption": None,
                    "normalized_fulltext_sha256": FULLTEXT_SHA,
                },
                "source_excerpt": excerpt,
                "source_excerpt_digest": content_digest(excerpt),
            },
        ),
        required_evidence_groups=(
            {
                "group_id": "group-1",
                "claim_ids": ("claim-1",),
                "alternative_sufficient_sets": (
                    {"set_id": "set-1", "required_anchor_ids": ("anchor-1",)},
                ),
            },
        ),
        provenance=provenance,
        review=review or _pending_review(),
    )


def _approved_review(*, minute: int = 4, evidence: str = "artifact://review/case-1") -> HumanReview:
    return HumanReview(
        status="approved",
        reviewer_id="case-reviewer",
        reviewer_kind="human",
        reviewed_at=_at(minute),
        notes="Question, answer, claims, evidence, and answerability were checked.",
        source_completeness="complete",
        checked_scope=_scope(),
        unanswerable_reason=None,
        evidence_reference=evidence,
    )


def _annotated(request: CandidateImport, *, annotation_minute: int = 2):
    candidate = import_question_candidate(request)
    provenance = _provenance(request, annotation_minute=annotation_minute)
    return annotate_candidate(
        candidate,
        provenance=provenance,
        reference_label=_answerable_reference(provenance),
        annotated_by=_action("annotate_fulltext", annotation_minute + 1, actor_id="annotator"),
    )


def _reviewed(
    request: CandidateImport | None = None,
    *,
    annotation_minute: int = 2,
    review_minute: int = 4,
    evidence: str = "artifact://review/case-1",
):
    annotated = _annotated(request or _request(), annotation_minute=annotation_minute)
    review = _approved_review(minute=review_minute, evidence=evidence)
    return review_candidate(
        annotated,
        review=review,
        reviewed_by=_action("review_case", review_minute, actor_id="case-reviewer", evidence=evidence),
    )



def _manifest(
    reviewed,
    *,
    dataset_version: str | None = None,
    frozen_minute: int = 6,
    supersedes: str | None = None,
    correction_reason: str | None = None,
) -> DatasetManifest:
    case = reviewed.reviewed_case
    assert case is not None
    version = dataset_version or case.dataset_version
    split = case.split
    return DatasetManifest.create(
        record_type="dataset",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        dataset_id=case.dataset_id,
        dataset_version=version,
        lifecycle_state="frozen",
        release_purpose="heldout_qualification" if split == "heldout" else "dev_regression",
        corpus_snapshot_id=f"corpus/{version}",
        corpus_snapshot_digest="f" * 64,
        paper_versions=case.provenance.sources,
        case_manifest=(
            {
                "case_id": case.case_id,
                "case_digest": case.case_digest,
                "question_digest": content_digest(case.question_projection()),
                "reference_digest": content_digest(case.reference_projection()),
                "paper_group_id": case.paper_group_id,
                "split": split,
                "review_status": case.review.status,
            },
        ),
        split_manifest={
            "method": "paper-family",
            "method_version": "split/v1",
            "deterministic_seed": "fixed-seed",
            "groups": (
                {
                    "paper_group_id": case.paper_group_id,
                    "split": split,
                    "sources": case.scope.sources,
                    "grouping_basis": "same paper/version family",
                    "dispute_resolution": None,
                },
            ),
        },
        review_manifest={
            "status": "approved",
            "reviewer_id": "dataset-reviewer",
            "reviewer_kind": "human",
            "reviewed_at": _at(frozen_minute - 1),
            "approved_case_ids": (case.case_id,),
            "corpus_checked": True,
            "split_checked": True,
            "contract_checked": True,
            "evidence_reference": f"artifact://review/dataset/{version}",
        },
        annotation_version=f"annotation/{version}",
        license_reference={
            "reference_id": "license-1",
            "reference_uri": "https://example.test/license",
            "terms_digest": "1" * 64,
            "permitted_use": "internal_evaluation",
        },
        authoring_protocol={
            "protocol_id": "question-first",
            "protocol_version": "authoring/v1",
            "protocol_digest": "2" * 64,
        },
        supersedes=supersedes,
        correction_reason=correction_reason,
        frozen_at=_at(frozen_minute),
    )


def _release(reviewed=None, *, frozen_minute: int = 6):
    candidate = reviewed or _reviewed()
    manifest = _manifest(candidate, frozen_minute=frozen_minute)
    return freeze_dataset(
        manifest=manifest,
        reviewed_candidates=(candidate,),
        published_by=_action("freeze_dataset", frozen_minute, actor_id="dataset-reviewer"),
    )


def test_question_first_lifecycle_reaches_read_only_separated_release() -> None:
    request = _request()
    draft = import_question_candidate(request)
    assert draft.lifecycle_state == "draft"
    assert draft.question_freeze.question_digest == content_digest(draft.question_freeze.question_input)
    assert draft.reference_label is None

    annotated = _annotated(request)
    assert annotated.lifecycle_state == "annotated"
    assert annotated.reference_label is not None
    assert annotated.reference_label.review.status == "pending"

    reviewed = _reviewed(request)
    assert reviewed.lifecycle_state == "reviewed"
    release = _release(reviewed)
    assert release.lifecycle_state == "frozen"
    assert release.manifest.dataset_digest == release.manifest.calculated_digest()
    assert release.release_digest == release.calculated_digest()
    assert release.questions == (reviewed.reviewed_case.question_record(),)
    assert release.references == (reviewed.reviewed_case.reference_record(),)
    assert "reference_answer" not in release.questions[0].model_dump_json()
    with pytest.raises(ValidationError):
        release.manifest.dataset_version = "mutated"  # type: ignore[misc]


def test_cross_stage_skips_and_question_first_input_leaks_are_rejected() -> None:
    draft = import_question_candidate(_request())
    with pytest.raises(AuthoringLifecycleError, match="only annotated"):
        review_candidate(
            draft,
            review=_approved_review(),
            reviewed_by=_action("review_case", 4, actor_id="case-reviewer", evidence="artifact://review/case-1"),
        )
    question = QuestionInput(question_text="Leaked target output?", scope=_scope())
    with pytest.raises(ValidationError, match="input boundary|retrieval output"):
        QuestionFreezeRecord(
            question_input=question,
            question_digest=content_digest(question),
            authoring_method="abstract_only",
            question_author_id="author",
            author_input_manifest=AuthorInputManifest(
                visible_materials=("title", "abstract", "declared_scope"), input_digest="3" * 64
            ),
            frozen_at=_at(1),
            imported_by=_action("import_question", 1, actor_id="author"),
            original_dataset_id=None,
            original_case_id=None,
            derived_from_case_ids=(),
            assistance=(
                AssistanceRecord(
                    actor_kind="llm",
                    actor_id="leaky-model",
                    model_or_rule_version="model/v1",
                    input_scope=("retrieval_output",),
                    generation_config_digest="4" * 64,
                ),
            ),
            sources=(_source(),),
        )


def test_annotation_cannot_autoapprove_and_llm_cannot_impersonate_review() -> None:
    request = _request()
    draft = import_question_candidate(request)
    provenance = _provenance(request)
    autoapproved = _answerable_reference(provenance, review=_approved_review())
    with pytest.raises(AuthoringLifecycleError, match="cannot auto-approve"):
        annotate_candidate(
            draft,
            provenance=provenance,
            reference_label=autoapproved,
            annotated_by=_action("annotate_fulltext", 3, actor_id="annotator"),
        )

    annotated = _annotated(request)
    with pytest.raises(AuthoringLifecycleError, match="named human"):
        review_candidate(
            annotated,
            review=_approved_review(),
            reviewed_by=_action(
                "review_case",
                4,
                actor_id="case-reviewer",
                kind="llm",
                source="llm_assistance",
                evidence="artifact://review/case-1",
            ),
        )


def test_incomplete_materials_cannot_be_labeled_unanswerable_and_needs_review_keeps_routing() -> None:
    request = _request(source=_source(completeness="incomplete"))
    draft = import_question_candidate(request)
    provenance = _provenance(request)
    unanswerable = ReferenceLabel(
        answerability="unanswerable",
        expected_behavior="abstain",
        reference_answer=None,
        reference_claims=(),
        evidence_anchors=(),
        required_evidence_groups=(),
        provenance=provenance,
        review=_pending_review(
            completeness="complete", checked=True, unanswerable_reason="The complete scope did not report it."
        ),
    )
    with pytest.raises(AuthoringLifecycleError, match="incomplete materials"):
        annotate_candidate(
            draft,
            provenance=provenance,
            reference_label=unanswerable,
            annotated_by=_action("annotate_fulltext", 3, actor_id="annotator"),
        )

    routed = record_candidate_disposition(
        draft,
        status="needs_review",
        destination="source-recovery-queue",
        reason="Required table is unavailable.",
        decided_by=_action("mark_needs_review", 3, actor_id="triage-reviewer"),
    )
    assert routed.disposition is not None
    assert routed.disposition.destination == "source-recovery-queue"
    assert routed.disposition.original_case_id == draft.case_id
    with pytest.raises(AuthoringLifecycleError, match="source-recovery-queue"):
        annotate_candidate(
            routed,
            provenance=provenance,
            reference_label=_answerable_reference(provenance),
            annotated_by=_action("annotate_fulltext", 3, actor_id="annotator"),
        )



def test_question_text_revision_resets_annotation_and_cannot_reuse_old_provenance_or_review() -> None:
    reviewed = _reviewed()
    revised_request = _request(
        question_text="Which exact training objective is minimized?",
        freeze_minute=5,
        action_name="revise_question",
        original_dataset_id="dataset-1",
        original_case_id="case-1",
        derived_from_case_ids=("case-1",),
    )
    revised = revise_candidate_question(reviewed, revised_request)
    assert revised.lifecycle_state == "draft"
    assert revised.reference_label is None
    assert revised.reviewed_case is None
    assert revised.prior_case is not None
    assert revised.prior_case.case_digest == reviewed.reviewed_case.case_digest
    assert revised.question_freeze.question_digest != reviewed.question_freeze.question_digest

    old_provenance = reviewed.reviewed_case.provenance
    with pytest.raises(AuthoringLifecycleError, match="source relationship|frozen question"):
        annotate_candidate(
            revised,
            provenance=old_provenance,
            reference_label=reviewed.reviewed_case.reference_projection(),
            annotated_by=_action("annotate_fulltext", 7, actor_id="annotator"),
        )


def test_unknown_exposure_cannot_be_recut_as_approved_heldout() -> None:
    request = _request(split="heldout", exposure=_exposure(status="unknown"))
    annotated = _annotated(request)
    with pytest.raises(ValidationError, match="known clean history"):
        review_candidate(
            annotated,
            review=_approved_review(),
            reviewed_by=_action("review_case", 4, actor_id="case-reviewer", evidence="artifact://review/case-1"),
        )


def test_freeze_requires_reviewed_exact_membership_and_detects_same_version_replacement() -> None:
    request = _request()
    annotated = _annotated(request)
    reviewed = _reviewed(request)
    manifest = _manifest(reviewed)
    with pytest.raises(AuthoringLifecycleError, match="reviewed candidates"):
        freeze_dataset(
            manifest=manifest,
            reviewed_candidates=(annotated,),
            published_by=_action("freeze_dataset", 6, actor_id="dataset-reviewer"),
        )

    release = _release(reviewed)
    replacement_content = release.manifest.model_dump(mode="python", exclude={"dataset_digest"})
    replacement_content["annotation_version"] = "annotation/same-version-replacement"
    replacement = DatasetManifest.create(**replacement_content)
    with pytest.raises(AuthoringLifecycleError, match="replaced or its digest changed"):
        assert_frozen_release_unchanged(release, observed_manifest=replacement)

    changed_case_content = reviewed.reviewed_case.model_dump(mode="python", exclude={"case_digest"})
    changed_case_content["question_type"] = "other"
    changed_case = reviewed.reviewed_case.create(**changed_case_content)
    with pytest.raises(AuthoringLifecycleError, match="membership or content changed"):
        assert_frozen_release_unchanged(release, observed_cases=(changed_case,))


def _corrected_reviewed(context, prior_case, *, evidence: str, review_minute: int = 12):
    request = _request(
        dataset_version=context.new_dataset_version,
        question_text="Which exact objective is minimized during training?",
        freeze_minute=10,
        original_dataset_id=prior_case.dataset_id,
        original_case_id=prior_case.case_id,
        derived_from_case_ids=(prior_case.case_id,),
    )
    draft = import_correction_candidate(context, prior_case, request)
    provenance = _provenance(request, annotation_minute=11)
    annotated = annotate_candidate(
        draft,
        provenance=provenance,
        reference_label=_answerable_reference(provenance),
        annotated_by=_action("annotate_fulltext", 11, actor_id="annotator"),
    )
    review = _approved_review(minute=review_minute, evidence=evidence)
    return review_candidate(
        annotated,
        review=review,
        reviewed_by=_action("review_case", review_minute, actor_id="case-reviewer", evidence=evidence),
    )


def test_correction_binds_prior_digests_and_requires_fresh_annotation_and_review() -> None:
    prior = _release()
    context = begin_dataset_correction(
        prior,
        new_dataset_version="2.0.0",
        reason="Correct question wording and re-check evidence.",
        initiated_by=_action("begin_correction", 8, actor_id="correction-owner"),
    )
    prior_case = prior.cases[0]
    carried_review = _corrected_reviewed(
        context,
        prior_case,
        evidence=prior_case.review.evidence_reference,
    )
    carried_manifest = _manifest(
        carried_reviewed := carried_review,
        dataset_version="2.0.0",
        frozen_minute=14,
        supersedes="1.0.0",
        correction_reason=context.reason,
    )
    with pytest.raises(AuthoringLifecycleError, match="carry forward prior review evidence"):
        freeze_dataset(
            manifest=carried_manifest,
            reviewed_candidates=(carried_reviewed,),
            published_by=_action("freeze_dataset", 14, actor_id="dataset-reviewer"),
            prior_release=prior,
            correction_context=context,
        )

    corrected = _corrected_reviewed(
        context,
        prior_case,
        evidence="artifact://review/case-1/version-2",
    )
    manifest = _manifest(
        corrected,
        dataset_version="2.0.0",
        frozen_minute=14,
        supersedes="1.0.0",
        correction_reason=context.reason,
    )
    release = freeze_dataset(
        manifest=manifest,
        reviewed_candidates=(corrected,),
        published_by=_action("freeze_dataset", 14, actor_id="dataset-reviewer"),
        prior_release=prior,
        correction_context=context,
    )
    assert release.correction is not None
    assert release.correction.prior_dataset_digest == prior.manifest.dataset_digest
    assert release.correction.prior_release_digest == prior.release_digest
    assert release.manifest.dataset_version == "2.0.0"
    assert release.manifest.dataset_digest != prior.manifest.dataset_digest
    assert_frozen_release_unchanged(prior)


def test_correction_requires_disposition_for_removed_prior_case_and_preserves_destination() -> None:
    prior = _release()
    context = begin_dataset_correction(
        prior,
        new_dataset_version="2.0.0",
        reason="Retire a case whose source license changed.",
        initiated_by=_action("begin_correction", 8, actor_id="correction-owner"),
    )
    disposition = record_prior_case_disposition(
        context,
        prior.cases[0],
        status="retired",
        destination="restricted-license-audit",
        reason="Source may no longer be used in evaluation.",
        decided_by=_action("retire_case", 9, actor_id="dataset-curator"),
    )
    assert disposition.destination == "restricted-license-audit"
    assert disposition.original_case_id == prior.cases[0].case_id
    assert disposition.prior_case is not None
    assert disposition.prior_case.case_digest == prior.cases[0].case_digest


@pytest.mark.parametrize(
    ("status", "action_name", "destination"),
    [
        ("needs_review", "mark_needs_review", "human-resolution-queue"),
        ("rejected", "reject_case", "rejected-candidate-audit"),
        ("retired", "retire_case", "retired-case-register"),
        ("revoked", "revoke_case", "revocation-register"),
    ],
)
def test_all_nonmember_dispositions_preserve_destination_and_case_link(
    status: str,
    action_name: str,
    destination: str,
) -> None:
    candidate = import_question_candidate(_request())
    routed = record_candidate_disposition(
        candidate,
        status=status,
        destination=destination,
        reason="Explicit curator decision.",
        decided_by=_action(action_name, 2, actor_id="dataset-curator"),
    )
    assert routed.disposition is not None
    assert routed.disposition.status == status
    assert routed.disposition.destination == destination
    assert routed.disposition.related_case_id == candidate.case_id
    assert routed.disposition.original_case_id == candidate.case_id
