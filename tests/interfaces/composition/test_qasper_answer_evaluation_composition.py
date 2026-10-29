from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from backend.research.application.qasper_assets import QasperAssetService
from backend.research.rag.evaluation.protocol_execution import CodeIdentity
from backend.research.rag.adapters.paper_field_text import extract_field_texts
from backend.research.rag.evaluation.qasper_retrieval import OfflineEmbeddingCache
from framework.events.canonical import checksum_for
from framework.harness import ContextBudget, ContextGraphIdentity, HarnessEventType
from framework.harness.context.models import CONTEXT_GRAPH_TASK_PLAN_STAGE_IDENTITY_SCHEMA_V2
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_CONDITION_POLICY_VERSION,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
)
from infrastructure.research.qasper_evaluation import FilesystemQasperAssets
from interfaces.composition.qasper_evaluation import (
    _durable_context_event_port,
    _reader_answer_context_runtime,
    build_qasper_answer_evaluation_service,
    build_qasper_answer_rejudge_service,
)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    dataset = tmp_path / "qasper.json"
    dataset.write_text(json.dumps({
        "p1": {
            "title": "Fixture",
            "abstract": "Encoder study.",
            "full_text": [{"section_name": "Method", "paragraphs": ["We train an encoder."]}],
            "figures_and_tables": [],
            "qas": [],
        }
    }), encoding="utf-8")
    root = Path(__file__).resolve().parents[3]
    chunks = QasperAssetService(
        FilesystemQasperAssets(dataset, None, repository_root=root)
    ).read_assets().chunks
    texts = {
        text
        for chunk in chunks
        for text in (chunk.content, *extract_field_texts(chunk).non_empty().values())
    }
    cache = tmp_path / "cache.npz"
    np.savez(
        cache,
        keys=np.asarray([OfflineEmbeddingCache.key(text) for text in sorted(texts)], dtype=str),
        vectors=np.ones((len(texts), 4), dtype=np.float32),
    )
    return dataset.resolve(), cache.resolve(), (tmp_path / "queries.json").resolve(), (tmp_path / "out").resolve()


def _stub_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEWS_PAPER_RAG_POLICY", raising=False)
    monkeypatch.delenv("NEWS_PAPER_RAG_POLICY_CONFIG", raising=False)
    monkeypatch.delenv("NEWS_QASPER_RETRIEVAL_PROFILE", raising=False)
    monkeypatch.setattr(
        "backend.research.application.llm_client.build_unity_paper_answer_model",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "backend.research.application.llm_client.build_unity_judge_llm_call",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "infrastructure.research.qasper_query_embedding.ProviderQueryEmbedder.from_env",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr("framework.shared.env.load_root_env", lambda: None)


@pytest.mark.parametrize("value", [0, -1, True])
def test_answer_composition_rejects_invalid_overfetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    _stub_models(monkeypatch)
    dataset, cache, query_vectors, output = _paths(tmp_path)

    with pytest.raises(ValueError, match="retrieval_overfetch must be a positive integer"):
        build_qasper_answer_evaluation_service(
            dataset_path=dataset,
            cache_path=cache,
            query_vector_path=query_vectors,
            output_directory=output,
            seed="seed",
            per_type=1,
            max_cases=1,
            judge=False,
            retrieval_overfetch=value,  # type: ignore[arg-type]
        )


def test_answer_composition_preserves_default_or_applies_explicit_overfetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch)
    dataset, cache, query_vectors, output = _paths(tmp_path)

    default = build_qasper_answer_evaluation_service(
        dataset_path=dataset, cache_path=cache, query_vector_path=query_vectors,
        output_directory=output, seed="seed", per_type=1, max_cases=1, judge=False,
    )
    expanded = build_qasper_answer_evaluation_service(
        dataset_path=dataset, cache_path=cache, query_vector_path=query_vectors,
        output_directory=output, seed="seed", per_type=1, max_cases=1, judge=False,
        retrieval_overfetch=9,
    )

    assert default._policy.overfetch_multiplier == 3
    assert default._reranker is None
    assert default._field_reranker is None
    assert expanded._policy.overfetch_multiplier == 9


def test_answer_composition_applies_explicit_context_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch)
    dataset, cache, query_vectors, output = _paths(tmp_path)

    default = build_qasper_answer_evaluation_service(
        dataset_path=dataset, cache_path=cache, query_vector_path=query_vectors,
        output_directory=output, seed="seed", per_type=1, max_cases=1, judge=False,
    )
    widened = build_qasper_answer_evaluation_service(
        dataset_path=dataset, cache_path=cache, query_vector_path=query_vectors,
        output_directory=output, seed="seed", per_type=1, max_cases=1, judge=False,
        max_context_items=16, max_context_tokens=4096,
    )

    assert (default._retrieval_profile.max_context_items, default._retrieval_profile.max_context_tokens) == (8, 2048)
    assert (widened._retrieval_profile.max_context_items, widened._retrieval_profile.max_context_tokens) == (16, 4096)


def test_answer_composition_enables_evidence_selection_with_the_evidence_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.paper_answer_model import FakePaperAnswerModel

    _stub_models(monkeypatch)
    built: list[dict] = []

    def evidence_model(**kwargs):
        built.append(kwargs)
        return FakePaperAnswerModel("{}", model="evidence-model")

    monkeypatch.setattr(
        "backend.research.application.llm_client.build_unity_evidence_model", evidence_model,
    )
    dataset, cache, query_vectors, output = _paths(tmp_path)

    service = build_qasper_answer_evaluation_service(
        dataset_path=dataset, cache_path=cache, query_vector_path=query_vectors,
        output_directory=output, seed="seed", per_type=1, max_cases=1, judge=False,
        max_context_items=16, evidence_pool_size=48, evidence_min_passages=3,
        evidence_render_mode="passage",
    )

    assert len(built) == 1
    assert service._retrieval_profile.evidence_pool_size == 48
    assert service._retrieval_profile.retrieval_context_items == 48
    identity = service._evidence_selector.measurement_identity
    assert (identity["pool_size"], identity["min_passages"], identity["render_mode"]) == (48, 3, "passage")


def test_answer_composition_rejects_evidence_options_without_a_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch)
    dataset, cache, query_vectors, output = _paths(tmp_path)

    with pytest.raises(ValueError, match="require evidence_pool_size"):
        build_qasper_answer_evaluation_service(
            dataset_path=dataset, cache_path=cache, query_vector_path=query_vectors,
            output_directory=output, seed="seed", per_type=1, max_cases=1, judge=False,
            evidence_min_relevance=6,
        )


def test_field_profile_composes_real_fixture_components_with_default_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch)
    dataset, cache, query_vectors, output = _paths(tmp_path)

    service = build_qasper_answer_evaluation_service(
        dataset_path=dataset, cache_path=cache, query_vector_path=query_vectors,
        output_directory=output, seed="seed", per_type=1, max_cases=1, judge=False,
        retrieval_profile="field_lightweight",
    )

    assert service._field_index_factory is not None
    assert type(service._reranker).__name__ == "LightweightLexicalReranker"
    assert type(service._field_reranker).__name__ == "LightweightLexicalReranker"
    assert service._retrieval_profile.execution_profile == "fixture"


def test_field_only_profile_composes_field_index_without_rerankers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_models(monkeypatch)
    dataset, cache, query_vectors, output = _paths(tmp_path)

    service = build_qasper_answer_evaluation_service(
        dataset_path=dataset, cache_path=cache, query_vector_path=query_vectors,
        output_directory=output, seed="seed", per_type=1, max_cases=1, judge=False,
        retrieval_profile="field_only",
    )

    assert service._field_index_factory is not None
    assert service._reranker is None
    assert service._field_reranker is None
    assert service._retrieval_profile.name == "field_only"
    assert service._retrieval_profile.field_index_enabled is True
    assert service._retrieval_profile.reranker_enabled is False
    assert service._retrieval_profile.field_reranker_enabled is False


def test_rejudge_composition_builds_only_judge_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def judge(_prompt: str) -> str:
        return '{"correct": true, "citations": [], "rationale": "ok"}'

    judge.measurement_identity = {"model": "judge-v2", "max_tokens": 400, "temperature": 0.0}
    monkeypatch.setattr(
        "backend.research.application.llm_client.build_unity_judge_llm_call",
        lambda **_kwargs: judge,
    )
    monkeypatch.setattr(
        "backend.research.application.llm_client.build_unity_paper_answer_model",
        lambda **_kwargs: pytest.fail("rejudge must not construct the answer provider"),
    )
    monkeypatch.setattr(
        "infrastructure.research.qasper_query_embedding.ProviderQueryEmbedder.from_env",
        lambda **_kwargs: pytest.fail("rejudge must not construct the query embedder"),
    )
    current_code = CodeIdentity.create(
        git_revision="a" * 40,
        workspace_content_fingerprint="b" * 64,
        mutable_patch_fingerprints=(),
        dirty=False,
    )
    monkeypatch.setattr(QasperAssetService, "code_identity", lambda _self: current_code)

    service = build_qasper_answer_rejudge_service(
        source_directory=(tmp_path / "source").resolve(),
        output_directory=(tmp_path / "output").resolve(),
        legacy_judge_model="historical-judge",
    )

    assert service._assets is None
    assert service._judge_model_identity["model"] == "judge-v2"
    assert service._legacy_judge_model_identity == {
        "model": "historical-judge", "source": "cli_user_confirmation",
    }
    assert service._code_identity == current_code.model_dump(mode="json")


def test_answer_context_runtime_persists_events_and_run_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = SimpleNamespace(
        provider="test-provider", model="test-model",
        max_input_tokens=16_384, max_output_tokens=1_024,
    )
    monkeypatch.setattr(
        "interfaces.composition.research_settings.ResearchRuntimeSettings.from_env",
        lambda: "env-settings",
    )
    monkeypatch.setattr(
        "interfaces.composition.research_llm_deployment.resolve_research_llm_runtime",
        lambda settings: (None, SimpleNamespace(llm=llm)) if settings == "env-settings" else None,
    )
    output = (tmp_path / "answer-run").resolve()
    runtime = _reader_answer_context_runtime(output)
    run_id = "qasper-context-persisted"
    assembler = runtime.assembler_factory(None)

    with runtime.run_scope(run_id):
        assembler.assemble({
            "run_id": run_id,
            "graph_identity": _context_graph_identity(run_id),
            "phase": "EXECUTE",
            "worker_id": "research-artifact-publisher",
            "worker_type": "function",
            "current_task_ref": "task://publish_artifacts",
            "current_instruction": "Publish verified Research artifacts.",
            "source_refs": ("source://paper",),
            "artifact_refs": tuple(
                f"artifact://research/{index}" for index in range(32)
            ),
            "evidence_refs": tuple(
                f"evidence://research/{index}" for index in range(16)
            ),
            "budget": ContextBudget(
                max_input_tokens=8_192,
                max_output_tokens=1_024,
                max_context_segments=6,
                max_evidence_items=8,
                max_memory_items=6,
                max_artifact_refs=24,
                reserved_output_tokens=512,
            ),
        })

    context_root = tmp_path / "answer-run.context"
    reopened = _durable_context_event_port(context_root)
    history = reopened.read_history(run_id)
    assert history
    assert HarnessEventType.CONTEXT_COMPACTION_PLANNED in {
        event.event_type for event in history
    }
    assert reopened.read_history("different-run") == ()
    assert (context_root / run_id).is_dir()


def _context_graph_identity(run_id: str) -> ContextGraphIdentity:
    projection = {
        "schema_version": CONTEXT_GRAPH_TASK_PLAN_STAGE_IDENTITY_SCHEMA_V2,
        "run_id": run_id,
        "graph_schema_version": GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
        "compiler_version": HARNESS_GRAPH_ONLY_COMPILER_VERSION,
        "condition_policy_version": HARNESS_CONDITION_POLICY_VERSION,
        "graph_id": "research.paper_analysis",
        "graph_version": "1",
        "graph_checksum": "sha256:" + "1" * 64,
        "stage_id": "publish_artifacts",
        "stage_binding_checksum": "sha256:" + "2" * 64,
        "graph_ref": "research.paper_analysis@1",
    }
    return ContextGraphIdentity(
        run_id=run_id,
        graph_id=projection["graph_id"],
        graph_version=projection["graph_version"],
        graph_ref=projection["graph_ref"],
        graph_schema_version=projection["graph_schema_version"],
        compiler_version=projection["compiler_version"],
        condition_policy_version=projection["condition_policy_version"],
        graph_checksum=projection["graph_checksum"],
        stage_id=projection["stage_id"],
        stage_binding_checksum=projection["stage_binding_checksum"],
        stage_identity_schema=projection["schema_version"],
        stage_identity_checksum=checksum_for(projection),
    )
