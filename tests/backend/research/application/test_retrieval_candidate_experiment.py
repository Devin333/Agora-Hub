from __future__ import annotations

from dataclasses import replace

import backend.research.rag.retrieval.candidate_experiment as candidate_experiment

import pytest

from backend.research.rag.retrieval.candidate_experiment import (
    ResearchCandidatePlanner,
    RetrievalCandidateExperimentConfig,
    evaluate_retrieval_candidate,
)
from framework.harness import (
    BoundedRAGSessionController,
    RAGBudget,
    RetrievalOperation,
    RetrievalPlanCandidate,
    RetrievalStepSpec,
    fake_rag_session_spec,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.rag.fake import (
    FakeRAGContextPackAssembler,
    FakeSourceVerifier,
    fake_research_evidence_packs,
)
from framework.harness.retrieval.fake import FakeRetrievalPort
from framework.harness.workers.result import HarnessWorkerResult, HarnessWorkerStatus


class _Worker:
    def __init__(self, candidate: dict | None = None, *, status=HarnessWorkerStatus.SUCCEEDED):
        self.candidate = candidate
        self.status = status
        self.requests: list[dict] = []

    def generate(self, request, **_kwargs):
        self.requests.append(request)
        output = {} if self.candidate is None else {"candidate": self.candidate}
        return HarnessWorkerResult(status=self.status, output=output)


def _candidate(query: str, *, metadata: dict | None = None, corpus: str = "research-papers") -> dict:
    return {
        "candidate_id": "candidate-1",
        "queries": [{
            "step_id": "candidate-1:query",
            "operation": RetrievalOperation.SEARCH_CORPUS.value,
            "query": query,
            "corpus": corpus,
            "max_results": 3,
        }],
        "metadata": metadata or {},
    }


def _spec(question: str, *, constraints=None, target_entities=()):
    base = fake_rag_session_spec(required_evidence_types=("experiment",))
    return replace(
        base,
        goal=replace(
            base.goal,
            question=question,
            constraints=dict(constraints or {}),
            target_entities=tuple(target_entities),
        ),
    )


def test_candidate_experiment_is_default_off_and_never_calls_worker() -> None:
    worker = _Worker(_candidate("worker query"))
    planner = ResearchCandidatePlanner(worker)
    spec = _spec("What does Atlas report?")

    plan = planner.plan(spec, round_index=0, gap_report={})

    assert worker.requests == []
    assert plan.queries[0].query == spec.goal.question
    assert plan.metadata["candidate_experiment"]["reason"] == "experiment_disabled"


def test_enabled_candidate_preserves_original_query_and_serializable_lineage() -> None:
    question = "What does Atlas report in 2024 for accuracy?"
    worker = _Worker(_candidate(question + " experiment"))
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True, allowed_expansion_terms=("experiment",)),
    )

    plan = planner.plan(_spec(question, target_entities=("Atlas",)), round_index=0, gap_report={})
    payload = plan.to_dict()

    assert payload["metadata"]["candidate_experiment"]["accepted"] is True
    assert payload["metadata"]["candidate_experiment_config"]["config_version"] == "research-retrieval-candidate-config-v1"
    assert payload["metadata"]["candidate_experiment_config"]["enabled"] is True
    assert payload["queries"][0]["metadata"]["original_query"] == question
    assert (
        payload["queries"][0]["metadata"]["scope_ref"]
        == _spec(question).source_policy["scope_ref"]
    )
    assert worker.requests[0]["candidate_query_limit"] == 2
    assert worker.requests[0]["timeout_seconds"] == 8.0
    assert worker.requests[0]["max_transport_attempts"] == 1


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("Which Atlas results are not evaluated? not experiment", "unbounded_candidate_expansion"),
        ("Which Atlas results are evaluated?", "negation_constraint_changed"),
        ("Which AtlasX results are not evaluated?", "entity_constraint_missing"),
        ("Which Atlas v10 results in 2024 show accuracy?", "version_constraint_missing"),
        ("Which Atlas v1 results in 2024 show inaccuracy?", "metric_constraint_missing"),
    ],
)
def test_candidate_constraint_changes_fail_closed(query: str, reason: str) -> None:
    question = "Which Atlas v1 results in 2024 show accuracy and are not evaluated?"
    candidate = RetrievalPlanCandidate.from_dict(_candidate(query))

    result = evaluate_retrieval_candidate(
        _spec(question, target_entities=("Atlas",)),
        candidate,
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    assert result.accepted is False
    assert result.used_fallback is True
    assert any(item["code"] == reason for item in result.violations)


def test_unicode_configured_constraints_are_exact_and_preserved() -> None:
    question = "比较星河模型，不包含英文资料"
    config = RetrievalCandidateExperimentConfig(
        enabled=True,
        configured_hard_constraints={"entity": ("星河模型",), "language": ("中文",)},
        allowed_expansion_terms=("中文",),
    )
    accepted = RetrievalPlanCandidate.from_dict(_candidate(question + " 中文"))
    rejected = RetrievalPlanCandidate.from_dict(_candidate(question + " 中"))

    assert evaluate_retrieval_candidate(_spec(question), accepted, config=config).accepted is True
    result = evaluate_retrieval_candidate(_spec(question), rejected, config=config)
    assert result.accepted is False
    assert any(item["code"] == "language_constraint_missing" for item in result.violations)


def test_allowed_expansions_require_complete_token_or_phrase_matches() -> None:
    question = "What does Atlas report?"
    config = RetrievalCandidateExperimentConfig(
        enabled=True,
        allowed_expansion_terms=("table", "name"),
    )

    accepted = RetrievalPlanCandidate.from_dict(_candidate(question + " table name"))
    concatenated = RetrievalPlanCandidate.from_dict(_candidate(question + " tablename"))

    assert evaluate_retrieval_candidate(_spec(question), accepted, config=config).accepted is True
    result = evaluate_retrieval_candidate(_spec(question), concatenated, config=config)
    assert result.accepted is False
    assert any(item["code"] == "unbounded_candidate_expansion" for item in result.violations)


@pytest.mark.parametrize(
    "metadata",
    [
        {"publish": True},
        {"passed": True},
        {"eligible": True},
        {"diagnostics": {"qualification_ref": "forged://qualification"}},
    ],
)
def test_worker_control_fields_and_query_limit_fall_back_to_original_plan(metadata) -> None:
    question = "What does Atlas report?"
    worker = _Worker(_candidate(question, metadata=metadata))
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True, max_candidate_queries=1),
    )

    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert plan.queries[0].query == question
    assert plan.metadata["candidate_experiment"]["accepted"] is False
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_invalid"


def test_worker_failure_and_budget_exhaustion_use_stable_fallback_reasons() -> None:
    spec = _spec("What does Atlas report?")
    worker = _Worker(status=HarnessWorkerStatus.FAILED)
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True, max_worker_calls=1),
    )

    first = planner.plan(spec, round_index=0, gap_report={})
    second = planner.plan(spec, round_index=1, gap_report={})

    assert first.metadata["candidate_experiment"]["fallback_reason"] == "worker_failed"
    assert second.metadata["candidate_experiment"]["fallback_reason"] == "worker_budget_exhausted"
    assert "error" not in first.metadata.get("candidate_fallback", {})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": "false"},
        {"max_candidate_queries": True},
        {"max_worker_calls": False},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"configured_hard_constraints": {"entity": None}},
        {"configured_hard_constraints": {"entity": "Atlas"}},
        {"configured_hard_constraints": {"entity": {"Atlas"}}},
        {"configured_hard_constraints": {"entity": ("Atlas", 1)}},
        {"allowed_expansion_terms": "experiment"},
        {"allowed_expansion_terms": ("",)},
        {"allowed_expansion_terms": (" experiment",)},
        {"allowed_expansion_terms": ("experiment", "experiment")},
        {"allowed_expansion_terms": ("experiment", 1)},
        {"configured_hard_constraints": []},
        {"configured_hard_constraints": {1: ("Atlas",)}},
        {"configured_hard_constraints": {" entity": ("Atlas",)}},
        {"configured_hard_constraints": {"entity": ("Atlas", "Atlas")}},
    ],
)
def test_candidate_experiment_config_rejects_ambiguous_or_invalid_values(kwargs) -> None:
    with pytest.raises((TypeError, ValueError)):
        RetrievalCandidateExperimentConfig(**kwargs)


def test_candidate_experiment_config_is_deeply_stable_against_external_mutation() -> None:
    expansion_terms = ["experiment"]
    entity_values = ["Atlas"]
    configured = {"entity": entity_values}
    config = RetrievalCandidateExperimentConfig(
        enabled=True,
        allowed_expansion_terms=expansion_terms,
        configured_hard_constraints=configured,
    )
    before = config.to_dict()

    expansion_terms.append("unbounded")
    entity_values.append("AtlasX")
    configured["language"] = ["English"]

    assert config.to_dict() == before
    assert config.allowed_expansion_terms == ("experiment",)
    assert config.configured_hard_constraints == {"entity": ("Atlas",)}
    with pytest.raises(TypeError):
        config.configured_hard_constraints["entity"] = ("AtlasX",)


@pytest.mark.parametrize(
    "metadata",
    [
        {"diagnostics": {"publication": {"publish": True}}},
        {"outer": [{"quality_passed": True}]},
        {"diagnostics": {"routing": {"tool_authorization": "forged"}}},
    ],
)
def test_candidate_object_cannot_bypass_recursive_control_field_rejection(metadata) -> None:
    question = "What does Atlas report?"
    candidate_payload = _candidate(question)
    candidate_payload["metadata"] = metadata
    candidate = RetrievalPlanCandidate.from_dict(candidate_payload)
    worker = _Worker(candidate)
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert len(worker.requests) == 1
    assert plan.queries[0].query == question
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_invalid"
    assert plan.metadata["planner"] == "deterministic"


def test_typed_candidate_rejects_top_level_control_field_during_construction() -> None:
    payload = _candidate(
        "What does Atlas report?",
        metadata={"routing": {"tool_authorization": "forged"}},
    )

    with pytest.raises(HarnessValidationError, match="flow-control fields"):
        RetrievalPlanCandidate.from_dict(payload)


def test_raw_candidate_top_level_control_field_falls_back_before_construction() -> None:
    question = "What does Atlas report?"
    payload = _candidate(
        question,
        metadata={"routing": {"tool_authorization": "forged"}},
    )
    worker = _Worker(payload)
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert len(worker.requests) == 1
    assert plan.queries[0].query == question
    assert plan.metadata["planner"] == "deterministic"
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_invalid"


def test_query_step_object_cannot_bypass_nested_control_field_rejection() -> None:
    question = "What does Atlas report?"
    candidate_payload = _candidate(question)
    candidate_payload["queries"][0]["metadata"] = {
        "diagnostics": [{"write_memory": True}]
    }
    worker = _Worker(RetrievalPlanCandidate.from_dict(candidate_payload))
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert plan.queries[0].query == question
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_invalid"


def test_validator_exception_uses_stable_original_plan_fallback(monkeypatch) -> None:
    question = "What does Atlas report?"
    worker = _Worker(_candidate(question))
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    def fail_validation(*_args, **_kwargs):
        raise RuntimeError("provider-secret-validator-detail")

    monkeypatch.setattr(
        candidate_experiment, "evaluate_retrieval_candidate", fail_validation
    )
    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert plan.queries[0].query == question
    assert plan.metadata["planner"] == "deterministic"
    assert plan.metadata["candidate_experiment"]["reason"] == "candidate_validation_failed"
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_validation_failed"
    assert "provider-secret" not in str(plan.to_dict())


def test_candidate_query_count_respects_remaining_session_budget() -> None:
    question = "What does Atlas report?"
    candidate_payload = _candidate(question)
    candidate_payload["queries"].append({
        **candidate_payload["queries"][0],
        "step_id": "candidate-1:query-2",
        "query": question + " experiment",
    })
    worker = _Worker(candidate_payload)
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(
            enabled=True,
            max_candidate_queries=2,
        ),
    )
    spec = _spec(question)
    spec = replace(spec, budget=replace(spec.budget, max_queries=1))

    plan = planner.plan(spec, round_index=0, gap_report={})

    assert len(worker.requests) == 1
    assert len(plan.queries) == 1
    assert plan.queries[0].query == question
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_budget_exceeded"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_refs", ["paper://p1/section-2"]),
        ("max_source_reads", 1),
        ("memory_namespace", "research:private"),
    ],
)
def test_search_candidate_cannot_smuggle_additional_read_or_memory_actions(
    field, value
) -> None:
    question = "What does Atlas report?"
    candidate_payload = _candidate(question)
    candidate_payload["queries"][0][field] = value
    worker = _Worker(candidate_payload)
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert len(worker.requests) == 1
    assert plan.queries[0].query == question
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "unsupported_operation"


@pytest.mark.parametrize(
    "candidate_query",
    [
        "what does Atlas report?",
        "What  does Atlas report?",
        " What does Atlas report?",
        "What does Atlas report? ",
        "Ｗhat does Atlas report?",
    ],
)
def test_v1_candidate_must_preserve_original_query_verbatim(candidate_query: str) -> None:
    question = "What does Atlas report?"
    candidate = RetrievalPlanCandidate.from_dict(_candidate(candidate_query))

    result = evaluate_retrieval_candidate(
        _spec(question),
        candidate,
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    assert result.accepted is False
    assert any(
        violation["code"] == "unbounded_candidate_expansion"
        for violation in result.violations
    )


def test_unknown_worker_fallback_detail_is_not_exposed() -> None:
    question = "What does Atlas report?"
    worker = _Worker(
        _candidate(
            question,
            metadata={"candidate_fallback": {"reason": "provider-secret-detail"}},
        )
    )
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert plan.queries[0].query == question
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_invalid"
    assert "provider-secret-detail" not in str(plan.metadata["candidate_experiment"])


def test_real_controller_executes_deterministic_fallback_for_overbudget_candidate() -> None:
    question = "What does Atlas report?"
    candidate_payload = _candidate(question)
    candidate_payload["queries"].append({
        **candidate_payload["queries"][0],
        "step_id": "candidate-1:query-2",
        "query": question + " experiment",
    })
    worker = _Worker(candidate_payload)
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(
            enabled=True,
            max_candidate_queries=2,
        ),
    )
    retrieval = FakeRetrievalPort(fake_research_evidence_packs())
    spec = _spec(question)
    spec = replace(
        spec,
        budget=RAGBudget(
            max_rounds=1,
            max_replans=0,
            max_queries=1,
            max_source_reads=0,
            max_memory_hits=1,
            max_context_items=8,
            max_context_tokens=2048,
            max_worker_calls=2,
        ),
    )
    controller = BoundedRAGSessionController(
        retrieval=retrieval,
        planner=planner,
        source_verifier=FakeSourceVerifier(),
        context_pack_assembler=FakeRAGContextPackAssembler(),
    )

    result = controller.run(spec)

    assert len(worker.requests) == 1
    assert len(retrieval.requests) == 1
    assert retrieval.requests[0].query == question + " experiment"
    assert result.budget_snapshot.queries_used == 1
    plan_event = next(
        event
        for event in result.transcript.events
        if event["event_type"] == "rag_plan_candidate_created"
    )
    plan = plan_event["payload"]["plan"]
    assert plan["metadata"]["planner"] == "deterministic"
    assert (
        plan["metadata"]["candidate_experiment"]["fallback_reason"]
        == "candidate_budget_exceeded"
    )


@pytest.mark.parametrize(
    ("query", "constraints", "reason"),
    [
        (
            "Which Atlas results in 2025 use English arXiv evidence for reported split?",
            {"source": "arXiv", "language": "English", "dataset_split": "reported split"},
            "time_constraint_missing",
        ),
        (
            "Which Atlas results in 2024 use French arXiv evidence for reported split?",
            {"source": "arXiv", "language": "English", "dataset_split": "reported split"},
            "language_constraint_missing",
        ),
        (
            "Which Atlas results in 2024 use English publisher evidence for reported split?",
            {"source": "arXiv", "language": "English", "dataset_split": "reported split"},
            "source_constraint_missing",
        ),
        (
            "Which Atlas results in 2024 use English arXiv evidence for hidden split?",
            {"source": "arXiv", "language": "English", "dataset_split": "reported split"},
            "configured_constraint_missing",
        ),
    ],
)
def test_source_language_time_and_explicit_constraints_are_preserved(
    query, constraints, reason
) -> None:
    question = (
        "Which Atlas results in 2024 use English arXiv evidence for reported split?"
    )
    result = evaluate_retrieval_candidate(
        _spec(question, constraints=constraints, target_entities=("Atlas",)),
        RetrievalPlanCandidate.from_dict(_candidate(query)),
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    assert result.accepted is False
    assert any(item["code"] == reason for item in result.violations)


@pytest.mark.parametrize(
    ("operation", "corpus", "reason"),
    [
        (RetrievalOperation.READ_SOURCE.value, "research-papers", "unsupported_operation"),
        (RetrievalOperation.SEARCH_CORPUS.value, "external-web", "source_constraint_changed"),
    ],
)
def test_candidate_operation_and_corpus_remain_in_allowed_search_scope(
    operation, corpus, reason
) -> None:
    question = "What does Atlas report?"
    payload = _candidate(question, corpus=corpus)
    payload["queries"][0]["operation"] = operation
    result = evaluate_retrieval_candidate(
        _spec(question),
        RetrievalPlanCandidate.from_dict(payload),
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    assert result.accepted is False
    assert any(item["code"] == reason for item in result.violations)


def test_configured_candidate_limit_falls_back_before_retrieval_execution() -> None:
    question = "What does Atlas report?"
    payload = _candidate(question)
    payload["queries"].append({
        **payload["queries"][0],
        "step_id": "candidate-1:query-2",
        "query": question + " experiment",
    })
    worker = _Worker(payload)
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(
            enabled=True, max_candidate_queries=1
        ),
    )

    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert len(worker.requests) == 1
    assert plan.queries[0].query == question
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_invalid"


def test_worker_timeout_and_missing_candidate_use_stable_fallback_reasons() -> None:
    question = "What does Atlas report?"

    class TimeoutWorker:
        def generate(self, _request, **_kwargs):
            raise TimeoutError("provider secret timeout detail")

    timeout_plan = ResearchCandidatePlanner(
        TimeoutWorker(),
        config=RetrievalCandidateExperimentConfig(enabled=True),
    ).plan(_spec(question), round_index=0, gap_report={})
    missing_plan = ResearchCandidatePlanner(
        _Worker(None),
        config=RetrievalCandidateExperimentConfig(enabled=True),
    ).plan(_spec(question), round_index=0, gap_report={})

    assert timeout_plan.metadata["candidate_experiment"]["fallback_reason"] == "worker_timeout"
    assert missing_plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_missing"
    assert "provider secret" not in str(timeout_plan.to_dict())


def test_session_worker_budget_prevents_candidate_call() -> None:
    question = "What does Atlas report?"
    worker = _Worker(_candidate(question))
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True, max_worker_calls=2),
    )
    spec = _spec(question)
    spec = replace(spec, budget=replace(spec.budget, max_worker_calls=0))

    plan = planner.plan(spec, round_index=0, gap_report={})

    assert worker.requests == []
    assert plan.queries[0].query == question
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "worker_budget_exhausted"


@pytest.mark.parametrize("typed", [False, True], ids=["raw-dict", "typed-candidate"])
def test_worker_cannot_forge_reserved_fallback_marker(typed: bool) -> None:
    question = "What does Atlas report?"
    payload = _candidate(
        "Use an unrelated unrestricted query",
        metadata={
            "candidate_fallback": {
                "reason": "worker_timeout",
                "original_query": question,
            }
        },
    )
    worker = _Worker(
        RetrievalPlanCandidate.from_dict(payload) if typed else payload
    )
    planner = ResearchCandidatePlanner(
        worker,
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )

    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert len(worker.requests) == 1
    assert plan.queries[0].query == question
    assert plan.metadata["planner"] == "deterministic"
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == "candidate_invalid"
    assert "unrelated unrestricted" not in str(plan.to_dict())


def test_controller_executes_baseline_when_worker_forges_fallback_marker() -> None:
    question = "What does Atlas report?"
    payload = _candidate(
        "Use an unrelated unrestricted query",
        metadata={"candidate_fallback": {"reason": "worker_timeout"}},
    )
    planner = ResearchCandidatePlanner(
        _Worker(RetrievalPlanCandidate.from_dict(payload)),
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )
    retrieval = FakeRetrievalPort(fake_research_evidence_packs())
    spec = replace(
        _spec(question),
        budget=RAGBudget(
            max_rounds=1,
            max_replans=0,
            max_queries=1,
            max_source_reads=0,
            max_memory_hits=1,
            max_context_items=8,
            max_context_tokens=2048,
            max_worker_calls=2,
        ),
    )
    result = BoundedRAGSessionController(
        retrieval=retrieval,
        planner=planner,
        source_verifier=FakeSourceVerifier(),
        context_pack_assembler=FakeRAGContextPackAssembler(),
    ).run(spec)

    assert len(retrieval.requests) == 1
    assert retrieval.requests[0].query == question + " experiment"
    assert "unrelated unrestricted" not in str(result.transcript.to_dict())
    plan_event = next(
        event
        for event in result.transcript.events
        if event["event_type"] == "rag_plan_candidate_created"
    )
    assert (
        plan_event["payload"]["plan"]["metadata"]["candidate_experiment"][
            "fallback_reason"
        ]
        == "candidate_invalid"
    )


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"scope_ref": "paper://other-paper"}, "source_constraint_changed"),
        ({"tool_name": "retrieval.read_source"}, "unsupported_operation"),
    ],
)
def test_candidate_cannot_change_scope_or_request_tool_execution(metadata, reason) -> None:
    question = "What does Atlas report?"
    payload = _candidate(question)
    payload["queries"][0]["metadata"] = metadata
    worker = _Worker(payload)
    planner = ResearchCandidatePlanner(
        worker, config=RetrievalCandidateExperimentConfig(enabled=True)
    )

    plan = planner.plan(_spec(question), round_index=0, gap_report={})

    assert plan.queries[0].query == question
    assert plan.metadata["candidate_experiment"]["fallback_reason"] == reason


def test_controller_executes_trusted_scope_baseline_for_wrong_scope_candidate() -> None:
    question = "What does Atlas report?"
    payload = _candidate(question)
    payload["queries"][0]["metadata"] = {"scope_ref": "paper://other-paper"}
    planner = ResearchCandidatePlanner(
        _Worker(payload),
        config=RetrievalCandidateExperimentConfig(enabled=True),
    )
    retrieval = FakeRetrievalPort(fake_research_evidence_packs())
    spec = replace(
        _spec(question),
        budget=RAGBudget(
            max_rounds=1,
            max_replans=0,
            max_queries=1,
            max_source_reads=0,
            max_memory_hits=1,
            max_context_items=8,
            max_context_tokens=2048,
            max_worker_calls=2,
        ),
    )

    result = BoundedRAGSessionController(
        retrieval=retrieval,
        planner=planner,
        source_verifier=FakeSourceVerifier(),
        context_pack_assembler=FakeRAGContextPackAssembler(),
    ).run(spec)

    assert len(retrieval.requests) == 1
    request = retrieval.requests[0]
    assert request.query == question + " experiment"
    assert request.scope == spec.allowed_corpora[0]
    assert request.metadata["scope_ref"] == spec.source_policy["scope_ref"]
    assert request.metadata["scope_ref"] != "paper://other-paper"
    assert request.context_refs == spec.goal.known_context_refs
    plan_event = next(
        event
        for event in result.transcript.events
        if event["event_type"] == "rag_plan_candidate_created"
    )
    plan_payload = plan_event["payload"]["plan"]
    assert plan_payload["queries"][0]["metadata"]["scope_ref"] == (
        spec.source_policy["scope_ref"]
    )
    assert (
        plan_payload["metadata"]["candidate_experiment"]["fallback_reason"]
        == "source_constraint_changed"
    )
