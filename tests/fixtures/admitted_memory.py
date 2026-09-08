"""Real durable memory grants for execution-bound RAG integration tests."""

from dataclasses import replace
from types import SimpleNamespace

from backend.research.graphs import build_paper_analysis_context_graph_identity
from framework.harness.ref_authority import RefDescriptor
from framework.harness.ref_memory import HarnessMemoryNamespaceReader, HarnessMemoryRecallRuntime
from framework.memory.models import MemoryKind, MemoryRecord, MemoryScope
from framework.memory.namespace import MemoryNamespacePublisher
from framework.memory.policy import MemoryPolicy
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from tests.framework.harness.test_ref_snapshot_store import _snapshot, _store


def admitted_memory(tmp_path, *, activity_id="rag-activity", commit=True, shared_owner=False):
    graph = build_paper_analysis_context_graph_identity(
        run_id="rag-grant-run", stage_id="run_research_rag",
    ).with_physical_activity(
        node_id="run_research_rag", node_instance_id="rag-node",
        activity_id=activity_id, activity_attempt=1,
    )
    execution = graph.to_graph_execution_identity()
    root = _snapshot()
    namespace = "research:tenant:tenant-1:user:owner-1"
    namespaces = FilesystemMemoryNamespaceStore(tmp_path / "namespaces")
    publisher = MemoryNamespacePublisher(
        namespaces, namespace=namespace, tenant_id=root.policy.tenant_id,
        owner_id=root.policy.owner_id, shared_read_only=False, policy=MemoryPolicy(),
    )
    records = tuple(MemoryRecord(
        memory_id=memory_id, content=content, kind=kind, scope=MemoryScope.GRAPH,
        namespace=namespace, tenant_id=root.policy.tenant_id, actor=root.policy.owner_id,
        refs={"evidence_id": "method-evidence"},
    ) for memory_id, content, kind in (
        ("mem/1", "Prior paper ask found the ablation evidence in section 4.", MemoryKind.EPISODIC),
        ("mem-2", "Semantic ablation evidence is excluded from RAG recall.", MemoryKind.SEMANTIC),
    ))
    metadata = publisher.publish(records)
    descriptor = RefDescriptor.memory(
        namespace=namespace, ref=metadata.exact_ref, run_id=execution.run_id,
        stage_id=graph.stage_id, tenant_id=metadata.tenant_id, owner_id=metadata.owner_id,
        source_checksum=metadata.source_checksum,
    )
    descriptors = (descriptor,)
    shared_metadata = None
    if shared_owner:
        shared_metadata = MemoryNamespacePublisher(
            namespaces, namespace=namespace + ".shared", tenant_id=root.policy.tenant_id,
            owner_id="other-owner", shared_read_only=True, policy=MemoryPolicy(),
        ).publish((replace(records[0], memory_id="shared-note", actor="other-owner", namespace=namespace + ".shared"),))
        descriptors += (RefDescriptor.memory(
            namespace=shared_metadata.namespace, ref=shared_metadata.exact_ref, run_id=execution.run_id,
            stage_id=graph.stage_id, tenant_id=shared_metadata.tenant_id,
            owner_id=shared_metadata.owner_id, source_checksum=shared_metadata.source_checksum,
            scope="SHARED_READ_ONLY",
        ),)
    root = replace(
        root, execution_identity=execution, stage_id=graph.stage_id,
        stage_binding_checksum=graph.stage_binding_checksum, descriptors=descriptors,
        policy=replace(
            root.policy, run_id=execution.run_id, stage_id=graph.stage_id,
            allowed_refs=tuple(item.ref for item in descriptors), allowed_ref_kinds=("memory",),
            allowed_artifact_types=("memory_namespace",), allowed_memory_namespaces=tuple(item.namespace for item in descriptors),
            pinned_checksums={item.ref: item.source_checksum for item in descriptors},
            shared_read_only_refs=(shared_metadata.exact_ref,) if shared_metadata else (),
        ),
    )
    snapshots, events = _store(tmp_path)
    if commit:
        snapshots.commit(root)
    recall = HarnessMemoryRecallRuntime(HarnessMemoryNamespaceReader(
        snapshot=root, snapshot_store=snapshots, namespace_store=namespaces,
        execution_identity=execution,
    ))
    return SimpleNamespace(
        graph=graph, root=root, namespaces=namespaces, metadata=metadata, records=records,
        publisher=publisher, snapshots=snapshots, events=events, recall=recall,
        shared_metadata=shared_metadata,
    )
