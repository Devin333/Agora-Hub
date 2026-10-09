from __future__ import annotations

import asyncio

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
