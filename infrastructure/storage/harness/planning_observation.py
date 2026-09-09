"""Filesystem persistence for immutable planning-observation receipts."""

from __future__ import annotations

import json
import os
import stat
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from framework.agent.artifacts.paths import resolve_artifact_descendant
from framework.agent.artifacts.stores.errors import ArtifactStoreMetadataError
from framework.agent.artifacts.stores.fs_safety import (
    is_link_or_reparse_point,
    reject_link_chain,
    verified_atomic_create,
    verified_exclusive_file_lock,
)
from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from framework.harness.task_plan.canonical import checksum, identifier, reference
from framework.harness.task_plan.planning_metadata import (
    PlanningObservationConflictError,
    PlanningObservationCorruptError,
    PlanningObservationDescriptor,
    PlanningObservationIncompleteError,
    PlanningObservationStorageError,
)
from framework.harness.task_plan.planning_observation import (
    PlanningCallIntent,
    PlanningObservationReceipt,
    PlanningObservationRequest,
    admit_planning_call,
)
from framework.shared.json import stable_json_dumps


DEFAULT_MAX_PLANNING_RECEIPT_BYTES = 1024 * 1024
DEFAULT_MAX_PLANNING_DESCRIPTORS = 1024


class FilesystemPlanningObservationStore:
    """One input-snapshot-scoped immutable receipt and descriptor store."""

    is_durable = True

    def __init__(
        self,
        root: str | Path,
        *,
        input_snapshot: RefAuthoritySnapshot,
        max_receipt_bytes: int = DEFAULT_MAX_PLANNING_RECEIPT_BYTES,
        max_descriptors: int = DEFAULT_MAX_PLANNING_DESCRIPTORS,
    ) -> None:
        if not isinstance(input_snapshot, RefAuthoritySnapshot):
            raise TypeError("input_snapshot must be RefAuthoritySnapshot")
        if input_snapshot.phase is not RefSnapshotPhase.INPUT_ADMISSION:
            raise ValueError("planning observation store requires an input admission snapshot")
        self.root = Path(root).expanduser().resolve(strict=False)
        self.input_snapshot = input_snapshot
        self.max_receipt_bytes = _positive(max_receipt_bytes, "max_receipt_bytes")
        self.max_descriptors = _positive(max_descriptors, "max_descriptors")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            root_info = os.lstat(self.root)
        except OSError as exc:
            raise _store_error(
                "planning_observation_store_unavailable",
                "planning observation store root is unavailable",
            ) from exc
        if is_link_or_reparse_point(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise _store_error(
                "planning_observation_store_unavailable",
                "planning observation store root must be a real directory",
            )
        self._storage_root = resolve_artifact_descendant(
            self.root,
            f"snapshot_{_checksum_path_token(input_snapshot.snapshot_ref)}",
            field="planning observation snapshot storage path",
        )
        try:
            self._storage_root.mkdir(exist_ok=True)
            reject_link_chain(
                self._storage_root,
                root=self.root,
                identity=input_snapshot.snapshot_ref,
                role="planning observation snapshot directory",
            )
            storage_info = os.lstat(self._storage_root)
        except (ArtifactStoreMetadataError, OSError) as exc:
            raise _store_error(
                "planning_observation_store_unavailable",
                "planning observation snapshot directory is unavailable",
            ) from exc
        if is_link_or_reparse_point(storage_info) or not stat.S_ISDIR(storage_info.st_mode):
            raise _store_error(
                "planning_observation_store_unavailable",
                "planning observation snapshot path must be a real directory",
            )

    def reserve_call(self, request: PlanningObservationRequest, tool_call_id: str, max_calls: int, timeout_ms: int = 30000) -> PlanningCallIntent | None:
        self._require_request_scope(request)
        intent = PlanningCallIntent(request, tool_call_id, max_calls, timeout_ms)
        intent_path = self._paths(request.request_checksum)[0].with_suffix(".intent")
        lock_path = self._storage_root / "planning-admission.lock"
        try:
            with verified_exclusive_file_lock(lock_path, root=self.root, identity=self.input_snapshot.snapshot_ref):
                paths = tuple(sorted(self._storage_root.glob("po_*.intent")))
                if len(paths) >= self.max_descriptors:
                    raise _store_error("planning_observation_query_limit_exceeded", "planning intent count exceeds its bound")
                intents = tuple(self._read_intent(path) for path in paths)
                receipts = self.receipts_for_scope(request.run_id, request.stage_id, request.planner_turn_id)
                intent = admit_planning_call(intent, intents, receipts)
                if intent is None:
                    return None
                payload = (stable_json_dumps(intent.to_dict()) + "\n").encode("utf-8")
                if len(payload) > self.max_receipt_bytes:
                    raise _corrupt("planning intent exceeds its size limit")
                if not verified_atomic_create(intent_path, payload, root=self.root, identity=request.request_checksum):
                    raise _corrupt("planning intent appeared during admission")
                if self._read_intent(intent_path) != intent:
                    raise _corrupt("planning intent changed during admission")
                return intent
        except (ArtifactStoreMetadataError, OSError) as exc:
            raise _store_error("planning_observation_store_unavailable", "planning intent admission failed") from exc

    def _read_intent(self, path: Path) -> PlanningCallIntent:
        try:
            before = self._regular_stat_or_none(path, "call intent")
            if before is None or before.st_size > self.max_receipt_bytes:
                raise _corrupt("planning intent is missing or oversized")
            with path.open("rb") as handle:
                if not os.path.samestat(before, os.fstat(handle.fileno())):
                    raise _corrupt("planning intent changed while opening")
                payload = handle.read(self.max_receipt_bytes + 1)
            reject_link_chain(path, root=self.root, identity=path.name, role="planning call intent")
            after = self._regular_stat_or_none(path, "call intent")
            if after is None or not os.path.samestat(before, after) or len(payload) != before.st_size:
                raise _corrupt("planning intent changed while reading")
            intent = PlanningCallIntent.from_dict(_object(json.loads(payload)))
            self._require_request_scope(intent.request)
            if self._paths(intent.request.request_checksum)[0].with_suffix(".intent") != path:
                raise _corrupt("planning intent path differs from identity")
            if (stable_json_dumps(intent.to_dict()) + "\n").encode("utf-8") != payload:
                raise _corrupt("planning intent is not canonical")
            return intent
        except (ArtifactStoreMetadataError, OSError, ValueError, TypeError) as exc:
            raise _corrupt("planning intent is corrupt") from exc

    def save(self, receipt: PlanningObservationReceipt) -> str:
        if not isinstance(receipt, PlanningObservationReceipt):
            raise TypeError("receipt must be PlanningObservationReceipt")
        self._require_receipt_scope(receipt)
        request_checksum = receipt.request.request_checksum
        intent_path = self._paths(request_checksum)[0].with_suffix(".intent")
        if self._regular_stat_or_none(intent_path, "call intent") is not None:
            self._read_intent(intent_path).require_receipt(receipt)
        payload = self._payload_bytes(receipt)
        descriptor = PlanningObservationDescriptor.for_receipt(
            receipt,
            input_snapshot_ref=self.input_snapshot.snapshot_ref,
            payload_checksum=_bytes_checksum(payload),
            payload_size_bytes=len(payload),
        )
        metadata = (stable_json_dumps(descriptor.to_dict()) + "\n").encode("utf-8")
        if len(payload) > self.max_receipt_bytes or len(metadata) > self.max_receipt_bytes:
            raise _store_error(
                "planning_observation_receipt_size_exceeded",
                "planning observation receipt exceeds its configured size limit",
            )
        payload_path, metadata_path, lock_path = self._paths(request_checksum)
        try:
            with verified_exclusive_file_lock(
                lock_path,
                root=self.root,
                identity=request_checksum,
            ):
                existing = self.describe_request(request_checksum)
                if existing is not None:
                    if existing != descriptor:
                        raise PlanningObservationConflictError(
                            "planning observation request has different receipt content",
                            code="planning_observation_idempotency_conflict",
                        )
                    if self._read_payload(existing, payload_path) != receipt:
                        raise PlanningObservationConflictError(
                            "planning observation request has different receipt content",
                            code="planning_observation_idempotency_conflict",
                        )
                    return existing.receipt_checksum
                if not verified_atomic_create(
                    metadata_path,
                    metadata,
                    root=self.root,
                    identity=f"{request_checksum}/metadata",
                ):
                    raise _corrupt("planning observation metadata appeared during commit")
                if not verified_atomic_create(
                    payload_path,
                    payload,
                    root=self.root,
                    identity=request_checksum,
                ):
                    raise _corrupt("planning observation payload appeared during commit")
                if self._read_payload(descriptor, payload_path) != receipt:
                    raise _corrupt("planning observation receipt changed during commit")
                return receipt.receipt_checksum
        except (PlanningObservationStorageError, PlanningObservationConflictError):
            raise
        except (ArtifactStoreMetadataError, OSError) as exc:
            raise _store_error(
                "planning_observation_store_unavailable",
                "planning observation receipt commit failed",
            ) from exc

    def by_request(self, request_checksum: str) -> PlanningObservationReceipt | None:
        descriptor = self.describe_request(request_checksum)
        if descriptor is None:
            return None
        return self._read_payload(descriptor, self._paths(request_checksum)[0])

    def by_source_ref(self, source_ref: str) -> PlanningObservationReceipt | None:
        descriptor = self.describe_ref(source_ref)
        if descriptor is None:
            return None
        return self._read_payload(
            descriptor,
            self._paths(descriptor.request_checksum)[0],
        )

    def receipts_for_scope(
        self,
        run_id: str,
        stage_id: str,
        planner_turn_id: str,
    ) -> tuple[PlanningObservationReceipt, ...]:
        return tuple(
            self._read_payload(item, self._paths(item.request_checksum)[0])
            for item in self.descriptors_for_scope(run_id, stage_id, planner_turn_id)
        )

    def describe_request(
        self,
        request_checksum: str,
    ) -> PlanningObservationDescriptor | None:
        request = checksum(request_checksum, "request_checksum")
        payload_path, metadata_path, _ = self._paths(request)
        descriptor = self._describe_paths(payload_path, metadata_path)
        if descriptor is not None and descriptor.request_checksum != request:
            raise _corrupt("planning observation descriptor request identity mismatch")
        return descriptor

    def describe_ref(
        self,
        source_ref: str,
    ) -> PlanningObservationDescriptor | None:
        source = reference(source_ref, "source_ref")
        matches = tuple(
            item for item in self._all_descriptors() if item.source_ref == source
        )
        if len(matches) > 1:
            raise _corrupt("planning observation source ref is ambiguous")
        return matches[0] if matches else None

    def descriptors_for_scope(
        self,
        run_id: str,
        stage_id: str,
        planner_turn_id: str,
    ) -> tuple[PlanningObservationDescriptor, ...]:
        scope = (
            identifier(run_id, "run_id"),
            identifier(stage_id, "stage_id"),
            identifier(planner_turn_id, "planner_turn_id"),
        )
        return tuple(
            sorted(
                (
                    item
                    for item in self._all_descriptors()
                    if (item.run_id, item.stage_id, item.planner_turn_id) == scope
                ),
                key=lambda item: (item.attempt, item.request_id),
            )
        )

    def _all_descriptors(self) -> tuple[PlanningObservationDescriptor, ...]:
        metadata_paths = tuple(sorted(self._storage_root.glob("po_*.meta")))
        payload_paths = tuple(sorted(self._storage_root.glob("po_*.json")))
        if len(metadata_paths) > self.max_descriptors or len(payload_paths) > self.max_descriptors:
            raise _store_error(
                "planning_observation_query_limit_exceeded",
                "planning observation descriptor query exceeds its bound",
            )
        metadata_ids = {item.stem for item in metadata_paths}
        payload_ids = {item.stem for item in payload_paths}
        if metadata_ids != payload_ids:
            raise PlanningObservationIncompleteError(
                "planning observation store contains a half-committed receipt",
                code="planning_observation_incomplete",
            )
        return tuple(
            _require_descriptor(
                self._describe_paths(
                    self._storage_root / f"{path.stem}.json",
                    path,
                )
            )
            for path in metadata_paths
        )

    def _describe_paths(
        self,
        payload_path: Path,
        metadata_path: Path,
    ) -> PlanningObservationDescriptor | None:
        try:
            payload_stat = self._regular_stat_or_none(payload_path, "receipt payload")
            metadata_stat = self._regular_stat_or_none(metadata_path, "receipt metadata")
            if payload_stat is None and metadata_stat is None:
                return None
            if payload_stat is None or metadata_stat is None:
                raise PlanningObservationIncompleteError(
                    "planning observation receipt is half committed",
                    code="planning_observation_incomplete",
                )
            if metadata_stat.st_size > self.max_receipt_bytes:
                raise _corrupt("planning observation descriptor exceeds its size limit")
            with metadata_path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not os.path.samestat(metadata_stat, opened):
                    raise _corrupt("planning observation descriptor changed while opening")
                content = handle.read(self.max_receipt_bytes + 1)
            if len(content) > self.max_receipt_bytes:
                raise _corrupt("planning observation descriptor exceeds its size limit")
            reject_link_chain(
                metadata_path,
                root=self.root,
                identity=metadata_path.name,
                role="planning observation descriptor read",
            )
            metadata_after = os.lstat(metadata_path)
            if not os.path.samestat(metadata_stat, metadata_after):
                raise _corrupt("planning observation descriptor changed while reading")
            descriptor = PlanningObservationDescriptor.from_dict(
                _object(json.loads(content.decode("utf-8")))
            )
            self._require_descriptor_scope(descriptor)
            expected_payload, expected_metadata, _ = self._paths(
                descriptor.request_checksum
            )
            if expected_payload != payload_path or expected_metadata != metadata_path:
                raise _corrupt("planning observation descriptor path identity mismatch")
            if descriptor.payload_size_bytes != payload_stat.st_size:
                raise _corrupt("planning observation payload size mismatch")
            reject_link_chain(
                payload_path,
                root=self.root,
                identity=payload_path.name,
                role="planning observation payload metadata",
            )
            after = os.lstat(payload_path)
            if not os.path.samestat(payload_stat, after):
                raise _corrupt("planning observation payload changed during metadata read")
            return descriptor
        except PlanningObservationStorageError:
            raise
        except FileNotFoundError as exc:
            raise PlanningObservationIncompleteError(
                "planning observation receipt changed during metadata read",
                code="planning_observation_incomplete",
            ) from exc
        except (
            ArtifactStoreMetadataError,
            HarnessValidationError,
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
        ) as exc:
            raise _corrupt("planning observation descriptor is corrupt") from exc

    def _read_payload(
        self,
        descriptor: PlanningObservationDescriptor,
        path: Path,
    ) -> PlanningObservationReceipt:
        try:
            before = self._regular_stat_or_none(path, "receipt payload")
            if before is None:
                raise PlanningObservationIncompleteError(
                    "planning observation payload is missing",
                    code="planning_observation_incomplete",
                )
            if before.st_size > self.max_receipt_bytes:
                raise _corrupt("planning observation payload exceeds its size limit")
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not os.path.samestat(before, opened):
                    raise _corrupt("planning observation payload changed while opening")
                content = handle.read(self.max_receipt_bytes + 1)
            if (
                len(content) != descriptor.payload_size_bytes
                or _bytes_checksum(content) != descriptor.payload_checksum
            ):
                raise _corrupt("planning observation payload checksum mismatch")
            reject_link_chain(
                path,
                root=self.root,
                identity=path.name,
                role="planning observation payload read",
            )
            after = os.lstat(path)
            if not os.path.samestat(before, after):
                raise _corrupt("planning observation payload changed while reading")
            receipt = PlanningObservationReceipt.from_dict(
                _object(json.loads(content.decode("utf-8")))
            )
            if self._payload_bytes(receipt) != content:
                raise _corrupt("planning observation payload is not canonical")
            expected = PlanningObservationDescriptor.for_receipt(
                receipt,
                input_snapshot_ref=self.input_snapshot.snapshot_ref,
                payload_checksum=descriptor.payload_checksum,
                payload_size_bytes=descriptor.payload_size_bytes,
            )
            if expected != descriptor:
                raise _corrupt("planning observation payload differs from metadata")
            intent_path = self._paths(receipt.request.request_checksum)[0].with_suffix(".intent")
            if self._regular_stat_or_none(intent_path, "call intent") is not None:
                self._read_intent(intent_path).require_receipt(receipt)
            return receipt
        except PlanningObservationStorageError:
            raise
        except (
            ArtifactStoreMetadataError,
            HarnessValidationError,
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
        ) as exc:
            raise _corrupt("planning observation receipt payload is corrupt") from exc

    def _regular_stat_or_none(self, path: Path, role: str) -> os.stat_result | None:
        try:
            reject_link_chain(path, root=self.root, identity=path.name, role=role)
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if is_link_or_reparse_point(info) or not stat.S_ISREG(info.st_mode):
            raise _corrupt(f"planning observation {role} is not a regular file")
        return info

    def _require_receipt_scope(self, receipt: PlanningObservationReceipt) -> None:
        self._require_request_scope(receipt.request)

    def _require_request_scope(self, request: PlanningObservationRequest) -> None:
        if (
            request.run_id != self.input_snapshot.run_id
            or request.stage_id != self.input_snapshot.stage_id
            or request.policy_checksum != self.input_snapshot.task_policy_checksum
        ):
            raise PlanningObservationConflictError(
                "planning observation receipt is outside the input snapshot scope",
                code="planning_observation_scope_mismatch",
            )

    def _require_descriptor_scope(
        self,
        descriptor: PlanningObservationDescriptor,
    ) -> None:
        if (
            descriptor.input_snapshot_ref != self.input_snapshot.snapshot_ref
            or descriptor.run_id != self.input_snapshot.run_id
            or descriptor.stage_id != self.input_snapshot.stage_id
            or descriptor.policy_checksum != self.input_snapshot.task_policy_checksum
        ):
            raise _corrupt("planning observation descriptor is outside its input snapshot")

    def _paths(self, request_checksum: str) -> tuple[Path, Path, Path]:
        request = checksum(request_checksum, "request_checksum")
        identity = checksum_for(
            {
                "input_snapshot_ref": self.input_snapshot.snapshot_ref,
                "request_checksum": request,
            }
        ).removeprefix("sha256:")
        identity = urlsafe_b64encode(bytes.fromhex(identity)).decode("ascii").rstrip("=")
        stem = f"po_{identity}"
        return tuple(
            resolve_artifact_descendant(
                self._storage_root,
                f"{stem}.{suffix}",
                field="planning observation storage path",
            )
            for suffix in ("json", "meta", "lock")
        )  # type: ignore[return-value]

    @staticmethod
    def _payload_bytes(receipt: PlanningObservationReceipt) -> bytes:
        return (stable_json_dumps(receipt.to_dict()) + "\n").encode("utf-8")


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bytes_checksum(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def _checksum_path_token(value: str) -> str:
    digest = checksum(value, "path_checksum").removeprefix("sha256:")
    return urlsafe_b64encode(bytes.fromhex(digest)).decode("ascii").rstrip("=")


def _object(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("planning observation document must be an object")
    return value


def _require_descriptor(
    value: PlanningObservationDescriptor | None,
) -> PlanningObservationDescriptor:
    if value is None:
        raise PlanningObservationIncompleteError(
            "planning observation descriptor disappeared",
            code="planning_observation_incomplete",
        )
    return value


def _store_error(code: str, message: str) -> PlanningObservationStorageError:
    return PlanningObservationStorageError(message, code=code)


def _corrupt(message: str) -> PlanningObservationCorruptError:
    return PlanningObservationCorruptError(
        message,
        code="planning_observation_receipt_corrupt",
    )


__all__ = [
    "DEFAULT_MAX_PLANNING_DESCRIPTORS",
    "DEFAULT_MAX_PLANNING_RECEIPT_BYTES",
    "FilesystemPlanningObservationStore",
]
