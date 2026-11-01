from __future__ import annotations

from dataclasses import replace

import pytest

from backend.research.domain.personal_memory import MAX_CONTENT_LENGTH, MAX_SNAPSHOT_RECORDS, PersonalMemorySourceRef
from backend.research.domain.personal_memory_context import PersonalMemoryContext, PersonalMemoryContextItem
from backend.research.rag.retrieval import paper_evidence_summarizer
from backend.research.rag.retrieval.paper_answer_generator import build_answer_system_prompt
from backend.research.rag.retrieval.paper_evidence_summarizer import (
    DEFAULT_SUMMARY_BATCH_SIZE,
    SUMMARY_BATCH_MAX_INPUT_CHARS,
    SUMMARY_PASSAGE_MAX_CHARS,
    build_evidence_summary_user_prompt,
)
from interfaces.composition.model_purpose_requirements import (
    MAX_QUESTION_CHARS,
    answer_prompt_overhead_chars,
    model_purpose_requirements,
    personal_memory_allowance_chars,
)
from interfaces.composition.research_settings import ResearchRuntimeSettings
from interfaces.services.model_connections import (
    ModelPurpose,
    PurposeRequirement,
    load_model_provider_catalog,
    parse_model_provider_catalog,
)
from interfaces.services.model_connections.admission import model_serves, unservable_purposes
from tests.interfaces.api.hosted_support import REPO_ROOT

HOSTED_ENV = {
    "NEWSROOM_LLM_CREDENTIAL_MODE": "invocation",
    "NEWS_RESEARCH_LLM_MAX_INPUT_TOKENS": "64000",
    "NEWS_RESEARCH_LLM_MAX_OUTPUT_TOKENS": "2048",
}


def _settings(**overrides: str) -> ResearchRuntimeSettings:
    return ResearchRuntimeSettings.from_env({**HOSTED_ENV, **overrides})


def _largest_memory_context() -> PersonalMemoryContext:
    # Quotes escape to two characters in JSON, so this is the longest payload
    # records of the maximum length and count can produce.
    return PersonalMemoryContext(
        policy_revision=1,
        items=tuple(
            PersonalMemoryContextItem(
                memory_id="pmem_" + f"{index:032x}",
                revision=1,
                content='"' * MAX_CONTENT_LENGTH,
                category="reading_preference",
                source_refs=(PersonalMemorySourceRef(source_type="user_input", source_ref="user://alice"),),
            )
            for index in range(MAX_SNAPSHOT_RECORDS)
        ),
    )


def test_answer_allowance_holds_the_largest_personal_memory_block() -> None:
    question = "q" * MAX_QUESTION_CHARS
    plain = build_answer_system_prompt(question)
    with_memory = build_answer_system_prompt(question, personal_memory_context=_largest_memory_context())

    assert len(plain) - len(question) <= answer_prompt_overhead_chars()
    assert len(with_memory) - len(plain) <= personal_memory_allowance_chars()


def test_evidence_summary_limit_holds_its_largest_batch_prompt() -> None:
    passages = ["p" * SUMMARY_PASSAGE_MAX_CHARS] * DEFAULT_SUMMARY_BATCH_SIZE

    prompt = build_evidence_summary_user_prompt("q" * MAX_QUESTION_CHARS, passages)

    assert len(paper_evidence_summarizer._SYSTEM_PROMPT) + len(prompt) <= SUMMARY_BATCH_MAX_INPUT_CHARS


def test_requirements_follow_the_configured_research_settings() -> None:
    requirements = model_purpose_requirements(_settings(NEWS_RESEARCH_RAG_MAX_CONTEXT_TOKENS="8192"))

    candidate = requirements[ModelPurpose.CANDIDATE_GENERATION]
    assert (candidate.input_tokens, candidate.output_tokens, candidate.structured_output) == (64000, 2048, True)
    answer = requirements[ModelPurpose.ANSWER]
    assert answer.input_tokens == (
        8192 + answer_prompt_overhead_chars() + personal_memory_allowance_chars() + MAX_QUESTION_CHARS
    )
    assert not answer.structured_output
    assert set(requirements) == set(ModelPurpose)


def test_requirements_need_a_resolved_input_window() -> None:
    settings = _settings()

    with pytest.raises(ValueError):
        model_purpose_requirements(replace(settings, llm=replace(settings.llm, max_input_tokens=None)))


def test_the_shipped_catalog_serves_every_purpose() -> None:
    catalog = load_model_provider_catalog(REPO_ROOT / "configs" / "hosted-llm-providers.yaml")

    assert unservable_purposes(catalog, model_purpose_requirements(_settings())) == ()


_CATALOG = {
    "schema_version": "hosted-llm-providers.v2",
    "providers": [
        {
            "id": "tiny",
            "label": "Tiny",
            "protocol": "openai_chat",
            "base_url": "https://tiny.example.com/v1",
            "default_model": "tiny",
            "models": [
                {
                    "id": "tiny",
                    "label": "Tiny",
                    "context_window_tokens": 50_000,
                    "reasoning": {"parameter": "openai_reasoning_effort", "efforts": ["off", "high"], "default": "high"},
                    "structured_output": {"mode": "json_object", "revision": "r", "source": "s"},
                }
            ],
        }
    ],
}


def test_a_model_serves_a_purpose_when_some_reasoning_tier_fits() -> None:
    model = parse_model_provider_catalog(_CATALOG).providers[0].models[0]
    requirement = PurposeRequirement(input_tokens=40_000, output_tokens=2_000)

    # High reasoning reserves 16384 extra tokens and would not fit; off does.
    assert model_serves(model, requirement)
    assert not model_serves(model, PurposeRequirement(input_tokens=49_000, output_tokens=2_000))


def test_unservable_purposes_are_reported_unless_a_custom_provider_is_offered() -> None:
    requirements = {
        ModelPurpose.ANSWER: PurposeRequirement(input_tokens=200_000, output_tokens=900),
        ModelPurpose.EVIDENCE_SUMMARY: PurposeRequirement(input_tokens=10_000, output_tokens=900),
        ModelPurpose.SECTION_NAVIGATION: PurposeRequirement(input_tokens=10_000, output_tokens=900),
        ModelPurpose.CANDIDATE_GENERATION: PurposeRequirement(
            input_tokens=10_000, output_tokens=900, structured_output=True
        ),
    }

    assert unservable_purposes(parse_model_provider_catalog(_CATALOG), requirements) == (ModelPurpose.ANSWER,)

    with_custom = {
        **_CATALOG,
        "providers": [
            *_CATALOG["providers"],
            {
                "id": "custom",
                "label": "Custom",
                "protocol": "openai_chat",
                "custom": True,
                "reasoning_parameters": ["none"],
                "structured_output_modes": ["json_object"],
            },
        ],
    }
    assert unservable_purposes(parse_model_provider_catalog(with_custom), requirements) == ()
