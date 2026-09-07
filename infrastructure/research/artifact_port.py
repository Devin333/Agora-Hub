from __future__ import annotations

import contextvars
import hashlib
import inspect
import json
import math
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable

from framework.agent.artifacts import (
    ArtifactChecksumMismatchError,
    ArtifactManager,
    ArtifactRef as StorageArtifactRef,
    ArtifactNotFoundError,
    ArtifactStoreMetadataError,
    FilesystemArtifactStore,
    compute_checksum,
    validate_artifact_path_segment,
)
from framework.agent.artifacts.stores.integrity import validate_sha256_checksum
from framework.agent.artifacts.stores.fs_safety import verified_exclusive_file_lock
from framework.events.canonical import checksum_for
from framework.events.projection import GraphEventProjection
from framework.harness import (
    ArtifactRef as HarnessArtifactRef,
    ArtifactWriteRequest,
    HarnessSideEffectDisposition,
    HarnessSideEffectOutcome,
)
from framework.harness.artifacts import (
    ArtifactReferenceDescriptor,
    GraphTerminalArtifact,
    GraphTerminalManifestV2,
    GraphTerminalManifestCommitRequest,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.shared.hashing import hash_text
from framework.shared.graph_identity import GraphRunIdentity
from framework.shared.json import stable_json_dumps
from infrastructure.research.diagnostics import emit_research_persistence_diagnostic
from infrastructure.storage.artifacts import FilesystemGraphTerminalArtifactStore
from infrastructure.storage.indexing import (
    GraphArtifactBindingEvidenceSource,
    GraphArtifactBindingProjection,
)
from backend.research.ports.artifact_publication import (
    RESEARCH_ARTIFACT_MANIFEST_VERSION,
    ResearchArtifactDiagnosticClaim,
    ResearchArtifactReadClaim,
    ResearchArtifactReadResolution,
    artifact_evidence_ref,
    artifact_member_evidence_ref,
)


_CONTEXT_REF_ONLY_ARTIFACT_TYPES = frozenset(
    {
        "context-aggregate-verification",
        "context-compaction-action-result",
        "context-compaction-plan",
        "context-compaction-planning-result",
        "context-compression-record-v2",
        "context-physical-admission",
        "context-result-snapshot",
        "context-source-snapshot",
    }
)
_GRAPH_RESULT_REF_ONLY_PREFIX = "graph-result-"


CANONICAL_ARTIFACT_SCHEME = "artifact"
CANONICAL_ARTIFACT_DIRECTORY = "artifacts"


class ArtifactRunBindingError(HarnessValidationError):
    """Raised when a Research artifact operation has no safe run binding."""


class ArtifactWriteConflictError(ArtifactStoreMetadataError):
    """Raised when an immutable canonical artifact would be overwritten."""


class ArtifactPublicationVisibilityError(ArtifactStoreMetadataError):
    """Raised when a manifest is not authorized for a normal reader."""

    def __init__(self, message: str, *, disposition: str) -> None:
        self.disposition = disposition
        super().__init__(message)


class FilesystemHarnessArtifactPort:
    """Persist Harness JSON artifacts below one context-local Research run."""

    def __init__(
        self,
        root: str | Path = ".newsroom/runs",
        *,
        artifact_manager: ArtifactManager | None = None,
        artifact_store: FilesystemArtifactStore | None = None,
        max_write_bytes: int | None = None,
        accepted_run_resolver: Callable[..., bool] | None = None,
        diagnostic_run_resolver: Callable[..., bool] | None = None,
        terminal_store: FilesystemGraphTerminalArtifactStore | None = None,
    ) -> None:
        if max_write_bytes is not None and max_write_bytes < 0:
            raise ValueError("max_write_bytes must be non-negative")
        configured_root = Path(root)
        if artifact_manager is not None:
            manager_root = Path(artifact_manager.root)
            if manager_root.resolve(strict=False) != configured_root.resolve(strict=False):
                raise ValueError("artifact_manager root does not match root")
        self.root = configured_root
        self.manager = artifact_manager or ArtifactManager(
            self.root,
            max_write_bytes=max_write_bytes,
        )
        self.store = artifact_store or FilesystemArtifactStore(self.root)
        if terminal_store is not None and (
            terminal_store.root.resolve(strict=False)
            != self.root.resolve(strict=False)
        ):
            raise ValueError("terminal_store root does not match root")
        self.terminal_store = terminal_store or FilesystemGraphTerminalArtifactStore(
            self.root
        )
        self.max_write_bytes = (
            max_write_bytes
            if max_write_bytes is not None
            else self.manager.max_write_bytes
        )
        if accepted_run_resolver is not None and not callable(accepted_run_resolver):
            raise TypeError("accepted_run_resolver must be callable")
        if diagnostic_run_resolver is not None and not callable(diagnostic_run_resolver):
            raise TypeError("diagnostic_run_resolver must be callable")
        self._accepted_run_resolver = accepted_run_resolver
        self._diagnostic_run_resolver = diagnostic_run_resolver
        self._manifest_lock = threading.RLock()
        self._run_binding: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"filesystem_harness_artifact_run_{id(self)}",
            default=None,
        )

    @property
    def current_run_id(self) -> str | None:
        return self._run_binding.get()

    def set_accepted_run_resolver(
        self,
        resolver: Callable[..., bool] | None,
    ) -> None:
        if resolver is not None and not callable(resolver):
            raise TypeError("accepted_run_resolver must be callable")
        self._accepted_run_resolver = resolver

    def set_diagnostic_run_resolver(
        self,
        resolver: Callable[..., bool] | None,
    ) -> None:
        if resolver is not None and not callable(resolver):
            raise TypeError("diagnostic_run_resolver must be callable")
        self._diagnostic_run_resolver = resolver

    @contextmanager
    def bind_run(self, run_id: str) -> Iterator[str]:
        validated = validate_artifact_path_segment(run_id, field="run_id")
        token = self._run_binding.set(validated)
        try:
            yield validated
        finally:
            self._run_binding.reset(token)

    @contextmanager
    def lock_run(self, run_id: str) -> Iterator[str]:
        """Serialize manifest membership changes for one canonical run."""

        validated = validate_artifact_path_segment(run_id, field="run_id")
        digest = hashlib.sha256(validated.encode("utf-8")).hexdigest()
        root = self.root.resolve(strict=False)
        lock_path = (
            root
            / "_records"
            / "graph_artifact_lifecycle"
            / "run_locks"
            / f"{digest}.lock"
        )
        with verified_exclusive_file_lock(
            lock_path,
            root=root,
            identity=f"graph-artifact-run:{validated}",
        ):
            yield validated

    def write_artifact(self, request: ArtifactWriteRequest) -> HarnessArtifactRef:
        run_id = self.current_run_id
        try:
            result = self._write_artifact(request)
        except Exception as exc:
            emit_research_persistence_diagnostic(
                component="artifact_store",
                operation="artifact_write",
                outcome="failed",
                reason=_artifact_failure_reason(exc),
                run_id=run_id,
            )
            raise
        emit_research_persistence_diagnostic(
            component="artifact_store",
            operation="artifact_write",
            outcome="succeeded",
            reason="completed",
            run_id=run_id,
        )
        return result

    def _write_artifact(self, request: ArtifactWriteRequest) -> HarnessArtifactRef:
        run_id = self._require_bound_run()
        if not isinstance(request, ArtifactWriteRequest):
            raise TypeError("request must be ArtifactWriteRequest")
        artifact_type = validate_artifact_path_segment(
            request.artifact_type,
            field="artifact_type",
        )
        request_run_id = request.metadata.get("run_id")
        if request_run_id is not None and request_run_id != run_id:
            raise ArtifactRunBindingError(
                "artifact metadata run_id conflicts with the bound run"
            )
        canonical_payload = request.to_dict()
        _assert_finite_json(canonical_payload)
        try:
            content = stable_json_dumps(canonical_payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreMetadataError(
                f"artifact payload is not JSON serializable: {artifact_type}"
            ) from exc
        self._enforce_size(content, artifact_type)

        relative_path = self._canonical_path(artifact_type)
        checksum = compute_checksum(content)
        with self._manifest_lock:
            with self.lock_run(run_id):
                existing = self._existing_artifact_result(
                    run_id=run_id,
                    artifact_type=artifact_type,
                    request=request,
                    content=content,
                    checksum=checksum,
                )
                if existing is not None:
                    return existing
                self.manager.write_bytes(run_id, relative_path, content)
                self.terminal_store.stage_artifact(
                    run_id=run_id,
                    artifact=_terminal_artifact_from_request(
                        run_id=run_id,
                        artifact_type=artifact_type,
                        relative_path=relative_path,
                        checksum=checksum,
                        size_bytes=len(content),
                        request=request,
                    ),
                )
        return HarnessArtifactRef(
            ref=self._canonical_ref(run_id, artifact_type),
            artifact_type=artifact_type,
            checksum=f"sha256:{checksum}",
            media_type=request.media_type,
            metadata=dict(request.metadata),
        )

    def read_artifact(self, ref: str) -> dict[str, Any]:
        run_id: str | None = None
        try:
            run_id, artifact_type = self._parse_ref(ref)
            result = self._read_artifact(run_id, artifact_type)
        except Exception as exc:
            emit_research_persistence_diagnostic(
                component="artifact_store",
                operation="artifact_read",
                outcome="failed",
                reason=_artifact_failure_reason(exc),
                run_id=run_id,
            )
            raise
        emit_research_persistence_diagnostic(
            component="artifact_store",
            operation="artifact_read",
            outcome="succeeded",
            reason="completed",
            run_id=run_id,
        )
        return result

    def verify_artifact_ref(self, ref: str, *, expected_run_id: str) -> None:
        """Verify canonical artifact integrity without exposing its payload.

        TaskPlan acceptance happens before Research terminal publication. This
        method therefore validates the run binding, manifest, checksum, size,
        and stored bytes directly, while the normal ``read_artifact`` method
        continues to enforce publication visibility for payload readers.
        """

        run_id: str | None = None
        try:
            run_id, artifact_type = self._parse_ref(ref)
            expected = validate_artifact_path_segment(
                expected_run_id,
                field="expected artifact ref run_id",
            )
            if run_id != expected:
                raise ArtifactRunBindingError(
                    "artifact ref run_id does not match the expected parent run"
                )
            manifest = self._artifact_metadata_projection(run_id)
            self._read_artifact_payload(
                manifest,
                run_id=run_id,
                artifact_type=artifact_type,
            )
        except Exception as exc:
            emit_research_persistence_diagnostic(
                component="artifact_store",
                operation="artifact_read",
                outcome="failed",
                reason=_artifact_failure_reason(exc),
                run_id=run_id,
            )
            raise
        emit_research_persistence_diagnostic(
            component="artifact_store",
            operation="artifact_read",
            outcome="succeeded",
            reason="completed",
            run_id=run_id,
        )

    def describe_artifact_ref(
        self,
        ref: str,
        *,
        expected_run_id: str,
        expected_tenant_id: str | None = None,
    ) -> ArtifactReferenceDescriptor:
        """Return verified authority metadata without reading artifact payload."""

        run_id: str | None = None
        try:
            run_id, artifact_type = self._parse_ref(ref)
            expected = validate_artifact_path_segment(
                expected_run_id,
                field="expected artifact ref run_id",
            )
            if run_id != expected:
                raise ArtifactRunBindingError(
                    "artifact ref run_id does not match the expected parent run"
                )
            try:
                terminal = self.terminal_store.read_terminal_manifest(run_id)
            except ArtifactNotFoundError:
                terminal = None
                artifact = self.terminal_store.read_staged_artifact(
                    run_id=run_id,
                    artifact_key=artifact_type,
                )
                manifest = _artifact_projection(
                    run_id,
                    (artifact,),
                    staging_only=True,
                )
            else:
                artifact = terminal.artifact(artifact_type)
                if artifact is None:
                    raise ArtifactNotFoundError(
                        f"artifact is not present in terminal manifest: {ref}"
                    )
                manifest = _terminal_manifest_projection(terminal)

            if (
                artifact.ref != ref
                or artifact.artifact_key != artifact_type
                or artifact.artifact_id != artifact_type
            ):
                raise ArtifactStoreMetadataError(
                    f"artifact authority identity mismatch: {artifact_type}"
                )
            tenant_id = terminal.tenant_id if terminal is not None else None
            if expected_tenant_id is not None:
                if (
                    not isinstance(expected_tenant_id, str)
                    or not expected_tenant_id.strip()
                ):
                    raise ArtifactRunBindingError(
                        "expected artifact tenant_id is required"
                    )
                if tenant_id is None:
                    raise ArtifactRunBindingError(
                        "trusted artifact tenant ownership is unavailable"
                    )
                if tenant_id != expected_tenant_id:
                    raise ArtifactRunBindingError(
                        "artifact tenant_id does not match the expected tenant"
                    )

            storage_ref = self._validated_artifact_storage_ref(
                manifest,
                run_id=run_id,
                artifact_type=artifact_type,
            )
            if (
                f"sha256:{storage_ref.checksum}" != artifact.content_checksum
                or storage_ref.size_bytes != artifact.byte_size
                or storage_ref.content_type != artifact.media_type
            ):
                raise ArtifactStoreMetadataError(
                    f"artifact authority metadata mismatch: {artifact_type}"
                )

            publication = terminal.publication if terminal is not None else None
            descriptor = ArtifactReferenceDescriptor(
                ref=artifact.ref,
                run_id=run_id,
                artifact_type=artifact.artifact_key,
                checksum=artifact.content_checksum,
                byte_size=artifact.byte_size,
                media_type=artifact.media_type,
                tenant_id=tenant_id,
                graph_id=terminal.graph_id if terminal is not None else None,
                node_id=artifact.node_id,
                attempt_id=artifact.attempt_id,
                identity_scope_ref=(
                    publication.identity_scope_ref
                    if publication is not None
                    else None
                ),
                subject_scope_ref=(
                    publication.subject_scope_ref
                    if publication is not None
                    else None
                ),
            )
        except Exception as exc:
            emit_research_persistence_diagnostic(
                component="artifact_store",
                operation="artifact_read",
                outcome="failed",
                reason=_artifact_failure_reason(exc),
                run_id=run_id,
            )
            raise
        emit_research_persistence_diagnostic(
            component="artifact_store",
            operation="artifact_read",
            outcome="succeeded",
            reason="completed",
            run_id=run_id,
        )
        return descriptor

    def read_graph_result_artifact(
        self,
        ref: str,
        *,
        expected_run_id: str,
    ) -> dict[str, Any]:
        """Read only a verified internal graph-result ref-only artifact."""

        run_id: str | None = None
        try:
            run_id, artifact_type = self._parse_ref(ref)
            expected = validate_artifact_path_segment(
                expected_run_id,
                field="expected graph result run_id",
            )
            if run_id != expected:
                raise ArtifactRunBindingError(
                    "graph result ref run_id does not match the expected run"
                )
            manifest = self._artifact_metadata_projection(run_id)
            relative_path = self._canonical_path(artifact_type)
            if not _is_verified_graph_result_ref_only_artifact(
                manifest,
                artifact_type=artifact_type,
                path=relative_path,
            ):
                raise ArtifactStoreMetadataError(
                    "artifact is not a verified graph-result ref-only object"
                )
            result = self._read_artifact_payload(
                manifest,
                run_id=run_id,
                artifact_type=artifact_type,
            )
        except Exception as exc:
            emit_research_persistence_diagnostic(
                component="artifact_store",
                operation="graph_result_artifact_read",
                outcome="failed",
                reason=_artifact_failure_reason(exc),
                run_id=run_id,
            )
            raise
        emit_research_persistence_diagnostic(
            component="artifact_store",
            operation="graph_result_artifact_read",
            outcome="succeeded",
            reason="completed",
            run_id=run_id,
        )
        return result

    def read_diagnostic_artifact(
        self,
        ref: str,
        *,
        identity_scope_ref: str,
        subject_scope_ref: str | None = None,
    ) -> dict[str, Any]:
        """Read a current Graph manifest retained in explicit quarantine."""

        run_id: str | None = None
        try:
            run_id, artifact_type = self._parse_ref(ref)
            manifest = self._artifact_metadata_projection(run_id)
            schema_version = self._require_current_publication_schema(manifest)
            disposition = "quarantine"
            diagnostic_claim = ResearchArtifactDiagnosticClaim(
                run_id=run_id,
                schema_version=schema_version,
                disposition=disposition,
                identity_scope_ref=_validated_checksum_ref(
                    identity_scope_ref,
                    field="diagnostic identity scope",
                    artifact_id=run_id,
                ),
                subject_scope_ref=(
                    _validated_checksum_ref(
                        subject_scope_ref,
                        field="diagnostic subject scope",
                        artifact_id=run_id,
                    )
                    if subject_scope_ref is not None
                    else None
                ),
                artifact_type=artifact_type,
            )
            if not self._resolve_diagnostic_access(diagnostic_claim):
                raise ArtifactPublicationVisibilityError(
                    "Research artifact diagnostic scope is not authorized",
                    disposition=disposition,
                )
            result = self._read_artifact_payload(
                manifest,
                run_id=run_id,
                artifact_type=artifact_type,
            )
        except Exception as exc:
            emit_research_persistence_diagnostic(
                component="artifact_store",
                operation="artifact_read",
                outcome="failed",
                reason=_artifact_failure_reason(exc),
                run_id=run_id,
            )
            raise
        emit_research_persistence_diagnostic(
            component="artifact_store",
            operation="artifact_read",
            outcome="succeeded",
            reason="completed",
            run_id=run_id,
        )
        return result

    def _read_artifact(self, run_id: str, artifact_type: str) -> dict[str, Any]:
        try:
            terminal = self.terminal_store.read_terminal_manifest(run_id)
        except ArtifactNotFoundError:
            staged = self.terminal_store.read_staged_artifact(
                run_id=run_id,
                artifact_key=artifact_type,
            )
            if not is_verified_internal_staged_artifact(staged):
                raise ArtifactPublicationVisibilityError(
                    "Research artifact is not terminally published",
                    disposition="staging_only",
                )
            return self._read_artifact_payload(
                _artifact_projection(run_id, (staged,), staging_only=True),
                run_id=run_id,
                artifact_type=artifact_type,
            )
        if terminal.publication is None:
            raise ArtifactPublicationVisibilityError(
                "Research artifact has no terminal publication authority",
                disposition="quarantine",
            )
        manifest = _terminal_manifest_projection(terminal)
        self._require_current_publication_schema(manifest)
        claim = self._validated_v2_publication_claim(manifest, run_id=run_id)
        if not self._resolve_accepted_run(claim).accepted:
            raise ArtifactPublicationVisibilityError(
                "Research artifact requires a matching accepted run disposition",
                disposition="quarantine",
            )
        return self._read_artifact_payload(
            manifest,
            run_id=run_id,
            artifact_type=artifact_type,
        )

    def _read_artifact_payload(
        self,
        manifest: dict[str, Any],
        *,
        run_id: str,
        artifact_type: str,
    ) -> dict[str, Any]:
        storage_ref = self._validated_artifact_storage_ref(
            manifest,
            run_id=run_id,
            artifact_type=artifact_type,
        )
        content = self.store.read(storage_ref)
        try:
            payload = json.loads(
                content.decode("utf-8"),
                parse_constant=_reject_nonfinite_json,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactStoreMetadataError(
                f"invalid artifact JSON: {artifact_type}"
            ) from exc
        if not isinstance(payload, dict):
            raise ArtifactStoreMetadataError(
                f"invalid artifact JSON shape: {artifact_type}"
            )
        if payload.get("artifact_type") != artifact_type:
            raise ArtifactStoreMetadataError(
                f"artifact type mismatch: {artifact_type}"
            )
        payload_metadata = payload.get("metadata")
        if not isinstance(payload_metadata, dict):
            raise ArtifactStoreMetadataError(
                f"artifact metadata shape is invalid: {artifact_type}"
            )
        payload_run_id = payload_metadata.get("run_id")
        if payload_run_id is not None and payload_run_id != run_id:
            raise ArtifactStoreMetadataError(
                f"artifact payload run identity mismatch: {artifact_type}"
            )
        return payload

    def _validated_artifact_storage_ref(
        self,
        manifest: dict[str, Any],
        *,
        run_id: str,
        artifact_type: str,
    ) -> StorageArtifactRef:
        if (
            not isinstance(manifest.get("manifest_hash"), str)
            and manifest.get("authority_mode") != "staging_only"
        ):
            raise ArtifactStoreMetadataError(
                f"artifact manifest hash is missing: {run_id}"
            )
        relative_path = self._canonical_path(artifact_type)
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict) or artifacts.get(artifact_type) != relative_path:
            raise ArtifactStoreMetadataError(
                f"artifact manifest path mismatch: {artifact_type}"
            )

        metadata = self._manifest_metadata(
            manifest,
            artifact_type,
            run_id,
            relative_path,
        )
        checksum = validate_sha256_checksum(
            metadata["checksum"],
            artifact_id=artifact_type,
            field="artifact manifest checksum",
        )
        size_bytes = metadata["size_bytes"]
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise ArtifactStoreMetadataError(
                f"invalid artifact manifest size: {artifact_type}"
            )
        content_type = metadata["content_type"]
        if not isinstance(content_type, str) or not content_type.strip():
            raise ArtifactStoreMetadataError(
                f"invalid artifact manifest content_type: {artifact_type}"
            )
        self._enforce_size(size_bytes, artifact_type)
        return StorageArtifactRef(
            artifact_id=artifact_type,
            run_id=run_id,
            artifact_type=artifact_type,
            path=relative_path,
            content_type=content_type,
            size_bytes=size_bytes,
            checksum=checksum,
        )

    def _validated_v2_publication_claim(
        self,
        manifest: dict[str, Any],
        *,
        run_id: str,
    ) -> ResearchArtifactReadClaim:
        identity_scope_ref = _validated_checksum_ref(
            manifest.get("identity_scope_ref"),
            field="publication identity scope",
            artifact_id=run_id,
        )
        subject_scope_ref = _validated_checksum_ref(
            manifest.get("subject_scope_ref"),
            field="publication subject scope",
            artifact_id=run_id,
        )
        publication_authority_ref = _validated_checksum_ref(
            manifest.get("publication_authority_ref"),
            field="publication authority",
            artifact_id=run_id,
        )
        terminal_outcome_ref = _validated_checksum_ref(
            manifest.get("terminal_side_effect_outcome_ref"),
            field="terminal side-effect outcome",
            artifact_id=run_id,
        )

        artifact_types = self._v2_artifact_types(manifest, run_id=run_id)
        artifact_refs = {
            artifact_type: self._canonical_ref(run_id, artifact_type)
            for artifact_type in artifact_types
        }
        expected_artifact_evidence_ref = artifact_evidence_ref(artifact_refs)
        persisted_artifact_evidence_ref = _validated_checksum_ref(
            manifest.get("artifact_evidence_ref"),
            field="artifact evidence",
            artifact_id=run_id,
        )
        if persisted_artifact_evidence_ref != expected_artifact_evidence_ref:
            raise ArtifactStoreMetadataError(
                f"Research artifact evidence mismatch: {run_id}"
            )

        member_evidence: list[dict[str, Any]] = []
        for artifact_type in artifact_types:
            relative_path = self._canonical_path(artifact_type)
            metadata = self._manifest_metadata(
                manifest,
                artifact_type,
                run_id,
                relative_path,
            )
            checksum = validate_sha256_checksum(
                metadata.get("checksum"),
                artifact_id=artifact_type,
                field="artifact manifest checksum",
            )
            size_bytes = metadata.get("size_bytes")
            if not isinstance(size_bytes, int) or size_bytes < 0:
                raise ArtifactStoreMetadataError(
                    f"invalid artifact manifest size: {artifact_type}"
                )
            content_type = metadata.get("content_type")
            if not isinstance(content_type, str) or not content_type.strip():
                raise ArtifactStoreMetadataError(
                    f"invalid artifact manifest content_type: {artifact_type}"
                )
            if (
                metadata.get("identity_scope_ref") != identity_scope_ref
                or metadata.get("subject_scope_ref") != subject_scope_ref
                or metadata.get("publication_authority_ref")
                != publication_authority_ref
                or metadata.get("artifact_evidence_ref")
                != persisted_artifact_evidence_ref
            ):
                raise ArtifactStoreMetadataError(
                    f"Research artifact member authority mismatch: {artifact_type}"
                )
            member_evidence.append(
                {
                    "artifact_type": artifact_type,
                    "artifact_ref": artifact_refs[artifact_type],
                    "path": relative_path,
                    "checksum": f"sha256:{checksum}",
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                }
            )
            # Verify every member before asking the accepted-run resolver.  The
            # resolver therefore never authorizes a manifest whose group bytes
            # have drifted from its terminal outcome.
            self.store.read(
                StorageArtifactRef(
                    artifact_id=artifact_type,
                    run_id=run_id,
                    artifact_type=artifact_type,
                    path=relative_path,
                    content_type=content_type,
                    size_bytes=size_bytes,
                    checksum=checksum,
                )
            )

        expected_member_evidence_ref = artifact_member_evidence_ref(member_evidence)
        persisted_member_evidence_ref = _validated_checksum_ref(
            manifest.get("artifact_member_evidence_ref"),
            field="artifact member evidence",
            artifact_id=run_id,
        )
        if persisted_member_evidence_ref != expected_member_evidence_ref:
            raise ArtifactStoreMetadataError(
                f"Research artifact member evidence mismatch: {run_id}"
            )
        persisted_member_evidence = manifest.get("artifact_member_evidence")
        if not isinstance(persisted_member_evidence, list):
            raise ArtifactStoreMetadataError(
                f"Research artifact member evidence is missing: {run_id}"
            )
        if not all(isinstance(item, Mapping) for item in persisted_member_evidence):
            raise ArtifactStoreMetadataError(
                f"Research artifact member evidence projection is invalid: {run_id}"
            )
        if sorted(persisted_member_evidence, key=lambda item: str(item.get("artifact_type"))) != sorted(
            member_evidence,
            key=lambda item: str(item.get("artifact_type")),
        ):
            raise ArtifactStoreMetadataError(
                f"Research artifact member evidence projection mismatch: {run_id}"
            )
        if manifest.get("terminal_publication_authority_ref") != publication_authority_ref:
            raise ArtifactStoreMetadataError(
                f"Research terminal publication authority mismatch: {run_id}"
            )

        outcome = self._validated_terminal_outcome(
            manifest,
            run_id=run_id,
            identity_scope_ref=identity_scope_ref,
            subject_scope_ref=subject_scope_ref,
            publication_authority_ref=publication_authority_ref,
            terminal_outcome_ref=terminal_outcome_ref,
            artifact_refs=artifact_refs,
            artifact_evidence_ref_value=persisted_artifact_evidence_ref,
            member_evidence_ref=persisted_member_evidence_ref,
            member_evidence=member_evidence,
        )
        if outcome.checksum != terminal_outcome_ref:
            raise ArtifactStoreMetadataError(
                f"Research terminal outcome checksum mismatch: {run_id}"
            )
        return ResearchArtifactReadClaim(
            run_id=run_id,
            schema_version=RESEARCH_ARTIFACT_MANIFEST_VERSION,
            identity_scope_ref=identity_scope_ref,
            subject_scope_ref=subject_scope_ref,
            publication_authority_ref=publication_authority_ref,
            artifact_evidence_ref=persisted_artifact_evidence_ref,
            terminal_side_effect_outcome_ref=terminal_outcome_ref,
            artifact_refs=tuple(sorted(artifact_refs.items())),
            member_checksums=tuple(
                sorted(
                    (
                        member["artifact_type"],
                        member["checksum"],
                    )
                    for member in member_evidence
                )
            ),
        )

    @staticmethod
    def _require_current_publication_schema(manifest: dict[str, Any]) -> str:
        value = manifest.get("publication_schema_version")
        if value != RESEARCH_ARTIFACT_MANIFEST_VERSION:
            raise ArtifactPublicationVisibilityError(
                "Research artifact manifest is legacy or unsupported",
                disposition="legacy_quarantined",
            )
        return value

    def _v2_artifact_types(
        self,
        manifest: dict[str, Any],
        *,
        run_id: str,
    ) -> tuple[str, ...]:
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ArtifactStoreMetadataError(
                f"Research artifact manifest is invalid: {run_id}"
            )
        artifact_types: list[str] = []
        for artifact_type, path in artifacts.items():
            if artifact_type == "manifest":
                if path != "manifest.json":
                    raise ArtifactStoreMetadataError(
                        f"Research manifest path mismatch: {run_id}"
                    )
                continue
            if not isinstance(artifact_type, str):
                raise ArtifactStoreMetadataError(
                    f"Research artifact identity is invalid: {run_id}"
                )
            validate_artifact_path_segment(artifact_type, field="artifact_type")
            if _is_verified_context_ref_only_artifact(
                manifest,
                artifact_type=artifact_type,
                path=path,
            ) or _is_verified_graph_result_ref_only_artifact(
                manifest,
                artifact_type=artifact_type,
                path=path,
            ) or _is_verified_graph_event_projection_manifest_artifact(
                manifest,
                artifact_type=artifact_type,
                path=path,
            ):
                continue
            if path != self._canonical_path(artifact_type):
                raise ArtifactStoreMetadataError(
                    f"Research artifact path mismatch: {artifact_type}"
                )
            artifact_types.append(artifact_type)
        if not artifact_types:
            raise ArtifactStoreMetadataError(
                f"Research artifact manifest has no published members: {run_id}"
            )
        return tuple(sorted(artifact_types))

    @staticmethod
    def _validated_terminal_outcome(
        manifest: dict[str, Any],
        *,
        run_id: str,
        identity_scope_ref: str,
        subject_scope_ref: str,
        publication_authority_ref: str,
        terminal_outcome_ref: str,
        artifact_refs: dict[str, str],
        artifact_evidence_ref_value: str,
        member_evidence_ref: str,
        member_evidence: list[dict[str, Any]],
    ) -> HarnessSideEffectOutcome:
        payload = manifest.get("terminal_side_effect_outcome")
        if not isinstance(payload, dict):
            raise ArtifactStoreMetadataError(
                f"Research terminal outcome is missing: {run_id}"
            )
        try:
            outcome = HarnessSideEffectOutcome.from_dict(payload)
        except Exception as exc:
            raise ArtifactStoreMetadataError(
                f"Research terminal outcome is invalid: {run_id}"
            ) from exc
        if (
            outcome.checksum != terminal_outcome_ref
            or outcome.run_id != run_id
            or outcome.identity_scope_ref != identity_scope_ref
            or outcome.subject_scope_ref != subject_scope_ref
            or outcome.decision_ref != publication_authority_ref
            or outcome.disposition is not HarnessSideEffectDisposition.ACCEPTED
            or tuple(sorted(outcome.public_refs))
            != tuple(sorted(artifact_refs.values()))
        ):
            raise ArtifactStoreMetadataError(
                f"Research terminal outcome authority mismatch: {run_id}"
            )
        metadata = outcome.metadata
        if (
            dict(metadata.get("artifact_refs") or {}) != artifact_refs
            or metadata.get("publication_authority_ref")
            != publication_authority_ref
            or metadata.get("artifact_evidence_ref")
            != artifact_evidence_ref_value
            or metadata.get("artifact_member_evidence_ref")
            != member_evidence_ref
        ):
            raise ArtifactStoreMetadataError(
                f"Research terminal outcome evidence mismatch: {run_id}"
            )
        persisted_members = metadata.get("members")
        if not isinstance(persisted_members, (list, tuple)):
            raise ArtifactStoreMetadataError(
                f"Research terminal outcome members are missing: {run_id}"
            )
        expected_members = {
            member["artifact_type"]: {
                "checksum": member["checksum"].removeprefix("sha256:"),
                "size_bytes": member["size_bytes"],
                "content_type": member["content_type"],
                "canonical_path": member["path"],
            }
            for member in member_evidence
        }
        actual_members: dict[str, dict[str, Any]] = {}
        for item in persisted_members:
            if not isinstance(item, Mapping) or not isinstance(
                item.get("artifact_type"), str
            ):
                raise ArtifactStoreMetadataError(
                    f"Research terminal outcome member is invalid: {run_id}"
                )
            artifact_type = item["artifact_type"]
            actual_members[artifact_type] = {
                "checksum": item.get("checksum"),
                "size_bytes": item.get("size_bytes"),
                "content_type": item.get("content_type"),
                "canonical_path": item.get("canonical_path"),
            }
        if actual_members != expected_members:
            raise ArtifactStoreMetadataError(
                f"Research terminal outcome member evidence mismatch: {run_id}"
            )
        return outcome

    def _resolve_accepted_run(
        self,
        claim: ResearchArtifactReadClaim,
    ) -> ResearchArtifactReadResolution:
        resolver = self._accepted_run_resolver
        if resolver is None:
            return ResearchArtifactReadResolution(accepted=False)
        raw_resolution = _invoke_evidence_resolver(
            resolver,
            claim,
            legacy_args=(
                claim.run_id,
                claim.identity_scope_ref,
                claim.publication_authority_ref,
            ),
            expanded_args=(
                claim.run_id,
                claim.identity_scope_ref,
                claim.subject_scope_ref,
                claim.publication_authority_ref,
                claim.artifact_evidence_ref,
                claim.terminal_side_effect_outcome_ref,
                claim.member_checksums,
            ),
        )
        if isinstance(raw_resolution, ResearchArtifactReadResolution):
            resolution = raw_resolution
        else:
            accepted = bool(raw_resolution)
            resolution = ResearchArtifactReadResolution(
                accepted=accepted,
                identity_scope_ref=(
                    claim.identity_scope_ref if accepted else None
                ),
            )
        if not resolution.accepted:
            return ResearchArtifactReadResolution(accepted=False)
        resolved_scope = resolution.identity_scope_ref
        if resolved_scope is None:
            return resolution
        try:
            validated_scope = _validated_checksum_ref(
                resolved_scope,
                field="resolved accepted-run identity scope",
                artifact_id=claim.run_id,
            )
        except ArtifactStoreMetadataError:
            return ResearchArtifactReadResolution(accepted=False)
        if (
            claim.identity_scope_ref is not None
            and validated_scope != claim.identity_scope_ref
        ):
            return ResearchArtifactReadResolution(accepted=False)
        return ResearchArtifactReadResolution(
            accepted=True,
            identity_scope_ref=validated_scope,
        )

    def _resolve_diagnostic_access(
        self,
        claim: ResearchArtifactDiagnosticClaim,
    ) -> bool:
        resolver = self._diagnostic_run_resolver
        if resolver is None:
            return False
        return bool(
            _invoke_evidence_resolver(
                resolver,
                claim,
                legacy_args=(
                    claim.run_id,
                    claim.identity_scope_ref,
                    claim.subject_scope_ref,
                ),
                expanded_args=(
                    claim.run_id,
                    claim.identity_scope_ref,
                    claim.subject_scope_ref,
                    claim.disposition,
                    claim.artifact_type,
                ),
            )
        )

    def _existing_artifact_result(
        self,
        *,
        run_id: str,
        artifact_type: str,
        request: ArtifactWriteRequest,
        content: bytes,
        checksum: str,
    ) -> HarnessArtifactRef | None:
        try:
            manifest = self._artifact_metadata_projection(run_id)
        except ArtifactNotFoundError:
            return None
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            raise ArtifactStoreMetadataError(
                f"artifact metadata projection is invalid: {run_id}"
            )
        if artifact_type not in artifacts:
            if manifest.get("authority_mode") == "terminal":
                raise ArtifactWriteConflictError(
                    f"Graph terminal manifest already exists: {run_id}"
                )
            return None
        existing_path = artifacts.get(artifact_type)
        expected_path = self._canonical_path(artifact_type)
        if existing_path != expected_path:
            raise ArtifactStoreMetadataError(
                f"artifact manifest path mismatch: {artifact_type}"
            )
        metadata = self._manifest_metadata(
            manifest,
            artifact_type,
            run_id,
            expected_path,
        )
        existing_checksum = validate_sha256_checksum(
            metadata["checksum"],
            artifact_id=artifact_type,
            field="artifact manifest checksum",
        )
        existing_size = metadata.get("size_bytes")
        existing_content_type = metadata.get("content_type")
        if (
            existing_checksum != checksum
            or existing_size != len(content)
            or existing_content_type != request.media_type
        ):
            raise ArtifactWriteConflictError(
                f"immutable artifact already exists with different content: {artifact_type}"
            )
        existing_payload = self._read_artifact_payload(
            manifest,
            run_id=run_id,
            artifact_type=artifact_type,
        )
        if stable_json_dumps(existing_payload) != stable_json_dumps(request.to_dict()):
            raise ArtifactWriteConflictError(
                f"immutable artifact metadata conflicts: {artifact_type}"
            )
        return HarnessArtifactRef(
            ref=self._canonical_ref(run_id, artifact_type),
            artifact_type=artifact_type,
            checksum=f"sha256:{existing_checksum}",
            media_type=str(existing_content_type),
            metadata=dict(request.metadata),
        )

    def _require_bound_run(self) -> str:
        run_id = self._run_binding.get()
        if run_id is None:
            raise ArtifactRunBindingError("artifact write requires a bound run")
        return run_id

    def read_terminal_manifest(self, run_id: str) -> GraphTerminalManifestV2:
        return self.terminal_store.read_terminal_manifest(run_id)

    def write_terminal_manifest(
        self,
        manifest: GraphTerminalManifestV2,
    ) -> GraphTerminalManifestV2:
        return self.terminal_store.write_terminal_manifest(manifest)

    def commit_unpublished_terminal_manifest(
        self,
        request: GraphTerminalManifestCommitRequest,
    ) -> GraphTerminalManifestV2:
        if not isinstance(request, GraphTerminalManifestCommitRequest):
            raise TypeError("request must be GraphTerminalManifestCommitRequest")
        staged = self.terminal_store.list_staged_artifacts(request.run_id)
        invalid = tuple(
            artifact.artifact_key
            for artifact in staged
            if not is_verified_internal_staged_artifact(artifact)
        )
        if invalid:
            raise ArtifactWriteConflictError(
                "unpublished Graph terminal commit found non-internal staged artifacts"
            )
        return self.terminal_store.write_terminal_manifest(
            request.to_manifest(artifacts=staged)
        )

    def stage_graph_event_projection(
        self,
        projection: GraphEventProjection,
        *,
        graph_identity: GraphRunIdentity,
        tenant_id: str,
    ) -> GraphTerminalArtifact:
        """Stage one verified Graph event projection in terminal Artifact state."""

        if not isinstance(projection, GraphEventProjection):
            raise TypeError("projection must be GraphEventProjection")
        if not isinstance(graph_identity, GraphRunIdentity):
            raise TypeError("graph_identity must be GraphRunIdentity")
        if projection.graph_identity != graph_identity:
            raise ArtifactStoreMetadataError(
                "Graph event projection identity does not match terminal Graph"
            )
        tenant = str(tenant_id or "").strip()
        if not tenant:
            raise ArtifactStoreMetadataError("Graph event projection tenant is required")
        run_id = self._require_bound_run()
        if projection.graph_identity.run_id != run_id:
            raise ArtifactRunBindingError(
                "Graph event projection run does not match the bound run"
            )
        root = self.manager.run_dir(run_id).resolve(strict=False)
        path = Path(projection.path).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ArtifactStoreMetadataError(
                "Graph event projection path escapes the bound run"
            ) from exc
        if path.name != "events.jsonl":
            raise ArtifactStoreMetadataError(
                "Graph event projection must use events.jsonl"
            )
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ArtifactNotFoundError(
                f"Graph event projection is unavailable: {run_id}"
            ) from exc
        checksum = f"sha256:{compute_checksum(content)}"
        if checksum != projection.checksum:
            raise ArtifactChecksumMismatchError(
                "Graph event projection checksum does not match exported content"
            )
        relative_path = path.relative_to(root).as_posix()
        if relative_path != "events.jsonl":
            raise ArtifactStoreMetadataError(
                "Graph event projection relative path is invalid"
            )
        metadata = {
            "schema_version": "newsroom.graph-event-projection-artifact/v1",
            "graph_identity": graph_identity.to_dict(),
            "graph_execution_version": projection.execution_version.to_dict(),
            "tenant_id": tenant,
            "stream_id": projection.stream_id,
            "high_watermark": projection.high_watermark,
            "event_count": projection.event_count,
            "checksum": projection.checksum,
            "content_checksum": projection.checksum,
        }
        binding_evidence_ref = checksum_for(
            {
                "artifact_id": "event_projection",
                "content_checksum": projection.checksum,
                "graph_identity": graph_identity.to_dict(),
                "tenant_id": tenant,
                "stream_id": projection.stream_id,
                "high_watermark": projection.high_watermark,
            }
        )
        metadata["graph_artifact_binding"] = GraphArtifactBindingProjection.for_system(
            artifact_id="event_projection",
            evidence_ref=binding_evidence_ref,
            evidence_source=GraphArtifactBindingEvidenceSource.GRAPH_EVENT_PROJECTION,
        ).to_dict()
        artifact = GraphTerminalArtifact(
            artifact_key="event_projection",
            artifact_id="event_projection",
            ref=self._canonical_ref(run_id, "event_projection"),
            relative_path=relative_path,
            content_checksum=projection.checksum,
            byte_size=len(content),
            media_type="application/x-ndjson",
            node_id="graph",
            attempt_id=f"projection-{projection.high_watermark or 0}",
            required_for_replay=True,
            required_for_publication=True,
            metadata=metadata,
        )
        with self.lock_run(run_id):
            return self.terminal_store.stage_artifact(
                run_id=run_id,
                artifact=artifact,
            )

    def list_staged_artifacts(
        self,
        run_id: str,
    ) -> tuple[GraphTerminalArtifact, ...]:
        return self.terminal_store.list_staged_artifacts(run_id)

    def _artifact_metadata_projection(self, run_id: str) -> dict[str, Any]:
        try:
            manifest = self.terminal_store.read_terminal_manifest(run_id)
        except ArtifactNotFoundError:
            staged = self.terminal_store.list_staged_artifacts(run_id)
            if not staged:
                raise
            return _artifact_projection(run_id, staged, staging_only=True)
        return _terminal_manifest_projection(manifest)

    def _manifest_metadata(
        self,
        manifest: dict[str, Any],
        artifact_type: str,
        run_id: str,
        relative_path: str,
    ) -> dict[str, Any]:
        all_metadata = manifest.get("artifact_metadata")
        if not isinstance(all_metadata, dict):
            raise ArtifactStoreMetadataError(
                f"artifact manifest metadata is missing: {artifact_type}"
            )
        metadata = all_metadata.get(artifact_type)
        if not isinstance(metadata, dict):
            raise ArtifactStoreMetadataError(
                f"artifact manifest metadata is missing: {artifact_type}"
            )
        if metadata.get("artifact_id") != artifact_type:
            raise ArtifactStoreMetadataError(
                f"artifact manifest artifact identity mismatch: {artifact_type}"
            )
        if metadata.get("run_id") != run_id:
            raise ArtifactStoreMetadataError(
                f"artifact manifest run identity mismatch: {artifact_type}"
            )
        if metadata.get("path") != relative_path:
            raise ArtifactStoreMetadataError(
                f"artifact manifest path mismatch: {artifact_type}"
            )

        refs = manifest.get("artifact_refs")
        ref = _find_manifest_entry(refs, artifact_type)
        if ref is None:
            raise ArtifactStoreMetadataError(
                f"artifact manifest ref is missing: {artifact_type}"
            )
        if (ref.get("uri") or ref.get("path")) != relative_path:
            raise ArtifactStoreMetadataError(
                f"artifact manifest ref path mismatch: {artifact_type}"
            )
        if (ref.get("content_hash") or ref.get("checksum")) != metadata.get("checksum"):
            raise ArtifactStoreMetadataError(
                f"artifact manifest ref checksum mismatch: {artifact_type}"
            )
        if ref.get("size_bytes") != metadata.get("size_bytes"):
            raise ArtifactStoreMetadataError(
                f"artifact manifest ref size mismatch: {artifact_type}"
            )

        index = manifest.get("artifact_index")
        indexed = _find_manifest_entry(index, artifact_type)
        if indexed is None:
            raise ArtifactStoreMetadataError(
                f"artifact manifest index is missing: {artifact_type}"
            )
        if indexed.get("run_id") != run_id or indexed.get("path") != relative_path:
            raise ArtifactStoreMetadataError(
                f"artifact manifest index identity mismatch: {artifact_type}"
            )
        if indexed.get("checksum") != metadata.get("checksum"):
            raise ArtifactStoreMetadataError(
                f"artifact manifest index checksum mismatch: {artifact_type}"
            )
        if indexed.get("size_bytes") != metadata.get("size_bytes"):
            raise ArtifactStoreMetadataError(
                f"artifact manifest index size mismatch: {artifact_type}"
            )
        return metadata

    def _enforce_size(self, content_or_size: bytes | int, artifact_type: str) -> None:
        if self.max_write_bytes is None:
            return
        size = len(content_or_size) if isinstance(content_or_size, bytes) else content_or_size
        if size > self.max_write_bytes:
            raise ValueError(
                f"artifact exceeds max_write_bytes: {artifact_type} ({size} > {self.max_write_bytes})"
            )

    @staticmethod
    def _canonical_path(artifact_type: str) -> str:
        file_name = f"{artifact_type}.json"
        if len(file_name) > 80:
            file_name = f"a-{hash_text(artifact_type)}.json"
        return f"{CANONICAL_ARTIFACT_DIRECTORY}/{file_name}"

    @staticmethod
    def _canonical_ref(run_id: str, artifact_type: str) -> str:
        return f"{CANONICAL_ARTIFACT_SCHEME}://{run_id}/{artifact_type}"

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str, str]:
        if not isinstance(ref, str) or not ref:
            raise ArtifactStoreMetadataError("artifact ref is required")
        try:
            parsed = urlsplit(ref)
        except ValueError as exc:
            raise ArtifactStoreMetadataError("artifact ref is invalid") from exc
        if parsed.scheme != CANONICAL_ARTIFACT_SCHEME or parsed.query or parsed.fragment:
            raise ArtifactStoreMetadataError("artifact ref is not canonical")
        if not parsed.netloc or ":" in parsed.netloc or parsed.path.count("/") != 1:
            raise ArtifactStoreMetadataError("artifact ref is not canonical")
        run_id = validate_artifact_path_segment(parsed.netloc, field="artifact ref run_id")
        artifact_type = validate_artifact_path_segment(
            parsed.path.lstrip("/"),
            field="artifact ref artifact_type",
        )
        canonical = FilesystemHarnessArtifactPort._canonical_ref(run_id, artifact_type)
        if ref != canonical:
            raise ArtifactStoreMetadataError("artifact ref is not canonical")
        return run_id, artifact_type


def _find_manifest_entry(entries: Any, artifact_id: str) -> dict[str, Any] | None:
    if not isinstance(entries, list):
        return None
    for item in entries:
        if isinstance(item, dict) and item.get("artifact_id") == artifact_id:
            return item
    return None


def _terminal_artifact_from_request(
    *,
    run_id: str,
    artifact_type: str,
    relative_path: str,
    checksum: str,
    size_bytes: int,
    request: ArtifactWriteRequest,
) -> GraphTerminalArtifact:
    metadata = dict(request.metadata)
    node_id = metadata.get("node_id")
    attempt_id = metadata.get("attempt_id")
    if not isinstance(node_id, str) or not node_id.strip():
        node_id = "context" if metadata.get("context_ref_only") is True else "artifact"
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        attempt_id = (
            "context-1" if metadata.get("context_ref_only") is True else "staging-1"
        )
    required_for_replay = metadata.get("required_for_replay")
    if required_for_replay is None:
        required_for_replay = metadata.get("context_ref_only") is True
    required_for_publication = metadata.get("required_for_publication", False)
    if not isinstance(required_for_replay, bool) or not isinstance(
        required_for_publication,
        bool,
    ):
        raise ArtifactStoreMetadataError(
            f"invalid Graph artifact lifecycle flags: {artifact_type}"
        )
    return GraphTerminalArtifact(
        artifact_key=artifact_type,
        artifact_id=artifact_type,
        ref=FilesystemHarnessArtifactPort._canonical_ref(run_id, artifact_type),
        relative_path=relative_path,
        content_checksum=f"sha256:{checksum}",
        byte_size=size_bytes,
        media_type=request.media_type,
        node_id=node_id,
        attempt_id=attempt_id,
        required_for_replay=required_for_replay,
        required_for_publication=required_for_publication,
        metadata=metadata,
    )


def _artifact_projection(
    run_id: str,
    artifacts: tuple[GraphTerminalArtifact, ...],
    *,
    staging_only: bool,
) -> dict[str, Any]:
    paths: dict[str, str] = {}
    metadata: dict[str, dict[str, Any]] = {}
    references: list[dict[str, Any]] = []
    for artifact in artifacts:
        paths[artifact.artifact_key] = artifact.relative_path
        entry = {
            "artifact_id": artifact.artifact_id,
            "run_id": run_id,
            "kind": artifact.artifact_key,
            "artifact_type": artifact.artifact_key,
            "path": artifact.relative_path,
            "uri": artifact.relative_path,
            "content_type": artifact.media_type,
            "media_type": artifact.media_type,
            "size_bytes": artifact.byte_size,
            "checksum": artifact.content_checksum.removeprefix("sha256:"),
            "metadata": dict(artifact.metadata),
            **dict(artifact.metadata),
        }
        metadata[artifact.artifact_key] = dict(entry)
        references.append(entry)
    return {
        "run_id": run_id,
        "artifacts": paths,
        "artifact_metadata": metadata,
        "artifact_refs": list(references),
        "artifact_index": list(references),
        "manifest_hash": None,
        "authority_mode": "staging_only" if staging_only else "terminal",
    }


def _terminal_manifest_projection(
    manifest: GraphTerminalManifestV2,
) -> dict[str, Any]:
    projection = _artifact_projection(
        manifest.run_id,
        manifest.artifacts,
        staging_only=False,
    )
    projection.update(
        {
            "schema_version": manifest.schema_version,
            "publication_schema_version": RESEARCH_ARTIFACT_MANIFEST_VERSION,
            "status": manifest.status.value,
            "manifest_hash": manifest.manifest_hash,
        }
    )
    publication = manifest.publication
    if publication is None:
        return projection
    publication_metadata = publication.to_dict()["metadata"]
    projection.update(
        {
            "identity_scope_ref": publication.identity_scope_ref,
            "subject_scope_ref": publication.subject_scope_ref,
            "publication_authority_ref": publication.publication_authority_ref,
            "terminal_publication_authority_ref": (
                publication.publication_authority_ref
            ),
            "terminal_side_effect_outcome_ref": (
                publication.terminal_side_effect_outcome_ref
            ),
            "artifact_evidence_ref": publication.artifact_evidence_ref,
            "artifact_member_evidence_ref": (
                publication.artifact_member_evidence_ref
            ),
            "artifact_member_evidence": publication_metadata.get(
                "artifact_member_evidence"
            ),
            "terminal_side_effect_outcome": publication_metadata.get(
                "terminal_side_effect_outcome"
            ),
            "publication_committed_at": publication.committed_at,
        }
    )
    return projection


def _is_research_manifest(manifest: Mapping[str, Any]) -> bool:
    graph_identity = manifest.get("graph_identity")
    graph_id = (
        graph_identity.get("graph_id")
        if isinstance(graph_identity, Mapping)
        else manifest.get("graph_id")
    )
    return (
        manifest.get("run_type") == "research"
        or manifest.get("profile") == "research"
        or (isinstance(graph_id, str) and graph_id.startswith("research."))
    )


def _validated_checksum_ref(
    value: Any,
    *,
    field: str,
    artifact_id: str,
) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ArtifactStoreMetadataError(
            f"invalid {field} for artifact {artifact_id}"
        )
    digest = validate_sha256_checksum(
        value.removeprefix("sha256:"),
        artifact_id=artifact_id,
        field=field,
    )
    return f"sha256:{digest}"


def _optional_checksum_ref(
    value: Any,
    *,
    field: str,
    artifact_id: str,
) -> str | None:
    if value is None:
        return None
    return _validated_checksum_ref(value, field=field, artifact_id=artifact_id)


def _invoke_evidence_resolver(
    resolver: Callable[..., bool],
    claim: ResearchArtifactReadClaim | ResearchArtifactDiagnosticClaim,
    *,
    legacy_args: tuple[Any, ...],
    expanded_args: tuple[Any, ...],
) -> Any:
    """Call old and new resolver shapes without hiding resolver failures."""

    try:
        signature = inspect.signature(resolver)
    except (TypeError, ValueError):
        return resolver(claim)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if has_varargs:
        return resolver(*expanded_args)
    if len(positional) <= 1:
        return resolver(claim)
    if len(positional) > len(legacy_args):
        return resolver(*expanded_args[: len(positional)])
    return resolver(*legacy_args)


def _assert_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ArtifactStoreMetadataError("artifact JSON contains non-finite number")
    if isinstance(value, dict):
        for item in value.values():
            _assert_finite_json(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _assert_finite_json(item)


def _reject_nonfinite_json(value: str) -> Any:
    raise ArtifactStoreMetadataError(
        f"artifact JSON contains non-finite number: {value}"
    )


def _artifact_failure_reason(exc: Exception) -> str:
    if isinstance(exc, ArtifactRunBindingError):
        return "run_binding_invalid"
    if isinstance(exc, ArtifactWriteConflictError):
        return "write_conflict"
    if isinstance(exc, ArtifactChecksumMismatchError):
        return "checksum_invalid"
    if isinstance(exc, ArtifactStoreMetadataError):
        return "metadata_invalid"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_input"
    if isinstance(exc, OSError):
        return "filesystem_unavailable"
    return "other"


def _is_verified_context_ref_only_artifact(
    manifest: Mapping[str, Any],
    *,
    artifact_type: Any,
    path: Any,
) -> bool:
    if not isinstance(artifact_type, str) or not isinstance(path, str):
        return False
    identity_suffix: str | None = None
    for allowed_type in _CONTEXT_REF_ONLY_ARTIFACT_TYPES:
        prefix = f"{allowed_type}-"
        if artifact_type.startswith(prefix):
            identity_suffix = artifact_type.removeprefix(prefix)
            break
    if identity_suffix is None or len(identity_suffix) != 64:
        return False
    try:
        int(identity_suffix, 16)
    except ValueError:
        return False
    artifact_index = manifest.get("artifact_index")
    if not isinstance(artifact_index, list):
        return False
    matches = [
        item
        for item in artifact_index
        if isinstance(item, Mapping)
        and item.get("artifact_id") == artifact_type
        and item.get("kind", item.get("artifact_key")) == artifact_type
        and item.get("path", item.get("relative_path")) == path
    ]
    if len(matches) != 1:
        return False
    metadata = matches[0].get("metadata")
    return (
        isinstance(metadata, Mapping)
        and metadata.get("context_ref_only") is True
        and metadata.get("identity_checksum") == f"sha256:{identity_suffix}"
    )


def _is_verified_graph_result_ref_only_artifact(
    manifest: Mapping[str, Any],
    *,
    artifact_type: Any,
    path: Any,
) -> bool:
    if (
        not isinstance(artifact_type, str)
        or not artifact_type.startswith(_GRAPH_RESULT_REF_ONLY_PREFIX)
        or not isinstance(path, str)
    ):
        return False
    identity_suffix = artifact_type.removeprefix(_GRAPH_RESULT_REF_ONLY_PREFIX)
    if len(identity_suffix) != 64:
        return False
    try:
        int(identity_suffix, 16)
    except ValueError:
        return False
    artifact_index = manifest.get("artifact_index")
    if not isinstance(artifact_index, list):
        return False
    matches = [
        item
        for item in artifact_index
        if isinstance(item, Mapping)
        and item.get("artifact_id") == artifact_type
        and item.get("kind", item.get("artifact_key")) == artifact_type
        and item.get("path", item.get("relative_path")) == path
    ]
    if len(matches) != 1:
        return False
    metadata = matches[0].get("metadata")
    return (
        isinstance(metadata, Mapping)
        and metadata.get("graph_result_ref_only") is True
        and metadata.get("identity_checksum") == f"sha256:{identity_suffix}"
    )


def _is_verified_graph_event_projection_manifest_artifact(
    manifest: Mapping[str, Any],
    *,
    artifact_type: Any,
    path: Any,
) -> bool:
    if artifact_type != "event_projection" or path != "events.jsonl":
        return False
    artifact_index = manifest.get("artifact_index")
    if not isinstance(artifact_index, list):
        return False
    matches = [
        item
        for item in artifact_index
        if isinstance(item, Mapping)
        and item.get("artifact_id") == artifact_type
        and item.get("kind", item.get("artifact_key")) == artifact_type
        and item.get("path", item.get("relative_path")) == path
    ]
    if len(matches) != 1:
        return False
    item = matches[0]
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    identity = metadata.get("graph_identity")
    if not isinstance(identity, Mapping):
        return False
    # The projection carries the immutable Graph run identity.  Execution
    # schema/compiler details live in ``graph_execution_version`` and must not
    # be duplicated into the identity payload.
    required_identity = {
        "run_id",
        "graph_id",
        "graph_version",
        "graph_ref",
        "graph_checksum",
    }
    if set(identity) != required_identity:
        return False
    run_id = manifest.get("run_id")
    checksum = item.get("checksum") or item.get("content_checksum")
    checksum_ref = (
        checksum
        if isinstance(checksum, str) and checksum.startswith("sha256:")
        else f"sha256:{checksum}"
    )
    content_type = item.get("content_type", item.get("media_type"))
    return (
        metadata.get("schema_version")
        == "newsroom.graph-event-projection-artifact/v1"
        and metadata.get("stream_id") == f"run:{run_id}"
        and metadata.get("tenant_id") not in (None, "")
        and identity.get("run_id") == run_id
        and identity.get("graph_ref")
        == f"{identity.get('graph_id')}@{identity.get('graph_version')}"
        and checksum is not None
        and metadata.get("checksum") == checksum_ref
        and metadata.get("content_checksum") == checksum_ref
        and content_type == "application/x-ndjson"
        and isinstance(metadata.get("graph_execution_version"), Mapping)
        and metadata["graph_execution_version"].get(
            "normalized_graph_checksum"
        )
        == identity.get("graph_checksum")
    )


def is_verified_internal_staged_artifact(
    artifact: GraphTerminalArtifact,
) -> bool:
    """Return whether a staged descriptor is an approved internal Graph member."""

    if not isinstance(artifact, GraphTerminalArtifact):
        return False
    if artifact.artifact_key == "event_projection":
        return _is_verified_graph_event_projection_artifact(artifact)
    metadata = artifact.metadata
    context_only = metadata.get("context_ref_only") is True
    graph_result_only = metadata.get("graph_result_ref_only") is True
    if context_only == graph_result_only:
        return False
    identity_suffix: str | None = None
    if context_only:
        for allowed_type in _CONTEXT_REF_ONLY_ARTIFACT_TYPES:
            prefix = f"{allowed_type}-"
            if artifact.artifact_key.startswith(prefix):
                identity_suffix = artifact.artifact_key.removeprefix(prefix)
                break
    elif artifact.artifact_key.startswith(_GRAPH_RESULT_REF_ONLY_PREFIX):
        identity_suffix = artifact.artifact_key.removeprefix(
            _GRAPH_RESULT_REF_ONLY_PREFIX
        )
    if identity_suffix is None or len(identity_suffix) != 64:
        return False
    try:
        int(identity_suffix, 16)
    except ValueError:
        return False
    return metadata.get("identity_checksum") == f"sha256:{identity_suffix}"


def _is_verified_graph_event_projection_artifact(
    artifact: GraphTerminalArtifact,
) -> bool:
    metadata = artifact.metadata
    if (
        metadata.get("schema_version")
        != "newsroom.graph-event-projection-artifact/v1"
        or artifact.artifact_id != "event_projection"
        or artifact.relative_path != "events.jsonl"
        or artifact.media_type != "application/x-ndjson"
        or metadata.get("stream_id") != f"run:{artifact.ref.split('/')[-2]}"
        or metadata.get("checksum") != artifact.content_checksum
        or metadata.get("content_checksum") != artifact.content_checksum
        or metadata.get("tenant_id") in (None, "")
    ):
        return False
    identity = metadata.get("graph_identity")
    if not isinstance(identity, Mapping):
        return False
    required_identity = {
        "run_id",
        "graph_id",
        "graph_version",
        "graph_ref",
        "graph_checksum",
    }
    if set(identity) != required_identity:
        return False
    if identity.get("run_id") != artifact.ref.split("/")[-2]:
        return False
    if identity.get("graph_ref") != (
        f"{identity.get('graph_id')}@{identity.get('graph_version')}"
    ):
        return False
    execution_version = metadata.get("graph_execution_version")
    if not isinstance(execution_version, Mapping):
        return False
    if set(execution_version) != {
        "graph_schema_version",
        "compiler_version",
        "normalized_graph_checksum",
    }:
        return False
    if execution_version.get("normalized_graph_checksum") != identity.get(
        "graph_checksum"
    ):
        return False
    high_watermark = metadata.get("high_watermark")
    event_count = metadata.get("event_count")
    if high_watermark is not None and (
        isinstance(high_watermark, bool)
        or not isinstance(high_watermark, int)
        or high_watermark < 1
    ):
        return False
    if (
        isinstance(event_count, bool)
        or not isinstance(event_count, int)
        or event_count < 0
        or event_count != (high_watermark or 0)
    ):
        return False
    return True


__all__ = [
    "ArtifactRunBindingError",
    "ArtifactPublicationVisibilityError",
    "ArtifactWriteConflictError",
    "CANONICAL_ARTIFACT_DIRECTORY",
    "CANONICAL_ARTIFACT_SCHEME",
    "FilesystemHarnessArtifactPort",
    "is_verified_internal_staged_artifact",
]
