from __future__ import annotations

import json

from framework.llm import (
    LOCAL_STRUCTURED_OUTPUT_DIALECT,
    ProviderStructuredOutputCapability,
    compile_structured_output_contract,
    structured_output_enforcement_keywords,
)
from framework.llm.clients.openai_compatible import OpenAICompatibleClient, OpenAICompatibleConfig
from infrastructure.research.guided_intent_candidate import GUIDED_INTENT_SCHEMA, StructuredGuidedIntentCandidateWorker
from tests.framework.llm._structured_output_release import approved_structured_output_release


def test_worker_uses_managed_schema_and_keeps_previous_result_order(monkeypatch) -> None:
    monkeypatch.setenv("TEST_GUIDED_KEY", "test-key")
    requests = []
    output = {
        "summary": "继续看第二篇论文",
        "query": "paper two follow-up",
        "sources": ["papers"],
        "constraints": {
            "recentYear": None, "hasCode": None, "paperType": None, "language": None,
            "license": None, "recentlyActive": None, "localRunnable": None,
        },
        "clarification": None,
    }

    def transport(request, timeout):
        requests.append(json.loads(request.data.decode("utf-8")))
        return json.dumps({
            "id": "guided-response",
            "choices": [{"message": {"role": "assistant", "content": json.dumps(output)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        }).encode("utf-8")

    contract = compile_structured_output_contract(GUIDED_INTENT_SCHEMA)
    capability = ProviderStructuredOutputCapability(
        provider="recorded",
        deployment="guided-model",
        mode="native_strict",
        supported_dialect=LOCAL_STRUCTURED_OUTPUT_DIALECT,
        supported_keywords=frozenset(structured_output_enforcement_keywords(contract.canonical_schema)),
        supports_local_refs=True,
        supports_stream_terminal_validation=True,
        revision="guided-native-v1",
        release=approved_structured_output_release(
            provider="recorded", deployment="guided-model", capability_revision="guided-native-v1",
        ),
    )
    client = OpenAICompatibleClient(
        OpenAICompatibleConfig(
            provider="recorded", base_url="https://llm.example/v1", model="guided-model",
            api_key_env="TEST_GUIDED_KEY", timeout_seconds=10.0,
        ),
        transport=transport,
        structured_output_capability=capability,
    )

    result = StructuredGuidedIntentCandidateWorker(client).generate_candidate({
        "question": "第二篇呢？",
        "answers": [],
        "previous": [{
            "question": "找论文", "results": [
                {"id": "one", "kind": "papers", "title": "One", "source": "arXiv", "url": "https://arxiv.org/abs/1"},
                {"id": "two", "kind": "papers", "title": "Two", "source": "arXiv", "url": "https://arxiv.org/abs/2"},
            ],
        }],
    })

    assert result == output
    sent = requests[0]
    assert sent["response_format"]["json_schema"]["schema"] == GUIDED_INTENT_SCHEMA
    assert '\"position\":2' in sent["messages"][1]["content"]


def test_worker_uses_validated_plain_json_when_deployment_has_no_managed_mode() -> None:
    output = {
        "summary": "找论文", "query": "agent", "sources": ["papers"], "constraints": {}, "clarification": None,
    }

    class Client:
        def complete(self, request):
            assert request.output_schema is None
            prompt = request.messages[1]["content"]
            assert "Response JSON Schema:" in prompt
            assert '"required":["summary","query","sources","constraints","clarification"]' in prompt
            from framework.llm import LLMResponse
            return LLMResponse(content=json.dumps(output))

    result = StructuredGuidedIntentCandidateWorker(Client(), managed_output=False).generate_candidate({
        "question": "找 Agent 论文",
    })
    assert result == output


def test_mixed_result_followup_preserves_positions_within_each_source():
    from infrastructure.research.guided_intent_candidate import _project_previous

    results = [{"id": f"{kind}-{index}", "kind": kind, "title": f"Result {index}"} for kind in ("papers", "projects") for index in range(1, 11)]
    context = _project_previous({"results": results}, include_results=True)
    assert len(context["results"]) == 20
    assert context["results"][11]["id"] == "projects-2"
    assert context["results"][11]["position"] == 2
