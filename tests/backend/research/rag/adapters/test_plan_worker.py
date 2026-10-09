from __future__ import annotations

import math

import pytest

from backend.research.rag.adapters.plan_worker import ResearchRAGPlanWorker
from framework.harness.rag.fake import fake_rag_session_spec
from framework.harness.workers.result import HarnessWorkerStatus


def test_research_rag_plan_worker_calls_candidate_worker_with_planner_task() -> None:
    worker = _CandidateWorker({"candidate": {"candidate_id": "plan-1", "queries": []}})
    adapter = ResearchRAGPlanWorker(worker)

    result = adapter.generate({"round_index": 1})

    assert result.status == HarnessWorkerStatus.SUCCEEDED
    assert result.output["candidate"]["candidate_id"] == "plan-1"
    assert worker.calls == [("rag_plan_candidate", {"round_index": 1})]


def test_research_rag_plan_worker_failed_result_on_exception() -> None:
    adapter = ResearchRAGPlanWorker(_FailingCandidateWorker())

    result = adapter.generate({"round_index": 1})

    assert result.status == HarnessWorkerStatus.FAILED
    assert result.error is None
    assert result.diagnostics == {"failure_code": "worker_failed"}


def test_research_rag_plan_worker_failed_result_on_non_dict_payload() -> None:
    adapter = ResearchRAGPlanWorker(_CandidateWorker(["not", "a", "dict"]))

    result = adapter.generate({"round_index": 1})

    assert result.status == HarnessWorkerStatus.FAILED
    assert "non-dict" in str(result.error)


def test_research_rag_plan_worker_forwards_transport_limits() -> None:
    worker = _CandidateWorker({"candidate": {"candidate_id": "plan-1", "queries": []}})
    adapter = ResearchRAGPlanWorker(worker)

    result = adapter.generate({
        "round_index": 1,
        "timeout_seconds": 3.5,
        "max_transport_attempts": 1,
    })

    assert result.status == HarnessWorkerStatus.SUCCEEDED
    assert worker.transport_limits == [(3.5, 1)]


def test_research_rag_plan_worker_forwards_physical_execution_identity() -> None:
    worker = _CandidateWorker({"candidate": {"candidate_id": "plan-1", "queries": []}})
    adapter = ResearchRAGPlanWorker(worker)
    identity = (
        fake_rag_session_spec()
        .graph_identity.with_physical_activity(
            node_id="build_evidence_context",
            node_instance_id="build-evidence-context:1",
            activity_id="activity-1",
            activity_attempt=2,
        )
        .to_graph_execution_identity()
    )

    result = adapter.generate({"round_index": 1}, execution_identity=identity)

    assert result.status == HarnessWorkerStatus.SUCCEEDED
    assert worker.identities == [identity]


class _CandidateWorker:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = []
        self.identities = []
        self.transport_limits = []

    def generate_candidate(
        self, *, task: str, payload: dict, execution_identity=None,
        timeout_seconds=None, max_transport_attempts=None,
    ):
        self.calls.append((task, payload))
        self.identities.append(execution_identity)
        self.transport_limits.append((timeout_seconds, max_transport_attempts))
        return self.payload


class _FailingCandidateWorker:
    def generate_candidate(self, *, task: str, payload: dict):
        raise RuntimeError("boom")


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", True),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1.0),
        ("timeout_seconds", math.inf),
        ("timeout_seconds", math.nan),
        ("timeout_seconds", "3.5"),
        ("max_transport_attempts", True),
        ("max_transport_attempts", 0),
        ("max_transport_attempts", -1),
        ("max_transport_attempts", 1.5),
        ("max_transport_attempts", "1"),
    ],
)
def test_explicit_invalid_transport_budget_fails_before_worker_dispatch(field, value) -> None:
    worker = _CandidateWorker({"candidate": {"candidate_id": "plan-1", "queries": []}})
    result = ResearchRAGPlanWorker(worker).generate({"round_index": 1, field: value})
    assert result.status == HarnessWorkerStatus.FAILED
    assert result.diagnostics == {"failure_code": "worker_failed"}
    assert worker.calls == []
    assert worker.transport_limits == []


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("provider deadline exhausted"),
        RuntimeError("sanitized",),
    ],
)
def test_worker_failure_is_classified_without_retrying_at_the_adapter(error) -> None:
    class Failing:
        def __init__(self):
            self.calls = 0

        def generate_candidate(self, **_kwargs):
            self.calls += 1
            raise error

    worker = Failing()
    result = ResearchRAGPlanWorker(worker).generate({
        "round_index": 1, "timeout_seconds": 1.0, "max_transport_attempts": 1,
    })
    assert result.status == HarnessWorkerStatus.FAILED
    assert result.diagnostics == {
        "failure_code": "worker_timeout" if isinstance(error, TimeoutError) else "worker_failed"
    }
    assert worker.calls == 1
