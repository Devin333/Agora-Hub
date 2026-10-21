from __future__ import annotations

import hashlib
import json

import pytest

from backend.research.application.qasper_evaluation import (
    QasperEvaluationConfig, QasperEvaluationService, _build_experiments,
    _ranked_rows, build_qasper_experiment_manifest,
)
from backend.research.rag.evaluation.qasper_baseline import _build_documents
from backend.research.rag.evaluation.qasper_mapping import FrozenQasperDataset, freeze_qasper_dataset
from backend.research.application.qasper_assets import QasperAssets, QasperCacheIdentity
from backend.research.rag.evaluation.qasper_retrieval import RetrievalChannelInputs
from backend.research.rag.evaluation.protocol_execution import CodeIdentity
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.qasper_experiments import PROFILES, build_sweep
from backend.research.rag.evaluation.qasper_protocol import (
    QasperCaseResult, QasperChannelProvenance, QasperMetric,
    QasperReportArtifact, RankedResult,
)
from backend.research.rag.evaluation.qasper_scoring import score_qasper_metrics


class MemoryArtifacts:
    def __init__(self):
        self.items = {}

    def publish(self, directory, objects):
        for name, payload in objects.items():
            key = (directory, name)
            if key in self.items and self.items[key] != payload:
                raise ValueError("write-once conflict")
        self.items.update({(directory, name): payload for name, payload in objects.items()})

    def read(self, directory, name):
        return self.items[(directory, name)]


class FakeAssets:
    def __init__(self):
        raw = {"paper-1": {"title": "Paper", "abstract": "",
            "full_text": [{"section_name": "Results", "paragraphs": ["Evidence supports the result."]}],
            "qas": [{"question_id": "q1", "question": "What is the evidence?",
                "answers": [{"answer": {"free_form_answer": "yes", "evidence": ["Evidence supports the result."]}}]}]}}
        docs, chunks = _build_documents(raw)
        code = CodeIdentity.create(git_revision="a" * 40, workspace_content_fingerprint=content_digest([]),
                                   mutable_patch_fingerprints=(), dirty=False)
        self.assets = QasperAssets(raw, tuple(docs), tuple(chunks), "b" * 64, code)
        self.retrieval_calls = 0

    def read_assets(self):
        return self.assets

    def cache_identity(self):
        return QasperCacheIdentity(sha256="c" * 64, dimension=4,
            model_id="fixture-embedding", model_version="fixture-v1")

    def retrieval_inputs(self, chunks, *, dense, field):
        self.retrieval_calls += 1
        class ReadOnlyChunks:
            def __init__(self, rows):
                self.rows = {item.chunk_id: item for item in rows}
            def list_chunks(self, paper_id):
                return [item for item in self.rows.values() if item.paper_id == paper_id]
            def get_chunk(self, chunk_id):
                return self.rows.get(chunk_id)
        return RetrievalChannelInputs(ReadOnlyChunks(chunks))


def test_mapping_is_frozen_persisted_and_revalidated():
    artifacts = MemoryArtifacts()
    service = QasperEvaluationService(FakeAssets(), artifacts, QasperEvaluationConfig(
        output_directory="offline-test", manifest_factory=lambda *args: None))

    frozen = service.freeze_mapping()

    reread = FrozenQasperDataset.model_validate_json(artifacts.read("offline-test", "mapping.json"))
    assert reread.digest == frozen.digest
    assert reread.cases[0].mapping_result.disposition == "valid"


def test_mapping_artifact_conflict_is_never_overwritten():
    artifacts = MemoryArtifacts()
    service = QasperEvaluationService(FakeAssets(), artifacts, QasperEvaluationConfig(
        output_directory="offline-test", manifest_factory=lambda *args: None))
    service.freeze_mapping()
    artifacts.items[("offline-test", "mapping.json")] = b"tampered"

    with pytest.raises(ValueError, match="write-once"):
        service.freeze_mapping()


def test_manifest_factory_builds_the_real_shared_execution_contract():
    assets = FakeAssets()
    raw = assets.read_assets()
    from backend.research.rag.evaluation.qasper_mapping import freeze_qasper_dataset
    frozen = freeze_qasper_dataset(raw.raw_dataset, raw.documents, raw.chunks)
    experiments = build_sweep(tuple(PROFILES), candidate_limits=(5,), overfetch_multipliers=(2,),
        rrf_ks=(20,), rerank_score_thresholds=(None,), channel_weights=({},), identity={
            "code": raw.code_identity.code_fingerprint, "dataset": frozen.dataset_digest,
            "index": frozen.index_sha256, "embedding": "c" * 64, "artifact": frozen.digest})
    cache = type("Cache", (), {"sha256": "c" * 64, "dimension": 4,
        "model_id": "fixture-embedding", "model_version": "fixture-v1"})()

    manifest = build_qasper_experiment_manifest(frozen, raw, cache, experiments)

    manifest.verify_digest()
    assert len(manifest.experiment_manifest.variants) == 7
    assert len(manifest.experiment_manifest.planned_units) == 7 * len(frozen.cases)
    assert manifest.experiment_manifest.record_type == "experiment_manifest"
    assert ("strict_hit", 10) in {item.key for item in manifest.metric_definitions}
    assert ("equivalent_ndcg", 10) in {item.key for item in manifest.metric_definitions}


def test_cli_service_operations_close_run_score_report_loop():
    artifacts = MemoryArtifacts()
    service = QasperEvaluationService(FakeAssets(), artifacts, QasperEvaluationConfig())
    service.freeze_mapping("mapping")
    assert service.validate_mapping("mapping")["valid"] is True

    result = service.run("run", "mapping", "bm25_only", {
        "candidate_limit": 5, "overfetch": 2, "rrf_k": 20, "rerank": False,
        "threshold": 0.25, "weights": {},
    })

    assert result["planned"] == 1
    assert service.score("run")["validated_cases"] == 1
    report = service.report("run")
    assert report["report_artifact"] == "report.json"
    summary = json.loads(artifacts.read("run", "report.json"))
    assert summary["experiments"][0]["profile"] == "bm25_only"
    assert summary["experiments"][0]["parameters"]["candidate_limit"] == 5
    dimensions = {
        row["dimension"]
        for row in summary["experiments"][0]["diagnostics"]["equivalent@5"]["slices"]
    }
    assert {"question_type", "paper", "section", "field", "evidence_count",
            "mapping_status", "first_hit_rank"} <= dimensions
    assert artifacts.read("run", "report.md")
    identifier = result["experiment_id"]
    persisted = QasperReportArtifact.model_validate_json(
        artifacts.read("run", f"{identifier}-report.json")
    )
    for field, suffix in {
        "manifest": "manifest.json", "cases": "cases.json",
        "metrics": "metrics.json", "failures": "failures.json",
        "report": "report.md",
    }.items():
        actual = artifacts.read("run", f"{identifier}-{suffix}")
        assert hashlib.sha256(actual).hexdigest() == getattr(
            persisted.artifacts, field
        ).content_sha256


def test_ranked_rows_use_candidate_limit_capped_final_results_without_rerank() -> None:
    chunks = FakeAssets().read_assets().chunks
    first = chunks[0]
    second = first.model_copy(update={"chunk_id": "second"})
    third = first.model_copy(update={"chunk_id": "third"})
    raw = tuple(
        {
            "channel": "bm25", "query_id": "q", "query_hash": "a" * 64,
            "rank": rank, "chunk_id": chunk.chunk_id, "score": float(4 - rank),
            "field_tag": None,
        }
        for rank, chunk in enumerate((first, second, third), start=1)
    )
    result = {
        "raw_channel_results": raw,
        "channel_candidates": {
            "bm25": ((first, 3.0), (second, 2.0), (third, 1.0))
        },
        "fused_results": ((first, 3.0), (second, 2.0), (third, 1.0)),
        "candidate_count_before_rerank": 2,
        "final_results": ((first, 3.0), (second, 2.0)),
    }

    rows = _ranked_rows(result, reranked=False)

    assert [row.chunk_id for row in rows if row.stage == "fused"] == [
        first.chunk_id, "second"
    ]


def test_disabled_rerank_thresholds_do_not_create_fake_sweep_variants() -> None:
    identity = {key: key for key in ("code", "dataset", "index", "embedding", "artifact")}
    config = QasperEvaluationConfig(
        candidate_limits=(5,), overfetch_multipliers=(2,), rrf_ks=(20,),
        rerank_enabled=(False, True),
        rerank_score_thresholds=(None, 0.0, 0.3),
        channel_weights=({},),
    )

    experiments = _build_experiments(("bm25_only",), config, identity)

    assert len(experiments) == 3
    assert sum(item.parameters.rerank_score_threshold is None for item in experiments) == 1


@pytest.mark.parametrize("mutation", ("dataset", "chunks", "corpus"))
def test_run_sweep_rejects_stale_frozen_inputs_before_retrieval(mutation) -> None:
    artifacts = MemoryArtifacts()
    assets = FakeAssets()
    service = QasperEvaluationService(assets, artifacts, QasperEvaluationConfig())
    service.freeze_mapping("mapping")
    current = assets.assets
    if mutation == "dataset":
        changed_raw = dict(current.raw_dataset)
        changed_raw["dataset_version"] = "changed"
        assets.assets = QasperAssets(
            changed_raw, current.documents, current.chunks, current.dataset_sha256,
            current.code_identity,
        )
    elif mutation == "chunks":
        changed = current.chunks[0].model_copy(update={"content": "Changed chunk text."})
        assets.assets = QasperAssets(
            current.raw_dataset, current.documents, (changed, *current.chunks[1:]),
            current.dataset_sha256, current.code_identity,
        )
    else:
        changed = current.documents[0].model_copy(update={"source_hash": "d" * 64})
        assets.assets = QasperAssets(
            current.raw_dataset, (changed,), current.chunks, current.dataset_sha256,
            current.code_identity,
        )

    with pytest.raises(ValueError, match="frozen mapping"):
        service.run_sweep(
            selected_profiles=("bm25_only",), mapping_directory="mapping",
            output_directory="run",
        )
    assert assets.retrieval_calls == 0


def test_report_aggregates_coverage_as_macro_average_and_preserves_case_denominators() -> None:
    raw = {
        "paper-1": {
            "title": "Paper", "abstract": "",
            "full_text": [{"section_name": "Results", "paragraphs": ["Evidence supports the result."]}],
            "qas": [
                {"question_id": "q1", "question": "First?", "answers": [{"answer": {"free_form_answer": "yes", "evidence": ["Evidence supports the result."]}}]},
                {"question_id": "q2", "question": "Second?", "answers": [{"answer": {"free_form_answer": "yes", "evidence": ["Evidence supports the result."]}}]},
            ],
        }
    }
    docs, chunks = _build_documents(raw)
    base = FakeAssets()
    assets = QasperAssets(raw, tuple(docs), tuple(chunks), "e" * 64, base.assets.code_identity)
    frozen = freeze_qasper_dataset(raw, assets.documents, assets.chunks)
    identity = {
        "code": assets.code_identity.code_fingerprint, "dataset": frozen.dataset_digest,
        "index": frozen.index_sha256, "embedding": "c" * 64, "artifact": frozen.digest,
    }
    experiment = build_sweep(
        ("bm25_only",), candidate_limits=(5,), overfetch_multipliers=(2,),
        rrf_ks=(20,), rerank_score_thresholds=(None,), channel_weights=({},),
        identity=identity,
    )[0]
    cache = base.cache_identity()
    manifest = build_qasper_experiment_manifest(frozen, assets, cache, (experiment,))
    units = {unit.case_id: unit for unit in manifest.experiment_manifest.planned_units}
    provenance = (QasperChannelProvenance(
        channel="bm25", query_id="q", query_sha256="a" * 64,
        channel_rank=1, raw_score=1.0,
    ),)
    cases = []
    for index, case in enumerate(frozen.cases):
        chunk_id = case.strict_anchors[0].paths[0][0]
        rows = (RankedResult(chunk_id=chunk_id, rank=1, stage="fused",
                             raw_score=1.0, provenance=provenance),)
        metrics = list(score_qasper_metrics(
            case, rows, manifest.metric_definitions, final_stage="fused"
        ))
        if index == 0:
            metrics = [
                metric.model_copy(update={"value": 0.5, "numerator": 1.0, "denominator": 2})
                if metric.metric_id.endswith("_coverage") else metric
                for metric in metrics
            ]
        cases.append(QasperCaseResult(
            manifest_digest=manifest.digest, run_id="run", attempt_id=f"attempt-{index}",
            unit=units[case.case_id], query=case.query, paper_ids=(case.paper_id,),
            status="ok", mapping_result=case.mapping_result, final_stage="fused",
            raw_ranked_results=rows, metrics=tuple(metrics), reason=None,
        ))
    service = QasperEvaluationService(base, MemoryArtifacts(), QasperEvaluationConfig())

    report = service._report(
        manifest, manifest.experiment_manifest.variants[0].variant_fingerprint,
        "run", tuple(cases), experiment,
    )

    coverage = next(
        metric for metric in report.metrics
        if metric.metric_id == "strict_coverage" and metric.k == 5
    )
    assert coverage.aggregation == "macro_average"
    assert coverage.value == 0.75
    assert coverage.numerator == 1.5
    assert coverage.denominator == 2
    assert [
        next(metric for metric in case.metrics
             if metric.metric_id == "strict_coverage" and metric.k == 5).denominator
        for case in report.case_results
    ] == [2, 1]


def test_cli_sweep_streams_variants_and_publishes_one_consolidated_index() -> None:
    artifacts = MemoryArtifacts()
    service = QasperEvaluationService(FakeAssets(), artifacts, QasperEvaluationConfig())
    service.freeze_mapping("mapping")

    result = service.sweep(
        "run", "mapping",
        {
            "candidate_limit": [5], "overfetch": [2], "rrf_k": [20],
            "rerank": [False, True], "threshold": [0.0, 0.3],
            "weights": [{}],
        },
        ["bm25_only"],
    )

    assert result["experiments"] == 3
    index = json.loads(artifacts.read("run", "run-index.json"))
    assert len(index["profiles"]) == 3
    assert len(index["manifest_digests"]) == 3
    assert service.score("run")["validated_cases"] == 3
    assert service.report("run")["experiments"] == 3
