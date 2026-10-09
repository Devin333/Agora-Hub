from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.agent.artifacts import (
    Artifact,
    ArtifactChecksumMismatchError,
    ArtifactIntegrityInspector,
    ArtifactManager,
    ArtifactManifest,
    ArtifactNotFoundError,
    ArtifactResolver,
    ArtifactStoreMetadataError,
    ArtifactWriteRequest,
    FilesystemArtifactStore,
    ArtifactPathError,
    LocalArtifactStore,
    compute_checksum,
)
from framework.agent.artifacts.observability import (
    ARTIFACT_CHECKSUM_MISMATCH_EVENT,
    ARTIFACT_CHECKSUM_MISSING_EVENT,
    ARTIFACT_METADATA_CORRUPT_EVENT,
    ARTIFACT_OBSERVABILITY_LOGGER,
)
from framework.agent.artifacts.stores import local as local_store_module
from framework.agent.artifacts.stores import filesystem as filesystem_store_module
from framework.agent.artifacts.stores import fs_safety as fs_safety_module
from framework.agent.artifacts.stores.fs_safety import (
    verified_atomic_write,
    verified_exclusive_file_lock,
)
from framework.agent.artifacts.stores.errors import artifact_observability_was_emitted


_MISSING = object()


def _extended_windows_path(path: Path) -> Path:
    return Path("\\\\?\\" + str(path))


def test_artifact_manager_publishes_resolves_and_deletes(tmp_path) -> None:
    manager = ArtifactManager(tmp_path)
    artifact = Artifact(
        artifact_id="a1",
        name="hello.txt",
        content_type="text/plain",
        content="hello",
    )

    ref = manager.publish(artifact)

    assert ref.artifact_id == "a1"
    assert manager.resolve(ref).content_bytes() == b"hello"
    assert [item.artifact_id for item in manager.list()] == ["a1"]

    manager.delete("a1")

    with pytest.raises(FileNotFoundError):
        manager.resolve(ref)


def test_local_store_supports_relative_configured_root(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore("artifacts")
    artifact = Artifact(
        artifact_id="a1",
        name="hello.txt",
        content_type="text/plain",
        content="hello",
    )

    ref = store.put(artifact)

    assert ref.uri == "objects/a1"
    assert store.get("a1") is not None
    assert [item.artifact_id for item in store.list()] == ["a1"]


def test_legacy_run_write_methods_reject_unsafe_paths(tmp_path) -> None:
    manager = ArtifactManager(tmp_path)

    manager.start_run("run-1")
    path = manager.write_json("run-1", "nested/payload.json", {"ok": True})

    assert path.relative_to(tmp_path).as_posix() == "run-1/nested/payload.json"
    with pytest.raises(ValueError):
        manager.write_text("run-1", "../secret.txt", "nope")


def test_run_relative_write_is_atomic_and_cleans_owned_temp_on_replace_failure(
    tmp_path,
    monkeypatch,
) -> None:
    manager = ArtifactManager(tmp_path)
    manager.start_run("run-1")
    target = manager.write_text("run-1", "payload.txt", "committed")

    def fail_replace(_source, _target) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(fs_safety_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        manager.write_text("run-1", "payload.txt", "uncommitted")

    assert target.read_text(encoding="utf-8") == "committed"
    assert list(target.parent.glob(".payload.txt.*.tmp")) == []


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path namespaces")
def test_verified_atomic_write_preserves_extended_windows_long_path_namespace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    nested = ("transcript",) * 40
    normal_target = root.joinpath("run-1", *nested, "payload.json")
    extended_target = _extended_windows_path(normal_target)

    assert len(str(normal_target)) > 260
    verified_atomic_write(
        extended_target,
        b'{"status":"ok"}',
        root=root,
        identity="run-1/transcript",
    )

    assert extended_target.read_bytes() == b'{"status":"ok"}'


def test_windows_artifact_lock_waits_for_empty_file_owner_without_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "entry.lock"
    lock_path.touch()
    original_identity = lock_path.stat()
    events: list[str] = []
    competing_owner = True
    acquired_descriptor: int | None = None

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 0

        @staticmethod
        def locking(descriptor: int, mode: int, size: int) -> None:
            nonlocal competing_owner, acquired_descriptor
            assert size == 1
            assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
            assert os.fstat(descriptor).st_size == 0
            if mode == FakeMsvcrt.LK_UNLCK:
                assert acquired_descriptor == descriptor
                events.append("release")
                acquired_descriptor = None
                return
            assert mode == FakeMsvcrt.LK_LOCK
            assert competing_owner
            # Model the existing blocking CRT call waiting for the current
            # byte-range owner to release before granting this descriptor.
            events.append("wait_for_competitor")
            competing_owner = False
            acquired_descriptor = descriptor
            events.append("acquire")

    def conflicting_initialization(_descriptor: int, _content: bytes) -> int:
        raise PermissionError("competing process owns initialization byte")

    monkeypatch.setattr(
        fs_safety_module,
        "os",
        SimpleNamespace(
            **{**vars(os), "name": "nt", "write": conflicting_initialization}
        ),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)

    with verified_exclusive_file_lock(lock_path, root=tmp_path, identity="entry"):
        assert acquired_descriptor is not None
        assert not competing_owner
        events.append("critical_section")

    assert events == ["wait_for_competitor", "acquire", "critical_section", "release"]
    assert acquired_descriptor is None
    assert lock_path.stat().st_size == 0
    assert os.path.samestat(original_identity, lock_path.stat())


def test_windows_artifact_lock_preserves_acquisition_error_and_closes_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "unavailable.lock"
    lock_path.touch()
    acquisition_error = PermissionError("byte-range lock unavailable")
    closed: list[int] = []
    attempted: list[int] = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 0

        @staticmethod
        def locking(descriptor: int, mode: int, size: int) -> None:
            assert mode == FakeMsvcrt.LK_LOCK
            assert size == 1
            assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
            attempted.append(descriptor)
            raise acquisition_error

    def close_descriptor(descriptor: int) -> None:
        closed.append(descriptor)
        os.close(descriptor)

    monkeypatch.setattr(
        fs_safety_module,
        "os",
        SimpleNamespace(**{**vars(os), "name": "nt", "close": close_descriptor}),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)

    with pytest.raises(
        ArtifactStoreMetadataError, match="artifact lock acquisition failed"
    ) as exc_info:
        with verified_exclusive_file_lock(lock_path, root=tmp_path, identity="entry"):
            pytest.fail("critical section entered without acquiring the byte-range lock")

    assert exc_info.value.__cause__ is acquisition_error
    assert len(attempted) == 1
    assert closed == attempted
    assert lock_path.stat().st_size == 0


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path namespaces")
def test_windows_extended_and_dos_paths_share_the_same_artifact_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    normal_lock_path = root / "run-1" / "_locks" / "entry.lock"
    extended_lock_path = _extended_windows_path(normal_lock_path)
    assert fs_safety_module._file_lock(normal_lock_path) is fs_safety_module._file_lock(
        extended_lock_path
    )
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    failures: list[BaseException] = []

    def hold_extended_lock() -> None:
        try:
            with verified_exclusive_file_lock(
                extended_lock_path,
                root=root,
                identity="run-1/entry",
            ):
                entered.set()
                assert release.wait(timeout=5)
        except BaseException as exc:  # assertions are surfaced below
            failures.append(exc)

    def acquire_dos_lock() -> None:
        try:
            assert entered.wait(timeout=5)
            with verified_exclusive_file_lock(
                normal_lock_path,
                root=root,
                identity="run-1/entry",
            ):
                second_entered.set()
        except BaseException as exc:  # assertions are surfaced below
            failures.append(exc)

    first = threading.Thread(target=hold_extended_lock)
    second = threading.Thread(target=acquire_dos_lock)
    first.start()
    second.start()
    assert entered.wait(timeout=5)
    assert not second_entered.wait(timeout=0.2)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert second_entered.is_set()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path namespaces")
def test_windows_concurrent_extended_transcript_directory_creation_is_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    normal_target = root / "run-1" / "transcript" / "events" / "one.json"
    extended_target = _extended_windows_path(
        root / "run-1" / "transcript" / "events" / "two.json"
    )
    start = threading.Barrier(2)
    failures: list[BaseException] = []

    def write(target: Path, content: bytes) -> None:
        try:
            start.wait(timeout=5)
            verified_atomic_write(
                target,
                content,
                root=root,
                identity=f"run-1/{target.name}",
            )
        except BaseException as exc:  # thread assertions are surfaced below
            failures.append(exc)

    first = threading.Thread(target=write, args=(normal_target, b"one"))
    second = threading.Thread(target=write, args=(extended_target, b"two"))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert normal_target.read_bytes() == b"one"
    assert extended_target.read_bytes() == b"two"


@pytest.mark.parametrize("run_id", ["../escape", "C:\\escape", "run:stream", "NUL"])
def test_manager_rejects_unsafe_run_ids_before_side_effect(tmp_path, run_id: str) -> None:
    manager = ArtifactManager(tmp_path)

    with pytest.raises(ArtifactPathError):
        manager.start_run(run_id)

    assert not tmp_path.exists() or not any(tmp_path.rglob("*"))


def test_filesystem_store_storage_compatibility(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path)

    ref = store.write(
        ArtifactWriteRequest(
            run_id="run-1",
            artifact_id="a1",
            artifact_type="report",
            scope_kind="standalone",
            content=b"{}",
            content_type="application/json",
        )
    )

    assert ref.scope_kind == "standalone"
    assert ref.path.startswith("objects/")
    assert store.read(ref) == b"{}"
    store.delete(ref)
    assert store.exists(ref) is False


def test_filesystem_store_rejects_final_symlink(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    ref = store.write(
        ArtifactWriteRequest(
            run_id="run-1",
            artifact_id="a1",
            artifact_type="report",
            scope_kind="standalone",
            content=b"trusted",
        )
    )
    path = tmp_path / "run-1" / ref.path
    target = path.with_name("target.bin")
    target.write_bytes(b"trusted")
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ArtifactStoreMetadataError, match="symlink"):
        store.read(ref)

    with pytest.raises(ArtifactStoreMetadataError, match="symlink"):
        store.exists(ref)

    with pytest.raises(ArtifactStoreMetadataError, match="symlink"):
        store.delete(ref)


def test_filesystem_store_rejects_symlinked_run_directory(tmp_path) -> None:
    store = FilesystemArtifactStore(tmp_path / "runs")
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    run_dir = store.root / "run-1"
    try:
        run_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ArtifactStoreMetadataError, match="symlink"):
        store.list("run-1")


def test_filesystem_store_rejects_opened_identity_change(
    tmp_path,
    monkeypatch,
) -> None:
    store = FilesystemArtifactStore(tmp_path)
    ref = store.write(
        ArtifactWriteRequest(
            run_id="run-1",
            artifact_id="a1",
            artifact_type="report",
            scope_kind="standalone",
            content=b"trusted",
        )
    )
    real_fstat = filesystem_store_module.os.fstat

    def changed_identity(descriptor):
        info = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=info.st_mode,
            st_dev=info.st_dev,
            st_ino=info.st_ino + 1,
        )

    monkeypatch.setattr(filesystem_store_module.os, "fstat", changed_identity)

    with pytest.raises(ArtifactStoreMetadataError, match="identity changed"):
        store.read(ref)


@pytest.mark.parametrize(
    ("artifact_id", "content_type", "content", "expected"),
    [
        ("text", "text/plain", "hello", b"hello"),
        ("json", "application/json", {"answer": 42}, b'{"answer":42}'),
        ("binary", "application/octet-stream", b"\x00\xff", b"\x00\xff"),
    ],
)
def test_local_store_verified_roundtrip(
    tmp_path,
    artifact_id: str,
    content_type: str,
    content,
    expected: bytes,
) -> None:
    store = LocalArtifactStore(tmp_path)
    artifact = Artifact(
        artifact_id=artifact_id,
        name=f"{artifact_id}.data",
        content_type=content_type,
        content=content,
        metadata={"source": "test"},
    )

    ref = store.put(artifact)
    resolved = store.get(artifact_id)
    persisted = json.loads(
        (tmp_path / ".metadata" / f"{artifact_id}.json").read_text(encoding="utf-8")
    )

    assert resolved is not None
    assert resolved.content_bytes() == expected
    assert resolved.metadata == {"source": "test"}
    assert ref.checksum == compute_checksum(expected)
    assert persisted["checksum"] == ref.checksum
    assert list(tmp_path.rglob("*.tmp")) == []


def test_local_store_returns_none_only_when_pair_is_absent(tmp_path) -> None:
    assert LocalArtifactStore(tmp_path).get("missing") is None


def test_local_store_rejects_metadata_only_pair(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"hello"))
    store.path_for("a1").unlink()

    with pytest.raises(ArtifactNotFoundError):
        store.get("a1")


def test_local_store_rejects_object_only_pair(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    path = store.path_for("a1")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"orphan")

    with pytest.raises(ArtifactStoreMetadataError):
        store.get("a1")


@pytest.mark.parametrize("target", ["object", "metadata"])
def test_local_store_get_rejects_non_regular_pair_target(tmp_path, target: str) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"hello"))
    path = (
        store.path_for("a1")
        if target == "object"
        else tmp_path / ".metadata" / "a1.json"
    )
    path.unlink()
    path.mkdir()

    with pytest.raises(
        ArtifactStoreMetadataError,
        match=rf"artifact {target} is not a regular file",
    ):
        store.get("a1")


@pytest.mark.parametrize("target", ["object", "metadata"])
def test_local_store_list_rejects_non_regular_pair_target(tmp_path, target: str) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"hello"))
    path = (
        store.path_for("a1")
        if target == "object"
        else tmp_path / ".metadata" / "a1.json"
    )
    path.unlink()
    path.mkdir()

    with pytest.raises(
        ArtifactStoreMetadataError,
        match=rf"artifact {target} is not a regular file",
    ):
        store.list()


@pytest.mark.parametrize("field", ["artifact_id", "uri"])
@pytest.mark.parametrize(
    "value",
    [_MISSING, None, "", "   ", " padded ", 7],
    ids=["missing", "null", "blank", "whitespace", "padded", "non-string"],
)
def test_local_store_list_rejects_invalid_required_persisted_field(
    tmp_path,
    field: str,
    value,
) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"hello"))
    metadata_path = tmp_path / ".metadata" / "a1.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    if value is _MISSING:
        payload.pop(field)
    else:
        payload[field] = value
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactStoreMetadataError, match=field):
        store.list()


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(lambda payload: payload.__setitem__("metadata", []), id="metadata"),
        pytest.param(lambda payload: payload.__setitem__("checksum", "invalid"), id="checksum"),
        pytest.param(lambda payload: payload.__setitem__("created_at", None), id="created-at"),
    ],
)
def test_local_store_list_rejects_corrupt_record_without_partial_result(
    tmp_path,
    mutation,
) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a0", "a0.txt", "text/plain", b"valid"))
    store.put(Artifact("z1", "z1.txt", "text/plain", b"corrupt"))
    metadata_path = tmp_path / ".metadata" / "z1.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    mutation(payload)
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactStoreMetadataError):
        store.list()


@pytest.mark.parametrize("content", ["{", "[]"])
def test_local_store_list_rejects_invalid_metadata_json_shape(
    tmp_path,
    content: str,
) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"hello"))
    (tmp_path / ".metadata" / "a1.json").write_text(content, encoding="utf-8")

    with pytest.raises(ArtifactStoreMetadataError):
        store.list()


def test_local_store_list_preserves_valid_reference_without_reading_object(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalArtifactStore(tmp_path)
    published = store.put(
        Artifact(
            "a1",
            "a1.txt",
            "text/plain",
            b"hello",
            metadata={"source": "test"},
        )
    )

    def reject_object_read(path: Path) -> bytes:
        raise AssertionError(f"list must not read object bytes: {path.name}")

    monkeypatch.setattr(Path, "read_bytes", reject_object_read)

    refs = store.list()

    assert len(refs) == 1
    assert refs[0].artifact_id == "a1"
    assert refs[0].uri == "objects/a1"
    assert refs[0].content_type == "text/plain"
    assert refs[0].checksum == published.checksum
    assert refs[0].metadata == {"source": "test"}


def test_local_store_metadata_classification_emits_once_at_store_boundary(
    tmp_path,
    caplog,
) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"hello"))
    metadata_path = tmp_path / ".metadata" / "a1.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["uri"] = None
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")
    caplog.set_level("INFO", logger=ARTIFACT_OBSERVABILITY_LOGGER)

    with pytest.raises(ArtifactStoreMetadataError):
        store.list()

    records = _artifact_event_records(caplog)
    assert [record.artifact_event_name for record in records] == [
        ARTIFACT_METADATA_CORRUPT_EVENT
    ]
    assert records[0].artifact_event_dimensions == {"store": "local"}


def test_local_store_checksum_missing_emits_once_at_store_boundary(
    tmp_path,
    caplog,
) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"legacy"))
    metadata_path = tmp_path / ".metadata" / "a1.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload.pop("checksum")
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")
    caplog.set_level("INFO", logger=ARTIFACT_OBSERVABILITY_LOGGER)

    artifact = store.get("a1")

    assert artifact is not None
    records = _artifact_event_records(caplog)
    assert [record.artifact_event_name for record in records] == [
        ARTIFACT_CHECKSUM_MISSING_EVENT
    ]
    assert records[0].artifact_event_dimensions == {"store": "local"}


def test_local_store_checksum_mismatch_emits_once_at_shared_verifier(
    tmp_path,
    caplog,
) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"trusted"))
    store.path_for("a1").write_bytes(b"tampered-secret-content")
    caplog.set_level("INFO", logger=ARTIFACT_OBSERVABILITY_LOGGER)

    with pytest.raises(ArtifactChecksumMismatchError):
        store.get("a1")

    records = _artifact_event_records(caplog)
    assert [record.artifact_event_name for record in records] == [
        ARTIFACT_CHECKSUM_MISMATCH_EVENT
    ]
    assert records[0].artifact_event_dimensions == {
        "store": "local",
        "operation": "read",
    }
    assert "tampered-secret-content" not in caplog.text


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda payload: payload.__setitem__("artifact_id", "other"), ArtifactStoreMetadataError),
        (lambda payload: payload.__setitem__("uri", "objects/other"), ArtifactStoreMetadataError),
        (lambda payload: payload.__setitem__("name", None), ArtifactStoreMetadataError),
        (lambda payload: payload.__setitem__("content_type", []), ArtifactStoreMetadataError),
        (lambda payload: payload.__setitem__("metadata", []), ArtifactStoreMetadataError),
        (lambda payload: payload.__setitem__("created_at", "not-a-time"), ArtifactStoreMetadataError),
        (lambda payload: payload.__setitem__("checksum", "invalid"), ArtifactStoreMetadataError),
        (lambda payload: payload.__setitem__("checksum", "A" * 64), ArtifactStoreMetadataError),
        (lambda payload: payload.__setitem__("checksum", None), ArtifactStoreMetadataError),
        (lambda payload: payload.__setitem__("checksum", "0" * 64), ArtifactChecksumMismatchError),
    ],
)
def test_local_store_rejects_corrupt_metadata(tmp_path, mutate, error) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"hello"))
    metadata_path = tmp_path / ".metadata" / "a1.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    mutate(payload)
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(error):
        store.get("a1")


def test_local_store_rejects_malformed_metadata_json(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"hello"))
    (tmp_path / ".metadata" / "a1.json").write_text("{", encoding="utf-8")

    with pytest.raises(ArtifactStoreMetadataError):
        store.get("a1")


def test_local_store_rejects_tampered_object_through_all_resolvers(tmp_path) -> None:
    manager = ArtifactManager(tmp_path)
    artifact = Artifact("a1", "a1.txt", "text/plain", b"trusted")
    ref = manager.publish(artifact)
    manager.store.path_for("a1").write_bytes(b"tampered")

    with pytest.raises(ArtifactChecksumMismatchError):
        manager.store.get("a1")
    with pytest.raises(ArtifactChecksumMismatchError):
        manager.resolve(ref)
    with pytest.raises(ArtifactChecksumMismatchError):
        ArtifactResolver(manager.store).resolve(ref)


def test_local_store_marks_legacy_missing_checksum_without_rewriting(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    ref = store.put(
        Artifact(
            "a1",
            "a1.txt",
            "text/plain",
            b"legacy",
            metadata={"_artifact_integrity": "forged", "source": "legacy"},
        )
    )
    metadata_path = tmp_path / ".metadata" / "a1.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload["metadata"] == {"source": "legacy"}
    assert ref.metadata == {"source": "legacy"}
    payload.pop("checksum")
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")
    persisted_before = metadata_path.read_bytes()

    artifact = store.get("a1")

    assert artifact is not None
    assert artifact.content_bytes() == b"legacy"
    assert artifact.metadata == {
        "_artifact_integrity": "checksum_missing",
        "source": "legacy",
    }
    assert artifact_observability_was_emitted(artifact, "checksum_missing")
    assert all("observability" not in key for key in artifact.metadata)
    assert metadata_path.read_bytes() == persisted_before


def test_local_store_legacy_missing_checksum_fails_integrity_inspection(tmp_path) -> None:
    store = LocalArtifactStore(tmp_path)
    ref = store.put(Artifact("a1", "a1.txt", "text/plain", b"legacy"))
    metadata_path = tmp_path / ".metadata" / "a1.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload.pop("checksum")
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    report = ArtifactIntegrityInspector(store).inspect(
        ArtifactManifest(run_id="run-1", artifacts=[ref])
    )

    assert report.valid is False
    assert report.checked_count == 1
    assert [issue.reason for issue in report.issues] == ["checksum_missing"]


def test_local_store_metadata_replace_failure_preserves_commit_marker_and_cleans_temps(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"old"))
    metadata_path = tmp_path / ".metadata" / "a1.json"
    committed_metadata = metadata_path.read_bytes()
    real_replace = os.replace
    replace_targets = []

    def fail_metadata_replace(source, destination) -> None:
        replace_targets.append(destination)
        if destination == metadata_path:
            raise OSError("metadata commit failed")
        real_replace(source, destination)

    monkeypatch.setattr(local_store_module.os, "replace", fail_metadata_replace)

    with pytest.raises(OSError, match="metadata commit failed"):
        store.put(Artifact("a1", "a1.txt", "text/plain", b"new"))

    assert replace_targets == [store.path_for("a1"), metadata_path]
    assert metadata_path.read_bytes() == committed_metadata
    assert list(tmp_path.rglob("*.tmp")) == []
    with pytest.raises(ArtifactChecksumMismatchError):
        store.get("a1")


def test_local_store_object_replace_failure_preserves_prior_pair_and_cleans_temps(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalArtifactStore(tmp_path)
    store.put(Artifact("a1", "a1.txt", "text/plain", b"old"))
    object_path = store.path_for("a1")
    metadata_path = tmp_path / ".metadata" / "a1.json"
    committed_object = object_path.read_bytes()
    committed_metadata = metadata_path.read_bytes()
    real_replace = os.replace
    replace_targets = []

    def fail_object_replace(source, destination) -> None:
        replace_targets.append(destination)
        if destination == object_path:
            raise OSError("object commit failed")
        real_replace(source, destination)

    monkeypatch.setattr(local_store_module.os, "replace", fail_object_replace)

    with pytest.raises(OSError, match="object commit failed"):
        store.put(Artifact("a1", "a1.txt", "text/plain", b"new"))

    assert replace_targets == [object_path]
    assert object_path.read_bytes() == committed_object
    assert metadata_path.read_bytes() == committed_metadata
    assert list(tmp_path.rglob("*.tmp")) == []
    restored = store.get("a1")
    assert restored is not None
    assert restored.content_bytes() == b"old"


def test_local_store_temp_write_failure_creates_no_committed_pair(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalArtifactStore(tmp_path)
    real_write = local_store_module._write_owned_temp
    calls = 0

    def fail_second_temp(target, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("metadata temp write failed")
        return real_write(target, content)

    monkeypatch.setattr(local_store_module, "_write_owned_temp", fail_second_temp)

    with pytest.raises(OSError, match="metadata temp write failed"):
        store.put(Artifact("a1", "a1.txt", "text/plain", b"new"))

    assert store.get("a1") is None
    assert list(tmp_path.rglob("*.tmp")) == []


def test_local_store_new_metadata_commit_failure_leaves_classified_orphan(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalArtifactStore(tmp_path)
    metadata_path = tmp_path / ".metadata" / "a1.json"
    real_replace = os.replace

    def fail_metadata_replace(source, destination) -> None:
        if destination == metadata_path:
            raise OSError("metadata commit failed")
        real_replace(source, destination)

    monkeypatch.setattr(local_store_module.os, "replace", fail_metadata_replace)

    with pytest.raises(OSError, match="metadata commit failed"):
        store.put(Artifact("a1", "a1.txt", "text/plain", b"new"))

    assert store.path_for("a1").read_bytes() == b"new"
    assert metadata_path.exists() is False
    assert list(tmp_path.rglob("*.tmp")) == []
    with pytest.raises(ArtifactStoreMetadataError):
        store.get("a1")


def _artifact_event_records(caplog):
    return [
        record
        for record in caplog.records
        if record.name == ARTIFACT_OBSERVABILITY_LOGGER
    ]
