from __future__ import annotations

from dataclasses import replace
from importlib import import_module

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_memory import HarnessMemoryNamespaceReader, HarnessMemoryRecallRuntime
from framework.memory.models import MemoryKind, MemoryQuery, MemoryRecord, MemoryScope
from framework.memory.namespace import MemoryNamespacePublisher
from framework.memory.policy import MemoryPolicy
from framework.tool import ToolCall, ToolExecutor, ToolPolicy, ToolRegistry, ToolStatus
from framework.tool.builtin.memory import register_memory_tools
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from tests.framework.harness.test_ref_memory import _setup
from tests.framework.harness.test_ref_snapshot_store import _store


def test_recall_reads_real_revision_with_bounded_context_and_lineage(tmp_path):
    root, metadata, reader, _, _, _ = _setup(tmp_path)
    runtime = HarnessMemoryRecallRuntime(reader)
    result = runtime.recall(MemoryQuery(query="evidence", limit=100, max_context_tokens=100_000))
    assert [item.memory_id for item in result.results] == ["note-1"]
    assert "evidence-bound note" in result.context_block.content
    assert result.query.limit == 5
    assert result.query.max_context_tokens == 1500
    assert result.diagnostics["input_snapshot_ref"] == root.snapshot_ref
    assert result.diagnostics["namespace_refs"] == [metadata.exact_ref]
    assert result.diagnostics["namespace_checksums"] == {metadata.exact_ref: metadata.source_checksum}
    assert not any(hasattr(runtime, name) for name in ("store", "write", "promote", "get"))


@pytest.mark.parametrize("query", [
    {"query": "evidence", "namespace": "private"},
    {"query": "evidence", "tenant_id": "sibling-tenant"},
    {"query": "evidence", "filters": {"owner_id": "sibling-owner"}},
    {"query": "evidence", "filters": {"actor": "sibling-owner"}},
    {"query": "evidence", "filters": {"run_id": "other-run"}},
    {"query": "evidence", "filters": {"collection": "latest"}},
    {"query": "evidence", "namespace_ref": "memory-namespace://" + "a" * 64},
    {"query": "evidence", "filters": {"revision": "latest"}},
    {"query": "evidence", "filters": {"owner_id": "owner-1", "actor": "other"}},
])
def test_recall_selectors_cannot_authorize_another_namespace(tmp_path, monkeypatch, query):
    _, _, reader, namespaces, _, _ = _setup(tmp_path)
    monkeypatch.setattr(namespaces, "describe", lambda ref: pytest.fail("unadmitted metadata read"))
    monkeypatch.setattr(namespaces, "read", lambda ref: pytest.fail("unadmitted payload read"))
    with pytest.raises(HarnessValidationError) as error:
        HarnessMemoryRecallRuntime(reader).recall(query)
    assert error.value.code == "REF_UNAUTHORIZED"


def test_missing_grant_and_alternate_execution_never_read_payload(tmp_path, monkeypatch):
    root, _, reader, namespaces, _, _ = _setup(tmp_path, commit_grant=False)
    monkeypatch.setattr(namespaces, "describe", lambda ref: pytest.fail("metadata read without grant"))
    runtime = HarnessMemoryRecallRuntime(reader)
    with pytest.raises(HarnessValidationError):
        runtime.recall("evidence")
    with pytest.raises(HarnessValidationError, match="caller"):
        runtime.validate_execution(replace(root.execution_identity, attempt=2))


def test_policy_denial_precedes_namespace_io(tmp_path, monkeypatch):
    _, _, reader, namespaces, _, _ = _setup(tmp_path)
    monkeypatch.setattr(namespaces, "describe", lambda ref: pytest.fail("disabled recall read metadata"))
    with pytest.raises(PermissionError):
        HarnessMemoryRecallRuntime(reader).recall("evidence", policy=MemoryPolicy(allow_recall=False))


def test_caller_policy_can_narrow_default_scopes_kinds_and_bounds(tmp_path):
    _, _, reader, _, _, _ = _setup(tmp_path)
    result = HarnessMemoryRecallRuntime(reader).recall("evidence", policy=MemoryPolicy(
        allowed_scopes=[MemoryScope.SESSION], allowed_kinds=[MemoryKind.SEMANTIC],
        max_recall_results=1, max_context_tokens=16,
    ))
    assert result.query.scopes == [MemoryScope.SESSION]
    assert result.query.kinds == [MemoryKind.SEMANTIC]
    assert result.query.limit == 1
    assert result.query.max_context_tokens == 16
    assert result.context_block.token_estimate <= 16


def test_caller_policy_cannot_expand_ceiling_or_turn_empty_intersection_into_all(tmp_path, monkeypatch):
    _, _, reader, namespaces, _, _ = _setup(tmp_path)
    runtime = HarnessMemoryRecallRuntime(reader)
    result = runtime.recall("evidence", policy=MemoryPolicy(max_recall_results=100, max_context_tokens=100_000))
    assert result.query.limit == 5
    assert result.query.max_context_tokens == 1500
    monkeypatch.setattr(namespaces, "read", lambda ref: pytest.fail("disjoint policy read payload"))
    with pytest.raises(HarnessValidationError, match="no permitted scope"):
        runtime.recall("evidence", policy=MemoryPolicy(allowed_scopes=[MemoryScope.GLOBAL]))


def test_reopen_recall_and_tool_ignore_newly_published_revision(tmp_path):
    root, metadata, reader, namespaces, _, events = _setup(tmp_path)
    original = HarnessMemoryRecallRuntime(reader).recall("evidence")
    published = MemoryNamespacePublisher(
        namespaces, namespace=metadata.namespace, tenant_id=metadata.tenant_id,
        owner_id=metadata.owner_id, shared_read_only=False, policy=MemoryPolicy(),
    ).publish((MemoryRecord(
        content="changed current evidence", memory_id="note-1", namespace=metadata.namespace,
        tenant_id=metadata.tenant_id, actor=metadata.owner_id, refs={"evidence_id": "new"},
    ),))
    assert published.exact_ref != metadata.exact_ref
    snapshots, _ = _store(tmp_path)
    runtime = HarnessMemoryRecallRuntime(HarnessMemoryNamespaceReader(
        snapshot=root, snapshot_store=snapshots,
        namespace_store=FilesystemMemoryNamespaceStore(tmp_path / "namespaces"),
        execution_identity=root.execution_identity,
    ))
    assert runtime.recall("evidence").to_dict() == original.to_dict()
    registry = ToolRegistry()
    register_memory_tools(registry, memory_recall=runtime, execution_identity=root.execution_identity)
    observation = ToolExecutor(registry, graph_identity=root.execution_identity).execute(
        ToolCall(tool_name="memory.recall", arguments={"query": "evidence"}),
        ToolPolicy(allowed_tools=["memory.recall"]),
    )
    assert observation.status is ToolStatus.SUCCEEDED
    assert "evidence-bound note" in str(observation.to_dict())
    assert "changed current evidence" not in str(observation.to_dict())
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1


def test_memory_owner_filter_narrows_real_records(tmp_path):
    root, _, reader, _, _, _ = _setup(tmp_path)
    result = HarnessMemoryRecallRuntime(reader).recall({
        "query": "evidence", "filters": {"owner_id": root.policy.owner_id},
    })
    assert [item.memory_id for item in result.results] == ["note-1"]


def test_empty_admitted_memory_needs_no_payload_store(tmp_path):
    from tests.framework.harness.test_ref_snapshot_store import _snapshot

    root = _snapshot()
    snapshots, _ = _store(tmp_path)
    snapshots.commit(root)
    reader = HarnessMemoryNamespaceReader(
        snapshot=root, snapshot_store=snapshots, namespace_store=None,
        execution_identity=root.execution_identity,
    )
    result = HarnessMemoryRecallRuntime(reader).recall("evidence")
    assert result.results == []
    assert result.context_block.content == ""
    assert result.diagnostics["namespace_refs"] == []


def test_namespace_payload_store_cannot_be_missing_with_memory_grant(tmp_path):
    root, _, _, _, snapshots, _ = _setup(tmp_path)
    with pytest.raises(TypeError, match="durable namespace"):
        HarnessMemoryNamespaceReader(
            snapshot=root, snapshot_store=snapshots, namespace_store=None,
            execution_identity=root.execution_identity,
        )


@pytest.mark.parametrize("module,name", [
    ("framework.tool.registry.catalog", "build_builtin_tool_registry"),
    ("infrastructure.tools.catalog", "build_builtin_tool_registry"),
    ("backend.tools", "build_business_tool_registry"),
])
def test_all_tool_catalog_ingresses_bind_the_authorized_memory_port(tmp_path, module, name):
    root, _, reader, _, _, _ = _setup(tmp_path)
    build = getattr(import_module(module), name)
    policy = MemoryPolicy(max_recall_results=1, max_context_tokens=16)
    registry = build(memory_recall=HarnessMemoryRecallRuntime(reader), memory_policy=policy, execution_identity=root.execution_identity)
    assert registry.require("memory.recall").graph_identity == root.execution_identity
    payload = registry.require("memory.recall").executor({"query": "evidence"})
    assert payload["result_count"] == 1
    assert payload["limit"] == 1
    assert payload["context_block"]["token_estimate"] <= 16
    with pytest.raises(ValueError, match="execution-bound recall"):
        build(memory_runtime=object(), execution_identity=root.execution_identity)


@pytest.mark.parametrize("policy,args", [
    (MemoryPolicy(allow_recall=False), {"query": "evidence"}),
    (MemoryPolicy(allowed_scopes=[MemoryScope.SESSION]), {"query": "evidence", "scopes": ["graph"]}),
    (MemoryPolicy(allowed_kinds=[MemoryKind.SEMANTIC]), {"query": "evidence", "kinds": ["episodic"]}),
    (MemoryPolicy(min_confidence_to_recall=0.8), {"query": "evidence", "min_score": 0.1}),
])
def test_tool_cannot_widen_bound_memory_policy(tmp_path, monkeypatch, policy, args):
    root, _, reader, namespaces, _, _ = _setup(tmp_path)
    registry = ToolRegistry()
    register_memory_tools(registry, memory_recall=HarnessMemoryRecallRuntime(reader), memory_policy=policy,
                          execution_identity=root.execution_identity)
    monkeypatch.setattr(namespaces, "read", lambda ref: pytest.fail("policy denied payload read"))
    with pytest.raises(PermissionError):
        registry.require("memory.recall").executor(args)
