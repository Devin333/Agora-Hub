from __future__ import annotations

import json

import pytest

from backend.research.ports.llm_worker import ResearchCandidateWorkerPort
from backend.research.rag.adapters.llm_plan_worker import LLMResearchRAGPlanCandidateWorker
from backend.research.rag.adapters.plan_worker import ResearchRAGPlanWorker
from framework.harness.rag.fake import fake_rag_session_spec
from framework.harness.rag.planner import WorkerRAGPlanner
from framework.harness.workers.result import HarnessWorkerStatus


def test_llm_research_rag_plan_worker_returns_candidate_payload() -> None:
    prompts: list[str] = []

    async def llm(prompt: str) -> str:
        prompts.append(prompt)
        return """
```json
{
  "candidate": {
    "candidate_id": "llm-plan-1",
    "queries": [
      {
        "step_id": "llm-plan-1:query",
        "operation": "search_corpus",
        "query": "experiment result table",
        "corpus": "research-papers",
        "max_results": 3,
        "metadata": {"evidence_type": "experiment"}
      }
    ],
    "expected_evidence": ["experiment"],
    "expected_gaps": [],
    "confidence": 0.72,
    "metadata": {"planner": "llm"}
  }
}
```
"""

    worker = LLMResearchRAGPlanCandidateWorker(llm)

    output = worker.generate_candidate(
        task="rag_plan_candidate",
        payload={
            "round_index": 1,
            "gap_report": {"missing_evidence_types": ["experiment"]},
            "executed_queries": ["method query"],
        },
    )

    assert isinstance(worker, ResearchCandidateWorkerPort)
    assert output["candidate"]["candidate_id"] == "llm-plan-1"
    assert output["candidate"]["queries"][0]["metadata"]["evidence_type"] == "experiment"
    assert "Do not include workflow routing" in prompts[0]
    assert "method query" in prompts[0]


def test_llm_research_rag_plan_worker_invalid_json_fails_through_adapter() -> None:
    worker = ResearchRAGPlanWorker(
        LLMResearchRAGPlanCandidateWorker(lambda prompt: "not json")
    )

    result = worker.generate({"round_index": 1})

    assert result.status == HarnessWorkerStatus.FAILED
    assert result.error is None
    assert result.diagnostics == {"failure_code": "worker_failed"}


def test_llm_research_rag_plan_worker_requires_candidate_object() -> None:
    worker = ResearchRAGPlanWorker(
        LLMResearchRAGPlanCandidateWorker(lambda prompt: '{"candidate": []}')
    )

    result = worker.generate({"round_index": 1})

    assert result.status == HarnessWorkerStatus.FAILED
    assert result.error is None
    assert result.diagnostics == {"failure_code": "worker_failed"}


def test_llm_research_rag_plan_worker_forwards_execution_identity_to_llm() -> None:
    identities = []

    def llm(prompt: str, *, execution_identity=None) -> str:
        identities.append(execution_identity)
        return '{"candidate":{"candidate_id":"plan-1","queries":[]}}'

    identity = (
        fake_rag_session_spec()
        .graph_identity.with_physical_activity(
            node_id="build_evidence_context",
            node_instance_id="build-evidence-context:1",
            activity_id="activity-1",
            activity_attempt=1,
        )
        .to_graph_execution_identity()
    )
    worker = LLMResearchRAGPlanCandidateWorker(llm)

    output = worker.generate_candidate(
        task="rag_plan_candidate",
        payload={"round_index": 1},
        execution_identity=identity,
    )

    assert output["candidate"]["candidate_id"] == "plan-1"
    assert identities == [identity]


def test_llm_research_rag_plan_worker_forwards_transport_limits_to_llm() -> None:
    received = []

    def llm(prompt: str, *, timeout_seconds=None, max_transport_attempts=None) -> str:
        received.append((timeout_seconds, max_transport_attempts))
        return '{"candidate":{"candidate_id":"plan-1","queries":[]}}'

    output = LLMResearchRAGPlanCandidateWorker(llm).generate_candidate(
        task="rag_plan_candidate",
        payload={"round_index": 1},
        timeout_seconds=2.5,
        max_transport_attempts=1,
    )

    assert output["candidate"]["candidate_id"] == "plan-1"
    assert received == [(2.5, 1)]


@pytest.mark.parametrize(
    "response,forbidden_path",
    [
        (
            {"candidate": {"candidate_id": "plan-1", "queries": []}, "publish": True},
            "publish",
        ),
        (
            {
                "candidate": {
                    "candidate_id": "plan-1",
                    "queries": [],
                    "metadata": {"quality_passed": True},
                }
            },
            "candidate.metadata.quality_passed",
        ),
        (
            {
                "candidate": {
                    "candidate_id": "plan-1",
                    "queries": [{"metadata": {"next_step": "publish"}}],
                }
            },
            "candidate.queries[0].metadata.next_step",
        ),
    ],
)
def test_raw_llm_control_fields_fail_through_adapter_instead_of_being_dropped(
    response,
    forbidden_path,
) -> None:
    candidate_worker = LLMResearchRAGPlanCandidateWorker(
        lambda _prompt: json.dumps(response)
    )
    planner_worker = ResearchRAGPlanWorker(candidate_worker)
    result = planner_worker.generate({"round_index": 1})

    assert result.status == HarnessWorkerStatus.FAILED
    assert result.diagnostics == {"failure_code": "worker_failed"}

    planner = WorkerRAGPlanner(planner_worker, max_worker_calls=1)
    candidate = planner.plan(
        fake_rag_session_spec(),
        round_index=1,
        gap_report={},
    )

    assert candidate.metadata["candidate_fallback"]["reason"] == "worker_failed"
    assert planner.worker_calls == 1

    with pytest.raises(
        ValueError,
        match=forbidden_path.replace("[", r"\[").replace("]", r"\]"),
    ):
        candidate_worker.generate_candidate(
            task="rag_plan_candidate",
            payload={"round_index": 1},
        )
