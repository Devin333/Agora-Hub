from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.research.application.research_input_contract import ResearchInputContractService
from framework.agent.loop import AgentLoop
from framework.agent.loop.prompt import PromptBuilder
from framework.agent.models import AgentSpec
from framework.harness import HarnessWorkerResult
from framework.harness.control_plane.errors import HarnessValidationError
from framework.tool import ToolExecutor, ToolRegistry
from interfaces.composition.research import (
    _ProductionResearchAnalysisWorker,
    _ResearchCandidateLLMClient,
)


def _context():
    service = ResearchInputContractService()
    profile = service.build_profile(
        tenant_id="tenant-1", paper_id="paper-1", source_ref="https://paper.example/v2",
        source_revision="source-v2", objective='Explain "method"\nwith \\exact source.',
        constraints=("Keep the reported test split",),
    )
    return service.prepare(profile, tenant_id="tenant-1", paper_id="paper-1",
                           source_ref="https://paper.example/v2", source_revision="source-v2")


def _recording_worker(context):
    calls = []
    worker = _ProductionResearchAnalysisWorker(
        "research.analysis.structure", dependencies=object(),
        workspace=SimpleNamespace(input_context=context),
    )

    def candidate(_dependencies, task, _workspace, **_kwargs):
        calls.append(task)
        return HarnessWorkerResult(status="succeeded", output={"output": {"summary": "candidate"}})

    worker._methods[worker.worker_id] = candidate
    return worker, calls


def test_real_agent_loop_translates_reserved_projection_and_only_admitted_dependencies() -> None:
    context = _context()
    worker, calls = _recording_worker(context)
    inputs = {
        "objective": "Analyze this child task",
        "input_refs": ["document", "evidence_pack", "task-result://prior"],
        "resolved_inputs": {
            "document": {"document_id": "document-1"},
            "evidence_pack": {"pack_id": "pack-1"},
            "task-result://prior": {
                "logical_ref": "task://prior/output", "output": {"prior": "candidate"},
            },
        },
    }
    result = AgentLoop(
        llm_client=_ResearchCandidateLLMClient(worker),
        tool_executor=ToolExecutor(ToolRegistry()),
    ).run(
        AgentSpec(agent_id="research-input-child", name="Research input child",
                  instructions="Generate a candidate from the admitted inputs."),
        inputs, [], run_id="research-input-child-run", standalone=True,
        prepared_input_projection=context.projection,
    )
    assert result.success is True
    assert len(calls) == 1
    assert calls[0]["prepared_input"] == context.candidate_projection()
    assert calls[0]["dependency_outputs"] == {"task://prior/output": {"prior": "candidate"}}
    assert "research_input" not in calls[0]["dependency_outputs"]


@pytest.mark.parametrize("damage", ["missing", "altered"])
def test_production_worker_rejects_missing_or_changed_projection_before_candidate_dispatch(damage) -> None:
    context = _context()
    worker, calls = _recording_worker(context)
    task = {"input_refs": ["document", "evidence_pack"]}
    if damage == "altered":
        envelope = context.candidate_projection()
        envelope["payload"]["objective"] = "Different objective"
        task["prepared_input"] = envelope
    with pytest.raises(HarnessValidationError) as exc:
        worker.execute(task)
    assert exc.value.code == "research_input_projection_mismatch"
    assert calls == []


def test_unprepared_worker_rejects_an_external_projection_before_candidate_dispatch() -> None:
    worker, calls = _recording_worker(None)
    with pytest.raises(HarnessValidationError) as exc:
        worker.execute({"input_refs": ["document", "evidence_pack"],
                        "prepared_input": _context().candidate_projection()})
    assert exc.value.code == "research_input_projection_unexpected"
    assert calls == []


def test_unprepared_agent_request_keeps_the_existing_worker_input_shape() -> None:
    worker, calls = _recording_worker(None)
    request = PromptBuilder().build(
        AgentSpec(agent_id="unprepared-child", name="Unprepared child",
                  instructions="Generate a candidate from the admitted inputs."),
        {"objective": "existing objective", "input_refs": ["document", "evidence_pack"]},
    )
    _ResearchCandidateLLMClient(worker).complete(request)
    assert calls == [{"input_refs": ["document", "evidence_pack"]}]
