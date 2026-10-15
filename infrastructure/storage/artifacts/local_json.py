from __future__ import annotations

import json
from pathlib import Path

from framework.agent.artifacts.models import ArtifactRef, artifact_identity_key
from framework.agent.artifacts.paths import (
    resolve_artifact_descendant,
    validate_artifact_path_segment,
    validate_relative_artifact_path,
)
from framework.agent.artifacts.stores.errors import ArtifactStoreMetadataError
from framework.agent.artifacts.stores.fs_safety import (
    reject_link_chain,
    verified_atomic_write,
    verified_exclusive_file_lock,
)
from framework.shared.graph_identity import GraphStageIdentity
from framework.shared.hashing import hash_text
from framework.shared.json import stable_json_dumps
from framework.shared.windows_paths import resolve_storage_path, storage_io_path


class ArtifactIndexNotFoundError(FileNotFoundError):
    pass


class LocalJsonArtifactIndexStore:
    def __init__(self, root: str | Path = ".newsroom/runs/_records/artifact_index") -> None:
        self.root = Path(root)

    def index_artifact(self, ref: ArtifactRef) -> Path:
        _validate_ref(ref)
        path = self._record_path(ref)
        payload = (stable_json_dumps(ref.to_dict()) + "\n").encode("utf-8")
        identity = _artifact_identity_label(ref)
        lock_path = resolve_artifact_descendant(
            self.root,
            "_locks",
            f"{hash_text(identity)}.lock",
            field="artifact index lock",
        )
        with verified_exclusive_file_lock(
            lock_path,
            root=self.root,
            identity=identity,
        ):
            if storage_io_path(path).exists():
                existing = self._read_ref(path, identity=identity)
                if existing != ref:
                    raise ArtifactStoreMetadataError(
                        f"artifact index identity conflict: {ref.artifact_id}"
                    )
                return path
            verified_atomic_write(
                path,
                payload,
                root=self.root,
                identity=identity,
            )
        return path

    def get_artifact(
        self,
        ref_or_run_id: ArtifactRef | str,
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        ref = self._resolve_lookup(ref_or_run_id, artifact_id)
        _validate_ref(ref)
        path = self._record_path(ref)
        if not storage_io_path(path).exists():
            raise ArtifactIndexNotFoundError(
                f"artifact index record not found: {_artifact_identity_label(ref)}"
            )
        stored = self._read_ref(path, identity=_artifact_identity_label(ref))
        if stored != ref:
            raise ArtifactStoreMetadataError(
                f"artifact index identity mismatch: {ref.artifact_id}"
            )
        return stored

    def list_by_run(self, run_id: str) -> list[ArtifactRef]:
        validate_artifact_path_segment(run_id, field="run_id")
        run_dir = self._run_dir(run_id)
        if not storage_io_path(run_dir).exists():
            return []
        refs = [
            self._read_ref(candidate, identity=f"{run_id}/{candidate.name}")
            for candidate in self._record_candidates(run_dir)
        ]
        for ref in refs:
            if ref.run_id != run_id:
                raise ArtifactStoreMetadataError(
                    f"artifact index run identity mismatch: {ref.artifact_id}"
                )
        return sorted(refs, key=_ref_sort_key)

    def list_all(self) -> list[ArtifactRef]:
        root = resolve_storage_path(self.root)
        if not storage_io_path(root).exists():
            return []
        refs: list[ArtifactRef] = []
        for candidate_run_dir in sorted(storage_io_path(root).iterdir()):
            if candidate_run_dir.name == "_locks":
                continue
            run_dir = resolve_artifact_descendant(
                self.root,
                candidate_run_dir.name,
                field="artifact index run directory",
            )
            if not storage_io_path(run_dir).is_dir():
                continue
            refs.extend(
                self._read_ref(candidate, identity=candidate.as_posix())
                for candidate in self._record_candidates(run_dir)
            )
        return sorted(refs, key=lambda ref: (ref.run_id, *_ref_sort_key(ref)))

    def list_by_node_instance(
        self,
        identity: GraphStageIdentity,
        *,
        activity_id: str | None = None,
        attempt: int | None = None,
    ) -> list[ArtifactRef]:
        identity = _require_stage_identity(identity)
        _validate_activity_filter(activity_id=activity_id, attempt=attempt)
        refs = [
            ref
            for ref in self.list_by_run(identity.run_id)
            if _matches_stage(ref, identity)
        ]
        if activity_id is not None:
            refs = [
                ref
                for ref in refs
                if ref.activity_id == activity_id and ref.attempt == attempt
            ]
        return refs

    def list_by_type(self, artifact_type: str, *, run_id: str | None = None) -> list[ArtifactRef]:
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            raise ValueError("artifact_type is required")
        refs = self.list_by_run(run_id) if run_id is not None else self.list_all()
        return [ref for ref in refs if ref.artifact_type == artifact_type]

    def delete_artifact(
        self,
        ref_or_run_id: ArtifactRef | str,
        artifact_id: str | None = None,
    ) -> None:
        ref = self._resolve_lookup(ref_or_run_id, artifact_id)
        _validate_ref(ref)
        path = self._record_path(ref)
        if not storage_io_path(path).exists():
            return
        stored = self._read_ref(path, identity=_artifact_identity_label(ref))
        if stored != ref:
            raise ArtifactStoreMetadataError(
                f"artifact index identity mismatch: {ref.artifact_id}"
            )
        storage_io_path(path).unlink()

    def _resolve_lookup(
        self,
        ref_or_run_id: ArtifactRef | str,
        artifact_id: str | None,
    ) -> ArtifactRef:
        if isinstance(ref_or_run_id, ArtifactRef):
            if artifact_id is not None:
                raise TypeError("artifact_id cannot accompany an ArtifactRef")
            return ref_or_run_id
        if artifact_id is None:
            raise TypeError("an ArtifactRef or run_id plus artifact_id is required")
        validate_artifact_path_segment(ref_or_run_id, field="run_id")
        _require_artifact_id(artifact_id)
        matches = [
            ref
            for ref in self.list_by_run(ref_or_run_id)
            if ref.scope_kind == "standalone" and ref.artifact_id == artifact_id
        ]
        if not matches:
            raise ArtifactIndexNotFoundError(
                f"artifact index record not found: {ref_or_run_id}/{artifact_id}"
            )
        if len(matches) != 1:
            raise ArtifactStoreMetadataError(
                f"standalone artifact identity is ambiguous: {ref_or_run_id}/{artifact_id}"
            )
        return matches[0]

    def _run_dir(self, run_id: str) -> Path:
        return resolve_artifact_descendant(
            self.root,
            _run_dir_name(run_id),
            field="run_id",
        )

    def _record_path(self, ref: ArtifactRef) -> Path:
        return resolve_artifact_descendant(
            self._run_dir(ref.run_id),
            _record_file_name(ref),
            field="artifact index record",
        )

    @staticmethod
    def _record_candidates(run_dir: Path) -> list[Path]:
        return sorted(
            candidate
            for candidate in storage_io_path(run_dir).glob("*.json")
            if candidate.is_file()
        )

    def _read_ref(self, path: Path, *, identity: str) -> ArtifactRef:
        reject_link_chain(
            path,
            root=resolve_storage_path(self.root),
            identity=identity,
            role="artifact index record",
        )
        try:
            payload = json.loads(storage_io_path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactStoreMetadataError(
                f"artifact index record is invalid: {identity}"
            ) from exc
        try:
            return ArtifactRef.from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactStoreMetadataError(
                f"artifact index record is invalid: {identity}"
            ) from exc


def _run_dir_name(run_id: str) -> str:
    return hash_text(run_id)[:12]


def _record_file_name(ref: ArtifactRef) -> str:
    digest = hash_text(artifact_identity_key(ref))
    return f"a-{digest}.json"


def _artifact_identity_parts(ref: ArtifactRef) -> tuple[str, ...]:
    return tuple(artifact_identity_key(ref).split("\x1f"))


def _artifact_identity_label(ref: ArtifactRef) -> str:
    return "/".join(_artifact_identity_parts(ref))


def _validate_ref(ref: ArtifactRef) -> None:
    if not isinstance(ref, ArtifactRef):
        raise TypeError("artifact reference is required")
    validate_artifact_path_segment(ref.run_id, field="run_id")
    validate_relative_artifact_path(ref.path, field="artifact path")


def _require_artifact_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("artifact_id is required")
    return value


def _require_stage_identity(value: GraphStageIdentity) -> GraphStageIdentity:
    if not isinstance(value, GraphStageIdentity):
        raise TypeError("GraphStageIdentity is required")
    return value


def _validate_activity_filter(*, activity_id: str | None, attempt: int | None) -> None:
    if (activity_id is None) != (attempt is None):
        raise ValueError("activity_id and attempt must be provided together")
    if activity_id is None:
        return
    if not isinstance(activity_id, str) or not activity_id.strip():
        raise ValueError("activity_id is required")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")


def _matches_stage(ref: ArtifactRef, identity: GraphStageIdentity) -> bool:
    return (
        ref.scope_kind == "graph"
        and ref.run_id == identity.run_id
        and ref.graph_id == identity.graph_id
        and ref.graph_version == identity.graph_version
        and ref.graph_ref == identity.graph_ref
        and ref.graph_checksum == identity.graph_checksum
        and ref.node_id == identity.node_id
        and ref.node_instance_id == identity.node_instance_id
    )


def _ref_sort_key(ref: ArtifactRef) -> tuple[object, ...]:
    return (ref.created_at, *_artifact_identity_parts(ref))
