from __future__ import annotations

import hashlib
import json
import os
import time
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from backend.research.ports.request_admission import (
    ResearchDesktopAdmission,
    ResearchDesktopAdmissionDecision,
    ResearchDesktopOperationKind,
    ResearchDesktopRequestStore,
    ResearchDesktopSavedReportRef,
)
from framework.shared.local_desktop import validate_lifecycle_timeout
from framework.shared.process_identity import current_process_identity, process_matches
from interfaces.services.json_file_store import (
    locked_json_file,
    read_json_object_unlocked,
    write_json_object_unlocked,
)

DEFAULT_DESKTOP_CONTROL_PATH = ".newsroom/local-desktop/control.json"
CONTROL_SCHEMA = "newsroom.research-desktop-control/v1"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_GENERATION_PATTERN = re.compile(r"^gen_[0-9a-f]{32}$")
_REPORT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "needs_review"})


class ResearchDesktopControlError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.details = dict(details or {})


class LocalJsonResearchDesktopRequestStore(ResearchDesktopRequestStore):
    """Durable owner-scoped admission metadata, recovery fencing, and report refs.

    This store does not own business runs or artifacts. It only records whether a
    browser request was admitted and points back to the existing Research/import
    authorities. The file lock makes admission atomic across API processes.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        configured = path or os.getenv("NEWSROOM_DESKTOP_CONTROL_PATH") or DEFAULT_DESKTOP_CONTROL_PATH
        self.path = Path(configured).expanduser().resolve()

    def generation(self) -> str:
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            self._prune_dead_leases(payload)
            self._write(resolved, payload)
            return str(payload["generation"])

    def begin_drain(self, *, timeout_seconds: float) -> dict[str, Any]:
        try:
            timeout = validate_lifecycle_timeout(
                timeout_seconds,
                field_name="drain timeout",
            )
        except ValueError as exc:
            raise ResearchDesktopControlError(
                "desktop_drain_timeout_invalid",
                "Drain timeout must be a finite number greater than 0 and at most 300 seconds",
            ) from exc
        deadline = time.monotonic() + timeout
        first = True
        while True:
            with locked_json_file(self.path) as resolved:
                payload = self._read_or_initialize(resolved)
                self._prune_dead_leases(payload)
                was_open = bool(payload.get("admissionOpen", True))
                payload["admissionOpen"] = False
                payload["drainStartedAt"] = payload.get("drainStartedAt") or _now()
                leases = payload.setdefault("activeLeases", {})
                active_count = len(leases)
                self._write(resolved, payload)
            if active_count == 0:
                return {
                    "status": "drained" if (first and was_open) else "already_drained",
                    "active_count": 0,
                    "generation": str(payload["generation"]),
                }
            if time.monotonic() >= deadline:
                return {
                    "status": "busy",
                    "active_count": active_count,
                    "generation": str(payload["generation"]),
                }
            first = False
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def resume_admission(self) -> dict[str, Any]:
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            self._prune_dead_leases(payload)
            already_open = bool(payload.get("admissionOpen", True))
            payload["admissionOpen"] = True
            payload.pop("drainStartedAt", None)
            self._write(resolved, payload)
            return {
                "status": "already_accepting" if already_open else "accepting",
                "active_count": len(payload.get("activeLeases", {})),
                "generation": str(payload["generation"]),
            }

    def admission_open(self) -> bool:
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            self._prune_dead_leases(payload)
            self._write(resolved, payload)
            return bool(payload.get("admissionOpen", True))

    def begin_untracked_execution(self, *, operation: str) -> str:
        actual_operation = _required_text(operation, "operation", maximum=256)
        lease_id = f"lease_{uuid4().hex}"
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            self._prune_dead_leases(payload)
            if not bool(payload.get("admissionOpen", True)):
                raise ResearchDesktopControlError(
                    "desktop_admission_draining",
                    "Desktop is draining and is not accepting new business operations",
                    status_code=503,
                    retryable=True,
                )
            payload.setdefault("activeLeases", {})[lease_id] = {
                "pid": os.getpid(),
                "processIdentity": current_process_identity().creation_id,
                "operation": actual_operation,
                "startedAt": _now(),
            }
            self._write(resolved, payload)
        return lease_id

    def end_untracked_execution(self, lease_id: str) -> None:
        actual_lease = _required_text(lease_id, "lease_id", maximum=128)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            leases = payload.setdefault("activeLeases", {})
            leases.pop(actual_lease, None)
            self._write(resolved, payload)

    def rotate_generation(self) -> str:
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            next_generation = _new_generation()
            now = _now()
            operations = payload.get("operations", {})
            if isinstance(operations, dict):
                for raw in operations.values():
                    if isinstance(raw, dict) and raw.get("status") not in _TERMINAL_STATUSES:
                        raw["status"] = "needs_review"
                        raw["updatedAt"] = now
                        raw["error"] = {
                            "code": "desktop_generation_rotated",
                            "message": "Request belongs to an earlier recovery generation",
                        }
            payload["generation"] = next_generation
            payload["rotatedAt"] = now
            payload["activeLeases"] = {}
            self._write(resolved, payload)
            return next_generation

    def admit(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
        kind: ResearchDesktopOperationKind,
        fingerprint: str,
        semantic: Mapping[str, Any],
    ) -> ResearchDesktopAdmissionDecision:
        owner = _required_text(owner_id, "owner_id", maximum=256)
        request = _request_id(request_id)
        actual_generation = _generation(generation)
        actual_fingerprint = _fingerprint(fingerprint)
        actual_kind = _operation_kind(kind)
        normalized_semantic = _json_mapping(semantic, "semantic")
        key = _operation_key(owner, request)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            self._prune_dead_leases(payload)
            self._require_current_generation(payload, actual_generation)
            if not bool(payload.get("admissionOpen", True)):
                raise ResearchDesktopControlError(
                    "desktop_admission_draining",
                    "Desktop is draining and is not accepting new business operations",
                    status_code=503,
                    retryable=True,
                )
            operations = payload.setdefault("operations", {})
            raw = operations.get(key)
            if isinstance(raw, dict):
                existing = _admission_from_record(raw)
                if existing.owner_id != owner:
                    raise ResearchDesktopControlError(
                        "desktop_request_not_found",
                        "Desktop request was not found",
                        status_code=404,
                    )
                if (
                    existing.generation != actual_generation
                    or existing.kind != actual_kind
                    or existing.fingerprint != actual_fingerprint
                    or dict(existing.semantic) != normalized_semantic
                ):
                    raise ResearchDesktopControlError(
                        "desktop_request_conflict",
                        "Request identity is already bound to different content",
                        status_code=409,
                        details={"requestId": request},
                    )
                return ResearchDesktopAdmissionDecision(
                    admission=existing,
                    created=False,
                    execute=False,
                )
            now = _now()
            record = {
                "ownerId": owner,
                "requestId": request,
                "generation": actual_generation,
                "kind": actual_kind,
                "fingerprint": actual_fingerprint,
                "semantic": normalized_semantic,
                "status": "running",
                "createdAt": now,
                "updatedAt": now,
            }
            operations[key] = record
            payload.setdefault("activeLeases", {})[key] = {
                "pid": os.getpid(),
                "processIdentity": current_process_identity().creation_id,
                "operation": actual_kind,
                "requestId": request,
                "startedAt": now,
            }
            self._write(resolved, payload)
            admission = _admission_from_record(record)
            return ResearchDesktopAdmissionDecision(
                admission=admission,
                created=True,
                execute=True,
            )

    def get(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
    ) -> ResearchDesktopAdmission | None:
        owner = _required_text(owner_id, "owner_id", maximum=256)
        request = _request_id(request_id)
        actual_generation = _generation(generation)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            self._prune_dead_leases(payload)
            self._write(resolved, payload)
            self._require_current_generation(payload, actual_generation)
            raw = payload.get("operations", {}).get(_operation_key(owner, request))
            if not isinstance(raw, dict):
                return None
            admission = _admission_from_record(raw)
            if admission.owner_id != owner or admission.generation != actual_generation:
                return None
            return admission

    def bind_authority(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
        run_id: str | None = None,
        import_id: str | None = None,
    ) -> ResearchDesktopAdmission:
        owner = _required_text(owner_id, "owner_id", maximum=256)
        request = _request_id(request_id)
        actual_generation = _generation(generation)
        if run_id is None and import_id is None:
            raise ResearchDesktopControlError(
                "desktop_authority_missing",
                "A durable business authority is required",
            )
        key = _operation_key(owner, request)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            self._require_current_generation(payload, actual_generation)
            raw = payload.get("operations", {}).get(key)
            if not isinstance(raw, dict) or raw.get("ownerId") != owner:
                raise ResearchDesktopControlError(
                    "desktop_request_not_found",
                    "Desktop request was not found",
                    status_code=404,
                )
            if raw.get("generation") != actual_generation:
                raise ResearchDesktopControlError(
                    "desktop_generation_stale",
                    "Request belongs to an earlier recovery generation",
                    status_code=409,
                )
            bindings = (
                ("runId", run_id, "run_id"),
                ("importId", import_id, "import_id"),
            )
            for key_name, value, field_name in bindings:
                if value is None:
                    continue
                normalized = _required_text(value, field_name, maximum=256)
                existing = raw.get(key_name)
                if existing is not None and existing != normalized:
                    raise ResearchDesktopControlError(
                        "desktop_authority_conflict",
                        "Request is already bound to a different business authority",
                        status_code=409,
                    )
                raw[key_name] = normalized
            raw["updatedAt"] = _now()
            self._write(resolved, payload)
            return _admission_from_record(raw)

    def complete(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
        run_id: str | None = None,
        import_id: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> ResearchDesktopAdmission:
        return self._transition(
            owner_id=owner_id,
            request_id=request_id,
            generation=generation,
            status="completed",
            run_id=run_id,
            import_id=import_id,
            result=result,
            error=None,
        )

    def fail(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
        error: Mapping[str, Any],
        needs_review: bool = False,
    ) -> ResearchDesktopAdmission:
        return self._transition(
            owner_id=owner_id,
            request_id=request_id,
            generation=generation,
            status="needs_review" if needs_review else "failed",
            error=_json_mapping(error, "error"),
        )

    def record_capability_verification(
        self,
        *,
        capability_id: str,
        provenance: Mapping[str, Any],
        runtime_identity: str,
    ) -> Mapping[str, Any]:
        capability = _required_text(capability_id, "capability_id", maximum=100)
        details = _json_mapping(provenance, "provenance")
        identity = _fingerprint(runtime_identity)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            record = {
                "capabilityId": capability,
                "verifiedAt": _now(),
                "generation": str(payload["generation"]),
                "runtimeIdentity": identity,
                "provenance": details,
            }
            payload.setdefault("capabilityVerifications", {})[capability] = record
            self._write(resolved, payload)
            return dict(record)

    def capability_verifications(
        self, *, runtime_identity: str
    ) -> Mapping[str, Mapping[str, Any]]:
        identity = _fingerprint(runtime_identity)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            current_generation = str(payload["generation"])
            raw = payload.get("capabilityVerifications", {})
            if not isinstance(raw, Mapping):
                return {}
            return {
                str(key): dict(value)
                for key, value in raw.items()
                if (
                    isinstance(value, Mapping)
                    and value.get("generation") == current_generation
                    and value.get("runtimeIdentity") == identity
                )
            }

    def save_report_ref(
        self,
        *,
        owner_id: str,
        report_id: str,
        run_id: str,
        paper_id: str,
        source_revision: str,
        title: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ResearchDesktopSavedReportRef:
        owner = _required_text(owner_id, "owner_id", maximum=256)
        report = _report_id(report_id)
        run = _required_text(run_id, "run_id", maximum=256)
        paper = _required_text(paper_id, "paper_id", maximum=256)
        revision = _required_text(source_revision, "source_revision", maximum=512)
        actual_title = _required_text(title, "title", maximum=500)
        actual_metadata = _json_mapping(metadata or {}, "metadata")
        key = _report_key(owner, report)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            reports = payload.setdefault("reports", {})
            now = _now()
            existing = reports.get(key)
            saved_at = str(existing.get("savedAt")) if isinstance(existing, dict) else now
            if isinstance(existing, dict) and (
                existing.get("ownerId") != owner
                or existing.get("runId") != run
                or existing.get("paperId") != paper
                or existing.get("sourceRevision") != revision
            ):
                raise ResearchDesktopControlError(
                    "desktop_report_conflict",
                    "Saved report identity is already bound to another Research result",
                    status_code=409,
                )
            record = {
                "ownerId": owner,
                "reportId": report,
                "runId": run,
                "paperId": paper,
                "sourceRevision": revision,
                "title": actual_title,
                "savedAt": saved_at,
                "updatedAt": now,
                "metadata": actual_metadata,
            }
            reports[key] = record
            self._write(resolved, payload)
            return _report_ref_from_record(record)

    def get_report_ref(
        self,
        *,
        owner_id: str,
        report_id: str,
    ) -> ResearchDesktopSavedReportRef | None:
        owner = _required_text(owner_id, "owner_id", maximum=256)
        report = _report_id(report_id)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            raw = payload.get("reports", {}).get(_report_key(owner, report))
            if not isinstance(raw, dict) or raw.get("ownerId") != owner:
                return None
            return _report_ref_from_record(raw)

    def list_report_refs(self, *, owner_id: str) -> tuple[ResearchDesktopSavedReportRef, ...]:
        owner = _required_text(owner_id, "owner_id", maximum=256)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            records = [
                _report_ref_from_record(raw)
                for raw in payload.get("reports", {}).values()
                if isinstance(raw, dict) and raw.get("ownerId") == owner
            ]
        return tuple(sorted(records, key=lambda item: (item.updated_at, item.report_id), reverse=True))

    def _transition(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
        status: str,
        run_id: str | None = None,
        import_id: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> ResearchDesktopAdmission:
        owner = _required_text(owner_id, "owner_id", maximum=256)
        request = _request_id(request_id)
        actual_generation = _generation(generation)
        key = _operation_key(owner, request)
        with locked_json_file(self.path) as resolved:
            payload = self._read_or_initialize(resolved)
            self._require_current_generation(payload, actual_generation)
            raw = payload.get("operations", {}).get(key)
            if not isinstance(raw, dict) or raw.get("ownerId") != owner:
                raise ResearchDesktopControlError(
                    "desktop_request_not_found",
                    "Desktop request was not found",
                    status_code=404,
                )
            if raw.get("generation") != actual_generation:
                raise ResearchDesktopControlError(
                    "desktop_generation_stale",
                    "Request belongs to an earlier recovery generation",
                    status_code=409,
                )
            current_status = raw.get("status")
            if current_status in {"completed", "failed"} or (
                current_status == "needs_review" and status == "needs_review"
            ):
                return _admission_from_record(raw)
            raw["status"] = status
            raw["updatedAt"] = _now()
            if run_id is not None:
                raw["runId"] = _required_text(run_id, "run_id", maximum=256)
            if import_id is not None:
                raw["importId"] = _required_text(import_id, "import_id", maximum=256)
            if result is not None:
                raw["result"] = _json_mapping(result, "result")
            if error is not None:
                raw["error"] = _json_mapping(error, "error")
            payload.setdefault("activeLeases", {}).pop(key, None)
            self._write(resolved, payload)
            return _admission_from_record(raw)

    def _read_or_initialize(self, path: Path) -> dict[str, Any]:
        raw = read_json_object_unlocked(path, default={})
        if not raw:
            payload = {
                "schema": CONTROL_SCHEMA,
                "generation": _new_generation(),
                "createdAt": _now(),
                "operations": {},
                "reports": {},
                "admissionOpen": True,
                "activeLeases": {},
            }
            self._write(path, payload)
            return payload
        if raw.get("schema") != CONTROL_SCHEMA:
            raise ResearchDesktopControlError(
                "desktop_control_schema_invalid",
                "Desktop control metadata schema is unsupported",
                status_code=500,
            )
        _generation(raw.get("generation"))
        if not isinstance(raw.get("operations", {}), dict) or not isinstance(raw.get("reports", {}), dict):
            raise ResearchDesktopControlError(
                "desktop_control_corrupt",
                "Desktop control metadata is invalid",
                status_code=500,
            )
        raw.setdefault("admissionOpen", True)
        raw.setdefault("activeLeases", {})
        raw.setdefault("capabilityVerifications", {})
        return raw

    @staticmethod
    def _prune_dead_leases(payload: dict[str, Any]) -> None:
        leases = payload.setdefault("activeLeases", {})
        if not isinstance(leases, dict):
            raise ResearchDesktopControlError(
                "desktop_control_corrupt",
                "Desktop active lease metadata is invalid",
                status_code=500,
            )
        dead = [
            key
            for key, raw in leases.items()
            if not isinstance(raw, Mapping) or not _process_alive(
                raw.get("pid"), raw.get("processIdentity")
            )
        ]
        for key in dead:
            leases.pop(key, None)
        operations = payload.get("operations", {})
        if isinstance(operations, dict):
            active_operation_keys = {
                str(key)
                for key, raw in leases.items()
                if isinstance(raw, Mapping) and raw.get("requestId")
            }
            now = _now()
            for operation_key, raw in operations.items():
                if (
                    isinstance(raw, dict)
                    and raw.get("status") == "running"
                    and str(operation_key) not in active_operation_keys
                ):
                    raw["status"] = "needs_review"
                    raw["updatedAt"] = now
                    raw["error"] = {
                        "code": "desktop_execution_interrupted",
                        "message": "The admitting process ended before a terminal result was recorded",
                    }

    @staticmethod
    def _write(path: Path, payload: Mapping[str, Any]) -> None:
        write_json_object_unlocked(path, dict(payload))

    @staticmethod
    def _require_current_generation(payload: Mapping[str, Any], generation: str) -> None:
        current = str(payload.get("generation") or "")
        if generation != current:
            raise ResearchDesktopControlError(
                "desktop_generation_stale",
                "Request belongs to an earlier recovery generation",
                status_code=409,
                details={"currentGeneration": current},
            )


def research_desktop_request_store_from_env() -> LocalJsonResearchDesktopRequestStore:
    return LocalJsonResearchDesktopRequestStore()


def canonical_request_fingerprint(semantic: Mapping[str, Any]) -> str:
    normalized = _json_mapping(semantic, "semantic")
    encoded = json.dumps(
        _canonical_json_value(normalized),
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _canonical_json_value(value[key])
            for key in sorted(value, key=_utf16_ordinal_key)
        }
    if isinstance(value, list | tuple):
        return [_canonical_json_value(item) for item in value]
    if value is None or isinstance(value, str | bool | int):
        return value
    raise ResearchDesktopControlError(
        "desktop_semantic_invalid",
        "Request semantics must contain only canonical JSON values",
    )


def _utf16_ordinal_key(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ResearchDesktopControlError(
            "desktop_semantic_invalid",
            "Request semantic keys must be strings",
        )
    return value.encode("utf-16-be", "surrogatepass")


def _admission_from_record(raw: Mapping[str, Any]) -> ResearchDesktopAdmission:
    status = str(raw.get("status") or "")
    if status not in {"admitted", "running", "completed", "failed", "needs_review"}:
        raise ResearchDesktopControlError("desktop_control_corrupt", "Desktop request status is invalid", status_code=500)
    return ResearchDesktopAdmission(
        owner_id=_required_text(raw.get("ownerId"), "ownerId", maximum=256),
        request_id=_request_id(raw.get("requestId")),
        generation=_generation(raw.get("generation")),
        kind=_operation_kind(raw.get("kind")),
        fingerprint=_fingerprint(raw.get("fingerprint")),
        semantic=_json_mapping(raw.get("semantic"), "semantic"),
        status=status,  # type: ignore[arg-type]
        created_at=_required_text(raw.get("createdAt"), "createdAt", maximum=64),
        updated_at=_required_text(raw.get("updatedAt"), "updatedAt", maximum=64),
        run_id=_optional_text(raw.get("runId"), maximum=256),
        import_id=_optional_text(raw.get("importId"), maximum=256),
        result=_optional_mapping(raw.get("result"), "result"),
        error=_optional_mapping(raw.get("error"), "error"),
    )


def _report_ref_from_record(raw: Mapping[str, Any]) -> ResearchDesktopSavedReportRef:
    return ResearchDesktopSavedReportRef(
        owner_id=_required_text(raw.get("ownerId"), "ownerId", maximum=256),
        report_id=_report_id(raw.get("reportId")),
        run_id=_required_text(raw.get("runId"), "runId", maximum=256),
        paper_id=_required_text(raw.get("paperId"), "paperId", maximum=256),
        source_revision=_required_text(raw.get("sourceRevision"), "sourceRevision", maximum=512),
        title=_required_text(raw.get("title"), "title", maximum=500),
        saved_at=_required_text(raw.get("savedAt"), "savedAt", maximum=64),
        updated_at=_required_text(raw.get("updatedAt"), "updatedAt", maximum=64),
        metadata=_json_mapping(raw.get("metadata", {}), "metadata"),
    )


def _operation_key(owner_id: str, request_id: str) -> str:
    digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    return f"{digest}:{request_id}"


def _report_key(owner_id: str, report_id: str) -> str:
    digest = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    return f"{digest}:{report_id}"


def _new_generation() -> str:
    return f"gen_{uuid4().hex}"


def _generation(value: Any) -> str:
    text = str(value or "").strip()
    if not _GENERATION_PATTERN.fullmatch(text):
        raise ResearchDesktopControlError("desktop_generation_invalid", "Desktop recovery generation is invalid")
    return text


def _request_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _REQUEST_ID_PATTERN.fullmatch(text):
        raise ResearchDesktopControlError("desktop_request_id_invalid", "requestId must be 8-128 safe characters")
    return text


def _report_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _REPORT_ID_PATTERN.fullmatch(text):
        raise ResearchDesktopControlError("desktop_report_id_invalid", "reportId must be 4-128 safe characters")
    return text


def _fingerprint(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not _FINGERPRINT_PATTERN.fullmatch(text):
        raise ResearchDesktopControlError("desktop_fingerprint_invalid", "requestFingerprint must be sha256:<hex>")
    return text


def _operation_kind(value: Any) -> ResearchDesktopOperationKind:
    text = str(value or "").strip()
    if text not in {"prepare", "analyze", "ask", "import"}:
        raise ResearchDesktopControlError("desktop_operation_invalid", "Desktop operation kind is invalid")
    return text  # type: ignore[return-value]


def _required_text(value: Any, field: str, *, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 0x20 for char in text):
        raise ResearchDesktopControlError("desktop_control_invalid", f"{field} is invalid")
    return text


def _optional_text(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, "value", maximum=maximum)


def _json_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDesktopControlError("desktop_control_invalid", f"{field} must be an object")
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ResearchDesktopControlError("desktop_control_invalid", f"{field} must be JSON serializable") from exc
    if not isinstance(decoded, dict):
        raise ResearchDesktopControlError("desktop_control_invalid", f"{field} must be an object")
    return decoded


def _optional_mapping(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _json_mapping(value, field)


def _process_alive(value: Any, creation_id: Any) -> bool:
    return process_matches(value, creation_id)

def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "CONTROL_SCHEMA",
    "DEFAULT_DESKTOP_CONTROL_PATH",
    "LocalJsonResearchDesktopRequestStore",
    "ResearchDesktopControlError",
    "canonical_request_fingerprint",
    "research_desktop_request_store_from_env",
]
