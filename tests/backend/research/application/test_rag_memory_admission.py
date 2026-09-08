from __future__ import annotations

from dataclasses import replace

import pytest

from backend.research.application.ask_paper import ResearchActorScope
from backend.research.application.rag_memory_admission import (
    HarnessResearchRAGMemoryAdmission,
)
from backend.research.domain import research_identity_scope_ref
from backend.research.graphs import (
    build_paper_analysis_context_graph_identity,
    build_paper_analysis_graph_definition,
)
from framework.events.canonical import checksum_for
from framework.harness.control_plane.activity_execution import (
    HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.graph_runtime import HarnessGraphActivity
from framework.harness.graph import HarnessGraphCompiler
from framework.harness.graph.activity import graph_activity_input_checksum
from framework.harness.graph.model import HarnessExecutableNode
from framework.harness.graph.reference import HarnessGraphReference
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.memory.models import MemoryQuery, MemoryRecord
from framework.memory.namespace import MemoryNamespacePublisher
from framework.memory.policy import MemoryPolicy
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from tests.framework.harness.test_ref_snapshot_store import _store


def _actor(*, user_id: str = "user-a") -> ResearchActorScope:
    return ResearchActorScope(
        tenant_id="tenant-a",
        user_id=user_id,
        memory_namespace=f"research:tenant:tenant-a:user:{user_id}",
    )


def _publish(store, actor: ResearchActorScope, *, content: str):
    return MemoryNamespacePublisher(
        store,
        namespace=actor.memory_namespace,
        tenant_id=actor.tenant_id or "",
        owner_id=actor.user_id or "",
        shared_read_only=False,
        policy=MemoryPolicy(),
    ).publish((MemoryRecord(
        memory_id="accepted-note",
        content=content,
        namespace=actor.memory_namespace,
        tenant_id=actor.tenant_id,
        actor=actor.user_id or "",
        refs={"evidence_id": "evidence-1"},
    ),))


def _physical_task(run_id: str, actor: ResearchActorScope, *, attempt: int = 1, document=None):
    graph = HarnessGraphCompiler().compile(
        build_paper_analysis_graph_definition()
    ).graph
    node = next(
        item
        for item in graph.nodes
        if isinstance(item, HarnessExecutableNode)
        and item.node_id == "run_research_rag"
    )
    source_task = {
        "run_id": run_id,
        "step_id": node.step_id,
        "worker_type": node.metadata["worker_type"],
        "inputs": {
            "document": document if document is not None else {"paper_id": "paper-1", "source": "accepted"},
            "memory_namespace": actor.memory_namespace,
        },
        "metadata": dict(node.metadata["step_metadata"]),
    }
    scope_ref = research_identity_scope_ref(actor.to_metadata())
    activity = HarnessGraphActivity(
        run_id=run_id,
        graph_ref=HarnessGraphReference.from_graph(graph),
        node_id=node.node_id,
        node_instance_id=f"{node.node_id}:{attempt}",
        step_ref=node.step_ref,
        worker_ref=node.worker_ref,
        activity_ref=node.activity_ref,
        attempt=attempt,
        input_ref=graph_activity_input_checksum(source_task),
        causal_decision_checksum=checksum_for(
            {"run_id": run_id, "node_id": node.node_id, "attempt": attempt}
        ),
        causal_decision_sequence=attempt,
        fencing_generation=1,
        tenant_scope_ref=scope_ref,
        identity_scope_ref=scope_ref,
    )
    task = {
        **source_task,
        HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY: HarnessGraphActivityTaskContext(
            activity=activity,
            graph_checkpoint_ref=f"checkpoint://{run_id}/{attempt}",
        ).to_dict(),
    }
    graph_identity = build_paper_analysis_context_graph_identity(
        run_id=run_id,
        stage_id="run_research_rag",
    ).with_physical_activity(
        node_id=node.node_id,
        node_instance_id=activity.node_instance_id,
        activity_id=activity.activity_id,
        activity_attempt=attempt,
    )
    return task, graph_identity


def _recall(memory, actor: ResearchActorScope):
    return memory.recall(MemoryQuery(
        query="accepted",
        namespace=actor.memory_namespace,
        tenant_id=actor.tenant_id,
        filters={"owner_id": actor.user_id},
        limit=3,
    ))


def test_physical_rag_admission_reuses_old_revision_and_new_execution_uses_new_revision(
    tmp_path,
    monkeypatch,
):
    actor = _actor()
    namespaces = FilesystemMemoryNamespaceStore(tmp_path / "namespaces")
    old = _publish(namespaces, actor, content="accepted old revision")
    snapshots, events = _store(tmp_path / "authority")
    service = HarnessRefAdmissionService(snapshots, memory_namespaces=namespaces)
    admission = HarnessResearchRAGMemoryAdmission(
        service,
        namespace_refs=(old.exact_ref,),
    )
    old_task, old_identity = _physical_task("rag-memory-old", actor)

    old_memory = admission.admit(
        old_task,
        actor_scope=actor,
        graph_identity=old_identity,
        dynamic_task_plan=False,
    )
    old_result = _recall(old_memory, actor)
    assert [item.record.content for item in old_result.results] == [
        "accepted old revision"
    ]
    assert old_result.diagnostics["namespace_refs"] == [old.exact_ref]
    assert events.get_stream_high_watermark(
        "run:rag-memory-old", tenant_id="control"
    ) == 1

    new = _publish(namespaces, actor, content="accepted new revision")
    reopened_snapshots, _ = _store(tmp_path / "authority")
    reopened_service = HarnessRefAdmissionService(
        reopened_snapshots,
        memory_namespaces=FilesystemMemoryNamespaceStore(tmp_path / "namespaces"),
    )
    reopened_admission = HarnessResearchRAGMemoryAdmission(
        reopened_service,
        namespace_refs=(new.exact_ref,),
    )
    real_describe = reopened_service.memory_namespaces.describe
    monkeypatch.setattr(
        reopened_service.memory_namespaces,
        "describe",
        lambda _ref: pytest.fail(
            "an existing execution must not resolve current namespace config"
        ),
    )
    restored = reopened_admission.admit(
        old_task,
        actor_scope=actor,
        graph_identity=old_identity,
        dynamic_task_plan=False,
    )
    assert restored.execution_identity == old_identity.to_graph_execution_identity()
    assert events.get_stream_high_watermark(
        "run:rag-memory-old", tenant_id="control"
    ) == 1

    monkeypatch.setattr(
        reopened_service.memory_namespaces,
        "describe",
        real_describe,
    )
    new_task, new_identity = _physical_task("rag-memory-new", actor)
    new_memory = reopened_admission.admit(
        new_task,
        actor_scope=actor,
        graph_identity=new_identity,
        dynamic_task_plan=False,
    )
    new_result = _recall(new_memory, actor)
    assert [item.record.content for item in new_result.results] == [
        "accepted new revision"
    ]
    assert new_result.diagnostics["namespace_refs"] == [new.exact_ref]
    assert old.exact_ref not in new_result.diagnostics["namespace_refs"]


def test_physical_rag_admission_rejects_wrong_actor_and_execution_before_namespace_read(
    tmp_path,
    monkeypatch,
):
    actor = _actor()
    namespaces = FilesystemMemoryNamespaceStore(tmp_path / "namespaces")
    revision = _publish(namespaces, actor, content="accepted actor note")
    snapshots, _ = _store(tmp_path / "authority")
    service = HarnessRefAdmissionService(snapshots, memory_namespaces=namespaces)
    admission = HarnessResearchRAGMemoryAdmission(
        service,
        namespace_refs=(revision.exact_ref,),
    )
    task, identity = _physical_task("rag-memory-denied", actor)
    calls = 0
    real_describe = namespaces.describe

    def counted_describe(ref):
        nonlocal calls
        calls += 1
        return real_describe(ref)

    monkeypatch.setattr(namespaces, "describe", counted_describe)
    with pytest.raises(HarnessValidationError) as wrong_actor:
        admission.admit(
            task,
            actor_scope=_actor(user_id="user-b"),
            graph_identity=identity,
            dynamic_task_plan=False,
        )
    assert wrong_actor.value.code == "REF_POLICY_SCOPE_MISMATCH"
    with pytest.raises(HarnessValidationError) as wrong_execution:
        admission.admit(
            task,
            actor_scope=actor,
            graph_identity=replace(identity, node_instance_id="foreign-instance"),
            dynamic_task_plan=False,
        )
    assert wrong_execution.value.code == "REF_SNAPSHOT_BINDING_MISMATCH"
    assert calls == 0


def test_missing_configured_revision_cannot_issue_a_physical_rag_grant(tmp_path):
    actor = _actor()
    namespaces = FilesystemMemoryNamespaceStore(tmp_path / "namespaces")
    snapshots, events = _store(tmp_path / "authority")
    service = HarnessRefAdmissionService(snapshots, memory_namespaces=namespaces)
    admission = HarnessResearchRAGMemoryAdmission(
        service,
        namespace_refs=("memory-namespace://" + "f" * 64,),
    )
    task, identity = _physical_task("rag-memory-missing", actor)

    with pytest.raises(HarnessValidationError) as exc_info:
        admission.admit(
            task,
            actor_scope=actor,
            graph_identity=identity,
            dynamic_task_plan=False,
        )
    assert exc_info.value.code == "REF_UNRESOLVED"
    assert events.get_stream_high_watermark(
        "run:rag-memory-missing", tenant_id="control"
    ) is None


def test_corrupt_durable_physical_rag_grant_never_falls_back_to_current_revision(
    tmp_path,
    monkeypatch,
):
    actor = _actor()
    namespaces = FilesystemMemoryNamespaceStore(tmp_path / "namespaces")
    revision = _publish(namespaces, actor, content="accepted note")
    authority_root = tmp_path / "authority"
    snapshots, _ = _store(authority_root)
    service = HarnessRefAdmissionService(snapshots, memory_namespaces=namespaces)
    admission = HarnessResearchRAGMemoryAdmission(
        service,
        namespace_refs=(revision.exact_ref,),
    )
    task, identity = _physical_task("rag-memory-corrupt", actor)
    admission.admit(
        task,
        actor_scope=actor,
        graph_identity=identity,
        dynamic_task_plan=False,
    )
    snapshot_artifact = next((authority_root / "artifacts").rglob("*.json"))
    snapshot_artifact.write_bytes(b'{"corrupt":true}')

    reopened, _ = _store(authority_root)
    reopened_service = HarnessRefAdmissionService(
        reopened,
        memory_namespaces=FilesystemMemoryNamespaceStore(tmp_path / "namespaces"),
    )
    monkeypatch.setattr(
        reopened_service.memory_namespaces,
        "describe",
        lambda _ref: pytest.fail("corrupt grants must fail before namespace fallback"),
    )
    corrupt_admission = HarnessResearchRAGMemoryAdmission(
        reopened_service,
        namespace_refs=(revision.exact_ref,),
    )
    with pytest.raises(HarnessValidationError):
        corrupt_admission.admit(
            task,
            actor_scope=actor,
            graph_identity=identity,
            dynamic_task_plan=False,
        )


def test_physical_capability_reaches_real_bounded_paper_rag_memory_context(tmp_path):
    from backend.research.application.bounded_document_rag import BoundedDocumentRAGRuntime
    from backend.research.application.paper_rag_session import PaperRAGSession
    from backend.research.rag.adapters.memory_port import ResearchRAGMemoryPort
    from framework.memory.models import MemoryKind
    from tests.backend.research.application.test_bounded_document_rag import (
        _MemoryChunkStore, _RelationalChunker, _document, _spec,
    )

    actor = _actor()
    run_id = "rag-memory-full-path"
    document = _document()
    namespaces = FilesystemMemoryNamespaceStore(tmp_path / "namespaces")
    revision = MemoryNamespacePublisher(
        namespaces, namespace=actor.memory_namespace, tenant_id=actor.tenant_id,
        owner_id=actor.user_id, shared_read_only=False, policy=MemoryPolicy(),
    ).publish((MemoryRecord(
        memory_id="grounded-paper-note", kind=MemoryKind.EPISODIC,
        content="Evidence available for 2401.00001: deterministic Harness routing and verification.",
        namespace=actor.memory_namespace, tenant_id=actor.tenant_id, actor=actor.user_id,
        refs={"evidence_id": "verified-paper-evidence"},
    ),))
    snapshots, _ = _store(tmp_path / "authority")
    admission = HarnessResearchRAGMemoryAdmission(
        HarnessRefAdmissionService(snapshots, memory_namespaces=namespaces), namespace_refs=(revision.exact_ref,),
    )
    task, identity = _physical_task(run_id, actor, document=document.to_dict())
    capability = admission.admit(task, actor_scope=actor, graph_identity=identity, dynamic_task_plan=False)
    spec = replace(_spec(run_id=run_id, session_id="rag-memory-full-session"), graph_identity=identity)
    runtime = BoundedDocumentRAGRuntime(
        _MemoryChunkStore(), chunker=_RelationalChunker(),
        authorized_session_factory=lambda store, memory: PaperRAGSession(store, memory=ResearchRAGMemoryPort(memory)),
    )
    result = runtime.run(session_spec=spec, document=document, memory_recall=capability)
    assert result.memory_context
    hit = result.memory_context[0]
    assert hit["namespace_ref"] == revision.exact_ref
    assert hit["namespace_checksum"] == revision.source_checksum
    assert hit["execution_identity"] == identity.to_graph_execution_identity().to_dict()
    assert hit["input_snapshot_ref"]
    assert "deterministic Harness" in hit["summary"]


def test_actual_graph_worker_receives_the_configured_memory_admission(tmp_path):
    from backend.research.application import AnalyzePaperRequest
    from backend.research.application.single_paper_runtime import ResearchSinglePaperRuntime
    from framework.harness import FakeArtifactPort, InMemoryHarnessEventPort
    from tests.backend.research.fakes import (
        FakeGithubRepositoryPort,
        FakeResearchDocumentCompiler,
        FakeResearchLLMWorker,
        FakeResearchRAGRuntime,
        FakeResearchSourceProvider,
        in_memory_node_output_resource_factory,
    )

    actor = _actor()
    namespaces = FilesystemMemoryNamespaceStore(tmp_path / "namespaces")
    revision = _publish(namespaces, actor, content="accepted note for the physical worker")
    snapshots, _ = _store(tmp_path / "authority")
    admission = HarnessResearchRAGMemoryAdmission(
        HarnessRefAdmissionService(snapshots, memory_namespaces=namespaces),
        namespace_refs=(revision.exact_ref,),
    )
    recalls = []

    class AuthorizedRAG(FakeResearchRAGRuntime):
        def run(self, *, session_spec, document, memory_recall):
            execution = session_spec.graph_identity.to_graph_execution_identity()
            memory_recall.validate_execution(execution)
            result = _recall(memory_recall, actor)
            assert result.results
            assert result.diagnostics["execution_identity"] == execution.to_dict()
            recalls.append(result)
            return super().run(session_spec=session_spec, document=document)

    runtime = ResearchSinglePaperRuntime(
        source_provider=FakeResearchSourceProvider(),
        document_compiler=FakeResearchDocumentCompiler(),
        llm_worker=FakeResearchLLMWorker(),
        github_repository=FakeGithubRepositoryPort(),
        rag_runtime=AuthorizedRAG(),
        rag_memory_admission=admission,
        artifact_port=FakeArtifactPort(),
        event_port_factory=lambda _run: InMemoryHarnessEventPort(),
        node_output_resource_factory=in_memory_node_output_resource_factory,
    )
    result = runtime.run(AnalyzePaperRequest(
        run_id="rag-memory-actual-worker", paper_id="paper-harness-001",
        source_ref="https://arxiv.org/abs/2606.00123",
        tenant_id=actor.tenant_id, user_id=actor.user_id,
        memory_namespace=actor.memory_namespace,
    ))
    assert result.succeeded, result.diagnostics
    assert len(recalls) == 1
    assert recalls[0].diagnostics["namespace_refs"] == [revision.exact_ref]
