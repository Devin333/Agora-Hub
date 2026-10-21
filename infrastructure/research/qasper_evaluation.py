"""Local byte readers, raw git observation and immutable artifact publication."""
from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import BinaryIO

from backend.research.ports.qasper_evaluation import (
    QasperRawCodeChange,
    QasperRawCodeIdentity,
    QasperRawSource,
)


def _git_error_detail(stderr: bytes | str | None) -> str:
    if stderr is None:
        return "no stderr output"
    if isinstance(stderr, bytes):
        value = stderr.decode(errors="replace")
    else:
        value = stderr
    return value.strip() or "no stderr output"


class FilesystemQasperAssets:
    """Storage-only input adapter; interpretation is owned by QasperAssetService."""

    def __init__(self, dataset_path: Path | None, cache_path: Path | None, *, repository_root: Path) -> None:
        # Pin relative paths at construction, independently of later cwd changes.
        self.dataset_path = Path(dataset_path).resolve() if dataset_path is not None else None
        self.cache_path = Path(cache_path).resolve() if cache_path is not None else None
        self.repository_root = Path(repository_root).resolve()

    def read_dataset(self) -> bytes:
        if self.dataset_path is None:
            raise ValueError("--dataset-path is required for mapping freeze and experiment execution")
        return self.dataset_path.read_bytes()

    def open_cache(self) -> BinaryIO:
        if self.cache_path is None:
            raise ValueError("--cache-path is required; this adapter never calls an embedding provider")
        return self.cache_path.open("rb")

    _GIT_TIMEOUT_SECONDS = 5.0
    _GIT_CONFIGURATION = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=")
    _GIT_REPOSITORY_ENVIRONMENT = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_SHALLOW_FILE",
        "GIT_REPLACE_REF_BASE",
        "GIT_GRAFT_FILE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    )

    def _git_probe(
        self, operation: str, arguments: tuple[str, ...], *, optional_locks: bool = True,
    ) -> bytes:
        env = os.environ.copy()
        for name in self._GIT_REPOSITORY_ENVIRONMENT:
            env.pop(name, None)
        env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
        if not optional_locks:
            env["GIT_OPTIONAL_LOCKS"] = "0"
        command = ("git", *self._GIT_CONFIGURATION, *arguments)
        try:
            completed = subprocess.run(
                command,
                cwd=self.repository_root,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._GIT_TIMEOUT_SECONDS,
                env=env,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            detail = _git_error_detail(exc.stderr)
            raise RuntimeError(
                f"QASPER git {operation} probe timed out after {self._GIT_TIMEOUT_SECONDS:g}s "
                f"in {self.repository_root}: {detail}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = _git_error_detail(exc.stderr)
            raise RuntimeError(
                f"QASPER git {operation} probe failed with exit code {exc.returncode} "
                f"in {self.repository_root}: {detail}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"QASPER git {operation} probe could not start in {self.repository_root}: {exc}"
            ) from exc
        return completed.stdout

    def read_code_identity(self, *, source_roots: Sequence[str]) -> QasperRawCodeIdentity:
        root = self.repository_root
        revision_bytes = self._git_probe("revision", ("rev-parse", "--verify", "HEAD"))
        try:
            revision = revision_bytes.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError("QASPER git revision probe returned non-ASCII output") from exc
        if len(revision) not in {40, 64} or any(char not in "0123456789abcdefABCDEF" for char in revision):
            raise RuntimeError(f"QASPER git revision probe returned an invalid object id: {revision!r}")
        sources = sorted(path for folder in source_roots for path in (root / folder).rglob("*.py")
                         if "__pycache__" not in path.parts)
        entries = tuple(QasperRawSource(path.relative_to(root).as_posix(), path.read_bytes()) for path in sources)
        # NUL records preserve Unicode, newlines, whitespace and literal arrows.
        # Renames/copies have a second path record that must not be discarded.
        status = self._git_probe(
            "status", ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *source_roots),
            optional_locks=False,
        ).split(b"\0")
        changes = []
        records = iter(status)
        try:
            for record in records:
                if not record:
                    continue
                if len(record) < 4 or record[2:3] != b" ":
                    raise ValueError("invalid porcelain record")
                change = record[:2].decode("ascii")
                paths = [os.fsdecode(record[3:])]
                if "R" in change or "C" in change:
                    paths.append(os.fsdecode(next(records)))
                for name in paths:
                    if name.endswith(".py") and name.split("/", 1)[0] in source_roots:
                        path = root / name
                        content = path.read_bytes() if path.is_file() else None
                        changes.append(QasperRawCodeChange(change, QasperRawSource(name, content)))
        except (UnicodeDecodeError, StopIteration, ValueError) as exc:
            raise RuntimeError("QASPER git status probe returned malformed porcelain output") from exc
        return QasperRawCodeIdentity(revision, entries, tuple(changes))


class FilesystemQasperArtifacts:
    """Per-file immutable publication, not a multi-file transaction.

    A complete, flushed staging file is linked into place atomically. Competing
    publishers can only create an absent name, never replace an existing inode;
    readers cannot observe partially written final files. Filesystems without
    hard-link support fail explicitly rather than falling back to overwrites.
    """

    @staticmethod
    def _target(directory: str, name: str) -> Path:
        # Apply the same portable filename rules on both Windows and POSIX;
        # colons also prohibit NTFS alternate data streams.
        reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³")
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or any(ord(char) < 32 or char in '/\\:<>"|?*' for char in name)
                or name.endswith((".", " ")) or name.split(".", 1)[0].upper() in reserved):
            raise ValueError("artifact names must be one portable filename")
        if not str(directory).strip():
            raise ValueError("an explicit artifact output directory is required")
        root = Path(directory).resolve()
        target = root / name
        if target.is_symlink() or target.resolve().parent != root:
            raise ValueError("artifact target escapes output directory or is a symbolic link")
        return target

    def read(self, directory: str, name: str) -> bytes:
        return self._target(directory, name).read_bytes()

    @staticmethod
    def _check_existing(path: Path, content: bytes) -> bool:
        if path.is_symlink():
            raise ValueError(f"immutable artifact target is a symbolic link: {path}")
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            return False
        if existing != content:
            raise ValueError(f"immutable artifact already exists with different bytes: {path}")
        return True

    def publish(self, directory: str, objects: Mapping[str, bytes]) -> None:
        targets: dict[Path, bytes] = {}
        names: set[str] = set()
        for name, content in objects.items():
            path = self._target(directory, name)
            if name.casefold() in names:
                raise ValueError("artifact names must not collide on case-insensitive filesystems")
            names.add(name.casefold())
            targets[path] = content
        # Validate the whole request before creating any files.
        for content in targets.values():
            if type(content) is not bytes:
                raise TypeError("artifact payloads must be concrete bytes")
        for path, content in targets.items():
            self._check_existing(path, content)
        for path, content in targets.items():
            if self._check_existing(path, content):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            staging: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".qasper-", suffix=".tmp", delete=False) as stream:
                    staging = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(staging, path)
                except FileExistsError:
                    if not self._check_existing(path, content):
                        raise ValueError(f"concurrent immutable artifact disappeared: {path}") from None
            finally:
                if staging is not None:
                    staging.unlink(missing_ok=True)

