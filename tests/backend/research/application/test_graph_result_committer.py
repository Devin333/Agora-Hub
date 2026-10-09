from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.research.application.graph_result_committer import (
    RESEARCH_NODE_RESULT_POLICIES,
    ResearchGraphResultCommitter,
    ResearchGraphResultRequestFactory,
)
from backend.research.graphs import (
    RESEARCH_PAPER_ANALYSIS_GRAPH_ID,
    build_dynamic_paper_analysis_graph_definition,
    build_paper_analysis_graph_definition,
    build_quality_dynamic_paper_analysis_graph_definition,
)
from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.event import HarnessEventType
from framework.harness.control_plane.graph_application import (
    HarnessGraphControlPlaneRuntime,
)
from framework.harness.control_plane.harness import (
    HarnessControlPlane,
    InMemoryHarnessEventPort,
)
from framework.harness.control_plane.node_output import (
    InMemoryHarnessNodeOutputResource,
)
from framework.harness.control_plane.state import HarnessRunSpec
from framework.harness.runtime import (
    ArtifactClass,
    ContextPolicy,
    GraphArtifactPersistenceConfig,
    GraphArtifactResultError,
    GraphArtifactResultErrorCode,
    GraphArtifactRolloutMode,
    HarnessGraphResultRuntime,
    RetentionClass,
)
from framework.harness.graph.compiler import HarnessGraphCompiler
from framework.harness.graph import (
    HarnessContractKind,
    HarnessContractReference,
    HarnessGraphDefinition,
    HarnessGraphLeafBinding,
    HarnessLeafActivityKind,
    HarnessWorkerType,
)
from framework.harness.graph.dsl import HarnessGraphSpec, StepRef
from framework.harness.graph.model import HarnessExecutableNode
from framework.harness.graph.activity import HarnessStepSpec
from framework.harness.graph.bindings import (
    HarnessActivityCapabilities,
    HarnessActivityContractBinding,
    HarnessLeafActivityBinding,
    HarnessRuntimeBindingAuthority,
    HarnessWorkerBinding,
)
from framework.harness.side_effects.fake import (
    CountingHarnessSideEffectHandler,
    InMemoryHarnessSideEffectStore,
)
from framework.harness.side_effects.models import (
    HarnessSideEffectDisposition,
    HarnessTerminalSideEffectPolicy,
)
from framework.harness.side_effects.registry import (
    HarnessSideEffectHandlerBinding,
    HarnessSideEffectRegistry,
)
from framework.harness.workers.result import HarnessWorkerResult
from framework.harness.runtime.activity_executor import (
    HarnessGraphPhysicalActivityExecutor,
)
from framework.harness.runtime.graph_dispatcher import (
    HarnessGraphPhysicalActivityDispatcher,
)
from framework.shared.attempts import AttemptSupervisor
from framework.shared.json import stable_json_dumps
from tests.framework.harness.runtime.test_graph_result_runtime import (
    FailResultProjectionPort,
)
from tests.framework.harness.runtime.test_materializer import (
    RecordingArtifactPort,
    RecordingAttempts,
    RecordingCache,
    RecordingCatalog,
    RecordingQuota,
    _materializer,
)


NOW = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)
TENANT_ID = "research-scope:sha256:" + "1" * 64
TENANT_SCOPE_REF = checksum_for(TENANT_ID)
SUBJECT_SCOPE_REF = checksum_for("paper-graph-result")


@pytest.mark.parametrize(
    ("node_id", "artifact_class", "retention_class", "context_policy"),
    (
        (
            "load_paper_source",
            ArtifactClass.EVIDENCE,
            RetentionClass.EVIDENCE,
            ContextPolicy.REF_LOAD_ALLOWED,
        ),
        (
            "compile_document",
            ArtifactClass.EVIDENCE,
            RetentionClass.EVIDENCE,
            ContextPolicy.REF_LOAD_ALLOWED,
        ),
        (
            "build_evidence_pack",
            ArtifactClass.EVIDENCE,
            RetentionClass.EVIDENCE,
            ContextPolicy.REF_LOAD_ALLOWED,
        ),
        (
            "verify_claims",
            ArtifactClass.EVIDENCE,
            RetentionClass.EVIDENCE,
            ContextPolicy.REF_LOAD_ALLOWED,
        ),
        (
            "quality_gate",
            ArtifactClass.EVIDENCE,
            RetentionClass.EVIDENCE,
            ContextPolicy.REF_LOAD_ALLOWED,
        ),
        (
            "run_research_rag",
            ArtifactClass.INTERMEDIATE,
            RetentionClass.RUN,
            ContextPolicy.SUMMARY_ONLY,
        ),
        (
            "analyze_structure",
            ArtifactClass.INTERMEDIATE,
            RetentionClass.RUN,
            ContextPolicy.SUMMARY_ONLY,
        ),
        (
            "analyze_contribution",
            ArtifactClass.INTERMEDIATE,
            RetentionClass.RUN,
            ContextPolicy.SUMMARY_ONLY,
        ),
        (
            "analyze_experiments",
            ArtifactClass.INTERMEDIATE,
            RetentionClass.RUN,
            ContextPolicy.SUMMARY_ONLY,
        ),
        (
            "dynamic_analysis_stage",
            ArtifactClass.INTERMEDIATE,
            RetentionClass.RUN,
            ContextPolicy.SUMMARY_ONLY,
        ),
        (
            "build_reader_payload",
            ArtifactClass.INTERMEDIATE,
            RetentionClass.RUN,
            ContextPolicy.SUMMARY_ONLY,
        ),
        (
            "build_paper_card",
            ArtifactClass.INTERMEDIATE,
            RetentionClass.RUN,
            ContextPolicy.SUMMARY_ONLY,
        ),
        (
            "publish_artifacts",
            ArtifactClass.REPORT,
            RetentionClass.REPORT,
            ContextPolicy.REF_LOAD_ALLOWED,
        ),
    ),
)
def test_research_node_policy_inventory_is_complete_and_trusted(
    node_id: str,
    artifact_class: ArtifactClass,
    retention_class: RetentionClass,
    context_policy: ContextPolicy,
) -> None:
    policy = RESEARCH_NODE_RESULT_POLICIES[node_id]

    assert policy.artifact_class is artifact_class
    assert policy.retention_class is retention_class
    assert policy.context_policy is context_policy
    assert policy.required_for_publication is (node_id == "publish_artifacts")


def test_research_node_policy_inventory_matches_compiled_graphs() -> None:
    graphs = tuple(
        HarnessGraphCompiler().compile(builder()).graph
        for builder in (
            build_paper_analysis_graph_definition,
            build_dynamic_paper_analysis_graph_definition,
            build_quality_dynamic_paper_analysis_graph_definition,
        )
    )
    request_factory = ResearchGraphResultRequestFactory()

    for graph in graphs:
        request_factory.validate_graph(graph)

    executable_node_ids = {
        node.node_id
        for graph in graphs
        for node in graph.nodes
        if isinstance(node, HarnessExecutableNode)
    }
    assert set(request_factory.policies) == executable_node_ids


def test_large_evidence_candidate_is_materialized_and_graph_state_is_bounded() -> None:
    raw_evidence = "evidence-body-" + "x" * 70_000
    worker_result = HarnessWorkerResult(
        "succeeded",
        output={"evidence_pack": {"content": raw_evidence}},
    )
    fixture = _run(
        node_id="build_evidence_pack",
        worker_result=worker_result,
    )

    envelope = fixture.envelope
    lineage = fixture.lineage
    stored = fixture.artifact.read_artifact(
        envelope.materialized_refs[0].ref
    )["payload"]["value"]
    state = fixture.port.recover_graph(fixture.run_id).state
    assert state is not None
    graph_json = stable_json_dumps(state.to_dict())

    assert envelope.persistence_decision.artifact_class is ArtifactClass.EVIDENCE
    assert envelope.persistence_decision.retention_class is RetentionClass.EVIDENCE
    assert envelope.persistence_decision.required is True
    assert (
        stored["worker_candidate"]["worker_result"]["output"]
        ["evidence_pack"]["content"]
        == raw_evidence
    )
    assert lineage.artifact_refs[0].artifact_class == "evidence"
    assert lineage.inline_projection == {
        "candidate_artifact_count": 0,
        "candidate_evidence_count": 0,
        "has_diagnostics": False,
        "has_error": False,
        "worker_candidate_ref": worker_result.candidate_result_ref,
        "worker_status": "succeeded",
    }
    assert raw_evidence not in graph_json
    assert len(graph_json) < 40_000
    assert fixture.catalog.requests[0].record == envelope.materialized_refs[0]

    much_larger_evidence = "evidence-body-" + "y" * 700_000
    much_larger = _run(
        node_id="build_evidence_pack",
        worker_result=HarnessWorkerResult(
            "succeeded",
            output={"evidence_pack": {"content": much_larger_evidence}},
        ),
    )
    much_larger_state = much_larger.port.recover_graph(much_larger.run_id).state
    assert much_larger_state is not None
    much_larger_graph_json = stable_json_dumps(much_larger_state.to_dict())

    assert much_larger_evidence not in much_larger_graph_json
    assert len(much_larger_graph_json) < 40_000
    assert abs(len(much_larger_graph_json) - len(graph_json)) < 256


def test_report_candidate_is_durable_without_publication_projection() -> None:
    fixture = _run(
        node_id="publish_artifacts",
        worker_result=HarnessWorkerResult(
            "succeeded",
            output={
                "artifact_bundle_ref": checksum_for("research-report-bundle"),
                "artifact_types": ["research-analysis", "research-paper-card"],
            },
        ),
    )

    envelope = fixture.envelope
    lineage = fixture.lineage

    assert envelope.persistence_decision.artifact_class is ArtifactClass.REPORT
    assert envelope.persistence_decision.retention_class is RetentionClass.REPORT
    assert envelope.persistence_decision.required is True
    assert envelope.materialized_refs[0].required_for_publication is True
    assert lineage.artifact_refs[0].required_for_publication is True
    assert "publication" not in lineage.inline_projection
    assert "public_refs" not in lineage.inline_projection


def test_failed_quality_gate_retains_diagnostics_without_publication_refs() -> None:
    failure = {
        "gate": "research_quality",
        "reason_code": "evidence_coverage_below_threshold",
    }
    fixture = _run(
        node_id="quality_gate",
        worker_result=HarnessWorkerResult(
            "failed",
            diagnostics={"gate_failures": [failure]},
            error="deterministic quality gate failed",
        ),
    )

    envelope = fixture.envelope
    lineage = fixture.lineage
    stored = fixture.artifact.read_artifact(
        envelope.materialized_refs[0].ref
    )["payload"]["value"]

    assert envelope.status.value == "failed"
    assert envelope.persistence_decision.artifact_class is ArtifactClass.EVIDENCE
    assert stored["worker_candidate"]["worker_result"]["diagnostics"] == {
        "gate_failures": [failure]
    }
    assert lineage.inline_projection["has_diagnostics"] is True
    assert lineage.inline_projection["has_error"] is True
    assert lineage.inline_projection["worker_status"] == "failed"
    assert all(not item.required_for_publication for item in lineage.artifact_refs)
    assert "public_refs" not in lineage.inline_projection
    committed = fixture.port.recover_graph(fixture.run_id).activity_result_commits[0]
    assert committed.result.status.value == "failed"


@pytest.mark.parametrize(
    "field_name",
    ("persistence", "persistence_mode", "retention", "retention_class"),
)
def test_worker_persistence_authority_is_rejected_before_materialization(
    field_name: str,
) -> None:
    fixture = _fixture(node_id="build_reader_payload")

    with pytest.raises(HarnessValidationError) as captured:
        HarnessWorkerResult(
            "succeeded",
            output={
                "reader_payload": {"title": "bounded"},
                field_name: "worker-choice",
            },
        )

    assert captured.value.code == "worker_decision_field_rejected"
    assert fixture.artifact.write_count == 0
    assert fixture.attempts.put_count == 0
    assert fixture.port.recover_graph(fixture.run_id).activity_result_commits == ()


def test_unknown_research_node_fails_closed_before_materialization() -> None:
    fixture = _fixture(node_id="unclassified_research_node")

    with pytest.raises(HarnessValidationError) as captured:
        _execute(fixture, HarnessWorkerResult("succeeded", output={"value": 1}))

    assert captured.value.code == "research_graph_result_policy_missing"
    assert fixture.artifact.write_count == 0
    assert fixture.attempts.put_count == 0
    assert fixture.port.recover_graph(fixture.run_id).activity_result_commits == ()


def test_scope_mismatch_fails_before_materialization() -> None:
    fixture = _fixture(
        node_id="run_research_rag",
        committer_tenant_id="research-scope:sha256:" + "2" * 64,
    )

    with pytest.raises(HarnessValidationError) as captured:
        _execute(fixture, HarnessWorkerResult("succeeded", output={"rag": {}}))

    assert captured.value.code == "graph_result_lineage_scope_mismatch"
    assert fixture.artifact.write_count == 0
    assert fixture.attempts.put_count == 0


def test_restart_reuses_recorded_worker_and_materialized_attempt() -> None:
    port = FailResultProjectionPort()
    fixture = _fixture(node_id="analyze_structure", port=port)
    worker_calls = 0

    def count_worker() -> None:
        nonlocal worker_calls
        worker_calls += 1
    worker_result = HarnessWorkerResult(
        "succeeded",
        output={"structure_candidate": {"summary": "durable"}},
    )
    authority = _test_runtime_authority(
        fixture.node_id,
        worker_result,
        fixture.side_effect_store,
    )
    authority.worker_bindings[0].implementation.on_execute = count_worker

    port.fail_result_projection = True
    with pytest.raises(RuntimeError, match="result projection unavailable"):
        _control_plane(fixture, authority).run(fixture.run_spec)

    assert worker_calls == 1
    assert fixture.artifact.write_count == 1
    assert fixture.attempts.put_count == 1
    interrupted = fixture.port.recover_graph(fixture.run_id)
    assert len(interrupted.activity_result_commits) == 1
    assert len(interrupted.pending_activity_results) == 1

    port.fail_result_projection = False
    recovered = _control_plane(fixture, authority).recover_and_run(
        fixture.run_spec
    )

    assert recovered.succeeded is True
    assert worker_calls == 1
    assert fixture.artifact.write_count == 1
    assert fixture.attempts.put_count == 1
    completed = fixture.port.recover_graph(fixture.run_id)
    assert len(completed.activity_result_commits) == 1
    assert completed.pending_activity_results == ()


class _Fixture:
    def __init__(
        self,
        *,
        node_id: str,
        run_spec: HarnessRunSpec,
        port,
        committer: ResearchGraphResultCommitter,
        artifact: RecordingArtifactPort,
        attempts: RecordingAttempts,
        catalog: RecordingCatalog,
        side_effect_store: InMemoryHarnessSideEffectStore,
        node_output_resource: InMemoryHarnessNodeOutputResource,
    ) -> None:
        self.node_id = node_id
        self.run_spec = run_spec
        self.run_id = run_spec.run_id
        self.port = port
        self.committer = committer
        self.artifact = artifact
        self.attempts = attempts
        self.catalog = catalog
        self.side_effect_store = side_effect_store
        self.node_output_resource = node_output_resource

    @property
    def envelope(self):
        assert len(self.attempts.envelopes) == 1
        return next(iter(self.attempts.envelopes.values()))

    @property
    def lineage(self):
        commits = self.port.recover_graph(self.run_id).activity_result_commits
        assert len(commits) == 1
        lineage = commits[0].result.result_lineage
        assert lineage is not None
        return lineage


def _run(
    *,
    node_id: str,
    worker_result: HarnessWorkerResult,
) -> _Fixture:
    fixture = _fixture(node_id=node_id)
    _execute(fixture, worker_result)
    return fixture


def _execute(
    fixture: _Fixture,
    worker_result: HarnessWorkerResult,
):
    return _control_plane(
        fixture,
        _test_runtime_authority(
            fixture.node_id,
            worker_result,
            fixture.side_effect_store,
        ),
    ).run(fixture.run_spec)


def _fixture(
    *,
    node_id: str,
    port=None,
    committer_tenant_id: str = TENANT_ID,
) -> _Fixture:
    run_id = f"run-research-result-{node_id.replace('_', '-')}"
    event_port = port or InMemoryHarnessEventPort()
    artifact = RecordingArtifactPort()
    attempts = RecordingAttempts()
    catalog = RecordingCatalog()
    side_effect_store = InMemoryHarnessSideEffectStore()
    node_output_resource = InMemoryHarnessNodeOutputResource()
    config = GraphArtifactPersistenceConfig(
        mode=GraphArtifactRolloutMode.ENFORCE
    )
    materializer = _materializer(
        artifact=artifact,
        attempts=attempts,
        cache=RecordingCache(),
        catalog=catalog,
        quota=RecordingQuota(),
        config=config,
    )
    committer = ResearchGraphResultCommitter(
        materializer=materializer,
        graph_result_runtime=HarnessGraphResultRuntime(
            HarnessGraphControlPlaneRuntime(event_port)
        ),
        config=config,
        tenant_id=committer_tenant_id,
        tenant_scope_ref=checksum_for(committer_tenant_id),
    )
    return _Fixture(
        node_id=node_id,
        run_spec=_run_spec(run_id, node_id),
        port=event_port,
        committer=committer,
        artifact=artifact,
        attempts=attempts,
        catalog=catalog,
        side_effect_store=side_effect_store,
        node_output_resource=node_output_resource,
    )


def _control_plane(
    fixture: _Fixture,
    authority: HarnessRuntimeBindingAuthority,
) -> HarnessControlPlane:
    control_plane = HarnessControlPlane(
        event_port=fixture.port,
        runtime_binding_authority=authority,
        side_effect_store=fixture.side_effect_store,
        graph_result_committer=fixture.committer,
    )
    executor = HarnessGraphPhysicalActivityExecutor(
        binding_authority=authority,
        input_resolver=control_plane,
        node_output_resource=fixture.node_output_resource,
        result_committer=None,
        supervisor=AttemptSupervisor(),
    )
    dispatcher = HarnessGraphPhysicalActivityDispatcher(
        executor=executor,
        graph_resolver=control_plane.graph_for_activity,
        input_resolver=control_plane,
        accept=control_plane.accept_graph_activity_for_execution,
        record_call_marker=control_plane.record_graph_activity_call_marker,
        record_result=control_plane.record_graph_activity_result_event,
        apply_result=control_plane.commit_physical_graph_result,
    )
    control_plane.install_graph_activity_dispatcher(dispatcher)
    return control_plane


def _run_spec(run_id: str, node_id: str) -> HarnessRunSpec:
    step = HarnessStepSpec(
        node_id,
        HarnessWorkerType.FUNCTION,
        output_key=node_id,
        metadata={"step_version": "1", "worker_version": "1"},
    )
    graph_spec = HarnessGraphSpec(
        RESEARCH_PAPER_ANALYSIS_GRAPH_ID,
        StepRef(node_id),
        terminal_output_keys=(node_id,),
    )
    graph = HarnessGraphDefinition(
        graph_id=graph_spec.graph_id,
        graph_version="1",
        root=graph_spec,
        activities=(step,),
        leaf_activity_bindings=(
            HarnessGraphLeafBinding(
                node_id,
                HarnessLeafActivityKind.FUNCTION,
                HarnessContractReference(
                    HarnessContractKind.WORKER,
                    f"research.result.{node_id}",
                    "1",
                ),
                HarnessContractReference(
                    HarnessContractKind.ACTIVITY,
                    f"research.result.{node_id}",
                    "1",
                ),
            ),
        ),
        task_plan_stage_bindings=(),
        committed_output_bindings=(),
        repair_bindings=(),
        terminal_side_effect_policy=HarnessTerminalSideEffectPolicy(
            policy_id="research.result.publication",
            version="1",
            handler="research.result.publication@1",
            kind="artifact_publication",
            requires_approval=False,
            retry_limit=1,
            not_required_evidence_ref=checksum_for("research-result-publication"),
        ),
    )
    return HarnessRunSpec(
        run_id=run_id,
        graph=graph,
        metadata={
            "research_runtime": "single_paper",
            "tenant_scope_ref": TENANT_SCOPE_REF,
            "identity_scope_ref": TENANT_SCOPE_REF,
            "subject_scope_ref": SUBJECT_SCOPE_REF,
        },
        created_at=NOW,
    )


class _ResultWorker:
    worker_version = "1"
    worker_type = HarnessWorkerType.FUNCTION

    def __init__(self, worker_id: str, result: HarnessWorkerResult, on_execute=None) -> None:
        self.worker_id = worker_id
        self.result = result
        self.on_execute = on_execute

    def execute(self, _task: dict) -> HarnessWorkerResult:
        if self.on_execute is not None:
            self.on_execute()
        return self.result


class _ResultActivity:
    capabilities = HarnessActivityCapabilities(stable_idempotency=True)

    def __init__(self, activity_id: str) -> None:
        self.activity_contract_id = activity_id
        self.activity_contract_version = "1"

    def dispatch(self, _request: dict) -> object:
        return None


def _test_runtime_authority(
    node_id: str,
    worker_result: HarnessWorkerResult,
    side_effect_store: InMemoryHarnessSideEffectStore,
) -> HarnessRuntimeBindingAuthority:
    contract_id = f"research.result.{node_id}"
    effect_handler = CountingHarnessSideEffectHandler(
        side_effect_store,
        disposition=HarnessSideEffectDisposition.ACCEPTED,
    )
    return HarnessRuntimeBindingAuthority(
        workers=(
            HarnessWorkerBinding(
                f"{contract_id}@1",
                HarnessWorkerType.FUNCTION,
                _ResultWorker(contract_id, worker_result),
            ),
        ),
        activities=(
            HarnessActivityContractBinding(
                f"{contract_id}@1",
                _ResultActivity(contract_id),
            ),
        ),
        leaf_activities=(
            HarnessLeafActivityBinding(
                HarnessLeafActivityKind.FUNCTION,
                f"{contract_id}@1",
                f"{contract_id}@1",
            ),
        ),
        side_effect_registry=HarnessSideEffectRegistry(
            (
                HarnessSideEffectHandlerBinding(
                    "research.result.publication@1",
                    "artifact_publication",
                    effect_handler,
                ),
            ),
        ),
    )
