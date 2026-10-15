from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from uuid import uuid4

from framework.agent.artifacts.models import ArtifactRef, ArtifactWriteRequest, compute_checksum
from framework.agent.artifacts.models.reference import canonical_artifact_relative_path
from framework.agent.artifacts.paths import (
    artifact_path_relative_to,
    resolve_artifact_descendant,
    validate_artifact_path_segment,
    validate_relative_artifact_path,
)
from framework.agent.artifacts.stores.errors import (
    ArtifactChecksumMismatchError,
    ArtifactNotFoundError,
    ArtifactStoreMetadataError,
)
from framework.agent.artifacts.stores.fs_safety import (
    is_link_or_reparse_point,
    reject_link_chain,
    verified_atomic_write,
    verified_exclusive_file_lock,
)
from framework.agent.artifacts.stores.integrity import verify_sha256_checksum
from framework.shared.windows_paths import resolve_storage_path, storage_io_path


class FilesystemArtifactStore:
    def __init__(self, root: str | Path = ".newsroom/runs") -> None:
        self.root = Path(root)

    def write(self, artifact: ArtifactWriteRequest) -> ArtifactRef:
        _validate_id(artifact.run_id, "run_id")
        artifact_id = artifact.artifact_id or uuid4().hex
        _require_artifact_id(artifact_id)
        if artifact.scope_kind == "graph" and artifact.relative_path is not None:
            raise ValueError("relative_path cannot override canonical artifact identity")

        relative_path = (
            canonical_artifact_relative_path(artifact, artifact_id=artifact_id)
            if artifact.scope_kind == "graph"
            else _standalone_relative_path(artifact, artifact_id=artifact_id)
        )
        target = self._raw_artifact_path(artifact.run_id, relative_path)
        data = artifact.content_bytes()
        identity = f"{artifact.run_id}/{artifact.scope_kind}/{artifact_id}"
        lock_path = self._lock_path(artifact.run_id, relative_path)
        with verified_exclusive_file_lock(
            lock_path,
            root=self.root,
            identity=identity,
        ):
            if storage_io_path(target).exists():
                existing = _read_verified_regular_file(
                    target,
                    root=resolve_storage_path(self.root),
                    artifact_id=artifact_id,
                )
                if existing != data:
                    raise ArtifactStoreMetadataError(
                        f"artifact identity conflict: {artifact_id}"
                    )
            else:
                verified_atomic_write(
                    target,
                    data,
                    root=self.root,
                    identity=identity,
                )

        return ArtifactRef(
            artifact_id=artifact_id,
            run_id=artifact.run_id,
            scope_kind=artifact.scope_kind,
            graph_id=artifact.graph_id,
            graph_version=artifact.graph_version,
            graph_ref=artifact.graph_ref,
            graph_checksum=artifact.graph_checksum,
            node_id=artifact.node_id,
            node_instance_id=artifact.node_instance_id,
            graph_checkpoint_ref=artifact.graph_checkpoint_ref,
            activity_id=artifact.activity_id,
            attempt=artifact.attempt,
            artifact_type=artifact.artifact_type,
            path=relative_path,
            content_type=artifact.content_type,
            size_bytes=len(data),
            checksum=compute_checksum(data),
            redacted=artifact.redacted,
            created_at=artifact.created_at,
            metadata=dict(artifact.metadata),
        )

    def read(self, artifact_ref: ArtifactRef) -> bytes:
        self._assert_ref_path(artifact_ref)
        path = self._raw_artifact_path(artifact_ref.run_id, artifact_ref.path)
        try:
            data = _read_verified_regular_file(
                path,
                root=resolve_storage_path(self.root),
                artifact_id=artifact_ref.artifact_id,
            )
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_ref.run_id}/{artifact_ref.path}") from exc
        except (IsADirectoryError, OSError) as exc:
            raise ArtifactStoreMetadataError(
                f"artifact is not a regular file: {artifact_ref.artifact_id}"
            ) from exc
        if artifact_ref.checksum:
            verify_sha256_checksum(
                data,
                artifact_ref.checksum,
                artifact_id=artifact_ref.artifact_id,
                store="filesystem",
                operation="read",
            )
        if artifact_ref.size_bytes is not None and len(data) != artifact_ref.size_bytes:
            raise ArtifactStoreMetadataError(
                f"artifact size mismatch: {artifact_ref.artifact_id}"
            )
        return data

    def exists(self, artifact_ref: ArtifactRef) -> bool:
        self._assert_ref_path(artifact_ref)
        path = self._raw_artifact_path(artifact_ref.run_id, artifact_ref.path)
        try:
            _reject_symlink_chain(
                path,
                root=resolve_storage_path(self.root),
                artifact_id=artifact_ref.artifact_id,
            )
        except FileNotFoundError:
            # A missing ancestor means the immutable artifact cannot exist.
            # Keep the existence query total while preserving link rejection
            # for every path component that is actually present.
            return False
        return storage_io_path(path).exists()

    def list(self, run_id: str) -> list[str]:
        validate_artifact_path_segment(run_id, field="run_id")
        root = resolve_storage_path(self.root)
        lexical_run_dir = root / run_id
        _reject_symlink_chain(
            lexical_run_dir,
            root=root,
            artifact_id=run_id,
        )
        run_dir = resolve_artifact_descendant(self.root, run_id, field="run_id")
        if not storage_io_path(run_dir).exists():
            return []
        paths: list[str] = []
        pending = [run_dir]
        while pending:
            directory = pending.pop()
            _reject_symlink_chain(
                directory,
                root=root,
                artifact_id=run_id,
            )
            for candidate in storage_io_path(directory).iterdir():
                relative_path = artifact_path_relative_to(candidate, run_dir).as_posix()
                if relative_path == "_locks" or relative_path.startswith("_locks/"):
                    continue
                info = os.lstat(storage_io_path(candidate))
                if is_link_or_reparse_point(info):
                    raise ArtifactStoreMetadataError(
                        "artifact list path contains a symlink, junction, or reparse "
                        f"point: {relative_path}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    pending.append(candidate)
                elif stat.S_ISREG(info.st_mode):
                    paths.append(relative_path)
        return sorted(paths)

    def checksum(self, artifact_ref: ArtifactRef) -> str:
        self._assert_ref_path(artifact_ref)
        path = self._raw_artifact_path(artifact_ref.run_id, artifact_ref.path)
        try:
            content = _read_verified_regular_file(
                path,
                root=resolve_storage_path(self.root),
                artifact_id=artifact_ref.artifact_id,
            )
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(f"artifact not found: {artifact_ref.run_id}/{artifact_ref.path}") from exc
        except (IsADirectoryError, OSError) as exc:
            raise ArtifactStoreMetadataError(
                f"artifact is not a regular file: {artifact_ref.artifact_id}"
            ) from exc
        return compute_checksum(content)

    def delete(self, artifact_ref: ArtifactRef) -> None:
        self._assert_ref_path(artifact_ref)
        path = self._raw_artifact_path(artifact_ref.run_id, artifact_ref.path)
        _reject_symlink_chain(
            path,
            root=resolve_storage_path(self.root),
            artifact_id=artifact_ref.artifact_id,
        )
        if storage_io_path(path).exists():
            storage_io_path(path).unlink()

    def _raw_artifact_path(self, run_id: str, relative_path: str) -> Path:
        """Return a lexical path so reads can reject final symlinks before open."""

        validate_artifact_path_segment(run_id, field="run_id")
        relative = validate_relative_artifact_path(relative_path, field="artifact path")
        root = resolve_storage_path(self.root)
        path = root.joinpath(run_id, *PurePosixPath(relative).parts)
        try:
            artifact_path_relative_to(path, root)
        except ValueError as exc:
            raise ArtifactStoreMetadataError("artifact path escapes the artifact root") from exc
        return path

    def _lock_path(self, run_id: str, relative_path: str) -> Path:
        lock_name = compute_checksum(relative_path.encode("utf-8"))
        return self._raw_artifact_path(run_id, f"_locks/{lock_name}.lock")

    @staticmethod
    def _assert_ref_path(artifact_ref: ArtifactRef) -> None:
        if artifact_ref.scope_kind != "graph":
            return
        expected_path = canonical_artifact_relative_path(artifact_ref)
        if artifact_ref.path != expected_path:
            raise ArtifactStoreMetadataError(
                f"artifact path does not match Graph identity: {artifact_ref.artifact_id}"
            )


def _read_verified_regular_file(path: Path, *, root: Path, artifact_id: str) -> bytes:
    _reject_symlink_chain(path, root=root, artifact_id=artifact_id)
    io_path = storage_io_path(path)
    before = os.lstat(io_path)
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactStoreMetadataError(
            f"artifact is not a regular file: {artifact_id}"
        )
    try:
        with io_path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ArtifactStoreMetadataError(
                    f"artifact is not a regular file: {artifact_id}"
                )
            if not os.path.samestat(before, opened):
                raise ArtifactStoreMetadataError(
                    f"artifact identity changed while opening: {artifact_id}"
                )
            _reject_symlink_chain(path, root=root, artifact_id=artifact_id)
            return handle.read()
    except FileNotFoundError:
        raise
    except IsADirectoryError:
        raise


def _reject_symlink_chain(path: Path, *, root: Path, artifact_id: str) -> None:
    reject_link_chain(
        path,
        root=root,
        identity=artifact_id,
        role="artifact",
    )


def _validate_id(value: str, label: str) -> None:
    validate_artifact_path_segment(value, field=label)


def _require_artifact_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("artifact_id is required")
    if "/" in value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("invalid artifact_id")
    return value


def _standalone_relative_path(
    artifact: ArtifactWriteRequest,
    *,
    artifact_id: str,
) -> str:
    if artifact.relative_path is not None:
        return validate_relative_artifact_path(
            artifact.relative_path,
            field="artifact path",
        )
    return canonical_artifact_relative_path(artifact, artifact_id=artifact_id)


__all__ = [
    "ArtifactChecksumMismatchError",
    "ArtifactNotFoundError",
    "FilesystemArtifactStore",
]
