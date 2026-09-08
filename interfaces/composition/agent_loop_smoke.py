from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from framework.agent.artifacts.stores.filesystem import FilesystemArtifactStore
from framework.agent.models import AgentSpec
from framework.events.canonical import checksum_for
from framework.events.runtime.publisher import EventRuntime
from framework.events.schema import default_event_schema_catalog
from framework.harness.control_plane.graph_runtime import HarnessGraphActivity
from framework.harness.ref_authority import RefAccessPolicy, RefDescriptor
from framework.harness.ref_memory import HarnessMemoryNamespaceReader, HarnessMemoryRecallRuntime
from framework.harness.ref_snapshot import RefAuthoritySnapshot
from framework.harness.ref_snapshot_store import DurableRefAuthoritySnapshotStore
from framework.memory.models import MemoryRecord
from framework.memory.namespace import MemoryNamespacePublisher
from framework.memory.policy import MemoryPolicy
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.time import utc_now
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from infrastructure.storage.conversation import LocalJsonConversationStore
from infrastructure.storage.events.sqlite import SQLiteEventStore
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from interfaces.services.agent_loop_smoke_service import (
    AgentLoopGraphSmokeApplicationService,
)


def build_agent_loop_graph_smoke_service(
    *,
    artifact_root: str | Path = ".newsroom/smoke",
    clock: Callable[[], datetime] = utc_now,
) -> AgentLoopGraphSmokeApplicationService:
    root = Path(artifact_root)
    return AgentLoopGraphSmokeApplicationService(
        artifact_port=FilesystemHarnessArtifactPort(root),
        conversation_store=LocalJsonConversationStore(
            root / "_state" / "agent-loop-conversations"
        ),
        artifact_root=root,
        clock=clock,
        memory_recall_factory=lambda activity, topic, agent: _memory_recall(
            root, activity, topic, agent,
        ),
    )


def _memory_recall(
    root: Path, activity: HarnessGraphActivity, topic: str, agent: AgentSpec,
) -> HarnessMemoryRecallRuntime:
    """Use fixture records with real durable authority and immutable retrieval."""
    state_root = root / "_state" / "agent-loop-memory"
    events = SQLiteEventStore(state_root / "events.sqlite3")
    snapshots = DurableRefAuthoritySnapshotStore(
        EventRuntime(store=events, schema_catalog=default_event_schema_catalog()),
        events, artifact_store=FilesystemArtifactStore(state_root / "grants"),
        tenant_id="local-smoke",
    )
    namespaces = FilesystemMemoryNamespaceStore(state_root / "namespaces")
    metadata = MemoryNamespacePublisher(
        namespaces, namespace="smoke.analysis", tenant_id=activity.tenant_scope_ref,
        owner_id=activity.identity_scope_ref, shared_read_only=False, policy=MemoryPolicy(),
    ).publish((MemoryRecord(
        memory_id="smoke-note", content=f"Fixture memory for {topic}",
        namespace="smoke.analysis", tenant_id=activity.tenant_scope_ref,
        actor=activity.identity_scope_ref, refs={"source_id": "fixture://agent-loop"},
        created_at=datetime(2026, 8, 16, tzinfo=UTC),
    ),))
    execution = GraphExecutionIdentity(
        run_id=activity.run_id, graph_id=activity.graph_ref.graph_id,
        graph_version=activity.graph_ref.identity_version,
        graph_ref=activity.graph_ref.identity_ref.exact_ref,
        graph_checksum=activity.graph_ref.checksum, node_id=activity.node_id,
        node_instance_id=activity.node_instance_id,
        activity_id=activity.activity_id, attempt=activity.attempt,
    )
    descriptor = RefDescriptor.memory(
        namespace=metadata.namespace, ref=metadata.exact_ref,
        run_id=activity.run_id, stage_id=activity.node_id,
        tenant_id=metadata.tenant_id, owner_id=metadata.owner_id,
        source_checksum=metadata.source_checksum,
    )
    policy = RefAccessPolicy(
        policy_id="smoke-memory", version="1", run_id=activity.run_id,
        stage_id=activity.node_id, tenant_id=activity.tenant_scope_ref,
        owner_id=activity.identity_scope_ref, allowed_refs=(descriptor.ref,),
        allowed_artifact_types=("memory_namespace",), allowed_ref_kinds=("memory",),
        allowed_memory_namespaces=(metadata.namespace,),
        pinned_checksums={descriptor.ref: descriptor.source_checksum},
    )
    snapshot = RefAuthoritySnapshot(
        execution_identity=execution, stage_id=activity.node_id,
        stage_binding_checksum=checksum_for({
            "graph_checksum": activity.graph_ref.checksum,
            "step_ref": activity.step_ref.exact_ref,
            "worker_ref": activity.worker_ref.exact_ref,
            "activity_ref": activity.activity_ref.exact_ref,
        }),
        task_policy_checksum=checksum_for(agent.to_dict()),
        source_checksum=activity.input_ref, policy=policy, descriptors=(descriptor,),
    )
    snapshots.commit(snapshot)
    return HarnessMemoryRecallRuntime(HarnessMemoryNamespaceReader(
        snapshot=snapshot, snapshot_store=snapshots,
        namespace_store=namespaces, execution_identity=execution,
    ))


__all__ = ["build_agent_loop_graph_smoke_service"]
