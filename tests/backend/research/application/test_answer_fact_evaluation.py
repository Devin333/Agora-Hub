from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from backend.research.application.answer_fact_evaluation import (
    AnswerFactEvaluationConfig,
    AnswerFactEvaluator,
)
from backend.research.application.qasper_answer_evaluation import (
    QasperAnswerEvaluationConfig,
    QasperAnswerEvaluationService,
)
from backend.research.application.reportbench_grounding import ReportBenchGroundingEvaluator
from backend.research.benchmark.reportbench import CrossSourceResearchResult
from backend.research.ports.answer_fact_extraction import (
    ClaimDecompositionRequest,
    ClaimDecompositionResult,
    ClaimDraft,
    ExtractorIdentity,
    FactDraft,
    FactExtractionRequest,
    FactExtractionResult,
    FactSourceRole,
)
from backend.research.ports.fact_entailment_judge import (
    EntailmentLabel,
    FactEntailmentJudgment,
    FactEntailmentRequest,
    FactJudgeIdentity,
    JudgeTargetKind,
    TargetJudgment,
)
from backend.research.rag.evaluation.protocol_answer_scoring import ClaimVerdict
from backend.research.rag.evaluation.protocol_judge_calibration import JudgeCalibrationGate
from backend.research.rag.evaluation.protocol_scoring import ScoreStatus, ScoreSource
from backend.research.rag.evaluation.qasper_baseline import _build_documents
from backend.research.rag.evaluation.qasper_reference import QasperGoldIdentity
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
from backend.research.application.qasper_assets import QasperAssets, QasperCacheIdentity
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_execution import CodeIdentity
from backend.research.rag.evaluation.qasper_retrieval import RetrievalChannelInputs
from tests.backend.research.application.test_qasper_evaluation import MemoryArtifacts

ENCODER_SENTENCE = "We use the BERT encoder, fine-tuned on SQuAD."
PRETRAINING_SENTENCE = "The model does not use additional pretraining beyond BERT."
PARAGRAPH = f"{ENCODER_SENTENCE} {PRETRAINING_SENTENCE}"
# BM25 needs a corpus where query terms are not in every chunk.
DISTRACTORS = (
    "Related work surveys question answering datasets and benchmarks.",
    "We report results on the development split with three random seeds.",
)
SPAN_QUESTION = "Which encoder is used?"
YES_NO_QUESTION = "Do they use additional pretraining?"
EXTRACTOR = ExtractorIdentity(provider="fixture", model_id="extractor", model_revision="r1", prompt_version="v1")
IDENTITY = QasperGoldIdentity(
    dataset_version="0.3", license_reference_id="cc-by-4.0",
    questions_frozen_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    answers_started_at=datetime(2020, 2, 1, tzinfo=timezone.utc),
    retrieved_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
)
ANSWERS = {
    SPAN_QUESTION: "The encoder is BERT [1].",
    YES_NO_QUESTION: "No, they do not use additional pretraining [1].",
}


def _raw() -> dict:
    return {"paper-1": {
        "title": "Paper", "abstract": "",
        "full_text": [
            {"section_name": "Related Work", "paragraphs": [DISTRACTORS[0]]},
            {"section_name": "Method", "paragraphs": [PARAGRAPH]},
            {"section_name": "Experiments", "paragraphs": [DISTRACTORS[1]]},
        ],
        "qas": [
            {"question_id": "q-span", "question": SPAN_QUESTION, "answers": [
                {"annotation_id": "a-span", "answer": {
                    "unanswerable": False, "yes_no": None, "extractive_spans": ["BERT"],
                    "free_form_answer": "", "evidence": [ENCODER_SENTENCE]}},
                {"annotation_id": "a-unanswerable", "answer": {"unanswerable": True}},
            ]},
            {"question_id": "q-yes-no", "question": YES_NO_QUESTION, "answers": [
                {"annotation_id": "a-no", "answer": {
                    "unanswerable": False, "yes_no": False, "extractive_spans": [], "free_form_answer": "",
                    "evidence": [PRETRAINING_SENTENCE]}},
            ]},
        ],
    }}


class _Assets:
    def __init__(self) -> None:
        raw = _raw()
        documents, chunks = _build_documents(raw)
        code = CodeIdentity.create(git_revision="a" * 40, workspace_content_fingerprint=content_digest([]),
                                   mutable_patch_fingerprints=(), dirty=False)
        self.assets = QasperAssets(raw, tuple(documents), tuple(chunks), "b" * 64, code)

    def read_assets(self):
        return self.assets

    def cache_identity(self):
        return QasperCacheIdentity(sha256="c" * 64, dimension=4, model_id="fixture", model_version="v1")

    def retrieval_inputs(self, chunks, *, dense, field):
        class _Chunks:
            def __init__(self, rows):
                self.rows = {item.chunk_id: item for item in rows}

            def list_chunks(self, paper_id):
                return [item for item in self.rows.values() if item.paper_id == paper_id]

            def get_chunk(self, chunk_id):
                return self.rows.get(chunk_id)

        return RetrievalChannelInputs(_Chunks(chunks))


async def _answer(prompt: str, **_: object) -> str:
    for question, answer in ANSWERS.items():
        if question in prompt:
            return answer
    raise AssertionError("unexpected prompt")


class _Decomposer:
    identity = EXTRACTOR

    async def decompose(self, request: ClaimDecompositionRequest) -> ClaimDecompositionResult:
        claims = {
            SPAN_QUESTION: (ClaimDraft(claim_text="The encoder is BERT.", grounding_span="The encoder is BERT"),),
            YES_NO_QUESTION: (ClaimDraft(
                claim_text="They do not use additional pretraining.",
                grounding_span="they do not use additional pretraining",
            ),),
        }[request.question]
        return ClaimDecompositionResult(request_id=request.request_id, extractor=EXTRACTOR, claims=claims)


class _Extractor:
    """Fixture extractor: proposes the obvious encoder fact, nothing for yes/no."""

    identity = EXTRACTOR

    async def extract(self, request: FactExtractionRequest) -> FactExtractionResult:
        drafts: tuple[FactDraft, ...] = ()
        if request.question == SPAN_QUESTION:
            if request.role is FactSourceRole.REFERENCE:
                drafts = (FactDraft(statement="BERT", categorical_value="BERT", statistic="encoder"),)
            elif request.role is FactSourceRole.CANDIDATE:
                drafts = (FactDraft(
                    statement="The encoder is BERT.", categorical_value="BERT", statistic="encoder",
                    aligned_target_id=request.alignment_targets[0].target_id if request.alignment_targets else None,
                ),)
            elif ENCODER_SENTENCE in request.source_text:
                drafts = (FactDraft(statement=ENCODER_SENTENCE, categorical_value="BERT",
                                    statistic="encoder"),)
        return FactExtractionResult(request_id=request.request_id, extractor=EXTRACTOR, drafts=drafts)


class _Judge:
    def __init__(self, provider: str) -> None:
        self._identity = FactJudgeIdentity(provider=provider, model_id="m", model_revision="r", prompt_version="v1")

    @property
    def identity(self) -> FactJudgeIdentity:
        return self._identity

    async def judge(self, request: FactEntailmentRequest) -> FactEntailmentJudgment:
        def label(target) -> EntailmentLabel:
            claim = request.claim_text.casefold()
            if request.target_kind is JudgeTargetKind.REFERENCE:
                # Calibration variants differ from the gold claim text; the real
                # answer restates the "no" answer in its own words.
                if claim == target.text.casefold() or claim.startswith("they do not use additional"):
                    return EntailmentLabel.ENTAILED
                return EntailmentLabel.CONTRADICTED
            return EntailmentLabel.ENTAILED if PRETRAINING_SENTENCE in target.text else EntailmentLabel.NEUTRAL

        return FactEntailmentJudgment(
            request_id=request.request_id,
            judge=self._identity,
            judgments=tuple(TargetJudgment(target_id=item.target_id, label=label(item)) for item in request.targets),
        )


def _service(*, judges=()) -> tuple[QasperAnswerEvaluationService, MemoryArtifacts]:
    artifacts = MemoryArtifacts()
    evaluator = AnswerFactEvaluator(
        decomposer=_Decomposer(), extractor=_Extractor(), judges=judges,
        config=AnswerFactEvaluationConfig(calibration_gate=JudgeCalibrationGate(min_variants=1)),
    )
    service = QasperAnswerEvaluationService(
        assets=_Assets(), artifacts=artifacts, generator=AnswerGenerator(_answer), evaluator=evaluator,
        config=QasperAnswerEvaluationConfig(gold_identity=IDENTITY, profile="bm25_only", run_id="run-1"),
    )
    return service, artifacts


def test_deterministic_run_scores_span_answer_and_leaves_yes_no_pending() -> None:
    service, artifacts = _service()
    report = asyncio.run(service.run("out"))
    assert report.cases == 2 and report.runtime_errors == 0 and report.judges_applied is False
    assert service.read_report("out").digest == report.digest
    import json
    cases = {item["case_id"]: item for item in json.loads(artifacts.read("out", "cases.json"))}
    span = cases["paper-1:q-span"]
    assert span["selected_annotation_id"] == "a-span"
    selected = next(item for item in span["outcomes"] if item["annotation_id"] == "a-span")
    assessment = selected["score"]["claim_assessments"][0]
    assert assessment["verdict"] == ClaimVerdict.CORRECT.value
    claim = assessment["claim_id"]
    assert claim.endswith(":claim-1")
    precision = next(item for item in report.metrics if item.metric_id == "answer.fact_precision")
    assert precision.status_counts.get(ScoreStatus.NEEDS_REVIEW.value) == 1
    assert precision.numerator == 1 and precision.denominator == 1
    reference = report.extraction["reference"]
    # Two reference claims; the yes/no claim is fixed by rule, only the span is extracted.
    assert (reference.targets, reference.fixed_targets, reference.drafts) == (2, 1, 1)


def test_calibrated_judges_resolve_the_pending_yes_no_answer() -> None:
    service, artifacts = _service(judges=(_Judge("openai"), _Judge("dashscope")))
    report = asyncio.run(service.run("out"))
    assert report.calibration is not None and report.calibration.passed, report.calibration.failures
    assert report.judges_applied is True
    precision = next(item for item in report.metrics if item.metric_id == "answer.fact_precision")
    assert precision.status_counts == {ScoreStatus.OK.value: 2}
    assert precision.value == 1.0
    assert precision.source_counts.get(ScoreSource.SEMANTIC_JUDGE.value) == 1
    faithfulness = next(item for item in report.metrics if item.metric_id == "answer.faithfulness")
    assert faithfulness.value == 1.0
    assert report.consensus_resolved_pairs >= 2


def test_uncalibrated_judges_are_not_applied() -> None:
    from backend.research.application.answer_fact_evaluation import AnswerFactEvaluator as Evaluator

    class _LazyJudge(_Judge):
        async def judge(self, request):
            return FactEntailmentJudgment(
                request_id=request.request_id, judge=self.identity,
                judgments=tuple(TargetJudgment(target_id=item.target_id, label=EntailmentLabel.ENTAILED)
                                for item in request.targets),
            )

    service, _ = _service(judges=(_LazyJudge("openai"), _LazyJudge("dashscope")))
    report = asyncio.run(service.run("out"))
    assert report.calibration is not None and not report.calibration.passed
    assert report.judges_applied is False
    assert Evaluator is AnswerFactEvaluator


def test_reportbench_grounding_uses_only_cited_quotes() -> None:
    result = CrossSourceResearchResult.model_validate({
        "task_id": "task-1",
        "question": SPAN_QUESTION,
        "report": "The encoder is BERT.",
        "sources": [{"source_id": "s1", "url": "https://example.org/paper", "title": "Paper", "kind": "paper"}],
        "citations": [{"source_id": "s1", "quote": ENCODER_SENTENCE}],
    })
    evaluator = ReportBenchGroundingEvaluator(decomposer=_Decomposer(), extractor=_Extractor())
    cases, report = asyncio.run(evaluator.evaluate_run(
        [result], run_id="rb-1", dataset_id="reportbench", dataset_version="v1", calibration=None,
    ))
    case = cases[0]
    assert case.status == "scored"
    assert case.score.context_kind == "cited_quotes"
    assert case.score.faithfulness.value == 1.0
    assert case.score.citation_coverage.status is ScoreStatus.NOT_APPLICABLE
    assert case.score.fact_precision.status is ScoreStatus.NOT_APPLICABLE
    assert report.reference_mode == "grounded" and report.abstention is None


def test_reportbench_without_quotes_is_not_scorable() -> None:
    result = CrossSourceResearchResult.model_validate({
        "task_id": "task-2", "question": SPAN_QUESTION, "report": "BERT.",
        "sources": [{"source_id": "s1", "url": "https://example.org/p", "title": "P", "kind": "paper"}],
        "citations": [{"source_id": "s1"}],
    })
    evaluator = ReportBenchGroundingEvaluator(decomposer=_Decomposer(), extractor=_Extractor())
    case = asyncio.run(evaluator.evaluate(result, calibration=None))
    assert case.status == "not_scorable" and case.score is None
