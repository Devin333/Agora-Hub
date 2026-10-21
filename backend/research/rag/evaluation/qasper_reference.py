"""Answer-level gold references built from QASPER's own annotations.

QASPER gives several independent answers per question; each annotator's answer
is one acceptable alternative (scores take the best alternative, as in the
official evaluation). For every annotation this module builds a
``ReferenceLabel`` whose review authority is ``dataset_gold``:

* yes/no answers become one claim with a deterministic polarity fact;
* extractive spans become one claim each (verbatim paper text);
* free-form answers become one claim;
* "unanswerable" becomes an unanswerable label (expected to abstain).

Evidence strings are anchored to the checked paper text; figure/table evidence
("FLOAT SELECTED") carries no text and is not anchored. A claim without any
text anchor cannot be verified, so its annotation is kept as needs_review
instead of being approved.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from backend.research.domain.document import ResearchDocument
from backend.research.rag.evaluation.protocol_common import (
    ImmutableVersionStr,
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import (
    AlternativeSufficientSet,
    AuthorInputManifest,
    CaseProvenance,
    EvidenceAnchor,
    EvidenceLocation,
    GoldAnnotationSource,
    QuestionInput,
    QuestionScope,
    ReferenceClaim,
    ReferenceLabel,
    ReferenceReview,
    RequiredEvidenceGroup,
    ScopedPaper,
    SourceRecord,
)
from backend.research.rag.evaluation.protocol_evidence_mapping import CheckedSourceText
from backend.research.rag.evaluation.protocol_fact_projections import ReferenceFactProjection
from backend.research.rag.evaluation.protocol_semantic_facts import SemanticFact
from backend.research.rag.evaluation.qasper_mapping import (
    build_checked_source,
    iter_qasper_annotations,
    iter_qasper_papers,
    iter_qasper_questions,
    locate_unique_span,
    qasper_answer_content,
)

QASPER_REFERENCE_BUILDER_VERSION = "qasper-answer-gold/v1"
YES_NO_OBJECT_ID = "question"
YES_NO_STATISTIC = "answer"
_YES_NO_RULE_ID = "qasper-yes-no-gold"


class QasperGoldIdentity(ProtocolModel):
    """Published identity and collection facts of the QASPER release used.

    The two collection timestamps come from the dataset's documented protocol
    (questions were written from title and abstract before full-text answering
    started); they are inputs, never invented here.
    """

    dataset_id: NonEmptyStr = "qasper"
    dataset_version: ImmutableVersionStr
    license_reference_id: NonEmptyStr
    source_uri_prefix: NonEmptyStr = "https://arxiv.org/abs/"
    questions_frozen_at: datetime
    answers_started_at: datetime
    retrieved_at: datetime

    @model_validator(mode="after")
    def _ordered(self) -> "QasperGoldIdentity":
        for value in (self.questions_frozen_at, self.answers_started_at, self.retrieved_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("QASPER collection timestamps must be timezone-aware")
        if self.answers_started_at <= self.questions_frozen_at:
            raise ValueError("full-text answering must start after questions were frozen")
        return self


class QasperGoldAlternative(ProtocolModel):
    annotation_id: NonEmptyStr
    answer_type: NonEmptyStr
    reference: ReferenceLabel
    fixed_reference_facts: tuple[ReferenceFactProjection, ...] = ()


class SkippedAnnotation(ProtocolModel):
    annotation_id: NonEmptyStr
    reason: NonEmptyStr


class QasperGoldCase(ProtocolModel):
    case_id: NonEmptyStr
    paper_id: NonEmptyStr
    question: NonEmptyStr
    question_type: NonEmptyStr
    scope: QuestionScope
    alternatives: tuple[QasperGoldAlternative, ...] = Field(min_length=1)
    skipped: tuple[SkippedAnnotation, ...] = ()


class QasperGoldDataset(ProtocolModel):
    builder_version: NonEmptyStr = QASPER_REFERENCE_BUILDER_VERSION
    identity: QasperGoldIdentity
    dataset_digest: Sha256Digest
    cases: tuple[QasperGoldCase, ...]
    skipped_questions: tuple[SkippedAnnotation, ...] = ()
    digest: Sha256Digest

    def verify_digest(self) -> None:
        if self.digest != content_digest(self.model_dump(mode="json", exclude={"digest"})):
            raise ValueError("QASPER gold dataset digest mismatch")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def yes_no_claim_text(question: str, answer: bool) -> str:
    return f"The answer to the question \"{question}\" is {'yes' if answer else 'no'}."


def _claims_for(question: str, answer: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, bool | None], ...]]:
    """(answer type, ((claim text, yes/no polarity or None), ...)) for one annotation."""

    yes_no = answer.get("yes_no")
    if isinstance(yes_no, bool):
        return "yes_no", ((yes_no_claim_text(question, yes_no), yes_no),)
    spans = answer.get("extractive_spans") or ()
    if isinstance(spans, Sequence) and not isinstance(spans, (str, bytes)):
        unique = tuple(dict.fromkeys(_text(item) for item in spans if _text(item)))
        if unique:
            return "extractive", tuple((item, None) for item in unique)
    free_form = _text(answer.get("free_form_answer"))
    if free_form:
        return "free_form", ((free_form, None),)
    return "empty", ()


def _evidence_excerpts(answer: Mapping[str, Any]) -> tuple[tuple[str, ...], int]:
    raw = answer.get("evidence") or ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return (), 0
    texts: list[str] = []
    floats = 0
    for item in raw:
        excerpt = str(item).strip()
        if not excerpt:
            continue
        if excerpt.casefold().startswith("float selected"):
            floats += 1
            continue
        if excerpt not in texts:
            texts.append(excerpt)
    return tuple(texts), floats


def _section_for(span: tuple[int, int], sections: Mapping[str, tuple[int, int]]) -> str | None:
    matching = [name for name, (start, end) in sections.items() if start <= span[0] and span[1] <= end]
    return matching[0] if len(matching) == 1 else None


def _anchors(
    excerpts: Sequence[str],
    *,
    prefix: str,
    claim_ids: tuple[str, ...],
    paper_id: str,
    source: CheckedSourceText,
    sections: Mapping[str, tuple[int, int]],
) -> tuple[EvidenceAnchor, ...]:
    anchors: list[EvidenceAnchor] = []
    for excerpt in excerpts:
        span = locate_unique_span(source.normalized_text, excerpt)
        section = _section_for(span, sections) if span is not None else None
        if span is None:
            # Evidence that does not occur exactly once in the checked text is
            # not a reproducible anchor.
            continue
        anchors.append(EvidenceAnchor(
            anchor_id=f"{prefix}-anchor-{_sha(excerpt)[:16]}",
            paper_id=paper_id,
            document_version=source.document_version,
            source_sha256=source.source_sha256,
            claim_ids=claim_ids,
            location=EvidenceLocation(
                content_type="text", pdf_page_index=None, printed_page=None,
                section=section or "unresolved-qasper-section", paragraph_id=None,
                char_start=None, char_end=None, element_id=None, table_id=None,
                row_labels=(), column_labels=(), cell_references=(), figure_id=None,
                caption=None, normalized_fulltext_sha256=source.normalized_text_sha256,
            ),
            source_excerpt=excerpt,
            source_excerpt_digest=content_digest(excerpt),
        ))
    return tuple(anchors)


def _provenance(
    identity: QasperGoldIdentity,
    *,
    case_id: str,
    question_id: str,
    question_digest: str,
    annotation_id: str,
    annotator_id: str,
    source_record: SourceRecord,
) -> CaseProvenance:
    return CaseProvenance(
        authoring_method="abstract_only",
        original_dataset_id=identity.dataset_id,
        original_case_id=question_id,
        derived_from_case_ids=(),
        question_author_id=f"qasper-question-writer:{question_id}",
        author_input_manifest=AuthorInputManifest(
            visible_materials=("title", "abstract", "declared_scope"),
            input_digest=content_digest({"case_id": case_id, "materials": "title+abstract"}),
        ),
        question_digest=question_digest,
        question_frozen_at=identity.questions_frozen_at,
        annotator_id=f"qasper-annotator:{annotator_id}",
        annotation_input_manifest=AuthorInputManifest(
            visible_materials=("fulltext", "declared_scope"),
            input_digest=content_digest({"case_id": case_id, "annotation_id": annotation_id}),
        ),
        annotation_started_at=identity.answers_started_at,
        assistance=(),
        sources=(source_record,),
    )


def _review(
    identity: QasperGoldIdentity,
    *,
    dataset_digest: str,
    case_id: str,
    question_id: str,
    annotation_id: str,
    annotator_count: int,
    scope: QuestionScope,
    approved: bool,
    unanswerable: bool,
    note: str,
    fields: tuple[str, ...],
) -> ReferenceReview:
    return ReferenceReview(
        status="approved" if approved else "needs_review",
        authority="dataset_gold",
        reviewer_id=QASPER_REFERENCE_BUILDER_VERSION if approved else None,
        reviewer_kind="rule" if approved else None,
        reviewed_at=identity.retrieved_at if approved else None,
        notes=note,
        source_completeness="complete" if approved else "unknown",
        checked_scope=scope if approved else None,
        unanswerable_reason=(
            "QASPER annotator marked the question unanswerable from the full text"
            if unanswerable else None
        ),
        evidence_reference=(
            f"dataset://{identity.dataset_id}/{identity.dataset_version}/{case_id}#{annotation_id}"
            if approved else None
        ),
        gold_source=GoldAnnotationSource(
            dataset_id=identity.dataset_id,
            dataset_version=identity.dataset_version,
            dataset_digest=dataset_digest,
            original_case_id=question_id,
            annotation_fields=fields,
            annotator_count=annotator_count,
        ),
    )


def _yes_no_fact(claim_id: str, claim_text: str, polarity: bool, anchor_ids: tuple[str, ...]) -> ReferenceFactProjection:
    return ReferenceFactProjection(
        reference_claim_id=claim_id,
        fact=SemanticFact(
            exact_quote=claim_text,
            polarity=polarity,
            object_id=YES_NO_OBJECT_ID,
            statistic=YES_NO_STATISTIC,
        ),
        evidence_anchor_ids=anchor_ids,
        rule_id=_YES_NO_RULE_ID,
        rule_version=QASPER_REFERENCE_BUILDER_VERSION,
    )


def build_qasper_gold(
    raw: Mapping[str, Any],
    documents: Sequence[ResearchDocument],
    identity: QasperGoldIdentity,
) -> QasperGoldDataset:
    """Every QASPER question with at least one usable annotation, as gold cases."""

    _, _, papers = iter_qasper_papers(raw)
    documents_by_id = {item.paper_id: item for item in documents}
    if set(documents_by_id) != set(papers) or len(documents_by_id) != len(documents):
        raise ValueError("documents must match the raw QASPER papers one-to-one")
    dataset_digest = content_digest(raw)
    cases: list[QasperGoldCase] = []
    skipped_questions: list[SkippedAnnotation] = []
    for paper_id, paper in papers.items():
        source, sections = build_checked_source(documents_by_id[paper_id])
        scope = QuestionScope(sources=(ScopedPaper(
            paper_id=paper_id, document_version=source.document_version, source_sha256=source.source_sha256,
        ),))
        source_record = SourceRecord(
            paper_id=paper_id,
            document_version=source.document_version,
            source_uri=f"{identity.source_uri_prefix}{paper_id}",
            source_sha256=source.source_sha256,
            retrieved_at=identity.retrieved_at,
            fulltext_artifact_digest=source.normalized_text_sha256,
            completeness="complete",
            access_status="available",
            source_format="other",
            license_reference_id=identity.license_reference_id,
        )
        for index, qa in enumerate(iter_qasper_questions(paper)):
            question_id = str(qa.get("question_id") or qa.get("id") or index)
            case_id = f"{paper_id}:{question_id}"
            question = _text(qa.get("question") or qa.get("query"))
            if not question:
                skipped_questions.append(SkippedAnnotation(annotation_id=case_id, reason="empty question"))
                continue
            question_digest = content_digest(QuestionInput(question_text=question, scope=scope))
            annotations = iter_qasper_annotations(qa)
            alternatives: list[QasperGoldAlternative] = []
            skipped: list[SkippedAnnotation] = []
            for position, annotation in enumerate(annotations):
                answer = qasper_answer_content(annotation)
                annotation_id = str(annotation.get("annotation_id") or f"annotation-{position + 1}")
                annotator_id = str(annotation.get("worker_id") or annotation_id)
                prefix = f"{case_id}#{annotation_id}"
                provenance = _provenance(
                    identity, case_id=case_id, question_id=question_id, question_digest=question_digest,
                    annotation_id=annotation_id, annotator_id=annotator_id, source_record=source_record,
                )
                common = dict(
                    dataset_digest=dataset_digest, case_id=case_id, question_id=question_id,
                    annotation_id=annotation_id, annotator_count=len(annotations), scope=scope,
                )
                if answer.get("unanswerable"):
                    alternatives.append(QasperGoldAlternative(
                        annotation_id=annotation_id,
                        answer_type="unanswerable",
                        reference=ReferenceLabel(
                            answerability="unanswerable",
                            expected_behavior="abstain",
                            reference_answer=None,
                            reference_claims=(),
                            evidence_anchors=(),
                            required_evidence_groups=(),
                            provenance=provenance,
                            review=_review(
                                identity, **common, approved=True, unanswerable=True,
                                note="QASPER annotation: unanswerable", fields=("unanswerable",),
                            ),
                        ),
                    ))
                    continue
                answer_type, claim_specs = _claims_for(question, answer)
                if not claim_specs:
                    skipped.append(SkippedAnnotation(annotation_id=annotation_id, reason="annotation has no answer"))
                    continue
                claims = tuple(
                    ReferenceClaim(claim_id=f"{prefix}:claim-{number}", claim_text=text)
                    for number, (text, _) in enumerate(claim_specs, start=1)
                )
                claim_ids = tuple(item.claim_id for item in claims)
                excerpts, float_count = _evidence_excerpts(answer)
                anchors = _anchors(
                    excerpts, prefix=prefix, claim_ids=claim_ids,
                    paper_id=paper_id, source=source, sections=sections,
                )
                approved = bool(anchors)
                if approved:
                    groups = tuple(
                        RequiredEvidenceGroup(
                            group_id=f"{claim.claim_id}:evidence",
                            claim_ids=(claim.claim_id,),
                            alternative_sufficient_sets=(AlternativeSufficientSet(
                                set_id=f"{claim.claim_id}:annotator-evidence",
                                required_anchor_ids=tuple(item.anchor_id for item in anchors),
                            ),),
                        )
                        for claim in claims
                    )
                    note = f"QASPER {answer_type} annotation anchored to {len(anchors)} text evidence span(s)"
                else:
                    groups = ()
                    note = (
                        "QASPER annotation has no text evidence that occurs exactly once in the paper"
                        + (f" ({float_count} figure/table evidence item(s) carry no text)" if float_count else "")
                    )
                fields = {
                    "yes_no": ("yes_no", "evidence"),
                    "extractive": ("extractive_spans", "evidence"),
                    "free_form": ("free_form_answer", "evidence"),
                }[answer_type]
                fixed = tuple(
                    _yes_no_fact(claim.claim_id, claim.claim_text, polarity, tuple(item.anchor_id for item in anchors))
                    for claim, (_, polarity) in zip(claims, claim_specs, strict=True)
                    if polarity is not None and anchors
                )
                alternatives.append(QasperGoldAlternative(
                    annotation_id=annotation_id,
                    answer_type=answer_type,
                    reference=ReferenceLabel(
                        answerability="answerable" if approved else "needs_review",
                        expected_behavior="answer" if approved else None,
                        reference_answer=" ".join(item.claim_text for item in claims) if approved else None,
                        reference_claims=claims,
                        evidence_anchors=anchors,
                        required_evidence_groups=groups,
                        provenance=provenance,
                        review=_review(
                            identity, **common, approved=approved, unanswerable=False,
                            note=note, fields=fields,
                        ),
                    ),
                    fixed_reference_facts=fixed,
                ))
            if not alternatives:
                skipped_questions.append(SkippedAnnotation(
                    annotation_id=case_id, reason="question has no usable annotation",
                ))
                continue
            cases.append(QasperGoldCase(
                case_id=case_id,
                paper_id=paper_id,
                question=question,
                question_type=str(qa.get("question_type") or "unknown"),
                scope=scope,
                alternatives=tuple(alternatives),
                skipped=tuple(skipped),
            ))
    draft = QasperGoldDataset(
        identity=identity,
        dataset_digest=dataset_digest,
        cases=tuple(cases),
        skipped_questions=tuple(skipped_questions),
        digest="0" * 64,
    )
    dataset = draft.model_copy(update={
        "digest": content_digest(draft.model_dump(mode="json", exclude={"digest"})),
    })
    dataset.verify_digest()
    return dataset


__all__ = [
    "QASPER_REFERENCE_BUILDER_VERSION",
    "YES_NO_OBJECT_ID",
    "YES_NO_STATISTIC",
    "QasperGoldAlternative",
    "QasperGoldCase",
    "QasperGoldDataset",
    "QasperGoldIdentity",
    "SkippedAnnotation",
    "build_qasper_gold",
    "yes_no_claim_text",
]
