from __future__ import annotations

from backend.research.application import paper_rag_session
from backend.research.application.paper_rag_session import PaperRAGSession
from backend.research.rag.retrieval.candidate_experiment import RetrievalCandidateExperimentConfig
from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.rag.models import ResearchRetrievalGoal
from framework.harness.context.assembler import ContextAssembler
from framework.harness.rag.context_pack_assembler import RAGContextPackAssembler
from framework.harness.rag.models import RAGBudget, RAGSessionSpec, RetrievalGoal
from framework.harness.rag.source_verifier import SourceVerifier


class _FakeRetriever:
    last_kwargs = {}

    def __init__(self, *args, **kwargs) -> None:
        _FakeRetriever.last_kwargs = kwargs


class _FakeRetrievalPort:
    last_retriever = None
    last_default_section_index = None

    def __init__(self, retriever, *, default_section_index: int = 0) -> None:
        _FakeRetrievalPort.last_retriever = retriever
        _FakeRetrievalPort.last_default_section_index = default_section_index


class _FakeController:
    last_retrieval = None
    last_planner = None
    last_answer_worker = None
    last_source_verifier = None
    last_memory = None
    last_context_pack_assembler = None
    last_event_recorder = None

    def __init__(
        self,
        *,
        retrieval,
        planner=None,
        answer_worker=None,
        source_verifier=None,
        memory=None,
        context_pack_assembler=None,
        event_recorder=None,
    ) -> None:
        _FakeController.last_retrieval = retrieval
        _FakeController.last_planner = planner
        _FakeController.last_answer_worker = answer_worker
        _FakeController.last_source_verifier = source_verifier
        _FakeController.last_memory = memory
        _FakeController.last_context_pack_assembler = context_pack_assembler
        _FakeController.last_event_recorder = event_recorder

    def run(self, spec):
        return {"spec": spec}


def test_paper_rag_session_passes_retrieval_policy_to_inner_retriever(monkeypatch):
    policy = object()
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)

    session = PaperRAGSession(object(), retrieval_policy=policy)
    result = session.run(
        ResearchRetrievalGoal(
            goal_id="g1",
            paper_id="p1",
            question="What does the figure show?",
            required_evidence_types=["figure"],
            allowed_source_refs=["paper://p1"],
            allowed_memory_namespaces=["research"],
        ),
        graph_identity=build_paper_analysis_context_graph_identity(run_id="run-1", stage_id="run_research_rag"),
        session_id="session-1",
        current_section_index=3,
    )

    assert _FakeRetriever.last_kwargs["policy"] is policy
    assert _FakeRetrievalPort.last_default_section_index == 3
    assert result["spec"].goal.question == "What does the figure show?"
    assert _FakeController.last_planner is None


def test_paper_rag_session_does_not_wire_worker_when_candidate_experiment_is_disabled(monkeypatch):
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)

    PaperRAGSession(object(), plan_worker=object()).run(
        ResearchRetrievalGoal(
            goal_id="g1",
            paper_id="p1",
            question="What does the figure show?",
            required_evidence_types=["figure"],
            allowed_source_refs=["paper://p1"],
            allowed_memory_namespaces=["research"],
        ),
        graph_identity=build_paper_analysis_context_graph_identity(run_id="run-disabled", stage_id="run_research_rag"),
        session_id="session-disabled",
    )

    assert _FakeController.last_planner is None


def test_paper_rag_session_wires_optional_worker_planner(monkeypatch):
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)
    worker = object()

    config = RetrievalCandidateExperimentConfig(
        enabled=True,
        max_candidate_queries=3,
        max_worker_calls=2,
        timeout_seconds=4.5,
        max_transport_attempts=2,
        allowed_expansion_terms=("figure",),
        configured_hard_constraints={"language": ("English",)},
    )
    session = PaperRAGSession(
        object(),
        plan_worker=worker,
        worker_planner_min_round_index=2,
        candidate_experiment=config,
    )
    result = session.run(
        ResearchRetrievalGoal(
            goal_id="g1",
            paper_id="p1",
            question="What does the figure show?",
            required_evidence_types=["figure"],
            allowed_source_refs=["paper://p1"],
            allowed_memory_namespaces=["research"],
        ),
        graph_identity=build_paper_analysis_context_graph_identity(run_id="run-1", stage_id="run_research_rag"),
        session_id="session-1",
    )

    assert _FakeController.last_planner is not None
    assert _FakeController.last_planner.min_round_index == 2
    assert _FakeController.last_planner.worker._worker is worker
    worker_planner = _FakeController.last_planner._worker_planner
    assert worker_planner.max_candidate_queries == 3
    assert worker_planner.max_worker_calls == 2
    assert worker_planner.timeout_seconds == 4.5
    assert worker_planner.max_transport_attempts == 2
    assert result["spec"].metadata["candidate_experiment"]["enabled"] is True
    assert result["spec"].metadata["candidate_experiment"]["config_version"] == "research-retrieval-candidate-config-v1"
    assert result["spec"].metadata["candidate_experiment"] == config.to_dict()


def test_paper_rag_session_wires_optional_answer_worker_and_generation_policy(monkeypatch):
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)
    answer_worker = object()

    session = PaperRAGSession(
        object(),
        answer_worker=answer_worker,
        generation_policy={"enabled": True},
    )
    result = session.run(
        ResearchRetrievalGoal(
            goal_id="g1",
            paper_id="p1",
            question="What does the figure show?",
            required_evidence_types=["figure"],
            allowed_source_refs=["paper://p1"],
            allowed_memory_namespaces=["research"],
        ),
        graph_identity=build_paper_analysis_context_graph_identity(run_id="run-1", stage_id="run_research_rag"),
        session_id="session-1",
    )

    assert _FakeController.last_answer_worker is answer_worker
    assert result["spec"].generation_policy == {"enabled": True}


def test_paper_rag_session_wires_optional_relevance_scorer_and_policy_thresholds(monkeypatch):
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)
    relevance_scorer = object()

    session = PaperRAGSession(object(), relevance_scorer=relevance_scorer)
    result = session.run(
        ResearchRetrievalGoal(
            goal_id="g1",
            paper_id="p1",
            question="What does the figure show?",
            required_evidence_types=["figure"],
            allowed_source_refs=["paper://p1"],
            allowed_memory_namespaces=["research"],
        ),
        graph_identity=build_paper_analysis_context_graph_identity(run_id="run-1", stage_id="run_research_rag"),
        session_id="session-1",
    )

    assert isinstance(_FakeController.last_source_verifier, SourceVerifier)
    assert _FakeController.last_source_verifier.relevance_scorer is relevance_scorer
    assert result["spec"].source_policy["min_relevance"] == 0.3
    assert result["spec"].source_policy["min_relevance_by_type"] == {
        "formula": 0.2,
        "table": 0.2,
    }


def test_paper_rag_session_wires_optional_memory_port(monkeypatch):
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)
    memory = object()

    session = PaperRAGSession(object(), memory=memory)
    session.run(
        ResearchRetrievalGoal(
            goal_id="g1",
            paper_id="p1",
            question="What does the figure show?",
            required_evidence_types=["figure"],
            allowed_source_refs=["paper://p1"],
            allowed_memory_namespaces=["research"],
        ),
        graph_identity=build_paper_analysis_context_graph_identity(run_id="run-1", stage_id="run_research_rag"),
        session_id="session-1",
    )

    assert _FakeController.last_memory is memory


def test_paper_rag_session_wires_optional_event_recorder(monkeypatch):
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)
    recorder = object()

    PaperRAGSession(object(), event_recorder=recorder).run(
        ResearchRetrievalGoal(
            goal_id="g1",
            paper_id="p1",
            question="What does the figure show?",
            required_evidence_types=["figure"],
            allowed_source_refs=["paper://p1"],
            allowed_memory_namespaces=["research"],
        ),
        graph_identity=build_paper_analysis_context_graph_identity(
            run_id="run-recorder", stage_id="run_research_rag"
        ),
        session_id="session-recorder",
    )

    assert _FakeController.last_event_recorder is recorder


def test_paper_rag_session_builds_run_scoped_context_assembler(monkeypatch):
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)
    supplied: list[RAGSessionSpec] = []

    def context_assembler_factory(spec: RAGSessionSpec) -> ContextAssembler:
        supplied.append(spec)
        return ContextAssembler()

    result = PaperRAGSession(
        object(),
        context_assembler_factory=context_assembler_factory,
    ).run(
        ResearchRetrievalGoal(
            goal_id="g1",
            paper_id="p1",
            question="Which evidence is supported?",
            required_evidence_types=["method"],
            allowed_source_refs=["paper://p1"],
            allowed_memory_namespaces=["research"],
        ),
        graph_identity=build_paper_analysis_context_graph_identity(run_id="run-context", stage_id="run_research_rag"),
        session_id="session-context",
    )

    assert supplied == [result["spec"]]
    assert isinstance(
        _FakeController.last_context_pack_assembler,
        RAGContextPackAssembler,
    )


def test_paper_rag_session_run_spec_preserves_supplied_contract_identity(monkeypatch):
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)
    supplied_spec = RAGSessionSpec(
        session_id="session-supplied",
        graph_identity=build_paper_analysis_context_graph_identity(run_id="run-supplied", stage_id="run_research_rag"),
        goal=RetrievalGoal(
            goal_id="goal-supplied",
            question="Which evidence is supported?",
            required_evidence_types=("method",),
            known_context_refs=("paper://p1",),
            metadata={"paper_id": "p1", "target_sections": ["method"]},
        ),
        allowed_corpora=("paper:p1",),
        allowed_memory_namespaces=("research",),
        allowed_tools=("paper_chunk_search",),
        source_policy={"allowed_source_refs": ["paper://p1"]},
        budget=RAGBudget(
            max_rounds=3,
            max_replans=2,
            max_queries=5,
            max_source_reads=7,
            max_memory_hits=1,
            max_context_items=4,
            max_context_tokens=1024,
            max_worker_calls=6,
        ),
        context_policy={"projection": "caller-owned"},
        generation_policy={"enabled": True, "mode": "caller-owned"},
        metadata={"paper_id": "p1", "run_id": "run-supplied"},
    )

    result = PaperRAGSession(object()).run_spec(supplied_spec, current_section_index=4)

    assert result["spec"] is supplied_spec
    assert result["spec"].budget is supplied_spec.budget
    assert result["spec"].source_policy is supplied_spec.source_policy
    assert result["spec"].context_policy is supplied_spec.context_policy
    assert result["spec"].generation_policy is supplied_spec.generation_policy
    assert _FakeRetrievalPort.last_default_section_index == 4


def test_paper_rag_session_propagates_tenant_scope_to_session_spec(monkeypatch):
    monkeypatch.setattr(paper_rag_session, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_session, "PaperChunkRetrievalPort", _FakeRetrievalPort)
    monkeypatch.setattr(paper_rag_session, "BoundedRAGSessionController", _FakeController)

    session = PaperRAGSession(object())
    result = session.run(
        ResearchRetrievalGoal(
            goal_id="g1",
            paper_id="p1",
            question="What does the figure show?",
            required_evidence_types=["figure"],
            allowed_source_refs=["paper://p1"],
            allowed_memory_namespaces=["research:tenant:tenant-a:user:user-1"],
            metadata={
                "tenant_id": "tenant-a",
                "user_id": "user-1",
                "memory_namespace": "research:tenant:tenant-a:user:user-1",
            },
        ),
        graph_identity=build_paper_analysis_context_graph_identity(run_id="run-1", stage_id="run_research_rag"),
        session_id="session-1",
    )

    spec = result["spec"]
    assert spec.source_policy["tenant_id"] == "tenant-a"
    assert spec.metadata["tenant_id"] == "tenant-a"
    assert spec.metadata["user_id"] == "user-1"
    assert spec.goal.metadata["memory_namespace"] == "research:tenant:tenant-a:user:user-1"
    assert spec.allowed_memory_namespaces == ("research:tenant:tenant-a:user:user-1",)
