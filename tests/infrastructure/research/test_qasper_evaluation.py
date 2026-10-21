from __future__ import annotations

import errno
import os
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from infrastructure.research.qasper_evaluation import FilesystemQasperArtifacts, FilesystemQasperAssets

@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("offline evaluation must never contact an embedding provider or the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def test_artifact_publish_is_idempotent_and_preflights_all_conflicts(tmp_path: Path) -> None:
    artifacts = FilesystemQasperArtifacts()
    output = tmp_path / "explicit" / "output"
    artifacts.publish(str(output), {"run.json": b"original", "empty.bin": b""})
    original_inode = (output / "run.json").stat().st_ino
    artifacts.publish(str(output), {"run.json": b"original"})
    assert (output / "run.json").stat().st_ino == original_inode
    with pytest.raises(ValueError, match="immutable artifact"):
        artifacts.publish(str(output), {"new.json": b"new", "run.json": b"replacement"})
    assert not (output / "new.json").exists()
    assert artifacts.read(str(output), "run.json") == b"original"
    assert artifacts.read(str(output), "empty.bin") == b""
    with pytest.raises(TypeError, match="concrete bytes"):
        artifacts.publish(str(output), {"new.json": b"new", "invalid": bytearray(b"data")})
    assert not (output / "new.json").exists()


@pytest.mark.parametrize("same_bytes", [False, True])
def test_concurrent_artifact_publication_is_atomic_no_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_bytes: bool,
) -> None:
    # Rendezvous immediately before the atomic primitive: both publishers have
    # already preflighted, and neither may have exposed even an empty final file.
    barrier = threading.Barrier(2)
    link = os.link
    payloads = (b"first" * 50000, (b"first" if same_bytes else b"second") * 50000)
    observed_staging = []

    def synchronized_link(source, target):
        observed_staging.append(Path(source).read_bytes())
        assert not Path(target).exists()
        barrier.wait(timeout=10)
        return link(source, target)

    monkeypatch.setattr(os, "link", synchronized_link)

    def publish(payload: bytes):
        try:
            FilesystemQasperArtifacts().publish(str(tmp_path), {"shared.bin": payload})
            return "published", payload
        except ValueError:
            return "conflict", payload

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, payloads))
    assert sorted(result for result, _ in results) == (["published", "published"] if same_bytes else ["conflict", "published"])
    published = next(payload for result, payload in results if result == "published")
    assert (tmp_path / "shared.bin").read_bytes() == published
    assert sorted(observed_staging) == sorted(payloads)
    assert not list(tmp_path.glob(".qasper-*.tmp"))


def test_publish_io_failure_leaves_no_partial_target_or_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_link(source, target):
        assert Path(source).read_bytes() == b"complete"
        raise OSError(errno.ENOSPC, "simulated full filesystem")

    monkeypatch.setattr(os, "link", failed_link)
    with pytest.raises(OSError, match="simulated full filesystem"):
        FilesystemQasperArtifacts().publish(str(tmp_path), {"output.bin": b"complete"})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "folder/file", "folder\\file", "name:stream",
                                  "CON", "aux.txt", "COM1.json", "LPT¹", "trailing.", "trailing ", "line\nfeed", 'bad"name'])
def test_artifact_names_are_portable(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="portable filename"):
        FilesystemQasperArtifacts().publish(str(tmp_path), {name: b"data"})


def test_artifact_case_aliases_fail_before_creating_any_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="case-insensitive"):
        FilesystemQasperArtifacts().publish(str(tmp_path), {"run.json": b"one", "RUN.json": b"two"})
    assert list(tmp_path.iterdir()) == []


def test_output_directory_must_be_explicit() -> None:
    with pytest.raises(ValueError, match="explicit artifact output directory"):
        FilesystemQasperArtifacts().publish("", {"output.json": b"data"})


def test_artifacts_reject_leaf_symlinks_without_touching_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the guard on Windows even when creating symlinks needs privileges.
    target = tmp_path / "alias"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original_is_symlink(path))
    artifacts = FilesystemQasperArtifacts()
    with pytest.raises(ValueError, match="symbolic link"):
        artifacts.publish(str(tmp_path), {"alias": b"data"})
    with pytest.raises(ValueError, match="symbolic link"):
        artifacts.read(str(tmp_path), "alias")
    assert list(tmp_path.iterdir()) == []


def test_code_identity_uses_bounded_fixed_noninteractive_git_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "backend" / "module.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "ambient-lock-setting")
    monkeypatch.setenv("GIT_DIR", "foreign/repo/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "foreign/worktree")
    monkeypatch.setenv("GIT_INDEX_FILE", "foreign/index")
    monkeypatch.setenv("GIT_COMMON_DIR", "foreign/common-git-dir")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "foreign/objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "foreign/alternate-objects")
    monkeypatch.setenv("GIT_NAMESPACE", "foreign-namespace")
    monkeypatch.setenv("GIT_SHALLOW_FILE", "foreign/shallow")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/foreign-replace")
    monkeypatch.setenv("GIT_GRAFT_FILE", "foreign/grafts")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "foreign/ceiling")
    monkeypatch.setenv("GIT_DISCOVERY_ACROSS_FILESYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "ambient-prompt-setting")
    monkeypatch.setenv("GCM_INTERACTIVE", "ambient-credential-setting")
    calls = []
    outputs = iter((b"a" * 40 + b"\n", b" M backend/module.py\0"))

    def completed(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=next(outputs), stderr=b"")

    monkeypatch.setattr(subprocess, "run", completed)
    identity = FilesystemQasperAssets(None, None, repository_root=tmp_path).read_code_identity(
        source_roots=("backend",),
    )

    assert identity.git_revision == "a" * 40
    assert identity.changes[0].source.content == source.read_bytes()
    assert [call[0] for call in calls] == [
        ("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=", "rev-parse", "--verify", "HEAD"),
        (
            "git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=", "status",
            "--porcelain=v1", "-z", "--untracked-files=all", "--", "backend",
        ),
    ]
    for _, kwargs in calls:
        assert kwargs["cwd"] == tmp_path.resolve()
        assert kwargs["check"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        assert kwargs["timeout"] == 5.0
        assert kwargs["shell"] is False
        assert kwargs["env"] is not os.environ
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GCM_INTERACTIVE"] == "Never"
        assert not {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
            "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE", "GIT_GRAFT_FILE",
            "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        }.intersection(kwargs["env"])
    assert calls[0][1]["env"]["GIT_OPTIONAL_LOCKS"] == "ambient-lock-setting"
    assert calls[1][1]["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert os.environ["GIT_OPTIONAL_LOCKS"] == "ambient-lock-setting"
    assert os.environ["GIT_DIR"] == "foreign/repo/.git"
    assert os.environ["GIT_WORK_TREE"] == "foreign/worktree"
    assert os.environ["GIT_INDEX_FILE"] == "foreign/index"
    assert os.environ["GIT_COMMON_DIR"] == "foreign/common-git-dir"
    assert os.environ["GIT_OBJECT_DIRECTORY"] == "foreign/objects"
    assert os.environ["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == "foreign/alternate-objects"
    assert os.environ["GIT_NAMESPACE"] == "foreign-namespace"
    assert os.environ["GIT_SHALLOW_FILE"] == "foreign/shallow"
    assert os.environ["GIT_REPLACE_REF_BASE"] == "refs/foreign-replace"
    assert os.environ["GIT_GRAFT_FILE"] == "foreign/grafts"
    assert os.environ["GIT_CEILING_DIRECTORIES"] == "foreign/ceiling"
    assert os.environ["GIT_DISCOVERY_ACROSS_FILESYSTEM"] == "1"
    assert os.environ["GIT_TERMINAL_PROMPT"] == "ambient-prompt-setting"
    assert os.environ["GCM_INTERACTIVE"] == "ambient-credential-setting"


@pytest.mark.parametrize(
    ("failure", "match"),
    [
        (subprocess.TimeoutExpired(("git",), 5, stderr=b"probe hung"), "timed out.*probe hung"),
        (subprocess.CalledProcessError(128, ("git",), stderr=b"not a repository"), "exit code 128.*not a repository"),
        (FileNotFoundError("git executable missing"), "could not start.*git executable missing"),
    ],
)
def test_code_identity_git_probe_failures_are_diagnostic_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException, match: str,
) -> None:
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    reader = FilesystemQasperAssets(None, None, repository_root=tmp_path)
    with pytest.raises(RuntimeError, match=match):
        reader.read_code_identity(source_roots=("backend",))


def test_code_identity_rejects_malformed_git_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=b"HEAD\n", stderr=b""),
    )
    with pytest.raises(RuntimeError, match="invalid object id"):
        FilesystemQasperAssets(None, None, repository_root=tmp_path).read_code_identity(
            source_roots=("backend",),
        )


def test_code_identity_status_failure_does_not_return_partial_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail_status(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0, stdout=b"b" * 40 + b"\n", stderr=b"")
        raise subprocess.CalledProcessError(128, command, stderr=b"status unavailable")

    monkeypatch.setattr(subprocess, "run", fail_status)
    with pytest.raises(RuntimeError, match="git status probe failed.*status unavailable"):
        FilesystemQasperAssets(None, None, repository_root=tmp_path).read_code_identity(
            source_roots=("backend",),
        )
