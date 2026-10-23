from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from backend.research.application.qasper_assets import QasperAssetService
from backend.research.rag.adapters.paper_field_text import extract_field_texts
from backend.research.rag.evaluation.qasper_retrieval import OfflineEmbeddingCache
from infrastructure.research.qasper_evaluation import FilesystemQasperAssets
from interfaces.composition.qasper_evaluation import build_qasper_answer_evaluation_service


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
