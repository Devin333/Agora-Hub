from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import subprocess

import pytest

from framework.agent.artifacts.models import ArtifactWriteRequest
from framework.agent.artifacts.paths import artifact_path_key
from framework.agent.artifacts.stores.errors import ArtifactStoreMetadataError
from framework.agent.artifacts.stores.filesystem import FilesystemArtifactStore
from framework.agent.artifacts.stores.fs_safety import (
    verified_atomic_create,
    verified_atomic_write,
)
from framework.shared.windows_paths import storage_io_path
from infrastructure.storage.artifacts.local_json import (
    ArtifactIndexNotFoundError,
    LocalJsonArtifactIndexStore,
)


def _ordinary_deep_root(tmp_path: Path, label: str) -> Path:
    root = tmp_path.joinpath(
        *(f"{label}-{index}-{'x' * 48}" for index in range(5)),
    )
    assert len(str(root)) > 260
    assert not str(root).startswith(chr(92) * 2 + "?" + chr(92))
    return root


def test_filesystem_artifact_store_round_trips_under_ordinary_deep_root(
    tmp_path: Path,
) -> None:
    root = _ordinary_deep_root(tmp_path, "artifact-root")
    store = FilesystemArtifactStore(root)
    digest = "a" * 64
    request = ArtifactWriteRequest(
        run_id="test-agent-loop-3a2ed95d67cf4ff3b07b65e103bfd2e5",
        artifact_id=f"ref_authority_{digest}",
        artifact_type="ref_authority_snapshot",
        relative_path=f"harness/ref-authority/{digest}.json",
        content=b'{"phase":"VERIFY","status":"passed"}',
        content_type="application/json",
        metadata={"owner_id": "owner-1"},
    )

    ref = store.write(request)
    target = root / ref.run_id / Path(ref.path)

    assert len(str(target)) > 260
    assert len(str(target.parent)) > 260
    assert ref.path == f"harness/ref-authority/{digest}.json"
    assert chr(92) * 2 + "?" + chr(92) not in ref.path
    assert store.exists(ref)
    assert store.read(ref) == request.content
    assert store.checksum(ref) == ref.checksum
    assert store.list(ref.run_id) == [ref.path]
    assert FilesystemArtifactStore(root).read(ref) == request.content
    assert artifact_path_key(storage_io_path(target)) == artifact_path_key(target)

    assert store.write(request) == ref
    with pytest.raises(ArtifactStoreMetadataError, match="identity conflict"):
        store.write(replace(request, content=b"changed"))

    store.delete(ref)
    assert not store.exists(ref)
    assert store.list(ref.run_id) == []


def test_atomic_create_replace_and_failure_cleanup_use_long_path_io_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _ordinary_deep_root(tmp_path, "atomic-root")
    target = root / "run-1" / "immutable" / f"{'b' * 64}.json"

    assert verified_atomic_create(target, b"first", root=root, identity="immutable")
    assert not verified_atomic_create(target, b"second", root=root, identity="immutable")
    assert storage_io_path(target).read_bytes() == b"first"

    verified_atomic_write(target, b"replaced", root=root, identity="replace")
    assert storage_io_path(target).read_bytes() == b"replaced"

    original_replace = os.replace

    def fail_replace(source, destination, *args, **kwargs):
        if os.name == "nt":
            assert artifact_path_key(Path(destination)) == artifact_path_key(
                storage_io_path(target)
            )
            assert artifact_path_key(Path(source).parent) == artifact_path_key(
                storage_io_path(target.parent)
            )
        raise OSError("injected long-path replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected long-path replace failure"):
        verified_atomic_write(target, b"uncommitted", root=root, identity="failure")
    monkeypatch.setattr(os, "replace", original_replace)

    assert storage_io_path(target).read_bytes() == b"replaced"
    assert list(storage_io_path(target.parent).glob(".t.*.tmp")) == []
    assert list(storage_io_path(target.parent).glob(".g.*.tmp")) == []


def test_local_json_artifact_index_crud_under_second_ordinary_deep_root(
    tmp_path: Path,
) -> None:
    artifact_root = _ordinary_deep_root(tmp_path, "payload-root")
    artifact_store = FilesystemArtifactStore(artifact_root)
    ref = artifact_store.write(
        ArtifactWriteRequest(
            run_id="run-index-long-path",
            artifact_id="owned-ref",
            artifact_type="report",
            relative_path="reports/owned-ref.json",
            content=b"{}",
            content_type="application/json",
            metadata={"owner_id": "owner-1"},
        )
    )
    index_root = _ordinary_deep_root(tmp_path, "index-root")
    index = LocalJsonArtifactIndexStore(index_root)

    record_path = index.index_artifact(ref)

    assert len(str(record_path)) > 260
    assert not str(record_path).startswith(chr(92) * 2 + "?" + chr(92))
    assert index.get_artifact(ref) == ref
    assert index.get_artifact(ref.run_id, ref.artifact_id) == ref
    assert index.list_by_run(ref.run_id) == [ref]
    assert index.list_by_type("report", run_id=ref.run_id) == [ref]
    assert index.list_all() == [ref]
    assert LocalJsonArtifactIndexStore(index_root).get_artifact(ref) == ref

    index.delete_artifact(ref)
    assert index.list_by_run(ref.run_id) == []
    assert index.list_all() == []
    with pytest.raises(ArtifactIndexNotFoundError):
        index.get_artifact(ref)



def test_list_rejects_junction_before_descending_into_external_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "list-root"
    outside = tmp_path / "list-outside"
    run_dir = root / "run-1"
    storage_io_path(run_dir).mkdir(parents=True)
    outside.mkdir()
    external_file = outside / "must-not-be-listed.json"
    external_file.write_bytes(b"outside")
    redirected = run_dir / "redirected"

    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(redirected), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr or created.stdout
    else:
        redirected.symlink_to(outside, target_is_directory=True)

    outside_enumerations = 0
    real_iterdir = Path.iterdir

    def tracked_iterdir(directory: Path):
        nonlocal outside_enumerations
        if artifact_path_key(storage_io_path(directory)) == artifact_path_key(
            storage_io_path(outside)
        ):
            outside_enumerations += 1
        return real_iterdir(directory)

    monkeypatch.setattr(Path, "iterdir", tracked_iterdir)
    try:
        with pytest.raises(ArtifactStoreMetadataError, match="symlink|junction|reparse"):
            FilesystemArtifactStore(root).list("run-1")
        assert outside_enumerations == 0
        assert external_file.read_bytes() == b"outside"
    finally:
        if os.name == "nt" and storage_io_path(redirected).exists():
            os.rmdir(storage_io_path(redirected))


def test_real_ancestor_junction_or_symlink_is_rejected_before_target_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifact-root"
    outside = tmp_path / "outside"
    storage_io_path(root / "run-1").mkdir(parents=True)
    outside.mkdir()
    redirected = root / "run-1" / "redirected"

    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(redirected), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr or created.stdout
    else:
        redirected.symlink_to(outside, target_is_directory=True)

    target = redirected.joinpath(*(("nested",) * 30), "payload.json")
    outside_target = outside.joinpath(*(("nested",) * 30), "payload.json")
    try:
        with pytest.raises(ArtifactStoreMetadataError, match="symlink|junction|reparse"):
            verified_atomic_write(target, b"blocked", root=root, identity="redirected")
        assert not storage_io_path(outside_target).exists()
    finally:
        if os.name == "nt" and storage_io_path(redirected).exists():
            os.rmdir(storage_io_path(redirected))
