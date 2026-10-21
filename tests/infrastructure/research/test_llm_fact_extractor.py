from __future__ import annotations

import asyncio
import json

import pytest

from backend.research.ports.answer_fact_extraction import (
    AlignmentTarget,
    AnswerFactExtractionError,
    ClaimDecompositionPort,
    ClaimDecompositionRequest,
    FactExtractionPort,
    FactExtractionRequest,
    FactSourceRole,
    ScopeVocabulary,
)
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_semantic_facts import NumericUnit
from framework.llm.clients.fake import FakeLLMClient
from framework.llm.clients.openai_compatible import LLMProviderError
from infrastructure.research.llm_fact_extractor import (
    FACT_EXTRACTION_PROMPT_VERSION,
    FactExtractorOutputError,
    FactExtractorProviderError,
    LLMAnswerFactExtractor,
)

SOURCE = "Model A reaches 95.3% accuracy on the test split."


def _fact_payload(**overrides) -> dict:
    fact = {
        "statement": SOURCE,
        "value_text": "95.3%",
        "numeric_value": 95.3,
        "unit": "percent",
        "polarity": None,
        "categorical_value": "",
        "object_id": "Model A",
        "dataset_id": None,
        "split_id": "test",
        "statistic": "accuracy",
        "conditions": [{"name": "seed", "value": "7"}, {"name": " ", "value": "x"}],
        "modality": "text",
        "aligned_target_id": "ref-1",
    }
    fact.update(overrides)
    return {"facts": [fact]}


def _request(role: FactSourceRole = FactSourceRole.CANDIDATE) -> FactExtractionRequest:
    return FactExtractionRequest(
        request_id="claim-1",
        role=role,
        question="What accuracy does Model A reach?",
        source_text=SOURCE,
        source_text_digest=content_digest(SOURCE),
        vocabulary=ScopeVocabulary(statistics=("accuracy",)),
        alignment_targets=(AlignmentTarget(target_id="ref-1", text="Model A reaches 95.3%."),)
        if role is FactSourceRole.CANDIDATE else (),
    )


def _extractor(client) -> LLMAnswerFactExtractor:
    return LLMAnswerFactExtractor(client, provider="dashscope", model_id="deepseek-v4-flash")


def test_extractor_satisfies_both_ports_and_records_identity() -> None:
    extractor = _extractor(FakeLLMClient([]))
    assert isinstance(extractor, FactExtractionPort)
    assert isinstance(extractor, ClaimDecompositionPort)
    assert extractor.identity.prompt_version == FACT_EXTRACTION_PROMPT_VERSION
    assert extractor.identity.rule_version.endswith("+unpinned")


def test_fact_extraction_request_is_deterministic_and_schema_bound() -> None:
    client = FakeLLMClient([json.dumps(_fact_payload())])
    result = asyncio.run(_extractor(client).extract(_request()))
    draft = result.drafts[0]
    assert draft.unit == NumericUnit.PERCENT.value
    assert draft.categorical_value is None
    assert draft.dataset_id is None
    assert [item.name for item in draft.conditions] == ["seed"]
    assert draft.aligned_target_id == "ref-1"
    request = client.requests[0]
    assert request.temperature == 0
    assert request.output_schema_name == "research_fact_extraction"
    sent = json.loads(request.messages[1]["content"])
    assert sent["SOURCE_TEXT"] == SOURCE
    assert sent["ALIGNMENT_TARGETS"][0]["target_id"] == "ref-1"


def test_claim_decomposition_parses_claims() -> None:
    answer = "Yes."
    client = FakeLLMClient([json.dumps({"claims": [
        {"claim_text": "Model A uses pretraining.", "grounding_span": "Yes", "occurrence": 1},
    ]})])
    result = asyncio.run(_extractor(client).decompose(ClaimDecompositionRequest(
        request_id="answer-1",
        question="Does Model A use pretraining?",
        answer_text=answer,
        answer_text_digest=content_digest(answer),
    )))
    assert result.claims[0].grounding_span == "Yes"
    assert client.requests[0].output_schema_name == "research_claim_decomposition"


def test_malformed_and_contract_violating_output_is_an_error_not_a_dropped_draft() -> None:
    with pytest.raises(FactExtractorOutputError, match="strict JSON"):
        asyncio.run(_extractor(FakeLLMClient(["not json"])).extract(_request()))
    with pytest.raises(FactExtractorOutputError, match="schema"):
        asyncio.run(_extractor(FakeLLMClient([json.dumps({"facts": [{"statement": SOURCE}]})])).extract(_request()))
    with pytest.raises(AnswerFactExtractionError, match="contract"):
        asyncio.run(_extractor(FakeLLMClient([json.dumps(_fact_payload(statement=""))])).extract(_request()))


def test_provider_failure_is_mapped_to_a_port_error() -> None:
    class _FailingClient:
        def complete(self, request):
            raise LLMProviderError("rate limited", provider="dashscope", model="m", retryable=True)

        def stream(self, request):  # pragma: no cover - not used
            raise NotImplementedError

    with pytest.raises(FactExtractorProviderError) as raised:
        asyncio.run(_extractor(_FailingClient()).extract(_request()))
    assert isinstance(raised.value, AnswerFactExtractionError)
    assert raised.value.retryable is True
