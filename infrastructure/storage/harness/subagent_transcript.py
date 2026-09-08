from __future__ import annotations

import os
import stat
import time
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from framework.agent.artifacts.paths import (
    ArtifactPathError,
    resolve_artifact_descendant,
    validate_artifact_path_segment,
)
from framework.agent.artifacts.stores.errors import ArtifactStoreMetadataError
from framework.agent.artifacts.stores.fs_safety import (
    is_link_or_reparse_point,
    reject_link_chain,
    verified_atomic_create,
    verified_exclusive_file_lock,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.observability import (
    DEFAULT_SUBAGENT_TRANSCRIPT_OBSERVATION_SINK,
    SUBAGENT_TRANSCRIPT_BYTES,
    SUBAGENT_TRANSCRIPT_COMMIT_FAILED,
    SUBAGENT_TRANSCRIPT_COMMIT_LATENCY_MS,
    SUBAGENT_TRANSCRIPT_COMMIT_SUCCEEDED,
    SUBAGENT_TRANSCRIPT_CONFLICT,
    SUBAGENT_TRANSCRIPT_CORRUPT,
    SUBAGENT_TRANSCRIPT_VERIFY_FAILED,
    SubAgentTranscriptObservation,
    SubAgentTranscriptObservationSink,
    record_subagent_transcript_observation,
)
from framework.harness.subagents.transcript import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_TRANSCRIPT_BYTES,
    MAX_PARENT_QUERY,
    SUBAGENT_BUNDLE_SCHEMA_V3,
    SUBAGENT_RECEIPT_SCHEMA_V3,
    SubAgentAttemptIdentity,
    SubAgentContextEvidence,
    SubAgentOutputDocument,
    SubAgentTranscript,
    SubAgentTranscriptConflictError,
    SubAgentTranscriptCorruptError,
    SubAgentTranscriptReceipt,
    SubAgentTranscriptStoreError,
    _bundle_schema_for_identity,
    _context_ref,
    _receipt_matches_bundle,
    _validate_bundle_identity,
)
from framework.harness.subagents.transcript_metadata import (
    SUBAGENT_ATTEMPT_DESCRIPTOR_SCHEMA_V1,
    SubAgentAttemptDescriptor,
    SubAgentAttemptMetadataIncompleteError,
    descriptor_for_bundle,
)
from framework.shared.json import json_loads, stable_json_dumps
from framework.shared.time import utc_now


class FilesystemSubAgentTranscriptStore:
    """Run-scoped immutable durable store for subagent attempt evidence."""

    is_durable = True

    def __init__(
        self,
        root: str | Path,
        *,
        max_transcript_bytes: int = DEFAULT_MAX_TRANSCRIPT_BYTES,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
        clock: Callable[[], Any] = utc_now,
        monotonic: Callable[[], float] = time.perf_counter,
        observation_sink: SubAgentTranscriptObservationSink | None = (
            DEFAULT_SUBAGENT_TRANSCRIPT_OBSERVATION_SINK
        ),
    ) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.max_transcript_bytes = _positive_limit(max_transcript_bytes, "max_transcript_bytes")
        self.max_output_bytes = _positive_limit(max_output_bytes, "max_output_bytes")
        self.max_bundle_bytes = _positive_limit(max_bundle_bytes, "max_bundle_bytes")
        if self.max_bundle_bytes < self.max_transcript_bytes or self.max_bundle_bytes < self.max_output_bytes:
            raise ValueError("max_bundle_bytes must cover each document limit")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        if observation_sink is not None and not isinstance(
            observation_sink,
            SubAgentTranscriptObservationSink,
        ):
            raise TypeError(
                "observation_sink must implement SubAgentTranscriptObservationSink"
            )
        self._clock = clock
        self._monotonic = monotonic
        self._observation_sink = observation_sink
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            root_info = os.lstat(self.root)
        except OSError as exc:
            raise _store_error(
                "subagent_transcript_store_unavailable",
                "create subagent transcript root failed",
                root=str(self.root),
            ) from exc
        if is_link_or_reparse_point(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise _store_error(
                "subagent_transcript_store_unavailable",
                "subagent transcript root must be a real directory",
                root=str(self.root),
            )

    def write(
        self,
        context: SubAgentContextEvidence,
        output: SubAgentOutputDocument,
        transcript: SubAgentTranscript,
    ) -> SubAgentTranscriptReceipt:
        started_at = self._monotonic()
        _validate_bundle_identity(context, output, transcript)
        identity = transcript.identity
        content = b""
        try:
            path = self._bundle_path(identity.parent_run_id, identity.transcript_id)
            metadata_path = self._metadata_path(
                identity.parent_run_id,
                identity.transcript_id,
            )
            lock_path = self._lock_path(identity.parent_run_id, identity.transcript_id)
            try:
                with verified_exclusive_file_lock(
                    lock_path,
                    root=self.root,
                    identity=f"{identity.parent_run_id}/{identity.transcript_id}",
                ):
                    descriptor = self.describe_attempt(identity)
                    if descriptor is not None:
                        receipt = descriptor.receipt
                        content = self._bundle_content(context, output, transcript, receipt)
                        self._enforce_sizes(context, output, transcript, content)
                        candidate = descriptor_for_bundle(
                            identity=identity,
                            receipt=receipt,
                            artifact_refs=output.artifact_refs,
                            bundle=content,
                        )
                        if candidate != descriptor:
                            raise SubAgentTranscriptConflictError(
                                "subagent transcript identity already has different content",
                                code="subagent_transcript_conflict",
                                details={"transcript_id": identity.transcript_id},
                            )
                        existing = self._read_bundle(path)
                        if (
                            existing[0] != context
                            or existing[1] != output
                            or existing[2] != transcript
                        ):
                            raise SubAgentTranscriptConflictError(
                                "subagent transcript identity already has different content",
                                code="subagent_transcript_conflict",
                                details={"transcript_id": identity.transcript_id},
                            )
                        committed = self.verify(receipt)
                    else:
                        receipt = self._new_receipt(context, output, transcript)
                        content = self._bundle_content(context, output, transcript, receipt)
                        self._enforce_sizes(context, output, transcript, content)
                        descriptor = descriptor_for_bundle(
                            identity=identity,
                            receipt=receipt,
                            artifact_refs=output.artifact_refs,
                            bundle=content,
                        )
                        metadata_content = (
                            stable_json_dumps(descriptor.to_dict()) + "\n"
                        ).encode("utf-8")
                        if len(metadata_content) > self.max_transcript_bytes:
                            raise _store_error(
                                "subagent_transcript_size_exceeded",
                                "subagent attempt descriptor exceeds configured size",
                                exceeded={"descriptor": {
                                    "size": len(metadata_content),
                                    "limit": self.max_transcript_bytes,
                                }},
                            )
                        metadata_created = verified_atomic_create(
                            metadata_path,
                            metadata_content,
                            root=self.root,
                            identity=f"{identity.parent_run_id}/{identity.transcript_id}/metadata",
                        )
                        if not metadata_created:
                            raise _corrupt(
                                "subagent attempt metadata appeared during serialized commit"
                            )
                        bundle_created = verified_atomic_create(
                            path,
                            content,
                            root=self.root,
                            identity=f"{identity.parent_run_id}/{identity.transcript_id}",
                        )
                        if not bundle_created:
                            raise _corrupt(
                                "subagent attempt bundle appeared during serialized commit"
                            )
                        committed = self.verify(receipt)
            except (ArtifactStoreMetadataError, OSError) as exc:
                raise _store_error(
                    "subagent_transcript_store_unavailable",
                    "commit subagent transcript bundle failed",
                    transcript_id=identity.transcript_id,
                ) from exc
        except Exception as exc:
            reason_code = _reason_code(exc)
            if isinstance(exc, SubAgentTranscriptConflictError):
                self._observe(
                    SUBAGENT_TRANSCRIPT_CONFLICT,
                    identity,
                    reason_code=reason_code,
                )
            self._observe(
                SUBAGENT_TRANSCRIPT_COMMIT_FAILED,
                identity,
                reason_code=reason_code,
            )
            raise
        self._observe(
            SUBAGENT_TRANSCRIPT_COMMIT_SUCCEEDED,
            identity,
            receipt=committed,
            value=1,
        )
        self._observe(
            SUBAGENT_TRANSCRIPT_BYTES,
            identity,
            receipt=committed,
            value=len(content),
        )
        self._observe(
            SUBAGENT_TRANSCRIPT_COMMIT_LATENCY_MS,
            identity,
            receipt=committed,
            value=max(0.0, (self._monotonic() - started_at) * 1000),
        )
        return committed

    def read(self, transcript_ref: str) -> SubAgentTranscript:
        _, parent, transcript_id = _parse_ref(transcript_ref, "subagent-transcript")
        transcript = self._read_bundle(self._bundle_path(parent, transcript_id))[2]
        if transcript.ref != transcript_ref:
            raise _corrupt("subagent transcript ref does not match stored identity")
        return transcript

    def read_context(self, context_ref: str) -> SubAgentContextEvidence:
        _, parent, transcript_id = _parse_ref(context_ref, "subagent-context")
        context = self._read_bundle(self._bundle_path(parent, transcript_id))[0]
        expected = _context_ref(context.identity, context.schema_version)
        if context_ref != expected:
            raise _corrupt("subagent context ref does not match stored identity")
        return context

    def read_output(self, output_ref: str) -> SubAgentOutputDocument:
        _, parent, transcript_id = _parse_ref(output_ref, "subagent-output")
        output = self._read_bundle(self._bundle_path(parent, transcript_id))[1]
        if output.ref != output_ref:
            raise _corrupt("subagent output ref does not match stored identity")
        return output

    def verify(self, receipt: SubAgentTranscriptReceipt) -> SubAgentTranscriptReceipt:
        if not isinstance(receipt, SubAgentTranscriptReceipt):
            raise TypeError("receipt must be SubAgentTranscriptReceipt")
        try:
            _, parent, transcript_id = _parse_ref(
                receipt.transcript_ref,
                "subagent-transcript",
            )
            if parent != receipt.parent_run_id or transcript_id != receipt.transcript_id:
                raise _corrupt(
                    "subagent receipt ref identity mismatch",
                    code="subagent_transcript_identity_mismatch",
                )
            context, output, transcript, stored = self._read_bundle(
                self._bundle_path(parent, transcript_id)
            )
            if stored != receipt:
                raise _corrupt("subagent receipt does not match committed bundle")
            _validate_bundle_identity(context, output, transcript)
            if not _receipt_matches_bundle(receipt, context, output, transcript):
                raise _corrupt("subagent receipt checksum or ref mismatch")
            return receipt
        except Exception as exc:
            reason_code = _reason_code(exc)
            self._observe_receipt_failure(
                SUBAGENT_TRANSCRIPT_VERIFY_FAILED,
                receipt,
                reason_code=reason_code,
            )
            if _is_corrupt_reason(reason_code):
                self._observe_receipt_failure(
                    SUBAGENT_TRANSCRIPT_CORRUPT,
                    receipt,
                    reason_code=reason_code,
                )
            raise

    def refs_for_parent(
        self,
        parent_run_id: str,
        *,
        limit: int = MAX_PARENT_QUERY,
    ) -> tuple[str, ...]:
        parent = _path_segment(parent_run_id, "parent_run_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > MAX_PARENT_QUERY:
            raise HarnessValidationError(
                "transcript parent query limit is invalid",
                code="subagent_transcript_query_limit_invalid",
            )
        directory = self._parent_dir(parent)
        if not directory.exists():
            return ()
        try:
            reject_link_chain(
                directory,
                root=self.root,
                identity=parent,
                role="subagent transcript parent query",
            )
            refs: list[str] = []
            for candidate in sorted(directory.glob("sat_*.json"), key=lambda item: item.name):
                if len(refs) >= limit:
                    break
                transcript_id = candidate.stem
                transcript = self._read_bundle(candidate)[2]
                if transcript.identity.parent_run_id != parent or transcript.transcript_id != transcript_id:
                    raise _corrupt("subagent parent query found mismatched bundle")
                refs.append(transcript.ref)
            return tuple(refs)
        except SubAgentTranscriptStoreError:
            raise
        except (ArtifactStoreMetadataError, OSError) as exc:
            raise _store_error(
                "subagent_transcript_store_unavailable",
                "query subagent transcript parent failed",
                parent_run_id=parent,
            ) from exc

    def find_by_identity(
        self,
        identity: SubAgentAttemptIdentity,
    ) -> SubAgentTranscriptReceipt | None:
        if not isinstance(identity, SubAgentAttemptIdentity):
            raise TypeError("identity must be SubAgentAttemptIdentity")
        descriptor = self.describe_attempt(identity)
        if descriptor is None:
            return None
        receipt = descriptor.receipt
        transcript = self.read(receipt.transcript_ref)
        if transcript.identity != identity:
            raise _corrupt("subagent identity lookup resolved a different attempt")
        return self.verify(receipt)

    def describe_attempt(
        self,
        identity: SubAgentAttemptIdentity,
    ) -> SubAgentAttemptDescriptor | None:
        if not isinstance(identity, SubAgentAttemptIdentity):
            raise TypeError("identity must be SubAgentAttemptIdentity")
        descriptor = self._describe_paths(
            self._bundle_path(identity.parent_run_id, identity.transcript_id),
            self._metadata_path(identity.parent_run_id, identity.transcript_id),
        )
        if descriptor is not None and descriptor.identity != identity:
            raise _corrupt(
                "subagent attempt descriptor resolved a different attempt",
                code="subagent_transcript_identity_mismatch",
            )
        return descriptor

    def describe_ref(self, ref: str) -> SubAgentAttemptDescriptor | None:
        kind = next(
            (
                candidate
                for candidate in (
                    "subagent-transcript",
                    "subagent-context",
                    "subagent-output",
                )
                if isinstance(ref, str) and ref.startswith(f"{candidate}://")
            ),
            None,
        )
        if kind is None:
            raise _store_error(
                "subagent_transcript_identity_mismatch",
                "subagent evidence ref is invalid",
                ref=str(ref),
            )
        _, parent, transcript_id = _parse_ref(ref, kind)
        descriptor = self._describe_paths(
            self._bundle_path(parent, transcript_id),
            self._metadata_path(parent, transcript_id),
        )
        if descriptor is None:
            return None
        expected = {
            "subagent-transcript": descriptor.receipt.transcript_ref,
            "subagent-context": descriptor.receipt.context_ref,
            "subagent-output": descriptor.receipt.output_ref,
        }[kind]
        if ref != expected:
            raise _corrupt(
                "subagent evidence ref does not match its descriptor",
                code="subagent_transcript_identity_mismatch",
            )
        return descriptor

    def _read_bundle(
        self,
        path: Path,
    ) -> tuple[
        SubAgentContextEvidence,
        SubAgentOutputDocument,
        SubAgentTranscript,
        SubAgentTranscriptReceipt,
    ]:
        try:
            descriptor = self._describe_paths(path, self._metadata_path_from_bundle(path))
            if descriptor is None:
                raise _store_error(
                    "subagent_transcript_not_found",
                    "subagent transcript bundle was not found",
                    path=str(path),
                )
            reject_link_chain(
                path,
                root=self.root,
                identity=path.name,
                role="subagent transcript read",
            )
            before = os.lstat(path)
            if is_link_or_reparse_point(before) or not stat.S_ISREG(before.st_mode):
                raise _corrupt("subagent transcript bundle is not a regular file")
            if before.st_size > self.max_bundle_bytes:
                raise _store_error(
                    "subagent_transcript_size_exceeded",
                    "subagent transcript bundle exceeds size limit",
                    size_bytes=before.st_size,
                )
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not os.path.samestat(before, opened):
                    raise _corrupt("subagent transcript bundle changed while opening")
                content = handle.read(self.max_bundle_bytes + 1)
            if len(content) > self.max_bundle_bytes:
                raise _store_error(
                    "subagent_transcript_size_exceeded",
                    "subagent transcript bundle exceeds size limit",
                )
            reject_link_chain(
                path,
                root=self.root,
                identity=path.name,
                role="subagent transcript read",
            )
            if (
                len(content) != descriptor.bundle_size_bytes
                or f"sha256:{sha256(content).hexdigest()}" != descriptor.bundle_checksum
            ):
                raise _corrupt("subagent transcript bundle does not match trusted metadata")
            payload = json_loads(content.decode("utf-8"))
            if not isinstance(payload, Mapping) or set(payload) != {
                "schema_version", "context", "output", "transcript", "receipt"
            }:
                raise _corrupt("subagent transcript bundle fields are invalid")
            if payload["schema_version"] != SUBAGENT_BUNDLE_SCHEMA_V3:
                raise _corrupt("subagent transcript bundle schema is unsupported", code="subagent_transcript_schema_unsupported")
            context = SubAgentContextEvidence.from_dict(_object(payload["context"], "context"))
            output = SubAgentOutputDocument.from_dict(_object(payload["output"], "output"))
            transcript = SubAgentTranscript.from_dict(_object(payload["transcript"], "transcript"))
            receipt = SubAgentTranscriptReceipt.from_dict(_object(payload["receipt"], "receipt"))
            _validate_bundle_identity(context, output, transcript)
            if payload["schema_version"] != _bundle_schema_for_identity(transcript.identity):
                raise _corrupt(
                    "subagent transcript bundle schema does not match its identity",
                    code="subagent_transcript_identity_mismatch",
                )
            if not _receipt_matches_bundle(receipt, context, output, transcript):
                raise _corrupt("subagent receipt checksum or ref mismatch")
            if receipt != descriptor.receipt or output.artifact_refs != descriptor.artifact_refs:
                raise _corrupt("subagent bundle identity does not match trusted metadata")
            self._enforce_sizes(context, output, transcript, content)
            return context, output, transcript, receipt
        except SubAgentTranscriptStoreError:
            raise
        except FileNotFoundError as exc:
            raise _store_error(
                "subagent_transcript_not_found",
                "subagent transcript bundle was not found",
                path=str(path),
            ) from exc
        except (ArtifactStoreMetadataError, HarnessValidationError, UnicodeError, ValueError, OSError) as exc:
            raise _corrupt("subagent transcript bundle is corrupt") from exc

    def _describe_paths(
        self,
        bundle_path: Path,
        metadata_path: Path,
    ) -> SubAgentAttemptDescriptor | None:
        try:
            bundle_stat = self._regular_stat_or_none(bundle_path, role="bundle metadata")
            metadata_stat = self._regular_stat_or_none(
                metadata_path,
                role="attempt descriptor",
            )
            if bundle_stat is None and metadata_stat is None:
                return None
            if bundle_stat is None or metadata_stat is None:
                raise SubAgentAttemptMetadataIncompleteError(
                    "subagent attempt has incomplete immutable metadata",
                    code="subagent_attempt_metadata_incomplete",
                    details={
                        "bundle_present": bundle_stat is not None,
                        "metadata_present": metadata_stat is not None,
                    },
                )
            if metadata_stat.st_size > self.max_transcript_bytes:
                raise _corrupt("subagent attempt descriptor exceeds size limit")
            with metadata_path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not os.path.samestat(metadata_stat, opened):
                    raise _corrupt("subagent attempt descriptor changed while opening")
                content = handle.read(self.max_transcript_bytes + 1)
            if len(content) > self.max_transcript_bytes:
                raise _corrupt("subagent attempt descriptor exceeds size limit")
            reject_link_chain(
                metadata_path,
                root=self.root,
                identity=metadata_path.name,
                role="subagent attempt descriptor read",
            )
            after = os.lstat(metadata_path)
            if not os.path.samestat(metadata_stat, after):
                raise _corrupt("subagent attempt descriptor changed while reading")
            payload = json_loads(content.decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise _corrupt("subagent attempt descriptor must be an object")
            descriptor = SubAgentAttemptDescriptor.from_dict(payload)
            if descriptor.schema_version != SUBAGENT_ATTEMPT_DESCRIPTOR_SCHEMA_V1:
                raise _corrupt("subagent attempt descriptor schema is unsupported")
            if descriptor.bundle_size_bytes != bundle_stat.st_size:
                raise _corrupt("subagent bundle size does not match trusted metadata")
            expected_bundle = self._bundle_path(
                descriptor.identity.parent_run_id,
                descriptor.identity.transcript_id,
            )
            expected_metadata = self._metadata_path(
                descriptor.identity.parent_run_id,
                descriptor.identity.transcript_id,
            )
            if expected_bundle != bundle_path or expected_metadata != metadata_path:
                raise _corrupt(
                    "subagent attempt descriptor path identity mismatch",
                    code="subagent_transcript_identity_mismatch",
                )
            reject_link_chain(
                bundle_path,
                root=self.root,
                identity=bundle_path.name,
                role="subagent bundle metadata",
            )
            bundle_after = os.lstat(bundle_path)
            if not os.path.samestat(bundle_stat, bundle_after):
                raise _corrupt("subagent bundle changed during metadata inspection")
            return descriptor
        except SubAgentTranscriptStoreError:
            raise
        except FileNotFoundError as exc:
            raise SubAgentAttemptMetadataIncompleteError(
                "subagent attempt metadata changed during inspection",
                code="subagent_attempt_metadata_incomplete",
            ) from exc
        except (ArtifactStoreMetadataError, HarnessValidationError, UnicodeError, ValueError, OSError) as exc:
            raise _corrupt("subagent attempt descriptor is corrupt") from exc

    def _regular_stat_or_none(
        self,
        path: Path,
        *,
        role: str,
    ) -> os.stat_result | None:
        try:
            reject_link_chain(
                path,
                root=self.root,
                identity=path.name,
                role=role,
            )
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if is_link_or_reparse_point(info) or not stat.S_ISREG(info.st_mode):
            raise _corrupt(f"subagent {role} is not a regular file")
        return info

    def _new_receipt(
        self,
        context: SubAgentContextEvidence,
        output: SubAgentOutputDocument,
        transcript: SubAgentTranscript,
    ) -> SubAgentTranscriptReceipt:
        identity = transcript.identity
        return SubAgentTranscriptReceipt(
            transcript_ref=transcript.ref,
            transcript_checksum=transcript.transcript_checksum,
            transcript_id=transcript.transcript_id,
            invocation_id=identity.invocation_id,
            parent_run_id=identity.parent_run_id,
            child_run_id=identity.child_run_id,
            task_instance_id=identity.task_instance_id,
            attempt=identity.attempt,
            context_ref=_context_ref(identity, context.schema_version),
            context_checksum=context.context_checksum,
            output_ref=output.ref,
            output_checksum=output.output_checksum,
            storage_revision=f"bundle:{identity.transcript_id}:v3",
            committed_at=self._clock(),
            identity_checksum=identity.identity_checksum,
            schema_version=SUBAGENT_RECEIPT_SCHEMA_V3,
        )

    @staticmethod
    def _bundle_content(
        context: SubAgentContextEvidence,
        output: SubAgentOutputDocument,
        transcript: SubAgentTranscript,
        receipt: SubAgentTranscriptReceipt,
    ) -> bytes:
        payload = {
            "schema_version": _bundle_schema_for_identity(transcript.identity),
            "context": context.to_dict(),
            "output": output.to_dict(),
            "transcript": transcript.to_dict(),
            "receipt": receipt.to_dict(),
        }
        return (stable_json_dumps(payload) + "\n").encode("utf-8")

    def _enforce_sizes(
        self,
        context: SubAgentContextEvidence,
        output: SubAgentOutputDocument,
        transcript: SubAgentTranscript,
        bundle: bytes,
    ) -> None:
        sizes = {
            "context": len(stable_json_dumps(context.to_dict()).encode("utf-8")),
            "output": len(stable_json_dumps(output.to_dict()).encode("utf-8")),
            "transcript": len(stable_json_dumps(transcript.to_dict()).encode("utf-8")),
            "bundle": len(bundle),
        }
        limits = {
            "context": self.max_transcript_bytes,
            "output": self.max_output_bytes,
            "transcript": self.max_transcript_bytes,
            "bundle": self.max_bundle_bytes,
        }
        exceeded = {name: {"size": size, "limit": limits[name]} for name, size in sizes.items() if size > limits[name]}
        if exceeded:
            raise _store_error(
                "subagent_transcript_size_exceeded",
                "subagent attempt evidence exceeds configured size",
                exceeded=exceeded,
            )

    def _parent_dir(self, parent_run_id: str) -> Path:
        try:
            return resolve_artifact_descendant(
                self.root,
                parent_run_id,
                "_harness/subagents",
                field="subagent transcript parent path",
            )
        except ArtifactPathError as exc:
            raise _store_error(
                "subagent_transcript_identity_mismatch",
                "subagent transcript parent path is invalid",
            ) from exc

    def _bundle_path(self, parent_run_id: str, transcript_id: str) -> Path:
        parent = _path_segment(parent_run_id, "parent_run_id")
        transcript = _path_segment(transcript_id, "transcript_id")
        if not transcript.startswith("sat_") or len(transcript) != 68:
            raise _store_error(
                "subagent_transcript_identity_mismatch",
                "subagent transcript id is invalid",
            )
        return resolve_artifact_descendant(
            self._parent_dir(parent),
            f"{transcript}.json",
            field="subagent transcript bundle path",
        )

    def _metadata_path(self, parent_run_id: str, transcript_id: str) -> Path:
        bundle = self._bundle_path(parent_run_id, transcript_id)
        return resolve_artifact_descendant(
            bundle.parent,
            f"{transcript_id}.meta",
            field="subagent attempt descriptor path",
        )

    def _metadata_path_from_bundle(self, bundle_path: Path) -> Path:
        return resolve_artifact_descendant(
            bundle_path.parent,
            f"{bundle_path.stem}.meta",
            field="subagent attempt descriptor path",
        )

    def _lock_path(self, parent_run_id: str, transcript_id: str) -> Path:
        bundle = self._bundle_path(parent_run_id, transcript_id)
        return resolve_artifact_descendant(
            bundle.parent,
            f"_locks/{transcript_id}.lock",
            field="subagent attempt commit lock path",
        )

    def _observe(
        self,
        name: str,
        identity: SubAgentAttemptIdentity,
        *,
        receipt: SubAgentTranscriptReceipt | None = None,
        reason_code: str | None = None,
        value: float | None = None,
    ) -> None:
        record_subagent_transcript_observation(
            self._observation_sink,
            SubAgentTranscriptObservation.from_identity(
                name,
                identity,
                receipt=receipt,
                reason_code=reason_code,
                value=value,
            ),
        )

    def _observe_receipt_failure(
        self,
        name: str,
        receipt: SubAgentTranscriptReceipt,
        *,
        reason_code: str,
    ) -> None:
        record_subagent_transcript_observation(
            self._observation_sink,
            SubAgentTranscriptObservation(
                name=name,
                transcript_id=receipt.transcript_id,
                invocation_id=receipt.invocation_id,
                parent_run_id=receipt.parent_run_id,
                child_run_id=receipt.child_run_id,
                task_instance_id=receipt.task_instance_id,
                attempt=receipt.attempt,
                transcript_ref=receipt.transcript_ref,
                transcript_checksum=receipt.transcript_checksum,
                output_ref=receipt.output_ref,
                output_checksum=receipt.output_checksum,
                reason_code=reason_code,
            ),
        )


def _parse_ref(value: str, kind: str) -> tuple[str, str, str]:
    version = next(
        (
            candidate
            for candidate in ("v3",)
            if isinstance(value, str) and value.startswith(f"{kind}://{candidate}/")
        ),
        None,
    )
    if version is None:
        raise _store_error(
            "subagent_transcript_identity_mismatch",
            "subagent evidence ref is invalid",
            ref=str(value),
        )
    prefix = f"{kind}://{version}/"
    parts = value.removeprefix(prefix).split("/")
    if len(parts) != 2:
        raise _store_error(
            "subagent_transcript_identity_mismatch",
            "subagent evidence ref is invalid",
            ref=value,
        )
    try:
        parent = validate_artifact_path_segment(parts[0], field="parent_run_id")
        transcript_id = validate_artifact_path_segment(parts[1], field="transcript_id")
    except ArtifactPathError as exc:
        raise _store_error(
            "subagent_transcript_identity_mismatch",
            "subagent evidence ref path is invalid",
        ) from exc
    return version, parent, transcript_id


def _path_segment(value: str, field_name: str) -> str:
    try:
        return validate_artifact_path_segment(value, field=field_name)
    except ArtifactPathError as exc:
        raise _store_error(
            "subagent_transcript_identity_mismatch",
            "subagent transcript path identity is invalid",
            field=field_name,
        ) from exc


def _object(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _corrupt(f"subagent bundle {field_name} must be an object")
    return value


def _positive_limit(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _reason_code(exc: Exception) -> str:
    value = getattr(exc, "code", None)
    if isinstance(value, str) and value:
        return value
    return "subagent_transcript_store_unavailable"


def _is_corrupt_reason(reason_code: str) -> bool:
    return reason_code not in {
        "subagent_transcript_not_found",
        "subagent_transcript_store_unavailable",
        "subagent_transcript_size_exceeded",
    }


def _store_error(code: str, message: str, **details: Any) -> SubAgentTranscriptStoreError:
    return SubAgentTranscriptStoreError(message, code=code, details=details)


def _corrupt(
    message: str,
    *,
    code: str = "subagent_transcript_corrupt",
) -> SubAgentTranscriptCorruptError:
    return SubAgentTranscriptCorruptError(message, code=code)


__all__ = ["FilesystemSubAgentTranscriptStore"]
