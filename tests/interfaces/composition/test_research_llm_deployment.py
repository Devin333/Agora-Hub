from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from interfaces.composition.research_errors import ResearchConfigurationError
from interfaces.composition.research_llm_deployment import resolve_research_llm_runtime
from interfaces.composition.research_settings import ResearchRuntimeSettings

_BASE_URL = "https://research-gateway.example.test/v1"


def _models_config(tmp_path: Path, *, context_window_tokens: int | None) -> Path:
    deployment = {
        "deployment_id": "research-answer",
        "provider": "openai-compatible",
        "provider_name": "openai-compatible",
        "model": "answer-model",
        "api_base": _BASE_URL,
        "api_key_env": "RESEARCH_TEST_KEY",
    }
    if context_window_tokens is not None:
        deployment["capabilities"] = {"context_window_tokens": context_window_tokens}
    path = tmp_path / "models.json"
    path.write_text(json.dumps({
        "model_groups": {"research-answer": {"deployments": [deployment]}},
        "routes": {"research-answer": {"model_group": "research-answer"}},
    }), encoding="utf-8")
    return path


def _settings(tmp_path: Path, *, context_window_tokens: int | None, **env: str) -> ResearchRuntimeSettings:
    return ResearchRuntimeSettings.from_env({
        "NEWS_RESEARCH_LLM_ROUTE_ID": "research-answer",
        "NEWS_RESEARCH_MODELS_CONFIG": str(_models_config(tmp_path, context_window_tokens=context_window_tokens)),
        "NEWS_RESEARCH_LLM_BASE_URL": _BASE_URL,
        "NEWS_RESEARCH_LLM_MODEL": "answer-model",
        "NEWS_RESEARCH_LLM_API_KEY_ENV": "RESEARCH_TEST_KEY",
        "RESEARCH_TEST_KEY": "synthetic-key",
        **env,
    }, cwd=tmp_path)


def test_input_window_is_the_deployment_window_minus_the_output_reservation(tmp_path: Path) -> None:
    settings = _settings(tmp_path, context_window_tokens=128_000)

    assert settings.llm.max_input_tokens is None
    _deployment, resolved = resolve_research_llm_runtime(settings)

    assert resolved.llm.max_input_tokens == 128_000 - 2_048
    assert resolved.llm.max_output_tokens == 2_048
    assert resolved.rag == settings.rag


def test_deployment_without_a_declared_window_keeps_the_conservative_default(tmp_path: Path) -> None:
    _deployment, resolved = resolve_research_llm_runtime(_settings(tmp_path, context_window_tokens=None))

    assert resolved.llm.max_input_tokens == 8_192


def test_explicit_input_window_wins_when_it_fits_the_deployment(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, context_window_tokens=128_000, NEWS_RESEARCH_LLM_MAX_INPUT_TOKENS="32000",
    )

    _deployment, resolved = resolve_research_llm_runtime(settings)

    assert resolved.llm.max_input_tokens == 32_000


def test_explicit_input_window_larger_than_the_deployment_is_rejected(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, context_window_tokens=16_000, NEWS_RESEARCH_LLM_MAX_INPUT_TOKENS="15000",
    )

    with pytest.raises(ResearchConfigurationError) as raised:
        resolve_research_llm_runtime(settings)

    assert raised.value.capabilities == ("research.llm.max_input_tokens",)


def test_rag_context_budget_must_fit_the_resolved_window(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, context_window_tokens=None, NEWS_RESEARCH_RAG_MAX_CONTEXT_TOKENS="16384",
    )

    with pytest.raises(ResearchConfigurationError) as raised:
        resolve_research_llm_runtime(settings)

    assert raised.value.capabilities == ("research.rag.max_context_tokens",)


def test_derived_window_too_small_for_the_output_reservation_is_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path, context_window_tokens=2_400)

    with pytest.raises(ResearchConfigurationError) as raised:
        resolve_research_llm_runtime(settings)

    assert raised.value.capabilities == ("research.llm.max_output_tokens",)


def test_resolution_requires_the_settings_model_to_match_the_route(tmp_path: Path) -> None:
    settings = _settings(tmp_path, context_window_tokens=128_000)
    mismatched = replace(settings, llm=replace(settings.llm, model="other-model"))

    with pytest.raises(ResearchConfigurationError) as raised:
        resolve_research_llm_runtime(mismatched)

    assert raised.value.capabilities == ("research.llm.model",)


def test_repository_research_route_declares_its_window(monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    # The repository route interpolates its endpoint from the process environment.
    monkeypatch.setenv("NEWS_RESEARCH_LLM_BASE_URL", _BASE_URL)
    settings = ResearchRuntimeSettings.from_env({
        "NEWS_RESEARCH_LLM_ROUTE_ID": "research-desktop-answer",
        "NEWS_RESEARCH_LLM_BASE_URL": _BASE_URL,
        "NEWS_RESEARCH_LLM_MODEL": "gpt-5.6-sol",
        "NEWS_RESEARCH_LLM_API_KEY_ENV": "OPENAI_API_KEY",
        "OPENAI_API_KEY": "synthetic-key",
        "NEWS_MODELS_CONFIG": str(repository_root / "configs" / "models.yaml"),
    }, cwd=repository_root)

    deployment, resolved = resolve_research_llm_runtime(settings)

    assert deployment.capabilities.context_window_tokens == 128_000
    assert resolved.llm.max_input_tokens == 125_952
