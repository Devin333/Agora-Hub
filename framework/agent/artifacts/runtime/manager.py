from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from framework.agent.artifacts.models import Artifact, ArtifactReference
from framework.agent.artifacts.paths import (
    artifact_path_relative_to,
    resolve_artifact_descendant,
    validate_artifact_path_segment,
    validate_relative_artifact_path,
)
from framework.agent.artifacts.stores import ArtifactStore, LocalArtifactStore
from framework.agent.artifacts.stores.errors import (
    ArtifactStoreMetadataError,
)
from framework.agent.artifacts.stores.fs_safety import (
    verified_atomic_write,
    verified_exclusive_file_lock,
)
from framework.governance import CompositeAndGate, GateCheckResult
from framework.shared.json import to_jsonable


class ArtifactManager:
    def __init__(
        self,
        root: str | Path,
        *,
        store: ArtifactStore | None = None,
        gate_enabled: bool = True,
        max_write_bytes: int | None = None,
    ) -> None:
        self.root = Path(root)
        self.store = store or LocalArtifactStore(self.root)
        self.gate_enabled = gate_enabled
        self.max_write_bytes = max_write_bytes

    def publish(self, artifact: Artifact) -> ArtifactReference:
        return self.store.put(artifact)

    def resolve(self, ref: ArtifactReference) -> Artifact:
        artifact = self.store.get(ref.artifact_id)
        if artifact is None:
            raise FileNotFoundError(f"artifact not found: {ref.artifact_id}")
        return artifact

    def list(self, run_id: str | None = None) -> list[ArtifactReference]:
        return self.store.list(prefix=run_id)

    def delete(self, artifact_id: str) -> None:
        self.store.delete(artifact_id)

    def start_run(self, run_id: str) -> Path:
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        return run_dir

    def run_dir(self, run_id: str) -> Path:
        validate_artifact_path_segment(run_id, field="run_id")
        return resolve_artifact_descendant(self.root, run_id, field="run_id")

    def write_json(self, run_id: str, name: str, data: Any) -> Path:
        content = (
            json.dumps(
                to_jsonable(data),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        return self.write_bytes(run_id, name, content)

    def write_text(self, run_id: str, name: str, text: str) -> Path:
        return self.write_bytes(run_id, name, text.encode("utf-8"))

    def write_bytes(self, run_id: str, name: str, data: bytes) -> Path:
        self._gate_write(run_id, name, data)
        target = self._raw_target(run_id, name)
        identity = f"{run_id}/{name}"
        # verified_atomic_write refuses when the target changes under it, so
        # concurrent writers to one artifact must take turns: the last whole
        # write wins instead of every racer failing.
        with verified_exclusive_file_lock(
            _artifact_lock_path(target),
            root=self.root,
            identity=identity,
        ):
            verified_atomic_write(target, data, root=self.root, identity=identity)
        return target

    def _raw_target(self, run_id: str, name: str) -> Path:
        validate_artifact_path_segment(run_id, field="run_id")
        relative = validate_relative_artifact_path(name, field="artifact name")
        root = self.root.resolve(strict=False)
        path = root.joinpath(run_id, *PurePosixPath(relative).parts)
        try:
            artifact_path_relative_to(path, root)
        except ValueError as exc:
            raise ArtifactStoreMetadataError(
                "artifact path escapes the artifact root"
            ) from exc
        return path

    def _gate_write(self, run_id: str, name: str, data: bytes) -> None:
        validate_artifact_path_segment(run_id, field="run_id")
        validate_relative_artifact_path(name, field="artifact name")
        if not self.gate_enabled:
            return
        relative = Path(name)
        checks = [
            GateCheckResult(
                check_id="artifact.path",
                dimension="artifact",
                passed=not relative.is_absolute() and ".." not in relative.parts,
                reason=f"artifact path is not relative to run directory: {name}",
                metadata={"run_id": run_id, "name": name},
            )
        ]
        if self.max_write_bytes is not None:
            checks.append(
                GateCheckResult(
                    check_id="artifact.size",
                    dimension="resource",
                    passed=len(data) <= self.max_write_bytes,
                    reason=(
                        f"artifact write exceeds max_write_bytes: {len(data)} > {self.max_write_bytes}"
                    ),
                    metadata={"size_bytes": len(data), "max_write_bytes": self.max_write_bytes},
                )
            )
        if any(token in str(name).casefold() for token in ("secret", "token", "password")):
            checks.append(
                GateCheckResult(
                    check_id="artifact.sensitive_name",
                    dimension="safety",
                    passed=False,
                    severity="warning",
                    reason=f"artifact name looks sensitive: {name}",
                )
            )
        gate = CompositeAndGate(f"artifact:{run_id}:{name}:gate").evaluate(checks)
        if gate.decision == "block":
            raise ValueError(f"artifact gate blocked: {gate.reason}; gate_result={gate.to_dict()}")


# Lock files sit beside the artifact with a dotted, suffixed name so artifact
# listings and searches (which skip dotfiles) never return them as content.
ARTIFACT_LOCK_SUFFIX = ".write.lock"


def _artifact_lock_path(target: Path) -> Path:
    return target.with_name(f".{target.name}{ARTIFACT_LOCK_SUFFIX}")
