from __future__ import annotations

import hashlib
import os
import socket
import subprocess
from pathlib import Path

import numpy as np
import pytest

from backend.research.application.qasper_assets import QasperAssetService
from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.qasper_retrieval import OfflineEmbeddingCache, QasperOfflineCacheError
from infrastructure.research.qasper_evaluation import FilesystemQasperAssets


def _assets(dataset_path, cache_path, *, repository_root, **kwargs):
    return QasperAssetService(
        FilesystemQasperAssets(dataset_path, cache_path, repository_root=repository_root), **kwargs,
    )


def observe_code_identity(root):
    return _assets(None, None, repository_root=root).code_identity()

def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    for name in ("backend", "framework", "infrastructure", "interfaces"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "module.py").write_text(f"VALUE = '{name}'\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-qm", "fixture")
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("offline evaluation must never contact an embedding provider or the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def _chunk(chunk_id: str = "c1", content: str = "cached body", paper_id: str = "p1") -> PaperChunk:
    return PaperChunk(chunk_id=chunk_id, paper_id=paper_id, parse_source="latex", chunk_type="paragraph",
                      content=content, section_title="Method", metadata={})


def _cache(path: Path, texts: tuple[str, ...]) -> Path:
    np.savez(path, keys=np.asarray([OfflineEmbeddingCache.key(text) for text in texts], dtype=str),
             vectors=np.ones((len(texts), 2), dtype=np.float32))
    return path


def test_sparse_inputs_ignore_even_invalid_cache_and_index_by_paper(tmp_path: Path) -> None:
    assets = _assets(None, tmp_path / "nonexistent.npz", repository_root=tmp_path)
    chunks = [_chunk(), _chunk("other", paper_id="p2"), _chunk("c2", "another body")]
    first = assets.retrieval_inputs(chunks, dense=False, field=False)
    assert assets.retrieval_inputs(chunks, dense=False, field=False) is first
    assert first.chunk_store.list_chunks("p1") == [chunks[0], chunks[2]]
    assert first.chunk_store.list_chunks("missing") == []
    assert first.chunk_store.get_chunk("c2") == chunks[2]
    returned = first.chunk_store.list_chunks("p1")
    returned.clear()
    assert len(first.chunk_store.list_chunks("p1")) == 2
    with pytest.raises(ValueError, match="duplicate"):
        assets.retrieval_inputs([chunks[0], chunks[0]], dense=False, field=False)


def test_inputs_do_not_reuse_stale_content_or_metadata_for_same_ids(tmp_path: Path) -> None:
    assets = _assets(None, None, repository_root=tmp_path)
    chunk = _chunk()
    first = assets.retrieval_inputs([chunk], dense=False, field=False)
    changed = chunk.model_copy(update={"content": "changed", "metadata": {"source": "new"}})
    second = assets.retrieval_inputs([changed], dense=False, field=False)
    assert second is not first
    assert second.chunk_store.get_chunk(chunk.chunk_id).content == "changed"
    changed.metadata["source"] = "updated"
    third = assets.retrieval_inputs([changed], dense=False, field=False)
    assert third is not second
    assert second.chunk_store.get_chunk(chunk.chunk_id).metadata == {"source": "new"}


@pytest.mark.parametrize("dense,field", [(True, False), (False, True), (True, True)])
def test_missing_chunk_or_field_embedding_fails_offline(tmp_path: Path, dense: bool, field: bool) -> None:
    path = _cache(tmp_path / "cache.npz", ())
    assets = _assets(None, path, repository_root=tmp_path)
    with pytest.raises(QasperOfflineCacheError, match="never calls an embedding provider"):
        assets.retrieval_inputs([_chunk()], dense=dense, field=field)


def test_missing_query_embedding_fails_offline_in_dense_and_field_search(tmp_path: Path) -> None:
    path = _cache(tmp_path / "cache.npz", ("cached body", "Method"))
    assets = _assets(None, path, repository_root=tmp_path)
    inputs = assets.retrieval_inputs([_chunk()], dense=True, field=True)
    with pytest.raises(QasperOfflineCacheError, match="never calls an embedding provider"):
        inputs.chunk_store.search_with_scores("p1", "uncached question")
    with pytest.raises(QasperOfflineCacheError, match="never calls an embedding provider"):
        inputs.field_index.search_field_vectors("p1", "uncached question")
    assert inputs.chunk_store.search_with_scores("p1", "cached body")[0][0].chunk_id == "c1"
    assert inputs.field_index.search_field_vectors("p1", "cached body")[0].chunk_id == "c1"


def test_channel_variants_reuse_dense_store_and_cache_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _cache(tmp_path / "cache.npz", ("cached body", "Method"))
    assets = _assets(None, path, repository_root=tmp_path)
    load = FilesystemQasperAssets.open_cache
    calls = []

    def counted_load(reader):
        calls.append(reader.cache_path)
        return load(reader)

    monkeypatch.setattr(FilesystemQasperAssets, "open_cache", counted_load)
    plain = assets.retrieval_inputs([_chunk()], dense=True, field=False)
    combined = assets.retrieval_inputs([_chunk()], dense=True, field=True)
    assert plain.chunk_store is combined.chunk_store
    assert assets.retrieval_inputs([_chunk()], dense=True, field=True) is combined
    assert calls == [path]


def test_repeated_retrieval_does_not_serialize_or_rebuild_frozen_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(None, None, repository_root=tmp_path)
    chunks = [_chunk()]
    first = assets.retrieval_inputs(chunks, dense=False, field=False)

    def unexpected_serialization(*args, **kwargs):
        pytest.fail("unchanged corpus must reuse its cached signature")

    monkeypatch.setattr(PaperChunk, "model_dump", unexpected_serialization)
    assert assets.retrieval_inputs(chunks, dense=False, field=False) is first


def test_cache_identity_hashes_file_bytes_not_semantic_vectors(tmp_path: Path) -> None:
    path = _cache(tmp_path / "cache.npz", ("query",))
    assets = _assets(None, path, repository_root=tmp_path,
                                    embedding_model_id="custom/model", embedding_model_version="v1")
    identity = assets.cache_identity()
    assert identity.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert identity.dimension == 2
    assert (identity.model_id, identity.model_version) == ("custom/model", "v1")
    # Same vectors, different archive bytes: identity must follow the actual file.
    np.savez_compressed(path, keys=np.asarray([OfflineEmbeddingCache.key("query")]),
                        vectors=np.ones((1, 2), dtype=np.float32))
    reloaded = _assets(None, path, repository_root=tmp_path).cache_identity()
    assert reloaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert reloaded.sha256 != identity.sha256
    assert assets.cache_identity() == identity  # The loaded snapshot retains its provenance.


def test_explicit_missing_paths_never_fall_back_to_ambient_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QASPER_DATASET_PATH", str(tmp_path / "dataset.json"))
    monkeypatch.setenv("QASPER_EMBEDDING_CACHE_PATH", str(_cache(tmp_path / "cache.npz", ("query",))))
    assets = _assets(None, None, repository_root=tmp_path)
    with pytest.raises(ValueError, match="--dataset-path"):
        assets.read_assets()
    with pytest.raises(ValueError, match="--cache-path"):
        assets.cache_identity()
    with pytest.raises(QasperOfflineCacheError, match="does not exist"):
        _assets(None, tmp_path / "missing.npz", repository_root=tmp_path).cache_identity()
    (tmp_path / "broken.npz").write_bytes(b"not an embedding archive")
    with pytest.raises(QasperOfflineCacheError, match="invalid embedding cache"):
        _assets(None, tmp_path / "broken.npz", repository_root=tmp_path).cache_identity()


def test_explicit_input_paths_are_pinned_before_cwd_changes(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = repository / "dataset.json"
    dataset.write_text('{"papers": {}}', encoding="utf-8")
    cache_path = _cache(repository / "cache.npz", ("query",))
    monkeypatch.chdir(repository)
    assets = _assets(Path("dataset.json"), Path("cache.npz"), repository_root=Path("."))
    monkeypatch.chdir(repository.parent)
    assert assets._reader.dataset_path == dataset.resolve()
    assert assets.read_assets().raw_dataset == {"papers": {}}
    assert assets.read_assets().dataset_sha256 == hashlib.sha256(dataset.read_bytes()).hexdigest()
    assert assets.cache_identity().sha256 == hashlib.sha256(cache_path.read_bytes()).hexdigest()
    assert assets.read_assets() is assets.read_assets()


@pytest.mark.parametrize("name", ["backend", "framework", "infrastructure", "interfaces"])
def test_code_identity_covers_every_runtime_root(repository: Path, name: str) -> None:
    clean = observe_code_identity(repository)
    assert not clean.dirty
    assert not clean.mutable_patch_fingerprints
    (repository / name / "module.py").write_text("VALUE = 'changed'\n", encoding="utf-8")
    dirty = observe_code_identity(repository)
    assert dirty.dirty
    assert dirty.git_revision == clean.git_revision
    assert dirty.workspace_content_fingerprint != clean.workspace_content_fingerprint
    assert dirty.mutable_patch_fingerprints
    assert dirty.code_fingerprint != clean.code_fingerprint


@pytest.mark.parametrize("operation", ["staged", "deleted", "untracked", "renamed"])
def test_code_identity_handles_dirty_paths_and_renames_without_loss(repository: Path, operation: str) -> None:
    before = observe_code_identity(repository)
    if operation == "staged":
        (repository / "backend/module.py").write_text("VALUE = 100\n", encoding="utf-8")
        _git(repository, "add", "backend/module.py")
    elif operation == "deleted":
        (repository / "framework/module.py").unlink()
    elif operation == "untracked":
        (repository / "interfaces/研究 new file.py").write_text("VALUE = 1\n", encoding="utf-8")
    else:
        _git(repository, "mv", "infrastructure/module.py", "infrastructure/研究 renamed.py")
    after = observe_code_identity(repository)
    assert after.dirty
    assert after.workspace_content_fingerprint != before.workspace_content_fingerprint
    assert after.mutable_patch_fingerprints
    assert after.code_fingerprint != before.code_fingerprint
    assert observe_code_identity(repository) == after


def test_code_identity_does_not_confuse_noncode_or_python_version_with_dependency_lock(repository: Path) -> None:
    before = observe_code_identity(repository)
    (repository / "interfaces/notes.md").write_text("notes", encoding="utf-8")
    (repository / "tests").mkdir()
    (repository / "tests/unrelated.py").write_text("VALUE = 42\n", encoding="utf-8")
    after = observe_code_identity(repository)
    assert after == before
    assert set(after.model_dump()) == {
        "git_revision", "workspace_content_fingerprint", "mutable_patch_fingerprints", "dirty", "code_fingerprint",
    }


def test_code_identity_disables_configured_fsmonitor_in_real_repository(
    repository: Path, capfd: pytest.CaptureFixture[str],
) -> None:
    missing_monitor = "definitely-missing-qasper-fsmonitor-command"
    _git(repository, "config", "core.fsmonitor", missing_monitor)
    capfd.readouterr()

    identity = observe_code_identity(repository)

    captured = capfd.readouterr()
    assert identity.git_revision
    assert not identity.dirty
    assert missing_monitor not in captured.err


def test_code_identity_status_does_not_refresh_git_index(repository: Path) -> None:
    tracked = repository / "backend" / "module.py"
    index = repository / ".git" / "index"
    before = index.read_bytes()
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

    identity = observe_code_identity(repository)

    assert not identity.dirty
    assert index.read_bytes() == before


def test_explicit_repository_root_ignores_parent_git_repository_redirects(
    repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=repository, text=True,
    ).strip()
    foreign = repository / "foreign-repository"
    (foreign / "backend").mkdir(parents=True)
    (foreign / "backend" / "module.py").write_text("FOREIGN = True\n", encoding="utf-8")
    _git(foreign, "init", "-q")
    _git(foreign, "add", ".")
    _git(foreign, "-c", "user.email=test@example.invalid", "-c", "user.name=Test", "commit", "-qm", "foreign")
    foreign_revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=foreign, text=True,
    ).strip()
    assert foreign_revision != root_revision

    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign / ".git" / "index"))
    monkeypatch.setenv("GIT_COMMON_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(foreign / ".git" / "objects"))
    (repository / "backend" / "module.py").write_text("EXPLICIT = True\n", encoding="utf-8")

    identity = observe_code_identity(repository)

    assert identity.git_revision == root_revision
    assert identity.dirty
    assert os.environ["GIT_DIR"] == str(foreign / ".git")
    assert os.environ["GIT_WORK_TREE"] == str(foreign)

