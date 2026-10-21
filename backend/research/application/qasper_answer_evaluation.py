"""End-to-end QASPER answer fact evaluation on the production answer path.

Per question: retrieve with a frozen QASPER profile, answer with the production
``AnswerGenerator``, and evaluate the answer against every QASPER annotator's
gold answer with ``AnswerFactEvaluator``. The context pack handed to scoring is
exactly the rendered passages the generator showed the model, so faithfulness
is measured against what the model actually saw.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from backend.research.application.answer_fact_evaluation import (
    AnswerFactCase,
    AnswerFactEvaluator,
    GeneratedCandidate,
    ReferenceAlternative,
)
from backend.research.application.qasper_assets import QasperAssetProvider
from backend.research.ports.qasper_evaluation import QasperArtifactStore
from backend.research.rag.evaluation.protocol_answer_fact_report import (
    AnswerFactCaseResult,
    AnswerFactRunReport,
    summarize_answer_fact_run,
)
from backend.research.rag.evaluation.protocol_claim_decomposition import citation_index_from_chunk_ids
from backend.research.rag.evaluation.qasper_experiments import PROFILES, SweepParameters, retrieve_profile
from backend.research.rag.evaluation.qasper_reference import (
    QasperGoldCase,
    QasperGoldDataset,
    QasperGoldIdentity,
    build_qasper_gold,
)
from backend.research.rag.retrieval.contracts import RetrievalResult
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
from backend.research.rag.retrieval.paper_policy import classify_query_intent
from framework.harness.rag.models import EvidenceCandidate, GroundedAnswerCandidate, RAGContextPack


class _OfflineRetrievalPolicy:
    multi_query_enabled = True


@dataclass(frozen=True)
class QasperAnswerEvaluationConfig:
    gold_identity: QasperGoldIdentity
    profile: str = "full_hybrid"
    parameters: SweepParameters = field(default_factory=SweepParameters)
    case_ids: tuple[str, ...] = ()
    max_cases: int | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"unknown QASPER retrieval profile {self.profile!r}")
        if self.max_cases is not None and self.max_cases < 1:
            raise ValueError("max_cases must be positive")


def answer_fact_case(case: QasperGoldCase) -> AnswerFactCase:
    return AnswerFactCase(
        case_id=case.case_id,
        question=case.question,
        alternatives=tuple(
            ReferenceAlternative(
                annotation_id=item.annotation_id,
                reference=item.reference,
                fixed_reference_facts=item.fixed_reference_facts,
            )
            for item in case.alternatives
        ),
    )


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


class QasperAnswerEvaluationService:
    def __init__(
        self,
        *,
        assets: QasperAssetProvider,
        artifacts: QasperArtifactStore,
        generator: AnswerGenerator,
        evaluator: AnswerFactEvaluator,
        config: QasperAnswerEvaluationConfig,
    ) -> None:
        self._assets = assets
        self._artifacts = artifacts
        self._generator = generator
        self._evaluator = evaluator
        self._config = config

    def build_gold(self) -> QasperGoldDataset:
        assets = self._assets.read_assets()
        return build_qasper_gold(assets.raw_dataset, assets.documents, self._config.gold_identity)

    def _selected_cases(self, gold: QasperGoldDataset) -> tuple[QasperGoldCase, ...]:
        cases = gold.cases
        if self._config.case_ids:
            wanted = set(self._config.case_ids)
            unknown = wanted - {item.case_id for item in cases}
            if unknown:
                raise ValueError(f"unknown QASPER case ids: {sorted(unknown)}")
            cases = tuple(item for item in cases if item.case_id in wanted)
        if self._config.max_cases is not None:
            cases = cases[: self._config.max_cases]
        return cases

    async def run(self, output_directory: str) -> AnswerFactRunReport:
        gold = self.build_gold()
        selected = self._selected_cases(gold)
        if not selected:
            raise ValueError("no QASPER case was selected for answer evaluation")
        run_id = self._config.run_id or f"qasper-answer-{uuid.uuid4().hex[:12]}"
        prepared = await self._evaluator.prepare_cases([answer_fact_case(item) for item in selected])
        calibration = await self._evaluator.calibrate(prepared)
        results: list[AnswerFactCaseResult] = []
        for gold_case, prepared_case in zip(selected, prepared, strict=True):
            generated = await self._generate(gold_case)
            results.append(await self._evaluator.evaluate_case(
                prepared_case, generated, calibration=calibration,
            ))
        report = summarize_answer_fact_run(
            results,
            run_id=run_id,
            dataset_id=gold.identity.dataset_id,
            dataset_version=gold.identity.dataset_version,
            dataset_digest=gold.dataset_digest,
            calibration=calibration,
        )
        self._artifacts.publish(output_directory, {
            "gold.json": _json(gold.model_dump(mode="json")),
            "cases.json": _json([item.model_dump(mode="json") for item in results]),
            "report.json": _json(report.model_dump(mode="json")),
        })
        return report

    def read_report(self, output_directory: str) -> AnswerFactRunReport:
        report = AnswerFactRunReport.model_validate_json(self._artifacts.read(output_directory, "report.json"))
        report.verify_digest()
        return report

    async def _generate(self, case: QasperGoldCase) -> GeneratedCandidate:
        profile = PROFILES[self._config.profile]
        assets = self._assets.read_assets()
        try:
            inputs = self._assets.retrieval_inputs(
                assets.chunks, dense=profile.semantic or profile.field, field=profile.field,
            )
            retrieval = retrieve_profile(
                profile=profile, inputs=inputs, paper_id=case.paper_id, question=case.question,
                policy=_OfflineRetrievalPolicy(), parameters=self._config.parameters,
            )
            ranked = list(retrieval["ranked_chunks"])
            generated = await self._generator.generate(
                case.question,
                RetrievalResult(
                    parent_chunks=[], child_chunks=ranked, ref_chunks=[],
                    intent=classify_query_intent(case.question),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a failed answer is a scored runtime error
            return GeneratedCandidate(candidate=None, context=None, runtime_error=f"{type(exc).__name__}: {exc}")
        scope = case.scope.sources[0]
        chunks = {chunk.chunk_id: chunk for chunk in ranked}
        evidence = tuple(
            EvidenceCandidate(
                evidence_id=chunk_id,
                title=chunks[chunk_id].section_title or chunks[chunk_id].chunk_type,
                summary=text,
                source_ref=f"qasper://{case.paper_id}/chunk/{chunk_id}",
                span_refs=(chunk_id,),
                evidence_type=chunks[chunk_id].chunk_type,
                lineage=(case.paper_id,),
                metadata={
                    "paper_id": case.paper_id,
                    "document_version": scope.document_version,
                    "source_sha256": scope.source_sha256,
                    "source_locator": f"qasper://{case.paper_id}/chunk/{chunk_id}",
                },
            )
            for chunk_id, text in zip(generated.context_chunk_ids, generated.contexts, strict=True)
            if text.strip()
        )
        context = RAGContextPack(
            pack_id=f"{case.case_id}#context",
            query=case.question,
            accepted_evidence=evidence,
            metadata={"retrieval_profile": self._config.profile},
        )
        candidate = GroundedAnswerCandidate(
            answer_id=f"{case.case_id}#answer",
            question=case.question,
            answer_text=generated.answer,
            cited_evidence_ids=(),
            claims=(),
            metadata={"generated_answer_metadata": dict(generated.context_metadata)},
        )
        return GeneratedCandidate(
            candidate=candidate,
            context=context,
            citation_index=citation_index_from_chunk_ids(generated.context_chunk_ids),
        )


__all__ = [
    "QasperAnswerEvaluationConfig",
    "QasperAnswerEvaluationService",
    "answer_fact_case",
]
