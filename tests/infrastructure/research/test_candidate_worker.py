from __future__ import annotations

import json
from typing import Any

import pytest

import infrastructure.research as research_adapters
from backend.research.ports.llm_worker import ResearchCandidateWorkerPort
from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.ports.reader_repair_candidate import (
    READER_REPAIR_APPLICATION_OBSERVATION_TASK,
    READER_REPAIR_PATCH_CANDIDATE_TASK,
)
from backend.research.rag.adapters.plan_worker import ResearchRAGPlanWorker
from framework.harness import RAGBudget, RAGSessionSpec, RetrievalGoal, WorkerRAGPlanner
from framework.llm import (
    LOCAL_STRUCTURED_OUTPUT_DIALECT,
    ProviderStructuredOutputCapability,
    LLMResponse,
    compile_structured_output_contract,
    structured_output_enforcement_keywords,
)
from framework.llm.structured_output import structured_output_graph_scope
from framework.shared.graph_identity import GraphExecutionIdentity
from tests.framework.llm._structured_output_release import (
    approved_structured_output_release,
)
from framework.llm.clients.openai_compatible import (
    LLMProviderError,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
)
from infrastructure.research.candidate_worker import (
    CANDIDATE_TASK_SCHEMAS,
    ResearchCandidateContractError,
    ResearchCandidateEvidenceScopeError,
    ResearchCandidateOutputError,
    ResearchCandidateProviderError,
    SUPPORTED_CANDIDATE_TASKS,
    StructuredResearchCandidateWorker,
)


@pytest.mark.parametrize("task", sorted(SUPPORTED_CANDIDATE_TASKS))
def test_worker_uses_task_specific_strict_schema_and_returns_candidate(
    monkeypatch: pytest.MonkeyPatch,
    task: str,
) -> None:
    worker, requests = _recorded_worker(monkeypatch, _valid_output(task))

    candidate = worker.generate_candidate(task=task, payload=_valid_payload(task))

    assert candidate == _valid_output(task)
    assert isinstance(worker, ResearchCandidateWorkerPort)
    request = requests[0]
    assert request["temperature"] == 0
    assert request["max_tokens"] == 2_048
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == CANDIDATE_TASK_SCHEMAS[task]
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False
    assert "quality or gate results" in request["messages"][0]["content"]


def test_planning_limits_reach_the_llm_request(monkeypatch):
    worker, _ = _recorded_worker(monkeypatch, _valid_output("candidate_task_plan"))
    complete = worker._client.complete
    limits = []

    def record(request):
        limits.append((request.timeout_seconds, request.max_transport_attempts))
        return complete(request)

    monkeypatch.setattr(worker._client, "complete", record)
    result = worker.generate_candidate(
        task="candidate_task_plan", payload=_valid_payload("candidate_task_plan"),
        timeout_seconds=30.0, max_transport_attempts=1,
    )
    assert result == _valid_output("candidate_task_plan")
    assert limits == [(30.0, 1)]


def test_every_candidate_schema_rejects_extra_fields_at_each_object_boundary() -> None:
    for task, schema in CANDIDATE_TASK_SCHEMAS.items():
        _assert_strict_objects(schema, path=task)


def test_candidate_worker_is_exported_by_research_adapter_package() -> None:
    assert (
        research_adapters.StructuredResearchCandidateWorker
        is StructuredResearchCandidateWorker
    )


def test_graph_candidate_request_binds_exact_execution_identity_and_release_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, _ = _recorded_worker(
        monkeypatch,
        _valid_output("candidate_task_plan"),
    )
    identity = GraphExecutionIdentity(
        run_id="research-run",
        graph_id="research.paper_analysis",
        graph_version="2",
        graph_ref="research.paper_analysis@2",
        graph_checksum="sha256:" + ("a" * 64),
        node_id="dynamic-analysis-stage",
        node_instance_id="dynamic-analysis-stage:1",
        activity_id="activity-1",
        attempt=1,
    )
    captured: list[Any] = []
    original_complete = worker._client.complete

    def capture(request: Any) -> Any:
        captured.append(request)
        return original_complete(request)

    monkeypatch.setattr(worker._client, "complete", capture)

    worker.generate_candidate(
        task="candidate_task_plan",
        payload=_valid_payload("candidate_task_plan"),
        execution_identity=identity,
    )

    request = captured[0]
    assert request.execution_identity == identity
    assert request.structured_output_policy.graph_scope == structured_output_graph_scope(
        identity
    )


def test_unknown_task_fails_before_calling_llm() -> None:
    client = _NeverClient()
    worker = StructuredResearchCandidateWorker(client)  # type: ignore[arg-type]

    with pytest.raises(ResearchCandidateContractError) as exc_info:
        worker.generate_candidate(
            task="candidate_publish_and_route",
            payload={"api_key": "DO-NOT-ECHO"},
        )

    assert client.calls == 0
    assert exc_info.value.error_code == "research_candidate_contract_error"
    assert exc_info.value.retryable is False
    assert exc_info.value.to_dict()["task"] is None
    assert "DO-NOT-ECHO" not in str(exc_info.value)


def test_malformed_json_maps_to_sanitized_non_retryable_output_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sk-response-TOPSECRET"
    worker, _ = _recorded_worker(monkeypatch, f"not-json {secret}", encode_json=False)

    with pytest.raises(ResearchCandidateOutputError) as exc_info:
        worker.generate_candidate(
            task="candidate_three_minute_read",
            payload=_valid_payload("candidate_three_minute_read"),
        )

    error = exc_info.value
    assert error.error_code == "research_candidate_output_error"
    assert error.retryable is False
    assert error.to_dict() == {
        "error_code": "research_candidate_output_error",
        "retryable": False,
        "task": "candidate_three_minute_read",
    }
    assert secret not in str(error)
    assert "not-json" not in str(error)


def test_non_standard_json_number_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _valid_output("candidate_three_minute_read")
    output["three_minute_read"]["confidence"] = float("nan")
    worker, _ = _recorded_worker(monkeypatch, output)

    with pytest.raises(ResearchCandidateOutputError):
        worker.generate_candidate(
            task="candidate_three_minute_read",
            payload=_valid_payload("candidate_three_minute_read"),
        )


def test_worker_rejects_unmanaged_structured_terminal_output() -> None:
    task = "candidate_three_minute_read"

    class UnmanagedClient:
        def complete(self, request):  # type: ignore[no-untyped-def]
            return LLMResponse(structured_output=_valid_output(task))

    worker = StructuredResearchCandidateWorker(UnmanagedClient())  # type: ignore[arg-type]

    with pytest.raises(ResearchCandidateOutputError):
        worker.generate_candidate(task=task, payload=_valid_payload(task))


@pytest.mark.parametrize("location", ["top", "nested"])
def test_extra_candidate_fields_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    output = _valid_output("candidate_three_minute_read")
    if location == "top":
        output["next_step"] = "publish_artifacts"
    else:
        output["three_minute_read"]["quality_passed"] = True
    worker, _ = _recorded_worker(monkeypatch, output)

    with pytest.raises(ResearchCandidateOutputError) as exc_info:
        worker.generate_candidate(
            task="candidate_three_minute_read",
            payload=_valid_payload("candidate_three_minute_read"),
        )

    assert exc_info.value.error_code == "research_candidate_output_error"
    assert "publish_artifacts" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("task", "mutate"),
    [
        (
            "candidate_three_minute_read",
            lambda output: output["three_minute_read"]["evidence_refs"][0].update(
                evidence_id="evidence-unknown"
            ),
        ),
        (
            "candidate_experiment_claims",
            lambda output: output["claims"][0].update(
                evidence_ids=["evidence-unknown"]
            ),
        ),
    ],
)
def test_unknown_evidence_id_is_rejected_after_schema_validation(
    monkeypatch: pytest.MonkeyPatch,
    task: str,
    mutate,
) -> None:
    output = _valid_output(task)
    mutate(output)
    worker, _ = _recorded_worker(monkeypatch, output)

    with pytest.raises(ResearchCandidateEvidenceScopeError) as exc_info:
        worker.generate_candidate(task=task, payload=_valid_payload(task))

    assert exc_info.value.error_code == "research_candidate_evidence_scope_error"
    assert exc_info.value.retryable is False
    assert "evidence-unknown" not in str(exc_info.value)


def test_evidence_candidate_fails_closed_when_payload_has_no_accepted_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, _ = _recorded_worker(
        monkeypatch,
        _valid_output("candidate_three_minute_read"),
    )
    payload = _valid_payload("candidate_three_minute_read")
    payload["evidence_pack"]["items"] = []

    with pytest.raises(ResearchCandidateEvidenceScopeError):
        worker.generate_candidate(
            task="candidate_three_minute_read",
            payload=payload,
        )


def test_evidence_id_cannot_be_paired_with_another_source_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _valid_output("candidate_three_minute_read")
    output["three_minute_read"]["evidence_refs"][0]["source_ref"] = (
        "paper://2606.00001/sec-experiment"
    )
    worker, _ = _recorded_worker(monkeypatch, output)

    with pytest.raises(ResearchCandidateEvidenceScopeError):
        worker.generate_candidate(
            task="candidate_three_minute_read",
            payload=_valid_payload("candidate_three_minute_read"),
        )


@pytest.mark.parametrize(
    ("task", "mutate"),
    [
        (
            "candidate_taxonomy",
            lambda output: output["taxonomy_candidates"][0].update(
                evidence_refs=["https://outside.example/private"]
            ),
        ),
        (
            "candidate_experiment_claims",
            lambda output: output["scores"][0].update(
                source_refs=["https://outside.example/private"]
            ),
        ),
    ],
)
def test_unknown_source_ref_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    task: str,
    mutate,
) -> None:
    output = _valid_output(task)
    mutate(output)
    worker, _ = _recorded_worker(monkeypatch, output)

    with pytest.raises(ResearchCandidateEvidenceScopeError):
        worker.generate_candidate(task=task, payload=_valid_payload(task))


@pytest.mark.parametrize(
    ("task", "mutate"),
    (
        (
            READER_REPAIR_PATCH_CANDIDATE_TASK,
            lambda output: output["patch_operations"][0].update(
                source_refs=["paper://outside/private"]
            ),
        ),
        (
            READER_REPAIR_APPLICATION_OBSERVATION_TASK,
            lambda output: output["observations"][0].update(
                evidence_refs=["paper://outside/private"]
            ),
        ),
    ),
)
def test_reader_repair_candidates_reject_refs_outside_issue_scope(
    monkeypatch: pytest.MonkeyPatch,
    task: str,
    mutate,
) -> None:
    output = _valid_output(task)
    mutate(output)
    worker, _ = _recorded_worker(monkeypatch, output)

    with pytest.raises(ResearchCandidateEvidenceScopeError):
        worker.generate_candidate(task=task, payload=_valid_payload(task))


def test_reader_repair_scope_excludes_refs_truncated_from_prompt_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _valid_payload(READER_REPAIR_PATCH_CANDIDATE_TASK)
    source_refs = [
        f"paper://2606.00001/section-{index:02d}" for index in range(40)
    ]
    payload["reader_repair_context_pack"]["source_refs"] = source_refs
    payload["reader_repair_context_pack"]["issue"]["source_refs"] = source_refs
    output = _valid_output(READER_REPAIR_PATCH_CANDIDATE_TASK)
    output["patch_operations"][0]["source_refs"] = [source_refs[-1]]
    worker, requests = _recorded_worker(monkeypatch, output)

    with pytest.raises(ResearchCandidateEvidenceScopeError):
        worker.generate_candidate(
            task=READER_REPAIR_PATCH_CANDIDATE_TASK,
            payload=payload,
        )

    prompt = "\n".join(
        message["content"] for message in requests[0]["messages"]
    )
    assert source_refs[0] in prompt
    assert source_refs[-1] not in prompt


def test_reader_repair_schemas_exclude_harness_owned_fields_and_open_maps() -> None:
    patch = CANDIDATE_TASK_SCHEMAS[READER_REPAIR_PATCH_CANDIDATE_TASK]
    observation = CANDIDATE_TASK_SCHEMAS[
        READER_REPAIR_APPLICATION_OBSERVATION_TASK
    ]

    assert set(patch["properties"]) == {
        "repair_summary",
        "patch_operations",
        "expected_effect",
        "risks",
        "confidence",
    }
    for definition_name, definition in patch["$defs"].items():
        properties = definition.get("properties", {})
        assert "metadata" not in properties
        if "op" in properties:
            assert "op" in definition["required"]
            assert "operation_id" not in properties
            assert "expected_before_checksum" not in properties
    assert set(observation["properties"]) == {"observations"}
    assert "metadata" not in observation["$defs"][
        "ReaderRepairVerificationObservation"
    ]["properties"]

    table_row = patch["$defs"]["ResearchTable"]["properties"]["rows"][
        "items"
    ]
    analysis_quality = patch["$defs"]["ResearchAnalysis"]["properties"][
        "quality"
    ]
    evidence_coverage = patch["$defs"]["ResearchEvidencePack"]["properties"][
        "coverage"
    ]
    assert set(table_row["properties"]) == {"entries"}
    assert set(analysis_quality["properties"]) == {"entries"}
    assert set(evidence_coverage["properties"]) == {"entries"}
    coverage_value = evidence_coverage["properties"]["entries"]["items"][
        "properties"
    ]["value"]
    assert coverage_value == {"type": "number", "minimum": 0, "maximum": 1}


@pytest.mark.parametrize(
    "task",
    (
        READER_REPAIR_PATCH_CANDIDATE_TASK,
        READER_REPAIR_APPLICATION_OBSERVATION_TASK,
    ),
)
def test_reader_repair_prompt_projection_removes_private_and_control_fields(
    monkeypatch: pytest.MonkeyPatch,
    task: str,
) -> None:
    payload = _valid_payload(task)
    if task == READER_REPAIR_PATCH_CANDIDATE_TASK:
        payload["reader_payload"]["paper"]["source_url"] = (
            "https://arxiv.org/abs/2606.00001?token=URL-PROMPT-SECRET"
        )
    worker, requests = _recorded_worker(monkeypatch, _valid_output(task))

    worker.generate_candidate(task=task, payload=payload)

    prompt = "\n".join(
        message["content"] for message in requests[0]["messages"]
    )
    for secret in (
        "Bearer PROMPT-SECRET",
        "CONTEXT-PROMPT-SECRET",
        "ISSUE-PROMPT-SECRET",
        "APPLICATION-PROMPT-SECRET",
        "URL-PROMPT-SECRET",
        "artifact://private",
    ):
        assert secret not in prompt
    assert '"metadata"' not in prompt
    assert '"artifact_refs"' not in prompt
    assert '"workflow_id"' not in prompt
    assert '"quality_passed"' not in prompt
    assert "paper://2606.00001/sec-method" in prompt


def test_taxonomy_candidate_can_cite_a_supplied_evidence_source_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _valid_output("candidate_taxonomy")
    output["taxonomy_candidates"][0]["evidence_refs"] = [
        "paper://2606.00001/sec-method"
    ]
    worker, _ = _recorded_worker(monkeypatch, output)

    candidate = worker.generate_candidate(
        task="candidate_taxonomy",
        payload=_valid_payload("candidate_taxonomy"),
    )

    assert candidate == output


def test_prompt_projection_excludes_secret_and_control_plane_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "top": "TOP-LEVEL-SECRET",
        "paper": "PAPER-METADATA-SECRET",
        "evidence": "EVIDENCE-METADATA-SECRET",
        "lineage": "LINEAGE-METADATA-SECRET",
        "query": "QUERY-TOKEN-SECRET",
        "non_http_userinfo": "NON-HTTP-USERINFO-SECRET",
        "non_http_fragment": "NON-HTTP-FRAGMENT-SECRET",
        "content_key": "sk-paper-content-TOPSECRET",
        "content_bearer": "Bearer EVIDENCE-CONTENT-TOPSECRET",
    }
    payload = _valid_payload("candidate_three_minute_read")
    payload["api_key"] = secrets["top"]
    payload["next_step"] = "publish_artifacts"
    payload["paper"]["metadata"] = {"password": secrets["paper"]}
    payload["paper"]["abstract"] += " " + secrets["content_key"]
    payload["paper"]["source_url"] = (
        "https://arxiv.org/abs/2606.00001?token=" + secrets["query"]
    )
    payload["paper"]["code_url"] = (
        "paper://user:" + secrets["non_http_userinfo"] + "@2606.00001/code"
    )
    payload["paper"]["pdf_url"] = (
        "paper://2606.00001/pdf#token=" + secrets["non_http_fragment"]
    )
    payload["evidence_pack"]["items"][0]["metadata"] = {
        "authorization": secrets["evidence"]
    }
    payload["evidence_pack"]["items"][0]["summary"] += (
        " " + secrets["content_bearer"]
    )
    payload["evidence_pack"]["lineage"]["metadata"] = {
        "credential": secrets["lineage"]
    }
    worker, requests = _recorded_worker(
        monkeypatch,
        _valid_output("candidate_three_minute_read"),
    )

    worker.generate_candidate(task="candidate_three_minute_read", payload=payload)

    prompt = "\n".join(
        message["content"] for message in requests[0]["messages"]
    )
    for secret in secrets.values():
        assert secret not in prompt
    assert "api_key" not in prompt
    assert '"next_step"' not in prompt
    assert "publish_artifacts" not in prompt
    assert "https://arxiv.org/abs/2606.00001" in prompt


def test_prompt_is_bounded_by_configured_input_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _valid_payload("candidate_taxonomy")
    payload["paper"]["abstract"] = "A" * 1_000_000
    payload["paper"]["authors"] = ["author" * 1_000] * 1_000
    worker, requests = _recorded_worker(
        monkeypatch,
        _valid_output("candidate_taxonomy"),
        max_input_tokens=2_048,
    )

    worker.generate_candidate(task="candidate_taxonomy", payload=payload)

    prompt = "\n".join(
        message["content"] for message in requests[0]["messages"]
    )
    assert len(prompt) <= 2_048 * 4
    assert len(json.dumps(requests[0], separators=(",", ":"))) <= 2_048 * 4
    assert len(prompt) < len(payload["paper"]["abstract"])


def test_provider_error_preserves_only_retryability_and_stable_code() -> None:
    secret = "credential=PROVIDER-TOPSECRET"
    error = LLMProviderError(
        f"raw provider failure {secret}",
        provider="recorded",
        error_type="rate_limit",
        retryable=True,
        status_code=429,
    )
    worker = StructuredResearchCandidateWorker(_FailingClient(error))  # type: ignore[arg-type]

    with pytest.raises(ResearchCandidateProviderError) as exc_info:
        worker.generate_candidate(
            task="candidate_taxonomy",
            payload=_valid_payload("candidate_taxonomy"),
        )

    mapped = exc_info.value
    assert mapped.to_dict() == {
        "error_code": "research_candidate_provider_error",
        "retryable": True,
        "task": "candidate_taxonomy",
    }
    assert secret not in str(mapped)
    assert "recorded" not in str(mapped)


def test_rag_candidate_control_fields_remain_harness_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _valid_output("rag_plan_candidate")
    output["candidate"]["metadata"]["halt_graph"] = True
    worker, _ = _recorded_worker(monkeypatch, output)

    with pytest.raises(ResearchCandidateOutputError):
        worker.generate_candidate(
            task="rag_plan_candidate",
            payload=_valid_payload("rag_plan_candidate"),
        )


def test_rag_candidate_rejects_source_ref_outside_supplied_session_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _valid_output("rag_plan_candidate")
    output["candidate"]["source_reading_plan"] = [
        {
            "step_id": "rag-plan-1:read-source",
            "operation": "read_source",
            "query": None,
            "corpus": None,
            "memory_namespace": None,
            "source_refs": ["paper://another-paper/sec-method"],
            "max_results": 0,
            "max_source_reads": 1,
            "timeout_seconds": None,
            "metadata": {
                "evidence_type": "method",
                "scope_ref": None,
            },
        }
    ]
    payload = _valid_payload("rag_plan_candidate")
    payload["session"]["source_policy"]["scope_ref"] = "paper://2606.00001"
    worker, requests = _recorded_worker(monkeypatch, output)

    with pytest.raises(ResearchCandidateEvidenceScopeError):
        worker.generate_candidate(
            task="rag_plan_candidate",
            payload=payload,
        )

    prompt = "\n".join(message["content"] for message in requests[0]["messages"])
    assert "paper://2606.00001/sec-method" in prompt
    assert '"required_scope_ref":"paper://2606.00001"' in prompt
    assert "paper://another-paper/sec-method" not in prompt


def test_rag_candidate_is_accepted_by_the_real_worker_planner_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, _ = _recorded_worker(
        monkeypatch,
        _valid_output("rag_plan_candidate"),
    )
    planner = WorkerRAGPlanner(ResearchRAGPlanWorker(worker))
    spec = RAGSessionSpec(
        session_id="rag-session-1",
        graph_identity=build_paper_analysis_context_graph_identity(
            run_id="run-1",
            stage_id="run_research_rag",
        ),
        goal=RetrievalGoal(
            goal_id="goal-1",
            question="What evidence supports the method?",
            required_evidence_types=("method",),
            target_entities=("Harness",),
        ),
        allowed_corpora=("research-papers",),
        allowed_memory_namespaces=("research:user:anonymous",),
        allowed_tools=("search_corpus",),
        budget=RAGBudget.safe_default(),
    )

    candidate = planner.plan(
        spec,
        round_index=1,
        gap_report={"missing_evidence_types": ["method"]},
        executed_queries=("previous query",),
    )

    assert candidate.candidate_id == "rag-plan-1"
    assert candidate.queries[0].operation.value == "search_corpus"
    assert candidate.metadata == {"planner": "llm"}


def _recorded_worker(
    monkeypatch: pytest.MonkeyPatch,
    output: Any,
    *,
    encode_json: bool = True,
    max_input_tokens: int = 8_192,
) -> tuple[StructuredResearchCandidateWorker, list[dict[str, Any]]]:
    monkeypatch.setenv("TEST_RESEARCH_LLM_KEY", "api-key-PROMPT-TOPSECRET")
    requests: list[dict[str, Any]] = []

    def transport(request, timeout: float) -> bytes:
        assert timeout == 10.0
        requests.append(json.loads(request.data.decode("utf-8")))
        content = json.dumps(output) if encode_json else str(output)
        return json.dumps(
            {
                "id": "recorded-response",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            }
        ).encode("utf-8")

    client = OpenAICompatibleClient(
        OpenAICompatibleConfig(
            provider="recorded",
            base_url="https://llm.example/v1",
            model="recorded-model",
            api_key_env="TEST_RESEARCH_LLM_KEY",
            timeout_seconds=10.0,
        ),
        transport=transport,
        structured_output_capability=_recorded_structured_output_capability(),
    )
    return (
        StructuredResearchCandidateWorker(
            client,
            max_input_tokens=max_input_tokens,
            max_output_tokens=2_048,
        ),
        requests,
    )


def _recorded_structured_output_capability() -> ProviderStructuredOutputCapability:
    supported_keywords: set[str] = set()
    for schema in CANDIDATE_TASK_SCHEMAS.values():
        contract = compile_structured_output_contract(schema)
        supported_keywords.update(
            structured_output_enforcement_keywords(contract.canonical_schema)
        )
    return ProviderStructuredOutputCapability(
        provider="recorded",
        deployment="recorded-model",
        mode="native_strict",
        supported_dialect=LOCAL_STRUCTURED_OUTPUT_DIALECT,
        supported_keywords=frozenset(supported_keywords),
        supports_local_refs=True,
        supports_stream_terminal_validation=True,
        revision="recorded-research-native-v1",
        release=approved_structured_output_release(
            provider="recorded",
            deployment="recorded-model",
            capability_revision="recorded-research-native-v1",
        ),
    )


def _valid_payload(task: str) -> dict[str, Any]:
    paper = {
        "paper_id": "2606.00001",
        "title": "Harness-grounded Research",
        "authors": ["Ada Lovelace"],
        "abstract": "A paper about deterministic Research gates.",
        "published_at": "2026-07-01T00:00:00Z",
        "source": "arxiv",
        "source_url": "https://arxiv.org/abs/2606.00001",
        "pdf_url": "https://arxiv.org/pdf/2606.00001.pdf",
        "code_url": None,
        "topics": ["cs.AI"],
    }
    evidence_pack = {
        "pack_id": "pack-2606.00001",
        "paper_id": "2606.00001",
        "items": [
            {
                "evidence_id": "evidence-method",
                "paper_id": "2606.00001",
                "title": "Method",
                "summary": "Harness owns routing while LLMs generate candidates.",
                "evidence_type": "method",
                "source_ref": "paper://2606.00001/sec-method",
                "span_refs": ["span-method-1"],
                "claim_refs": [],
                "confidence": 0.95,
            },
            {
                "evidence_id": "evidence-experiment",
                "paper_id": "2606.00001",
                "title": "Experiments",
                "summary": "The paper reports an accuracy result.",
                "evidence_type": "experiment",
                "source_ref": "paper://2606.00001/sec-experiment",
                "span_refs": ["span-experiment-1"],
                "claim_refs": [],
                "confidence": 0.9,
            },
        ],
        "missing_information": [],
        "lineage": {
            "source_refs": ["https://arxiv.org/abs/2606.00001"],
        },
    }
    if task == "candidate_three_minute_read":
        return {"paper": paper, "evidence_pack": evidence_pack}
    if task == "candidate_taxonomy":
        return {"paper": paper, "evidence_pack": evidence_pack}
    if task == "candidate_experiment_claims":
        return {"evidence_pack": evidence_pack}
    if task == "candidate_task_plan":
        return {
            "stage": {
                "run_id": "run-1",
                "workflow_id": "research.paper_analysis.dynamic",
                "stage_id": "dynamic_analysis_stage",
                "graph_checksum": "sha256:" + "1" * 64,
                "context_refs": {
                    "document": "document",
                    "evidence_pack": "evidence_pack",
                },
                "policy_ref": "research.analysis@1",
                "budget": {},
            },
            "required_output_roles": [
                "analysis.structure",
                "analysis.contribution",
                "analysis.experiments",
            ],
            "allowed_capabilities": [
                "research.analysis.structure",
                "research.analysis.contribution",
                "research.analysis.experiments",
            ],
        }
    if task == "rag_plan_candidate":
        return {
            "session": {
                "session_id": "rag-session-1",
                "run_id": "run-1",
                "workflow_id": "research.paper_analysis",
                "step_id": "run_research_rag",
                "goal": {
                    "goal_id": "goal-1",
                    "question": "What evidence supports the method?",
                    "required_evidence_types": ["method"],
                    "target_entities": ["Harness"],
                    "missing_information": [],
                },
                "allowed_corpora": ["research-papers"],
                "allowed_memory_namespaces": ["research:user:anonymous"],
                "allowed_tools": ["search_corpus"],
                "source_policy": {
                    "allowed_source_refs": ["paper://2606.00001/sec-method"],
                },
                "budget": {
                    "max_rounds": 2,
                    "max_replans": 1,
                    "max_queries": 4,
                    "max_source_reads": 8,
                    "max_memory_hits": 4,
                    "max_context_items": 8,
                    "max_context_tokens": 2_048,
                    "max_worker_calls": 8,
                },
            },
            "round_index": 1,
            "gap_report": {"missing_evidence_types": ["method"]},
            "executed_queries": ["previous query"],
            "forbidden_fields": ["quality_passed", "halt_graph"],
        }
    if task == READER_REPAIR_PATCH_CANDIDATE_TASK:
        source_ref = "paper://2606.00001/sec-method"
        return {
            "reader_payload": {
                "payload_id": "reader-payload-2606.00001",
                "paper": paper,
                "document": {
                    "paper_id": "2606.00001",
                    "source_hash": "sha256-reader-source",
                    "sections": [],
                    "lineage": {"source_refs": [source_ref]},
                    "metadata": {"artifact_refs": ["artifact://private"]},
                },
                "metadata": {"authorization": "Bearer PROMPT-SECRET"},
            },
            "reader_repair_context_pack": {
                "context_id": "reader-repair-context-1",
                "issue": {
                    "issue_id": "reader-issue-1",
                    "paper_id": "2606.00001",
                    "run_id": "reader-repair-run-1",
                    "issue_type": "missing_required_section",
                    "error_signature": "missing-method",
                    "symptom": "The method section is missing.",
                    "source_refs": [source_ref],
                },
                "source_refs": [source_ref],
                "metadata": {"token": "CONTEXT-PROMPT-SECRET"},
            },
        }
    if task == READER_REPAIR_APPLICATION_OBSERVATION_TASK:
        source_ref = "paper://2606.00001/sec-method"
        return {
            "reader_issue": {
                "issue_id": "reader-issue-1",
                "paper_id": "2606.00001",
                "run_id": "reader-repair-run-1",
                "issue_type": "missing_required_section",
                "error_signature": "missing-method",
                "symptom": "The method section is missing.",
                "source_refs": [source_ref],
                "metadata": {"password": "ISSUE-PROMPT-SECRET"},
            },
            "reader_repair_patch_candidate": {
                "candidate_id": "reader-repair-candidate-1",
                "repair_summary": "Restore the method section.",
                "target_region_refs": [source_ref],
                "patch_operations": [],
                "expected_effect": "The method section is visible.",
                "metadata": {"next_step": "publish"},
            },
            "reader_repair_application_candidate": {
                "application_id": "reader-repair-application-1",
                "candidate_id": "reader-repair-candidate-1",
                "source_refs": [source_ref],
                "metadata": {"api_key": "APPLICATION-PROMPT-SECRET"},
            },
        }
    raise AssertionError(task)


def _valid_output(task: str) -> dict[str, Any]:
    if task == "candidate_three_minute_read":
        return {
            "three_minute_read": {
                "problem": "Research agents need auditable workflow control.",
                "core_idea": "Harness owns routing and gates.",
                "key_contributions": ["Bounded candidate generation"],
                "method_summary": "A deterministic PLAN EXECUTE VERIFY runtime.",
                "experiment_summary": "The paper reports an accuracy result.",
                "limitations": ["Single-paper scope"],
                "why_it_matters": "Candidates cannot publish themselves.",
                "read_next": ["Deterministic gate results"],
                "evidence_refs": [
                    {
                        "evidence_id": "evidence-method",
                        "source_ref": "paper://2606.00001/sec-method",
                        "section_id": "sec-method",
                        "confidence": 0.95,
                    }
                ],
                "confidence": 0.9,
            }
        }
    if task == "candidate_task_plan":
        return {
            "tasks": [
                {
                    "task_id": "analyze-structure",
                    "objective": "Analyze paper structure from accepted evidence.",
                    "worker_capability": "research.analysis.structure",
                    "input_refs": ["document", "evidence_pack"],
                    "depends_on": [],
                    "priority": 10,
                },
                {
                    "task_id": "analyze-contribution",
                    "objective": "Analyze paper contributions from accepted evidence.",
                    "worker_capability": "research.analysis.contribution",
                    "input_refs": ["document", "evidence_pack"],
                    "depends_on": [],
                    "priority": 10,
                },
                {
                    "task_id": "analyze-experiments",
                    "objective": "Analyze experiments after the structure task.",
                    "worker_capability": "research.analysis.experiments",
                    "input_refs": ["document", "evidence_pack"],
                    "depends_on": ["analyze-structure"],
                    "priority": 5,
                },
            ],
            "requested_max_parallelism": 3,
        }
    if task == "candidate_taxonomy":
        return {
            "taxonomy_candidates": [
                {
                    "level": "domain",
                    "term_id": "code",
                    "label": "Code",
                    "evidence_refs": ["https://arxiv.org/abs/2606.00001"],
                    "confidence": 0.85,
                }
            ]
        }
    if task == "candidate_experiment_claims":
        return {
            "claims": [
                {
                    "claim_id": "claim-method",
                    "text": "Harness owns routing.",
                    "claim_type": "method",
                    "section_id": "sec-method",
                    "evidence_ids": ["evidence-method"],
                    "confidence": 0.9,
                }
            ],
            "scores": [
                {
                    "score_id": "score-accuracy",
                    "benchmark_id": "claim_verification",
                    "dataset_id": "single_paper_eval",
                    "metric_id": "accuracy",
                    "value": 0.87,
                    "source_refs": ["paper://2606.00001/sec-experiment"],
                }
            ],
        }
    if task == "rag_plan_candidate":
        return {
            "candidate": {
                "candidate_id": "rag-plan-1",
                "queries": [
                    {
                        "step_id": "rag-plan-1:query",
                        "operation": "search_corpus",
                        "query": "method evidence",
                        "corpus": "research-papers",
                        "memory_namespace": None,
                        "source_refs": [],
                        "max_results": 3,
                        "max_source_reads": 0,
                        "timeout_seconds": None,
                        "metadata": {
                            "evidence_type": "method",
                            "scope_ref": None,
                        },
                    }
                ],
                "source_reading_plan": [],
                "memory_recall_plan": [],
                "expected_evidence": ["method"],
                "expected_gaps": [],
                "risks": ["bounded_retrieval_budget"],
                "confidence": 0.8,
                "metadata": {"planner": "llm"},
            }
        }
    if task == READER_REPAIR_PATCH_CANDIDATE_TASK:
        return {
            "repair_summary": "Remove the ungrounded analysis candidate.",
            "patch_operations": [
                {
                    "op": "remove_analysis",
                    "source_refs": ["paper://2606.00001/sec-method"],
                }
            ],
            "expected_effect": "The ungrounded analysis is removed.",
            "risks": ["analysis must be regenerated after verification"],
            "confidence": 0.84,
        }
    if task == READER_REPAIR_APPLICATION_OBSERVATION_TASK:
        return {
            "observations": [
                {
                    "check_id": "source-backed-method",
                    "finding": "The repaired method cites the accepted source.",
                    "evidence_refs": ["paper://2606.00001/sec-method"],
                }
            ]
        }
    raise AssertionError(task)


def _assert_strict_objects(schema: Any, *, path: str) -> None:
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, path
        for name, child in schema.get("properties", {}).items():
            _assert_strict_objects(child, path=f"{path}.{name}")
    item_schema = schema.get("items")
    if item_schema is not None:
        _assert_strict_objects(item_schema, path=f"{path}[]")


class _NeverClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        raise AssertionError("LLM must not be called")


class _FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def complete(self, request):
        raise self.error
