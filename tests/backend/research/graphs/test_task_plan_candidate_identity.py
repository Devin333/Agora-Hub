from __future__ import annotations

from framework.harness.graph import HarnessGraphCompiler
from framework.harness.task_plan import PlanBuildRequest, TaskPlanStageBinding
from framework.shared.graph_identity import GraphExecutionIdentity

from backend.research.graphs import (
    RESEARCH_DYNAMIC_CAPABILITIES,
    RESEARCH_DYNAMIC_STAGE_ID,
    ResearchAnalysisPlanCandidateBuilder,
    build_dynamic_paper_analysis_graph_definition,
    build_research_analysis_task_plan_policy,
)


class _IdentityRecordingWorker:
    def __init__(self) -> None:
        self.identities: list[GraphExecutionIdentity | None] = []

    def generate_candidate(
        self,
        *,
        task: str,
        payload: dict[str, object],
        execution_identity: GraphExecutionIdentity | None = None,
        timeout_seconds: float | None = None,
        max_transport_attempts: int | None = None,
    ) -> dict[str, object]:
        assert task == "candidate_task_plan"
        assert timeout_seconds == 30.0
        assert max_transport_attempts == 1
        assert payload["stage"]
        self.identities.append(execution_identity)
        return {
            "tasks": [
                {
                    "task_id": f"analysis-task-{index}",
                    "objective": f"Analyze {capability}.",
                    "worker_capability": capability,
                    "input_refs": ["document", "evidence_pack"],
                    "depends_on": [],
                    "priority": 10 - index,
                }
                for index, capability in enumerate(RESEARCH_DYNAMIC_CAPABILITIES, 1)
            ],
            "requested_max_parallelism": 3,
        }


def test_candidate_builder_forwards_physical_graph_identity() -> None:
    graph = HarnessGraphCompiler().compile(
        build_dynamic_paper_analysis_graph_definition()
    ).graph
    binding = TaskPlanStageBinding(graph, RESEARCH_DYNAMIC_STAGE_ID)
    identity = GraphExecutionIdentity(
        run_id="research-run",
        graph_id=binding.graph_id,
        graph_version=binding.graph_version,
        graph_ref=binding.graph.graph_ref.exact_ref,
        graph_checksum=binding.graph_checksum,
        node_id=binding.node_id,
        node_instance_id="dynamic-analysis-stage:1",
        activity_id="activity-1",
        attempt=1,
    )
    request = PlanBuildRequest(
        run_id="research-run",
        stage_binding=binding,
        context_refs={"document": "document", "evidence_pack": "evidence_pack"},
        policy=build_research_analysis_task_plan_policy(),
        execution_identity=identity,
    )
    worker = _IdentityRecordingWorker()

    candidate = ResearchAnalysisPlanCandidateBuilder(worker).build_candidate(request)

    assert candidate.matches_stage_identity(request.stage_identity)
    assert worker.identities == [identity]
    assert request.to_dict()["execution_identity"] == identity.to_dict()
