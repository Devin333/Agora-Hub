from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from backend.research.rag.retrieval.expanders.section_navigation import (
    DEFAULT_NAVIGATION_MAX_PASSAGES_PER_SECTION,
    DEFAULT_NAVIGATION_MAX_SECTIONS,
    SectionNavigationConfig,
)
from backend.research.rag.retrieval.paper_section_navigator import (
    DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS,
    LLMSectionNavigator,
)
from backend.research.rag.retrieval.answer_evidence_selection import (
    DEFAULT_EVIDENCE_MIN_PASSAGES,
    EVIDENCE_SELECTION_STRATEGY,
)
from framework.harness import RAGBudget
from framework.llm.clients.openai_compatible import OpenAICompatibleConfig
from interfaces.composition.research_errors import ResearchConfigurationError
from interfaces.composition.research_reader_answer import (
    reader_answer_worker_factory,
    reader_evidence_selector,
    reader_rag_budget,
    reader_section_navigation,
)
from interfaces.composition.research_settings import ResearchRAGSettings, ResearchRuntimeSettings


class _Client:
    def __init__(self) -> None:
        self.config = OpenAICompatibleConfig(
            provider="test-provider",
            base_url="https://provider.test/v1",
            model="test-model",
            api_key_env="TEST_PROVIDER_API_KEY",
        )

    def complete(self, request):  # pragma: no cover - never called by composition
        raise AssertionError("composition must not call the model")


def _settings(tmp_path: Path, **overrides) -> ResearchRAGSettings:
    return ResearchRAGSettings(
        backend="local",
        local_root=(tmp_path / "chunks").resolve(),
        qdrant_url=None,
        collection="research_test_chunks",
        vector_size=32,
        max_rounds=6,
        max_replans=1,
        max_queries=12,
        max_source_reads=24,
        max_memory_hits=8,
        max_context_items=8,
        max_context_tokens=4096,
        max_worker_calls=16,
        **overrides,
    )


def _generator(worker):
    return worker._generator  # noqa: SLF001 - composition wiring under test


def test_reader_answer_path_without_evidence_selection_keeps_the_item_budget(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    budget = reader_rag_budget(settings)
    limits = _generator(reader_answer_worker_factory(_Client(), settings, max_output_tokens=900)()).context_limits

    assert settings.evidence_selection_enabled is False
    assert reader_evidence_selector(_Client(), settings) is None
    assert budget == replace(RAGBudget.safe_default(), max_context_items=8, max_context_tokens=4096)
    assert limits["max_context_chunks"] == 8
    assert limits["evidence_selection"] is None


def test_reader_answer_path_with_evidence_selection_admits_the_pool(tmp_path: Path) -> None:
    settings = _settings(tmp_path, evidence_pool_size=32, evidence_min_relevance=5)

    budget = reader_rag_budget(settings)
    factory = reader_answer_worker_factory(_Client(), settings, max_output_tokens=900)
    limits = _generator(factory()).context_limits

    # The session admits the whole pool; the generator still answers from at most 8.
    assert budget.max_context_items == 32
    assert limits["max_context_chunks"] == 8
    selection = limits["evidence_selection"]
    assert selection["strategy"] == EVIDENCE_SELECTION_STRATEGY
    assert selection["pool_size"] == 32
    assert selection["min_passages"] == DEFAULT_EVIDENCE_MIN_PASSAGES
    assert selection["min_relevance"] == 5
    assert selection["render_mode"] == "summary"
    assert selection["summarizer"]["model"]["model"] == "test-model"


@pytest.mark.parametrize(
    ("overrides", "capability"),
    [
        ({"evidence_pool_size": 4}, "research.rag.evidence_pool_size"),
        ({"evidence_pool_size": 65}, "research.rag.evidence_pool_size"),
        ({"evidence_pool_size": 16, "evidence_min_passages": 17}, "research.rag.evidence_min_passages"),
        ({"evidence_min_relevance": 11}, "research.rag.evidence_min_relevance"),
        ({"evidence_render_mode": "full"}, "research.rag.evidence_render_mode"),
        ({"section_navigation_enabled": "yes"}, "research.rag.section_navigation"),
        ({"navigation_max_sections": 0}, "research.rag.navigation_max_sections"),
        ({"navigation_max_sections": 9}, "research.rag.navigation_max_sections"),
        (
            {"navigation_max_passages_per_section": 0},
            "research.rag.navigation_max_passages_per_section",
        ),
    ],
)
def test_invalid_evidence_settings_fail_closed(tmp_path: Path, overrides, capability) -> None:
    with pytest.raises(ResearchConfigurationError) as raised:
        _settings(tmp_path, **overrides)

    assert capability in raised.value.capabilities


def test_evidence_selection_is_read_from_the_environment(tmp_path: Path) -> None:
    settings = ResearchRuntimeSettings.from_env(
        {
            "DASHSCOPE_API_KEY": "sk-test-research-secret",
            "NEWS_RESEARCH_RAG_EVIDENCE_POOL_SIZE": "48",
            "NEWS_RESEARCH_RAG_EVIDENCE_MIN_PASSAGES": "3",
            "NEWS_RESEARCH_RAG_EVIDENCE_MIN_RELEVANCE": "6",
            "NEWS_RESEARCH_RAG_EVIDENCE_RENDER_MODE": "Passage",
        },
        cwd=tmp_path,
    )

    assert settings.rag.evidence_pool_size == 48
    assert settings.rag.evidence_min_passages == 3
    assert settings.rag.evidence_min_relevance == 6
    assert settings.rag.evidence_render_mode == "passage"
    assert settings.rag.reader_context_items == 48


def test_evidence_selection_is_off_by_default(tmp_path: Path) -> None:
    settings = ResearchRuntimeSettings.from_env({"DASHSCOPE_API_KEY": "sk-test"}, cwd=tmp_path)

    assert settings.rag.evidence_selection_enabled is False
    assert settings.rag.reader_context_items == settings.rag.max_context_items


def test_section_navigation_is_off_by_default(tmp_path: Path) -> None:
    settings = ResearchRuntimeSettings.from_env({"DASHSCOPE_API_KEY": "sk-test"}, cwd=tmp_path)

    assert settings.rag.section_navigation_enabled is False
    assert settings.rag.navigation_max_sections == DEFAULT_NAVIGATION_MAX_SECTIONS
    assert settings.rag.navigation_max_passages_per_section == DEFAULT_NAVIGATION_MAX_PASSAGES_PER_SECTION
    assert reader_section_navigation(_Client(), settings.rag) == (None, None)


def test_section_navigation_is_read_from_the_environment_and_built_on_the_research_llm(
    tmp_path: Path,
) -> None:
    settings = ResearchRuntimeSettings.from_env(
        {
            "DASHSCOPE_API_KEY": "sk-test-research-secret",
            "NEWS_RESEARCH_RAG_SECTION_NAVIGATION": "on",
            "NEWS_RESEARCH_RAG_NAVIGATION_MAX_SECTIONS": "3",
            "NEWS_RESEARCH_RAG_NAVIGATION_MAX_PASSAGES_PER_SECTION": "1",
        },
        cwd=tmp_path,
    )

    navigator, config = reader_section_navigation(_Client(), settings.rag)

    assert settings.rag.section_navigation_enabled is True
    assert isinstance(navigator, LLMSectionNavigator)
    assert config == SectionNavigationConfig(max_sections=3, max_passages_per_section=1)
    model_identity = navigator.measurement_identity["model"]
    assert model_identity["model"] == "test-model"
    assert model_identity["max_tokens"] == DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS


def test_invalid_section_navigation_environment_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ResearchConfigurationError) as raised:
        ResearchRuntimeSettings.from_env(
            {"DASHSCOPE_API_KEY": "sk-test", "NEWS_RESEARCH_RAG_SECTION_NAVIGATION": "maybe"},
            cwd=tmp_path,
        )

    assert "research.rag.section_navigation" in raised.value.capabilities


def test_reader_rag_sessions_carry_the_configured_section_navigation(tmp_path: Path) -> None:
    from backend.research.application.ask_paper import AskPaperUseCase
    from backend.research.application.paper_rag_session import ContextWindowLimits
    from interfaces.composition.research import _local_reader_rag_service

    settings = _settings(tmp_path, section_navigation_enabled=True, navigation_max_sections=3)
    navigator, config = reader_section_navigation(_Client(), settings)
    service = _local_reader_rag_service(
        ask_use_case=AskPaperUseCase(),
        chunk_store=object(),  # type: ignore[arg-type]
        answer_worker_factory=lambda: None,
        budget=reader_rag_budget(settings),
        context_assembler_factory=lambda _spec: None,  # type: ignore[arg-type, return-value]
        context_window=ContextWindowLimits(max_input_tokens=8192, max_output_tokens=900),
        run_scope=lambda _run_id: None,  # type: ignore[arg-type, return-value]
        transcript_root=tmp_path / "transcripts",
        section_navigator=navigator,
        section_navigation=config,
    )

    session = service._session_factory()  # noqa: SLF001 - composition wiring under test

    assert session._section_navigator is navigator  # noqa: SLF001
    assert session._section_navigation == SectionNavigationConfig(max_sections=3)  # noqa: SLF001
