from __future__ import annotations

from dataclasses import replace

import pytest

from backend.research.application import AnalyzePaperRequest, AnalyzePaperUseCase
from backend.research.application.claim_evidence_eligibility import (
    RESEARCH_CLAIM_ELIGIBILITY_GATE_REF,
)
from backend.research.application.research_input_contract import (
    ResearchInputContractError,
    ResearchInputContractService,
)
from backend.research.application.single_paper_runtime import ResearchSinglePaperRuntime
from backend.research.graphs import (
    RESEARCH_DYNAMIC_QUALITY_PAPER_ANALYSIS_GRAPH_ID,
    build_quality_dynamic_paper_analysis_graph_definition,
)
from backend.research.domain import ResearchTable
from backend.research.domain.claim_review import (
    ResearchProgressObservation,
    ResearchProgressScope,
)
from framework.events.canonical import checksum_for
from backend.research.domain.run_disposition import research_event_tenant_id
from framework.harness import (
    FakeArtifactPort,
    HarnessEventType,
    InMemoryHarnessEventPort,
)
from framework.harness.control_plane.graph_state import (
    RunLifecycle,
    project_public_graph_status,
)
from framework.harness.graph.decision import HarnessGraphDecisionType
from tests.backend.research.fakes import (
    FakeGithubRepositoryPort,
    FakeResearchDocumentCompiler,
    FakeResearchLLMWorker,
    FakeResearchRAGRuntime,
    FakeResearchSourceProvider,
    in_memory_node_output_resource_factory,
)
from tests.backend.research.integration.test_dynamic_paper_analysis_task_plan import (
    _DynamicTaskPlanFactory,
)


class _NumericComparisonCompiler(FakeResearchDocumentCompiler):
    def compile(self, source):
        document = super().compile(source)
        table = ResearchTable(
            table_id="table-results",
            caption="Accuracy benchmark results",
            source_ref="paper://paper-harness-001/table-results",
            columns=["Method", "Accuracy (%)", "Dataset", "Split"],
            rows=[
                {
                    "Method": "Harness",
                    "Accuracy (%)": 0.92,
                    "Dataset": "claim-verification-v1",
                    "Split": "test",
                },
                {
                    "Method": "baseline",
                    "Accuracy (%)": 0.83,
                    "Dataset": "claim-verification-v1",
                    "Split": "test",
                },
            ],
            page=7,
        )
        return document.model_copy(
            update={
                "source_snapshot_id": source.source_hash,
                "tables": [table],
            }
        )


class _CountingLLMWorker(FakeResearchLLMWorker):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.requests = []

    def generate_candidate(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        return super().generate_candidate(**kwargs)


class _CountingSourceProvider(FakeResearchSourceProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def fetch_paper(self, source_ref: str):
        self.calls += 1
        return super().fetch_paper(source_ref)

    def fetch_source_record(self, paper_id: str):
        self.calls += 1
        return super().fetch_source_record(paper_id)


def _options(**changes):
    return {
        "dynamic_analysis": True,
        "research_input_quality_enabled": True,
        "research_input_source_revision": "sha256-harness-paper-source",
        "research_input_objective": "Explain the paper's method and reported evidence.",
        "research_input_constraints": [
            "Use only evidence from the accepted paper source",
            "Do not present model candidates as verified conclusions",
        ],
        "research_eligibility_gate_version": RESEARCH_CLAIM_ELIGIBILITY_GATE_REF,
        **changes,
    }


def _runtime(
    tmp_path,
    *,
    source_provider=None,
    llm_worker=None,
    artifact_port=None,
    compiler=None,
    event_port=None,
):
    factory = _DynamicTaskPlanFactory(transcript_root=tmp_path / "transcripts")
    event_port = event_port or InMemoryHarnessEventPort()
    runtime = ResearchSinglePaperRuntime(
        source_provider=source_provider or FakeResearchSourceProvider(),
        document_compiler=compiler or FakeResearchDocumentCompiler(),
        llm_worker=llm_worker or FakeResearchLLMWorker(),
        github_repository=FakeGithubRepositoryPort(),
        rag_runtime=FakeResearchRAGRuntime(),
        artifact_port=artifact_port or FakeArtifactPort(),
        event_port_factory=lambda _run_id: event_port,
        dynamic_task_plan_runner_factory=factory,
        node_output_resource_factory=in_memory_node_output_resource_factory,
    )
    runtime._test_dynamic_factory = factory
    return runtime


def test_opted_in_dynamic_graph_publishes_only_the_qualified_v1_claim(tmp_path) -> None:
    event_port = InMemoryHarnessEventPort()
    llm_worker = _CountingLLMWorker()
    result = AnalyzePaperUseCase(
        _runtime(
            tmp_path,
            compiler=_NumericComparisonCompiler(),
            event_port=event_port,
            llm_worker=llm_worker,
        )
    ).analyze(
        AnalyzePaperRequest(
            run_id="research-input-quality-success",
            paper_id="paper-harness-001",
            source_ref="https://arxiv.org/abs/2606.00123",
            user_id="user-1",
            options=_options(max_replans=1),
        )
    )

    assert result.succeeded is True
    assert result.graph_id == RESEARCH_DYNAMIC_QUALITY_PAPER_ANALYSIS_GRAPH_ID
    assert result.analysis is not None
    assert result.reader_payload is not None
    assert result.paper_card is not None
    assert result.trace.metadata["research_eligibility_gate_version"] == RESEARCH_CLAIM_ELIGIBILITY_GATE_REF
    assert result.trace.metadata["research_input_profile_id"] == "research.single-paper.input"
    actor = {"user_id": "user-1", "memory_namespace": result.trace.metadata["memory_namespace"]}
    assert result.trace.metadata["research_input_tenant_id"] == research_event_tenant_id(actor)
    assert result.trace.metadata["research_input_profile_revision"] == "1"
    assert result.trace.metadata["research_input_profile_checksum"].startswith("sha256:")
    analysis_requests = [request for request in llm_worker.requests if request["task"] != "candidate_task_plan"]
    assert analysis_requests
    for request in analysis_requests:
        envelope = request["payload"]["prepared_input"]
        assert envelope["payload"]["actor_scope"]["user_id"] == "user-1"
        assert envelope["payload"]["profile_checksum"] == result.trace.metadata["research_input_profile_checksum"]
        assert envelope["payload"]["objective"] == _options()["research_input_objective"]
        assert envelope["payload"]["constraints"] == _options()["research_input_constraints"]
        assert envelope["payload"]["source_revision"] == _options()["research_input_source_revision"]

    qualified = [
        claim
        for claim in result.analysis.claims
        if claim.metadata.get("conclusion_status") == "verified_conclusion"
    ]
    assert len(qualified) == 1
    claim = qualified[0]
    assert claim.metadata["claim_revision"] == 1
    assert claim.metadata["eligibility_gate_ref"] == RESEARCH_CLAIM_ELIGIBILITY_GATE_REF
    assert claim.metadata["qualification_ref"].startswith("claim-qualification:sha256:")
    assert claim.metadata["source_refs"] == [
        "paper://paper-harness-001/table-results"
    ]
    assert claim.evidence_ids
    assert claim.text in result.analysis.experiments
    assert "Harness" in claim.text
    assert "baseline" in claim.text

    gate_event = next(
        event
        for event in result.trace.events
        if event.event_type is HarnessEventType.GATE_EVALUATED
        and event.node_id == "verify_claims"
        and event.payload.get("gate") == "ResearchClaimEvidenceEligibilityGate"
    )
    assert gate_event.payload["passed"] is True
    assert gate_event.payload["details"]["harness_gate"]["reference"] == RESEARCH_CLAIM_ELIGIBILITY_GATE_REF
    assert gate_event.payload["details"]["qualification_ref"] == claim.metadata["qualification_ref"]

    recovery = event_port.recover_graph(result.run_id)
    assert recovery.state is not None
    assert recovery.state.graph_ref.graph_id == RESEARCH_DYNAMIC_QUALITY_PAPER_ANALYSIS_GRAPH_ID
    verify_phases = tuple(
        event.payload
        for event in result.trace.events
        if event.event_type is HarnessEventType.GRAPH_PHASE_TRANSITION_RECORDED
        and event.node_id == "verify_claims"
    )
    assert {(item["phase"], item["boundary"]) for item in verify_phases} == {
        ("plan", "entry"),
        ("plan", "exit"),
        ("execute", "entry"),
        ("execute", "exit"),
        ("verify", "entry"),
        ("verify", "exit"),
    }
    assert all(item["record_checksum"].startswith("sha256:") for item in verify_phases)
    assert tuple(event.payload for event in result.trace.events if event.event_type is HarnessEventType.GRAPH_PHASE_TRANSITION_RECORDED)


def test_quality_graph_binds_the_exact_claim_eligibility_gate() -> None:
    definition = build_quality_dynamic_paper_analysis_graph_definition()
    verify_claims = next(
        activity
        for activity in definition.activities
        if activity.step_id == "verify_claims"
    )

    assert definition.graph_id == RESEARCH_DYNAMIC_QUALITY_PAPER_ANALYSIS_GRAPH_ID
    assert verify_claims.quality_gate == RESEARCH_CLAIM_ELIGIBILITY_GATE_REF
    assert definition.root.metadata["claim_eligibility_gate"] == RESEARCH_CLAIM_ELIGIBILITY_GATE_REF

def test_missing_v1_comparison_halts_without_publication_and_replays_phases(tmp_path) -> None:
    artifact_port = FakeArtifactPort()
    event_port = InMemoryHarnessEventPort()
    runtime = _runtime(
        tmp_path,
        artifact_port=artifact_port,
        event_port=event_port,
    )
    run_options = _options(max_replans=1)
    result = AnalyzePaperUseCase(runtime).analyze(
        AnalyzePaperRequest(
            run_id="research-input-quality-insufficient",
            paper_id="paper-harness-001",
            source_ref="https://arxiv.org/abs/2606.00123",
            user_id="user-1",
            options=run_options,
        )
    )

    assert result.succeeded is False
    if result.status != "halted":
        failed_workers = {
            key: value
            for key, value in result.diagnostics.get("worker_results", {}).items()
            if value.get("status") != "succeeded"
        }
        factory = runtime._test_dynamic_factory
        store = factory.stores[0]
        plan = store.plan(result.run_id, "dynamic_analysis_stage")
        raise AssertionError({
            "status": result.status,
            "terminal_reason": result.diagnostics.get("terminal_reason"),
            "failed_workers": failed_workers,
            "child_execution_errors": factory.execution_errors,
            "task_plan_events": [
                {
                    "event_type": event.event_type,
                    "task_id": event.task_id,
                    "task_instance_id": event.task_instance_id,
                    "reason_code": event.reason_code,
                    "payload": dict(event.payload),
                }
                for event in store.read_events(
                    result.run_id, "dynamic_analysis_stage"
                )
                if (
                    "FAIL" in event.event_type
                    or "REJECT" in event.event_type
                    or "INDETERMINATE" in event.event_type
                    or event.reason_code
                )
            ],
            "task_results": (
                []
                if plan is None
                else [
                    record.to_dict()
                    for record in store.results_for(
                        result.run_id,
                        "dynamic_analysis_stage",
                        plan.plan_id,
                        plan.version,
                    )
                ]
            ),
        })
    assert result.analysis is None
    assert artifact_port.storage == {}
    assert runtime._test_dynamic_factory.execution_errors == []
    transcript_store = runtime._test_dynamic_factory.transcript_stores[0]
    child_refs = transcript_store.refs_for_parent(result.run_id)
    assert len(child_refs) == 3
    for child_ref in child_refs:
        descriptor = transcript_store.describe_ref(child_ref)
        assert transcript_store.verify(descriptor.receipt) == descriptor.receipt
    events = tuple(result.trace.events)
    failure = next(
        event
        for event in events
        if event.event_type is HarnessEventType.GATE_EVALUATED
        and event.node_id == "verify_claims"
        and event.payload.get("gate") == "ResearchClaimEvidenceEligibilityGate"
    )
    assert failure.payload["passed"] is False
    assert failure.payload["details"]["reason_code"] == "claim_eligibility_insufficient_evidence"
    assert failure.payload["details"]["harness_gate"]["reference"] == RESEARCH_CLAIM_ELIGIBILITY_GATE_REF
    progress = ResearchProgressObservation.from_dict(
        failure.payload["details"]["progress_observations"][0]
    )
    assert progress.run_id == result.run_id
    assert progress.paper_id == "paper-harness-001"
    assert progress.observation_scope is ResearchProgressScope.RUN
    assert progress.claim_id is None
    assert progress.resolver_status is None
    assert progress.observation_only is True
    assert progress.metrics == {
        "eligible_claims": 0,
        "required_claims": 1,
        "verified_progress_ratio": 0.0,
    }
    assert isinstance(progress.metrics["eligible_claims"], int)
    assert isinstance(progress.metrics["verified_progress_ratio"], float)
    accepted_outputs = {
        item["step_id"]: item["output"]
        for item in result.diagnostics["worker_results"].values()
        if item["step_id"] in {
            "load_paper_source", "compile_document", "build_evidence_pack"
        }
    }
    assert dict(progress.evidence_context_refs) == {
        "paper_source": checksum_for(accepted_outputs["load_paper_source"]),
        "document": checksum_for(accepted_outputs["compile_document"]),
        "evidence_pack": checksum_for(accepted_outputs["build_evidence_pack"]),
    }
    assert tuple(failure.payload["details"]["progress_observation_refs"]) == (
        progress.checksum,
    )
    assert not any(
        event.event_type is HarnessEventType.GRAPH_WORKER_CALLED
        and event.node_id == "publish_artifacts"
        for event in events
    )

    recovery = event_port.recover_graph(result.run_id)
    assert recovery.state is not None
    # HALT is a lifecycle safety stop, not a RunOutcome enum member. Verify
    # the committed lifecycle and decision rather than treating NONE as HALT.
    assert recovery.state.lifecycle is RunLifecycle.HALTED
    assert project_public_graph_status(
        recovery.state.lifecycle, recovery.state.outcome,
    ).value == result.status == "halted"
    halt_decisions = tuple(
        commit.decision
        for commit in recovery.decision_commits
        if commit.decision.decision_type is HarnessGraphDecisionType.HALT_RUN
    )
    assert halt_decisions
    assert recovery.state.terminal_reason_code == halt_decisions[-1].reason_code
    assert recovery.projection_commits[-1].state.lifecycle is RunLifecycle.HALTED
    assert recovery.projection_commits[-1].state.projection_checksum == recovery.state.projection_checksum
    verify_phases = tuple(
        event.payload
        for event in events
        if event.event_type is HarnessEventType.GRAPH_PHASE_TRANSITION_RECORDED
        and event.node_id == "verify_claims"
    )
    assert verify_phases
    assert recovery.graph is not None
    verify_definition = next(node for node in recovery.graph.nodes if getattr(node, "step_id", None) == "verify_claims")
    retry_policy = verify_definition.metadata["retry_policy"]
    replan_budget = recovery.state.budgets.require("replans")
    verify_commits = tuple(
        commit for commit in recovery.decision_commits
        if commit.decision.node_id == verify_definition.node_id
    )
    dispatches = tuple(commit for commit in verify_commits if commit.decision.decision_type is HarnessGraphDecisionType.DISPATCH_ACTIVITY)
    replans = tuple(commit for commit in verify_commits if commit.decision.decision_type is HarnessGraphDecisionType.REPLAN_NODE)
    retries = tuple(commit for commit in verify_commits if commit.decision.decision_type is HarnessGraphDecisionType.RETRY_NODE)
    plans = tuple(commit for commit in verify_commits if commit.decision.decision_type is HarnessGraphDecisionType.ENTER_STEP_PHASE)
    verifications = tuple(commit for commit in verify_commits if commit.decision.decision_type is HarnessGraphDecisionType.VERIFY_ACTIVITY_RESULT)

    # Worker retry and failed-gate replan have separate budgets. This frozen
    # node has no worker retries; one run replan permits one new execution.
    assert retry_policy["effective_max_attempts"] == retry_policy["max_retries"] + 1
    assert retry_policy["max_retries"] == 0
    assert retries == ()
    assert recovery.state.budgets.require("retries").used == 0
    assert len(replans) == replan_budget.used == replan_budget.limit == run_options["max_replans"]
    max_executions = retry_policy["effective_max_attempts"] * (1 + replan_budget.limit)
    assert len(dispatches) == 1 + len(replans) == max_executions
    assert tuple(commit.decision.attempt for commit in dispatches) == tuple(range(1, max_executions + 1))
    assert len(plans) == len(verifications) == len(dispatches)
    for index, replan in enumerate(replans):
        assert replan.decision.reason_code == "verification_failed_replan"
        assert replan.decision.attempt == dispatches[index].decision.attempt
        assert verifications[index].sequence < replan.sequence < plans[index + 1].sequence < dispatches[index + 1].sequence
    assert halt_decisions[-1].reason_code == "verification_failed_replans_exhausted"
    assert halt_decisions[-1].payload["budget_exhausted"] == "replans"
    assert halt_decisions[-1].attempt == dispatches[-1].decision.attempt
    verify_state = next(node for node in recovery.state.node_instances if node.identity.node_id == verify_definition.node_id)
    assert verify_state.attempt == len(dispatches)
    assert verify_state.replans == len(replans)
    for budget in recovery.state.budgets.counters:
        assert budget.used + budget.reserved <= budget.limit

    # PLAN records the previous dispatch count; EXECUTE increments attempt.
    # Derive every entry/exit from committed decisions to check both rounds
    # without assuming PLAN and EXECUTE share an attempt number.
    phase_by_decision = {
        HarnessGraphDecisionType.ENTER_STEP_PHASE: "plan",
        HarnessGraphDecisionType.DISPATCH_ACTIVITY: "execute",
        HarnessGraphDecisionType.VERIFY_ACTIVITY_RESULT: "verify",
    }
    expected_phases = tuple(
        (phase_by_decision[commit.decision.decision_type], boundary, commit.decision.attempt)
        for commit in verify_commits
        if commit.decision.decision_type in phase_by_decision
        for boundary in ("entry", "exit")
    )
    assert tuple((item["phase"], item["boundary"], item["attempt"]) for item in verify_phases) == expected_phases
    eligibility_failures = tuple(
        event for event in events
        if event.event_type is HarnessEventType.GATE_EVALUATED
        and event.node_id == "verify_claims"
        and event.payload.get("gate") == "ResearchClaimEvidenceEligibilityGate"
    )
    assert len(eligibility_failures) == len(dispatches)
    assert all(event.payload["passed"] is False for event in eligibility_failures)
    for event in eligibility_failures:
        replayed = ResearchProgressObservation.from_dict(
            event.payload["details"]["progress_observations"][0]
        )
        assert replayed == progress
        assert tuple(event.payload["details"]["progress_observation_refs"]) == (
            progress.checksum,
        )
    history = tuple(event_port.read_history(result.run_id))
    assert tuple(
        event.payload for event in history
        if event.event_type is HarnessEventType.GATE_EVALUATED
        and event.node_id == "verify_claims"
        and event.payload.get("gate") == "ResearchClaimEvidenceEligibilityGate"
    ) == tuple(event.payload for event in eligibility_failures)
    assert tuple(event.payload for event in history if event.event_type is HarnessEventType.GRAPH_PHASE_TRANSITION_RECORDED and event.node_id == "verify_claims") == verify_phases


def test_unknown_or_wrong_gate_rejects_before_source_worker_and_publication(tmp_path) -> None:
    source = _CountingSourceProvider()
    llm = _CountingLLMWorker()
    artifact_port = FakeArtifactPort()
    runtime = _runtime(
        tmp_path,
        source_provider=source,
        llm_worker=llm,
        artifact_port=artifact_port,
    )

    with pytest.raises(ResearchInputContractError, match="must match"):
        runtime.run(
            AnalyzePaperRequest(
                run_id="research-input-quality-wrong-gate",
                paper_id="paper-harness-001",
                source_ref="https://arxiv.org/abs/2606.00123",
                user_id="user-1",
                options=_options(
                    research_eligibility_gate_version="SummarySchemaGate@1"
                ),
            )
        )

    assert source.calls == 0
    assert llm.calls == 0
    assert artifact_port.storage == {}


def test_invalid_profile_rejects_before_source_worker_and_publication(tmp_path) -> None:
    source = _CountingSourceProvider()
    llm = _CountingLLMWorker()
    artifact_port = FakeArtifactPort()
    runtime = _runtime(
        tmp_path,
        source_provider=source,
        llm_worker=llm,
        artifact_port=artifact_port,
    )
    invalid = _options(
        research_input_profile={
            "profile_id": "research.dynamic.v1",
            "profile_revision": "1",
            "tenant_id": "default",
            "paper_id": "paper-harness-001",
            "task_id": "task-1",
            "task_revision": "1",
            "objective": "one",
            "constraints": [],
            "items": [],
            "template_ref": "prompt://research",
            "template_revision": "1",
            "output_schema_ref": "schema://research/v1",
            "budget_input_tokens": 100,
            "budget_output_tokens": 100,
        }
    )

    with pytest.raises(ResearchInputContractError, match="invalid"):
        runtime.run(
            AnalyzePaperRequest(
                run_id="research-input-quality-invalid-profile",
                paper_id="paper-harness-001",
                source_ref="https://arxiv.org/abs/2606.00123",
                user_id="user-1",
                options=invalid,
            )
        )

    assert source.calls == 0
    assert llm.calls == 0
    assert artifact_port.storage == {}




@pytest.mark.parametrize("changes", [
    {"research_input_profile": []},
    {"research_input_profile": "forged"},
    {"research_input_profile": True},
    {"research_input_constraints": {"use source": True}},
    {"research_input_constraints": "use source"},
    {"research_input_constraints": [1]},
    {"research_input_objective": 7},
    {"research_input_source_revision": None},
])
def test_invalid_controlled_input_options_reject_before_any_dispatch(tmp_path, changes) -> None:
    source = _CountingSourceProvider()
    llm = _CountingLLMWorker()
    artifact_port = FakeArtifactPort()
    runtime = _runtime(tmp_path, source_provider=source, llm_worker=llm, artifact_port=artifact_port)
    with pytest.raises(ResearchInputContractError):
        runtime.run(AnalyzePaperRequest(
            run_id="invalid-research-input-options", paper_id="paper-harness-001",
            source_ref="https://arxiv.org/abs/2606.00123", user_id="user-1",
            options=_options(**changes),
        ))
    assert source.calls == 0
    assert llm.calls == 0
    assert artifact_port.storage == {}


@pytest.mark.parametrize("scope", [
    {"user_id": "user-2"},
    {"tenant_id": "different-tenant"},
    {"memory_namespace": "research:user:other-user"},
    {"paper_id": "different-paper"},
])
def test_profile_cannot_cross_actor_scope_before_source_or_worker_dispatch(tmp_path, scope) -> None:
    source = _CountingSourceProvider()
    llm = _CountingLLMWorker()
    artifact_port = FakeArtifactPort()
    runtime = _runtime(tmp_path, source_provider=source, llm_worker=llm, artifact_port=artifact_port)
    actor = runtime.ask_use_case.resolve_actor_scope(user_id="user-1", tenant_id=None)
    options = _options()
    profile = ResearchInputContractService.build_profile(
        tenant_id=research_event_tenant_id(actor.to_metadata()),
        paper_id="paper-harness-001", source_ref="https://arxiv.org/abs/2606.00123",
        source_revision=options["research_input_source_revision"],
        objective=options["research_input_objective"],
        constraints=tuple(options["research_input_constraints"]),
    )
    profile = replace(profile, items=(profile.items[0], replace(profile.items[1], scope=scope)))
    with pytest.raises(ResearchInputContractError, match="scope mismatch"):
        runtime.run(AnalyzePaperRequest(
            run_id="cross-user-research-input", paper_id="paper-harness-001",
            source_ref="https://arxiv.org/abs/2606.00123", user_id="user-1",
            options=_options(research_input_profile=profile.to_dict()),
        ))
    assert source.calls == 0
    assert llm.calls == 0
    assert artifact_port.storage == {}


def test_actual_source_revision_drift_stops_before_analysis_or_publication(tmp_path) -> None:
    source = _CountingSourceProvider()
    llm = _CountingLLMWorker()
    artifact_port = FakeArtifactPort()
    result = _runtime(tmp_path, source_provider=source, llm_worker=llm, artifact_port=artifact_port).run(
        AnalyzePaperRequest(
            run_id="research-input-source-drift", paper_id="paper-harness-001",
            source_ref="https://arxiv.org/abs/2606.00123", user_id="user-1",
            options=_options(research_input_source_revision="obsolete-source-revision"),
        )
    )
    assert result.succeeded is False
    assert source.calls == 2
    assert llm.calls == 0
    assert artifact_port.storage == {}
