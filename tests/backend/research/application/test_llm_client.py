from __future__ import annotations

import asyncio

import pytest

from backend.research.application import llm_client, llm_selectors
from framework.llm.models import LLMResponse
from framework.shared.graph_identity import GraphExecutionIdentity

_SELECTOR_KEYS = tuple(
    dict.fromkeys(
        (
            "OPENAI_MODEL",
            *llm_selectors.RESEARCH_ANSWER_BASE_URL_KEYS,
            *llm_selectors.RESEARCH_ANSWER_MODEL_KEYS,
            *llm_selectors.RESEARCH_ANSWER_API_KEY_ENV_KEYS,
            *llm_selectors.EVAL_JUDGE_BASE_URL_KEYS,
            *llm_selectors.EVAL_JUDGE_MODEL_KEYS,
            *llm_selectors.EVAL_JUDGE_API_KEY_ENV_KEYS,
        )
    )
)


@pytest.fixture(autouse=True)
def _hermetic_llm_env(monkeypatch):
    """Keep the repository .env from leaking real endpoints or models into these tests."""
    monkeypatch.setattr(llm_client, "load_root_env", lambda: None)
    for key in _SELECTOR_KEYS:
        monkeypatch.delenv(key, raising=False)


class _RecordingClient:
    def __init__(self, config, **kwargs) -> None:
        self.config = config

    def complete(self, request):
        return LLMResponse(content="ok", model=self.config.model)


def test_paper_answer_model_uses_research_route_not_generic_model(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "OpenAICompatibleClient", _RecordingClient)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://generic.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "generic-judge-model")
    monkeypatch.setenv("NEWS_RESEARCH_LLM_BASE_URL", "https://research.test/v1")
    monkeypatch.setenv("NEWS_RESEARCH_LLM_MODEL", "research-answer-model")
    monkeypatch.setenv("NEWS_RESEARCH_LLM_API_KEY_ENV", "RESEARCH_KEY")

    answer = llm_client.build_unity_paper_answer_model()._client.config
    judge = llm_client._build_generic_unity_client().config

    assert (answer.model, answer.base_url, answer.api_key_env) == (
        "research-answer-model",
        "https://research.test/v1",
        "RESEARCH_KEY",
    )
    assert (judge.model, judge.base_url, judge.api_key_env) == (
        "generic-judge-model",
        "https://generic.test/v1",
        "OPENAI_API_KEY",
    )


def test_paper_answer_model_falls_back_to_generic_selectors(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "OpenAICompatibleClient", _RecordingClient)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://generic.test/v1")
    monkeypatch.setenv("NEWS_LLM_MODEL", "news-model")
    monkeypatch.setenv("OPENAI_MODEL", "openai-model")

    answer = llm_client.build_unity_paper_answer_model()._client.config

    assert answer.model == "news-model"
    assert answer.base_url == "https://generic.test/v1"
    assert answer.api_key_env == "OPENAI_API_KEY"


def test_paper_answer_model_requires_configured_endpoint_and_model(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "OpenAICompatibleClient", _RecordingClient)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://generic.test/v1")

    with pytest.raises(ValueError, match="missing model"):
        llm_client.build_unity_paper_answer_model()


def test_research_settings_and_eval_builder_share_answer_selectors() -> None:
    from backend.research.application import llm_selectors
    from interfaces.composition import research_settings

    assert research_settings.RESEARCH_ANSWER_MODEL_KEYS is llm_selectors.RESEARCH_ANSWER_MODEL_KEYS
    assert research_settings.RESEARCH_ANSWER_BASE_URL_KEYS is llm_selectors.RESEARCH_ANSWER_BASE_URL_KEYS
    assert (
        research_settings.RESEARCH_ANSWER_API_KEY_ENV_KEYS
        is llm_selectors.RESEARCH_ANSWER_API_KEY_ENV_KEYS
    )


def test_build_unity_llm_call_binds_graph_execution_identity(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    captured = []

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def complete(self, request):
            captured.append(request)
            return LLMResponse(
                content="answer",
                execution_identity=request.execution_identity,
            )

    monkeypatch.setattr(llm_client, "OpenAICompatibleClient", _FakeClient)
    identity = GraphExecutionIdentity(
        run_id="run-1",
        graph_id="research.graph",
        graph_version="1",
        graph_ref="research.graph@1",
        graph_checksum="sha256:" + "1" * 64,
        node_id="answer",
        node_instance_id="answer:1",
        activity_id="activity-1",
        attempt=2,
    )

    call = llm_client.build_unity_llm_call(max_tokens=42, temperature=0.0)
    result = asyncio.run(call(
        "answer this", execution_identity=identity,
        timeout_seconds=1.25, max_transport_attempts=1,
    ))

    assert result == "answer"
    assert len(captured) == 1
    assert captured[0].execution_identity == identity
    assert captured[0].max_tokens == 42
    assert captured[0].timeout_seconds == 1.25
    assert captured[0].max_transport_attempts == 1


def test_unity_transport_budget_is_request_scoped_and_default_retry_is_unchanged(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = []
    policies = []

    class Client:
        def __init__(self, config, *, transport, retry_policy):
            policies.append(retry_policy)

        def complete(self, request):
            captured.append(request)
            return LLMResponse(content="answer")

    monkeypatch.setattr(llm_client, "OpenAICompatibleClient", Client)
    call = llm_client.build_unity_llm_call()
    asyncio.run(call("no personal memory"))
    asyncio.run(call("selected memory", max_transport_attempts=1))
    asyncio.run(call("no memory again"))
    assert [request.max_transport_attempts for request in captured] == [None, 1, None]
    assert len(policies) == 1
    assert policies[0].max_attempts == 3


def test_memory_send_never_retries_inside_transport_after_permission_is_revoked(monkeypatch) -> None:
    from urllib.error import URLError
    from backend.research.document.models import PaperChunk
    from backend.research.domain.personal_memory import PersonalMemorySourceRef
    from backend.research.domain.personal_memory_context import PersonalMemoryContext, PersonalMemoryContextItem
    from backend.research.rag.retrieval.contracts import RetrievalResult
    from backend.research.ports.paper_answer_model import PaperAnswerModelError
    from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator

    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    calls = []
    context = PersonalMemoryContext(policy_revision=1, items=(PersonalMemoryContextItem(
        memory_id="pmem_" + "1" * 32, revision=1, content="Prefer methods first.",
        category="reading_preference",
        source_refs=(PersonalMemorySourceRef(source_type="user_input", source_ref="user://alice"),),
    ),))

    class Provider:
        revoked = False
        checks = 0

        def current(self):
            self.checks += 1
            if self.revoked:
                raise PermissionError("remote model permission revoked")
            return context

    provider = Provider()

    def transport(request, timeout_seconds):
        calls.append(request)
        provider.revoked = True
        raise URLError("injected connection failure; no network is used")

    real_client = llm_client.OpenAICompatibleClient

    def client(config, **kwargs):
        return real_client(config, **kwargs, sleep=lambda _: None)

    monkeypatch.setattr(llm_client, "OpenAICompatibleClient", client)
    monkeypatch.setattr(llm_client, "_browser_ua_transport", transport)
    generator = AnswerGenerator(llm_client.build_unity_paper_answer_model())
    retrieval = RetrievalResult(parent_chunks=[], child_chunks=[PaperChunk(
        chunk_id="chunk-1", paper_id="p1", parse_source="latex", chunk_type="paragraph",
        content="The method retrieves verified evidence.",
    )], ref_chunks=[], intent="general")
    with pytest.raises(PaperAnswerModelError) as raised:
        asyncio.run(generator.generate("What is the method?", retrieval,
                    personal_memory_context=context, personal_memory_context_provider=provider))
    assert raised.value.code == "provider_unavailable"
    assert len(calls) == 1
    with pytest.raises(PermissionError):
        asyncio.run(generator.generate("What is the method?", retrieval,
                    personal_memory_context=context, personal_memory_context_provider=provider))
    assert provider.checks == 2
    assert len(calls) == 1  # No memory request is sent after revocation.

    # The same answer model retains the configured three attempts for ordinary requests.
    with pytest.raises(PaperAnswerModelError):
        asyncio.run(generator.generate("What is the method?", retrieval))
    assert len(calls) == 4


def test_unity_paper_answer_model_sends_system_and_user_messages(monkeypatch) -> None:
    from backend.research.ports.paper_answer_model import PaperAnswerModelRequest

    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    captured = []

    class _FakeClient:
        def __init__(self, config, **kwargs) -> None:
            self.config = config

        def complete(self, request):
            captured.append(request)
            return LLMResponse(content="Answer [1].", model="test-model", usage={"input_tokens": 9, "output_tokens": 3})

    monkeypatch.setattr(llm_client, "OpenAICompatibleClient", _FakeClient)
    model = llm_client.build_unity_paper_answer_model(max_tokens=77)

    response = asyncio.run(model.complete(PaperAnswerModelRequest(system_prompt="system", user_prompt="user")))

    assert response.text == "Answer [1]."
    assert response.model == "test-model"
    assert response.usage.input_tokens == 9
    assert captured[0].messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert captured[0].max_tokens == 77
    assert captured[0].temperature == 0.0
    assert captured[0].max_transport_attempts is None


def test_judge_uses_explicit_judge_model_and_never_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(llm_client, "OpenAICompatibleClient", _RecordingClient)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://generic.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "generic-model")
    monkeypatch.setenv("NEWS_RESEARCH_LLM_MODEL", "answer-model")

    with pytest.raises(ValueError, match="evaluation judge is not configured; missing model"):
        llm_client.build_unity_judge_llm_call()

    captured = []

    class _CapturingClient(_RecordingClient):
        def complete(self, request):
            captured.append((self.config.model, self.config.base_url, request.temperature))
            return LLMResponse(content="judged")

    monkeypatch.setattr(llm_client, "OpenAICompatibleClient", _CapturingClient)
    monkeypatch.setenv("NEWS_RAG_EVAL_JUDGE_MODEL", "judge-model")
    call = llm_client.build_unity_judge_llm_call(max_tokens=50)

    assert asyncio.run(call("judge this")) == "judged"
    assert captured == [("judge-model", "https://generic.test/v1", 0.0)]
