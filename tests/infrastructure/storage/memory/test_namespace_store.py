from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from framework.memory.models import MemoryKind, MemoryRecord, MemoryScope
from framework.memory.namespace import (
    MemoryNamespaceDescriptorPort,
    MemoryNamespaceError,
    MemoryNamespaceRevision,
    MemoryNamespaceStorePort,
    namespace_bytes,
)
from infrastructure.storage.memory import namespace as storage_module
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore


FIXED_TIME = datetime(2026, 9, 8, 1, 2, 3, tzinfo=UTC)


def _record(
    memory_id: str = "memory-1",
    *,
    namespace: str = "research.analysis",
    tenant_id: str = "tenant-1",
    owner_id: str = "owner-1",
    content: str = "alpha",
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope.AGENT,
        summary=f"summary for {memory_id}",
        content=content,
        actor=owner_id,
        namespace=namespace,
        tenant_id=tenant_id,
        created_at=FIXED_TIME,
        metadata={"source": "trusted-writer"},
        refs={"evidence_id": f"evidence-{memory_id}"},
    )


def _revision(
    *,
    namespace: str = "research.analysis",
    tenant_id: str = "tenant-1",
    owner_id: str = "owner-1",
    shared_read_only: bool = True,
    records: tuple[MemoryRecord, ...] | None = None,
) -> MemoryNamespaceRevision:
    return MemoryNamespaceRevision.create(
        namespace=namespace,
        tenant_id=tenant_id,
        owner_id=owner_id,
        shared_read_only=shared_read_only,
        records=records or (
            _record(
                namespace=namespace,
                tenant_id=tenant_id,
                owner_id=owner_id,
            ),
        ),
    )


def _paths(root: Path, revision: MemoryNamespaceRevision) -> tuple[Path, Path]:
    digest = revision.descriptor.revision
    return root / f"{digest}.json", root / f"{digest}.meta"


def test_store_roundtrip_reopen_and_protocols(tmp_path: Path) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)

    assert isinstance(store, MemoryNamespaceDescriptorPort)
    assert isinstance(store, MemoryNamespaceStorePort)
    assert store.commit(revision) == revision.descriptor
    assert store.describe(revision.descriptor.exact_ref) == revision.descriptor
    assert store.read(revision.descriptor.exact_ref) == revision
    assert store.read(revision.descriptor.exact_ref).records() == revision.records()

    reopened = FilesystemMemoryNamespaceStore(tmp_path)
    assert reopened.describe(revision.descriptor.exact_ref) == revision.descriptor
    assert reopened.read(revision.descriptor.exact_ref) == revision
    assert revision.descriptor.source_checksum == revision.descriptor.exact_ref.replace(
        "memory-namespace://", "sha256:"
    )


def test_describe_is_metadata_only_and_never_opens_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)
    store.commit(revision)
    original_open = storage_module.os.open

    def guarded_open(path, *args, **kwargs):
        if str(path).endswith(".json"):
            raise AssertionError("describe must not open namespace payload")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "open", guarded_open)

    assert store.describe(revision.descriptor.exact_ref) == revision.descriptor


def test_payload_corruption_is_detected_only_when_payload_is_read(tmp_path: Path) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)
    store.commit(revision)
    payload_path, _ = _paths(tmp_path, revision)
    content = payload_path.read_bytes()
    assert b"alpha" in content
    payload_path.write_bytes(content.replace(b"alpha", b"omega", 1))

    assert store.describe(revision.descriptor.exact_ref) == revision.descriptor
    with pytest.raises(MemoryNamespaceError) as error:
        store.read(revision.descriptor.exact_ref)
    assert error.value.code == "MEMORY_NAMESPACE_CORRUPT"


def test_metadata_tampering_is_rejected_before_payload_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)
    store.commit(revision)
    _, metadata_path = _paths(tmp_path, revision)
    content = metadata_path.read_bytes()
    assert b"tenant-1" in content
    metadata_path.write_bytes(content.replace(b"tenant-1", b"tenant-X", 1))
    original_open = storage_module.os.open

    def guarded_open(path, *args, **kwargs):
        if str(path).endswith(".json"):
            raise AssertionError("metadata rejection must precede payload read")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "open", guarded_open)
    with pytest.raises(MemoryNamespaceError) as error:
        store.describe(revision.descriptor.exact_ref)
    assert error.value.code == "MEMORY_NAMESPACE_CORRUPT"


@pytest.mark.parametrize("missing_suffix", (".meta", ".json"))
def test_one_sided_revision_fails_closed_across_reopen(
    tmp_path: Path,
    missing_suffix: str,
) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)
    store.commit(revision)
    payload_path, metadata_path = _paths(tmp_path, revision)
    {".json": payload_path, ".meta": metadata_path}[missing_suffix].unlink()

    reopened = FilesystemMemoryNamespaceStore(tmp_path)
    with pytest.raises(MemoryNamespaceError) as describe_error:
        reopened.describe(revision.descriptor.exact_ref)
    assert describe_error.value.code == "MEMORY_NAMESPACE_INCOMPLETE"
    with pytest.raises(MemoryNamespaceError) as read_error:
        reopened.read(revision.descriptor.exact_ref)
    assert read_error.value.code == "MEMORY_NAMESPACE_INCOMPLETE"


def test_crash_after_metadata_create_remains_incomplete_and_is_not_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)
    atomic_create = storage_module.verified_atomic_create
    calls = 0

    def fail_before_payload(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected crash before payload commit")
        return atomic_create(*args, **kwargs)

    monkeypatch.setattr(storage_module, "verified_atomic_create", fail_before_payload)
    with pytest.raises(MemoryNamespaceError) as error:
        store.commit(revision)
    assert error.value.code == "MEMORY_NAMESPACE_STORAGE_UNAVAILABLE"
    assert len(tuple(tmp_path.glob("*.meta"))) == 1
    assert not tuple(tmp_path.glob("*.json"))

    reopened = FilesystemMemoryNamespaceStore(tmp_path)
    with pytest.raises(MemoryNamespaceError) as retry_error:
        reopened.commit(revision)
    assert retry_error.value.code == "MEMORY_NAMESPACE_INCOMPLETE"


def test_exact_ref_has_no_logical_or_latest_fallback(tmp_path: Path) -> None:
    store = FilesystemMemoryNamespaceStore(tmp_path)

    for ref in ("research.analysis", "memory-namespace://latest"):
        with pytest.raises(MemoryNamespaceError) as error:
            store.describe(ref)
        assert error.value.code == "MEMORY_NAMESPACE_INVALID"
    assert not tuple(tmp_path.iterdir())


def test_namespace_tenant_owner_and_sharing_are_content_addressed_isolates(
    tmp_path: Path,
) -> None:
    revisions = (
        _revision(),
        _revision(namespace="research.private"),
        _revision(tenant_id="tenant-2"),
        _revision(owner_id="owner-2"),
        _revision(shared_read_only=False),
    )
    store = FilesystemMemoryNamespaceStore(tmp_path)

    descriptors = tuple(store.commit(item) for item in revisions)

    assert len({item.exact_ref for item in descriptors}) == len(revisions)
    assert tuple(store.describe(item.exact_ref) for item in descriptors) == descriptors
    assert len(tuple(tmp_path.glob("*.meta"))) == len(revisions)
    assert len(tuple(tmp_path.glob("*.json"))) == len(revisions)


def test_existing_exact_ref_with_different_metadata_is_a_conflict(tmp_path: Path) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)
    store.commit(revision)
    _, metadata_path = _paths(tmp_path, revision)
    conflicting = replace(revision.descriptor, owner_id="other-owner")
    metadata_path.write_bytes(namespace_bytes(conflicting.to_dict()))

    with pytest.raises(MemoryNamespaceError) as error:
        store.commit(revision)
    assert error.value.code == "MEMORY_NAMESPACE_CONFLICT"


def test_identical_threaded_commits_reuse_one_immutable_pair(tmp_path: Path) -> None:
    revision = _revision()
    stores = tuple(FilesystemMemoryNamespaceStore(tmp_path) for _ in range(8))

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        descriptors = tuple(pool.map(lambda item: item.commit(revision), stores))

    assert descriptors == (revision.descriptor,) * len(stores)
    assert len(tuple(tmp_path.glob("*.meta"))) == 1
    assert len(tuple(tmp_path.glob("*.json"))) == 1


def test_identical_cross_process_commits_reuse_one_immutable_pair(
    tmp_path: Path,
) -> None:
    script = """
import sys
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from tests.infrastructure.storage.memory.test_namespace_store import _revision

revision = _revision()
descriptor = FilesystemMemoryNamespaceStore(sys.argv[1]).commit(revision)
print(descriptor.exact_ref)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[4])
    processes = tuple(
        subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=Path(__file__).parents[4],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    )
    outputs = tuple(process.communicate(timeout=20) for process in processes)
    exact_ref = _revision().descriptor.exact_ref

    for process, (stdout, stderr) in zip(processes, outputs, strict=True):
        assert process.returncode == 0, stderr
        assert stdout.strip() == exact_ref
    assert len(tuple(tmp_path.glob("*.meta"))) == 1
    assert len(tuple(tmp_path.glob("*.json"))) == 1


def test_record_and_payload_limits_reject_before_commit(tmp_path: Path) -> None:
    records = (
        _record("memory-1"),
        _record("memory-2", content="beta"),
    )
    revision = _revision(records=records)
    record_limited = FilesystemMemoryNamespaceStore(tmp_path, max_records=1)
    with pytest.raises(MemoryNamespaceError) as record_error:
        record_limited.commit(revision)
    assert record_error.value.code == "MEMORY_NAMESPACE_LIMIT_EXCEEDED"

    payload_limited = FilesystemMemoryNamespaceStore(
        tmp_path,
        max_payload_bytes=revision.descriptor.payload_size_bytes - 1,
    )
    with pytest.raises(MemoryNamespaceError) as payload_error:
        payload_limited.commit(revision)
    assert payload_error.value.code == "MEMORY_NAMESPACE_LIMIT_EXCEEDED"

    metadata_limited = FilesystemMemoryNamespaceStore(tmp_path, max_metadata_bytes=1)
    with pytest.raises(MemoryNamespaceError) as metadata_error:
        metadata_limited.commit(revision)
    assert metadata_error.value.code == "MEMORY_NAMESPACE_LIMIT_EXCEEDED"
    assert not tuple(tmp_path.glob("*.meta"))
    assert not tuple(tmp_path.glob("*.json"))


def test_missing_revision_and_invalid_limits_are_typed(tmp_path: Path) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)
    assert store.describe(revision.descriptor.exact_ref) is None
    with pytest.raises(MemoryNamespaceError) as error:
        store.read(revision.descriptor.exact_ref)
    assert error.value.code == "MEMORY_NAMESPACE_MISSING"

    for field in ("max_payload_bytes", "max_metadata_bytes", "max_records"):
        with pytest.raises(ValueError):
            FilesystemMemoryNamespaceStore(tmp_path, **{field: 0})


def test_noncanonical_metadata_is_rejected(tmp_path: Path) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)
    store.commit(revision)
    _, metadata_path = _paths(tmp_path, revision)
    document = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata_path.write_text(
        json.dumps(document, indent=2),
        encoding="utf-8",
        newline="",
    )

    with pytest.raises(MemoryNamespaceError) as error:
        store.describe(revision.descriptor.exact_ref)
    assert error.value.code == "MEMORY_NAMESPACE_CORRUPT"


def test_hardlinked_payload_is_rejected(tmp_path: Path) -> None:
    revision = _revision()
    store = FilesystemMemoryNamespaceStore(tmp_path)
    store.commit(revision)
    payload_path, _ = _paths(tmp_path, revision)
    os.link(payload_path, tmp_path / "payload-alias")

    with pytest.raises(MemoryNamespaceError) as error:
        store.describe(revision.descriptor.exact_ref)
    assert error.value.code == "MEMORY_NAMESPACE_CORRUPT"


def test_linked_metadata_or_windows_junction_root_is_rejected(tmp_path: Path) -> None:
    revision = _revision()
    namespace_root = tmp_path / "namespace-data"
    store = FilesystemMemoryNamespaceStore(namespace_root)
    store.commit(revision)
    _, metadata_path = _paths(namespace_root, revision)
    backup = tmp_path / "metadata-backup"
    metadata_path.replace(backup)
    try:
        metadata_path.symlink_to(backup)
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 1314:
            raise
        # Directory junctions exercise the actual Windows reparse boundary
        # without requiring the symbolic-link privilege.
        alias = tmp_path / "namespace-alias"
        environment = os.environ.copy()
        environment["MEMORY_TEST_LINK_PATH"] = str(alias)
        environment["MEMORY_TEST_LINK_TARGET"] = str(namespace_root)
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "New-Item -ItemType Junction -Path $env:MEMORY_TEST_LINK_PATH "
             "-Target $env:MEMORY_TEST_LINK_TARGET -ErrorAction Stop | Out-Null"],
            env=environment, check=True, capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        with pytest.raises(MemoryNamespaceError) as error:
            FilesystemMemoryNamespaceStore(alias)
        assert error.value.code == "MEMORY_NAMESPACE_STORAGE_UNAVAILABLE"
        return

    with pytest.raises(MemoryNamespaceError) as error:
        store.describe(revision.descriptor.exact_ref)
    assert error.value.code == "MEMORY_NAMESPACE_CORRUPT"
