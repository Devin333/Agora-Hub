from __future__ import annotations

import asyncio

import pytest

from backend.research.application import llm_client
from framework.llm.models import LLMResponse
from framework.shared.graph_identity import GraphExecutionIdentity


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
    from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
    from framework.llm.clients.openai_compatible import LLMProviderError

    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
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
    call = llm_client.build_unity_llm_call()
    generator = AnswerGenerator(call)
    retrieval = RetrievalResult(parent_chunks=[], child_chunks=[PaperChunk(
        chunk_id="chunk-1", paper_id="p1", parse_source="latex", chunk_type="paragraph",
        content="The method retrieves verified evidence.",
    )], ref_chunks=[], intent="general")
    with pytest.raises(LLMProviderError):
        asyncio.run(generator.generate("What is the method?", retrieval,
                    personal_memory_context=context, personal_memory_context_provider=provider))
    assert len(calls) == 1
    with pytest.raises(PermissionError):
        asyncio.run(generator.generate("What is the method?", retrieval,
                    personal_memory_context=context, personal_memory_context_provider=provider))
    assert provider.checks == 2
    assert len(calls) == 1  # No memory request is sent after revocation.

    # This same application callable retains three attempts for ordinary requests.
    with pytest.raises(LLMProviderError):
        asyncio.run(call("No personal memory"))
    assert len(calls) == 4
